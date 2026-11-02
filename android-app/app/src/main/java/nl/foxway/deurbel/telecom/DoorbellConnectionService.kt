package nl.foxway.deurbel.telecom

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.telecom.*
import android.util.Log
import kotlinx.coroutines.*
import android.app.NotificationManager
import android.app.PendingIntent
import androidx.core.app.NotificationCompat
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.R
import nl.foxway.deurbel.data.models.ClaimRequest
import nl.foxway.deurbel.data.models.DoorbellEvent
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.CallEventBus
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.ui.call.CallActivity
import nl.foxway.deurbel.webrtc.PrewarmEngine

/**
 * Android Telecom ConnectionService for Foxway Doorbell.
 *
 * Implements Android Telecom Framework with PROPERTY_SELF_MANAGED:
 *  - Treats incoming doorbell rings as genuine phone calls (like Arlo & WhatsApp).
 *  - Opens full-screen over the lockscreen / sleep mode.
 *  - Automatically alerts connected smartwatches (Galaxy Watch, WearOS) and Android Auto.
 *  - Manages ringtone, vibration, and call states (Answer, Reject, Disconnect).
 */
class DoorbellConnectionService : ConnectionService() {

    companion object {
        private const val TAG = "DoorbellConnService"
        const val EXTRA_CALL_ID = "nl.foxway.deurbel.CALL_ID"
        const val EXTRA_SNAPSHOT_URL = "nl.foxway.deurbel.SNAPSHOT_URL"
        private const val ACCOUNT_ID = "foxway_doorbell_telecom"

        var currentConnection: DoorbellConnection? = null
        private var lastCallTime: Long = 0L
        private var lastCallId: String = ""

        fun getPhoneAccountHandle(context: Context): PhoneAccountHandle {
            val componentName = ComponentName(context, DoorbellConnectionService::class.java)
            return PhoneAccountHandle(componentName, ACCOUNT_ID)
        }

        fun registerPhoneAccount(context: Context) {
            try {
                val telecomManager = context.getSystemService(Context.TELECOM_SERVICE) as? TelecomManager ?: return
                val handle = getPhoneAccountHandle(context)
                val phoneAccount = PhoneAccount.builder(handle, "Foxway Deurbel")
                    .setCapabilities(
                        PhoneAccount.CAPABILITY_SELF_MANAGED or
                                PhoneAccount.CAPABILITY_SUPPORTS_VIDEO_CALLING
                    )
                    .setIcon(Icon.createWithResource(context, R.drawable.ic_launcher))
                    .setHighlightColor(0xFF10B981.toInt())
                    .setShortDescription("Foxway Deurbel Oproep")
                    .addSupportedUriScheme("tel")
                    .build()
                telecomManager.registerPhoneAccount(phoneAccount)
                Log.i(TAG, "PhoneAccount registered successfully with TelecomManager")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to register PhoneAccount", e)
            }
        }

        const val NOTIFICATION_ID_RING = 8008
        const val ACTION_DECLINE_CALL = "nl.foxway.deurbel.ACTION_DECLINE_CALL"

        fun showIncomingCallNotification(context: Context, callId: String, snapshotUrl: String) {
            try {
                if (CallActivity.isCallActivityVisible) {
                    Log.i(TAG, "CallActivity is already visible in foreground — suppressing duplicate heads-up notification banner")
                    cancelCallNotification(context)
                    return
                }
                val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return

                val fullScreenIntent = Intent(context, CallActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                            Intent.FLAG_ACTIVITY_CLEAR_TOP or
                            Intent.FLAG_ACTIVITY_SINGLE_TOP
                    putExtra(CallActivity.EXTRA_CALL_ID, callId)
                    putExtra(CallActivity.EXTRA_IS_ACTIVE, false)
                    putExtra(CallActivity.EXTRA_SNAPSHOT_URL, snapshotUrl)
                }
                val fullScreenPendingIntent = PendingIntent.getActivity(
                    context,
                    callId.hashCode(),
                    fullScreenIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )

                val answerIntent = Intent(context, CallActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                            Intent.FLAG_ACTIVITY_CLEAR_TOP or
                            Intent.FLAG_ACTIVITY_SINGLE_TOP
                    putExtra(CallActivity.EXTRA_CALL_ID, callId)
                    putExtra(CallActivity.EXTRA_IS_ACTIVE, true)
                    putExtra(CallActivity.EXTRA_SNAPSHOT_URL, snapshotUrl)
                }
                val answerPendingIntent = PendingIntent.getActivity(
                    context,
                    callId.hashCode() + 1,
                    answerIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )

                val declineIntent = Intent(context, CallActionReceiver::class.java).apply {
                    action = ACTION_DECLINE_CALL
                    putExtra(EXTRA_CALL_ID, callId)
                }
                val declinePendingIntent = PendingIntent.getBroadcast(
                    context,
                    callId.hashCode() + 2,
                    declineIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )

                val notifBuilder = NotificationCompat.Builder(context, FoxwayApp.CHANNEL_RING)
                    .setSmallIcon(R.drawable.ic_stat_foxway_house_bell)
                    .setContentTitle("Inkomende deurbel oproep")
                    .setContentText("Er staat iemand bij de voordeur")
                    .setCategory(NotificationCompat.CATEGORY_CALL)
                    .setPriority(NotificationCompat.PRIORITY_MAX)
                    .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                    .setOngoing(true)
                    .setAutoCancel(true)
                    .setContentIntent(fullScreenPendingIntent)
                    .setFullScreenIntent(fullScreenPendingIntent, true)
                    .addAction(R.drawable.ic_launcher, "Opnemen", answerPendingIntent)
                    .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Weigeren", declinePendingIntent)

                notificationManager.notify(NOTIFICATION_ID_RING, notifBuilder.build())
                Log.i(TAG, "Posted incoming call notification with fullScreenIntent")
            } catch (e: Exception) {
                Log.e(TAG, "Error posting fullScreenIntent notification", e)
            }
        }

        fun cancelCallNotification(context: Context) {
            try {
                val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                notificationManager?.cancel(NOTIFICATION_ID_RING)
            } catch (e: Exception) {
                Log.w(TAG, "Error cancelling call notification: ${e.message}")
            }
        }

        fun startIncomingCall(context: Context, callId: String, snapshotUrl: String) {
            val now = System.currentTimeMillis()
            // Deduplication (TAAK 4): Prevent rapid duplicate incoming call flashes within 10s
            if (callId.isNotBlank() && callId == lastCallId && (now - lastCallTime) < 10000L) {
                Log.i(TAG, "startIncomingCall duplicate ignored for callId=$callId (delta=${now - lastCallTime}ms)")
                return
            }
            if (CallActivity.isCallActivityVisible && (now - lastCallTime) < 5000L) {
                Log.i(TAG, "startIncomingCall ignored: CallActivity already active in foreground")
                return
            }
            lastCallTime = now
            lastCallId = callId
            Log.i(TAG, "startIncomingCall triggered: callId=$callId")
            // 1. Post high-priority full-screen intent notification immediately
            // Bypasses Android 10-14 background activity launch restrictions and wakes screen
            showIncomingCallNotification(context, callId, snapshotUrl)

            try {
                registerPhoneAccount(context)
                val telecomManager = context.getSystemService(Context.TELECOM_SERVICE) as? TelecomManager
                if (telecomManager == null) {
                    Log.w(TAG, "TelecomManager unavailable, falling back to direct CallActivity")
                    launchCallActivity(context, callId, snapshotUrl, false)
                    return
                }

                val handle = getPhoneAccountHandle(context)
                val extras = Bundle().apply {
                    putString(EXTRA_CALL_ID, callId)
                    putString(EXTRA_SNAPSHOT_URL, snapshotUrl)
                    putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle)
                    putParcelable(
                        TelecomManager.EXTRA_INCOMING_CALL_ADDRESS,
                        Uri.fromParts("tel", "Voordeur", null)
                    )
                }

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    telecomManager.addNewIncomingCall(handle, extras)
                    Log.i(TAG, "addNewIncomingCall successfully submitted to Android Telecom")
                } else {
                    launchCallActivity(context, callId, snapshotUrl, false)
                }
            } catch (e: Exception) {
                Log.e(TAG, "addNewIncomingCall failed, launching CallActivity fallback", e)
                launchCallActivity(context, callId, snapshotUrl, false)
            }
        }

        fun launchCallActivity(context: Context, callId: String, snapshotUrl: String, isActive: Boolean) {
            val intent = Intent(context, CallActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra(CallActivity.EXTRA_CALL_ID, callId)
                putExtra(CallActivity.EXTRA_IS_ACTIVE, isActive)
                putExtra(CallActivity.EXTRA_SNAPSHOT_URL, snapshotUrl)
            }
            context.startActivity(intent)
        }

        fun notifyAnswered() {
            cancelCallNotification(FoxwayApp.instance)
            currentConnection?.let { conn ->
                Log.i(TAG, "notifyAnswered called from UI")
                conn.stopRingtone()
                conn.setActive()
            }
        }

        fun resetDeduplication() {
            lastCallTime = 0L
            lastCallId = ""
            CallActivity.isCallActivityVisible = false
        }

        fun notifyRejected() {
            resetDeduplication()
            cancelCallNotification(FoxwayApp.instance)
            currentConnection?.let { conn ->
                Log.i(TAG, "notifyRejected called from UI")
                conn.stopRingtone()
                conn.setDisconnected(DisconnectCause(DisconnectCause.REJECTED))
                conn.destroy()
                if (currentConnection == conn) currentConnection = null
            }
        }

        fun notifyDisconnected() {
            resetDeduplication()
            cancelCallNotification(FoxwayApp.instance)
            currentConnection?.let { conn ->
                Log.i(TAG, "notifyDisconnected called from UI")
                conn.stopRingtone()
                conn.setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
                conn.destroy()
                if (currentConnection == conn) currentConnection = null
            }
        }

        fun silenceRingtone() {
            cancelCallNotification(FoxwayApp.instance)
            currentConnection?.let { conn ->
                Log.i(TAG, "silenceRingtone called from UI")
                conn.stopRingtone()
            }
        }

        fun notifyDismissed() {
            resetDeduplication()
            cancelCallNotification(FoxwayApp.instance)
            currentConnection?.let { conn ->
                Log.i(TAG, "notifyDismissed called")
                conn.stopRingtone()
                conn.setDisconnected(DisconnectCause(DisconnectCause.CANCELED))
                conn.destroy()
                if (currentConnection == conn) currentConnection = null
            }
        }
    }

    override fun onCreateIncomingConnection(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest?
    ): Connection {
        val extras = request?.extras ?: Bundle()
        val callId = extras.getString(EXTRA_CALL_ID) ?: ""
        val snapshotUrl = extras.getString(EXTRA_SNAPSHOT_URL) ?: ""
        Log.i(TAG, "onCreateIncomingConnection: callId=$callId")

        val connection = DoorbellConnection(this, callId, snapshotUrl)
        connection.connectionCapabilities = Connection.CAPABILITY_SUPPORT_HOLD or
                Connection.CAPABILITY_MUTE or
                Connection.CAPABILITY_SUPPORTS_VT_LOCAL_BIDIRECTIONAL or
                Connection.CAPABILITY_SUPPORTS_VT_REMOTE_BIDIRECTIONAL
        connection.connectionProperties = Connection.PROPERTY_SELF_MANAGED
        connection.audioModeIsVoip = true
        connection.setCallerDisplayName("Voordeur / Foxway Deurbel", TelecomManager.PRESENTATION_ALLOWED)
        connection.setAddress(Uri.fromParts("tel", "Voordeur", null), TelecomManager.PRESENTATION_ALLOWED)

        connection.setInitializing()
        connection.setRinging()
        connection.setInitialized()

        currentConnection = connection

        // Start ringtone & vibration for incoming doorbell call
        connection.startRingtone()

        // Start LiveStreamController pre-warm immediately
        nl.foxway.deurbel.network.LiveStreamController.getInstance(this).start()

        // Launch full-screen CallActivity over lockscreen only if not already visible
        if (!CallActivity.isCallActivityVisible) {
            launchCallActivity(this, callId, snapshotUrl, false)
        }

        return connection
    }

    override fun onCreateIncomingConnectionFailed(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest?
    ) {
        super.onCreateIncomingConnectionFailed(connectionManagerPhoneAccount, request)
        Log.e(TAG, "onCreateIncomingConnectionFailed - activating fallback CallActivity")
        val extras = request?.extras ?: Bundle()
        val callId = extras.getString(EXTRA_CALL_ID) ?: ""
        val snapshotUrl = extras.getString(EXTRA_SNAPSHOT_URL) ?: ""
        nl.foxway.deurbel.network.LiveStreamController.getInstance(this).start()
        if (!CallActivity.isCallActivityVisible) {
            launchCallActivity(this, callId, snapshotUrl, false)
        }
    }

    class DoorbellConnection(
        private val service: DoorbellConnectionService,
        val callId: String,
        val snapshotUrl: String
    ) : Connection() {

        private var ringtone: Ringtone? = null
        private var vibrator: Vibrator? = null
        private val coroutineScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

        init {
            initAudio()
            observeEvents()
        }

        private fun initAudio() {
            try {
                val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
                ringtone = RingtoneManager.getRingtone(service, uri)?.apply {
                    audioAttributes = AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                }

                vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    val manager = service.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                    manager.defaultVibrator
                } else {
                    @Suppress("DEPRECATION")
                    service.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                }
            } catch (e: Exception) {
                Log.w(TAG, "Error initializing ringtone/vibrator: ${e.message}")
            }
        }

        fun startRingtone() {
            try {
                ringtone?.play()
                val pattern = longArrayOf(0, 800, 400, 800, 400, 800)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    vibrator?.vibrate(VibrationEffect.createWaveform(pattern, 0))
                } else {
                    @Suppress("DEPRECATION")
                    vibrator?.vibrate(pattern, 0)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Error starting ringtone: ${e.message}")
            }
        }

        fun stopRingtone() {
            try {
                ringtone?.stop()
                vibrator?.cancel()
            } catch (e: Exception) {
                Log.w(TAG, "Error stopping ringtone: ${e.message}")
            }
        }

        private fun observeEvents() {
            coroutineScope.launch {
                val currentUserId = FoxwayApp.instance.preferencesManager.getUserId()
                CallEventBus.events.collect { event ->
                    when (event.event) {
                        DoorbellEvent.CALL_CLAIMED -> {
                            if (event.userId != currentUserId) {
                                // Claimed elsewhere by another family member!
                                Log.i(TAG, "DoorbellConnection: Call claimed elsewhere by ${event.answeredBy}")
                                stopRingtone()
                                setDisconnected(DisconnectCause(DisconnectCause.OTHER))
                                destroy()
                                if (currentConnection == this@DoorbellConnection) currentConnection = null
                                PrewarmEngine.stopPrewarm()
                            }
                        }
                        DoorbellEvent.CALL_MISSED, DoorbellEvent.CALL_ENDED -> {
                            Log.i(TAG, "DoorbellConnection: Call ended or missed")
                            stopRingtone()
                            setDisconnected(DisconnectCause(DisconnectCause.REMOTE))
                            destroy()
                            if (currentConnection == this@DoorbellConnection) currentConnection = null
                            PrewarmEngine.stopPrewarm()
                        }
                    }
                }
            }
        }

        override fun onShowIncomingCallUi() {
            Log.i(TAG, "Telecom onShowIncomingCallUi called")
            launchCallActivity(service, callId, snapshotUrl, false)
        }

        override fun onAnswer(videoState: Int) {
            Log.i(TAG, "Telecom onAnswer called (Wearable/Auto/Headset): callId=$callId")
            setActive()
            stopRingtone()
            PrewarmEngine.answerCall()

            // Claim on backend
            coroutineScope.launch {
                try {
                    val prefs = FoxwayApp.instance.preferencesManager
                    val api = ApiClientFactory.createApi()
                    api.claim(callId, ClaimRequest(prefs.getUserId(), prefs.getUserName()))
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to claim call in onAnswer", e)
                }
            }

            launchCallActivity(service, callId, snapshotUrl, true)
        }

        override fun onReject() {
            Log.i(TAG, "Telecom onReject called (Wearable/Auto/Headset): callId=$callId")
            stopRingtone()
            setDisconnected(DisconnectCause(DisconnectCause.REJECTED))
            destroy()
            if (currentConnection == this) currentConnection = null
            PrewarmEngine.stopPrewarm()

            // Hangup on backend
            coroutineScope.launch {
                try {
                    val api = ApiClientFactory.createApi()
                    api.hangup(callId)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to hangup call in onReject", e)
                }
            }
            CallEventBus.postEvent(DoorbellEvent(DoorbellEvent.CALL_ENDED, mapOf("call_id" to callId)))
        }

        override fun onDisconnect() {
            Log.i(TAG, "Telecom onDisconnect called: callId=$callId")
            stopRingtone()
            setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
            destroy()
            if (currentConnection == this) currentConnection = null
            PrewarmEngine.stopPrewarm()

            coroutineScope.launch {
                try {
                    val api = ApiClientFactory.createApi()
                    api.hangup(callId)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to hangup call in onDisconnect", e)
                }
            }
            CallEventBus.postEvent(DoorbellEvent(DoorbellEvent.CALL_ENDED, mapOf("call_id" to callId)))
        }

        override fun onAbort() {
            Log.i(TAG, "Telecom onAbort called: callId=$callId")
            stopRingtone()
            setDisconnected(DisconnectCause(DisconnectCause.CANCELED))
            destroy()
            if (currentConnection == this) currentConnection = null
            PrewarmEngine.stopPrewarm()
        }
    }
}
