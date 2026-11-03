package nl.foxway.deurbel.network

import android.util.Log
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.TypeAdapter
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import com.google.gson.stream.JsonWriter
import nl.foxway.deurbel.data.models.EndpointConfig
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Factory for creating Retrofit [FoxwayApi] instances.
 *
 * Supports dynamic base URL switching (LAN ↔ WAN) via [ConnectionManager] and [EndpointResolver].
 * Includes [dynamicWanInterceptor] ensuring any request on WAN/5G automatically resolves
 * to https://deurbel.foxway.nl.
 */
object ApiClientFactory {

    private const val TAG = "ApiClientFactory"
    const val WAN_BASE_URL = "https://deurbel.foxway.nl"

    private val loggingInterceptor = HttpLoggingInterceptor { message ->
        Log.d(TAG, message)
    }.apply {
        level = HttpLoggingInterceptor.Level.BODY
    }

    private val dynamicWanInterceptor = Interceptor { chain ->
        var request = chain.request()
        try {
            val cm = ConnectionManager.getInstance()
            val isWan = cm.isWanMode.value
            val targetBase = if (isWan) WAN_BASE_URL else cm.activeBaseUrl.value.ifBlank { WAN_BASE_URL }
            val parsedTarget = targetBase.toHttpUrlOrNull()

            if (parsedTarget != null) {
                val reqUrl = request.url
                val isReqLocal = reqUrl.host.startsWith("192.168.") ||
                        reqUrl.host.startsWith("10.") ||
                        reqUrl.host.startsWith("172.") ||
                        reqUrl.host == "localhost" ||
                        reqUrl.host == "127.0.0.1"

                if (isWan && isReqLocal) {
                    val rewritten = reqUrl.newBuilder()
                        .scheme(parsedTarget.scheme)
                        .host(parsedTarget.host)
                        .port(parsedTarget.port)
                        .build()
                    Log.i(TAG, "DynamicWanInterceptor: Rewriting $reqUrl -> $rewritten")
                    request = request.newBuilder().url(rewritten).build()
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error in dynamic routing interceptor: ${e.message}")
        }
        chain.proceed(request)
    }

    val lenientGson: Gson = GsonBuilder()
        .registerTypeAdapter(Boolean::class.java, LenientBooleanAdapter())
        .registerTypeAdapter(java.lang.Boolean::class.java, LenientBooleanAdapter())
        .create()

    val okHttpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .addInterceptor(dynamicWanInterceptor)
        .addInterceptor(loggingInterceptor)
        .retryOnConnectionFailure(true)
        .build()

    /**
     * Create a [FoxwayApi] targeting the active dynamic base URL from [ConnectionManager].
     */
    fun createApi(): FoxwayApi {
        val cm = ConnectionManager.getInstance()
        val isWan = cm.isWanMode.value
        val baseUrl = if (isWan) WAN_BASE_URL else cm.activeBaseUrl.value.ifBlank { WAN_BASE_URL }
        return createApi(baseUrl)
    }

    /**
     * Create a [FoxwayApi] targeting the given base URL.
     */
    fun createApi(baseUrl: String?): FoxwayApi {
        val cm = ConnectionManager.getInstance()
        val isWan = cm.isWanMode.value
        val rawUrl = if (isWan) {
            WAN_BASE_URL
        } else if (baseUrl.isNullOrBlank()) {
            cm.activeBaseUrl.value.ifBlank { WAN_BASE_URL }
        } else {
            baseUrl
        }
        val url = if (rawUrl.endsWith("/")) rawUrl else "$rawUrl/"
        return Retrofit.Builder()
            .baseUrl(url)
            .client(okHttpClient)
            .addConverterFactory(GsonConverterFactory.create(lenientGson))
            .build()
            .create(FoxwayApi::class.java)
    }

    private class LenientBooleanAdapter : TypeAdapter<Boolean>() {
        override fun write(out: JsonWriter, value: Boolean?) {
            if (value == null) {
                out.nullValue()
            } else {
                out.value(value)
            }
        }

        override fun read(reader: JsonReader): Boolean {
            return when (reader.peek()) {
                JsonToken.BOOLEAN -> reader.nextBoolean()
                JsonToken.NUMBER -> reader.nextInt() != 0
                JsonToken.STRING -> {
                    val str = reader.nextString().trim()
                    str.equals("true", ignoreCase = true) || str == "1"
                }
                JsonToken.NULL -> {
                    reader.nextNull()
                    false
                }
                else -> {
                    reader.skipValue()
                    false
                }
            }
        }
    }

    /**
     * Create a [FoxwayApi] using the currently resolved endpoints.
     */
    fun createApi(resolver: EndpointResolver): FoxwayApi {
        val cm = ConnectionManager.getInstance()
        val isWan = cm.isWanMode.value
        val baseUrl = if (isWan) {
            WAN_BASE_URL
        } else {
            val cmUrl = cm.activeBaseUrl.value
            if (cmUrl.isNotBlank()) cmUrl else resolver.activeApiUrl.value.ifBlank { WAN_BASE_URL }
        }
        return createApi(baseUrl)
    }
}
