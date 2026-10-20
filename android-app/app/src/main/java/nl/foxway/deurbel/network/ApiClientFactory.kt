package nl.foxway.deurbel.network

import android.util.Log
import com.google.gson.Gson
import nl.foxway.deurbel.data.models.EndpointConfig
import nl.foxway.deurbel.data.PreferencesManager
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Factory for creating Retrofit [FoxwayApi] instances.
 *
 * Supports dynamic base URL switching (LAN ↔ WAN) via [EndpointResolver].
 * Each call to [createApi] builds a new Retrofit instance pointing at
 * the currently resolved base URL.
 */
object ApiClientFactory {

    private const val TAG = "ApiClientFactory"

    private val loggingInterceptor = HttpLoggingInterceptor { message ->
        Log.d(TAG, message)
    }.apply {
        level = HttpLoggingInterceptor.Level.BODY
    }

    private val okHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .addInterceptor(loggingInterceptor)
        .retryOnConnectionFailure(true)
        .build()

    /**
     * Create a [FoxwayApi] targeting the given base URL.
     */
    fun createApi(baseUrl: String): FoxwayApi {
        val url = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        return Retrofit.Builder()
            .baseUrl(url)
            .client(okHttpClient)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(FoxwayApi::class.java)
    }

    /**
     * Create a [FoxwayApi] using the currently resolved endpoints.
     */
    fun createApi(resolver: EndpointResolver): FoxwayApi {
        val baseUrl = resolver.activeApiUrl.value.ifBlank {
            EndpointConfig.DEFAULT.lanApiUrl
        }
        return createApi(baseUrl)
    }
}
