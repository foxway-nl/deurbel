package nl.foxway.deurbel.ui.settings

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.PullToRefreshDefaults
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
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
import nl.foxway.deurbel.BuildConfig
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.AppLogger
import nl.foxway.deurbel.data.LogCategory
import nl.foxway.deurbel.data.LogEntry
import nl.foxway.deurbel.data.models.*
import coil.compose.AsyncImage
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.ConnectionManager
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
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun SettingsScreen(
    onUnpair: () -> Unit,
    openPermissions: Boolean = false,
    onPermissionsOpened: (() -> Unit)? = null
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val resolver = remember { EndpointResolver(prefs) }
    val connectionManager = remember { ConnectionManager.getInstance(context) }
    val activeBaseUrl by connectionManager.activeBaseUrl.collectAsState()
    val isWanMode by connectionManager.isWanMode.collectAsState()
    val appLogger = remember { AppLogger.getInstance(context) }

    val scrollState = rememberScrollState()
    val bringIntoViewRequester = remember { BringIntoViewRequester() }
    var showPermissionsDetail by remember { mutableStateOf(openPermissions) }

    LaunchedEffect(openPermissions) {
        if (openPermissions) {
            showPermissionsDetail = true
            delay(150)
            try {
                bringIntoViewRequester.bringIntoView()
            } catch (_: Exception) {
                scrollState.animateScrollTo(scrollState.maxValue)
            }
            onPermissionsOpened?.invoke()
        }
    }

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
    var currentApiBaseUrl by remember { mutableStateOf(connectionManager.getEffectiveBaseUrl()) }
    var isCleaningJunk by remember { mutableStateOf(false) }

    LaunchedEffect(activeBaseUrl, isWanMode) {
        currentApiBaseUrl = connectionManager.getEffectiveBaseUrl()
    }

    // Dialogs
    var showHardwareTestDialog by remember { mutableStateOf(false) }
    var showLogsDialog by remember { mutableStateOf(false) }
    var isTriggeringShelly by remember { mutableStateOf(false) }
    var shellyTriggerResult by remember { mutableStateOf<String?>(null) }

    val modernSwitchColors = SwitchDefaults.colors(
        checkedThumbColor = EmeraldPrimary,
        checkedTrackColor = EmeraldPrimary.copy(alpha = 0.5f),
        uncheckedThumbColor = Color(0xFFB0BEC5),
        uncheckedTrackColor = Color(0xFF37474F),
        uncheckedBorderColor = Color(0xFF546E7A)
    )

    val saveSettingsToPrefs: (DetectionSettingsDto) -> Unit = { det ->
        context.getSharedPreferences("foxway_settings", Context.MODE_PRIVATE).edit()
            .putBoolean("detection_enabled", det.detectionEnabled)
            .putBoolean("record_person", det.recordPerson)
            .putBoolean("record_vehicle", det.recordVehicle)
            .putBoolean("record_animal", det.recordAnimal)
            .putBoolean("record_motion", det.recordMotion)
            .putBoolean("notify_person", det.notifyPerson)
            .putBoolean("notify_vehicle", det.notifyVehicle)
            .putBoolean("notify_animal", det.notifyAnimal)
            .putBoolean("notify_motion", det.notifyMotion)
            .putBoolean("quiet_hours_enabled", det.quietHoursEnabled)
            .putString("quiet_hours_start", det.quietHoursStart)
            .putString("quiet_hours_end", det.quietHoursEnd)
            .apply()
    }

    val loadRemoteSettings = {
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi()
                val det = api.getDetectionSettings()
                if (det.isSuccessful) {
                    det.body()?.let {
                        detectionSettings = it
                        saveSettingsToPrefs(it)
                    }
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
        saveSettingsToPrefs(updated)
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi()
                api.updateDetectionSettings(updated)
            } catch (_: Exception) {}
        }
    }

    val reportStatusDump: () -> Unit = {
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi()
                val deviceMap = mapOf<String, Any>(
                    "brand" to android.os.Build.BRAND,
                    "model" to android.os.Build.MODEL,
                    "manufacturer" to android.os.Build.MANUFACTURER,
                    "android_sdk" to android.os.Build.VERSION.SDK_INT,
                    "app_version" to "v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
                    "base_url" to activeBaseUrl,
                    "is_wan_mode" to isWanMode
                )
                val logsList = appLogger.logsFlow.value
                val logsText = logsList.take(40).joinToString("\n") { entry ->
                    "[${entry.formattedTime()}] [${entry.category.name}] ${entry.message}${if (!entry.details.isNullOrBlank()) " (${entry.details})" else ""}"
                }
                val payload = mapOf<String, Any>(
                    "device" to deviceMap,
                    "screen_state" to "SettingsScreen",
                    "client_logs" to logsText
                )
                val resp = api.postClientStatusDump(payload)
                if (resp.isSuccessful) {
                    Toast.makeText(context, "✅ Status-dump succesvol gerapporteerd!", Toast.LENGTH_LONG).show()
                } else {
                    Toast.makeText(context, "Status-dump fout: HTTP ${resp.code()}", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Toast.makeText(context, "Verzendfout: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    val saveRetention: (Int) -> Unit = { days ->
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi()
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
                val api = ApiClientFactory.createApi()
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
                val api = ApiClientFactory.createApi()
                api.deleteUser(targetUserId)
                Toast.makeText(context, "Apparaat verwijderd", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                refreshUsers()
            }
        }
    }

    var isRefreshing by remember { mutableStateOf(false) }
    val pullRefreshState = rememberPullToRefreshState()

    val refreshAll: () -> Unit = {
        coroutineScope.launch {
            isRefreshing = true
            try {
                permState = PermissionChecker.checkAll(context)
                endpointConfig = prefs.getEndpointConfig()
                val jobUsers = launch {
                    try {
                        val api = ApiClientFactory.createApi()
                        val resp = api.getUsers()
                        if (resp.isSuccessful) {
                            activeUsers = resp.body() ?: emptyList()
                        }
                    } catch (_: Exception) {}
                }
                val jobSettings = launch {
                    try {
                        val api = ApiClientFactory.createApi()
                        val det = api.getDetectionSettings()
                        if (det.isSuccessful) {
                            det.body()?.let {
                                detectionSettings = it
                                saveSettingsToPrefs(it)
                            }
                        }
                        val stor = api.getStorageSettings()
                        if (stor.isSuccessful) {
                            stor.body()?.let { storageStats = it }
                        }
                    } catch (_: Exception) {}
                }
                jobUsers.join()
                jobSettings.join()
            } finally {
                isRefreshing = false
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
                    IconButton(onClick = { showLogsDialog = true }) {
                        Icon(imageVector = Icons.Default.Info, contentDescription = "Logboek", tint = CyanAccent)
                    }
                }
            )
        },
        containerColor = BackgroundDark
    ) { padding ->
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = { refreshAll() },
            state = pullRefreshState,
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
            indicator = {
                PullToRefreshDefaults.Indicator(
                    state = pullRefreshState,
                    isRefreshing = isRefreshing,
                    modifier = Modifier.align(Alignment.TopCenter),
                    containerColor = SurfaceDark,
                    color = EmeraldPrimary
                )
            }
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(scrollState)
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(20.dp)
            ) {
            // ── KAART 1: Profiel & Gekoppelde Apparaten ──────────────
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
                    // Current User Profile Header
                    Row(
                        modifier = Modifier.fillMaxWidth(),
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

                        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
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
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(6.dp)
                            ) {
                                Box(modifier = Modifier.size(8.dp).background(LanGreen, CircleShape))
                                Text(
                                    text = "Dit toestel (${android.os.Build.MODEL}) • Online",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = EmeraldLight
                                )
                            }
                        }
                    }

                    HorizontalDivider(color = DividerDark)

                    // Active Users & Connected Family Members List
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            "Gekoppelde Toestellen & Gezinsleden",
                            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.SemiBold),
                            color = CyanAccent
                        )
                        if (isLoadingUsers) {
                            CircularProgressIndicator(color = EmeraldPrimary, modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        }
                    }

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
                }
            }

            // ── KAART 2: AI-Detectie & Meldingen ─────────────────────
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
                    // Header / Master AI Switch
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column(modifier = Modifier.weight(1f).padding(end = 12.dp)) {
                            Text("Slimme AI Detectie", fontWeight = FontWeight.Bold, color = TextPrimary)
                            Text("Arlo-stijl objectherkenning & classificatie", style = MaterialTheme.typography.bodySmall, color = TextSecondary)
                        }
                        Switch(
                            checked = detectionSettings.detectionEnabled,
                            onCheckedChange = { saveDetection(detectionSettings.copy(detectionEnabled = it)) },
                            colors = modernSwitchColors
                        )
                    }

                    HorizontalDivider(color = DividerDark)

                    // 1. Mensen (Opnemen & Melding)
                    CompactDetectionRow(
                        icon = Icons.Default.Person,
                        iconTint = EmeraldPrimary,
                        title = "Mensen",
                        description = "Persoon voor de deur",
                        recordChecked = detectionSettings.recordPerson,
                        onRecordChange = { saveDetection(detectionSettings.copy(recordPerson = it)) },
                        notifyChecked = detectionSettings.notifyPerson,
                        onNotifyChange = { saveDetection(detectionSettings.copy(notifyPerson = it)) },
                        enabled = detectionSettings.detectionEnabled,
                        switchColors = modernSwitchColors
                    )

                    HorizontalDivider(color = DividerDark)

                    // 2. Voertuigen (Opnemen & Melding)
                    CompactDetectionRow(
                        icon = Icons.Default.DirectionsCar,
                        iconTint = CyanAccent,
                        title = "Voertuigen",
                        description = "Rijdende auto's, bezorgbussen",
                        recordChecked = detectionSettings.recordVehicle,
                        onRecordChange = { saveDetection(detectionSettings.copy(recordVehicle = it)) },
                        notifyChecked = detectionSettings.notifyVehicle,
                        onNotifyChange = { saveDetection(detectionSettings.copy(notifyVehicle = it)) },
                        enabled = detectionSettings.detectionEnabled,
                        switchColors = modernSwitchColors
                    )

                    HorizontalDivider(color = DividerDark)

                    // 3. Dieren (Opnemen & Melding)
                    CompactDetectionRow(
                        icon = Icons.Default.Pets,
                        iconTint = Color(0xFFF59E0B),
                        title = "Dieren",
                        description = "Hond, kat, vogel",
                        recordChecked = detectionSettings.recordAnimal,
                        onRecordChange = { saveDetection(detectionSettings.copy(recordAnimal = it)) },
                        notifyChecked = detectionSettings.notifyAnimal,
                        onNotifyChange = { saveDetection(detectionSettings.copy(notifyAnimal = it)) },
                        enabled = detectionSettings.detectionEnabled,
                        switchColors = modernSwitchColors
                    )

                    HorizontalDivider(color = DividerDark)

                    // 4. Stille Uren / Nachtmodus
                    Column(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
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
                                Text(
                                    "Muteert pushmeldingen tussen ${detectionSettings.quietHoursStart} en ${detectionSettings.quietHoursEnd} (opnames blijven actief)",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = TextSecondary
                                )
                            }
                            Switch(
                                checked = detectionSettings.quietHoursEnabled,
                                onCheckedChange = { saveDetection(detectionSettings.copy(quietHoursEnabled = it)) },
                                colors = modernSwitchColors
                            )
                        }

                        if (detectionSettings.quietHoursEnabled) {
                            Spacer(modifier = Modifier.height(10.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                OutlinedTextField(
                                    value = detectionSettings.quietHoursStart,
                                    onValueChange = { newVal ->
                                        if (newVal.length <= 5) {
                                            saveDetection(detectionSettings.copy(quietHoursStart = newVal))
                                        }
                                    },
                                    label = { Text("Begintijd", color = TextSecondary, fontSize = 11.sp) },
                                    singleLine = true,
                                    modifier = Modifier.weight(1f),
                                    textStyle = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary),
                                    colors = OutlinedTextFieldDefaults.colors(
                                        focusedBorderColor = CyanAccent,
                                        unfocusedBorderColor = DividerDark,
                                        focusedContainerColor = SurfaceDark,
                                        unfocusedContainerColor = SurfaceDark
                                    )
                                )

                                OutlinedTextField(
                                    value = detectionSettings.quietHoursEnd,
                                    onValueChange = { newVal ->
                                        if (newVal.length <= 5) {
                                            saveDetection(detectionSettings.copy(quietHoursEnd = newVal))
                                        }
                                    },
                                    label = { Text("Eindtijd", color = TextSecondary, fontSize = 11.sp) },
                                    singleLine = true,
                                    modifier = Modifier.weight(1f),
                                    textStyle = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary),
                                    colors = OutlinedTextFieldDefaults.colors(
                                        focusedBorderColor = CyanAccent,
                                        unfocusedBorderColor = DividerDark,
                                        focusedContainerColor = SurfaceDark,
                                        unfocusedContainerColor = SurfaceDark
                                    )
                                )
                            }
                        }
                    }
                }
            }

            // ── KAART 3: Opslag & Bewaarduur ─────────────────────────
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
                            Text("Opslaggebruik", fontWeight = FontWeight.Bold, color = TextPrimary)
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
                                    val api = ApiClientFactory.createApi()
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
                            Text("🧹 Wis oude bewegingsopnames", fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }

            // ── KAART 4: Systeem & Diagnose ──────────────────────────
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
                    Text("Systeem & Diagnose", fontWeight = FontWeight.Bold, color = TextPrimary)

                    // 1. Android Permissies & Betrouwbaarheid status
                    val allPermsOk = permState.allGranted
                    Surface(
                        color = SurfaceVariantDark,
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .bringIntoViewRequester(bringIntoViewRequester)
                            .clickable { showPermissionsDetail = !showPermissionsDetail }
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Row(
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                                ) {
                                    Box(
                                        modifier = Modifier.size(10.dp).background(
                                            if (allPermsOk) LanGreen else Color(0xFFF59E0B),
                                            CircleShape
                                        )
                                    )
                                    Text(
                                        text = if (allPermsOk) "Android Permissies: Optimaal (5/5)" else "Android Permissies: Aandacht vereist",
                                        style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                                        color = TextPrimary
                                    )
                                }
                                Icon(
                                    imageVector = if (showPermissionsDetail) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                                    contentDescription = null,
                                    tint = TextSecondary
                                )
                            }

                            if (showPermissionsDetail) {
                                Spacer(modifier = Modifier.height(10.dp))
                                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    PermissionCheckItem(
                                        title = "Meldingen (POST_NOTIFICATIONS)",
                                        subtitle = "Noodzakelijk voor oproepen",
                                        isGranted = permState.hasNotificationPermission,
                                        onFix = { PermissionChecker.openNotificationSettings(context) }
                                    )
                                    HorizontalDivider(color = DividerDark)
                                    PermissionCheckItem(
                                        title = "Microfoon (RECORD_AUDIO)",
                                        subtitle = "Voor twee-weg audio praten",
                                        isGranted = permState.hasRecordAudioPermission,
                                        onFix = { PermissionChecker.openAppDetails(context) }
                                    )
                                    HorizontalDivider(color = DividerDark)
                                    PermissionCheckItem(
                                        title = "Batterijbeheer: Niet beperkt",
                                        subtitle = "Houdt achtergrondservice actief",
                                        isGranted = permState.isBatteryOptimizationIgnored,
                                        onFix = { PermissionChecker.openBatteryOptimizationSettings(context) }
                                    )
                                    HorizontalDivider(color = DividerDark)
                                    PermissionCheckItem(
                                        title = "Bovenop weergeven (Appear on top)",
                                        subtitle = "Opent oproepscherm direct",
                                        isGranted = permState.canDrawOverlays,
                                        onFix = { PermissionChecker.openOverlaySettings(context) }
                                    )
                                    HorizontalDivider(color = DividerDark)
                                    PermissionCheckItem(
                                        title = "Volledig scherm meldingen",
                                        subtitle = "Rinkelen bij vergrendeld scherm",
                                        isGranted = permState.canUseFullScreenIntent,
                                        onFix = { PermissionChecker.openFullScreenIntentSettings(context) }
                                    )
                                }
                            }
                        }
                    }

                    HorizontalDivider(color = DividerDark)

                    // Knop 1: Start Deurbel Hardware Test
                    Button(
                        onClick = { showHardwareTestDialog = true },
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = EmeraldPrimary),
                        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    ) {
                        Icon(imageVector = Icons.Default.GraphicEq, contentDescription = null, tint = BackgroundDark)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Start Deurbel Hardware Test",
                            color = BackgroundDark,
                            fontWeight = FontWeight.Bold,
                            fontSize = 14.sp,
                            maxLines = 1,
                            textAlign = TextAlign.Center
                        )
                    }

                    // Knop 2: Test Aanbel Trigger (Simuleer Drukknop) - exact zelfde styling als Knop 1
                    Button(
                        onClick = {
                            coroutineScope.launch {
                                try {
                                    isTriggeringShelly = true
                                    shellyTriggerResult = null
                                    nl.foxway.deurbel.telecom.DoorbellConnectionService.resetDeduplication()
                                    val api = ApiClientFactory.createApi()
                                    val resp = api.triggerShellyWebhook(force = true)
                                    if (resp.isSuccessful) {
                                        shellyTriggerResult = "✅ Drukknop-oproep verzonden naar backend!"
                                        val body = resp.body()
                                        val cid = (body?.get("call_id") as? String) ?: "test_${System.currentTimeMillis()}"
                                        val snap = (body?.get("snapshot_url") as? String) ?: ""
                                        nl.foxway.deurbel.telecom.DoorbellConnectionService.resetDeduplication()
                                        nl.foxway.deurbel.telecom.DoorbellConnectionService.launchCallActivity(context, cid, snap, false)
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
                        colors = ButtonDefaults.buttonColors(
                            containerColor = EmeraldPrimary,
                            disabledContainerColor = EmeraldPrimary.copy(alpha = 0.6f)
                        ),
                        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    ) {
                        if (isTriggeringShelly) {
                            CircularProgressIndicator(color = BackgroundDark, strokeWidth = 2.dp, modifier = Modifier.size(18.dp))
                        } else {
                            Icon(imageVector = Icons.Default.NotificationsActive, contentDescription = null, tint = BackgroundDark, modifier = Modifier.size(20.dp))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = "Test Aanbel Trigger (Simuleer Drukknop)",
                                color = BackgroundDark,
                                fontWeight = FontWeight.Bold,
                                fontSize = 14.sp,
                                maxLines = 1,
                                textAlign = TextAlign.Center
                            )
                        }
                    }

                    shellyTriggerResult?.let {
                        Text(text = it, style = MaterialTheme.typography.bodySmall, color = CyanAccent)
                    }

                    // Knop 3: Open Web Beheerpaneel (/admin) - met seamless 1-click auto-login!
                    var isOpeningAdmin by remember { mutableStateOf(false) }
                    OutlinedButton(
                        onClick = {
                            coroutineScope.launch {
                                try {
                                    isOpeningAdmin = true
                                    val api = ApiClientFactory.createApi()
                                    val tokenResp = api.getAdminAuthToken()
                                    val token = if (tokenResp.isSuccessful) tokenResp.body()?.get("token") else null
                                    val baseUrl = if (isWanMode) "https://deurbel.foxway.nl" else currentApiBaseUrl.trimEnd('/')
                                    val adminUrl = if (!token.isNullOrBlank()) {
                                        "$baseUrl/admin?token=$token"
                                    } else {
                                        "$baseUrl/admin"
                                    }
                                    val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(adminUrl)).apply {
                                        flags = Intent.FLAG_ACTIVITY_NEW_TASK
                                    }
                                    context.startActivity(browserIntent)
                                } catch (e: Exception) {
                                    val baseUrl = if (isWanMode) "https://deurbel.foxway.nl" else currentApiBaseUrl.trimEnd('/')
                                    try {
                                        val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse("$baseUrl/admin")).apply {
                                            flags = Intent.FLAG_ACTIVITY_NEW_TASK
                                        }
                                        context.startActivity(browserIntent)
                                    } catch (ex: Exception) {
                                        Toast.makeText(context, "Kan browser niet openen: ${ex.localizedMessage}", Toast.LENGTH_SHORT).show()
                                    }
                                } finally {
                                    isOpeningAdmin = false
                                }
                            }
                        },
                        enabled = !isOpeningAdmin,
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CyanAccent),
                        border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.5f)),
                        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    ) {
                        if (isOpeningAdmin) {
                            CircularProgressIndicator(color = CyanAccent, strokeWidth = 2.dp, modifier = Modifier.size(18.dp))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = "Inloggen & openen...",
                                color = CyanAccent,
                                fontWeight = FontWeight.SemiBold,
                                fontSize = 14.sp,
                                maxLines = 1,
                                textAlign = TextAlign.Center
                            )
                        } else {
                            Icon(imageVector = Icons.Default.AdminPanelSettings, contentDescription = null, tint = CyanAccent)
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = "Open Web Beheerpaneel (/admin)",
                                color = CyanAccent,
                                fontWeight = FontWeight.SemiBold,
                                fontSize = 14.sp,
                                maxLines = 1,
                                textAlign = TextAlign.Center
                            )
                        }
                    }

                    HorizontalDivider(color = DividerDark)

                    // Knop 4: Apparaat Ontkoppelen
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
                        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    ) {
                        Icon(imageVector = Icons.Default.LinkOff, contentDescription = null, tint = CoralDecline)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Apparaat Ontkoppelen",
                            color = CoralDecline,
                            fontWeight = FontWeight.SemiBold,
                            fontSize = 14.sp,
                            maxLines = 1,
                            textAlign = TextAlign.Center
                        )
                    }
                }
            }

            // App Versie & Updates Footer
            val appVersionText = remember {
                try {
                    val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
                    val vName = pInfo.versionName ?: BuildConfig.VERSION_NAME
                    val vCode = if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
                        pInfo.longVersionCode
                    } else {
                        @Suppress("DEPRECATION")
                        pInfo.versionCode.toLong()
                    }
                    "Foxway Deurbel v$vName (Build $vCode)"
                } catch (_: Exception) {
                    "Foxway Deurbel v${BuildConfig.VERSION_NAME} (Build ${BuildConfig.VERSION_CODE})"
                }
            }

            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 12.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                Text(
                    text = appVersionText,
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = TextPrimary
                )
                Text(
                    text = "Verbonden met: $currentApiBaseUrl",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary
                )
            }
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
            onDismiss = { showLogsDialog = false },
            onReportStatusDump = { reportStatusDump() }
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
                    text = "Voert een volledige akoestische loopback test uit: de backend zendt een 1000 Hz pieptoon naar de deurbel-luidspreker (poort 8000), luistert gelijktijdig via de microfoon (RTSP) en berekent de frequentiepiek via FFT en de signaal-ruisverhouding (SNR).",
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
                                audioTestDetail = "Toon wordt verzonden naar de deurbel-luidspreker en microfoon-audio wordt geanalyseerd..."
                                val api = ApiClientFactory.createApi()
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
    onDismiss: () -> Unit,
    onReportStatusDump: () -> Unit = {}
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
                    modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    FilterChip(
                        selected = selectedCategory == null,
                        onClick = { selectedCategory = null },
                        label = { Text("Alles", maxLines = 1, softWrap = false) },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = EmeraldPrimary,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.CALL,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.CALL) null else LogCategory.CALL },
                        label = { Text("Oproepen", maxLines = 1, softWrap = false) },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = LanGreen,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.NETWORK,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.NETWORK) null else LogCategory.NETWORK },
                        label = { Text("Netwerk", maxLines = 1, softWrap = false) },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = CyanAccent,
                            selectedLabelColor = BackgroundDark
                        )
                    )
                    FilterChip(
                        selected = selectedCategory == LogCategory.ERROR,
                        onClick = { selectedCategory = if (selectedCategory == LogCategory.ERROR) null else LogCategory.ERROR },
                        label = { Text("Errors", maxLines = 1, softWrap = false) },
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

                Button(
                    onClick = { onReportStatusDump() },
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = CyanAccent.copy(alpha = 0.2f),
                        contentColor = CyanAccent
                    ),
                    border = androidx.compose.foundation.BorderStroke(1.dp, CyanAccent)
                ) {
                    Text("📤 Rapporteer Fout / Status-Dump", fontWeight = FontWeight.Bold)
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

    val triggerBadge = when {
        entry.details?.contains("SHAZAM + SNIFF") == true || entry.message.contains("SHAZAM + SNIFF") -> "⚡ [SHAZAM + SNIFF]" to AmberWarning
        entry.details?.contains("NETWORK_SNIFF") == true || entry.message.contains("NETWORK_SNIFF") -> "🌐 [NETWORK_SNIFF]" to CyanAccent
        entry.details?.contains("SHAZAM_CHIME") == true || entry.message.contains("SHAZAM_CHIME") -> "🔔 [SHAZAM_CHIME]" to EmeraldPrimary
        else -> null
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
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                if (triggerBadge != null) {
                    Text(
                        text = triggerBadge.first,
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = triggerBadge.second
                    )
                }
                Text(
                    text = entry.category.name,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = categoryColor
                )
            }
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

@Composable
private fun CompactDetectionRow(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    iconTint: androidx.compose.ui.graphics.Color,
    title: String,
    description: String,
    recordChecked: Boolean,
    onRecordChange: (Boolean) -> Unit,
    notifyChecked: Boolean,
    onNotifyChange: (Boolean) -> Unit,
    enabled: Boolean,
    switchColors: SwitchColors
) {
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Icon(imageVector = icon, contentDescription = null, tint = iconTint, modifier = Modifier.size(20.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(title, fontWeight = FontWeight.SemiBold, color = TextPrimary)
                Text(description, style = MaterialTheme.typography.bodySmall, color = TextSecondary)
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text("Opnemen:", style = MaterialTheme.typography.labelMedium, color = TextSecondary)
                Switch(
                    checked = recordChecked,
                    onCheckedChange = onRecordChange,
                    enabled = enabled,
                    colors = switchColors,
                    modifier = Modifier.scale(0.85f)
                )
            }
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text("Melding:", style = MaterialTheme.typography.labelMedium, color = TextSecondary)
                Switch(
                    checked = notifyChecked,
                    onCheckedChange = onNotifyChange,
                    enabled = enabled && recordChecked,
                    colors = switchColors,
                    modifier = Modifier.scale(0.85f)
                )
            }
        }
    }
}


