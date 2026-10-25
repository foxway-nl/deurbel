package nl.foxway.deurbel.network

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import nl.foxway.deurbel.data.PreferencesManager
import nl.foxway.deurbel.data.models.EndpointConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Smart Endpoint Resolver — automatic sub-150ms LAN detection with
 * seamless WAN fallback.
 *
 * Per SPEC §4.4:
 *   - When `192.168.2.1:8090` responds within 150ms → use direct LAN URLs
 *   - Otherwise → use WAN URLs (`https://deurbel.foxway.nl`)
 *
 * Provides observable state so the UI can show LAN 🟢 / 5G 🌐.
 */
class EndpointResolver(
    private val preferencesManager: PreferencesManager
) {

    companion object {
        private const val TAG = "EndpointResolver"
        private const val LAN_PROBE_TIMEOUT_MS = 600L
        private const val LAN_HEALTH_PATH = "/api/v1/status"
    }

    /** Whether the device is currently on the home LAN. */
    enum class ConnectionType { LAN, WAN, UNKNOWN }

    private val _connectionType = MutableStateFlow(ConnectionType.UNKNOWN)
    val connectionType: StateFlow<ConnectionType> = _connectionType.asStateFlow()

    private val _activeApiUrl = MutableStateFlow("")
    val activeApiUrl: StateFlow<String> = _activeApiUrl.asStateFlow()

    private val _activeGo2rtcWs = MutableStateFlow("")
    val activeGo2rtcWs: StateFlow<String> = _activeGo2rtcWs.asStateFlow()

    private val probeClient = OkHttpClient.Builder()
        .connectTimeout(LAN_PROBE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(LAN_PROBE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .callTimeout(LAN_PROBE_TIMEOUT_MS + 500L, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    /**
     * Probe the LAN endpoint and resolve the best API + WebRTC URLs.
     *
     * Should be called:
     *   1. On app foreground / resume
     *   2. On network connectivity change
     *   3. Before each WebSocket reconnection
     *   4. When an incoming ring is received
     */
    suspend fun resolve(): ResolvedEndpoints = withContext(Dispatchers.IO) {
        val config = preferencesManager.getEndpointConfig()

        val isLan = try {
            val request = Request.Builder()
                .url("${config.lanApiUrl}$LAN_HEALTH_PATH")
                .build()
            val response = probeClient.newCall(request).execute()
            val success = response.isSuccessful
            response.close()
            success
        } catch (e: Exception) {
            Log.d(TAG, "LAN probe failed (${e.message}), using WAN")
            false
        }

        val result = if (isLan) {
            _connectionType.value = ConnectionType.LAN
            _activeApiUrl.value = config.lanApiUrl
            _activeGo2rtcWs.value = config.go2rtcLanWs
            ResolvedEndpoints(
                apiBaseUrl = config.lanApiUrl,
                go2rtcWsUrl = config.go2rtcLanWs,
                wsUrl = config.lanApiUrl.replace("http://", "ws://").replace("https://", "wss://"),
                isLan = true
            )
        } else {
            val wanWs = config.wanApiUrl.replace("https://", "wss://").replace("http://", "ws://")
            val proxiedGo2rtc = "$wanWs/api/ws"
            _connectionType.value = ConnectionType.WAN
            _activeApiUrl.value = config.wanApiUrl
            _activeGo2rtcWs.value = proxiedGo2rtc
            ResolvedEndpoints(
                apiBaseUrl = config.wanApiUrl,
                go2rtcWsUrl = proxiedGo2rtc,
                wsUrl = wanWs,
                isLan = false
            )
        }

        Log.i(TAG, "Resolved: ${if (result.isLan) "LAN" else "WAN"} → ${result.apiBaseUrl}")
        result
    }

    /**
     * Resolve with explicit LAN/WAN endpoints (used during initial pairing
     * when the config may not be in DataStore yet).
     */
    fun resolveWith(config: EndpointConfig, isLan: Boolean): ResolvedEndpoints {
        _connectionType.value = if (isLan) ConnectionType.LAN else ConnectionType.WAN
        val baseUrl = if (isLan) config.lanApiUrl else config.wanApiUrl
        _activeApiUrl.value = baseUrl
        _activeGo2rtcWs.value = if (isLan) config.go2rtcLanWs else config.go2rtcWanWs
        return ResolvedEndpoints(
            apiBaseUrl = baseUrl,
            go2rtcWsUrl = if (isLan) config.go2rtcLanWs else config.go2rtcWanWs,
            wsUrl = baseUrl.replace("http://", "ws://").replace("https://", "wss://"),
            isLan = isLan
        )
    }
}

/**
 * Immutable snapshot of the resolved endpoint URLs.
 */
data class ResolvedEndpoints(
    val apiBaseUrl: String,
    val go2rtcWsUrl: String,
    val wsUrl: String,
    val isLan: Boolean
) {
    /** Full WebSocket URL for the persistent push channel. */
    fun wsUrlForUser(userId: String): String = "$wsUrl/ws/$userId"
}
