package nl.foxway.deurbel

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import nl.foxway.deurbel.network.WebSocketForegroundService
import nl.foxway.deurbel.ui.clips.ClipsScreen
import nl.foxway.deurbel.ui.live.LiveScreen
import nl.foxway.deurbel.ui.onboarding.OnboardingScreen
import nl.foxway.deurbel.ui.settings.SettingsScreen
import nl.foxway.deurbel.ui.theme.BackgroundDark
import nl.foxway.deurbel.ui.theme.DividerDark
import nl.foxway.deurbel.ui.theme.EmeraldPrimary
import nl.foxway.deurbel.ui.theme.FoxwayDeurbelTheme
import nl.foxway.deurbel.ui.theme.SurfaceDark
import nl.foxway.deurbel.ui.theme.TextPrimary
import nl.foxway.deurbel.ui.theme.TextSecondary

enum class MainTab(val label: String, val icon: ImageVector) {
    LIVE("Live Kijken", Icons.Default.Videocam),
    CLIPS("Opnames", Icons.Default.VideoLibrary),
    SETTINGS("Instellingen", Icons.Default.Settings)
}

class MainActivity : ComponentActivity() {

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ ->
        // Permissions handled
    }

    private var initialTargetClipId by mutableStateOf<String?>(null)
    private var initialTargetScreen by mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        parseDeeplink(intent)
        requestRequiredPermissions()

        setContent {
            FoxwayDeurbelTheme {
                val prefs = remember { FoxwayApp.instance.preferencesManager }
                val isPaired by prefs.isPaired.collectAsState(initial = false)

                LaunchedEffect(isPaired) {
                    if (isPaired) {
                        try {
                            WebSocketForegroundService.start(this@MainActivity)
                        } catch (_: Exception) {}
                        FoxwayApp.instance.syncFcmToken()
                    }
                }

                if (!isPaired) {
                    OnboardingScreen(
                        onPairingSuccess = {
                            try {
                                WebSocketForegroundService.start(this@MainActivity)
                            } catch (_: Exception) {}
                            FoxwayApp.instance.syncFcmToken()
                        }
                    )
                } else {
                    MainAppScaffold(
                        initialTargetScreen = initialTargetScreen,
                        initialTargetClipId = initialTargetClipId,
                        onUnpair = {
                            WebSocketForegroundService.stop(this@MainActivity)
                        }
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        parseDeeplink(intent)
    }

    private fun parseDeeplink(intent: android.content.Intent?) {
        val screen = intent?.getStringExtra("screen")
        val clipId = intent?.getStringExtra("clip_id")
        if (screen == "clips" || !clipId.isNullOrBlank()) {
            initialTargetScreen = "clips"
            initialTargetClipId = clipId
        }
    }

    private fun requestRequiredPermissions() {
        val permissions = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                permissions.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.RECORD_AUDIO)
        }
        if (permissions.isNotEmpty()) {
            permissionLauncher.launch(permissions.toTypedArray())
        }
    }
}

@Composable
fun MainAppScaffold(
    initialTargetScreen: String? = null,
    initialTargetClipId: String? = null,
    onUnpair: () -> Unit
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    var activeTargetClipId by remember { mutableStateOf(initialTargetClipId) }
    var selectedTab by remember {
        mutableStateOf(if (initialTargetScreen == "clips" || !initialTargetClipId.isNullOrBlank()) MainTab.CLIPS else MainTab.LIVE)
    }
    var permState by remember { mutableStateOf(nl.foxway.deurbel.util.PermissionChecker.checkAll(context)) }
    var openPermissionsInSettings by remember { mutableStateOf(false) }

    LaunchedEffect(initialTargetScreen, initialTargetClipId) {
        if (initialTargetScreen == "clips" || !initialTargetClipId.isNullOrBlank()) {
            selectedTab = MainTab.CLIPS
            activeTargetClipId = initialTargetClipId
        }
    }

    // Check permissions on enter / resume
    val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
            if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME) {
                permState = nl.foxway.deurbel.util.PermissionChecker.checkAll(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    Scaffold(
        bottomBar = {
            NavigationBar(
                containerColor = SurfaceDark,
                contentColor = TextPrimary
            ) {
                MainTab.values().forEach { tab ->
                    NavigationBarItem(
                        icon = { Icon(imageVector = tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label) },
                        selected = selectedTab == tab,
                        onClick = { selectedTab = tab },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = EmeraldPrimary,
                            selectedTextColor = EmeraldPrimary,
                            unselectedIconColor = TextSecondary,
                            unselectedTextColor = TextSecondary,
                            indicatorColor = BackgroundDark
                        )
                    )
                }
            }
        },
        containerColor = BackgroundDark
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            // High-priority permission warning banner
            if (!permState.allGranted) {
                Surface(
                    color = androidx.compose.ui.graphics.Color(0xFFDC2626),
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 6.dp),
                    shape = androidx.compose.foundation.shape.RoundedCornerShape(10.dp),
                    onClick = {
                        openPermissionsInSettings = true
                        selectedTab = MainTab.SETTINGS
                    }
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                        verticalAlignment = androidx.compose.ui.Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Icon(
                            imageVector = Icons.Default.Warning,
                            contentDescription = "Waarschuwing",
                            tint = androidx.compose.ui.graphics.Color.White,
                            modifier = Modifier.size(22.dp)
                        )
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Belangrijk: Deurbel kan niet rinkelen als het scherm uit staat",
                                style = MaterialTheme.typography.bodySmall.copy(
                                    fontWeight = androidx.compose.ui.text.font.FontWeight.Bold,
                                    color = androidx.compose.ui.graphics.Color.White
                                )
                            )
                            Text(
                                text = "Tik hier om de ${permState.missingCount} ontbrekende Android-instellingen op te lossen →",
                                style = MaterialTheme.typography.labelSmall.copy(
                                    color = androidx.compose.ui.graphics.Color.White.copy(alpha = 0.9f)
                                )
                            )
                        }
                    }
                }
            }

            Box(modifier = Modifier.fillMaxSize()) {
                when (selectedTab) {
                    MainTab.LIVE -> LiveScreen()
                    MainTab.CLIPS -> ClipsScreen(
                        initialClipId = activeTargetClipId,
                        onClipSelected = {
                            activeTargetClipId = null
                            (context as? android.app.Activity)?.intent?.apply {
                                removeExtra("clip_id")
                                removeExtra("screen")
                            }
                        }
                    )
                    MainTab.SETTINGS -> SettingsScreen(
                        onUnpair = onUnpair,
                        openPermissions = openPermissionsInSettings,
                        onPermissionsOpened = { openPermissionsInSettings = false }
                    )
                }
            }
        }
    }
}
