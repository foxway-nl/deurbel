package nl.foxway.deurbel.telecom

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import nl.foxway.deurbel.FoxwayApp
import nl.foxway.deurbel.network.ApiClientFactory
import nl.foxway.deurbel.network.EndpointResolver

/**
 * Handles notification actions (e.g., Decline / Weigeren) for incoming doorbell calls.
 */
class CallActionReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "CallActionReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val callId = intent.getStringExtra(DoorbellConnectionService.EXTRA_CALL_ID) ?: ""
        Log.i(TAG, "Received call action: ${intent.action}, callId=$callId")

        when (intent.action) {
            DoorbellConnectionService.ACTION_DECLINE_CALL -> {
                DoorbellConnectionService.notifyRejected()
                DoorbellConnectionService.cancelCallNotification(context)

                if (callId.isNotBlank()) {
                    CoroutineScope(Dispatchers.IO).launch {
                        try {
                            val resolver = EndpointResolver(FoxwayApp.instance.preferencesManager)
                            val resolved = resolver.resolve()
                            val api = ApiClientFactory.createApi(resolved.apiBaseUrl)
                            api.hangup(callId)
                            Log.i(TAG, "Call $callId hung up via notification action")
                        } catch (e: Exception) {
                            Log.w(TAG, "Error hanging up call $callId: ${e.message}")
                        }
                    }
                }
            }
        }
    }
}
