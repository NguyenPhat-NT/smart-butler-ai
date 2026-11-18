# -*- coding: utf-8 -*-
"""
Bộ Tự Học Thói Quen Ngầm & Tự Động Bật/Tắt Thiết Bị (0 extra Gemini API calls).

Thu thập lịch sử thao tác thật của người dùng (`device_events`), tự phát hiện các mẫu lặp lại
(khung giờ, ngưỡng nhiệt độ DHT11, thời lượng sử dụng, hiện diện PC) bằng Python thuần,
kết hợp quy tắc từ chat (`habit_action`) và thực thi tự động an toàn cho Quạt BLDC,
Ổ cắm BroadLink (Đèn phòng, Quạt thông gió) và Loa ESP32-C3.
"""

import time
import asyncio
import datetime
import logging
import statistics
from typing import Any, Dict, List, Optional, Tuple

import database
import lan_events
import lan_presence

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

_META_HABIT_SETTINGS = "smart_habit_settings"

DEFAULT_HABIT_SETTINGS: Dict[str, Any] = {
    "auto_enabled": True,
    "notify_telegram": True,
    "min_days": 3,
    "feedback_window_sec": 600,
    "manual_backoff_sec": 5400,
}

USER_SOURCES = (
    "user_dashboard",
    "user_telegram",
    "user_mcp",
    "user_physical",
    "user_action",
    "user_chat",
)

_RETENTION_DAYS = 45
_ANALYZE_WINDOW_DAYS = 21
_LAST_ANALYZE_TS = 0.0
_LAST_TG_NOTIFY_TS = 0.0


def _vn_dt(now_ts: Optional[float] = None) -> datetime.datetime:
    ts = float(now_ts if now_ts is not None else time.time())
    return datetime.datetime.fromtimestamp(ts, tz=VN_TZ)


def get_habit_settings() -> Dict[str, Any]:
    raw = database._get_json_meta(_META_HABIT_SETTINGS)
    out = dict(DEFAULT_HABIT_SETTINGS)
    if isinstance(raw, dict):
        if "auto_enabled" in raw:
            out["auto_enabled"] = bool(raw["auto_enabled"])
        if "notify_telegram" in raw:
            out["notify_telegram"] = bool(raw["notify_telegram"])
        for k, lo, hi in (
            ("min_days", 2, 10),
            ("feedback_window_sec", 120, 1800),
            ("manual_backoff_sec", 600, 14400),
        ):
            if k in raw:
                try:
                    out[k] = max(lo, min(hi, int(raw[k])))
                except (TypeError, ValueError):
                    pass
    return out


def save_habit_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_habit_settings()
    if isinstance(patch, dict):
        if "auto_enabled" in patch:
            cur["auto_enabled"] = bool(patch["auto_enabled"])
        if "notify_telegram" in patch:
            cur["notify_telegram"] = bool(patch["notify_telegram"])
        for k, lo, hi in (
            ("min_days", 2, 10),
            ("feedback_window_sec", 120, 1800),
            ("manual_backoff_sec", 600, 14400),
        ):
            if k in patch:
                try:
                    cur[k] = max(lo, min(hi, int(patch[k])))
                except (TypeError, ValueError):
                    pass
    database._set_json_meta(_META_HABIT_SETTINGS, cur)
    publish_update()
    return cur


def _canonical_op(device_type: str, action: str, value_num: Optional[float]) -> str:
    act = str(action or "").strip().lower()
    if act in ("power_on", "on", "turn_on", "resume", "play", "focus", "chill"):
        return "play" if device_type == "speaker" and act in ("play", "focus", "chill") else "power_on"
    if act in ("power_off", "off", "turn_off", "stop", "pause"):
        return "stop" if device_type == "speaker" and act in ("stop", "power_off", "off", "turn_off") else "power_off"
    if act in ("set_speed", "speed"):
        if value_num is not None and float(value_num) <= 0:
            return "power_off"
        return "set_speed"
    if act in ("volume", "volume_set", "volume_up", "volume_down", "volume_delta"):
        return "volume"
    return act or "power_on"


def _is_turn_on_action(action: str) -> bool:
    return action in ("power_on", "set_speed", "play", "resume", "volume")


def _is_opposite_action(auto_act: str, user_act: str, auto_val: Optional[float], user_val: Optional[float]) -> bool:
    a_on = auto_act in ("power_on", "set_speed", "play", "resume")
    a_off = auto_act in ("power_off", "stop", "pause")
    u_on = user_act in ("power_on", "set_speed", "play", "resume")
    u_off = user_act in ("power_off", "stop", "pause")
    if a_on and u_off:
        return True
    if a_off and u_on:
        return True
    if auto_act == "set_speed" and user_act == "set_speed" and auto_val is not None and user_val is not None:
        return abs(float(auto_val) - float(user_val)) >= 10.0
    if auto_act == "volume" and user_act == "volume" and auto_val is not None and user_val is not None:
        return abs(float(auto_val) - float(user_val)) >= 12.0
    return False


def get_habits_snapshot() -> Dict[str, Any]:
    return {
        "settings": get_habit_settings(),
        "habits": database.list_learned_habits(),
    }


def publish_update() -> None:
    try:
        lan_events.publish("habits_update", get_habits_snapshot())
    except Exception:
        pass


# ==================== GHI NHẬN SỰ KIỆN & PHẢN HỒI NGƯỢC ====================

def _apply_negative_feedback(
    device_type: str,
    target: str,
    user_op: str,
    value_num: Optional[float],
    now_ts: float,
) -> List[str]:
    cfg = get_habit_settings()
    win_sec = float(cfg["feedback_window_sec"])
    recent = database.get_recent_device_events(device_type, target, since_ts=now_ts - win_sec, limit=12)
    recent_auto = [ev for ev in recent if ev.get("source") == "habit_auto"]
    if not recent_auto:
        return []

    affected_keys: List[str] = []
    habits = database.list_learned_habits()
    for h in habits:
        if h.get("device_type") != device_type or str(h.get("target") or "").lower() != target.lower():
            continue
        trig_at = float(h.get("last_triggered_at") or 0.0)
        if trig_at <= 0.0 or (now_ts - trig_at) > win_sec:
            continue
        h_op = _canonical_op(device_type, str(h.get("action") or ""), h.get("action_value"))
        if not _is_opposite_action(h_op, user_op, h.get("action_value"), value_num):
            continue

        neg_cnt = int(h.get("negative_count") or 0) + 1
        new_conf = max(0.05, float(h.get("confidence") or 0.5) - 0.25)
        new_status = "muted" if neg_cnt >= 2 else str(h.get("status") or "learning")
        database.update_habit_runtime(
            h["habit_key"],
            negative_count=neg_cnt,
            confidence=new_conf,
            status=new_status,
            now_ts=now_ts,
        )
        affected_keys.append(h["habit_key"])
        logger.info(
            f"🔄 [Habits] Phản hồi ngược '{h['habit_key']}' (neg={neg_cnt}, conf={new_conf:.2f}, status={new_status})"
        )
        lan_events.emit_notify(
            title="🔄 Tự điều chỉnh thói quen",
            body=(
                f"Đã {'tạm tắt' if new_status == 'muted' else 'hạ độ tin cậy'} "
                f"thói quen \"{h.get('title')}\" vì bạn vừa chỉnh ngược lại."
            ),
            level="info",
            source="habits",
            speak_tts=False,
        )
    if affected_keys:
        publish_update()
    return affected_keys


def record_device_event(
    device_type: str,
    target: str,
    action: str,
    *,
    value_num: Optional[float] = None,
    value_text: str = "",
    source: str = "user_action",
    now_ts: Optional[float] = None,
) -> int:
    now = float(now_ts if now_ts is not None else time.time())
    dt = _vn_dt(now)
    d_type = str(device_type or "").strip().lower()
    tgt = str(target or "").strip()
    op = _canonical_op(d_type, action, value_num)
    if not d_type or not tgt or not op:
        return 0

    dht = None
    try:
        dht = database.get_esp32_sensor_data(max_age_sec=1800.0, now_ts=now)
    except Exception:
        dht = None
    pres_state = ""
    try:
        pres_state = str((lan_presence.get_presence_snapshot() or {}).get("state") or "")
    except Exception:
        pres_state = ""

    row_id = database.insert_device_event(
        d_type,
        tgt,
        op,
        value_num=float(value_num) if value_num is not None else None,
        value_text=str(value_text or "")[:120],
        source=str(source or "user_action"),
        hour_vn=dt.hour,
        minute_vn=dt.minute,
        weekday=dt.weekday(),
        date_vn=dt.strftime("%Y-%m-%d"),
        temp_c=float(dht["temperature_c"]) if dht and dht.get("temperature_c") is not None else None,
        humidity_pct=float(dht["humidity_pct"]) if dht and dht.get("humidity_pct") is not None else None,
        presence_state=pres_state,
        created_at=now,
        prune_before_ts=now - _RETENTION_DAYS * 86400.0,
    )
    if str(source or "") in USER_SOURCES:
        _apply_negative_feedback(d_type, tgt, op, value_num, now)
    return row_id


def notify_device_event(*args, **kwargs) -> int:
    """Wrapper an toàn không bao giờ ném exception ngược lại caller điều khiển phần cứng."""
    try:
        return record_device_event(*args, **kwargs)
    except Exception as e:
        logger.debug(f"Habits record_device_event skipped: {e}")
        return 0


# ==================== BỘ PHÂN TÍCH MẪU LẶP LẠI (PYTHON THUẦN) ====================

def _cluster_minutes(events: List[Dict[str, Any]], max_gap_mins: int = 50) -> List[List[Dict[str, Any]]]:
    """Gom nhóm các sự kiện có khung giờ VN gần nhau (kể cả qua nửa đêm 23:50–00:10)."""
    if not events:
        return []
    ordered = sorted(events, key=lambda e: int(e["hour_vn"]) * 60 + int(e["minute_vn"]))
    clusters: List[List[Dict[str, Any]]] = [[ordered[0]]]
    for ev in ordered[1:]:
        cur_m = int(ev["hour_vn"]) * 60 + int(ev["minute_vn"])
        prev_m = int(clusters[-1][-1]["hour_vn"]) * 60 + int(clusters[-1][-1]["minute_vn"])
        if (cur_m - prev_m) <= max_gap_mins:
            clusters[-1].append(ev)
        else:
            clusters.append([ev])
    if len(clusters) > 1:
        first_m = int(clusters[0][0]["hour_vn"]) * 60 + int(clusters[0][0]["minute_vn"])
        last_m = int(clusters[-1][-1]["hour_vn"]) * 60 + int(clusters[-1][-1]["minute_vn"])
        if (first_m + 1440 - last_m) <= max_gap_mins:
            clusters[0] = clusters[-1] + clusters[0]
            clusters.pop()
    return clusters


def _fmt_hhmm(mins_of_day: int) -> str:
    m = int(mins_of_day) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _human_action_label(device_type: str, target: str, action: str, val: Optional[float]) -> str:
    if action in ("power_on", "play"):
        if device_type == "speaker":
            return f"Mở nhạc {target}"
        if val is not None and float(val) > 0 and device_type == "fan":
            return f"Bật {target} ({int(round(val))}%)"
        return f"Bật {target}"
    if action == "set_speed" and val is not None:
        return f"Chỉnh {target} mức {int(round(val))}%"
    if action == "volume" and val is not None:
        return f"Đặt âm lượng {target} {int(round(val))}%"
    if action in ("power_off", "stop"):
        return f"Tắt {target}"
    return f"{action} {target}"


def analyze_and_sync_habits(now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """Quét log thao tác thật trong 21 ngày gần nhất để cập nhật bảng `learned_habits` (0 token Gemini)."""
    global _LAST_ANALYZE_TS
    now = float(now_ts if now_ts is not None else time.time())
    _LAST_ANALYZE_TS = now
    cfg = get_habit_settings()
    min_days = int(cfg["min_days"])

    events = database.get_device_events_since(
        since_ts=now - _ANALYZE_WINDOW_DAYS * 86400.0,
        sources=list(USER_SOURCES),
        limit=2500,
    )
    if not events:
        return database.list_learned_habits()

    # 1) Thói quen theo khung giờ VN
    by_dev_op: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for ev in events:
        d_type = ev["device_type"]
        tgt = ev["target"]
        raw_act = ev["action"]
        op = "power_on" if (d_type == "fan" and raw_act == "set_speed" and (ev.get("value_num") or 0) > 0) else raw_act
        by_dev_op.setdefault((d_type, tgt, op), []).append(ev)

    for (d_type, tgt, op), grp in by_dev_op.items():
        all_op_days = max(1, len({e["date_vn"] for e in grp}))
        for cl in _cluster_minutes(grp, max_gap_mins=50):
            ev_days = len({e["date_vn"] for e in cl})
            if ev_days < 2:
                continue
            mins = [int(e["hour_vn"]) * 60 + int(e["minute_vn"]) for e in cl]
            if max(mins) - min(mins) > 720:
                mins = [m + 1440 if m < 360 else m for m in mins]
            med_min = int(round(statistics.median(mins))) % 1440
            consistency = ev_days / float(max(ev_days, all_op_days))
            conf = min(0.95, 0.35 + 0.14 * ev_days + 0.20 * consistency)
            status = "active" if (ev_days >= min_days and conf >= 0.55) else "learning"

            nums = [float(e["value_num"]) for e in cl if e.get("value_num") is not None and float(e["value_num"]) > 0]
            med_val = round(statistics.median(nums)) if nums else None
            queries = [str(e.get("value_text") or "").strip() for e in cl if str(e.get("value_text") or "").strip()]
            top_query = max(set(queries), key=queries.count) if queries else ""

            hb = med_min // 60
            h_key = f"time:{d_type}:{tgt.lower()}:{op}:{hb:02d}"
            act_verb = "set_speed" if (d_type == "fan" and op == "power_on" and med_val) else op
            title = f"{_human_action_label(d_type, tgt, act_verb, med_val)} lúc ~{_fmt_hhmm(med_min)}"
            database.upsert_learned_habit(
                habit_key=h_key,
                pattern_type="time_window",
                device_type=d_type,
                target=tgt,
                action=act_verb,
                action_value=med_val,
                meta={"trigger_min": med_min, "tolerance_mins": 20, "query": top_query},
                title=title,
                source="behavior",
                status=status,
                confidence=conf,
                evidence_days=ev_days,
                occurrences=len(cl),
                last_seen_at=max(float(e["created_at"]) for e in cl),
                now_ts=now,
            )

    # 2) Thói quen bật/chỉnh Quạt BLDC theo ngưỡng nhiệt độ DHT11
    fan_hot_events = [
        e for e in events
        if e["device_type"] == "fan"
        and e["action"] in ("power_on", "set_speed")
        and e.get("temp_c") is not None
        and float(e["temp_c"]) >= 28.0
    ]
    if fan_hot_events:
        hot_days = len({e["date_vn"] for e in fan_hot_events})
        if hot_days >= 2:
            temps = sorted(float(e["temp_c"]) for e in fan_hot_events)
            p25_temp = temps[max(0, len(temps) // 4)]
            thresh_temp = round(p25_temp * 2.0) / 2.0
            spds = [float(e["value_num"]) for e in fan_hot_events if e.get("value_num") is not None and float(e["value_num"]) > 0]
            med_spd = round(statistics.median(spds)) if spds else 20.0
            conf = min(0.92, 0.36 + 0.14 * hot_days + 0.12)
            status = "active" if (hot_days >= min_days and conf >= 0.55) else "learning"
            tgt_fan = fan_hot_events[-1]["target"]
            database.upsert_learned_habit(
                habit_key=f"temp:fan:{tgt_fan.lower()}:on",
                pattern_type="temp_threshold",
                device_type="fan",
                target=tgt_fan,
                action="set_speed" if spds else "power_on",
                action_value=med_spd,
                meta={"temp_min": thresh_temp},
                title=f"Bật {tgt_fan} ({int(med_spd)}%) khi phòng ≥ {thresh_temp:.1f}°C",
                source="behavior",
                status=status,
                confidence=conf,
                evidence_days=hot_days,
                occurrences=len(fan_hot_events),
                last_seen_at=max(float(e["created_at"]) for e in fan_hot_events),
                now_ts=now,
            )

    # 3) Thói quen thời lượng dùng (Bật -> Tắt sau ~N phút, rất hữu ích cho Quạt thông gió & Ổ cắm)
    by_dev_seq: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for ev in events:
        by_dev_seq.setdefault((ev["device_type"], ev["target"]), []).append(ev)

    for (d_type, tgt), seq in by_dev_seq.items():
        if d_type not in ("plug", "fan"):
            continue
        durations: List[Tuple[float, str, float]] = []
        last_on: Optional[Dict[str, Any]] = None
        for ev in seq:
            act = ev["action"]
            if act in ("power_on", "set_speed") and (ev.get("value_num") is None or float(ev["value_num"]) > 0):
                if last_on is None:
                    last_on = ev
            elif act == "power_off" and last_on is not None:
                dt_mins = (float(ev["created_at"]) - float(last_on["created_at"])) / 60.0
                if 5.0 <= dt_mins <= 180.0:
                    durations.append((dt_mins, ev["date_vn"], float(ev["created_at"])))
                last_on = None
        dur_days = len({d[1] for d in durations})
        if dur_days >= 2:
            vals = sorted(d[0] for d in durations)
            med_dur = round(statistics.median(vals))
            spread = vals[-1] - vals[0]
            if spread <= max(30.0, med_dur * 0.9):
                conf = min(0.92, 0.36 + 0.14 * dur_days + 0.14)
                status = "active" if (dur_days >= min_days and conf >= 0.55) else "learning"
                database.upsert_learned_habit(
                    habit_key=f"dur:{d_type}:{tgt.lower()}:power_off",
                    pattern_type="duration_off",
                    device_type=d_type,
                    target=tgt,
                    action="power_off",
                    action_value=float(med_dur),
                    meta={"duration_mins": int(med_dur)},
                    title=f"Tự tắt {tgt} sau ~{int(med_dur)} phút bật",
                    source="behavior",
                    status=status,
                    confidence=conf,
                    evidence_days=dur_days,
                    occurrences=len(durations),
                    last_seen_at=max(d[2] for d in durations),
                    now_ts=now,
                )

    # 4) Thói quen gắn với trạng thái bật PC (`home_pc`)
    for (d_type, tgt, op), grp in by_dev_op.items():
        if op not in ("power_on", "play"):
            continue
        pc_events = [e for e in grp if e.get("presence_state") == "home_pc"]
        pc_days = len({e["date_vn"] for e in pc_events})
        total_days = max(1, len({e["date_vn"] for e in grp}))
        if pc_days >= 2 and (pc_days / float(max(pc_days, total_days))) >= 0.65:
            hours = [int(e["hour_vn"]) for e in pc_events]
            h_lo = max(0, min(hours) - 1)
            h_hi = min(23, max(hours) + 1)
            conf = min(0.90, 0.34 + 0.14 * pc_days + 0.15)
            status = "active" if (pc_days >= min_days and conf >= 0.55) else "learning"
            nums = [float(e["value_num"]) for e in pc_events if e.get("value_num") is not None and float(e["value_num"]) > 0]
            med_val = round(statistics.median(nums)) if nums else None
            database.upsert_learned_habit(
                habit_key=f"pc:{d_type}:{tgt.lower()}:{op}",
                pattern_type="presence_home_pc",
                device_type=d_type,
                target=tgt,
                action=op,
                action_value=med_val,
                meta={"hour_from": h_lo, "hour_to": h_hi},
                title=f"{_human_action_label(d_type, tgt, op, med_val)} khi đang bật PC ({h_lo:02d}h–{h_hi:02d}h)",
                source="behavior",
                status=status,
                confidence=conf,
                evidence_days=pc_days,
                occurrences=len(pc_events),
                last_seen_at=max(float(e["created_at"]) for e in pc_events),
                now_ts=now,
            )

    return database.list_learned_habits()


# ==================== LỆNH CHAT / MCP QUẢN LÝ THÓI QUEN ====================

def _find_habits_by_query(query: str) -> List[Dict[str, Any]]:
    q = str(query or "").strip().lower()
    if not q:
        return []
    habits = database.list_learned_habits()
    exact = [h for h in habits if h["habit_key"].lower() == q or str(h["id"]) == q]
    if exact:
        return exact
    return [
        h for h in habits
        if q in str(h.get("target") or "").lower()
        or q in str(h.get("title") or "").lower()
        or q in str(h.get("habit_key") or "").lower()
    ]


def _resolve_device_type_and_target(raw_target: str) -> Tuple[str, str]:
    t = str(raw_target or "").strip()
    tl = t.lower()
    if any(k in tl for k in ("loa", "nhạc", "speaker", "esp32")):
        return "speaker", "Loa phòng"
    if any(k in tl for k in ("quạt phòng", "quạt bldc", "fan")) and "thông gió" not in tl and "hút" not in tl:
        return "fan", "Quạt phòng"
    if "thông gió" in tl or "quạt hút" in tl:
        return "plug", "Quạt thông gió"
    if "đèn" in tl:
        return "plug", "Đèn phòng"
    return "plug", t or "Đèn phòng"


def execute_ai_habit_action(ai_action: Any, now_ts: Optional[float] = None) -> str:
    """Xử lý `habit_action` từ Pass 1 chat hoặc MCP tool `manage_learned_habit` (0 extra calls)."""
    if not isinstance(ai_action, dict):
        return ""
    now = float(now_ts if now_ts is not None else time.time())
    act = str(ai_action.get("action") or "").strip().lower()
    if not act or act == "null":
        return ""

    if act == "analyze":
        habits = analyze_and_sync_habits(now_ts=now)
        publish_update()
        return f"Đã phân tích lại thói quen ({len(habits)} mẫu)."

    if act in ("set_master", "master_toggle"):
        en = bool(ai_action.get("enabled", True))
        save_habit_settings({"auto_enabled": en})
        return f"Đã {'BẬT' if en else 'TẮT'} tự động hóa theo thói quen."

    key_or_q = str(ai_action.get("habit_key") or ai_action.get("id") or ai_action.get("target") or "").strip()
    if act in ("mute", "disable", "enable", "forget", "delete"):
        matches = _find_habits_by_query(key_or_q)
        if not matches:
            return ""
        changed: List[str] = []
        for h in matches[:3]:
            hk = h["habit_key"]
            if act in ("forget", "delete"):
                if database.delete_learned_habit(hk):
                    changed.append(h["title"])
            elif act in ("mute", "disable"):
                database.update_habit_runtime(hk, status="muted", now_ts=now)
                changed.append(h["title"])
            else:
                database.update_habit_runtime(
                    hk, status="active", negative_count=0, confidence=max(0.75, float(h.get("confidence") or 0.75)), now_ts=now
                )
                changed.append(h["title"])
        if changed:
            publish_update()
            verb = "quên" if act in ("forget", "delete") else ("tạm tắt" if act in ("mute", "disable") else "bật lại")
            return f"Đã {verb}: {', '.join(changed)}."
        return ""

    if act in ("learn", "add", "upsert"):
        raw_tgt = str(ai_action.get("target") or "").strip()
        if not raw_tgt:
            return ""
        d_type, tgt = _resolve_device_type_and_target(raw_tgt)
        dev_act = str(ai_action.get("device_action") or "power_on").strip().lower()
        val = ai_action.get("value")
        val_f = float(val) if val is not None else None
        trig_time = str(ai_action.get("trigger_time") or "").strip()
        temp_min = ai_action.get("temp_min")
        dur_mins = ai_action.get("duration_mins")
        title = str(ai_action.get("pattern") or "").strip()

        if temp_min is not None:
            t_val = round(float(temp_min) * 2.0) / 2.0
            h_key = f"chat:temp:{d_type}:{tgt.lower()}:{dev_act}:{t_val:.1f}"
            p_type = "temp_threshold"
            meta: Dict[str, Any] = {"temp_min": t_val}
            if not title:
                title = f"{_human_action_label(d_type, tgt, dev_act, val_f)} khi phòng ≥ {t_val:.1f}°C"
        elif dur_mins is not None and int(dur_mins) > 0:
            dm = int(dur_mins)
            h_key = f"chat:dur:{d_type}:{tgt.lower()}:power_off"
            p_type = "duration_off"
            dev_act = "power_off"
            val_f = float(dm)
            meta = {"duration_mins": dm}
            if not title:
                title = f"Tự tắt {tgt} sau ~{dm} phút bật"
        elif trig_time and ":" in trig_time:
            parts = trig_time.split(":", 1)
            hh = max(0, min(23, int(parts[0])))
            mm = max(0, min(59, int(parts[1][:2])))
            t_min = hh * 60 + mm
            h_key = f"chat:time:{d_type}:{tgt.lower()}:{dev_act}:{hh:02d}{mm:02d}"
            p_type = "time_window"
            meta = {"trigger_min": t_min, "tolerance_mins": 20, "query": str(ai_action.get("query") or "")}
            if not title:
                title = f"{_human_action_label(d_type, tgt, dev_act, val_f)} lúc ~{hh:02d}:{mm:02d}"
        else:
            h_key = f"chat:pc:{d_type}:{tgt.lower()}:{dev_act}"
            p_type = "presence_home_pc"
            meta = {"hour_from": 0, "hour_to": 23}
            if not title:
                title = f"{_human_action_label(d_type, tgt, dev_act, val_f)} khi ở phòng bật PC"

        saved = database.upsert_learned_habit(
            habit_key=h_key,
            pattern_type=p_type,
            device_type=d_type,
            target=tgt,
            action=dev_act,
            action_value=val_f,
            meta=meta,
            title=title,
            source="chat",
            status="active",
            confidence=0.92,
            evidence_days=3,
            occurrences=1,
            last_seen_at=now,
            now_ts=now,
            preserve_muted=False,
        )
        publish_update()
        return f"Đã ghi nhớ thói quen: {saved.get('title') or title}."

    return ""


# ==================== ĐÁNH GIÁ & THỰC THI TỰ ĐỘNG AN TOÀN ====================

def _minutes_diff_circular(a_min: int, b_min: int) -> int:
    d = abs(int(a_min) - int(b_min)) % 1440
    return min(d, 1440 - d)


def _is_nocturnal_quiet_sleep(dt_vn: datetime.datetime, pc_online: bool, last_user_msg_ts: float, now_ts: float) -> bool:
    mins = dt_vn.hour * 60 + dt_vn.minute
    in_sleep_hours = (mins >= 30) and (mins <= 390)  # 00:30 .. 06:30 VN
    if not in_sleep_hours:
        return False
    recently_chatting = last_user_msg_ts > 0 and (now_ts - last_user_msg_ts) <= 1800.0
    return (not pc_online) and (not recently_chatting)


async def evaluate_and_execute_habits(
    *,
    bot: Any = None,
    target_user_id: int = 0,
    now_ts: Optional[float] = None,
) -> List[str]:
    """Đánh giá các thói quen `active` và chỉ thực thi khi thỏa đủ điều kiện an toàn."""
    now = float(now_ts if now_ts is not None else time.time())
    cfg = get_habit_settings()
    if not cfg.get("auto_enabled", True):
        return []

    active_habits = database.list_learned_habits(statuses=["active"])
    if not active_habits:
        return []

    dt = _vn_dt(now)
    cur_min = dt.hour * 60 + dt.minute
    today_vn = dt.strftime("%Y-%m-%d")

    pres = lan_presence.get_presence_snapshot() or {}
    pres_state = str(pres.get("state") or "unconfigured")
    pc_online = bool(pres.get("pc_online"))

    dht = database.get_esp32_sensor_data(max_age_sec=1800.0, now_ts=now)
    temp_c = float(dht["temperature_c"]) if dht and dht.get("temperature_c") is not None else None

    last_user_msg = 0.0
    in_call = False
    if int(target_user_id or 0) > 0:
        try:
            u_state = database.get_or_create_user(int(target_user_id))
            last_user_msg = float(u_state.get("last_user_message_time") or 0.0)
            ss = database.get_session_state(int(target_user_id))
            in_call = str(ss.get("call_status") or "").lower() == "in_call"
        except Exception:
            pass

    nocturnal_sleep = _is_nocturnal_quiet_sleep(dt, pc_online, last_user_msg, now)
    backoff_sec = float(cfg["manual_backoff_sec"])

    import fan_controller
    import smart_plugs
    import esp32_audio

    fan_ctrl = fan_controller.get_controller()
    plug_ctrl = smart_plugs.get_controller()
    spk_mgr = esp32_audio.get_manager()

    fan_snap = fan_ctrl.get_status_snapshot()
    plugs_snap = plug_ctrl.get_status_snapshot()
    spk_snap = spk_mgr.get_status_snapshot()

    recent_user_events = database.get_device_events_since(
        since_ts=now - max(backoff_sec, 4.0 * 3600.0),
        sources=list(USER_SOURCES),
        limit=200,
    )

    executed_notes: List[str] = []
    for h in active_habits:
        hk = h["habit_key"]
        p_type = h["pattern_type"]
        d_type = h["device_type"]
        tgt = h["target"]
        act = h["action"]
        val = h.get("action_value")
        meta = h.get("meta") or {}

        last_trig_ts = float(h.get("last_triggered_at") or 0.0)
        last_trig_date = str(h.get("last_triggered_date") or "")

        # 1) Kiểm tra điều kiện kích hoạt của mẫu
        matched = False
        duration_on_ts = 0.0
        if p_type == "time_window":
            trig_min = int(meta.get("trigger_min", -1))
            tol = int(meta.get("tolerance_mins", 20))
            if trig_min >= 0 and _minutes_diff_circular(cur_min, trig_min) <= tol:
                if last_trig_date != today_vn and (now - last_trig_ts) >= 6 * 3600.0:
                    matched = True
        elif p_type == "temp_threshold":
            t_min = meta.get("temp_min")
            if t_min is not None and temp_c is not None and temp_c >= float(t_min):
                if (now - last_trig_ts) >= 4 * 3600.0:
                    matched = True
        elif p_type == "presence_home_pc":
            h_lo = int(meta.get("hour_from", 0))
            h_hi = int(meta.get("hour_to", 23))
            if pc_online and pres_state == "home_pc" and (h_lo <= dt.hour <= h_hi):
                if last_trig_date != today_vn and (now - last_trig_ts) >= 6 * 3600.0:
                    matched = True
        elif p_type == "duration_off":
            dur_mins = int(meta.get("duration_mins") or val or 0)
            if dur_mins >= 5 and (now - last_trig_ts) >= 3600.0:
                dev_evs = [
                    e for e in recent_user_events
                    if e["device_type"] == d_type and e["target"].lower() == tgt.lower()
                ]
                if dev_evs:
                    last_ev = dev_evs[-1]
                    if last_ev["action"] in ("power_on", "set_speed"):
                        elapsed_m = (now - float(last_ev["created_at"])) / 60.0
                        if dur_mins <= elapsed_m <= (dur_mins + 25.0):
                            matched = True
                            duration_on_ts = float(last_ev["created_at"])

        if not matched:
            continue

        # 2) Khóa an toàn (Presence, Giờ ngủ, Call, Manual Backoff)
        is_turn_on = _is_turn_on_action(act)
        if is_turn_on:
            if pres_state == "away":
                continue
            if nocturnal_sleep and d_type in ("plug", "speaker"):
                continue
            if d_type == "speaker" and in_call:
                continue

        # Tôn trọng thao tác tay gần đây (duration_off tính từ lần bật nên chỉ lùi nếu bật < dur_mins)
        user_touches = [
            e for e in recent_user_events
            if e["device_type"] == d_type and e["target"].lower() == tgt.lower()
            and (now - float(e["created_at"])) <= backoff_sec
        ]
        if p_type != "duration_off" and user_touches:
            continue
        if p_type == "duration_off" and any(float(e["created_at"]) > duration_on_ts + 60.0 for e in user_touches):
            continue

        # 3) Kiểm tra trạng thái hiện tại để tránh bấm thừa + thực thi thật
        action_done_desc = ""
        if d_type == "fan":
            if not fan_snap.get("configured"):
                continue
            if not fan_snap.get("online") or (now - float(fan_snap.get("last_state_at") or 0.0)) > 90.0:
                await fan_ctrl.get_state(refresh=True)
                fan_snap = fan_ctrl.get_status_snapshot()
            if not fan_snap.get("online"):
                continue
            st = fan_snap.get("state") or {}
            is_on_now = bool(st.get("is_on"))
            if act in ("power_on", "set_speed"):
                if is_on_now and (act == "power_on" or p_type == "temp_threshold"):
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                if act == "set_speed" and val is not None and is_on_now and abs(int(st.get("speed_pct", 0)) - int(round(val))) <= 3:
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                if not is_on_now:
                    res = await fan_ctrl.set_power(True, manual=False, reason=f"habit:{hk}", event_source="habit_auto")
                    if res is None:
                        continue
                if act == "set_speed" and val is not None:
                    res_s = await fan_ctrl.set_speed(int(round(val)), manual=False, event_source="habit_auto")
                    if res_s is None:
                        continue
                action_done_desc = h["title"]
            elif act == "power_off":
                if not is_on_now:
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                res = await fan_ctrl.set_power(False, manual=False, reason=f"habit:{hk}", event_source="habit_auto")
                if res is None:
                    continue
                action_done_desc = h["title"]

        elif d_type == "plug":
            plug_item = next((p for p in plugs_snap if str(p.get("name") or "").lower() == tgt.lower()), None)
            if not plug_item:
                continue
            if not plug_item.get("online") or (now - float(plug_item.get("checked_at") or 0.0)) > 90.0:
                await plug_ctrl.refresh_all()
                plugs_snap = plug_ctrl.get_status_snapshot()
                plug_item = next((p for p in plugs_snap if str(p.get("name") or "").lower() == tgt.lower()), None)
            if not plug_item or not plug_item.get("online") or plug_item.get("locked"):
                continue
            want_on = is_turn_on
            if plug_item.get("is_on") is want_on:
                database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                continue
            res = await plug_ctrl.set_power(tgt, want_on, reason=f"habit:{hk}", event_source="habit_auto")
            if res is None:
                continue
            action_done_desc = h["title"]

        elif d_type == "speaker":
            st_spk = str(spk_snap.get("status") or "stopped").lower()
            if act in ("play", "power_on", "resume"):
                if st_spk == "playing":
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                q_str = str(meta.get("query") or "").strip()
                cmd = {"action": "play" if q_str else "power_on", "query": q_str}
                note = await esp32_audio.execute_music_command_dict(cmd, event_source="habit_auto")
                if not note:
                    continue
                action_done_desc = h["title"]
            elif act in ("stop", "power_off", "pause"):
                if st_spk == "stopped":
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                note = await esp32_audio.execute_music_command_dict({"action": "power_off"}, event_source="habit_auto")
                if not note:
                    continue
                action_done_desc = h["title"]
            elif act == "volume" and val is not None:
                cur_v = int(spk_snap.get("volume") or 0)
                if abs(cur_v - int(round(val))) <= 3:
                    database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
                    continue
                note = await esp32_audio.execute_music_command_dict(
                    {"action": "volume", "volume": int(round(val))}, event_source="habit_auto"
                )
                if not note:
                    continue
                action_done_desc = h["title"]

        if action_done_desc:
            database.update_habit_runtime(hk, last_triggered_at=now, last_triggered_date=today_vn, now_ts=now)
            executed_notes.append(action_done_desc)
            lan_events.emit_notify(
                title="🧠 Tự động theo thói quen",
                body=action_done_desc,
                level="info",
                source="habits",
                speak_tts=False,
            )
            logger.info(f"🧠 [Habit Auto Executed]: {hk} -> {action_done_desc}")

    if executed_notes:
        publish_update()
        await _send_gentle_habit_telegram(bot, target_user_id, executed_notes, cfg, now)

    return executed_notes


async def _send_gentle_habit_telegram(
    bot: Any,
    target_user_id: int,
    notes: List[str],
    cfg: Dict[str, Any],
    now_ts: float,
) -> None:
    global _LAST_TG_NOTIFY_TS
    if not bot or int(target_user_id or 0) <= 0 or not cfg.get("notify_telegram", True):
        return
    if lan_presence.is_quiet_hours_now(now_ts=now_ts):
        return
    if (now_ts - _LAST_TG_NOTIFY_TS) < 900.0:
        return
    _LAST_TG_NOTIFY_TS = now_ts
    summary = "; ".join(notes[:2])
    import persona
    pcfg = persona.get_persona_config()
    msg = f"🤖 [{pcfg['butler_name']}] Vừa tự động thực hiện theo thói quen: {summary}. (Nếu chưa đúng ý {pcfg['owner_name']}, hãy chỉnh lại trên Dashboard hoặc thiết bị để mình ghi nhớ nhé.)"
    try:
        await bot.send_message(int(target_user_id), msg)
    except Exception as e:
        logger.debug(f"Habits telegram notify skipped: {e}")


def format_habits_for_ai(limit: int = 8) -> str:
    """Định dạng ngắn gọn danh sách thói quen cho Pass 2/3 hoặc MCP tool `get_learned_habits`."""
    cfg = get_habit_settings()
    habits = database.list_learned_habits()[:limit]
    if not habits:
        return "Chưa ghi nhận thói quen lặp lại rõ ràng nào (hệ thống vẫn đang học ngầm từ thao tác thiết bị của bạn)."
    lines = [f"- Chế độ tự động theo thói quen: {'ĐANG BẬT' if cfg.get('auto_enabled') else 'ĐANG TẮT'}"]
    for h in habits:
        st_vi = {"active": "Đang áp dụng", "learning": "Đang theo dõi", "muted": "Đã tắt"}.get(h["status"], h["status"])
        lines.append(
            f"  • [{h['habit_key']}] {h['title']} — {st_vi} "
            f"(tin cậy {int(round(float(h['confidence']) * 100))}%, {h['evidence_days']} ngày)"
        )
    return "\n".join(lines)


async def habit_monitor_loop(
    bot: Any = None,
    target_user_id: int = 0,
    interval_sec: float = 60.0,
) -> None:
    """Vòng lặp nền mỗi 60s (phân tích mẫu mỗi 10p, kiểm tra điều kiện kích hoạt mỗi 60s, 0 LLM call)."""
    await asyncio.sleep(15.0)
    while True:
        now = time.time()
        try:
            if (now - _LAST_ANALYZE_TS) >= 600.0:
                await asyncio.to_thread(analyze_and_sync_habits, now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Lỗi phân tích thói quen định kỳ: {e}")

        try:
            await evaluate_and_execute_habits(bot=bot, target_user_id=target_user_id, now_ts=now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Lỗi thực thi thói quen tự động: {e}")

        await asyncio.sleep(interval_sec)
