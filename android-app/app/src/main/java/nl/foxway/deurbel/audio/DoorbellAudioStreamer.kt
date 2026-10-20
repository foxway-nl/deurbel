package nl.foxway.deurbel.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.*
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

/**
 * Streams live PCM audio from the Android microphone directly to the FastAPI
 * `/api/v1/audio/stream` WebSocket endpoint.
 *
 * The backend transcode engine pipes these frames through FFmpeg into
 * native AAC-LC ADTS frames and transmits them over Hikvision Port 8000 SDK
 * directly to the outdoor doorbell speaker.
 */
class DoorbellAudioStreamer {

    companion object {
        private const val TAG = "DoorbellAudioStreamer"
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private const val CHUNK_SIZE = 1280 // 40ms of 16kHz 16-bit mono PCM (640 samples)
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // Keep alive indefinitely while talking
        .writeTimeout(5, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    private var audioRecord: AudioRecord? = null
    private var webSocket: WebSocket? = null
    private var streamJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    @SuppressLint("MissingPermission")
    fun startStreaming(baseUrlOrWs: String) {
        if (_isStreaming.value) return

        val minBufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
        val bufferSize = maxOf(minBufferSize, CHUNK_SIZE * 4)

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                SAMPLE_RATE,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                bufferSize
            )
            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord initialization failed, state=${audioRecord?.state}")
                return
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed creating AudioRecord", e)
            return
        }

        // Convert HTTP/HTTPS to WS/WSS
        val wsBase = baseUrlOrWs
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .trimEnd('/')

        val wsUrl = "$wsBase/api/v1/audio/stream"
        Log.i(TAG, "Connecting live mic stream to $wsUrl")

        val request = Request.Builder().url(wsUrl).build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "Audio stream WebSocket connected to doorbell")
                _isStreaming.value = true
                startRecordLoop()
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "Audio stream WebSocket failed", t)
                stopStreaming()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "Audio stream WebSocket closed ($code: $reason)")
                stopStreaming()
            }
        })
    }

    private fun startRecordLoop() {
        streamJob?.cancel()
        streamJob = scope.launch {
            try {
                audioRecord?.startRecording()
                val buffer = ByteArray(CHUNK_SIZE)
                while (isActive && _isStreaming.value) {
                    val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                    if (read > 0 && webSocket != null) {
                        webSocket?.send(buffer.toByteString(0, read))
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Exception during live audio capture loop", e)
            } finally {
                try {
                    audioRecord?.stop()
                } catch (ignored: Exception) {}
            }
        }
    }

    fun stopStreaming() {
        if (!_isStreaming.value && webSocket == null && audioRecord == null) return
        _isStreaming.value = false
        streamJob?.cancel()
        streamJob = null

        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (ignored: Exception) {}
        audioRecord = null

        try {
            webSocket?.close(1000, "User muted talk button")
        } catch (ignored: Exception) {}
        webSocket = null
        Log.i(TAG, "Audio streaming stopped cleanly")
    }
}
