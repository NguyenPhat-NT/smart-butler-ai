# -*- coding: utf-8 -*-
"""
Web Dashboard nội bộ qua aiohttp.web (nhúng chung tiến trình bot.py tại cổng 8080):
- Xác thực PIN nhẹ bằng Cookie ký HMAC-SHA256 + chống brute-force theo IP.
- Đẩy thông báo tại chỗ qua SSE (`/api/events`), quản trị CRUD Ký ức/Hồ sơ/Lịch hẹn,
  quét thiết bị LAN 1-click gán MAC, Backup/Restore DB và theo dõi Watchdog.
"""
import os
import hmac
import json
import glob
import time
import shutil
import tempfile
import hashlib
import secrets
import platform
import asyncio
import datetime
import ipaddress
import logging
from typing import Dict, Any, Optional, Tuple
from aiohttp import web, tcp_helpers, WSMsgType

import config
import database
import gemini_client
import tts_client
import lan_events
import lan_presence
import lan_watchdog
import backup_helper
import circadian
import proactive_engine
import storage_helper
import esp32_audio
import fan_controller
import smart_plugs
import mcp_tools
import habit_engine
import persona

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=getattr(config, "TIMEZONE_OFFSET_HOURS", 7)))

APP_BOT_KEY: web.AppKey = web.AppKey("bot", object)
APP_TARGET_UID_KEY: web.AppKey = web.AppKey("target_user_id", int)
APP_RUNTIME_KEY: web.AppKey = web.AppKey("runtime_state", dict)


def _get_runtime(app: web.Application) -> Dict[str, Any]:
    rt = app.get(APP_RUNTIME_KEY)
    if rt is None:
        rt = {
            "sse_queues": set(),
            "esp32_ws_clients": set(),
            "esp32_stream_tasks": set(),
            "shutting_down": False,
        }
        app[APP_RUNTIME_KEY] = rt
    return rt

_COOKIE_NAME = "ds_session"
_SESSION_TTL_SEC = 30 * 86400
_LOGIN_WINDOW_SEC = 60.0
_LOGIN_MAX_FAILS = 5
_LOGIN_FAILS: Dict[str, Tuple[int, float]] = {}  # ip -> (fail_count, blocked_until)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_MAX_JSON_BODY_BYTES = 128 * 1024
_PROCESS_STARTED_AT = time.time()
_CPU_PREV_SAMPLE: Optional[Tuple[int, int, float]] = None
_CPU_LAST_PCT: Optional[float] = None


def _read_proc_cpu_totals() -> Optional[Tuple[int, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline().strip()
        if not first.startswith("cpu "):
            return None
        parts = [int(x) for x in first.split()[1:9]]
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except Exception:
        return None


def _sample_cpu_percent() -> Optional[float]:
    global _CPU_PREV_SAMPLE, _CPU_LAST_PCT
    s1 = _read_proc_cpu_totals()
    if s1 is None:
        return None
    now = time.monotonic()
    if _CPU_PREV_SAMPLE is None:
        time.sleep(0.08)
        s2 = _read_proc_cpu_totals()
        if s2 is None:
            return None
        dt = s2[0] - s1[0]
        didle = s2[1] - s1[1]
        pct = round(max(0.0, min(100.0, (1.0 - didle / dt) * 100.0)), 1) if dt > 0 else 0.0
        _CPU_PREV_SAMPLE = (s2[0], s2[1], time.monotonic())
        _CPU_LAST_PCT = pct
        return pct

    prev_total, prev_idle, prev_ts = _CPU_PREV_SAMPLE
    if (now - prev_ts) >= 0.4:
        dt = s1[0] - prev_total
        didle = s1[1] - prev_idle
        if dt > 0:
            _CPU_LAST_PCT = round(max(0.0, min(100.0, (1.0 - didle / dt) * 100.0)), 1)
        _CPU_PREV_SAMPLE = (s1[0], s1[1], now)
    return _CPU_LAST_PCT


def _read_meminfo_and_rss() -> Dict[str, Any]:
    mem: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                num_str = v.strip().split()[0]
                if num_str.isdigit():
                    mem[k.strip()] = int(num_str)
    except Exception:
        pass

    ram = None
    swap = None
    if mem.get("MemTotal"):
        total_mb = round(mem["MemTotal"] / 1024.0, 1)
        avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        avail_mb = round(avail_kb / 1024.0, 1)
        used_mb = round(max(0.0, total_mb - avail_mb), 1)
        pct = round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
        ram = {"total_mb": total_mb, "used_mb": used_mb, "avail_mb": avail_mb, "pct": pct}
    if mem.get("SwapTotal") is not None:
        st_mb = round(mem.get("SwapTotal", 0) / 1024.0, 1)
        sf_mb = round(mem.get("SwapFree", 0) / 1024.0, 1)
        su_mb = round(max(0.0, st_mb - sf_mb), 1)
        spct = round((su_mb / st_mb) * 100.0, 1) if st_mb > 0 else 0.0
        swap = {"total_mb": st_mb, "used_mb": su_mb, "free_mb": sf_mb, "pct": spct}

    bot_rss_mb = None
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split(":")[1].strip().split()[0])
                    bot_rss_mb = round(kb / 1024.0, 1)
                    break
    except Exception:
        pass
    return {"ram": ram, "swap": swap, "bot_rss_mb": bot_rss_mb}


def _read_soc_temperature() -> Tuple[Optional[float], str]:
    best_temp: Optional[float] = None
    best_zone = ""
    cpu_temp: Optional[float] = None
    cpu_zone = ""
    for zone_dir in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            with open(os.path.join(zone_dir, "temp"), "r", encoding="utf-8") as tf:
                raw = float(tf.read().strip())
            temp_c = round(raw / 1000.0 if raw > 200.0 else raw, 1)
            ztype = ""
            type_path = os.path.join(zone_dir, "type")
            if os.path.exists(type_path):
                with open(type_path, "r", encoding="utf-8") as yf:
                    ztype = yf.read().strip()
            if "cpu" in ztype.lower():
                if cpu_temp is None or temp_c > cpu_temp:
                    cpu_temp, cpu_zone = temp_c, ztype
            if best_temp is None or temp_c > best_temp:
                best_temp, best_zone = temp_c, ztype
        except Exception:
            continue
    return (cpu_temp, cpu_zone) if cpu_temp is not None else (best_temp, best_zone)


def _get_system_health_snapshot(usb_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cpu_pct = _sample_cpu_percent()
    mem_data = _read_meminfo_and_rss()
    temp_c, temp_zone = _read_soc_temperature()

    load_avg = None
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            parts = f.read().split()[:3]
            if len(parts) == 3:
                load_avg = [round(float(x), 2) for x in parts]
    except Exception:
        if hasattr(os, "getloadavg"):
            try:
                load_avg = [round(x, 2) for x in os.getloadavg()]
            except Exception:
                load_avg = None

    uptime_sec = None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            uptime_sec = int(float(f.read().split()[0]))
    except Exception:
        pass

    root_disk = None
    try:
        target = "/" if os.name != "nt" else os.path.dirname(os.path.abspath(__file__))
        du = shutil.disk_usage(target)
        total_gb = round(du.total / (1024 ** 3), 2)
        used_gb = round(du.used / (1024 ** 3), 2)
        free_gb = round(du.free / (1024 ** 3), 2)
        pct = round((du.used / du.total) * 100.0, 1) if du.total > 0 else 0.0
        root_disk = {"total_gb": total_gb, "used_gb": used_gb, "free_gb": free_gb, "pct": pct}
    except Exception:
        pass

    dsp_c_loaded = False
    try:
        import esp32_dsp
        dsp_c_loaded = bool(getattr(esp32_dsp, "_C_LIB", None) is not None or esp32_dsp._load_c_dsp_lib() is not None)
    except Exception:
        pass

    usb = usb_snapshot or storage_helper.get_usb_storage_snapshot()
    usb_mounted = bool(usb.get("mounted"))
    usb_avail = bool(usb_mounted and usb.get("healthy"))
    usb_total_mb = round(float(usb.get("total_bytes") or 0) / (1024.0 * 1024.0), 1) if usb_mounted else None
    usb_free_mb = round(float(usb.get("free_bytes") or 0) / (1024.0 * 1024.0), 1) if usb_mounted else None
    return {
        "hostname": platform.node() or os.name,
        "cpu_pct": cpu_pct,
        "cpu_cores": os.cpu_count() or 1,
        "load_avg": load_avg,
        "ram": mem_data["ram"],
        "swap": mem_data["swap"],
        "bot_rss_mb": mem_data["bot_rss_mb"],
        "temp_c": temp_c,
        "temp_zone": temp_zone,
        "uptime_sec": uptime_sec,
        "process_uptime_sec": int(max(0.0, time.time() - _PROCESS_STARTED_AT)),
        "disk_root": root_disk,
        "usb": {
            "available": usb_avail,
            "mounted": usb_mounted,
            "readonly": bool(usb.get("readonly")),
            "last_error": str(usb.get("last_error") or ""),
            "total_mb": usb_total_mb,
            "free_mb": usb_free_mb,
            "mount_dir": usb.get("mount_point"),
        },
        "dsp_c_loaded": dsp_c_loaded,
        "logic_audit_enabled": bool(getattr(config, "LOGIC_AUDIT_ENABLED", True)),
    }


def _resolve_user_id(app: Optional[web.Application] = None) -> int:
    if config.ALLOWED_USER_ID and config.ALLOWED_USER_ID > 0:
        return int(config.ALLOWED_USER_ID)
    if app is not None and int(app.get(APP_TARGET_UID_KEY) or 0) > 0:
        return int(app[APP_TARGET_UID_KEY])
    return database.get_primary_user_id()


def get_effective_pin() -> str:
    """Lấy PIN từ .env; nếu bỏ trống thì tự sinh 6 số lưu bền vững trong app_metadata."""
    if config.DASHBOARD_PIN:
        return config.DASHBOARD_PIN
    stored = database.get_metadata("dashboard_pin_auto")
    if stored and stored.strip():
        return stored.strip()
    auto_pin = f"{secrets.randbelow(900000) + 100000}"
    database.set_metadata("dashboard_pin_auto", auto_pin)
    return auto_pin


def _signing_key() -> bytes:
    pin = get_effective_pin()
    material = f"{config.BOT_TOKEN}:ds_dashboard:{pin}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _create_session_token() -> str:
    exp = int(time.time()) + _SESSION_TTL_SEC
    nonce = secrets.token_hex(12)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: Optional[str]) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp_s, nonce, sig = token.split(".", 2)
    if not exp_s.isdigit() or not nonce:
        return False
    if int(exp_s) < int(time.time()):
        return False
    payload = f"{exp_s}.{nonce}"
    expected = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _client_ip(request: web.Request) -> str:
    ip = (request.remote or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip or "unknown"


def _pin_matches(candidate: Any, expected: Any) -> bool:
    """So sánh PIN an toàn theo thời gian hằng, chấp nhận cả chuỗi Unicode."""
    try:
        return hmac.compare_digest(str(candidate or "").encode("utf-8"), str(expected or "").encode("utf-8"))
    except Exception:
        return False


def _pin_block_remain(ip: str, now: float) -> float:
    _, blocked_until = _LOGIN_FAILS.get(ip, (0, 0.0))
    return max(0.0, blocked_until - now)


def _register_pin_failure(ip: str, now: float) -> int:
    """Ghi nhận 1 lần sai PIN; trả về số lần sai hiện tại (0 nếu vừa bị khóa)."""
    fails, _ = _LOGIN_FAILS.get(ip, (0, 0.0))
    fails += 1
    if fails >= _LOGIN_MAX_FAILS:
        _LOGIN_FAILS[ip] = (0, now + _LOGIN_WINDOW_SEC)
        return 0
    _LOGIN_FAILS[ip] = (fails, 0.0)
    return fails


def _purge_expired_login_fails(now: float) -> None:
    """Dọn các entry hết hạn để dict không phình mãi."""
    if len(_LOGIN_FAILS) < 32:
        return
    for ip, (fails, blocked_until) in list(_LOGIN_FAILS.items()):
        if blocked_until and blocked_until <= now:
            _LOGIN_FAILS.pop(ip, None)


def _json_ok(data: Any = None, status: int = 200) -> web.Response:
    return web.json_response(
        {"ok": True, "data": data if data is not None else {}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _json_err(message: str, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "error": str(message)},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


async def _parse_json(request: web.Request) -> Dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_private_lan_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip_str or "").strip())
        return bool(addr.is_private or addr.is_loopback)
    except ValueError:
        return False


def _matches_pin_header(request: web.Request) -> bool:
    ip = _client_ip(request)
    now = time.time()
    if _pin_block_remain(ip, now) > 0:
        return False
    hdr_pin = (request.headers.get("X-Dashboard-Pin") or "").strip()
    if not hdr_pin:
        auth_hdr = (request.headers.get("Authorization") or "").strip()
        if auth_hdr.lower().startswith("bearer "):
            hdr_pin = auth_hdr[7:].strip()
    if not hdr_pin:
        return False
    if _pin_matches(hdr_pin, get_effective_pin()):
        _LOGIN_FAILS.pop(ip, None)
        return True
    _register_pin_failure(ip, now)
    return False


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path

    # Chặn payload quá lớn ở các route JSON thường (route restore có giới hạn riêng)
    if request.method in ("POST", "PUT", "PATCH") and path != "/api/backups/restore":
        cl = request.content_length or 0
        if cl > _MAX_JSON_BODY_BYTES:
            return _json_err("Payload quá lớn.", status=413)

    if path in ("/", "/api/health", "/api/login", "/api/logout", "/api/auth/check"):
        return await handler(request)

    if path == "/mcp":
        token = request.cookies.get(_COOKIE_NAME)
        if _verify_session_token(token):
            return await handler(request)
        has_proxy_hdr = bool(request.headers.get("X-Forwarded-For") or request.headers.get("Forwarded"))
        if (not has_proxy_hdr and _is_private_lan_ip(_client_ip(request))) or _matches_pin_header(request):
            return await handler(request)
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Unauthorized: provide X-Dashboard-Pin or Authorization Bearer PIN."},
            },
            status=401,
            headers={"Cache-Control": "no-store"},
        )

    if path.startswith("/api/"):
        token = request.cookies.get(_COOKIE_NAME)
        if _verify_session_token(token):
            return await handler(request)
        if path == "/api/lan/now_playing" and _matches_pin_header(request):
            return await handler(request)
        if path in ("/api/esp32/stream", "/api/esp32/ws", "/api/esp32/event", "/api/esp32/telemetry", "/api/esp32/status"):
            has_proxy_hdr = bool(request.headers.get("X-Forwarded-For") or request.headers.get("Forwarded"))
            if (not has_proxy_hdr and _is_private_lan_ip(_client_ip(request))) or _matches_pin_header(request):
                return await handler(request)
        return _json_err("Chưa đăng nhập hoặc phiên hết hạn.", status=401)
    return await handler(request)


async def handle_index(request: web.Request) -> web.StreamResponse:
    html_path = os.path.join(_STATIC_DIR, "dashboard.html")
    if not os.path.isfile(html_path):
        return web.Response(text="Dashboard HTML không tồn tại.", status=404)
    return web.FileResponse(html_path, headers={"Cache-Control": "no-store"})


async def handle_health(request: web.Request) -> web.Response:
    return _json_ok({"service": "smart_butler", "status": "ok"})


async def handle_auth_check(request: web.Request) -> web.Response:
    authed = _verify_session_token(request.cookies.get(_COOKIE_NAME))
    return _json_ok({"authenticated": authed})


async def handle_login(request: web.Request) -> web.Response:
    ip = _client_ip(request)
    now = time.time()
    _purge_expired_login_fails(now)
    remain = _pin_block_remain(ip, now)
    if remain > 0:
        return _json_err(f"Nhập sai PIN nhiều lần, vui lòng đợi {int(remain) + 1}s.", status=429)

    body = await _parse_json(request)
    pin_input = str(body.get("pin") or "").strip()
    if not pin_input or not _pin_matches(pin_input, get_effective_pin()):
        fails = _register_pin_failure(ip, now)
        if fails == 0:
            return _json_err("Sai PIN quá 5 lần, tạm khóa 60 giây.", status=429)
        return _json_err(f"PIN chưa đúng (lần {fails}/{_LOGIN_MAX_FAILS}).", status=401)

    _LOGIN_FAILS.pop(ip, None)
    token = _create_session_token()
    resp = _json_ok({"authenticated": True})
    resp.set_cookie(
        _COOKIE_NAME,
        token,
        max_age=_SESSION_TTL_SEC,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    resp = _json_ok({"authenticated": False})
    resp.del_cookie(_COOKIE_NAME, path="/")
    return resp


def _build_overview_payload(client_ip: str, app: Optional[web.Application] = None) -> Dict[str, Any]:
    uid = _resolve_user_id(app)
    profile = database.get_or_create_user(uid) if uid > 0 else {}
    presence = lan_presence.get_presence_snapshot()
    watchdog = lan_watchdog.get_watchdog_snapshot()
    quota = gemini_client.get_api_quota_status()
    backup = backup_helper.get_backup_summary()
    mem_counts = database.list_memories_filtered(uid, limit=1) if uid > 0 else {"total": 0}
    chats_cnt = database.get_chat_history_paginated(uid, limit=1) if uid > 0 else {"total": 0}
    pending_rems = database.get_pending_reminders(uid, limit=50) if uid > 0 else []

    usb_snapshot = storage_helper.get_usb_storage_snapshot()
    system_snapshot = _get_system_health_snapshot(usb_snapshot=usb_snapshot)

    return {
        "user_id": uid,
        "client_ip": client_ip,
        "presence": presence,
        "watchdog": watchdog,
        "system": system_snapshot,
        "tts": {
            "effective_url": lan_presence.get_effective_tts_url() or config.TTS_API_URL,
            "configured_url": config.TTS_API_URL,
            "online": bool(presence.get("tts_online")),
            "checked_at": presence.get("tts_checked_at") or 0.0,
        },
        "music_dl": {
            "effective_url": lan_presence.get_effective_music_dl_url() or "",
            "configured_url": lan_presence.get_configured_music_dl_url() or "",
            "online": bool(presence.get("music_dl_online")),
            "checked_at": presence.get("music_dl_checked_at") or 0.0,
        },
        "circadian": circadian.get_circadian_state(),
        "now_playing": database.get_pc_now_playing(),
        "jukebox": esp32_audio.get_manager().get_status_snapshot(),
        "fan": fan_controller.get_controller().get_status_snapshot(),
        "plugs": smart_plugs.get_controller().get_status_snapshot(),
        "habits": habit_engine.get_habits_snapshot(),
        "dht11": database.get_esp32_sensor_data(max_age_sec=3600.0),
        "quota": quota,
        "backup": backup,
        "usb_storage": usb_snapshot,
        "counts": {
            "memories": mem_counts["total"],
            "chats": chats_cnt["total"],
            "pending_reminders": len(pending_rems),
        },
        "profile": profile,
        "persona": persona.get_persona_config(),
        "notifications": lan_events.get_recent_notifications(25),
        "settings": {
            "lan": lan_presence.get_lan_settings(),
            "watchdog": lan_watchdog.get_watchdog_settings(),
            "backup": backup_helper.get_backup_settings(),
            "fan": fan_controller.get_fan_settings(),
        },
    }


async def handle_overview(request: web.Request) -> web.Response:
    # Chạy trong thread để không chặn event loop (SQLite + os.listdir + đọc settings)
    payload = await asyncio.to_thread(_build_overview_payload, _client_ip(request), request.app)
    return _json_ok(payload)


async def handle_sse_events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    q = lan_events.subscribe()
    rt = _get_runtime(request.app)
    sse_queues = rt["sse_queues"]
    sse_queues.add(q)
    try:
        init_payload = json.dumps(
            {"presence": lan_presence.get_presence_snapshot(), "watchdog": lan_watchdog.get_watchdog_snapshot()},
            ensure_ascii=False,
        )
        await resp.write(f"event: init\ndata: {init_payload}\n\n".encode("utf-8"))
        while not rt.get("shutting_down"):
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15.0)
                if evt is None:
                    # Sentinel: subscriber bị loại do vượt giới hạn -> đóng kết nối để không treo task
                    break
                data_s = json.dumps(evt["data"], ensure_ascii=False)
                await resp.write(f"event: {evt['type']}\ndata: {data_s}\n\n".encode("utf-8"))
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as e:
        logger.debug(f"SSE kết thúc: {type(e).__name__}: {e}")
    finally:
        sse_queues.discard(q)
        lan_events.unsubscribe(q)
    return resp


async def handle_notify_test(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    speak = bool(body.get("speak_tts"))
    butler_name = persona.get_persona_config()["butler_name"]
    title = str(body.get("title") or f"{butler_name} • Thông báo thử").strip()[:100]
    msg = str(body.get("body") or "Thông báo tại chỗ qua Wi-Fi nội bộ đã sẵn sàng!").strip()[:300]
    item = lan_events.emit_notify(title=title, body=msg, level="info", source="test", speak_tts=speak)
    return _json_ok(item)


def _parse_tts_audition_params(params: Any) -> dict:
    mood = str(params.get("mood") or "").strip() or None
    tod = str(params.get("time_of_day") or "").strip() or None
    prosody_raw = str(params.get("prosody") if params.get("prosody") is not None else "1").strip().lower()
    prosody = prosody_raw not in ("0", "false", "no", "off")
    intensity = None
    if params.get("intensity") not in (None, ""):
        try:
            intensity = max(0.25, min(1.45, float(params.get("intensity"))))
        except (TypeError, ValueError):
            intensity = None
    return {"mood": mood, "time_of_day": tod, "intensity": intensity, "prosody": prosody}


async def handle_tts_speak_alert(request: web.Request) -> web.Response:
    text = (request.query.get("text") or "").strip()[:480]
    if not text:
        return _json_err("Thiếu tham số text.", 400)
    opts = _parse_tts_audition_params(request.query)
    res = await tts_client.synthesize_voice(text, timeout_sec=24.0, **opts)
    if not res:
        return _json_err("VieNeu-TTS trên PC đang tắt hoặc không phản hồi.", 503)
    audio_bytes, fmt = res
    ctype = "audio/wav" if fmt == "wav" else "audio/ogg"
    return web.Response(body=audio_bytes, content_type=ctype, headers={"Cache-Control": "no-store"})


async def handle_tts_audition_esp32(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    text = str(body.get("text") or "").strip()[:480]
    if not text:
        return _json_err("Thiếu nội dung thoại cần đọc thử.", 400)
    opts = _parse_tts_audition_params(body)
    res = await tts_client.synthesize_voice(text, timeout_sec=24.0, **opts)
    if not res:
        return _json_err("VieNeu-TTS trên PC đang tắt hoặc không phản hồi.", 503)
    audio_bytes, fmt = res
    mgr = esp32_audio.get_manager()
    if not (mgr.is_esp32_online() or mgr.active_ws_clients > 0 or mgr.active_stream_clients > 0):
        return _json_err("Loa ESP32-C3 hiện chưa kết nối.", 409)
    ok = await mgr.enqueue_voice_overlay(audio_bytes, fmt or "ogg")
    if not ok:
        return _json_err("Không phát được âm thanh ra loa ESP32.", 500)
    return _json_ok({
        "bytes": len(audio_bytes),
        "format": fmt or "ogg",
        "mood": opts["mood"] or "auto",
        "intensity": opts["intensity"] if opts["intensity"] is not None else 1.0,
    })


async def handle_memories_list(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_ok({"items": [], "total": 0})
    search = request.query.get("search", "")
    category = request.query.get("category", "all")
    try:
        limit = int(request.query.get("limit", "60"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return _json_err("limit/offset không hợp lệ.", 400)
    return _json_ok(database.list_memories_filtered(uid, search=search, category=category, limit=limit, offset=offset))


async def _compute_embedding_best_effort(text: str) -> Optional[bytes]:
    try:
        import vector_memory
        vec = await asyncio.wait_for(vector_memory.get_text_embedding(text), timeout=4.5)
        if vec:
            return vector_memory.pack_vector(vec)
    except Exception as e:
        logger.debug(f"Tạo embedding nhanh từ Dashboard bỏ qua: {e}")
    return None


async def handle_memories_create(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_err("Chưa có user_id. Hãy bấm /start với bot trên Telegram trước.", 400)
    body = await _parse_json(request)
    fact = str(body.get("fact") or "").strip()
    if len(fact) < 3:
        return _json_err("Nội dung ký ức cần tối thiểu 3 ký tự.", 400)
    category = str(body.get("category") or "personal").strip() or "personal"
    try:
        imp = int(body.get("importance") or 2)
    except (TypeError, ValueError):
        imp = 2

    emb = await _compute_embedding_best_effort(fact)
    mem_id = database.add_memory(uid, fact=fact, category=category, embedding=emb, importance=imp)
    if not mem_id:
        return _json_err("Ký ức này đã tồn tại hoặc không hợp lệ.", 400)
    # Chỉ gỡ khỏi Sổ từ chối SAU khi thêm thành công (tránh xóa dấu vết dù thêm thất bại)
    database.unreject_memory(uid, fact)
    return _json_ok({"id": mem_id, "embedded": bool(emb)}, status=201)


async def handle_memories_update(request: web.Request) -> web.Response:
    try:
        mem_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json_err("ID ký ức không hợp lệ.", 400)
    body = await _parse_json(request)
    fact = str(body.get("fact") or "").strip()
    if len(fact) < 3:
        return _json_err("Nội dung ký ức cần tối thiểu 3 ký tự.", 400)
    category = body.get("category")
    imp = body.get("importance")
    try:
        importance_val = int(imp) if imp is not None and str(imp).strip() != "" else None
    except (TypeError, ValueError):
        return _json_err("Độ quan trọng phải là số từ 1 đến 3.", 400)
    emb = await _compute_embedding_best_effort(fact)
    updated = database.update_memory_fact(
        memory_id=mem_id,
        new_fact=fact,
        category=str(category).strip() if category else None,
        embedding=emb,
        importance=importance_val,
    )
    if not updated:
        return _json_err("Không tìm thấy ký ức cần sửa.", 404)
    return _json_ok({"id": mem_id, "embedded": bool(emb)})


async def handle_memories_delete(request: web.Request) -> web.Response:
    try:
        mem_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json_err("ID ký ức không hợp lệ.", 400)
    deleted = database.delete_memory(mem_id, record_rejection=True, source="dashboard")
    return _json_ok({"deleted": mem_id, "rejected": bool(deleted)})


async def handle_profile_get(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_err("Chưa có user_id.", 400)
    return _json_ok(database.get_or_create_user(uid))


async def handle_profile_patch(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_err("Chưa có user_id.", 400)
    body = await _parse_json(request)
    try:
        updated = database.update_user_profile_manual(
            user_id=uid,
            affinity_score=body.get("affinity_score"),
            mood=body.get("mood"),
            tease_level=body.get("tease_level"),
            stubborn_level=body.get("stubborn_level"),
            clingy_level=body.get("clingy_level"),
            unanswered_proactive_count=0 if body.get("reset_unanswered") else None,
        )
    except (TypeError, ValueError) as e:
        return _json_err(f"Giá trị không hợp lệ: {e}", 400)
    return _json_ok(updated)


async def handle_persona_get(request: web.Request) -> web.Response:
    return _json_ok(persona.get_persona_config())


async def handle_persona_patch(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    if not isinstance(body, dict):
        return _json_err("Payload JSON không hợp lệ.", 400)
    updated = persona.save_persona_config(body)
    return _json_ok(updated)


async def handle_chats_list(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_ok({"items": [], "total": 0})
    search = request.query.get("search", "")
    try:
        limit = int(request.query.get("limit", "60"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return _json_err("limit/offset không hợp lệ.", 400)
    return _json_ok(database.get_chat_history_paginated(uid, limit=limit, offset=offset, search=search))


async def handle_reminders_list(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_ok([])
    status = request.query.get("status", "all")
    return _json_ok(database.get_reminders_all(uid, status=status, limit=80))


async def handle_reminders_create(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_err("Chưa có user_id.", 400)
    body = await _parse_json(request)
    title = str(body.get("title") or "").strip()
    if not title:
        return _json_err("Cần nhập nội dung nhắc hẹn.", 400)
    kind = str(body.get("kind") or "user_reminder").strip()
    if kind not in ("user_reminder", "ai_promise", "ai_followup"):
        kind = "user_reminder"
    note = str(body.get("context_note") or "").strip()

    due_ts = None
    if body.get("due_iso_vn"):
        try:
            dt = datetime.datetime.fromisoformat(str(body["due_iso_vn"]).strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN_TZ)
            due_ts = dt.timestamp()
        except Exception:
            return _json_err("Định dạng thời gian ISO không hợp lệ (YYYY-MM-DDTHH:MM).", 400)
    elif body.get("delay_minutes") is not None:
        try:
            mins = max(1.0, float(body["delay_minutes"]))
            due_ts = time.time() + mins * 60.0
        except (TypeError, ValueError):
            return _json_err("Số phút delay không hợp lệ.", 400)
    else:
        due_ts = time.time() + 1800.0

    rid = database.create_reminder(uid, kind=kind, title=title, due_at=due_ts, context_note=note)
    return _json_ok({"id": rid, "due_at": due_ts}, status=201)


async def handle_reminders_cancel(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    try:
        rid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json_err("ID reminder không hợp lệ.", 400)
    ok = database.update_reminder(uid, rid, status="cancelled")
    if not ok:
        return _json_err("Không tìm thấy lịch hẹn.", 404)
    return _json_ok({"id": rid, "status": "cancelled"})


async def handle_reminders_delete(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    try:
        rid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json_err("ID reminder không hợp lệ.", 400)
    ok = database.delete_reminder(uid, rid)
    if not ok:
        return _json_err("Không tìm thấy lịch hẹn.", 404)
    return _json_ok({"deleted": rid})


async def handle_lan_scan(request: web.Request) -> web.Response:
    client_ip = _client_ip(request)
    devices = await lan_presence.scan_lan_devices(probe_subnet=True)
    for d in devices:
        d["is_current_client"] = (d["ip"] == client_ip)
    return _json_ok({"client_ip": client_ip, "devices": devices})


async def handle_settings_get(request: web.Request) -> web.Response:
    return _json_ok({
        "lan": lan_presence.get_lan_settings(),
        "watchdog": lan_watchdog.get_watchdog_settings(),
        "backup": backup_helper.get_backup_settings(),
        "fan": fan_controller.get_fan_settings(),
    })


async def handle_settings_patch(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    if "lan" in body and isinstance(body["lan"], dict):
        lan_presence.save_lan_settings(body["lan"])
    if "watchdog" in body and isinstance(body["watchdog"], dict):
        lan_watchdog.save_watchdog_settings(body["watchdog"])
    if "backup" in body and isinstance(body["backup"], dict):
        backup_helper.save_backup_settings(body["backup"])
    if "fan" in body and isinstance(body["fan"], dict):
        fan_controller.save_fan_settings(body["fan"])
        fan_controller.get_controller().publish_update()

    # Hỗ trợ gửi phẳng các key của lan & fan trực tiếp
    flat_lan_keys = ("phone_mac", "pc_mac", "music_dl_url", "presence_enabled", "away_grace_mins", "quiet_start_hour", "quiet_end_hour", "notify_sound_enabled", "notify_tts_enabled")
    flat_patch = {k: body[k] for k in flat_lan_keys if k in body}
    if flat_patch:
        lan_presence.save_lan_settings(flat_patch)

    flat_fan_keys = ("fan_mac", "fan_url")
    fan_flat_patch = {k: body[k] for k in flat_fan_keys if k in body}
    if fan_flat_patch:
        fan_controller.save_fan_settings(fan_flat_patch)
        await fan_controller.get_controller().get_state(refresh=True)
        fan_controller.get_controller().publish_update()

    uid = _resolve_user_id(request.app)
    presence = await lan_presence.refresh_presence_once(target_user_id=uid)
    return _json_ok({
        "presence": presence,
        "fan": fan_controller.get_controller().get_status_snapshot(),
        "plugs": smart_plugs.get_controller().get_status_snapshot(),
        "settings": {
            "lan": lan_presence.get_lan_settings(),
            "watchdog": lan_watchdog.get_watchdog_settings(),
            "backup": backup_helper.get_backup_settings(),
            "fan": fan_controller.get_fan_settings(),
        },
    })


async def handle_backups_list(request: web.Request) -> web.Response:
    return _json_ok({
        "summary": backup_helper.get_backup_summary(),
        "items": backup_helper.list_backups(),
    })


async def handle_backups_create(request: web.Request) -> web.Response:
    info = await backup_helper.create_backup(tag="web")
    return _json_ok(info, status=201)


async def handle_backups_send_telegram(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    bot = request.app.get(APP_BOT_KEY)
    if bot is None or uid <= 0:
        return _json_err("Bot Telegram chưa sẵn sàng hoặc chưa có user_id.", 400)
    try:
        info = await backup_helper.send_backup_to_telegram(bot, uid, reason="Từ Dashboard", create_new=True)
        return _json_ok(info)
    except Exception as e:
        return _json_err(f"Gửi backup lên Telegram thất bại: {e}", 500)


async def handle_backups_download(request: web.Request) -> web.StreamResponse:
    filename = request.match_info.get("filename", "")
    try:
        tmp_db, out_name = await asyncio.to_thread(backup_helper.export_decompressed_temp_sync, filename)
    except FileNotFoundError:
        return _json_err("Không tìm thấy file sao lưu.", 404)
    except Exception as e:
        return _json_err(f"Giải nén sao lưu lỗi: {e}", 500)

    try:
        file_size = os.path.getsize(tmp_db)
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "Content-Length": str(file_size),
                "Cache-Control": "no-store",
            },
        )
        await resp.prepare(request)
        with open(tmp_db, "rb") as f:
            while True:
                chunk = await asyncio.to_thread(f.read, 65536)
                if not chunk:
                    break
                await resp.write(chunk)
        await resp.write_eof()
        return resp
    except (asyncio.CancelledError, ConnectionResetError):
        raise
    finally:
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass


async def handle_backups_restore(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    ctype = (request.headers.get("Content-Type") or "").lower()
    try:
        if "multipart/form-data" in ctype:
            reader = await request.multipart()
            bdir = backup_helper.get_backup_dir()
            fd, tmp_upload_path = tempfile.mkstemp(prefix="ds_upload_", suffix=".bin", dir=bdir)
            os.close(fd)
            got_file = False
            try:
                while True:
                    part = await reader.next()
                    if part is None:
                        break
                    if part.name == "file":
                        total = 0
                        with open(tmp_upload_path, "wb") as f:
                            while True:
                                chunk = await part.read_chunk(65536)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > backup_helper.MAX_RESTORE_UPLOAD_BYTES:
                                    return _json_err("File tải lên vượt quá 32MB.", 413)
                                await asyncio.to_thread(f.write, chunk)
                        got_file = (total > 0)
                        break
                if not got_file:
                    return _json_err("Không tìm thấy trường file tải lên.", 400)
                res = await backup_helper.restore_backup_file(tmp_upload_path, user_id=uid)
                return _json_ok(res)
            finally:
                if os.path.exists(tmp_upload_path):
                    try:
                        os.remove(tmp_upload_path)
                    except OSError:
                        pass

        body = await _parse_json(request)
        filename = str(body.get("filename") or "").strip()
        if not filename:
            return _json_err("Cần truyền filename hoặc upload file .db/.db.gz.", 400)
        res = await backup_helper.restore_backup_snapshot(filename, user_id=uid)
        return _json_ok(res)
    except FileNotFoundError:
        return _json_err("Không tìm thấy snapshot yêu cầu.", 404)
    except ValueError as e:
        return _json_err(str(e), 400)
    except Exception as e:
        logger.error(f"Restore DB thất bại: {e}", exc_info=True)
        return _json_err(f"Khôi phục thất bại: {e}", 500)


async def handle_watchdog_events(request: web.Request) -> web.Response:
    etype = request.query.get("type") or None
    try:
        limit = int(request.query.get("limit", "80"))
    except ValueError:
        limit = 80
    return _json_ok({
        "state": lan_watchdog.get_watchdog_snapshot(),
        "events": database.get_system_events(event_type=etype, limit=limit),
    })


async def handle_watchdog_check_now(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    bot = request.app.get(APP_BOT_KEY)
    snap = await lan_watchdog.run_watchdog_check(bot=bot, target_user_id=uid)
    return _json_ok(snap)


async def handle_journal_list(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    try:
        limit_days = max(1, min(30, int(request.query.get("days", "14"))))
    except ValueError:
        limit_days = 14
    days = database.get_journal_days(uid, limit_days=limit_days) if uid > 0 else []
    return _json_ok({"days": days})


async def handle_engine_get(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    if uid <= 0:
        return _json_err("Chưa có user_id.", 400)
    return _json_ok(proactive_engine.get_engine_snapshot(uid))


async def handle_open_loop_resolve(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    loop_id = (request.match_info.get("loop_id") or "").strip()
    if uid <= 0 or not loop_id:
        return _json_err("Yêu cầu không hợp lệ.", 400)
    ok = database.resolve_open_loop_by_id(uid, loop_id)
    if not ok:
        return _json_err("Không tìm thấy mạch chuyện đang mở.", 404)
    return _json_ok({"resolved": loop_id})


async def handle_rejected_delete(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request.app)
    try:
        rej_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json_err("ID không hợp lệ.", 400)
    ok = database.unreject_memory_by_id(uid, rej_id) if uid > 0 else False
    if not ok:
        return _json_err("Không tìm thấy mục trong Sổ từ chối.", 404)
    return _json_ok({"removed": rej_id})


async def handle_lan_now_playing_get(request: web.Request) -> web.Response:
    return _json_ok({"now_playing": database.get_pc_now_playing()})


async def handle_lan_now_playing_post(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    if not body:
        return _json_err("Thiếu payload media JSON.", 400)
    data = database.set_pc_now_playing(body, pc_ip=_client_ip(request))
    lan_events.publish("now_playing_update", data)
    return _json_ok({"now_playing": data})


# --- ESP32-C3 Smart Speaker & Jukebox Endpoints ---

async def handle_esp32_stream(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/pcm; rate=24000; channels=1; bits=16",
            "Cache-Control": "no-cache, no-store",
            "Connection": "close",
            "X-Accel-Buffering": "no",
        },
    )
    client_ip = _client_ip(request)
    if request.transport is not None:
        tcp_helpers.tcp_nodelay(request.transport, True)
    rt = _get_runtime(request.app)
    stream_tasks = rt["esp32_stream_tasks"]
    cur_task = asyncio.current_task()
    if cur_task is not None:
        stream_tasks.add(cur_task)
    try:
        await resp.prepare(request)
        async for chunk in esp32_audio.get_manager().stream_pcm_generator(client_ip=client_ip):
            if rt.get("shutting_down"):
                break
            if chunk:
                await resp.write(chunk)
    except (asyncio.CancelledError, ConnectionResetError, OSError):
        pass
    except Exception as e:
        logger.debug(f"Luồng âm thanh ESP32 đóng ({client_ip}): {type(e).__name__}: {e}")
    finally:
        if cur_task is not None:
            stream_tasks.discard(cur_task)
    return resp


async def handle_esp32_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket hai chiều: OpenStick gửi binary 0x01+Opus + JSON điều khiển; ESP32 gửi hello/button/telemetry."""
    ws = web.WebSocketResponse(
        heartbeat=20.0,
        timeout=1.0,
        max_msg_size=65536,
        autoclose=True,
        autoping=True,
        protocols=("arduino",),
    )
    if request.transport is not None:
        try:
            tcp_helpers.tcp_nodelay(request.transport, True)
        except Exception:
            pass
    await ws.prepare(request)

    client_ip = _client_ip(request)
    mgr = esp32_audio.get_manager()
    ctrl_q: asyncio.Queue = asyncio.Queue(maxsize=32)
    rt = _get_runtime(request.app)
    ws_clients = rt["esp32_ws_clients"]
    ws_clients.add(ws)
    ws_gen = mgr.register_ws_session(ctrl_q, client_ip=client_ip)
    send_lock = asyncio.Lock()

    async def _send_json(payload: Dict[str, Any]) -> None:
        if ws.closed:
            return
        txt = json.dumps(payload, ensure_ascii=False)
        async with send_lock:
            if not ws.closed:
                await ws.send_str(txt)

    async def _send_opus(opus_pkt: bytes) -> None:
        if ws.closed:
            return
        frame = bytes((esp32_audio.WS_OPUS_AUDIO_TYPE,)) + opus_pkt
        async with send_lock:
            if not ws.closed:
                await ws.send_bytes(frame)

    async def _ctrl_sender() -> None:
        try:
            while not ws.closed and not rt.get("shutting_down"):
                item = await ctrl_q.get()
                if item is None or item.get("type") == "__close__":
                    break
                await _send_json(item)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            logger.debug(f"WS ctrl sender đóng ({client_ip}): {type(e).__name__}: {e}")

    async def _audio_sender() -> None:
        try:
            async for opus_pkt in mgr.stream_opus_frames(client_ip=client_ip, ws_gen=ws_gen):
                if ws.closed or rt.get("shutting_down"):
                    break
                await _send_opus(opus_pkt)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            logger.warning(f"WS Opus sender dừng ({client_ip}): {type(e).__name__}: {e}")

    async def _reader() -> None:
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    mtype = str(data.get("type") or "").strip().lower()
                    if mtype == "hello":
                        mgr.mark_ws_hello(data, client_ip=client_ip)
                        esp32_audio.ESP32AudioManager._enqueue_ws_item(ctrl_q, {
                            "type": "hello_ack",
                            "codec": "opus",
                            "sample_rate": esp32_audio.SAMPLE_RATE,
                            "frame_ms": esp32_audio.OPUS_FRAME_MS,
                            "status": mgr.status,
                            "volume": mgr.volume,
                        })
                    elif mtype == "button" or (not mtype and ("button" in data or "action" in data)):
                        action = str(data.get("button") or data.get("action") or "").strip().lower()
                        if action:
                            res = await mgr.handle_button_event(action, client_ip=client_ip)
                            esp32_audio.ESP32AudioManager._enqueue_ws_item(ctrl_q, {
                                "type": "ack",
                                "event": "button",
                                "action": action,
                                "ok": bool(res.get("ok")),
                            })
                    elif mtype == "telemetry" or ("temperature_c" in data and "humidity_pct" in data):
                        try:
                            mgr.update_dht11_telemetry(
                                data.get("temperature_c"),
                                data.get("humidity_pct"),
                                rssi=data.get("rssi"),
                                client_ip=client_ip,
                            )
                        except ValueError as e:
                            logger.debug(f"WS telemetry không hợp lệ ({client_ip}): {e}")
                    elif mtype == "ping":
                        mgr.mark_esp32_seen(ip=client_ip, rssi=data.get("rssi"))
                        esp32_audio.ESP32AudioManager._enqueue_ws_item(ctrl_q, {
                            "type": "pong",
                            "ts": time.time(),
                        })
                elif msg.type == WSMsgType.ERROR:
                    break
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass

    reader_task = asyncio.create_task(_reader())
    ctrl_task = asyncio.create_task(_ctrl_sender())
    audio_task = asyncio.create_task(_audio_sender())
    try:
        await asyncio.wait(
            {reader_task, ctrl_task, audio_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        pass
    finally:
        for t in (reader_task, ctrl_task, audio_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(reader_task, ctrl_task, audio_task, return_exceptions=True)
        ws_clients.discard(ws)
        mgr.unregister_ws_session(ws_gen)
        await mgr._close_source_if_idle()
        if not ws.closed:
            try:
                await asyncio.wait_for(ws.close(), timeout=0.5)
            except Exception:
                pass
    return ws


async def handle_esp32_event(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    action = str(body.get("button") or body.get("action") or "").strip().lower()
    if not action:
        return _json_err("Thiếu trường button/action.", 400)
    res = await esp32_audio.get_manager().handle_button_event(action, client_ip=_client_ip(request))
    if not res.get("ok"):
        return _json_err(res.get("error") or "Thao tác không hợp lệ.", 400)
    return _json_ok(res)


async def handle_esp32_telemetry(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    if "temperature_c" not in body or "humidity_pct" not in body:
        return _json_err("Cần truyền temperature_c và humidity_pct.", 400)
    try:
        saved = esp32_audio.get_manager().update_dht11_telemetry(
            body.get("temperature_c"),
            body.get("humidity_pct"),
            rssi=body.get("rssi"),
            client_ip=_client_ip(request),
        )
    except ValueError as e:
        return _json_err(str(e), 400)
    return _json_ok(saved)


async def handle_esp32_status(request: web.Request) -> web.Response:
    return _json_ok(esp32_audio.get_manager().get_status_snapshot())


async def handle_jukebox_play(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    target = str(body.get("video_id") or body.get("query") or "").strip()
    if not target:
        return _json_err("Thiếu query hoặc video_id.", 400)
    play_now = bool(body.get("play_now", True))
    mgr = esp32_audio.get_manager()

    cached = esp32_audio.find_cached_track(target)
    if cached:
        res = await mgr.enqueue_or_play(target, play_now=play_now)
        if play_now:
            habit_engine.notify_device_event(
                "speaker", "Loa phòng", "play",
                value_num=float(mgr.volume), value_text=cached.get("title") or target,
                source="user_dashboard",
            )
        return _json_ok({"queued_download": False, "result": res, "jukebox": mgr.get_status_snapshot()})

    mgr.spawn_play_or_search(target, play_now=play_now)
    if play_now:
        habit_engine.notify_device_event(
            "speaker", "Loa phòng", "play",
            value_num=float(mgr.volume), value_text=target,
            source="user_dashboard",
        )
    return _json_ok({"queued_download": True, "query": target, "jukebox": mgr.get_status_snapshot()})


async def handle_jukebox_control(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    action = str(body.get("action") or "").strip().lower()
    value = body.get("value")
    mgr = esp32_audio.get_manager()

    if action == "pause":
        snap = await mgr.pause()
        habit_engine.notify_device_event("speaker", "Loa phòng", "pause", value_num=float(mgr.volume), source="user_dashboard")
    elif action == "resume":
        snap = await mgr.resume()
        tr = snap.get("current_track") or {}
        habit_engine.notify_device_event("speaker", "Loa phòng", "play", value_num=float(mgr.volume), value_text=tr.get("title") or "", source="user_dashboard")
    elif action == "toggle":
        snap = await mgr.toggle_play_pause()
        st = snap.get("status")
        tr = snap.get("current_track") or {}
        if st in ("playing", "paused"):
            habit_engine.notify_device_event("speaker", "Loa phòng", "play" if st == "playing" else "pause", value_num=float(mgr.volume), value_text=tr.get("title") or "", source="user_dashboard")
    elif action == "skip":
        snap = await mgr.skip()
        tr = snap.get("current_track") or {}
        if tr:
            habit_engine.notify_device_event("speaker", "Loa phòng", "play", value_num=float(mgr.volume), value_text=tr.get("title") or "", source="user_dashboard")
    elif action == "stop":
        snap = await mgr.stop()
        habit_engine.notify_device_event("speaker", "Loa phòng", "stop", source="user_dashboard")
    elif action == "volume":
        try:
            mgr.set_volume(int(value))
        except (TypeError, ValueError):
            return _json_err("Giá trị volume phải là số 0..100.", 400)
        snap = mgr.get_status_snapshot()
        habit_engine.notify_device_event("speaker", "Loa phòng", "volume", value_num=float(snap.get("volume", 0)), source="user_dashboard")
    elif action == "auto_dj":
        mgr.set_auto_dj(bool(value))
        snap = mgr.get_status_snapshot()
    elif action == "voice_autoplay":
        mgr.set_voice_autoplay(bool(value))
        snap = mgr.get_status_snapshot()
    elif action == "sleep_timer":
        try:
            mins = int(value or 0)
        except (TypeError, ValueError):
            return _json_err("Giá trị sleep_timer phải là số phút >= 0.", 400)
        mgr.set_sleep_timer(mins)
        snap = mgr.get_status_snapshot()
    elif action == "delete_cache":
        ok = await mgr.delete_from_cache(str(value or ""))
        if not ok:
            return _json_err("Không tìm thấy bài cần xóa trong cache.", 404)
        snap = mgr.get_status_snapshot()
    else:
        return _json_err("Hành động Jukebox không hợp lệ.", 400)

    return _json_ok(snap)


async def handle_jukebox_eq(request: web.Request) -> web.Response:
    """Chỉnh trực tiếp EQ Loudness / Bass / Treble / High-pass 60Hz và Chuẩn hóa âm lượng cho loa ESP32-C3."""
    body = await _parse_json(request)
    mgr = esp32_audio.get_manager()
    raw_norm = body.get("normalize")
    norm_val = None
    if raw_norm is not None:
        norm_val = raw_norm if isinstance(raw_norm, bool) else str(raw_norm).strip().lower() not in ("0", "false", "no", "off")
    try:
        eq = mgr.set_eq_settings(
            preset=body.get("preset"),
            bass_db=body.get("bass_db"),
            treble_db=body.get("treble_db"),
            highpass_hz=body.get("highpass_hz"),
            enabled=body.get("enabled"),
            normalize=norm_val,
        )
    except (TypeError, ValueError) as e:
        return _json_err(f"Tham số EQ không hợp lệ: {e}", 400)
    return _json_ok({"eq": eq, "jukebox": mgr.get_status_snapshot()})


async def handle_jukebox_session(request: web.Request) -> web.Response:
    """Điều khiển phiên Tập trung / Chill / Chợp mắt và Báo thức sáng trên loa ESP32-C3."""
    body = await _parse_json(request)
    action = str(body.get("action") or "").strip().lower()
    mgr = esp32_audio.get_manager()

    if action in ("start_focus", "start_chill", "start_nap"):
        mode = action.replace("start_", "")
        default_min = 20 if mode == "nap" else 15
        try:
            mins = int(body.get("minutes") if body.get("minutes") is not None else default_min)
        except (TypeError, ValueError):
            return _json_err("Số phút phải là số nguyên >= 1.", 400)
        res = await mgr.start_timed_session(
            mode=mode,
            minutes=mins,
            query=str(body.get("query") or ""),
            label=str(body.get("label") or ""),
        )
        return _json_ok({"result": res, "jukebox": mgr.get_status_snapshot()})

    if action == "cancel_session":
        snap = mgr.cancel_timed_session()
        return _json_ok({"jukebox": snap})

    if action in ("set_alarm", "schedule_music"):
        kind = "music" if (action == "schedule_music" or str(body.get("kind") or "").strip().lower() == "music") else "alarm"
        time_vn = str(body.get("time_vn") or body.get("time") or "").strip()
        due_ts = None
        delay_raw = body.get("delay_minutes")
        if delay_raw is None and not time_vn and body.get("minutes") is not None:
            delay_raw = body.get("minutes")
        delay_mins = 0
        if not time_vn and delay_raw is not None:
            try:
                delay_mins = max(1, min(1440, int(delay_raw)))
                due_ts = time.time() + delay_mins * 60.0
            except (TypeError, ValueError):
                return _json_err("Giá trị phút không hợp lệ.", 400)
        dur_raw = body.get("duration_min")
        if dur_raw is None:
            dur_raw = body.get("duration_minutes")
        try:
            dur_min = max(0, min(360, int(dur_raw))) if dur_raw is not None and str(dur_raw).strip() != "" else 0
        except (TypeError, ValueError):
            return _json_err("Thời lượng phát phải là số nguyên >= 0.", 400)
        query = str(body.get("query") or "").strip()
        label = str(body.get("label") or "").strip()
        try:
            if kind == "music":
                alarm = mgr.schedule_music(
                    due_ts=due_ts,
                    time_str=time_vn,
                    delay_minutes=delay_mins,
                    query=query,
                    label=label,
                    duration_min=dur_min,
                )
            else:
                alarm = mgr.add_alarm_clock(
                    due_ts=due_ts,
                    time_str=time_vn,
                    label=label,
                    query=query,
                    kind="alarm",
                    duration_min=dur_min,
                )
        except ValueError as e:
            return _json_err(str(e), 400)
        return _json_ok({"alarm": alarm, "jukebox": mgr.get_status_snapshot()})

    if action == "cancel_alarm":
        alarm_id = str(body.get("alarm_id") or body.get("id") or "").strip()
        if not alarm_id or not mgr.cancel_alarm(alarm_id):
            return _json_err("Không tìm thấy báo thức cần hủy.", 404)
        return _json_ok({"jukebox": mgr.get_status_snapshot()})

    if action == "dismiss_alarm":
        snap = await mgr.dismiss_alarm()
        return _json_ok({"jukebox": snap})

    if action == "snooze_alarm":
        try:
            mins = int(body.get("minutes") or 5)
        except (TypeError, ValueError):
            mins = 5
        res = await mgr.snooze_alarm(minutes=mins)
        return _json_ok({"result": res, "jukebox": mgr.get_status_snapshot()})

    return _json_err("Hành động phiên/báo thức không hợp lệ.", 400)


async def handle_jukebox_cookies_get(request: web.Request) -> web.Response:
    return _json_ok(esp32_audio.get_cookies_status())


async def handle_jukebox_cookies_post(request: web.Request) -> web.Response:
    """Lưu file youtube_cookies.txt (từ upload hoặc dán nội dung Netscape) lên USB/OpenStick."""
    raw_text = ""
    if (request.content_type or "").lower().startswith("multipart/"):
        reader = await request.multipart()
        async for field in reader:
            if field.name in ("file", "cookies"):
                chunks = bytearray()
                while True:
                    part = await field.read_chunk(65536)
                    if not part:
                        break
                    if len(chunks) + len(part) > 512 * 1024:
                        return _json_err("File cookies vượt quá 512 KB.", 400)
                    chunks.extend(part)
                raw_text = bytes(chunks).decode("utf-8", errors="replace")
                break
    else:
        body = await _parse_json(request)
        raw_text = str(body.get("cookies_text") or body.get("content") or "")

    try:
        status = esp32_audio.save_youtube_cookies(raw_text)
    except ValueError as e:
        return _json_err(str(e), 400)
    esp32_audio.get_manager().publish_update()
    return _json_ok({"cookies": status, "jukebox": esp32_audio.get_manager().get_status_snapshot()})


async def handle_jukebox_cookies_delete(request: web.Request) -> web.Response:
    status = esp32_audio.clear_youtube_cookies()
    mgr = esp32_audio.get_manager()
    mgr.publish_update()
    return _json_ok({"cookies": status, "jukebox": mgr.get_status_snapshot()})


async def _on_dashboard_startup(app: web.Application) -> None:
    _get_runtime(app)["shutting_down"] = False
    mgr = esp32_audio.get_manager()
    bot = app.get(APP_BOT_KEY)
    if mgr._session_event_handler is None and bot is not None:
        import scheduler

        async def _dispatch_speaker_event(ev):
            uid = _resolve_user_id(app)
            if uid <= 0:
                return
            await scheduler.handle_speaker_session_event(bot, uid, ev)

        mgr.set_session_event_handler(_dispatch_speaker_event)
    mgr.ensure_background_loops()


async def _on_dashboard_shutdown(app: web.Application) -> None:
    """Đóng nhanh mọi SSE/HTTP-PCM/WebSocket stream khi tắt server để systemd không bị treo 90s."""
    rt = _get_runtime(app)
    rt["shutting_down"] = True
    for q in list(rt.get("sse_queues") or ()):
        lan_events._close_subscriber(q)

    mgr = getattr(esp32_audio, "_MANAGER", None)
    if mgr is not None:
        mgr._ws_gen += 1
        mgr._stream_gen += 1
        for q in list(mgr._ws_control_queues.values()):
            esp32_audio.ESP32AudioManager._enqueue_ws_item(q, {"type": "__close__"})
        mgr._signal_wake()
        if mgr._source is not None:
            try:
                await mgr._source.close()
            except Exception:
                pass
            mgr._source = None
            mgr._source_track_id = None

    for ws in list(rt.get("esp32_ws_clients") or ()):
        if not ws.closed:
            try:
                await asyncio.wait_for(ws.close(), timeout=1.0)
            except Exception:
                pass

    for task in list(rt.get("esp32_stream_tasks") or ()):
        if not task.done():
            task.cancel()


def create_app(bot=None, target_user_id: int = 0) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=backup_helper.MAX_RESTORE_UPLOAD_BYTES + 1024 * 1024)
    app[APP_BOT_KEY] = bot
    app[APP_TARGET_UID_KEY] = int(target_user_id or 0)
    app[APP_RUNTIME_KEY] = {
        "sse_queues": set(),
        "esp32_ws_clients": set(),
        "esp32_stream_tasks": set(),
        "shutting_down": False,
    }
    app.on_startup.append(_on_dashboard_startup)
    app.on_shutdown.append(_on_dashboard_shutdown)

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/auth/check", handle_auth_check)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)

    app.router.add_get("/api/overview", handle_overview)
    app.router.add_get("/api/events", handle_sse_events)
    app.router.add_post("/api/notify/test", handle_notify_test)
    app.router.add_get("/api/tts/speak_alert", handle_tts_speak_alert)
    app.router.add_post("/api/tts/audition_esp32", handle_tts_audition_esp32)

    app.router.add_get("/api/memories", handle_memories_list)
    app.router.add_post("/api/memories", handle_memories_create)
    app.router.add_patch("/api/memories/{id}", handle_memories_update)
    app.router.add_delete("/api/memories/{id}", handle_memories_delete)

    app.router.add_get("/api/profile", handle_profile_get)
    app.router.add_patch("/api/profile", handle_profile_patch)
    app.router.add_get("/api/persona", handle_persona_get)
    app.router.add_patch("/api/persona", handle_persona_patch)

    app.router.add_get("/api/chats", handle_chats_list)

    app.router.add_get("/api/reminders", handle_reminders_list)
    app.router.add_post("/api/reminders", handle_reminders_create)
    app.router.add_post("/api/reminders/{id}/cancel", handle_reminders_cancel)
    app.router.add_delete("/api/reminders/{id}", handle_reminders_delete)

    app.router.add_post("/api/lan/scan", handle_lan_scan)
    app.router.add_get("/api/lan/now_playing", handle_lan_now_playing_get)
    app.router.add_post("/api/lan/now_playing", handle_lan_now_playing_post)
    app.router.add_get("/api/settings", handle_settings_get)
    app.router.add_patch("/api/settings", handle_settings_patch)

    app.router.add_get("/api/journal", handle_journal_list)
    app.router.add_get("/api/engine", handle_engine_get)
    app.router.add_post("/api/engine/open_loops/{loop_id}/resolve", handle_open_loop_resolve)
    app.router.add_delete("/api/engine/rejected/{id}", handle_rejected_delete)

    app.router.add_get("/api/backups", handle_backups_list)
    app.router.add_post("/api/backups/create", handle_backups_create)
    app.router.add_post("/api/backups/telegram", handle_backups_send_telegram)
    app.router.add_get("/api/backups/{filename}/download", handle_backups_download)
    app.router.add_post("/api/backups/restore", handle_backups_restore)

    app.router.add_get("/api/watchdog/events", handle_watchdog_events)
    app.router.add_post("/api/watchdog/check", handle_watchdog_check_now)

    app.router.add_get("/api/esp32/stream", handle_esp32_stream)
    app.router.add_get("/api/esp32/ws", handle_esp32_ws)
    app.router.add_post("/api/esp32/event", handle_esp32_event)
    app.router.add_post("/api/esp32/telemetry", handle_esp32_telemetry)
    app.router.add_get("/api/esp32/status", handle_esp32_status)

    app.router.add_get("/api/jukebox/status", handle_esp32_status)
    app.router.add_post("/api/jukebox/play", handle_jukebox_play)
    app.router.add_post("/api/jukebox/control", handle_jukebox_control)
    app.router.add_post("/api/jukebox/eq", handle_jukebox_eq)
    app.router.add_post("/api/jukebox/session", handle_jukebox_session)
    app.router.add_get("/api/jukebox/cookies", handle_jukebox_cookies_get)
    app.router.add_post("/api/jukebox/cookies", handle_jukebox_cookies_post)
    app.router.add_delete("/api/jukebox/cookies", handle_jukebox_cookies_delete)

    app.router.add_get("/api/fan/status", handle_fan_status)
    app.router.add_post("/api/fan/scan", handle_fan_scan)
    app.router.add_post("/api/fan/control", handle_fan_control)
    app.router.add_patch("/api/fan/config", handle_fan_config)
    app.router.add_post("/api/fan/config", handle_fan_config)

    app.router.add_get("/api/plugs/status", handle_plugs_status)
    app.router.add_post("/api/plugs/scan", handle_plugs_scan)
    app.router.add_post("/api/plugs/control", handle_plugs_control)
    app.router.add_patch("/api/plugs/config", handle_plugs_config)
    app.router.add_post("/api/plugs/config", handle_plugs_config)

    app.router.add_get("/api/habits", handle_habits_get)
    app.router.add_post("/api/habits/analyze", handle_habits_analyze)
    app.router.add_patch("/api/habits/settings", handle_habits_settings)
    app.router.add_post("/api/habits/settings", handle_habits_settings)
    app.router.add_post("/api/habits/control", handle_habits_control)

    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_delete("/mcp", handle_mcp_delete)
    return app


async def handle_fan_status(request: web.Request) -> web.Response:
    ctrl = fan_controller.get_controller()
    if request.query.get("refresh", "1") != "0":
        await ctrl.get_state(refresh=True)
    return _json_ok({"fan": ctrl.get_status_snapshot()})


async def handle_fan_scan(request: web.Request) -> web.Response:
    res = await fan_controller.get_controller().discover_fan_on_lan()
    return _json_ok(res)


async def handle_fan_control(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    action = str(body.get("action") or "").strip().lower()
    value = body.get("value")
    ctrl = fan_controller.get_controller()

    if action == "refresh":
        await ctrl.get_state(refresh=True)
        ctrl.publish_update()
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    if action == "power":
        if isinstance(value, bool):
            target_on = value
        elif isinstance(value, (int, float)):
            target_on = int(value) != 0
        else:
            sval = str(value or "").strip().lower()
            if sval in ("1", "true", "on"):
                target_on = True
            elif sval in ("0", "false", "off"):
                target_on = False
            else:
                cur = await ctrl.get_state(refresh=True)
                target_on = not bool((cur or {}).get("is_on"))
        res = await ctrl.set_power(target_on, manual=True, reason="Dashboard")
        if res is None:
            return _json_err("Quạt ESP-01S không phản hồi trên mạng LAN.", 502)
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    if action == "speed":
        try:
            pct = int(value)
        except (TypeError, ValueError):
            return _json_err("Giá trị tốc độ phải là số 0..100.", 400)
        res = await ctrl.set_speed(pct, manual=True, event_source="user_dashboard")
        if res is None:
            return _json_err("Quạt ESP-01S không phản hồi trên mạng LAN.", 502)
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    if action == "mode":
        try:
            mode_val = 1 if int(value) == 1 else 0
        except (TypeError, ValueError):
            mode_val = 1 if str(value or "").strip().lower() in ("natural", "tu_nhien", "1") else 0
        res = await ctrl.set_mode(mode_val, manual=True)
        if res is None:
            return _json_err("Quạt ESP-01S không phản hồi trên mạng LAN.", 502)
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    if action == "sleep_ramp":
        ok = await ctrl.start_sleep_ramp(target_pct=0, step_pct=1)
        if not ok:
            return _json_err("Quạt đang tắt hoặc đã ở sẵn mức 0% (1005 Hz).", 400)
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    if action == "cancel_ramp":
        ctrl.cancel_ramp()
        ctrl.publish_update()
        return _json_ok({"fan": ctrl.get_status_snapshot()})

    return _json_err("Hành động điều khiển quạt không hợp lệ.", 400)


async def handle_fan_config(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    allowed = {
        "fan_mac", "fan_url", "auto_enabled", "auto_away", "auto_home",
        "auto_sleep", "sleep_ask_time", "sleep_wait_mins", "sleep_voice",
        "ramp_step_sec", "max_freq_hz",
    }
    patch = {k: body[k] for k in allowed if k in body}
    saved = fan_controller.save_fan_settings(patch)
    ctrl = fan_controller.get_controller()
    if "fan_url" in patch or "fan_mac" in patch:
        ctrl._resolved_url = ""
        await ctrl.get_state(refresh=True)
    ctrl.publish_update()
    return _json_ok({
        "fan": ctrl.get_status_snapshot(),
        "settings": saved,
    })


async def handle_plugs_status(request: web.Request) -> web.Response:
    ctrl = smart_plugs.get_controller()
    if request.query.get("refresh", "1") != "0":
        await ctrl.refresh_all()
    return _json_ok({"plugs": ctrl.get_status_snapshot()})


async def handle_plugs_scan(request: web.Request) -> web.Response:
    res = await smart_plugs.get_controller().discover_on_lan()
    return _json_ok(res)


async def handle_plugs_control(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    mac = str(body.get("mac") or body.get("target") or "").strip()
    action = str(body.get("action") or "").strip().lower()
    ctrl = smart_plugs.get_controller()
    if not mac:
        return _json_err("Thiếu MAC ổ cắm.", 400)

    if action == "refresh":
        await ctrl.refresh_all()
        ctrl.publish_update()
        return _json_ok({"plugs": ctrl.get_status_snapshot()})

    if action == "cancel_timer":
        ctrl.cancel_timer(mac)
        return _json_ok({"plugs": ctrl.get_status_snapshot()})

    if action == "timer":
        try:
            mins = int(body.get("delay_minutes") or body.get("minutes") or 0)
        except (TypeError, ValueError):
            mins = 0
        if mins <= 0:
            return _json_err("Số phút hẹn giờ phải > 0.", 400)
        want_on = bool(body.get("value", False))
        tm = ctrl.schedule_timer(mac, want_on, mins)
        if tm is None:
            return _json_err("Không tìm thấy ổ cắm cần hẹn giờ.", 404)
        return _json_ok({"timer": tm, "plugs": ctrl.get_status_snapshot()})

    if action in ("power", "toggle"):
        plugs_found = ctrl._find_plugs(mac)
        if not plugs_found:
            return _json_err("Không tìm thấy ổ cắm.", 404)
        if action == "toggle":
            cur = await ctrl.refresh_plug(plugs_found[0])
            target_on = not bool(cur) if cur is not None else True
        else:
            val = body.get("value")
            if isinstance(val, bool):
                target_on = val
            else:
                target_on = str(val or "").strip().lower() in ("1", "true", "on")
        res = await ctrl.set_power(mac, target_on, reason="Dashboard")
        if res is None:
            return _json_err("Ổ cắm BroadLink không phản hồi hoặc đang bật Lock Device.", 502)
        return _json_ok({"plugs": ctrl.get_status_snapshot()})

    return _json_err("Hành động điều khiển ổ cắm không hợp lệ.", 400)


async def handle_plugs_config(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    mac = lan_presence.normalize_mac(body.get("mac") or "")
    if not mac:
        return _json_err("Thiếu MAC hợp lệ của ổ cắm.", 400)
    plugs = smart_plugs.load_plugs_config()
    if body.get("delete"):
        saved = smart_plugs.save_plugs_config([p for p in plugs if p["mac"] != mac])
        smart_plugs.get_controller().publish_update()
        return _json_ok({"plugs": smart_plugs.get_controller().get_status_snapshot(), "saved_count": len(saved)})

    updated = False
    for p in plugs:
        if p["mac"] == mac:
            if "name" in body and str(body["name"]).strip():
                p["name"] = str(body["name"]).strip()
            if "icon" in body and str(body["icon"]).strip():
                p["icon"] = str(body["icon"]).strip()
            if "aliases" in body:
                p["aliases"] = body["aliases"]
            for bf in ("auto_away", "auto_home", "home_only_evening", "auto_sleep"):
                if bf in body:
                    p[bf] = bool(body[bf])
            updated = True
            break
    if not updated:
        return _json_err("Không tìm thấy ổ cắm có MAC này.", 404)
    smart_plugs.save_plugs_config(plugs)
    ctrl = smart_plugs.get_controller()
    ctrl.publish_update()
    return _json_ok({"plugs": ctrl.get_status_snapshot()})


async def handle_habits_get(request: web.Request) -> web.Response:
    return _json_ok({"habits": habit_engine.get_habits_snapshot()})


async def handle_habits_analyze(request: web.Request) -> web.Response:
    await asyncio.to_thread(habit_engine.analyze_and_sync_habits)
    habit_engine.publish_update()
    return _json_ok({"habits": habit_engine.get_habits_snapshot()})


async def handle_habits_settings(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    patch = {}
    for k in ("auto_enabled", "notify_telegram", "min_days"):
        if k in body:
            patch[k] = body[k]
    saved = habit_engine.save_habit_settings(patch)
    return _json_ok({"settings": saved, "habits": habit_engine.get_habits_snapshot()})


async def handle_habits_control(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    action = str(body.get("action") or "").strip().lower()
    habit_key = str(body.get("habit_key") or body.get("key") or "").strip()
    if action not in ("enable", "mute", "forget", "delete") or not habit_key:
        return _json_err("Cần action (enable|mute|forget) và habit_key hợp lệ.", 400)
    note = habit_engine.execute_ai_habit_action({"action": action, "habit_key": habit_key})
    if not note:
        return _json_err("Không tìm thấy thói quen cần thao tác.", 404)
    return _json_ok({"note": note, "habits": habit_engine.get_habits_snapshot()})


async def handle_mcp_get(request: web.Request) -> web.Response:
    accept = (request.headers.get("Accept") or "").lower()
    if "text/event-stream" in accept:
        return web.Response(status=405, headers={"Allow": "POST, DELETE", "Cache-Control": "no-store"})
    return web.json_response(
        {
            "protocol": "mcp",
            "transport": "streamable-http",
            "serverInfo": {
                "name": mcp_tools.MCP_SERVER_NAME,
                "version": mcp_tools.MCP_SERVER_VERSION,
            },
            "endpoint": "/mcp",
            "tools": [t["name"] for t in mcp_tools.list_mcp_tools()],
        },
        headers={"Cache-Control": "no-store"},
    )


async def handle_mcp_delete(request: web.Request) -> web.Response:
    return web.Response(status=204, headers={"Cache-Control": "no-store"})


async def handle_mcp_post(request: web.Request) -> web.Response:
    try:
        raw_text = await request.text()
        raw_body = json.loads(raw_text) if raw_text.strip() else None
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status=400,
            headers={"Cache-Control": "no-store"},
        )
    resp_obj, status, hdrs = await mcp_tools.handle_mcp_jsonrpc_payload(raw_body)
    if status == 202 or resp_obj is None:
        return web.Response(status=202, headers=hdrs)
    return web.json_response(resp_obj, status=status, headers=hdrs)


async def start_dashboard(bot=None, target_user_id: int = 0) -> Optional[web.AppRunner]:
    """Khởi động Web Dashboard trên OpenStick và trả về AppRunner để đóng sạch khi thoát."""
    if not config.DASHBOARD_ENABLED:
        logger.info("Web Dashboard đã tắt theo DASHBOARD_ENABLED=false.")
        return None
    pin = get_effective_pin()
    app = create_app(bot=bot, target_user_id=target_user_id)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.DASHBOARD_HOST, config.DASHBOARD_PORT, shutdown_timeout=2.0)
    await site.start()
    pin_source = ".env" if config.DASHBOARD_PIN else "auto (app_metadata)"
    logger.info(
        f"🌐 [Web Dashboard]: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT} "
        f"(PIN mode: {pin_source}, len={len(pin)})"
    )
    return runner


async def stop_dashboard(runner: Optional[web.AppRunner]):
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception as e:
            logger.debug(f"Đóng Web Dashboard: {e}")
