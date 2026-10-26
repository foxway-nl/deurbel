package nl.foxway.deurbel.network

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import com.google.gson.Gson
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import nl.foxway.deurbel.data.PreferencesManager
import nl.foxway.deurbel.data.models.OnboardingBootstrapResponse
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.Inet4Address
import java.util.concurrent.TimeUnit

/**
 * 100% Dynamic Network Discovery & Auto-Heal for Foxway Doorbell Server.
 *
 * Scans the local /24 subnet over Wi-Fi on port 8090 (/api/v1/onboarding/bootstrap)
 * with a 350ms timeout per host in parallel. Discovers the server dynamically
 * without any hardcoded IP addresses or domains!
 */
class NetworkDiscovery(private val context: Context) {

    private val prefsManager = PreferencesManager(context)
    private val gson = Gson()

    private val probeClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(350, TimeUnit.MILLISECONDS)
        .readTimeout(350, TimeUnit.MILLISECONDS)
        .callTimeout(500, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    data class DiscoveryResult(
        val serverLanUrl: String,
        val serverWanUrl: String,
        val cameraDiscovered: Boolean,
        val cameraIp: String,
        val cameraConfigured: Boolean,
        val doorbellOnline: Boolean,
        val statusMessage: String,
        val bootstrapToken: String
    )

    /**
     * Determines the active Wi-Fi IPv4 subnet prefix (e.g. "192.168.1." or "10.0.0.").
     * Returns null if not connected to Wi-Fi.
     */
    fun getLocalWifiSubnetPrefix(): String? {
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return null
            val activeNetwork = cm.activeNetwork ?: return null
            val caps = cm.getNetworkCapabilities(activeNetwork) ?: return null

            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                return null
            }

            val linkProperties = cm.getLinkProperties(activeNetwork) ?: return null
            for (linkAddress in linkProperties.linkAddresses) {
                val address = linkAddress.address
                if (address is Inet4Address && !address.isLoopbackAddress && !address.isLinkLocalAddress) {
                    val hostAddress = address.hostAddress ?: continue
                    val lastDot = hostAddress.lastIndexOf('.')
                    if (lastDot > 0) {
                        return hostAddress.substring(0, lastDot + 1)
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error resolving local Wi-Fi subnet: ${e.message}")
        }
        return null
    }

    /**
     * Fast parallel asynchronous discovery of the doorbell server on the active Wi-Fi network.
     * Searches all 254 hosts on port 8090 concurrently and returns the first responding server.
     */
    suspend fun discoverServer(timeoutMs: Long = 1800): DiscoveryResult? = withContext(Dispatchers.IO) {
        // 1. Try cached endpoint first for sub-second instant reconnection
        val cachedConfig = prefsManager.getEndpointConfig()
        if (cachedConfig.lanApiUrl.isNotBlank()) {
            val cachedResult = probeHost(cachedConfig.lanApiUrl)
            if (cachedResult != null) {
                Log.i(TAG, "Instant reconnection via cached LAN URL: ${cachedConfig.lanApiUrl}")
                updateStoredEndpoints(cachedResult)
                return@withContext cachedResult
            }
        }

        // 2. Resolve local Wi-Fi subnet prefix
        val prefix = getLocalWifiSubnetPrefix()
        if (prefix.isNullOrBlank()) {
            Log.w(TAG, "No Wi-Fi subnet detected, attempting mDNS fallback")
            return@withContext discoverViaMdns()
        }

        Log.i(TAG, "Scanning active subnet ${prefix}0/24 on port 8090...")
        val foundDeferred = CompletableDeferred<DiscoveryResult?>()
        val parentJob = Job()
        val scanScope = CoroutineScope(Dispatchers.IO + parentJob)

        // Launch concurrent scan of all 254 subnet hosts
        for (i in 1..254) {
            val targetUrl = "http://$prefix$i:8090"
            scanScope.launch {
                if (foundDeferred.isCompleted) return@launch
                val result = probeHost(targetUrl)
                if (result != null && !foundDeferred.isCompleted) {
                    Log.i(TAG, "Server discovered at $targetUrl (Camera IP: ${result.cameraIp})")
                    foundDeferred.complete(result)
                    parentJob.cancel()
                }
            }
        }

        // Wait for first response or timeout
        val result = withTimeoutOrNull(timeoutMs) {
            try {
                foundDeferred.await()
            } catch (e: CancellationException) {
                null
            }
        }

        parentJob.cancel()

        if (result != null) {
            updateStoredEndpoints(result)
            return@withContext result
        }

        // 3. Fallback to mDNS discovery
        Log.i(TAG, "Subnet scan timed out, trying mDNS fallback...")
        discoverViaMdns()
    }

    /**
     * Probes an individual host URL on /api/v1/onboarding/bootstrap.
     */
    private suspend fun probeHost(baseUrl: String): DiscoveryResult? = withContext(Dispatchers.IO) {
        val cleanUrl = baseUrl.trimEnd('/')
        val request = Request.Builder()
            .url("$cleanUrl/api/v1/onboarding/bootstrap")
            .get()
            .build()

        try {
            probeClient.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    val bodyString = response.body?.string() ?: return@withContext null
                    val boot = gson.fromJson(bodyString, OnboardingBootstrapResponse::class.java)

                    val lanUrl = boot.serverLanUrl.ifBlank { boot.lanUrl.ifBlank { cleanUrl } }
                    val wanUrl = boot.serverWanUrl.ifBlank { boot.wanUrl }
                    val camIp = boot.cameraIp
                    val camDiscovered = boot.cameraDiscovered || camIp.isNotBlank()
                    val camConfigured = boot.cameraConfigured || boot.cameraCredentialsConfigured

                    return@withContext DiscoveryResult(
                        serverLanUrl = lanUrl,
                        serverWanUrl = wanUrl,
                        cameraDiscovered = camDiscovered,
                        cameraIp = camIp,
                        cameraConfigured = camConfigured,
                        doorbellOnline = boot.doorbellOnline,
                        statusMessage = boot.doorbellStatusMessage,
                        bootstrapToken = boot.bootstrapToken.ifBlank { boot.token }
                    )
                }
            }
        } catch (_: Exception) {
            // Connection failed or timed out — ignore and proceed with other hosts
        }
        return@withContext null
    }

    private suspend fun discoverViaMdns(): DiscoveryResult? {
        return try {
            val mdns = MdnsDiscovery(context)
            val discovered = mdns.discoverFirst()
            if (discovered != null && discovered.lanUrl.isNotBlank()) {
                probeHost(discovered.lanUrl)
            } else null
        } catch (e: Exception) {
            Log.w(TAG, "mDNS discovery failed: ${e.message}")
            null
        }
    }

    private suspend fun updateStoredEndpoints(result: DiscoveryResult) {
        try {
            prefsManager.updateEndpoints(
                lanUrl = result.serverLanUrl,
                wanUrl = result.serverWanUrl
            )
        } catch (e: Exception) {
            Log.w(TAG, "Failed updating stored endpoints: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "NetworkDiscovery"
    }
}
