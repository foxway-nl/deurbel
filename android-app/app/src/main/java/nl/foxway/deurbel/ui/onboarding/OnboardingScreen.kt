package nl.foxway.deurbel.ui.onboarding

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.DiscoveredDoorbell
import nl.foxway.deurbel.data.models.PairRequest
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.MdnsDiscovery
import androidx.compose.foundation.BorderStroke
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import org.json.JSONObject
import nl.foxway.deurbel.ui.theme.*

/**
 * 1-Click LAN Discovery, Pairing & Samsung One UI Permission Checklist.
 *
 * Implements SPEC §3.2, §4.1 & §4.4.
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
    var showQrInputDialog by remember { mutableStateOf(false) }

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

            // Naam invoerveld voor gezinslid (optioneel)
            OutlinedTextField(
                value = userName,
                onValueChange = { userName = it },
                label = { Text("Jouw naam (optioneel)") },
                placeholder = { Text("Optioneel (automatisch uniek indien leeg)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = EmeraldPrimary,
                    unfocusedBorderColor = DividerDark,
                    focusedTextColor = TextPrimary,
                    unfocusedTextColor = TextPrimary,
                    focusedLabelColor = EmeraldPrimary,
                    unfocusedLabelColor = TextSecondary,
                    cursorColor = EmeraldPrimary
                )
            )

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

            // Connect Button
            Button(
                onClick = {
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
                            } catch (e: Exception) {
                                null
                            }

                            // 2. Als LAN niet bereikbaar is, val automatisch terug op WAN
                            if (resp == null || !resp.isSuccessful) {
                                try {
                                    val wanApi = ApiClientFactory.createApi(targetWan)
                                    resp = wanApi.pair(request)
                                } catch (e: Exception) {
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
                enabled = !isPairing,
                colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(52.dp)
            ) {
                if (isPairing) {
                    CircularProgressIndicator(color = BackgroundDark, modifier = Modifier.size(24.dp))
                } else {
                    Text(
                        text = if (userName.isBlank()) "1-Klik Koppelen (Als gast)" else "1-Klik Koppelen als $userName",
                        color = BackgroundDark,
                        fontWeight = FontWeight.Bold,
                        fontSize = 16.sp
                    )
                }
            }

            OutlinedButton(
                onClick = { showQrInputDialog = true },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(48.dp),
                shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = CyanAccent),
                border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.5f))
            ) {
                Icon(imageVector = Icons.Default.QrCodeScanner, contentDescription = null, tint = CyanAccent)
                Spacer(modifier = Modifier.width(8.dp))
                Text("📷 Direct koppelen via QR-code / Code", fontWeight = FontWeight.SemiBold)
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
                        icon = Icons.Default.Fullscreen,
                        title = "Volledig Scherm op Vergrendelscherm",
                        subtitle = "Toont beloproep direct als je scherm uit staat",
                        onClick = {
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                                val intent = Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT).apply {
                                    data = Uri.parse("package:${context.packageName}")
                                }
                                context.startActivity(intent)
                            }
                        }
                    )

                    HorizontalDivider(color = DividerDark)

                    // 2. Battery optimization exemption
                    PermissionItem(
                        icon = Icons.Default.BatteryAlert,
                        title = "Batterijoptimalisatie Uitschakelen",
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

    if (showQrInputDialog) {
        QrCodeInputDialog(
            context = context,
            deviceName = deviceName,
            onDismiss = { showQrInputDialog = false },
            onPairSuccess = {
                onPairingSuccess()
            }
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

@Composable
private fun QrCodeInputDialog(
    context: android.content.Context,
    deviceName: String,
    onDismiss: () -> Unit,
    onPairSuccess: () -> Unit
) {
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    var codeText by remember { mutableStateOf("") }
    var isProcessing by remember { mutableStateOf(false) }
    var errorMsg by remember { mutableStateOf<String?>(null) }

    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .clip(RoundedCornerShape(20.dp)),
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
                    Text("Koppelcode / QR Invoeren", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, color = TextPrimary)
                    IconButton(onClick = onDismiss) {
                        Icon(imageVector = Icons.Default.Close, contentDescription = "Sluiten", tint = TextSecondary)
                    }
                }
                Text("Plak hier de koppelcode of JSON van de QR-code die door een ander gezinslid is gegenereerd:", style = MaterialTheme.typography.bodySmall, color = TextSecondary)

                OutlinedTextField(
                    value = codeText,
                    onValueChange = { codeText = it },
                    placeholder = { Text("Plak koppelcode of JSON hier...") },
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

                errorMsg?.let {
                    Text(text = it, color = CoralDecline, style = MaterialTheme.typography.bodySmall)
                }

                Button(
                    onClick = {
                        coroutineScope.launch {
                            isProcessing = true
                            errorMsg = null
                            try {
                                val trimmed = codeText.trim()
                                var targetLan = "http://192.168.2.1:8090"
                                var targetWan = "https://deurbel.foxway.nl"
                                if (trimmed.startsWith("{")) {
                                    val obj = JSONObject(trimmed)
                                    if (obj.has("lan_url")) targetLan = obj.getString("lan_url")
                                    if (obj.has("wan_url")) targetWan = obj.getString("wan_url")
                                }
                                val fcmToken = prefs.getFcmToken()
                                val androidId = Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID) ?: ""
                                val request = PairRequest(
                                    userName = "",
                                    deviceName = deviceName.trim(),
                                    fcmToken = fcmToken,
                                    deviceId = androidId,
                                    androidVersion = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
                                    connectionType = "QR"
                                )
                                var resp = try {
                                    val lanApi = ApiClientFactory.createApi(targetLan)
                                    lanApi.pair(request)
                                } catch (_: Exception) { null }
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
                                    onDismiss()
                                    onPairSuccess()
                                } else {
                                    errorMsg = "Koppelen via code mislukt (${resp?.code() ?: "Geen verbinding"})"
                                }
                            } catch (e: Exception) {
                                errorMsg = "Ongeldige code: ${e.localizedMessage}"
                            } finally {
                                isProcessing = false
                            }
                        }
                    },
                    enabled = !isProcessing && codeText.isNotBlank(),
                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(48.dp)
                ) {
                    if (isProcessing) {
                        CircularProgressIndicator(color = BackgroundDark, modifier = Modifier.size(20.dp))
                    } else {
                        Text("Koppelen met Code", color = BackgroundDark, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}
