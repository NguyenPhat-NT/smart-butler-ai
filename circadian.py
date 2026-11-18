# -*- coding: utf-8 -*-
"""
Nhịp sinh học trong ngày (Circadian Rhythm) cho Smart Butler theo múi giờ địa phương (mặc định UTC+7).
"""
import datetime
from typing import Any, Dict, Optional

import config

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=config.TIMEZONE_OFFSET_HOURS))

CIRCADIAN_SCHEDULE = [
    (
        0, 6, "deep_night", "Khuya nghỉ ngơi",
        0.10, 0.20, 0.05,
        "Giờ ngủ đêm yên tĩnh, chỉ báo việc khẩn cấp hoặc tự động hóa an toàn.",
    ),
    (
        6, 9, "morning_wake", "Buổi sáng khởi động",
        0.85, 0.70, 0.85,
        "Sáng mới dậy, phù hợp tóm tắt thời tiết, lịch nhắc và chuẩn bị ngày mới.",
    ),
    (
        9, 12, "focus_morning", "Làm việc buổi sáng",
        0.65, 0.65, 0.55,
        "Khung giờ tập trung buổi sáng, trả lời gọn và hỗ trợ công việc.",
    ),
    (
        12, 14, "midday_break", "Nghỉ trưa",
        0.60, 0.70, 0.65,
        "Giờ ăn trưa & nghỉ ngơi giữa ngày.",
    ),
    (
        14, 18, "afternoon_focus", "Buổi chiều làm việc",
        0.70, 0.68, 0.60,
        "Tỉnh táo làm việc chiều, theo dõi phòng & lịch nhắc.",
    ),
    (
        18, 22, "evening_relax", "Buổi tối thư giãn",
        0.80, 0.78, 0.80,
        "Buổi tối ở nhà thư giãn, điều chỉnh đèn/quạt/nhạc tự nhiên.",
    ),
    (
        22, 24, "late_night", "Cuối ngày",
        0.45, 0.55, 0.35,
        "Sắp tới giờ nghỉ ngơi, giữ tông nhẹ nhàng và kiểm tra tắt thiết bị thừa.",
    ),
]


def _local_datetime(now_ts: Optional[float] = None) -> datetime.datetime:
    if now_ts is None:
        return datetime.datetime.now(LOCAL_TZ)
    return datetime.datetime.fromtimestamp(float(now_ts), tz=LOCAL_TZ)


def get_circadian_state(
    now_ts: Optional[float] = None,
    last_user_msg_ts: float = 0.0,
    sleep_grace_active: bool = False,
) -> Dict[str, Any]:
    dt = _local_datetime(now_ts)
    hour = dt.hour
    phase_id, label_vi, energy, sociability, pro_mult, note = CIRCADIAN_SCHEDULE[0][2:]
    for start_h, end_h, p_id, lbl, e, s, pm, desc in CIRCADIAN_SCHEDULE:
        if start_h <= hour < end_h:
            phase_id, label_vi, energy, sociability, pro_mult, note = p_id, lbl, e, s, pm, desc
            break

    recently_active = False
    if last_user_msg_ts:
        ref_ts = float(now_ts) if now_ts is not None else datetime.datetime.now(LOCAL_TZ).timestamp()
        recently_active = (ref_ts - float(last_user_msg_ts)) < 720.0

    is_sleep_window = (0 <= hour < 6) and not sleep_grace_active
    is_morning_story_window = 6 <= hour < 12
    return {
        "hour": hour,
        "minute": dt.minute,
        "time_str": dt.strftime("%H:%M"),
        "date_str": dt.strftime("%Y-%m-%d"),
        "phase": phase_id,
        "label_vi": label_vi,
        "energy": energy,
        "sociability": sociability,
        "proactive_multiplier": pro_mult,
        "is_sleep_window": is_sleep_window,
        "is_morning_story_window": is_morning_story_window,
        "recently_active": recently_active,
        "note": note,
    }


def format_circadian_for_prompt(
    now_ts: Optional[float] = None,
    sleep_grace_active: bool = False,
) -> str:
    c = get_circadian_state(now_ts=now_ts, sleep_grace_active=sleep_grace_active)
    sleep_note = " (Đang trong giờ nghỉ đêm — giữ câu ngắn gọn, tránh làm phiền)" if c["is_sleep_window"] else ""
    return (
        f"[Nhịp sinh học hiện tại ({c['time_str']}): {c['label_vi']} | "
        f"Mức sẵn sàng: {int(c['energy']*100)}% | {c['note']}{sleep_note}]"
    )
