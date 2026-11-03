package nl.foxway.deurbel

import android.app.Activity
import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import nl.foxway.deurbel.data.PreferencesManager
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foxway Deurbel Application class v2.3.0.
 *
 * Initialises notification channels, telecom phone account, and robust crash logging.
 */
class FoxwayApp : Application() {

    lateinit var preferencesManager: PreferencesManager
        private set

    private var currentScreenName: String = "AppStart"

    val crashCoroutineExceptionHandler = CoroutineExceptionHandler { _, throwable ->
        Log.e(TAG, "Uncaught Coroutine Exception: ${throwable.message}", throwable)
        recordAndUploadCrash(throwable, "CoroutineExceptionHandler ($currentScreenName)")
    }

    val applicationScope = kotlinx.coroutines.CoroutineScope(
        kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.IO + crashCoroutineExceptionHandler
    )

    override fun onCreate() {
        super.onCreate()
        instance = this
        preferencesManager = PreferencesManager(this)

        setupCrashHandler()
        registerScreenTracker()
        createNotificationChannels()
        nl.foxway.deurbel.telecom.DoorbellConnectionService.registerPhoneAccount(this)

        applicationScope.launch {
            checkAndUploadPendingCrash()
            try {
                if (preferencesManager.isPaired.first()) {
                    nl.foxway.deurbel.network.WebSocketForegroundService.start(this@FoxwayApp)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Could not auto-start background service: ${e.message}")
            }
            syncFcmToken()
        }
    }

    /**
     * Proactively fetch and synchronize the FCM registration token with the backend.
     * Ensures high-priority wakeup pushes always reach this device even after reboot/sleep.
     */
    fun syncFcmToken() {
        applicationScope.launch {
            try {
                FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
                    if (task.isSuccessful && !task.result.isNullOrBlank()) {
                        val token = task.result
                        applicationScope.launch {
                            try {
                                preferencesManager.saveFcmToken(token)
                                val userId = preferencesManager.getUserId()
                                if (userId.isNotBlank()) {
                                    val resolver = nl.foxway.deurbel.network.EndpointResolver(preferencesManager)
                                    val resolved = resolver.resolve()
                                    val api = nl.foxway.deurbel.network.ApiClientFactory.createApi(resolved.apiBaseUrl)
                                    val response = api.updateFcmToken(
                                        userId,
                                        nl.foxway.deurbel.data.models.FcmTokenUpdate(token)
                                    )
                                    if (response.isSuccessful) {
                                        Log.i(TAG, "FCM token successfully synced to backend for user: $userId")
                                    } else {
                                        Log.w(TAG, "FCM token sync response code: ${response.code()}")
                                    }
                                }
                            } catch (e: Exception) {
                                Log.w(TAG, "Failed uploading FCM token to backend: ${e.message}")
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "FirebaseMessaging getInstance failed: ${e.message}")
            }
        }
    }

    private fun registerScreenTracker() {
        registerActivityLifecycleCallbacks(object : ActivityLifecycleCallbacks {
            override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
            override fun onActivityStarted(activity: Activity) {}
            override fun onActivityResumed(activity: Activity) {
                currentScreenName = activity.javaClass.simpleName
            }
            override fun onActivityPaused(activity: Activity) {}
            override fun onActivityStopped(activity: Activity) {}
            override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}
            override fun onActivityDestroyed(activity: Activity) {}
        })
    }

    private fun setupCrashHandler() {
        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                Log.e(TAG, "FATAL CRASH on thread ${thread.name}: ${throwable.message}", throwable)
                recordAndUploadCrash(throwable, "UncaughtException: ${thread.name} ($currentScreenName)")
            } catch (e: Exception) {
                Log.e(TAG, "Error in crash handler: ${e.message}", e)
            } finally {
                defaultHandler?.uncaughtException(thread, throwable)
            }
        }
    }

    private fun recordAndUploadCrash(throwable: Throwable, screenContext: String) {
        val crashFile = File(filesDir, CRASH_FILE_NAME)
        val stacktrace = Log.getStackTraceString(throwable)
        val payload = JSONObject().apply {
            put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("os_version", "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            put("error_message", throwable.message ?: throwable.javaClass.name)
            put("stacktrace", stacktrace)
            put("screen_state", screenContext)
            put("app_version", BuildConfig.VERSION_NAME)
            put("timestamp", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US).format(Date()))
        }

        // 1. Write synchronously to local file
        try {
            crashFile.writeText(payload.toString())
        } catch (e: Exception) {
            Log.e(TAG, "Failed writing crash to local file: ${e.message}")
        }

        // 2. Best-effort synchronous POST to server (max 2 seconds)
        try {
            val thread = Thread {
                postCrashToServer(payload.toString())
            }
            thread.start()
            thread.join(2000)
        } catch (_: Exception) {}
    }

    private fun checkAndUploadPendingCrash() {
        try {
            val crashFile = File(filesDir, CRASH_FILE_NAME)
            if (crashFile.exists()) {
                val jsonStr = crashFile.readText()
                if (jsonStr.isNotBlank()) {
                    val success = postCrashToServer(jsonStr)
                    if (success) {
                        crashFile.delete()
                        Log.i(TAG, "Pending crash log uploaded and removed")
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed checking pending crash: ${e.message}")
        }
    }

    private fun postCrashToServer(jsonBody: String): Boolean {
        val targets = listOf(
            "http://192.168.2.1:8090/api/v1/logs/client-crash",
            "https://deurbel.foxway.nl/api/v1/logs/client-crash"
        )
        for (urlStr in targets) {
            try {
                val url = URL(urlStr)
                val conn = url.openConnection() as HttpURLConnection
                conn.connectTimeout = 2500
                conn.readTimeout = 2500
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "application/json")
                conn.doOutput = true
                conn.outputStream.use { os ->
                    os.write(jsonBody.toByteArray(Charsets.UTF_8))
                }
                val code = conn.responseCode
                conn.disconnect()
                if (code in 200..299) {
                    return true
                }
            } catch (_: Exception) {
                // Try next endpoint
            }
        }
        return false
    }

    /**
     * Create notification channels required for incoming call alerts, motion detections,
     * and the persistent WebSocket foreground service.
     */
    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java) ?: return

        // High-priority ring channel — fullScreenIntent triggers here
        val ringChannel = NotificationChannel(
            CHANNEL_RING,
            getString(R.string.channel_ring_name),
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = getString(R.string.channel_ring_description)
            setBypassDnd(true)
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 400, 200, 400, 200, 400)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            setSound(null, null) // We play custom ringtone via MediaPlayer
        }
        manager.createNotificationChannel(ringChannel)

        // Min-priority service channel — completely silent and minimized
        val serviceChannel = NotificationChannel(
            CHANNEL_SERVICE,
            getString(R.string.channel_service_name),
            NotificationManager.IMPORTANCE_MIN
        ).apply {
            description = getString(R.string.channel_service_description)
            setShowBadge(false)
            enableLights(false)
            enableVibration(false)
        }
        manager.createNotificationChannel(serviceChannel)

        // General info channel
        val infoChannel = NotificationChannel(
            CHANNEL_INFO,
            "Informatie",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Algemene meldingen"
        }
        manager.createNotificationChannel(infoChannel)

        // High-importance motion & AI detection channel
        val motionChannel = NotificationChannel(
            CHANNEL_MOTION,
            "Beweging & AI Detectie",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Meldingen bij gedetecteerde personen, voertuigen en dieren met momentopname"
            enableVibration(true)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        manager.createNotificationChannel(motionChannel)
    }

    companion object {
        private const val TAG = "FoxwayApp"
        private const val CRASH_FILE_NAME = "crash_last.json"

        const val CHANNEL_RING = "foxway_doorbell_ring"
        const val CHANNEL_MOTION = "foxway_doorbell_motion"
        const val CHANNEL_SERVICE = "foxway_doorbell_service"
        const val CHANNEL_INFO = "foxway_doorbell_info"

        lateinit var instance: FoxwayApp
            private set
    }
}
