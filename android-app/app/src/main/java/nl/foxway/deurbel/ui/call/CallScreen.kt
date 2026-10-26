package nl.foxway.deurbel.ui.call

import androidx.compose.animation.*
import androidx.compose.foundation.background
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import coil.compose.AsyncImage
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import nl.foxway.deurbel.network.ConnectionManager
import nl.foxway.deurbel.ui.theme.*
import nl.foxway.deurbel.webrtc.WebRtcSession
import org.webrtc.SurfaceViewRenderer

/**
 * Professional Incoming & Active Doorbell Call UI.
 *
 * Implements Task 2:
 *  - Immediate Pre-Answer Live Video Preview: video starts automatically when screen opens.
 *  - 100% removal of quick-reply pills ("Pakketje", "Ik kom eraan", "Geen...").
 *  - 3 equally sized (64.dp) instant-response buttons in horizontal row:
 *      1. 🔴 Ophangen / Sluiten (64.dp, #E53935)
 *      2. 🔕 Stil / Negeren (64.dp, #37474F, label "Stil kijker")
 *      3. 🟢 Opnemen (64.dp, #2E7D32)
 *  - Active Call controls: Hangup, Mic toggle, Speaker toggle.
 *  - Reactive 5G/WAN switch without reload via LaunchedEffect(activeBaseUrl).
 */
@Composable
fun CallScreen(
    callId: String,
    snapshotUrl: String,
    isActive: Boolean,
    isSilenced: Boolean = false,
    answeredByElsewhere: String?,
    webRtcSession: WebRtcSession?,
    isMicMuted: Boolean,
    isSpeakerOn: Boolean,
    onAnswer: () -> Unit,
    onSilence: () -> Unit,
    onHangup: () -> Unit,
    onToggleMic: () -> Unit,
    onToggleSpeaker: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val connectionManager = remember { ConnectionManager.getInstance(context) }
    val activeBaseUrl by connectionManager.activeBaseUrl.collectAsState()
    val isWanMode by connectionManager.isWanMode.collectAsState()

    val hasWebRtcVideo by (webRtcSession?.hasVideoTrack ?: remember { MutableStateFlow(false) }).collectAsState()
    var isExoBuffering by remember { mutableStateOf(true) }
    var isFitMode by remember { mutableStateOf(true) }

    // ExoPlayer for instant pre-answer live stream and 5G playback
    val exoPlayer = remember {
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(400, 1200, 200, 400)
            .build()
        ExoPlayer.Builder(context)
            .setLoadControl(loadControl)
            .build().apply {
                playWhenReady = true
                repeatMode = Player.REPEAT_MODE_OFF
            }
    }

    DisposableEffect(exoPlayer) {
        val listener = object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                isExoBuffering = (state == Player.STATE_BUFFERING)
            }
            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                isExoBuffering = false
                // Auto reconnect after 500ms
                coroutineScope.launch {
                    delay(500L)
                    connectionManager.refreshRoute()
                    val currentBase = connectionManager.activeBaseUrl.value.ifBlank { "https://deurbel.foxway.nl" }
                    val streamUrl = "$currentBase/api/v1/stream.mp4?src=deurbel_sd"
                    val mediaItem = MediaItem.fromUri(streamUrl)
                    exoPlayer.setMediaItem(mediaItem)
                    exoPlayer.prepare()
                    exoPlayer.play()
                }
            }
        }
        exoPlayer.addListener(listener)
        onDispose {
            exoPlayer.removeListener(listener)
            exoPlayer.stop()
            exoPlayer.release()
        }
    }

    // Direct Pre-Answer Live Stream Auto-Start & Network Switch (Task 1 & Task 2)
    LaunchedEffect(activeBaseUrl) {
        if (activeBaseUrl.isNotBlank()) {
            val streamUrl = "$activeBaseUrl/api/v1/stream.mp4?src=deurbel_sd"
            val mediaItem = MediaItem.Builder()
                .setUri(streamUrl)
                .setLiveConfiguration(
                    MediaItem.LiveConfiguration.Builder()
                        .setMaxPlaybackSpeed(1.02f)
                        .build()
                )
                .build()
            exoPlayer.setMediaItem(mediaItem)
            exoPlayer.prepare()
            exoPlayer.play()
        }
    }

    // Control volume: muted during pre-answer ringtone so beltoon isn't disturbed, unmuted after answer
    LaunchedEffect(isActive, isSpeakerOn) {
        exoPlayer.volume = if (isActive && isSpeakerOn) 1f else 0f
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
    ) {
        // ── 1. Live Video Stream / Snapshot Background (Always active) ──
        Box(
            modifier = Modifier.fillMaxSize(),
            contentAlignment = Alignment.Center
        ) {
            if (isActive && webRtcSession != null && hasWebRtcVideo) {
                AndroidView(
                    factory = { ctx ->
                        SurfaceViewRenderer(ctx).apply {
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
            } else {
                // Live ExoPlayer stream running before and after answering
                AndroidView(
                    factory = { ctx ->
                        PlayerView(ctx).apply {
                            player = exoPlayer
                            useController = false
                            resizeMode = if (isFitMode) AspectRatioFrameLayout.RESIZE_MODE_FIT
                            else AspectRatioFrameLayout.RESIZE_MODE_ZOOM
                        }
                    },
                    update = { view ->
                        view.player = exoPlayer
                        view.resizeMode = if (isFitMode) AspectRatioFrameLayout.RESIZE_MODE_FIT
                        else AspectRatioFrameLayout.RESIZE_MODE_ZOOM
                    },
                    modifier = if (isFitMode) {
                        Modifier
                            .fillMaxWidth()
                            .aspectRatio(16f / 9f)
                    } else {
                        Modifier.fillMaxSize()
                    }
                )

                if (isExoBuffering && snapshotUrl.isNotBlank()) {
                    AsyncImage(
                        model = snapshotUrl,
                        contentDescription = "Snapshot preview",
                        contentScale = if (isFitMode) ContentScale.Fit else ContentScale.Crop,
                        modifier = if (isFitMode) Modifier.fillMaxWidth().aspectRatio(16f / 9f) else Modifier.fillMaxSize()
                    )
                }

                if (isExoBuffering) {
                    CircularProgressIndicator(color = Color(0xFF2E7D32), modifier = Modifier.size(36.dp))
                }
            }
        }

        // Dark gradient overlay for readability of controls
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

        // ── 2. Top Bar: Caller Info & Status Badge + Aspect Ratio Toggle ──
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(top = 16.dp, start = 20.dp, end = 20.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Surface(
                color = Color(0xCC1E293B),
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155))
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .background(
                                if (isActive) Color(0xFF2E7D32)
                                else if (isSilenced) Color(0xFFF59E0B)
                                else Color(0xFFE53935),
                                CircleShape
                            )
                    )
                    Text(
                        text = if (isActive) "Actief gesprek • Voordeur"
                        else if (isSilenced) "Stil meekijken • Voordeur"
                        else "Aanbellen • Live beeld",
                        style = MaterialTheme.typography.labelMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = Color.White
                    )
                }
            }

            // Fit / Fill toggle
            Surface(
                color = Color(0xCC1E293B),
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155)),
                modifier = Modifier.clickable { isFitMode = !isFitMode }
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Icon(
                        imageVector = if (isFitMode) Icons.Default.AspectRatio else Icons.Default.FitScreen,
                        contentDescription = "Beeldverhouding",
                        tint = Color(0xFF38BDF8),
                        modifier = Modifier.size(16.dp)
                    )
                    Text(
                        text = if (isFitMode) "Fit" else "Fill",
                        style = MaterialTheme.typography.labelSmall,
                        color = Color.White
                    )
                }
            }
        }

        // Active microphone banner
        if (isActive && !isMicMuted) {
            Surface(
                color = Color(0xFFDC2626),
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

        // ── 3. Multi-User Claim Banner ──
        AnimatedVisibility(
            visible = answeredByElsewhere != null,
            enter = fadeIn(),
            exit = fadeOut(),
            modifier = Modifier
                .align(Alignment.Center)
                .padding(24.dp)
        ) {
            Surface(
                color = Color(0xFF0F172A),
                shape = RoundedCornerShape(16.dp),
                border = androidx.compose.foundation.BorderStroke(2.dp, Color(0xFF38BDF8)),
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
                        tint = Color(0xFF38BDF8),
                        modifier = Modifier.size(48.dp)
                    )
                    Text(
                        text = "Beantwoord door $answeredByElsewhere",
                        style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                        color = Color.White,
                        textAlign = TextAlign.Center
                    )
                    Text(
                        text = "Deze oproep wordt gesloten…",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color(0xFF94A3B8)
                    )
                }
            }
        }

        // ── 4. Bottom Controls: Incoming (3 equal 64.dp buttons) vs Active ──
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 28.dp, start = 16.dp, end = 16.dp),
            contentAlignment = Alignment.Center
        ) {
            if (!isActive && answeredByElsewhere == null) {
                // 3 Perfect Geproportioneerde, Direct Reagerende Knoppen (Task 2)
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.SpaceEvenly,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // 1. 🔴 Ophangen / Sluiten
                    CallActionButton(
                        icon = Icons.Default.CallEnd,
                        label = "Ophangen",
                        backgroundColor = Color(0xFFE53935),
                        iconTint = Color.White,
                        onClick = onHangup
                    )

                    // 2. 🔕 Stil / Negeren
                    CallActionButton(
                        icon = Icons.Default.NotificationsOff,
                        label = "Stil kijker",
                        backgroundColor = Color(0xFF37474F),
                        iconTint = Color.White,
                        onClick = onSilence
                    )

                    // 3. 🟢 Opnemen (EXACT 64.dp, same as red)
                    CallActionButton(
                        icon = Icons.Default.Call,
                        label = "Opnemen",
                        backgroundColor = Color(0xFF2E7D32),
                        iconTint = Color.White,
                        onClick = onAnswer
                    )
                }
            } else if (isActive && answeredByElsewhere == null) {
                // Active In-Call Floating Control Bar
                Surface(
                    color = Color(0xCC0F172A),
                    shape = RoundedCornerShape(36.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155))
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 24.dp, vertical = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(24.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Mic toggle
                        IconButton(
                            onClick = onToggleMic,
                            modifier = Modifier
                                .size(52.dp)
                                .background(
                                    if (!isMicMuted) Color(0xFFDC2626) else Color(0x33FFFFFF),
                                    CircleShape
                                )
                        ) {
                            Icon(
                                imageVector = if (isMicMuted) Icons.Default.MicOff else Icons.Default.Mic,
                                contentDescription = "Microfoon",
                                tint = Color.White,
                                modifier = Modifier.size(26.dp)
                            )
                        }

                        // 🔴 Ophangen
                        IconButton(
                            onClick = onHangup,
                            modifier = Modifier
                                .size(64.dp)
                                .background(Color(0xFFE53935), CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.CallEnd,
                                contentDescription = "Ophangen",
                                tint = Color.White,
                                modifier = Modifier.size(32.dp)
                            )
                        }

                        // Speaker toggle
                        IconButton(
                            onClick = onToggleSpeaker,
                            modifier = Modifier
                                .size(52.dp)
                                .background(Color(0x33FFFFFF), CircleShape)
                        ) {
                            Icon(
                                imageVector = if (isSpeakerOn) Icons.Default.VolumeUp else Icons.Default.VolumeOff,
                                contentDescription = "Luidspreker",
                                tint = Color.White,
                                modifier = Modifier.size(26.dp)
                            )
                        }
                    }
                }
            }
        }
    }
}

/**
 * 64.dp Circular Action Button with Label for incoming calls.
 * Immediate touch feedback without heavy animation loops (<10ms UI response).
 */
@Composable
private fun CallActionButton(
    icon: ImageVector,
    label: String,
    backgroundColor: Color,
    iconTint: Color,
    onClick: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Surface(
            modifier = Modifier
                .size(64.dp)
                .clip(CircleShape)
                .clickable(onClick = onClick),
            shape = CircleShape,
            color = backgroundColor,
            shadowElevation = 4.dp
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    imageVector = icon,
                    contentDescription = label,
                    tint = iconTint,
                    modifier = Modifier.size(30.dp)
                )
            }
        }
        Text(
            text = label,
            color = Color.White,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium
        )
    }
}
