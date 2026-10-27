package nl.foxway.deurbel.network

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.asSharedFlow
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.MainActivity
import nl.foxway.deurbel.R
import nl.foxway.deurbel.data.models.DoorbellEvent
import nl.foxway.deurbel.data.models.EventItemDto
import nl.foxway.deurbel.telecom.DoorbellConnectionService
import java.net.HttpURLConnection
import java.net.URL

/**
 * Foreground service that keeps the persistent WebSocket connection alive
 * and maintains a 3-second fallback polling loop for reliable event delivery (Task 6).
 *
 * Implements:
 *  - Persistent WebSocket connection with automatic reconnect
 *  - 3-second lightweight fallback polling on /api/v1/events?since_id=...
 *  - Screen WakeLock & Full-screen incoming call UI on "ring"
 *  - High-priority Android notification with BigPictureStyle snapshot on "motion"
 */
class WebSocketForegroundService : Service() {

    companion object {
        private const val TAG = "WsForegroundService"
        private const val NOTIFICATION_ID = 9001

        fun start(context: Context) {
            val intent = Intent(context, WebSocketForegroundService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, WebSocketForegroundService::class.java)
            context.stopService(intent)
        }
    }

    private lateinit var persistentWebSocket: PersistentWebSocket
    private lateinit var endpointResolver: EndpointResolver
    private var eventCollectorJob: Job? = null
    private var pollJob: Job? = null
    private var lastEventId: Long = 0L
    private var isFirstPoll: Boolean = true
    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "Service created")

        val prefs = FoxwayApp.instance.preferencesManager
        endpointResolver = EndpointResolver(prefs)
        persistentWebSocket = PersistentWebSocket(prefs, endpointResolver)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())

        // Start WebSocket
        persistentWebSocket.start()

        // Collect WebSocket events
        eventCollectorJob?.cancel()
        eventCollectorJob = serviceScope.launch {
            persistentWebSocket.events.collect { event ->
                handleWebSocketEvent(event)
            }
        }

        // Start 3-second polling fallback loop (Task 6)
        pollJob?.cancel()
        pollJob = serviceScope.launch(Dispatchers.IO) {
            while (isActive) {
                try {
                    pollEvents()
                } catch (e: Exception) {
                    Log.w(TAG, "Event poll failed: ${e.message}")
                }
                delay(3000L)
            }
        }

        Log.i(TAG, "Service started, WebSocket & 3s Polling Fallback active")
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        eventCollectorJob?.cancel()
        pollJob?.cancel()
        persistentWebSocket.stop()
        serviceScope.cancel()
        Log.i(TAG, "Service destroyed")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ── 3-Second Fallback Polling (Task 6) ───────────────────────────

    private suspend fun pollEvents() {
        val resolved = endpointResolver.resolve()
        val api = ApiClientFactory.createApi(resolved.apiBaseUrl)

        if (isFirstPoll) {
            // First time running: fetch latest event ID to avoid replaying old historical events
            try {
                val initialResp = api.getEvents(sinceId = 0, limit = 1)
                if (initialResp.isSuccessful && initialResp.body() != null) {
                    val events = initialResp.body()!!.events
                    if (events.isNotEmpty()) {
                        lastEventId = events.maxOf { it.id }
                    }
                }
            } catch (_: Exception) {}
            isFirstPoll = false
            return
        }

        val resp = api.getEvents(sinceId = lastEventId, limit = 20)
        if (resp.isSuccessful && resp.body() != null) {
            val events = resp.body()!!.events
            for (item in events) {
                if (item.id > lastEventId) {
                    lastEventId = item.id
                    processEventItem(item)
                }
            }
        }
    }

    private suspend fun processEventItem(item: EventItemDto) {
        Log.i(TAG, "Processing event: id=${item.id}, type=${item.type}, cat=${item.category}")
        when (item.type.lowercase()) {
            "ring" -> {
                handleRinging(
                    callId = item.clipId.ifBlank { "ring_${item.id}" },
                    snapshotUrl = item.snapshotUrl
                )
            }
            "motion" -> {
                handleMotionNotification(item)
            }
        }
    }

    // ── WebSocket Event handling ─────────────────────────────────────

    private suspend fun handleWebSocketEvent(event: DoorbellEvent) {
        Log.i(TAG, "Handling WS event: ${event.event}")
        when (event.event) {
            DoorbellEvent.CALL_RINGING -> {
                val callId = event.callId ?: return
                val snapshotUrl = event.snapshotUrl ?: ""
                handleRinging(callId, snapshotUrl)
            }
            DoorbellEvent.CALL_CLAIMED -> handleClaimed(event)
            DoorbellEvent.CALL_MISSED -> handleMissed(event)
            DoorbellEvent.CALL_ENDED -> handleEnded(event)
            DoorbellEvent.USER_JOINED -> handleUserJoined(event)
        }
    }

    private fun handleRinging(callId: String, snapshotUrl: String) {
        Log.i(TAG, "CALL_RINGING triggered: callId=$callId")

        // 1. Acquire WakeLock to turn the screen on
        try {
            val powerManager = getSystemService(Context.POWER_SERVICE) as? PowerManager
            val wakeLock = powerManager?.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "Foxway:IncomingCallWakeLock"
            )
            wakeLock?.acquire(30_000L) // 30s timeout
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire WakeLock: ${e.message}")
        }

        nl.foxway.deurbel.data.AppLogger.getInstance(this)
            .call("Inkomende oproep van deurbel (ID: ${callId.take(8)})")

        // 2. Start full-screen incoming call UI & pre-warm live stream
        LiveStreamController.getInstance(this).start()
        DoorbellConnectionService.startIncomingCall(this, callId, snapshotUrl)
        CallEventBus.postEvent(DoorbellEvent(event = DoorbellEvent.CALL_RINGING, callId = callId, snapshotUrl = snapshotUrl))
    }

    private suspend fun handleMotionNotification(event: EventItemDto) {
        withContext(Dispatchers.IO) {
            try {
                val resolved = endpointResolver.resolve()
                val fullSnapshotUrl = if (event.snapshotUrl.startsWith("http")) {
                    event.snapshotUrl
                } else if (event.snapshotUrl.isNotBlank()) {
                    "${resolved.apiBaseUrl}${event.snapshotUrl}"
                } else null

                val bitmap = fullSnapshotUrl?.let { urlStr ->
                    try {
                        val conn = URL(urlStr).openConnection() as HttpURLConnection
                        conn.connectTimeout = 3000
                        conn.readTimeout = 3000
                        conn.inputStream.use { input ->
                            BitmapFactory.decodeStream(input)
                        }
                    } catch (_: Exception) {
                        null
                    }
                }

                val intent = Intent(this@WebSocketForegroundService, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                    putExtra("screen", "clips")
                    putExtra("clip_id", event.clipId ?: "")
                    putExtra("event_id", event.id)
                }
                val pendingIntent = PendingIntent.getActivity(
                    this@WebSocketForegroundService,
                    event.id.toInt(),
                    intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )

                val notifBuilder = NotificationCompat.Builder(this@WebSocketForegroundService, FoxwayApp.CHANNEL_MOTION)
                    .setSmallIcon(android.R.drawable.ic_menu_camera)
                    .setContentTitle(event.title.ifBlank { "Beweging gedetecteerd" })
                    .setContentText("Beweging waargenomen bij de voordeur")
                    .setPriority(NotificationCompat.PRIORITY_HIGH)
                    .setAutoCancel(true)
                    .setContentIntent(pendingIntent)

                if (bitmap != null) {
                    notifBuilder.setStyle(
                        NotificationCompat.BigPictureStyle()
                            .bigPicture(bitmap)
                            .setSummaryText(event.title)
                    )
                }

                val notificationManager = getSystemService(NotificationManager::class.java)
                notificationManager?.notify(event.id.toInt(), notifBuilder.build())
            } catch (e: Exception) {
                Log.w(TAG, "Error showing motion notification: ${e.message}")
            }
        }
    }

    private fun handleClaimed(event: DoorbellEvent) {
        val answeredBy = event.answeredBy ?: "iemand"
        Log.i(TAG, "CALL_CLAIMED by $answeredBy")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Oproep beantwoord door $answeredBy")
        CallEventBus.postEvent(event)
    }

    private fun handleMissed(event: DoorbellEvent) {
        val callId = event.callId ?: ""
        Log.i(TAG, "CALL_MISSED: $callId")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Gemiste oproep")
        CallEventBus.postEvent(event)

        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("screen", "clips")
            putExtra("clip_id", callId)
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            callId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notif = NotificationCompat.Builder(this, FoxwayApp.CHANNEL_INFO)
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setContentTitle("Gemiste deurbel oproep")
            .setContentText("Er is aangebeld bij de voordeur. Tik om de opname te bekijken.")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()
        getSystemService(NotificationManager::class.java)?.notify(callId.hashCode(), notif)
    }

    private fun handleEnded(event: DoorbellEvent) {
        Log.i(TAG, "CALL_ENDED: ${event.callId}")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Gesprek beëindigd")
        CallEventBus.postEvent(event)
    }

    private suspend fun handleUserJoined(event: DoorbellEvent) {
        val name = event.userName ?: "Gezinslid"
        val currentUserId = FoxwayApp.instance.preferencesManager.getUserId()
        if (event.userId != currentUserId) {
            Log.i(TAG, "USER_JOINED: $name (${event.userId})")
            nl.foxway.deurbel.data.AppLogger.getInstance(this).info("👤 $name is verbonden met de Foxway Deurbel")
            showUserJoinedNotification(name)
        }
        CallEventBus.postEvent(event)
    }

    private fun showUserJoinedNotification(userName: String) {
        val notificationManager = getSystemService(NotificationManager::class.java)
        val notification = NotificationCompat.Builder(this, FoxwayApp.CHANNEL_INFO)
            .setSmallIcon(android.R.drawable.ic_menu_myplaces)
            .setContentTitle("Foxway Deurbel")
            .setContentText("👤 $userName is verbonden met de Foxway Deurbel")
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setTimeoutAfter(8000)
            .build()
        notificationManager?.notify(9005, notification)
    }

    // ── Service Notification ─────────────────────────────────────────

    private fun buildNotification(): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, FoxwayApp.CHANNEL_SERVICE)
            .setContentTitle("Foxway Deurbel")
            .setContentText("Achtergrondverbinding actief")
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }
}

// ──────────────────────────────────────────────────────────────────
//  Simple event bus for cross-layer communication
// ──────────────────────────────────────────────────────────────────

/**
 * In-process event bus using SharedFlow for delivering doorbell events
 * from the WebSocket/FCM service layer to the UI/Telecom layer.
 */
object CallEventBus {
    private val _events = kotlinx.coroutines.flow.MutableSharedFlow<DoorbellEvent>(
        replay = 1,
        extraBufferCapacity = 8
    )
    val events: kotlinx.coroutines.flow.SharedFlow<DoorbellEvent> = _events.asSharedFlow()

    fun postEvent(event: DoorbellEvent) {
        _events.tryEmit(event)
    }
}
