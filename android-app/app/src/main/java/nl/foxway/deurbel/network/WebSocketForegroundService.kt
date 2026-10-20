package nl.foxway.deurbel.network

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.asSharedFlow
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.MainActivity
import nl.foxway.deurbel.R
import nl.foxway.deurbel.data.models.DoorbellEvent
import nl.foxway.deurbel.telecom.DoorbellConnectionService

/**
 * Foreground service that keeps the persistent WebSocket connection alive.
 *
 * This service starts as a foreground service with `FOREGROUND_SERVICE_DATA_SYNC`
 * type, maintaining a low-priority notification so that Android does not kill
 * the process during Doze mode.
 *
 * When a [DoorbellEvent.CALL_RINGING] event arrives, it delegates to the
 * telecom layer (Milestone 3) to show the incoming call screen.
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

        // Collect events
        eventCollectorJob?.cancel()
        eventCollectorJob = serviceScope.launch {
            persistentWebSocket.events.collect { event ->
                handleEvent(event)
            }
        }

        Log.i(TAG, "Service started, WebSocket active")
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        eventCollectorJob?.cancel()
        persistentWebSocket.stop()
        serviceScope.cancel()
        Log.i(TAG, "Service destroyed")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ── Event handling ───────────────────────────────────────────────

    private suspend fun handleEvent(event: DoorbellEvent) {
        Log.i(TAG, "Handling event: ${event.event}")
        when (event.event) {
            DoorbellEvent.CALL_RINGING -> handleRinging(event)
            DoorbellEvent.CALL_CLAIMED -> handleClaimed(event)
            DoorbellEvent.CALL_MISSED -> handleMissed(event)
            DoorbellEvent.CALL_ENDED -> handleEnded(event)
            DoorbellEvent.USER_JOINED -> handleUserJoined(event)
        }
    }

    private fun handleRinging(event: DoorbellEvent) {
        val callId = event.callId ?: return
        val snapshotUrl = event.snapshotUrl ?: ""
        Log.i(TAG, "CALL_RINGING: callId=$callId")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Inkomende oproep van deurbel (ID: ${callId.take(8)})")
        DoorbellConnectionService.startIncomingCall(this, callId, snapshotUrl)
        CallEventBus.postEvent(event)
    }

    private fun handleClaimed(event: DoorbellEvent) {
        val answeredBy = event.answeredBy ?: "iemand"
        Log.i(TAG, "CALL_CLAIMED by $answeredBy")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Oproep beantwoord door $answeredBy")
        CallEventBus.postEvent(event)
    }

    private fun handleMissed(event: DoorbellEvent) {
        Log.i(TAG, "CALL_MISSED: ${event.callId}")
        nl.foxway.deurbel.data.AppLogger.getInstance(this).call("Gemiste oproep")
        CallEventBus.postEvent(event)
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
        val notificationManager = getSystemService(android.app.NotificationManager::class.java)
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

    // ── Notification ─────────────────────────────────────────────────

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
