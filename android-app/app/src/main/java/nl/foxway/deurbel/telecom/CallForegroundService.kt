package nl.foxway.deurbel.telecom

import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder

/**
 * Legacy service stub preserved for backward compatibility.
 *
 * Notification-based incoming call logic has been completely replaced by the
 * official Android Telecom Framework in [DoorbellConnectionService].
 */
class CallForegroundService : Service() {

    companion object {
        fun stop(context: Context) {
            try {
                val intent = Intent(context, CallForegroundService::class.java)
                context.stopService(intent)
            } catch (_: Exception) {}
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        stopSelf()
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
