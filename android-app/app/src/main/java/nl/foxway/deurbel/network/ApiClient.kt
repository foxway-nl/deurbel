package nl.foxway.deurbel.network

import android.content.Context
import nl.foxway.deurbel.FoxwayApp
import okhttp3.OkHttpClient

/**
 * Global dynamic ApiClient delegating directly to [ConnectionManager.activeBaseUrl].
 * Guarantees that every API call automatically resolves to https://deurbel.foxway.nl
 * when on 5G/WAN.
 */
object ApiClient {

    /**
     * Get active [FoxwayApi] instance dynamically matching current LAN/5G state.
     */
    val api: FoxwayApi
        get() = ApiClientFactory.createApi()

    /**
     * Get the active base URL currently in use by the application.
     */
    val activeBaseUrl: String
        get() = ConnectionManager.getInstance().activeBaseUrl.value.ifBlank { ApiClientFactory.WAN_BASE_URL }

    /**
     * Shared OkHttpClient with dynamic WAN URL rewriting.
     */
    val okHttpClient: OkHttpClient
        get() = ApiClientFactory.okHttpClient
}
