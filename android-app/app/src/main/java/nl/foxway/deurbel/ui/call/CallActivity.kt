package nl.foxway.deurbel.ui.call

import android.app.KeyguardManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.audio.DoorbellAudioStreamer
import nl.foxway.deurbel.data.models.ClaimRequest
import nl.foxway.deurbel.data.models.DoorbellEvent
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.CallEventBus
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.network.LiveStreamController
import nl.foxway.deurbel.telecom.DoorbellConnectionService
import nl.foxway.deurbel.ui.theme.FoxwayDeurbelTheme

/**
 * Fullscreen Incoming & Active Call Activity.
 *
 * Implements:
 *  - Single-activity enforcement: never allows double stacked call screens.
 *  - Instant line-1 UI dispatch: 0ms UI freeze on Ophangen, Opnemen, Stil.
 *  - Seamless Always-Warm video & audio via LiveStreamController.
 */
class CallActivity : ComponentActivity() {

    companion object {
        private const val TAG = "CallActivity"
        const val EXTRA_CALL_ID = "call_id"
        const val EXTRA_IS_ACTIVE = "is_active"
        const val EXTRA_SNAPSHOT_URL = "snapshot_url"

        @Volatile
        var isCallActivityVisible: Boolean = false
    }

    private var callId: String = ""
    private var snapshotUrl: String = ""
    private var isClaimedByMe: Boolean = false
    private val audioStreamer = DoorbellAudioStreamer()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        callId = intent.getStringExtra(EXTRA_CALL_ID) ?: ""
        snapshotUrl = intent.getStringExtra(EXTRA_SNAPSHOT_URL) ?: ""
        val initialActive = intent.getBooleanExtra(EXTRA_IS_ACTIVE, false)
        if (initialActive) {
            isClaimedByMe = true
            DoorbellConnectionService.cancelCallNotification(this)
            handleAnswer()
        }

        setupLockscreenFlags()

        // Pre-warm live stream immediately
        LiveStreamController.getInstance(this).start()

        setContent {
            FoxwayDeurbelTheme {
                var isActive by remember { mutableStateOf(initialActive) }
                var answeredByElsewhere by remember { mutableStateOf<String?>(null) }
                var isMicMuted by remember { mutableStateOf(true) }
                var isSpeakerOn by remember { mutableStateOf(true) }
                var isSilenced by remember { mutableStateOf(false) }

                // Listen for multi-user claim and hangup events
                LaunchedEffect(Unit) {
                    val currentUserId = FoxwayApp.instance.preferencesManager.getUserId()
                    CallEventBus.events.collect { event ->
                        when (event.event) {
                            DoorbellEvent.CALL_CLAIMED -> {
                                if (event.userId != currentUserId) {
                                    answeredByElsewhere = event.answeredBy ?: "iemand anders"
                                    delay(2500)
                                    finishAndRemoveTask()
                                }
                            }
                            DoorbellEvent.CALL_ENDED, DoorbellEvent.CALL_MISSED -> {
                                delay(800)
                                finishAndRemoveTask()
                            }
                        }
                    }
                }

                // Handle two-way microphone talkback
                val prefs = remember { FoxwayApp.instance.preferencesManager }
                val resolver = remember { EndpointResolver(prefs) }
                LaunchedEffect(isActive, isMicMuted) {
                    if (isActive && !isMicMuted) {
                        try {
                            val resolved = resolver.resolve()
                            audioStreamer.startStreaming(resolved.apiBaseUrl)
                        } catch (e: Exception) {
                            Log.w(TAG, "Error starting mic uplink: ${e.message}")
                        }
                    } else {
                        audioStreamer.stopStreaming()
                    }
                }

                CallScreen(
                    callId = callId,
                    snapshotUrl = snapshotUrl,
                    isActive = isActive,
                    isSilenced = isSilenced,
                    answeredByElsewhere = answeredByElsewhere,
                    isMicMuted = isMicMuted,
                    isSpeakerOn = isSpeakerOn,
                    onAnswer = {
                        isActive = true
                        handleAnswer()
                    },
                    onSilence = {
                        isSilenced = true
                        handleSilence()
                    },
                    onHangup = {
                        handleHangup()
                    },
                    onToggleMic = {
                        isMicMuted = !isMicMuted
                    },
                    onToggleSpeaker = {
                        isSpeakerOn = !isSpeakerOn
                        LiveStreamController.getInstance(this@CallActivity).setAudioMuted(!isSpeakerOn)
                    }
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        isCallActivityVisible = true
    }

    override fun onStop() {
        super.onStop()
        isCallActivityVisible = false
    }

    /**
     * Answer incoming call.
     * Line 1: Updates UI state instantly on Main Thread (<10ms).
     * Line 2: Background API claim on Dispatchers.IO.
     */
    private fun handleAnswer() {
        isClaimedByMe = true
        DoorbellConnectionService.silenceRingtone()
        LiveStreamController.getInstance(this).setAudioMuted(false)

        FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
            try {
                DoorbellConnectionService.notifyAnswered()
                val prefs = FoxwayApp.instance.preferencesManager
                val resolver = EndpointResolver(prefs)
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                val userId = prefs.getUserId()
                val userName = prefs.getUserName()
                api.claim(callId, ClaimRequest(userId = userId, userName = userName))
            } catch (e: Exception) {
                Log.e(TAG, "Error claiming call in background: ${e.message}")
            }
        }
    }

    /**
     * Silence ringtone.
     * Line 1: Stops ringing and vibrating on Main Thread while video keeps playing.
     */
    private fun handleSilence() {
        DoorbellConnectionService.silenceRingtone()
    }

    /**
     * Hangup call.
     * Line 1: Dismisses the call screen instantly on UI thread (<10ms).
     * Line 2: Stops audio streamer & runs API hangup on Dispatchers.IO.
     */
    private fun handleHangup() {
        // Line 1: Dismiss UI immediately
        finishAndRemoveTask()
        DoorbellConnectionService.notifyDisconnected()
        audioStreamer.stopStreaming()

        // Line 2: Background network cleanup
        val cid = callId
        FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
            try {
                val prefs = FoxwayApp.instance.preferencesManager
                val resolver = EndpointResolver(prefs)
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                api.hangup(cid)
            } catch (e: Exception) {
                Log.w(TAG, "Failed hangup API: ${e.message}")
            }
        }
    }

    private fun setupLockscreenFlags() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            val keyguardManager = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
            keyguardManager.requestDismissKeyguard(this, null)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                        WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD or
                        WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
            )
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onDestroy() {
        super.onDestroy()
        isCallActivityVisible = false
        audioStreamer.stopStreaming()
        DoorbellConnectionService.cancelCallNotification(this)
        if (isClaimedByMe && callId.isNotEmpty()) {
            val cid = callId
            FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
                try {
                    val prefs = FoxwayApp.instance.preferencesManager
                    val resolver = EndpointResolver(prefs)
                    val resolved = resolver.resolve()
                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                    api.hangup(cid)
                } catch (_: Exception) {}
            }
        } else {
            DoorbellConnectionService.notifyDismissed()
        }
    }
}
