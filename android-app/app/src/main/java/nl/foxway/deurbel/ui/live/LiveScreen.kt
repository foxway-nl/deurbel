package nl.foxway.deurbel.ui.live

import android.view.MotionEvent
import android.widget.Toast
import androidx.compose.animation.core.*
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.pointerInteropFilter
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.audio.DoorbellAudioStreamer
import nl.foxway.deurbel.data.models.ManualRecordStart
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.ConnectionManager
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.network.EndpointResolver.ConnectionType
import nl.foxway.deurbel.network.LiveStreamController
import nl.foxway.deurbel.ui.theme.*

/**
 * Production-Grade Live View Screen (Always-Warm RAM WebSocket Stream):
 *  - 5G & LAN Instant Viewing: Zero black screens, zero UDP blocking.
 *  - First frame arrives in <100ms directly from server memory.
 *  - Two-way Talkback: Hold-to-Talk pipes microphone audio to outdoor speaker.
 *  - Audio Listen: Real-time 16kHz PCM audio streaming from doorbell mic.
 *  - 4:3 Native Aspect Ratio Fit / Fill toggle.
 *  - Manual Live Recording: Prominent "Opnemen" toggle with red pulsing timer.
 */
@OptIn(ExperimentalComposeUiApi::class)
@Composable
fun LiveScreen() {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val resolver = remember { EndpointResolver(prefs) }

    val connectionManager = remember { ConnectionManager.getInstance(context) }
    val activeBaseUrl by connectionManager.activeBaseUrl.collectAsState()
    val isWanMode by connectionManager.isWanMode.collectAsState()
    val connectionType by resolver.connectionType.collectAsState()

    val liveStream = remember { LiveStreamController.getInstance(context) }
    val frame by liveStream.currentBitmap.collectAsState()
    val isConnected by liveStream.isConnected.collectAsState()

    val audioStreamer = remember { DoorbellAudioStreamer() }

    var isPttActive by remember { mutableStateOf(false) }
    var isFitMode by remember { mutableStateOf(true) }
    var isSpeakerMuted by remember { mutableStateOf(false) }

    // Manual recording state
    var isRecording by remember { mutableStateOf(false) }
    var recordSeconds by remember { mutableStateOf(0) }

    // Pulsing animation for recording dot
    val infiniteTransition = rememberInfiniteTransition(label = "recording_pulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 0.2f,
        animationSpec = infiniteRepeatable(
            animation = tween(600, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulse_alpha"
    )

    // Start live stream when screen enters
    LaunchedEffect(Unit) {
        liveStream.start()
        liveStream.setAudioMuted(isSpeakerMuted)
    }

    LaunchedEffect(isSpeakerMuted) {
        liveStream.setAudioMuted(isSpeakerMuted)
    }

    // Timer effect while recording is active
    LaunchedEffect(isRecording) {
        if (isRecording) {
            recordSeconds = 0
            while (isRecording) {
                delay(1000L)
                recordSeconds++
            }
        }
    }

    // Toggle recording helper
    val toggleRecording = {
        coroutineScope.launch {
            try {
                val targetUrl = activeBaseUrl.ifEmpty { resolver.activeApiUrl.value }
                val api = ApiClientFactory.createApi(targetUrl)
                val userName = prefs.getUserName().ifEmpty { "Gezinslid" }
                if (!isRecording) {
                    val resp = api.startManualRecording(ManualRecordStart(userName, "deurbel"))
                    if (resp.isSuccessful) {
                        isRecording = true
                        Toast.makeText(context, "Opname gestart…", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    isRecording = false
                    val resp = api.stopManualRecording(userName)
                    if (resp.isSuccessful) {
                        Toast.makeText(context, "✅ Handmatige opname opgeslagen in Clips", Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                isRecording = false
            }
        }
    }

    // Lifecycle binding
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE, Lifecycle.Event.ON_STOP -> {
                    if (isRecording) {
                        isRecording = false
                    }
                    audioStreamer.stopStreaming()
                }
                Lifecycle.Event.ON_RESUME -> {
                    liveStream.start()
                }
                else -> {}
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            audioStreamer.stopStreaming()
        }
    }

    // Native Pinch-to-Zoom & Drag-Panning state (strictly isolated to video container)
    var zoomScale by remember { mutableFloatStateOf(1f) }
    var panOffsetX by remember { mutableFloatStateOf(0f) }
    var panOffsetY by remember { mutableFloatStateOf(0f) }

    LaunchedEffect(isFitMode) {
        zoomScale = 1f
        panOffsetX = 0f
        panOffsetY = 0f
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(BackgroundDark)
    ) {
        // ── 1. Video Container (Native 4:3 Fit / Fill) ──
        Box(
            modifier = Modifier
                .fillMaxSize()
                .clipToBounds(),
            contentAlignment = Alignment.Center
        ) {
            Box(
                modifier = (if (isFitMode) {
                    Modifier
                        .fillMaxWidth()
                        .aspectRatio(4f / 3f)
                } else {
                    Modifier.fillMaxSize()
                })
                    .graphicsLayer {
                        scaleX = zoomScale
                        scaleY = zoomScale
                        translationX = panOffsetX
                        translationY = panOffsetY
                    }
                    .pointerInput(Unit) {
                        detectTapGestures(
                            onDoubleTap = {
                                if (zoomScale > 1.05f) {
                                    zoomScale = 1f
                                    panOffsetX = 0f
                                    panOffsetY = 0f
                                } else {
                                    zoomScale = 2.5f
                                }
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
                        }
                    },
                contentAlignment = Alignment.Center
            ) {
                if (frame != null) {
                    Image(
                        bitmap = frame!!.asImageBitmap(),
                        contentDescription = "Live beeld voordeur",
                        contentScale = if (isFitMode) ContentScale.Fit else ContentScale.Crop,
                        modifier = Modifier.fillMaxSize()
                    )
                } else {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .background(Color(0xFF0F172A)),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(
                            color = EmeraldPrimary,
                            modifier = Modifier.size(36.dp),
                            strokeWidth = 3.dp
                        )
                    }
                }
            }

            // Floating Zoom Badge
            if (zoomScale > 1.05f) {
                Surface(
                    color = Color.Black.copy(alpha = 0.70f),
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
                    modifier = Modifier
                        .align(Alignment.TopCenter)
                        .padding(top = 64.dp)
                        .clickable {
                            zoomScale = 1f
                            panOffsetX = 0f
                            panOffsetY = 0f
                        }
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 14.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Icon(Icons.Default.ZoomIn, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(16.dp))
                        Text(
                            text = "${String.format("%.1fx", zoomScale)} (Tik om te resetten)",
                            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                    }
                }
            }
        }

        // Gradient overlay for controls
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        colors = listOf(
                            Color.Black.copy(alpha = 0.65f),
                            Color.Transparent,
                            Color.Black.copy(alpha = 0.80f)
                        )
                    )
                )
        )

        // ── 2. Top Bar: Connection Badge, Audio Mute & Aspect Ratio ──
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Connection badge
            Surface(
                color = SurfaceGlass,
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .background(
                                if (!isWanMode) LanGreen else CyanAccent,
                                CircleShape
                            )
                    )
                    Text(
                        text = if (!isWanMode) "🟢 Lokaal" else "🔵 Online (5G)",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
                        color = TextPrimary
                    )
                }
            }

            // Controls: Mute Speaker + Fit/Fill
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Speaker Mute Toggle
                Surface(
                    color = if (isSpeakerMuted) CoralDecline.copy(alpha = 0.3f) else SurfaceGlass,
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, if (isSpeakerMuted) CoralDecline else DividerDark),
                    modifier = Modifier.clickable { isSpeakerMuted = !isSpeakerMuted }
                ) {
                    Box(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                        Icon(
                            imageVector = if (isSpeakerMuted) Icons.Default.VolumeOff else Icons.Default.VolumeUp,
                            contentDescription = "Deurbel Geluid",
                            tint = if (isSpeakerMuted) CoralDecline else EmeraldPrimary,
                            modifier = Modifier.size(18.dp)
                        )
                    }
                }

                // Fit / Fill Toggle
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
                    modifier = Modifier.clickable { isFitMode = !isFitMode }
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
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
                            text = if (isFitMode) "4:3 Fit" else "Fill",
                            style = MaterialTheme.typography.labelSmall,
                            color = TextPrimary
                        )
                    }
                }
            }
        }

        // Active microphone banner
        if (isPttActive) {
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

        // ── 3. Bottom Control Bar: Opnemen (Manual Record) + Push-to-Talk ──
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 24.dp, start = 20.dp, end = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // Manual Live Recording Button
            Surface(
                color = if (isRecording) Color(0xFF7F1D1D).copy(alpha = 0.9f) else SurfaceGlass,
                shape = RoundedCornerShape(24.dp),
                border = androidx.compose.foundation.BorderStroke(
                    1.5.dp,
                    if (isRecording) Color(0xFFEF4444) else DividerDark
                ),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(48.dp)
                    .clickable { toggleRecording() }
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Box(
                        modifier = Modifier
                            .size(12.dp)
                            .alpha(if (isRecording) pulseAlpha else 1f)
                            .background(Color(0xFFEF4444), CircleShape)
                    )
                    Spacer(modifier = Modifier.width(10.dp))
                    Text(
                        text = if (isRecording) {
                            val min = recordSeconds / 60
                            val sec = recordSeconds % 60
                            "OPNEMEN STOPPEN (%02d:%02d)".format(min, sec)
                        } else {
                            "OPNEMEN (LIVE CLIP OPSLAAN)"
                        },
                        color = if (isRecording) Color.White else TextPrimary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 13.sp
                    )
                }
            }

            // Push-to-Talk Button
            Surface(
                color = if (isPttActive) Color(0xFFDC2626) else SurfaceGlass,
                shape = RoundedCornerShape(28.dp),
                border = androidx.compose.foundation.BorderStroke(
                    2.dp,
                    if (isPttActive) Color.White else DividerDark
                ),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(58.dp)
                    .pointerInteropFilter { motionEvent ->
                        when (motionEvent.action) {
                            MotionEvent.ACTION_DOWN -> {
                                isPttActive = true
                                val targetUrl = activeBaseUrl.ifEmpty { "https://deurbel.foxway.nl" }
                                audioStreamer.startStreaming(targetUrl)
                                true
                            }
                            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                                isPttActive = false
                                audioStreamer.stopStreaming()
                                true
                            }
                            else -> false
                        }
                    }
            ) {
                Row(
                    modifier = Modifier.fillMaxSize(),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        imageVector = if (isPttActive) Icons.Default.Mic else Icons.Default.MicOff,
                        contentDescription = "Push-to-Talk",
                        tint = if (isPttActive) Color.White else EmeraldPrimary,
                        modifier = Modifier.size(24.dp)
                    )
                    Spacer(modifier = Modifier.width(10.dp))
                    Text(
                        text = if (isPttActive) "Laat los om te stoppen met praten" else "Ingedrukt houden om te praten",
                        color = if (isPttActive) Color.White else TextPrimary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 14.sp
                    )
                }
            }
        }
    }
}
