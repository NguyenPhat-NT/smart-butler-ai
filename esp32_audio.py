# -*- coding: utf-8 -*-
"""
ESP32-C3 Smart Speaker & Standalone YouTube Jukebox Engine (chạy hoàn toàn trên OpenStick):
- Quản lý kho cache nhạc cục bộ (ưu tiên ổ USB ngoài `/mnt/usb` khi sẵn sàng; tự rơi về `music_cache/` 150 MB nếu USB lỗi/ngắt).
- Tìm kiếm & tải âm thanh `.m4a` nhẹ qua `yt-dlp`, xong là thoát ngay để giải phóng RAM.
- Giải mã luồng âm thanh bằng `faad`, `mpg123`, `opusdec`, `sox` sang PCM 24000 Hz 16-bit mono (~< 3 MB RAM).
- Trộn giọng nói Smart Butler (Audio Ducking: tự động hạ nhạc xuống 20% khi trợ lý gửi voice note / nhắc việc).
- Xử lý nút chạm TTP223 (`single`, `double`, `long`), cảm biến nhiệt ẩm DHT11 và ý định âm nhạc tự nhiên tiếng Việt.
"""
import io
import os
import re
import sys
import json
import time
import wave
import glob
import shlex
import shutil
import signal
import struct
import subprocess
import random
import datetime
import tempfile
import unicodedata
import asyncio
import contextvars
import ipaddress
import logging
from array import array
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from urllib.parse import urlparse, parse_qs, quote
import aiohttp

import config
import database
import esp32_dsp
import lan_events
import storage_helper

logger = logging.getLogger(__name__)

# --- Thông số luồng PCM chuẩn cho ESP32-C3 + MAX98357 ---
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH * CHANNELS  # 960 bytes
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS  # 48000 bytes/s

DUCK_MUSIC_RATIO = 0.20
MAX_VOICE_BUFFER_BYTES = BYTES_PER_SEC * 60  # Tối đa 60s thoại chờ phát (~2.88 MB)
MAX_CACHE_BYTES = config.INTERNAL_MUSIC_CACHE_MAX_MB * 1024 * 1024  # 150 MB LRU cap dự phòng eMMC
USB_MAX_CACHE_BYTES = config.USB_MUSIC_CACHE_MAX_MB * 1024 * 1024  # mặc định 4 GB trên ổ USB ngoài
USB_FREE_HEADROOM_BYTES = 1024 * 1024 * 1024  # Luôn chừa trống >= 1 GB trên ổ USB (hoặc >= 128 MB)
EMMC_MAX_DOWNLOAD_MB = 45
USB_MAX_DOWNLOAD_MB = 150
DEFAULT_VOLUME = 12
DEFAULT_VOICE_AUTOPLAY = False
_META_JUKEBOX_VOLUME = "esp32_jukebox_volume"
_META_VOICE_AUTOPLAY = "esp32_voice_autoplay"
_META_EQ_SETTINGS = "esp32_eq_settings"
_META_NORM_ENABLED = "esp32_norm_enabled"
_META_ALARMS = "esp32_alarms"
_META_TIMED_SESSION = "esp32_timed_session"
STREAM_BATCH_FRAMES = 1    # 20ms (960 bytes): vừa khít 1 gói TCP/Wi-Fi (<1460 MSS) và phản hồi mượt
PREFILL_LEAD_SEC = 0.34    # Bơm trước ~340ms khi bắt đầu/nối lại để làm đầy jitter buffer trên ESP32
_SILENCE_FRAME = b"\x00" * FRAME_BYTES

# Chuẩn Opus qua WebSocket (60ms = 1440 mẫu @ 24kHz mono, bitrate 32kbps ~240 bytes/gói)
OPUS_FRAME_MS = 60
OPUS_FRAME_SAMPLES = SAMPLE_RATE * OPUS_FRAME_MS // 1000  # 1440 samples
OPUS_FRAME_BYTES = OPUS_FRAME_SAMPLES * SAMPLE_WIDTH * CHANNELS  # 2880 bytes PCM16
OPUS_PCM_FRAMES_PER_PKT = OPUS_FRAME_MS // FRAME_MS  # 3 khung PCM 20ms = 1 gói Opus 60ms
OPUS_BITRATE = 32000
OPUS_MAX_PACKET_BYTES = 768
OPUS_PREFILL_LEAD_SEC = 0.78  # Bơm trước ~13 gói Opus (780ms, ~3.1 KB) cho jitter buffer
WS_OPUS_AUDIO_TYPE = 0x01

LIVE_AUDIO_SILENCE_TIMEOUT_SEC = 300.0  # Tự ngắt sau 5 phút im lặng liên tục
LIVE_AUDIO_IDLE_STALE_SEC = 15.0        # Tự ngắt nếu mất luồng gói quá 15 giây
LIVE_BUFFER_MAX_BYTES = FRAME_BYTES * 10   # 200ms trần buffer tránh trễ tích lũy
LIVE_BUFFER_TRIM_TO_BYTES = FRAME_BYTES * 4  # Cắt gọn về 80ms khi đầy
LIVE_SILENCE_PEAK_THRESHOLD = 45        # Ngưỡng biên độ int16 coi là có tiếng
LIVE_OPUS_PREFILL_LEAD_SEC = 0.14       # Chế độ Live độ trễ thấp (~140ms)

_OPUS_APPLICATION_AUDIO = 2049
_OPUS_SET_BITRATE_REQUEST = 4002
_OPUS_SET_COMPLEXITY_REQUEST = 4010
_OPUS_SET_DTX_REQUEST = 4016
_OPUS_SET_SIGNAL_REQUEST = 4024
_OPUS_SIGNAL_MUSIC = 3002
_OPUS_RESET_STATE = 4028

_LIBOPUS = None
_LIBOPUS_TRIED = False


def _load_libopus():
    """Tải thư viện hệ thống libopus qua ctypes (0 tiến trình con)."""
    global _LIBOPUS, _LIBOPUS_TRIED
    if _LIBOPUS_TRIED:
        return _LIBOPUS
    _LIBOPUS_TRIED = True

    import ctypes
    import ctypes.util

    candidates = []
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    candidates.extend([
        "/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "libopus.so.0",
        "libopus-0.dll",
        "opus.dll",
    ])

    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
            lib.opus_encoder_create.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
            ]
            lib.opus_encoder_create.restype = ctypes.c_void_p
            lib.opus_encode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int16),
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.c_int32,
            ]
            lib.opus_encode.restype = ctypes.c_int32
            lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
            lib.opus_encoder_destroy.restype = None
            lib.opus_strerror.argtypes = [ctypes.c_int]
            lib.opus_strerror.restype = ctypes.c_char_p
            _LIBOPUS = lib
            return _LIBOPUS
        except Exception:
            continue
    return None


def is_opus_available() -> bool:
    """Kiểm tra libopus sẵn sàng trên hệ thống hay chưa."""
    return _load_libopus() is not None


class OpusStreamEncoder:
    """Bộ nén Opus thời gian thực dùng libopus.so qua ctypes (60ms @ 24kHz mono)."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        bitrate: int = OPUS_BITRATE,
        complexity: int = 5,
    ):
        import ctypes

        lib = _load_libopus()
        if lib is None:
            raise RuntimeError("Thư viện libopus không khả dụng trên hệ thống này.")
        self._ctypes = ctypes
        self._lib = lib
        err = ctypes.c_int(0)
        enc = lib.opus_encoder_create(
            int(sample_rate),
            int(channels),
            _OPUS_APPLICATION_AUDIO,
            ctypes.byref(err),
        )
        if not enc or err.value != 0:
            raise RuntimeError(f"opus_encoder_create lỗi {err.value}")
        self._enc = ctypes.c_void_p(enc)
        self._in_buf = (ctypes.c_int16 * OPUS_FRAME_SAMPLES)()
        self._out_buf = (ctypes.c_ubyte * OPUS_MAX_PACKET_BYTES)()

        # Biến số vararg (request, int32): để argtypes=None khi gọi opus_encoder_ctl
        for req, val in (
            (_OPUS_SET_BITRATE_REQUEST, int(bitrate)),
            (_OPUS_SET_COMPLEXITY_REQUEST, int(complexity)),
            (_OPUS_SET_SIGNAL_REQUEST, _OPUS_SIGNAL_MUSIC),
            (_OPUS_SET_DTX_REQUEST, 0),
        ):
            rc = lib.opus_encoder_ctl(self._enc, ctypes.c_int(req), ctypes.c_int32(val))
            if rc != 0:
                self.close()
                raise RuntimeError(f"opus_encoder_ctl({req}) lỗi {rc}")

    def encode_frame(self, pcm_bytes: bytes) -> bytes:
        """Nén 1 khung 60ms PCM16 mono (<=2880 bytes, tự đệm 0 nếu thiếu) thành gói Opus thô."""
        if not self._enc:
            raise RuntimeError("OpusStreamEncoder đã đóng.")
        ctypes = self._ctypes
        raw = pcm_bytes[:OPUS_FRAME_BYTES]
        if len(raw) < OPUS_FRAME_BYTES:
            raw = raw + (b"\x00" * (OPUS_FRAME_BYTES - len(raw)))
        ctypes.memmove(self._in_buf, raw, OPUS_FRAME_BYTES)
        out_len = int(
            self._lib.opus_encode(
                self._enc,
                self._in_buf,
                OPUS_FRAME_SAMPLES,
                self._out_buf,
                OPUS_MAX_PACKET_BYTES,
            )
        )
        if out_len < 0:
            msg = self._lib.opus_strerror(ctypes.c_int(out_len)).decode("utf-8", errors="ignore")
            raise RuntimeError(f"opus_encode lỗi {out_len}: {msg}")
        return bytes(self._out_buf[:out_len])

    def reset(self) -> None:
        if self._enc:
            self._lib.opus_encoder_ctl(self._enc, self._ctypes.c_int(_OPUS_RESET_STATE))

    def close(self) -> None:
        enc = getattr(self, "_enc", None)
        if enc:
            self._enc = None
            try:
                self._lib.opus_encoder_destroy(enc)
            except Exception:
                pass

    def __del__(self):
        self.close()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "music_cache")
LOCAL_YT_CACHE_DIR = os.path.join(BASE_DIR, ".yt_dlp_cache")
YOUTUBE_COOKIES_PATH = os.path.abspath(
    os.getenv("YOUTUBE_COOKIES_PATH", os.path.join(BASE_DIR, "youtube_cookies.txt"))
)
INDEX_FILENAME = "index.json"
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:[^#\s]*&)?v=)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
_ANY_YT_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s]+",
    re.IGNORECASE,
)
_ANY_SC_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|on\.)?soundcloud\.com/[^\s]+",
    re.IGNORECASE,
)

MOOD_SEARCH_POOLS: Dict[str, List[str]] = {
    "focus": [
        "lofi hip hop beats study focus instrumental",
        "nhạc lofi không lời tập trung làm việc nhẹ nhàng",
        "piano rain study ambient instrumental",
    ],
    "chill": [
        "nhạc indie việt chill nhẹ nhàng",
        "lofi acoustic việt nam thư giãn",
        "nhạc chill buổi tối nhẹ nhàng",
    ],
    "nap": [
        "nhạc không lời ru ngủ tiếng mưa nhẹ nhàng",
        "ambient sleep piano soft rain",
    ],
    "morning": [
        "nhạc acoustic buổi sáng nhẹ nhàng vui tươi",
        "morning coffee acoustic chill indie",
    ],
    "rainy": [
        "nhạc indie việt nghe ngày mưa chill",
        "rainy day lofi acoustic chill",
    ],
    "energetic": [
        "nhạc indie việt vui tươi yêu đời",
        "upbeat chill acoustic morning",
    ],
}

_MOOD_CACHE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "focus": (
        "focus", "lofi", "lo-fi", "instrumental", "piano", "study", "ambient",
        "rain", "mưa", "không lời", "acoustic", "coffee", "tập trung", "chill",
    ),
    "chill": (
        "chill", "indie", "ngọt", "vũ", "chillies", "acoustic", "lofi",
        "thư giãn", "nhẹ", "đêm", "rain",
    ),
    "nap": (
        "ngủ", "sleep", "rain", "mưa", "ru", "piano", "ambient", "lullaby",
        "lofi", "không lời",
    ),
    "morning": (
        "sáng", "morning", "coffee", "cà phê", "acoustic", "indie", "ngọt",
        "chill", "nắng",
    ),
    "rainy": ("mưa", "rain", "indie", "chill", "lofi", "vũ", "acoustic"),
    "energetic": ("vui", "nắng", "morning", "upbeat", "indie", "pop"),
}

_LAST_DOWNLOAD_ERROR: Dict[str, Any] = {}


def get_last_download_error() -> Dict[str, Any]:
    return dict(_LAST_DOWNLOAD_ERROR) if _LAST_DOWNLOAD_ERROR else {}


def _set_last_download_error(
    code: str,
    message: str,
    query: str = "",
    fallback_note: str = "",
) -> Dict[str, Any]:
    global _LAST_DOWNLOAD_ERROR
    _LAST_DOWNLOAD_ERROR = {
        "code": str(code or "unknown"),
        "message": str(message or "").strip(),
        "query": str(query or "")[:140],
        "fallback_note": str(fallback_note or "").strip(),
        "at": time.time(),
    }
    return dict(_LAST_DOWNLOAD_ERROR)


def clear_last_download_error() -> None:
    global _LAST_DOWNLOAD_ERROR
    _LAST_DOWNLOAD_ERROR = {}


def _parse_netscape_cookie_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            lines.append(line)
    return lines


def get_youtube_cookies_path() -> Optional[str]:
    """Trả về đường dẫn file cookies.txt nếu tồn tại, khác rỗng và có ít nhất 1 dòng cookie hợp lệ."""
    p = YOUTUBE_COOKIES_PATH
    if not p or not os.path.isfile(p):
        return None
    try:
        if os.path.getsize(p) <= 0:
            return None
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read(256 * 1024)
        if _parse_netscape_cookie_lines(txt):
            return p
    except OSError:
        return None
    return None


def get_cookies_status() -> Dict[str, Any]:
    p = get_youtube_cookies_path()
    if not p:
        return {
            "configured": False,
            "cookie_count": 0,
            "has_youtube_auth": False,
            "updated_at": 0.0,
        }
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read(512 * 1024)
        lines = _parse_netscape_cookie_lines(txt)
        yt_auth = any("youtube.com" in ln.lower() or "google.com" in ln.lower() for ln in lines)
        return {
            "configured": True,
            "cookie_count": len(lines),
            "has_youtube_auth": yt_auth,
            "updated_at": os.path.getmtime(p),
        }
    except OSError:
        return {
            "configured": False,
            "cookie_count": 0,
            "has_youtube_auth": False,
            "updated_at": 0.0,
        }


def save_youtube_cookies(cookies_text: str) -> Dict[str, Any]:
    raw = str(cookies_text or "").strip()
    lines = _parse_netscape_cookie_lines(raw)
    if not lines:
        raise ValueError(
            "Nội dung cookies không đúng chuẩn Netscape cookies.txt (thiếu cột phân cách Tab)."
        )
    if not any("youtube.com" in ln.lower() or "google.com" in ln.lower() for ln in lines):
        raise ValueError("Không thấy dòng cookie của youtube.com hoặc google.com trong file đã dán.")

    parent = os.path.dirname(YOUTUBE_COOKIES_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    header = "# Netscape HTTP Cookie File\n"
    body = header + "\n".join(lines) + "\n"
    tmp_path = YOUTUBE_COOKIES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    try:
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, YOUTUBE_COOKIES_PATH)
    clear_last_download_error()
    logger.info(f"🍪 [Jukebox] Đã lưu {len(lines)} dòng YouTube cookies")
    return get_cookies_status()


def clear_youtube_cookies() -> Dict[str, Any]:
    try:
        if os.path.isfile(YOUTUBE_COOKIES_PATH):
            os.remove(YOUTUBE_COOKIES_PATH)
    except OSError as e:
        logger.warning(f"Không xóa được file cookies: {e}")
    return get_cookies_status()


def get_cache_dir_with_mode() -> Tuple[str, bool]:
    return storage_helper.get_expendable_dir("music_cache", CACHE_DIR, create=True)


def get_cache_dir() -> str:
    return get_cache_dir_with_mode()[0]


def get_effective_max_cache_bytes() -> int:
    _, using_usb = get_cache_dir_with_mode()
    if not using_usb:
        return MAX_CACHE_BYTES
    snap = storage_helper.get_usb_storage_snapshot()
    total_b = int(snap.get("total_bytes") or 0)
    if total_b <= 0:
        return USB_MAX_CACHE_BYTES
    reserve = min(USB_FREE_HEADROOM_BYTES, max(128 * 1024 * 1024, total_b // 5))
    return max(MAX_CACHE_BYTES, min(USB_MAX_CACHE_BYTES, max(0, total_b - reserve)))


def _index_path() -> str:
    return os.path.join(get_cache_dir(), INDEX_FILENAME)


def ensure_cache_dir() -> str:
    cdir, using_usb = get_cache_dir_with_mode()
    try:
        os.makedirs(cdir, exist_ok=True)
        return cdir
    except OSError as e:
        if using_usb:
            storage_helper.invalidate_usb_health(str(e))
            os.makedirs(CACHE_DIR, exist_ok=True)
            return CACHE_DIR
        raise


def normalize_query(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def extract_youtube_video_id(text: str) -> Optional[str]:
    if not text:
        return None
    m = _YT_ID_RE.search(str(text))
    return m.group(1) if m else None


def extract_youtube_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ANY_YT_URL_RE.search(str(text))
    return m.group(0).rstrip(".,!?:;)]") if m else None


def normalize_youtube_single_url(url: str) -> str:
    """Chuẩn hóa link YouTube đơn lẻ (bỏ ?list=RD..., start_radio, t=... để yt-dlp tải nhanh và đúng bài)."""
    clean = str(url or "").strip().rstrip(".,!?:;)]")
    if not clean:
        return ""
    vid = extract_youtube_video_id(clean)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return clean


def extract_soundcloud_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ANY_SC_URL_RE.search(str(text))
    return m.group(0).rstrip(".,!?:;)]") if m else None


def extract_music_url(text: str) -> Optional[str]:
    yt = extract_youtube_url(text)
    if yt:
        return normalize_youtube_single_url(yt)
    return extract_soundcloud_url(text)


def _load_cache_index_from_dir(cdir: str) -> Dict[str, Any]:
    """Đọc `index.json` từ `cdir` và tự lọc mục thiếu file."""
    ipath = os.path.join(cdir, INDEX_FILENAME)
    if not os.path.isfile(ipath):
        return {"tracks": {}}
    with open(ipath, "r", encoding="utf-8") as f:
        raw = json.load(f)

    tracks_in = raw.get("tracks") if isinstance(raw, dict) else {}
    if not isinstance(tracks_in, dict):
        tracks_in = {}

    cleaned: Dict[str, Dict[str, Any]] = {}
    for vid, entry in tracks_in.items():
        if not isinstance(entry, dict):
            continue
        fname = str(entry.get("filename") or "").strip()
        if not fname:
            continue
        fpath = os.path.join(cdir, os.path.basename(fname))
        if os.path.isfile(fpath):
            fsize = os.path.getsize(fpath)
            if fsize > 0:
                item = dict(entry)
                item["video_id"] = str(item.get("video_id") or vid)
                item["filename"] = os.path.basename(fname)
                item["size_bytes"] = fsize
                cleaned[item["video_id"]] = item
    return {"tracks": cleaned}


def load_cache_index() -> Dict[str, Any]:
    """Đọc `index.json` và tự dọn những mục không còn file thật trên đĩa (tự chuyển eMMC nếu USB lỗi)."""
    cdir, using_usb = get_cache_dir_with_mode()
    try:
        return _load_cache_index_from_dir(cdir)
    except OSError as e:
        logger.warning(f"Lỗi I/O đọc music_cache/index.json ({cdir}): {e}")
        if using_usb:
            storage_helper.invalidate_usb_health(str(e))
            try:
                return _load_cache_index_from_dir(CACHE_DIR)
            except Exception as e2:
                logger.warning(f"Lỗi đọc fallback music_cache/index.json: {e2}")
        return {"tracks": {}}
    except Exception as e:
        logger.warning(f"Lỗi đọc music_cache/index.json: {e}")
        return {"tracks": {}}


def save_cache_index(index_data: Dict[str, Any], target_dir: Optional[str] = None) -> bool:
    cdir = target_dir or ensure_cache_dir()
    ipath = os.path.join(cdir, INDEX_FILENAME)
    tmp_path = ipath + ".tmp"
    try:
        os.makedirs(cdir, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ipath)
        return True
    except OSError as e:
        logger.warning(f"Lỗi ghi music_cache/index.json ({cdir}): {e}")
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        if storage_helper.is_usb_path(cdir):
            storage_helper.invalidate_usb_health(str(e))
        return False


def _track_with_path(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(entry)
    out["filepath"] = os.path.join(get_cache_dir(), os.path.basename(str(entry.get("filename") or "")))
    return out


_TOKEN_STOPWORDS = {"bai", "nhac", "ca", "khuc", "hat", "official", "mv", "video", "audio", "lyric", "lyrics"}


def _folded_tokens(text: str) -> List[str]:
    """Tách từ chuẩn hóa (bỏ dấu tiếng Việt) để khớp tên bài/truy vấn linh hoạt."""
    s = unicodedata.normalize("NFD", str(text or "").lower().replace("đ", "d"))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return [tok for tok in re.findall(r"[a-z0-9]+", s) if len(tok) >= 2]


def find_cached_track(query_or_url: str) -> Optional[Dict[str, Any]]:
    """Tìm bài đã tải trong `music_cache/index.json` theo video_id, truy vấn cũ, tiêu đề hoặc từ khóa."""
    raw = str(query_or_url or "").strip()
    if not raw:
        return None
    idx = load_cache_index()
    tracks = idx.get("tracks") or {}

    # 1. Khớp trực tiếp theo key/video_id (ID YouTube 11 ký tự, ID SoundCloud số, hoặc key tùy ý trong cache)
    yt_vid = extract_youtube_video_id(raw)
    vid = yt_vid or (raw if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw) else None)
    for key in (raw, yt_vid):
        if key and key in tracks:
            t = _track_with_path(tracks[key])
            if os.path.isfile(t["filepath"]):
                return t

    # 2. Khớp theo URL, yt:<vid> alias, query chuẩn hóa hoặc tiêu đề bài hát
    candidates = [normalize_query(raw)]
    if vid:
        candidates.append(f"yt:{vid.lower()}")
    sc_url = extract_soundcloud_url(raw)
    if sc_url:
        candidates.append(normalize_query(sc_url))
    candidates = [c for c in candidates if c]
    if not candidates:
        return None

    for entry in tracks.values():
        queries = [normalize_query(q) for q in (entry.get("queries") or [])]
        title_norm = normalize_query(entry.get("title") or "")
        for cand in candidates:
            if cand in queries or (title_norm and title_norm == cand):
                t = _track_with_path(entry)
                if os.path.isfile(t["filepath"]):
                    return t

    # 3. Khớp tập từ khóa cho truy vấn dạng tên bài/nghệ sĩ (vd 'Da LAB Thanh Xuân' vs 'Da LAB - Thanh Xuân (Official...)')
    if not vid and not sc_url and "://" not in raw:
        q_tokens = [t for t in _folded_tokens(raw) if t not in _TOKEN_STOPWORDS] or _folded_tokens(raw)
        if len(q_tokens) >= 2 or (len(q_tokens) == 1 and len(q_tokens[0]) >= 5):
            q_set = set(q_tokens)
            best: Optional[Tuple[float, float, Dict[str, Any]]] = None
            for entry in tracks.values():
                t = _track_with_path(entry)
                if not os.path.isfile(t["filepath"]):
                    continue
                title_toks = _folded_tokens(entry.get("title") or "")
                hay_set = set(title_toks)
                for q in (entry.get("queries") or []):
                    hay_set.update(_folded_tokens(q))
                if not hay_set:
                    continue
                if q_set.issubset(hay_set):
                    overlap_ratio = len(q_set) / max(len(set(title_toks)) or 1, 1)
                    score = (1.0 if q_set.issubset(set(title_toks)) else 0.7) + overlap_ratio
                    last_p = float(entry.get("last_played") or entry.get("added_at") or 0.0)
                    if best is None or (score, last_p) > (best[0], best[1]):
                        best = (score, last_p, t)
            if best is not None:
                return best[2]
    return None


def enforce_cache_limit(protected_video_id: Optional[str] = None, max_bytes: Optional[int] = None) -> int:
    """Xóa bớt các bài ít nghe gần đây nhất (LRU) nếu kho vượt quá `max_bytes`. Trả về số file đã xóa."""
    limit_bytes = int(max_bytes) if max_bytes is not None else get_effective_max_cache_bytes()
    idx = load_cache_index()
    tracks: Dict[str, Dict[str, Any]] = idx.get("tracks") or {}
    cdir = get_cache_dir()

    total = sum(int(t.get("size_bytes") or 0) for t in tracks.values())
    if total <= limit_bytes:
        return 0

    candidates = sorted(
        tracks.values(),
        key=lambda t: float(t.get("last_played") or t.get("added_at") or 0.0),
    )
    removed = 0
    for entry in candidates:
        if total <= limit_bytes:
            break
        vid = entry.get("video_id")
        if protected_video_id and vid == protected_video_id:
            continue
        fpath = os.path.join(cdir, os.path.basename(str(entry.get("filename") or "")))
        sz = int(entry.get("size_bytes") or 0)
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except Exception as e:
            logger.warning(f"Không xóa được file cache cũ {fpath}: {e}")
            continue
        tracks.pop(vid, None)
        total = max(0, total - sz)
        removed += 1

    if removed > 0:
        save_cache_index(idx, target_dir=cdir)
    return removed


def list_cached_tracks() -> Dict[str, Any]:
    """Liệt kê toàn bộ kho nhạc cache kèm dung lượng hiện tại và trạng thái ổ USB."""
    idx = load_cache_index()
    cdir, using_usb = get_cache_dir_with_mode()
    tracks = list((idx.get("tracks") or {}).values())
    tracks.sort(key=lambda t: float(t.get("last_played") or t.get("added_at") or 0.0), reverse=True)
    total_bytes = sum(int(t.get("size_bytes") or 0) for t in tracks)
    return {
        "tracks": tracks,
        "count": len(tracks),
        "total_bytes": total_bytes,
        "max_bytes": get_effective_max_cache_bytes(),
        "storage_mode": "usb" if using_usb else "internal",
        "cache_dir": cdir,
        "usb": storage_helper.get_usb_storage_snapshot(),
    }


def _touch_cached_track(video_id: str, extra_query: str = "") -> None:
    if not video_id:
        return
    idx = load_cache_index()
    tracks = idx.get("tracks") or {}
    entry = tracks.get(video_id)
    if not entry:
        return
    entry["last_played"] = time.time()
    q_norm = normalize_query(extra_query)
    if q_norm and not extract_youtube_url(q_norm):
        qs = list(entry.get("queries") or [])
        if q_norm not in [normalize_query(x) for x in qs]:
            qs.append(q_norm)
            entry["queries"] = qs[-10:]
    save_cache_index(idx)


def _find_yt_dlp_cmd() -> List[str]:
    for cand in (
        "/usr/local/bin/yt-dlp",
        os.path.join(sys.prefix, "bin", "yt-dlp"),
        shutil.which("yt-dlp"),
    ):
        if cand and os.path.isfile(cand):
            return [cand]
    return [sys.executable, "-m", "yt_dlp"]


_YT_FORCE_COOKIELESS: contextvars.ContextVar[bool] = contextvars.ContextVar("yt_force_cookieless", default=False)


def _detect_yt_dlp_js_runtime() -> Optional[str]:
    """
    Tìm JavaScript runtime gọn nhẹ cho `yt-dlp` EJS challenge solver.
    Ưu tiên QuickJS (~vài MB RAM, hợp OpenStick 512MB) trước Deno/Node/Bun.
    """
    candidates = (
        ("quickjs", ("qjs", "quickjs"), ("/usr/local/bin/qjs", "/usr/bin/qjs", "/usr/local/bin/quickjs", "/usr/bin/quickjs")),
        ("deno", ("deno",), ("/usr/local/bin/deno", "/usr/bin/deno", "/root/.deno/bin/deno")),
        ("node", ("node", "nodejs"), ("/usr/local/bin/node", "/usr/bin/node", "/usr/bin/nodejs")),
        ("bun", ("bun",), ("/usr/local/bin/bun", "/usr/bin/bun", "/root/.bun/bin/bun")),
    )
    for rt_name, bins, extra_paths in candidates:
        for b in bins:
            found = shutil.which(b)
            if found:
                return f"{rt_name}:{found}"
        for p in extra_paths:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return f"{rt_name}:{p}"
    return None


def _classify_yt_dlp_error(stdout_s: str, stderr_s: str, rc: int) -> Tuple[str, str]:
    """Phân loại lỗi yt-dlp từ stdout/stderr để thông báo chính xác và kích hoạt fallback phù hợp."""
    combined = f"{stderr_s}\n{stdout_s}".strip()
    low = combined.lower()
    err_lines = [ln.strip() for ln in combined.splitlines() if "error:" in ln.lower()]
    detail = err_lines[-1] if err_lines else (stderr_s or stdout_s or combined).strip()
    if any(
        tok in low
        for tok in (
            "read-only",
            "input/output error",
            "no such device",
            "permission denied",
            "no space left",
            "unable to open for writing",
            "unable to write",
            "errno 5",
            "errno 28",
            "errno 30",
            "erofs",
        )
    ):
        return "io_error", f"Lỗi ghi bộ nhớ lưu trữ: {detail[:160]}"
    if "sign in to confirm" in low or "not a bot" in low or "cookies-from-browser" in low:
        return (
            "bot_check",
            "YouTube yêu cầu xác minh tài khoản (chưa có youtube_cookies.txt hợp lệ trên Dashboard).",
        )
    if "larger than max-filesize" in low or "file is larger than" in low:
        return "too_large", "File nhạc vượt quá giới hạn dung lượng cho phép."
    if (
        "signature solving failed" in low
        or "challenge solver" in low
        or "supported javascript runtime" in low
        or "n challenge solving failed" in low
        or "error solving n challenge" in low
    ):
        return (
            "js_challenge",
            "yt-dlp thiếu JavaScript runtime/EJS solver để giải mã YouTube (cần cập nhật yt-dlp, yt-dlp-ejs và cài quickjs/node).",
        )
    if "video unavailable" in low or "private video" in low or "requested format is not available" in low:
        return "unavailable", f"Video/định dạng không khả dụng: {detail[:160]}"
    if rc != 0:
        return "yt_dlp_error", f"yt-dlp lỗi ({rc}): {detail[:180]}"
    return "empty_file", "Không thấy file âm thanh sau khi tải xong."


async def _fetch_youtube_oembed(yt_url: str, timeout_sec: float = 3.5) -> Optional[Dict[str, str]]:
    """Tra cứu tiêu đề công khai qua YouTube oEmbed để dùng fallback SoundCloud khi YouTube đòi cookies."""
    clean = normalize_youtube_single_url(yt_url)
    if not clean:
        return None
    endpoint = f"https://www.youtube.com/oembed?url={quote(clean, safe='')}&format=json"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as sess:
            async with sess.get(endpoint) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, dict) and data.get("title"):
                    return {
                        "title": str(data.get("title") or "").strip(),
                        "author": str(data.get("author_name") or "").strip(),
                    }
    except Exception:
        return None
    return None


def _clean_oembed_search_title(title: str) -> str:
    raw = str(title or "").strip()
    if not raw:
        return ""
    s = re.sub(
        r"[\[\(][^\]\)]{1,40}(?:official|mv|lyric|lyrics|audio|live|hd|4k|video|visualizer)[^\]\)]{0,40}[\]\)]",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"[\U0001F1E0-\U0001FAFF\u2600-\u27BF\uFE0F\u200D|#♪♫★☆•·~]+", " ", s)
    if re.search(r"\b(?:playlist|tuyển tập|quả list|list|mix|tổng hợp)\b", s, flags=re.IGNORECASE):
        m_quote = re.search(r"['\"“‘]([^'\"”’]{3,50})['\"”’]", s)
        if m_quote:
            quoted = re.sub(r"\s+", " ", m_quote.group(1)).strip()
            if quoted:
                return quoted[:90]
    s = re.sub(r"['\"“”‘’]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:90] or raw[:90]


def _build_soundcloud_fallback_queries(title: str) -> List[str]:
    """Tạo danh sách truy vấn SoundCloud từ gọn chuẩn hóa đến vế rút gọn nếu tiêu đề YouTube quá dài."""
    clean = _clean_oembed_search_title(title)
    if not clean:
        return []
    candidates = [clean]
    first_seg = re.split(r"\s[-–—:/|]\s", clean, maxsplit=1)[0].strip()
    if first_seg and len(first_seg) >= 4 and first_seg.lower() != clean.lower():
        candidates.append(first_seg)
    words = clean.split()
    if len(words) >= 5:
        short = " ".join(words[:4]).strip()
        if len(short) >= 4 and short.lower() not in {c.lower() for c in candidates}:
            candidates.append(short)
    return candidates


async def _download_media_to_dir(
    raw: str,
    target: str,
    cdir: str,
    using_usb: bool,
    source_kind: str = "youtube",
    alias_queries: Optional[List[str]] = None,
    timeout_sec: float = 240.0,
    use_cookies: Optional[bool] = None,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    try:
        os.makedirs(cdir, exist_ok=True)
    except OSError as e:
        if using_usb:
            storage_helper.invalidate_usb_health(str(e))
        return None, "io_error", f"Lỗi thư mục cache: {e}"

    limit_mb = USB_MAX_DOWNLOAD_MB if using_usb else EMMC_MAX_DOWNLOAD_MB
    out_tmpl = os.path.join(cdir, "%(id)s.%(ext)s")
    is_soundcloud = (source_kind == "soundcloud")

    cmd = _find_yt_dlp_cmd() + ["--no-playlist", "--no-update"]
    if not is_soundcloud:
        js_rt = _detect_yt_dlp_js_runtime()
        if js_rt:
            cmd.extend(["--js-runtimes", js_rt, "--remote-components", "ejs:github"])
        allow_cookies = (not _YT_FORCE_COOKIELESS.get()) if use_cookies is None else bool(use_cookies)
        cookies_path = get_youtube_cookies_path() if allow_cookies else None
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        else:
            cmd.extend(["--extractor-args", "youtube:player_client=visionos,default"])
        fmt_sel = (
            f"140[filesize<={limit_mb}M]/140[filesize_approx<={limit_mb}M]"
            f"/139/ba[ext=m4a][filesize<={limit_mb}M]/ba[acodec^=mp4a][filesize<={limit_mb}M]"
            f"/ba[filesize<={limit_mb}M]/140/ba"
        )
    else:
        fmt_sel = "ba[ext=mp3]/hls_aac_160k/hls_aac_96k/ba[ext=m4a]/ba"

    cmd.extend([
        "-f", fmt_sel,
        "--max-filesize", f"{limit_mb}M",
        "--print-json",
    ])
    env = os.environ.copy()
    if using_usb:
        usb_yt_cache = storage_helper.get_usb_subdir("yt_dlp_cache", create=True)
        usb_tmp = storage_helper.get_usb_subdir("tmp", create=True)
        if usb_yt_cache:
            cmd.extend(["--cache-dir", usb_yt_cache])
        if usb_tmp:
            env["TMPDIR"] = usb_tmp
    cmd.extend(["-o", out_tmpl, target])

    if os.name == "posix" and shutil.which("nice"):
        cmd = ["nice", "-n", "5"] + cmd

    src_label = "SoundCloud" if is_soundcloud else "YouTube"
    logger.info(f"🎵 [Jukebox] Bắt đầu tải {src_label} ({'USB' if using_usb else 'eMMC'}): {target}")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=(os.name == "posix"),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        if proc is not None and getattr(proc, "returncode", None) is None:
            await _terminate_subprocess_group(proc, timeout_sec=1.5)
        msg = f"Tải {src_label} quá thời gian ({int(timeout_sec)}s): {target[:80]}"
        logger.warning(f"🎵 [Jukebox] {msg}")
        return None, "timeout", msg
    except asyncio.CancelledError:
        if proc is not None and getattr(proc, "returncode", None) is None:
            await _terminate_subprocess_group(proc, timeout_sec=1.0)
        raise
    except Exception as e:
        if proc is not None and getattr(proc, "returncode", None) is None:
            await _terminate_subprocess_group(proc, timeout_sec=1.0)
        msg = f"Lỗi chạy yt-dlp ({src_label}): {e}"
        logger.error(f"🎵 [Jukebox] {msg}")
        return None, "exec_error", msg

    stdout_s = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr_s = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err_code, err_msg = _classify_yt_dlp_error(stdout_s, stderr_s, proc.returncode)
        logger.warning(f"🎵 [Jukebox] {src_label} trả lỗi {proc.returncode} ({err_code}): {stderr_s[:280]}")
        if using_usb and err_code == "io_error":
            storage_helper.invalidate_usb_health(stderr_s[:120])
        return None, err_code, err_msg

    info: Dict[str, Any] = {}
    for line in reversed(stdout_s.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("id"):
                    info = parsed
                    break
            except Exception:
                continue

    raw_vid = str(info.get("id") or "").strip()
    vid = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_vid).strip("._")
    if not vid:
        err_code, err_msg = _classify_yt_dlp_error(stdout_s, stderr_s, 0)
        logger.warning(f"🎵 [Jukebox] {src_label} không trả JSON metadata hợp lệ ({err_msg})")
        return None, err_code, err_msg

    chosen_path = None
    try:
        for req in (info.get("requested_downloads") or []):
            fp = req.get("filepath") if isinstance(req, dict) else None
            if fp and os.path.isfile(fp):
                chosen_path = fp
                break
        if not chosen_path:
            ext = str(info.get("ext") or "m4a").strip()
            cand = os.path.join(cdir, f"{vid}.{ext}")
            if os.path.isfile(cand):
                chosen_path = cand
            else:
                matches = [
                    p for p in glob.glob(os.path.join(cdir, f"{vid}.*"))
                    if not p.endswith(".part") and not p.endswith(".json")
                ]
                if matches:
                    chosen_path = matches[0]

        if not chosen_path or not os.path.isfile(chosen_path) or os.path.getsize(chosen_path) <= 0:
            err_code, err_msg = _classify_yt_dlp_error(stdout_s, stderr_s, 0)
            if err_code == "empty_file" and int(info.get("filesize") or info.get("filesize_approx") or 0) > limit_mb * 1024 * 1024:
                err_code = "too_large"
                err_msg = f"Bài '{info.get('title') or vid}' vượt quá giới hạn {limit_mb} MB."
            logger.warning(f"🎵 [Jukebox] Không thấy file âm thanh sau khi tải id={vid} ({err_msg})")
            return None, err_code, err_msg

    except OSError as e:
        return _handle_cache_io_error(using_usb, chosen_path or vid, e)

    return await _commit_cached_track(
        raw=raw,
        cdir=cdir,
        using_usb=using_usb,
        source_kind=source_kind,
        vid=vid,
        info=info,
        chosen_path=chosen_path,
        alias_queries=alias_queries,
        source_label=src_label,
    )


async def _download_youtube_to_dir(
    raw: str,
    target: str,
    yt_url: Optional[str],
    cdir: str,
    using_usb: bool,
    timeout_sec: float = 240.0,
) -> Optional[Dict[str, Any]]:
    track, err_code, err_msg = await _download_media_to_dir(
        raw=raw,
        target=target,
        cdir=cdir,
        using_usb=using_usb,
        source_kind="soundcloud" if (extract_soundcloud_url(target) or target.startswith("scsearch")) else "youtube",
        timeout_sec=timeout_sec,
    )
    if err_code:
        _set_last_download_error(err_code, err_msg, query=raw)
    return track


async def _download_with_usb_fallback(
    *,
    raw: str,
    target: str,
    cdir: str,
    using_usb: bool,
    source_kind: str = "youtube",
    yt_url: Optional[str] = None,
    alias_queries: Optional[List[str]] = None,
    timeout_sec: float = 240.0,
) -> Tuple[Optional[Dict[str, Any]], str, str, str, bool]:
    """Tải media về cdir và tự động chuyển dự phòng sang eMMC (CACHE_DIR) nếu USB ngắt/read-only giữa chừng."""
    async def _attempt(cur_dir: str, cur_usb: bool) -> Tuple[Optional[Dict[str, Any]], str, str]:
        if source_kind == "youtube":
            clear_last_download_error()
            track = await _download_youtube_to_dir(raw, target, yt_url, cur_dir, cur_usb, timeout_sec=timeout_sec)
            if track is not None:
                return track, "", ""
            last = get_last_download_error()
            return (
                None,
                str(last.get("code") or "yt_dlp_error"),
                str(last.get("message") or "Không tải được từ YouTube."),
            )
        return await _download_media_to_dir(
            raw=raw,
            target=target,
            cdir=cur_dir,
            using_usb=cur_usb,
            source_kind=source_kind,
            alias_queries=alias_queries,
            timeout_sec=timeout_sec,
        )

    res, err_code, err_msg = await _attempt(cdir, using_usb)
    cur_dir, cur_usb = cdir, using_usb
    if res is None and using_usb and not storage_helper.is_usb_healthy(force_refresh=True, respect_cooldown=True):
        logger.warning("🎵 [Jukebox] USB gặp sự cố khi tải -> thử lưu dự phòng sang bộ nhớ trong (eMMC)")
        cur_dir, cur_usb = CACHE_DIR, False
        res, err_code, err_msg = await _attempt(CACHE_DIR, False)
    return res, err_code, err_msg, cur_dir, cur_usb


async def _download_youtube_with_cookieless_retry(
    *,
    raw: str,
    target: str,
    cdir: str,
    using_usb: bool,
    yt_url: Optional[str],
    timeout_sec: float,
) -> Tuple[Optional[Dict[str, Any]], str, str, str, bool]:
    """Tải YouTube theo cấu hình cookies hiện tại; nếu cookies vướng JS challenge/format -> thử lại 1 lần không cookies (visionos/default)."""
    res, err_code, err_msg, cdir, using_usb = await _download_with_usb_fallback(
        raw=raw,
        target=target,
        cdir=cdir,
        using_usb=using_usb,
        source_kind="youtube",
        yt_url=yt_url,
        timeout_sec=timeout_sec,
    )
    if res:
        return res, "", "", cdir, using_usb

    if get_youtube_cookies_path() and err_code in ("js_challenge", "unavailable", "yt_dlp_error", "bot_check"):
        logger.info(
            f"🎵 [Jukebox] YouTube với cookies lỗi ({err_code}) -> thử lại không cookies (visionos/default): {target}"
        )
        tok = _YT_FORCE_COOKIELESS.set(True)
        try:
            retry_res, retry_code, retry_msg, cdir, using_usb = await _download_with_usb_fallback(
                raw=raw,
                target=target,
                cdir=cdir,
                using_usb=using_usb,
                source_kind="youtube",
                yt_url=yt_url,
                timeout_sec=timeout_sec,
            )
        finally:
            _YT_FORCE_COOKIELESS.reset(tok)
        if retry_res:
            return retry_res, "", "", cdir, using_usb
        if err_code != "js_challenge" or retry_code in ("io_error", "too_large"):
            err_code, err_msg = retry_code, retry_msg

    return None, err_code, err_msg, cdir, using_usb


_MUSIC_DL_FAIL_UNTIL: Dict[str, float] = {}
_MUSIC_DL_FAIL_COOLDOWN_SEC = 25.0
_BRIDGE_CHUNK_BYTES = 65536


def _handle_cache_io_error(using_usb: bool, label: str, exc: OSError) -> Tuple[None, str, str]:
    msg = f"Lỗi I/O với file cache {label}: {exc}"
    logger.warning(f"🎵 [Jukebox] {msg}")
    if using_usb:
        storage_helper.invalidate_usb_health(str(exc))
    return None, "io_error", msg


def _kill_sync_process_group(proc: subprocess.Popen) -> None:
    """Dừng trọn vẹn process con (và nhóm `sh -c` trên POSIX) sau khi đo loudness xong hoặc ngắt sớm."""
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    if proc.poll() is None:
        if os.name == "posix":
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=0.4)
                    return
                except subprocess.TimeoutExpired:
                    continue
                except Exception:
                    break
        try:
            proc.terminate()
            proc.wait(timeout=0.4)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=0.4)
            except Exception:
                pass
    else:
        try:
            proc.wait(timeout=0.2)
        except Exception:
            pass


TRACK_NORM_VERSION = 2


def _track_needs_loudness_scan(ent: Any) -> bool:
    if not isinstance(ent, dict):
        return False
    if ent.get("norm_rms_db") is None:
        return True
    try:
        return int(ent.get("norm_ver") or 0) < TRACK_NORM_VERSION
    except (TypeError, ValueError):
        return True


def _analyze_track_loudness_sync(
    filepath: str,
    max_analyze_sec: float = 420.0,
    allow_container_convert: bool = True,
) -> Optional[Dict[str, float]]:
    """
    Giải mã tối đa `max_analyze_sec` giây âm thanh của bài hát theo từng block nhỏ
    để đo mức RMS chủ động (`norm_rms_db`) & đỉnh (`norm_peak_db`) với bộ nhớ < 1 MB.
    """
    if not filepath or not os.path.isfile(filepath):
        return None
    try:
        if os.path.getsize(filepath) < 4096:
            return None
    except OSError:
        return None

    ext = os.path.splitext(filepath)[1].lower()
    if allow_container_convert and ext in (".m4a", ".aac", ".mp4") and shutil.which("faad"):
        _ensure_faad_compatible_m4a(filepath)

    cmd = _build_track_decoder_cmd(filepath)
    if not cmd:
        return None

    proc: Optional[subprocess.Popen] = None
    analyzer = esp32_dsp.LoudnessAnalyzer(sample_rate=SAMPLE_RATE)
    max_bytes = max(BYTES_PER_SEC, int(max(10.0, float(max_analyze_sec)) * BYTES_PER_SEC))
    total_read = 0
    chunk_size = 19200  # 0.4s mono 24kHz PCM16

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        assert proc.stdout is not None
        while total_read < max_bytes:
            need = min(chunk_size, max_bytes - total_read)
            chunk = proc.stdout.read(need)
            if not chunk:
                break
            analyzer.feed_pcm16(chunk)
            total_read += len(chunk)
    except Exception as e:
        logger.debug(f"🎛️ [Loudness] Bỏ qua phân tích file {os.path.basename(filepath)}: {e}")
        return None
    finally:
        if proc is not None:
            _kill_sync_process_group(proc)

    if total_read < BYTES_PER_SEC // 2:
        return None
    return analyzer.finish()


async def _commit_cached_track(
    *,
    raw: str,
    cdir: str,
    using_usb: bool,
    source_kind: str,
    vid: str,
    info: Dict[str, Any],
    chosen_path: str,
    alias_queries: Optional[List[str]] = None,
    source_label: str = "",
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Chuẩn hóa container DASH -> ADTS nếu cần, đo/lưu loudness, ghi `index.json` và dọn LRU cache."""
    try:
        # YouTube/SoundCloud itag 140/hls_aac dùng fragmented MP4 (dash/iso6) khiến `faad` lỗi `parse:-1`.
        # Giải bọc fMP4 sang ADTS AAC tại chỗ để `faad | sox` phát mượt mà không cần cài thêm ffmpeg.
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, _ensure_faad_compatible_m4a, chosen_path):
            logger.info(f"🎵 [Jukebox] Đã chuẩn hóa container DASH -> ADTS cho {os.path.basename(chosen_path)}")
        file_size = os.path.getsize(chosen_path)
    except OSError as e:
        return _handle_cache_io_error(using_usb, chosen_path or vid, e)

    norm_ver = 0
    if info.get("norm_ver") is not None:
        try:
            norm_ver = int(info.get("norm_ver") or 0)
        except (TypeError, ValueError):
            norm_ver = 0
    norm_rms_db = _safe_float(info.get("norm_rms_db"), None) if info.get("norm_rms_db") is not None else None
    if norm_rms_db is not None and not (-60.0 <= norm_rms_db <= 6.0):
        norm_rms_db = None
    norm_peak_db = _safe_float(info.get("norm_peak_db"), None) if info.get("norm_peak_db") is not None else None
    if norm_peak_db is not None and not (-60.0 <= norm_peak_db <= 12.0):
        norm_peak_db = None
    if (norm_rms_db is None or norm_ver < TRACK_NORM_VERSION) and file_size >= 4096:
        try:
            measured = await asyncio.wait_for(
                loop.run_in_executor(None, _analyze_track_loudness_sync, chosen_path, 420.0, False),
                timeout=25.0,
            )
            if measured:
                norm_rms_db = measured.get("rms_db")
                norm_peak_db = measured.get("peak_db")
                norm_ver = TRACK_NORM_VERSION
        except Exception:
            pass

    now = time.time()
    idx = _load_cache_index_from_dir(cdir) if os.path.isfile(os.path.join(cdir, INDEX_FILENAME)) else {"tracks": {}}
    tracks = idx.setdefault("tracks", {})
    existing = tracks.get(vid) or {}
    queries = list(existing.get("queries") or [])
    q_norm = normalize_query(raw)
    if q_norm and not extract_music_url(raw) and q_norm not in [normalize_query(x) for x in queries]:
        queries.append(q_norm)
    for alias in (alias_queries or []):
        an = normalize_query(alias)
        if an and an not in [normalize_query(x) for x in queries]:
            queries.append(an)

    entry = {
        "video_id": vid,
        "source": source_kind,
        "title": str(info.get("title") or vid)[:180],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:120],
        "duration": int(info.get("duration") or 0),
        "ext": os.path.splitext(chosen_path)[1].lstrip(".").lower() or str(info.get("ext") or "m4a"),
        "filename": os.path.basename(chosen_path),
        "size_bytes": file_size,
        "queries": queries[-14:],
        "added_at": float(existing.get("added_at") or now),
        "last_played": now,
    }
    if norm_rms_db is not None:
        entry["norm_rms_db"] = round(float(norm_rms_db), 2)
        if norm_ver > 0:
            entry["norm_ver"] = int(norm_ver)
    elif existing.get("norm_rms_db") is not None:
        entry["norm_rms_db"] = existing.get("norm_rms_db")
        if existing.get("norm_ver") is not None:
            entry["norm_ver"] = existing.get("norm_ver")
    if norm_peak_db is not None:
        entry["norm_peak_db"] = round(float(norm_peak_db), 2)
    elif existing.get("norm_peak_db") is not None:
        entry["norm_peak_db"] = existing.get("norm_peak_db")
    tracks[vid] = entry
    if not save_cache_index(idx, target_dir=cdir):
        return None, "io_error", "Không ghi được index.json."
    enforce_cache_limit(protected_video_id=vid)

    src_label = source_label or ("SoundCloud" if source_kind == "soundcloud" else "YouTube")
    logger.info(
        f"🎵 [Jukebox] Đã cache xong ({src_label} | {'USB' if using_usb else 'eMMC'}): "
        f"'{entry['title']}' ({entry['size_bytes'] / 1024 / 1024:.2f} MB)"
    )
    out = dict(entry)
    out["filepath"] = os.path.join(cdir, entry["filename"])
    return out, "", ""


def _get_music_dl_api_key() -> str:
    return str(getattr(config, "MUSIC_DL_API_KEY", "") or getattr(config, "TTS_API_KEY", "") or "").strip()


def _get_music_dl_headers() -> Dict[str, str]:
    key = _get_music_dl_api_key()
    return {"X-API-Key": key} if key else {}


def _get_music_dl_endpoint() -> str:
    try:
        import lan_presence
        url = str(lan_presence.get_effective_music_dl_url() or "").strip().rstrip("/")
        if url:
            return url
    except Exception:
        pass
    return str(getattr(config, "MUSIC_DL_API_URL", "") or "").strip().rstrip("/")


async def check_music_dl_health(bridge_url: Optional[str] = None, timeout_sec: float = 2.0) -> bool:
    """Kiểm tra nhanh trạng thái PC LAN Music Download Bridge qua GET /health."""
    base = str(bridge_url if bridge_url is not None else _get_music_dl_endpoint() or "").strip().rstrip("/")
    if not base:
        return False
    headers = _get_music_dl_headers()
    try:
        timeout = aiohttp.ClientTimeout(total=max(0.5, float(timeout_sec)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/health", headers=headers) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return bool(isinstance(data, dict) and data.get("ok"))
    except Exception:
        return False


async def _stream_bridge_file_to_dir(
    *,
    session: aiohttp.ClientSession,
    file_url: str,
    headers: Dict[str, str],
    raw: str,
    cdir: str,
    using_usb: bool,
    source_kind: str,
    vid: str,
    info: Dict[str, Any],
    ext: str,
    alias_queries: Optional[List[str]] = None,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Kéo từng chunk 64 KB từ PC Bridge về đĩa USB/eMMC để tiêu thụ RAM tối thiểu."""
    final_path = os.path.join(cdir, f"{vid}.{ext}")
    part_path = final_path + ".part"
    try:
        os.makedirs(cdir, exist_ok=True)
        async with session.get(file_url, headers=headers) as resp:
            if resp.status != 200:
                return None, "bridge_http", f"PC Bridge trả HTTP {resp.status} khi tải file."
            written = 0
            with open(part_path, "wb") as fp:
                async for chunk in resp.content.iter_chunked(_BRIDGE_CHUNK_BYTES):
                    if chunk:
                        fp.write(chunk)
                        written += len(chunk)
            if written <= 0:
                raise OSError("File nhận từ PC Bridge rỗng.")
        os.replace(part_path, final_path)
    except OSError as e:
        return _handle_cache_io_error(using_usb, final_path, e)
    except Exception as e:
        return None, "bridge_stream", f"Lỗi stream file từ PC Bridge: {e}"
    finally:
        if os.path.isfile(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass

    res, err_code, err_msg = await _commit_cached_track(
        raw=raw,
        cdir=cdir,
        using_usb=using_usb,
        source_kind=source_kind,
        vid=vid,
        info=info,
        chosen_path=final_path,
        alias_queries=alias_queries,
        source_label="PC Bridge",
    )
    if not res and err_code == "io_error" and os.path.isfile(final_path):
        try:
            os.remove(final_path)
        except OSError:
            pass
    return res, err_code, err_msg


async def _download_via_pc_bridge(
    *,
    raw: str,
    cdir: str,
    using_usb: bool,
    alias_queries: Optional[List[str]] = None,
    timeout_sec: float = 240.0,
) -> Tuple[Optional[Dict[str, Any]], str, str, str, str, bool]:
    """Ủy thác tìm/tải nhạc sang PC LAN Bridge, tự fallback eMMC nếu USB lỗi và rơi về yt-dlp cục bộ nếu PC tắt."""
    base = _get_music_dl_endpoint()
    if not base:
        return None, "bridge_unconfigured", "", "", cdir, using_usb

    try:
        import lan_presence
        if lan_presence.is_music_dl_online():
            _MUSIC_DL_FAIL_UNTIL.pop(base, None)
    except Exception:
        pass

    now_ts = time.time()
    if now_ts < _MUSIC_DL_FAIL_UNTIL.get(base, 0.0):
        return None, "bridge_unavailable", "PC Music Bridge tạm nghỉ.", "", cdir, using_usb

    limit_mb = USB_MAX_DOWNLOAD_MB if using_usb else EMMC_MAX_DOWNLOAD_MB
    req_timeout = max(20.0, float(timeout_sec or 240.0))
    timeout = aiohttp.ClientTimeout(
        total=req_timeout,
        sock_connect=2.0,
        sock_read=min(req_timeout, 220.0),
    )
    headers = _get_music_dl_headers()
    payload = {
        "query_or_url": raw,
        "max_filesize_mb": limit_mb,
        "timeout_sec": min(req_timeout - 5.0, 200.0),
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logger.info(f"💿 [PC Bridge] Gửi yêu cầu tải '{raw[:60]}' -> {base}")
            async with session.post(f"{base}/download", json=payload, headers=headers) as resp:
                if resp.status in (401, 403):
                    _MUSIC_DL_FAIL_UNTIL[base] = time.time() + _MUSIC_DL_FAIL_COOLDOWN_SEC
                    logger.warning(f"💿 [PC Bridge] Từ chối xác thực ({resp.status}); kiểm tra MUSIC_DL_API_KEY.")
                    return None, "bridge_auth", "Sai API key PC Music Bridge.", "", cdir, using_usb

                data = None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None

                if resp.status == 422 and isinstance(data, dict):
                    _MUSIC_DL_FAIL_UNTIL.pop(base, None)
                    return (
                        None,
                        str(data.get("error_code") or "bridge_failed"),
                        str(data.get("error") or "PC Bridge không tải được bài này."),
                        "",
                        cdir,
                        using_usb,
                    )

                if resp.status != 200 or not isinstance(data, dict) or not data.get("ok"):
                    _MUSIC_DL_FAIL_UNTIL[base] = time.time() + _MUSIC_DL_FAIL_COOLDOWN_SEC
                    return None, "bridge_http", f"PC Bridge HTTP {resp.status}", "", cdir, using_usb

            _MUSIC_DL_FAIL_UNTIL.pop(base, None)
            token = str(data.get("token") or "").strip()
            dl_path = str(data.get("download_path") or (f"/files/{token}" if token else "")).strip()
            tr = data.get("track") if isinstance(data.get("track"), dict) else {}
            fb_note = str(data.get("fallback_note") or "").strip()
            if not token or not dl_path:
                return None, "bridge_bad_response", "PC Bridge không trả về token tải file.", "", cdir, using_usb

            vid_raw = os.path.basename(str(tr.get("video_id") or "").strip())
            vid = re.sub(r"[^A-Za-z0-9_-]", "_", vid_raw)[:64] or "track"
            ext = re.sub(r"[^A-Za-z0-9]", "", str(tr.get("ext") or "m4a").lower()) or "m4a"
            source_kind = str(tr.get("source") or "youtube")
            file_url = f"{base}{dl_path if dl_path.startswith('/') else '/' + dl_path}"
            info = {
                "title": tr.get("title") or vid,
                "uploader": tr.get("uploader") or "",
                "duration": int(tr.get("duration") or 0),
                "ext": ext,
            }
            if tr.get("norm_rms_db") is not None:
                info["norm_rms_db"] = tr.get("norm_rms_db")
            if tr.get("norm_peak_db") is not None:
                info["norm_peak_db"] = tr.get("norm_peak_db")
            if tr.get("norm_ver") is not None:
                info["norm_ver"] = tr.get("norm_ver")

            try:
                res, err_code, err_msg = await _stream_bridge_file_to_dir(
                    session=session,
                    file_url=file_url,
                    headers=headers,
                    raw=raw,
                    cdir=cdir,
                    using_usb=using_usb,
                    source_kind=source_kind,
                    vid=vid,
                    info=info,
                    ext=ext,
                    alias_queries=alias_queries,
                )
                if not res and err_code == "io_error" and using_usb:
                    size_est = int(tr.get("size_bytes") or 0)
                    if size_est > EMMC_MAX_DOWNLOAD_MB * 1024 * 1024:
                        return (
                            None,
                            "too_large",
                            f"USB vừa lỗi và bài ({size_est / 1024 / 1024:.1f} MB) vượt quá giới hạn {EMMC_MAX_DOWNLOAD_MB} MB của eMMC.",
                            "",
                            CACHE_DIR,
                            False,
                        )
                    logger.warning("💿 [PC Bridge] Lỗi I/O USB khi kéo file -> tự thử lưu vào bộ nhớ trong (eMMC)...")
                    cdir, using_usb = CACHE_DIR, False
                    res, err_code, err_msg = await _stream_bridge_file_to_dir(
                        session=session,
                        file_url=file_url,
                        headers=headers,
                        raw=raw,
                        cdir=cdir,
                        using_usb=using_usb,
                        source_kind=source_kind,
                        vid=vid,
                        info=info,
                        ext=ext,
                        alias_queries=alias_queries,
                    )
                if res:
                    return res, "", "", fb_note, cdir, using_usb
                return None, err_code, err_msg, "", cdir, using_usb
            finally:
                try:
                    async with session.delete(
                        f"{base}/files/{token}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=2.5),
                    ):
                        pass
                except Exception:
                    pass
    except Exception as e:
        _MUSIC_DL_FAIL_UNTIL[base] = time.time() + _MUSIC_DL_FAIL_COOLDOWN_SEC
        logger.info(f"💿 [PC Bridge] Không kết nối được ({e}) -> chuyển sang tải trực tiếp trên OpenStick.")
        return None, "bridge_unavailable", str(e), "", cdir, using_usb


async def search_and_cache_track(
    query_or_url: str,
    timeout_sec: float = 240.0,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """
    Tìm kiếm & cache bài hát vào USB (rơi về eMMC nếu USB lỗi):
    - Hỗ trợ chuẩn hóa link YouTube (bỏ ?list=RD), YouTube cookies (`youtube_cookies.txt`),
      link SoundCloud trực tiếp, và tự động fallback sang SoundCloud (`scsearch1`) khi YouTube đòi xác minh.
    Trả về `(track_or_None, error_message, fallback_note)`.
    """
    raw = str(query_or_url or "").strip()
    if not raw:
        return None, "Thiếu tên bài hát hoặc link.", ""

    cached = find_cached_track(raw)
    if cached:
        _touch_cached_track(cached["video_id"], raw)
        clear_last_download_error()
        return cached, "", ""

    mgr = get_manager()
    async with mgr.get_download_lock():
        cached = find_cached_track(raw)
        if cached:
            _touch_cached_track(cached["video_id"], raw)
            clear_last_download_error()
            return cached, "", ""

        cdir, using_usb = get_cache_dir_with_mode()
        yt_url_raw = extract_youtube_url(raw)
        sc_url = extract_soundcloud_url(raw)

        bridge_aliases: List[str] = []
        if yt_url_raw:
            yt_vid_alias = extract_youtube_video_id(yt_url_raw)
            if yt_vid_alias:
                bridge_aliases.append(yt_vid_alias)

        bridge_res, b_code, b_msg, b_note, cdir, using_usb = await _download_via_pc_bridge(
            raw=raw,
            cdir=cdir,
            using_usb=using_usb,
            alias_queries=bridge_aliases,
            timeout_sec=timeout_sec,
        )
        if bridge_res:
            if b_note:
                _set_last_download_error("pc_bridge_fallback", b_msg, query=raw, fallback_note=b_note)
            else:
                clear_last_download_error()
            return bridge_res, "", b_note
        if b_code == "too_large":
            _set_last_download_error("too_large", b_msg, query=raw)
            return None, b_msg, ""
        if b_code not in ("bridge_unconfigured", "bridge_unavailable"):
            logger.info(f"💿 [PC Bridge] Rơi về tải cục bộ trên OpenStick (lý do: {b_code})")

        if sc_url:
            res, err_code, err_msg, cdir, using_usb = await _download_with_usb_fallback(
                raw=raw,
                target=sc_url,
                cdir=cdir,
                using_usb=using_usb,
                source_kind="soundcloud",
                alias_queries=[sc_url],
                timeout_sec=timeout_sec,
            )
            if res:
                clear_last_download_error()
                return res, "", ""
            _set_last_download_error(err_code, err_msg, query=raw)
            return None, err_msg, ""

        if yt_url_raw:
            clean_yt = normalize_youtube_single_url(yt_url_raw)
            vid = extract_youtube_video_id(yt_url_raw) or ""
            res, err_code, err_msg, cdir, using_usb = await _download_youtube_with_cookieless_retry(
                raw=raw,
                target=clean_yt,
                cdir=cdir,
                using_usb=using_usb,
                yt_url=clean_yt,
                timeout_sec=timeout_sec,
            )
            if res:
                clear_last_download_error()
                return res, "", ""

            # Nếu YouTube đòi xác minh/lỗi JS/format -> lấy tên bài qua oEmbed công khai và tìm bản tương đương trên SoundCloud
            if err_code in ("bot_check", "unavailable", "js_challenge"):
                meta = await _fetch_youtube_oembed(clean_yt)
                if meta and meta.get("title"):
                    sc_queries = _build_soundcloud_fallback_queries(meta["title"])
                    clean_title = sc_queries[0] if sc_queries else _clean_oembed_search_title(meta["title"])
                    fb_note = f"YouTube fallback ({err_code}) cho '{clean_title}' -> đang tự tìm bản trên SoundCloud."
                    logger.info(f"🎵 [Jukebox] {fb_note}")
                    aliases = [f"yt:{vid}", meta["title"], clean_title] if vid else [meta["title"], clean_title]
                    sc_res = None
                    sc_code = ""
                    sc_msg = ""
                    for sc_q in (sc_queries or [clean_title]):
                        sc_res, sc_code, sc_msg, cdir, using_usb = await _download_with_usb_fallback(
                            raw=sc_q,
                            target=f"scsearch1:{sc_q}",
                            cdir=cdir,
                            using_usb=using_usb,
                            source_kind="soundcloud",
                            alias_queries=aliases if sc_q == clean_title else aliases + [sc_q],
                            timeout_sec=timeout_sec,
                        )
                        if sc_res or sc_code in ("io_error", "timeout"):
                            clean_title = sc_q
                            break
                    if sc_res:
                        fb_reason = "cần cookies" if err_code == "bot_check" else "không tải được trực tiếp"
                        _set_last_download_error(
                            err_code,
                            err_msg,
                            query=raw,
                            fallback_note=f"YouTube {fb_reason} cho '{clean_title}' -> đã tự phát bản từ SoundCloud.",
                        )
                        return sc_res, "", f"YouTube {fb_reason} -> Hệ thống đã tự tìm bản '{clean_title}' trên SoundCloud phát cho anh."
                    err_msg = f"{err_msg} (Đã thử tìm '{clean_title}' trên SoundCloud nhưng không thấy: {sc_msg})"

            _set_last_download_error(err_code, err_msg, query=raw)
            return None, err_msg, ""

        # Truy vấn tìm kiếm chữ: thử YouTube trước, nếu lỗi/đòi bot check thì fallback sang SoundCloud ngay
        res, err_code, err_msg, cdir, using_usb = await _download_youtube_with_cookieless_retry(
            raw=raw,
            target=f"ytsearch1:{raw}",
            cdir=cdir,
            using_usb=using_usb,
            yt_url=None,
            timeout_sec=timeout_sec,
        )
        if res:
            clear_last_download_error()
            return res, "", ""

        if err_code != "io_error":
            fb_note = f"YouTube search lỗi ({err_code}) -> tự động chuyển tìm '{raw}' trên SoundCloud."
            logger.info(f"🎵 [Jukebox] {fb_note}")
            sc_res, sc_code, sc_msg, cdir, using_usb = await _download_with_usb_fallback(
                raw=raw,
                target=f"scsearch1:{raw}",
                cdir=cdir,
                using_usb=using_usb,
                source_kind="soundcloud",
                alias_queries=[raw],
                timeout_sec=timeout_sec,
            )
            if sc_res:
                _set_last_download_error(
                    err_code,
                    err_msg,
                    query=raw,
                    fallback_note=f"Đã tự fallback sang SoundCloud cho '{raw}'.",
                )
                return sc_res, "", f"Đã tự động lấy từ SoundCloud cho '{raw}'."
            err_msg = f"{err_msg} | SoundCloud: {sc_msg}"

        _set_last_download_error(err_code, err_msg, query=raw)
        return None, err_msg, ""


async def search_and_cache_youtube(query_or_url: str, timeout_sec: float = 240.0) -> Optional[Dict[str, Any]]:
    """Tải và cache nhạc (tự động hỗ trợ YouTube + SoundCloud fallback), giữ chữ ký tương thích ngược."""
    track, _err_msg, _fb = await search_and_cache_track(query_or_url, timeout_sec=timeout_sec)
    return track


# --- Giải mã & Trộn PCM 24kHz s16le mono (Audio Ducking) ---

def _pad_pcm_frame(frame: Optional[bytes]) -> bytes:
    if not frame:
        return b""
    if len(frame) >= FRAME_BYTES:
        return frame[:FRAME_BYTES]
    return frame + (b"\x00" * (FRAME_BYTES - len(frame)))


def _scale_pcm16(pcm_bytes: bytes, gain: float) -> bytes:
    """Nhân biên độ khung PCM 16-bit signed LE bằng `esp32_dsp.scale_pcm16` (C / Python fallback)."""
    return esp32_dsp.scale_pcm16(pcm_bytes, gain)


def _resolve_voice_gain(vol: float) -> float:
    """Tính gain giọng overlay dịu tai, tỉ lệ thuận với âm lượng loa thay vì nhảy vọt 75%."""
    if vol <= 0.0:
        return 0.0
    return min(0.85, max(vol * 1.5, min(0.35, vol + 0.14)))


def mix_pcm_frame(
    music_frame: Optional[bytes],
    voice_frame: Optional[bytes],
    volume_pct: int = DEFAULT_VOLUME,
    duck_ratio: float = DUCK_MUSIC_RATIO,
) -> bytes:
    """
    Trộn khung PCM 20ms (960 bytes = 480 mẫu 16-bit signed LE) giữa nhạc nền và giọng Smart Butler.
    - Nếu có cả nhạc + giọng: hạ nhạc xuống `duck_ratio` (20%) và giữ thông báo giọng nói rõ nét, dịu tai theo volume.
    - Nếu volume_pct == 0 (mute): tắt hoàn toàn cả hai kênh.
    """
    m_pad = _pad_pcm_frame(music_frame)
    v_pad = _pad_pcm_frame(voice_frame)

    if not m_pad and not v_pad:
        return _SILENCE_FRAME

    vol = max(0.0, min(1.0, float(volume_pct) / 100.0))
    if vol <= 0.0:
        return _SILENCE_FRAME

    if m_pad and not v_pad:
        if vol >= 0.995:
            return m_pad
        return _scale_pcm16(m_pad, vol)

    voice_gain = _resolve_voice_gain(vol)
    if v_pad and not m_pad:
        return _scale_pcm16(v_pad, voice_gain)

    # Cả nhạc và thoại cùng phát -> Duck nhạc nền xuống 20% và trộn 1 pass
    music_gain = vol * max(0.0, min(1.0, float(duck_ratio)))
    return esp32_dsp.mix_pcm16(m_pad, music_gain, v_pad, voice_gain)


def _linear_resample_mono_s16(pcm_bytes: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """Resample nhanh hoàn toàn bằng Python thuần cho WAV khi không có `sox` trên môi trường test."""
    if not pcm_bytes or src_rate <= 0 or src_rate == dst_rate:
        return pcm_bytes
    src = array("h")
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    src.frombytes(pcm_bytes[:usable])
    n_src = len(src)
    if n_src < 2:
        return b""
    n_dst = max(1, int(round(n_src * float(dst_rate) / float(src_rate))))
    ratio = float(src_rate) / float(dst_rate)
    out = array("h")
    for i in range(n_dst):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        if idx + 1 < n_src:
            val = int(src[idx] * (1.0 - frac) + src[idx + 1] * frac)
        else:
            val = src[n_src - 1]
        out.append(max(-32768, min(32767, val)))
    return out.tobytes()


def _decode_wav_bytes_pure(wav_bytes: bytes) -> Optional[bytes]:
    """Giải mã WAV chuẩn 16-bit bằng thư viện chuẩn `wave` (0 subprocess, <1ms)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return None

    if sw != 2 or not frames:
        return None
    if nch == 2:
        arr = array("h")
        usable = len(frames) - (len(frames) % 4)
        arr.frombytes(frames[:usable])
        mono = array("h", ((arr[i] + arr[i + 1]) // 2 for i in range(0, len(arr), 2)))
        frames = mono.tobytes()
    elif nch != 1:
        return None
    if sr != SAMPLE_RATE:
        frames = _linear_resample_mono_s16(frames, sr, SAMPLE_RATE)
    return frames


async def decode_voice_bytes_to_pcm(audio_bytes: bytes, audio_format: str = "ogg") -> bytes:
    """Chuyển voice note (.ogg Opus / .wav / .pcm) sang luồng PCM 24000 Hz 16-bit signed mono."""
    if not audio_bytes:
        return b""

    fmt = str(audio_format or "").strip().lower().lstrip(".")
    if fmt in ("pcm", "raw"):
        usable = len(audio_bytes) - (len(audio_bytes) % 2)
        return audio_bytes[:usable]

    if fmt == "wav" or audio_bytes[:4] == b"RIFF":
        pure = _decode_wav_bytes_pure(audio_bytes)
        if pure is not None:
            return pure
        if shutil.which("sox"):
            cmd = ["sox", "-q", "-t", "wav", "-", "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1", "-"]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"]
        else:
            return b""
    else:
        is_opus = (fmt == "opus") or (b"OpusHead" in audio_bytes[:96])
        if is_opus and shutil.which("opusdec") and shutil.which("sox") and shutil.which("sh"):
            cmd = [
                "sh", "-c",
                f"opusdec --quiet --rate {SAMPLE_RATE} --force-wav - - | "
                f"sox --ignore-length -q -t wav - -t raw -r {SAMPLE_RATE} -e signed -b 16 -c 1 -",
            ]
        elif is_opus and shutil.which("opusdec"):
            # `opusdec - -` mặc định xuất raw PCM signed 16-bit khi không truyền `--force-wav`
            cmd = ["opusdec", "--quiet", "--rate", str(SAMPLE_RATE), "-", "-"]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"]
        elif shutil.which("sox"):
            cmd = ["sox", "-q", "-t", fmt or "ogg", "-", "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1", "-"]
        else:
            logger.warning("Không tìm thấy bộ giải mã opusdec/sox/ffmpeg cho voice overlay")
            return b""

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(input=audio_bytes), timeout=15.0)
        if proc.returncode != 0:
            logger.warning(f"Giải mã voice overlay thất bại (rc={proc.returncode}): {(err_b or b'')[:200]}")
            return b""
        usable = len(out_b) - (len(out_b) % 2)
        return out_b[:usable]
    except asyncio.CancelledError:
        if proc is not None and getattr(proc, "returncode", None) is None:
            await _terminate_subprocess_group(proc, timeout_sec=1.0)
        raise
    except Exception as e:
        if proc is not None and getattr(proc, "returncode", None) is None:
            await _terminate_subprocess_group(proc, timeout_sec=1.0)
        logger.warning(f"Lỗi decode_voice_bytes_to_pcm: {e}")
        return b""


def _iter_mp4_boxes(buf: bytes, start: int, end: int):
    """Duyệt từng ISO BMFF box `(type, box_start, payload_start, box_end)` trong khoảng byte cho trước."""
    pos = start
    while pos + 8 <= end:
        sz, tp = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if sz == 1 and pos + 16 <= end:
            sz = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        elif sz == 0:
            sz = end - pos
        if sz < hdr or pos + sz > end:
            break
        yield tp, pos, pos + hdr, pos + sz
        pos += sz


def _read_mp4_desc_len(buf: bytes, off: int, limit: int) -> Tuple[int, int]:
    """Đọc độ dài biến thiên (varint 7-bit) của MP4 Descriptor."""
    val = 0
    for _ in range(4):
        if off >= limit:
            break
        b = buf[off]
        off += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val, off


def _parse_aac_asc_from_fmp4(data: bytes) -> Tuple[int, int, int]:
    """Trích xuất `(profile, freq_idx, chan_cfg)` từ box `esds` (AudioSpecificConfig) của fMP4."""
    default_cfg = (1, 4, 2)  # AAC-LC, 44100 Hz, stereo
    esds_pos = data.find(b"esds")
    if esds_pos < 4 or esds_pos + 16 > len(data):
        return default_cfg

    box_sz = struct.unpack(">I", data[esds_pos - 4:esds_pos])[0]
    box_end = min(len(data), esds_pos - 4 + max(box_sz, 64))
    off = esds_pos + 8  # qua b"esds" (4B) + FullBox version/flags (4B)

    while off + 2 <= box_end:
        tag = data[off]
        off += 1
        ln, off = _read_mp4_desc_len(data, off, box_end)
        if tag == 0x03:
            if off + 3 > box_end:
                break
            flags = data[off + 2]
            off += 3
            if flags & 0x80:
                off += 2
            if flags & 0x40 and off < box_end:
                off += 1 + data[off]
            if flags & 0x20:
                off += 2
        elif tag == 0x04:
            off += 13
        elif tag == 0x05:
            if off + 2 <= box_end and ln >= 2:
                b0, b1 = data[off], data[off + 1]
                aot = (b0 >> 3) & 0x1F
                freq_idx = ((b0 & 0x07) << 1) | ((b1 >> 7) & 0x01)
                chan_cfg = (b1 >> 3) & 0x0F
                profile = 1 if aot in (2, 5, 29) else max(0, min(3, aot - 1))
                if 0 <= freq_idx <= 12 and 1 <= chan_cfg <= 7:
                    return profile, freq_idx, chan_cfg
            break
        else:
            off += max(0, ln)
    return default_cfg


def _extract_trun_sample_sizes(data: bytes, traf_start: int, traf_end: int) -> List[int]:
    """Lấy danh sách kích thước từng khung AAC thô từ các box `tfhd`/`trun` bên trong `traf`."""
    def_sz = 0
    sizes: List[int] = []
    for tp3, _s3, h3, e3 in _iter_mp4_boxes(data, traf_start, traf_end):
        if tp3 == b"tfhd" and h3 + 8 <= e3:
            flags = int.from_bytes(data[h3 + 1:h3 + 4], "big")
            off = h3 + 8
            if flags & 0x01:
                off += 8
            if flags & 0x02:
                off += 4
            if flags & 0x08:
                off += 4
            if (flags & 0x10) and off + 4 <= e3:
                def_sz = struct.unpack(">I", data[off:off + 4])[0]
        elif tp3 == b"trun" and h3 + 8 <= e3:
            flags = int.from_bytes(data[h3 + 1:h3 + 4], "big")
            cnt = struct.unpack(">I", data[h3 + 4:h3 + 8])[0]
            off = h3 + 8
            if flags & 0x01:
                off += 4
            if flags & 0x04:
                off += 4
            for _ in range(cnt):
                if off >= e3:
                    break
                if flags & 0x100:
                    off += 4
                if flags & 0x200 and off + 4 <= e3:
                    sz_s = struct.unpack(">I", data[off:off + 4])[0]
                    off += 4
                else:
                    sz_s = def_sz
                if flags & 0x400:
                    off += 4
                if flags & 0x800:
                    off += 4
                if sz_s > 0:
                    sizes.append(sz_s)
    return sizes


def _make_adts_header(frame_len: int, profile: int, freq_idx: int, chan_cfg: int) -> bytes:
    """Tạo header ADTS 7 byte (MPEG-4, no CRC) cho một khung AAC thô."""
    fl = frame_len + 7
    return bytes([
        0xFF,
        0xF1,
        ((profile & 0x03) << 6) | ((freq_idx & 0x0F) << 2) | ((chan_cfg >> 2) & 0x01),
        ((chan_cfg & 0x03) << 6) | ((fl >> 11) & 0x03),
        (fl >> 3) & 0xFF,
        ((fl & 0x07) << 5) | 0x1F,
        0xFC,
    ])


def _unpack_mp4_stsz(data: bytes, h: int, e: int) -> List[int]:
    if h + 12 > e:
        return []
    fixed_sz, cnt = struct.unpack(">II", data[h + 4:h + 12])
    if cnt <= 0 or cnt > 2_000_000:
        return []
    if fixed_sz > 0:
        return [fixed_sz] * cnt
    if h + 12 + cnt * 4 > e:
        cnt = (e - (h + 12)) // 4
    return list(struct.unpack(f">{cnt}I", data[h + 12:h + 12 + cnt * 4]))


def _unpack_mp4_chunk_offsets(data: bytes, tp: bytes, h: int, e: int) -> List[int]:
    if h + 8 > e:
        return []
    cnt = struct.unpack(">I", data[h + 4:h + 8])[0]
    if cnt <= 0 or cnt > 2_000_000:
        return []
    if tp == b"stco":
        cnt = min(cnt, (e - (h + 8)) // 4)
        return list(struct.unpack(f">{cnt}I", data[h + 8:h + 8 + cnt * 4]))
    cnt = min(cnt, (e - (h + 8)) // 8)
    return list(struct.unpack(f">{cnt}Q", data[h + 8:h + 8 + cnt * 8]))


def _unpack_mp4_stsc(data: bytes, h: int, e: int) -> List[Tuple[int, int]]:
    if h + 8 > e:
        return []
    cnt = struct.unpack(">I", data[h + 4:h + 8])[0]
    cnt = min(cnt, max(0, (e - (h + 8)) // 12))
    runs: List[Tuple[int, int]] = []
    off = h + 8
    for _ in range(cnt):
        first_chunk, samples_per_chunk, _sdi = struct.unpack(">III", data[off:off + 12])
        runs.append((first_chunk, samples_per_chunk))
        off += 12
    return runs


def _extract_aac_stbl_from_moov(moov_bytes: bytes) -> Optional[Tuple[Tuple[int, int, int], List[int], List[int], List[Tuple[int, int]]]]:
    """Trích xuất cấu hình AAC (`esds`) + bảng `stsz`/`stsc`/`stco`/`co64` của track âm thanh từ MP4 chuẩn."""
    for tp, _s, mh, me in _iter_mp4_boxes(moov_bytes, 0, len(moov_bytes)):
        if tp != b"moov":
            continue
        for tp_t, _st, th, te in _iter_mp4_boxes(moov_bytes, mh, me):
            if tp_t != b"trak":
                continue
            handler_type = b""
            stbl_range: Optional[Tuple[int, int]] = None
            for tp_m, _sm, mdh, mde in _iter_mp4_boxes(moov_bytes, th, te):
                if tp_m != b"mdia":
                    continue
                for tp_sub, _ss, sh, se in _iter_mp4_boxes(moov_bytes, mdh, mde):
                    if tp_sub == b"hdlr" and sh + 12 <= se:
                        handler_type = moov_bytes[sh + 8:sh + 12]
                    elif tp_sub == b"minf":
                        for tp_minf, _smi, mih, mie in _iter_mp4_boxes(moov_bytes, sh, se):
                            if tp_minf == b"stbl":
                                stbl_range = (mih, mie)
            if handler_type != b"soun" or stbl_range is None:
                continue

            sample_sizes: List[int] = []
            chunk_offsets: List[int] = []
            stsc_runs: List[Tuple[int, int]] = []
            has_mp4a = False
            asc = (1, 4, 2)
            for tp_sb, s_sb, bh, be in _iter_mp4_boxes(moov_bytes, stbl_range[0], stbl_range[1]):
                if tp_sb == b"stsd":
                    stsd_slice = moov_bytes[s_sb:be]
                    if b"mp4a" in stsd_slice:
                        has_mp4a = True
                        asc = _parse_aac_asc_from_fmp4(stsd_slice)
                elif tp_sb == b"stsz":
                    sample_sizes = _unpack_mp4_stsz(moov_bytes, bh, be)
                elif tp_sb in (b"stco", b"co64") and not chunk_offsets:
                    chunk_offsets = _unpack_mp4_chunk_offsets(moov_bytes, tp_sb, bh, be)
                elif tp_sb == b"stsc":
                    stsc_runs = _unpack_mp4_stsc(moov_bytes, bh, be)

            if has_mp4a and sample_sizes and chunk_offsets:
                if not stsc_runs:
                    stsc_runs = [(1, len(sample_sizes))]
                return asc, sample_sizes, chunk_offsets, stsc_runs
    return None


def _write_stbl_aac_to_adts(
    fin,
    fout,
    file_size: int,
    asc: Tuple[int, int, int],
    sample_sizes: List[int],
    chunk_offsets: List[int],
    stsc_runs: List[Tuple[int, int]],
) -> int:
    """Giải bọc từng khung AAC theo bảng `stco`/`stsc`/`stsz` sang ADTS mà không nạp cả file vào RAM."""
    profile, freq_idx, chan_cfg = asc
    sample_idx = 0
    total_samples = len(sample_sizes)
    num_chunks = len(chunk_offsets)
    run_idx = 0
    wrote_frames = 0

    for c_idx in range(1, num_chunks + 1):
        if sample_idx >= total_samples:
            break
        while run_idx + 1 < len(stsc_runs) and c_idx >= stsc_runs[run_idx + 1][0]:
            run_idx += 1
        samples_in_chunk = stsc_runs[run_idx][1] if run_idx < len(stsc_runs) else 0
        chunk_off = chunk_offsets[c_idx - 1]
        if samples_in_chunk <= 0 or chunk_off < 0 or chunk_off >= file_size:
            continue
        fin.seek(chunk_off)
        for _ in range(samples_in_chunk):
            if sample_idx >= total_samples:
                break
            sz_s = sample_sizes[sample_idx]
            sample_idx += 1
            if sz_s <= 0:
                continue
            frame_data = fin.read(sz_s)
            if len(frame_data) < sz_s:
                return wrote_frames
            fout.write(_make_adts_header(sz_s, profile, freq_idx, chan_cfg))
            fout.write(frame_data)
            wrote_frames += 1
    return wrote_frames


def _ensure_faad_compatible_m4a(filepath: str) -> bool:
    """
    Chuyển tại chỗ file MP4 AAC (`dash`/`iso6` fMP4 hoặc `isom`/`mp42`/`M4A ` MP4 chuẩn)
    sang luồng ADTS AAC thô (streaming, tốn < 2MB RAM kể cả file dài 150MB)
    để `faad` V2.10 giải mã trực tiếp mà không lỗi `parse:-1`.
    Trả về True nếu file đã được chuyển đổi, False nếu đã là ADTS hoặc không phải AAC-in-MP4.
    """
    if not filepath or not os.path.isfile(filepath):
        return False
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 16:
            return False
        with open(filepath, "rb") as f:
            head = f.read(32)
    except Exception:
        return False

    if len(head) < 12 or head[:2] in (b"\xff\xf1", b"\xff\xf9") or head[4:8] != b"ftyp":
        return False

    tmp_path = filepath + ".adts.tmp"
    profile, freq_idx, chan_cfg = (1, 4, 2)
    pending_sizes: List[int] = []
    wrote_frames = 0
    standard_stbl = None

    try:
        with open(filepath, "rb") as fin, open(tmp_path, "wb", buffering=256 * 1024) as fout:
            pos = 0
            while pos + 8 <= file_size:
                fin.seek(pos)
                hdr_bytes = fin.read(16)
                if len(hdr_bytes) < 8:
                    break
                sz, tp = struct.unpack(">I4s", hdr_bytes[:8])
                hdr = 8
                if sz == 1 and len(hdr_bytes) >= 16:
                    sz = struct.unpack(">Q", hdr_bytes[8:16])[0]
                    hdr = 16
                elif sz == 0:
                    sz = file_size - pos
                if sz < hdr or pos + sz > file_size:
                    break

                payload_start = pos + hdr
                payload_len = sz - hdr
                box_end = pos + sz

                if tp == b"moov" and sz <= 16 * 1024 * 1024:
                    fin.seek(pos)
                    moov_bytes = fin.read(sz)
                    profile, freq_idx, chan_cfg = _parse_aac_asc_from_fmp4(moov_bytes)
                    standard_stbl = _extract_aac_stbl_from_moov(moov_bytes)
                elif tp == b"moof" and payload_len <= 8 * 1024 * 1024:
                    standard_stbl = None  # File fMP4 phân mảnh ưu tiên moof+mdat
                    fin.seek(payload_start)
                    moof_payload = fin.read(payload_len)
                    for tp2, _s2, h2, e2 in _iter_mp4_boxes(moof_payload, 0, len(moof_payload)):
                        if tp2 == b"traf":
                            pending_sizes.extend(_extract_trun_sample_sizes(moof_payload, h2, e2))
                elif tp == b"mdat" and pending_sizes:
                    fin.seek(payload_start)
                    cur = payload_start
                    for sz_s in pending_sizes:
                        if cur + sz_s > box_end:
                            break
                        frame_data = fin.read(sz_s)
                        if len(frame_data) < sz_s:
                            break
                        fout.write(_make_adts_header(sz_s, profile, freq_idx, chan_cfg))
                        fout.write(frame_data)
                        cur += sz_s
                        wrote_frames += 1
                    pending_sizes.clear()

                pos = box_end

            if wrote_frames == 0 and standard_stbl is not None:
                asc, sample_sizes, chunk_offsets, stsc_runs = standard_stbl
                wrote_frames = _write_stbl_aac_to_adts(
                    fin,
                    fout,
                    file_size,
                    asc,
                    sample_sizes,
                    chunk_offsets,
                    stsc_runs,
                )

        if wrote_frames == 0:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
            return False

        os.replace(tmp_path, filepath)
        return True
    except Exception as e:
        logger.warning(f"Lỗi chuyển fMP4 sang ADTS cho {filepath}: {e}")
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _build_track_decoder_cmd(filepath: str) -> Optional[List[str]]:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".m4a", ".aac", ".mp4"):
        if shutil.which("faad") and shutil.which("sox") and shutil.which("sh"):
            return [
                "sh", "-c",
                f'faad -q -w "$1" | sox --ignore-length -q -t wav - -t raw -r {SAMPLE_RATE} -e signed -b 16 -c 1 -',
                "esp32_decode",
                filepath,
            ]
    elif ext == ".mp3":
        if shutil.which("mpg123"):
            return ["mpg123", "-q", "-s", "-r", str(SAMPLE_RATE), "-m", "--encoding", "s16", filepath]
        if shutil.which("sox"):
            return ["sox", "-q", filepath, "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1", "-"]
    elif ext in (".opus", ".ogg"):
        if shutil.which("opusdec") and shutil.which("sox") and shutil.which("sh"):
            return [
                "sh", "-c",
                f'opusdec --quiet --rate {SAMPLE_RATE} --force-wav "$1" - | sox --ignore-length -q -t wav - -t raw -r {SAMPLE_RATE} -e signed -b 16 -c 1 -',
                "esp32_decode",
                filepath,
            ]
    elif ext == ".wav":
        if shutil.which("sox"):
            return ["sox", "-q", filepath, "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1", "-"]

    if shutil.which("ffmpeg"):
        return [
            "ffmpeg", "-v", "quiet", "-i", filepath,
            "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
        ]
    return None


async def _terminate_subprocess_group(proc: asyncio.subprocess.Process, timeout_sec: float = 1.5) -> None:
    """Dừng trọn vẹn cả process group (`sh -c "faad | sox"` / `yt-dlp`) để tránh để lại tiến trình con mồ côi."""
    use_pg = (os.name == "posix")
    pid = getattr(proc, "pid", None)
    pgid = pid if (use_pg and pid is not None) else None

    if getattr(proc, "returncode", None) is None:
        for sig, wait_timeout in ((signal.SIGTERM, timeout_sec), (signal.SIGKILL, 1.0)):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                elif sig == signal.SIGTERM:
                    if hasattr(proc, "terminate"):
                        proc.terminate()
                else:
                    if hasattr(proc, "kill"):
                        proc.kill()
            except ProcessLookupError:
                break
            except Exception:
                try:
                    if sig == signal.SIGTERM and hasattr(proc, "terminate"):
                        proc.terminate()
                    elif hasattr(proc, "kill"):
                        proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
                    break
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
            break

    # Khi `sh` thoát sớm sau SIGTERM, tiến trình con trong pipeline (như `sox`) có thể vẫn giữ pipe -> quét SIGKILL cả nhóm.
    if pgid is not None:
        await asyncio.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass


class _TrackPcmSource:
    """Nguồn đọc PCM 24kHz s16le mono theo khung 960 bytes từ file `.pcm` thô hoặc tiến trình C siêu nhẹ."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.ext = os.path.splitext(filepath)[1].lower()
        self._raw_file = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._eof = False

    async def start(self) -> bool:
        if not os.path.isfile(self.filepath):
            return False
        if self.ext in (".pcm", ".raw"):
            self._raw_file = open(self.filepath, "rb")
            return True
        if self.ext == ".wav" and not shutil.which("sox") and not shutil.which("ffmpeg"):
            with open(self.filepath, "rb") as f:
                pcm = _decode_wav_bytes_pure(f.read())
            if pcm is None:
                return False
            self._raw_file = io.BytesIO(pcm)
            return True

        if self.ext in (".m4a", ".aac", ".mp4") and shutil.which("faad"):
            loop = asyncio.get_running_loop()
            if await loop.run_in_executor(None, _ensure_faad_compatible_m4a, self.filepath):
                logger.info(f"🎵 [Jukebox] Chuyển đổi container DASH -> ADTS khi phát: {os.path.basename(self.filepath)}")

        cmd = _build_track_decoder_cmd(self.filepath)
        if not cmd:
            logger.error(f"Không có công cụ giải mã phù hợp cho file: {self.filepath}")
            return False
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        return True

    async def read_frame(self) -> Optional[bytes]:
        if self._eof:
            return None
        if self._raw_file is not None:
            chunk = self._raw_file.read(FRAME_BYTES)
            if not chunk:
                self._eof = True
                return None
            return chunk

        if self._proc is None or self._proc.stdout is None:
            self._eof = True
            return None

        buf = bytearray()
        while len(buf) < FRAME_BYTES:
            try:
                chunk = await self._proc.stdout.read(FRAME_BYTES - len(buf))
            except Exception:
                chunk = b""
            if not chunk:
                self._eof = True
                break
            buf.extend(chunk)
        return bytes(buf) if buf else None

    async def close(self) -> None:
        self._eof = True
        if self._raw_file is not None:
            try:
                self._raw_file.close()
            except Exception:
                pass
            self._raw_file = None
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            await _terminate_subprocess_group(proc, timeout_sec=1.5)


class ESP32AudioManager:
    """Điều phối phát nhạc Jukebox, DSP EQ loa 4cm, Phiên Tập trung/Báo thức, Voice Ducking, TTP223 & DHT11."""

    def __init__(self):
        self.status: str = "stopped"  # stopped | loading | playing | paused
        self.current_track: Optional[Dict[str, Any]] = None
        self.queue: List[Dict[str, Any]] = []
        self._volume: int = DEFAULT_VOLUME
        self.auto_dj: bool = True
        self._voice_autoplay: bool = DEFAULT_VOICE_AUTOPLAY
        self._persisted_db_path: Optional[str] = None
        self.sleep_timer_until: float = 0.0
        self.loading_query: str = ""
        self.last_error: str = ""
        self.last_download_notice: str = ""

        self.dsp = esp32_dsp.DspProcessor(sample_rate=SAMPLE_RATE)
        self._eq_settings: Dict[str, Any] = dict(self.dsp.config)
        self._norm_enabled: bool = esp32_dsp.DEFAULT_NORM_ENABLED
        self.dsp.configure_norm(enabled=self._norm_enabled)
        self.timed_session: Optional[Dict[str, Any]] = None
        self.alarms: List[Dict[str, Any]] = []
        self.alarm_ringing: Optional[Dict[str, Any]] = None
        self._recent_track_ids: List[str] = []

        self._fade_cur: float = 1.0
        self._fade_target: float = 1.0
        self._fade_step: float = 0.0
        self._fade_done_action: str = ""
        self._stopping_fade: bool = False

        self.esp32_ip: str = ""
        self.esp32_last_seen: float = 0.0
        self.esp32_rssi: Optional[int] = None
        self.active_stream_clients: int = 0
        self.active_ws_clients: int = 0
        self.esp32_codec: str = "pcm"

        self._voice_pcm_buffer = bytearray()
        self._voice_end_cooldown_until: float = 0.0
        self._source: Optional[_TrackPcmSource] = None
        self._source_track_id: Optional[str] = None
        self._source_revision: int = 0
        self._flush_revision: int = 0
        self._stream_gen: int = 0
        self._ws_gen: int = 0
        self._ws_control_queues: Dict[int, asyncio.Queue] = {}
        self._ws_last_hello: Dict[str, Any] = {}

        self.live_stream_active: bool = False
        self.live_stream_paused: bool = False
        self.live_stream_source: str = ""
        self.live_stream_label: str = ""
        self.live_stream_client_ip: str = ""
        self.live_stream_started_at: float = 0.0
        self.live_stream_last_frame_at: float = 0.0
        self.live_stream_last_audible_at: float = 0.0
        self._live_pcm_buffer = bytearray()
        self._live_gen: int = 0
        self._pc_bridge_pull_task: Optional[asyncio.Task] = None

        self._download_lock: Optional[asyncio.Lock] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._bg_tasks: set = set()
        self._active_downloads: int = 0
        self._timer_task: Optional[asyncio.Task] = None
        self._backfill_task: Optional[asyncio.Task] = None
        self._session_event_handler = None
        self._pending_timer_events: set = set()
        self._paused_at: float = 0.0

    def _ensure_persisted_loaded(self) -> None:
        cur_db = str(getattr(config, "DB_PATH", "") or "")
        if self._persisted_db_path == cur_db:
            return
        try:
            raw_vol = database.get_metadata(_META_JUKEBOX_VOLUME)
            raw_va = database.get_metadata(_META_VOICE_AUTOPLAY)
            raw_eq = database.get_metadata(_META_EQ_SETTINGS)
            raw_norm = database.get_metadata(_META_NORM_ENABLED)
            raw_alarms = database.get_metadata(_META_ALARMS)
            raw_sess = database.get_metadata(_META_TIMED_SESSION)
        except Exception:
            return

        if raw_vol is not None and str(raw_vol).strip():
            try:
                self._volume = max(0, min(100, int(str(raw_vol).strip())))
            except ValueError:
                self._volume = DEFAULT_VOLUME
        else:
            self._volume = DEFAULT_VOLUME

        if raw_va is not None and str(raw_va).strip():
            self._voice_autoplay = str(raw_va).strip().lower() in ("1", "true", "yes", "on")
        else:
            self._voice_autoplay = DEFAULT_VOICE_AUTOPLAY

        if raw_norm is not None and str(raw_norm).strip():
            self._norm_enabled = str(raw_norm).strip().lower() in ("1", "true", "yes", "on")
        else:
            self._norm_enabled = esp32_dsp.DEFAULT_NORM_ENABLED
        self.dsp.configure_norm(enabled=self._norm_enabled)

        if raw_eq and str(raw_eq).strip():
            try:
                parsed_eq = json.loads(raw_eq)
                if isinstance(parsed_eq, dict):
                    self._eq_settings = self.dsp.configure(
                        preset=parsed_eq.get("preset"),
                        highpass_hz=parsed_eq.get("highpass_hz"),
                        bass_db=parsed_eq.get("bass_db"),
                        treble_db=parsed_eq.get("treble_db"),
                        enabled=parsed_eq.get("enabled"),
                    )
            except Exception:
                self._eq_settings = self.dsp.configure(preset=esp32_dsp.DEFAULT_PRESET)
        else:
            self._eq_settings = self.dsp.configure(preset=esp32_dsp.DEFAULT_PRESET)

        self.alarms = []
        if raw_alarms and str(raw_alarms).strip():
            try:
                arr = json.loads(raw_alarms)
                if isinstance(arr, list):
                    now_ts = time.time()
                    self.alarms = [
                        a for a in arr
                        if isinstance(a, dict) and float(a.get("due_at") or 0) > now_ts - 60.0
                    ]
            except Exception:
                self.alarms = []

        self.timed_session = None
        if raw_sess and str(raw_sess).strip():
            try:
                sess = json.loads(raw_sess)
                if isinstance(sess, dict) and float(sess.get("ends_at") or 0) > time.time():
                    self.timed_session = sess
            except Exception:
                self.timed_session = None

        self._persisted_db_path = cur_db

    def reload_persisted_state(self) -> None:
        """Nạp lại cấu hình bền vững (volume, EQ, báo thức, timed_session) sau khi restore DB."""
        self._persisted_db_path = ""
        self._ensure_persisted_loaded()
        self.push_ws_control({"type": "state", "status": self.status, "volume": self._volume})
        self.publish_update()

    def _save_alarms(self) -> None:
        try:
            database.set_metadata(_META_ALARMS, json.dumps(self.alarms, ensure_ascii=False))
        except Exception:
            pass

    def _save_timed_session(self) -> None:
        try:
            val = json.dumps(self.timed_session, ensure_ascii=False) if self.timed_session else ""
            database.set_metadata(_META_TIMED_SESSION, val)
        except Exception:
            pass

    def get_eq_settings(self) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        out = dict(self._eq_settings)
        out["normalize"] = bool(self._norm_enabled)
        out["norm_status"] = self.dsp.get_norm_status()
        out["presets"] = {
            k: {"label": v["label"], "highpass_hz": v["highpass_hz"], "bass_db": v["bass_db"], "treble_db": v["treble_db"], "enabled": v["enabled"]}
            for k, v in esp32_dsp.EQ_PRESETS.items()
        }
        return out

    def set_normalize_enabled(self, enabled: bool) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        self._norm_enabled = bool(enabled)
        self.dsp.configure_norm(enabled=self._norm_enabled, reset_agc=not self._norm_enabled)
        try:
            database.set_metadata(_META_NORM_ENABLED, "1" if self._norm_enabled else "0")
            self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")
        except Exception:
            pass
        self.publish_update()
        logger.info(f"🎚️ [Loudness] Chuẩn hóa âm lượng bài to/nhỏ: {'BẬT' if self._norm_enabled else 'TẮT'}")
        return self.get_eq_settings()

    def set_eq_settings(
        self,
        preset: Optional[str] = None,
        bass_db: Optional[float] = None,
        treble_db: Optional[float] = None,
        highpass_hz: Optional[float] = None,
        enabled: Optional[bool] = None,
        normalize: Optional[bool] = None,
    ) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        if normalize is not None:
            self._norm_enabled = bool(normalize)
            self.dsp.configure_norm(enabled=self._norm_enabled, reset_agc=not self._norm_enabled)
            try:
                database.set_metadata(_META_NORM_ENABLED, "1" if self._norm_enabled else "0")
                self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")
            except Exception:
                pass
            if preset is None and bass_db is None and treble_db is None and highpass_hz is None and enabled is None:
                self.publish_update()
                logger.info(f"🎚️ [Loudness] Chuẩn hóa âm lượng bài to/nhỏ: {'BẬT' if self._norm_enabled else 'TẮT'}")
                return self.get_eq_settings()

        cur = dict(self._eq_settings)
        if preset is None and any(v is not None for v in (bass_db, treble_db, highpass_hz)):
            # Giữ nguyên highpass/bass/treble còn lại khi kéo từng thanh trượt lẻ
            cfg = self.dsp.configure(
                preset="custom",
                highpass_hz=cur.get("highpass_hz", 60.0) if highpass_hz is None else highpass_hz,
                bass_db=cur.get("bass_db", 4.0) if bass_db is None else bass_db,
                treble_db=cur.get("treble_db", 3.5) if treble_db is None else treble_db,
                enabled=True if enabled is None else enabled,
            )
        else:
            cfg = self.dsp.configure(
                preset=preset or cur.get("preset", esp32_dsp.DEFAULT_PRESET),
                highpass_hz=highpass_hz,
                bass_db=bass_db,
                treble_db=treble_db,
                enabled=enabled,
            )
        self._eq_settings = cfg
        try:
            database.set_metadata(_META_EQ_SETTINGS, json.dumps(cfg, ensure_ascii=False))
            self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")
        except Exception:
            pass
        self.publish_update()
        logger.info(
            f"🎛️ [EQ] preset={cfg['preset']} | HP={cfg['highpass_hz']}Hz | "
            f"Bass={cfg['bass_db']:+.1f}dB | Treble={cfg['treble_db']:+.1f}dB | Norm={'ON' if self._norm_enabled else 'OFF'}"
        )
        return self.get_eq_settings()

    def set_session_event_handler(self, handler) -> None:
        self._session_event_handler = handler

    def ensure_background_loops(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._timer_task is None or self._timer_task.done():
            self._timer_task = loop.create_task(self._precision_timer_loop())
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = loop.create_task(self._backfill_cache_loudness_loop())
        if hasattr(esp32_dsp, "warmup_async"):
            t = loop.create_task(esp32_dsp.warmup_async())
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    async def shutdown(self) -> None:
        """Đóng toàn bộ luồng giải mã con và task nền trước khi tắt event loop."""
        for task in [self._timer_task, self._backfill_task, *list(self._bg_tasks), *list(self._pending_timer_events)]:
            if task and not task.done():
                task.cancel()
        tasks_to_wait = [t for t in [self._timer_task, self._backfill_task, *list(self._bg_tasks), *list(self._pending_timer_events)] if t]
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        self._timer_task = None
        self._backfill_task = None
        if self._source is not None:
            try:
                await self._source.close()
            except Exception:
                pass
            self._source = None
            self._source_track_id = None

    async def _backfill_cache_loudness_loop(self) -> None:
        """Quét nền nhẹ từng bài trong cache chưa đo hoặc đang dùng chuẩn cũ (< TRACK_NORM_VERSION)."""
        try:
            await asyncio.sleep(2.0)
            cdir, _ = get_cache_dir_with_mode()
            idx = _load_cache_index_from_dir(cdir)
            vids = [
                vid for vid, ent in (idx.get("tracks") or {}).items()
                if _track_needs_loudness_scan(ent)
            ]
            if not vids:
                return
            loop = asyncio.get_running_loop()
            updated_cnt = 0
            for vid in vids:
                if self._active_downloads > 0:
                    await asyncio.sleep(2.0)
                cur_vid = (self.current_track or {}).get("video_id")
                is_cur_playing = (self.status == "playing" and cur_vid == vid)
                cur_idx = _load_cache_index_from_dir(cdir)
                ent = (cur_idx.get("tracks") or {}).get(vid)
                if not _track_needs_loudness_scan(ent):
                    continue
                fname = os.path.basename(str(ent.get("filename") or ""))
                fpath = os.path.join(cdir, fname)
                if not fname or not os.path.isfile(fpath) or os.path.getsize(fpath) < 4096:
                    continue
                measured = await loop.run_in_executor(
                    None,
                    _analyze_track_loudness_sync,
                    fpath,
                    420.0,
                    not is_cur_playing,
                )
                if measured:
                    latest_idx = _load_cache_index_from_dir(cdir)
                    latest_ent = (latest_idx.get("tracks") or {}).get(vid)
                    if isinstance(latest_ent, dict):
                        latest_ent["norm_rms_db"] = measured["rms_db"]
                        if "peak_db" in measured:
                            latest_ent["norm_peak_db"] = measured["peak_db"]
                        latest_ent["norm_ver"] = TRACK_NORM_VERSION
                        try:
                            latest_ent["size_bytes"] = os.path.getsize(fpath)
                        except OSError:
                            pass
                        save_cache_index(latest_idx, target_dir=cdir)
                        updated_cnt += 1
                        if (self.current_track or {}).get("video_id") == vid:
                            self.current_track["norm_rms_db"] = measured["rms_db"]
                            if "peak_db" in measured:
                                self.current_track["norm_peak_db"] = measured["peak_db"]
                            tg, has_tg = esp32_dsp.compute_track_norm_gain(
                                measured.get("rms_db"),
                                measured.get("peak_db"),
                            )
                            self.dsp.configure_norm(
                                enabled=self._norm_enabled,
                                track_gain=tg,
                                has_track_gain=has_tg,
                                reset_agc=False,
                            )
                await asyncio.sleep(0.5)
            if updated_cnt > 0:
                logger.info(f"🎚️ [Loudness] Đã phân tích chuẩn hóa âm lượng (v{TRACK_NORM_VERSION}) cho {updated_cnt} bài trong cache.")
                self.publish_update()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"🎚️ [Loudness] Dừng quét nền cache: {e}")

    async def _precision_timer_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._tick_timers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"⏰ [Jukebox Timer] Lỗi tick: {e}")

    def is_quiet_session_active(self, now_ts: Optional[float] = None) -> bool:
        """True nếu đang trong phiên Tập trung hoặc Chợp mắt (tạm hoãn tin nhắn chủ động rủ rỉ)."""
        self._ensure_persisted_loaded()
        sess = self.timed_session
        if not sess:
            return False
        now = float(now_ts if now_ts is not None else time.time())
        return bool(sess.get("silence_proactive")) and float(sess.get("ends_at") or 0) > now

    def start_fade_in(self, duration_sec: float = 8.0, start_gain: float = 0.08) -> None:
        steps = max(1, int(round(max(0.2, duration_sec) * 1000.0 / FRAME_MS)))
        g0 = max(0.02, min(0.95, float(start_gain)))
        self._fade_cur = g0
        self._fade_target = 1.0
        self._fade_step = (1.0 - g0) / float(steps)
        self._fade_done_action = ""
        self._stopping_fade = False

    def start_fade_out(self, duration_sec: float = 2.5, on_complete: str = "stop") -> None:
        steps = max(1, int(round(max(0.2, duration_sec) * 1000.0 / FRAME_MS)))
        self._fade_target = 0.0
        self._fade_step = -max(0.05, self._fade_cur) / float(steps)
        self._fade_done_action = on_complete
        self._stopping_fade = False

    def _reset_fade(self) -> None:
        self._fade_cur = 1.0
        self._fade_target = 1.0
        self._fade_step = 0.0
        self._fade_done_action = ""
        self._stopping_fade = False

    def _apply_fade_to_frame(self, pcm_frame: bytes) -> bytes:
        cur = self._fade_cur
        tgt = self._fade_target
        step = self._fade_step
        if step != 0.0:
            nxt = cur + step
            if (step > 0 and nxt >= tgt) or (step < 0 and nxt <= tgt):
                nxt = tgt
                self._fade_step = 0.0
                if nxt <= 0.005 and self._fade_done_action and not self._stopping_fade:
                    self._stopping_fade = True
                    act = self._fade_done_action
                    self._fade_done_action = ""
                    try:
                        t = asyncio.create_task(self._complete_fade_action(act))
                        self._bg_tasks.add(t)
                        t.add_done_callback(self._bg_tasks.discard)
                    except RuntimeError:
                        pass
            self._fade_cur = max(0.0, min(1.0, nxt))
            cur = self._fade_cur

        if cur >= 0.995:
            return pcm_frame
        if cur <= 0.005:
            return _SILENCE_FRAME
        return _scale_pcm16(pcm_frame, cur)

    async def _complete_fade_action(self, action: str) -> None:
        if action == "pause":
            await self.pause()
        else:
            await self.stop()
        self._reset_fade()

    def _pick_cached_track_for_mode(
        self,
        mode: str,
        query: str = "",
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        if query:
            explicit = find_cached_track(query)
            if explicit:
                return explicit
        tracks = list_cached_tracks().get("tracks") or []
        if not tracks:
            return None

        ex_set = set(exclude_ids or [])
        kws = _MOOD_CACHE_KEYWORDS.get(mode, ())
        if not kws:
            return None

        scored: List[Tuple[int, Dict[str, Any]]] = []
        for t in tracks:
            hay = " ".join([
                str(t.get("title") or ""),
                str(t.get("uploader") or ""),
                " ".join(str(q) for q in (t.get("queries") or [])),
            ]).lower()
            score = sum(1 for kw in kws if kw in hay)
            if score > 0:
                scored.append((score, t))

        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], float(item[1].get("last_played") or 0.0)), reverse=True)
        non_recent = [item for item in scored if item[1].get("video_id") not in ex_set]
        chosen_pool = non_recent[:4] if non_recent else scored[:4]
        chosen = random.choice(chosen_pool)[1]
        return _track_with_path(chosen)

    def _emit_session_event(self, payload: Dict[str, Any]) -> None:
        handler = self._session_event_handler
        if handler is None:
            return
        try:
            task = asyncio.create_task(handler(payload))
            self._pending_timer_events.add(task)
            task.add_done_callback(self._pending_timer_events.discard)
        except RuntimeError:
            pass

    async def start_timed_session(
        self,
        mode: str,
        minutes: int,
        query: str = "",
        label: str = "",
        silence_proactive: Optional[bool] = None,
        play_music: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Khởi động phiên Tập trung / Chill / Chợp mắt theo thời lượng xác định (tới từng giây)."""
        self._ensure_persisted_loaded()
        self.ensure_background_loops()
        m_clean = mode if mode in ("focus", "chill", "nap") else "focus"
        mins = max(1, min(360, int(minutes)))
        now = time.time()
        ends_at = now + mins * 60.0
        default_labels = {
            "focus": f"Tập trung {mins}p",
            "chill": f"Thư giãn {mins}p",
            "nap": f"Chợp mắt {mins}p",
        }
        sess_label = (label or "").strip() or default_labels[m_clean]
        quiet = (m_clean in ("focus", "nap")) if silence_proactive is None else bool(silence_proactive)
        want_music = (m_clean in ("focus", "chill")) if play_music is None else bool(play_music)

        self.timed_session = {
            "id": f"{m_clean}_{int(now)}",
            "mode": m_clean,
            "label": sess_label,
            "duration_min": mins,
            "started_at": now,
            "ends_at": ends_at,
            "query": str(query or "").strip(),
            "silence_proactive": quiet,
        }
        self._save_timed_session()
        # Tách bạch với sleep timer cũ để timed_session chủ động fade-out & gọi sự kiện kết thúc
        self.sleep_timer_until = 0.0
        self.alarm_ringing = None
        self._reset_fade()

        picked_track: Optional[Dict[str, Any]] = None
        queued_fresh = False
        if want_music:
            clean_q = str(query or "").strip()
            cached = self._pick_cached_track_for_mode(
                m_clean, query=clean_q, exclude_ids=self._recent_track_ids[-3:]
            )
            if cached:
                picked_track = cached
                await self.play_track(cached)
                if clean_q and clean_q.lower() not in [normalize_query(x) for x in (cached.get("queries") or [])]:
                    self.spawn_play_or_search(clean_q, play_now=False)
                    queued_fresh = True
                elif not clean_q and len(list_cached_tracks().get("tracks") or []) < 3:
                    fresh_q = random.choice(MOOD_SEARCH_POOLS.get(m_clean, MOOD_SEARCH_POOLS["focus"]))
                    self.spawn_play_or_search(fresh_q, play_now=False)
                    queued_fresh = True
            else:
                search_q = clean_q or random.choice(MOOD_SEARCH_POOLS.get(m_clean, MOOD_SEARCH_POOLS["focus"]))
                self.spawn_play_or_search(search_q, play_now=True)
                queued_fresh = True
        elif m_clean == "nap" and not query and self.status == "playing":
            # Khi báo "chợp mắt 20p" mà không đòi nhạc ru -> dịu dần nhạc hiện tại về im lặng cho dễ ngủ
            self.start_fade_out(3.0, on_complete="stop")

        self.publish_update()
        logger.info(
            f"⏱️ [Session START] mode={m_clean} | {mins}p | label='{sess_label}' | "
            f"quiet_proactive={quiet} | instant_track={picked_track['title'] if picked_track else None}"
        )
        return {
            "ok": True,
            "session": dict(self.timed_session),
            "track": picked_track,
            "loading": queued_fresh and not picked_track,
            "snapshot": self.get_status_snapshot(),
        }

    def cancel_timed_session(self) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        self.timed_session = None
        self._save_timed_session()
        self.publish_update()
        return self.get_status_snapshot()

    def add_alarm_clock(
        self,
        due_ts: Optional[float] = None,
        time_str: str = "",
        label: str = "",
        query: str = "",
        kind: str = "alarm",
        duration_min: int = 0,
    ) -> Dict[str, Any]:
        """Đặt báo thức hoặc lịch phát nhạc tự động theo mốc giờ VN HH:MM hoặc timestamp cụ thể."""
        self._ensure_persisted_loaded()
        self.ensure_background_loops()
        now = time.time()
        resolved_ts = float(due_ts) if due_ts else 0.0
        hm_clean = ""
        if not resolved_ts and time_str:
            resolved_ts, hm_clean = parse_schedule_time_vn(time_str, now_ts=now)
        if not resolved_ts or resolved_ts <= now:
            raise ValueError("Giờ báo thức không hợp lệ (dùng dạng HH:MM).")

        if not hm_clean:
            hm_clean = datetime.datetime.fromtimestamp(resolved_ts, tz=VN_TZ).strftime("%H:%M")
        norm_kind = "music" if str(kind or "").strip().lower() == "music" else "alarm"
        dur_min = max(0, min(360, _safe_int(duration_min, 0) or 0))
        clean_query = str(query or "").strip()
        prefix = "mus" if norm_kind == "music" else "alm"
        aid = f"{prefix}_{int(resolved_ts)}_{random.randint(100, 999)}"
        default_lbl = (
            f"Phát nhạc {hm_clean}" + (f" • {clean_query}" if clean_query else "")
            if norm_kind == "music"
            else f"Báo thức {hm_clean}"
        )
        alarm = {
            "id": aid,
            "due_at": resolved_ts,
            "time_vn": hm_clean,
            "label": (label or "").strip() or default_lbl,
            "query": clean_query,
            "kind": norm_kind,
            "duration_min": dur_min,
            "created_at": now,
        }
        # Tránh đặt trùng cùng loại trong vòng 90 giây
        self.alarms = [
            a for a in self.alarms
            if abs(float(a.get("due_at") or 0) - resolved_ts) > 90.0
            or str(a.get("kind") or "alarm") != norm_kind
        ]
        self.alarms.append(alarm)
        self.alarms.sort(key=lambda a: float(a.get("due_at") or 0))
        self._save_alarms()
        self.publish_update()
        tag = "Music SCHED" if norm_kind == "music" else "Alarm SET"
        logger.info(f"⏰ [{tag}] {alarm['time_vn']} ({alarm['label']})")
        return alarm

    def schedule_music(
        self,
        due_ts: Optional[float] = None,
        time_str: str = "",
        delay_minutes: int = 0,
        query: str = "",
        label: str = "",
        duration_min: int = 0,
    ) -> Dict[str, Any]:
        """Lên lịch phát nhạc vào mốc giờ VN hoặc sau delay_minutes phút, kèm tải trước ngầm nếu chưa cache."""
        clean_query = str(query or "").strip()
        delay_m = max(0, min(1440, _safe_int(delay_minutes, 0) or 0))
        if not due_ts and not time_str and delay_m > 0:
            due_ts = time.time() + delay_m * 60.0
        item = self.add_alarm_clock(
            due_ts=due_ts,
            time_str=time_str,
            label=label,
            query=clean_query,
            kind="music",
            duration_min=duration_min,
        )
        if clean_query and find_cached_track(clean_query) is None:
            self.spawn_precache_track(clean_query)
        return item

    def cancel_alarm(self, alarm_id: str) -> bool:
        self._ensure_persisted_loaded()
        before = len(self.alarms)
        self.alarms = [a for a in self.alarms if str(a.get("id")) != str(alarm_id)]
        changed = len(self.alarms) != before
        if changed:
            self._save_alarms()
            self.publish_update()
        return changed

    async def dismiss_alarm(self) -> Dict[str, Any]:
        if self.alarm_ringing:
            self.alarm_ringing = None
            await self.stop()
            self._voice_pcm_buffer.clear()
            self.publish_update()
        return self.get_status_snapshot()

    async def snooze_alarm(self, minutes: int = 5) -> Dict[str, Any]:
        mins = max(1, min(30, int(minutes)))
        lbl = (self.alarm_ringing or {}).get("label") or "Báo thức"
        q = (self.alarm_ringing or {}).get("query") or ""
        self.alarm_ringing = None
        await self.stop()
        self._voice_pcm_buffer.clear()
        alarm = self.add_alarm_clock(due_ts=time.time() + mins * 60.0, label=f"{lbl} (Báo lại +{mins}p)", query=q)
        return {"ok": True, "alarm": alarm, "snapshot": self.get_status_snapshot()}

    async def _trigger_wake_alarm(self, label: str, kind: str, query: str = "", duration_min: int = 0) -> None:
        now = time.time()
        self.alarm_ringing = {
            "kind": kind,
            "label": label,
            "query": query,
            "started_at": now,
        }
        # Bật nhạc buổi sáng / nhẹ nhàng tăng âm lượng dần (fade-in 8s) nếu đang bật loa (volume > 0)
        if self.volume > 0:
            clean_q = str(query or "").strip()
            wake_track = find_cached_track(clean_q) if clean_q else (
                self._pick_cached_track_for_mode("morning") or self._pick_cached_track_for_mode("chill")
            )
            self.start_fade_in(duration_sec=8.0, start_gain=0.10)
            if wake_track:
                await self.play_track(wake_track)
            elif clean_q:
                self.spawn_play_or_search(clean_q, play_now=True)
        self.publish_update()
        self._emit_session_event({
            "kind": kind,
            "label": label,
            "duration_min": duration_min,
            "query": query,
            "triggered_at": now,
        })

    async def _trigger_scheduled_music(self, item: Dict[str, Any]) -> None:
        now = time.time()
        query = str(item.get("query") or "").strip()
        label = str(item.get("label") or "").strip()
        time_vn = str(item.get("time_vn") or "").strip()
        duration_min = max(0, min(360, _safe_int(item.get("duration_min"), 0) or 0))

        track: Optional[Dict[str, Any]] = None
        if query:
            track = find_cached_track(query)
        else:
            track = self._pick_cached_track_for_mode("chill") or self._pick_cached_track_for_mode("morning")
            if not track:
                query = random.choice(MOOD_SEARCH_POOLS["chill"])

        if self.volume > 0:
            if duration_min > 0:
                self.set_sleep_timer(duration_min)
            self.start_fade_in(duration_sec=5.0, start_gain=0.15)
            if track:
                await self.play_track(track)
            elif query:
                self.spawn_play_or_search(query, play_now=True)

        self.publish_update()
        self._emit_session_event({
            "kind": "scheduled_music",
            "label": label,
            "query": query,
            "track_title": (track or {}).get("title") or query or label,
            "time_vn": time_vn,
            "duration_min": duration_min,
            "triggered_at": now,
        })
        logger.info(
            f"🎵 [Music TRIGGER] {time_vn} | query='{query}' | cached={bool(track)} | dur={duration_min}p"
        )

    async def _finish_timed_session_after_fade(
        self,
        mode: str,
        mins: int,
        lbl: str,
        triggered_at: float,
        session_source_rev: Optional[int] = None,
    ) -> None:
        try:
            await asyncio.sleep(2.6)
            if self.status != "stopped" and (
                session_source_rev is None or self._source_revision == session_source_rev
            ):
                await self.stop()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"⏰ [TimedSession] Lỗi hoàn tất fade-out: {e}")
        self.publish_update()
        self._emit_session_event({
            "kind": f"{mode}_done",
            "mode": mode,
            "label": lbl,
            "duration_min": mins,
            "triggered_at": triggered_at,
        })

    async def _tick_timers(self) -> None:
        self._ensure_persisted_loaded()
        now = time.time()
        await self._check_sleep_timer()

        # Tự giải phóng tiến trình giải mã con nếu tạm dừng quá 3 phút để tiết kiệm RAM/USB handle
        if (
            self.status == "paused"
            and self._source is not None
            and self._paused_at > 0
            and (now - self._paused_at) >= 180.0
            and not self._voice_pcm_buffer
        ):
            try:
                await self._source.close()
            except Exception:
                pass
            self._source = None
            self._source_track_id = None

        # Tự tắt báo thức nếu đổ chuông quá 15 phút không ai tắt
        if self.alarm_ringing and (now - float(self.alarm_ringing.get("started_at") or now)) >= 900.0:
            await self.dismiss_alarm()

        sess = self.timed_session
        if sess and float(sess.get("ends_at") or 0) <= now:
            finished = dict(sess)
            self.timed_session = None
            self._save_timed_session()
            mode = str(finished.get("mode") or "focus")
            mins = _safe_int(finished.get("duration_min"), 15) or 15
            lbl = str(finished.get("label") or "")
            if mode == "nap":
                await self._trigger_wake_alarm(
                    label=lbl or f"Dậy thôi anh ơi (hết chợp mắt {mins}p)",
                    kind="nap_wake",
                    query=str(finished.get("query") or ""),
                    duration_min=mins,
                )
            else:
                if self.status == "playing" and (self.active_ws_clients > 0 or self.active_stream_clients > 0):
                    # Chạy fade-out 2.5s trên task nền để không block nhịp kiểm tra báo thức 1s/lần
                    rev_snapshot = self._source_revision
                    self.start_fade_out(duration_sec=2.5, on_complete="stop")
                    t = asyncio.create_task(
                        self._finish_timed_session_after_fade(mode, mins, lbl, now, session_source_rev=rev_snapshot)
                    )
                    self._bg_tasks.add(t)
                    t.add_done_callback(self._bg_tasks.discard)
                else:
                    if self.status in ("playing", "paused", "loading"):
                        await self.stop()
                    self.publish_update()
                    self._emit_session_event({
                        "kind": f"{mode}_done",
                        "mode": mode,
                        "label": lbl,
                        "duration_min": mins,
                        "triggered_at": now,
                    })

        due_alarms = [a for a in list(self.alarms) if float(a.get("due_at") or 0) <= now]
        if due_alarms:
            due_ids = {str(a.get("id")) for a in due_alarms}
            self.alarms = [a for a in self.alarms if str(a.get("id")) not in due_ids]
            self._save_alarms()
            due_wake = [a for a in due_alarms if str(a.get("kind") or "alarm") != "music"]
            due_music = [a for a in due_alarms if str(a.get("kind") or "alarm") == "music"]

            if due_wake:
                first = due_wake[0]
                labels: List[str] = []
                merged_query = str(first.get("query") or "")
                for a in due_wake:
                    lbl_item = str(a.get("label") or f"Báo thức {a.get('time_vn', '')}").strip()
                    if lbl_item and lbl_item not in labels:
                        labels.append(lbl_item)
                    if not merged_query and a.get("query"):
                        merged_query = str(a.get("query") or "")
                if len(due_wake) > 1:
                    logger.info(f"⏰ [Alarm MERGE] Gộp {len(due_wake)} báo thức đến hạn cùng lúc: {labels}")
                await self._trigger_wake_alarm(
                    label=" | ".join(labels) if labels else f"Báo thức {first.get('time_vn', '')}",
                    kind="alarm_wake",
                    query=merged_query,
                )

            if due_music:
                await self._trigger_scheduled_music(due_music[-1])

    @property
    def volume(self) -> int:
        self._ensure_persisted_loaded()
        return self._volume

    @volume.setter
    def volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")

    def is_voice_autoplay_enabled(self) -> bool:
        self._ensure_persisted_loaded()
        return bool(self._voice_autoplay)

    def set_voice_autoplay(self, enabled: bool) -> bool:
        self._ensure_persisted_loaded()
        self._voice_autoplay = bool(enabled)
        try:
            database.set_metadata(_META_VOICE_AUTOPLAY, "1" if self._voice_autoplay else "0")
            self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")
        except Exception:
            pass
        self.publish_update()
        return self._voice_autoplay

    def get_download_lock(self) -> asyncio.Lock:
        if self._download_lock is None:
            self._download_lock = asyncio.Lock()
        return self._download_lock

    def _signal_wake(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    def _has_active_audio_clients(self) -> bool:
        return (self.active_stream_clients + self.active_ws_clients) > 0

    async def _close_source_if_idle(self) -> None:
        if not self._has_active_audio_clients() and self._source is not None:
            await self._source.close()
            self._source = None
            self._source_track_id = None

    def register_ws_session(self, ctrl_queue: asyncio.Queue, client_ip: str = "") -> int:
        """Đăng ký kết nối WebSocket hai chiều mới từ ESP32 (thay thế phiên cũ nếu có)."""
        self._ws_gen += 1
        self._stream_gen += 1
        gen = self._ws_gen
        for old_gen, old_q in list(self._ws_control_queues.items()):
            if old_gen != gen:
                self._enqueue_ws_item(old_q, {"type": "__close__"})
        self._ws_control_queues[gen] = ctrl_queue
        self.active_ws_clients = len(self._ws_control_queues)
        if self.esp32_codec == "pcm":
            self.esp32_codec = "opus"
        self.mark_esp32_seen(ip=client_ip)
        self._enqueue_ws_item(ctrl_queue, {
            "type": "state",
            "status": self.status,
            "volume": self.volume,
        })
        self._signal_wake()
        self.publish_update()
        return gen

    def unregister_ws_session(self, gen: int) -> None:
        self._ws_control_queues.pop(gen, None)
        self.active_ws_clients = len(self._ws_control_queues)
        if self.active_ws_clients == 0 and self.active_stream_clients == 0:
            self.esp32_codec = "pcm"
        self._signal_wake()
        self.publish_update()

    def mark_ws_hello(self, payload: Dict[str, Any], client_ip: str = "") -> None:
        codec = str(payload.get("codec") or "opus").strip().lower() or "opus"
        self.esp32_codec = codec
        self._ws_last_hello = dict(payload)
        self.mark_esp32_seen(ip=client_ip, rssi=payload.get("rssi"))
        self.publish_update()

    @staticmethod
    def _enqueue_ws_item(q: asyncio.Queue, payload: Dict[str, Any]) -> None:
        try:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)
        except Exception:
            pass

    def push_ws_control(self, payload: Dict[str, Any]) -> None:
        """Đẩy thông điệp điều khiển JSON xuống ESP32 qua mọi phiên WebSocket đang mở."""
        if not self._ws_control_queues:
            return
        for q in list(self._ws_control_queues.values()):
            self._enqueue_ws_item(q, payload)

    def mark_esp32_seen(self, ip: str = "", rssi: Optional[int] = None) -> None:
        if ip:
            self.esp32_ip = ip
        if rssi is not None:
            try:
                self.esp32_rssi = int(rssi)
            except (TypeError, ValueError):
                pass
        self.esp32_last_seen = time.time()

    def is_esp32_online(self, now_ts: Optional[float] = None) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        if self._has_active_audio_clients():
            return True
        return self.esp32_last_seen > 0 and (now - self.esp32_last_seen) <= 90.0

    def is_ducking(self) -> bool:
        return bool(self._voice_pcm_buffer) or (time.monotonic() < self._voice_end_cooldown_until)

    def get_status_snapshot(self) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        now = time.time()
        sleep_remain = max(0, int(round(self.sleep_timer_until - now))) if self.sleep_timer_until > now else 0
        sess_out = None
        if self.timed_session and float(self.timed_session.get("ends_at") or 0) > now:
            sess_out = dict(self.timed_session)
            sess_out["remaining_sec"] = max(0, int(round(float(sess_out["ends_at"]) - now)))
        alarms_out = [
            {**a, "remaining_sec": max(0, int(round(float(a.get("due_at") or 0) - now)))}
            for a in self.alarms
            if float(a.get("due_at") or 0) > now - 30.0
        ]
        cache_info = list_cached_tracks()
        dht = database.get_esp32_sensor_data(max_age_sec=3600.0, now_ts=now)
        return {
            "status": self.status,
            "current_track": dict(self.current_track) if self.current_track else None,
            "queue": [{"video_id": q.get("video_id"), "title": q.get("title"), "uploader": q.get("uploader"), "duration": q.get("duration", 0)} for q in self.queue],
            "volume": self.volume,
            "auto_dj": self.auto_dj,
            "voice_autoplay": self.is_voice_autoplay_enabled(),
            "ducking_voice": self.is_ducking(),
            "loading_query": self.loading_query,
            "last_error": self.last_error,
            "last_download_notice": self.last_download_notice,
            "last_download_error": get_last_download_error(),
            "sleep_timer_remain_sec": sleep_remain,
            "eq": self.get_eq_settings(),
            "normalize": self.dsp.get_norm_status(),
            "timed_session": sess_out,
            "alarms": alarms_out,
            "alarm_ringing": dict(self.alarm_ringing) if self.alarm_ringing else None,
            "cookies": get_cookies_status(),
            "esp32": {
                "online": self.is_esp32_online(now),
                "ip": self.esp32_ip,
                "rssi": self.esp32_rssi,
                "last_seen": self.esp32_last_seen,
                "stream_clients": self.active_stream_clients,
                "ws_clients": self.active_ws_clients,
                "ws_connected": self.active_ws_clients > 0,
                "codec": self.esp32_codec,
            },
            "live_stream": {
                "active": bool(self.live_stream_active),
                "paused": bool(self.live_stream_paused),
                "source": self.live_stream_source,
                "label": self.live_stream_label,
                "client_ip": self.live_stream_client_ip,
                "started_at": self.live_stream_started_at,
                "silence_sec": (
                    max(0, int(round(now - self.live_stream_last_audible_at)))
                    if (self.live_stream_active and self.live_stream_last_audible_at > 0)
                    else 0
                ),
                "pc_bridge_configured": bool(_get_music_dl_endpoint()),
            },
            "dht11": dht,
            "cache": cache_info,
        }

    def publish_update(self) -> None:
        lan_events.publish("jukebox_update", self.get_status_snapshot())

    @staticmethod
    def _is_pcm_chunk_audible(pcm_bytes: bytes) -> bool:
        n = len(pcm_bytes) & ~1
        if n < 2:
            return False
        samples = array("h")
        samples.frombytes(pcm_bytes[:n])
        thr = LIVE_SILENCE_PEAK_THRESHOLD
        for s in samples:
            if s >= thr or s <= -thr:
                return True
        return False

    def _cancel_pc_bridge_pull_task(self) -> None:
        task = self._pc_bridge_pull_task
        self._pc_bridge_pull_task = None
        if task is not None and not task.done():
            try:
                cur = asyncio.current_task()
            except RuntimeError:
                cur = None
            if cur is not task:
                task.cancel()

    async def start_live_stream(self, source: str = "web_cast", label: str = "", client_ip: str = "") -> int:
        """Kích hoạt chế độ Loa Không Dây (Last-Sender Wins): tự dừng Jukebox cũ và chuyển ESP32 sang Low-Latency."""
        self._ensure_persisted_loaded()
        self._live_gen += 1
        my_gen = self._live_gen
        if source != "pc_loopback":
            self._cancel_pc_bridge_pull_task()

        self._source_revision += 1
        self._flush_revision += 1
        if self._source is not None:
            try:
                await self._source.close()
            except Exception:
                pass
            self._source = None
            self._source_track_id = None

        self._live_pcm_buffer.clear()
        self._reset_fade()
        self.dsp.reset()
        self.dsp.configure_norm(
            enabled=self._norm_enabled,
            track_gain=1.0,
            has_track_gain=False,
            reset_agc=True,
        )

        now = time.time()
        clean_src = str(source or "web_cast").strip() or "web_cast"
        clean_lbl = str(label or "").strip()[:100] or "📡 Loa Không Dây WebSocket"
        self.live_stream_active = True
        self.live_stream_paused = False
        self.live_stream_source = clean_src
        self.live_stream_label = clean_lbl
        self.live_stream_client_ip = str(client_ip or "").strip()
        self.live_stream_started_at = now
        self.live_stream_last_frame_at = now
        self.live_stream_last_audible_at = now
        self._paused_at = 0.0
        self.loading_query = ""
        self.last_error = ""
        self.status = "playing"
        self.current_track = {
            "video_id": "__live_ws__",
            "source": clean_src,
            "title": clean_lbl,
            "uploader": f"Live WebSocket ({self.live_stream_client_ip or 'LAN'})",
            "duration": 0,
            "live": True,
            "started_at": now,
        }

        self.push_ws_control({"type": "latency_mode", "low_latency": True})
        self.push_ws_control({"type": "audio_reset", "reason": "live_start", "video_id": "__live_ws__"})
        self.push_ws_control({
            "type": "state",
            "status": self.status,
            "volume": self.volume,
            "video_id": "__live_ws__",
        })
        self._signal_wake()
        self.publish_update()
        logger.info(f"📡 [Live Speaker] Bắt đầu '{clean_lbl}' (source={clean_src}, ip={self.live_stream_client_ip or '—'})")
        return my_gen

    def push_live_pcm(self, pcm_bytes: bytes, live_gen: Optional[int] = None) -> bool:
        """Nhận khối PCM 24kHz s16le mono từ WebSocket nguồn Live; trả False nếu phiên đã bị chiếm/tắt."""
        if not self.live_stream_active:
            return False
        if live_gen is not None and live_gen != self._live_gen:
            return False
        if not pcm_bytes:
            return True

        now = time.time()
        self.live_stream_last_frame_at = now
        if self._is_pcm_chunk_audible(pcm_bytes):
            self.live_stream_last_audible_at = now
        if self.live_stream_paused:
            return True

        even_len = len(pcm_bytes) & ~1
        if even_len <= 0:
            return True
        self._live_pcm_buffer.extend(pcm_bytes[:even_len])
        if len(self._live_pcm_buffer) > LIVE_BUFFER_MAX_BYTES:
            drop = (len(self._live_pcm_buffer) - LIVE_BUFFER_TRIM_TO_BYTES) & ~1
            if drop > 0:
                del self._live_pcm_buffer[:drop]
        self._signal_wake()
        return True

    async def stop_live_stream(self, live_gen: Optional[int] = None, reason: str = "stop") -> Dict[str, Any]:
        """Dừng chế độ Loa Không Dây (nếu `live_gen` khớp) và trả ESP32 về buffer tiêu chuẩn."""
        if live_gen is not None and live_gen != self._live_gen:
            return self.get_status_snapshot()

        self._live_gen += 1
        self._cancel_pc_bridge_pull_task()
        was_active = self.live_stream_active
        self.live_stream_active = False
        self.live_stream_paused = False
        self.live_stream_source = ""
        self.live_stream_label = ""
        self.live_stream_client_ip = ""
        self.live_stream_started_at = 0.0
        self.live_stream_last_frame_at = 0.0
        self.live_stream_last_audible_at = 0.0
        self._live_pcm_buffer.clear()

        if self.current_track and self.current_track.get("video_id") == "__live_ws__":
            self.current_track = None
            self.status = "stopped"
            self._paused_at = 0.0

        if was_active:
            self._flush_revision += 1
            self.dsp.reset()
            self.push_ws_control({"type": "latency_mode", "low_latency": False})
            self.push_ws_control({"type": "audio_reset", "reason": f"live_{reason}"})
            self.push_ws_control({"type": "state", "status": self.status, "volume": self.volume})
            self._signal_wake()
            self.publish_update()
            logger.info(f"📡 [Live Speaker] Đã dừng ({reason}).")
        return self.get_status_snapshot()

    async def _run_pc_bridge_pull_loop(self, session: aiohttp.ClientSession, ws: Any, my_gen: int) -> None:
        stop_reason = "pc_bridge_closed"
        try:
            async for msg in ws:
                if my_gen != self._live_gen or not self.live_stream_active:
                    stop_reason = "preempted"
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if not self.push_live_pcm(msg.data, live_gen=my_gen):
                        stop_reason = "preempted"
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        except Exception as e:
            logger.debug(f"PC Bridge Live WS loop kết thúc: {type(e).__name__}: {e}")
        finally:
            for obj in (ws, session):
                try:
                    await obj.close()
                except Exception:
                    pass
            if my_gen == self._live_gen and self.live_stream_active:
                await self.stop_live_stream(live_gen=my_gen, reason=stop_reason)

    async def start_pc_bridge_live_stream(self, preferred_ip: str = "") -> Dict[str, Any]:
        """Kết nối WebSocket tới PC Bridge (:8091/live_audio/ws), ưu tiên đúng IP PC đang thao tác."""
        candidates: List[str] = []
        clean_ip = str(preferred_ip or "").strip()
        if clean_ip:
            try:
                addr = ipaddress.ip_address(clean_ip)
                if addr.is_private and not addr.is_loopback:
                    candidates.append(f"http://{clean_ip}:8091")
            except ValueError:
                pass

        default_ep = _get_music_dl_endpoint()
        if default_ep and default_ep not in candidates:
            candidates.append(default_ep)
        if not candidates:
            raise ValueError("Chưa cấu hình hoặc chưa thấy PC Bridge (:8091) đang bật trên mạng LAN.")

        self._cancel_pc_bridge_pull_task()
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=2.0))
        ws = None
        chosen_base = ""
        last_err: Optional[Exception] = None
        for base in candidates:
            if base.startswith("https://"):
                ws_base = "wss://" + base[len("https://"):]
            elif base.startswith("http://"):
                ws_base = "ws://" + base[len("http://"):]
            else:
                ws_base = "ws://" + base
            ws_url = f"{ws_base}/live_audio/ws"
            try:
                ws = await session.ws_connect(ws_url, headers=_get_music_dl_headers(), heartbeat=15.0)
                chosen_base = base
                break
            except Exception as e:
                last_err = e

        if ws is None:
            await session.close()
            raise RuntimeError(
                f"Không kết nối được Live Audio WS trên {', '.join(candidates)}: "
                f"{type(last_err).__name__}: {last_err}"
            ) from last_err

        host_label = urlparse(chosen_base).hostname or "PC"
        my_gen = await self.start_live_stream(
            source="pc_loopback",
            label=f"💻 Âm thanh Windows ({host_label})",
            client_ip=host_label,
        )
        task = asyncio.create_task(self._run_pc_bridge_pull_loop(session, ws, my_gen))
        self._pc_bridge_pull_task = task
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return self.get_status_snapshot()

    async def play_track(self, track: Dict[str, Any], reset_audio: bool = True) -> Dict[str, Any]:
        self._ensure_persisted_loaded()
        if self.live_stream_active:
            await self.stop_live_stream(reason="track_change")
        self._source_revision += 1
        if reset_audio:
            self._flush_revision += 1
            self.dsp.reset()
        vid = str(track.get("video_id") or "")
        rms_db = track.get("norm_rms_db")
        peak_db = track.get("norm_peak_db")
        if rms_db is None and vid:
            try:
                idx_ent = (load_cache_index().get("tracks") or {}).get(vid) or {}
                rms_db = idx_ent.get("norm_rms_db")
                if peak_db is None:
                    peak_db = idx_ent.get("norm_peak_db")
            except Exception:
                pass
        t_gain, has_tg = esp32_dsp.compute_track_norm_gain(rms_db, peak_db)
        self.dsp.configure_norm(
            enabled=self._norm_enabled,
            track_gain=t_gain,
            has_track_gain=has_tg,
            reset_agc=True,
        )
        if vid:
            if vid in self._recent_track_ids:
                self._recent_track_ids.remove(vid)
            self._recent_track_ids.append(vid)
            self._recent_track_ids = self._recent_track_ids[-12:]
        self.current_track = {
            "video_id": vid,
            "source": track.get("source", "youtube"),
            "title": track.get("title", "Không rõ tên bài"),
            "uploader": track.get("uploader", ""),
            "duration": int(track.get("duration") or 0),
            "filepath": track.get("filepath", ""),
            "started_at": time.time(),
        }
        if rms_db is not None:
            self.current_track["norm_rms_db"] = rms_db
        if peak_db is not None:
            self.current_track["norm_peak_db"] = peak_db
        self.status = "playing"
        self.loading_query = ""
        self.last_error = ""
        _touch_cached_track(vid)
        if reset_audio:
            self.push_ws_control({
                "type": "audio_reset",
                "reason": "track_change",
                "video_id": self.current_track["video_id"],
            })
        self.push_ws_control({
            "type": "state",
            "status": self.status,
            "volume": self.volume,
            "video_id": self.current_track["video_id"],
        })
        self._signal_wake()
        self.publish_update()
        logger.info(f"🎵 [Jukebox] Phát bài: {self.current_track['title']}")
        return self.get_status_snapshot()

    async def enqueue_or_play(self, query_or_url: str, play_now: bool = True) -> Dict[str, Any]:
        clean = str(query_or_url or "").strip()
        if not clean:
            return {"ok": False, "error": "Thiếu tên bài hát hoặc link nhạc."}

        track = find_cached_track(clean)
        from_cache = track is not None
        fallback_note = ""
        err_detail = ""
        if track is None:
            start_rev = self._source_revision
            self._active_downloads += 1
            try:
                if play_now and self.status == "stopped":
                    self.status = "loading"
                self.loading_query = clean[:120]
                self.last_error = ""
                self.last_download_notice = ""
                self.publish_update()
                track, err_detail, fallback_note = await search_and_cache_track(clean)
            finally:
                self._active_downloads = max(0, self._active_downloads - 1)
                if self._active_downloads == 0:
                    self.loading_query = ""
        else:
            start_rev = self._source_revision

        if track is None:
            self.last_error = err_detail or f"Không tìm hoặc tải được '{clean[:60]}'"
            if self.status == "loading" and self._active_downloads == 0:
                self.status = "stopped"
            self.publish_update()
            return {"ok": False, "error": self.last_error}

        self.last_download_notice = fallback_note
        if play_now and self._source_revision != start_rev:
            self.publish_update()
            return {"ok": True, "action": "cached_only", "track": track, "from_cache": from_cache, "notice": fallback_note}
        if not play_now and self.status in ("playing", "paused") and self.current_track:
            self.queue.append(track)
            self.publish_update()
            return {"ok": True, "action": "queued", "track": track, "from_cache": from_cache, "notice": fallback_note}

        self._reset_fade()
        await self.play_track(track)
        return {"ok": True, "action": "playing", "track": track, "from_cache": from_cache, "notice": fallback_note}

    def spawn_play_or_search(self, query_or_url: str, play_now: bool = True) -> asyncio.Task:
        task = asyncio.create_task(self.enqueue_or_play(query_or_url, play_now=play_now))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def precache_track(self, query_or_url: str) -> Optional[Dict[str, Any]]:
        """Tìm & tải trước bài hát vào cache mà không làm gián đoạn nhạc đang phát."""
        clean = str(query_or_url or "").strip()
        if not clean:
            return None
        cached = find_cached_track(clean)
        if cached:
            return cached
        try:
            track, err_detail, _ = await search_and_cache_track(clean)
            if track:
                logger.info(f"💿 [Music PRECACHE] Đã tải sẵn '{track.get('title')}' cho lịch hẹn phát.")
                self.publish_update()
            elif err_detail:
                logger.debug(f"Music precache '{clean}' chưa tải được: {err_detail}")
            return track
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Music precache '{clean}' bỏ qua: {e}")
            return None

    def spawn_precache_track(self, query_or_url: str) -> Optional[asyncio.Task]:
        clean = str(query_or_url or "").strip()
        if not clean or find_cached_track(clean) is not None:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = asyncio.create_task(self.precache_track(clean))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def pause(self) -> Dict[str, Any]:
        if self.live_stream_active:
            if not self.live_stream_paused:
                self.live_stream_paused = True
                self._live_pcm_buffer.clear()
                self._flush_revision += 1
                self.status = "paused"
                self._paused_at = time.time()
                self.push_ws_control({"type": "audio_reset", "reason": "pause"})
                self.push_ws_control({"type": "state", "status": self.status, "volume": self.volume})
                self._signal_wake()
                self.publish_update()
            return self.get_status_snapshot()
        if self.status == "playing":
            self._flush_revision += 1
            self.status = "paused"
            self._paused_at = time.time()
            self.push_ws_control({"type": "audio_reset", "reason": "pause"})
            self.push_ws_control({"type": "state", "status": self.status, "volume": self.volume})
            self._signal_wake()
            self.publish_update()
        return self.get_status_snapshot()

    async def resume(self) -> Dict[str, Any]:
        if self.live_stream_active:
            if self.live_stream_paused or self.status != "playing":
                self._reset_fade()
                self._paused_at = 0.0
                self.live_stream_paused = False
                now = time.time()
                self.live_stream_last_frame_at = now
                self.live_stream_last_audible_at = now
                self.status = "playing"
                self.push_ws_control({
                    "type": "state",
                    "status": self.status,
                    "volume": self.volume,
                    "video_id": "__live_ws__",
                })
                self._signal_wake()
                self.publish_update()
            return self.get_status_snapshot()
        if self.status == "paused" and self.current_track:
            self._reset_fade()
            self._paused_at = 0.0
            self.status = "playing"
            self.push_ws_control({
                "type": "state",
                "status": self.status,
                "volume": self.volume,
                "video_id": self.current_track.get("video_id", ""),
            })
            self._signal_wake()
            self.publish_update()
            return self.get_status_snapshot()
        if self.status == "stopped":
            self._reset_fade()
            self._paused_at = 0.0
            return await self._start_from_cache_or_queue()
        return self.get_status_snapshot()

    async def toggle_play_pause(self) -> Dict[str, Any]:
        if self.status == "playing":
            return await self.pause()
        return await self.resume()

    async def stop(self) -> Dict[str, Any]:
        if self.live_stream_active:
            return await self.stop_live_stream(reason="stop")
        self._source_revision += 1
        self._flush_revision += 1
        self.status = "stopped"
        self._paused_at = 0.0
        self.current_track = None
        self.loading_query = ""
        self.sleep_timer_until = 0.0
        self._reset_fade()
        self.dsp.reset()
        if self._source is not None:
            await self._source.close()
            self._source = None
            self._source_track_id = None
        self.push_ws_control({"type": "audio_reset", "reason": "stop"})
        self.push_ws_control({"type": "state", "status": self.status, "volume": self.volume})
        self._signal_wake()
        self.publish_update()
        return self.get_status_snapshot()

    async def _start_from_cache_or_queue(self, exclude_vid: Optional[str] = None, reset_audio: bool = True) -> Dict[str, Any]:
        if self.queue:
            nxt = self.queue.pop(0)
            return await self.play_track(nxt, reset_audio=reset_audio)

        sess = self.timed_session
        if sess and float(sess.get("ends_at") or 0) > time.time():
            mode = str(sess.get("mode") or "focus")
            ex = list(self._recent_track_ids[-4:])
            if exclude_vid and exclude_vid not in ex:
                ex.append(exclude_vid)
            mood_pick = self._pick_cached_track_for_mode(mode, exclude_ids=ex)
            if mood_pick:
                return await self.play_track(mood_pick, reset_audio=reset_audio)

        cache_info = list_cached_tracks()
        tracks = cache_info.get("tracks") or []
        if not tracks:
            self.status = "stopped"
            self.current_track = None
            self.publish_update()
            return self.get_status_snapshot()

        recent_ex = set(self._recent_track_ids[-3:])
        if exclude_vid:
            recent_ex.add(exclude_vid)
        pool = [t for t in tracks if t.get("video_id") not in recent_ex]
        if not pool and exclude_vid and len(tracks) > 1:
            pool = [t for t in tracks if t.get("video_id") != exclude_vid]
        if not pool:
            pool = tracks
        chosen = random.choice(pool)
        return await self.play_track(_track_with_path(chosen), reset_audio=reset_audio)

    async def skip(self) -> Dict[str, Any]:
        cur_vid = self.current_track.get("video_id") if self.current_track else None
        if self.queue:
            nxt = self.queue.pop(0)
            return await self.play_track(nxt)
        if self.auto_dj or (self.timed_session and float(self.timed_session.get("ends_at") or 0) > time.time()):
            return await self._start_from_cache_or_queue(exclude_vid=cur_vid)
        return await self.stop()

    def set_volume(self, value: int) -> int:
        self._ensure_persisted_loaded()
        self._volume = max(0, min(100, int(value)))
        try:
            database.set_metadata(_META_JUKEBOX_VOLUME, str(self._volume))
            self._persisted_db_path = str(getattr(config, "DB_PATH", "") or "")
        except Exception:
            pass
        self.push_ws_control({"type": "state", "status": self.status, "volume": self._volume})
        self.publish_update()
        return self._volume

    def adjust_volume(self, delta: int) -> int:
        return self.set_volume(self.volume + int(delta))

    def set_auto_dj(self, enabled: bool) -> bool:
        self.auto_dj = bool(enabled)
        self.publish_update()
        return self.auto_dj

    def set_sleep_timer(self, minutes: int) -> float:
        self.ensure_background_loops()
        mins = max(0, min(360, int(minutes)))
        self.sleep_timer_until = (time.time() + mins * 60.0) if mins > 0 else 0.0
        self.publish_update()
        return self.sleep_timer_until

    async def delete_from_cache(self, video_id: str) -> bool:
        vid = str(video_id or "").strip()
        if not vid:
            return False
        if self.current_track and self.current_track.get("video_id") == vid:
            await self.stop()
        self.queue = [q for q in self.queue if q.get("video_id") != vid]
        idx = load_cache_index()
        tracks = idx.get("tracks") or {}
        entry = tracks.pop(vid, None)
        if not entry:
            return False
        fpath = os.path.join(get_cache_dir(), os.path.basename(str(entry.get("filename") or "")))
        if os.path.isfile(fpath):
            try:
                os.remove(fpath)
            except Exception as e:
                logger.warning(f"Không xóa được file cache {fpath}: {e}")
        save_cache_index(idx)
        self.publish_update()
        return True

    def _append_voice_pcm_bytes(self, pcm: bytes) -> None:
        if not pcm:
            return
        if len(self._voice_pcm_buffer) + len(pcm) > MAX_VOICE_BUFFER_BYTES:
            overflow = len(self._voice_pcm_buffer) + len(pcm) - MAX_VOICE_BUFFER_BYTES
            del self._voice_pcm_buffer[:overflow]
        self._voice_pcm_buffer.extend(pcm)
        self._signal_wake()
        self.publish_update()

    async def play_chime(self, kind: str = "focus_done") -> bool:
        """Phát tiếng chuông nhẹ tổng hợp bằng PCM trực tiếp lên loa ESP32 (hỗ trợ ducking nếu đang phát nhạc)."""
        pcm = esp32_dsp.generate_chime_pcm(kind=kind, sample_rate=SAMPLE_RATE)
        if not pcm:
            return False
        self._append_voice_pcm_bytes(pcm + (b"\x00" * (FRAME_BYTES * 6)))
        logger.info(f"🔔 [ESP32 Chime] Đã phát chuông '{kind}' lên loa")
        return True

    async def enqueue_voice_overlay(
        self,
        audio_bytes: bytes,
        audio_format: str = "ogg",
        prepend_chime: Optional[str] = None,
    ) -> bool:
        """Giải mã voice Smart Butler sang PCM 24kHz và đưa vào bộ trộn Audio Ducking cho ESP32."""
        if not audio_bytes:
            return False
        pcm = await decode_voice_bytes_to_pcm(audio_bytes, audio_format=audio_format)
        if not pcm:
            return False
        if prepend_chime:
            chime_pcm = esp32_dsp.generate_chime_pcm(kind=prepend_chime, sample_rate=SAMPLE_RATE)
            if chime_pcm:
                pcm = chime_pcm + (b"\x00" * (FRAME_BYTES * 8)) + pcm
        self._append_voice_pcm_bytes(pcm)
        logger.info(f"🔊 [ESP32 Ducking] Đã xếp hàng {len(pcm) / BYTES_PER_SEC:.1f}s giọng Smart Butler ra loa")
        return True

    async def handle_button_event(self, action: str, client_ip: str = "") -> Dict[str, Any]:
        if client_ip:
            self.mark_esp32_seen(ip=client_ip)
        act = str(action or "").strip().lower()

        if self.alarm_ringing:
            if act in ("single", "long"):
                snap = await self.dismiss_alarm()
                msg = "Đã tắt báo thức trên loa"
            elif act == "double":
                res = await self.snooze_alarm(5)
                snap = res["snapshot"]
                msg = "Báo thức ngủ nướng +5 phút"
            else:
                return {"ok": False, "error": f"Unknown button action: {act}"}
            lan_events.emit_notify(title="⏰ Nút chạm báo thức ESP32", body=msg, level="info", source="esp32", speak_tts=False)
            return {"ok": True, "action": act, "jukebox": snap}

        if act == "single":
            snap = await self.toggle_play_pause()
            msg = f"Play/Pause -> {snap['status']}"
        elif act == "double":
            snap = await self.skip()
            title = (snap.get("current_track") or {}).get("title") or "Hết nhạc"
            msg = f"Qua bài -> {title}"
        elif act == "long":
            snap = await self.stop()
            msg = "Đã dừng phát nhạc"
        else:
            return {"ok": False, "error": f"Unknown button action: {act}"}

        lan_events.emit_notify(
            title="🎛️ Nút chạm loa ESP32-C3",
            body=msg,
            level="info",
            source="esp32",
            speak_tts=False,
        )
        return {"ok": True, "action": act, "jukebox": snap}

    def update_dht11_telemetry(
        self,
        temp_c: Any,
        humidity_pct: Any,
        rssi: Any = None,
        client_ip: str = "",
    ) -> Dict[str, Any]:
        try:
            t_val = round(float(temp_c), 1)
            h_val = round(float(humidity_pct), 1)
        except (TypeError, ValueError):
            raise ValueError("Nhiệt độ hoặc độ ẩm không phải số hợp lệ.")
        if not (-10.0 <= t_val <= 65.0) or not (5.0 <= h_val <= 100.0):
            raise ValueError("Giá trị cảm biến DHT11 vượt ngoài ngưỡng vật lý.")
        rssi_val = None
        if rssi is not None:
            try:
                rssi_val = int(rssi)
            except (TypeError, ValueError):
                rssi_val = None
        self.mark_esp32_seen(ip=client_ip, rssi=rssi_val)
        saved = database.set_esp32_sensor_data(t_val, h_val, rssi=rssi_val)
        lan_events.publish("dht11_update", saved)
        return saved

    async def _ensure_active_source(self) -> Optional[_TrackPcmSource]:
        if self.status != "playing" or not self.current_track:
            return None
        fp = str(self.current_track.get("filepath") or "")
        rev_key = f"{self._source_revision}:{self.current_track.get('video_id')}:{fp}"
        if self._source is not None and self._source_track_id == rev_key:
            return self._source

        if self._source is not None:
            await self._source.close()
            self._source = None
            self._source_track_id = None

        src = _TrackPcmSource(fp)
        started = await src.start()
        if not started:
            await src.close()
            if fp and storage_helper.is_usb_path(fp):
                storage_helper.is_usb_healthy(force_refresh=True)
            await self.skip()
            return None
        self._source = src
        self._source_track_id = rev_key
        return src

    async def _check_sleep_timer(self) -> None:
        now_wall = time.time()
        if self.sleep_timer_until > 0 and now_wall >= self.sleep_timer_until and self.status in ("playing", "paused"):
            logger.info("🌙 [Jukebox] Hết hẹn giờ ngủ -> tự động tắt nhạc")
            self.sleep_timer_until = 0.0
            self.start_fade_out(2.5, on_complete="stop")

    async def _read_next_mixed_pcm_frame(self, consec_empty: int) -> Tuple[Optional[bytes], int]:
        """Đọc, lọc DSP 60Hz+Bass/Treble, áp fade mượt và trộn 1 khung PCM 20ms (960 bytes)."""
        self._ensure_persisted_loaded()
        for _ in range(3):
            music_frame: Optional[bytes] = None
            if self.live_stream_active:
                now = time.time()
                if (
                    self.live_stream_last_audible_at > 0
                    and (now - self.live_stream_last_audible_at) >= LIVE_AUDIO_SILENCE_TIMEOUT_SEC
                ):
                    await self.stop_live_stream(reason="silence_timeout")
                    break
                if (
                    not self.live_stream_paused
                    and self.live_stream_last_frame_at > 0
                    and (now - self.live_stream_last_frame_at) >= LIVE_AUDIO_IDLE_STALE_SEC
                ):
                    await self.stop_live_stream(reason="stream_idle_timeout")
                    break
                if not self.live_stream_paused and len(self._live_pcm_buffer) >= FRAME_BYTES:
                    raw_live = bytes(self._live_pcm_buffer[:FRAME_BYTES])
                    del self._live_pcm_buffer[:FRAME_BYTES]
                    music_frame = self.dsp.process_pcm16(raw_live)
                    music_frame = self._apply_fade_to_frame(music_frame)
                consec_empty = 0
            else:
                cur_playing = (self.status == "playing" and self.current_track is not None)
                if cur_playing:
                    src = await self._ensure_active_source()
                    if src is None:
                        consec_empty += 1
                        if consec_empty >= 2:
                            logger.warning("🎵 [Jukebox] Nguồn phát lỗi/không mở được liên tiếp -> tạm dừng để tránh lặp.")
                            if not self.last_error:
                                self.last_error = "Không thể mở hoặc giải mã nguồn phát nhạc."
                            if self.status != "stopped":
                                await self.stop()
                            break
                        if self.status == "playing" and self.current_track is not None:
                            continue
                    else:
                        music_frame = await src.read_frame()
                        if music_frame is None:
                            await src.close()
                            self._source = None
                            self._source_track_id = None
                            consec_empty += 1
                            if consec_empty >= 2:
                                logger.warning("🎵 [Jukebox] Nguồn phát kết thúc liên tiếp -> tạm dừng để tránh lặp.")
                                await self.stop()
                                break
                            sess_active = bool(self.timed_session and float(self.timed_session.get("ends_at") or 0) > time.time())
                            if self.queue:
                                nxt = self.queue.pop(0)
                                await self.play_track(nxt, reset_audio=False)
                            elif self.auto_dj or sess_active:
                                cur_vid = self.current_track.get("video_id") if self.current_track else None
                                await self._start_from_cache_or_queue(exclude_vid=cur_vid, reset_audio=False)
                            else:
                                await self.stop()
                            continue
                        consec_empty = 0
                        music_frame = _pad_pcm_frame(music_frame)
                        music_frame = self.dsp.process_pcm16(music_frame)
                        music_frame = self._apply_fade_to_frame(music_frame)

            voice_frame: Optional[bytes] = None
            if self._voice_pcm_buffer:
                take = min(FRAME_BYTES, len(self._voice_pcm_buffer))
                voice_frame = bytes(self._voice_pcm_buffer[:take])
                del self._voice_pcm_buffer[:take]
                if not self._voice_pcm_buffer:
                    # Giữ hiệu ứng ducking mượt thêm 250ms sau khi dứt câu thoại
                    self._voice_end_cooldown_until = time.monotonic() + 0.25
                    self.publish_update()

            if music_frame is None and voice_frame is None:
                return None, consec_empty

            eff_vol = max(self.volume, 12) if (self.alarm_ringing and self.volume > 0) else self.volume
            return mix_pcm_frame(music_frame, voice_frame, volume_pct=eff_vol), consec_empty
        return None, consec_empty

    async def stream_pcm_generator(self, client_ip: str = "") -> AsyncIterator[bytes]:
        """Sinh luồng PCM 24kHz s16le mono chuẩn thời gian thực 20ms/frame cho ESP32-C3."""
        self.ensure_background_loops()
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        self._stream_gen += 1
        my_gen = self._stream_gen
        self._signal_wake()
        self.active_stream_clients += 1
        self.mark_esp32_seen(ip=client_ip)
        self.publish_update()

        next_tick = time.monotonic() - PREFILL_LEAD_SEC
        last_heartbeat = time.monotonic()
        was_idle = True
        consec_empty = 0
        try:
            while my_gen == self._stream_gen:
                await self._check_sleep_timer()

                has_voice = bool(self._voice_pcm_buffer)
                is_playing = (self.status == "playing" and self.current_track is not None)

                if not is_playing and not has_voice:
                    was_idle = True
                    consec_empty = 0
                    now_m = time.monotonic()
                    if (now_m - last_heartbeat) >= 0.5:
                        last_heartbeat = now_m
                        self.mark_esp32_seen(ip=client_ip)
                        yield b"\x00\x00"
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=0.25)
                    except asyncio.TimeoutError:
                        pass
                    continue

                if was_idle:
                    next_tick = time.monotonic() - PREFILL_LEAD_SEC
                    was_idle = False

                batch = bytearray()
                frames_sent = 0
                for _ in range(STREAM_BATCH_FRAMES):
                    frame, consec_empty = await self._read_next_mixed_pcm_frame(consec_empty)
                    if frame is None:
                        break
                    batch.extend(frame)
                    frames_sent += 1

                if not batch:
                    await asyncio.sleep(0.02)
                    continue

                yield bytes(batch)
                last_heartbeat = time.monotonic()

                next_tick += (frames_sent * FRAME_MS) / 1000.0
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.45:
                    # Giữ lại phần bù prefill nhỏ để tránh ESP32 đói buffer sau nghẽn CPU thoáng qua
                    next_tick = time.monotonic() - (PREFILL_LEAD_SEC * 0.8)
        finally:
            self.active_stream_clients = max(0, self.active_stream_clients - 1)
            await self._close_source_if_idle()
            self.publish_update()

    async def stream_opus_frames(
        self,
        client_ip: str = "",
        ws_gen: Optional[int] = None,
    ) -> AsyncIterator[bytes]:
        """Sinh luồng gói Opus 60ms (~32kbps @ 24kHz mono) cho kết nối WebSocket hai chiều."""
        self.ensure_background_loops()
        if self._wake_event is None:
            self._wake_event = asyncio.Event()

        encoder = OpusStreamEncoder()
        next_tick = time.monotonic() - OPUS_PREFILL_LEAD_SEC
        last_seen_bump = time.monotonic()
        was_idle = True
        consec_empty = 0
        seen_flush_rev = self._flush_revision
        start_stream_gen = self._stream_gen

        def _session_alive() -> bool:
            return (ws_gen is None or ws_gen == self._ws_gen) and start_stream_gen == self._stream_gen

        try:
            while _session_alive():
                await self._check_sleep_timer()

                has_voice = bool(self._voice_pcm_buffer)
                is_playing = (self.status == "playing" and self.current_track is not None)

                if not is_playing and not has_voice:
                    was_idle = True
                    consec_empty = 0
                    now_m = time.monotonic()
                    if (now_m - last_seen_bump) >= 5.0:
                        last_seen_bump = now_m
                        self.mark_esp32_seen(ip=client_ip)
                    self._wake_event.clear()
                    # Kiểm tra lại sau clear để không lỡ tín hiệu wake vừa bật
                    if (self.status == "playing" and self.current_track is not None) or self._voice_pcm_buffer or not _session_alive():
                        continue
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    continue

                if was_idle:
                    lead = LIVE_OPUS_PREFILL_LEAD_SEC if self.live_stream_active else OPUS_PREFILL_LEAD_SEC
                    next_tick = time.monotonic() - lead
                    was_idle = False
                    encoder.reset()
                    seen_flush_rev = self._flush_revision

                pcm_acc = bytearray()
                start_flush_rev = self._flush_revision
                start_playing = is_playing
                live_wait_tries = 0
                while len(pcm_acc) < OPUS_FRAME_BYTES:
                    frame, consec_empty = await self._read_next_mixed_pcm_frame(consec_empty)
                    if frame is not None:
                        pcm_acc.extend(frame)
                        if self._flush_revision != start_flush_rev:
                            break
                        continue
                    if (
                        self.live_stream_active
                        and not self.live_stream_paused
                        and not self._voice_pcm_buffer
                        and _session_alive()
                        and self._flush_revision == start_flush_rev
                        and live_wait_tries < 4
                    ):
                        live_wait_tries += 1
                        self._wake_event.clear()
                        if len(self._live_pcm_buffer) >= FRAME_BYTES or self._voice_pcm_buffer or not self.live_stream_active or self.live_stream_paused or not _session_alive():
                            continue
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=0.025)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    break

                if not _session_alive():
                    break

                if self._flush_revision != start_flush_rev:
                    seen_flush_rev = self._flush_revision
                    encoder.reset()
                    was_idle = True
                    continue

                if not pcm_acc:
                    if self.live_stream_active and not self._voice_pcm_buffer:
                        self._wake_event.clear()
                        if len(self._live_pcm_buffer) < FRAME_BYTES and _session_alive() and self.live_stream_active:
                            try:
                                await asyncio.wait_for(self._wake_event.wait(), timeout=0.25)
                            except asyncio.TimeoutError:
                                pass
                        was_idle = True
                    else:
                        await asyncio.sleep(0.02)
                    continue

                # Nếu người dùng vừa pause/stop ngay giữa khung 60ms thì bỏ phần dư (ESP32 đã nhận audio_reset)
                if len(pcm_acc) < OPUS_FRAME_BYTES and start_playing and self.status != "playing" and not self._voice_pcm_buffer:
                    was_idle = True
                    continue

                if len(pcm_acc) < OPUS_FRAME_BYTES:
                    pcm_acc.extend(b"\x00" * (OPUS_FRAME_BYTES - len(pcm_acc)))

                if seen_flush_rev != self._flush_revision:
                    seen_flush_rev = self._flush_revision
                    encoder.reset()

                pkt = encoder.encode_frame(bytes(pcm_acc))
                yield pkt

                now_m = time.monotonic()
                if (now_m - last_seen_bump) >= 5.0:
                    last_seen_bump = now_m
                    self.mark_esp32_seen(ip=client_ip)

                lead_now = LIVE_OPUS_PREFILL_LEAD_SEC if self.live_stream_active else OPUS_PREFILL_LEAD_SEC
                next_tick += OPUS_FRAME_MS / 1000.0
                delay = next_tick - time.monotonic()
                if delay > 0.005:
                    self._wake_event.clear()
                    # Chỉ giữ wakeup nếu có thay đổi nguồn/trạng thái dừng
                    if not _session_alive() or self._flush_revision != seen_flush_rev or (self.status != "playing" and not self._voice_pcm_buffer):
                        was_idle = True
                        continue
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    if self._flush_revision != seen_flush_rev or (self.status != "playing" and not self._voice_pcm_buffer):
                        was_idle = True
                elif delay < -(lead_now + 0.25):
                    next_tick = time.monotonic() - (lead_now * 0.8)
        finally:
            encoder.close()
            await self._close_source_if_idle()


_MANAGER: Optional[ESP32AudioManager] = None


def get_manager() -> ESP32AudioManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ESP32AudioManager()
    return _MANAGER


def get_jukebox_status_for_prompt() -> str:
    """Định dạng ngắn gọn trạng thái loa phòng & phiên tập trung/báo thức cho ngữ cảnh AI (tuyệt đối giấu từ kỹ thuật)."""
    mgr = get_manager()
    snap = mgr.get_status_snapshot()
    parts: List[str] = []
    track = snap.get("current_track")
    if snap.get("status") == "playing" and track:
        artist = f" ({track['uploader']})" if track.get("uploader") else ""
        parts.append(f"Đang phát \"{track['title']}\"{artist}, âm lượng {snap.get('volume', DEFAULT_VOLUME)}%")
    elif snap.get("status") == "loading" and snap.get("loading_query"):
        parts.append(f"Đang tìm & tải nhạc \"{snap['loading_query']}\"")

    sess = snap.get("timed_session")
    if sess:
        rem_min = max(1, int(round(float(sess.get("remaining_sec") or 0) / 60.0)))
        parts.append(f"Đang chạy chế độ {sess.get('label') or sess.get('mode')} (còn khoảng {rem_min} phút)")

    alarms = snap.get("alarms") or []
    if alarms:
        parts.append(f"Đang đặt báo thức gọi dậy lúc {alarms[0].get('time_vn')}")

    if snap.get("last_error") and snap.get("status") == "stopped":
        parts.append(f"Lưu ý lỗi tải gần nhất: {snap['last_error']}")

    if not parts:
        return ""
    return (
        "[LOA PHÒNG: "
        + " | ".join(parts)
        + " — em có thể nhắc khéo tự nhiên đúng tên bài/phiên nếu hợp mạch chuyện, "
        + "TUYỆT ĐỐI KHÔNG nhắc từ ESP32/USB/OpenStick/IP/cookies/SoundCloud/server kỹ thuật.]"
    )


# --- Bộ phân tích Ý định Âm nhạc, Tập trung & Báo thức Tự nhiên từ Chat Telegram ---

_NON_MUSIC_NOUN_RE = re.compile(
    r"^(?:đèn|quạt|máy\s*lạnh|điều\s*hòa|cửa(?:\s*sổ)?|ti\s*vi|tv|tivi|máy\s*tính|pc|laptop|wifi|mạng|nước|bếp|loa)\b",
    re.IGNORECASE,
)
_PAST_OR_QUESTION_RE = re.compile(
    r"\b(?:hồi\s+(?:trước|xưa|nhỏ|nãy|sáng|trưa|chiều|tối|hôm)|hôm\s+qua|ngày\s+xưa|"
    r"em\s+(?:có\s+)?thích|sao\s+lại|có\s+hay\s+nghe|hay\s+nghe\s+nhạc\s+gì|"
    r"đã\s+(?:tập\s+trung\s+xong|mở|bật|nghe)|có\s+(?:mở|bật|nghe))\b",
    re.IGNORECASE,
)
_SLEEP_TIMER_RE = re.compile(
    r"(?:hẹn|sau|tầm|khoảng)?\s*(\d{1,3})\s*(?:phút|p\b|m\b)\s*(?:nữa\s*)?(?:tắt|dừng|ngưng)\s*nhạc"
    r"|(?:tắt|dừng)\s*nhạc\s*(?:sau|trong)\s*(\d{1,3})\s*(?:phút|p\b|m\b)",
    re.IGNORECASE,
)
_FOCUS_TIMER_RE = re.compile(
    r"(?:tập\s*trung|tap\s*trung|focus|pomodoro|học\s*bài|hoc\s*bai|làm\s*việc|lam\s*viec|code|ôn\s*bài)"
    r"[^\d]{0,36}?(\d{1,3})\s*(?:phút|phut|p\b|m\b)",
    re.IGNORECASE,
)
_CHILL_TIMER_RE = re.compile(
    r"(?:nghe\s*nhạc\s*)?(?:chill|thư\s*giãn|thu\s*gian|xả\s*hơi)"
    r"[^\d]{0,24}?(\d{1,3})\s*(?:phút|phut|p\b|m\b)",
    re.IGNORECASE,
)
_NAP_TIMER_RE = re.compile(
    r"(?:chợp\s*mắt|chop\s*mat|ngủ\s*trưa|ngu\s*trua|nghỉ\s*(?:mắt|ngơi|xíu)|ngủ\s*(?:xíu|chút|tý|tí)|báo\s*thức\s*sau)"
    r"[^\d]{0,28}?(\d{1,3})\s*(?:phút|phut|p\b|m\b)",
    re.IGNORECASE,
)
_PERIOD_TOKEN = r"(?:sáng|sang|trưa|trua|chiều|chieu|tối|toi|đêm|dem|khuya|am|pm)"
_DAY_TOKEN = r"(?:hôm\s*nay|tối\s*nay|sáng\s*nay|chiều\s*nay|trưa\s*nay|sáng\s*mai|tối\s*mai|chiều\s*mai|ngày\s*mai|mai|nay)"

_CLOCK_ALARM_RE = re.compile(
    rf"(?:(?:đặt\s*|hẹn\s*)?báo\s*thức|(?:nhớ\s*)?(?:kêu|gọi)\s*(?:anh|mình)\s*dậy\s*(?:lúc)?)"
    rf"\s*(?:lúc\s*)?(?P<h1>\d{{1,2}})\s*(?:giờ|gio|h|:)\s*(?P<m1>\d{{0,2}})\s*(?P<p1>{_PERIOD_TOKEN})?(?:\s*(?P<d1>{_DAY_TOKEN}))?"
    rf"|(?P<h2>\d{{1,2}})\s*(?:giờ|gio|h|:)\s*(?P<m2>\d{{0,2}})\s*(?P<p2>{_PERIOD_TOKEN})?\s*(?P<d2>{_DAY_TOKEN})?\s*(?:nhớ\s*)?(?:kêu|gọi)\s*(?:anh|mình)\s*dậy",
    re.IGNORECASE,
)

_SCHEDULE_RANGE_RE = re.compile(
    rf"\b(?:vào\s+)?từ\s+(?:lúc\s+)?(?:(?P<p1a>{_PERIOD_TOKEN})\s*)?(?:(?P<d1>{_DAY_TOKEN})\s*)?"
    rf"(?P<h1>\d{{1,2}})\s*(?:giờ|gio|h|:)\s*(?P<m1>\d{{0,2}})\s*(?P<p1b>{_PERIOD_TOKEN})?\s*"
    rf"(?:đến|tới|\-|~)\s*(?:lúc\s+)?(?P<h2>\d{{1,2}})\s*(?:giờ|gio|h|:)\s*(?P<m2>\d{{0,2}})\s*"
    rf"(?P<p2>{_PERIOD_TOKEN})?(?:\s*(?P<d2>{_DAY_TOKEN}))?",
    re.IGNORECASE,
)
_SCHEDULE_DELAY_HOURS_RE = re.compile(
    r"\b(?:(?:hẹn|vào|sau)\s+)?(\d{1,2})\s*(?:tiếng|tieng|giờ|gio)\s*(?:(rưỡi|ruoi)|(\d{1,2})\s*(?:phút|phut|p\b))?\s*nữa\b"
    r"|\bsau\s+(\d{1,2})\s*(?:tiếng|tieng|giờ|gio)\s*(?:(rưỡi|ruoi)|(\d{1,2})\s*(?:phút|phut|p\b))?(?!\s*(?:đến|tới))",
    re.IGNORECASE,
)
_SCHEDULE_DELAY_MINS_RE = re.compile(
    r"\b(?:(?:hẹn|vào|tầm|khoảng)\s+)?(\d{1,3})\s*(?:phút|phut|p\b|m\b)\s*nữa\b"
    r"|\bsau\s+(\d{1,3})\s*(?:phút|phut|p\b|m\b)\b",
    re.IGNORECASE,
)
_SCHEDULE_CLOCK_RE = re.compile(
    rf"(?P<prep>\b(?:vào\s*(?:lúc|hồi)?|hẹn\s*(?:lúc)?|lúc|hồi|tầm|khoảng|đúng)\s+)?"
    rf"(?:(?P<pa>{_PERIOD_TOKEN})\s*)?(?:(?P<da>{_DAY_TOKEN})\s*)?"
    rf"(?<!\d)(?P<hh>\d{{1,2}})\s*(?P<sep>giờ|gio|h|:)\s*(?P<mm>\d{{0,2}})\s*"
    rf"(?P<pb>{_PERIOD_TOKEN})?(?:\s*(?P<db>{_DAY_TOKEN}))?(?!\d)",
    re.IGNORECASE,
)
_SCHEDULE_DUR_MINS_RE = re.compile(
    r"\b(?:trong(?:\s+vòng)?|phát(?:\s+trong)?|nghe(?:\s+trong)?|kéo\s+dài|khoảng|tầm)\s+"
    r"(\d{1,3})\s*(?:phút|phut|p\b|m\b)",
    re.IGNORECASE,
)
_SCHEDULE_DUR_HOURS_RE = re.compile(
    r"\b(?:trong(?:\s+vòng)?|phát(?:\s+trong)?|nghe(?:\s+trong)?|kéo\s+dài)\s+"
    r"(\d{1,2})\s*(?:tiếng|tieng|giờ|gio|h\b)(?:\s*(rưỡi|ruoi)|(?:\s*(\d{1,2})\s*(?:phút|phut|p\b)))?",
    re.IGNORECASE,
)
_DELEGATE_MUSIC_CHOICE_RE = re.compile(
    r"\b(?:(?:em|đan|bé|vợ)\s*(?:tự\s*)?chọn|tự\s*chọn|chọn\s+(?:giúp|giùm|hộ|cho)|"
    r"tùy\s+(?:em|đan|bé)|gợi\s+ý|bài\s+(?:nào|gì)|nhạc\s+(?:nào|gì)|"
    r"ca\s*khúc\s+(?:nào|gì)|list\s+(?:nào|gì)|playlist\s+(?:nào|gì)|mở\s+gì\s+(?:hay|hợp|nghe))\b",
    re.IGNORECASE,
)
_VAGUE_QUERY_ONLY_RE = re.compile(
    r"^(?:gì(?:\s+đó)?|nào(?:\s+đó)?|hay\s+hay|vui\s+vui|buồn\s+buồn|nhẹ\s+nhàng|"
    r"một\s+bài|1\s+bài|bài\s+nào(?:\s+cũng\s+được)?|nhạc(?:\s+gì)?(?:\s+cũng\s+được)?|"
    r"hợp\s+(?:gu|mood)|nghe\s+(?:đỡ|cho\s+(?:vui|tỉnh|dễ\s*ngủ)))$",
    re.IGNORECASE,
)
_PLAY_VERB_RE = re.compile(r"\b(?:mở|bật|phát|chơi|nghe)\b", re.IGNORECASE)
_MUSIC_NOUN_TRIGGER_RE = re.compile(
    r"\b(?:bài(?:\s+hát)?|nhạc|ca\s+khúc|mv|track|playlist|list|link)\b",
    re.IGNORECASE,
)
_EQ_CMD_RE = re.compile(
    r"(?:tăng|thêm|nâng|giảm|bớt|hạ)\s*(?:xíu\s*|chút\s*|tí\s*)?(?:âm\s*)?(bass|trầm|treble|chép|tép|sáng)"
    r"|(?:chỉnh|đổi|bật|tắt)\s*(?:(?:chế\s*độ|sang|eq)\s*){0,2}(loudness|bass\s*ấm|focus\s*rõ|flat|eq)\b",
    re.IGNORECASE,
)
_NORM_CMD_RE = re.compile(
    r"\b(?:bật|mở|tắt)\s*(?:chế\s*độ\s*)?(?:chuẩn\s*hóa|cân\s*bằng)\s*(?:âm\s*lượng|âm\s*thanh|nhạc|to\s*nhỏ|loudness)\b",
    re.IGNORECASE,
)
_VOL_SET_RE = re.compile(
    r"(?:âm\s*lượng|volume|vol|loa|nhạc)\s*(?:lên|xuống|về|còn|ở|mức|=|:)?\s*(\d{1,3})\s*%?",
    re.IGNORECASE,
)
_VOL_DOWN_RE = re.compile(
    r"(?:vặn|chỉnh|mở|cho|hạ|giảm)\s*(?:nhạc|loa|âm\s*lượng|tiếng)?\s*(?:nhỏ|bé|êm|nhẹ|dịu|thấp)\s*(?:lại|xuống|xíu|tí|đi)?"
    r"|(?:nhỏ|bé)\s*(?:tiếng|nhạc|loa)\s*(?:lại|xuống|xíu)",
    re.IGNORECASE,
)
_VOL_UP_RE = re.compile(
    r"(?:vặn|chỉnh|mở|cho|tăng)\s*(?:nhạc|loa|âm\s*lượng|tiếng)?\s*(?:to|lớn|mạnh|rõ|cao)\s*(?:lên|ra|xíu|tí|đi)?"
    r"|(?:to|lớn)\s*(?:tiếng|nhạc|loa)\s*(?:lên|xíu)",
    re.IGNORECASE,
)
_STOP_MUSIC_RE = re.compile(
    r"(?:tắt|dừng|ngưng|stop|pause|tạm\s*dừng)\s*(?:giúp\s*anh\s*|hộ\s*anh\s*|giùm\s*)?(?:cái\s*)?(?:nhạc|bài(?:\s*này)?|loa|phiên\s*tập\s*trung)\b",
    re.IGNORECASE,
)
_SKIP_MUSIC_RE = re.compile(
    r"(?:qua|đổi|chuyển|next|skip|bỏ\s*qua)\s*(?:sang\s*)?(?:bài|nhạc)(?:\s*(?:khác|mới|tiếp(?:\s*theo)?))?\b",
    re.IGNORECASE,
)
_RESUME_OR_GENERIC_PLAY_RE = re.compile(
    r"^(?:(?:em|đan|bé|vợ)\s*(?:ơi)?\s*[,]?\s*)?(?:bật|mở|phát)\s*(?:lại\s*|tiếp\s*)?(?:nhạc|bài(?:\s*đó)?)(?:\s*(?:lên|lại|tiếp|đi|nha|nhé))?\s*[!?.~]*$",
    re.IGNORECASE,
)
_EXPLICIT_PLAY_RE = re.compile(
    r"^(?:(?:em|đan|bé|vợ)\s*(?:ơi)?\s*[,]?\s*)?"
    r"(?:(?:mở|bật|phát|chơi)\s+(?:giúp\s+(?:anh\s+)?|cho\s+(?:anh\s+)?|hộ\s+(?:anh\s+)?|giùm\s+)?(?:thử\s+)?"
    r"(?:bài(?:\s+hát)?|nhạc|ca\s+khúc|mv|track|link)|"
    r"(?:cho\s+(?:anh\s+)?nghe|muốn\s+nghe|nghe\s+(?:thử\s+)?)\s*(?:bài(?:\s+hát)?|nhạc|ca\s+khúc))"
    r"\s*[:\-]?\s*(.+)$",
    re.IGNORECASE,
)


def _resolve_hm_with_period(hh: int, mm: int, period: str = "") -> Optional[Tuple[int, int]]:
    """Chuẩn hóa (giờ, phút) theo sáng/trưa/chiều/tối/đêm/AM/PM."""
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    p = str(period or "").strip().lower()
    if p in ("chiều", "chieu", "tối", "toi", "pm"):
        if 1 <= hh <= 11:
            hh += 12
    elif p in ("đêm", "dem", "khuya"):
        if hh == 12:
            hh = 0
        elif 6 <= hh <= 11:
            hh += 12
    elif p in ("trưa", "trua"):
        if 1 <= hh <= 5:
            hh += 12
    elif p in ("sáng", "sang", "am"):
        if hh == 12:
            hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def parse_schedule_time_vn(time_str: str, now_ts: Optional[float] = None) -> Tuple[float, str]:
    """
    Phân tích mốc giờ VN ('08:00', '8h30', '8h sáng', '9h tối', '8h sáng mai', '2026-11-16 08:00')
    thành (unix_timestamp, 'HH:MM'). Tự đẩy sang ngày mai nếu đã qua hoặc có chữ 'mai'.
    """
    now = float(now_ts if now_ts is not None else time.time())
    s = str(time_str or "").strip()
    if not s:
        raise ValueError("Thiếu giờ hẹn.")

    m_iso = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2})[:h](\d{2})(?::(\d{2}))?$", s)
    if m_iso:
        try:
            d = datetime.date.fromisoformat(m_iso.group(1))
            hh = int(m_iso.group(2))
            mm = int(m_iso.group(3))
            ss = int(m_iso.group(4) or 0)
            target_dt = datetime.datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=VN_TZ)
            ts = target_dt.timestamp()
            if ts > now:
                return ts, f"{hh:02d}:{mm:02d}"
        except ValueError:
            pass

    tomorrow = bool(re.search(r"\b(?:ngày\s*)?mai\b", s, re.IGNORECASE))
    m = re.match(
        r"^(?:(?:vào\s*)?(?:lúc\s*)?)?"
        r"(?:(sáng|trưa|chiều|tối|đêm|khuya)\s*)?"
        r"(?:(?:ngày\s*)?(?:mai|hôm\s*nay|nay)\s*)?"
        r"(\d{1,2})\s*(?:giờ|gio|h|g|:)\s*(\d{0,2})\s*"
        r"(sáng|sang|trưa|trua|chiều|chieu|tối|toi|đêm|dem|khuya|am|pm)?"
        r"(?:\s*(?:hôm\s*nay|tối\s*nay|sáng\s*nay|chiều\s*nay|ngày\s*mai|mai))?$",
        s,
        re.IGNORECASE,
    )
    if not m:
        raise ValueError("Giờ báo thức / hẹn nhạc không hợp lệ (dùng dạng HH:MM hoặc 8h sáng).")

    period = m.group(4) or m.group(1) or ""
    hm = _resolve_hm_with_period(int(m.group(2)), int(m.group(3) or 0), period)
    if hm is None:
        raise ValueError("Giờ hẹn ngoài phạm vi 00:00-23:59.")
    hh, mm = hm
    now_vn = datetime.datetime.fromtimestamp(now, tz=VN_TZ)
    target_dt = now_vn.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tomorrow and target_dt.date() <= now_vn.date():
        target_dt += datetime.timedelta(days=1)
    if target_dt.timestamp() <= now + 15.0:
        target_dt += datetime.timedelta(days=1)
    return target_dt.timestamp(), f"{hh:02d}:{mm:02d}"


def _clean_song_query(q: str) -> str:
    s = str(q or "").strip()
    trailing_re = re.compile(
        r"(?:^|\s*,?\s+)(?:em\s+(?:ơi|nhé|nha)|nha|nhé|nhe|giúp\s+(?:anh|mình)|"
        r"cho\s+(?:anh|mình)|hộ\s+(?:anh|mình)|giùm\s+(?:anh|mình)|"
        r"với(?:\s+nha|\s+nhé)?|đi|lên|trên\s+loa|rồi(?:\s+tắt)?|cũng\s+được)\s*[!?.~]*$",
        re.IGNORECASE,
    )
    for _ in range(4):
        s2 = trailing_re.sub("", s).strip(" \"'“”.,!?:-")
        if s2 == s:
            break
        s = s2
    return s.strip(" \"'“”.,!?:-")


def _extract_topic_hint_for_session(raw: str) -> str:
    m_topic = re.search(
        r"(?:nhạc|ca\s*khúc|bài(?:\s+hát)?)\s+([A-Za-zÀ-ỹ0-9\s\-]{2,45}?)"
        r"(?=\s*(?:trong\s+\d+|để\s+|tập\s*trung|pomodoro|chợp\s*mắt|thư\s*giãn|\d+\s*(?:phút|phut|p\b|m\b)|$))",
        raw,
        re.IGNORECASE,
    )
    if not m_topic:
        return ""
    topic = _clean_song_query(m_topic.group(1))
    if topic.lower() in ("gì đó", "nào đó", "tập trung", "nhẹ"):
        return ""
    return topic


def _strip_text_span(s: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", f"{s[:start]} {s[end:]}").strip()


def _detect_scheduled_music_command(raw: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Nhận diện yêu cầu hẹn giờ / khoảng thời gian mở nhạc:
    - Trả về ('command', dict) khi người dùng nêu rõ tên bài/chủ đề + mốc giờ/delay/khoảng thời gian.
    - Trả về ('defer_to_ai', None) khi người dùng nhờ trợ lý tự chọn bài ở một mốc giờ/khoảng thời gian.
    - Trả về ('not_scheduled', None) nếu câu không phải hẹn giờ phát nhạc.
    """
    has_schedule = False
    time_str = ""
    tomorrow = False
    delay_minutes = 0
    duration_min = 0
    rem = raw

    m_rng = _SCHEDULE_RANGE_RE.search(raw)
    if m_rng:
        p2 = m_rng.group("p2") or ""
        p1 = m_rng.group("p1b") or m_rng.group("p1a") or ""
        h1, m1_i = int(m_rng.group("h1")), int(m_rng.group("m1") or 0)
        h2, m2_i = int(m_rng.group("h2")), int(m_rng.group("m2") or 0)
        end_hm = _resolve_hm_with_period(h2, m2_i, p2)
        if p1:
            start_hm = _resolve_hm_with_period(h1, m1_i, p1)
        elif p2 and end_hm is not None:
            cand_p2 = _resolve_hm_with_period(h1, m1_i, p2)
            cand_plain = _resolve_hm_with_period(h1, m1_i, "")
            start_hm = (
                cand_p2
                if cand_p2 is not None and (cand_p2[0] * 60 + cand_p2[1]) < (end_hm[0] * 60 + end_hm[1])
                else cand_plain
            )
        else:
            start_hm = _resolve_hm_with_period(h1, m1_i, "")
        if start_hm is not None and end_hm is not None:
            time_str = f"{start_hm[0]:02d}:{start_hm[1]:02d}"
            diff_m = (end_hm[0] * 60 + end_hm[1]) - (start_hm[0] * 60 + start_hm[1])
            if diff_m <= 0:
                diff_m += 24 * 60
            if 1 <= diff_m <= 360:
                duration_min = diff_m
            tomorrow = bool(re.search(r"mai", f"{m_rng.group('d1') or ''} {m_rng.group('d2') or ''}", re.IGNORECASE))
            rem = _strip_text_span(raw, m_rng.start(), m_rng.end())
            has_schedule = True

    if not has_schedule:
        m_dh = _SCHEDULE_DELAY_HOURS_RE.search(raw)
        if m_dh:
            hrs = int(m_dh.group(1) or m_dh.group(4) or 0)
            half = bool(m_dh.group(2) or m_dh.group(5))
            extra_m = int(m_dh.group(3) or m_dh.group(6) or 0)
            total_m = hrs * 60 + (30 if half else extra_m)
            if 1 <= total_m <= 1440:
                delay_minutes = total_m
                rem = _strip_text_span(raw, m_dh.start(), m_dh.end())
                has_schedule = True
        else:
            m_dm = _SCHEDULE_DELAY_MINS_RE.search(raw)
            if m_dm:
                mins_v = int(m_dm.group(1) or m_dm.group(2) or 0)
                if 1 <= mins_v <= 1440:
                    delay_minutes = mins_v
                    rem = _strip_text_span(raw, m_dm.start(), m_dm.end())
                    has_schedule = True

    if not has_schedule:
        m_verb = _PLAY_VERB_RE.search(raw)
        verb_pos = m_verb.start() if m_verb else -1
        candidates = []
        for mc in _SCHEDULE_CLOCK_RE.finditer(raw):
            p = mc.group("pb") or mc.group("pa") or ""
            hm = _resolve_hm_with_period(int(mc.group("hh")), int(mc.group("mm") or 0), p)
            if hm is None:
                continue
            has_prep = bool(mc.group("prep"))
            has_qual = bool(mc.group("pa") or mc.group("pb") or mc.group("da") or mc.group("db"))
            has_prec = (":" in mc.group("sep")) or ("giờ" in mc.group("sep").lower()) or bool(mc.group("mm"))
            before_verb = (verb_pos >= 0 and mc.start() < verb_pos)
            if has_prep or has_qual or has_prec or before_verb:
                candidates.append((1 if has_prep else 0, mc.start(), mc, hm))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            _, _, mc, hm = candidates[-1]
            time_str = f"{hm[0]:02d}:{hm[1]:02d}"
            tomorrow = bool(re.search(r"mai", f"{mc.group('da') or ''} {mc.group('db') or ''}", re.IGNORECASE))
            rem = _strip_text_span(raw, mc.start(), mc.end())
            has_schedule = True

    if not has_schedule:
        return "not_scheduled", None

    if duration_min == 0:
        m_dur_h = _SCHEDULE_DUR_HOURS_RE.search(rem)
        if m_dur_h:
            hrs = int(m_dur_h.group(1) or 0)
            half = bool(m_dur_h.group(2))
            extra_m = int(m_dur_h.group(3) or 0)
            dur_v = hrs * 60 + (30 if half else extra_m)
            if 1 <= dur_v <= 360:
                duration_min = dur_v
                rem = _strip_text_span(rem, m_dur_h.start(), m_dur_h.end())
        else:
            m_dur_m = _SCHEDULE_DUR_MINS_RE.search(rem)
            if m_dur_m:
                dur_v = int(m_dur_m.group(1) or 0)
                if 1 <= dur_v <= 360:
                    duration_min = dur_v
                    rem = _strip_text_span(rem, m_dur_m.start(), m_dur_m.end())

    music_url = extract_music_url(raw)
    has_music_cue = bool(
        music_url
        or _MUSIC_NOUN_TRIGGER_RE.search(rem)
        or re.search(r"\b(?:chill|lofi|acoustic|piano)\b", rem, re.IGNORECASE)
    )
    if not has_music_cue:
        return "not_scheduled", None
    if not (_PLAY_VERB_RE.search(rem) or _DELEGATE_MUSIC_CHOICE_RE.search(raw) or music_url):
        return "not_scheduled", None

    if _DELEGATE_MUSIC_CHOICE_RE.search(raw) and not music_url:
        return "defer_to_ai", None

    query = ""
    if music_url:
        query = music_url
    else:
        rem_norm = re.sub(r"\s+", " ", rem).strip(" ,.-")
        m_q = re.search(
            r"(?:mở|bật|phát|chơi|nghe)\s+"
            r"(?:(?:giúp|cho|hộ|giùm)\s+(?:anh\s+|mình\s+)?)?(?:thử\s+)?(?:lên\s+)?"
            r"(bài(?:\s+hát)?|nhạc|ca\s+khúc|mv|track|playlist|list)\b"
            r"(?:\s*[:\-]?\s*(.+))?$",
            rem_norm,
            re.IGNORECASE,
        )
        if not m_q:
            return "defer_to_ai", None
        noun = m_q.group(1).lower()
        q_raw = _clean_song_query(m_q.group(2) or "")
        if q_raw and _NON_MUSIC_NOUN_RE.match(q_raw):
            return "not_scheduled", None
        if not q_raw or _VAGUE_QUERY_ONLY_RE.match(q_raw):
            return "defer_to_ai", None
        query = f"nhạc {q_raw}" if noun == "nhạc" and not q_raw.lower().startswith("nhạc") else q_raw

    return "command", {
        "action": "schedule_music",
        "query": query,
        "time_str": time_str,
        "tomorrow": tomorrow,
        "delay_minutes": delay_minutes,
        "duration_min": duration_min,
    }


def detect_music_command(text: str) -> Optional[Dict[str, Any]]:
    """
    Nhận diện ý định điều khiển nhạc/loa, Tập trung/Pomodoro, Chợp mắt/Báo thức, Lên lịch phát nhạc và EQ.
    Trả về dict `{action, ...}` hoặc `None` nếu chỉ là tán gẫu / nhường AI Pass 1 tự chọn nhạc.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 280 or _PAST_OR_QUESTION_RE.search(raw):
        return None

    m_sleep = _SLEEP_TIMER_RE.search(raw)
    if m_sleep:
        mins = int(m_sleep.group(1) or m_sleep.group(2) or 0)
        if 1 <= mins <= 360:
            return {"action": "sleep_timer", "minutes": mins}

    m_nap = _NAP_TIMER_RE.search(raw)
    if m_nap:
        mins = int(m_nap.group(1) or 0)
        if 1 <= mins <= 240:
            want_music = bool(re.search(r"\b(?:nhạc|mưa|ru|rain|lofi)\b", raw, re.IGNORECASE))
            return {"action": "start_nap", "minutes": mins, "play_music": want_music}

    m_alm = _CLOCK_ALARM_RE.search(raw)
    if m_alm:
        hh_s = m_alm.group("h1") if m_alm.group("h1") is not None else m_alm.group("h2")
        mm_s = m_alm.group("m1") if m_alm.group("h1") is not None else m_alm.group("m2")
        period = (m_alm.group("p1") if m_alm.group("h1") is not None else m_alm.group("p2")) or ""
        day_tok = (m_alm.group("d1") or m_alm.group("d2") or "").strip()
        tomorrow = bool(
            re.search(r"\b(?:ngày\s*)?mai\b", day_tok, re.IGNORECASE)
            or re.search(r"\b(?:ngày\s*)?mai\b", raw, re.IGNORECASE)
        )
        hm = _resolve_hm_with_period(int(hh_s or 0), int(mm_s or 0), period)
        if hm is not None:
            res: Dict[str, Any] = {"action": "set_alarm", "time_str": f"{hm[0]:02d}:{hm[1]:02d}"}
            if tomorrow:
                res["tomorrow"] = True
            return res

    sched_status, sched_cmd = _detect_scheduled_music_command(raw)
    if sched_status == "command" and sched_cmd is not None:
        return sched_cmd
    if sched_status == "defer_to_ai":
        return None

    m_focus = _FOCUS_TIMER_RE.search(raw)
    if m_focus:
        mins = int(m_focus.group(1) or 0)
        if 1 <= mins <= 240:
            return {"action": "start_focus", "minutes": mins, "query": _extract_topic_hint_for_session(raw)}

    m_chill = _CHILL_TIMER_RE.search(raw)
    if m_chill:
        mins = int(m_chill.group(1) or 0)
        if 1 <= mins <= 240:
            return {"action": "start_chill", "minutes": mins, "query": _extract_topic_hint_for_session(raw)}

    if re.search(r"\b(?:tắt\s*báo\s*thức|dậy\s*rồi\s*(?:em|đan)?)\b", raw, re.IGNORECASE):
        mgr = get_manager()
        if mgr.alarm_ringing:
            return {"action": "dismiss_alarm"}

    if re.search(r"\b(?:ngủ\s*thêm\s*\d*\s*p|snooze)\b", raw, re.IGNORECASE):
        mgr = get_manager()
        if mgr.alarm_ringing:
            return {"action": "snooze_alarm", "minutes": 5}

    if _NORM_CMD_RE.search(raw):
        low_n = raw.lower()
        return {"action": "normalize_set", "enabled": not ("tắt" in low_n)}

    m_eq = _EQ_CMD_RE.search(raw)
    if m_eq:
        low = raw.lower()
        if "tắt eq" in low:
            return {"action": "eq_preset", "preset": "off"}
        if "loudness" in low:
            return {"action": "eq_preset", "preset": "loudness"}
        if "bass ấm" in low or "chỉnh eq warm" in low:
            return {"action": "eq_preset", "preset": "warm"}
        if "focus" in low or "giọng rõ" in low:
            return {"action": "eq_preset", "preset": "clear"}
        if "flat" in low:
            return {"action": "eq_preset", "preset": "flat"}
        band = (m_eq.group(1) or "").lower()
        up = bool(re.search(r"tăng|thêm|nâng", low))
        delta = 1.5 if up else -1.5
        if band in ("bass", "trầm"):
            return {"action": "eq_delta", "bass_delta": delta}
        if band in ("treble", "chép", "tép", "sáng"):
            return {"action": "eq_delta", "treble_delta": delta}

    m_vol = _VOL_SET_RE.search(raw)
    if m_vol:
        val = max(0, min(100, int(m_vol.group(1))))
        return {"action": "volume_set", "value": val}

    if _VOL_DOWN_RE.search(raw):
        return {"action": "volume_delta", "delta": -15}

    if _VOL_UP_RE.search(raw):
        return {"action": "volume_delta", "delta": 15}

    if _STOP_MUSIC_RE.search(raw):
        act = "pause" if re.search(r"tạm\s*dừng|pause", raw, re.IGNORECASE) else "stop"
        return {"action": act}

    if _SKIP_MUSIC_RE.search(raw):
        return {"action": "skip"}

    if _RESUME_OR_GENERIC_PLAY_RE.match(raw):
        return {"action": "resume"}

    music_url = extract_music_url(raw)
    m_play = _EXPLICIT_PLAY_RE.match(raw)
    if m_play:
        query = _clean_song_query(m_play.group(1))
        if query and not _NON_MUSIC_NOUN_RE.match(query) and len(query) >= 2:
            if query.lower() in ("lên", "đi", "ngay", "tiếp", "lại"):
                return {"action": "resume"}
            return {"action": "play", "query": music_url or query}

    # Nếu gửi thẳng link YouTube/SoundCloud trần hoặc kèm ý định nghe/bật/mở/loa
    if music_url and (
        raw.strip() == music_url
        or re.search(r"\b(?:nghe|bật|mở|phát|loa|nhạc|chill)\b", raw, re.IGNORECASE)
    ):
        return {"action": "play", "query": music_url}

    return None


def _safe_int(val: Any, default: Optional[int] = 0) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return int(round(val))
    s = str(val).strip()
    if not s:
        return default
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _format_alarm_time_with_tomorrow(time_str: str, tomorrow: bool) -> str:
    clean = str(time_str or "").strip()
    if tomorrow and clean and "mai" not in clean.lower():
        return f"{clean} mai"
    return clean


async def execute_music_command_dict(
    cmd: Dict[str, Any],
    from_ai_dj: bool = False,
    event_source: str = "user_telegram",
) -> str:
    """Thực thi cấu trúc lệnh nhạc/phiên tập trung/báo thức/EQ và trả về ghi chú trạng thái cho Smart Butler."""
    if not cmd or not isinstance(cmd, dict):
        return ""
    mgr = get_manager()
    act = str(cmd.get("action") or "").strip().lower()

    def _log_spk(ev_act: str, val: Optional[float] = None, txt: str = "") -> None:
        if event_source:
            import habit_engine
            habit_engine.notify_device_event(
                "speaker", "Loa phòng", ev_act, value_num=val, value_text=txt, source=event_source
            )

    if act == "play":
        query = str(cmd.get("query") or "").strip()
        if not query:
            return ""
        cached = find_cached_track(query)
        if (
            from_ai_dj
            and mgr.status == "playing"
            and mgr.current_track
            and (
                (cached and cached.get("video_id") == mgr.current_track.get("video_id"))
                or set(_folded_tokens(query)).issubset(set(_folded_tokens(mgr.current_track.get("title") or "")))
            )
        ):
            return (
                f"[HỆ THỐNG LOA PHÒNG: Bài \"{mgr.current_track.get('title') or query}\" đang phát sẵn trên loa rồi, "
                f"tiếp tục phát tự nhiên không cần bật lại.]"
            )
        if cached:
            await mgr.play_track(cached)
            _log_spk("play", val=float(mgr.volume), txt=cached.get("title") or query)
            return (
                f"[HỆ THỐNG LOA PHÒNG: Đã tìm thấy sẵn trong kho và BẮT ĐẦU PHÁT bài "
                f"\"{cached['title']}\" lên loa phòng. Hãy đáp ngọt ngào tự nhiên (tự nhiên đúng phong cách quản gia), "
                f"TUYỆT ĐỐI không nhắc từ hệ thống/cache/ESP32/server.]"
            )
        mgr.spawn_play_or_search(query, play_now=True)
        _log_spk("play", val=float(mgr.volume), txt=query)
        return (
            f"[HỆ THỐNG LOA PHÒNG: Đang tự động tìm và tải bài \"{query}\" để chuẩn bị phát ra loa phòng "
            f"(vài giây sẽ tự kêu lên). Hãy đáp tự nhiên kiểu 'đợi xíu em lên nhạc cho anh nha' "
            f"và KHÔNG bịa lý do mình không bật được nhạc.]"
        )

    if act in ("start_focus", "focus"):
        raw_mins = _safe_int(cmd.get("minutes") if cmd.get("minutes") is not None else cmd.get("duration_minutes"), 15)
        mins = max(1, min(240, raw_mins if raw_mins is not None else 15))
        query = str(cmd.get("query") or "").strip()
        res = await mgr.start_timed_session("focus", mins, query=query)
        tr = res.get("track")
        track_desc = f"đang phát ngay bài \"{tr['title']}\"" if tr else "đang chọn & tải bản nhạc lofi/không lời hợp để tập trung"
        return (
            f"[HỆ THỐNG LOA PHÒNG: Đã bật PHIÊN TẬP TRUNG {mins} PHÚT ({track_desc}) — "
            f"hệ thống sẽ giữ im lặng trên Telegram trong {mins} phút này và tự tắt nhạc + gọi nhắc anh khi hết giờ. "
            f"Hãy cổ vũ anh 1-2 câu thật gọn, dễ thương kiểu 'rồi em bật nhạc và bấm {mins} phút cho anh tập trung nè, cố lên xong việc em gọi nha'.]"
        )

    if act in ("start_chill", "chill"):
        raw_mins = _safe_int(cmd.get("minutes") if cmd.get("minutes") is not None else cmd.get("duration_minutes"), 20)
        mins = max(1, min(240, raw_mins if raw_mins is not None else 20))
        query = str(cmd.get("query") or "").strip()
        res = await mgr.start_timed_session("chill", mins, query=query)
        tr = res.get("track")
        track_desc = f"đang phát bài \"{tr['title']}\"" if tr else "đang tìm bản nhạc chill hợp gu"
        return (
            f"[HỆ THỐNG LOA PHÒNG: Đã bật chế độ Thư giãn / Chill {mins} phút ({track_desc}), hết giờ sẽ tự giảm nhỏ và tắt.]"
        )

    if act in ("start_nap", "nap"):
        raw_mins = _safe_int(cmd.get("minutes") if cmd.get("minutes") is not None else cmd.get("duration_minutes"), 20)
        mins = max(1, min(240, raw_mins if raw_mins is not None else 20))
        query = str(cmd.get("query") or "").strip()
        want_music = bool(cmd.get("play_music")) or bool(query)
        await mgr.start_timed_session("nap", mins, query=query, play_music=want_music)
        return (
            f"[HỆ THỐNG LOA PHÒNG: Đã hẹn CHỢP MẮT {mins} PHÚT cho chủ nhà (tạm yên lặng suốt {mins}p, "
            f"hết {mins}p loa sẽ tự tăng âm nhẹ nhàng + chuông & thông báo giọng nói gọi anh dậy). Chúc anh ngủ xíu cho khỏe nha.]"
        )

    if act in ("schedule_music", "schedule") or (
        act in ("set_alarm", "alarm") and str(cmd.get("kind") or "").strip().lower() == "music"
    ):
        query = str(cmd.get("query") or "").strip()
        time_str = str(cmd.get("time_str") or cmd.get("alarm_time_vn") or "").strip()
        delay_mins = max(0, min(1440, _safe_int(cmd.get("delay_minutes"), 0) or 0))
        dur_raw = cmd.get("duration_min")
        if dur_raw is None:
            dur_raw = cmd.get("duration_minutes") if cmd.get("duration_minutes") is not None else cmd.get("minutes")
        dur_mins = max(0, min(360, _safe_int(dur_raw, 0) or 0))
        time_str_resolved = _format_alarm_time_with_tomorrow(time_str, bool(cmd.get("tomorrow")))
        label = str(cmd.get("label") or "").strip()
        try:
            cached = find_cached_track(query) if query else None
            item = mgr.schedule_music(
                time_str=time_str_resolved,
                delay_minutes=delay_mins,
                query=query,
                label=label,
                duration_min=dur_mins,
            )
        except ValueError:
            return "[HỆ THỐNG LOA PHÒNG: Giờ hẹn phát nhạc chưa rõ (nhờ anh nói rõ dạng 8h sáng, 20:30 hoặc 30 phút nữa nha).]"

        song_desc = f"bài \"{(cached or {}).get('title') or query}\"" if query else "bài nhạc hợp gu"
        dur_desc = f" và phát trong {dur_mins} phút" if dur_mins > 0 else ""
        cache_state = "đã có sẵn trong kho" if cached else "đang tải sẵn trước ở chế độ nền"
        return (
            f"[HỆ THỐNG LOA PHÒNG: Đã lên lịch TỰ ĐỘNG PHÁT NHẠC vào lúc {item['time_vn']}{dur_desc} "
            f"với {song_desc} ({cache_state}, tới giờ sẽ tự mở nhẹ nhàng cho chủ nhà). "
            f"Hãy xác nhận ngọt ngào tự nhiên giờ hẹn & tên bài, TUYỆT ĐỐI không nhắc từ hệ thống/cache/ESP32/server.]"
        )

    if act in ("set_alarm", "alarm"):
        time_str = str(cmd.get("time_str") or cmd.get("alarm_time_vn") or "").strip()
        time_str_resolved = _format_alarm_time_with_tomorrow(time_str, bool(cmd.get("tomorrow")))
        query = str(cmd.get("query") or "").strip()
        try:
            alm = mgr.add_alarm_clock(time_str=time_str_resolved, label=f"Báo thức {time_str}", query=query)
            return f"[HỆ THỐNG LOA PHÒNG: Đã đặt BÁO THỨC GỌI DẬY nhẹ nhàng trên loa vào lúc {alm['time_vn']}.]"
        except ValueError:
            return "[HỆ THỐNG LOA PHÒNG: Giờ báo thức chưa rõ (nhờ chủ nhà nói rõ dạng 6h30 hoặc 07:15).]"

    if act == "dismiss_alarm":
        await mgr.dismiss_alarm()
        return "[HỆ THỐNG LOA PHÒNG: Đã tắt báo thức buổi sáng cho anh rồi đó.]"

    if act == "snooze_alarm":
        raw_mins = _safe_int(cmd.get("minutes"), 5)
        mins = max(1, min(30, raw_mins if raw_mins is not None else 5))
        res = await mgr.snooze_alarm(mins)
        return f"[HỆ THỐNG LOA PHÒNG: Đã cho anh ngủ nướng thêm {mins} phút (báo lại lúc {res['alarm']['time_vn']}).]"

    if act in ("normalize_set", "loudness_norm", "normalize"):
        raw_en = cmd.get("enabled")
        en = (
            bool(raw_en)
            if isinstance(raw_en, bool)
            else str(raw_en if raw_en is not None else "1").strip().lower() not in ("0", "false", "no", "off")
        )
        cfg = mgr.set_normalize_enabled(en)
        st_txt = "BẬT (tự cân bằng bài to/nhỏ đều nhau)" if cfg.get("normalize") else "TẮT"
        return f"[HỆ THỐNG LOA PHÒNG: Đã {st_txt} chế độ chuẩn hóa âm lượng nhạc.]"

    if act in ("eq_preset", "eq"):
        preset = str(cmd.get("preset") or cmd.get("eq_preset") or "loudness").strip().lower()
        cfg = mgr.set_eq_settings(preset=preset)
        return f"[HỆ THỐNG LOA PHÒNG: Đã chuyển EQ loa sang chế độ {cfg['label']} (HP {cfg['highpass_hz']}Hz, Bass {cfg['bass_db']:+.1f}dB, Treble {cfg['treble_db']:+.1f}dB).]"

    if act == "eq_delta":
        cur = mgr.get_eq_settings()
        new_bass = cur["bass_db"] + _safe_float(cmd.get("bass_delta"), 0.0)
        new_treble = cur["treble_db"] + _safe_float(cmd.get("treble_delta"), 0.0)
        cfg = mgr.set_eq_settings(bass_db=new_bass, treble_db=new_treble)
        return f"[HỆ THỐNG LOA PHÒNG: Đã chỉnh EQ loa thành Bass {cfg['bass_db']:+.1f}dB, Treble {cfg['treble_db']:+.1f}dB.]"

    if act in ("resume", "power_on", "on", "turn_on", "unpause"):
        query = str(cmd.get("query") or "").strip()
        if query:
            cached = find_cached_track(query)
            if cached:
                await mgr.play_track(cached)
                _log_spk("play", val=float(mgr.volume), txt=cached.get("title") or query)
                return f"[HỆ THỐNG LOA PHÒNG: Đã bật loa và phát bài \"{cached['title']}\".]"
            mgr.spawn_play_or_search(query, play_now=True)
            _log_spk("play", val=float(mgr.volume), txt=query)
            return f"[HỆ THỐNG LOA PHÒNG: Đang tìm và tải bài \"{query}\" để bật phát trên loa.]"
        snap = await mgr.resume()
        tr = snap.get("current_track")
        if tr:
            _log_spk("play", val=float(snap.get("volume", DEFAULT_VOLUME)), txt=tr.get("title") or "")
            return f"[HỆ THỐNG LOA PHÒNG: Đã bật phát tiếp bài \"{tr['title']}\" trên loa phòng anh (âm lượng {snap.get('volume', DEFAULT_VOLUME)}%).]"
        return (
            "[HỆ THỐNG LOA PHÒNG: Kho nhạc trong loa hiện đang trống nên chưa có bài cũ để mở lại — "
            "hãy hỏi chủ nhà muốn bật bài gì rồi gõ tên bài hoặc gửi link YouTube cho trợ lý nhé.]"
        )

    if act in ("skip", "next", "next_track"):
        snap = await mgr.skip()
        tr = snap.get("current_track")
        if tr:
            _log_spk("play", val=float(snap.get("volume", DEFAULT_VOLUME)), txt=tr.get("title") or "")
            return f"[HỆ THỐNG LOA PHÒNG: Đã chuyển sang bài \"{tr['title']}\".]"
        return "[HỆ THỐNG LOA PHÒNG: Không còn bài khác trong hàng đợi/kho nhạc nên đã tạm dừng.]"

    if act in ("toggle", "toggle_play_pause", "play_pause"):
        snap = await mgr.toggle_play_pause()
        tr = snap.get("current_track")
        if snap.get("status") == "playing" and tr:
            _log_spk("play", val=float(snap.get("volume", DEFAULT_VOLUME)), txt=tr.get("title") or "")
            return f"[HỆ THỐNG LOA PHÒNG: Đã bật phát lại bài \"{tr['title']}\".]"
        if snap.get("status") == "paused" and tr:
            _log_spk("pause", val=float(snap.get("volume", DEFAULT_VOLUME)), txt=tr.get("title") or "")
            return f"[HỆ THỐNG LOA PHÒNG: Đã tạm dừng bài \"{tr['title']}\".]"
        return "[HỆ THỐNG LOA PHÒNG: Kho nhạc trong loa đang trống nên chưa có bài để bật.]"

    if act == "pause":
        await mgr.pause()
        _log_spk("pause", val=float(mgr.volume))
        return "[HỆ THỐNG LOA PHÒNG: Đã tạm dừng phát nhạc trên loa phòng anh.]"

    if act in ("stop", "power_off", "off", "turn_off"):
        mgr.cancel_timed_session()
        await mgr.stop()
        _log_spk("stop")
        return "[HỆ THỐNG LOA PHÒNG: Đã tắt nhạc & dừng phiên hiện tại theo ý anh.]"

    if act in ("volume_set", "volume"):
        val = cmd.get("value") if cmd.get("value") is not None else cmd.get("volume")
        parsed_v = _safe_int(val, None)
        if parsed_v is None:
            return ""
        v = mgr.set_volume(parsed_v)
        _log_spk("volume", val=float(v))
        return f"[HỆ THỐNG LOA PHÒNG: Đã chỉnh âm lượng loa về mức {v}%.]"

    if act in ("volume_delta", "volume_up", "volume_down"):
        default_delta = 10 if act == "volume_up" else (-10 if act == "volume_down" else 0)
        raw_d = cmd.get("delta") if cmd.get("delta") is not None else cmd.get("value")
        delta_v = _safe_int(raw_d, default_delta)
        if delta_v is None:
            delta_v = default_delta
        if act == "volume_down" and delta_v > 0:
            delta_v = -delta_v
        elif act == "volume_up" and delta_v < 0:
            delta_v = -delta_v
        v = mgr.adjust_volume(delta_v)
        _log_spk("volume", val=float(v))
        return f"[HỆ THỐNG LOA PHÒNG: Đã điều chỉnh âm lượng loa thành {v}%.]"

    if act == "sleep_timer":
        raw_st = (
            cmd.get("minutes")
            if cmd.get("minutes") is not None
            else (
                cmd.get("duration_minutes")
                if cmd.get("duration_minutes") is not None
                else cmd.get("delay_minutes")
            )
        )
        mins = _safe_int(raw_st, 0) or 0
        if mins <= 0:
            return ""
        mgr.set_sleep_timer(mins)
        return f"[HỆ THỐNG LOA PHÒNG: Đã hẹn giờ {mins} phút nữa tự tắt nhạc cho anh ngủ ngon.]"

    return ""


async def execute_ai_music_action(ai_action: Any, *, event_source: str = "user_telegram") -> str:
    """Thực thi cấu trúc `music_action` do AI DJ Pass 1 đề xuất."""
    if not isinstance(ai_action, dict):
        return ""
    act = str(ai_action.get("action") or "").strip().lower()
    if not act or act == "null":
        return ""
    dur_val = ai_action.get("duration_minutes")
    if dur_val is None:
        dur_val = ai_action.get("minutes")
    normalized: Dict[str, Any] = {
        "action": act,
        "query": ai_action.get("query") or "",
        "minutes": dur_val,
        "duration_minutes": dur_val,
        "delay_minutes": ai_action.get("delay_minutes"),
        "time_str": ai_action.get("alarm_time_vn") or "",
        "tomorrow": bool(ai_action.get("tomorrow")),
        "play_music": bool(ai_action.get("play_music")),
        "kind": ai_action.get("kind") or "",
        "label": ai_action.get("label") or "",
        "preset": ai_action.get("preset") or ai_action.get("eq_preset") or "",
        "eq_preset": ai_action.get("eq_preset") or ai_action.get("preset") or "",
        "volume": ai_action.get("volume"),
        "delta": ai_action.get("delta"),
        "bass_delta": ai_action.get("bass_delta"),
        "treble_delta": ai_action.get("treble_delta"),
        "enabled": ai_action.get("enabled"),
    }
    return await execute_music_command_dict(normalized, from_ai_dj=True, event_source=event_source)


async def handle_natural_music_intent(text: str) -> str:
    """
    Phân tích và thực thi lệnh nhạc/loa tự nhiên trong lượt chat Telegram.
    Trả về chuỗi ghi chú ngữ cảnh để Smart Butler phản hồi khớp trạng thái thật của loa.
    """
    cmd = detect_music_command(text)
    if not cmd:
        return ""
    return await execute_music_command_dict(cmd)
