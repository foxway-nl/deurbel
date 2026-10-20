package nl.foxway.deurbel.webrtc

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.util.Log
import org.webrtc.audio.AudioDeviceModule
import org.webrtc.audio.JavaAudioDeviceModule

/**
 * Audio manager handling Hardware Acoustic Echo Cancellation (AEC),
 * Noise Suppression (NS), AudioManager mode switching, and Speakerphone routing.
 *
 * Implements SPEC §4.2:
 *  - JavaAudioDeviceModule with setUseHardwareAcousticEchoCanceler(true)
 *  - setUseHardwareNoiseSuppressor(true)
 *  - AudioAttributes with USAGE_VOICE_COMMUNICATION & CONTENT_TYPE_SPEECH
 *  - AudioManager.mode = MODE_IN_COMMUNICATION
 *  - Default Speakerphone = ON during doorbell calls
 */
class HardwareAudioManager(private val context: Context) {

    companion object {
        private const val TAG = "HardwareAudioManager"
    }

    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var previousAudioMode: Int = AudioManager.MODE_NORMAL
    private var previousSpeakerState: Boolean = false
    private var audioFocusRequest: AudioFocusRequest? = null

    /**
     * Create the [AudioDeviceModule] tuned for bidirectional voice with
     * hardware echo cancellation.
     */
    fun createAudioDeviceModule(): AudioDeviceModule {
        val audioAttributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()

        val adm = JavaAudioDeviceModule.builder(context)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .setAudioAttributes(audioAttributes)
            .setAudioRecordErrorCallback(object : JavaAudioDeviceModule.AudioRecordErrorCallback {
                override fun onWebRtcAudioRecordInitError(errorMessage: String?) {
                    Log.e(TAG, "AudioRecord init error: $errorMessage")
                }
                override fun onWebRtcAudioRecordStartError(
                    errorCode: JavaAudioDeviceModule.AudioRecordStartErrorCode?,
                    errorMessage: String?
                ) {
                    Log.e(TAG, "AudioRecord start error: $errorCode - $errorMessage")
                }
                override fun onWebRtcAudioRecordError(errorMessage: String?) {
                    Log.e(TAG, "AudioRecord error: $errorMessage")
                }
            })
            .setAudioTrackErrorCallback(object : JavaAudioDeviceModule.AudioTrackErrorCallback {
                override fun onWebRtcAudioTrackInitError(errorMessage: String?) {
                    Log.e(TAG, "AudioTrack init error: $errorMessage")
                }
                override fun onWebRtcAudioTrackStartError(
                    errorCode: JavaAudioDeviceModule.AudioTrackStartErrorCode?,
                    errorMessage: String?
                ) {
                    Log.e(TAG, "AudioTrack start error: $errorCode - $errorMessage")
                }
                override fun onWebRtcAudioTrackError(errorMessage: String?) {
                    Log.e(TAG, "AudioTrack error: $errorMessage")
                }
            })
            .createAudioDeviceModule()

        Log.i(TAG, "JavaAudioDeviceModule initialized with HW AEC and NS")
        return adm
    }

    /**
     * Route audio to voice communication mode and request audio focus.
     */
    fun startCommunicationMode(enableSpeaker: Boolean = true) {
        previousAudioMode = audioManager.mode
        @Suppress("DEPRECATION")
        previousSpeakerState = audioManager.isSpeakerphoneOn

        // Request audio focus for voice call
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val playbackAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build()

            val focusRequest = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
                .setAudioAttributes(playbackAttributes)
                .setAcceptsDelayedFocusGain(false)
                .setOnAudioFocusChangeListener { focusChange ->
                    Log.d(TAG, "Audio focus changed: $focusChange")
                }
                .build()

            audioFocusRequest = focusRequest
            audioManager.requestAudioFocus(focusRequest)
        } else {
            @Suppress("DEPRECATION")
            audioManager.requestAudioFocus(
                null,
                AudioManager.STREAM_VOICE_CALL,
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE
            )
        }

        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        setSpeakerphone(enableSpeaker)
        Log.i(TAG, "Audio set to MODE_IN_COMMUNICATION, speakerphone=$enableSpeaker")
    }

    /**
     * Toggle speakerphone on/off.
     */
    fun setSpeakerphone(enabled: Boolean) {
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = enabled
        Log.d(TAG, "Speakerphone set to $enabled")
    }

    @Suppress("DEPRECATION")
    fun isSpeakerphoneOn(): Boolean = audioManager.isSpeakerphoneOn

    /**
     * Restore previous audio settings and abandon focus.
     */
    fun stopCommunicationMode() {
        setSpeakerphone(previousSpeakerState)
        audioManager.mode = previousAudioMode

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
            audioFocusRequest = null
        } else {
            @Suppress("DEPRECATION")
            audioManager.abandonAudioFocus(null)
        }

        Log.i(TAG, "Audio communication mode stopped, restored mode $previousAudioMode")
    }
}
