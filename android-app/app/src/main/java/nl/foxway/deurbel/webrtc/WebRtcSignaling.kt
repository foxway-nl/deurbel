package nl.foxway.deurbel.webrtc

import android.util.Log
import com.google.gson.Gson
import com.google.gson.annotations.SerializedName
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.webrtc.IceCandidate
import org.webrtc.SessionDescription
import java.util.concurrent.TimeUnit

/**
 * Signaling client for go2rtc WebRTC streams.
 *
 * Supports both go2rtc native WebSocket signaling (`/api/ws?src=deurbel`)
 * and WHEP HTTP POST signaling.
 */
class WebRtcSignaling {

    companion object {
        private const val TAG = "WebRtcSignaling"
    }

    private val gson = Gson()
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var webSocket: WebSocket? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private val _remoteAnswers = MutableSharedFlow<SessionDescription>(replay = 1)
    val remoteAnswers: SharedFlow<SessionDescription> = _remoteAnswers.asSharedFlow()

    private val _remoteCandidates = MutableSharedFlow<IceCandidate>(replay = 16)
    val remoteCandidates: SharedFlow<IceCandidate> = _remoteCandidates.asSharedFlow()

    data class Go2rtcMessage(
        val type: String,
        val value: String
    )

    /**
     * Connect to go2rtc WebSocket signaling.
     *
     * @param wsUrl e.g. "ws://<host>:1984" or "wss://<domain>"
     * @param streamName e.g. "deurbel"
     */
    fun connect(wsUrl: String, streamName: String, onConnected: () -> Unit = {}) {
        val cleanUrl = wsUrl.trimEnd('/')
        val signalingUrl = "$cleanUrl/api/ws?src=$streamName"
        Log.i(TAG, "Connecting to go2rtc WS: $signalingUrl")

        val request = Request.Builder().url(signalingUrl).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "go2rtc signaling WebSocket connected")
                onConnected()
            }

            override fun onMessage(ws: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "go2rtc signaling WS error: ${t.message}")
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "go2rtc signaling WS closed: $code $reason")
            }
        })
    }

    private fun handleMessage(text: String) {
        try {
            val msg = gson.fromJson(text, Go2rtcMessage::class.java)
            when (msg.type) {
                "webrtc/answer" -> {
                    Log.i(TAG, "Received SDP answer from go2rtc")
                    val sdp = SessionDescription(SessionDescription.Type.ANSWER, msg.value)
                    scope.launch { _remoteAnswers.emit(sdp) }
                }
                "webrtc/candidate" -> {
                    val parts = msg.value.split(" ")
                    val sdpMid = if (parts.size > 1) parts[0] else "0"
                    val sdpMLineIndex = 0
                    val candidate = IceCandidate(sdpMid, sdpMLineIndex, msg.value)
                    scope.launch { _remoteCandidates.emit(candidate) }
                }
                else -> {
                    Log.d(TAG, "Unhandled go2rtc message: ${msg.type}")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse go2rtc message: $text", e)
        }
    }

    /**
     * Send local SDP offer to go2rtc.
     */
    fun sendOffer(sdp: SessionDescription) {
        val payload = gson.toJson(Go2rtcMessage(type = "webrtc/offer", value = sdp.description))
        Log.i(TAG, "Sending SDP offer to go2rtc")
        webSocket?.send(payload)
    }

    /**
     * Send local ICE candidate to go2rtc.
     */
    fun sendCandidate(candidate: IceCandidate) {
        val payload = gson.toJson(
            Go2rtcMessage(
                type = "webrtc/candidate",
                value = candidate.sdp
            )
        )
        webSocket?.send(payload)
    }

    /**
     * WHEP HTTP fallback if WebSocket is unavailable.
     */
    suspend fun whepExchange(httpUrl: String, streamName: String, offerSdp: String): String =
        withContext(Dispatchers.IO) {
            val endpoint = if (httpUrl.contains("/api/")) {
                httpUrl
            } else {
                "${httpUrl.trimEnd('/')}/api/v1/webrtc?src=$streamName"
            }
            val body = offerSdp.toRequestBody("application/sdp".toMediaType())
            val request = Request.Builder()
                .url(endpoint)
                .post(body)
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw Exception("WHEP failed: ${response.code} ${response.message}")
                }
                response.body?.string() ?: throw Exception("Empty WHEP response")
            }
        }

    fun close() {
        webSocket?.close(1000, "Normal closure")
        webSocket = null
        scope.cancel()
    }
}
