# -*- coding: utf-8 -*-
"""
Client nhẹ kết nối tới VieNeu-TTS Server (chạy trên PC LAN/Tailscale).
Nếu server đang tắt hoặc quá thời gian chờ kết nối, trả về None để bot gửi tin nhắn chữ bình thường.
"""
import time
import re
import logging
from typing import Optional, Tuple
import aiohttp
import config

logger = logging.getLogger(__name__)

_LOCAL_TTS_CACHE = {"url": "", "ok": False, "ts": 0.0}

VOICE_REQUEST_RE = re.compile(
    r"(gửi|gởi|cho|xin|nghe|thu|làm|nói|đọc|kể|bật)\s*(lại\s*|thêm\s*)?(cho\s*)?(anh\s*)?(1\s*|một\s*|cái\s*)?(voice|ghi âm|tin nhắn thoại|giọng nói|giọng em|thoại)"
    r"|\b(nói|đọc|hát|nghe)\s+(lại|thêm|nữa|cho\s*anh\s*nghe|anh\s*nghe)(\s+(đi|ik|nha|nhé|thử|xem|nữa|đi mà|với))*"
    r"|\b(voice|ghi âm|tin nhắn thoại)\b"
    r"|chưa\s*nghe\s*rõ",
    re.IGNORECASE,
)
_VOICE_FOLLOWUP_RE = re.compile(
    r"(chưa|hông|hong|không|đâu)\s*(thấy|nghe|có|nhận|được)|gửi lại|gởi lại|nói lại|nghe nữa|thêm\s*(lần|đoạn|câu|nữa)|\blag\b|\blỗi\b",
    re.IGNORECASE,
)
_RECENT_VOICE_MENTION_RE = re.compile(
    r"(\[Voice note\]|gửi voice|gởi voice|tin nhắn thoại|ghi âm|nghe cho kĩ|giọng em|nghe thêm|độc quyền)",
    re.IGNORECASE,
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Regex rút gọn phụ âm/nguyên âm kéo dài ("nhaaa" -> "nha", "ơiiii" -> "ơi", "áaaa" -> "á")
# mà vẫn bảo toàn từ tiếng Anh có 2 chữ cái giống nhau như "cool", "sleep", "good".
_REPEAT_3_RE = re.compile(r"([A-Za-zÀ-ỹ])\1{2,}", re.UNICODE)
_TONED_VOWEL_TAIL_RE = re.compile(
    r"([àáảãạằắẳẵặầấẩẫậ])a{1,}|([èéẻẽẹềếểễệ])e{1,}|([ìíỉĩị])i{1,}"
    r"|([òóỏõọồốổỗộờớởỡợ])o{1,}|([ùúủũụừứửữự])u{1,}|([ỳýỷỹỵ])y{1,}",
    re.IGNORECASE,
)
_TRAILING_PARTICLE_REPEAT_RE = re.compile(r"\b(nh|d|n|m|v)aa+\b|\bơi+\b", re.IGNORECASE)
# Giữ lại các thẻ biểu cảm Voice v2.2 (3 thẻ VieNeu gốc + 5 thẻ diễn cảm mở rộng)
_CUE_INNER_PATTERN = (
    r"(?:cười\s*khúc\s*khích|cuoi\s*khuc\s*khich|cười\s*nhẹ|cuoi\s*nhe|giggle"
    r"|cười|cuoi|chuckle"
    r"|thở\s*dài|tho\s*dai|sigh"
    r"|hắng\s*giọng|hang\s*giong|clear\s*throat"
    r"|hứ|hu|hừ|pout"
    r"|ngập\s*ngừng|ngap\s*ngung|hesitate|chần\s*chừ"
    r"|ngạc\s*nhiên|ngac\s*nhien|bất\s*ngờ|surprised"
    r"|thì\s*thầm|thi\s*tham|nói\s*nhỏ|whisper)"
)
_STRAY_BRACKET_RE = re.compile(rf"\[(?!\s*{_CUE_INNER_PATTERN}\s*\])[^\]\n]{{1,40}}\]", re.IGNORECASE)
_ANY_CUE_RE = re.compile(rf"\[\s*{_CUE_INNER_PATTERN}\s*\]", re.IGNORECASE)
_EMPHASIS_SPAN_RE = re.compile(r"\*([^*\n]{1,80}?)\*")

_SLANG_MAP = (
    (re.compile(r"\bkkk+\b", re.IGNORECASE), "hì"),
    (re.compile(r"\bhaha+(?:\s*ha+)*\b", re.IGNORECASE), "ha ha"),
    (re.compile(r"\bhehe+(?:\s*he+)*\b", re.IGNORECASE), "hê hê"),
    (re.compile(r"\b(?:ahihi+|hihi+)\b", re.IGNORECASE), "hì hì"),
    # Giữ nguyên "hông" / "hổng" vì giọng miền Nam v3turbo đọc rất tự nhiên; chỉ gom biến thể thiếu dấu
    (re.compile(r"\b(?:hong|hok|hk|hem|hơm)\b", re.IGNORECASE), "hông"),
    (re.compile(r"\bko\b", re.IGNORECASE), "không"),
    (re.compile(r"\b(?:d?zậy|d?zợ|d?zị)\b", re.IGNORECASE), "vậy"),
    (re.compile(r"\bd?zạ\b", re.IGNORECASE), "dạ"),
    (re.compile(r"\bd?zui\b", re.IGNORECASE), "vui"),
    (re.compile(r"\bd?zì\b", re.IGNORECASE), "gì"),
    (re.compile(r"\bik\b", re.IGNORECASE), "đi"),
    (re.compile(r"\bwá\b", re.IGNORECASE), "quá"),
    (re.compile(r"\bb[ií][kt]\b", re.IGNORECASE), "biết"),
    (re.compile(r"\bmún\b", re.IGNORECASE), "muốn"),
    (re.compile(r"\blun\b", re.IGNORECASE), "luôn"),
    (re.compile(r"\bhum\b", re.IGNORECASE), "hôm"),
    (re.compile(r"\bngừi\b", re.IGNORECASE), "người"),
    (re.compile(r"\bđg\b", re.IGNORECASE), "đang"),
    (re.compile(r"\btrc\b", re.IGNORECASE), "trước"),
    (re.compile(r"\bnc\b", re.IGNORECASE), "nói chuyện"),
    (re.compile(r"\bmng\b", re.IGNORECASE), "mọi người"),
    (re.compile(r"\bđc\b", re.IGNORECASE), "được"),
    (re.compile(r"\bntn\b", re.IGNORECASE), "như thế nào"),
    (re.compile(r"\b(?:ok+|oke+|okela+|ô\s*kê+)\b", re.IGNORECASE), "okay"),
    (re.compile(r"\bokie+\b|\boki\b", re.IGNORECASE), "okie"),
    (re.compile(r"\bUTH\b"), "u tê hát"),
    (re.compile(r"\bUI(?=\s*(?:$|[.,!?]))"), "iu ai"),
)


def user_asks_for_voice(text: str, recent: Optional[list] = None) -> bool:
    """True nếu người dùng đang chủ động muốn nghe voice note hoặc báo chưa thấy voice vừa hứa."""
    if not text:
        return False
    if VOICE_REQUEST_RE.search(text):
        return True
    if recent and _VOICE_FOLLOWUP_RE.search(text):
        for msg in recent[-4:]:
            if msg.get("role") == "model" and _RECENT_VOICE_MENTION_RE.search(msg.get("content", "")):
                return True
    return False


def _collapse_elongation(text: str) -> str:
    """Rút gọn chữ kéo dài kiểu Gen Z để G2P không đánh vần từng ký tự."""
    out = _REPEAT_3_RE.sub(r"\1", text)
    out = _TONED_VOWEL_TAIL_RE.sub(
        lambda m: next((g for g in m.groups() if g is not None), m.group(0)),
        out,
    )
    out = _TRAILING_PARTICLE_REPEAT_RE.sub(
        lambda m: "ơi" if m.group(0).lower().startswith("ơ") else f"{m.group(1)}a",
        out,
    )
    return out


def get_time_of_day_bucket(now_ts: Optional[float] = None) -> str:
    """Trả về khung giờ Việt Nam ('morning' | 'afternoon' | 'evening' | 'late_night') cho ngữ điệu TTS."""
    try:
        import circadian
        hr = int(circadian._vn_datetime(now_ts).hour)
    except Exception:
        return "day"
    if 5 <= hr < 12:
        return "morning"
    if 12 <= hr < 18:
        return "afternoon"
    if 18 <= hr < 22:
        return "evening"
    return "late_night"


def compute_contextual_voice_intensity(
    mood: Optional[str] = None,
    time_of_day: Optional[str] = None,
    text: str = "",
    affinity_score: Optional[int] = None,
) -> float:
    """Tính cường độ diễn cảm (0.70–1.30) tự nhiên theo tâm trạng, khung giờ và mật độ nhấn nhá."""
    m = (mood or "").strip().lower()
    tod = (time_of_day or "").strip().lower() or get_time_of_day_bucket()

    base = 1.00
    if any(k in m for k in ("hào hứng", "bất ngờ", "nũng nịu", "làm nũng", "tủi thân")):
        base = 1.15
    elif any(k in m for k in ("vui vẻ", "cà khịa", "dỗi nhẹ", "lo lắng", "cộc nhẹ")):
        base = 1.08
    elif any(k in m for k in ("ngọt ngào", "ngại ngùng", "thì thầm khuya")):
        base = 1.04
    elif any(k in m for k in ("bình yên", "tự nhiên", "lười biếng", "buồn ngủ")):
        base = 0.92

    if "*" in (text or "") or _ANY_CUE_RE.search(text or "") or "!" in (text or ""):
        base += 0.05
    if affinity_score is not None and int(affinity_score) >= 75 and any(
        k in m for k in ("ngọt ngào", "nũng nịu", "thì thầm khuya", "ngại ngùng")
    ):
        base += 0.04
    if tod == "late_night" and "thì thầm" not in m and "ngọt ngào" not in m:
        base -= 0.06

    return round(max(0.70, min(1.30, base)), 2)


def strip_speech_markup(text: str, keep_cues: bool = False) -> str:
    """Gỡ bỏ dấu nhấn *...* (và cue [cười]/[thở dài] nếu keep_cues=False) để lưu ký ức hoặc fallback chữ."""
    if not text:
        return ""
    s = _EMPHASIS_SPAN_RE.sub(r"\1", str(text))
    s = s.replace("*", "")
    if not keep_cues:
        s = _ANY_CUE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return s.strip()


def strip_balanced_stray_asterisks(text: str) -> str:
    """Giữ nguyên các cụm *nhấn* có cặp đóng mở cân đối, chỉ xóa dấu * lẻ sót lại."""
    if not text or "*" not in text:
        return text or ""
    kept: dict[str, str] = {}

    def _keep(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not inner:
            return " "
        tok = f"\ue000EK{len(kept)}\ue001"
        kept[tok] = f"*{inner}*"
        return tok

    out = _EMPHASIS_SPAN_RE.sub(_keep, text).replace("*", "")
    for tok, val in kept.items():
        out = out.replace(tok, val)
    return out


def clean_text_for_speech(text: str, max_chars: int = 900, keep_emphasis: bool = True) -> str:
    """Làm sạch emoji, markdown, link và viết tắt; bảo toàn [cười]/[thở dài]/[hắng giọng] và *từ nhấn* hợp lệ."""
    if not text:
        return ""
    cleaned = _URL_RE.sub("", text)
    cleaned = _EMOJI_RE.sub(" ", cleaned)
    cleaned = _STRAY_BRACKET_RE.sub(" ", cleaned)

    emp_tokens: dict[str, str] = {}
    if keep_emphasis:
        def _protect_emp(match: re.Match) -> str:
            inner = re.sub(r"[*_`~>|]", "", match.group(1)).strip()
            if not inner:
                return " "
            tok = f"\ue000EK{len(emp_tokens)}\ue001"
            emp_tokens[tok] = f"*{inner}*"
            return f" {tok} "

        cleaned = _EMPHASIS_SPAN_RE.sub(_protect_emp, cleaned)

    cleaned = re.sub(r"[*_`~>|]", "", cleaned)
    for pattern, repl in _SLANG_MAP:
        cleaned = pattern.sub(repl, cleaned)
    cleaned = _collapse_elongation(cleaned)
    cleaned = re.sub(r"\.{3,}|…", "... ", cleaned)
    cleaned = re.sub(r"\s*[—–]\s*", " — ", cleaned)
    cleaned = re.sub(r"([!?])\1+", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])(?=\S)", r"\1 ", cleaned)

    for tok, emp_val in emp_tokens.items():
        cleaned = cleaned.replace(tok, emp_val)

    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        last_sent = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if last_sent > max(40, max_chars // 3):
            cleaned = cut[: last_sent + 1].strip()
        else:
            last_space = cut.rfind(" ")
            base_cut = cut[:last_space] if last_space > 40 else cut
            cleaned = base_cut.rstrip(" ,;:").strip()
        cleaned = strip_balanced_stray_asterisks(cleaned)
    return cleaned


def get_effective_tts_url() -> str:
    """Trả về URL VieNeu-TTS hiệu lực (ưu tiên IP mới phân giải từ PC MAC nếu có)."""
    try:
        import lan_presence
        url = lan_presence.get_effective_tts_url()
        if url:
            return url.strip().rstrip("/")
    except Exception:
        pass
    return config.TTS_API_URL


async def check_tts_health(tts_base_url: Optional[str] = None, timeout_sec: float = 2.5) -> bool:
    """Kiểm tra nhanh VieNeu-TTS `/health` để biết PC có đang mở TTS hay không."""
    base = (tts_base_url if tts_base_url is not None else get_effective_tts_url()).strip().rstrip("/")
    if not base:
        return False

    import gemini_client

    url = f"{base}/health"
    headers = {}
    if config.TTS_API_KEY:
        headers["X-API-Key"] = config.TTS_API_KEY
    ok = False
    try:
        session = gemini_client.get_session()
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_sec, connect=1.5),
        ) as resp:
            ok = (resp.status == 200)
    except Exception:
        ok = False
    _LOCAL_TTS_CACHE.update({"url": base, "ok": ok, "ts": time.time()})
    return ok


async def is_tts_available() -> bool:
    """Trả về True nếu TTS đang cấu hình và có thể liên lạc được (dùng cache từ lan_presence)."""
    base = get_effective_tts_url()
    if not base:
        return False
    now_ts = time.time()
    try:
        import lan_presence
        snap = lan_presence.get_presence_snapshot()
        settings = snap.get("settings") or {}
        checked_at = float(snap.get("tts_checked_at") or 0.0)
        if (now_ts - checked_at) < 75.0:
            return bool(snap.get("tts_online"))
        if settings.get("pc_mac") and not snap.get("pc_online"):
            if not config.TTS_API_URL or snap.get("tts_url") != config.TTS_API_URL:
                return False
    except Exception:
        pass

    if _LOCAL_TTS_CACHE["url"] == base and (now_ts - float(_LOCAL_TTS_CACHE["ts"])) < 45.0:
        return bool(_LOCAL_TTS_CACHE["ok"])
    return await check_tts_health(base, timeout_sec=2.5)


async def synthesize_voice(
    text: str,
    timeout_sec: float = 60.0,
    mood: Optional[str] = None,
    time_of_day: Optional[str] = None,
    intensity: Optional[float] = None,
    prosody: bool = True,
) -> Optional[Tuple[bytes, str]]:
    """
    Gọi VieNeu-TTS Server tạo file âm thanh (.ogg Opus hoặc .wav) có nhấn nhá & cảm xúc.
    Trả về (audio_bytes, format) hoặc None nếu chưa cấu hình / PC đang tắt.
    """
    base_url = get_effective_tts_url()
    if not base_url:
        return None
    speak_text = clean_text_for_speech(text, keep_emphasis=bool(prosody))
    if len(speak_text) < 2:
        return None

    import gemini_client

    url = f"{base_url}/tts"
    headers = {"Content-Type": "application/json"}
    if config.TTS_API_KEY:
        headers["X-API-Key"] = config.TTS_API_KEY

    payload: dict = {"text": speak_text, "format": "ogg"}
    if mood and str(mood).strip():
        payload["mood"] = str(mood).strip()
    tod = str(time_of_day).strip() if time_of_day is not None else get_time_of_day_bucket()
    if tod:
        payload["time_of_day"] = tod
    if intensity is not None:
        payload["intensity"] = float(intensity)
    if not prosody:
        payload["prosody"] = False

    try:
        session = gemini_client.get_session()
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_sec, connect=2.5),
        ) as resp:
            if resp.status == 200:
                audio_bytes = await resp.read()
                fmt = (resp.headers.get("X-Audio-Format") or "ogg").strip().lower()
                if audio_bytes:
                    logger.info(
                        f"🎙️ [TTS Voice OK]: {len(audio_bytes)} bytes ({fmt}), "
                        f"mood={resp.headers.get('X-Prosody-Mood', '?')}, "
                        f"intensity={resp.headers.get('X-Prosody-Intensity', '?')}, "
                        f"synth={resp.headers.get('X-Synthesis-Seconds', '?')}s"
                    )
                    return audio_bytes, fmt
            else:
                err = (await resp.text())[:150]
                logger.warning(f"TTS server HTTP {resp.status}: {err}")
    except Exception as e:
        logger.info(f"TTS server không phản hồi (chuyển về nhắn chữ bình thường): {type(e).__name__}: {e}")
    return None
