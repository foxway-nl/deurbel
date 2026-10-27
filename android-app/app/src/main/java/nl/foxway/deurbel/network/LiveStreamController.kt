package nl.foxway.deurbel.network

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.*
import okio.ByteString
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Always-Warm Live Stream Controller.
 *
 * Connects to `/api/v1/ws/live` on LAN or WAN/5G over standard port 8090/443:
 *  - First frame arrives in <100ms directly from server RAM (zero wait for keyframes).
 *  - Decodes 15 fps 4:3 JPEG video frames into [currentBitmap].
 *  - Plays incoming 16kHz PCM audio via low-latency [AudioTrack].
 *  - Shared across LiveScreen and CallScreen so transitioning from RINGING to ANSWERED
 *    never interrupts video playback.
 */
class LiveStreamController private constructor(private val context: Context) {

    companion object {
        private const val TAG = "LiveStreamController"
        private const val AUDIO_SAMPLE_RATE = 16000

        @Volatile
        private var instance: LiveStreamController? = null

        fun getInstance(context: Context): LiveStreamController {
            return instance ?: synchronized(this) {
                instance ?: LiveStreamController(context.applicationContext).also { instance = it }
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val connectionManager = ConnectionManager.getInstance(context)

    private val _currentBitmap = MutableStateFlow<Bitmap?>(null)
    val currentBitmap: StateFlow<Bitmap?> = _currentBitmap.asStateFlow()

    private val _isConnected = MutableStateFlow(false)
    val isConnected: StateFlow<Boolean> = _isConnected.asStateFlow()

    private val _isAudioMuted = MutableStateFlow(true)
    val isAudioMuted: StateFlow<Boolean> = _isAudioMuted.asStateFlow()

    private var client: OkHttpClient? = null
    private var webSocket: WebSocket? = null
    private var audioTrack: AudioTrack? = null
    private val isRunning = AtomicBoolean(false)
    private var reconnectJob: Job? = null

    init {
        initAudioTrack()
    }

    private fun initAudioTrack() {
        try {
            val minBufferSize = AudioTrack.getMinBufferSize(
                AUDIO_SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            val bufferSize = maxOf(minBufferSize, 4096)

            val audioAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build()

            val audioFormat = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(AUDIO_SAMPLE_RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build()

            audioTrack = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                AudioTrack.Builder()
                    .setAudioAttributes(audioAttributes)
                    .setAudioFormat(audioFormat)
                    .setBufferSizeInBytes(bufferSize)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                    .build()
            } else {
                @Suppress("DEPRECATION")
                AudioTrack(
                    AudioManager.STREAM_VOICE_CALL,
                    AUDIO_SAMPLE_RATE,
                    AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize,
                    AudioTrack.MODE_STREAM
                )
            }
            audioTrack?.play()
        } catch (e: Exception) {
            Log.w(TAG, "Could not initialize AudioTrack: ${e.message}")
        }
    }

    fun start() {
        if (isRunning.getAndSet(true)) {
            // Already running
            return
        }
        Log.i(TAG, "Starting LiveStreamController")
        connectWebSocket()
    }

    fun stop() {
        isRunning.set(false)
        reconnectJob?.cancel()
        reconnectJob = null
        try {
            webSocket?.close(1000, "App closed")
        } catch (_: Exception) {}
        webSocket = null
        _isConnected.value = false
    }

    fun setAudioMuted(muted: Boolean) {
        _isAudioMuted.value = muted
        Log.d(TAG, "setAudioMuted: $muted")
    }

    private fun connectWebSocket() {
        if (!isRunning.get()) return

        val activeBase = connectionManager.activeBaseUrl.value.ifBlank { "https://deurbel.foxway.nl" }
        val wsUrl = activeBase
            .replace("https://", "wss://")
            .replace("http://", "ws://") + "/api/v1/ws/live"

        Log.i(TAG, "Connecting live stream WebSocket to: $wsUrl")

        if (client == null) {
            client = OkHttpClient.Builder()
                .connectTimeout(4, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .writeTimeout(4, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .build()
        }

        val request = Request.Builder().url(wsUrl).build()
        webSocket = client?.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "Live stream WebSocket opened: $wsUrl")
                _isConnected.value = true
            }

            override fun onMessage(ws: WebSocket, bytes: ByteString) {
                val data = bytes.toByteArray()
                if (data.isEmpty()) return
                when (data[0]) {
                    0x01.toByte() -> {
                        // Video Frame: JPEG bytes
                        try {
                            val bitmap = BitmapFactory.decodeByteArray(data, 1, data.size - 1)
                            if (bitmap != null) {
                                _currentBitmap.value = bitmap
                            }
                        } catch (e: Exception) {
                            Log.w(TAG, "Failed decoding live JPEG frame: ${e.message}")
                        }
                    }
                    0x02.toByte() -> {
                        // Audio Frame: 16kHz 16-bit mono PCM
                        if (!_isAudioMuted.value && audioTrack != null) {
                            try {
                                audioTrack?.write(data, 1, data.size - 1)
                            } catch (e: Exception) {
                                Log.w(TAG, "Error writing to AudioTrack: ${e.message}")
                            }
                        }
                    }
                }
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "Live stream WebSocket error: ${t.message}")
                _isConnected.value = false
                scheduleReconnect()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "Live stream WebSocket closed: $reason ($code)")
                _isConnected.value = false
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (!isRunning.get()) return
        if (reconnectJob?.isActive == true) return

        reconnectJob = scope.launch {
            delay(1000L)
            if (isRunning.get()) {
                connectionManager.refreshRoute()
                connectWebSocket()
            }
        }
    }
}
