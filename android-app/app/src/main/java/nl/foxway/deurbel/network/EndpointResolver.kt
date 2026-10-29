package nl.foxway.deurbel.network

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.PreferencesManager
import nl.foxway.deurbel.data.models.EndpointConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Smart Endpoint Resolver — automatic LAN detection with
 * dynamic auto-healing and seamless WAN fallback.
 *
 * Provides observable state so the UI can show LAN 🟢 / 5G 🌐.
 */
class EndpointResolver(
    private val preferencesManager: PreferencesManager
) {

    companion object {
        private const val TAG = "EndpointResolver"
        private const val LAN_PROBE_TIMEOUT_MS = 450L
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
        .callTimeout(LAN_PROBE_TIMEOUT_MS + 400L, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    /**
     * Probe the LAN endpoint and resolve the best API + WebRTC URLs.
     * If LAN fails on Wi-Fi, triggers auto-healing subnet discovery.
     */
    suspend fun resolve(): ResolvedEndpoints = withContext(Dispatchers.IO) {
        var config = preferencesManager.getEndpointConfig()

        val appCtx = FoxwayApp.instance
        val cm = appCtx.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        val network = cm?.activeNetwork
        val caps = cm?.getNetworkCapabilities(network)
        val isCellularOnly = caps != null &&
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
                !caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)

        if (isCellularOnly) {
            _connectionType.value = ConnectionType.WAN
            val baseUrl = config.wanApiUrl.ifBlank { "https://deurbel.foxway.nl" }.trimEnd('/')
            _activeApiUrl.value = baseUrl
            _activeGo2rtcWs.value = config.go2rtcWanWs.ifBlank { "wss://deurbel.foxway.nl/api/go2rtc/ws" }
            return@withContext ResolvedEndpoints(
                apiBaseUrl = baseUrl,
                go2rtcWsUrl = _activeGo2rtcWs.value,
                wsUrl = baseUrl.replace("http://", "ws://").replace("https://", "wss://"),
                isLan = false
            )
        }

        var isLan = false
        if (config.lanApiUrl.isNotBlank()) {
            isLan = try {
                val request = Request.Builder()
                    .url("${config.lanApiUrl.trimEnd('/')}$LAN_HEALTH_PATH")
                    .build()
                val response = probeClient.newCall(request).execute()
                val success = response.isSuccessful
                response.close()
                success
            } catch (e: Exception) {
                Log.d(TAG, "Cached LAN probe failed (${e.message})")
                false
            }
        }

        // Auto-heal: If LAN failed, run dynamic subnet discovery to find changed server IP
        if (!isLan) {
            try {
                val appCtx = nl.foxway.deurbel.FoxwayApp.instance
                val discovery = NetworkDiscovery(appCtx)
                val discovered = discovery.discoverServer(timeoutMs = 700)
                if (discovered != null && discovered.serverLanUrl.isNotBlank()) {
                    Log.i(TAG, "Auto-healed LAN server IP to: ${discovered.serverLanUrl}")
                    config = preferencesManager.getEndpointConfig()
                    isLan = true
                }
            } catch (e: Exception) {
                Log.d(TAG, "Subnet auto-heal skipped: ${e.message}")
            }
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
            val wanBase = config.wanApiUrl.ifBlank { "https://deurbel.foxway.nl" }.trimEnd('/')
            val wanWs = wanBase.replace("https://", "wss://").replace("http://", "ws://")
            val proxiedGo2rtc = "$wanWs/api/ws"
            _connectionType.value = ConnectionType.WAN
            _activeApiUrl.value = wanBase
            _activeGo2rtcWs.value = proxiedGo2rtc
            ResolvedEndpoints(
                apiBaseUrl = wanBase,
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
