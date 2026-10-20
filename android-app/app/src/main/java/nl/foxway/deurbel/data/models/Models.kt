package nl.foxway.deurbel.data.models

import com.google.gson.annotations.SerializedName

// ──────────────────────────────────────────────────────────────────
//  API Request / Response data classes
// ──────────────────────────────────────────────────────────────────

/** POST /api/v1/pair request */
data class PairRequest(
    @SerializedName("user_name") val userName: String = "",
    @SerializedName("device_name") val deviceName: String,
    @SerializedName("fcm_token") val fcmToken: String = "",
    @SerializedName("device_id") val deviceId: String = "",
    @SerializedName("android_version") val androidVersion: String = "",
    @SerializedName("connection_type") val connectionType: String = ""
)

/** POST /api/v1/pair response */
data class PairResponse(
    @SerializedName("user_id") val userId: String,
    @SerializedName("user_name") val userName: String,
    @SerializedName("device_name") val deviceName: String,
    @SerializedName("jwt_token") val jwtToken: String,
    @SerializedName("lan_url") val lanUrl: String,
    @SerializedName("wan_url") val wanUrl: String,
    @SerializedName("go2rtc_lan_ws") val go2rtcLanWs: String,
    @SerializedName("go2rtc_wan_ws") val go2rtcWanWs: String
)

/** POST /api/v1/call/{call_id}/claim request */
data class ClaimRequest(
    @SerializedName("user_id") val userId: String,
    @SerializedName("user_name") val userName: String
)

/** POST /api/v1/call/{call_id}/claim response */
data class ClaimResponse(
    val status: String,
    @SerializedName("webrtc_src") val webrtcSrc: String? = null,
    @SerializedName("answered_by") val answeredBy: String? = null
)

/** POST /api/v1/call/{call_id}/quick-reply request */
data class QuickReplyRequest(
    @SerializedName("reply_id") val replyId: String,
    @SerializedName("user_name") val userName: String
)

/** POST /api/v1/clips/{clip_id}/trim request */
data class TrimRequest(
    @SerializedName("start_sec") val startSec: Double,
    @SerializedName("end_sec") val endSec: Double
)

/** PUT /api/v1/devices/{user_id}/fcm-token request */
data class FcmTokenUpdate(
    @SerializedName("fcm_token") val fcmToken: String
)

// ──────────────────────────────────────────────────────────────────
//  Domain models
// ──────────────────────────────────────────────────────────────────

/** Clip item from GET /api/v1/clips */
data class ClipItem(
    val id: String,
    val timestamp: String,
    val duration: Double,
    @SerializedName("snapshot_url") val snapshotUrl: String,
    @SerializedName("video_url") val videoUrl: String,
    @SerializedName("answered_by") val answeredBy: String?,
    val starred: Boolean = false
)

/** Trim response from POST /api/v1/clips/{clip_id}/trim */
data class TrimResponse(
    val status: String,
    @SerializedName("clip_id") val clipId: String,
    @SerializedName("video_url") val videoUrl: String,
    @SerializedName("download_url") val downloadUrl: String
)

/** Call status from GET /api/v1/status */
data class CallStatus(
    val state: String = "IDLE",
    @SerializedName("call_id") val callId: String? = null,
    @SerializedName("claimed_by") val claimedBy: String? = null,
    @SerializedName("started_at") val startedAt: String? = null,
    @SerializedName("connected_ws_users") val connectedWsUsers: List<String>? = null
)

/** Ring trigger response from POST /api/v1/ring */
data class RingResponse(
    val status: String,
    @SerializedName("call_id") val callId: String? = null,
    @SerializedName("snapshot_url") val snapshotUrl: String? = null,
    val timestamp: String? = null,
    val reason: String? = null
)

/** Active family member / device */
data class UserInfo(
    @SerializedName("user_id") val userId: String,
    @SerializedName("user_name") val userName: String,
    @SerializedName("device_name") val deviceName: String,
    val online: Boolean = false,
    @SerializedName("last_seen") val lastSeen: String = "",
    @SerializedName("android_version") val androidVersion: String = "",
    @SerializedName("connection_type") val connectionType: String = ""
)

data class OnboardingBootstrapResponse(
    val status: String,
    @SerializedName("server_name") val serverName: String = "",
    @SerializedName("lan_url") val lanUrl: String = "",
    @SerializedName("wan_url") val wanUrl: String = "",
    @SerializedName("camera_ip") val cameraIp: String = "",
    @SerializedName("camera_port") val cameraPort: Int = 8000,
    @SerializedName("camera_user") val cameraUser: String = "admin",
    @SerializedName("camera_credentials_configured") val cameraCredentialsConfigured: Boolean = false,
    @SerializedName("doorbell_online") val doorbellOnline: Boolean = true,
    @SerializedName("doorbell_status_message") val doorbellStatusMessage: String = ""
)

data class OnboardingQrResponse(
    val status: String,
    val payload: Map<String, Any?> = emptyMap(),
    @SerializedName("payload_json") val payloadJson: String = "",
    @SerializedName("qr_image_url") val qrImageUrl: String = ""
)

data class AppVersionResponse(
    val version: String,
    @SerializedName("version_code") val versionCode: Int = 1,
    @SerializedName("min_supported_version") val minSupportedVersion: String = "1.0.0",
    @SerializedName("update_available") val updateAvailable: Boolean = false,
    val changelog: String = ""
)

data class ShellyWebhookStatusResponse(
    val status: String = "active",
    val detector: String = "",
    @SerializedName("webhook_url_lan") val webhookUrlLan: String = "",
    @SerializedName("webhook_url_wan") val webhookUrlWan: String = "",
    val instruction: String = ""
)

// ──────────────────────────────────────────────────────────────────
//  WebSocket event model
// ──────────────────────────────────────────────────────────────────

/** Incoming WebSocket/FCM event envelope */
data class DoorbellEvent(
    val event: String,
    val data: Map<String, Any?> = emptyMap()
) {
    val callId: String?
        get() = data["call_id"] as? String

    val snapshotUrl: String?
        get() = data["snapshot_url"] as? String

    val answeredBy: String?
        get() = data["answered_by"] as? String

    val userId: String?
        get() = data["user_id"] as? String

    val userName: String?
        get() = data["user_name"] as? String

    val deviceName: String?
        get() = data["device_name"] as? String

    val timestamp: String?
        get() = data["timestamp"] as? String

    companion object {
        const val CALL_RINGING = "CALL_RINGING"
        const val CALL_CLAIMED = "CALL_CLAIMED"
        const val CALL_MISSED = "CALL_MISSED"
        const val CALL_ENDED = "CALL_ENDED"
        const val USER_JOINED = "USER_JOINED"
    }
}

// ──────────────────────────────────────────────────────────────────
//  Discovered doorbell (mDNS)
// ──────────────────────────────────────────────────────────────────

/** Represents a Foxway doorbell discovered on the LAN via mDNS. */
data class DiscoveredDoorbell(
    val hostAddress: String,
    val port: Int,
    val lanUrl: String,
    val wanUrl: String,
    val go2rtcLan: String,
    val go2rtcWan: String,
    val version: String = ""
)

// ──────────────────────────────────────────────────────────────────
//  Smart AI Detection & Storage Settings Models
// ──────────────────────────────────────────────────────────────────

data class DetectionSettingsDto(
    @SerializedName("detection_enabled") val detectionEnabled: Boolean = true,
    @SerializedName("record_person") val recordPerson: Boolean = true,
    @SerializedName("record_vehicle") val recordVehicle: Boolean = true,
    @SerializedName("record_animal") val recordAnimal: Boolean = true,
    @SerializedName("record_motion") val recordMotion: Boolean = false,
    @SerializedName("notify_person") val notifyPerson: Boolean = true,
    @SerializedName("notify_vehicle") val notifyVehicle: Boolean = true,
    @SerializedName("notify_animal") val notifyAnimal: Boolean = true,
    @SerializedName("notify_motion") val notifyMotion: Boolean = false,
    @SerializedName("quiet_hours_enabled") val quietHoursEnabled: Boolean = false,
    @SerializedName("quiet_hours_start") val quietHoursStart: String = "23:00",
    @SerializedName("quiet_hours_end") val quietHoursEnd: String = "07:00",
    @SerializedName("activity_zone_top_ignore_percent") val activityZoneTopIgnorePercent: Int = 20,
    @SerializedName("motion_sensitivity") val motionSensitivity: Double = 1.5
)

data class StorageStatsDto(
    @SerializedName("retention_days") val retentionDays: Int = 30,
    @SerializedName("max_storage_gb") val maxStorageGb: Double = 10.0,
    @SerializedName("used_mb") val usedMb: Double = 0.0,
    @SerializedName("max_mb") val maxMb: Double = 10240.0,
    @SerializedName("total_clips") val totalClips: Int = 0,
    @SerializedName("usage_percent") val usagePercent: Double = 0.0
)

data class StorageSettingsUpdate(
    @SerializedName("retention_days") val retentionDays: Int,
    @SerializedName("max_storage_gb") val maxStorageGb: Double
)

data class ManualRecordStart(
    @SerializedName("user_name") val userName: String,
    @SerializedName("stream_name") val streamName: String = "deurbel"
)

data class DoorbellHealthDto(
    val online: Boolean,
    @SerializedName("doorbell_ip") val doorbellIp: String,
    @SerializedName("last_seen_seconds_ago") val lastSeenSecondsAgo: Int
)

// ──────────────────────────────────────────────────────────────────
//  Endpoint configuration (persisted after pairing)
// ──────────────────────────────────────────────────────────────────

/** All URLs needed by the app, resolved after pairing. */
data class EndpointConfig(
    val lanApiUrl: String,
    val wanApiUrl: String,
    val go2rtcLanWs: String,
    val go2rtcWanWs: String
) {
    companion object {
        val DEFAULT = EndpointConfig(
            lanApiUrl = "http://192.168.2.1:8090",
            wanApiUrl = "https://deurbel.foxway.nl",
            go2rtcLanWs = "ws://192.168.2.1:1984",
            go2rtcWanWs = "wss://deurbel.foxway.nl/api/ws"
        )
    }
}

