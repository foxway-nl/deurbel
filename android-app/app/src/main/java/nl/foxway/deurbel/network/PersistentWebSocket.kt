package nl.foxway.deurbel.network

import android.util.Log
import com.google.gson.Gson
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import nl.foxway.deurbel.data.PreferencesManager
import nl.foxway.deurbel.data.models.DoorbellEvent
import okhttp3.*
import java.util.concurrent.TimeUnit

/**
 * Persistent WebSocket connection to the backend's `/ws/{user_id}` endpoint.
 *
 * Features:
 *   - Automatic reconnection with exponential backoff (1s → 2s → 4s → max 30s)
 *   - Heartbeat ping every 25 seconds to keep NAT/ALG connections alive
 *   - Emits parsed [DoorbellEvent] objects via a SharedFlow
 *   - Forwards FCM token updates to the backend inline
 *
 * Lifecycle is managed by [WebSocketForegroundService].
 */
class PersistentWebSocket(
    private val preferencesManager: PreferencesManager,
    private val endpointResolver: EndpointResolver
) {

    companion object {
        private const val TAG = "PersistentWS"
        private const val HEARTBEAT_INTERVAL_MS = 25_000L
        private val BACKOFF_STEPS = longArrayOf(1_000L, 3_000L, 5_000L, 15_000L)
    }

    private val gson = Gson()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private val _events = MutableSharedFlow<DoorbellEvent>(
        replay = 0,
        extraBufferCapacity = 16
    )
    val events: SharedFlow<DoorbellEvent> = _events.asSharedFlow()

    private var webSocket: WebSocket? = null
    private var heartbeatJob: Job? = null
    private var reconnectJob: Job? = null
    private var backoffIndex = 0
    private var currentLatch: CompletableDeferred<Unit>? = null
    private var isRunning = false

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // no read timeout for persistent WS
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    /**
     * Start the persistent WebSocket connection.
     * Resolves the best endpoint (LAN/WAN) before connecting.
     */
    fun start() {
        if (isRunning) return
        isRunning = true
        scope.launch { connectLoop() }
    }

    /**
     * Force immediate reconnect (e.g. after WiFi <-> 5G network switch).
     */
    fun forceReconnect(reason: String) {
        Log.i(TAG, "forceReconnect called: $reason")
        backoffIndex = 0
        try {
            webSocket?.cancel()
        } catch (_: Exception) {}
        currentLatch?.complete(Unit)
    }

    /**
     * Stop the WebSocket and cancel all coroutines.
     */
    fun stop() {
        isRunning = false
        heartbeatJob?.cancel()
        reconnectJob?.cancel()
        webSocket?.close(1000, "Client shutdown")
        webSocket = null
    }

    private suspend fun connectLoop() {
        while (isRunning) {
            try {
                val userId = preferencesManager.getUserId()
                if (userId.isBlank()) {
                    Log.w(TAG, "Not paired yet, waiting 5s before retry")
                    delay(5_000)
                    continue
                }

                val resolved = endpointResolver.resolve()
                val wsUrl = resolved.wsUrlForUser(userId)
                Log.i(TAG, "Connecting to $wsUrl")

                val request = Request.Builder()
                    .url(wsUrl)
                    .build()

                val latch = CompletableDeferred<Unit>()
                currentLatch = latch

                webSocket = client.newWebSocket(request, object : WebSocketListener() {
                    override fun onOpen(ws: WebSocket, response: Response) {
                        Log.i(TAG, "WebSocket connected to $wsUrl")
                        backoffIndex = 0
                        startHeartbeat(ws)
                        val isLan = resolved.isLan
                        nl.foxway.deurbel.data.AppLogger.getInstance(nl.foxway.deurbel.FoxwayApp.instance)
                            .network("WebSocket verbonden met ${if (isLan) "LAN" else "WAN"}")
                    }

                    override fun onMessage(ws: WebSocket, text: String) {
                        handleMessage(text)
                    }

                    override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                        Log.d(TAG, "WebSocket closing: $code $reason")
                        ws.close(1000, null)
                    }

                    override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                        Log.i(TAG, "WebSocket closed: $code $reason")
                        heartbeatJob?.cancel()
                        latch.complete(Unit)
                    }

                    override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                        val isNormalAbort = t is java.io.InterruptedIOException ||
                                           (t is java.net.SocketException && (t.message?.contains("closed", ignoreCase = true) == true || t.message?.contains("canceled", ignoreCase = true) == true))
                        if (isNormalAbort) {
                            Log.i(TAG, "WebSocket disconnected normally: ${t.message}")
                        } else {
                            Log.w(TAG, "WebSocket failure: ${t.message}")
                            nl.foxway.deurbel.data.AppLogger.getInstance(nl.foxway.deurbel.FoxwayApp.instance)
                                .network("WebSocket verbinding verbroken: ${t.message}")
                        }
                        heartbeatJob?.cancel()
                        latch.complete(Unit)
                    }
                })

                // Wait until the connection is closed or fails
                latch.await()

            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.w(TAG, "Connection error: ${e.message}")
            } finally {
                currentLatch = null
            }

            if (!isRunning) break

            val delayMs = BACKOFF_STEPS[backoffIndex.coerceAtMost(BACKOFF_STEPS.size - 1)]
            Log.i(TAG, "Reconnecting in ${delayMs}ms… (step $backoffIndex)")
            delay(delayMs)
            if (backoffIndex < BACKOFF_STEPS.size - 1) {
                backoffIndex++
            }
        }
    }

    private fun handleMessage(text: String) {
        if (text == "pong") return

        try {
            val event = gson.fromJson(text, DoorbellEvent::class.java)
            if (event.event.isNotBlank()) {
                Log.d(TAG, "Event received: ${event.event}")
                scope.launch { _events.emit(event) }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse WS message: $text", e)
        }
    }

    private fun startHeartbeat(ws: WebSocket) {
        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (isActive) {
                delay(HEARTBEAT_INTERVAL_MS)
                try {
                    ws.send("ping")
                } catch (e: Exception) {
                    Log.w(TAG, "Heartbeat send failed", e)
                    break
                }
            }
        }
    }

    /**
     * Send an inline FCM token update through the active WebSocket.
     */
    fun sendFcmTokenUpdate(token: String) {
        try {
            val json = gson.toJson(mapOf("type" to "fcm_token_update", "fcm_token" to token))
            webSocket?.send(json)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to send FCM token via WS", e)
        }
    }
}
