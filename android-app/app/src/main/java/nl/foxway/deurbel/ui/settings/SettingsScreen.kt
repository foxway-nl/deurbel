package nl.foxway.deurbel.ui.settings

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.AppLogger
import nl.foxway.deurbel.data.LogCategory
import nl.foxway.deurbel.data.LogEntry
import nl.foxway.deurbel.data.models.*
import coil.compose.AsyncImage
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.EndpointResolver
import nl.foxway.deurbel.ui.theme.*
import nl.foxway.deurbel.util.PermissionChecker
import nl.foxway.deurbel.util.SystemPermissionState

/**
 * Settings & System Diagnostics Screen.
 *
 * Implements SPEC §4 & Round 2 Requirements #2, #6, #7, #8:
 *  - Realtime 5-Point Android System & Permission Checklist
 *  - Active Family Members / Connected Devices Card
 *  - Hardware & Sound Diagnostic Test (Live video, outdoor audio meter, speak test)
 *  - 30-Day Rotating Event Logbook with filter, search, copy & clear
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onUnpair: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val resolver = remember { EndpointResolver(prefs) }
    val appLogger = remember { AppLogger.getInstance(context) }

    val userName by prefs.userName.collectAsState(initial = "")
    val userId by prefs.userId.collectAsState(initial = "")

    var endpointConfig by remember { mutableStateOf(EndpointConfig.DEFAULT) }
    var connectionStatus by remember { mutableStateOf("Nog niet getest") }
    var isTesting by remember { mutableStateOf(false) }

    // Realtime permissions check
    var permState by remember { mutableStateOf(PermissionChecker.checkAll(context)) }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                permState = PermissionChecker.checkAll(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    // Active users
    var activeUsers by remember { mutableStateOf<List<UserInfo>>(emptyList()) }
    var isLoadingUsers by remember { mutableStateOf(false) }

    // Smart AI Detection & Storage settings state
    var detectionSettings by remember { mutableStateOf(DetectionSettingsDto()) }
    var storageStats by remember { mutableStateOf(StorageStatsDto()) }
    var currentApiBaseUrl by remember { mutableStateOf("http://192.168.2.1:8090") }
    var isCleaningJunk by remember { mutableStateOf(false) }

    // Dialogs
    var showHardwareTestDialog by remember { mutableStateOf(false) }
    var showLogsDialog by remember { mutableStateOf(false) }
    var showQrDialog by remember { mutableStateOf(false) }
    var isTriggeringShelly by remember { mutableStateOf(false) }
    var shellyTriggerResult by remember { mutableStateOf<String?>(null) }

    val modernSwitchColors = SwitchDefaults.colors(
        checkedThumbColor = EmeraldPrimary,
        checkedTrackColor = EmeraldPrimary.copy(alpha = 0.5f),
        uncheckedThumbColor = Color(0xFFB0BEC5),
        uncheckedTrackColor = Color(0xFF37474F),
        uncheckedBorderColor = Color(0xFF546E7A)
    )

    val loadRemoteSettings = {
        coroutineScope.launch {
            try {
                val resolved = resolver.resolve()
                currentApiBaseUrl = resolved.apiBaseUrl
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                val det = api.getDetectionSettings()
                if (det.isSuccessful) {
                    det.body()?.let { detectionSettings = it }
                }
                val stor = api.getStorageSettings()
                if (stor.isSuccessful) {
                    stor.body()?.let { storageStats = it }
                }
            } catch (_: Exception) {}
        }
    }

    val saveDetection: (DetectionSettingsDto) -> Unit = { updated ->
        detectionSettings = updated
        coroutineScope.launch {
            try {
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                api.updateDetectionSettings(updated)
            } catch (_: Exception) {}
        }
    }

    val saveRetention: (Int) -> Unit = { days ->
        coroutineScope.launch {
            try {
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                val resp = api.updateStorageSettings(StorageSettingsUpdate(days, storageStats.maxStorageGb))
                if (resp.isSuccessful) {
                    resp.body()?.let { storageStats = it }
                    Toast.makeText(context, "Bewaarduur ingesteld op $days dagen", Toast.LENGTH_SHORT).show()
                }
            } catch (_: Exception) {}
        }
    }

    val refreshUsers = {
        coroutineScope.launch {
            try {
                isLoadingUsers = true
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                val resp = api.getUsers()
                if (resp.isSuccessful) {
                    activeUsers = resp.body() ?: emptyList()
                }
            } catch (_: Exception) {
            } finally {
                isLoadingUsers = false
            }
        }
    }

    val deleteUser: (String) -> Unit = { targetUserId ->
        coroutineScope.launch {
            try {
                activeUsers = activeUsers.filter { it.userId != targetUserId }
                val resolved = resolver.resolve()
                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                api.deleteUser(targetUserId)
                Toast.makeText(context, "Apparaat verwijderd", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                refreshUsers()
            }
        }
    }

    LaunchedEffect(Unit) {
        endpointConfig = prefs.getEndpointConfig()
        refreshUsers()
        loadRemoteSettings()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Instellingen & Beheer", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = BackgroundDark,
                    titleContentColor = TextPrimary
                ),
                actions = {
                    IconButton(onClick = {
                        permState = PermissionChecker.checkAll(context)
                        refreshUsers()
                    }) {
                        Icon(imageVector = Icons.Default.Refresh, contentDescription = "Vernieuwen", tint = TextPrimary)
                    }
                }
            )
        },
        containerColor = BackgroundDark
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp)
        ) {
            // ── 1. User Profile Card ─────────────────────────────────
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Row(
                    modifier = Modifier.padding(16.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(54.dp)
                            .background(EmeraldDark, CircleShape),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            text = userName.take(1).uppercase().ifBlank { "M" },
                            style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                    }

                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text(
                            text = userName.ifBlank { "Foxway Gebruiker" },
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                        Text(
                            text = "Gekoppeld ID: ${userId.take(12)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                    }
                }
            }

            // ── 2. Realtime Permissie & Systeem Check (Requirement #2) ──
            Text("Android Systeem & Betrouwbaarheid", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    PermissionCheckItem(
                        title = "Meldingen (POST_NOTIFICATIONS)",
                        subtitle = "Noodzakelijk om oproepen te ontvangen",
                        isGranted = permState.hasNotificationPermission,
                        onFix = { PermissionChecker.openNotificationSettings(context) }
                    )
                    HorizontalDivider(color = DividerDark)

                    PermissionCheckItem(
                        title = "Microfoon (RECORD_AUDIO)",
                        subtitle = "Noodzakelijk voor terugpraten naar buiten",
                        isGranted = permState.hasRecordAudioPermission,
                        onFix = { PermissionChecker.openAppDetails(context) }
                    )
                    HorizontalDivider(color = DividerDark)

                    PermissionCheckItem(
                        title = "Batterijbeheer: Niet beperkt",
                        subtitle = "Voorkomt dat Android de deurbel-achtergrondservice stopt",
                        isGranted = permState.isBatteryOptimizationIgnored,
                        onFix = { PermissionChecker.openBatteryOptimizationSettings(context) }
                    )
                    HorizontalDivider(color = DividerDark)

                    PermissionCheckItem(
                        title = "Bovenop weergeven (Appear on top)",
                        subtitle = "Breekt direct open bij een inkomende oproep",
                        isGranted = permState.canDrawOverlays,
                        onFix = { PermissionChecker.openOverlaySettings(context) }
                    )
                    HorizontalDivider(color = DividerDark)

                    PermissionCheckItem(
                        title = "Volledig scherm meldingen (Android 14+)",
                        subtitle = "Laat de deurbel direct rinkelen als het scherm uit staat",
                        isGranted = permState.canUseFullScreenIntent,
                        onFix = { PermissionChecker.openFullScreenIntentSettings(context) }
                    )
                }
            }

            // ── 3. Actieve Gezinsleden / Apparaten (Requirement #6) ──
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Actieve Gezinsleden / Apparaten", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
                if (isLoadingUsers) {
                    CircularProgressIndicator(color = EmeraldPrimary, modifier = Modifier.size(18.dp))
                }
            }

            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    if (activeUsers.isEmpty()) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Box(modifier = Modifier.size(8.dp).background(LanGreen, CircleShape))
                            Text(
                                text = "$userName (Dit toestel) • Online",
                                style = MaterialTheme.typography.bodyMedium,
                                color = TextPrimary
                            )
                        }
                    } else {
                        activeUsers.forEachIndexed { index, user ->
                            val isCurrentDevice = (user.userId == userId) ||
                                    (user.userName.equals(userName, ignoreCase = true) && user.deviceName.equals(android.os.Build.MODEL, ignoreCase = true))
                            val isOnline = isCurrentDevice || user.online

                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Row(
                                    modifier = Modifier.weight(1f),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                                ) {
                                    Box(
                                        modifier = Modifier
                                            .size(10.dp)
                                            .background(if (isOnline) LanGreen else TextSecondary, CircleShape)
                                    )
                                    Column {
                                        Text(
                                            text = if (isCurrentDevice) "${user.userName} (${user.deviceName}) • Dit toestel"
                                            else "${user.userName} (${user.deviceName})",
                                            style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                                            color = TextPrimary
                                        )
                                        Text(
                                            text = if (isOnline) "🟢 Nu verbonden"
                                            else "Laatst gezien: ${formatAmsterdamTime(user.lastSeen)}",
                                            style = MaterialTheme.typography.bodySmall,
                                            color = if (isOnline) EmeraldLight else TextSecondary
                                        )
                                        if (user.androidVersion.isNotBlank() || user.connectionType.isNotBlank()) {
                                            Text(
                                                text = "📱 ${user.androidVersion.ifBlank { "Android" }} • ${user.connectionType.ifBlank { "LAN/WAN" }}",
                                                style = MaterialTheme.typography.labelSmall,
                                                color = CyanAccent
                                            )
                                        }
                                    }
                                }

                                if (!isOnline && !isCurrentDevice) {
                                    IconButton(
                                        onClick = { deleteUser(user.userId) },
                                        modifier = Modifier.size(32.dp)
                                    ) {
                                        Icon(
                                            imageVector = Icons.Default.DeleteOutline,
                                            contentDescription = "Verwijderen",
                                            tint = CoralDecline,
                                            modifier = Modifier.size(18.dp)
                                        )
                                    }
                                }
                            }
                            if (index < activeUsers.size - 1) {
                                HorizontalDivider(color = DividerDark)
                            }
                        }
                    }

                    HorizontalDivider(color = DividerDark)
                    OutlinedButton(
                        onClick = { showQrDialog = true },
                        modifier = Modifier
                            .fillMaxWidth()
                            .defaultMinSize(minHeight = 44.dp),
                        shape = RoundedCornerShape(10.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CyanAccent),
                        border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.5f))
                    ) {
                        Icon(imageVector = Icons.Default.QrCode, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("Toon Koppel QR-Code voor Gezinslid", fontWeight = FontWeight.SemiBold)
                    }
                }
            }

            // ── 3a. Opname- & Detectie-instellingen (Task 5) ────────
            Text("Opname- & Detectie-instellingen", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    // Master AI Switch
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column(modifier = Modifier.weight(1f).padding(end = 12.dp)) {
                            Text("Slimme AI Detectie (Arlo-stijl)", fontWeight = FontWeight.Bold, color = TextPrimary)
                            Text("Detecteert objecten op basis van lichte tweetraps CPU-analyse", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                        }
                        Switch(
                            checked = detectionSettings.detectionEnabled,
                            onCheckedChange = { saveDetection(detectionSettings.copy(detectionEnabled = it)) },
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    Text("Kies welke categorieën worden opgenomen:", style = MaterialTheme.typography.labelMedium, color = CyanAccent)

                    // 1. Mensen
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.Person, contentDescription = null, tint = EmeraldPrimary, modifier = Modifier.size(20.dp))
                            Text("Mensen opnemen", color = TextPrimary)
                        }
                        Switch(
                            checked = detectionSettings.recordPerson,
                            onCheckedChange = { saveDetection(detectionSettings.copy(recordPerson = it)) },
                            enabled = detectionSettings.detectionEnabled,
                            colors = modernSwitchColors
                        )
                    }

                    // 2. Voertuigen
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.DirectionsCar, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(20.dp))
                            Text("Voertuigen opnemen (auto, fiets, vracht)", color = TextPrimary)
                        }
                        Switch(
                            checked = detectionSettings.recordVehicle,
                            onCheckedChange = { saveDetection(detectionSettings.copy(recordVehicle = it)) },
                            enabled = detectionSettings.detectionEnabled,
                            colors = modernSwitchColors
                        )
                    }

                    // 3. Dieren
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.Pets, contentDescription = null, tint = Color(0xFFF59E0B), modifier = Modifier.size(20.dp))
                            Text("Dieren opnemen (hond, kat, vogel)", color = TextPrimary)
                        }
                        Switch(
                            checked = detectionSettings.recordAnimal,
                            onCheckedChange = { saveDetection(detectionSettings.copy(recordAnimal = it)) },
                            enabled = detectionSettings.detectionEnabled,
                            colors = modernSwitchColors
                        )
                    }

                    // 4. Overige beweging
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.MotionPhotosOn, contentDescription = null, tint = TextSecondary, modifier = Modifier.size(20.dp))
                            Text("Overige beweging opnemen", color = TextPrimary)
                        }
                        Switch(
                            checked = detectionSettings.recordMotion,
                            onCheckedChange = { saveDetection(detectionSettings.copy(recordMotion = it)) },
                            enabled = detectionSettings.detectionEnabled,
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    // Stille Uren / Nachtmodus
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column(modifier = Modifier.weight(1f).padding(end = 12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                Icon(imageVector = Icons.Default.Bedtime, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(18.dp))
                                Text("Stille Uren / Nachtmodus", fontWeight = FontWeight.SemiBold, color = TextPrimary)
                            }
                            Text("Muteert luide meldingen tussen ${detectionSettings.quietHoursStart} en ${detectionSettings.quietHoursEnd}, opnames blijven actief.", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                        }
                        Switch(
                            checked = detectionSettings.quietHoursEnabled,
                            onCheckedChange = { saveDetection(detectionSettings.copy(quietHoursEnabled = it)) },
                            colors = modernSwitchColors
                        )
                    }

                    // Activiteitszone Slider & Live Voorbeeld
                    Column(
                        modifier = Modifier.fillMaxWidth(),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                Icon(imageVector = Icons.Default.Crop, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(18.dp))
                                Text("Activiteitszone (Bovenkant negeren)", fontWeight = FontWeight.SemiBold, color = TextPrimary)
                            }
                            Text(
                                text = "${detectionSettings.activityZoneTopIgnorePercent}%",
                                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                                color = CyanAccent
                            )
                        }
                        Text(
                            text = "Negeert beweging op straat of overhangende bomen in de bovenste zone van het camerabeeld (0% - 70%).",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )

                        Slider(
                            value = detectionSettings.activityZoneTopIgnorePercent.toFloat(),
                            onValueChange = { newPct ->
                                detectionSettings = detectionSettings.copy(activityZoneTopIgnorePercent = newPct.toInt())
                            },
                            onValueChangeFinished = {
                                saveDetection(detectionSettings)
                            },
                            valueRange = 0f..70f,
                            steps = 13,
                            colors = SliderDefaults.colors(
                                thumbColor = CyanAccent,
                                activeTrackColor = CyanAccent,
                                inactiveTrackColor = SurfaceVariantDark
                            )
                        )

                        // Live Snapshot Preview with Red Line Overlay
                        var previewTimestamp by remember { mutableLongStateOf(System.currentTimeMillis()) }
                        Surface(
                            shape = RoundedCornerShape(12.dp),
                            color = BackgroundDark,
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(180.dp)
                                .border(1.dp, DividerDark, RoundedCornerShape(12.dp))
                        ) {
                            Box(contentAlignment = Alignment.Center) {
                                val previewUrl = "$currentApiBaseUrl/api/v1/activity-zone/preview?top_pct=${detectionSettings.activityZoneTopIgnorePercent}&t=$previewTimestamp"
                                AsyncImage(
                                    model = previewUrl,
                                    contentDescription = "Activiteitszone Voorbeeld",
                                    modifier = Modifier.fillMaxSize(),
                                    contentScale = androidx.compose.ui.layout.ContentScale.Fit
                                )
                                IconButton(
                                    onClick = { previewTimestamp = System.currentTimeMillis() },
                                    modifier = Modifier
                                        .align(Alignment.TopEnd)
                                        .padding(6.dp)
                                        .size(32.dp)
                                        .background(BackgroundDark.copy(alpha = 0.6f), CircleShape)
                                ) {
                                    Icon(Icons.Default.Refresh, contentDescription = "Vernieuw snapshot", tint = TextPrimary, modifier = Modifier.size(18.dp))
                                }
                            }
                        }
                    }
                }
            }

            // ── 3a2. Meldingen bij Beweging & AI Detectie ──────────────────────
            Text("Meldingen bij Beweging & AI Detectie", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    Text("Stel in voor welke gedetecteerde gebeurtenissen je een pushmelding op je telefoon ontvangt:", style = MaterialTheme.typography.bodySmall, color = TextSecondary)

                    // 1. Mensen melding
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.Person, contentDescription = null, tint = EmeraldPrimary, modifier = Modifier.size(20.dp))
                            Column {
                                Text("Melding bij Persoon", color = TextPrimary, fontWeight = FontWeight.Medium)
                                Text("Directe pushmelding als er iemand bij de voordeur loopt", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                            }
                        }
                        Switch(
                            checked = detectionSettings.notifyPerson,
                            onCheckedChange = { saveDetection(detectionSettings.copy(notifyPerson = it)) },
                            enabled = detectionSettings.detectionEnabled && detectionSettings.recordPerson,
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    // 2. Voertuigen melding
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.DirectionsCar, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(20.dp))
                            Column {
                                Text("Melding bij Voertuig", color = TextPrimary, fontWeight = FontWeight.Medium)
                                Text("Pushmelding bij auto's, bezorgbussen of fietsers", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                            }
                        }
                        Switch(
                            checked = detectionSettings.notifyVehicle,
                            onCheckedChange = { saveDetection(detectionSettings.copy(notifyVehicle = it)) },
                            enabled = detectionSettings.detectionEnabled && detectionSettings.recordVehicle,
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    // 3. Dieren melding
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.Pets, contentDescription = null, tint = Color(0xFFF59E0B), modifier = Modifier.size(20.dp))
                            Column {
                                Text("Melding bij Dier", color = TextPrimary, fontWeight = FontWeight.Medium)
                                Text("Pushmelding bij katten, honden of vogels", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                            }
                        }
                        Switch(
                            checked = detectionSettings.notifyAnimal,
                            onCheckedChange = { saveDetection(detectionSettings.copy(notifyAnimal = it)) },
                            enabled = detectionSettings.detectionEnabled && detectionSettings.recordAnimal,
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    // 4. Overige beweging melding
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Row(
                            modifier = Modifier.weight(1f).padding(end = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(imageVector = Icons.Default.MotionPhotosOn, contentDescription = null, tint = TextSecondary, modifier = Modifier.size(20.dp))
                            Column {
                                Text("Melding bij Overige Beweging", color = TextPrimary, fontWeight = FontWeight.Medium)
                                Text("Pushmelding bij pixelbeweging zonder objectherkenning", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                            }
                        }
                        Switch(
                            checked = detectionSettings.notifyMotion,
                            onCheckedChange = { saveDetection(detectionSettings.copy(notifyMotion = it)) },
                            enabled = detectionSettings.detectionEnabled && detectionSettings.recordMotion,
                            colors = modernSwitchColors
                        )
                    }
                }
            }

            // ── 3b. Bewaarduur & Opslagcapaciteit (Task 6) ────────────
            Text("Bewaarduur & Opslagcapaciteit", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    // Disk Usage
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text("Huidig opslaggebruik", fontWeight = FontWeight.Bold, color = TextPrimary)
                            Text(
                                text = "${storageStats.usedMb} MB van ${storageStats.maxStorageGb} GB (${storageStats.usagePercent}%)",
                                style = MaterialTheme.typography.bodySmall,
                                color = CyanAccent
                            )
                        }
                        Text("${storageStats.totalClips} opnames", style = MaterialTheme.typography.labelMedium, color = TextSecondary)
                    }

                    LinearProgressIndicator(
                        progress = (storageStats.usagePercent.toFloat() / 100f).coerceIn(0f, 1f),
                        modifier = Modifier.fillMaxWidth().height(8.dp).clip(RoundedCornerShape(4.dp)),
                        color = if (storageStats.usagePercent > 85) Color(0xFFEF4444) else EmeraldPrimary,
                        trackColor = SurfaceVariantDark
                    )

                    HorizontalDivider(color = DividerDark)

                    // Bewaarduur Dagen Buttons
                    Text("Automatische Bewaarduur:", fontWeight = FontWeight.SemiBold, color = TextPrimary)
                    Text("Oudere niet-beveiligde opnames worden automatisch gewist om schijfruimte te sparen.", style = MaterialTheme.typography.bodySmall, color = TextSecondary)

                    val retentionOptions = listOf(3, 7, 14, 30, 60, 90)
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        retentionOptions.take(3).forEach { days ->
                            val isSelected = storageStats.retentionDays == days
                            Surface(
                                color = if (isSelected) EmeraldPrimary else SurfaceVariantDark,
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier
                                    .weight(1f)
                                    .clickable { saveRetention(days) }
                            ) {
                                Box(
                                    modifier = Modifier.padding(vertical = 10.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(
                                        text = "$days dgn",
                                        fontWeight = FontWeight.Bold,
                                        color = if (isSelected) BackgroundDark else TextPrimary,
                                        fontSize = 13.sp
                                    )
                                }
                            }
                        }
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        retentionOptions.drop(3).forEach { days ->
                            val isSelected = storageStats.retentionDays == days
                            Surface(
                                color = if (isSelected) EmeraldPrimary else SurfaceVariantDark,
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier
                                    .weight(1f)
                                    .clickable { saveRetention(days) }
                            ) {
                                Box(
                                    modifier = Modifier.padding(vertical = 10.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(
                                        text = "$days dgn",
                                        fontWeight = FontWeight.Bold,
                                        color = if (isSelected) BackgroundDark else TextPrimary,
                                        fontSize = 13.sp
                                    )
                                }
                            }
                        }
                    }

                    Surface(
                        color = SurfaceGlass,
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Text("⭐", fontSize = 16.sp)
                            Text(
                                text = "Opnames met een ster worden nooit automatisch gewist.",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextPrimary
                            )
                        }
                    }

                    HorizontalDivider(color = DividerDark)

                    OutlinedButton(
                        onClick = {
                            coroutineScope.launch {
                                try {
                                    isCleaningJunk = true
                                    val resolved = resolver.resolve()
                                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                                    val resp = api.cleanupUnclassifiedClips(includeVehicles = true)
                                    if (resp.isSuccessful) {
                                        val body = resp.body()
                                        val del = (body?.get("deleted_count") as? Number)?.toInt() ?: 0
                                        val freed = (body?.get("freed_mb") as? Number)?.toDouble() ?: 0.0
                                        Toast.makeText(context, "🧹 Opschonen voltooid: $del opnames gewist (${String.format(java.util.Locale.US, "%.1f", freed)} MB vrijgemaakt)", Toast.LENGTH_LONG).show()
                                        loadRemoteSettings()
                                    } else {
                                        Toast.makeText(context, "Opschonen mislukt: HTTP ${resp.code()}", Toast.LENGTH_SHORT).show()
                                    }
                                } catch (e: Exception) {
                                    Toast.makeText(context, "Fout: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
                                } finally {
                                    isCleaningJunk = false
                                }
                            }
                        },
                        enabled = !isCleaningJunk,
                        modifier = Modifier.fillMaxWidth().defaultMinSize(minHeight = 44.dp),
                        shape = RoundedCornerShape(10.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CoralDecline),
                        border = BorderStroke(1.dp, CoralDecline.copy(alpha = 0.5f))
                    ) {
                        if (isCleaningJunk) {
                            CircularProgressIndicator(color = CoralDecline, modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                            Spacer(modifier = Modifier.width(8.dp))
                            Text("Bezig met opschonen...")
                        } else {
                            Icon(imageVector = Icons.Default.DeleteSweep, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text("🧹 Wis alle niet-geclassificeerde / oude bewegingsopnames", fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }

            // ── 4. Systeem- & Geluidstest (Requirement #7) ───────────
            Text("Systeem- & Geluidstest", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Text(
                        text = "Controleer of de deurbel-camera, de microfoon buiten en de buiten-luidspreker goed functioneren.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Button(
                        onClick = { showHardwareTestDialog = true },
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                        modifier = Modifier.fillMaxWidth().height(48.dp)
                    ) {
                        Icon(imageVector = Icons.Default.GraphicEq, contentDescription = null, tint = BackgroundDark)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("Start Deurbel Hardware Test", color = BackgroundDark, fontWeight = FontWeight.Bold)
                    }
                }
            }

            // ── 5. Lokaal 30-Dagen Logboek (Requirement #8) ──────────
            Text("Systeem Logboek", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Text(
                        text = "Lokale gebeurtenissen van de afgelopen 30 dagen: oproepen, wie heeft opgenomen, netwerkwissels en WebRTC-status.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    OutlinedButton(
                        onClick = { showLogsDialog = true },
                        shape = RoundedCornerShape(12.dp),
                        modifier = Modifier.fillMaxWidth().height(48.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
                    ) {
                        Icon(imageVector = Icons.Default.Article, contentDescription = null, tint = CyanAccent)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("Logboek bekijken (30 dagen)", color = CyanAccent)
                    }
                }
            }

            // ── 6. Verbinding & Endpoints ────────────────────────────
            Text("Netwerk & Verbinding", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    EndpointRow(label = "LAN API", value = endpointConfig.lanApiUrl)
                    HorizontalDivider(color = DividerDark)
                    EndpointRow(label = "WAN API (Foxway Cloud)", value = endpointConfig.wanApiUrl)
                    HorizontalDivider(color = DividerDark)
                    EndpointRow(label = "WebRTC Proxy", value = "/api/v1/webrtc?src=deurbel (WHEP)")

                    Button(
                        onClick = {
                            coroutineScope.launch {
                                try {
                                    isTesting = true
                                    val resolved = resolver.resolve()
                                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                                    val resp = api.getStatus()
                                    if (resp.isSuccessful) {
                                        connectionStatus = "Verbonden (${if (resolved.isLan) "LAN" else "WAN"}) — Status: ${resp.body()?.state}"
                                        appLogger.network("Handmatige netwerktest succesvol: ${resolved.apiBaseUrl}")
                                    } else {
                                        connectionStatus = "Fout: HTTP ${resp.code()}"
                                    }
                                } catch (e: Exception) {
                                    connectionStatus = "Niet bereikbaar: ${e.localizedMessage}"
                                    appLogger.error("Netwerktest mislukt", e.message)
                                } finally {
                                    isTesting = false
                                }
                            }
                        },
                        enabled = !isTesting,
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = SurfaceVariantDark),
                        modifier = Modifier.fillMaxWidth().height(44.dp)
                    ) {
                        if (isTesting) {
                            CircularProgressIndicator(color = EmeraldPrimary, modifier = Modifier.size(18.dp))
                        } else {
                            Icon(imageVector = Icons.Default.NetworkCheck, contentDescription = null, tint = TextPrimary, modifier = Modifier.size(18.dp))
                            Spacer(modifier = Modifier.width(6.dp))
                            Text("Verbinding Testen", color = TextPrimary)
                        }
                    }

                    Text(text = connectionStatus, style = MaterialTheme.typography.bodySmall, color = CyanAccent)
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            // ── 6b. Deurbel Drukknop & Shelly Plus Uni Webhook ────────
            Text("Deurbel Drukknop & Shelly Plus Uni Webhook", style = MaterialTheme.typography.titleMedium, color = TextPrimary)
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceDark),
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Box(modifier = Modifier.size(10.dp).background(LanGreen, CircleShape))
                        Text("Aanbel-script actief (Luistert naar 728 Hz chime signature)", style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = TextPrimary)
                    }
                    Text(
                        text = "Shelly Plus Uni Webhook URL (LAN):",
                        style = MaterialTheme.typography.labelMedium,
                        color = CyanAccent
                    )
                    Surface(
                        color = SurfaceVariantDark,
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(
                                text = "http://192.168.2.1:8090/api/v1/webhook/shelly-ring",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextPrimary,
                                modifier = Modifier.weight(1f)
                            )
                            IconButton(
                                onClick = {
                                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                    clipboard.setPrimaryClip(ClipData.newPlainText("Shelly Webhook LAN", "http://192.168.2.1:8090/api/v1/webhook/shelly-ring"))
                                    Toast.makeText(context, "LAN Webhook URL gekopieerd!", Toast.LENGTH_SHORT).show()
                                },
                                modifier = Modifier.size(28.dp)
                            ) {
                                Icon(imageVector = Icons.Default.ContentCopy, contentDescription = "Kopieer", tint = CyanAccent, modifier = Modifier.size(16.dp))
                            }
                        }
                    }

                    Text(
                        text = "Shelly Plus Uni Webhook URL (WAN / Foxway Cloud):",
                        style = MaterialTheme.typography.labelMedium,
                        color = CyanAccent
                    )
                    Surface(
                        color = SurfaceVariantDark,
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(
                                text = "https://deurbel.foxway.nl/api/v1/webhook/shelly-ring",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextPrimary,
                                modifier = Modifier.weight(1f)
                            )
                            IconButton(
                                onClick = {
                                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                    clipboard.setPrimaryClip(ClipData.newPlainText("Shelly Webhook WAN", "https://deurbel.foxway.nl/api/v1/webhook/shelly-ring"))
                                    Toast.makeText(context, "WAN Webhook URL gekopieerd!", Toast.LENGTH_SHORT).show()
                                },
                                modifier = Modifier.size(28.dp)
                            ) {
                                Icon(imageVector = Icons.Default.ContentCopy, contentDescription = "Kopieer", tint = CyanAccent, modifier = Modifier.size(16.dp))
                            }
                        }
                    }

                    Button(
                        onClick = {
                            coroutineScope.launch {
                                try {
                                    isTriggeringShelly = true
                                    shellyTriggerResult = null
                                    val resolved = resolver.resolve()
                                    val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                                    val resp = api.triggerShellyWebhook()
                                    if (resp.isSuccessful) {
                                        shellyTriggerResult = "✅ Drukknop-oproep succesvol gesimuleerd!"
                                    } else {
                                        shellyTriggerResult = "⚠️ Reactie: HTTP ${resp.code()}"
                                    }
                                } catch (e: Exception) {
                                    shellyTriggerResult = "❌ Fout: ${e.localizedMessage}"
                                } finally {
                                    isTriggeringShelly = false
                                }
                            }
                        },
                        enabled = !isTriggeringShelly,
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                        modifier = Modifier.fillMaxWidth().defaultMinSize(minHeight = 44.dp)
                    ) {
                        if (isTriggeringShelly) {
                            CircularProgressIndicator(color = BackgroundDark, modifier = Modifier.size(18.dp))
                        } else {
                            Icon(imageVector = Icons.Default.NotificationsActive, contentDescription = null, tint = BackgroundDark, modifier = Modifier.size(18.dp))
                            Spacer(modifier = Modifier.width(6.dp))
                            Text("Test Aanbel Trigger (Simuleer Drukknop)", color = BackgroundDark, fontWeight = FontWeight.Bold)
                        }
                    }

                    shellyTriggerResult?.let {
                        Text(text = it, style = MaterialTheme.typography.bodySmall, color = CyanAccent)
                    }
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            // ── 7. Ontkoppelen ───────────────────────────────────────
            OutlinedButton(
                onClick = {
                    coroutineScope.launch {
                        prefs.clearAll()
                        appLogger.info("Toestel ontkoppeld door gebruiker")
                        onUnpair()
                    }
                },
                shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = CoralDecline),
                border = BorderStroke(1.dp, CoralDecline),
                modifier = Modifier.fillMaxWidth().height(48.dp)
            ) {
                Icon(imageVector = Icons.Default.LinkOff, contentDescription = null, tint = CoralDecline)
                Spacer(modifier = Modifier.width(8.dp))
                Text("Apparaat Ontkoppelen", color = CoralDecline, fontWeight = FontWeight.SemiBold)
            }

            // App Versie & Updates Footer
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 12.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                Text(
                    text = "Foxway Deurbel v1.3.0 (Build 4)",
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = TextPrimary
                )
                Text(
                    text = "Proxy: https://deurbel.foxway.nl (Poort 443 HTTPS/WSS)",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary
                )
            }
        }
    }

    // ── Dialog 1: Hardware Test Dialog (Requirement #7) ──────────────
    if (showHardwareTestDialog) {
        HardwareTestDialog(
            resolver = resolver,
            onDismiss = { showHardwareTestDialog = false }
        )
    }

    // ── Dialog 2: 30-Dagen Logboek Dialog (Requirement #8) ───────────
    if (showLogsDialog) {
        LogViewerDialog(
            appLogger = appLogger,
            onDismiss = { showLogsDialog = false }
        )
    }

    // ── Dialog 3: Koppel QR-Code Dialog ──────────────────────────────
    if (showQrDialog) {
        QrCodeDialog(
            resolver = resolver,
            onDismiss = { showQrDialog = false }
        )
    }
}

// ──────────────────────────────────────────────────────────────────
//  Permission Check Item Row
// ──────────────────────────────────────────────────────────────────

@Composable
private fun PermissionCheckItem(
    title: String,
    subtitle: String,
    isGranted: Boolean,
    onFix: () -> Unit
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
            Icon(
                imageVector = if (isGranted) Icons.Default.CheckCircle else Icons.Default.ErrorOutline,
                contentDescription = null,
                tint = if (isGranted) LanGreen else CoralDecline,
                modifier = Modifier.size(24.dp)
            )
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

        if (!isGranted) {
            Button(
                onClick = onFix,
                shape = RoundedCornerShape(8.dp),
                colors = ButtonDefaults.buttonColors(containerColor = CoralDecline),
                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 6.dp)
            ) {
                Text("Herstel", color = Color.White, style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

// ──────────────────────────────────────────────────────────────────
//  Timezone Formatting Helper (Europe/Amsterdam)
// ──────────────────────────────────────────────────────────────────

private fun formatAmsterdamTime(isoTimestamp: String): String {
    return try {
        val clean = isoTimestamp.replace("Z", "")
        val parser = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US).apply {
            timeZone = java.util.TimeZone.getTimeZone("UTC")
        }
        val date = parser.parse(clean.take(19)) ?: return isoTimestamp

        val amsterdamTz = java.util.TimeZone.getTimeZone("Europe/Amsterdam")
        val now = java.util.Calendar.getInstance(amsterdamTz)
        val itemCal = java.util.Calendar.getInstance(amsterdamTz).apply { time = date }

        val timeFormat = java.text.SimpleDateFormat("HH:mm", java.util.Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        val timeStr = timeFormat.format(date)

        val isToday = now.get(java.util.Calendar.YEAR) == itemCal.get(java.util.Calendar.YEAR) &&
                now.get(java.util.Calendar.DAY_OF_YEAR) == itemCal.get(java.util.Calendar.DAY_OF_YEAR)
        if (isToday) return "Vandaag om $timeStr"

        val yesterdayCal = (now.clone() as java.util.Calendar).apply { add(java.util.Calendar.DAY_OF_YEAR, -1) }
        val isYesterday = yesterdayCal.get(java.util.Calendar.YEAR) == itemCal.get(java.util.Calendar.YEAR) &&
                yesterdayCal.get(java.util.Calendar.DAY_OF_YEAR) == itemCal.get(java.util.Calendar.DAY_OF_YEAR)
        if (isYesterday) return "Gisteren om $timeStr"

        val fullFormat = java.text.SimpleDateFormat("d MMM, HH:mm", java.util.Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        fullFormat.format(date)
    } catch (_: Exception) {
        isoTimestamp.take(16).replace("T", " ")
    }
}

// ──────────────────────────────────────────────────────────────────
//  Hardware Test Dialog: Geautomatiseerde Audio Test via Poort 8000
// ──────────────────────────────────────────────────────────────────

@Composable
private fun HardwareTestDialog(
    resolver: EndpointResolver,
    onDismiss: () -> Unit
) {
    val coroutineScope = rememberCoroutineScope()
    var isTestingAudio by remember { mutableStateOf(false) }
    var testPassed by remember { mutableStateOf<Boolean?>(null) }
    var audioTestResult by remember { mutableStateOf<String?>(null) }
    var audioTestDetail by remember { mutableStateOf<String?>(null) }

    var testElapsedSec by remember { mutableFloatStateOf(0f) }

    LaunchedEffect(isTestingAudio) {
        if (isTestingAudio) {
            testElapsedSec = 0f
            val startTime = System.currentTimeMillis()
            while (isTestingAudio && testElapsedSec < 3f) {
                delay(80)
                testElapsedSec = (System.currentTimeMillis() - startTime) / 1000f
            }
        }
    }

    Dialog(
        onDismissRequest = { if (!isTestingAudio) onDismiss() },
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.94f)
                .wrapContentHeight(),
            shape = RoundedCornerShape(20.dp),
            color = SurfaceDark,
            border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
        ) {
            Column(
                modifier = Modifier.padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Icon(imageVector = Icons.Default.VolumeUp, contentDescription = null, tint = EmeraldPrimary)
                        Text(
                            text = "Deurbel Hardware & Audio Test",
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                    }
                    IconButton(
                        onClick = onDismiss,
                        enabled = !isTestingAudio
                    ) {
                        Icon(imageVector = Icons.Default.Close, contentDescription = "Sluiten", tint = TextSecondary)
                    }
                }

                Text(
                    text = "Voert een volledige akoestische loopback test uit: de backend zendt een 1000 Hz pieptoon naar de deurbel-luidspreker (192.168.2.31:8000), luistert gelijktijdig via de microfoon (RTSP) en berekent de frequentiepiek via FFT en de signaal-ruisverhouding (SNR).",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary
                )

                HorizontalDivider(color = DividerDark)

                // Status indicator
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text(
                            text = "Akoestische loopback verificatie:",
                            style = MaterialTheme.typography.labelMedium,
                            color = TextPrimary
                        )
                        Text(
                            text = if (isTestingAudio) "Bezig met loopback analyse…" else if (testPassed == true) "Geslaagd" else if (testPassed == false) "Mislukt" else "Gereed",
                            style = MaterialTheme.typography.labelSmall,
                            color = if (isTestingAudio) CyanAccent else if (testPassed == true) LanGreen else if (testPassed == false) CoralDecline else TextSecondary
                        )
                    }
                    LinearProgressIndicator(
                        progress = if (isTestingAudio) (testElapsedSec / 4f).coerceIn(0f, 1f) else if (testPassed == true) 1f else 0f,
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(10.dp)
                            .clip(RoundedCornerShape(5.dp)),
                        color = if (testPassed == true) LanGreen else CyanAccent,
                        trackColor = SurfaceVariantDark,
                    )
                }

                // Resultaat banner
                audioTestResult?.let { res ->
                    Surface(
                        color = if (testPassed == true) Color(0xFF064E3B) else Color(0xFF7F1D1D),
                        shape = RoundedCornerShape(12.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(
                            modifier = Modifier.padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(4.dp)
                        ) {
                            Text(
                                text = res,
                                style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Bold),
                                color = Color.White
                            )
                            audioTestDetail?.let { det ->
                                Text(
                                    text = det,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Color.White.copy(alpha = 0.85f)
                                )
                            }
                        }
                    }
                }

                // Start Knop
                Button(
                    onClick = {
                        coroutineScope.launch {
                            try {
                                isTestingAudio = true
                                testPassed = null
                                audioTestResult = "Bezig met akoestische loopback test…"
                                audioTestDetail = "Toon wordt verzonden naar 192.168.2.31:8000 en microfoon-audio wordt geanalyseerd..."
                                val resolved = resolver.resolve()
                                val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                                val resp = api.testAudioLoopback()
                                if (resp.isSuccessful && resp.body() != null) {
                                    val b = resp.body()!!
                                    if (b.passed) {
                                        testPassed = true
                                        audioTestResult = "✅ Akoestische Loopback Geslaagd!"
                                        audioTestDetail = "• Luidspreker: Succesvol verzonden\n• Microfoon loopback: Waargenomen!\n• Frequentiepiek: ${b.peakHz} Hz (doel: 950-1050 Hz)\n• Signaal-ruisverhouding: ${b.snrDb} dB"
                                    } else {
                                        testPassed = false
                                        audioTestResult = "⚠️ Geen akoestische piek waargenomen"
                                        audioTestDetail = "• Luidspreker: ${if (b.speakerSent) "Verzonden" else "Mislukt"}\n• Microfoon: ${b.message}\n• Piek: ${b.peakHz} Hz (${b.snrDb} dB)"
                                    }
                                } else {
                                    testPassed = false
                                    audioTestResult = "❌ Fout: HTTP ${resp.code()}"
                                    audioTestDetail = "Backend endpoint gaf een foutmelding terug."
                                }
                            } catch (e: Exception) {
                                testPassed = false
                                audioTestResult = "❌ Test mislukt: ${e.localizedMessage}"
                                audioTestDetail = "Fout tijdens communicatie met backend."
                            } finally {
                                isTestingAudio = false
                            }
                        }
                    },
                    enabled = !isTestingAudio,
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                    modifier = Modifier
                        .fillMaxWidth()
                        .defaultMinSize(minHeight = 52.dp)
                ) {
                    if (isTestingAudio) {
                        CircularProgressIndicator(color = BackgroundDark, modifier = Modifier.size(20.dp))
                        Spacer(modifier = Modifier.width(10.dp))
                        Text("Verzenden… ${(3f - testElapsedSec).coerceAtLeast(0f).toInt()}s", color = BackgroundDark, fontWeight = FontWeight.Bold)
                    } else {
                        Icon(imageVector = Icons.Default.VolumeUp, contentDescription = null, tint = BackgroundDark)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Speel 1000 Hz Test-Pieptoon (Speaker)",
                            color = BackgroundDark,
                            fontWeight = FontWeight.Bold,
                            style = MaterialTheme.typography.bodyMedium
                        )
                    }
                }
            }
        }
    }
}

// ──────────────────────────────────────────────────────────────────
//  30-Day Log Viewer Dialog (Requirement #8)
// ──────────────────────────────────────────────────────────────────

@Composable
private fun LogViewerDialog(
    appLogger: AppLogger,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val allLogs by appLogger.logsFlow.collectAsState()

    var searchQuery by remember { mutableStateOf("") }
    var selectedCategory by remember { mutableStateOf<LogCategory?>(null) }

    val filteredLogs = remember(allLogs, searchQuery, selectedCategory) {
        allLogs.filter { entry ->
            val matchesCategory = selectedCategory == null || entry.category == selectedCategory
            val matchesSearch = searchQuery.isBlank() ||
                    entry.message.contains(searchQuery, ignoreCase = true) ||
                    (entry.details?.contains(searchQuery, ignoreCase = true) == true)
            matchesCategory && matchesSearch
        }
    }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.95f)
                .fillMaxHeight(0.88f),
            shape = RoundedCornerShape(20.dp),
            color = SurfaceDark,
            border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                // Header
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text(
                            text = "Logboek (30 dagen)",
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                        Text(
                            text = "${filteredLogs.size} gebeurtenissen opgeslagen",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                    }
                    IconButton(onClick = onDismiss) {
                        Icon(imageVector = Icons.Default.Close, contentDescription = "Sluiten", tint = TextSecondary)
                    }
                }

                // Search Bar
                OutlinedTextField(
                    value = searchQuery,
                    onValueChange = { searchQuery = it },
                    placeholder = { Text("Zoek in logs…", fontSize = 14.sp) },
                    leadingIcon = { Icon(Icons.Default.Search, contentDescription = null, tint = TextSecondary, modifier = Modifier.size(18.dp)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = EmeraldPrimary,
                        unfocusedBorderColor = DividerDark
                    )
                )

                // Category Filter Chips
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    FilterChip(
                        selected = selectedCategory == null,
                        onClick = { selectedCategory = null },
                        label = { Text("Alles") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = EmeraldPrimary,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.CALL,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.CALL) null else LogCategory.CALL },
                        label = { Text("Oproepen") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = LanGreen,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.NETWORK,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.NETWORK) null else LogCategory.NETWORK },
                        label = { Text("Netwerk") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = CyanAccent,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.ERROR,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.ERROR) null else LogCategory.ERROR },
                        label = { Text("Errors") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = CoralDecline,
                            selectedLabelColor = Color.White
                        )
                    )
                }

                // Log entries list
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .background(BackgroundDark)
                        .padding(8.dp)
                ) {
                    if (filteredLogs.isEmpty()) {
                        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text("Geen gebeurtenissen gevonden", color = TextSecondary, fontSize = 13.sp)
                        }
                    } else {
                        LazyColumn(
                            modifier = Modifier.fillMaxSize(),
                            verticalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            items(filteredLogs, key = { it.id }) { log ->
                                LogItemRow(log)
                            }
                        }
                    }
                }

                // Action buttons: Copy / Share, Clear
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    OutlinedButton(
                        onClick = {
                            val exported = appLogger.exportAsText()
                            val shareIntent = Intent(Intent.ACTION_SEND).apply {
                                type = "text/plain"
                                putExtra(Intent.EXTRA_SUBJECT, "Foxway Deurbel Logs")
                                putExtra(Intent.EXTRA_TEXT, exported)
                            }
                            context.startActivity(Intent.createChooser(shareIntent, "Logs delen"))
                        },
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.weight(1f),
                        border = androidx.compose.foundation.BorderStroke(1.dp, DividerDark)
                    ) {
                        Icon(imageVector = Icons.Default.Share, contentDescription = null, tint = TextPrimary, modifier = Modifier.size(16.dp))
                        Spacer(modifier = Modifier.width(6.dp))
                        Text("Delen / Exporteer", color = TextPrimary)
                    }

                    OutlinedButton(
                        onClick = {
                            appLogger.clear()
                            Toast.makeText(context, "Logboek gewist", Toast.LENGTH_SHORT).show()
                        },
                        shape = RoundedCornerShape(10.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CoralDecline),
                        border = androidx.compose.foundation.BorderStroke(1.dp, CoralDecline.copy(alpha = 0.5f))
                    ) {
                        Icon(imageVector = Icons.Default.Delete, contentDescription = null, tint = CoralDecline, modifier = Modifier.size(16.dp))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("Wissen", color = CoralDecline)
                    }
                }
            }
        }
    }
}

@Composable
private fun LogItemRow(entry: LogEntry) {
    val categoryColor = when (entry.category) {
        LogCategory.CALL -> LanGreen
        LogCategory.NETWORK -> CyanAccent
        LogCategory.ERROR -> CoralDecline
        LogCategory.INFO -> TextSecondary
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(SurfaceDark.copy(alpha = 0.7f))
            .padding(10.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = entry.formattedTime(),
                style = MaterialTheme.typography.labelSmall,
                color = TextSecondary
            )
            Text(
                text = entry.category.name,
                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                color = categoryColor
            )
        }
        Text(
            text = entry.message,
            style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
            color = TextPrimary
        )
        if (!entry.details.isNullOrBlank()) {
            Text(
                text = entry.details,
                style = MaterialTheme.typography.labelSmall,
                color = TextSecondary
            )
        }
    }
}

@Composable
private fun EndpointRow(label: String, value: String) {
    Column {
        Text(text = label, style = MaterialTheme.typography.labelSmall, color = TextSecondary)
        Text(text = value, style = MaterialTheme.typography.bodyMedium, color = TextPrimary)
    }
}

// ──────────────────────────────────────────────────────────────────
//  Koppel QR-Code Dialog (Requirement #3)
// ──────────────────────────────────────────────────────────────────

@Composable
private fun QrCodeDialog(
    resolver: EndpointResolver,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    var isLoading by remember { mutableStateOf(true) }
    var qrBitmap by remember { mutableStateOf<android.graphics.Bitmap?>(null) }
    var qrPayloadJson by remember { mutableStateOf("") }
    var errorMsg by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        try {
            val resolved = resolver.resolve()
            val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
            val resp = api.getOnboardingQr()
            if (resp.isSuccessful && resp.body() != null) {
                val body = resp.body()!!
                qrPayloadJson = body.payloadJson
                val rawB64 = body.qrImageUrl.substringAfter("base64,")
                val decodedBytes = android.util.Base64.decode(rawB64, android.util.Base64.DEFAULT)
                qrBitmap = android.graphics.BitmapFactory.decodeByteArray(decodedBytes, 0, decodedBytes.size)
            } else {
                errorMsg = "Fout bij ophalen QR: HTTP ${resp.code()}"
            }
        } catch (e: Exception) {
            errorMsg = "Kon QR niet laden: ${e.localizedMessage}"
        } finally {
            isLoading = false
        }
    }

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
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("Koppel QR-Code", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold, color = TextPrimary)
                    IconButton(onClick = onDismiss) {
                        Icon(imageVector = Icons.Default.Close, contentDescription = "Sluiten", tint = TextSecondary)
                    }
                }
                Text(
                    text = "Laat een gezinslid deze QR-code scannen in de Foxway app om direct te koppelen.",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary,
                    textAlign = TextAlign.Center
                )

                if (isLoading) {
                    CircularProgressIndicator(color = EmeraldPrimary, modifier = Modifier.size(48.dp))
                } else if (errorMsg != null) {
                    Text(text = errorMsg ?: "", color = CoralDecline, style = MaterialTheme.typography.bodyMedium)
                } else if (qrBitmap != null) {
                    Image(
                        bitmap = qrBitmap!!.asImageBitmap(),
                        contentDescription = "QR Code",
                        modifier = Modifier
                            .size(220.dp)
                            .background(Color.White, RoundedCornerShape(12.dp))
                            .padding(10.dp)
                    )
                    OutlinedButton(
                        onClick = {
                            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                            clipboard.setPrimaryClip(ClipData.newPlainText("Foxway QR Payload", qrPayloadJson))
                            Toast.makeText(context, "Koppelcode gekopieerd!", Toast.LENGTH_SHORT).show()
                        },
                        modifier = Modifier
                            .fillMaxWidth()
                            .defaultMinSize(minHeight = 44.dp),
                        shape = RoundedCornerShape(10.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CyanAccent),
                        border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.5f))
                    ) {
                        Icon(imageVector = Icons.Default.ContentCopy, contentDescription = null, modifier = Modifier.size(16.dp))
                        Spacer(modifier = Modifier.width(6.dp))
                        Text("Kopieer Koppelcode", fontWeight = FontWeight.SemiBold)
                    }
                }
            }
        }
    }
}
