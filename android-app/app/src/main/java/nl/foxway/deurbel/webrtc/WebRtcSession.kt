package nl.foxway.deurbel.webrtc

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.webrtc.*
import java.util.concurrent.Executors

/**
 * Encapsulates a complete WebRTC PeerConnection session to go2rtc.
 *
 * Provides:
 *  - Hardware AEC initialization via [HardwareAudioManager]
 *  - EGL context sharing for zero-copy HW video decoding
 *  - Local microphone capture with dynamic mute/unmute (PTT support)
 *  - Remote audio and video track handling
 *  - Pre-warming support (mute tracks until answered)
 */
class WebRtcSession(
    private val context: Context,
    val eglBase: EglBase = EglBase.create()
) {

    companion object {
        private const val TAG = "WebRtcSession"
        private const val AUDIO_TRACK_ID = "foxway_audio_mic"
    }

    private val audioManager = HardwareAudioManager(context)
    private val signaling = WebRtcSignaling()
    private val audioStreamer = nl.foxway.deurbel.audio.DoorbellAudioStreamer()
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private var activeBaseUrl: String = ""
    private var factory: PeerConnectionFactory? = null
    private var peerConnection: PeerConnection? = null
    private var localAudioTrack: AudioTrack? = null
    private var remoteVideoTrack: VideoTrack? = null
    private var remoteAudioTrack: AudioTrack? = null
    private var attachedRenderer: SurfaceViewRenderer? = null

    private val _connectionState = MutableStateFlow(PeerConnection.PeerConnectionState.NEW)
    val connectionState: StateFlow<PeerConnection.PeerConnectionState> = _connectionState.asStateFlow()

    private val _hasVideoTrack = MutableStateFlow(false)
    val hasVideoTrack: StateFlow<Boolean> = _hasVideoTrack.asStateFlow()

    private val _isMicrophoneMuted = MutableStateFlow(true)
    val isMicrophoneMuted: StateFlow<Boolean> = _isMicrophoneMuted.asStateFlow()

    private val _isRemoteAudioMuted = MutableStateFlow(true)
    val isRemoteAudioMuted: StateFlow<Boolean> = _isRemoteAudioMuted.asStateFlow()

    /**
     * Initialize the WebRTC peer connection factory with hardware acceleration.
     */
    fun initFactory() {
        val initOptions = PeerConnectionFactory.InitializationOptions.builder(context)
            .setEnableInternalTracer(false)
            .createInitializationOptions()
        PeerConnectionFactory.initialize(initOptions)

        val adm = audioManager.createAudioDeviceModule()
        val decoderFactory = DefaultVideoDecoderFactory(eglBase.eglBaseContext)
        val encoderFactory = DefaultVideoEncoderFactory(eglBase.eglBaseContext, true, true)

        factory = PeerConnectionFactory.builder()
            .setAudioDeviceModule(adm)
            .setVideoDecoderFactory(decoderFactory)
            .setVideoEncoderFactory(encoderFactory)
            .createPeerConnectionFactory()

        Log.i(TAG, "PeerConnectionFactory created with HW decoder and AEC")
    }

    /**
     * Start connection to go2rtc stream.
     *
     * Supports both WHEP HTTP endpoint and WebSocket signaling.
     *
     * @param signalingUrl HTTP base URL or WS URL
     * @param streamName "deurbel"
     * @param isPrewarm If true, starts muted so video decoder warms up silently
     */
    fun start(
        signalingUrl: String,
        streamName: String = "deurbel",
        isPrewarm: Boolean = false
    ) {
        activeBaseUrl = signalingUrl
        if (factory == null) initFactory()

        audioManager.startCommunicationMode(enableSpeaker = true)

        val host = signalingUrl.removePrefix("https://").removePrefix("http://").removePrefix("wss://").removePrefix("ws://").substringBefore(":").substringBefore("/")
        val iceServers = mutableListOf(
            PeerConnection.IceServer.builder("stun:stun.l.google.com:19302").createIceServer()
        )
        if (host.isNotBlank()) {
            iceServers.add(PeerConnection.IceServer.builder("stun:$host:8555").createIceServer())
        }

        val rtcConfig = PeerConnection.RTCConfiguration(iceServers).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.REQUIRE
            tcpCandidatePolicy = PeerConnection.TcpCandidatePolicy.ENABLED
        }

        peerConnection = factory?.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onSignalingChange(state: PeerConnection.SignalingState?) {
                Log.d(TAG, "SignalingState: $state")
            }

            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) {
                Log.d(TAG, "IceConnectionState: $state")
            }

            override fun onConnectionChange(newState: PeerConnection.PeerConnectionState?) {
                Log.i(TAG, "PeerConnectionState: $newState")
                newState?.let { _connectionState.value = it }
            }

            override fun onIceCandidate(candidate: IceCandidate?) {
                candidate?.let { signaling.sendCandidate(it) }
            }

            override fun onTrack(transceiver: RtpTransceiver?) {
                val track = transceiver?.receiver?.track()
                Log.i(TAG, "onTrack: kind=${track?.kind()} id=${track?.id()}")
                if (track is VideoTrack) {
                    remoteVideoTrack = track
                    _hasVideoTrack.value = true
                    // Fix black screen: attach to renderer immediately if already bound in Compose!
                    attachedRenderer?.let { renderer ->
                        try {
                            track.addSink(renderer)
                            Log.i(TAG, "Successfully attached incoming VideoTrack to existing renderer")
                        } catch (e: Exception) {
                            Log.e(TAG, "Failed attaching sink to renderer", e)
                        }
                    }
                } else if (track is AudioTrack) {
                    remoteAudioTrack = track
                    track.setEnabled(!isPrewarm)
                    _isRemoteAudioMuted.value = isPrewarm
                }
            }

            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}
            override fun onIceConnectionReceivingChange(receiving: Boolean) {}
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {}
            override fun onAddStream(stream: MediaStream?) {}
            override fun onRemoveStream(stream: MediaStream?) {}
            override fun onDataChannel(dataChannel: DataChannel?) {}
            override fun onRenegotiationNeeded() {}
        })

        // Add local audio track (bidirectional talkback) - ALWAYS starts MUTED per Requirement #4
        val audioSource = factory?.createAudioSource(MediaConstraints())
        localAudioTrack = factory?.createAudioTrack(AUDIO_TRACK_ID, audioSource)
        localAudioTrack?.setEnabled(false)
        _isMicrophoneMuted.value = true
        peerConnection?.addTrack(localAudioTrack, listOf("mediaStream"))

        // Add transceiver for receiving remote video & audio
        peerConnection?.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY)
        )

        // Native go2rtc WebSocket signaling (exact match to browser video-rtc.js)
        Log.i(TAG, "Starting native go2rtc WebRTC signaling for $streamName via $signalingUrl")
        signaling.connect(signalingUrl, streamName) {
            createOffer()
        }

        scope.launch {
            signaling.remoteAnswers.collect { answerSdp ->
                peerConnection?.setRemoteDescription(object : SdpObserver {
                    override fun onSetSuccess() {
                        Log.i(TAG, "Remote SDP answer applied")
                    }
                    override fun onSetFailure(error: String?) {
                        Log.e(TAG, "Failed to apply remote SDP answer: $error")
                        // If WS answer failed, try WHEP fallback
                        val httpUrl = signalingUrl.replace("^wss://".toRegex(), "https://").replace("^ws://".toRegex(), "http://")
                        createWhepOffer(httpUrl, streamName)
                    }
                    override fun onCreateSuccess(desc: SessionDescription?) {}
                    override fun onCreateFailure(error: String?) {}
                }, answerSdp)
            }
        }

        scope.launch {
            signaling.remoteCandidates.collect { candidate ->
                peerConnection?.addIceCandidate(candidate)
            }
        }
    }

    private fun createWhepOffer(httpUrl: String, streamName: String) {
        val constraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "true"))
            optional.add(MediaConstraints.KeyValuePair("googLowDelay", "true"))
            optional.add(MediaConstraints.KeyValuePair("googCpuOveruseDetection", "false"))
        }

        peerConnection?.createOffer(object : SdpObserver {
            override fun onCreateSuccess(desc: SessionDescription?) {
                desc ?: return
                peerConnection?.setLocalDescription(object : SdpObserver {
                    override fun onSetSuccess() {
                        Log.i(TAG, "Local SDP offer applied, exchanging via WHEP to $httpUrl")
                        scope.launch {
                            try {
                                val answerSdp = signaling.whepExchange(httpUrl, streamName, desc.description)
                                val sessionDesc = SessionDescription(SessionDescription.Type.ANSWER, answerSdp)
                                peerConnection?.setRemoteDescription(object : SdpObserver {
                                    override fun onSetSuccess() {
                                        Log.i(TAG, "WHEP remote SDP answer applied successfully!")
                                    }
                                    override fun onSetFailure(error: String?) {
                                        Log.e(TAG, "Failed to apply WHEP remote SDP: $error")
                                    }
                                    override fun onCreateSuccess(p0: SessionDescription?) {}
                                    override fun onCreateFailure(p0: String?) {}
                                }, sessionDesc)
                            } catch (e: Exception) {
                                Log.e(TAG, "WHEP exchange failed", e)
                            }
                        }
                    }
                    override fun onSetFailure(error: String?) {
                        Log.e(TAG, "Failed to set local SDP for WHEP: $error")
                    }
                    override fun onCreateSuccess(p0: SessionDescription?) {}
                    override fun onCreateFailure(p0: String?) {}
                }, desc)
            }
            override fun onCreateFailure(error: String?) {
                Log.e(TAG, "Failed to create WHEP SDP offer: $error")
            }
            override fun onSetSuccess() {}
            override fun onSetFailure(p0: String?) {}
        }, constraints)
    }

    private fun createOffer() {
        val constraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "true"))
            optional.add(MediaConstraints.KeyValuePair("googLowDelay", "true"))
            optional.add(MediaConstraints.KeyValuePair("googCpuOveruseDetection", "false"))
        }

        peerConnection?.createOffer(object : SdpObserver {
            override fun onCreateSuccess(desc: SessionDescription?) {
                desc ?: return
                peerConnection?.setLocalDescription(object : SdpObserver {
                    override fun onSetSuccess() {
                        Log.i(TAG, "Local SDP offer applied, sending to go2rtc")
                        signaling.sendOffer(desc)
                    }
                    override fun onSetFailure(error: String?) {
                        Log.e(TAG, "Failed to set local SDP: $error")
                    }
                    override fun onCreateSuccess(p0: SessionDescription?) {}
                    override fun onCreateFailure(p0: String?) {}
                }, desc)
            }

            override fun onCreateFailure(error: String?) {
                Log.e(TAG, "Failed to create SDP offer: $error")
            }

            override fun onSetSuccess() {}
            override fun onSetFailure(p0: String?) {}
        }, constraints)
    }

    /**
     * Unmute audio tracks when answering a call.
     * Mic starts strictly MUTED by default (Requirement #4) unless unmuteMic is explicitly true.
     */
    fun unmuteForAnswer(unmuteMic: Boolean = false) {
        remoteAudioTrack?.setEnabled(true)
        _isRemoteAudioMuted.value = false

        setMicrophoneMuted(!unmuteMic)
        Log.i(TAG, "Unmuted for answer: remoteAudio=ON, mic=${if (unmuteMic) "ON" else "MUTED"}")
    }

    /**
     * Mute or unmute remote audio from doorbell outdoor microphone.
     */
    fun setRemoteAudioMuted(muted: Boolean) {
        remoteAudioTrack?.setEnabled(!muted)
        _isRemoteAudioMuted.value = muted
    }

    /**
     * Set local microphone mute state (used by Push-to-Talk / Mute button).
     * Connects live audio streaming directly to the doorbell speaker via Port 8000 SDK.
     */
    fun setMicrophoneMuted(muted: Boolean) {
        localAudioTrack?.setEnabled(!muted)
        _isMicrophoneMuted.value = muted
        if (!muted && activeBaseUrl.isNotBlank()) {
            audioStreamer.startStreaming(activeBaseUrl)
        } else {
            audioStreamer.stopStreaming()
        }
        Log.d(TAG, "Microphone set to muted=$muted (live stream=${!muted})")
    }

    /**
     * Toggle speakerphone.
     */
    fun setSpeakerphone(enabled: Boolean) {
        audioManager.setSpeakerphone(enabled)
    }

    fun isSpeakerphoneOn(): Boolean = audioManager.isSpeakerphoneOn()

    /**
     * Attach remote video track to a Compose SurfaceViewRenderer.
     */
    fun attachRenderer(
        renderer: SurfaceViewRenderer,
        scalingType: RendererCommon.ScalingType = RendererCommon.ScalingType.SCALE_ASPECT_FIT
    ) {
        attachedRenderer = renderer
        try {
            renderer.init(eglBase.eglBaseContext, null)
        } catch (_: Exception) {}
        renderer.setScalingType(scalingType)
        renderer.setEnableHardwareScaler(true)
        renderer.setMirror(false)
        renderer.requestLayout()
        remoteVideoTrack?.addSink(renderer)
        Log.i(TAG, "Attached SurfaceViewRenderer (hasTrack=${remoteVideoTrack != null})")
    }

    /**
     * Dynamically change scaling type (Fit vs Fill).
     */
    fun setScalingType(scalingType: RendererCommon.ScalingType) {
        attachedRenderer?.setScalingType(scalingType)
        attachedRenderer?.requestLayout()
    }

    /**
     * Detach remote video track from renderer.
     */
    fun detachRenderer(renderer: SurfaceViewRenderer) {
        remoteVideoTrack?.removeSink(renderer)
        if (attachedRenderer == renderer) {
            attachedRenderer = null
        }
        try {
            renderer.release()
        } catch (_: Exception) {}
        Log.i(TAG, "Detached and released SurfaceViewRenderer")
    }

    /**
     * Close the WebRTC session and release resources.
     */
    fun close() {
        audioStreamer.stopStreaming()
        signaling.close()
        attachedRenderer = null
        remoteVideoTrack = null
        remoteAudioTrack = null
        localAudioTrack?.dispose()
        localAudioTrack = null
        peerConnection?.close()
        peerConnection?.dispose()
        peerConnection = null
        factory?.dispose()
        factory = null
        audioManager.stopCommunicationMode()
        scope.cancel()
        Log.i(TAG, "WebRtcSession closed and disposed")
    }
}
