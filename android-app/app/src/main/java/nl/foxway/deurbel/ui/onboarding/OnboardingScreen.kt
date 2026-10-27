package nl.foxway.deurbel.ui.onboarding

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.AppLogger
import nl.foxway.deurbel.data.models.CameraSettingsUpdate
import nl.foxway.deurbel.data.models.PairRequest
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.ConnectionManager
import nl.foxway.deurbel.network.NetworkDiscovery
import nl.foxway.deurbel.ui.theme.*
import org.json.JSONObject

/**
 * 100% Dynamic Onboarding & Pairing Screen for Foxway Doorbell.
 *
 * Implements Task 1 & 2:
 * - Parallel LAN subnet scanning + WAN cloud bootstrap (deurbel.foxway.nl)
 * - 5G Cloud connection detection & instant spinner stop
 * - Live Status Card (5G Cloud vs Local LAN auto-discovery)
 * - Veld 1: Jouw naam (verplicht, standaard "")
 * - Veld 2: Deurbel Gebruikersnaam (standaard "admin")
 * - Veld 3: Deurbel Wachtwoord / Verificatiecode met wachtwoord-oogje en [i] info icoon
 * - Eén enkele "Koppelen" knop die LAN en WAN opslaat in ConnectionManager
 * - Full-screen Samsung One UI permission setup
 */
@Composable
fun OnboardingScreen(
    onPairingSuccess: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val networkDiscovery = remember { NetworkDiscovery(context) }

    var isScanning by remember { mutableStateOf(true) }
    var discoveryResult by remember { mutableStateOf<NetworkDiscovery.DiscoveryResult?>(null) }
    var scanFailed by remember { mutableStateOf(false) }

    // Form fields
    var userName by remember { mutableStateOf("") }
    var doorbellUser by remember { mutableStateOf("admin") }
    var doorbellPassword by remember { mutableStateOf("") }
    var passwordVisible by remember { mutableStateOf(false) }
    var showPasswordInfoDialog by remember { mutableStateOf(false) }
    var showManualCredentials by remember { mutableStateOf(false) }

    // Manual network fallback expander
    var showManualServerConfig by remember { mutableStateOf(false) }
    var manualLanUrl by remember { mutableStateOf("") }
    var manualWanUrl by remember { mutableStateOf("") }

    val deviceName = remember {
        "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}"
    }

    var isPairing by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }

    val isNameValid = userName.trim().length >= 2

    // Perform parallel discovery: Wi-Fi subnet scan + WAN cloud bootstrap
    val performDiscovery: () -> Unit = {
        coroutineScope.launch {
            isScanning = true
            scanFailed = false
            errorMessage = null

            val isWifi = networkDiscovery.isWifiConnected()

            // 1. Probe WAN bootstrap always (timeout 3000ms)
            val wanDeferred = async(Dispatchers.IO) {
                networkDiscovery.probeWanBootstrap("https://deurbel.foxway.nl")
            }

            if (!isWifi) {
                // Phone is on 5G / Cellular: directly await WAN bootstrap!
                val wanResult = wanDeferred.await()
                isScanning = false
                if (wanResult != null) {
                    discoveryResult = wanResult
                    scanFailed = false
                    manualLanUrl = wanResult.serverLanUrl.ifBlank { "http://192.168.2.1:8090" }
                    manualWanUrl = wanResult.serverWanUrl.ifBlank { "https://deurbel.foxway.nl" }
                } else {
                    scanFailed = true
                }
            } else {
                // Phone is on Wi-Fi: scan local /24 subnet on port 8090 in parallel
                val lanDeferred = async(Dispatchers.IO) {
                    networkDiscovery.discoverServer(timeoutMs = 2500)
                }

                val lanResult = lanDeferred.await()
                if (lanResult != null) {
                    wanDeferred.cancel()
                    isScanning = false
                    discoveryResult = lanResult
                    scanFailed = false
                    if (lanResult.serverLanUrl.isNotBlank()) manualLanUrl = lanResult.serverLanUrl
                    if (lanResult.serverWanUrl.isNotBlank()) manualWanUrl = lanResult.serverWanUrl
                } else {
                    // LAN timed out, fallback to WAN cloud probe
                    val wanResult = wanDeferred.await()
                    isScanning = false
                    if (wanResult != null) {
                        discoveryResult = wanResult
                        scanFailed = false
                        if (wanResult.serverLanUrl.isNotBlank()) manualLanUrl = wanResult.serverLanUrl
                        if (wanResult.serverWanUrl.isNotBlank()) manualWanUrl = wanResult.serverWanUrl
                    } else {
                        scanFailed = true
                    }
                }
            }
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
            verticalArrangement = Arrangement.spacedBy(18.dp)
        ) {
            Spacer(modifier = Modifier.height(16.dp))

            // Logo & Title
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.size(90.dp)
            ) {
                if (isScanning) {
                    Box(
                        modifier = Modifier
                            .size(80.dp)
                            .scale(pulseScale)
                            .background(EmeraldPrimary.copy(alpha = pulseAlpha), CircleShape)
                    )
                }
                Box(
                    modifier = Modifier
                        .size(64.dp)
                        .background(SurfaceVariantDark, CircleShape)
                        .border(2.dp, EmeraldPrimary, CircleShape),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = Icons.Default.Doorbell,
                        contentDescription = "Doorbell",
                        tint = EmeraldPrimary,
                        modifier = Modifier.size(34.dp)
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
                    text = if (isScanning) "Zoeken naar deurbel op je lokale netwerk…" else "Klaar voor koppelen",
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextSecondary,
                    textAlign = TextAlign.Center
                )
            }

            // ══════════════════════════════════════════════════════════════
            // Bovenste Statuskaart (Live Netwerkscan)
            // ══════════════════════════════════════════════════════════════
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceGlass),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(
                        1.dp,
                        if (discoveryResult != null) LanGreen.copy(alpha = 0.5f) else DividerDark,
                        RoundedCornerShape(16.dp)
                    )
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    if (isScanning) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            CircularProgressIndicator(
                                color = EmeraldPrimary,
                                modifier = Modifier.size(18.dp),
                                strokeWidth = 2.dp
                            )
                            Text(
                                text = "Zoeken naar server & deurbel op lokaal subnet…",
                                style = MaterialTheme.typography.bodyMedium,
                                color = TextPrimary
                            )
                        }
                    } else if (discoveryResult != null) {
                        val res = discoveryResult!!
                        val isWan = res.discoveredViaWan || !networkDiscovery.isWifiConnected()

                        if (isWan) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(10.dp)
                            ) {
                                Box(
                                    modifier = Modifier
                                        .size(10.dp)
                                        .background(LanGreen, CircleShape)
                                )
                                Column {
                                    Text(
                                        text = "✅ Verbonden via 5G Cloud (deurbel.foxway.nl)",
                                        style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.SemiBold),
                                        color = LanGreen
                                    )
                                    Text(
                                        text = "Deurbel online op server",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = TextSecondary
                                    )
                                }
                            }
                        } else {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(10.dp)
                            ) {
                                Box(
                                    modifier = Modifier
                                        .size(10.dp)
                                        .background(LanGreen, CircleShape)
                                )
                                Text(
                                    text = if (res.cameraIp.isNotBlank())
                                        "✅ Server & Deurbel automatisch gevonden op netwerk (${res.cameraIp})"
                                    else
                                        "✅ Server automatisch gevonden op netwerk (${res.serverLanUrl})",
                                    style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.SemiBold),
                                    color = LanGreen
                                )
                            }
                        }

                        if (res.cameraConfigured) {
                            Surface(
                                color = EmeraldPrimary.copy(alpha = 0.12f),
                                shape = RoundedCornerShape(8.dp),
                                border = BorderStroke(1.dp, EmeraldPrimary.copy(alpha = 0.3f)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Row(
                                    modifier = Modifier.padding(8.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                                ) {
                                    Icon(
                                        imageVector = Icons.Default.CheckCircle,
                                        contentDescription = null,
                                        tint = EmeraldPrimary,
                                        modifier = Modifier.size(16.dp)
                                    )
                                    Text(
                                        text = if (isWan)
                                            "Deurbel is reeds geconfigureerd op server."
                                        else
                                            "Deurbel is reeds geconfigureerd. Je hoeft alleen je naam in te vullen!",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = EmeraldLight
                                    )
                                }
                            }
                        }
                    } else {
                        // Scan failed
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            Text(
                                text = "⚠️ Geen deurbel of server gevonden op dit netwerk",
                                style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                                color = AmberWarning
                            )
                        }

                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Button(
                                onClick = { performDiscovery() },
                                colors = ButtonDefaults.buttonColors(containerColor = SurfaceVariantDark),
                                shape = RoundedCornerShape(8.dp),
                                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
                            ) {
                                Icon(
                                    imageVector = Icons.Default.Refresh,
                                    contentDescription = null,
                                    modifier = Modifier.size(16.dp),
                                    tint = EmeraldPrimary
                                )
                                Spacer(modifier = Modifier.width(6.dp))
                                Text("🔄 Opnieuw zoeken", color = TextPrimary, fontSize = 13.sp)
                            }

                            TextButton(
                                onClick = { showManualServerConfig = !showManualServerConfig },
                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp)
                            ) {
                                Text(
                                    text = if (showManualServerConfig) "Verberg handmatig" else "Handmatig opgeven",
                                    color = CyanAccent,
                                    fontSize = 13.sp
                                )
                            }
                        }

                        if (showManualServerConfig) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(top = 8.dp),
                                    verticalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                OutlinedTextField(
                                    value = manualLanUrl,
                                    onValueChange = { manualLanUrl = it },
                                    label = { Text("Server LAN URL (bv. http://192.168.1.50:8090)") },
                                    singleLine = true,
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = OutlinedTextFieldDefaults.colors(
                                        focusedBorderColor = EmeraldPrimary,
                                        unfocusedBorderColor = DividerDark,
                                        focusedTextColor = TextPrimary,
                                        unfocusedTextColor = TextPrimary
                                    )
                                )
                                OutlinedTextField(
                                    value = manualWanUrl,
                                    onValueChange = { manualWanUrl = it },
                                    label = { Text("Server WAN / Domein (optioneel)") },
                                    singleLine = true,
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = OutlinedTextFieldDefaults.colors(
                                        focusedBorderColor = EmeraldPrimary,
                                        unfocusedBorderColor = DividerDark,
                                        focusedTextColor = TextPrimary,
                                        unfocusedTextColor = TextPrimary
                                    )
                                )
                            }
                        }
                    }
                }
            }

            // ══════════════════════════════════════════════════════════════
            // Veld 1 — Jouw naam (verplicht)
            // ══════════════════════════════════════════════════════════════
            Column(
                modifier = Modifier.fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(4.dp)
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

                if (userName.isNotBlank() && !isNameValid) {
                    Text(
                        text = "Vul minimaal 2 tekens in",
                        color = AmberWarning,
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(start = 4.dp)
                    )
                }
            }

            // ══════════════════════════════════════════════════════════════
            // Veld 2 & 3 — Deurbel Inloggegevens
            // ══════════════════════════════════════════════════════════════
            val isWan = discoveryResult?.discoveredViaWan == true || !networkDiscovery.isWifiConnected()
            val isCameraAlreadyConfigured = discoveryResult?.cameraConfigured == true

            if (isCameraAlreadyConfigured && !isWan) {
                // Op vertrouwd lokaal Wi-Fi (LAN): standaard ingeklapt onder "⚙️ Deurbel-inloggegevens"
                Surface(
                    color = SurfaceVariantDark,
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = "⚙️ Deurbel-inloggegevens (al geconfigureerd op server)",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                        TextButton(onClick = { showManualCredentials = !showManualCredentials }) {
                            Text(
                                text = if (showManualCredentials) "Sluiten" else "Aanpassen",
                                color = CyanAccent,
                                fontSize = 12.sp
                            )
                        }
                    }
                }
            }

            if (!isCameraAlreadyConfigured || showManualCredentials || isWan) {
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    if (isWan && isCameraAlreadyConfigured) {
                        Text(
                            text = "Ter beveiliging op 5G Cloud: controleer gebruikersnaam en wachtwoord.",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                    }

                    // Veld 2: Deurbel Gebruikersnaam
                    OutlinedTextField(
                        value = doorbellUser,
                        onValueChange = { doorbellUser = it },
                        label = { Text("Deurbel Gebruikersnaam") },
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

                    // Veld 3: Deurbel Wachtwoord / Verificatiecode + Info Icoon
                    OutlinedTextField(
                        value = doorbellPassword,
                        onValueChange = { doorbellPassword = it },
                        label = { Text("Deurbel Wachtwoord / Verificatiecode") },
                        placeholder = { Text("Verificatiecode op sticker of camera-wachtwoord") },
                        singleLine = true,
                        visualTransformation = if (passwordVisible) VisualTransformation.None else PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(12.dp),
                        trailingIcon = {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                IconButton(onClick = { passwordVisible = !passwordVisible }) {
                                    Icon(
                                        imageVector = if (passwordVisible) Icons.Default.Visibility else Icons.Default.VisibilityOff,
                                        contentDescription = if (passwordVisible) "Verberg wachtwoord" else "Toon wachtwoord",
                                        tint = TextSecondary
                                    )
                                }
                                IconButton(onClick = { showPasswordInfoDialog = true }) {
                                    Icon(
                                        imageVector = Icons.Default.Info,
                                        contentDescription = "Informatie over wachtwoord",
                                        tint = CyanAccent
                                    )
                                }
                            }
                        },
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
                }
            }

            // Foutmelding
            errorMessage?.let {
                Card(
                    colors = CardDefaults.cardColors(containerColor = CoralDecline.copy(alpha = 0.15f)),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        text = it,
                        color = CoralDecline,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(12.dp)
                    )
                }
            }

            // ══════════════════════════════════════════════════════════════
            // Eén Knop — "Koppelen"
            // ══════════════════════════════════════════════════════════════
            Button(
                onClick = {
                    if (!isNameValid) return@Button

                    val targetLan = discoveryResult?.serverLanUrl?.ifBlank { null }
                        ?: manualLanUrl.trim().ifBlank { null }
                    val targetWan = discoveryResult?.serverWanUrl?.ifBlank { null }
                        ?: manualWanUrl.trim().ifBlank { null }

                    if (targetLan == null && targetWan == null) {
                        errorMessage = "Geen server-URL beschikbaar. Voer handmatig een serveradres in of voer een netwerkscan uit."
                        return@Button
                    }

                    coroutineScope.launch {
                        isPairing = true
                        errorMessage = null
                        try {
                            val fcmToken = prefs.getFcmToken()
                            val androidId = Settings.Secure.getString(
                                context.contentResolver,
                                Settings.Secure.ANDROID_ID
                            ) ?: ""

                            val bootstrapToken = discoveryResult?.bootstrapToken

                            // Optionally update camera credentials on the backend if the user entered any
                            if (doorbellPassword.isNotBlank()) {
                                val camIp = discoveryResult?.cameraIp ?: ""
                                val credsPayload = CameraSettingsUpdate(
                                    ip = camIp,
                                    port = 8000,
                                    user = doorbellUser.trim().ifBlank { "admin" },
                                    password = doorbellPassword
                                )
                                try {
                                    if (targetLan != null) {
                                        ApiClientFactory.createApi(targetLan).updateCameraSettings(credsPayload)
                                    } else if (targetWan != null) {
                                        ApiClientFactory.createApi(targetWan).updateCameraSettings(credsPayload)
                                    }
                                } catch (_: Exception) {}
                            }

                            val pairRequest = PairRequest(
                                userName = userName.trim(),
                                deviceName = deviceName.trim(),
                                fcmToken = fcmToken,
                                deviceId = androidId,
                                androidVersion = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
                                connectionType = "AUTO_DISCOVERY",
                                token = bootstrapToken
                            )

                            val isWanOnly = discoveryResult?.discoveredViaWan == true || !networkDiscovery.isWifiConnected()

                            // 1. Probeer eerst LAN indien niet op WAN/5G
                            var resp = if (!isWanOnly && targetLan != null) {
                                try {
                                    val lanApi = ApiClientFactory.createApi(targetLan)
                                    lanApi.pair(pairRequest)
                                } catch (_: Exception) {
                                    null
                                }
                            } else null

                            // 2. Als LAN faalt of op 5G/WAN zit: roep direct WAN (https://deurbel.foxway.nl) aan
                            if ((resp == null || !resp.isSuccessful) && targetWan != null) {
                                try {
                                    val wanApi = ApiClientFactory.createApi(targetWan)
                                    resp = wanApi.pair(pairRequest)
                                } catch (_: Exception) {
                                    // WAN also failed
                                }
                            }

                            if (resp != null && resp.isSuccessful && resp.body() != null) {
                                val body = resp.body()!!
                                val lanToSave = body.lanUrl.ifBlank { targetLan ?: "http://192.168.2.1:8090" }
                                val wanToSave = body.wanUrl.ifBlank { targetWan ?: "https://deurbel.foxway.nl" }

                                prefs.savePairingData(
                                    userId = body.userId,
                                    userName = body.userName,
                                    deviceName = body.deviceName,
                                    jwtToken = body.jwtToken,
                                    lanUrl = lanToSave,
                                    wanUrl = wanToSave,
                                    go2rtcLanWs = body.go2rtcLanWs,
                                    go2rtcWanWs = body.go2rtcWanWs
                                )
                                ConnectionManager.getInstance(context).updateEndpoints(
                                    lan = lanToSave,
                                    wan = wanToSave
                                )
                                AppLogger.getInstance(context).info("Toestel gekoppeld als ${body.userName}")
                                onPairingSuccess()
                            } else {
                                val errorDetail = try {
                                    resp?.errorBody()?.string()?.let { errStr ->
                                        JSONObject(errStr).optString("detail", errStr)
                                    }
                                } catch (_: Exception) { null }
                                errorMessage = errorDetail ?: "Koppelen mislukt (${resp?.code() ?: "Geen verbinding met server"})"
                            }
                        } catch (e: Exception) {
                            errorMessage = "Fout bij koppelen: ${e.localizedMessage}"
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
                        text = if (!isNameValid) "Vul naam in om te koppelen" else "Koppelen",
                        color = if (isNameValid) BackgroundDark else TextSecondary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 16.sp
                    )
                }
            }

            // ══════════════════════════════════════════════════════════════
            // Samsung One UI Permission Checklist
            // ══════════════════════════════════════════════════════════════
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

    // Info Alert Dialog voor wachtwoord / verificatiecode
    if (showPasswordInfoDialog) {
        AlertDialog(
            onDismissRequest = { showPasswordInfoDialog = false },
            title = {
                Text(
                    text = "Deurbel Inloggegevens",
                    fontWeight = FontWeight.Bold,
                    color = TextPrimary
                )
            },
            text = {
                Text(
                    text = "De standaard gebruikersnaam is 'admin'. Het wachtwoord is de verificatiecode (6-12 tekens op de sticker achterop de deurbel of in het boekje) of het zelf ingestelde camera-wachtwoord.",
                    color = TextSecondary,
                    lineHeight = 20.sp
                )
            },
            confirmButton = {
                Button(
                    onClick = { showPasswordInfoDialog = false },
                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary)
                ) {
                    Text("Begrepen", color = BackgroundDark, fontWeight = FontWeight.Bold)
                }
            },
            containerColor = SurfaceGlass,
            shape = RoundedCornerShape(16.dp)
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
                Text(
                    text = title,
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = TextPrimary
                )
                Text(
                    text = subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary
                )
            }
        }
        IconButton(onClick = onClick) {
            Icon(
                imageVector = Icons.Default.ChevronRight,
                contentDescription = "Instellen",
                tint = TextSecondary
            )
        }
    }
}
