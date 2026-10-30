package nl.foxway.deurbel.network

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.data.PreferencesManager
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Reactive Connection Manager.
 *
 * Monitors real-time network state via ConnectivityManager.NetworkCallback:
 *  - Realtime StateFlow<String> activeBaseUrl
 *  - Realtime StateFlow<Boolean> isWanMode
 * Automatically falls back to WAN ("https://deurbel.foxway.nl") when on 5G/Cellular
 * or when the 300ms LAN ping fails.
 */
class ConnectionManager private constructor(private val context: Context) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val prefs: PreferencesManager = PreferencesManager(context)

    private val _lanUrl = MutableStateFlow("")
    val lanUrl: StateFlow<String> = _lanUrl.asStateFlow()

    private val _wanUrl = MutableStateFlow("https://deurbel.foxway.nl")
    val wanUrl: StateFlow<String> = _wanUrl.asStateFlow()

    private val _activeBaseUrl = MutableStateFlow("https://deurbel.foxway.nl")
    val activeBaseUrl: StateFlow<String> = _activeBaseUrl.asStateFlow()

    private val _isWanMode = MutableStateFlow(true)
    val isWanMode: StateFlow<Boolean> = _isWanMode.asStateFlow()

    private val pingClient = OkHttpClient.Builder()
        .connectTimeout(300, TimeUnit.MILLISECONDS)
        .readTimeout(300, TimeUnit.MILLISECONDS)
        .callTimeout(400, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    init {
        // Load initial URLs from preferences
        scope.launch {
            val endpointConfig = prefs.getEndpointConfig()
            val lan = endpointConfig.lanApiUrl.ifBlank { "" }
            val wan = endpointConfig.wanApiUrl.ifBlank { "https://deurbel.foxway.nl" }
            _lanUrl.value = lan
            _wanUrl.value = wan
            refreshRoute()
        }

        registerNetworkCallback()
    }

    private fun registerNetworkCallback() {
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return
            val request = NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build()

            cm.registerNetworkCallback(request, object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    Log.d(TAG, "Network available, refreshing route")
                    refreshRoute()
                }

                override fun onLost(network: Network) {
                    Log.d(TAG, "Network lost, refreshing route")
                    refreshRoute()
                }

                override fun onCapabilitiesChanged(network: Network, networkCapabilities: NetworkCapabilities) {
                    Log.d(TAG, "Network capabilities changed, refreshing route")
                    refreshRoute()
                }
            })
        } catch (e: Exception) {
            Log.w(TAG, "Failed registering network callback: ${e.message}")
        }
    }

    /**
     * Update known URLs persistently (e.g. from discovery or pairing).
     */
    fun updateEndpoints(lan: String, wan: String) {
        if (lan.isNotBlank()) _lanUrl.value = lan
        if (wan.isNotBlank()) _wanUrl.value = wan
        scope.launch {
            prefs.updateEndpoints(lanUrl = lan, wanUrl = wan)
            refreshRoute()
        }
    }

    /**
     * Actively probe the route and update activeBaseUrl + isWanMode.
     */
    fun refreshRoute() {
        scope.launch {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val network = cm?.activeNetwork
            val caps = cm?.getNetworkCapabilities(network)

            val currentWan = _wanUrl.value.ifBlank { "https://deurbel.foxway.nl" }.trimEnd('/')
            val currentLan = _lanUrl.value.trimEnd('/')

            val isWifi = caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
            if (!isWifi) {
                // If there is NO Wi-Fi active (4G/5G Cellular), lock immediately and permanently to WAN! (Taak 7)
                if (!_isWanMode.value || _activeBaseUrl.value != currentWan) {
                    Log.i(TAG, "No Wi-Fi active (Cellular/5G): Locking route to WAN ($currentWan)")
                    _isWanMode.value = true
                    _activeBaseUrl.value = currentWan
                }
                return@launch
            }

            var useLan = false
            if (isWifi && currentLan.isNotBlank()) {
                useLan = pingLanHost(currentLan)
            }

            if (useLan) {
                if (_isWanMode.value || _activeBaseUrl.value != currentLan) {
                    Log.i(TAG, "Switching route to LAN: $currentLan")
                    _isWanMode.value = false
                    _activeBaseUrl.value = currentLan
                }
            } else {
                if (!_isWanMode.value || _activeBaseUrl.value != currentWan) {
                    Log.i(TAG, "Switching route to WAN (5G/Off-site): $currentWan")
                    _isWanMode.value = true
                    _activeBaseUrl.value = currentWan
                }
            }
        }
    }

    private fun isConnectedToWifi(): Boolean {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return false
            val network = cm.activeNetwork ?: return false
            val caps = cm.getNetworkCapabilities(network) ?: return false
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
        } catch (e: Exception) {
            false
        }
    }

    private suspend fun pingLanHost(lanBaseUrl: String): Boolean = withContext(Dispatchers.IO) {
        val url = "$lanBaseUrl/api/v1/status"
        try {
            val req = Request.Builder().url(url).get().build()
            pingClient.newCall(req).execute().use { response ->
                response.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }

    fun getEffectiveBaseUrl(): String {
        return if (_isWanMode.value) {
            _wanUrl.value.ifBlank { "https://deurbel.foxway.nl" }.trimEnd('/')
        } else {
            _activeBaseUrl.value.ifBlank { "https://deurbel.foxway.nl" }.trimEnd('/')
        }
    }

    fun getEffectiveWsUrl(path: String = ""): String {
        val base = getEffectiveBaseUrl()
        val wsBase = base.replace("https://", "wss://").replace("http://", "ws://").trimEnd('/')
        val cleanPath = if (path.isEmpty()) "" else if (path.startsWith("/")) path else "/$path"
        return "$wsBase$cleanPath"
    }

    companion object {
        private const val TAG = "ConnectionManager"

        @Volatile
        private var instance: ConnectionManager? = null

        fun getInstance(context: Context = FoxwayApp.instance): ConnectionManager {
            return instance ?: synchronized(this) {
                instance ?: ConnectionManager(context.applicationContext).also { instance = it }
            }
        }
    }
}
