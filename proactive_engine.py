# -*- coding: utf-8 -*-
"""
Proactive Engine cho Smart Butler AI (0 lời gọi LLM, <5ms trên Snapdragon 410):
1. Continuous Context Drive: 5 trục động lực (connection, pride, valence, arousal, immersion)
   dao động theo thời gian, kiểu kết thúc lượt trước & nhịp sinh hoạt trong ngày.
2. ProactiveArbiter: Cổng kiểm soát trước & sau evaluate_proactive_turn (cap ngày, giãn cách
   tối thiểu, khung giờ ngủ Circadian, neo ngữ cảnh), ghi log quyết định cho Web Dashboard.
3. Wake-Lottery Micro-Rituals: Tối đa 2 gợi ý nghi thức nhỏ mỗi ngày khi chủ động mở lời.
"""
import datetime
import random
import time
from typing import Any, Dict, List, Optional

import circadian
import config
import database
import persona

VN_TZ = datetime.timezone(datetime.timedelta(hours=getattr(config, "TIMEZONE_OFFSET_HOURS", 7)))

DEFAULT_DAILY_CAP = 6
MIN_INTERVAL_MINS = 40.0
DRIVE_THRESHOLD = 0.38
LOTTERY_DAILY_LIMIT = 2
LOTTERY_COOLDOWN_SEC = 3 * 3600

_DEFAULT_AXES = {
    "connection": 0.56,
    "pride": 0.34,
    "valence": 0.68,
    "arousal": 0.52,
    "immersion": 0.36,
    "last_turn_type": "neutral",
    "drive_score": 0.45,
}


def _clamp01(v: float) -> float:
    return round(max(0.05, min(0.98, float(v))), 3)


def _classify_turn_type(mood: str = "", user_text: str = "", bot_text: str = "", delta: int = 0) -> str:
    joined = f"{user_text} {bot_text}".lower()
    mood_l = (mood or "").lower()
    if any(k in joined for k in ("ngủ ngon", "ngủ trước", "khò", "g9", "good night", "đi ngủ đây")):
        return "goodnight"
    if any(k in mood_l for k in ("căng", "lạnh", "cộc")) or delta <= -1:
        return "cold"
    if any(k in mood_l for k in ("vui", "hào hứng", "tích cực", "thoải mái")) and delta >= 1:
        return "sweet"
    if "?" in user_text or any(k in joined for k in ("tí nữa", "lát", "đang dở", "chờ xíu")):
        return "mid_topic"
    return "neutral"


def compute_drive_score(
    axes: Dict[str, Any],
    circadian_state: Dict[str, Any],
    open_loops_count: int = 0,
    just_arrived_home: bool = False,
) -> float:
    conn = float(axes.get("connection", 0.55))
    pride = float(axes.get("pride", 0.35))
    valence = float(axes.get("valence", 0.65))
    arousal = float(axes.get("arousal", 0.5))
    immersion = float(axes.get("immersion", 0.35))

    base = (
        conn * 0.44
        + valence * 0.18
        + arousal * 0.12
        + (1.0 - immersion) * 0.14
        + (1.0 - pride * 0.65) * 0.12
    )
    anchor_bonus = min(0.12, open_loops_count * 0.06) + (0.14 if just_arrived_home else 0.0)
    circ_mult = float(circadian_state.get("proactive_multiplier", 1.0))
    scaled = (base + anchor_bonus) * (0.55 + 0.45 * circ_mult)
    return round(max(0.02, min(0.99, scaled)), 3)


def _load_axes(user_id: int, now_ts: float) -> Dict[str, Any]:
    raw = database.get_jiwen_state(user_id)
    out = dict(_DEFAULT_AXES)
    out.update({k: raw[k] for k in _DEFAULT_AXES if k in raw})
    out["last_tick_at"] = float(raw.get("last_tick_at") or now_ts)
    return out


def tick_drive(
    user_id: int,
    last_user_msg_ts: float = 0.0,
    last_bot_msg_ts: float = 0.0,
    user_state: Optional[Dict[str, Any]] = None,
    just_arrived_home: bool = False,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Cập nhật 5 trục động lực theo thời gian trôi qua và nhịp sinh hoạt."""
    now = float(now_ts if now_ts is not None else time.time())
    axes = _load_axes(user_id, now)
    elapsed_hrs = max(0.0, min(12.0, (now - float(axes.get("last_tick_at") or now)) / 3600.0))
    circ = circadian.get_circadian_state(now_ts=now, last_user_msg_ts=last_user_msg_ts)
    turn_type = str(axes.get("last_turn_type") or "neutral")

    conn_rate = 0.065
    if turn_type == "goodnight":
        conn_rate = 0.025
    elif turn_type == "mid_topic":
        conn_rate = 0.095
    elif turn_type == "cold":
        conn_rate = 0.032

    if elapsed_hrs > 0.005:
        axes["connection"] = _clamp01(float(axes["connection"]) + conn_rate * elapsed_hrs)
        axes["immersion"] = _clamp01(float(axes["immersion"]) + (0.30 - float(axes["immersion"])) * min(1.0, 0.25 * elapsed_hrs))
        axes["pride"] = _clamp01(float(axes["pride"]) + (0.34 - float(axes["pride"])) * min(1.0, 0.14 * elapsed_hrs))
        axes["valence"] = _clamp01(float(axes["valence"]) + (0.68 - float(axes["valence"])) * min(1.0, 0.12 * elapsed_hrs))
        target_arousal = 0.25 + 0.60 * float(circ["energy"])
        axes["arousal"] = _clamp01(float(axes["arousal"]) + (target_arousal - float(axes["arousal"])) * min(1.0, 0.35 * elapsed_hrs))

    mins_since_bot = max(0.0, (now - last_bot_msg_ts) / 60.0) if last_bot_msg_ts > 0 else 999.0
    if mins_since_bot < 20.0:
        axes["connection"] = _clamp01(min(float(axes["connection"]), 0.45))

    unanswered = int((user_state or {}).get("unanswered_proactive_count") or 0)
    if unanswered >= 1:
        axes["pride"] = _clamp01(max(float(axes["pride"]), 0.48 + unanswered * 0.14))

    eligible_loops = database.get_eligible_open_loops(
        user_id, limit=3, cooldown_sec=7200.0, now_ts=now, respect_target_time=True
    )
    axes["last_tick_at"] = now
    axes["drive_score"] = compute_drive_score(
        axes,
        circ,
        open_loops_count=len(eligible_loops),
        just_arrived_home=just_arrived_home,
    )
    database.save_jiwen_state(user_id, axes)
    return {
        "axes": axes,
        "circadian": circ,
        "eligible_open_loops": eligible_loops,
    }


def on_user_turn(
    user_id: int,
    delta_affinity: int = 0,
    mood: str = "",
    user_text: str = "",
    bot_text: str = "",
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Cập nhật trục động lực ngay sau một lượt trao đổi hai chiều với chủ nhà."""
    now = float(now_ts if now_ts is not None else time.time())
    axes = _load_axes(user_id, now)
    turn_type = _classify_turn_type(mood=mood, user_text=user_text, bot_text=bot_text, delta=delta_affinity)

    axes["last_turn_type"] = turn_type
    axes["connection"] = _clamp01(max(0.32, float(axes["connection"]) - 0.14))
    axes["immersion"] = _clamp01(0.42 if turn_type != "goodnight" else 0.72)

    if delta_affinity > 0:
        axes["valence"] = _clamp01(float(axes["valence"]) + min(0.12, delta_affinity * 0.04))
        axes["pride"] = _clamp01(float(axes["pride"]) - 0.05)
    elif delta_affinity < 0 or turn_type == "cold":
        axes["valence"] = _clamp01(float(axes["valence"]) - 0.10)
        axes["pride"] = _clamp01(float(axes["pride"]) + 0.12)

    circ = circadian.get_circadian_state(now_ts=now, last_user_msg_ts=now)
    axes["arousal"] = _clamp01(0.35 + 0.45 * float(circ["energy"]) + (0.08 if turn_type == "mid_topic" else 0.0))
    axes["last_tick_at"] = now
    axes["drive_score"] = compute_drive_score(axes, circ)
    database.save_jiwen_state(user_id, axes)
    return axes


def on_bot_proactive_sent(
    user_id: int,
    unanswered_count: int = 1,
    now_ts: Optional[float] = None,
) -> None:
    """Giảm áp lực chủ động ngay sau khi bot vừa gửi 1 lượt hỏi thăm thành công."""
    now = float(now_ts if now_ts is not None else time.time())
    axes = _load_axes(user_id, now)
    axes["connection"] = _clamp01(float(axes["connection"]) - 0.22)
    axes["pride"] = _clamp01(float(axes["pride"]) + 0.08 * max(1, unanswered_count))
    axes["immersion"] = _clamp01(float(axes["immersion"]) + 0.12)
    axes["last_tick_at"] = now
    circ = circadian.get_circadian_state(now_ts=now)
    axes["drive_score"] = compute_drive_score(axes, circ)
    database.save_jiwen_state(user_id, axes)


def _should_skip_duplicate_log(user_id: int, reason: str, now_ts: float) -> bool:
    recent = database.get_proactive_decisions(user_id, limit=3)
    if not recent:
        return False
    latest = recent[0]
    if latest.get("reason") == reason and (now_ts - float(latest.get("created_at") or 0.0)) < 1800:
        return True
    return False


def evaluate_wakeup_gate(
    user_id: int,
    user_state: Optional[Dict[str, Any]] = None,
    just_arrived_home: bool = False,
    daily_cap: int = DEFAULT_DAILY_CAP,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Cổng ProactiveArbiter quyết định có đáng gọi LLM evaluate_proactive_turn lúc này không.
    Trả dict: {allow, decision, reason, recheck_mins, drive_score, circadian, sent_today, daily_cap, axes}
    """
    now = float(now_ts if now_ts is not None else time.time())
    u_state = user_state or database.get_or_create_user(user_id)
    last_user_ts = float(u_state.get("last_user_message_time") or 0.0)
    last_bot_ts = float(u_state.get("last_bot_message_time") or 0.0)
    unanswered = int(u_state.get("unanswered_proactive_count") or 0)

    tick = tick_drive(
        user_id,
        last_user_msg_ts=last_user_ts,
        last_bot_msg_ts=last_bot_ts,
        user_state=u_state,
        just_arrived_home=just_arrived_home,
        now_ts=now,
    )
    axes = tick["axes"]
    circ = tick["circadian"]
    eligible_loops = tick["eligible_open_loops"]
    drive_score = float(axes["drive_score"])
    sent_today = database.count_proactive_sent_today(user_id, now_ts=now)
    mins_since_bot = max(0.0, (now - last_bot_ts) / 60.0) if last_bot_ts > 0 else 999.0

    def _reject(reason: str, recheck_mins: int) -> Dict[str, Any]:
        if not _should_skip_duplicate_log(user_id, reason, now):
            database.record_proactive_decision(
                user_id,
                decision="pre_rejected",
                reason=reason,
                category=circ["phase"],
                score=drive_score,
                details={
                    "sent_today": sent_today,
                    "daily_cap": daily_cap,
                    "mins_since_bot": round(mins_since_bot, 1),
                    "unanswered": unanswered,
                },
                now_ts=now,
            )
        return {
            "allow": False,
            "decision": "pre_rejected",
            "reason": reason,
            "recheck_mins": recheck_mins,
            "drive_score": drive_score,
            "circadian": circ,
            "sent_today": sent_today,
            "daily_cap": daily_cap,
            "axes": axes,
        }

    if unanswered >= 2:
        return _reject("unanswered_wait", 300)

    if circ["is_sleep_window"]:
        return _reject("circadian_sleep_window", 90)

    if sent_today >= max(1, int(daily_cap)):
        return _reject("daily_cap_reached", 180)

    min_gap = 20.0 if just_arrived_home else MIN_INTERVAL_MINS
    if mins_since_bot < min_gap:
        return _reject("min_interval_cooldown", max(20, int(min_gap - mins_since_bot) + 8))

    has_anchor = (
        just_arrived_home
        or bool(eligible_loops)
        or mins_since_bot >= 110.0
    )

    if not has_anchor and drive_score < DRIVE_THRESHOLD:
        return _reject("drive_below_threshold", 60)

    return {
        "allow": True,
        "decision": "allow",
        "reason": "arbiter_passed",
        "recheck_mins": 0,
        "drive_score": drive_score,
        "circadian": circ,
        "sent_today": sent_today,
        "daily_cap": daily_cap,
        "axes": axes,
        "has_open_loop": bool(eligible_loops),
        "has_unshared_dream": False,
    }


def record_eval_outcome(
    user_id: int,
    decision: str,
    reason: str = "",
    category: str = "",
    score: float = 0.0,
    details: Optional[Dict[str, Any]] = None,
    now_ts: Optional[float] = None,
) -> None:
    database.record_proactive_decision(
        user_id,
        decision=decision,
        reason=reason,
        category=category,
        score=score,
        details=details,
        now_ts=now_ts,
    )


def peek_wake_lottery_coupon(user_id: int, now_ts: Optional[float] = None) -> Optional[Dict[str, str]]:
    """
    Lấy 1 gợi ý micro-ritual miễn phí cho lượt chủ động (tối đa 2 lần/ngày, cách nhau >= 3h).
    Chỉ trừ lượt khi chủ động gửi thật qua consume_wake_lottery_coupon().
    """
    now = float(now_ts if now_ts is not None else time.time())
    today = datetime.datetime.fromtimestamp(now, tz=VN_TZ).strftime("%Y-%m-%d")
    state = database.get_wake_lottery_state(user_id)
    if state.get("date") != today:
        state = {"date": today, "used_count": 0, "last_draw_at": 0.0, "last_category": "", "history": []}

    used = int(state.get("used_count") or 0)
    last_draw = float(state.get("last_draw_at") or 0.0)
    if used >= LOTTERY_DAILY_LIMIT:
        return None
    if last_draw > 0 and (now - last_draw) < LOTTERY_COOLDOWN_SEC:
        return None

    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    weather_city = p["weather_city"]
    circ = circadian.get_circadian_state(now_ts=now)

    pool: List[Dict[str, str]] = [
        {
            "category": "habit_check",
            "hint": f"Nhắc nhẹ {owner_name} kiểm tra sự thoải mái trong phòng hoặc nghỉ giải lao ngắn tùy theo nhịp {circ['label_vi']}.",
        },
        {
            "category": "weather_tip",
            "hint": f"Gợi ý nhẹ theo dõi thời tiết hoặc lịch trình hôm nay tại {weather_city} cho {owner_name}.",
        },
        {
            "category": "mini_question",
            "hint": f"Hỏi {owner_name} 1 câu ngắn hữu ích để ghi nhớ thêm thói quen sinh hoạt hoặc sở thích điều khiển nhà thông minh.",
        },
    ]

    now_playing = database.get_pc_now_playing(now_ts=now)
    if now_playing:
        pool.insert(0, {
            "category": "music_vibe",
            "hint": f"Nhắc tự nhiên bài nhạc {owner_name} đang phát ('{now_playing['title']}') hoặc hỏi có cần chỉnh âm lượng/EQ không.",
        })

    loops = database.get_eligible_open_loops(
        user_id,
        limit=1,
        cooldown_sec=7200.0,
        min_age_sec=1800.0,
        now_ts=now,
        respect_target_time=True,
    )
    if loops:
        loop_item = loops[0]
        t_ts, t_lbl = database._infer_loop_target_info(loop_item)
        time_note = f" [mốc tham chiếu: {t_lbl}]" if t_lbl else ""
        pool.insert(0, {
            "category": "open_loop_ping",
            "hint": (
                f"Hỏi thăm nhẹ tiếp việc đang theo dõi{time_note} "
                f"(chỉ nhắc đúng thực tế đồng hồ hiện tại): "
                f"'{loop_item['topic']}' ({loop_item.get('note') or ''})."
            ),
        })

    last_cat = state.get("last_category") or ""
    filtered = [c for c in pool if c["category"] != last_cat] or pool
    if filtered and filtered[0]["category"] in ("open_loop_ping", "music_vibe"):
        return filtered[0]
    return random.choice(filtered)


def consume_wake_lottery_coupon(
    user_id: int,
    coupon: Optional[Dict[str, str]],
    now_ts: Optional[float] = None,
) -> None:
    if not coupon or not coupon.get("category"):
        return
    now = float(now_ts if now_ts is not None else time.time())
    today = datetime.datetime.fromtimestamp(now, tz=VN_TZ).strftime("%Y-%m-%d")
    state = database.get_wake_lottery_state(user_id)
    if state.get("date") != today:
        state = {"date": today, "used_count": 0, "last_draw_at": 0.0, "last_category": "", "history": []}

    hist = list(state.get("history") or [])
    hist.append({"category": coupon["category"], "ts": now})
    state["date"] = today
    state["used_count"] = int(state.get("used_count") or 0) + 1
    state["last_draw_at"] = now
    state["last_category"] = coupon["category"]
    state["history"] = hist[-10:]
    database.save_wake_lottery_state(user_id, state)


def get_engine_snapshot(user_id: int, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Trả về toàn bộ trạng thái Proactive Engine cho Dashboard & /status."""
    now = float(now_ts if now_ts is not None else time.time())
    today = datetime.datetime.fromtimestamp(now, tz=VN_TZ).strftime("%Y-%m-%d")
    u_state = database.get_or_create_user(user_id)
    last_user_ts = float(u_state.get("last_user_message_time") or 0.0)
    last_bot_ts = float(u_state.get("last_bot_message_time") or 0.0)

    axes = _load_axes(user_id, now)
    circ = circadian.get_circadian_state(now_ts=now, last_user_msg_ts=last_user_ts)
    open_loops = database.get_open_loops(user_id, include_resolved=False, now_ts=now)
    axes["drive_score"] = compute_drive_score(axes, circ, open_loops_count=len(open_loops))

    lottery = database.get_wake_lottery_state(user_id)
    if lottery.get("date") != today:
        lottery = {"date": today, "used_count": 0, "last_draw_at": 0.0, "last_category": "", "history": []}

    return {
        "circadian": circ,
        "jiwen": axes,
        "daily_cap": DEFAULT_DAILY_CAP,
        "sent_today": database.count_proactive_sent_today(user_id, now_ts=now),
        "mins_since_bot": round(max(0.0, (now - last_bot_ts) / 60.0), 1) if last_bot_ts > 0 else 999.0,
        "lottery": {
            "date": lottery.get("date", today),
            "used_count": int(lottery.get("used_count") or 0),
            "daily_limit": LOTTERY_DAILY_LIMIT,
            "last_category": lottery.get("last_category") or "",
        },
        "open_loops": open_loops,
        "decisions": database.get_proactive_decisions(user_id, limit=25),
        "rejected_memories": database.list_rejected_memories(user_id, limit=30),
        "now_playing": database.get_pc_now_playing(now_ts=now),
    }
