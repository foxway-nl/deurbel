package nl.foxway.deurbel.util

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.content.ContextCompat

/**
 * Validates and provides intents for the 5 critical Android system permissions
 * required for reliable doorbell ringing over lockscreen and background operation.
 *
 * Implements SPEC §4.1 & User Request Requirement #2.
 */
data class SystemPermissionState(
    val hasNotificationPermission: Boolean,
    val hasRecordAudioPermission: Boolean,
    val isBatteryOptimizationIgnored: Boolean,
    val canDrawOverlays: Boolean,
    val canUseFullScreenIntent: Boolean
) {
    val allGranted: Boolean
        get() = hasNotificationPermission &&
                hasRecordAudioPermission &&
                isBatteryOptimizationIgnored &&
                canDrawOverlays &&
                canUseFullScreenIntent

    val missingCount: Int
        get() = listOf(
            hasNotificationPermission,
            hasRecordAudioPermission,
            isBatteryOptimizationIgnored,
            canDrawOverlays,
            canUseFullScreenIntent
        ).count { !it }
}

object PermissionChecker {

    fun checkAll(context: Context): SystemPermissionState {
        // 1. POST_NOTIFICATIONS
        val hasNotifications = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
        } else {
            true
        }

        // 2. RECORD_AUDIO
        val hasAudio = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED

        // 3. Battery optimization exemption
        val powerManager = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
        val batteryIgnored = powerManager?.isIgnoringBatteryOptimizations(context.packageName) == true

        // 4. Appear on top (canDrawOverlays)
        val canOverlay = Settings.canDrawOverlays(context)

        // 5. Full screen intent (Android 14+ / API 34+)
        val canFullScreen = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            notificationManager?.canUseFullScreenIntent() ?: true
        } else {
            true
        }

        return SystemPermissionState(
            hasNotificationPermission = hasNotifications,
            hasRecordAudioPermission = hasAudio,
            isBatteryOptimizationIgnored = batteryIgnored,
            canDrawOverlays = canOverlay,
            canUseFullScreenIntent = canFullScreen
        )
    }

    fun openNotificationSettings(context: Context) {
        val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
            putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(intent)
        } catch (_: Exception) {
            openAppDetails(context)
        }
    }

    fun openBatteryOptimizationSettings(context: Context) {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:${context.packageName}")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        } else {
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        }
        try {
            context.startActivity(intent)
        } catch (_: Exception) {
            try {
                context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
            } catch (_: Exception) {
                openAppDetails(context)
            }
        }
    }

    fun openOverlaySettings(context: Context) {
        val intent = Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:${context.packageName}")
        ).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(intent)
        } catch (_: Exception) {
            openAppDetails(context)
        }
    }

    fun openFullScreenIntentSettings(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val intent = Intent(
                Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                Uri.parse("package:${context.packageName}")
            ).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            try {
                context.startActivity(intent)
                return
            } catch (_: Exception) {}
        }
        openAppDetails(context)
    }

    fun openAppDetails(context: Context) {
        val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
            data = Uri.parse("package:${context.packageName}")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }
}
