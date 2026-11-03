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
import android.media.AudioManager
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.CallEventBus
import nl.foxway.deurbel.network.ConnectionManager
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
        isCallActivityVisible = true
        DoorbellConnectionService.cancelCallNotification(this)

        callId = intent.getStringExtra(EXTRA_CALL_ID) ?: ""
        snapshotUrl = intent.getStringExtra(EXTRA_SNAPSHOT_URL) ?: ""
        val initialActive = intent.getBooleanExtra(EXTRA_IS_ACTIVE, false)
        if (initialActive) {
            isClaimedByMe = true
            DoorbellConnectionService.cancelCallNotification(this)
            handleAnswer()
        }

        setupLockscreenFlags()

        // Force speakerphone on incoming/active doorbell call
        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
            audioManager?.isSpeakerphoneOn = true
        } catch (_: Exception) {}

        // Pre-warm live stream safely
        try {
            LiveStreamController.getInstance(this).start()
        } catch (e: Exception) {
            Log.w(TAG, "LiveStream start error in onCreate: ${e.message}")
        }

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
                val connectionManager = remember { ConnectionManager.getInstance(this@CallActivity) }
                LaunchedEffect(isActive, isMicMuted) {
                    if (isActive && !isMicMuted) {
                        try {
                            val targetUrl = connectionManager.getEffectiveBaseUrl()
                            audioStreamer.startStreaming(targetUrl)
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
                        try {
                            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
                            audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
                            audioManager?.isSpeakerphoneOn = isSpeakerOn
                        } catch (_: Exception) {}
                        LiveStreamController.getInstance(this@CallActivity).setAudioMuted(!isSpeakerOn)
                    }
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        isCallActivityVisible = true
        DoorbellConnectionService.cancelCallNotification(this)
    }

    override fun onResume() {
        super.onResume()
        isCallActivityVisible = true
        DoorbellConnectionService.cancelCallNotification(this)
    }

    override fun onStop() {
        super.onStop()
        isCallActivityVisible = false
        audioStreamer.stopStreaming()
        LiveStreamController.getInstance(this).hardKillAudio()
        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            audioManager?.mode = AudioManager.MODE_NORMAL
            audioManager?.isSpeakerphoneOn = false
        } catch (_: Exception) {}
    }

    /**
     * Answer incoming call.
     * Line 1: Updates UI state instantly on Main Thread (<10ms).
     * Line 2: Background API claim on Dispatchers.IO.
     */
    private fun handleAnswer() {
        isClaimedByMe = true
        DoorbellConnectionService.silenceRingtone()
        DoorbellConnectionService.cancelCallNotification(this)
        DoorbellConnectionService.notifyAnswered()

        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
            audioManager?.isSpeakerphoneOn = true
        } catch (_: Exception) {}

        LiveStreamController.getInstance(this).setAudioMuted(false)

        FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
            try {
                val prefs = FoxwayApp.instance.preferencesManager
                val api = ApiClientFactory.createApi()
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
        DoorbellConnectionService.cancelCallNotification(this)
    }

    /**
     * Hangup call.
     * Line 1: Dismisses the call screen instantly on UI thread (<10ms).
     * Line 2: Stops audio streamer & runs API hangup on Dispatchers.IO.
     */
    private fun handleHangup() {
        // Line 1: Dismiss UI immediately
        finishAndRemoveTask()
        DoorbellConnectionService.silenceRingtone()
        DoorbellConnectionService.cancelCallNotification(this)
        DoorbellConnectionService.resetDeduplication()
        DoorbellConnectionService.notifyDisconnected()
        audioStreamer.stopStreaming()
        LiveStreamController.getInstance(this).hardKillAudio()
        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            audioManager?.mode = AudioManager.MODE_NORMAL
            audioManager?.isSpeakerphoneOn = false
        } catch (_: Exception) {}

        // Line 2: Background network cleanup
        val cid = callId
        FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
            try {
                val api = ApiClientFactory.createApi()
                api.hangup(cid)
            } catch (e: Exception) {
                Log.w(TAG, "Failed hangup API: ${e.message}")
            }
        }
    }

    private fun setupLockscreenFlags() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                setShowWhenLocked(true)
                setTurnScreenOn(true)
                val keyguardManager = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
                keyguardManager?.requestDismissKeyguard(this, null)
            } else {
                @Suppress("DEPRECATION")
                window.addFlags(
                    WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD or
                            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                )
            }
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } catch (e: Exception) {
            Log.w(TAG, "setupLockscreenFlags error: ${e.message}")
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isCallActivityVisible = false
        DoorbellConnectionService.resetDeduplication()
        audioStreamer.stopStreaming()
        LiveStreamController.getInstance(this).hardKillAudio()
        DoorbellConnectionService.silenceRingtone()
        DoorbellConnectionService.cancelCallNotification(this)

        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            audioManager?.mode = AudioManager.MODE_NORMAL
            audioManager?.isSpeakerphoneOn = false
        } catch (_: Exception) {}

        if (isClaimedByMe && callId.isNotEmpty()) {
            val cid = callId
            FoxwayApp.instance.applicationScope.launch(Dispatchers.IO) {
                try {
                    val api = ApiClientFactory.createApi()
                    api.hangup(cid)
                } catch (_: Exception) {}
            }
        } else {
            DoorbellConnectionService.notifyDismissed()
        }
    }
}
