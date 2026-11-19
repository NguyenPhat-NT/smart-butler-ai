# -*- coding: utf-8 -*-
"""
Điều khiển & tự động hóa Quạt BLDC ESP-01S (HTTP Port 80) từ OpenStick.

Kết nối qua mạng LAN Wi-Fi với ESP-01S (firmware ESP01S_BLDC_Fan.ino):
  - Dò IP theo MAC tĩnh, URL cấu hình tay, hoặc quét subnet tìm `/api/state`.
  - Theo dõi Wi-Fi điện thoại qua `lan_presence` để tự tắt quạt khi đi vắng và bật lại khi về.
  - Khuya tới giờ hẹn ngủ, nếu chủ nhà im ắng >= 15 phút thì Smart Butler nhắn hỏi nhẹ qua Telegram;
    nếu không trả lời sau `sleep_wait_mins` phút sẽ giảm tốc độ 1% mỗi ~1.2s về 0% (1005 Hz).
  - Bảo vệ ghi đè thủ công: phát hiện thay đổi ngoài từ Web UI ESP-01S và tạm nhường tự động hóa.
"""

import re
import json
import time
import asyncio
import datetime
import ipaddress
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

import database
import lan_events
import lan_presence

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

_SETTINGS_META_KEY = "fan_settings_v1"
_RUNTIME_META_KEY = "fan_runtime_v1"

FAN_HTTP_PORT = 80
HTTP_TIMEOUT_SEC = 3.0
DISCOVERY_TIMEOUT_SEC = 1.2
EEPROM_DEBOUNCE_MAX_STEP_SEC = 1.8

SLEEP_CHAT_IDLE_SEC = 900.0        # 15 phút không nhắn trước khi hỏi đi ngủ
SLEEP_WINDOW_HOURS = 4.0           # Cửa sổ 4 giờ tính từ sleep_ask_time
MANUAL_OVERRIDE_COOLDOWN_SEC = 900.0
MONITOR_POLL_INTERVAL_SEC = 30.0
AWAY_PHONE_ONLY_SYNC = True

_TIME_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "fan_mac": "",
    "fan_url": "",
    "auto_enabled": True,
    "auto_away": True,
    "auto_home": True,
    "auto_sleep": True,
    "sleep_ask_time": "01:00",
    "sleep_wait_mins": 30,
    "sleep_voice": False,
    "ramp_step_sec": 1.2,
    "max_freq_hz": 1800,
}

FAN_MIN_FREQ_HZ = 1005
FAN_MAX_FREQ_HZ = 3000

_DEFAULT_RUNTIME: Dict[str, Any] = {
    "last_known_ip": "",
    "sleep_prompted_date": "",
    "sleep_prompt_at": 0.0,
    "sleep_prompt_status": "none",  # none | waiting | cancelled | ramped
    "sleep_reask_date": "",
    "phone_away_since": 0.0,
    "phone_away_triggered": False,
    "plugs_away_triggered": False,
    "last_phone_online": None,
    "last_cmd": None,
    "manual_override_until": 0.0,
    "timers": [],
}

_FAN_KEYWORDS_RE = re.compile(r"\b(quạt|fan|gió\s*tự\s*nhiên|cố\s*định|1005\s*hz)\b", re.IGNORECASE)
_EXCLUDED_FAN_DEVICES_RE = re.compile(r"\b(quạt\s+thông\s+gió|quạt\s+hút)\b", re.IGNORECASE)


def _strip_non_bldc_fan_mentions(text: str) -> str:
    return _EXCLUDED_FAN_DEVICES_RE.sub(" ", text or "").strip()


def _clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def _clamp_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, round(float(val), 2)))
    except (TypeError, ValueError):
        return default


def _parse_bool(val: Any, default: bool) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def normalize_fan_url(raw: str) -> str:
    """Chuẩn hóa URL gốc của ESP-01S (vd `http://192.168.1.50`), trả rỗng nếu không hợp lệ."""
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return ""
    candidate = text if "://" in text else f"http://{text}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    host = (parsed.hostname or "").strip()
    if not host:
        return ""
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    port = f":{parsed.port}" if parsed.port and parsed.port != 80 else ""
    return f"{scheme}://{host}{port}"


def normalize_hhmm(raw: str, default: str = "01:00") -> str:
    """Chuẩn hóa giờ phút dạng `HH:MM` (00:00..23:59)."""
    s = str(raw or "").strip()
    m = _TIME_HHMM_RE.match(s)
    if not m:
        return default
    h = int(m.group(1))
    mi = int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return default
    return f"{h:02d}:{mi:02d}"


def _extract_host_ip(url_str: str) -> str:
    if not url_str:
        return ""
    try:
        parsed = urlparse(url_str if "://" in url_str else f"http://{url_str}")
        return (parsed.hostname or "").strip()
    except Exception:
        return ""


def _default_sleep_ask_time() -> str:
    try:
        lan_cfg = lan_presence.get_lan_settings()
        q_start = _clamp_int(lan_cfg.get("quiet_start_hour"), 1, 0, 23)
        return f"{q_start:02d}:00"
    except Exception:
        return _DEFAULT_SETTINGS["sleep_ask_time"]


def freq_to_speed_pct(freq_hz: Any) -> int:
    """Quy đổi tần số (1005..3000 Hz) sang % tốc độ ESP-01S (0..100%)."""
    hz = _clamp_int(freq_hz, 1800, FAN_MIN_FREQ_HZ, FAN_MAX_FREQ_HZ)
    return _clamp_int(round((hz - FAN_MIN_FREQ_HZ) / float(FAN_MAX_FREQ_HZ - FAN_MIN_FREQ_HZ) * 100.0), 40, 0, 100)


def normalize_fan_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    src = dict(raw or {})
    default_time = _default_sleep_ask_time()
    max_freq_hz = _clamp_int(src.get("max_freq_hz"), 1800, 1200, FAN_MAX_FREQ_HZ)
    return {
        "fan_mac": lan_presence.normalize_mac(src.get("fan_mac", "")),
        "fan_url": normalize_fan_url(src.get("fan_url", "")),
        "auto_enabled": _parse_bool(src.get("auto_enabled"), True),
        "auto_away": _parse_bool(src.get("auto_away"), True),
        "auto_home": _parse_bool(src.get("auto_home"), True),
        "auto_sleep": _parse_bool(src.get("auto_sleep"), True),
        "sleep_ask_time": normalize_hhmm(src.get("sleep_ask_time", default_time), default=default_time),
        "sleep_wait_mins": _clamp_int(src.get("sleep_wait_mins"), 30, 5, 180),
        "sleep_voice": _parse_bool(src.get("sleep_voice"), False),
        "ramp_step_sec": _clamp_float(src.get("ramp_step_sec"), 1.2, 0.4, EEPROM_DEBOUNCE_MAX_STEP_SEC),
        "max_freq_hz": max_freq_hz,
        "max_speed_pct": freq_to_speed_pct(max_freq_hz),
    }


def get_fan_settings() -> Dict[str, Any]:
    try:
        raw_txt = database.get_metadata(_SETTINGS_META_KEY)
        if raw_txt:
            return normalize_fan_settings(json.loads(raw_txt))
    except Exception:
        pass
    return normalize_fan_settings({})


def save_fan_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = get_fan_settings()
    merged = dict(current)
    merged.update(patch or {})
    normalized = normalize_fan_settings(merged)
    try:
        database.set_metadata(_SETTINGS_META_KEY, json.dumps(normalized, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Lỗi lưu fan_settings: {e}")
    return normalized


def _normalize_fan_timers(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        act = str(item.get("action") or "").strip().lower()
        try:
            due_at = float(item.get("due_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if act not in ("on", "off", "set_speed") or due_at <= 0:
            continue
        entry: Dict[str, Any] = {
            "action": act,
            "due_at": due_at,
            "created_at": float(item.get("created_at") or 0.0),
        }
        if item.get("speed_pct") is not None:
            try:
                entry["speed_pct"] = int(item["speed_pct"])
            except (TypeError, ValueError):
                pass
        out.append(entry)
    out.sort(key=lambda t: float(t.get("due_at", 0.0)))
    return out


def _load_runtime() -> Dict[str, Any]:
    rt = dict(_DEFAULT_RUNTIME)
    rt["timers"] = []
    try:
        raw = database.get_metadata(_RUNTIME_META_KEY)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                rt.update(parsed)
    except Exception:
        pass
    rt["timers"] = _normalize_fan_timers(rt.get("timers"))
    return rt


def _save_runtime(rt: Dict[str, Any]) -> None:
    try:
        database.set_metadata(_RUNTIME_META_KEY, json.dumps(rt, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"Lỗi lưu fan_runtime: {e}")


def is_valid_fan_state_payload(data: Any) -> bool:
    """Nhận diện đúng JSON trả về từ ESP01S_BLDC_Fan.ino `/api/state`."""
    if not isinstance(data, dict):
        return False
    required = ("is_on", "speed_pct", "mode", "output_freq")
    return all(k in data for k in required)


def normalize_fan_state(data: Dict[str, Any]) -> Dict[str, Any]:
    is_on = bool(data.get("is_on"))
    speed_pct = _clamp_int(data.get("speed_pct"), 0, 0, 100)
    mode = 1 if int(data.get("mode") or 0) == 1 else 0
    output_freq = _clamp_int(data.get("output_freq"), 0, 0, 5000)
    status = str(data.get("status") or ("RUNNING" if is_on else "OFF")).strip().upper()
    status_text = str(data.get("status_text") or "").strip()
    return {
        "is_on": is_on,
        "speed_pct": speed_pct,
        "mode": mode,
        "mode_label": "Gió tự nhiên" if mode == 1 else "Cố định",
        "output_freq": output_freq,
        "status": status,
        "status_text": status_text,
    }


def _compute_sleep_window(
    now_ts: float,
    sleep_ask_time: str,
    window_hours: float = SLEEP_WINDOW_HOURS,
) -> Optional[Tuple[str, float, float]]:
    """
    Trả về `(cycle_date_key, start_ts, end_ts)` nếu `now_ts` đang nằm trong cửa sổ
    hỏi đi ngủ `[ask_time, ask_time + window_hours]`. Hỗ trợ cả mốc qua nửa đêm (vd 23:30).
    """
    hhmm = normalize_hhmm(sleep_ask_time, "01:00")
    hour_s, min_s = hhmm.split(":")
    ask_h, ask_m = int(hour_s), int(min_s)
    now_vn = datetime.datetime.fromtimestamp(now_ts, tz=VN_TZ)

    for day_offset in (0, -1):
        base_date = now_vn.date() + datetime.timedelta(days=day_offset)
        start_dt = datetime.datetime(
            base_date.year, base_date.month, base_date.day,
            ask_h, ask_m, 0, tzinfo=VN_TZ,
        )
        start_ts = start_dt.timestamp()
        end_ts = start_ts + window_hours * 3600.0
        if start_ts <= now_ts <= end_ts:
            return base_date.strftime("%Y-%m-%d"), start_ts, end_ts
    return None


def classify_sleep_reply(text: str) -> str:
    """
    Phân loại phản hồi của anh sau tin hỏi đi ngủ:
      - 'awake': anh xác nhận chưa ngủ / giữ nguyên / đừng hạ
      - 'sleep': anh đồng ý đi ngủ / bảo hạ quạt
      - 'other': nói sang chuyện khác
    """
    low = (text or "").strip().lower()
    if not low:
        return "other"

    awake_re = re.compile(
        r"\b(chưa\s*ngủ|chưa\s*đâu|chưa\s*mà|còn\s*thức|đang\s*thức|chưa\s*buồn\s*ngủ|"
        r"đừng\s*(?:hạ|tắt|giảm)|để\s*(?:vậy|yên|nguyên)|giữ\s*nguyên|khỏi\s*(?:hạ|tắt)|"
        r"nóng\s*lắm|từ\s*từ\s*hẵng)\b",
        re.IGNORECASE,
    )
    if awake_re.search(low):
        return "awake"

    if re.search(r"^\s*chưa\b", low) and "ngủ" not in low.replace("chưa", ""):
        return "awake"

    sleep_re = re.compile(
        r"\b(ngủ\s*rồi|sắp\s*ngủ|đi\s*ngủ|ngủ\s*đây|ngủ\s*nha|ngủ\s*ngon|buồn\s*ngủ|"
        r"hạ\s*(?:quạt|đi|xuống|nha)|giảm\s*(?:quạt|đi|xuống|nha)|vặn\s*nhỏ|cho\s*nhỏ\s*lại|"
        r"ok(?:e|ela)?|ừm?|uhm?|dạ\s*hạ|được\s*(?:đó|nha|em))\b",
        re.IGNORECASE,
    )
    if sleep_re.search(low):
        return "sleep"

    return "other"


def detect_natural_fan_command(text: str) -> Optional[Dict[str, Any]]:
    """
    Phát hiện lệnh điều khiển quạt rõ ràng trong câu chat tiếng Việt.
    Trả về None nếu câu không mang mệnh lệnh rõ ràng (nhường AI Pass 1 hoặc ngữ cảnh tự nhiên).
    """
    raw = _strip_non_bldc_fan_mentions(text)
    if not raw:
        return None
    low = raw.lower()
    if not _FAN_KEYWORDS_RE.search(low):
        return None

    # Các câu hỏi thăm / kể chuyện không phải mệnh lệnh trực tiếp
    if re.search(r"(quạt\s+(?:sao|thế\s*nào|bao\s*nhiêu|ổn\s*không|bật\s*chưa)\??\s*$)", low):
        return {"action": "status"}

    # Hạ quạt đi ngủ (0% / 1005 Hz)
    if re.search(
        r"\b(hạ\s*(?:dần\s*)?quạt\s*(?:đi\s*ngủ|về\s*0|1005)|quạt\s*(?:chế\s*độ\s*ngủ|1005\s*hz)|"
        r"vặn\s*quạt\s*(?:về\s*0|thấp\s*nhất)|giảm\s*dần\s*quạt)\b",
        low,
    ):
        return {"action": "sleep_ramp"}

    # Đặt tốc độ theo Hz cụ thể (vd "chỉnh quạt 1800hz" / "2300 hz")
    m_hz = re.search(r"(?:quạt|tốc\s*độ(?:\s*quạt)?).*?(\d{3,4})\s*hz\b", low)
    if m_hz:
        pct = freq_to_speed_pct(m_hz.group(1))
        return {"action": "set_speed", "speed_pct": pct}

    # Đặt mức quạt nhanh (Mức 1..4 hoặc tối đa)
    m_lvl = re.search(r"\bmức\s*([1-4])\b(?!\s*%)", low)
    if m_lvl:
        lvl_map = {"1": 10, "2": 20, "3": 30, "4": 40}
        return {"action": "set_speed", "speed_pct": lvl_map[m_lvl.group(1)]}
    if re.search(r"\b(tối\s*đa|hết\s*cỡ|mạnh\s*nhất|max)\b", low):
        return {"action": "set_speed", "speed_pct": 100}

    # Đặt % tốc độ cụ thể
    m_pct = re.search(
        r"(?:quạt|tốc\s*độ(?:\s*quạt)?)\s*(?:lên|xuống|về|mức|là|ở|tới|sang)?\s*(\d{1,3})\s*%?",
        low,
    )
    if not m_pct:
        m_pct = re.search(r"(?:bật|mở|vặn|chỉnh|đặt|set|để)\s+quạt.*?(\d{1,3})\s*%", low)
    if m_pct:
        pct = _clamp_int(m_pct.group(1), 30, 0, 100)
        return {"action": "set_speed", "speed_pct": pct}

    # Chế độ gió tự nhiên / cố định
    if re.search(r"\b(gió\s*tự\s*nhiên|chế\s*độ\s*tự\s*nhiên|tự\s*nhiên)\b", low):
        return {"action": "mode_natural"}
    if re.search(r"\b(gió\s*cố\s*định|gió\s*thường|tắt\s*gió\s*tự\s*nhiên|chế\s*độ\s*cố\s*định)\b", low):
        return {"action": "mode_fixed"}

    # Bật / tắt nguồn quạt
    if re.search(r"\b(tắt|ngắt|dừng|stop)\s+(?:cái\s+)?quạt\b|\bquạt\s+tắt\b", low):
        return {"action": "power_off"}
    if re.search(r"\b(bật|mở)\s+(?:cái\s+)?quạt\b|\bmở\s*giúp.*?quạt\b", low):
        return {"action": "power_on"}

    # Tăng / giảm bước speed
    if re.search(r"\b(tăng|mạnh\s*(?:hơn|lên)|to\s*lên|vặn\s*mạnh)\b", low):
        return {"action": "speed_up", "step": 10}
    if re.search(r"\b(giảm|nhỏ\s*(?:hơn|lại|xuống)|yếu\s*(?:hơn|bớt)|nhẹ\s*(?:hơn|bớt)|hạ\s+quạt)\b", low):
        return {"action": "speed_down", "step": 10}

    return None


async def _lookup_ip_for_mac(mac: str) -> str:
    norm_mac = lan_presence.normalize_mac(mac)
    if not norm_mac:
        return ""
    try:
        out = await lan_presence._run_cmd("ip", "neigh", "show", timeout=2.0)
        for ent in lan_presence.parse_ip_neigh(out):
            if ent.get("mac") == norm_mac and ent.get("ip"):
                return str(ent["ip"])
    except Exception:
        pass
    return ""


async def _build_proactive_sleep_prompt_text(
    speed_pct: int,
    wait_mins: int,
    plug_names: Optional[List[str]] = None,
) -> str:
    import persona
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    plugs_desc = ", ".join(plug_names or [])
    if speed_pct > 0 and plugs_desc:
        item_desc = f"quạt phòng đang chạy {speed_pct}% và {plugs_desc} vẫn đang bật"
        action_desc = f"{b_name} sẽ tự hạ quạt về 0% và tắt {plugs_desc} giúp {o_name} ngủ ngon nhé"
    elif plugs_desc:
        item_desc = f"{plugs_desc} vẫn đang bật sáng"
        action_desc = f"{b_name} sẽ tự tắt {plugs_desc} giúp {o_name} nghỉ ngơi nhé"
    else:
        item_desc = f"quạt đang chạy {speed_pct}%"
        action_desc = f"{b_name} sẽ tự hạ quạt dần về 0% cho đỡ lạnh nhé"

    fallback = (
        f"{o_name} ơi, muộn rồi mà thấy {item_desc}. "
        f"Nếu {wait_mins} phút nữa chưa thấy phản hồi thì {action_desc}."
    )
    try:
        import config
        import gemini_client
        if not getattr(config, "GEMINI_API_KEYS", None):
            return fallback
        prompt = (
            f"Viết đúng 1-2 câu tiếng Việt thật ngắn (dưới 40 từ) theo vai {b_name} ({pcfg['style']}) "
            f"hỏi thăm xem {o_name} đã ngủ chưa vì thấy muộn rồi mà {item_desc}; "
            f"nhắn khéo nếu khoảng {wait_mins} phút nữa chưa có phản hồi thì {action_desc}. "
            f"Phong cách: {pcfg['tone_desc']} Tối đa 1 emoji."
        )
        ai_text = await asyncio.wait_for(
            gemini_client.call_gemini_api(
                model=config.CHAT_MODEL,
                system_instruction=f"Bạn là {b_name}, trợ lý quản gia Smart Home cá nhân của {o_name}.",
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                response_json=False,
            ),
            timeout=6.0,
        )
        clean = str(ai_text or "").strip().strip('"').strip()
        if len(clean) >= 12:
            return clean
    except Exception:
        pass
    return fallback


class FanController:
    """Quản lý kết nối HTTP LAN, điều khiển thủ công/AI và tự động hóa cho ESP-01S BLDC Fan."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cmd_lock: Optional[asyncio.Lock] = None
        self._tick_lock: Optional[asyncio.Lock] = None
        self._ramp_task: Optional[asyncio.Task] = None
        self._last_state: Optional[Dict[str, Any]] = None
        self._last_state_at: float = 0.0
        self._online: bool = False
        self._resolved_url: str = ""
        self._resolved_ip: str = ""
        self._last_error: str = ""
        self._ramping_active: bool = False
        self._ramp_target_pct: int = 0
        self._runtime: Dict[str, Any] = _load_runtime()

    def _get_cmd_lock(self) -> asyncio.Lock:
        if self._cmd_lock is None:
            self._cmd_lock = asyncio.Lock()
        return self._cmd_lock

    def _get_tick_lock(self) -> asyncio.Lock:
        if self._tick_lock is None:
            self._tick_lock = asyncio.Lock()
        return self._tick_lock

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC, connect=1.5, sock_read=HTTP_TIMEOUT_SEC)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        t = self._ramp_task
        self.cancel_ramp()
        if t is not None and not t.done():
            await asyncio.gather(t, return_exceptions=True)
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _save_rt(self) -> None:
        _save_runtime(self._runtime)

    def _record_cmd(self, state: Dict[str, Any], from_sleep_ramp: bool = False) -> None:
        self._runtime["last_cmd"] = {
            "is_on": bool(state.get("is_on")),
            "speed_pct": int(state.get("speed_pct", 0)),
            "mode": int(state.get("mode", 0)),
            "at": time.time(),
            "from_sleep_ramp": bool(from_sleep_ramp),
        }
        self._save_rt()

    def _detect_external_override(self, live_state: Dict[str, Any], now_ts: float) -> bool:
        """
        Kiểm tra xem trạng thái thực tế trên ESP-01S có khác bất ngờ với lệnh OpenStick
        vừa ra gần đây hay không (dấu hiệu user vừa chỉnh trực tiếp qua Web UI 192.168.x.x của quạt).
        """
        last_cmd = self._runtime.get("last_cmd")
        if not isinstance(last_cmd, dict):
            return False
        cmd_at = float(last_cmd.get("at") or 0.0)
        if cmd_at <= 0.0 or (now_ts - cmd_at) > 43200.0:
            return False

        changed = (
            bool(live_state.get("is_on")) != bool(last_cmd.get("is_on"))
            or (
                bool(live_state.get("is_on"))
                and abs(int(live_state.get("speed_pct", 0)) - int(last_cmd.get("speed_pct", 0))) >= 2
            )
            or int(live_state.get("mode", 0)) != int(last_cmd.get("mode", 0))
        )
        if not changed:
            return False

        # Cập nhật mốc tham chiếu mới để không kích hoạt lặp lại, đồng thời tạm nhường tự động hóa
        self._record_cmd(live_state)
        self._runtime["manual_override_until"] = max(
            float(self._runtime.get("manual_override_until") or 0.0),
            now_ts + MANUAL_OVERRIDE_COOLDOWN_SEC,
        )
        if self._runtime.get("sleep_prompt_status") == "waiting":
            self._runtime["sleep_prompt_status"] = "cancelled"
        self._save_rt()
        import habit_engine
        habit_engine.notify_device_event(
            "fan",
            "Quạt phòng",
            "set_speed" if live_state.get("is_on") else "power_off",
            value_num=float(live_state.get("speed_pct", 0)) if live_state.get("is_on") else 0.0,
            source="user_physical",
        )
        logger.info("🌬️ [Fan] Phát hiện thay đổi trực tiếp trên ESP-01S -> tạm nhường tự động hóa 15p")
        return True

    async def _resolve_candidate_urls(self, settings: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        seen = set()

        def _add(url_val: str) -> None:
            norm = normalize_fan_url(url_val)
            if norm and norm not in seen:
                seen.add(norm)
                candidates.append(norm)

        if self._resolved_url:
            _add(self._resolved_url)
        mac = settings.get("fan_mac", "")
        if mac:
            mac_ip = await _lookup_ip_for_mac(mac)
            if mac_ip:
                _add(f"http://{mac_ip}")
        if settings.get("fan_url"):
            _add(settings["fan_url"])
        last_ip = str(self._runtime.get("last_known_ip") or "").strip()
        if last_ip:
            _add(f"http://{last_ip}")
        return candidates

    async def _request_fan(self, path_and_query: str) -> Optional[Dict[str, Any]]:
        settings = get_fan_settings()
        candidates = await self._resolve_candidate_urls(settings)
        if not candidates:
            self._online = False
            self._last_error = "Chưa cấu hình IP hoặc MAC quạt."
            return None

        session = await self._get_http_session()
        last_err = ""
        for base in candidates:
            url = f"{base}{path_and_query}"
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        continue
                    data = await resp.json(content_type=None)
                    if not is_valid_fan_state_payload(data):
                        last_err = "Phản hồi không đúng chuẩn ESP01S_BLDC_Fan."
                        continue
                    norm_state = normalize_fan_state(data)
                    self._resolved_url = base
                    self._resolved_ip = _extract_host_ip(base)
                    self._last_state = norm_state
                    self._last_state_at = time.time()
                    self._online = True
                    self._last_error = ""
                    if self._resolved_ip and self._runtime.get("last_known_ip") != self._resolved_ip:
                        self._runtime["last_known_ip"] = self._resolved_ip
                        self._save_rt()
                    return norm_state
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if base == self._resolved_url:
                    self._resolved_url = ""

        self._online = False
        self._last_error = last_err or "Không kết nối được tới quạt ESP-01S."
        return None

    def get_timers_snapshot(self, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        now_v = float(now_ts if now_ts is not None else time.time())
        timers = _normalize_fan_timers(self._runtime.get("timers"))
        out: List[Dict[str, Any]] = []
        for t in timers:
            due_at = float(t["due_at"])
            if due_at <= now_v:
                continue
            item: Dict[str, Any] = {
                "action": t["action"],
                "due_at": due_at,
                "remaining_sec": max(0, int(due_at - now_v)),
            }
            if t.get("speed_pct") is not None:
                item["speed_pct"] = int(t["speed_pct"])
            out.append(item)
        return out

    def schedule_timer(
        self,
        action: str,
        due_at: float,
        *,
        speed_pct: Optional[int] = None,
        replace: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_v = float(now_ts if now_ts is not None else time.time())
        act = str(action or "").strip().lower()
        if act in ("power_on",):
            act = "on"
        elif act in ("power_off",):
            act = "off"
        if act not in ("on", "off", "set_speed"):
            raise ValueError(f"Invalid fan timer action: {action}")
        entry: Dict[str, Any] = {
            "action": act,
            "due_at": float(due_at),
            "created_at": now_v,
        }
        if speed_pct is not None:
            entry["speed_pct"] = int(speed_pct)
        timers = [] if replace else _normalize_fan_timers(self._runtime.get("timers"))
        timers.append(entry)
        self._runtime["timers"] = _normalize_fan_timers(timers)
        self._save_rt()
        self.publish_update()
        return entry

    def schedule_sequence(
        self,
        start_action: str,
        due_at: float,
        *,
        speed_pct: Optional[int] = None,
        duration_minutes: Optional[float] = None,
        now_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now_v = float(now_ts if now_ts is not None else time.time())
        act = str(start_action or "").strip().lower()
        if act in ("power_on",):
            act = "on"
        elif act in ("power_off",):
            act = "off"
        steps: List[Dict[str, Any]] = []
        start_entry: Dict[str, Any] = {
            "action": act,
            "due_at": float(due_at),
            "created_at": now_v,
        }
        if speed_pct is not None:
            start_entry["speed_pct"] = int(speed_pct)
        steps.append(start_entry)
        if duration_minutes is not None and float(duration_minutes) > 0 and act != "off":
            steps.append({
                "action": "off",
                "due_at": float(due_at) + float(duration_minutes) * 60.0,
                "created_at": now_v,
            })
        self._runtime["timers"] = _normalize_fan_timers(steps)
        self._save_rt()
        self.publish_update()
        return list(self._runtime["timers"])

    def cancel_timer(self) -> int:
        prev = _normalize_fan_timers(self._runtime.get("timers"))
        self._runtime["timers"] = []
        self._save_rt()
        self.publish_update()
        return len(prev)

    def get_status_snapshot(self) -> Dict[str, Any]:
        settings = get_fan_settings()
        rt = self._runtime
        now_ts = time.time()
        sleep_status = str(rt.get("sleep_prompt_status") or "none")
        prompt_at = float(rt.get("sleep_prompt_at") or 0.0)
        wait_secs = int(settings.get("sleep_wait_mins", 30)) * 60
        sleep_countdown = max(0, int((prompt_at + wait_secs) - now_ts)) if sleep_status == "waiting" and prompt_at > 0 else 0
        override_until = float(rt.get("manual_override_until") or 0.0)
        configured = bool(settings.get("fan_url") or settings.get("fan_mac") or self._resolved_url)
        upcoming_timers = self.get_timers_snapshot(now_ts=now_ts)

        return {
            "configured": configured,
            "online": bool(self._online),
            "fan_url": self._resolved_url or settings.get("fan_url") or "",
            "resolved_ip": self._resolved_ip or _extract_host_ip(settings.get("fan_url") or ""),
            "fan_mac": settings.get("fan_mac") or "",
            "last_error": self._last_error,
            "state": dict(self._last_state) if self._last_state else None,
            "last_state_at": self._last_state_at,
            "ramping": bool(self._ramping_active),
            "ramp_target_pct": self._ramp_target_pct if self._ramping_active else None,
            "timers": upcoming_timers,
            "automation": {
                "phone_away_triggered": bool(rt.get("phone_away_triggered")),
                "phone_away_since": float(rt.get("phone_away_since") or 0.0),
                "sleep_prompted_date": rt.get("sleep_prompted_date") or "",
                "sleep_prompt_status": sleep_status,
                "sleep_countdown_sec": sleep_countdown,
                "manual_override_sec": max(0, int(override_until - now_ts)),
                "timer": upcoming_timers[0] if upcoming_timers else None,
            },
            "settings": settings,
        }

    def publish_update(self) -> None:
        lan_events.publish("fan_update", self.get_status_snapshot())

    async def get_state(self, refresh: bool = True, check_override: bool = False) -> Optional[Dict[str, Any]]:
        if not refresh and self._last_state is not None and (time.time() - self._last_state_at) < 5.0:
            return dict(self._last_state)
        state = await self._request_fan("/api/state")
        if state is not None and check_override:
            self._detect_external_override(state, time.time())
        return state

    def cancel_ramp(self) -> bool:
        was_running = self._ramping_active
        if self._ramp_task is not None and not self._ramp_task.done():
            self._ramp_task.cancel()
        self._ramp_task = None
        self._ramping_active = False
        return was_running

    async def set_power(
        self,
        on: bool,
        *,
        manual: bool = False,
        reason: str = "",
        event_source: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        self.cancel_ramp()
        async with self._get_cmd_lock():
            state = await self._request_fan(f"/api/power?state={1 if on else 0}")
            if state is not None:
                self._record_cmd(state)
                if manual:
                    self._runtime["manual_override_until"] = time.time() + MANUAL_OVERRIDE_COOLDOWN_SEC
                    if self._runtime.get("sleep_prompt_status") == "waiting":
                        self._runtime["sleep_prompt_status"] = "cancelled"
                    self._save_rt()
                self.publish_update()
                src = event_source or (
                    ("user_dashboard" if "dashboard" in reason.lower() else ("user_telegram" if reason in ("AI/Chat", "speed_up while off") else "user_action"))
                    if manual
                    else "system_auto"
                )
                import habit_engine
                habit_engine.notify_device_event(
                    "fan",
                    "Quạt phòng",
                    "power_on" if on else "power_off",
                    value_num=float(state.get("speed_pct", 0)) if on else 0.0,
                    source=src,
                )
                if reason:
                    logger.info(f"🌬️ [Fan] Power -> {'ON' if on else 'OFF'} ({reason})")
            return state

    async def set_speed(
        self,
        speed_pct: int,
        *,
        manual: bool = False,
        from_sleep_ramp: bool = False,
        event_source: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not from_sleep_ramp:
            self.cancel_ramp()
        max_pct = int(get_fan_settings().get("max_speed_pct", 40))
        pct = _clamp_int(speed_pct, 0, 0, max_pct if not from_sleep_ramp else 100)
        async with self._get_cmd_lock():
            state = await self._request_fan(f"/api/speed?val={pct}")
            if state is not None:
                self._record_cmd(state, from_sleep_ramp=from_sleep_ramp)
                if manual:
                    self._runtime["manual_override_until"] = time.time() + MANUAL_OVERRIDE_COOLDOWN_SEC
                    if self._runtime.get("sleep_prompt_status") == "waiting":
                        self._runtime["sleep_prompt_status"] = "cancelled"
                    self._save_rt()
                self.publish_update()
                if not from_sleep_ramp:
                    src = event_source or ("user_action" if manual else "system_auto")
                    import habit_engine
                    habit_engine.notify_device_event(
                        "fan",
                        "Quạt phòng",
                        "set_speed",
                        value_num=float(state.get("speed_pct", pct)),
                        source=src,
                    )
            return state

    async def set_mode(self, mode: int, *, manual: bool = False) -> Optional[Dict[str, Any]]:
        m = 1 if int(mode) == 1 else 0
        async with self._get_cmd_lock():
            state = await self._request_fan(f"/api/mode?val={m}")
            if state is not None:
                self._record_cmd(state)
                if manual:
                    self._runtime["manual_override_until"] = time.time() + MANUAL_OVERRIDE_COOLDOWN_SEC
                    self._save_rt()
                self.publish_update()
            return state

    async def start_sleep_ramp(
        self,
        target_pct: int = 0,
        step_pct: int = 1,
        step_interval_sec: Optional[float] = None,
    ) -> bool:
        """
        Khởi chạy tác vụ giảm tốc độ quạt từng 1% một (nhịp < 2.0s để ESP-01S debounce
        không commit EEPROM giữa chừng, chỉ ghi flash đúng 1 lần khi về tới 0% / 1005 Hz).
        """
        self.cancel_ramp()
        cur = await self.get_state(refresh=True)
        if not cur or not cur.get("is_on"):
            return False
        start_pct = int(cur.get("speed_pct", 0))
        target = _clamp_int(target_pct, 0, 0, 100)
        if start_pct <= target:
            return False

        cfg = get_fan_settings()
        interval = float(step_interval_sec if step_interval_sec is not None else cfg.get("ramp_step_sec", 1.2))
        interval = max(0.05, min(EEPROM_DEBOUNCE_MAX_STEP_SEC, interval))
        step = max(1, int(step_pct))

        self._ramping_active = True
        self._ramp_target_pct = target

        async def _ramp_worker() -> None:
            self.publish_update()
            logger.info(f"🌙 [Fan] Bắt đầu giảm tốc mượt {start_pct}% -> {target}% (bước {step}%/{interval:.2f}s)")
            try:
                cur_val = start_pct
                step_idx = 0
                while cur_val > target:
                    next_val = max(target, cur_val - step)
                    # Kiểm tra định kỳ xem user có đang chỉnh quạt qua Web UI ngoài không
                    if step_idx > 0 and step_idx % 5 == 0:
                        probe = await self.get_state(refresh=True)
                        if probe is None or not probe.get("is_on"):
                            logger.info("🌙 [Fan] Dừng giảm tốc vì quạt đã tắt.")
                            return
                        if abs(int(probe.get("speed_pct", 0)) - cur_val) >= 2:
                            logger.info("🌙 [Fan] Dừng giảm tốc vì phát hiện chỉnh tay trên Web UI quạt.")
                            self._detect_external_override(probe, time.time())
                            return

                    res = await self.set_speed(next_val, from_sleep_ramp=True)
                    if res is None or not res.get("is_on"):
                        return
                    cur_val = next_val
                    step_idx += 1
                    if cur_val > target:
                        await asyncio.sleep(interval)

                logger.info(f"🌙 [Fan] Hoàn tất giảm tốc về {target}% (1005 Hz).")
                lan_events.emit_notify(
                    title="🌙 Quạt đã hạ êm về 1005 Hz",
                    body=f"Giảm dần từ {start_pct}% về {target}% để anh ngủ ngon không bị lạnh.",
                    level="info",
                    source="fan",
                    speak_tts=False,
                )
            except asyncio.CancelledError:
                logger.info("🌙 [Fan] Tác vụ giảm tốc quạt đã được hủy.")
            finally:
                self._ramping_active = False
                self._ramp_task = None
                self.publish_update()

        self._ramp_task = asyncio.create_task(_ramp_worker())
        return True

    async def discover_fan_on_lan(
        self,
        candidate_ips: Optional[List[str]] = None,
        probe_port: int = FAN_HTTP_PORT,
    ) -> Dict[str, Any]:
        """
        Quét mạng LAN và dò endpoint HTTP `/api/state` chuẩn ESP01S_BLDC_Fan.
        Khi tìm thấy sẽ tự lưu `fan_url` + `fan_mac` (nếu có trong ARP).
        """
        port_num = max(1, min(65535, int(probe_port or FAN_HTTP_PORT)))
        port_suffix = f":{port_num}" if port_num != 80 else ""
        settings = get_fan_settings()
        ip_to_mac: Dict[str, str] = {}
        ordered_ips: List[str] = []
        seen_ips = set()

        def _push_ip(ip_val: str) -> None:
            s = str(ip_val or "").strip()
            if not s or s in seen_ips:
                return
            try:
                if ipaddress.ip_address(s).version == 4:
                    seen_ips.add(s)
                    ordered_ips.append(s)
            except ValueError:
                pass

        # Ưu tiên IP hiện biết / URL đã lưu / candidate truyền vào (hữu ích khi unit test)
        for ip_hint in (
            _extract_host_ip(self._resolved_url),
            _extract_host_ip(settings.get("fan_url") or ""),
            self._runtime.get("last_known_ip") or "",
        ):
            _push_ip(ip_hint)
        for extra_ip in candidate_ips or []:
            _push_ip(extra_ip)

        if candidate_ips is None:
            try:
                devs = await lan_presence.scan_lan_devices(probe_subnet=True)
                target_mac = settings.get("fan_mac") or ""
                # Đưa MAC quạt lên đầu, tiếp đó tới thiết bị lạ chưa gắn phone/pc
                devs_sorted = sorted(
                    devs,
                    key=lambda d: (
                        0 if target_mac and d.get("mac") == target_mac else
                        (2 if (d.get("is_phone") or d.get("is_pc")) else 1)
                    ),
                )
                for d in devs_sorted:
                    d_ip = str(d.get("ip") or "")
                    d_mac = lan_presence.normalize_mac(d.get("mac") or "")
                    if d_ip:
                        if d_mac:
                            ip_to_mac[d_ip] = d_mac
                        _push_ip(d_ip)
            except Exception as e:
                logger.warning(f"Lỗi scan LAN tìm quạt: {e}")

        sem = asyncio.Semaphore(16)
        found_result: Optional[Tuple[str, Dict[str, Any]]] = None
        probe_timeout = aiohttp.ClientTimeout(total=DISCOVERY_TIMEOUT_SEC, connect=0.8)

        async def _probe_ip(session: aiohttp.ClientSession, ip_addr: str) -> Optional[Tuple[str, Dict[str, Any]]]:
            async with sem:
                try:
                    async with session.get(f"http://{ip_addr}{port_suffix}/api/state") as resp:
                        if resp.status != 200:
                            return None
                        data = await resp.json(content_type=None)
                        if is_valid_fan_state_payload(data):
                            return ip_addr, normalize_fan_state(data)
                except Exception:
                    return None
            return None

        async with aiohttp.ClientSession(timeout=probe_timeout) as scan_sess:
            tasks = [asyncio.create_task(_probe_ip(scan_sess, ip)) for ip in ordered_ips]
            for fut in asyncio.as_completed(tasks):
                res = await fut
                if res is not None:
                    found_result = res
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    break
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if found_result is None:
            return {
                "found": False,
                "scanned_count": len(ordered_ips),
                "snapshot": self.get_status_snapshot(),
            }

        found_ip, norm_state = found_result
        found_url = f"http://{found_ip}{port_suffix}"
        found_mac = ip_to_mac.get(found_ip) or ""
        if not found_mac:
            # Đọc lại ARP ngay sau khi ESP-01S vừa trả lời HTTP để lấy đúng MAC
            try:
                out = await lan_presence._run_cmd("ip", "neigh", "show", timeout=1.5)
                for ent in lan_presence.parse_ip_neigh(out):
                    if ent.get("ip") == found_ip and ent.get("mac"):
                        found_mac = ent["mac"]
                        break
            except Exception:
                pass
        if not found_mac:
            found_mac = settings.get("fan_mac") or ""

        patch: Dict[str, Any] = {"fan_url": found_url}
        if found_mac:
            patch["fan_mac"] = found_mac
        saved_cfg = save_fan_settings(patch)

        self._resolved_url = found_url
        self._resolved_ip = found_ip
        self._last_state = norm_state
        self._last_state_at = time.time()
        self._online = True
        self._last_error = ""
        self._runtime["last_known_ip"] = found_ip
        self._save_rt()
        self.publish_update()

        return {
            "found": True,
            "fan_url": found_url,
            "fan_mac": saved_cfg.get("fan_mac") or "",
            "state": norm_state,
            "scanned_count": len(ordered_ips),
            "snapshot": self.get_status_snapshot(),
        }

    async def _send_sleep_checkin(
        self,
        bot: Any,
        target_user_id: int,
        speed_pct: int,
        wait_mins: int,
        voice_enabled: bool,
        plug_names: Optional[List[str]] = None,
    ) -> bool:
        """Nhờ Smart Butler gửi câu hỏi nhẹ nhàng trước khi hạ tốc độ quạt / tắt đèn ban đêm."""
        if bot is None or int(target_user_id or 0) <= 0:
            return False

        msg_text = await _build_proactive_sleep_prompt_text(speed_pct, wait_mins, plug_names=plug_names)
        try:
            import scheduler
            import persona
            b_name = persona.get_persona_config()["butler_name"]
            sent_ok = await scheduler._dispatch_proactive_messages(
                bot=bot,
                target_user_id=int(target_user_id),
                messages=[msg_text],
                new_mood="chu đáo",
                count_as_unanswered=False,
                base_delay=0,
                voice_text=(msg_text if voice_enabled else None),
                notify_title=f"🌙 {b_name} hỏi thăm giờ ngủ & quạt",
                speak_notify_tts=False,
            )
            return bool(sent_ok)
        except Exception as e:
            logger.warning(f"Lỗi gửi tin hỏi thăm đi ngủ cho quạt: {e}")
            return False

    async def _evaluate_away_home(
        self,
        pres: Dict[str, Any],
        settings: Dict[str, Any],
        now_ts: float,
    ) -> bool:
        """
        Theo dõi điện thoại Wi-Fi để tắt quạt/ổ cắm khi ra ngoài (>= away_grace_mins)
        và bật lại khi về phòng. Trả về True nếu điện thoại đang được xác nhận ở nhà.
        """
        phone_mac = str(pres.get("phone_mac") or "").strip()
        if not phone_mac:
            return True

        phone_online = bool(pres.get("phone_online"))
        pres_settings = pres.get("settings") if isinstance(pres.get("settings"), dict) else {}
        raw_grace = pres.get("away_grace_mins")
        if raw_grace is None:
            raw_grace = pres_settings.get("away_grace_mins")
        if raw_grace is None:
            raw_grace = lan_presence.get_lan_settings().get("away_grace_mins", 8)
        grace_mins = _clamp_int(raw_grace, 8, 2, 60)
        grace_secs = grace_mins * 60.0

        rt = self._runtime
        override_active = now_ts < float(rt.get("manual_override_until") or 0.0)
        fan_auto = bool(settings.get("auto_enabled", True))
        fan_configured = bool(settings.get("fan_url") or settings.get("fan_mac") or self._resolved_url)

        if phone_online:
            was_fan_away = bool(rt.get("phone_away_triggered"))
            was_plugs_away = bool(rt.get("plugs_away_triggered"))
            rt["phone_away_since"] = 0.0
            rt["phone_away_triggered"] = False
            rt["plugs_away_triggered"] = False
            rt["last_phone_online"] = True
            self._save_rt()

            if was_fan_away and not override_active and fan_auto and fan_configured and settings.get("auto_home", True):
                cur = await self.get_state(refresh=True)
                if cur is not None and not cur.get("is_on"):
                    await self.set_power(True, reason="về phòng (phone Wi-Fi online)")
                    lan_events.emit_notify(
                        title="🏠 Chào mừng về phòng",
                        body="Điện thoại vừa vào lại Wi-Fi nên quạt được tự động bật lại.",
                        level="info",
                        source="fan",
                        speak_tts=False,
                    )
            if was_plugs_away or was_fan_away:
                try:
                    import smart_plugs
                    await smart_plugs.get_controller().on_home_returned(now_ts=now_ts)
                except Exception as plug_err:
                    logger.debug(f"Lỗi auto_home BroadLink: {plug_err}")
            return True

        # Điện thoại đang offline
        away_since = float(rt.get("phone_away_since") or 0.0)
        if away_since <= 0.0:
            pres_away_since = float(pres.get("away_since") or 0.0)
            away_since = pres_away_since if pres_away_since > 0.0 else now_ts
            rt["phone_away_since"] = away_since
            rt["last_phone_online"] = False
            self._save_rt()

        away_elapsed = now_ts - away_since
        if away_elapsed >= grace_secs:
            # Ổ cắm BroadLink chạy độc lập với manual_override của riêng Quạt
            if not rt.get("plugs_away_triggered"):
                rt["plugs_away_triggered"] = True
                self._save_rt()
                try:
                    import smart_plugs
                    await smart_plugs.get_controller().on_away_triggered()
                except Exception as plug_err:
                    logger.debug(f"Lỗi auto_away BroadLink: {plug_err}")

            if not rt.get("phone_away_triggered") and not override_active:
                rt["phone_away_triggered"] = True
                self._save_rt()
                if fan_auto and fan_configured and settings.get("auto_away", True):
                    cur = await self.get_state(refresh=True)
                    if cur is not None and cur.get("is_on"):
                        await self.set_power(False, reason=f"ra ngoài >= {grace_mins}p")
                        lan_events.emit_notify(
                            title="🚪 Tự tắt quạt khi ra ngoài",
                            body=f"Điện thoại rời Wi-Fi quá {grace_mins} phút -> quạt đã tắt (giữ tốc độ cũ trong EEPROM).",
                            level="info",
                            source="fan",
                            speak_tts=False,
                        )
        return not bool(rt.get("phone_away_triggered") or rt.get("plugs_away_triggered"))

    async def _evaluate_sleep_checkin(
        self,
        bot: Any,
        target_user_id: int,
        settings: Dict[str, Any],
        now_ts: float,
    ) -> None:
        if not settings.get("auto_sleep", True):
            return
        rt = self._runtime
        if now_ts < float(rt.get("manual_override_until") or 0.0):
            return

        win = _compute_sleep_window(now_ts, settings.get("sleep_ask_time", "01:00"))
        if win is None:
            return
        cycle_date, _win_start, _win_end = win

        prompted_date = str(rt.get("sleep_prompted_date") or "")
        prompt_status = str(rt.get("sleep_prompt_status") or "none")
        prompt_at = float(rt.get("sleep_prompt_at") or 0.0)

        # Đã xử lý xong trong đêm nay -> không hỏi lặp lại
        if prompted_date == cycle_date and prompt_status in ("cancelled", "ramped"):
            return

        # Đang đếm chờ anh trả lời sau khi Đan đã hỏi
        if prompted_date == cycle_date and prompt_status == "waiting" and prompt_at > 0.0:
            wait_secs = int(settings.get("sleep_wait_mins", 30)) * 60.0
            if (now_ts - prompt_at) >= wait_secs:
                cur = await self.get_state(refresh=True, check_override=True)
                if now_ts < float(rt.get("manual_override_until") or 0.0):
                    return
                rt["sleep_prompt_status"] = "ramped"
                self._save_rt()
                if cur is not None and cur.get("is_on") and int(cur.get("speed_pct", 0)) > 0:
                    await self.start_sleep_ramp(
                        target_pct=0,
                        step_pct=1,
                        step_interval_sec=float(settings.get("ramp_step_sec", 1.2)),
                    )
                try:
                    import smart_plugs
                    await smart_plugs.get_controller().on_sleep_triggered()
                except Exception as plug_err:
                    logger.debug(f"Lỗi auto_sleep BroadLink: {plug_err}")
            return

        # Chưa hỏi trong đêm nay: kiểm tra user có đang chat sát giờ (< 15 phút) không
        last_user_msg = 0.0
        if int(target_user_id or 0) > 0:
            try:
                st = database.get_or_create_user(int(target_user_id))
                last_user_msg = float(st.get("last_user_message_time") or 0.0)
            except Exception:
                last_user_msg = 0.0

        if last_user_msg > 0.0 and (now_ts - last_user_msg) < SLEEP_CHAT_IDLE_SEC:
            return

        # Kiểm tra xem có quạt đang chạy >0% HOẶC ổ cắm đi ngủ (Đèn phòng) đang bật không
        cur = await self.get_state(refresh=True, check_override=True)
        fan_speed = int(cur.get("speed_pct", 0)) if (cur and cur.get("is_on")) else 0
        sleep_plug_names: List[str] = []
        try:
            import smart_plugs
            active_plugs = await smart_plugs.get_controller().get_active_sleep_plugs()
            sleep_plug_names = [p["name"] for p in active_plugs]
        except Exception:
            sleep_plug_names = []

        if fan_speed <= 0 and not sleep_plug_names:
            return

        wait_mins = int(settings.get("sleep_wait_mins", 30))
        sent_ok = await self._send_sleep_checkin(
            bot=bot,
            target_user_id=target_user_id,
            speed_pct=fan_speed,
            wait_mins=wait_mins,
            voice_enabled=bool(settings.get("sleep_voice", False)),
            plug_names=sleep_plug_names,
        )
        if sent_ok:
            rt["sleep_prompted_date"] = cycle_date
            rt["sleep_prompt_at"] = now_ts
            rt["sleep_prompt_status"] = "waiting"
            self._save_rt()
            self.publish_update()

    async def _tick_timers(self, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        now_v = float(now_ts if now_ts is not None else time.time())
        timers = _normalize_fan_timers(self._runtime.get("timers"))
        if not timers:
            return []
        due = [t for t in timers if float(t.get("due_at", 0.0)) <= now_v]
        if not due:
            return []
        remaining = [t for t in timers if float(t.get("due_at", 0.0)) > now_v]
        self._runtime["timers"] = remaining
        self._save_rt()
        executed: List[Dict[str, Any]] = []
        for step in due:
            act = str(step.get("action") or "").strip().lower()
            res = None
            if act == "on":
                res = await self.set_power(True, manual=False, reason="Hẹn giờ bật quạt", event_source="fan_timer")
            elif act == "off":
                res = await self.set_power(False, manual=False, reason="Hẹn giờ tắt quạt", event_source="fan_timer")
            elif act == "set_speed":
                max_pct = int(get_fan_settings().get("max_speed_pct", 40))
                pct = _clamp_int(step.get("speed_pct"), min(20, max_pct), 0, max_pct)
                cur = await self.get_state(refresh=False)
                if cur is None or not cur.get("is_on"):
                    await self.set_power(True, manual=False, reason="Hẹn giờ bật quạt", event_source="fan_timer")
                res = await self.set_speed(pct, manual=False, event_source="fan_timer")
            if res is not None:
                executed.append({"action": act, "due_at": float(step["due_at"]), "state": res})
        self.publish_update()
        return executed

    def schedule_auto_off(self, off_due_at: float, *, now_ts: Optional[float] = None) -> Dict[str, Any]:
        return self.schedule_timer("off", float(off_due_at), replace=True, now_ts=now_ts)

    async def tick(
        self,
        *,
        bot: Any = None,
        target_user_id: int = 0,
        presence_snapshot: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Một nhịp kiểm tra trạng thái quạt, ổ cắm BroadLink, vắng nhà/về nhà và giờ ngủ."""
        async with self._get_tick_lock():
            now_v = float(now_ts if now_ts is not None else time.time())
            settings = get_fan_settings()
            configured = bool(settings.get("fan_url") or settings.get("fan_mac") or self._resolved_url)

            has_plugs = False
            try:
                import smart_plugs
                plug_ctrl = smart_plugs.get_controller()
                has_plugs = bool(smart_plugs.load_plugs_config())
                if has_plugs:
                    await plug_ctrl.tick_timers(now_ts=now_v)
                    last_p_poll = float(getattr(self, "_last_plug_poll", 0.0) or 0.0)
                    if (now_v - last_p_poll) >= 60.0:
                        self._last_plug_poll = now_v
                        await plug_ctrl.refresh_all()
            except Exception:
                pass

            await self._tick_timers(now_v)

            if not configured and not has_plugs:
                return self.get_status_snapshot()

            # Làm mới trạng thái quạt live nếu chưa có hoặc đã quá 25s
            if configured and (self._last_state is None or (now_v - self._last_state_at) >= 25.0):
                await self.get_state(refresh=True, check_override=(not self._ramping_active))

            pres = presence_snapshot if presence_snapshot is not None else lan_presence.get_presence_snapshot()
            is_home = await self._evaluate_away_home(pres, settings, now_v)
            if is_home and settings.get("auto_enabled", True):
                await self._evaluate_sleep_checkin(bot, int(target_user_id or 0), settings, now_v)

            return self.get_status_snapshot()

    async def execute_ai_fan_action(self, ai_action: Any, *, event_source: str = "user_telegram") -> str:
        """Thực thi cấu trúc `fan_action` từ AI Pass 1 hoặc bộ bắt ý định tự nhiên."""
        if not isinstance(ai_action, dict):
            return ""
        act = str(ai_action.get("action") or "").strip().lower()
        if not act or act == "null":
            return ""

        now_ts = time.time()
        dur_raw = ai_action.get("duration_minutes")
        if dur_raw is None:
            dur_raw = ai_action.get("duration_mins")
        if dur_raw is None:
            dur_raw = ai_action.get("for_minutes")
        if dur_raw is None:
            dur_raw = ai_action.get("minutes")
        dur_mins: Optional[float] = None
        if dur_raw is not None and str(dur_raw).strip() != "":
            try:
                dur_v = float(dur_raw)
                if dur_v > 0:
                    dur_mins = min(1440.0, dur_v)
            except (TypeError, ValueError):
                dur_mins = None

        at_time_vn = (
            ai_action.get("at_time_vn")
            or ai_action.get("alarm_time_vn")
            or ai_action.get("start_time_vn")
            or ai_action.get("due_iso_vn")
        )
        delay_raw = (
            ai_action.get("delay_minutes")
            if ai_action.get("delay_minutes") is not None
            else ai_action.get("after_minutes")
        )
        has_explicit_start = bool(str(at_time_vn or "").strip()) or (
            delay_raw is not None and str(delay_raw).strip() != ""
        )

        if act in ("cancel_timer", "cancel_schedule"):
            removed = self.cancel_timer()
            if removed:
                return f"[Hệ thống Quạt: Đã hủy {removed} lịch hẹn giờ quạt.]"
            return "[Hệ thống Quạt: Quạt hiện chưa có lịch hẹn giờ nào đang chờ.]"

        if act in ("schedule_on", "schedule_off") or (
            act in ("power_on", "on", "power_off", "off", "set_speed", "speed") and has_explicit_start
        ):
            import diary_helper
            due_at = diary_helper.resolve_device_schedule_ts(
                at_time_vn=at_time_vn,
                delay_minutes=delay_raw,
                now_ts=now_ts,
            )
            if due_at is None and act == "schedule_off" and dur_mins is not None:
                due_at = now_ts + dur_mins * 60.0
                dur_mins = None
            if due_at is None:
                return "[Hệ thống Quạt: Chưa rõ mốc giờ hoặc số phút hẹn giờ quạt.]"

            due_str = datetime.datetime.fromtimestamp(due_at, tz=VN_TZ).strftime("%H:%M")
            max_pct = int(get_fan_settings().get("max_speed_pct", 40))
            pct_raw = ai_action.get("speed_pct")
            if pct_raw is None and ai_action.get("freq_hz") is not None:
                pct_raw = freq_to_speed_pct(ai_action.get("freq_hz"))

            if act in ("schedule_off", "power_off", "off"):
                self.schedule_sequence("off", due_at, now_ts=now_ts)
                return f"[Hệ thống Quạt: Đã hẹn {due_str} sẽ tắt quạt.]"

            step_action = "set_speed" if pct_raw is not None else "on"
            speed_pct = _clamp_int(pct_raw, min(20, max_pct), 0, max_pct) if pct_raw is not None else None
            steps = self.schedule_sequence(
                step_action,
                due_at,
                speed_pct=speed_pct,
                duration_minutes=dur_mins,
                now_ts=now_ts,
            )
            spd_note = f" ở mức {speed_pct}%" if speed_pct is not None else ""
            if dur_mins is not None and len(steps) >= 2:
                off_str = datetime.datetime.fromtimestamp(steps[-1]["due_at"], tz=VN_TZ).strftime("%H:%M")
                dur_lbl = f"{int(dur_mins)}" if float(dur_mins).is_integer() else f"{dur_mins:g}"
                return f"[Hệ thống Quạt: Đã hẹn {due_str} bật quạt{spd_note}, sau {dur_lbl} phút (lúc {off_str}) tự tắt.]"
            return f"[Hệ thống Quạt: Đã hẹn {due_str} sẽ bật quạt{spd_note}.]"

        if act == "status":
            cur = await self.get_state(refresh=True)
            if cur is None:
                return "[Hệ thống Quạt: Quạt ESP-01S hiện đang ngoại tuyến hoặc chưa cấu hình IP/MAC.]"
            on_txt = f"Đang bật {cur['speed_pct']}% ({cur['output_freq']} Hz), chế độ {cur['mode_label']}" if cur["is_on"] else f"Đang tắt (mức nhớ {cur['speed_pct']}%)"
            return f"[Hệ thống Quạt: {on_txt}.]"

        if act in ("power_on", "on"):
            res = await self.set_power(True, manual=True, reason="AI/Chat", event_source=event_source)
            if res is None:
                return "[Hệ thống Quạt: Không kết nối được quạt ESP-01S để bật.]"
            if dur_mins is not None:
                off_at = now_ts + dur_mins * 60.0
                self.schedule_auto_off(off_at, now_ts=now_ts)
                off_str = datetime.datetime.fromtimestamp(off_at, tz=VN_TZ).strftime("%H:%M")
                dur_lbl = f"{int(dur_mins)}" if float(dur_mins).is_integer() else f"{dur_mins:g}"
                return f"[Hệ thống Quạt: Đã bật quạt ({res['speed_pct']}% • {res['mode_label']}) và hẹn tự tắt sau {dur_lbl} phút (lúc {off_str}).]"
            return f"[Hệ thống Quạt: Đã bật quạt ({res['speed_pct']}% • {res['mode_label']}).]"

        if act in ("power_off", "off"):
            self._runtime["sleep_prompt_status"] = "ramped" if self._runtime.get("sleep_prompt_status") == "waiting" else self._runtime.get("sleep_prompt_status", "none")
            self._save_rt()
            res = await self.set_power(False, manual=True, reason="AI/Chat", event_source=event_source)
            if res is None:
                return "[Hệ thống Quạt: Không kết nối được quạt ESP-01S để tắt.]"
            return "[Hệ thống Quạt: Đã tắt quạt (giữ nguyên mức tốc độ cũ trong EEPROM).]"

        if act == "sleep_ramp":
            # Nếu user tự chủ động xin hạ đi ngủ -> hủy trạng thái chờ hỏi đêm nay
            self._runtime["sleep_prompt_status"] = "ramped"
            self._save_rt()
            ok = await self.start_sleep_ramp(target_pct=0, step_pct=1)
            if not ok:
                cur = await self.get_state(refresh=True)
                if cur is None:
                    return "[Hệ thống Quạt: Không kết nối được quạt ESP-01S.]"
                if not cur.get("is_on"):
                    return "[Hệ thống Quạt: Quạt đang tắt sẵn rồi.]"
                return "[Hệ thống Quạt: Quạt đã ở mức 0% (1005 Hz) rồi.]"
            return "[Hệ thống Quạt: Đang giảm tốc độ quạt từng 1% một xuống 0% (1005 Hz) cho anh dễ ngủ.]"

        if act in ("mode_natural", "natural"):
            res = await self.set_mode(1, manual=True)
            if res is None:
                return "[Hệ thống Quạt: Không đổi được chế độ quạt.]"
            return f"[Hệ thống Quạt: Đã chuyển sang chế độ Gió tự nhiên ({res['speed_pct']}%).]"

        if act in ("mode_fixed", "fixed"):
            res = await self.set_mode(0, manual=True)
            if res is None:
                return "[Hệ thống Quạt: Không đổi được chế độ quạt.]"
            return f"[Hệ thống Quạt: Đã chuyển về chế độ Gió cố định ({res['speed_pct']}%).]"

        if act in ("set_speed", "speed"):
            pct_raw = ai_action.get("speed_pct")
            if pct_raw is None and ai_action.get("freq_hz") is not None:
                pct_raw = freq_to_speed_pct(ai_action.get("freq_hz"))
            if pct_raw is None:
                return ""
            max_pct = int(get_fan_settings().get("max_speed_pct", 40))
            pct = _clamp_int(pct_raw, min(30, max_pct), 0, max_pct)
            cur = await self.get_state(refresh=True)
            if cur is not None and not cur.get("is_on"):
                await self.set_power(True, manual=True, reason="set_speed while off", event_source=event_source)
            res = await self.set_speed(pct, manual=True, event_source=event_source)
            if res is None:
                return "[Hệ thống Quạt: Không chỉnh được tốc độ quạt.]"
            if dur_mins is not None:
                off_at = now_ts + dur_mins * 60.0
                self.schedule_auto_off(off_at, now_ts=now_ts)
                off_str = datetime.datetime.fromtimestamp(off_at, tz=VN_TZ).strftime("%H:%M")
                dur_lbl = f"{int(dur_mins)}" if float(dur_mins).is_integer() else f"{dur_mins:g}"
                return f"[Hệ thống Quạt: Đã chỉnh tốc độ quạt về {res['speed_pct']}% ({res['output_freq']} Hz) và hẹn tự tắt sau {dur_lbl} phút (lúc {off_str}).]"
            return f"[Hệ thống Quạt: Đã chỉnh tốc độ quạt về {res['speed_pct']}% ({res['output_freq']} Hz).]"

        if act in ("speed_up", "speed_down"):
            step = _clamp_int(ai_action.get("step"), 10, 5, 50)
            max_pct = int(get_fan_settings().get("max_speed_pct", 40))
            cur = await self.get_state(refresh=True)
            if cur is None:
                return "[Hệ thống Quạt: Không đọc được trạng thái quạt hiện tại.]"
            if not cur.get("is_on") and act == "speed_up":
                cur = await self.set_power(True, manual=True, reason="speed_up while off", event_source=event_source) or cur
            delta = step if act == "speed_up" else -step
            target = _clamp_int(int(cur.get("speed_pct", 0)) + delta, 0, 0, max_pct)
            res = await self.set_speed(target, manual=True, event_source=event_source)
            if res is None:
                return "[Hệ thống Quạt: Không chỉnh được tốc độ quạt.]"
            return f"[Hệ thống Quạt: Đã {'tăng' if delta > 0 else 'giảm'} tốc độ quạt từ {cur['speed_pct']}% sang {res['speed_pct']}% ({res['output_freq']} Hz).]"

        return ""

    def build_sleep_prompt_context_note(self) -> str:
        """Ghi chú ngữ cảnh read-only cho Pass 1 khi vừa hỏi thăm giờ ngủ (01:00) và đang chờ trả lời."""
        if self._runtime.get("sleep_prompt_status") != "waiting":
            return ""
        return (
            "[NGỮ CẢNH HỎI GIỜ NGỦ QUẠT]: Đan vừa nhắn hỏi xem anh ngủ chưa để tự hạ quạt về 0% & tắt ổ cắm đi ngủ (auto_sleep). "
            "Nếu anh đồng ý đi ngủ/bảo hạ -> xuất `fan_action`: {\"action\": \"sleep_ramp\"}; "
            "nếu anh bảo chưa ngủ/đừng hạ/giữ nguyên -> để `fan_action`: null (hệ thống sẽ tự hủy hẹn hạ quạt đêm nay)."
        )

    async def reconcile_sleep_prompt_after_ai(self, text: str, fan_action: Any = None) -> str:
        """Đồng bộ trạng thái sleep_prompt sau khi Pass 1 đã đọc ngữ cảnh và quyết định `fan_action`."""
        rt = self._runtime
        if rt.get("sleep_prompt_status") != "waiting":
            return ""
        act = str((fan_action or {}).get("action") or "").strip().lower() if isinstance(fan_action, dict) else ""
        if act in ("sleep_ramp", "power_off", "off"):
            rt["sleep_prompt_status"] = "ramped"
            self._save_rt()
            self.publish_update()
            plugs_off: List[str] = []
            try:
                import smart_plugs
                plugs_off = await smart_plugs.get_controller().on_sleep_triggered()
            except Exception:
                pass
            if plugs_off:
                return f"[Hệ thống Ổ cắm giờ ngủ: Đã tự động tắt {', '.join(plugs_off)} cùng lúc đi ngủ.]"
            return ""

        reply_kind = classify_sleep_reply(text)
        if reply_kind == "awake" or act in ("power_on", "on", "set_speed", "speed", "speed_up"):
            rt["sleep_prompt_status"] = "cancelled"
            rt["manual_override_until"] = time.time() + MANUAL_OVERRIDE_COOLDOWN_SEC
            self._save_rt()
            self.publish_update()
            return "[Hệ thống Quạt: Anh xác nhận chưa ngủ / giữ nguyên -> đã hủy tự hạ quạt đêm nay.]"

        today_vn = datetime.datetime.now(VN_TZ).strftime("%Y-%m-%d")
        if rt.get("sleep_reask_date") != today_vn:
            rt["sleep_prompted_date"] = ""
            rt["sleep_prompt_status"] = "none"
            rt["sleep_reask_date"] = today_vn
        else:
            rt["sleep_prompt_status"] = "cancelled"
        self._save_rt()
        self.publish_update()
        return ""

    async def handle_natural_fan_intent(self, text: str) -> str:
        """
        Xử lý trả lời check-in đi ngủ HOẶC lệnh quạt trực tiếp khi anh nhắn Telegram.
        Trả về ghi chú hệ thống (extra_system_note) cho LLM Pass 1 & 2.
        """
        notes: List[str] = []
        rt = self._runtime
        cmd = detect_natural_fan_command(text)

        # 1) Nếu đang chờ anh trả lời tin nhắn hỏi đi ngủ -> ưu tiên xử lý phản hồi trước
        # Chỉ nhường lệnh trực tiếp nếu anh nói rõ lệnh nguồn/mức % cụ thể (vd "bật/tắt" hoặc "40%")
        is_explicit_specific_cmd = bool(cmd and cmd.get("action") in ("power_on", "power_off", "set_speed"))
        if rt.get("sleep_prompt_status") == "waiting" and not is_explicit_specific_cmd:
            reply_kind = classify_sleep_reply(text)
            if reply_kind == "awake":
                rt["sleep_prompt_status"] = "cancelled"
                rt["manual_override_until"] = time.time() + MANUAL_OVERRIDE_COOLDOWN_SEC
                self._save_rt()
                self.publish_update()
                notes.append("[Hệ thống Quạt: Chủ nhà báo chưa ngủ / muốn giữ nguyên quạt -> đã hủy tự hạ quạt đêm nay.]")
            elif reply_kind == "sleep" or (cmd and cmd.get("action") in ("speed_down", "sleep_ramp")):
                rt["sleep_prompt_status"] = "ramped"
                self._save_rt()
                self.publish_update()
                ok = await self.start_sleep_ramp(target_pct=0, step_pct=1)
                plugs_off: List[str] = []
                try:
                    import smart_plugs
                    plugs_off = await smart_plugs.get_controller().on_sleep_triggered()
                except Exception:
                    pass
                plug_msg = f" Đồng thời tắt {', '.join(plugs_off)}." if plugs_off else ""
                if ok:
                    notes.append(f"[Hệ thống Quạt: Chủ nhà xác nhận đi ngủ -> vừa bắt đầu giảm tốc quạt dần về 0% (1005 Hz).{plug_msg}]")
                else:
                    notes.append(f"[Hệ thống Quạt: Chủ nhà xác nhận đi ngủ (quạt hiện đã tắt hoặc ở mức 0%).{plug_msg}]")
                return "\n".join(notes)
            else:
                # Nhắn chuyện khác ngay sau lúc hỏi -> chủ nhà đang còn thức, dời 1 lần để 15p rảnh mới hỏi lại
                today_vn = datetime.datetime.now(VN_TZ).strftime("%Y-%m-%d")
                if rt.get("sleep_reask_date") != today_vn:
                    rt["sleep_prompted_date"] = ""
                    rt["sleep_prompt_status"] = "none"
                    rt["sleep_reask_date"] = today_vn
                else:
                    rt["sleep_prompt_status"] = "cancelled"
                self._save_rt()
                self.publish_update()

        # 2) Lệnh quạt trực tiếp
        if cmd:
            note = await self.execute_ai_fan_action(cmd)
            if note:
                notes.append(note)
        elif _FAN_KEYWORDS_RE.search(_strip_non_bldc_fan_mentions(text or "")):
            cur = await self.get_state(refresh=True)
            if cur is not None:
                on_txt = f"Đang bật {cur['speed_pct']}% ({cur['output_freq']} Hz), chế độ {cur['mode_label']}" if cur["is_on"] else f"Đang tắt (mức nhớ {cur['speed_pct']}%)"
                notes.append(f"[Trạng thái Quạt hiện tại: {on_txt}.]")

        return "\n".join(notes)


_CONTROLLER: Optional[FanController] = None


def get_controller() -> FanController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = FanController()
    return _CONTROLLER


async def fan_monitor_loop(bot: Any = None, target_user_id: int = 0) -> None:
    """
    Vòng lặp nền lắng nghe sự kiện `presence_update` (để bật lại quạt tức thì khi về nhà)
    và kiểm tra chu kỳ giờ ngủ / trạng thái quạt ESP-01S mỗi 30 giây.
    """
    ctrl = get_controller()
    q = lan_events.subscribe()
    logger.info("🌬️ Khởi động vòng theo dõi Quạt BLDC ESP-01S...")
    try:
        while True:
            latest_pres: Optional[Dict[str, Any]] = None
            try:
                ev = await asyncio.wait_for(q.get(), timeout=MONITOR_POLL_INTERVAL_SEC)
                if isinstance(ev, dict) and (ev.get("type") or ev.get("event")) == "presence_update":
                    data = ev.get("data")
                    if isinstance(data, dict):
                        latest_pres = data
                # Rút bớt event dồn nếu có nhiều cái liên tiếp
                while not q.empty():
                    extra = q.get_nowait()
                    if isinstance(extra, dict) and (extra.get("type") or extra.get("event")) == "presence_update" and isinstance(extra.get("data"), dict):
                        latest_pres = extra["data"]
            except asyncio.TimeoutError:
                pass

            try:
                uid = int(target_user_id or 0)
                if uid <= 0:
                    try:
                        uid = int(database.get_primary_user_id() or 0)
                    except Exception:
                        uid = 0
                await ctrl.tick(bot=bot, target_user_id=uid, presence_snapshot=latest_pres)
            except Exception as e:
                logger.warning(f"Lỗi trong fan_monitor_loop: {e}")
    except asyncio.CancelledError:
        pass
    finally:
        lan_events.unsubscribe(q)
        await ctrl.close()
