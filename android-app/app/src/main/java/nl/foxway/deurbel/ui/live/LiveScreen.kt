package nl.foxway.deurbel.ui.live

import android.view.MotionEvent
import android.widget.Toast
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
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInteropFilter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.audio.DoorbellAudioStreamer
import nl.foxway.deurbel.data.models.ManualRecordStart
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.network.EndpointResolver.ConnectionType
import nl.foxway.deurbel.ui.theme.*
import nl.foxway.deurbel.webrtc.WebRtcSession
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer

/**
 * Production-Grade Live View Screen:
 *  - 5G / WAN Resilience: Automatically streams buffer-free HTTPS MP4 over Port 443 via ExoPlayer
 *    when on 5G/WAN or when WebRTC UDP fails to connect.
 *  - LAN Ultra Low Latency: Hardware-accelerated WebRTC via go2rtc WHEP with fallback to ExoPlayer.
 *  - Unmistakable loading feedback: "Verbinden met 5G live stream..." and retry on error.
 *  - Two-way Talkback: Push-to-Talk works over WebRTC on LAN and over WebSocket audio stream on 5G.
 *  - Fullscreen / Aspect Ratio toggle (Fit vs Fill) and Speaker Mute / Unmute.
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

    val connectionType by resolver.connectionType.collectAsState()
    var session by remember { mutableStateOf<WebRtcSession?>(null) }
    val audioStreamer = remember { DoorbellAudioStreamer() }
    var activeBaseUrl by remember { mutableStateOf("") }

    var isPttActive by remember { mutableStateOf(false) }
    var isFitMode by remember { mutableStateOf(true) }
    var isHd by remember { mutableStateOf(false) } // Default SD for fast mobile & 5G loading
    var isSpeakerMuted by remember { mutableStateOf(false) }

    // Stream mode: WebRTC or ExoPlayer (for 5G/WAN)
    var isExoMode by remember { mutableStateOf(false) }
    var isStreamBuffering by remember { mutableStateOf(true) }
    var streamErrorMessage by remember { mutableStateOf<String?>(null) }

    // Manual recording state
    var isRecording by remember { mutableStateOf(false) }
    var recordSeconds by remember { mutableStateOf(0) }

    // Pulsing animation for the recording dot
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

    // ExoPlayer instance for 5G / WAN playback
    val exoPlayer = remember {
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(
                500,  // minBufferMs
                1500, // maxBufferMs
                250,  // bufferForPlaybackMs
                500   // bufferForPlaybackAfterRebufferMs
            )
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
                isStreamBuffering = (state == Player.STATE_BUFFERING)
                if (state == Player.STATE_READY) {
                    streamErrorMessage = null
                }
            }
            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                isStreamBuffering = false
                streamErrorMessage = "5G live stream onderbroken (${error.localizedMessage ?: "Verbindingsfout"})"
            }
        }
        exoPlayer.addListener(listener)
        onDispose {
            exoPlayer.removeListener(listener)
            exoPlayer.stop()
            exoPlayer.release()
        }
    }

    LaunchedEffect(isSpeakerMuted) {
        exoPlayer.volume = if (isSpeakerMuted) 0f else 1f
        session?.setRemoteAudioMuted(isSpeakerMuted)
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

    // Helper to start stream
    val startStream = {
        coroutineScope.launch {
            try {
                session?.setMicrophoneMuted(true)
                session?.close()
                session = null
                exoPlayer.stop()
                streamErrorMessage = null
                isStreamBuffering = true

                val resolved = resolver.resolve()
                activeBaseUrl = resolved.apiBaseUrl
                val streamName = if (isHd) "deurbel" else "deurbel_sub"

                // Check connection type: On WAN/5G, use ExoPlayer over HTTPS port 443 directly
                if (connectionType == ConnectionType.WAN) {
                    isExoMode = true
                    val streamUrl = "${resolved.apiBaseUrl}/api/v1/stream.mp4?src=$streamName"
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
                } else {
                    // LAN mode: try direct WebRTC with 2.5s fallback to ExoPlayer
                    isExoMode = false
                    val s = WebRtcSession(context.applicationContext)
                    session = s
                    s.setMicrophoneMuted(true)
                    s.setRemoteAudioMuted(isSpeakerMuted)
                    s.start(
                        signalingUrl = resolved.apiBaseUrl,
                        streamName = streamName,
                        isPrewarm = false
                    )

                    // Watchdog: If WebRTC does not receive video within 2.5s, fall back to ExoPlayer
                    delay(2500L)
                    if (session == s && !s.hasVideoTrack.value) {
                        isExoMode = true
                        s.close()
                        session = null
                        val streamUrl = "${resolved.apiBaseUrl}/api/v1/stream.mp4?src=$streamName"
                        val mediaItem = MediaItem.fromUri(streamUrl)
                        exoPlayer.setMediaItem(mediaItem)
                        exoPlayer.prepare()
                    } else if (session == s) {
                        isStreamBuffering = false
                    }
                }
            } catch (e: Exception) {
                streamErrorMessage = "Verbindingsfout: ${e.localizedMessage}"
                isStreamBuffering = false
            }
        }
    }

    // Toggle recording helper
    val toggleRecording = {
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi(activeBaseUrl.ifEmpty { "https://deurbel.foxway.nl" })
                val userName = prefs.getUserName().ifEmpty { "Gezinslid" }
                if (!isRecording) {
                    val streamName = if (isHd) "deurbel" else "deurbel_sub"
                    val resp = api.startManualRecording(ManualRecordStart(userName, streamName))
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

    // Strict Android Lifecycle binding: Stop audio & player on background
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE, Lifecycle.Event.ON_STOP -> {
                    if (isRecording) {
                        isRecording = false
                    }
                    audioStreamer.stopStreaming()
                    session?.setMicrophoneMuted(true)
                    session?.close()
                    session = null
                    exoPlayer.stop()
                }
                Lifecycle.Event.ON_RESUME -> {
                    startStream()
                }
                else -> {}
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            audioStreamer.stopStreaming()
            session?.setMicrophoneMuted(true)
            session?.close()
            session = null
            exoPlayer.stop()
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(BackgroundDark)
    ) {
        // ── Video Renderer (WebRTC or ExoPlayer) ──
        if (isExoMode) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
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
            }
        } else {
            session?.let { activeSession ->
                Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    AndroidView(
                        factory = { ctx ->
                            SurfaceViewRenderer(ctx).apply {
                                val scaling = if (isFitMode) RendererCommon.ScalingType.SCALE_ASPECT_FIT
                                else RendererCommon.ScalingType.SCALE_ASPECT_FILL
                                setScalingType(scaling)
                                setEnableHardwareScaler(true)
                                activeSession.attachRenderer(this, scaling)
                                requestLayout()
                            }
                        },
                        update = { view ->
                            val scaling = if (isFitMode) RendererCommon.ScalingType.SCALE_ASPECT_FIT
                            else RendererCommon.ScalingType.SCALE_ASPECT_FILL
                            view.setScalingType(scaling)
                            view.requestLayout()
                            activeSession.setScalingType(scaling)
                        },
                        modifier = if (isFitMode) {
                            Modifier
                                .fillMaxWidth()
                                .aspectRatio(16f / 9f)
                        } else {
                            Modifier.fillMaxSize()
                        }
                    )
                }
            }
        }

        // ── Loading & Buffering Indicator ──
        if (isStreamBuffering && streamErrorMessage == null) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color.Black.copy(alpha = 0.45f)),
                contentAlignment = Alignment.Center
            ) {
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(16.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 20.dp, vertical = 14.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        CircularProgressIndicator(color = EmeraldPrimary, modifier = Modifier.size(22.dp), strokeWidth = 2.5.dp)
                        Text(
                            text = if (connectionType == ConnectionType.WAN) "Verbinden met 5G live stream…" else "Verbinden met deurbel…",
                            style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                            color = TextPrimary
                        )
                    }
                }
            }
        }

        // ── Stream Error Banner with Retry ──
        streamErrorMessage?.let { err ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color.Black.copy(alpha = 0.7f)),
                contentAlignment = Alignment.Center
            ) {
                Surface(
                    color = SurfaceDark,
                    shape = RoundedCornerShape(16.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, CoralDecline),
                    modifier = Modifier.padding(24.dp)
                ) {
                    Column(
                        modifier = Modifier.padding(20.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Icon(Icons.Default.WifiOff, contentDescription = null, tint = CoralDecline, modifier = Modifier.size(36.dp))
                        Text(
                            text = err,
                            style = MaterialTheme.typography.bodyMedium,
                            color = TextPrimary,
                            textAlign = androidx.compose.ui.text.style.TextAlign.Center
                        )
                        Button(
                            onClick = { startStream() },
                            colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                            shape = RoundedCornerShape(10.dp)
                        ) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text("Opnieuw verbinden", color = BackgroundDark, fontWeight = FontWeight.Bold)
                        }
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

        // ── Top Bar: Connection Badge, Quality (SD/HD), Audio Mute & Aspect Ratio ──
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
                                if (connectionType == ConnectionType.LAN) LanGreen else CyanAccent,
                                CircleShape
                            )
                    )
                    Text(
                        text = if (connectionType == ConnectionType.LAN) "🟢 Direct LAN" else "🌐 5G / WAN Live",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
                        color = TextPrimary
                    )
                }
            }

            // Controls: Mute Speaker + SD/HD Toggle + Fit/Fill
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

                // SD / HD Toggle
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
                    modifier = Modifier.clickable {
                        isHd = !isHd
                        startStream()
                    }
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        Text(
                            text = if (isHd) "HD 1080p" else "SD 432p",
                            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                            color = if (isHd) EmeraldPrimary else CyanAccent
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
                            text = if (isFitMode) "Fit" else "Fill",
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

        // ── Bottom Control Bar: Opnemen (Manual Record) + Push-to-Talk ──
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 24.dp, start = 20.dp, end = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // ── Manual Live Recording Pill Button ──
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
                    // Pulsing Red Dot
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

            // ── Push-to-Talk (PTT) Pill Button ──
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
                                if (isExoMode) {
                                    audioStreamer.startStreaming(activeBaseUrl)
                                } else {
                                    session?.setMicrophoneMuted(false)
                                }
                                true
                            }
                            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                                isPttActive = false
                                if (isExoMode) {
                                    audioStreamer.stopStreaming()
                                } else {
                                    session?.setMicrophoneMuted(true)
                                }
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
