package nl.foxway.deurbel.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import nl.foxway.deurbel.data.models.EndpointConfig

/**
 * DataStore-backed persistence for authentication state, user profile,
 * and endpoint configuration.
 */

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "foxway_prefs")

class PreferencesManager(private val context: Context) {

    // ── Keys ─────────────────────────────────────────────────────────
    private object Keys {
        val IS_PAIRED = booleanPreferencesKey("is_paired")
        val USER_ID = stringPreferencesKey("user_id")
        val USER_NAME = stringPreferencesKey("user_name")
        val DEVICE_NAME = stringPreferencesKey("device_name")
        val JWT_TOKEN = stringPreferencesKey("jwt_token")
        val FCM_TOKEN = stringPreferencesKey("fcm_token")
        val LAN_API_URL = stringPreferencesKey("lan_api_url")
        val WAN_API_URL = stringPreferencesKey("wan_api_url")
        val GO2RTC_LAN_WS = stringPreferencesKey("go2rtc_lan_ws")
        val GO2RTC_WAN_WS = stringPreferencesKey("go2rtc_wan_ws")
        val ONBOARDING_COMPLETE = booleanPreferencesKey("onboarding_complete")
    }

    // ── Pairing state ────────────────────────────────────────────────

    val isPaired: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[Keys.IS_PAIRED] ?: false
    }

    val isOnboardingComplete: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[Keys.ONBOARDING_COMPLETE] ?: false
    }

    suspend fun isPairedSync(): Boolean =
        context.dataStore.data.first()[Keys.IS_PAIRED] ?: false

    // ── User profile ─────────────────────────────────────────────────

    val userId: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[Keys.USER_ID] ?: ""
    }

    val userName: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[Keys.USER_NAME] ?: ""
    }

    val jwtToken: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[Keys.JWT_TOKEN] ?: ""
    }

    suspend fun getUserId(): String =
        context.dataStore.data.first()[Keys.USER_ID] ?: ""

    suspend fun getUserName(): String =
        context.dataStore.data.first()[Keys.USER_NAME] ?: ""

    suspend fun getJwtToken(): String =
        context.dataStore.data.first()[Keys.JWT_TOKEN] ?: ""

    suspend fun getFcmToken(): String =
        context.dataStore.data.first()[Keys.FCM_TOKEN] ?: ""

    // ── Endpoint config ──────────────────────────────────────────────

    suspend fun getEndpointConfig(): EndpointConfig {
        val prefs = context.dataStore.data.first()
        return EndpointConfig(
            lanApiUrl = prefs[Keys.LAN_API_URL] ?: EndpointConfig.DEFAULT.lanApiUrl,
            wanApiUrl = prefs[Keys.WAN_API_URL] ?: EndpointConfig.DEFAULT.wanApiUrl,
            go2rtcLanWs = prefs[Keys.GO2RTC_LAN_WS] ?: EndpointConfig.DEFAULT.go2rtcLanWs,
            go2rtcWanWs = prefs[Keys.GO2RTC_WAN_WS] ?: EndpointConfig.DEFAULT.go2rtcWanWs,
        )
    }

    // ── Save operations ──────────────────────────────────────────────

    /**
     * Persist all pairing data after a successful POST /api/v1/pair.
     */
    suspend fun savePairingData(
        userId: String,
        userName: String,
        deviceName: String,
        jwtToken: String,
        lanUrl: String,
        wanUrl: String,
        go2rtcLanWs: String,
        go2rtcWanWs: String
    ) {
        context.dataStore.edit { prefs ->
            prefs[Keys.IS_PAIRED] = true
            prefs[Keys.USER_ID] = userId
            prefs[Keys.USER_NAME] = userName
            prefs[Keys.DEVICE_NAME] = deviceName
            prefs[Keys.JWT_TOKEN] = jwtToken
            prefs[Keys.LAN_API_URL] = lanUrl
            prefs[Keys.WAN_API_URL] = wanUrl
            prefs[Keys.GO2RTC_LAN_WS] = go2rtcLanWs
            prefs[Keys.GO2RTC_WAN_WS] = go2rtcWanWs
            prefs[Keys.ONBOARDING_COMPLETE] = true
        }
    }

    suspend fun saveFcmToken(token: String) {
        context.dataStore.edit { prefs ->
            prefs[Keys.FCM_TOKEN] = token
        }
    }

    /**
     * Clear all stored data (unpair / factory reset).
     */
    suspend fun clearAll() {
        context.dataStore.edit { it.clear() }
    }
}
