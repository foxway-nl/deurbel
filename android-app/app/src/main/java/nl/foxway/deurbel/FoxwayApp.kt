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

        // Min-priority service channel — completely silent and minimized
        val serviceChannel = NotificationChannel(
            CHANNEL_SERVICE,
            getString(R.string.channel_service_name),
            NotificationManager.IMPORTANCE_MIN
        ).apply {
            description = getString(R.string.channel_service_description)
            setShowBadge(false)
            enableLights(false)
            enableVibration(false)
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

        // High-importance motion & AI detection channel (Task 6)
        val motionChannel = NotificationChannel(
            CHANNEL_MOTION,
            "Beweging & AI Detectie",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Meldingen bij gedetecteerde personen, voertuigen en dieren met momentopname"
            enableVibration(true)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        manager.createNotificationChannel(motionChannel)
    }

    companion object {
        const val CHANNEL_RING = "foxway_doorbell_ring"
        const val CHANNEL_MOTION = "foxway_doorbell_motion"
        const val CHANNEL_SERVICE = "foxway_doorbell_service"
        const val CHANNEL_INFO = "foxway_doorbell_info"

        lateinit var instance: FoxwayApp
            private set
    }
}
