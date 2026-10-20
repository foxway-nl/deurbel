package nl.foxway.deurbel.telecom

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.AppLogger
import nl.foxway.deurbel.network.WebSocketForegroundService

/**
 * BroadcastReceiver triggered on system boot (BOOT_COMPLETED).
 * Automatically restarts the WebSocketForegroundService so incoming doorbell rings
 * are never missed even after a phone restart.
 *
 * Implements SPEC §4.1 & Requirement #3.
 */
class BootReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            Log.i(TAG, "Device booted, checking pairing state...")
            val prefs = FoxwayApp.instance.preferencesManager
            val logger = AppLogger.getInstance(context)

            CoroutineScope(Dispatchers.IO).launch {
                try {
                    val isPaired = prefs.isPaired.first()
                    if (isPaired) {
                        Log.i(TAG, "Device is paired. Starting WebSocketForegroundService on boot.")
                        logger.info("Telefoon herstart: Deurbel achtergrondservice gestart")
                        WebSocketForegroundService.start(context)
                    } else {
                        Log.d(TAG, "Device not paired yet, skipping background service start.")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error starting service on boot", e)
                    logger.error("Fout bij herstarten achtergrondservice op boot", e.message)
                }
            }
        }
    }
}
