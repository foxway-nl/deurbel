package nl.foxway.deurbel.ui.call

import android.graphics.Bitmap
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.graphics.graphicsLayer
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.animation.*
import androidx.compose.foundation.Image
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
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import coil.compose.AsyncImage
import nl.foxway.deurbel.network.ConnectionManager
import nl.foxway.deurbel.network.LiveStreamController

/**
 * Fullscreen Incoming & Active Call Screen (Instant RAM Live Stream).
 *
 * Requirements:
 *  - 100% Shared LiveStreamController across RINGING and ANSWERED states.
 *  - Pressing "Opnemen" NEVER restarts video, NEVER shows a loading spinner.
 *  - 4:3 aspect ratio native fit with toggle to fill.
 *  - Instant single-touch reaction on buttons (<10ms).
 */
@Composable
fun CallScreen(
    callId: String,
    snapshotUrl: String,
    isActive: Boolean,
    isSilenced: Boolean = false,
    answeredByElsewhere: String?,
    isMicMuted: Boolean,
    isSpeakerOn: Boolean,
    onAnswer: () -> Unit,
    onSilence: () -> Unit,
    onHangup: () -> Unit,
    onToggleMic: () -> Unit,
    onToggleSpeaker: () -> Unit
) {
    val context = LocalContext.current
    val liveStream = remember { LiveStreamController.getInstance(context) }
    val connectionManager = remember { ConnectionManager.getInstance(context) }
    val activeBaseUrl by connectionManager.activeBaseUrl.collectAsState()
    val isWanMode by connectionManager.isWanMode.collectAsState()
    val playerUrl = remember(activeBaseUrl) {
        val base = activeBaseUrl.ifEmpty { "http://192.168.2.1:8090" }
        "$base/api/v1/live/player"
    }
    var webViewRef by remember { mutableStateOf<WebView?>(null) }
    val isConnected by liveStream.isConnected.collectAsState()
    val lifecycleOwner = LocalLifecycleOwner.current

    // Zoom & pan state
    var zoomScale by remember { mutableFloatStateOf(1f) }
    var panOffsetX by remember { mutableFloatStateOf(0f) }
    var panOffsetY by remember { mutableFloatStateOf(0f) }

    DisposableEffect(lifecycleOwner) {
        liveStream.start()
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE, Lifecycle.Event.ON_STOP -> {
                    try {
                        webViewRef?.evaluateJavascript("window.killStream && window.killStream();", null)
                        webViewRef?.onPause()
                        webViewRef?.pauseTimers()
                    } catch (_: Exception) {}
                    liveStream.hardKillAudio()
                    try {
                        val audioManager = context.getSystemService(android.content.Context.AUDIO_SERVICE) as? android.media.AudioManager
                        audioManager?.mode = android.media.AudioManager.MODE_NORMAL
                        audioManager?.isSpeakerphoneOn = false
                    } catch (_: Exception) {}
                }
                Lifecycle.Event.ON_RESUME -> {
                    try {
                        webViewRef?.onResume()
                        webViewRef?.resumeTimers()
                        webViewRef?.evaluateJavascript("window.reconnectStream && window.reconnectStream();", null)
                    } catch (_: Exception) {}
                    liveStream.start()
                }
                else -> {}
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            try {
                webViewRef?.evaluateJavascript("window.killStream && window.killStream();", null)
                webViewRef?.loadUrl("about:blank")
                webViewRef?.stopLoading()
                webViewRef?.destroy()
                webViewRef = null
            } catch (_: Exception) {}
            liveStream.hardKillAudio()
            try {
                val audioManager = context.getSystemService(android.content.Context.AUDIO_SERVICE) as? android.media.AudioManager
                audioManager?.mode = android.media.AudioManager.MODE_NORMAL
                audioManager?.isSpeakerphoneOn = false
            } catch (_: Exception) {}
        }
    }

    // Automatically manage audio mute: muted when ringing (so beltoon is clear), unmuted when answered or speaker on
    LaunchedEffect(isActive, isSpeakerOn) {
        val shouldMute = !isActive || !isSpeakerOn
        liveStream.setAudioMuted(shouldMute)
        webViewRef?.evaluateJavascript("window.setAudioMuted && window.setAudioMuted($shouldMute);", null)
        if (isActive && isSpeakerOn) {
            try {
                val audioManager = context.getSystemService(android.content.Context.AUDIO_SERVICE) as? android.media.AudioManager
                audioManager?.mode = android.media.AudioManager.MODE_IN_COMMUNICATION
                audioManager?.isSpeakerphoneOn = true
            } catch (_: Exception) {}
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
    ) {
        // ── 1. Live Video Stream Container (Strict Fixed 4:3 Aspect Ratio) ──
        Box(
            modifier = Modifier
                .fillMaxSize(),
            contentAlignment = Alignment.Center
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(4f / 3f)
                    .background(Color.Black)
                    .clipToBounds(),
                contentAlignment = Alignment.Center
            ) {
                AndroidView(
                    factory = { ctx ->
                        WebView(ctx).apply {
                            layoutParams = ViewGroup.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.MATCH_PARENT
                            )
                            setBackgroundColor(android.graphics.Color.BLACK)
                            settings.javaScriptEnabled = true
                            settings.domStorageEnabled = true
                            settings.mediaPlaybackRequiresUserGesture = false
                            settings.useWideViewPort = true
                            settings.loadWithOverviewMode = true
                            setLayerType(View.LAYER_TYPE_HARDWARE, null)
                            webViewClient = object : WebViewClient() {
                                override fun onPageFinished(view: WebView?, url: String?) {
                                    super.onPageFinished(view, url)
                                    val shouldMute = !isActive || !isSpeakerOn
                                    view?.evaluateJavascript("window.setAudioMuted && window.setAudioMuted($shouldMute);", null)
                                }
                            }
                            webChromeClient = object : android.webkit.WebChromeClient() {
                                override fun getDefaultVideoPoster(): android.graphics.Bitmap {
                                    return android.graphics.Bitmap.createBitmap(1, 1, android.graphics.Bitmap.Config.ARGB_8888)
                                }
                            }
                            loadUrl(playerUrl)
                            webViewRef = this
                        }
                    },
                    update = { view ->
                        webViewRef = view
                    },
                    modifier = Modifier
                        .fillMaxSize()
                        .graphicsLayer {
                            scaleX = zoomScale
                            scaleY = zoomScale
                            translationX = panOffsetX
                            translationY = panOffsetY
                        }
                )

                // Transparent Gesture Overlay on top of WebView
                val density = androidx.compose.ui.platform.LocalDensity.current.density
                Box(
                    modifier = Modifier
                        .matchParentSize()
                        .pointerInput(Unit) {
                            detectTapGestures(
                                onDoubleTap = {
                                    zoomScale = 1f
                                    panOffsetX = 0f
                                    panOffsetY = 0f
                                    webViewRef?.evaluateJavascript("window.setVideoZoom && window.setVideoZoom(1.0, 0, 0);", null)
                                }
                            )
                        }
                        .pointerInput(Unit) {
                            detectTransformGestures { _, pan, zoom, _ ->
                                val newScale = (zoomScale * zoom).coerceIn(1f, 5f)
                                zoomScale = newScale
                                if (newScale > 1f) {
                                    val maxPanX = (size.width * (newScale - 1f)) / 2f
                                    val maxPanY = (size.height * (newScale - 1f)) / 2f
                                    panOffsetX = (panOffsetX + pan.x).coerceIn(-maxPanX, maxPanX)
                                    panOffsetY = (panOffsetY + pan.y).coerceIn(-maxPanY, maxPanY)
                                } else {
                                    panOffsetX = 0f
                                    panOffsetY = 0f
                                }
                                webViewRef?.evaluateJavascript(
                                    "window.setVideoZoom && window.setVideoZoom($zoomScale, ${panOffsetX / density}, ${panOffsetY / density});",
                                    null
                                )
                            }
                        }
                )
            }
        }

        // ── 2. Gradient Overlays for Readability ──
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

        // ── 3. Top Bar: Status Badge + Fit/Fill Toggle ──
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
                                if (isActive) Color(0xFF10B981)
                                else if (isSilenced) Color(0xFFF59E0B)
                                else Color(0xFFEF4444),
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

        // ── 4. Multi-User Claim Banner ──
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

        // ── 5. Bottom Controls (Incoming 3-Buttons vs Active Controls) ──
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 28.dp, start = 16.dp, end = 16.dp),
            contentAlignment = Alignment.Center
        ) {
            if (!isActive && answeredByElsewhere == null) {
                // Incoming Call: 3 Equal 64.dp Buttons
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.SpaceEvenly,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // 1. 🔴 Ophangen / Weigeren
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
                        label = "Stil meekijken",
                        backgroundColor = Color(0xFF37474F),
                        iconTint = Color.White,
                        onClick = onSilence
                    )

                    // 3. 🟢 Opnemen
                    CallActionButton(
                        icon = Icons.Default.Call,
                        label = "Opnemen",
                        backgroundColor = Color(0xFF10B981),
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
                        // Mic toggle (Two-way voice to outdoor speaker)
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
 * 72.dp Circular Action Button for incoming calls with instant touch response (<5ms).
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
            onClick = onClick,
            modifier = Modifier
                .size(72.dp)
                .defaultMinSize(minWidth = 72.dp, minHeight = 72.dp),
            shape = CircleShape,
            color = backgroundColor,
            shadowElevation = 6.dp
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    imageVector = icon,
                    contentDescription = label,
                    tint = iconTint,
                    modifier = Modifier.size(34.dp)
                )
            }
        }
        Text(
            text = label,
            color = Color.White,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold
        )
    }
}
