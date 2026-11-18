# -*- coding: utf-8 -*-
"""
Phát hiện và lên lịch tự động cho các lời hứa / dặn dò "làm xong X thì nhắn lại"
(Deferred Check-Ins & AI Promises), giúp Smart Butler luôn nhớ giữ lời nhắn lại
sau khi ăn, tắm, về tới nhà, làm bài, v.v. kể cả khi Pass 1 quên xuất reminder.
"""
import datetime
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

_SENTENCE_SPLIT_RE = re.compile(r"(?:\r?\n|[.!?\u2026]+|(?<=\w),\s+(?=(?:chờ|đợi|tầm|khoảng|chừng|tí|tý|lát|chút|ăn|tắm|về|làm|học|ngủ|nấu|dọn|rửa|em|đan)))", re.IGNORECASE)

_NEGATION_BEFORE_ACTION_RE = re.compile(
    r"(?:không|ko|k|chưa|chẳng|đừng|khỏi|miễn|thôi\s+khỏi|ngại|lười)\s+"
    r"(?:có\s+)?(?:nhắn|ib|inbox|báo|báo\s*cáo|kể|gửi|gởi|gọi|call|alo|rep|trả\s*lời)",
    re.IGNORECASE,
)

_PAST_OR_REPROACH_RE = re.compile(
    r"(?:"
    r"\bsao\s+(?:không|ko|k|chưa)\b"
    r"|\bmà\s+(?:không|ko|k|chưa)\s+(?:thấy\s+)?(?:nhắn|báo|gọi|rep)\b"
    r"|\bquên\s+(?:nhắn|báo|gọi|gửi)\b"
    r"|\bhôm\s+qua\b"
    r"|\bhồi\s+(?:nãy|sáng|trưa|chiều|tối|khuya)\b"
    r"|\bnãy\s+giờ\b"
    r")",
    re.IGNORECASE,
)

_GOODNIGHT_BANTER_RE = re.compile(
    r"(?:\bngủ\s+ngon\b|\bchúc\s+ngủ\s+ngon\b|\bbai\s+bai\b|\bdội\s+bom\b|\bnếu\s+anh\s+(?:không|ko|k)\b)",
    re.IGNORECASE,
)

_USER_SUBJECT_BACK_TO_AI_RE = re.compile(
    r"\b(?:anh|anh\s+iu|anh\s+yêu|mình)\s+(?:sẽ\s+|mới\s+|rồi\s+)?(?:nhắn|ib|inbox|báo|gọi|call|alo)\s+(?:lại\s+)?(?:cho\s+)?(?:em|đan|bé)\b",
    re.IGNORECASE,
)

# --- Các mẫu Chủ nhà dặn trợ lý nhắn/báo lại sau khi làm xong hoặc khi rảnh ---
_TRIGGER_EVENT = (
    r"(?:xong|tới\s+(?:nhà|nơi|chỗ|phòng)|đến\s+(?:nhà|nơi)|về\s+(?:tới\s+)?(?:nhà|phòng)"
    r"|(?:ngủ\s+|thức\s+|mai\s+|sáng\s+)?dậy|tỉnh(?:\s+dậy)?|rảnh)"
)
_CALLBACK_VERB = r"(?:nhắn(?:\s+tin)?|ib|inbox|báo(?:\s+cáo)?|kể|gửi|gởi|gọi(?:\s+điện)?|call|alo|rep|trả\s+lời)"

_CALLBACK_TARGET_BOT_RE = re.compile(
    rf"{_CALLBACK_VERB}\s*(?:lại\s+|liền\s+|ngay\s+|nha\s+|nhé\s+)?(?:cho\s+)?(?:em|đan|bé)\b",
    re.IGNORECASE,
)

_BOT_DECLINE_OR_NEGATE_RE = re.compile(
    r"(?:"
    r"(?:không|ko|k|chưa|chẳng|đừng|khỏi|miễn|ngại|lười)\s+(?:có\s+|thể\s+|hứa\s+)?"
    r"(?:nhắn|ib|inbox|báo|báo\s*cáo|kể|gửi|gởi|gọi|call|alo|rep|trả\s*lời)"
    r"|\b(?:không\s+hứa(?:\s+trước)?|chưa\s+chắc|bận\s+rồi|không\s+rảnh|chưa\s+rảnh|khó\s+(?:nhắn|gọi))\b"
    r"|\bchưa\s+(?:ăn|tắm|làm|học|về|ngủ)\s+xong\s+đâu\b"
    r")",
    re.IGNORECASE,
)

_USER_ASK_PATTERNS = (
    re.compile(
        rf"{_TRIGGER_EVENT}"
        rf"(?:\s+(?:rồi|nhớ|hãy|thì|là|nha|nhé)){{0,3}}[^.!?\n]{{0,35}}?"
        rf"(?:nhớ\s+)?{_CALLBACK_VERB}\s*(?:lại\s+|liền\s+|ngay\s+|giùm\s+|hộ\s+)?(?:cho\s+)?(?:anh(?:\s+iu|\s+người\s+yêu)?|mình)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:khi\s+nào|lúc\s+nào|bao\s+giờ|chừng\s+nào)[^.!?\n]{{0,35}}?"
        rf"(?:xong|tới\s+(?:nhà|nơi)|đến\s+(?:nhà|nơi)|rảnh)[^.!?\n]{{0,25}}?"
        rf"(?:nhớ\s+)?{_CALLBACK_VERB}\s*(?:lại\s+)?(?:cho\s+)?(?:anh(?:\s+iu|\s+người\s+yêu)?|mình)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:nhớ\s+)?{_CALLBACK_VERB}\s*(?:lại\s+)?(?:cho\s+)?(?:anh(?:\s+iu|\s+người\s+yêu)?|mình)\s+"
        rf"(?:sau\s+khi|khi|lúc)\s+(?:em\s+|đan\s+)?[^.!?\n]{{1,35}}?(?:xong|tới\s+(?:nhà|nơi)|về\s+nhà|rảnh)\b",
        re.IGNORECASE,
    ),
    # "Em đi ăn đi rồi nhắn anh nha", "Tắm đi rồi gọi anh nhé"
    re.compile(
        rf"\b(?:ăn|tắm|gội|dọn|nấu|học|làm|nghỉ|về)[^.!?\n]{{0,22}}?\b(?:đi|đã|rồi)\s*(?:rồi\s+)?(?:nhớ\s+)?"
        rf"{_CALLBACK_VERB}\s*(?:lại\s+|liền\s+|ngay\s+)?(?:cho\s+)?(?:anh(?:\s+iu|\s+người\s+yêu)?|mình)\b",
        re.IGNORECASE,
    ),
)

# --- Các mẫu Đan tự hứa/hẹn sẽ nhắn/báo lại sau ---
_BOT_SUBJECT = r"(?:em|đan)"
_BOT_PROMISE_PATTERNS = (
    # "Ăn xong báo cáo ngay cho anh nha" (không cần lặp chữ em nếu hướng rõ tới anh)
    re.compile(
        rf"{_TRIGGER_EVENT}"
        rf"(?:\s+(?:cái|rồi|là|thì|nè|nha|nhé|đã)){{0,3}}[^.!?\n]{{0,24}}?"
        rf"(?:sẽ\s+|mới\s+|lại\s+)?{_CALLBACK_VERB}\s*"
        rf"(?:lại\s+|liền\s+|ngay\s+|lập\s+tức\s+)?(?:cho\s+anh|anh\s+(?:nha|nhé|liền|ngay|yên\s+tâm))\b",
        re.IGNORECASE,
    ),
    # "Ăn xong em nhắn / em báo cáo", "Mai dậy em nhắn anh liền" (có rõ chủ ngữ em/Đan trước động từ)
    re.compile(
        rf"{_TRIGGER_EVENT}"
        rf"(?:\s+(?:cái|rồi|là|thì|nè|nha|nhé|đã)){{0,3}}[^.!?\n]{{0,24}}?"
        rf"\b{_BOT_SUBJECT}\b\s+(?:sẽ\s+|mới\s+|lại\s+)?{_CALLBACK_VERB}\b",
        re.IGNORECASE,
    ),
    # "Em đi ăn cơm cái đã rồi em nhắn lại", "Tắm xíu rồi em gọi anh"
    re.compile(
        rf"(?:"
        rf"\b{_BOT_SUBJECT}\b\s+(?:đi\s+|cứ\s+|phải\s+|vào\s+|ra\s+|lên\s+|phụ\s+)?(?:ăn|tắm|gội|nấu|dọn\s+cơm|rửa\s+chén|rửa\s+bát|làm\s+bài|học|về\s+nhà|phụ\s+mẹ)"
        rf"[^.!?\n]{{0,32}}?(?:cái\s+đã|đã|xíu|chút|tý|tí|lát|rồi)\s*(?:rồi|thì|là)?\s*(?:\b{_BOT_SUBJECT}\b\s*)?(?:sẽ\s+|lại\s+)?{_CALLBACK_VERB}\b"
        rf"|"
        rf"\b(?:ăn|tắm|gội|nấu|dọn\s+cơm|rửa\s+chén|rửa\s+bát|làm\s+bài|học|về\s+nhà|phụ\s+mẹ)[^.!?\n]{{0,28}}?"
        rf"(?:cái\s+đã|đã|xíu|chút|tý|tí|lát)\s*(?:rồi|thì|là)?\s*\b{_BOT_SUBJECT}\b\s+(?:sẽ\s+|lại\s+)?{_CALLBACK_VERB}\b"
        rf")",
        re.IGNORECASE,
    ),
    # "Tầm 20 phút nữa em nhắn lại", "Chút nữa/Lát nữa/Lát em báo anh"
    re.compile(
        rf"(?:(?:tầm|khoảng|chừng|cỡ|độ)\s+)?(?:\d+|nửa|một|hai|ba|vài)\s*(?:phút|p|tiếng|giờ)\s+nữa"
        rf"[^.!?\n]{{0,24}}?\b{_BOT_SUBJECT}\b\s*(?:sẽ\s+)?{_CALLBACK_VERB}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:tí(?:\s+nữa)?|tý(?:\s+nữa)?|lát(?:\s+nữa)?|chút(?:\s+nữa)?|xíu\s+nữa)\b"
        rf"[^.!?\n]{{0,24}}?\b{_BOT_SUBJECT}\b\s*(?:sẽ\s+)?{_CALLBACK_VERB}\b",
        re.IGNORECASE,
    ),
    # "Chờ em xíu (rồi) em nhắn/báo"
    re.compile(
        rf"(?:chờ|đợi)\s+{_BOT_SUBJECT}\s+(?:xíu|chút|tí|tý|lát|\d+\s*(?:phút|p))"
        rf"[^.!?\n]{{0,45}}?(?:\b{_BOT_SUBJECT}\b\s*)?(?:sẽ\s+)?{_CALLBACK_VERB}\b",
        re.IGNORECASE,
    ),
)

_EXPLICIT_MINUTES_RE = re.compile(
    r"(?:(?:tầm|khoảng|chừng|cỡ|độ|đợi|chờ|sau)\s+)?(\d{1,3})\s*(?:phút|phut|p)\b(?:\s*nữa)?",
    re.IGNORECASE,
)
_EXPLICIT_HOURS_RE = re.compile(
    r"(?:(?:tầm|khoảng|chừng|cỡ|độ|sau)\s+)?(nửa|1|2|3|4|một|hai|ba|\d+(?:[.,]\d+)?)\s*(?:tiếng|giờ)\s*nữa\b",
    re.IGNORECASE,
)

# Bản đồ nhóm hoạt động -> (tên chủ đề chuẩn, từ khóa hoàn thành, min_mins, max_mins)
_ACTIVITY_PROFILES: Tuple[Tuple[Tuple[str, ...], str, Tuple[str, ...], int, int], ...] = (
    (
        ("ăn", "cơm", "bữa trưa", "bữa tối", "bữa sáng", "bún", "phở", "mì", "nấu", "dọn cơm"),
        "sau khi ăn xong",
        ("ăn", "cơm", "trưa", "tối", "sáng", "no"),
        28,
        38,
    ),
    (
        ("tắm", "gội", "tắm rửa"),
        "sau khi tắm xong",
        ("tắm", "gội"),
        22,
        30,
    ),
    (
        ("rửa chén", "rửa bát", "dọn dẹp", "phơi đồ", "quét nhà", "lau nhà", "phụ mẹ"),
        "sau khi làm việc nhà xong",
        ("rửa chén", "rửa bát", "dọn", "phụ mẹ", "phơi"),
        20,
        30,
    ),
    (
        ("về tới nhà", "tới nhà", "tới nơi", "đến nơi", "chạy về", "về phòng", "về tới"),
        "khi về tới nơi",
        ("về tới", "tới nhà", "tới nơi", "đến nơi", "về phòng", "về rồi"),
        25,
        40,
    ),
    (
        ("học", "làm bài", "làm việc", "code", "họp", "thi", "kiểm tra", "thiết kế", "figma", "cày", "đọc"),
        "sau khi làm xong việc",
        ("học", "làm bài", "làm việc", "xong việc", "họp", "figma", "layout"),
        40,
        55,
    ),
    (
        ("xem", "coi", "nghe xong"),
        "sau khi xem xong",
        ("xem", "coi", "nghe"),
        20,
        32,
    ),
    (
        ("xíu", "chút", "tí", "tý", "lấy đồ", "pha trà", "pha nước", "rửa mặt", "thay đồ"),
        "sau vài phút nữa",
        ("xong",),
        10,
        15,
    ),
)

_DONE_MARKER_RE = re.compile(
    r"(?:vừa|mới|đã|cuối\s+cùng\s+cũng)[^.!?\n]{0,20}(?:xong\s+(?:rồi|rùi|nè|nha|nhé|đây|ạ)|về\s+(?:tới|đến)\s+(?:nhà|nơi)\s+(?:rồi|rùi|nè)|tới\s+(?:nhà|nơi)\s+(?:rồi|rùi|nè))"
    r"|(?:xong\s+(?:rồi|rùi)\s+(?:nè|nha|đây|anh))",
    re.IGNORECASE,
)


def _split_clauses(text: str) -> List[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    return [c.strip() for c in _SENTENCE_SPLIT_RE.split(clean) if c and c.strip()]


def _clause_looks_like_goodnight(clause: str, full_text: str) -> bool:
    if _GOODNIGHT_BANTER_RE.search(clause):
        return True
    if _GOODNIGHT_BANTER_RE.search(full_text) and not any(
        w in clause.lower() for w in ("ăn", "tắm", "gội", "về", "học", "làm", "phút", "mai dậy", "sáng mai")
    ):
        return True
    return False


def _find_matching_user_clause(user_text: str) -> Optional[str]:
    for clause in _split_clauses(user_text):
        if _NEGATION_BEFORE_ACTION_RE.search(clause):
            continue
        if _USER_SUBJECT_BACK_TO_AI_RE.search(clause):
            continue
        if _PAST_OR_REPROACH_RE.search(clause):
            continue
        for pat in _USER_ASK_PATTERNS:
            if pat.search(clause):
                return clause
    return None


def _find_matching_bot_clause(bot_text: str) -> Optional[str]:
    for clause in _split_clauses(bot_text):
        if _NEGATION_BEFORE_ACTION_RE.search(clause):
            continue
        if _CALLBACK_TARGET_BOT_RE.search(clause) and not re.search(
            rf"{_CALLBACK_VERB}\s*(?:lại\s+|liền\s+|ngay\s+)?(?:cho\s+)?anh\b",
            clause,
            re.IGNORECASE,
        ):
            continue
        if _clause_looks_like_goodnight(clause, bot_text):
            continue
        for pat in _BOT_PROMISE_PATTERNS:
            if pat.search(clause):
                return clause
    return None


def has_deferred_promise_intent(user_text: str = "", bot_text: str = "") -> bool:
    """True nếu anh vừa dặn Đan nhắn lại sau một việc HOẶC Đan vừa tự hẹn nhắn lại."""
    return bool(_find_matching_bot_clause(bot_text) or _find_matching_user_clause(user_text))


def _detect_activity_profile(combined_text: str) -> Tuple[str, Tuple[str, ...], int, int]:
    low = combined_text.lower()
    for triggers, topic_label, kw_tuple, min_m, max_m in _ACTIVITY_PROFILES:
        if any(tok in low for tok in triggers):
            return topic_label, kw_tuple, min_m, max_m
    return "khi xong việc", ("xong",), 22, 32


def _parse_relative_due_ts(
    clause_text: str,
    full_text: str,
    now_ts: float,
    min_default_mins: int,
    max_default_mins: int,
    rng: random.Random,
) -> float:
    low_clause = clause_text.lower()
    low_full = full_text.lower()
    vn_now = datetime.datetime.fromtimestamp(now_ts, tz=VN_TZ)

    # 1) Số phút cụ thể ("20 phút nữa") — chỉ lấy trong đúng mệnh đề hứa
    m_min = _EXPLICIT_MINUTES_RE.search(low_clause)
    if m_min:
        try:
            mins = float(m_min.group(1))
            if 3 <= mins <= 360:
                return now_ts + mins * 60.0
        except ValueError:
            pass

    # 2) Số giờ cụ thể ("nửa tiếng nữa", "1 tiếng nữa")
    m_hr = _EXPLICIT_HOURS_RE.search(low_clause)
    if m_hr:
        raw_h = m_hr.group(1).replace(",", ".").strip()
        word_map = {"nửa": 0.5, "một": 1.0, "hai": 2.0, "ba": 3.0}
        hrs = word_map.get(raw_h)
        if hrs is None:
            try:
                hrs = float(raw_h)
            except ValueError:
                hrs = None
        if hrs and 0.2 <= hrs <= 12.0:
            return now_ts + hrs * 3600.0

    # 3) Mốc giờ VN cụ thể trong mệnh đề hứa ("tối 9h gọi", "4h chiều về tới nhà em nhắn")
    try:
        import diary_helper
        parsed_ts = diary_helper.parse_vn_clock_to_ts(clause_text, now_ts=now_ts, roll_forward_if_past=True)
        if parsed_ts and (now_ts + 5 * 60.0) <= parsed_ts <= (now_ts + 18 * 3600.0):
            return float(parsed_ts)
    except Exception:
        pass

    # 4) Mốc buổi trong ngày nếu không có giờ chính xác
    if any(k in low_clause for k in ("sáng mai", "mai dậy", "ngày mai")):
        target_day = vn_now.date() if vn_now.hour < 5 else (vn_now.date() + datetime.timedelta(days=1))
        jitter = rng.randint(20, 40)
        target_dt = datetime.datetime(target_day.year, target_day.month, target_day.day, 8, jitter, tzinfo=VN_TZ)
        return max(now_ts + 1800.0, target_dt.timestamp())

    if any(k in low_clause for k in ("tối nay", "tối về", "tối rảnh")) and vn_now.hour < 19:
        jitter = rng.randint(45, 70)
        target_dt = vn_now.replace(hour=19, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=jitter)
        return max(now_ts + 1800.0, target_dt.timestamp())

    if any(k in low_clause for k in ("chiều nay", "chiều về", "chiều rảnh")) and vn_now.hour < 14:
        jitter = rng.randint(25, 55)
        target_dt = vn_now.replace(hour=14, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=jitter)
        return max(now_ts + 1800.0, target_dt.timestamp())

    # 5) Heuristic tự nhiên theo hoạt động (kèm jitter nhẹ)
    mins = rng.randint(min_default_mins, max_default_mins)
    cand_ts = now_ts + mins * 60.0

    # Tránh đánh thức vào rạng sáng 01:00–06:45 nếu chỉ là việc vặt không gấp lúc khuya
    cand_dt = datetime.datetime.fromtimestamp(cand_ts, tz=VN_TZ)
    if 1 <= cand_dt.hour < 6 and any(w in low_clause for w in ("ngủ", "mai", "sáng")):
        target_dt = cand_dt.replace(hour=8, minute=rng.randint(20, 40), second=0, microsecond=0)
        return target_dt.timestamp()
    return cand_ts


def extract_deferred_promise(
    user_text: str = "",
    bot_text: str = "",
    now_ts: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """
    Trích xuất lịch tự báo lại (`ai_promise`) từ cặp tin nhắn vừa diễn ra.
    Trả về None nếu không có lời hứa/dặn dò nhắn lại sau khi xong việc.
    """
    now = float(now_ts if now_ts is not None else time.time())
    r_gen = rng or random.Random()

    bot_clause = _find_matching_bot_clause(bot_text)
    user_clause = _find_matching_user_clause(user_text)
    if not bot_clause and not user_clause:
        return None
    if user_clause and not bot_clause and _BOT_DECLINE_OR_NEGATE_RE.search(bot_text or ""):
        return None

    primary_clause = bot_clause or user_clause or ""
    combined = f"{user_clause or ''} | {bot_clause or ''}".strip(" |")
    topic_label, kw_tuple, min_m, max_m = _detect_activity_profile(combined)

    due_at = _parse_relative_due_ts(
        clause_text=primary_clause,
        full_text=combined,
        now_ts=now,
        min_default_mins=min_m,
        max_default_mins=max_m,
        rng=r_gen,
    )

    if any(v in primary_clause.lower() for v in ("gọi", "call", "alo")):
        action_verb = "Nhắn hoặc gọi cho chủ nhà"
    elif any(v in primary_clause.lower() for v in ("gửi", "gởi", "chụp")):
        action_verb = "Gửi/nhắn lại cho chủ nhà"
    elif "báo cáo" in primary_clause.lower() or "báo" in primary_clause.lower():
        action_verb = "Nhắn báo cáo cho chủ nhà"
    else:
        action_verb = "Nhắn lại cho chủ nhà"

    title = f"{action_verb} {topic_label}".strip()
    source = "bot_promise" if bot_clause else "user_ask"
    snippet = (bot_clause or user_clause or "").strip()[:140]
    context_note = f"Trợ lý đã nhận lời / hẹn {topic_label}: '{snippet}'"

    return {
        "title": title[:180],
        "context_note": context_note[:280],
        "due_at": float(due_at),
        "delay_mins": round(max(1.0, (due_at - now) / 60.0), 1),
        "activity_topic": topic_label,
        "activity_keywords": list(kw_tuple),
        "source": source,
    }


def is_promise_fulfilled_by_text(
    promise_title: str,
    promise_note: str,
    bot_msg_text: str,
) -> bool:
    """Kiểm tra xem một tin Đan đã gửi sau thời điểm hứa có báo hoàn tất việc đó rồi chưa."""
    msg = str(bot_msg_text or "").strip()
    if not msg:
        return False
    # Không nhầm với chính câu hứa ("ăn xong em báo liền")
    if _find_matching_bot_clause(msg) and not _DONE_MARKER_RE.search(msg):
        return False

    low_msg = msg.lower()
    combined_p = f"{promise_title or ''} {promise_note or ''}".lower()
    _, kw_tuple, _, _ = _detect_activity_profile(combined_p)

    if any(kw in low_msg for kw in kw_tuple) and _DONE_MARKER_RE.search(low_msg):
        return True
    # Trường hợp nói ngắn gọn: "em ăn xong rồi nè", "về tới nhà rồi anh"
    for kw in kw_tuple:
        if kw == "xong":
            continue
        if re.search(rf"\b{re.escape(kw)}[^.!?\n]{{0,18}}\bxong\s+(?:rồi|rùi|nè|đây|nha|nhé)\b", low_msg, re.IGNORECASE):
            return True
    if "nhà" in combined_p or "nơi" in combined_p or "phòng" in combined_p:
        if re.search(r"(?:về|tới|đến)\s+(?:tới\s+|đến\s+)?(?:nhà|nơi|phòng)\s+(?:rồi|rùi|nè|đây|nha)", low_msg, re.IGNORECASE):
            return True
    return False
