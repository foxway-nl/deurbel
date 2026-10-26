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
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.ClaimRequest
import nl.foxway.deurbel.data.models.DoorbellEvent
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.CallEventBus
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.telecom.DoorbellConnectionService
import nl.foxway.deurbel.ui.theme.FoxwayDeurbelTheme
import nl.foxway.deurbel.webrtc.PrewarmEngine
import nl.foxway.deurbel.webrtc.WebRtcSession

/**
 * Fullscreen Incoming & Active Call Activity.
 *
 * Implements SPEC §4.1 & §4.3 (Milestone 3):
 *  - Activates over lockscreen via setShowWhenLocked & setTurnScreenOn
 *  - Displays live snapshot and pre-warmed video
 *  - Handles Answer, Decline, Hangup, and Quick-Replies
 *  - Multi-user dismissal: auto-dismisses if another family member answers.
 */
class CallActivity : ComponentActivity() {

    companion object {
        private const val TAG = "CallActivity"
        const val EXTRA_CALL_ID = "call_id"
        const val EXTRA_IS_ACTIVE = "is_active"
        const val EXTRA_SNAPSHOT_URL = "snapshot_url"
    }

    private var callId: String = ""
    private var snapshotUrl: String = ""
    private var isClaimedByMe: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        callId = intent.getStringExtra(EXTRA_CALL_ID) ?: ""
        snapshotUrl = intent.getStringExtra(EXTRA_SNAPSHOT_URL) ?: ""
        val initialActive = intent.getBooleanExtra(EXTRA_IS_ACTIVE, false)
        if (initialActive) {
            isClaimedByMe = true
        }

        setupLockscreenFlags()

        setContent {
            FoxwayDeurbelTheme {
                val session by PrewarmEngine.currentSessionFlow.collectAsState()
                var isActive by remember { mutableStateOf(initialActive) }
                var answeredByElsewhere by remember { mutableStateOf<String?>(null) }
                var isMicMuted by remember { mutableStateOf(true) }
                var isSpeakerOn by remember { mutableStateOf(true) }
                val resolver = remember { EndpointResolver(FoxwayApp.instance.preferencesManager) }
                var streamUrl by remember {
                    val activeBase = resolver.activeApiUrl.value
                    mutableStateOf(if (activeBase.isNotBlank()) "$activeBase/api/v1/stream.mp4?src=deurbel" else "")
                }

                LaunchedEffect(Unit) {
                    try {
                        val resolved = resolver.resolve()
                        streamUrl = "${resolved.apiBaseUrl}/api/v1/stream.mp4?src=deurbel"
                    } catch (_: Exception) {}
                }

                // Listen for multi-user claim events
                LaunchedEffect(Unit) {
                    val currentUserId = FoxwayApp.instance.preferencesManager.getUserId()
                    CallEventBus.events.collect { event ->
                        when (event.event) {
                            DoorbellEvent.CALL_CLAIMED -> {
                                if (event.userId != currentUserId) {
                                    // Another family member answered first!
                                    answeredByElsewhere = event.answeredBy ?: "iemand anders"
                                    delay(3000)
                                    finishAndRemoveTask()
                                }
                            }
                            DoorbellEvent.CALL_ENDED, DoorbellEvent.CALL_MISSED -> {
                                delay(1000)
                                finishAndRemoveTask()
                            }
                        }
                    }
                }

                var isSilenced by remember { mutableStateOf(false) }

                CallScreen(
                    callId = callId,
                    snapshotUrl = snapshotUrl,
                    isActive = isActive,
                    isSilenced = isSilenced,
                    answeredByElsewhere = answeredByElsewhere,
                    webRtcSession = session,
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
                        session?.let {
                            val newMuted = !isMicMuted
                            it.setMicrophoneMuted(newMuted)
                            isMicMuted = newMuted
                        }
                    },
                    onToggleSpeaker = {
                        session?.let {
                            val newSpeaker = !isSpeakerOn
                            it.setSpeakerphone(newSpeaker)
                            isSpeakerOn = newSpeaker
                        }
                    }
                )
            }
        }
    }

    private fun handleAnswer() {
        isClaimedByMe = true
        DoorbellConnectionService.notifyAnswered()
        lifecycleScope.launch {
            try {
                // 1. Unmute incoming audio, keep mic strictly muted until user pushes to talk
                PrewarmEngine.answerCall()

                // 2. Claim call on backend in parallel
                val prefs = FoxwayApp.instance.preferencesManager
                val resolver = EndpointResolver(prefs)
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                val userId = prefs.getUserId()
                val userName = prefs.getUserName()

                api.claim(callId, ClaimRequest(userId = userId, userName = userName))
            } catch (e: Exception) {
                Log.e(TAG, "Error in handleAnswer", e)
            }
        }
    }

    private fun handleSilence() {
        DoorbellConnectionService.silenceRingtone()
    }

    private fun handleHangup() {
        val wasClaimed = isClaimedByMe
        isClaimedByMe = false
        DoorbellConnectionService.notifyDisconnected()
        lifecycleScope.launch {
            try {
                val prefs = FoxwayApp.instance.preferencesManager
                val resolver = EndpointResolver(prefs)
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                api.hangup(callId)
            } catch (e: Exception) {
                Log.e(TAG, "Error in handleHangup", e)
            } finally {
                PrewarmEngine.stopPrewarm()
                finishAndRemoveTask()
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
        if (isClaimedByMe && callId.isNotEmpty()) {
            val cid = callId
            FoxwayApp.instance.applicationScope.launch {
                try {
                    val prefs = FoxwayApp.instance.preferencesManager
                    val resolver = EndpointResolver(prefs)
                    val resolved = resolver.resolve()
                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                    api.hangup(cid)
                    Log.i(TAG, "Cleaned up call state on onDestroy: $cid")
                } catch (e: Exception) {
                    Log.w(TAG, "Failed hangup in onDestroy: ${e.message}")
                }
            }
        } else {
            DoorbellConnectionService.notifyDismissed()
        }
        PrewarmEngine.stopPrewarm()
    }
}
