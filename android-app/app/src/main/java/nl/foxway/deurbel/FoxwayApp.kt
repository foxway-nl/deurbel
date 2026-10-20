package nl.foxway.deurbel

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import nl.foxway.deurbel.data.PreferencesManager

/**
 * Foxway Deurbel Application class.
 *
 * Initialises notification channels and global singletons at app start.
 */
class FoxwayApp : Application() {

    lateinit var preferencesManager: PreferencesManager
        private set

    val applicationScope = kotlinx.coroutines.CoroutineScope(
        kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.IO
    )

    override fun onCreate() {
        super.onCreate()
        instance = this
        preferencesManager = PreferencesManager(this)
        createNotificationChannels()
        nl.foxway.deurbel.telecom.DoorbellConnectionService.registerPhoneAccount(this)
    }

    /**
     * Create notification channels required for incoming call alerts and
     * the persistent WebSocket foreground service.
     */
    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java) ?: return

        // High-priority ring channel — fullScreenIntent triggers here
        val ringChannel = NotificationChannel(
            CHANNEL_RING,
            getString(R.string.channel_ring_name),
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = getString(R.string.channel_ring_description)
            setBypassDnd(true)
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 400, 200, 400, 200, 400)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            setSound(null, null) // We play custom ringtone via MediaPlayer
        }
        manager.createNotificationChannel(ringChannel)

        // Low-priority service channel
        val serviceChannel = NotificationChannel(
            CHANNEL_SERVICE,
            getString(R.string.channel_service_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.channel_service_description)
            setShowBadge(false)
        }
        manager.createNotificationChannel(serviceChannel)

        // General info channel
        val infoChannel = NotificationChannel(
            CHANNEL_INFO,
            "Informatie",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Algemene meldingen"
        }
        manager.createNotificationChannel(infoChannel)
    }

    companion object {
        const val CHANNEL_RING = "foxway_doorbell_ring"
        const val CHANNEL_SERVICE = "foxway_doorbell_service"
        const val CHANNEL_INFO = "foxway_doorbell_info"

        lateinit var instance: FoxwayApp
            private set
    }
}
