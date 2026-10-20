package nl.foxway.deurbel.data

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.*

enum class LogCategory {
    INFO,
    CALL,
    NETWORK,
    ERROR
}

data class LogEntry(
    val id: String = UUID.randomUUID().toString(),
    val timestamp: Long = System.currentTimeMillis(),
    val category: LogCategory,
    val message: String,
    val details: String? = null
) {
    fun formattedTime(): String {
        val sdf = SimpleDateFormat("dd-MM HH:mm:ss", Locale.getDefault())
        return sdf.format(Date(timestamp))
    }

    fun toJson(): JSONObject {
        return JSONObject().apply {
            put("id", id)
            put("timestamp", timestamp)
            put("category", category.name)
            put("message", message)
            if (details != null) put("details", details)
        }
    }

    companion object {
        fun fromJson(json: JSONObject): LogEntry {
            return LogEntry(
                id = json.optString("id", UUID.randomUUID().toString()),
                timestamp = json.optLong("timestamp", System.currentTimeMillis()),
                category = try {
                    LogCategory.valueOf(json.optString("category", "INFO"))
                } catch (_: Exception) {
                    LogCategory.INFO
                },
                message = json.optString("message", ""),
                details = if (json.has("details")) json.getString("details") else null
            )
        }
    }
}

/**
 * 30-Day Rotating Local App Logger.
 *
 * Implements SPEC §4 & User Request Requirement #8:
 *  - Stores logs locally in JSON format
 *  - Automatically purges entries older than 30 days
 *  - Thread-safe and exposed via StateFlow for Jetpack Compose UI
 */
class AppLogger(private val context: Context) {

    companion object {
        private const val TAG = "AppLogger"
        private const val FILE_NAME = "foxway_event_logs.json"
        private const val RETENTION_MILLIS = 30L * 24 * 60 * 60 * 1000 // 30 days

        @Volatile
        private var instance: AppLogger? = null

        fun getInstance(context: Context): AppLogger {
            return instance ?: synchronized(this) {
                instance ?: AppLogger(context.applicationContext).also { instance = it }
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val mutex = Mutex()
    private val logFile by lazy { File(context.filesDir, FILE_NAME) }

    private val _logsFlow = MutableStateFlow<List<LogEntry>>(emptyList())
    val logsFlow: StateFlow<List<LogEntry>> = _logsFlow.asStateFlow()

    init {
        scope.launch {
            loadAndPruneLogs()
        }
    }

    fun log(category: LogCategory, message: String, details: String? = null) {
        val entry = LogEntry(
            category = category,
            message = message,
            details = details
        )
        Log.i(TAG, "[${category.name}] $message ${details ?: ""}")

        scope.launch {
            mutex.withLock {
                val current = _logsFlow.value.toMutableList()
                current.add(0, entry) // Newest first

                // Prune older than 30 days
                val cutoff = System.currentTimeMillis() - RETENTION_MILLIS
                val pruned = current.filter { it.timestamp >= cutoff }

                _logsFlow.value = pruned
                saveToFile(pruned)
            }
        }
    }

    fun info(message: String, details: String? = null) = log(LogCategory.INFO, message, details)
    fun call(message: String, details: String? = null) = log(LogCategory.CALL, message, details)
    fun network(message: String, details: String? = null) = log(LogCategory.NETWORK, message, details)
    fun error(message: String, details: String? = null) = log(LogCategory.ERROR, message, details)

    fun clear() {
        scope.launch {
            mutex.withLock {
                _logsFlow.value = emptyList()
                if (logFile.exists()) {
                    logFile.delete()
                }
            }
        }
    }

    fun exportAsText(): String {
        val sb = StringBuilder()
        sb.appendLine("═══════════════════════════════════════")
        sb.appendLine("Foxway Deurbel — Systeem Logboek (30 dagen)")
        sb.appendLine("Tijdstip: ${SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(Date())}")
        sb.appendLine("═══════════════════════════════════════\n")

        _logsFlow.value.forEach { entry ->
            sb.appendLine("[${entry.formattedTime()}] [${entry.category.name}] ${entry.message}")
            if (!entry.details.isNullOrBlank()) {
                sb.appendLine("   Details: ${entry.details}")
            }
        }
        return sb.toString()
    }

    private fun loadAndPruneLogs() {
        if (!logFile.exists()) return
        try {
            val content = logFile.readText()
            if (content.isBlank()) return
            val jsonArray = JSONArray(content)
            val list = mutableListOf<LogEntry>()
            val cutoff = System.currentTimeMillis() - RETENTION_MILLIS

            for (i in 0 until jsonArray.length()) {
                val obj = jsonArray.getJSONObject(i)
                val entry = LogEntry.fromJson(obj)
                if (entry.timestamp >= cutoff) {
                    list.add(entry)
                }
            }

            // Sort newest first
            list.sortByDescending { it.timestamp }
            _logsFlow.value = list
            saveToFile(list)
        } catch (e: Exception) {
            Log.e(TAG, "Error loading logs", e)
        }
    }

    private fun saveToFile(entries: List<LogEntry>) {
        try {
            val jsonArray = JSONArray()
            // Cap at 2000 entries max to prevent excessive file size
            entries.take(2000).forEach {
                jsonArray.put(it.toJson())
            }
            logFile.writeText(jsonArray.toString())
        } catch (e: Exception) {
            Log.e(TAG, "Error saving logs", e)
        }
    }
}
