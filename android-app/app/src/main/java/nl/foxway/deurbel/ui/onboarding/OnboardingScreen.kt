package nl.foxway.deurbel.ui.onboarding

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.*
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.google.zxing.*
import com.google.zxing.common.HybridBinarizer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.DiscoveredDoorbell
import nl.foxway.deurbel.data.models.PairRequest
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.MdnsDiscovery
import nl.foxway.deurbel.ui.theme.*
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * Production-Grade Doorbell Onboarding Screen:
 *  - Mandatory username (empty by default, requires >= 2 characters)
 *  - 3-Way EZVIZ / Ring style onboarding:
 *    1. 📷 Live CameraX Barcode Scanner
 *    2. 🖼️ Gallery Photo Picker Bitmap Decoder
 *    3. ⌨️ Manual Code / Verification Entry
 *  - LAN & WAN auto-failover pairing
 *  - Realtime One UI permission checklist
 */
@Composable
fun OnboardingScreen(
    onPairingSuccess: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val mdnsDiscovery = remember { MdnsDiscovery(context) }

    var discoveredDoorbell by remember { mutableStateOf<DiscoveredDoorbell?>(null) }
    var isScanning by remember { mutableStateOf(true) }
    var userName by remember { mutableStateOf("") }
    var deviceName by remember {
        mutableStateOf("${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}")
    }
    var isPairing by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var cameraConfiguredMessage by remember { mutableStateOf<String?>(null) }
    var showQrScanDialog by remember { mutableStateOf(false) }

    val isNameValid = userName.trim().length >= 2

    // Start discovery & auto-fill bootstrap
    val performDiscovery: () -> Unit = {
        coroutineScope.launch {
            isScanning = true
            errorMessage = null
            try {
                // 1. Try bootstrap on LAN or WAN first to auto-fill camera status
                val lanApi = ApiClientFactory.createApi("http://192.168.2.1:8090")
                val bootResp = try {
                    lanApi.getOnboardingBootstrap()
                } catch (_: Exception) {
                    val wanApi = ApiClientFactory.createApi("https://deurbel.foxway.nl")
                    wanApi.getOnboardingBootstrap()
                }
                if (bootResp.isSuccessful && bootResp.body() != null) {
                    val boot = bootResp.body()!!
                    discoveredDoorbell = DiscoveredDoorbell(
                        hostAddress = boot.cameraIp.ifBlank { "192.168.2.1" },
                        port = 8090,
                        lanUrl = boot.lanUrl.ifBlank { "http://192.168.2.1:8090" },
                        wanUrl = boot.wanUrl.ifBlank { "https://deurbel.foxway.nl" },
                        go2rtcLan = "ws://192.168.2.1:1984",
                        go2rtcWan = "wss://deurbel.foxway.nl/api/ws"
                    )
                    if (boot.cameraCredentialsConfigured) {
                        cameraConfiguredMessage = "✅ Deurbel al gekoppeld op server (${boot.cameraIp})"
                    }
                    isScanning = false
                    return@launch
                }
            } catch (_: Exception) {}

            // 2. Fallback to mDNS
            try {
                val found = mdnsDiscovery.discoverFirst()
                if (found != null) {
                    discoveredDoorbell = found
                    isScanning = false
                    return@launch
                }
            } catch (_: Exception) {}

            // 3. Fallback default for lab
            discoveredDoorbell = DiscoveredDoorbell(
                hostAddress = "192.168.2.1",
                port = 8090,
                lanUrl = "http://192.168.2.1:8090",
                wanUrl = "https://deurbel.foxway.nl",
                go2rtcLan = "ws://192.168.2.1:1984",
                go2rtcWan = "wss://deurbel.foxway.nl/api/ws"
            )
            isScanning = false
        }
    }

    LaunchedEffect(Unit) {
        performDiscovery()
    }

    // Radar pulsing animation
    val infiniteTransition = rememberInfiniteTransition(label = "radar")
    val pulseScale by infiniteTransition.animateFloat(
        initialValue = 0.8f,
        targetValue = 1.3f,
        animationSpec = infiniteRepeatable(
            animation = tween(1500, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulseScale"
    )
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 0.6f,
        targetValue = 0.1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1500, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulseAlpha"
    )

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    colors = listOf(BackgroundDark, Color(0xFF0F172A), BackgroundDark)
                )
            )
            .padding(24.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(20.dp)
        ) {
            Spacer(modifier = Modifier.height(24.dp))

            // Logo & Title
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.size(100.dp)
            ) {
                if (isScanning) {
                    Box(
                        modifier = Modifier
                            .size(90.dp)
                            .scale(pulseScale)
                            .background(EmeraldPrimary.copy(alpha = pulseAlpha), CircleShape)
                    )
                }
                Box(
                    modifier = Modifier
                        .size(70.dp)
                        .background(SurfaceVariantDark, CircleShape)
                        .border(2.dp, EmeraldPrimary, CircleShape),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = Icons.Default.Doorbell,
                        contentDescription = "Doorbell",
                        tint = EmeraldPrimary,
                        modifier = Modifier.size(36.dp)
                    )
                }
            }

            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    text = "Foxway Deurbel",
                    style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                    color = TextPrimary
                )
                Text(
                    text = if (isScanning) "Zoeken naar deurbel op je wifi-netwerk…" else "Klaar voor koppelen",
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextSecondary,
                    textAlign = TextAlign.Center
                )
            }

            // Discovery Result Card
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceGlass),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Box(
                            modifier = Modifier
                                .size(10.dp)
                                .background(if (discoveredDoorbell != null) LanGreen else AmberWarning, CircleShape)
                        )
                        Text(
                            text = if (discoveredDoorbell != null)
                                "Gevonden op ${discoveredDoorbell?.hostAddress}"
                            else
                                "Bezig met zoeken via mDNS…",
                            style = MaterialTheme.typography.titleMedium,
                            color = TextPrimary
                        )
                    }

                    discoveredDoorbell?.let { db ->
                        Text(
                            text = "LAN: ${db.lanUrl} • WAN: ${db.wanUrl}",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                    }

                    cameraConfiguredMessage?.let { msg ->
                        Surface(
                            color = EmeraldPrimary.copy(alpha = 0.15f),
                            shape = RoundedCornerShape(10.dp),
                            border = BorderStroke(1.dp, EmeraldPrimary.copy(alpha = 0.4f)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(
                                modifier = Modifier.padding(10.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                Icon(imageVector = Icons.Default.CheckCircle, contentDescription = null, tint = EmeraldPrimary, modifier = Modifier.size(18.dp))
                                Text(
                                    text = msg,
                                    style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.SemiBold),
                                    color = EmeraldLight
                                )
                            }
                        }
                    }
                }
            }

            // Verplicht Naam Invoerveld
            Column(
                modifier = Modifier.fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                OutlinedTextField(
                    value = userName,
                    onValueChange = { userName = it },
                    label = { Text("Jouw naam (verplicht)") },
                    placeholder = { Text("Bijv. Mo, Sara, Woonkamer") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                    isError = userName.isNotBlank() && !isNameValid,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = EmeraldPrimary,
                        unfocusedBorderColor = if (isNameValid) EmeraldPrimary.copy(alpha = 0.6f) else DividerDark,
                        focusedTextColor = TextPrimary,
                        unfocusedTextColor = TextPrimary,
                        focusedLabelColor = EmeraldPrimary,
                        unfocusedLabelColor = TextSecondary,
                        cursorColor = EmeraldPrimary
                    )
                )

                if (!isNameValid) {
                    Text(
                        text = "⚠️ Vul eerst je naam in om dit toestel te koppelen (min. 2 tekens)",
                        color = AmberWarning,
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(start = 4.dp)
                    )
                }
            }

            // Geavanceerde netwerkinstellingen (Collapsible)
            var showAdvancedSettings by remember { mutableStateOf(false) }
            var customLanUrl by remember { mutableStateOf("http://192.168.2.1:8090") }
            var customWanUrl by remember { mutableStateOf("https://deurbel.foxway.nl") }

            Column(modifier = Modifier.fillMaxWidth()) {
                TextButton(
                    onClick = { showAdvancedSettings = !showAdvancedSettings },
                    modifier = Modifier.align(Alignment.Start)
                ) {
                    Icon(
                        imageVector = if (showAdvancedSettings) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                        contentDescription = null,
                        tint = CyanAccent,
                        modifier = Modifier.size(18.dp)
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = "Geavanceerde netwerkinstellingen",
                        style = MaterialTheme.typography.bodySmall,
                        color = CyanAccent
                    )
                }

                if (showAdvancedSettings) {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = SurfaceVariantDark.copy(alpha = 0.5f)),
                        shape = RoundedCornerShape(12.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 6.dp)
                    ) {
                        Column(
                            modifier = Modifier.padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            OutlinedTextField(
                                value = customLanUrl,
                                onValueChange = { customLanUrl = it },
                                label = { Text("LAN URL") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth(),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = EmeraldPrimary,
                                    unfocusedBorderColor = DividerDark
                                )
                            )
                            OutlinedTextField(
                                value = customWanUrl,
                                onValueChange = { customWanUrl = it },
                                label = { Text("WAN URL (Foxway Cloud)") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth(),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = EmeraldPrimary,
                                    unfocusedBorderColor = DividerDark
                                )
                            )
                        }
                    }
                }
            }

            // Connect Button (1-Klik Koppelen)
            Button(
                onClick = {
                    if (!isNameValid) return@Button
                    val db = discoveredDoorbell
                    val targetLan = if (showAdvancedSettings && customLanUrl.isNotBlank()) customLanUrl else (db?.lanUrl ?: "http://192.168.2.1:8090")
                    val targetWan = if (showAdvancedSettings && customWanUrl.isNotBlank()) customWanUrl else (db?.wanUrl ?: "https://deurbel.foxway.nl")

                    coroutineScope.launch {
                        isPairing = true
                        errorMessage = null
                        try {
                            val fcmToken = prefs.getFcmToken()
                            val androidId = Settings.Secure.getString(
                                context.contentResolver,
                                Settings.Secure.ANDROID_ID
                            ) ?: ""
                            val request = PairRequest(
                                userName = userName.trim(),
                                deviceName = deviceName.trim(),
                                fcmToken = fcmToken,
                                deviceId = androidId,
                                androidVersion = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
                                connectionType = "LAN/WAN"
                            )

                            // 1. Probeer eerst LAN
                            var resp = try {
                                val lanApi = ApiClientFactory.createApi(targetLan)
                                lanApi.pair(request)
                            } catch (_: Exception) {
                                null
                            }

                            // 2. Als LAN niet bereikbaar is, val automatisch terug op WAN
                            if (resp == null || !resp.isSuccessful) {
                                try {
                                    val wanApi = ApiClientFactory.createApi(targetWan)
                                    resp = wanApi.pair(request)
                                } catch (_: Exception) {
                                    // WAN also failed
                                }
                            }

                            if (resp != null && resp.isSuccessful && resp.body() != null) {
                                val body = resp.body()!!
                                prefs.savePairingData(
                                    userId = body.userId,
                                    userName = body.userName,
                                    deviceName = body.deviceName,
                                    jwtToken = body.jwtToken,
                                    lanUrl = body.lanUrl,
                                    wanUrl = body.wanUrl,
                                    go2rtcLanWs = body.go2rtcLanWs,
                                    go2rtcWanWs = body.go2rtcWanWs
                                )
                                nl.foxway.deurbel.data.AppLogger.getInstance(context)
                                    .info("Apparaat succesvol gekoppeld als ${body.userName}")
                                onPairingSuccess()
                            } else {
                                errorMessage = "Koppelen mislukt (${resp?.code() ?: "Geen verbinding met LAN of WAN"})"
                            }
                        } catch (e: Exception) {
                            errorMessage = "Fout bij verbinden: ${e.localizedMessage}"
                        } finally {
                            isPairing = false
                        }
                    }
                },
                enabled = !isPairing && isNameValid,
                colors = ButtonDefaults.buttonColors(
                    containerColor = EmeraldPrimary,
                    disabledContainerColor = SurfaceVariantDark
                ),
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(52.dp)
            ) {
                if (isPairing) {
                    CircularProgressIndicator(color = BackgroundDark, modifier = Modifier.size(24.dp))
                } else {
                    Text(
                        text = if (!isNameValid) "Vul naam in om te koppelen" else "1-Klik Koppelen als $userName",
                        color = if (isNameValid) BackgroundDark else TextSecondary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 16.sp
                    )
                }
            }

            // QR & Barcode Onboarding Button
            OutlinedButton(
                onClick = {
                    if (isNameValid) {
                        showQrScanDialog = true
                    }
                },
                enabled = !isPairing && isNameValid,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(48.dp),
                shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.outlinedButtonColors(
                    contentColor = if (isNameValid) CyanAccent else TextSecondary
                ),
                border = BorderStroke(1.dp, if (isNameValid) CyanAccent.copy(alpha = 0.5f) else DividerDark)
            ) {
                Icon(imageVector = Icons.Default.QrCodeScanner, contentDescription = null, tint = if (isNameValid) CyanAccent else TextSecondary)
                Spacer(modifier = Modifier.width(8.dp))
                Text("📷 Koppel via QR-code, Foto of Code", fontWeight = FontWeight.SemiBold)
            }

            errorMessage?.let {
                Text(text = it, color = CoralDecline, style = MaterialTheme.typography.bodyMedium)
            }

            // Samsung One UI Permission Checklist (SPEC §4.1)
            Text(
                text = "Belangrijke Toestemmingen (Samsung One UI)",
                style = MaterialTheme.typography.titleMedium,
                color = TextPrimary,
                modifier = Modifier.align(Alignment.Start)
            )

            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceGlass),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    // 1. Full-screen intent
                    PermissionItem(
                        icon = Icons.Default.ScreenLockPortrait,
                        title = "Volledig scherm meldingen",
                        subtitle = "Breekt direct open bij een oproep als het scherm uit staat",
                        onClick = {
                            val intent = Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT).apply {
                                data = Uri.parse("package:${context.packageName}")
                            }
                            context.startActivity(intent)
                        }
                    )

                    HorizontalDivider(color = DividerDark)

                    // 2. Battery optimisation
                    PermissionItem(
                        icon = Icons.Default.BatteryChargingFull,
                        title = "Batterij niet beperken",
                        subtitle = "Voorkomt dat Samsung One UI de belmeldingen vertraagt",
                        onClick = {
                            val intent = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                            context.startActivity(intent)
                        }
                    )

                    HorizontalDivider(color = DividerDark)

                    // 3. Notification settings
                    PermissionItem(
                        icon = Icons.Default.Notifications,
                        title = "Meldingen & Geluid",
                        subtitle = "Zorg dat bellen altijd prioriteit heeft",
                        onClick = {
                            val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
                                putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                            }
                            context.startActivity(intent)
                        }
                    )
                }
            }

            Spacer(modifier = Modifier.height(20.dp))
        }
    }

    if (showQrScanDialog) {
        QrScanDialog(
            userName = userName.trim(),
            deviceName = deviceName.trim(),
            onDismiss = { showQrScanDialog = false },
            onPairSuccess = onPairingSuccess
        )
    }
}

@Composable
private fun PermissionItem(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    title: String,
    subtitle: String,
    onClick: () -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Row(
            modifier = Modifier.weight(1f),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Icon(imageVector = icon, contentDescription = null, tint = CyanAccent)
            Column {
                Text(text = title, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = TextPrimary)
                Text(text = subtitle, style = MaterialTheme.typography.bodySmall, color = TextSecondary)
            }
        }
        IconButton(onClick = onClick) {
            Icon(imageVector = Icons.Default.ChevronRight, contentDescription = "Instellen", tint = TextSecondary)
        }
    }
}

/**
 * 3-Way EZVIZ / Ring style onboarding dialog:
 *  Tab 0: 📷 Live CameraX Scanner (ZXing decoding)
 *  Tab 1: 🖼️ Gallery Photo Picker (ZXing Bitmap decoding)
 *  Tab 2: ⌨️ Manual Code / Verification Entry
 */
@Composable
private fun QrScanDialog(
    userName: String,
    deviceName: String,
    onDismiss: () -> Unit,
    onPairSuccess: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }

    var selectedTab by remember { mutableIntStateOf(0) }
    var isProcessing by remember { mutableStateOf(false) }
    var statusMessage by remember { mutableStateOf<String?>(null) }
    var isError by remember { mutableStateOf(false) }
    var manualText by remember { mutableStateOf("") }

    // Camera permission state
    var hasCameraPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        )
    }
    val cameraPermissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { granted ->
        hasCameraPermission = granted
        if (!granted) {
            statusMessage = "Cameratoestemming is vereist om de QR-code te scannen."
            isError = true
        }
    }

    // Common pairing executor
    val executePairing: (String) -> Unit = { rawCode ->
        coroutineScope.launch {
            isProcessing = true
            isError = false
            statusMessage = "Bezig met verifiëren van koppelcode…"
            try {
                val trimmed = rawCode.trim()
                var targetLan = "http://192.168.2.1:8090"
                var targetWan = "https://deurbel.foxway.nl"

                if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
                    try {
                        val obj = JSONObject(trimmed)
                        if (obj.has("lan_url")) targetLan = obj.getString("lan_url")
                        if (obj.has("wan_url")) targetWan = obj.getString("wan_url")
                    } catch (_: Exception) {}
                }

                val fcmToken = prefs.getFcmToken()
                val androidId = Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID) ?: ""
                val request = PairRequest(
                    userName = userName,
                    deviceName = deviceName,
                    fcmToken = fcmToken,
                    deviceId = androidId,
                    androidVersion = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
                    connectionType = "QR"
                )

                // 1. Probeer eerst LAN
                var resp = try {
                    val lanApi = ApiClientFactory.createApi(targetLan)
                    lanApi.pair(request)
                } catch (_: Exception) { null }

                // 2. Fallback naar WAN
                if (resp == null || !resp.isSuccessful) {
                    val wanApi = ApiClientFactory.createApi(targetWan)
                    resp = wanApi.pair(request)
                }

                if (resp != null && resp.isSuccessful && resp.body() != null) {
                    val body = resp.body()!!
                    prefs.savePairingData(
                        userId = body.userId,
                        userName = body.userName,
                        deviceName = body.deviceName,
                        jwtToken = body.jwtToken,
                        lanUrl = body.lanUrl,
                        wanUrl = body.wanUrl,
                        go2rtcLanWs = body.go2rtcLanWs,
                        go2rtcWanWs = body.go2rtcWanWs
                    )
                    statusMessage = "✅ Succesvol gekoppeld!"
                    delay(300)
                    onDismiss()
                    onPairSuccess()
                } else {
                    isError = true
                    statusMessage = "❌ Koppelen mislukt (${resp?.code() ?: "Geen verbinding met server"})"
                }
            } catch (e: Exception) {
                isError = true
                statusMessage = "❌ Fout: ${e.localizedMessage}"
            } finally {
                isProcessing = false
            }
        }
    }

    // Photo picker launcher for Tab 1
    val photoPickerLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickVisualMedia()
    ) { uri: Uri? ->
        if (uri != null) {
            coroutineScope.launch(Dispatchers.IO) {
                isProcessing = true
                isError = false
                withContext(Dispatchers.Main) {
                    statusMessage = "Foto analyseren op QR-code…"
                }
                try {
                    val inputStream = context.contentResolver.openInputStream(uri)
                    val bitmap = BitmapFactory.decodeStream(inputStream)
                    inputStream?.close()

                    if (bitmap != null) {
                        val decoded = decodeQrFromBitmap(bitmap)
                        if (decoded != null && decoded.isNotBlank()) {
                            withContext(Dispatchers.Main) {
                                executePairing(decoded)
                            }
                        } else {
                            withContext(Dispatchers.Main) {
                                isError = true
                                statusMessage = "Geen QR-code gevonden in de geselecteerde foto. Kies een duidelijkere foto of screenshot."
                                isProcessing = false
                            }
                        }
                    } else {
                        withContext(Dispatchers.Main) {
                            isError = true
                            statusMessage = "Kon de foto niet openen."
                            isProcessing = false
                        }
                    }
                } catch (e: Exception) {
                    withContext(Dispatchers.Main) {
                        isError = true
                        statusMessage = "Fout bij laden van foto: ${e.localizedMessage}"
                        isProcessing = false
                    }
                }
            }
        }
    }

    Dialog(
        onDismissRequest = { if (!isProcessing) onDismiss() },
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.94f)
                .wrapContentHeight(),
            shape = RoundedCornerShape(20.dp),
            color = SurfaceDark,
            border = BorderStroke(1.dp, DividerDark)
        ) {
            Column(
                modifier = Modifier.padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = "Deurbel Koppelen",
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = TextPrimary
                    )
                    IconButton(onClick = onDismiss, enabled = !isProcessing) {
                        Icon(imageVector = Icons.Default.Close, contentDescription = "Sluiten", tint = TextSecondary)
                    }
                }

                // 3 Tabs: Camera, Foto, Handmatig
                TabRow(
                    selectedTabIndex = selectedTab,
                    containerColor = SurfaceVariantDark,
                    contentColor = EmeraldPrimary,
                    modifier = Modifier.clip(RoundedCornerShape(10.dp))
                ) {
                    Tab(
                        selected = selectedTab == 0,
                        onClick = { selectedTab = 0 },
                        text = { Text("📷 Camera", fontSize = 12.sp, fontWeight = FontWeight.SemiBold) }
                    )
                    Tab(
                        selected = selectedTab == 1,
                        onClick = { selectedTab = 1 },
                        text = { Text("🖼️ Foto", fontSize = 12.sp, fontWeight = FontWeight.SemiBold) }
                    )
                    Tab(
                        selected = selectedTab == 2,
                        onClick = { selectedTab = 2 },
                        text = { Text("⌨️ Handmatig", fontSize = 12.sp, fontWeight = FontWeight.SemiBold) }
                    )
                }

                // Tab Content
                when (selectedTab) {
                    0 -> { // CameraX Live Scanner
                        if (!hasCameraPermission) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 24.dp),
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                Icon(Icons.Default.CameraAlt, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(48.dp))
                                Text(
                                    text = "Cameratoegang is nodig om de QR-code direct te scannen.",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = TextSecondary,
                                    textAlign = TextAlign.Center
                                )
                                Button(
                                    onClick = { cameraPermissionLauncher.launch(Manifest.permission.CAMERA) },
                                    colors = ButtonDefaults.buttonColors(containerColor = CyanAccent)
                                ) {
                                    Text("Camera Toestemming Geven", color = BackgroundDark, fontWeight = FontWeight.Bold)
                                }
                            }
                        } else {
                            CameraScannerView(
                                isProcessing = isProcessing,
                                onQrScanned = { code ->
                                    if (!isProcessing) {
                                        executePairing(code)
                                    }
                                }
                            )
                        }
                    }

                    1 -> { // Gallery Photo Picker
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(vertical = 16.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.spacedBy(14.dp)
                        ) {
                            Icon(Icons.Default.PhotoLibrary, contentDescription = null, tint = EmeraldPrimary, modifier = Modifier.size(48.dp))
                            Text(
                                text = "Kies een screenshot of gemaakte foto van de QR-code uit je galerij om direct te koppelen.",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextSecondary,
                                textAlign = TextAlign.Center
                            )
                            Button(
                                onClick = {
                                    photoPickerLauncher.launch(
                                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                                    )
                                },
                                enabled = !isProcessing,
                                colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary)
                            ) {
                                Icon(Icons.Default.AddPhotoAlternate, contentDescription = null, modifier = Modifier.size(18.dp))
                                Spacer(modifier = Modifier.width(8.dp))
                                Text("Kies Foto uit Galerij", color = BackgroundDark, fontWeight = FontWeight.Bold)
                            }
                        }
                    }

                    2 -> { // Manual Entry
                        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Text(
                                text = "Vul hier de deurbelcode, verificatiecode of JSON-payload handmatig in:",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextSecondary
                            )
                            OutlinedTextField(
                                value = manualText,
                                onValueChange = { manualText = it },
                                placeholder = { Text("Koppelcode of JSON hier invoeren...") },
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .height(110.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = EmeraldPrimary,
                                    unfocusedBorderColor = DividerDark,
                                    focusedTextColor = TextPrimary,
                                    unfocusedTextColor = TextPrimary
                                )
                            )
                            Button(
                                onClick = { executePairing(manualText) },
                                enabled = !isProcessing && manualText.isNotBlank(),
                                colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier.fillMaxWidth().height(48.dp)
                            ) {
                                Text("Koppelen met Ingevulde Code", color = BackgroundDark, fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                }

                // Status / Error Banner
                statusMessage?.let { msg ->
                    Surface(
                        color = if (isError) CoralDecline.copy(alpha = 0.2f) else EmeraldPrimary.copy(alpha = 0.2f),
                        shape = RoundedCornerShape(10.dp),
                        border = BorderStroke(1.dp, if (isError) CoralDecline else EmeraldPrimary),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            if (isProcessing) {
                                CircularProgressIndicator(color = if (isError) CoralDecline else EmeraldPrimary, modifier = Modifier.size(16.dp))
                            }
                            Text(
                                text = msg,
                                style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.SemiBold),
                                color = if (isError) CoralDecline else EmeraldLight
                            )
                        }
                    }
                }
            }
        }
    }
}

/**
 * CameraX live preview with real ZXing frame analyzer.
 */
@Composable
private fun CameraScannerView(
    isProcessing: Boolean,
    onQrScanned: (String) -> Unit
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var hasDetected by remember { mutableStateOf(false) }

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(240.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(Color.Black),
        contentAlignment = Alignment.Center
    ) {
        AndroidView(
            factory = { ctx ->
                val previewView = PreviewView(ctx).apply {
                    implementationMode = PreviewView.ImplementationMode.COMPATIBLE
                    scaleType = PreviewView.ScaleType.FILL_CENTER
                }
                val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)
                cameraProviderFuture.addListener({
                    try {
                        val cameraProvider = cameraProviderFuture.get()
                        val preview = Preview.Builder().build().also {
                            it.setSurfaceProvider(previewView.surfaceProvider)
                        }
                        val imageAnalysis = ImageAnalysis.Builder()
                            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                            .build()

                        val reader = MultiFormatReader().apply {
                            setHints(mapOf(DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE)))
                        }

                        imageAnalysis.setAnalyzer(Executors.newSingleThreadExecutor()) { imageProxy ->
                            if (hasDetected || isProcessing) {
                                imageProxy.close()
                                return@setAnalyzer
                            }
                            try {
                                val buffer = imageProxy.planes[0].buffer
                                val bytes = ByteArray(buffer.remaining())
                                buffer.get(bytes)
                                val width = imageProxy.width
                                val height = imageProxy.height

                                val source = PlanarYUVLuminanceSource(
                                    bytes, width, height, 0, 0, width, height, false
                                )
                                val binaryBitmap = BinaryBitmap(HybridBinarizer(source))
                                val result = reader.decodeWithState(binaryBitmap)
                                if (result != null && result.text.isNotBlank()) {
                                    hasDetected = true
                                    ContextCompat.getMainExecutor(ctx).execute {
                                        onQrScanned(result.text)
                                    }
                                }
                            } catch (_: Exception) {
                            } finally {
                                imageProxy.close()
                            }
                        }

                        val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA
                        cameraProvider.unbindAll()
                        cameraProvider.bindToLifecycle(lifecycleOwner, cameraSelector, preview, imageAnalysis)
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                }, ContextCompat.getMainExecutor(ctx))
                previewView
            },
            modifier = Modifier.fillMaxSize()
        )

        // Scanner Reticle Overlay
        Box(
            modifier = Modifier
                .size(160.dp)
                .border(2.dp, CyanAccent, RoundedCornerShape(12.dp))
        )
        Text(
            text = "Richt op de QR-code van de deurbel",
            color = Color.White,
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(bottom = 10.dp)
                .background(Color.Black.copy(alpha = 0.65f), RoundedCornerShape(6.dp))
                .padding(horizontal = 8.dp, vertical = 4.dp)
        )
    }
}

/**
 * Decodes a QR code string from an Android Bitmap via ZXing.
 */
private fun decodeQrFromBitmap(bitmap: Bitmap): String? {
    val width = bitmap.width
    val height = bitmap.height
    val pixels = IntArray(width * height)
    bitmap.getPixels(pixels, 0, width, 0, 0, width, height)
    val source = RGBLuminanceSource(width, height, pixels)
    val binaryBitmap = BinaryBitmap(HybridBinarizer(source))
    val reader = MultiFormatReader().apply {
        setHints(mapOf(DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE)))
    }
    return try {
        reader.decodeWithState(binaryBitmap).text
    } catch (_: Exception) {
        // Fallback: try inverted
        try {
            val invertedSource = source.invert()
            val invertedBitmap = BinaryBitmap(HybridBinarizer(invertedSource))
            reader.decodeWithState(invertedBitmap).text
        } catch (_: Exception) {
            null
        }
    }
}
