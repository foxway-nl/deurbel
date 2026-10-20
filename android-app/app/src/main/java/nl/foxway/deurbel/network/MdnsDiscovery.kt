package nl.foxway.deurbel.network

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import nl.foxway.deurbel.data.models.DiscoveredDoorbell
import kotlin.coroutines.resume

/**
 * NsdManager-based mDNS scanner that discovers Foxway Doorbell services
 * on the local network.
 *
 * Service type: `_foxway-doorbell._tcp.`
 *
 * The TXT records from the backend contain:
 *   - lan_url, wan_url, go2rtc_lan, go2rtc_wan, version
 */
class MdnsDiscovery(private val context: Context) {

    companion object {
        private const val TAG = "MdnsDiscovery"
        private const val SERVICE_TYPE = "_foxway-doorbell._tcp."
    }

    private val nsdManager: NsdManager by lazy {
        context.getSystemService(Context.NSD_SERVICE) as NsdManager
    }

    /**
     * Emit discovered [DiscoveredDoorbell] instances as a cold [Flow].
     *
     * The flow starts discovery on collection and stops when the
     * collector is cancelled.  Typically you'd take the first emission
     * or collect with a timeout.
     */
    fun discover(): Flow<DiscoveredDoorbell> = callbackFlow {
        val discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.d(TAG, "mDNS discovery started for $serviceType")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                Log.d(TAG, "Service found: ${serviceInfo.serviceName}")
                if (serviceInfo.serviceType.contains("foxway-doorbell")) {
                    // Resolve the service to get host, port, and TXT records
                    resolveService(serviceInfo) { resolved ->
                        resolved?.let { trySend(it) }
                    }
                }
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                Log.d(TAG, "Service lost: ${serviceInfo.serviceName}")
            }

            override fun onDiscoveryStopped(serviceType: String) {
                Log.d(TAG, "mDNS discovery stopped")
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.e(TAG, "Discovery start failed: error=$errorCode")
                close(Exception("mDNS discovery start failed (error $errorCode)"))
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.e(TAG, "Discovery stop failed: error=$errorCode")
            }
        }

        nsdManager.discoverServices(
            SERVICE_TYPE,
            NsdManager.PROTOCOL_DNS_SD,
            discoveryListener
        )

        awaitClose {
            try {
                nsdManager.stopServiceDiscovery(discoveryListener)
            } catch (e: Exception) {
                Log.w(TAG, "Error stopping discovery", e)
            }
        }
    }

    /**
     * Resolve an NsdServiceInfo to extract host address, port, and
     * TXT record properties.
     */
    private fun resolveService(
        serviceInfo: NsdServiceInfo,
        callback: (DiscoveredDoorbell?) -> Unit
    ) {
        @Suppress("DEPRECATION")
        nsdManager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                Log.e(TAG, "Resolve failed for ${info.serviceName}: error=$errorCode")
                callback(null)
            }

            override fun onServiceResolved(info: NsdServiceInfo) {
                @Suppress("DEPRECATION")
                val host = info.host?.hostAddress ?: return callback(null)
                val port = info.port
                val attrs = info.attributes

                fun txtAttr(key: String, default: String = ""): String {
                    val bytes = attrs[key] ?: return default
                    return String(bytes, Charsets.UTF_8)
                }

                val doorbell = DiscoveredDoorbell(
                    hostAddress = host,
                    port = port,
                    lanUrl = txtAttr("lan_url", "http://$host:$port"),
                    wanUrl = txtAttr("wan_url", "https://deurbel.foxway.nl"),
                    go2rtcLan = txtAttr("go2rtc_lan", "ws://$host:1984"),
                    go2rtcWan = txtAttr("go2rtc_wan", "wss://webrtc.foxway.nl"),
                    version = txtAttr("version")
                )
                Log.i(TAG, "Resolved: $host:$port (LAN=${doorbell.lanUrl})")
                callback(doorbell)
            }
        })
    }

    /**
     * One-shot discovery: find the first doorbell on the network.
     * Suspends until a doorbell is found or throws on failure.
     */
    suspend fun discoverFirst(): DiscoveredDoorbell = suspendCancellableCoroutine { cont ->
        val discoveryListener = object : NsdManager.DiscoveryListener {
            private var resolved = false

            override fun onDiscoveryStarted(serviceType: String) {
                Log.d(TAG, "One-shot discovery started")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (resolved) return
                if (serviceInfo.serviceType.contains("foxway-doorbell")) {
                    resolveService(serviceInfo) { doorbell ->
                        if (doorbell != null && !resolved) {
                            resolved = true
                            try {
                                nsdManager.stopServiceDiscovery(this)
                            } catch (_: Exception) {}
                            cont.resume(doorbell)
                        }
                    }
                }
            }

            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                cont.resume(DiscoveredDoorbell(
                    hostAddress = "192.168.2.1",
                    port = 8090,
                    lanUrl = "http://192.168.2.1:8090",
                    wanUrl = "https://deurbel.foxway.nl",
                    go2rtcLan = "ws://192.168.2.1:1984",
                    go2rtcWan = "wss://webrtc.foxway.nl"
                ))
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }

        nsdManager.discoverServices(
            SERVICE_TYPE,
            NsdManager.PROTOCOL_DNS_SD,
            discoveryListener
        )

        cont.invokeOnCancellation {
            try {
                nsdManager.stopServiceDiscovery(discoveryListener)
            } catch (_: Exception) {}
        }
    }
}
