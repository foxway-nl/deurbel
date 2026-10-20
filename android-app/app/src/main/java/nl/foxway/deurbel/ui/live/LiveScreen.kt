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
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.ManualRecordStart
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.ui.theme.*
import nl.foxway.deurbel.webrtc.WebRtcSession
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer

/**
 * Production-Grade Live View Screen:
 *  - Lifecycle-bound: Stream and talk strictly disconnect/mute on ON_PAUSE/ON_STOP.
 *  - Adaptive Quality: Toggle between SD (fast 5G load, 432p) and HD (1080p).
 *  - Manual Live Recording: Prominent "Opnemen" toggle with red pulsing timer (00:12).
 *  - Stealth Talkback: Mic is muted by default; Push-to-Talk pipes to Port 8000 AAC-LC.
 *  - Dynamic LAN 🟢 / 5G 🌐 indicator.
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
    var activeBaseUrl by remember { mutableStateOf("") }
    var isPttActive by remember { mutableStateOf(false) }
    var isFitMode by remember { mutableStateOf(true) }
    var isHd by remember { mutableStateOf(false) } // Default SD on mobile for ultra-fast 5G load

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

                val resolved = resolver.resolve()
                activeBaseUrl = resolved.apiBaseUrl
                val s = WebRtcSession(context.applicationContext)
                session = s
                s.setMicrophoneMuted(true)
                val streamName = if (isHd) "deurbel" else "deurbel_sub"
                s.start(
                    signalingUrl = resolved.apiBaseUrl,
                    streamName = streamName,
                    isPrewarm = false
                )
            } catch (e: Exception) {
                // Handled gracefully
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
                    // Mute and disconnect immediately when leaving screen or locking phone
                    if (isRecording) {
                        isRecording = false
                    }
                    session?.setMicrophoneMuted(true)
                    session?.close()
                    session = null
                }
                Lifecycle.Event.ON_RESUME -> {
                    if (session == null) {
                        startStream()
                    }
                }
                else -> {}
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            session?.setMicrophoneMuted(true)
            session?.close()
            session = null
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(BackgroundDark)
    ) {
        // ── Video Renderer ──
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
        } ?: run {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(color = EmeraldPrimary)
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

        // ── Top Bar: Connection Badge, Quality (SD/HD), and Aspect Ratio ──
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
                                if (connectionType == EndpointResolver.ConnectionType.LAN) LanGreen else WanBlue,
                                CircleShape
                            )
                    )
                    Text(
                        text = if (connectionType == EndpointResolver.ConnectionType.LAN) "LAN (Thuis)" else "5G (Onderweg)",
                        style = MaterialTheme.typography.labelMedium,
                        color = TextPrimary
                    )
                }
            }

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                // Adaptive SD / HD Quality Toggle
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, if (isHd) EmeraldPrimary else DividerDark),
                    modifier = Modifier.clickable {
                        isHd = !isHd
                        startStream()
                    }
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Icon(
                            imageVector = if (isHd) Icons.Default.HighQuality else Icons.Default.Speed,
                            contentDescription = "Kwaliteit",
                            tint = if (isHd) EmeraldPrimary else CyanAccent,
                            modifier = Modifier.size(16.dp)
                        )
                        Text(
                            text = if (isHd) "HD 1080p" else "SD (5G)",
                            style = MaterialTheme.typography.labelSmall,
                            color = if (isHd) EmeraldPrimary else TextPrimary,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }

                // Aspect ratio toggle button (Fit vs Fill)
                Surface(
                    color = SurfaceGlass,
                    shape = RoundedCornerShape(20.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark),
                    modifier = Modifier.clickable {
                        isFitMode = !isFitMode
                        session?.setScalingType(
                            if (isFitMode) RendererCommon.ScalingType.SCALE_ASPECT_FIT
                            else RendererCommon.ScalingType.SCALE_ASPECT_FILL
                        )
                    }
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
                                session?.setMicrophoneMuted(false)
                                true
                            }
                            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                                isPttActive = false
                                session?.setMicrophoneMuted(true)
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
                        tint = if (isPttActive) Color.White else TextPrimary,
                        modifier = Modifier.size(26.dp)
                    )
                    Spacer(modifier = Modifier.width(10.dp))
                    Text(
                        text = if (isPttActive) "AAN HET SPREKEN… (LOSLATEN = MUTE)" else "HOUD INGEDRUKT OM TE PRATEN",
                        color = if (isPttActive) Color.White else TextPrimary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 13.sp
                    )
                }
            }
        }
    }
}
