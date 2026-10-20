package nl.foxway.deurbel.ui.call

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import coil.compose.AsyncImage
import nl.foxway.deurbel.ui.theme.*
import nl.foxway.deurbel.webrtc.WebRtcSession
import org.webrtc.SurfaceViewRenderer

/**
 * Incoming & Active Doorbell Call UI.
 *
 * Implements SPEC §4.3 & §4.5:
 *  - Edge-to-edge video/snapshot display
 *  - Quick-reply pills ("📦 Zet pakketje bij de deur", "🏃 Ik kom eraan")
 *  - Large Answer/Decline buttons with subtle pulsing
 *  - Floating glassmorphic control bar during active call
 *  - Multi-user "Beantwoord door Mohamed" dismissal banner
 */
@Composable
fun CallScreen(
    callId: String,
    snapshotUrl: String,
    isActive: Boolean,
    answeredByElsewhere: String?,
    webRtcSession: WebRtcSession?,
    isMicMuted: Boolean,
    isSpeakerOn: Boolean,
    onAnswer: () -> Unit,
    onDecline: () -> Unit,
    onHangup: () -> Unit,
    onToggleMic: () -> Unit,
    onToggleSpeaker: () -> Unit,
    onQuickReply: (String) -> Unit
) {
    // Pulse animation for the green Answer button
    val infiniteTransition = rememberInfiniteTransition(label = "answerPulse")
    val answerPulseScale by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 1.08f,
        animationSpec = infiniteRepeatable(
            animation = tween(900, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "answerPulseScale"
    )

    var isFitMode by remember { mutableStateOf(true) }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
    ) {
        // ── 1. Video Stream / Snapshot Background ──
        Box(
            modifier = Modifier.fillMaxSize(),
            contentAlignment = Alignment.Center
        ) {
            if (webRtcSession != null) {
                AndroidView(
                    factory = { context ->
                        SurfaceViewRenderer(context).apply {
                            val scaling = if (isFitMode) org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FIT
                            else org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FILL
                            setScalingType(scaling)
                            setEnableHardwareScaler(true)
                            webRtcSession.attachRenderer(this, scaling)
                            requestLayout()
                        }
                    },
                    update = { view ->
                        val scaling = if (isFitMode) org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FIT
                        else org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FILL
                        view.setScalingType(scaling)
                        view.requestLayout()
                        webRtcSession.setScalingType(scaling)
                    },
                    modifier = if (isFitMode) {
                        Modifier
                            .fillMaxWidth()
                            .aspectRatio(16f / 9f)
                    } else {
                        Modifier.fillMaxSize()
                    }
                )
            } else if (snapshotUrl.isNotBlank()) {
                AsyncImage(
                    model = snapshotUrl,
                    contentDescription = "Doorbell Snapshot",
                    contentScale = if (isFitMode) ContentScale.Fit else ContentScale.Crop,
                    modifier = if (isFitMode) Modifier.fillMaxWidth().aspectRatio(16f / 9f) else Modifier.fillMaxSize()
                )
            }
        }

        // Dark gradient overlay for readability of UI controls
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        colors = listOf(
                            Color.Black.copy(alpha = 0.65f),
                            Color.Transparent,
                            Color.Black.copy(alpha = 0.85f)
                        )
                    )
                )
        )

        // ── 2. Top Bar: Caller Info & Status + Aspect Ratio Toggle ──
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(top = 16.dp, start = 20.dp, end = 20.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Surface(
                color = SurfaceGlass,
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .background(if (isActive) LanGreen else CoralDecline, CircleShape)
                    )
                    Text(
                        text = if (isActive) "Actief • Voordeur" else "Aanbellen • Voordeur",
                        style = MaterialTheme.typography.labelLarge,
                        color = TextPrimary
                    )
                }
            }

            // Aspect ratio toggle
            Surface(
                color = SurfaceGlass,
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
                modifier = Modifier.clickable {
                    isFitMode = !isFitMode
                    webRtcSession?.setScalingType(
                        if (isFitMode) org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FIT
                        else org.webrtc.RendererCommon.ScalingType.SCALE_ASPECT_FILL
                    )
                }
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Icon(
                        imageVector = if (isFitMode) Icons.Default.AspectRatio else Icons.Default.FitScreen,
                        contentDescription = "Beeldverhouding",
                        tint = CyanAccent,
                        modifier = Modifier.size(16.dp)
                    )
                    Text(
                        text = if (isFitMode) "Overzicht (Fit)" else "Vullend (Fill)",
                        style = MaterialTheme.typography.labelSmall,
                        color = TextPrimary
                    )
                }
            }
        }

        // Active microphone banner
        if (isActive && !isMicMuted) {
            Surface(
                color = Color(0xFFDC2626), // Bright red
                shape = RoundedCornerShape(20.dp),
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .statusBarsPadding()
                    .padding(top = 70.dp)
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(modifier = Modifier.size(8.dp).background(Color.White, CircleShape))
                    Text(
                        text = "MICROFOON ACTIEF — DEURBEL HOORT JOU",
                        color = Color.White,
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            }
        }

        // ── 3. Multi-User Claim Banner (Dismissal Notification) ──
        AnimatedVisibility(
            visible = answeredByElsewhere != null,
            enter = fadeIn() + slideInVertically(),
            exit = fadeOut() + slideOutVertically(),
            modifier = Modifier
                .align(Alignment.Center)
                .padding(24.dp)
        ) {
            Surface(
                color = SurfaceDark,
                shape = RoundedCornerShape(16.dp),
                border = androidx.compose.foundation.BorderStroke(2.dp, CyanAccent),
                shadowElevation = 8.dp
            ) {
                Column(
                    modifier = Modifier.padding(24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Icon(
                        imageVector = Icons.Default.CheckCircle,
                        contentDescription = null,
                        tint = CyanAccent,
                        modifier = Modifier.size(48.dp)
                    )
                    Text(
                        text = "Beantwoord door $answeredByElsewhere",
                        style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                        color = TextPrimary,
                        textAlign = TextAlign.Center
                    )
                    Text(
                        text = "Deze oproep wordt gesloten…",
                        style = MaterialTheme.typography.bodyMedium,
                        color = TextSecondary
                    )
                }
            }
        }

        // ── 4. Bottom Controls: Incoming vs Active State ──
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 24.dp, start = 16.dp, end = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            if (!isActive && answeredByElsewhere == null) {
                // Quick-reply pills: 3 buttons per Requirement #3
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    QuickReplyPill(
                        text = "📦 Pakketje",
                        modifier = Modifier.weight(1f),
                        onClick = { onQuickReply("pakketje") }
                    )
                    QuickReplyPill(
                        text = "🏃 Kom eraan",
                        modifier = Modifier.weight(1f),
                        onClick = { onQuickReply("kom_eraan") }
                    )
                    QuickReplyPill(
                        text = "✋ Geen interesse",
                        modifier = Modifier.weight(1.1f),
                        onClick = { onQuickReply("geen_interesse") }
                    )
                }

                // Big Answer & Decline Buttons
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 24.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // Decline
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        IconButton(
                            onClick = onDecline,
                            modifier = Modifier
                                .size(72.dp)
                                .background(CoralDecline, CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.CallEnd,
                                contentDescription = "Weigeren",
                                tint = Color.White,
                                modifier = Modifier.size(36.dp)
                            )
                        }
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("Weigeren", color = TextSecondary, style = MaterialTheme.typography.bodySmall)
                    }

                    // Answer
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        IconButton(
                            onClick = onAnswer,
                            modifier = Modifier
                                .scale(answerPulseScale)
                                .size(82.dp)
                                .background(EmeraldPrimary, CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Call,
                                contentDescription = "Opnemen",
                                tint = BackgroundDark,
                                modifier = Modifier.size(42.dp)
                            )
                        }
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("Opnemen", color = EmeraldPrimary, style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Bold))
                    }
                }
            } else if (isActive && answeredByElsewhere == null) {
                // Active Call Floating Bar
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(32.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 24.dp, vertical = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(28.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Mic Mute toggle
                        IconButton(onClick = onToggleMic) {
                            Icon(
                                imageVector = if (isMicMuted) Icons.Default.MicOff else Icons.Default.Mic,
                                contentDescription = "Microfoon",
                                tint = if (isMicMuted) CoralDecline else TextPrimary,
                                modifier = Modifier.size(28.dp)
                            )
                        }

                        // Speaker toggle
                        IconButton(onClick = onToggleSpeaker) {
                            @Suppress("DEPRECATION")
                            val icon = if (isSpeakerOn) Icons.Default.VolumeUp else Icons.Default.VolumeDown
                            Icon(
                                imageVector = icon,
                                contentDescription = "Speaker",
                                tint = if (isSpeakerOn) EmeraldPrimary else TextSecondary,
                                modifier = Modifier.size(28.dp)
                            )
                        }

                        // Hangup
                        IconButton(
                            onClick = onHangup,
                            modifier = Modifier
                                .size(56.dp)
                                .background(CoralDecline, CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.CallEnd,
                                contentDescription = "Ophangen",
                                tint = Color.White,
                                modifier = Modifier.size(28.dp)
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun QuickReplyPill(
    text: String,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    Surface(
        color = SurfaceGlass,
        shape = RoundedCornerShape(24.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
        modifier = modifier.clickable { onClick() }
    ) {
        Box(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
            contentAlignment = Alignment.Center
        ) {
            Text(
                text = text,
                style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
                color = TextPrimary,
                maxLines = 1
            )
        }
    }
}
