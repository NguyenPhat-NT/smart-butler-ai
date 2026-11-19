# -*- coding: utf-8 -*-
"""
MCP Tool Registry & JSON-RPC 2.0 Handler cho OpenStick Smart Home & Smart Butler AI.

Dùng chung một bộ công cụ chuẩn MCP JSON Schema cho cả hai luồng:
  1) Nội bộ AI (`Pass 1` tự quyết định chọn `fetch_tools` -> thực thi song song -> đưa sang `Pass 2/3`).
  2) Bên ngoài qua HTTP `/mcp` (`http://<BOT_HOST>:8080/mcp` — JSON-RPC 2.0:
     `initialize`, `tools/list`, `tools/call`, `ping`) để PC / IDE (Antigravity, Cursor, Claude Desktop)
     tự đọc trạng thái và điều khiển Quạt BLDC, Ổ cắm BroadLink SPmini, Loa ESP32 & Hiện diện LAN.
"""

import json
import time
import uuid
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import database
import lan_presence

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "smart-butler-mcp"
MCP_SERVER_VERSION = "1.0.0"
MCP_SUPPORTED_PROTOCOLS = ("2025-03-26", "2024-11-05")

READ_TOOL_NAMES = (
    "get_fan_state",
    "get_plugs_state",
    "get_presence_state",
    "get_room_climate",
    "get_speaker_state",
    "get_room_overview",
    "get_learned_habits",
    "get_router_state",
)


# ==================== HANDLERS CÁC TOOL ====================

async def _tool_get_fan_state(args: Dict[str, Any]) -> Dict[str, Any]:
    import fan_controller
    ctrl = fan_controller.get_controller()
    snap = ctrl.get_status_snapshot()
    refresh = bool(args.get("refresh", True))
    if refresh and snap.get("configured"):
        last_at = float(snap.get("last_state_at") or 0.0)
        if (time.time() - last_at) > 3.0:
            try:
                await asyncio.wait_for(ctrl.get_state(refresh=True), timeout=1.3)
            except Exception:
                pass
            snap = ctrl.get_status_snapshot()

    st = snap.get("state") or {}
    return {
        "configured": bool(snap.get("configured")),
        "online": bool(snap.get("online")),
        "is_on": bool(st.get("is_on")) if snap.get("online") else None,
        "speed_pct": st.get("speed_pct") if snap.get("online") else None,
        "output_freq_hz": st.get("output_freq") if snap.get("online") else None,
        "mode": st.get("mode_label") if snap.get("online") else None,
        "ramping_to_sleep": bool(snap.get("ramping")),
        "max_freq_hz": (snap.get("settings") or {}).get("max_freq_hz", 1800),
        "max_speed_pct": (snap.get("settings") or {}).get("max_speed_pct", 40),
        "sleep_prompt_status": (snap.get("automation") or {}).get("sleep_prompt_status", "none"),
    }


async def _tool_get_plugs_state(args: Dict[str, Any]) -> Dict[str, Any]:
    import smart_plugs
    ctrl = smart_plugs.get_controller()
    plugs = ctrl.get_status_snapshot()
    refresh = bool(args.get("refresh", True))
    if refresh and plugs:
        now_v = time.time()
        if any((now_v - float(p.get("checked_at") or 0.0)) > 3.0 for p in plugs):
            try:
                await asyncio.wait_for(ctrl.refresh_all(), timeout=1.5)
            except Exception:
                pass
            plugs = ctrl.get_status_snapshot()

    items: List[Dict[str, Any]] = []
    for p in plugs:
        items.append({
            "name": p["name"],
            "mac": p["mac"],
            "ip": p.get("ip", ""),
            "online": bool(p.get("online")),
            "locked": bool(p.get("locked")),
            "is_on": p.get("is_on"),
            "auto_away": bool(p.get("auto_away")),
            "auto_home": bool(p.get("auto_home")),
            "auto_sleep": bool(p.get("auto_sleep")),
            "timer": p.get("timer"),
        })
    return {"count": len(items), "plugs": items}


async def _tool_get_presence_state(args: Dict[str, Any]) -> Dict[str, Any]:
    snap = lan_presence.get_presence_snapshot()
    now_v = time.time()
    away_since = float(snap.get("away_since") or 0.0)
    away_mins = max(0, int((now_v - away_since) / 60.0)) if (snap.get("state") == "away" and away_since > 0) else 0
    return {
        "state": snap.get("state", "unconfigured"),
        "summary": database._format_presence_summary(snap, now_ts=now_v) or "Chưa cấu hình MAC theo dõi",
        "phone_online": bool(snap.get("phone_online")),
        "pc_online": bool(snap.get("pc_online")),
        "away_mins": away_mins,
    }


async def _tool_get_room_climate(args: Dict[str, Any]) -> Dict[str, Any]:
    dht = database.get_esp32_sensor_data(max_age_sec=1800.0)
    if not dht:
        return {"available": False, "note": "Chưa có dữ liệu cảm biến nhiệt độ/độ ẩm gần đây"}
    return {
        "available": True,
        "temperature_c": dht.get("temperature_c"),
        "humidity_pct": dht.get("humidity_pct"),
        "age_sec": max(0, int(time.time() - float(dht.get("updated_at") or time.time()))),
    }


async def _tool_get_speaker_state(args: Dict[str, Any]) -> Dict[str, Any]:
    import esp32_audio
    snap = esp32_audio.get_manager().get_status_snapshot()
    esp = snap.get("esp32") or {}
    cur = snap.get("current_track") or {}
    queue = snap.get("queue") or []
    eq_cfg = snap.get("eq") or {}
    norm_cfg = snap.get("normalize") or {}
    np_pc = database.get_pc_now_playing() or {}
    playback_state = str(snap.get("status") or snap.get("state") or "stopped").strip().lower()
    return {
        "esp32_online": bool(esp.get("online")),
        "esp32_ip": esp.get("ip") or "",
        "esp32_rssi": esp.get("rssi"),
        "ws_connected": bool(esp.get("ws_connected")),
        "stream_clients": int(esp.get("stream_clients") or 0),
        "playback_state": playback_state,
        "is_playing": playback_state == "playing",
        "volume": snap.get("volume"),
        "track_title": cur.get("title") or "",
        "track_artist": cur.get("uploader") or "",
        "track_duration_sec": int(cur.get("duration") or 0) if cur else 0,
        "loading_query": snap.get("loading_query") or "",
        "queue_count": len(queue),
        "queue_preview": [str(q.get("title") or "") for q in queue[:5] if q.get("title")],
        "eq_preset": eq_cfg.get("preset") or "",
        "eq_label": eq_cfg.get("label") or "",
        "normalize": bool(norm_cfg.get("enabled")),
        "auto_dj": bool(snap.get("auto_dj")),
        "sleep_timer_remain_sec": int(snap.get("sleep_timer_remain_sec") or 0),
        "pc_now_playing": np_pc.get("title") if np_pc.get("active") else "",
        "timed_session": snap.get("timed_session"),
        "alarms_count": len(snap.get("alarms") or []),
    }


async def _tool_get_router_state(args: Dict[str, Any]) -> Dict[str, Any]:
    import router_controller
    ctrl = router_controller.get_controller()
    snap = ctrl.get_status_snapshot()
    refresh = bool(args.get("refresh", True))
    if refresh and snap.get("enabled"):
        last_at = float(snap.get("last_checked_at") or 0.0)
        if not snap.get("online") or (time.time() - last_at) > 5.0:
            try:
                await asyncio.wait_for(ctrl.refresh_state(force=True), timeout=2.2)
            except Exception:
                pass
            snap = ctrl.get_status_snapshot()
    clients = []
    for c in (snap.get("clients") or [])[:20]:
        clients.append({
            "label": c.get("label"),
            "ip": c.get("ip"),
            "mac": c.get("mac"),
            "band": c.get("band"),
            "ssid": c.get("ssid"),
            "signal_dbm": c.get("signal_dbm"),
            "rx_rate_mbps": c.get("rx_rate_mbps"),
            "tx_rate_mbps": c.get("tx_rate_mbps"),
            "rx_mb": c.get("rx_mb"),
            "tx_mb": c.get("tx_mb"),
            "is_known": bool(c.get("is_known")),
        })
    return {
        "configured": bool(snap.get("configured")),
        "enabled": bool(snap.get("enabled")),
        "online": bool(snap.get("online")),
        "model": snap.get("model") or "",
        "firmware": snap.get("firmware") or "",
        "uptime_text": snap.get("uptime_text") or "",
        "load_1m": snap.get("load_1m"),
        "ram_used_mb": snap.get("ram_used_mb"),
        "ram_total_mb": snap.get("ram_total_mb"),
        "ram_used_pct": snap.get("ram_used_pct"),
        "conntrack_count": snap.get("conntrack_count"),
        "wisp": snap.get("wisp"),
        "uplinks": snap.get("uplinks") or [],
        "radios": snap.get("radios") or [],
        "clients_count": int(snap.get("clients_count") or 0),
        "unknown_count": int(snap.get("unknown_count") or 0),
        "clients": clients,
        "last_error": snap.get("last_error") or "",
    }


async def _tool_get_room_overview(args: Dict[str, Any]) -> Dict[str, Any]:
    refresh = bool(args.get("refresh", True))
    sub_args = {"refresh": refresh}
    import router_controller
    r_enabled = router_controller.get_router_settings(mask_secret=True).get("enabled")
    tasks = [
        _tool_get_fan_state(sub_args),
        _tool_get_plugs_state(sub_args),
        _tool_get_presence_state({}),
        _tool_get_room_climate({}),
        _tool_get_speaker_state({}),
    ]
    if r_enabled:
        tasks.append(_tool_get_router_state({"refresh": False}))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    fan_res, plugs_res, pres_res, cli_res, spk_res = results[:5]
    out: Dict[str, Any] = {
        "presence": pres_res if isinstance(pres_res, dict) else {"error": str(pres_res)},
        "fan": fan_res if isinstance(fan_res, dict) else {"error": str(fan_res)},
        "plugs": plugs_res if isinstance(plugs_res, dict) else {"error": str(plugs_res)},
        "climate": cli_res if isinstance(cli_res, dict) else {"error": str(cli_res)},
        "speaker": spk_res if isinstance(spk_res, dict) else {"error": str(spk_res)},
    }
    if r_enabled and len(results) > 5 and isinstance(results[5], dict):
        out["router"] = results[5]
    return out


async def _tool_set_fan(args: Dict[str, Any]) -> Dict[str, Any]:
    import fan_controller
    ctrl = fan_controller.get_controller()
    if args.get("freq_hz") is not None and args.get("speed_pct") is None:
        args = dict(args, speed_pct=fan_controller.freq_to_speed_pct(args["freq_hz"]))
    action = str(args.get("action") or "").strip().lower()
    if not action:
        if "power" in args:
            action = "power_on" if bool(args["power"]) else "power_off"
        elif args.get("speed_pct") is not None:
            action = "set_speed"
        elif args.get("mode") in ("fixed", "natural", 0, 1):
            action = "mode_natural" if str(args["mode"]).lower() in ("natural", "1") else "mode_fixed"

    note = await ctrl.execute_ai_fan_action({"action": action, **args}, event_source="user_mcp")
    state = await _tool_get_fan_state({"refresh": False})
    return {"ok": bool(note), "note": note or "Hành động quạt không hợp lệ.", "fan": state}


async def _tool_set_plug(args: Dict[str, Any]) -> Dict[str, Any]:
    import smart_plugs
    ctrl = smart_plugs.get_controller()
    action = str(args.get("action") or "").strip().lower()
    target = args.get("target") or args.get("name") or args.get("mac") or "Đèn phòng"
    if not action and "power" in args:
        action = "power_on" if bool(args["power"]) else "power_off"
    note = await ctrl.execute_ai_plug_action(
        {
            **args,
            "action": action,
            "target": target,
        },
        event_source="user_mcp",
    )
    plugs = await _tool_get_plugs_state({"refresh": False})
    return {"ok": bool(note), "note": note or "Không tìm thấy ổ cắm hoặc hành động không hợp lệ.", **plugs}


async def _tool_control_speaker(args: Dict[str, Any]) -> Dict[str, Any]:
    import esp32_audio
    norm_args = dict(args or {})
    act = str(norm_args.get("action") or "").strip().lower()
    if act == "power" and "power" in norm_args:
        act = "power_on" if bool(norm_args["power"]) else "power_off"
    elif not act:
        if "power" in norm_args:
            act = "power_on" if bool(norm_args["power"]) else "power_off"
        elif norm_args.get("query"):
            act = "play"
        elif norm_args.get("volume") is not None or norm_args.get("value") is not None:
            act = "volume"
        elif norm_args.get("delta") is not None:
            act = "volume_delta"
        elif norm_args.get("eq_preset") or norm_args.get("preset"):
            act = "eq"
    norm_args["action"] = act
    note = await esp32_audio.execute_ai_music_action(norm_args, event_source="user_mcp")
    spk = await _tool_get_speaker_state({})
    return {"ok": bool(note), "note": note or "Lệnh loa không hợp lệ.", "speaker": spk}


async def _tool_get_learned_habits(args: Dict[str, Any]) -> Dict[str, Any]:
    import habit_engine
    if bool(args.get("analyze", False)):
        await asyncio.to_thread(habit_engine.analyze_and_sync_habits)
    snap = habit_engine.get_habits_snapshot()
    return {**snap, "summary": habit_engine.format_habits_for_ai()}


async def _tool_manage_learned_habit(args: Dict[str, Any]) -> Dict[str, Any]:
    import habit_engine
    note = await asyncio.to_thread(habit_engine.execute_ai_habit_action, args)
    snap = habit_engine.get_habits_snapshot()
    return {"ok": bool(note), "note": note or "Không tìm thấy thói quen hoặc lệnh không hợp lệ.", **snap}


async def _tool_control_router(args: Dict[str, Any]) -> Dict[str, Any]:
    import router_controller
    ctrl = router_controller.get_controller()
    act = str(args.get("action") or "refresh").strip().lower()
    if act in ("untrust", "untrust_mac"):
        res = ctrl.untrust_mac(str(args.get("target") or ""))
        return {**res, "router": await _tool_get_router_state({"refresh": False})}
    note = await ctrl.execute_ai_router_action(args)
    snap = await _tool_get_router_state({"refresh": False})
    return {"ok": bool(note and "Từ chối" not in note and "không" not in note.lower()), "note": note or "Lệnh router không hợp lệ.", "router": snap}


# ==================== MCP TOOL SCHEMA REGISTRY ====================

_TOOL_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "get_fan_state",
        "description": "Đọc trạng thái thực tế của Quạt BLDC phòng (Bật/Tắt, tốc độ %, tần số Hz, chế độ Gió tự nhiên/Cố định, trần Hz).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Làm mới trực tiếp từ mạch ESP-01S (mặc định true)."}
            },
        },
        "handler": _tool_get_fan_state,
    },
    {
        "name": "get_plugs_state",
        "description": "Đọc trạng thái bật/tắt và hẹn giờ của tất cả ổ cắm thông minh BroadLink SPmini (Đèn phòng, Quạt thông gió, ...).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Làm mới trực tiếp qua UDP LAN (mặc định true)."}
            },
        },
        "handler": _tool_get_plugs_state,
    },
    {
        "name": "get_presence_state",
        "description": "Kiểm tra chủ nhà hiện đang ở phòng, đang bật PC, hay đang đi vắng khỏi Wi-Fi nhà bao lâu.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_get_presence_state,
    },
    {
        "name": "get_room_climate",
        "description": "Đọc nhiệt độ (°C) và độ ẩm (%) hiện tại trong phòng từ cảm biến DHT11 trên loa ESP32-C3.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_get_room_climate,
    },
    {
        "name": "get_speaker_state",
        "description": "Đọc trạng thái loa ESP32-C3 (nhạc đang phát trên loa hoặc PC, âm lượng, báo thức, phiên Focus/Chill).",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_get_speaker_state,
    },
    {
        "name": "get_room_overview",
        "description": "Đọc gộp toàn bộ trạng thái phòng cùng lúc (Hiện diện + Quạt BLDC + Ổ cắm BroadLink + Nhiệt ẩm + Loa).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Làm mới dữ liệu thiết bị live (mặc định true)."}
            },
        },
        "handler": _tool_get_room_overview,
    },
    {
        "name": "set_fan",
        "description": "Điều khiển hoặc hẹn giờ Quạt BLDC ESP-01S (bật/tắt, chỉnh %/Hz trong trần cho phép, đổi chế độ gió, giảm dần đi ngủ, hẹn giờ bật/tắt/chạy N phút).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["power_on", "power_off", "set_speed", "speed_up", "speed_down", "sleep_ramp", "mode_fixed", "mode_natural", "schedule_on", "schedule_off", "cancel_timer"],
                },
                "speed_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                "freq_hz": {"type": "integer", "minimum": 1005, "maximum": 3000},
                "at_time_vn": {"type": "string", "description": "Mốc giờ VN 'HH:MM' để hẹn bật/tắt (vd '22:00')."},
                "delay_minutes": {"type": "number", "minimum": 1, "maximum": 1440},
                "duration_minutes": {"type": "number", "minimum": 1, "maximum": 1440, "description": "Số phút chạy rồi tự tắt sau khi bật."},
            },
            "required": ["action"],
        },
        "handler": _tool_set_fan,
    },
    {
        "name": "set_plug",
        "description": "Bật/tắt hoặc hẹn giờ ổ cắm BroadLink SPmini theo tên (vd 'Đèn phòng', 'Quạt thông gió') hoặc MAC.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["power_on", "power_off", "toggle", "schedule_off", "schedule_on", "cancel_timer"],
                },
                "target": {"type": "string", "description": "Tên ổ (vd 'Đèn phòng', 'Quạt thông gió', 'all') hoặc MAC."},
                "at_time_vn": {"type": "string", "description": "Mốc giờ VN 'HH:MM' để hẹn bật/tắt (vd '22:00')."},
                "delay_minutes": {"type": "number", "minimum": 1, "maximum": 1440},
                "duration_minutes": {"type": "number", "minimum": 1, "maximum": 1440, "description": "Số phút bật rồi tự tắt (vd bật trong 10p)."},
            },
            "required": ["action", "target"],
        },
        "handler": _tool_set_plug,
    },
    {
        "name": "control_speaker",
        "description": (
            "Điều khiển loa nhạc ESP32-C3: bật phát (play/power_on/resume), tắt (stop/power_off), "
            "tạm dừng (pause/toggle), chuyển bài (next/skip), chỉnh âm lượng (volume/volume_up/volume_down/volume_delta), "
            "chọn EQ, hẹn giờ tắt (sleep_timer), hoặc chế độ Focus/Chill/Nap/Báo thức."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play",
                        "power_on",
                        "power_off",
                        "resume",
                        "pause",
                        "toggle",
                        "skip",
                        "next",
                        "stop",
                        "volume",
                        "volume_up",
                        "volume_down",
                        "volume_delta",
                        "eq",
                        "normalize",
                        "sleep_timer",
                        "focus",
                        "chill",
                        "nap",
                        "schedule_music",
                        "alarm",
                        "dismiss_alarm",
                        "snooze_alarm",
                    ],
                },
                "query": {"type": "string", "description": "Tên bài hát / từ khóa (khi dùng play hoặc schedule_music)."},
                "volume": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Mức âm lượng tuyệt đối 0..100%."},
                "delta": {"type": "integer", "minimum": -100, "maximum": 100, "description": "Mức tăng/giảm âm lượng (+/-%)."},
                "eq_preset": {"type": "string", "enum": ["loudness", "warm", "clear", "flat", "off"], "description": "Tên preset EQ (loudness, warm, clear, flat, off)."},
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 360},
                "delay_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "alarm_time_vn": {"type": "string", "description": "Giờ hẹn dạng HH:MM (giờ VN 24h)."},
                "enabled": {"type": "boolean", "description": "Dùng cho action normalize."},
            },
            "required": ["action"],
        },
        "handler": _tool_control_speaker,
    },
    {
        "name": "get_learned_habits",
        "description": "Đọc danh sách các thói quen bật/tắt tự học (từ hành vi dùng thiết bị & cuộc chat) và trạng thái tự động hóa.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analyze": {"type": "boolean", "description": "Quét phân tích lại lịch sử thao tác ngay trước khi trả về (mặc định false)."}
            },
        },
        "handler": _tool_get_learned_habits,
    },
    {
        "name": "manage_learned_habit",
        "description": "Ghi nhớ, bật lại, tạm tắt (mute), quên/xóa 1 thói quen hoặc bật/tắt công tắc tổng tự động theo thói quen.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["learn", "enable", "mute", "forget", "analyze", "set_master"],
                },
                "habit_key": {"type": "string"},
                "target": {"type": "string"},
                "device_action": {"type": "string"},
                "trigger_time": {"type": "string"},
                "temp_min": {"type": "number"},
                "duration_mins": {"type": "integer"},
                "value": {"type": "number"},
                "pattern": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["action"],
        },
        "handler": _tool_manage_learned_habit,
    },
    {
        "name": "get_router_state",
        "description": "Đọc trạng thái Router Wi-Fi OpenWrt (CPU/RAM/uptime, các đường mạng WWAN WISP & 4G MWAN3, thiết bị Wi-Fi đang kết nối & máy lạ).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Làm mới trực tiếp từ OpenWrt /ubus (mặc định true)."}
            },
        },
        "handler": _tool_get_router_state,
    },
    {
        "name": "control_router",
        "description": "Thao tác an toàn trên Router OpenWrt: làm mới (refresh), bật PC từ xa bằng Wake-on-LAN (wake_pc), đánh dấu quen biết (trust_mac), hoặc ngắt thiết bị lạ ngoài whitelist (kick_client). KHÔNG cho phép reboot hay sửa cấu hình Wi-Fi.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["refresh", "wake_pc", "kick_client", "trust_mac", "untrust_mac"],
                },
                "target": {"type": "string", "description": "MAC, IP hoặc tên máy mục tiêu."},
                "alias": {"type": "string", "description": "Tên gợi nhớ khi dùng trust_mac."},
                "ban_sec": {"type": "integer", "minimum": 0, "maximum": 3600, "description": "Số giây chặn tạm sau khi kick_client."},
            },
            "required": ["action"],
        },
        "handler": _tool_control_router,
    },
]

_TOOL_MAP: Dict[str, Dict[str, Any]] = {t["name"]: t for t in _TOOL_REGISTRY}


def list_mcp_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["inputSchema"],
        }
        for t in _TOOL_REGISTRY
    ]


async def call_mcp_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tool = _TOOL_MAP.get(str(name or "").strip())
    if tool is None:
        raise KeyError(f"Unknown MCP tool: {name}")
    handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]] = tool["handler"]
    return await handler(dict(arguments or {}))


# ==================== NỘI BỘ SMART BUTLER AI (KIỂU A: FETCH_TOOLS) ====================

_AI_TOOL_ALIASES = {
    "fan": "get_fan_state",
    "get_fan": "get_fan_state",
    "fan_state": "get_fan_state",
    "plugs": "get_plugs_state",
    "plug": "get_plugs_state",
    "light": "get_plugs_state",
    "presence": "get_presence_state",
    "climate": "get_room_climate",
    "dht11": "get_room_climate",
    "temp": "get_room_climate",
    "speaker": "get_speaker_state",
    "music": "get_speaker_state",
    "overview": "get_room_overview",
    "room": "get_room_overview",
    "habits": "get_learned_habits",
    "habit": "get_learned_habits",
    "thoi_quen": "get_learned_habits",
    "router": "get_router_state",
    "router_state": "get_router_state",
    "wifi": "get_router_state",
    "mang": "get_router_state",
    "network": "get_router_state",
    "internet": "get_router_state",
    "wan": "get_router_state",
    "clients": "get_router_state",
}


def normalize_ai_fetch_tools(raw_tools: Any) -> List[str]:
    """Chuẩn hóa danh sách `fetch_tools` do Pass 1 đề xuất (chỉ cho phép tool đọc an toàn, tối đa 4 tool)."""
    if not isinstance(raw_tools, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw_tools:
        key = str(item or "").strip().lower()
        canonical = _AI_TOOL_ALIASES.get(key, key)
        if canonical in READ_TOOL_NAMES and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
            if len(out) >= 4:
                break
    return out


def format_ai_tool_results_block(tool_results: Dict[str, Dict[str, Any]]) -> str:
    """Biến JSON kết quả MCP tools thành chỉ dẫn tự nhiên, chống lộ thuật ngữ kỹ thuật cho Pass 2 & Pass 3."""
    if not tool_results:
        return ""
    lines: List[str] = []

    def _fmt_fan(f: Dict[str, Any]) -> None:
        if not f.get("configured"):
            lines.append("- Quạt phòng: Chưa gắn cấu hình điều khiển")
        elif not f.get("online"):
            lines.append("- Quạt phòng: Ngoại tuyến / chưa phản hồi")
        elif f.get("is_on"):
            extra = " (đang giảm dần về 0%)" if f.get("ramping_to_sleep") else ""
            lines.append(
                f"- Quạt phòng: Đang BẬT {f.get('speed_pct')}% ({f.get('output_freq_hz')} Hz), "
                f"chế độ {f.get('mode') or 'Cố định'}{extra}"
            )
        else:
            lines.append(f"- Quạt phòng: Đang TẮT (mức nhớ cũ {f.get('speed_pct', 0)}%)")

    def _fmt_plugs(pl: Dict[str, Any]) -> None:
        for p in pl.get("plugs") or []:
            if p.get("locked"):
                st_txt = "Đang khóa điều khiển LAN"
            elif not p.get("online") or p.get("is_on") is None:
                st_txt = "Ngoại tuyến"
            else:
                st_txt = "Đang BẬT" if p["is_on"] else "Đang TẮT"
            tm = p.get("timer")
            tm_txt = ""
            if isinstance(tm, dict) and tm.get("remaining_sec"):
                mins = max(1, (int(tm["remaining_sec"]) + 59) // 60)
                tm_txt = f" (hẹn {'bật' if tm.get('action') == 'on' else 'tắt'} sau {mins}p)"
            lines.append(f"- {p.get('name')}: {st_txt}{tm_txt}")

    def _fmt_presence(pr: Dict[str, Any]) -> None:
        if pr.get("summary"):
            lines.append(f"- Hiện diện phòng: {pr['summary']}")

    def _fmt_climate(cl: Dict[str, Any]) -> None:
        if cl.get("available"):
            lines.append(f"- Nhiệt độ & độ ẩm phòng: {cl.get('temperature_c')}°C, độ ẩm {cl.get('humidity_pct')}%")
        else:
            lines.append("- Nhiệt độ & độ ẩm phòng: Chưa có số liệu cảm biến gần đây")

    def _fmt_speaker(sp: Dict[str, Any]) -> None:
        st = str(sp.get("playback_state") or "stopped").lower()
        vol = sp.get("volume")
        vol_txt = f"âm lượng {vol}%" if vol is not None else ""
        title = sp.get("track_title") or ""
        artist = f" ({sp['track_artist']})" if sp.get("track_artist") else ""
        extras: List[str] = []
        if vol_txt:
            extras.append(vol_txt)
        if sp.get("eq_label"):
            extras.append(f"EQ {sp['eq_label']}")
        q_cnt = int(sp.get("queue_count") or 0)
        if q_cnt > 0:
            extras.append(f"hàng đợi còn {q_cnt} bài")
        slp = int(sp.get("sleep_timer_remain_sec") or 0)
        if slp > 0:
            extras.append(f"hẹn tắt sau {max(1, (slp + 59) // 60)}p")
        sess = sp.get("timed_session")
        if isinstance(sess, dict) and sess.get("remaining_sec"):
            extras.append(f"chế độ {sess.get('label') or sess.get('mode')} còn {max(1, (int(sess['remaining_sec']) + 59) // 60)}p")
        extra_str = f" ({', '.join(extras)})" if extras else ""

        if title and st == "playing":
            lines.append(f"- Loa nhạc phòng: Đang phát \"{title}\"{artist}{extra_str}")
        elif title and st == "paused":
            lines.append(f"- Loa nhạc phòng: Đang tạm dừng bài \"{title}\"{artist}{extra_str}")
        elif st == "loading" and sp.get("loading_query"):
            lines.append(f"- Loa nhạc phòng: Đang tìm & tải bài \"{sp['loading_query']}\"{extra_str}")
        elif sp.get("esp32_online"):
            lines.append(f"- Loa phòng: Đang bật chờ, chưa phát nhạc{extra_str}")
        else:
            lines.append(f"- Loa phòng: Ngoại tuyến{extra_str}")

        if sp.get("pc_now_playing"):
            lines.append(f"- Nhạc trên máy tính: Đang nghe \"{sp['pc_now_playing']}\"")

    def _fmt_router(rt: Dict[str, Any]) -> None:
        if not rt.get("configured") or not rt.get("enabled"):
            lines.append("- Router Wi-Fi: Chưa kích hoạt kết nối quản lý")
            return
        if not rt.get("online"):
            lines.append(f"- Router Wi-Fi: Không kết nối được ({rt.get('last_error') or 'offline'})")
            return
        uplink_parts: List[str] = []
        for u in (rt.get("uplinks") or []):
            st_u = u.get("mwan_status") or ("online" if u.get("up") else "offline")
            extra_u = f" '{u['wisp_ssid']}' ({u['signal_dbm']} dBm)" if u.get("wisp_ssid") else ""
            lat_u = f", {u['latency_ms']}ms" if u.get("latency_ms") is not None else ""
            uplink_parts.append(f"{u.get('name')}: {st_u}{extra_u}{lat_u}")
        uplink_txt = "; ".join(uplink_parts) if uplink_parts else "đang kiểm tra"
        unknowns = [c for c in (rt.get("clients") or []) if not c.get("is_known")]
        unk_txt = (
            f"; có {len(unknowns)} máy lạ: " + ", ".join(f"{u.get('label')} ({u.get('band')})" for u in unknowns[:3])
            if unknowns
            else "; không có thiết bị lạ"
        )
        known_names = ", ".join(
            f"{c.get('label')} ({c.get('band')})" for c in (rt.get("clients") or []) if c.get("is_known")
        )
        lines.append(
            f"- Router Wi-Fi ({rt.get('model') or 'OpenWrt'}, bật {rt.get('uptime_text')}, tải {rt.get('load_1m')}, RAM {rt.get('ram_used_pct')}%): "
            f"Đường mạng [{uplink_txt}]; tổng {rt.get('clients_count', 0)} máy đang vào Wi-Fi{unk_txt}"
            + (f" [{known_names}]" if known_names else "")
        )

    if "get_room_overview" in tool_results:
        ov = tool_results["get_room_overview"]
        if isinstance(ov.get("presence"), dict):
            _fmt_presence(ov["presence"])
        if isinstance(ov.get("fan"), dict):
            _fmt_fan(ov["fan"])
        if isinstance(ov.get("plugs"), dict):
            _fmt_plugs(ov["plugs"])
        if isinstance(ov.get("climate"), dict):
            _fmt_climate(ov["climate"])
        if isinstance(ov.get("speaker"), dict):
            _fmt_speaker(ov["speaker"])
        if isinstance(ov.get("router"), dict):
            _fmt_router(ov["router"])
    else:
        if "get_presence_state" in tool_results:
            _fmt_presence(tool_results["get_presence_state"])
        if "get_fan_state" in tool_results:
            _fmt_fan(tool_results["get_fan_state"])
        if "get_plugs_state" in tool_results:
            _fmt_plugs(tool_results["get_plugs_state"])
        if "get_room_climate" in tool_results:
            _fmt_climate(tool_results["get_room_climate"])
        if "get_speaker_state" in tool_results:
            _fmt_speaker(tool_results["get_speaker_state"])
        if "get_router_state" in tool_results:
            _fmt_router(tool_results["get_router_state"])

    if "get_learned_habits" in tool_results:
        hb_sum = str((tool_results["get_learned_habits"] or {}).get("summary") or "").strip()
        if hb_sum:
            lines.append("- Thói quen phòng đã học:\n" + hb_sum)

    if not lines:
        return ""
    return (
        "[TRẠNG THÁI THỰC TẾ VỪA TRA CỨU TỪ MCP TOOLS:\n"
        + "\n".join(lines)
        + "\nQuy tắc: Trả lời tự nhiên đúng phong cách quản gia/trợ lý (chỉ nêu % hoặc Hz khi chủ nhà hỏi rõ mức; "
        "nếu ghi 'Ngoại tuyến' thì báo thật chưa đọc được, TUYỆT ĐỐI KHÔNG tự bịa hay nhắc tới ESP-01S/BroadLink/MCP/IP/MAC/API/UDP).]"
    )


async def execute_fetch_tools_for_ai(raw_tools: Any, timeout_sec: float = 1.6) -> str:
    """Chạy song song các tool mà Pass 1 AI tự chọn và trả về block text chuẩn cho Pass 2 & Pass 3."""
    tools = normalize_ai_fetch_tools(raw_tools)
    if not tools:
        return ""
    if "get_room_overview" in tools:
        tools = ["get_room_overview", "get_learned_habits"] if "get_learned_habits" in tools else ["get_room_overview"]

    coros = [call_mcp_tool(t, {"refresh": True}) for t in tools]
    try:
        results = await asyncio.wait_for(asyncio.gather(*coros, return_exceptions=True), timeout=timeout_sec)
    except Exception:
        results = [TimeoutError("timeout") for _ in tools]

    mapped: Dict[str, Dict[str, Any]] = {}
    for name, res in zip(tools, results):
        if isinstance(res, dict):
            mapped[name] = res
    return format_ai_tool_results_block(mapped)


# ==================== STREAMABLE HTTP JSON-RPC 2.0 HANDLER ====================

async def _handle_single_jsonrpc(req_obj: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], int]:
    req_id = req_obj.get("id")
    method = str(req_obj.get("method") or "").strip()
    params = req_obj.get("params") if isinstance(req_obj.get("params"), dict) else {}

    def _ok(result: Any) -> Tuple[Optional[Dict[str, Any]], int]:
        if req_id is None and "id" not in req_obj:
            return None, 202
        return {"jsonrpc": "2.0", "id": req_id, "result": result}, 200

    def _err(code: int, msg: str) -> Tuple[Optional[Dict[str, Any]], int]:
        if req_id is None and "id" not in req_obj:
            return None, 202
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}, 200

    if method == "initialize":
        req_ver = str(params.get("protocolVersion") or "").strip()
        chosen_ver = req_ver if req_ver in MCP_SUPPORTED_PROTOCOLS else MCP_SUPPORTED_PROTOCOLS[0]
        return _ok({
            "protocolVersion": chosen_ver,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": MCP_SERVER_NAME,
                "version": MCP_SERVER_VERSION,
            },
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None, 202

    if method == "ping":
        return _ok({})

    if method == "tools/list":
        return _ok({"tools": list_mcp_tools()})

    if method == "tools/call":
        t_name = str(params.get("name") or "").strip()
        t_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not t_name:
            return _err(-32602, "Missing tool name in params.name")
        if t_name not in _TOOL_MAP:
            return _err(-32602, f"Unknown tool: {t_name}")
        try:
            data = await call_mcp_tool(t_name, t_args)
            return _ok({
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            })
        except Exception as e:
            logger.warning(f"MCP tools/call '{t_name}' error: {e}")
            return _ok({
                "content": [{"type": "text", "text": f"Error executing {t_name}: {e}"}],
                "isError": True,
            })

    return _err(-32601, f"Method not found: {method}")


async def handle_mcp_jsonrpc_payload(raw_body: Any) -> Tuple[Any, int, Dict[str, str]]:
    """Xử lý request payload JSON-RPC 2.0 đơn hoặc batch cho route `/mcp`."""
    headers = {"Mcp-Session-Id": str(uuid.uuid4()), "Cache-Control": "no-store"}
    if isinstance(raw_body, list):
        if not raw_body:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Empty batch"}}, 400, headers
        replies = []
        for item in raw_body:
            if isinstance(item, dict):
                resp_item, _ = await _handle_single_jsonrpc(item)
                if resp_item is not None:
                    replies.append(resp_item)
        if not replies:
            return None, 202, headers
        return replies, 200, headers

    if not isinstance(raw_body, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}, 400, headers

    resp_obj, status = await _handle_single_jsonrpc(raw_body)
    return resp_obj, status, headers
