"""
Hikvision & EZVIZ DB1 Pro Audio Bridge.

Routes audio and test tones to the doorbell speaker via:
1. Hikvision HCNetSDK binary voice talk on port 8000 (NET_DVR_StartVoiceCom_MR_V30).
   Uses native hardware AAC-LC 16kHz ADTS frames (verified via automated loopback FFT).
2. Live streaming session (LiveVoiceComSession) for real-time talkback from phone microphone.

Hardened concurrency model:
- Reentrant threading lock (_voicecom_thread_lock) ensures strictly ONE active session.
- Automatic force-close of lingering voice handles and 150ms reset delay.
- 1x automatic retry on StartVoiceCom failure.
- Returns structured success/error dictionaries to avoid unhandled HTTP 500s.
"""

import asyncio
import ctypes
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger("foxway.audio_bridge")

def _get_credentials() -> tuple[str, int, str, str]:
    """Retrieve dynamic camera credentials from DB or fallback to environment variables."""
    try:
        from app.models import _DB_PATH
        import sqlite3
        con = sqlite3.connect(_DB_PATH)
        cur = con.cursor()
        res = dict(cur.execute("SELECT key, value FROM app_settings WHERE key LIKE 'camera_%'").fetchall())
        con.close()
        from app.config import settings
        ip = res.get("camera_ip") or settings.DOORBELL_IP
        port = int(res.get("camera_sdk_port") or settings.DOORBELL_SDK_PORT)
        user = res.get("camera_user") or settings.DOORBELL_USER
        pw = res.get("camera_pass") or settings.DOORBELL_PASS
        return ip, port, user, pw
    except Exception:
        from app.config import settings
        return (
            settings.DOORBELL_IP,
            int(settings.DOORBELL_SDK_PORT),
            settings.DOORBELL_USER,
            settings.DOORBELL_PASS,
        )

# Master locks and active session tracking
_speak_lock = asyncio.Lock()
_voicecom_thread_lock = threading.RLock()
_active_voice_handle: int = -1
_active_user_id: int = -1
_active_sdk = None


def _get_sdk():
    search_dirs = [
        "/usr/local/lib/python3.11/site-packages/HCNetSDK/Libs/linux",
        "/app/HCNetSDK/Libs/linux",
        "/mnt/opslag/deurbel/backend/HCNetSDK/Libs/linux",
    ]
    try:
        import HCNetSDK
        pkg_dir = Path(HCNetSDK.__file__).parent / "Libs" / "linux"
        if pkg_dir.exists():
            search_dirs.insert(0, str(pkg_dir))
    except Exception:
        pass

    sdk_dir = next((d for d in search_dirs if os.path.exists(os.path.join(d, "libhcnetsdk.so"))), None)
    if not sdk_dir:
        raise RuntimeError("libhcnetsdk.so not found")

    com_dir = os.path.join(sdk_dir, "HCNetSDKCom")
    cur_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{sdk_dir}:{com_dir}:{cur_ld}"

    for dep in ["libhpr.so", "libcrypto.so.1.1", "libssl.so.1.1", "libHCCore.so"]:
        dep_p = os.path.join(sdk_dir, dep)
        if os.path.exists(dep_p):
            try:
                ctypes.CDLL(dep_p, mode=ctypes.RTLD_GLOBAL)
            except Exception:
                pass

    sdk = ctypes.CDLL(os.path.join(sdk_dir, "libhcnetsdk.so"), mode=ctypes.RTLD_GLOBAL)

    class NET_DVR_LOCAL_SDK_PATH(ctypes.Structure):
        _fields_ = [("sPath", ctypes.c_char * 256), ("byRes", ctypes.c_byte * 128)]

    p_com = NET_DVR_LOCAL_SDK_PATH(sPath=f"{com_dir}/".encode("utf-8"))
    sdk.NET_DVR_SetSDKInitCfg(1, ctypes.byref(p_com))

    p_lib = NET_DVR_LOCAL_SDK_PATH(sPath=f"{sdk_dir}/".encode("utf-8"))
    sdk.NET_DVR_SetSDKInitCfg(2, ctypes.byref(p_lib))

    if not sdk.NET_DVR_Init():
        raise RuntimeError(f"NET_DVR_Init failed: {sdk.NET_DVR_GetLastError()}")

    return sdk


def force_close_active_voicecom(logout_user: bool = False):
    """Force close any lingering voice handle and optionally user session, resetting hardware channel."""
    global _active_voice_handle, _active_user_id, _active_sdk
    with _voicecom_thread_lock:
        if _active_sdk is not None:
            if _active_voice_handle >= 0:
                try:
                    logger.info("Force-closing lingering VoiceCom handle %d", _active_voice_handle)
                    _active_sdk.NET_DVR_StopVoiceCom(_active_voice_handle)
                except Exception as e:
                    logger.debug("Exception stopping lingering voice handle: %s", e)
                _active_voice_handle = -1
                time.sleep(0.050)
            if logout_user and _active_user_id >= 0:
                try:
                    logger.info("Logging out SDK user session %d", _active_user_id)
                    _active_sdk.NET_DVR_Logout(_active_user_id)
                except Exception as e:
                    logger.debug("Exception logging out user session: %s", e)
                _active_user_id = -1


def prewarm_sdk_session() -> int:
    """
    Asynchronously pre-authenticate with the camera over Hikvision port 8000 SDK.
    Caches the user_id session so when the user answers, StartVoiceCom connects
    instantly in under 200ms without waiting for network login handshakes.
    """
    global _active_user_id, _active_sdk
    with _voicecom_thread_lock:
        try:
            if _active_sdk is None:
                _active_sdk = _get_sdk()

            if _active_user_id >= 0:
                return _active_user_id

            class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
                _fields_ = [("sSerialNumber", ctypes.c_byte * 48)] + [("res", ctypes.c_byte * 96)]

            dev_info = NET_DVR_DEVICEINFO_V30()
            _active_sdk.NET_DVR_Login_V30.restype = ctypes.c_int32
            _active_sdk.NET_DVR_Login_V30.argtypes = [
                ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p
            ]

            cam_ip, cam_port, cam_user, cam_pass = _get_credentials()
            uid = _active_sdk.NET_DVR_Login_V30(
                cam_ip.encode("utf-8"), cam_port, cam_user.encode("utf-8"), cam_pass.encode("utf-8"), ctypes.byref(dev_info)
            )
            if uid >= 0:
                _active_user_id = uid
                logger.info("⚡ Pre-warmed Hikvision SDK session ready (user_id=%d)", uid)
                return uid
            else:
                err = _active_sdk.NET_DVR_GetLastError()
                logger.warning("Pre-warm SDK login attempt returned %d", err)
                return -1
        except Exception as e:
            logger.warning("Pre-warm SDK exception: %s", e)
            return -1


async def async_prewarm_sdk_session() -> None:
    """Async background task for pre-warming camera SDK login on incoming call."""
    await asyncio.to_thread(prewarm_sdk_session)


def _parse_adts_frames(adts_data: bytes) -> List[bytes]:
    """Parse raw AAC ADTS byte stream into discrete ADTS frames."""
    frames = []
    idx = 0
    while idx < len(adts_data) - 7:
        if adts_data[idx] == 0xFF and (adts_data[idx + 1] & 0xF0) == 0xF0:
            flen = (
                ((adts_data[idx + 3] & 0x03) << 11)
                | (adts_data[idx + 4] << 3)
                | ((adts_data[idx + 5] & 0xE0) >> 5)
            )
            if flen > 0 and idx + flen <= len(adts_data):
                frames.append(adts_data[idx : idx + flen])
                idx += flen
                continue
        idx += 1
    return frames


class LiveVoiceComSession:
    """
    Manages a live bidirectional voice session with the doorbell.
    Pipes incoming 16kHz 16-bit mono PCM bytes from phone microphone through
    a persistent FFmpeg AAC-LC encoder and streams ADTS frames via port 8000 SDK.
    """

    def __init__(self):
        self.sdk = None
        self.uid = -1
        self.v_handle = -1
        self.proc = None
        self._reader_thread = None
        self._running = False
        self._lock = threading.Lock()
        self._cb = None

    def start(self) -> bool:
        global _active_voice_handle, _active_user_id, _active_sdk
        with _voicecom_thread_lock:
            # Stop any lingering voice channel without logging out warm user session
            force_close_active_voicecom(logout_user=False)

            self.sdk = _get_sdk()
            _active_sdk = self.sdk

            class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
                _fields_ = [("sSerialNumber", ctypes.c_byte * 48)] + [("res", ctypes.c_byte * 96)]

            self.sdk.NET_DVR_Login_V30.restype = ctypes.c_int32
            self.sdk.NET_DVR_Login_V30.argtypes = [
                ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p
            ]

            VOICE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_byte, ctypes.c_void_p)
            def _cb(h, b, s, f, u): pass
            self._cb = VOICE_CB(_cb)

            self.sdk.NET_DVR_StartVoiceCom_MR_V30.restype = ctypes.c_int32
            self.sdk.NET_DVR_StartVoiceCom_MR_V30.argtypes = [ctypes.c_int32, ctypes.c_uint32, VOICE_CB, ctypes.c_void_p]

            for attempt in range(1, 4):
                try:
                    # Fast-path: use pre-warmed user session if available
                    if _active_user_id >= 0:
                        self.uid = _active_user_id
                    else:
                        dev_info = NET_DVR_DEVICEINFO_V30()
                        cam_ip, cam_port, cam_user, cam_pass = _get_credentials()
                        self.uid = self.sdk.NET_DVR_Login_V30(
                            cam_ip.encode("utf-8"), cam_port, cam_user.encode("utf-8"), cam_pass.encode("utf-8"), ctypes.byref(dev_info)
                        )
                        if self.uid < 0:
                            err = self.sdk.NET_DVR_GetLastError()
                            logger.warning("SDK login attempt %d failed: %d", attempt, err)
                            if attempt < 3:
                                time.sleep(0.150 * attempt)
                                continue
                            return False
                        _active_user_id = self.uid

                    # Start voice communication channel (<50ms)
                    self.v_handle = self.sdk.NET_DVR_StartVoiceCom_MR_V30(self.uid, 0, self._cb, None)
                    if self.v_handle < 0:
                        logger.debug("Voice channel 0 failed, trying channel 1...")
                        self.v_handle = self.sdk.NET_DVR_StartVoiceCom_MR_V30(self.uid, 1, self._cb, None)

                    if self.v_handle < 0:
                        err = self.sdk.NET_DVR_GetLastError()
                        logger.warning("StartVoiceCom_MR_V30 attempt %d failed: %d (resetting session)", attempt, err)
                        try:
                            self.sdk.NET_DVR_Logout(self.uid)
                        except Exception:
                            pass
                        _active_user_id = -1
                        self.uid = -1
                        if attempt < 3:
                            time.sleep(0.150 * attempt)
                            continue
                        return False

                    # Successfully established
                    _active_voice_handle = self.v_handle
                    _active_user_id = self.uid
                    break

                except Exception as e:
                    logger.error("LiveVoiceComSession start error: %s", e)
                    _active_user_id = -1
                    if attempt < 3:
                        time.sleep(0.150 * attempt)
                        continue
                    return False

            self.sdk.NET_DVR_VoiceComSendData.restype = ctypes.c_bool
            self.sdk.NET_DVR_VoiceComSendData.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32]

            cmd = [
                "ffmpeg", "-y",
                "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                "-c:a", "aac", "-b:a", "64k", "-flush_packets", "1",
                "-f", "adts", "pipe:1"
            ]
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )

            self._running = True
            self._reader_thread = threading.Thread(target=self._read_and_stream_adts, daemon=True)
            self._reader_thread.start()
            logger.info("LiveVoiceComSession established (handle: %d)", self.v_handle)
            return True

    def feed_pcm(self, pcm_bytes: bytes):
        if not self._running or not self.proc or not self.proc.stdin:
            return
        with self._lock:
            try:
                self.proc.stdin.write(pcm_bytes)
                self.proc.stdin.flush()
            except Exception as e:
                logger.error("Error writing PCM to transcode pipe: %s", e)

    def _read_and_stream_adts(self):
        buf = bytearray()
        while self._running and self.proc and self.proc.stdout:
            try:
                chunk = self.proc.stdout.read1(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 7:
                    if buf[0] == 0xFF and (buf[1] & 0xF0) == 0xF0:
                        flen = ((buf[3] & 0x03) << 11) | (buf[4] << 3) | ((buf[5] & 0xE0) >> 5)
                        if flen > 0 and len(buf) >= flen:
                            frame = bytes(buf[:flen])
                            del buf[:flen]
                            if self.v_handle >= 0:
                                self.sdk.NET_DVR_VoiceComSendData(self.v_handle, frame, len(frame))
                            continue
                    del buf[0]
            except Exception as e:
                logger.debug("Live stream reader exception: %s", e)
                break

    def stop(self):
        global _active_voice_handle, _active_user_id
        self._running = False
        with self._lock:
            if self.proc:
                try:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                    self.proc.terminate()
                    self.proc.wait(timeout=0.5)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None

            with _voicecom_thread_lock:
                if self.v_handle >= 0:
                    try:
                        self.sdk.NET_DVR_StopVoiceCom(self.v_handle)
                    except Exception:
                        pass
                    if _active_voice_handle == self.v_handle:
                        _active_voice_handle = -1
                    self.v_handle = -1

                if self.uid >= 0:
                    try:
                        self.sdk.NET_DVR_Logout(self.uid)
                    except Exception:
                        pass
                    if _active_user_id == self.uid:
                        _active_user_id = -1
                    self.uid = -1
        logger.info("LiveVoiceComSession closed cleanly")


def send_test_tone_to_doorbell(duration: float = 1.0, volume: float = 0.3) -> bool:
    """Generate 1000 Hz test tone and stream to speaker via port 8000 SDK."""
    out_file = "/tmp/speak_test_tone.adts"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=1000:sample_rate=16000:duration={duration}",
        "-af", f"volume={volume * 8.0}",
        "-ar", "16000", "-ac", "1",
        "-c:a", "aac", "-b:a", "48k",
        "-f", "adts", out_file
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out_file, "rb") as f:
        data = f.read()

    frames = _parse_adts_frames(data)
    if not frames:
        logger.error("Failed to parse test tone ADTS frames")
        return False

    return _stream_frames_via_sdk(frames, frame_interval=0.064)


def send_audio_to_doorbell(
    audio_data: Optional[bytes] = None,
    file_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Send audio payload to doorbell speaker."""
    out_file = "/tmp/speak_payload.adts"

    if file_path:
        cmd = [
            "ffmpeg", "-y", "-i", str(file_path),
            "-ar", "16000", "-ac", "1",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "adts", out_file
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            logger.error("FFmpeg file transcode failed: %s", proc.stderr.decode()[-200:])
            return False
    elif audio_data:
        cmd = [
            "ffmpeg", "-y", "-i", "pipe:0",
            "-ar", "16000", "-ac", "1",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "adts", out_file
        ]
        proc = subprocess.run(cmd, input=audio_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            logger.error("FFmpeg audio stream transcode failed: %s", proc.stderr.decode()[-200:])
            return False
    else:
        return send_test_tone_to_doorbell()

    if not os.path.exists(out_file):
        return False

    with open(out_file, "rb") as f:
        data = f.read()

    frames = _parse_adts_frames(data)
    if not frames:
        logger.error("No valid ADTS frames found in transcode output")
        return False

    return _stream_frames_via_sdk(frames, frame_interval=0.064)


def _stream_frames_via_sdk(frames: List[bytes], frame_interval: float = 0.064) -> bool:
    """Stream discrete ADTS frames to EZVIZ DB1 Pro on port 8000 using Hikvision HCNetSDK with concurrency locks and retry."""
    global _active_voice_handle, _active_user_id, _active_sdk

    with _voicecom_thread_lock:
        force_close_active_voicecom()

        try:
            sdk = _get_sdk()
            _active_sdk = sdk
        except Exception as e:
            logger.error("Failed loading SDK: %s", e)
            return False

        class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
            _fields_ = [("sSerialNumber", ctypes.c_byte * 48)] + [("res", ctypes.c_byte * 96)]

        dev_info = NET_DVR_DEVICEINFO_V30()
        sdk.NET_DVR_Login_V30.restype = ctypes.c_int32
        sdk.NET_DVR_Login_V30.argtypes = [
            ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p
        ]

        uid = -1
        v_handle = -1

        for attempt in range(1, 4):
            cam_ip, cam_port, cam_user, cam_pass = _get_credentials()
            uid = sdk.NET_DVR_Login_V30(
                cam_ip.encode("utf-8"), cam_port, cam_user.encode("utf-8"), cam_pass.encode("utf-8"), ctypes.byref(dev_info)
            )
            if uid < 0:
                err = sdk.NET_DVR_GetLastError()
                logger.warning("NET_DVR_Login_V30 attempt %d failed: %d", attempt, err)
                if attempt < 3:
                    time.sleep(0.400 * attempt)
                    continue
                return False

            VOICE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_byte, ctypes.c_void_p)
            def _dummy_cb(h, b, s, f, u): pass
            cb = VOICE_CB(_dummy_cb)

            sdk.NET_DVR_StartVoiceCom_MR_V30.restype = ctypes.c_int32
            sdk.NET_DVR_StartVoiceCom_MR_V30.argtypes = [ctypes.c_int32, ctypes.c_uint32, VOICE_CB, ctypes.c_void_p]

            v_handle = sdk.NET_DVR_StartVoiceCom_MR_V30(uid, 0, cb, None)
            if v_handle < 0:
                logger.debug("Voice channel 0 failed, trying channel 1...")
                v_handle = sdk.NET_DVR_StartVoiceCom_MR_V30(uid, 1, cb, None)

            if v_handle < 0:
                err = sdk.NET_DVR_GetLastError()
                logger.warning("NET_DVR_StartVoiceCom_MR_V30 attempt %d failed: %d", attempt, err)
                sdk.NET_DVR_Logout(uid)
                uid = -1
                if attempt < 3:
                    time.sleep(0.400 * attempt)
                    continue
                return False
            break

        _active_voice_handle = v_handle
        _active_user_id = uid

        sdk.NET_DVR_VoiceComSendData.restype = ctypes.c_bool
        sdk.NET_DVR_VoiceComSendData.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_uint32]

        sent = 0
        start_t = time.time()
        try:
            for frame in frames:
                ok = sdk.NET_DVR_VoiceComSendData(v_handle, frame, len(frame))
                if ok:
                    sent += 1
                target_t = start_t + sent * frame_interval
                sleep_t = target_t - time.time()
                if sleep_t > 0:
                    time.sleep(sleep_t)
            logger.info("Streamed %d frames (%.2fs) to doorbell speaker", sent, sent * frame_interval)
            return sent > 0
        finally:
            try:
                sdk.NET_DVR_StopVoiceCom(v_handle)
            except Exception:
                pass
            try:
                sdk.NET_DVR_Logout(uid)
            except Exception:
                pass
            _active_voice_handle = -1
            _active_user_id = -1
            time.sleep(0.200)


async def play_audio_to_doorbell_async(
    audio_data: Optional[bytes] = None,
    file_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Async wrapper ensuring single-access lock to the doorbell speaker."""
    async with _speak_lock:
        return await asyncio.to_thread(send_audio_to_doorbell, audio_data, file_path)


async def run_acoustic_loopback_test(rtsp_url: Optional[str] = None) -> dict:
    """
    True End-to-End Acoustic Loopback Verification:
    1. Simultaneously opens RTSP incoming microphone audio stream via ffmpeg to capture 3.5s.
    2. While recording, transmits 1000 Hz sine wave via Port 8000 SDK to doorbell outdoor speaker for 2.0s.
    3. Runs FFT frequency analysis (numpy.fft.rfft) on captured mic audio.
    4. Verifies peak at 950 Hz - 1050 Hz with SNR calculation.
    """
    async with _speak_lock:
        return await asyncio.to_thread(_run_acoustic_loopback_sync, rtsp_url)


def _run_acoustic_loopback_sync(rtsp_url: Optional[str] = None) -> dict:
    import numpy as np
    from app.config import settings
    url = rtsp_url or settings.GO2RTC_RTSP_URL

    cmd = [
        "ffmpeg", "-y", "-nostdin", "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "32768", "-analyzeduration", "200000",
        "-rtsp_transport", "tcp", "-i", url,
        "-vn", "-f", "s16le", "-ac", "1", "-ar", "16000",
        "-loglevel", "quiet", "-"
    ]
    proc = None
    buf = bytearray()
    running = True
    ready_event = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=65536
        )

        def reader():
            while running and proc and proc.stdout:
                try:
                    chunk = proc.stdout.read(4000)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) >= 12800 and not ready_event.is_set():
                        ready_event.set()
                except Exception:
                    break

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Wait until mic RTSP reader has actually received >= 0.4s PCM audio (12800 bytes)
        ready_event.wait(timeout=4.0)
        speaker_ok = send_test_tone_to_doorbell(duration=2.0, volume=0.85)
        time.sleep(1.0)
        running = False
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        t.join(timeout=1.0)

        if len(buf) < 16000:
            logger.warning("Acoustic loopback capture buffer incomplete: %d bytes", len(buf))
            return {
                "status": "error",
                "passed": False,
                "speaker_sent": bool(speaker_ok),
                "mic_loopback_detected": False,
                "peak_hz": 0.0,
                "snr_db": 0.0,
                "message": "Microfoon opname via RTSP mislukt of onvoldoende data",
            }

        pcm = np.frombuffer(buf, dtype=np.int16)
        rate = 16000
        fft_vals = np.abs(np.fft.rfft(pcm))
        freqs = np.fft.rfftfreq(len(pcm), 1.0 / rate)

        target_idx = np.where((freqs >= 950) & (freqs <= 1050))[0]
        if len(target_idx) == 0:
            return {
                "status": "error",
                "passed": False,
                "speaker_sent": bool(speaker_ok),
                "mic_loopback_detected": False,
                "peak_hz": 0.0,
                "snr_db": 0.0,
                "message": "Frequentie bereik 950-1050 Hz niet gevonden in FFT",
            }

        peak_idx = target_idx[np.argmax(fft_vals[target_idx])]
        peak_freq = float(freqs[peak_idx])
        peak_mag = float(fft_vals[peak_idx])

        noise_mask = (freqs >= 300) & (freqs <= 3000) & ~((freqs >= 900) & (freqs <= 1100))
        noise_floor = float(np.median(fft_vals[noise_mask])) + 1e-6
        snr_db = float(20 * np.log10(peak_mag / noise_floor))

        detected = bool((950 <= peak_freq <= 1050) and (snr_db >= 3.0))

        return {
            "status": "ok",
            "passed": detected,
            "speaker_sent": bool(speaker_ok),
            "mic_loopback_detected": detected,
            "peak_hz": round(peak_freq, 1),
            "snr_db": round(snr_db, 1),
            "message": f"✅ Test Geslaagd: Piek {peak_freq:.0f} Hz ({snr_db:.1f} dB)" if detected else f"❌ Geen duidelijke piek waargenomen ({peak_freq:.0f} Hz, {snr_db:.1f} dB)",
        }
    except Exception as e:
        logger.error("Acoustic loopback test exception: %s", e)
        return {
            "status": "error",
            "passed": False,
            "speaker_sent": False,
            "mic_loopback_detected": False,
            "peak_hz": 0.0,
            "snr_db": 0.0,
            "message": f"Fout tijdens loopback test: {e}",
        }
    finally:
        running = False
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

