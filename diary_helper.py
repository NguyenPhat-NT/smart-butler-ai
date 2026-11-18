# -*- coding: utf-8 -*-
import re
import time
import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

import config
import database
import persona
import trend_helper

logger = logging.getLogger(__name__)
VN_TZ = datetime.timezone(datetime.timedelta(hours=config.TIMEZONE_OFFSET_HOURS))


async def generate_daily_diary(date_str: Optional[str] = None, force: bool = False) -> str:
    """Tạo bản tóm tắt lịch sinh hoạt / trợ lý trong ngày (Daily Briefing Context)."""
    if not date_str:
        date_str = trend_helper.get_today_date_str()
    if not force:
        existing = database.get_today_diary(date_str)
        if existing:
            return existing

    today_trends = database.get_today_trends(date_str) or ""
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    vn_time = datetime.datetime.now(VN_TZ)
    weekday_names = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    weekday_str = weekday_names[vn_time.weekday()]

    brief_text = (
        f"- Sáng ({weekday_str} {date_str}): {b_name} trực sẵn sàng kiểm tra phòng, thời tiết và lịch nhắc cho {o_name}.\n"
        f"- Trưa: Theo dõi nhiệt độ phòng, nhắc nghỉ ngơi nếu làm việc liên tục.\n"
        f"- Chiều: Hỗ trợ công việc, phát nhạc tập trung hoặc điều chỉnh quạt/ánh sáng theo nhu cầu.\n"
        f"- Tối: Chuyển chế độ thư giãn, kiểm tra các thiết bị trước khi đi ngủ.\n"
    )
    if today_trends:
        brief_text += f"Tin & thời tiết hôm nay:\n{today_trends}"
    database.save_today_diary(date_str, brief_text.strip())
    return brief_text.strip()


async def get_or_create_today_diary() -> str:
    today_str = trend_helper.get_today_date_str()
    diary = database.get_today_diary(today_str)
    if not diary:
        diary = await generate_daily_diary(today_str)
    return diary


def parse_ts_float(raw_ts: Any) -> Optional[float]:
    if raw_ts is None or raw_ts == "":
        return None
    try:
        if isinstance(raw_ts, (int, float)):
            val = float(raw_ts)
            return val if val > 0 else None
        s = str(raw_ts).strip()
        if not s:
            return None
        try:
            val = float(s)
            return val if val > 0 else None
        except ValueError:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError, OSError):
        return None


def format_vn_duration(seconds: float) -> str:
    total_mins = max(1, int(round(abs(seconds) / 60.0)))
    if total_mins < 60:
        return f"{total_mins} phút"
    hrs, mins = divmod(total_mins, 60)
    if hrs >= 24:
        days, rem_h = divmod(hrs, 24)
        return f"{days} ngày {rem_h} giờ" if rem_h else f"{days} ngày"
    return f"{hrs} giờ {mins} phút" if mins else f"{hrs} giờ"


def format_relative_vn_ts(ts_val: Any, now_ts: Optional[float] = None) -> str:
    parsed = parse_ts_float(ts_val)
    if not parsed:
        return ""
    now_v = float(now_ts if now_ts is not None else time.time())
    vn_dt = datetime.datetime.fromtimestamp(parsed, tz=VN_TZ)
    vn_now = datetime.datetime.fromtimestamp(now_v, tz=VN_TZ)
    day_diff = (vn_now.date() - vn_dt.date()).days
    hm = vn_dt.strftime("%H:%M")
    if day_diff == 0:
        return f"Hôm nay {hm}"
    if day_diff == 1:
        return f"Hôm qua {hm}"
    return vn_dt.strftime("%d/%m %H:%M")


def get_vietnam_time_context(
    last_user_time: float = 0.0,
    now_ts: Optional[float] = None,
) -> dict:
    now_val = float(now_ts if now_ts is not None else time.time())
    vn_now = datetime.datetime.fromtimestamp(now_val, tz=VN_TZ)
    owner = persona.get_persona_config()["owner_name"]

    hour = vn_now.hour
    minute = vn_now.minute
    mins_of_day = hour * 60 + minute
    time_str = vn_now.strftime("%H:%M")
    date_str = vn_now.strftime("%d/%m/%Y")
    iso_date = vn_now.strftime("%Y-%m-%d")

    weekday_names = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    weekday_str = weekday_names[vn_now.weekday()]

    if mins_of_day < 300:
        session, session_key, daypart_note = "đêm khuya", "Khuya", "Đang đêm khuya / rạng sáng (CHƯA phải ban ngày)."
    elif mins_of_day < 510:
        session, session_key, daypart_note = "sáng sớm", "Sáng", "Đang sáng sớm (CHƯA tới trưa)."
    elif mins_of_day < 690:
        session, session_key, daypart_note = "buổi sáng", "Sáng", "Còn đang buổi sáng (CHƯA tới trưa)."
    elif mins_of_day < 750:
        session, session_key, daypart_note = "buổi trưa", "Trưa", "Đang tầm trưa (chưa quá trưa)."
    elif mins_of_day < 810:
        session, session_key, daypart_note = "buổi trưa", "Trưa", "Đang giờ nghỉ trưa / xế trưa."
    elif mins_of_day < 1080:
        session, session_key, daypart_note = "buổi chiều", "Chiều", "Đang buổi chiều."
    elif mins_of_day < 1320:
        session, session_key, daypart_note = "buổi tối", "Tối", "Đang buổi tối."
    else:
        session, session_key, daypart_note = "tối muộn", "Tối", "Đang tối muộn sắp khuya."

    gap_desc = "Đang trong luồng trao đổi"
    if last_user_time and float(last_user_time) > 0:
        last_ts_f = float(last_user_time)
        gap_sec = max(0.0, now_val - last_ts_f)
        if gap_sec < 120:
            gap_desc = "Đang trao đổi liên tục"
        elif gap_sec < 3600:
            gap_desc = f"{owner} vừa nhắn lại sau khoảng {int(gap_sec // 60)} phút"
        elif gap_sec < 86400:
            last_dt = datetime.datetime.fromtimestamp(last_ts_f, tz=VN_TZ)
            dur_txt = format_vn_duration(gap_sec)
            crossed_night = (last_dt.hour >= 22 or last_dt.hour < 6) and (5 <= hour < 13)
            if crossed_night:
                gap_desc = f"Đã khoảng {dur_txt} từ lúc nghỉ/nhắn lần cuối lúc {last_dt.strftime('%H:%M')} (qua đêm)"
            else:
                gap_desc = f"Đã khoảng {dur_txt} kể từ tin nhắn trước (lúc {last_dt.strftime('%H:%M')})"
        else:
            gap_desc = f"Đã hơn {int(gap_sec // 86400)} ngày kể từ lần nhắn trước"

    return {
        "time_str": time_str,
        "date_str": date_str,
        "iso_date": iso_date,
        "weekday_str": weekday_str,
        "session": session,
        "session_key": session_key,
        "daypart_note": daypart_note,
        "gap_desc": gap_desc,
        "hour": hour,
        "minute": minute,
        "month": vn_now.month,
        "day": vn_now.day,
        "year": vn_now.year,
        "minutes_of_day": mins_of_day,
        "now_ts": now_val,
    }


def extract_current_session_activity(diary_text: str, session_key: str) -> str:
    if not diary_text or session_key == "Khuya":
        return ""
    for line in diary_text.strip().split("\n"):
        clean = line.strip().lstrip("-* ").lower()
        if session_key.lower() in clean[:15]:
            parts = line.split(":", 1)
            return parts[1].strip() if len(parts) > 1 else line.strip()
    return ""


def format_authoritative_time_banner(
    time_context: Optional[Dict[str, Any]] = None,
    now_ts: Optional[float] = None,
) -> str:
    ctx = time_context or get_vietnam_time_context(now_ts=now_ts)
    return (
        "=== ĐỒNG HỒ THỰC TẾ HIỆN TẠI (SỰ THẬT TUYỆT ĐỐI) ===\n"
        f"- Bây giờ là: {ctx.get('time_str', '')} ({ctx.get('session', '')}), "
        f"{ctx.get('weekday_str', '')} ngày {ctx.get('date_str', '')}.\n"
        f"- Mốc thời điểm: {ctx.get('daypart_note', '')}"
    )


_CLOCK_HM_RE = re.compile(
    r"(?<![\d/:-])(\d{1,2})\s*(?::|h|giờ)\s*(rưỡi|\d{1,2})?(?:\s*(?:phút|p))?(?:\s*(sáng|trưa|chiều|tối|đêm|khuya|am|pm))?\b",
    re.IGNORECASE,
)
_DURATION_FOLLOW_RE = re.compile(r"^\s*(?:tiếng|đồng\s*hồ|nữa|trước|qua|liền|liên\s*tục)\b", re.IGNORECASE)


def parse_vn_clock_to_ts(
    raw: Any,
    now_ts: Optional[float] = None,
    roll_forward_if_past: bool = True,
) -> Optional[float]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() in ("null", "none"):
        return None
    base_now = float(now_ts if now_ts is not None else time.time())
    vn_now = datetime.datetime.fromtimestamp(base_now, tz=VN_TZ)

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt_vn = datetime.datetime.strptime(text[:19], fmt).replace(tzinfo=VN_TZ)
            return dt_vn.timestamp()
        except ValueError:
            continue

    m_vn = re.match(
        r"^\s*(\d{1,2})\s*(?::|h|giờ)\s*(rưỡi|\d{1,2})?(?:\s*(?:phút|p))?(?:\s*(sáng|trưa|chiều|tối|đêm|khuya|am|pm))?\s*$",
        text,
        re.IGNORECASE,
    )
    if not m_vn:
        return None
    hh = int(m_vn.group(1))
    mm_raw = (m_vn.group(2) or "").lower()
    mm = 30 if mm_raw == "rưỡi" else (int(mm_raw) if mm_raw.isdigit() else 0)
    period = (m_vn.group(3) or "").lower()
    if period in ("chiều", "tối", "pm") and 1 <= hh <= 11:
        hh += 12
    elif period in ("đêm", "khuya", "am") and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    candidate_vn = vn_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if roll_forward_if_past and candidate_vn <= vn_now - datetime.timedelta(minutes=2):
        candidate_vn += datetime.timedelta(days=1)
    return candidate_vn.timestamp()


def extract_vn_clock_candidates(
    text: str,
    ref_dt: datetime.datetime,
    assume_same_day: bool = False,
) -> List[Tuple[datetime.datetime, str]]:
    if not text or not isinstance(text, str):
        return []
    found: List[Tuple[datetime.datetime, str]] = []
    seen = set()
    for m in _CLOCK_HM_RE.finditer(text):
        tail = text[m.end(): m.end() + 24]
        if _DURATION_FOLLOW_RE.match(tail):
            continue
        sep_match = m.group(0).lower()
        if not ((":" in sep_match) or ("h" in sep_match) or ("giờ" in sep_match)):
            continue
        hh = int(m.group(1))
        mm_raw = (m.group(2) or "").lower()
        mm = 30 if mm_raw == "rưỡi" else (int(mm_raw) if mm_raw.isdigit() else 0)
        period = (m.group(3) or "").lower()
        if mm >= 60 or hh > 23:
            continue
        raw_token = m.group(0).strip()
        prefix_ctx = text[max(0, m.start() - 28): m.start()].lower()
        if any(k in prefix_ctx for k in ("hôm qua", "tối qua", "sáng qua", "hồi ", "lúc nãy ", "hôm bữa ", "hôm trước ")):
            continue

        explicit_24h = (hh >= 13) or bool(period) or (":" in sep_match and len(m.group(1)) == 2 and hh == 0)
        if period in ("chiều", "tối", "pm") and 1 <= hh <= 11:
            hh += 12
        elif period in ("đêm", "khuya", "am") and hh == 12:
            hh = 0
        elif not explicit_24h:
            if ref_dt.hour < 13 and 5 <= hh <= 12:
                pass
            elif ref_dt.hour >= 13 and 13 <= hh <= 23:
                pass
            else:
                continue

        target_dt = ref_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if any(k in prefix_ctx for k in ("ngày mai", "mai ", "sáng mai", "trưa mai", "chiều mai", "tối mai")):
            if not (assume_same_day and ref_dt.hour < 5 and 5 <= hh <= 13):
                target_dt += datetime.timedelta(days=1)

        key = target_dt.strftime("%Y-%m-%d %H:%M")
        if key not in seen:
            seen.add(key)
            found.append((target_dt, raw_token))
    return found


def build_temporal_comparison_hints(
    user_text: str = "",
    now_ts: Optional[float] = None,
    pending_reminders: Optional[List[Dict[str, Any]]] = None,
    open_loops: Optional[List[Dict[str, Any]]] = None,
) -> str:
    now_val = float(now_ts if now_ts is not None else time.time())
    vn_now = datetime.datetime.fromtimestamp(now_val, tz=VN_TZ)
    now_hm = vn_now.strftime("%H:%M")

    comparisons: List[str] = []
    seen_targets = set()

    def _add_cmp(target_ts_val: float, label: str = ""):
        target_dt = datetime.datetime.fromtimestamp(target_ts_val, tz=VN_TZ)
        day_diff = (target_dt.date() - vn_now.date()).days
        if abs(day_diff) > 1:
            return
        t_key = target_dt.strftime("%Y-%m-%d %H:%M")
        if t_key in seen_targets:
            return
        seen_targets.add(t_key)
        target_hm = target_dt.strftime("%H:%M")
        day_prefix = "ngày mai " if day_diff == 1 else ("hôm qua " if day_diff == -1 else "")
        diff_sec = target_ts_val - now_val
        diff_mins = diff_sec / 60.0
        lbl = f" ({label})" if label else ""
        if diff_mins > 2.0:
            comparisons.append(
                f"- ĐỐI CHIẾU THỜI GIAN{lbl}: Hiện tại mới là {now_hm} -> CHƯA TỚI {day_prefix}{target_hm} "
                f"(còn {format_vn_duration(diff_sec)} nữa mới tới {day_prefix}{target_hm})."
            )
        elif -2.0 <= diff_mins <= 2.0:
            comparisons.append(f"- ĐỐI CHIẾU THỜI GIAN{lbl}: Hiện tại là {now_hm} -> ĐANG ĐÚNG MỐC {day_prefix}{target_hm}.")
        else:
            comparisons.append(
                f"- ĐỐI CHIẾU THỜI GIAN{lbl}: Hiện tại là {now_hm} -> ĐÃ QUA mốc {day_prefix}{target_hm} được {format_vn_duration(-diff_sec)}."
            )

    for dt_cand, _ in extract_vn_clock_candidates(user_text or "", vn_now):
        _add_cmp(dt_cand.timestamp(), label="mốc giờ vừa nhắc")

    for loop in (open_loops or [])[:3]:
        t_ts = loop.get("target_ts")
        if t_ts and float(t_ts) > 0:
            _add_cmp(float(t_ts), label=f"mạch '{loop.get('topic', '')}'")
        else:
            created = parse_ts_float(loop.get("created_at"))
            if created:
                c_dt = datetime.datetime.fromtimestamp(created, tz=VN_TZ)
                if (vn_now.date() - c_dt.date()).days <= 1:
                    combined = f"{loop.get('topic', '')} {loop.get('note', '')}"
                    for dt_cand, _ in extract_vn_clock_candidates(combined, c_dt, assume_same_day=(c_dt.hour < 5)):
                        _add_cmp(dt_cand.timestamp(), label=f"mạch '{loop.get('topic', '')}'")

    for rem in (pending_reminders or [])[:4]:
        due_v = parse_ts_float(rem.get("due_at"))
        if due_v:
            _add_cmp(due_v, label=f"nhắc hẹn #{rem.get('id', '')}")

    if not comparisons:
        return ""
    comparisons.append(
        f"- QUY TẮC SỰ THẬT ĐỒNG HỒ: Đồng hồ thực tế đang chỉ đúng {now_hm}; "
        f"TUYỆT ĐỐI KHÔNG khẳng định 'đã quá' một mốc giờ khi phía trên ghi CHƯA TỚI."
    )
    return "=== KIỂM CHỨNG TOÁN HỌC THỜI GIAN (CHÍNH XÁC 100%) ===\n" + "\n".join(comparisons)


def build_situational_mood_context(
    current_mood: str,
    mood_updated_at: float = 0.0,
    last_user_time: float = 0.0,
    today_trends: str = "",
    now_ts: Optional[float] = None,
) -> str:
    now_val = float(now_ts if now_ts is not None else time.time())
    vn_now = datetime.datetime.fromtimestamp(now_val, tz=VN_TZ)
    hhmm = vn_now.hour + vn_now.minute / 60.0
    cues: List[str] = []
    if 6.0 <= hhmm < 9.0:
        cues.append("Buổi sáng mới bắt đầu: ưu tiên gọn gàng, hỗ trợ khởi động ngày mới hiệu quả.")
    elif hhmm >= 23.0 or hhmm < 5.0:
        cues.append("Khung giờ khuya: giữ câu chữ dịu nhẹ, súc tích, không làm phiền.")
    if "mưa" in (today_trends or "").lower():
        cues.append("Thời tiết hôm nay có thể có mưa, nhắc nhẹ mang áo mưa nếu ra ngoài.")
    return " ".join(cues) if cues else "Trạng thái vận hành ổn định, sẵn sàng hỗ trợ."
