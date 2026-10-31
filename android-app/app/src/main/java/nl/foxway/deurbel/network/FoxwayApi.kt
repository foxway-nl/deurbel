package nl.foxway.deurbel.network

import nl.foxway.deurbel.data.models.*
import retrofit2.Response
import retrofit2.http.*

/**
 * Retrofit API interface for the Foxway Doorbell backend.
 *
 * Base URL is set dynamically by the [EndpointResolver] and may switch
 * between LAN and WAN endpoints at runtime.
 */
interface FoxwayApi {

    // ── Pairing ──────────────────────────────────────────────────────

    @POST("/api/v1/pair")
    suspend fun pair(@Body request: PairRequest): Response<PairResponse>

    // ── Call state ───────────────────────────────────────────────────

    @GET("/api/v1/status")
    suspend fun getStatus(): Response<CallStatus>

    @POST("/api/v1/ring")
    suspend fun ring(): Response<RingResponse>

    @POST("/api/v1/call/{callId}/claim")
    suspend fun claim(
        @Path("callId") callId: String,
        @Body request: ClaimRequest
    ): Response<ClaimResponse>

    @POST("/api/v1/call/{callId}/hangup")
    suspend fun hangup(@Path("callId") callId: String): Response<Map<String, String>>

    @POST("/api/v1/call/{callId}/quick-reply")
    suspend fun quickReply(
        @Path("callId") callId: String,
        @Body request: QuickReplyRequest
    ): Response<Map<String, String>>

    // ── FCM token ────────────────────────────────────────────────────

    @PUT("/api/v1/devices/{userId}/fcm-token")
    suspend fun updateFcmToken(
        @Path("userId") userId: String,
        @Body request: FcmTokenUpdate
    ): Response<Map<String, String>>

    // ── Clips ────────────────────────────────────────────────────────

    @GET("/api/v1/clips")
    suspend fun getClips(
        @Query("limit") limit: Int = 100,
        @Query("offset") offset: Int = 0
    ): Response<List<ClipItem>>

    @GET("/api/v1/clips/{clipId}")
    suspend fun getClipDetail(
        @Path("clipId") clipId: String
    ): Response<ClipItem>

    @POST("/api/v1/clips/{clipId}/trim")
    suspend fun trimClip(
        @Path("clipId") clipId: String,
        @Body request: TrimRequest
    ): Response<TrimResponse>

    @PUT("/api/v1/clips/{clipId}/star")
    suspend fun starClip(
        @Path("clipId") clipId: String,
        @Body request: Map<String, Boolean>
    ): Response<Map<String, Any>>

    @DELETE("/api/v1/clips/{clipId}")
    suspend fun deleteClip(@Path("clipId") clipId: String): Response<Map<String, String>>

    // ── Users / Family members ───────────────────────────────────────

    @GET("/api/v1/users")
    suspend fun getUsers(): Response<List<UserInfo>>

    @DELETE("/api/v1/users/{userId}")
    suspend fun deleteUser(@Path("userId") userId: String): Response<Map<String, String>>

    // ── Hardware Test ────────────────────────────────────────────────

    @POST("/api/v1/test/audio")
    suspend fun testAudio(): Response<Map<String, String>>

    @POST("/api/v1/test/audio-loopback")
    suspend fun testAudioLoopback(): Response<AudioLoopbackResponse>

    @POST("/api/v1/audio/speak")
    suspend fun speakAudio(
        @Query("reply_id") replyId: String? = null
    ): Response<Map<String, Any>>

    // ── WebRTC WHEP Proxy ────────────────────────────────────────────

    @POST("/api/v1/webrtc")
    @Headers("Content-Type: application/sdp")
    suspend fun webrtcProxy(
        @Query("src") src: String = "deurbel",
        @Body offerSdp: okhttp3.RequestBody
    ): Response<okhttp3.ResponseBody>

    // ── Manual Live Recording ────────────────────────────────────────

    @POST("/api/v1/recordings/manual/start")
    suspend fun startManualRecording(
        @Body request: ManualRecordStart
    ): Response<Map<String, Any>>

    @POST("/api/v1/recordings/manual/stop")
    suspend fun stopManualRecording(
        @Query("user_name") userName: String
    ): Response<Map<String, Any>>

    @GET("/api/v1/recordings/manual/status")
    suspend fun getManualRecordingStatus(): Response<Map<String, Any>>

    // ── Smart AI Detection Settings ──────────────────────────────────

    @GET("/api/v1/settings/detection")
    suspend fun getDetectionSettings(): Response<DetectionSettingsDto>

    @PUT("/api/v1/settings/detection")
    suspend fun updateDetectionSettings(
        @Body request: DetectionSettingsDto
    ): Response<DetectionSettingsDto>

    // ── Storage & Retention Settings ─────────────────────────────────

    @GET("/api/v1/settings/storage")
    suspend fun getStorageSettings(): Response<StorageStatsDto>

    @PUT("/api/v1/settings/storage")
    suspend fun updateStorageSettings(
        @Body request: StorageSettingsUpdate
    ): Response<StorageStatsDto>

    // ── Doorbell Health ──────────────────────────────────────────────

    @GET("/api/v1/doorbell/health")
    suspend fun getDoorbellHealth(): Response<DoorbellHealthDto>

    // ── Onboarding & QR ──────────────────────────────────────────────

    @GET("/api/v1/onboarding/bootstrap")
    suspend fun getOnboardingBootstrap(): Response<OnboardingBootstrapResponse>

    @GET("/api/v1/onboarding/qr")
    suspend fun getOnboardingQr(): Response<OnboardingQrResponse>

    // ── App Updates & Version ─────────────────────────────────────────

    @GET("/api/v1/app/version")
    suspend fun getAppVersion(): Response<AppVersionResponse>

    // ── Shelly Plus Uni Webhook ───────────────────────────────────────

    @GET("/api/v1/webhook/shelly-ring/status")
    suspend fun getShellyWebhookStatus(): Response<ShellyWebhookStatusResponse>

    @POST("/api/v1/webhook/shelly-ring")
    suspend fun triggerShellyWebhook(@Query("force") force: Boolean = true): Response<Map<String, Any>>

    // ── Live Stream Anti-Freeze Restart ──────────────────────────────

    @POST("/api/v1/live/restart")
    suspend fun restartLiveStream(): Response<Map<String, Any>>

    // ── Junk Clips Cleanup ────────────────────────────────────────────

    @POST("/api/v1/clips/cleanup-unclassified")
    suspend fun cleanupUnclassifiedClips(
        @Query("include_unstarred_vehicles") includeVehicles: Boolean = true
    ): Response<Map<String, Any>>

    // ── Event Queue Fallback (Task 6) ─────────────────────────────────

    @GET("/api/v1/events")
    suspend fun getEvents(
        @Query("since_id") sinceId: Long = 0,
        @Query("limit") limit: Int = 50
    ): Response<EventsResponse>

    // ── Camera Credentials ───────────────────────────────────────────

    @PUT("/api/v1/settings/camera")
    suspend fun updateCameraSettings(@Body body: CameraSettingsUpdate): Response<Map<String, Any>>

    // ── Client Status Dump & Diagnostics ──────────────────────────────

    @POST("/api/v1/logs/client")
    suspend fun postClientStatusDump(
        @Body body: Map<String, Any>
    ): Response<Map<String, Any>>

    // ── Admin Auto-Login Token ────────────────────────────────────────

    @GET("/api/v1/admin/auth-token")
    suspend fun getAdminAuthToken(): Response<Map<String, String>>
}

