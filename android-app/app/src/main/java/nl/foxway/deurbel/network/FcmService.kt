package nl.foxway.deurbel.network

import android.util.Log
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.google.gson.Gson
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.DoorbellEvent

/**
 * Firebase Cloud Messaging service — receives high-priority data-only pushes
 * from the backend when the WebSocket connection is not active (app killed,
 * device in Doze, etc.).
 *
 * FCM data payloads from the backend have this structure:
 * ```json
 * {
 *   "event": "CALL_RINGING",
 *   "call_id": "abc123def456",
 *   "snapshot_url": "/api/v1/snapshots/abc123def456.jpg",
 *   "timestamp": "2026-10-18T15:30:00Z"
 * }
 * ```
 *
 * The `event` field is always a top-level key; all other fields are
 * the same as the WebSocket event `data` payload.
 */
class FcmService : FirebaseMessagingService() {

    companion object {
        private const val TAG = "FcmService"
    }

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    /**
     * Called when a data-only FCM message is received.
     *
     * Since we use `priority: "high"` and data-only (no `notification` key),
     * this is called even when the app is in the background or killed,
     * bypassing the Android notification tray entirely.
     */
    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)

        val data = message.data
        val eventType = data["event"] ?: run {
            Log.w(TAG, "FCM message without 'event' key, ignoring")
            return
        }

        Log.i(TAG, "FCM received: event=$eventType")

        // Build a DoorbellEvent from the flat FCM data map
        val eventData = data.toMutableMap()
        eventData.remove("event")

        // Parse JSON-encoded values back to their original types
        val parsedData = mutableMapOf<String, Any?>()
        for ((key, value) in eventData) {
            parsedData[key] = tryParseJson(value)
        }

        val doorbellEvent = DoorbellEvent(
            event = eventType,
            data = parsedData
        )

        // Dispatch to the same event bus as the WebSocket service
        CallEventBus.postEvent(doorbellEvent)

        // If we received CALL_RINGING and the WebSocket service isn't running,
        // start it so we have a live connection for subsequent events
        if (eventType == DoorbellEvent.CALL_RINGING) {
            val callId = doorbellEvent.callId ?: ""
            val snapshotUrl = doorbellEvent.snapshotUrl ?: ""
            try {
                nl.foxway.deurbel.telecom.DoorbellConnectionService.startIncomingCall(
                    applicationContext,
                    callId,
                    snapshotUrl
                )
                WebSocketForegroundService.start(applicationContext)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start services from FCM", e)
            }
        }
    }

    /**
     * Called when the FCM registration token is refreshed.
     *
     * Persists the new token locally and sends it to the backend
     * (both via REST API and inline WebSocket message).
     */
    override fun onNewToken(token: String) {
        super.onNewToken(token)
        Log.i(TAG, "FCM token refreshed: ${token.take(20)}…")

        serviceScope.launch {
            try {
                // Save locally
                val prefs = FoxwayApp.instance.preferencesManager
                prefs.saveFcmToken(token)

                // Send to backend via API
                val userId = prefs.getUserId()
                if (userId.isNotBlank()) {
                    val resolver = EndpointResolver(prefs)
                    val resolved = resolver.resolve()
                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                    val response = api.updateFcmToken(
                        userId,
                        nl.foxway.deurbel.data.models.FcmTokenUpdate(token)
                    )
                    if (response.isSuccessful) {
                        Log.i(TAG, "FCM token updated on backend")
                    } else {
                        Log.w(TAG, "FCM token update failed: ${response.code()}")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to update FCM token", e)
            }
        }
    }

    /**
     * Try to parse a JSON string value back to its native type.
     * FCM data payloads only support string values, so the backend
     * JSON-encodes non-string values.
     */
    private fun tryParseJson(value: String): Any? {
        return try {
            val gson = Gson()
            gson.fromJson(value, Any::class.java) ?: value
        } catch (e: Exception) {
            value
        }
    }
}
