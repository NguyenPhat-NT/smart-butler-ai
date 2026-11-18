# -*- coding: utf-8 -*-
"""
Bộ xử lý DSP âm thanh thời gian thực cho loa toàn dãy 4cm (thùng thông hơi, cắt 60Hz):
- Tầng 1: Lọc thông cao (High-Pass Butterworth bậc 2, mặc định 60 Hz) bảo vệ củ loa dưới tần số cổng hơi.
- Tầng 2: Low-Shelf Bass Boost (~135 Hz) tạo độ ấm & nảy ở mức âm lượng thấp.
- Tầng 3: High-Shelf Treble Boost (~5.2 kHz) tăng độ tơi sáng, chi tiết vocal/guitar.
- Tầng 4: Soft-knee peak limiter chống méo clip PCM16.
Tự động dùng thư viện C `libesp32_dsp.so` (nếu có gcc trên Linux OpenStick) hoặc fallback Python thuần đồng nhất toán học.
"""
import asyncio
import ctypes
import logging
import math
import os
import shutil
import subprocess
import threading
from array import array
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
DEFAULT_HIGHPASS_HZ = 60.0
DEFAULT_BASS_DB = 4.0
DEFAULT_TREBLE_DB = 3.5
DEFAULT_PRESET = "loudness"

DEFAULT_NORM_ENABLED = True
DEFAULT_NORM_TARGET_DB = -15.0
DEFAULT_NORM_TARGET_RMS = 10.0 ** (DEFAULT_NORM_TARGET_DB / 20.0)
NORM_MIN_GAIN = 0.30
NORM_MAX_GAIN = 3.00

EQ_PRESETS: Dict[str, Dict[str, Any]] = {
    "loudness": {
        "label": "Loudness loa 4cm (60Hz)",
        "highpass_hz": 60.0,
        "bass_db": 4.0,
        "treble_db": 3.5,
        "enabled": True,
    },
    "warm": {
        "label": "Bass ấm Chill",
        "highpass_hz": 60.0,
        "bass_db": 6.0,
        "treble_db": 2.0,
        "enabled": True,
    },
    "clear": {
        "label": "Giọng & Focus sáng rõ",
        "highpass_hz": 75.0,
        "bass_db": 1.0,
        "treble_db": 5.0,
        "enabled": True,
    },
    "flat": {
        "label": "Tắt Boost (chỉ cắt 60Hz)",
        "highpass_hz": 60.0,
        "bass_db": 0.0,
        "treble_db": 0.0,
        "enabled": True,
    },
    "off": {
        "label": "Tắt hoàn toàn EQ",
        "highpass_hz": 0.0,
        "bass_db": 0.0,
        "treble_db": 0.0,
        "enabled": False,
    },
}


class _Biquad:
    __slots__ = ("b0", "b1", "b2", "a1", "a2", "z1", "z2")

    def __init__(self) -> None:
        self.b0 = 1.0
        self.b1 = 0.0
        self.b2 = 0.0
        self.a1 = 0.0
        self.a2 = 0.0
        self.z1 = 0.0
        self.z2 = 0.0

    def set_identity(self) -> None:
        self.b0 = 1.0
        self.b1 = 0.0
        self.b2 = 0.0
        self.a1 = 0.0
        self.a2 = 0.0

    def reset(self) -> None:
        self.z1 = 0.0
        self.z2 = 0.0

    def set_highpass(self, fs: float, f0: float, q: float = 0.7071067811865476) -> None:
        if f0 <= 1.0 or f0 >= fs * 0.45:
            self.set_identity()
            return
        w0 = 2.0 * math.pi * f0 / fs
        cw = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        self.b0 = ((1.0 + cw) * 0.5) / a0
        self.b1 = (-(1.0 + cw)) / a0
        self.b2 = ((1.0 + cw) * 0.5) / a0
        self.a1 = (-2.0 * cw) / a0
        self.a2 = (1.0 - alpha) / a0

    def set_low_shelf(self, fs: float, f0: float, gain_db: float, slope: float = 0.75) -> None:
        if abs(gain_db) < 0.05 or f0 <= 1.0 or f0 >= fs * 0.45:
            self.set_identity()
            return
        a = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * math.pi * f0 / fs
        cw = math.cos(w0)
        sw = math.sin(w0)
        inner = max(0.0, (a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)
        alpha = (sw * 0.5) * math.sqrt(inner)
        sq = 2.0 * math.sqrt(a) * alpha
        a0 = (a + 1.0) + (a - 1.0) * cw + sq
        self.b0 = (a * ((a + 1.0) - (a - 1.0) * cw + sq)) / a0
        self.b1 = (2.0 * a * ((a - 1.0) - (a + 1.0) * cw)) / a0
        self.b2 = (a * ((a + 1.0) - (a - 1.0) * cw - sq)) / a0
        self.a1 = (-2.0 * ((a - 1.0) + (a + 1.0) * cw)) / a0
        self.a2 = ((a + 1.0) + (a - 1.0) * cw - sq) / a0

    def set_high_shelf(self, fs: float, f0: float, gain_db: float, slope: float = 0.75) -> None:
        if abs(gain_db) < 0.05 or f0 <= 1.0 or f0 >= fs * 0.45:
            self.set_identity()
            return
        a = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * math.pi * f0 / fs
        cw = math.cos(w0)
        sw = math.sin(w0)
        inner = max(0.0, (a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)
        alpha = (sw * 0.5) * math.sqrt(inner)
        sq = 2.0 * math.sqrt(a) * alpha
        a0 = (a + 1.0) - (a - 1.0) * cw + sq
        self.b0 = (a * ((a + 1.0) + (a - 1.0) * cw + sq)) / a0
        self.b1 = (-2.0 * a * ((a - 1.0) + (a + 1.0) * cw)) / a0
        self.b2 = (a * ((a + 1.0) + (a - 1.0) * cw - sq)) / a0
        self.a1 = (2.0 * ((a - 1.0) - (a + 1.0) * cw)) / a0
        self.a2 = ((a + 1.0) - (a - 1.0) * cw - sq) / a0


class _CBiquadStruct(ctypes.Structure):
    _fields_ = [
        ("b0", ctypes.c_double),
        ("b1", ctypes.c_double),
        ("b2", ctypes.c_double),
        ("a1", ctypes.c_double),
        ("a2", ctypes.c_double),
        ("z1", ctypes.c_double),
        ("z2", ctypes.c_double),
    ]


class _CDspStateStruct(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_int),
        ("enabled", ctypes.c_int),
        ("norm_enabled", ctypes.c_int),
        ("track_gain", ctypes.c_double),
        ("hp", _CBiquadStruct),
        ("low_shelf", _CBiquadStruct),
        ("high_shelf", _CBiquadStruct),
    ]


_C_LIB: Optional[ctypes.CDLL] = None
_C_LIB_TRIED = False
_C_LIB_LOCK = threading.Lock()


def _bind_c_dsp_lib(lib: ctypes.CDLL) -> None:
    lib.dsp_configure.argtypes = [
        ctypes.POINTER(_CDspStateStruct),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.c_double,
    ]
    lib.dsp_configure.restype = None
    lib.dsp_reset.argtypes = [ctypes.POINTER(_CDspStateStruct)]
    lib.dsp_reset.restype = None
    lib.dsp_process_s16.argtypes = [
        ctypes.POINTER(_CDspStateStruct),
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
    ]
    lib.dsp_process_s16.restype = None
    if hasattr(lib, "dsp_set_track_norm"):
        lib.dsp_set_track_norm.argtypes = [
            ctypes.POINTER(_CDspStateStruct),
            ctypes.c_int,
            ctypes.c_double,
        ]
        lib.dsp_set_track_norm.restype = None
    if hasattr(lib, "dsp_analyze_block_s16"):
        lib.dsp_analyze_block_s16.argtypes = [
            ctypes.POINTER(_CBiquadStruct),
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        lib.dsp_analyze_block_s16.restype = None
    if hasattr(lib, "dsp_scale_s16"):
        lib.dsp_scale_s16.argtypes = [
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_double,
        ]
        lib.dsp_scale_s16.restype = None
    if hasattr(lib, "dsp_mix_s16"):
        lib.dsp_mix_s16.argtypes = [
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
        ]
        lib.dsp_mix_s16.restype = None


def _load_c_dsp_lib() -> Optional[ctypes.CDLL]:
    global _C_LIB, _C_LIB_TRIED
    if _C_LIB is not None:
        return _C_LIB
    if _C_LIB_TRIED or os.name != "posix":
        return None
    with _C_LIB_LOCK:
        if _C_LIB is not None:
            return _C_LIB
        if _C_LIB_TRIED:
            return None
        _C_LIB_TRIED = True
        base_dir = os.path.dirname(os.path.abspath(__file__))
        c_path = os.path.join(base_dir, "esp32_dsp.c")
        so_path = os.path.join(base_dir, "libesp32_dsp.so")
        try:
            need_build = os.path.isfile(c_path) and (
                not os.path.isfile(so_path) or os.path.getmtime(c_path) > os.path.getmtime(so_path)
            )
            if need_build and shutil.which("gcc"):
                subprocess.run(
                    ["gcc", "-O2", "-shared", "-fPIC", c_path, "-lm", "-o", so_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
            if os.path.isfile(so_path):
                lib = ctypes.CDLL(so_path)
                if not hasattr(lib, "dsp_set_track_norm") and os.path.isfile(c_path) and shutil.which("gcc"):
                    subprocess.run(
                        ["gcc", "-O2", "-shared", "-fPIC", c_path, "-lm", "-o", so_path],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=15,
                    )
                    lib = ctypes.CDLL(so_path)
                _bind_c_dsp_lib(lib)
                _C_LIB = lib
                logger.info("🎛️ [DSP] Đã nạp bộ lọc C libesp32_dsp.so (RBJ 60Hz HP + Bass/Treble + Track ReplayGain)")
        except Exception as e:
            logger.warning(f"🎛️ [DSP] Dùng fallback Python biquad (không nạp được libesp32_dsp.so): {e}")
            _C_LIB = None
        return _C_LIB


def clamp_eq_params(
    preset: Optional[str] = None,
    highpass_hz: Optional[float] = None,
    bass_db: Optional[float] = None,
    treble_db: Optional[float] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Chuẩn hóa bộ tham số EQ về miền an toàn cho củ loa 4cm."""
    raw_preset = str(preset or DEFAULT_PRESET).strip().lower()
    if raw_preset in EQ_PRESETS:
        base = dict(EQ_PRESETS[raw_preset])
        p_name = raw_preset
    else:
        base = dict(EQ_PRESETS[DEFAULT_PRESET])
        p_name = "custom"

    hp = float(base["highpass_hz"] if highpass_hz is None else highpass_hz)
    hp = 0.0 if hp <= 1.0 else max(35.0, min(160.0, hp))
    bs = max(-6.0, min(9.0, float(base["bass_db"] if bass_db is None else bass_db)))
    tr = max(-6.0, min(9.0, float(base["treble_db"] if treble_db is None else treble_db)))
    en = bool(base["enabled"] if enabled is None else enabled)
    if p_name == "off":
        en = False
    return {
        "preset": p_name,
        "label": EQ_PRESETS.get(p_name, {}).get("label", "Tùy chỉnh"),
        "highpass_hz": round(hp, 1),
        "bass_db": round(bs, 1),
        "treble_db": round(tr, 1),
        "enabled": en,
    }


def compute_track_norm_gain(
    rms_db: Optional[float],
    peak_db: Optional[float] = None,
    target_db: float = DEFAULT_NORM_TARGET_DB,
) -> Tuple[float, bool]:
    """
    Quy đổi mức RMS/LUFS đã đo của bài hát sang hệ số gain tiền xử lý an toàn.
    Trả về `(track_gain, has_track_gain)`.
    """
    if rms_db is None:
        return 1.0, False
    try:
        r_db = float(rms_db)
    except (TypeError, ValueError):
        return 1.0, False
    if not math.isfinite(r_db) or r_db < -60.0 or r_db > 6.0:
        return 1.0, False

    t_db = max(-24.0, min(-8.0, float(target_db)))
    gain = 10.0 ** ((t_db - r_db) / 20.0)

    # Giới hạn đỉnh trước soft-knee limiter tối đa ~2.2 (+6.8 dB) với bài giàu dynamic
    if peak_db is not None:
        try:
            p_db = float(peak_db)
            if math.isfinite(p_db) and -40.0 <= p_db <= 6.0:
                pk_lin = 10.0 ** (p_db / 20.0)
                if pk_lin > 0.05:
                    gain = min(gain, max(1.25, 2.20 / pk_lin))
        except (TypeError, ValueError):
            pass

    gain = max(NORM_MIN_GAIN, min(NORM_MAX_GAIN, gain))
    return round(gain, 4), True


class LoudnessAnalyzer:
    """Đo độ lớn RMS chủ động theo block 400ms (lọc HP 60Hz + gate hai tầng kiểu EBU R128)."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, highpass_hz: float = DEFAULT_HIGHPASS_HZ):
        self.sample_rate = max(8000, int(sample_rate))
        self.highpass_hz = float(highpass_hz)
        self._block_samples = max(480, int(round(self.sample_rate * 0.4)))
        self._block_bytes = self._block_samples * 2
        self._buf = bytearray()
        self._block_ms: List[float] = []
        self._max_peak: float = 0.0

        self._c_lib = _load_c_dsp_lib()
        self._hp = _Biquad()
        self._hp.set_highpass(float(self.sample_rate), self.highpass_hz)
        self._c_hp: Optional[_CBiquadStruct] = None
        if self._c_lib is not None and hasattr(self._c_lib, "dsp_analyze_block_s16"):
            self._c_hp = _CBiquadStruct(
                b0=self._hp.b0,
                b1=self._hp.b1,
                b2=self._hp.b2,
                a1=self._hp.a1,
                a2=self._hp.a2,
                z1=0.0,
                z2=0.0,
            )

    def _process_block(self, raw_block: bytes) -> None:
        usable = len(raw_block) - (len(raw_block) % 2)
        if usable < 2:
            return
        n_samples = usable // 2
        raw = raw_block[:usable]
        if self._c_lib is not None and self._c_hp is not None and hasattr(self._c_lib, "dsp_analyze_block_s16"):
            arr = (ctypes.c_int16 * n_samples).from_buffer_copy(raw)
            out_ms = ctypes.c_double(0.0)
            out_pk = ctypes.c_double(0.0)
            self._c_lib.dsp_analyze_block_s16(
                ctypes.byref(self._c_hp),
                arr,
                n_samples,
                ctypes.byref(out_ms),
                ctypes.byref(out_pk),
            )
            ms_val = float(out_ms.value)
            pk_val = float(out_pk.value)
        else:
            samples = array("h")
            samples.frombytes(raw)
            hp = self._hp
            inv = 1.0 / 32768.0
            sum_sq = 0.0
            pk_val = 0.0
            for s in samples:
                x = s * inv
                ax = abs(x)
                if ax > pk_val:
                    pk_val = ax
                y = hp.b0 * x + hp.z1
                hp.z1 = hp.b1 * x - hp.a1 * y + hp.z2
                hp.z2 = hp.b2 * x - hp.a2 * y
                sum_sq += y * y
            ms_val = sum_sq / float(n_samples)

        if pk_val > self._max_peak:
            self._max_peak = pk_val
        self._block_ms.append(ms_val)

    def feed_pcm16(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        self._buf.extend(pcm_bytes)
        while len(self._buf) >= self._block_bytes:
            blk = bytes(self._buf[:self._block_bytes])
            del self._buf[:self._block_bytes]
            self._process_block(blk)

    def finish(self) -> Optional[Dict[str, float]]:
        rem = len(self._buf) - (len(self._buf) % 2)
        if rem >= max(960, self._block_bytes // 10):
            self._process_block(bytes(self._buf[:rem]))
        self._buf.clear()

        if not self._block_ms:
            return None

        abs_gate_ms = 10.0 ** (-45.0 / 10.0)
        gated1 = [m for m in self._block_ms if m >= abs_gate_ms]
        if not gated1:
            gated1 = [m for m in self._block_ms if m > 1e-8]
        if not gated1:
            return None

        avg1 = sum(gated1) / float(len(gated1))
        rel_gate = avg1 * 0.1  # -10 dB so voi muc trung binh chu dong
        gated2 = [m for m in gated1 if m >= rel_gate]
        if not gated2:
            gated2 = gated1
        avg2 = sum(gated2) / float(len(gated2))

        rms_db = round(10.0 * math.log10(max(1e-12, avg2)), 2)
        peak_db = round(20.0 * math.log10(max(1e-6, self._max_peak)), 2)
        return {"rms_db": rms_db, "peak_db": peak_db}


class DspProcessor:
    """Bộ lọc High-Pass + Bass/Treble Shelf + Track ReplayGain giữ nguyên dynamic tự nhiên của bài hát."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = max(8000, int(sample_rate))
        self._lock = threading.Lock()
        self._c_lib = _load_c_dsp_lib()
        self._c_state = _CDspStateStruct() if self._c_lib is not None else None
        self._hp = _Biquad()
        self._low = _Biquad()
        self._high = _Biquad()

        self._norm_enabled: bool = False
        self._has_track_gain: bool = False
        self._track_gain: float = 1.0
        self._norm_target_db: float = DEFAULT_NORM_TARGET_DB

        self.config: Dict[str, Any] = clamp_eq_params(DEFAULT_PRESET)
        self._apply_config_locked()
        self._apply_norm_locked()

    def _apply_norm_locked(self) -> None:
        if self._c_lib is not None and self._c_state is not None and hasattr(self._c_lib, "dsp_set_track_norm"):
            self._c_lib.dsp_set_track_norm(
                ctypes.byref(self._c_state),
                1 if self._norm_enabled else 0,
                float(self._track_gain),
            )

    def _apply_config_locked(self) -> None:
        cfg = self.config
        if self._c_lib is not None and self._c_state is not None:
            self._c_lib.dsp_configure(
                ctypes.byref(self._c_state),
                self.sample_rate,
                1 if cfg["enabled"] else 0,
                float(cfg["highpass_hz"]),
                float(cfg["bass_db"]),
                float(cfg["treble_db"]),
            )
        fs = float(self.sample_rate)
        self._hp.set_highpass(fs, float(cfg["highpass_hz"]))
        self._low.set_low_shelf(fs, 135.0, float(cfg["bass_db"]), 0.75)
        self._high.set_high_shelf(fs, 5200.0, float(cfg["treble_db"]), 0.75)

    def configure(
        self,
        preset: Optional[str] = None,
        highpass_hz: Optional[float] = None,
        bass_db: Optional[float] = None,
        treble_db: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.config = clamp_eq_params(
                preset=preset,
                highpass_hz=highpass_hz,
                bass_db=bass_db,
                treble_db=treble_db,
                enabled=enabled,
            )
            self._apply_config_locked()
            return dict(self.config)

    def configure_norm(
        self,
        enabled: Optional[bool] = None,
        target_db: Optional[float] = None,
        track_gain: Optional[float] = None,
        has_track_gain: Optional[bool] = None,
        reset_agc: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if enabled is not None:
                self._norm_enabled = bool(enabled)
            if target_db is not None:
                t_db = max(-24.0, min(-8.0, float(target_db)))
                self._norm_target_db = round(t_db, 2)
            if track_gain is not None:
                self._track_gain = max(0.20, min(4.00, float(track_gain)))
            if has_track_gain is not None:
                self._has_track_gain = bool(has_track_gain)
            self._apply_norm_locked()
            return self._norm_status_locked()

    def _norm_status_locked(self) -> Dict[str, Any]:
        eff_g = self._track_gain if self._norm_enabled else 1.0
        return {
            "enabled": self._norm_enabled,
            "mode": "track_average",
            "target_db": self._norm_target_db,
            "has_track_gain": self._has_track_gain,
            "track_gain": round(self._track_gain, 4),
            "track_db": round(20.0 * math.log10(max(1e-4, self._track_gain)), 1),
            "effective_gain": round(eff_g, 4),
            "effective_db": round(20.0 * math.log10(max(1e-4, eff_g)), 1),
        }

    def get_norm_status(self) -> Dict[str, Any]:
        with self._lock:
            return self._norm_status_locked()

    def reset_norm(self) -> None:
        pass

    def reset(self) -> None:
        with self._lock:
            if self._c_lib is not None and self._c_state is not None:
                self._c_lib.dsp_reset(ctypes.byref(self._c_state))
            self._hp.reset()
            self._low.reset()
            self._high.reset()

    def process_pcm16(self, pcm_bytes: bytes) -> bytes:
        if not pcm_bytes:
            return b""
        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable <= 0:
            return b""
        raw = pcm_bytes[:usable]
        with self._lock:
            do_eq = bool(self.config.get("enabled", True))
            do_norm = bool(self._norm_enabled)
            if not do_eq and not do_norm:
                return raw
            n_samples = usable // 2
            if self._c_lib is not None and self._c_state is not None:
                buf = (ctypes.c_int16 * n_samples).from_buffer_copy(raw)
                self._c_lib.dsp_process_s16(ctypes.byref(self._c_state), buf, n_samples)
                return ctypes.string_at(buf, usable)

            samples = array("h")
            samples.frombytes(raw)
            hp = self._hp
            low = self._low
            high = self._high
            inv = 1.0 / 32768.0
            knee = 0.78
            span = 0.21
            tg = self._track_gain if do_norm else 1.0

            for i, s in enumerate(samples):
                x = s * inv
                if do_norm:
                    x *= tg

                if do_eq:
                    y = hp.b0 * x + hp.z1
                    hp.z1 = hp.b1 * x - hp.a1 * y + hp.z2
                    hp.z2 = hp.b2 * x - hp.a2 * y
                    x = y

                    y = low.b0 * x + low.z1
                    low.z1 = low.b1 * x - low.a1 * y + low.z2
                    low.z2 = low.b2 * x - low.a2 * y
                    x = y

                    y = high.b0 * x + high.z1
                    high.z1 = high.b1 * x - high.a1 * y + high.z2
                    high.z2 = high.b2 * x - high.a2 * y
                    x = y

                if x > knee:
                    x = knee + span * math.tanh((x - knee) / span)
                elif x < -knee:
                    x = -knee - span * math.tanh((-x - knee) / span)

                val = int(round(x * 32767.0))
                if val > 32767:
                    val = 32767
                elif val < -32768:
                    val = -32768
                samples[i] = val

            return samples.tobytes()


def generate_chime_pcm(kind: str = "focus_done", sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Tổng hợp tiếng chuông báo êm tai thuần PCM16 mono 24kHz (không cần file âm thanh ngoài):
    - focus_done: hợp âm nhẹ C5 -> E5 -> G5 báo hết phiên tập trung.
    - wake_alarm: chuỗi nốt sáng ấm C5 -> G5 -> B5 -> D6 gọi dậy dịu dàng.
    - ping: 2 nốt nhẹ E5 -> G5 báo chuyển chế độ.
    """
    if kind == "wake_alarm":
        notes: List[Tuple[float, float, float]] = [
            (0.00, 523.25, 0.45),
            (0.22, 783.99, 0.50),
            (0.46, 987.77, 0.55),
            (0.72, 1174.66, 0.85),
            (1.25, 523.25, 0.45),
            (1.48, 783.99, 0.55),
            (1.74, 1046.50, 0.90),
        ]
        total_sec = 2.65
        base_amp = 0.34
    elif kind == "ping":
        notes = [
            (0.00, 659.25, 0.22),
            (0.11, 783.99, 0.32),
        ]
        total_sec = 0.45
        base_amp = 0.24
    else:
        # focus_done mặc định
        notes = [
            (0.00, 523.25, 0.48),
            (0.18, 659.25, 0.55),
            (0.38, 783.99, 0.85),
        ]
        total_sec = 1.25
        base_amp = 0.30

    n_total = max(1, int(round(total_sec * sample_rate)))
    buf = [0.0] * n_total
    two_pi = 2.0 * math.pi
    for start_s, freq, dur_s in notes:
        i0 = max(0, int(round(start_s * sample_rate)))
        i1 = min(n_total, i0 + int(round(dur_s * sample_rate)))
        attack = max(1, int(round(0.015 * sample_rate)))
        for idx in range(i0, i1):
            dt = (idx - i0) / float(sample_rate)
            env = math.exp(-dt * (4.2 / max(0.2, dur_s)))
            if (idx - i0) < attack:
                env *= (idx - i0) / float(attack)
            tone = (
                0.78 * math.sin(two_pi * freq * dt)
                + 0.16 * math.sin(two_pi * freq * 2.0 * dt)
                + 0.06 * math.sin(two_pi * freq * 3.0 * dt)
            )
            buf[idx] += base_amp * env * tone

    out = array("h")
    for x in buf:
        v = max(-32768, min(32767, int(round(max(-0.92, min(0.92, x)) * 32767.0))))
        out.append(v)
    return out.tobytes()


def scale_pcm16(pcm_bytes: bytes, gain: float) -> bytes:
    """Nhân biên độ PCM 16-bit signed LE bằng C `dsp_scale_s16` (hoặc Python `array('h')` nếu thiếu `.so`)."""
    if not pcm_bytes:
        return b""
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    raw = pcm_bytes[:usable]
    n_samples = usable // 2
    g = float(gain)

    lib = _load_c_dsp_lib()
    if lib is not None and hasattr(lib, "dsp_scale_s16"):
        in_buf = (ctypes.c_int16 * n_samples).from_buffer_copy(raw)
        out_buf = (ctypes.c_int16 * n_samples)()
        lib.dsp_scale_s16(in_buf, out_buf, n_samples, g)
        return ctypes.string_at(out_buf, usable)

    arr = array("h")
    arr.frombytes(raw)
    out = array("h", (max(-32768, min(32767, int(round(s * g)))) for s in arr))
    return out.tobytes()


def mix_pcm16(pcm_a: bytes, gain_a: float, pcm_b: bytes, gain_b: float) -> bytes:
    """Trộn hai luồng PCM 16-bit signed LE theo hệ số gain bằng C `dsp_mix_s16` hoặc fallback Python."""
    usable = min(len(pcm_a), len(pcm_b))
    usable -= usable % 2
    if usable <= 0:
        return b""
    ra = pcm_a[:usable]
    rb = pcm_b[:usable]
    n_samples = usable // 2
    ga = float(gain_a)
    gb = float(gain_b)

    lib = _load_c_dsp_lib()
    if lib is not None and hasattr(lib, "dsp_mix_s16"):
        buf_a = (ctypes.c_int16 * n_samples).from_buffer_copy(ra)
        buf_b = (ctypes.c_int16 * n_samples).from_buffer_copy(rb)
        out_buf = (ctypes.c_int16 * n_samples)()
        lib.dsp_mix_s16(buf_a, ga, buf_b, gb, out_buf, n_samples)
        return ctypes.string_at(out_buf, usable)

    arr_a = array("h")
    arr_b = array("h")
    arr_a.frombytes(ra)
    arr_b.frombytes(rb)
    out = array(
        "h",
        (max(-32768, min(32767, int(round(arr_a[i] * ga + arr_b[i] * gb)))) for i in range(n_samples)),
    )
    return out.tobytes()


async def warmup_async() -> bool:
    """Biên dịch/nạp thư viện C DSP ở worker thread lúc khởi động để tránh nghẽn event loop."""
    return (await asyncio.to_thread(_load_c_dsp_lib)) is not None
