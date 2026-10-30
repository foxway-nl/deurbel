package nl.foxway.deurbel.ui.clips

import android.app.DatePickerDialog
import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import coil.compose.AsyncImage
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.models.ClipItem
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.ConnectionManager
import nl.foxway.deurbel.network.EndpointResolver
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.media3.ui.AspectRatioFrameLayout
import kotlinx.coroutines.delay
import nl.foxway.deurbel.ui.theme.*
import java.text.SimpleDateFormat
import java.util.*

/**
 * Clips Timeline, ExoPlayer Player & DownloadManager.
 *
 * Implements:
 *  - Strict chronological sorting (newest first)
 *  - Sticky date headers: Vandaag (20 okt), Gisteren (19 okt), 18 okt 2026, etc.
 *  - Horizontal filter chips: [Alles], [🔔 Aangebeld], [🚶 Mensen], [🚗 Voertuigen], [🐾 Dieren], [⭐ Favorieten]
 *  - Date Picker filter dialog (Kalender-kiezer)
 *  - Exact time (20:26:14), duration (00:15), file size (1.8 MB), and detection badge
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ClipsScreen(
    initialClipId: String? = null,
    onClipSelected: () -> Unit = {}
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = remember { FoxwayApp.instance.preferencesManager }
    val resolver = remember { EndpointResolver(prefs) }
    val connectionManager = remember { ConnectionManager.getInstance(context) }
    val activeBaseUrl by connectionManager.activeBaseUrl.collectAsState()
    val isWanMode by connectionManager.isWanMode.collectAsState()

    var clips by remember { mutableStateOf<List<ClipItem>>(emptyList()) }
    var isLoading by remember { mutableStateOf(true) }
    var selectedClip by remember { mutableStateOf<ClipItem?>(null) }
    var baseUrl by remember { mutableStateOf(connectionManager.getEffectiveBaseUrl()) }

    LaunchedEffect(activeBaseUrl, isWanMode) {
        baseUrl = connectionManager.getEffectiveBaseUrl()
    }

    // Auto-select initialClipId if passed (Deeplinking from notification)
    LaunchedEffect(initialClipId) {
        if (!initialClipId.isNullOrBlank()) {
            val targetId = initialClipId
            onClipSelected() // Clear pending deeplink parameter immediately
            val found = clips.firstOrNull { it.id == targetId }
            if (found != null) {
                selectedClip = found
            } else if (baseUrl.isNotBlank()) {
                // Query server immediately for 2-phase lifecycle status
                try {
                    val api = ApiClientFactory.createApi(baseUrl)
                    val resp = api.getClipDetail(targetId)
                    if (resp.isSuccessful && resp.body() != null) {
                        selectedClip = resp.body()
                    } else {
                        selectedClip = ClipItem(
                            id = targetId,
                            timestamp = java.time.Instant.now().toString(),
                            duration = 0.0,
                            sizeBytes = 0L,
                            snapshotUrl = "/api/v1/clips/$targetId/thumbnail",
                            videoUrl = "/api/v1/clips/$targetId/stream",
                            answeredBy = "Opname bekijken",
                            starred = false,
                            status = "RECORDING",
                            thumbnailUrl = "/api/v1/clips/$targetId/thumbnail"
                        )
                    }
                } catch (_: Exception) {
                    selectedClip = ClipItem(
                        id = targetId,
                        timestamp = java.time.Instant.now().toString(),
                        duration = 0.0,
                        sizeBytes = 0L,
                        snapshotUrl = "/api/v1/clips/$targetId/thumbnail",
                        videoUrl = "/api/v1/clips/$targetId/stream",
                        answeredBy = "Opname bekijken",
                        starred = false,
                        status = "RECORDING",
                        thumbnailUrl = "/api/v1/clips/$targetId/thumbnail"
                    )
                }
            }
        }
    }

    // Filtering states
    var selectedCategory by remember { mutableStateOf("Alles") }
    var selectedDateYmd by remember { mutableStateOf<String?>(null) } // "yyyy-MM-dd"

    val categories = listOf("Alles", "🔔 Aangebeld", "🚶 Mensen", "🚗 Voertuigen", "🐾 Dieren", "⭐ Favorieten")

    // Load clips
    val loadClips = {
        coroutineScope.launch {
            try {
                isLoading = true
                val currentBase = connectionManager.getEffectiveBaseUrl()
                baseUrl = currentBase
                val api = ApiClientFactory.createApi(currentBase)
                val resp = api.getClips(limit = 50)
                if (resp.isSuccessful) {
                    clips = (resp.body() ?: emptyList()).sortedByDescending { it.timestamp }
                }
            } catch (_: Exception) {
            } finally {
                isLoading = false
            }
        }
    }

    val deleteClip: (String) -> Unit = { clipId ->
        coroutineScope.launch {
            try {
                // Optimistic UI removal
                clips = clips.filter { it.id != clipId }
                val api = ApiClientFactory.createApi(baseUrl)
                api.deleteClip(clipId)
                nl.foxway.deurbel.data.AppLogger.getInstance(context).info("Opname verwijderd: $clipId")
                Toast.makeText(context, "Opname verwijderd", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                loadClips()
            }
        }
    }

    val toggleStar: (String, Boolean) -> Unit = { clipId, currentStarred ->
        val newStarred = !currentStarred
        clips = clips.map { if (it.id == clipId) it.copy(starred = newStarred) else it }
        coroutineScope.launch {
            try {
                val api = ApiClientFactory.createApi(baseUrl)
                api.starClip(clipId, mapOf("starred" to newStarred))
                Toast.makeText(
                    context,
                    if (newStarred) "⭐ Opname vergrendeld (blijft bewaard)" else "Opname ontgrendeld",
                    Toast.LENGTH_SHORT
                ).show()
            } catch (e: Exception) {
                loadClips()
            }
        }
    }

    LaunchedEffect(Unit) {
        loadClips()
    }

    // Filter clips
    val filteredClips = remember(clips, selectedCategory, selectedDateYmd) {
        clips.filter { clip ->
            // Category filter
            val matchesCategory = when (selectedCategory) {
                "Alles" -> true
                "🔔 Aangebeld" -> {
                    val ans = clip.answeredBy ?: ""
                    ans.contains("Aanbel", ignoreCase = true) || clip.id.contains("ring", ignoreCase = true) || clip.id.contains("call", ignoreCase = true) || (!ans.contains("AI:") && !ans.contains("Handmatige"))
                }
                "🚶 Mensen" -> {
                    val ans = clip.answeredBy ?: ""
                    ans.contains("Persoon", ignoreCase = true) || clip.id.contains("person", ignoreCase = true)
                }
                "🚗 Voertuigen" -> {
                    val ans = clip.answeredBy ?: ""
                    ans.contains("Voertuig", ignoreCase = true) || ans.contains("Auto", ignoreCase = true) || clip.id.contains("vehicle", ignoreCase = true)
                }
                "🐾 Dieren" -> {
                    val ans = clip.answeredBy ?: ""
                    ans.contains("Dier", ignoreCase = true) || ans.contains("Hond", ignoreCase = true) || ans.contains("Kat", ignoreCase = true) || clip.id.contains("animal", ignoreCase = true)
                }
                "⭐ Favorieten" -> clip.starred
                else -> true
            }

            // Date filter
            val matchesDate = if (selectedDateYmd == null) {
                true
            } else {
                extractDateYmd(clip.timestamp) == selectedDateYmd
            }

            matchesCategory && matchesDate
        }
    }

    // Group filtered clips by Dutch date header
    val groupedClips = remember(filteredClips) {
        filteredClips.groupBy { formatDateHeader(it.timestamp) }
    }

    // Date picker dialog launcher
    val showDatePicker = {
        val cal = Calendar.getInstance()
        DatePickerDialog(
            context,
            { _, year, month, dayOfMonth ->
                val pickedYmd = String.format(Locale.US, "%04d-%02d-%02d", year, month + 1, dayOfMonth)
                selectedDateYmd = pickedYmd
            },
            cal.get(Calendar.YEAR),
            cal.get(Calendar.MONTH),
            cal.get(Calendar.DAY_OF_MONTH)
        ).show()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Opnames & Bezoekers", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = BackgroundDark,
                    titleContentColor = TextPrimary
                ),
                actions = {
                    IconButton(onClick = { showDatePicker() }) {
                        Icon(
                            imageVector = Icons.Default.CalendarMonth,
                            contentDescription = "Kalender filter",
                            tint = if (selectedDateYmd != null) EmeraldPrimary else TextPrimary
                        )
                    }
                    IconButton(onClick = { loadClips() }) {
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
        ) {
            // Horizontal Filter Chips Bar
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                categories.forEach { cat ->
                    val isSelected = selectedCategory == cat
                    Surface(
                        color = if (isSelected) EmeraldPrimary.copy(alpha = 0.20f) else SurfaceDark,
                        shape = RoundedCornerShape(20.dp),
                        border = androidx.compose.foundation.BorderStroke(
                            1.dp,
                            if (isSelected) EmeraldPrimary else DividerDark
                        ),
                        modifier = Modifier.clickable { selectedCategory = cat }
                    ) {
                        Text(
                            text = cat,
                            color = if (isSelected) EmeraldPrimary else TextSecondary,
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)
                        )
                    }
                }

                // Active Date Filter Chip
                if (selectedDateYmd != null) {
                    Surface(
                        color = CyanAccent.copy(alpha = 0.20f),
                        shape = RoundedCornerShape(20.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, CyanAccent),
                        modifier = Modifier.clickable { selectedDateYmd = null }
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)
                        ) {
                            Text(
                                text = "📅 ${formatChipDate(selectedDateYmd!!)} ✕",
                                color = CyanAccent,
                                style = MaterialTheme.typography.labelMedium,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }
                }
            }

            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
            ) {
                if (isLoading && clips.isEmpty()) {
                    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = EmeraldPrimary)
                    }
                } else if (filteredClips.isEmpty()) {
                    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Icon(imageVector = Icons.Default.FilterAltOff, contentDescription = null, tint = TextSecondary, modifier = Modifier.size(40.dp))
                            Text("Geen opnames gevonden voor dit filter", color = TextSecondary)
                            if (selectedCategory != "Alles" || selectedDateYmd != null) {
                                TextButton(onClick = {
                                    selectedCategory = "Alles"
                                    selectedDateYmd = null
                                }) {
                                    Text("Wis filters", color = EmeraldPrimary)
                                }
                            }
                        }
                    }
                } else {
                    LazyColumn(
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(bottom = 24.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        groupedClips.forEach { (dateHeader, clipList) ->
                            item(key = "header_$dateHeader") {
                                Surface(
                                    color = BackgroundDark,
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(horizontal = 16.dp, vertical = 6.dp)
                                ) {
                                    Row(
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                                    ) {
                                        Text(
                                            text = dateHeader,
                                            style = MaterialTheme.typography.titleSmall,
                                            fontWeight = FontWeight.Bold,
                                            color = EmeraldPrimary
                                        )
                                        Box(
                                            modifier = Modifier
                                                .background(SurfaceVariantDark, CircleShape)
                                                .padding(horizontal = 8.dp, vertical = 2.dp)
                                        ) {
                                            Text(
                                                text = "${clipList.size}",
                                                style = MaterialTheme.typography.labelSmall,
                                                color = TextSecondary
                                            )
                                        }
                                    }
                                }
                            }

                            items(clipList, key = { it.id }) { clip ->
                                Box(modifier = Modifier.padding(horizontal = 16.dp)) {
                                    ClipCard(
                                        clip = clip,
                                        baseUrl = baseUrl,
                                        onPlay = { selectedClip = clip },
                                        onDelete = { deleteClip(clip.id) },
                                        onToggleStar = { toggleStar(clip.id, clip.starred) }
                                    )
                                }
                            }
                        }
                    }
                }

                // Player Dialog
                selectedClip?.let { clip ->
                    ClipPlayerDialog(
                        clip = clip,
                        baseUrl = baseUrl,
                        onDismiss = {
                            selectedClip = null
                            onClipSelected()
                        }
                    )
                }
            }
        }
    }
}

private fun downloadClip(context: Context, downloadUrl: String, filename: String) {
    try {
        val uri = Uri.parse(downloadUrl)
        val request = DownloadManager.Request(uri).apply {
            setTitle("Foxway Deurbel Opname")
            setDescription("Bezig met opslaan in Downloads: $filename")
            setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
            setAllowedOverMetered(true)
            setAllowedOverRoaming(true)
        }
        val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        dm.enqueue(request)
        Toast.makeText(context, "Video opgeslagen in Downloads", Toast.LENGTH_LONG).show()
    } catch (e: Exception) {
        Toast.makeText(context, "Download mislukt: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
    }
}

private fun extractDateYmd(isoTimestamp: String): String {
    return try {
        val clean = isoTimestamp.replace("Z", "")
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val date = parser.parse(clean.take(19)) ?: return isoTimestamp.take(10)
        val amsterdamTz = TimeZone.getTimeZone("Europe/Amsterdam")
        val ymdFormat = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = amsterdamTz
        }
        ymdFormat.format(date)
    } catch (_: Exception) {
        isoTimestamp.take(10)
    }
}

private fun formatChipDate(ymd: String): String {
    return try {
        val parser = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        val date = parser.parse(ymd) ?: return ymd
        val fmt = SimpleDateFormat("d MMM", Locale("nl", "NL"))
        fmt.format(date)
    } catch (_: Exception) {
        ymd
    }
}

private fun formatDateHeader(isoTimestamp: String): String {
    val amsterdamTz = TimeZone.getTimeZone("Europe/Amsterdam")
    val now = Calendar.getInstance(amsterdamTz)
    if (isoTimestamp.isBlank()) {
        val dFormat = SimpleDateFormat("d MMM", Locale("nl", "NL")).apply { timeZone = amsterdamTz }
        return "Vandaag (${dFormat.format(now.time)})"
    }
    return try {
        val clean = isoTimestamp.replace("Z", "")
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val date = parser.parse(clean.take(19)) ?: now.time

        val itemCal = Calendar.getInstance(amsterdamTz).apply { time = date }

        val isToday = now.get(Calendar.YEAR) == itemCal.get(Calendar.YEAR) &&
                now.get(Calendar.DAY_OF_YEAR) == itemCal.get(Calendar.DAY_OF_YEAR)
        if (isToday) {
            val dFormat = SimpleDateFormat("d MMM", Locale("nl", "NL")).apply { timeZone = amsterdamTz }
            return "Vandaag (${dFormat.format(date)})"
        }

        val yesterdayCal = (now.clone() as Calendar).apply { add(Calendar.DAY_OF_YEAR, -1) }
        val isYesterday = yesterdayCal.get(Calendar.YEAR) == itemCal.get(Calendar.YEAR) &&
                yesterdayCal.get(Calendar.DAY_OF_YEAR) == itemCal.get(Calendar.DAY_OF_YEAR)
        if (isYesterday) {
            val dFormat = SimpleDateFormat("d MMM", Locale("nl", "NL")).apply { timeZone = amsterdamTz }
            return "Gisteren (${dFormat.format(date)})"
        }

        val fullFormat = SimpleDateFormat("d MMMM yyyy", Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        fullFormat.format(date)
    } catch (_: Exception) {
        val dFormat = SimpleDateFormat("d MMM", Locale("nl", "NL")).apply { timeZone = amsterdamTz }
        "Vandaag (${dFormat.format(now.time)})"
    }
}

private fun formatExactTime(isoTimestamp: String): String {
    val amsterdamTz = TimeZone.getTimeZone("Europe/Amsterdam")
    val now = Calendar.getInstance(amsterdamTz)
    if (isoTimestamp.isBlank()) {
        val timeFormat = SimpleDateFormat("HH:mm:ss", Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        return timeFormat.format(now.time)
    }
    return try {
        val clean = isoTimestamp.replace("Z", "")
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val date = parser.parse(clean.take(19)) ?: now.time
        val timeFormat = SimpleDateFormat("HH:mm:ss", Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        timeFormat.format(date)
    } catch (_: Exception) {
        val timeFormat = SimpleDateFormat("HH:mm:ss", Locale("nl", "NL")).apply {
            timeZone = amsterdamTz
        }
        timeFormat.format(now.time)
    }
}

private fun formatDuration(durationSeconds: Double): String {
    val totalSec = durationSeconds.toInt()
    val min = totalSec / 60
    val sec = totalSec % 60
    return String.format(Locale.US, "%02d:%02d", min, sec)
}

private fun formatFileSize(sizeBytes: Long): String {
    if (sizeBytes <= 0) return ""
    val mb = sizeBytes / (1024.0 * 1024.0)
    return String.format(Locale.US, "%.1f MB", mb)
}

@Composable
private fun ClipCard(
    clip: ClipItem,
    baseUrl: String,
    onPlay: () -> Unit,
    onDelete: () -> Unit,
    onToggleStar: () -> Unit
) {
    val context = LocalContext.current
    val fullSnapshotUrl = if (clip.snapshotUrl.isNotBlank()) {
        if (clip.snapshotUrl.startsWith("http")) clip.snapshotUrl else "$baseUrl${clip.snapshotUrl}"
    } else ""
    val videoStreamUrl = if (clip.videoUrl.startsWith("http")) clip.videoUrl else "$baseUrl${clip.videoUrl}"

    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceDark),
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, DividerDark, RoundedCornerShape(16.dp))
    ) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                // Snapshot Thumbnail with Play Icon & Duration Badge
                Box(
                    modifier = Modifier
                        .size(width = 104.dp, height = 78.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .background(SurfaceVariantDark)
                        .clickable { onPlay() }
                ) {
                    if (fullSnapshotUrl.isNotBlank()) {
                        AsyncImage(
                            model = fullSnapshotUrl,
                            contentDescription = "Clip thumbnail",
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.fillMaxSize()
                        )
                    }
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .background(Color.Black.copy(alpha = 0.25f)),
                        contentAlignment = Alignment.Center
                    ) {
                        Icon(
                            imageVector = Icons.Default.PlayCircle,
                            contentDescription = "Afspelen",
                            tint = Color.White.copy(alpha = 0.9f),
                            modifier = Modifier.size(32.dp)
                        )
                    }
                    Box(
                        modifier = Modifier
                            .align(Alignment.BottomEnd)
                            .padding(4.dp)
                            .background(Color.Black.copy(alpha = 0.75f), RoundedCornerShape(4.dp))
                            .padding(horizontal = 4.dp, vertical = 2.dp)
                    ) {
                        Text(
                            text = if (clip.isRecording) "🔴 Nu aan het opnemen..." else formatDuration(clip.duration),
                            style = MaterialTheme.typography.labelSmall,
                            color = if (clip.isRecording) Color(0xFFEF4444) else TextPrimary
                        )
                    }
                }

                // Info column
                Column(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(5.dp)
                ) {
                    // Exact time & file size row
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = formatExactTime(clip.timestamp),
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                            color = TextPrimary
                        )
                        val sizeText = formatFileSize(clip.sizeBytes)
                        if (sizeText.isNotBlank()) {
                            Text(
                                text = sizeText,
                                style = MaterialTheme.typography.labelSmall,
                                color = TextSecondary
                            )
                        }
                    }

                    // Detection category badge
                    val (badgeIcon, statusText, badgeColor) = when {
                        !clip.answeredBy.isNullOrBlank() && clip.answeredBy.contains("Handmatige") -> {
                            Triple("🎥", clip.answeredBy, CoralDecline)
                        }
                        !clip.answeredBy.isNullOrBlank() && clip.answeredBy.contains("Persoon") -> {
                            Triple("🚶", "Persoon gedetecteerd", EmeraldPrimary)
                        }
                        !clip.answeredBy.isNullOrBlank() && (clip.answeredBy.contains("Voertuig") || clip.answeredBy.contains("Auto")) -> {
                            Triple("🚗", "Voertuig gedetecteerd", CyanAccent)
                        }
                        !clip.answeredBy.isNullOrBlank() && clip.answeredBy.contains("Dier") -> {
                            Triple("🐾", "Dier gedetecteerd", AmberWarning)
                        }
                        !clip.answeredBy.isNullOrBlank() && clip.answeredBy.startsWith("AI:") -> {
                            Triple("🤖", clip.answeredBy.replace("AI:", "").trim(), CyanAccent)
                        }
                        !clip.answeredBy.isNullOrBlank() && (clip.answeredBy.contains("pakketje") || clip.answeredBy.contains("quick") || clip.answeredBy.contains("antwoord")) -> {
                            Triple("💬", "Snel antwoord gestuurd", CyanAccent)
                        }
                        !clip.answeredBy.isNullOrBlank() -> {
                            Triple("🔔", "Beantwoord door ${clip.answeredBy}", LanGreen)
                        }
                        else -> {
                            Triple("🔔", "Aangebeld / Oproep", AmberWarning)
                        }
                    }

                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        Text(
                            text = "$badgeIcon $statusText",
                            style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
                            color = badgeColor
                        )
                    }
                }
            }

            HorizontalDivider(color = DividerDark.copy(alpha = 0.5f))

            // Action Buttons: Afspelen, Downloaden, Star (Lock), Verwijderen
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                // 1. Afspelen
                TextButton(
                    onClick = onPlay,
                    contentPadding = PaddingValues(horizontal = 6.dp, vertical = 4.dp)
                ) {
                    Icon(imageVector = Icons.Default.PlayArrow, contentDescription = null, tint = EmeraldPrimary, modifier = Modifier.size(18.dp))
                    Spacer(modifier = Modifier.width(3.dp))
                    Text("Afspelen", color = EmeraldPrimary, style = MaterialTheme.typography.labelMedium)
                }

                // 2. Downloaden
                TextButton(
                    onClick = {
                        val filename = "foxway_${clip.id}.mp4"
                        val fileDownloadUrl = "$baseUrl/api/v1/clips/${clip.id}/file"
                        downloadClip(context, fileDownloadUrl, filename)
                    },
                    contentPadding = PaddingValues(horizontal = 6.dp, vertical = 4.dp)
                ) {
                    Icon(imageVector = Icons.Default.Download, contentDescription = null, tint = CyanAccent, modifier = Modifier.size(18.dp))
                    Spacer(modifier = Modifier.width(3.dp))
                    Text("Downloaden", color = CyanAccent, style = MaterialTheme.typography.labelMedium)
                }

                // 3. Star / Lock Toggle (⭐)
                IconButton(
                    onClick = onToggleStar,
                    modifier = Modifier.size(36.dp)
                ) {
                    Icon(
                        imageVector = if (clip.starred) Icons.Default.Star else Icons.Default.StarBorder,
                        contentDescription = if (clip.starred) "Vastgezet" else "Vastzetten",
                        tint = if (clip.starred) Color(0xFFF59E0B) else TextSecondary,
                        modifier = Modifier.size(20.dp)
                    )
                }

                // 4. Verwijderen
                var showDeleteConfirm by remember { mutableStateOf(false) }
                IconButton(
                    onClick = { showDeleteConfirm = true },
                    modifier = Modifier.size(36.dp)
                ) {
                    Icon(imageVector = Icons.Default.DeleteOutline, contentDescription = "Verwijderen", tint = CoralDecline, modifier = Modifier.size(20.dp))
                }

                if (showDeleteConfirm) {
                    AlertDialog(
                        onDismissRequest = { showDeleteConfirm = false },
                        title = { Text("Opname verwijderen?") },
                        text = { Text("Weet je zeker dat je deze clip wilt verwijderen van de server?") },
                        confirmButton = {
                            TextButton(onClick = {
                                showDeleteConfirm = false
                                onDelete()
                            }) {
                                Text("Verwijderen", color = CoralDecline, fontWeight = FontWeight.Bold)
                            }
                        },
                        dismissButton = {
                            TextButton(onClick = { showDeleteConfirm = false }) {
                                Text("Annuleren", color = TextSecondary)
                            }
                        },
                        containerColor = SurfaceDark,
                        titleContentColor = TextPrimary,
                        textContentColor = TextSecondary
                    )
                }
            }
        }
    }
}

/**
 * 100% Fullscreen Video Player with Pinch-to-Zoom (up to 5x), Pan & Seekbar Controls.
 */
@Composable
private fun ClipPlayerDialog(
    clip: ClipItem,
    baseUrl: String,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    var currentClip by remember { mutableStateOf(clip) }
    var isRecordingState by remember { mutableStateOf(clip.isRecording || clip.duration <= 0.0) }

    var scale by remember { mutableFloatStateOf(1f) }
    var offsetX by remember { mutableFloatStateOf(0f) }
    var offsetY by remember { mutableFloatStateOf(0f) }
    var isFitMode by remember { mutableStateOf(true) }
    var controlsVisible by remember { mutableStateOf(true) }

    var isPlaying by remember { mutableStateOf(true) }
    var currentPositionMs by remember { mutableLongStateOf(0L) }
    var durationMs by remember { mutableLongStateOf(0L) }
    var isUserSeeking by remember { mutableStateOf(false) }
    var seekPositionRatio by remember { mutableFloatStateOf(0f) }

    val exoPlayer = remember {
        ExoPlayer.Builder(context).build()
    }

    DisposableEffect(Unit) {
        onDispose {
            exoPlayer.release()
        }
    }

    // Polling loop for active recordings (TAAK 2.1)
    LaunchedEffect(currentClip.id) {
        if (isRecordingState) {
            val api = ApiClientFactory.createApi(baseUrl)
            while (isRecordingState) {
                delay(2000L)
                try {
                    val resp = api.getClipDetail(currentClip.id)
                    if (resp.isSuccessful && resp.body() != null) {
                        val updated = resp.body()!!
                        currentClip = updated
                        if (updated.status.equals("READY", ignoreCase = true) && updated.duration > 0.0) {
                            isRecordingState = false
                            val streamUrl = if (updated.videoUrl.startsWith("http")) updated.videoUrl else "$baseUrl${updated.videoUrl}"
                            exoPlayer.setMediaItem(MediaItem.fromUri(streamUrl))
                            exoPlayer.prepare()
                            exoPlayer.playWhenReady = true
                            break
                        }
                    }
                } catch (_: Exception) {}
            }
        } else {
            val videoStreamUrl = if (currentClip.videoUrl.startsWith("http")) currentClip.videoUrl else "$baseUrl${currentClip.videoUrl}"
            exoPlayer.setMediaItem(MediaItem.fromUri(videoStreamUrl))
            exoPlayer.prepare()
            exoPlayer.playWhenReady = true
        }
    }

    // Playback state and seekbar synchronizer
    LaunchedEffect(exoPlayer, isUserSeeking, isRecordingState) {
        if (!isRecordingState) {
            while (true) {
                if (!isUserSeeking) {
                    currentPositionMs = exoPlayer.currentPosition.coerceAtLeast(0L)
                    durationMs = exoPlayer.duration.coerceAtLeast(0L)
                    if (durationMs > 0) {
                        seekPositionRatio = (currentPositionMs.toFloat() / durationMs.toFloat()).coerceIn(0f, 1f)
                    }
                }
                isPlaying = exoPlayer.isPlaying
                delay(200L)
            }
        }
    }

    val handleDismiss = {
        try {
            exoPlayer.stop()
            exoPlayer.clearMediaItems()
            exoPlayer.release()
        } catch (_: Exception) {}
        onDismiss()
    }

    DisposableEffect(Unit) {
        onDispose {
            try {
                exoPlayer.stop()
                exoPlayer.clearMediaItems()
                exoPlayer.release()
            } catch (_: Exception) {}
        }
    }

    Dialog(
        onDismissRequest = handleDismiss,
        properties = DialogProperties(
            usePlatformDefaultWidth = false,
            dismissOnBackPress = true
        )
    ) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(Color.Black)
        ) {
            // ── 1. Video Container with Pinch-to-Zoom (up to 5x), Pan & Double-tap Reset ──
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .pointerInput(Unit) {
                        detectTapGestures(
                            onDoubleTap = {
                                scale = 1f
                                offsetX = 0f
                                offsetY = 0f
                            },
                            onTap = {
                                controlsVisible = !controlsVisible
                            }
                        )
                    }
                    .pointerInput(Unit) {
                        detectTransformGestures { _, pan, zoom, _ ->
                            val newScale = (scale * zoom).coerceIn(1f, 5f)
                            scale = newScale
                            if (newScale > 1f) {
                                offsetX = (offsetX + pan.x).coerceIn(-1200f * (newScale - 1f), 1200f * (newScale - 1f))
                                offsetY = (offsetY + pan.y).coerceIn(-1200f * (newScale - 1f), 1200f * (newScale - 1f))
                            } else {
                                offsetX = 0f
                                offsetY = 0f
                            }
                        }
                    }
                    .graphicsLayer {
                        scaleX = scale
                        scaleY = scale
                        translationX = offsetX
                        translationY = offsetY
                    },
                contentAlignment = Alignment.Center
            ) {
                if (!isRecordingState) {
                    AndroidView(
                        factory = { ctx ->
                            PlayerView(ctx).apply {
                                player = exoPlayer
                                useController = false
                                setShowBuffering(PlayerView.SHOW_BUFFERING_ALWAYS)
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
                                .aspectRatio(4f / 3f)
                        } else {
                            Modifier.fillMaxSize()
                        }
                    )
                } else {
                    // Recording in progress! Show snapshot background + neat status overlay (TAAK 2.1)
                    val fullThumbUrl = if (currentClip.thumbnailUrl.isNotBlank()) {
                        if (currentClip.thumbnailUrl.startsWith("http")) currentClip.thumbnailUrl else "$baseUrl${currentClip.thumbnailUrl}"
                    } else if (currentClip.snapshotUrl.isNotBlank()) {
                        if (currentClip.snapshotUrl.startsWith("http")) currentClip.snapshotUrl else "$baseUrl${currentClip.snapshotUrl}"
                    } else "$baseUrl/api/v1/clips/${currentClip.id}/thumbnail"

                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .aspectRatio(4f / 3f)
                            .background(Color.Black),
                        contentAlignment = Alignment.Center
                    ) {
                        AsyncImage(
                            model = fullThumbUrl,
                            contentDescription = "Live snapshot preview",
                            contentScale = ContentScale.Fit,
                            modifier = Modifier.fillMaxSize()
                        )
                        Box(
                            modifier = Modifier
                                .fillMaxSize()
                                .background(Color.Black.copy(alpha = 0.45f))
                        )
                        Surface(
                            color = Color(0xDD0F172A),
                            shape = RoundedCornerShape(16.dp),
                            border = androidx.compose.foundation.BorderStroke(1.5.dp, Color(0xFFEF4444)),
                            modifier = Modifier.padding(16.dp)
                        ) {
                            Column(
                                modifier = Modifier.padding(horizontal = 20.dp, vertical = 16.dp),
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.spacedBy(10.dp)
                            ) {
                                CircularProgressIndicator(
                                    color = Color(0xFFEF4444),
                                    modifier = Modifier.size(32.dp),
                                    strokeWidth = 3.dp
                                )
                                Text(
                                    text = "🔴 Opname wordt nog afgerond (20s na-opname)...",
                                    style = MaterialTheme.typography.titleSmall,
                                    fontWeight = FontWeight.Bold,
                                    color = Color.White
                                )
                                Text(
                                    text = "Nog enkele seconden geduld",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Color(0xFF94A3B8)
                                )
                            }
                        }
                    }
                }
            }

            // ── 2. Top Controls Overlay ──
            if (controlsVisible) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .statusBarsPadding()
                        .padding(horizontal = 16.dp, vertical = 12.dp)
                        .align(Alignment.TopCenter),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = currentClip.answeredBy?.ifBlank { "Opname bekijken" } ?: "Opname bekijken",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold,
                            color = Color.White
                        )
                        val timestampToFormat = currentClip.timestamp.ifBlank {
                            java.time.Instant.now().toString()
                        }
                        Text(
                            text = "${formatDateHeader(timestampToFormat)} • ${formatExactTime(timestampToFormat)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = Color(0xFF94A3B8)
                        )
                    }

                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Fit / Fill toggle button
                        Surface(
                            color = Color(0xCC1E293B),
                            shape = RoundedCornerShape(18.dp),
                            border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155)),
                            modifier = Modifier.clickable {
                                isFitMode = !isFitMode
                            }
                        ) {
                            Row(
                                modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(4.dp)
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
                                    color = Color.White
                                )
                            }
                        }

                        // Close (X) button
                        IconButton(
                            onClick = handleDismiss,
                            modifier = Modifier
                                .size(40.dp)
                                .background(Color(0xCC1E293B), CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Close,
                                contentDescription = "Sluiten",
                                tint = Color.White,
                                modifier = Modifier.size(22.dp)
                            )
                        }
                    }
                }
            }

            // ── 3. Bottom Controls Overlay (Seekbar, Play/Pause, Download) ──
            if (controlsVisible) {
                if (isRecordingState) {
                    Surface(
                        modifier = Modifier
                            .fillMaxWidth()
                            .align(Alignment.BottomCenter)
                            .navigationBarsPadding()
                            .padding(horizontal = 16.dp, vertical = 16.dp),
                        color = Color(0xDD0F172A),
                        shape = RoundedCornerShape(24.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFFEF4444))
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 20.dp, vertical = 14.dp),
                            horizontalArrangement = Arrangement.Center,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                text = "🔴 Bezig met verwerken van video & pre-roll...",
                                style = MaterialTheme.typography.bodyMedium,
                                fontWeight = FontWeight.SemiBold,
                                color = Color(0xFFEF4444)
                            )
                        }
                    }
                } else {
                    Surface(
                        modifier = Modifier
                            .fillMaxWidth()
                            .align(Alignment.BottomCenter)
                            .navigationBarsPadding()
                            .padding(horizontal = 16.dp, vertical = 16.dp),
                        color = Color(0xDD0F172A),
                        shape = RoundedCornerShape(24.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155))
                    ) {
                        Column(
                            modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            // Time indicators
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text(
                                    text = formatDurationMs(currentPositionMs),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Color.White
                                )
                                Text(
                                    text = formatDurationMs(durationMs),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Color(0xFF94A3B8)
                                )
                            }

                            // Interactive Seekbar
                            Slider(
                                value = if (isUserSeeking) seekPositionRatio else (if (durationMs > 0) currentPositionMs.toFloat() / durationMs.toFloat() else 0f),
                                onValueChange = {
                                 isUserSeeking = true
                                 seekPositionRatio = it
                                },
                                onValueChangeFinished = {
                                    if (durationMs > 0) {
                                        val targetMs = (seekPositionRatio * durationMs).toLong()
                                        exoPlayer.seekTo(targetMs)
                                    }
                                    isUserSeeking = false
                                },
                                colors = SliderDefaults.colors(
                                    thumbColor = CyanAccent,
                                    activeTrackColor = CyanAccent,
                                    inactiveTrackColor = Color(0xFF334155)
                                ),
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .height(20.dp)
                            )

                            // Play/Pause, -10s, +10s, Download controls
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Row(
                                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    // Play / Pause
                                    IconButton(
                                        onClick = {
                                            if (exoPlayer.isPlaying) {
                                                exoPlayer.pause()
                                            } else {
                                                if (exoPlayer.playbackState == Player.STATE_ENDED) {
                                                    exoPlayer.seekTo(0)
                                                }
                                                exoPlayer.play()
                                            }
                                            isPlaying = exoPlayer.isPlaying
                                        },
                                        modifier = Modifier
                                            .size(44.dp)
                                            .background(CyanAccent, CircleShape)
                                    ) {
                                        Icon(
                                            imageVector = if (isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow,
                                            contentDescription = if (isPlaying) "Pauzeren" else "Afspelen",
                                            tint = BackgroundDark,
                                            modifier = Modifier.size(24.dp)
                                        )
                                    }

                                    // Reset zoom indicator if zoomed in
                                    if (scale > 1.05f) {
                                        Surface(
                                            color = Color(0x3338BDF8),
                                            shape = RoundedCornerShape(12.dp),
                                            modifier = Modifier.clickable {
                                                scale = 1f
                                                offsetX = 0f
                                                offsetY = 0f
                                            }
                                        ) {
                                            Text(
                                                text = "${String.format("%.1f", scale)}x (Reset)",
                                                color = CyanAccent,
                                                style = MaterialTheme.typography.labelSmall,
                                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                                            )
                                        }
                                    }
                                }

                                // Download Button
                                Button(
                                    onClick = {
                                        val filename = "foxway_${clip.id}.mp4"
                                        val fileDownloadUrl = "$baseUrl/api/v1/clips/${clip.id}/file"
                                        downloadClip(context, fileDownloadUrl, filename)
                                    },
                                    shape = RoundedCornerShape(12.dp),
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1E293B)),
                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155)),
                                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 8.dp)
                                ) {
                                    Icon(
                                        imageVector = Icons.Default.Download,
                                        contentDescription = null,
                                        tint = CyanAccent,
                                        modifier = Modifier.size(18.dp)
                                    )
                                    Spacer(modifier = Modifier.width(6.dp))
                                    Text(
                                        text = "Downloaden",
                                        color = Color.White,
                                        style = MaterialTheme.typography.labelMedium,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

private fun formatDurationMs(ms: Long): String {
    val totalSeconds = (ms / 1000).coerceAtLeast(0)
    val minutes = totalSeconds / 60
    val seconds = totalSeconds % 60
    return String.format("%02d:%02d", minutes, seconds)
}
