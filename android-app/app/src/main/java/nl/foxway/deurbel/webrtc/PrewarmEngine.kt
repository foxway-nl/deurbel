package nl.foxway.deurbel.webrtc

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.network.EndpointResolver

/**
 * Singleton engine managing WebRTC session pre-warming.
 *
 * Implements SPEC §4.2:
 *  - Initiates background PeerConnection on incoming ring
 *  - H.264 video decoder warms up silently on first keyframe
 *  - Enables audio tracks instantly on answer for 0ms perceived latency
 */
object PrewarmEngine {

    private const val TAG = "PrewarmEngine"

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var activeSession: WebRtcSession? = null
    private var prewarmedCallId: String? = null

    private val _currentSessionFlow = MutableStateFlow<WebRtcSession?>(null)
    val currentSessionFlow: StateFlow<WebRtcSession?> = _currentSessionFlow.asStateFlow()

    /**
     * Start pre-warming WebRTC connection on incoming ring.
     */
    fun startPrewarm(context: Context, callId: String) {
        if (activeSession != null && prewarmedCallId == callId) {
            Log.d(TAG, "Already pre-warming for call $callId")
            return
        }

        stopPrewarm()
        prewarmedCallId = callId

        scope.launch {
            try {
                val prefs = FoxwayApp.instance.preferencesManager
                val resolver = EndpointResolver(prefs)
                val resolved = resolver.resolve()

                Log.i(TAG, "Pre-warming WebRTC session via proxy ${resolved.apiBaseUrl}")
                val session = WebRtcSession(context.applicationContext)
                activeSession = session
                _currentSessionFlow.value = session

                session.start(
                    signalingUrl = resolved.apiBaseUrl,
                    streamName = "deurbel",
                    isPrewarm = true
                )
            } catch (e: Exception) {
                Log.e(TAG, "Pre-warm failed for call $callId", e)
            }
        }
    }

    /**
     * Called when the user taps "Opnemen" (Answer).
     * Unmutes remote audio and local mic in 0ms!
     */
    fun answerCall(): WebRtcSession? {
        val session = activeSession ?: return null
        session.unmuteForAnswer(unmuteMic = true)
        Log.i(TAG, "Call answered, WebRTC unmuted")
        return session
    }

    /**
     * Get the active or pre-warmed session.
     */
    fun getSession(): WebRtcSession? = activeSession

    /**
     * Release pre-warmed session (call ended, missed, or answered elsewhere).
     */
    fun stopPrewarm() {
        activeSession?.close()
        activeSession = null
        prewarmedCallId = null
        _currentSessionFlow.value = null
        Log.d(TAG, "Pre-warm engine stopped")
    }
}
