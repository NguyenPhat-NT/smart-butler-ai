# -*- coding: utf-8 -*-
"""
LAN Watchdog tự phục hồi cho OpenStick:
- Theo dõi 3 tầng: Gateway LAN -> Internet IP (1.1.1.1/8.8.8.8) -> DNS phân giải API.
- Tự phục hồi Wi-Fi / bổ sung DNS dự phòng qua NetworkManager (`nmcli`) có cooldown chống loop.
- Tuyệt đối KHÔNG ghi đè GPIO đèn của `jz0145-leds.service`.
"""
import re
import time
import socket
import asyncio
import datetime
import logging
from typing import Dict, Any, Optional, Tuple

import config
import database
import lan_events
import lan_presence

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

META_WD_AUTO_HEAL = "wd_auto_heal"
META_WD_AUTO_DNS = "wd_auto_dns_fallback"
META_WD_TG_RECOVERY = "wd_notify_telegram_on_recovery"

_DNS_TEST_HOSTS = ("api.telegram.org", "generativelanguage.googleapis.com")
_HEAL_COOLDOWN_SEC = 90.0
_GW_CONFIRM_DELAY_SEC = 2.5
# Tránh 2 lần kiểm tra/heal chạy chồng nhau (vòng lặp 60s + nút "kiểm tra ngay" trên Dashboard)
_WD_LOCK: Optional[asyncio.Lock] = None

_WD_STATE: Dict[str, Any] = {
    "status": "starting",  # starting | healthy | dns_degraded | gateway_down | internet_down
    "gateway_ip": "192.168.1.1",
    "iface": "wlan0",
    "wifi_connection": "",
    "gateway_ok": True,
    "internet_ok": True,
    "dns_ok": True,
    "consecutive_gw_fails": 0,
    "consecutive_inet_fails": 0,
    "consecutive_dns_fails": 0,
    "outage_started_at": 0.0,
    "outage_cause": "",
    "last_heal_at": 0.0,
    "last_heal_action": "",
    "last_check_at": 0.0,
    "heals_count": 0,
    "outages_count": 0,
}


def _parse_bool(val: Any, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def get_watchdog_settings() -> Dict[str, bool]:
    meta = database.get_metadata_dict("wd_")
    return {
        "auto_heal": _parse_bool(meta.get(META_WD_AUTO_HEAL), True),
        "auto_dns_fallback": _parse_bool(meta.get(META_WD_AUTO_DNS), True),
        "notify_telegram_on_recovery": _parse_bool(meta.get(META_WD_TG_RECOVERY), True),
    }


def save_watchdog_settings(patch: Dict[str, Any]) -> Dict[str, bool]:
    if "auto_heal" in patch:
        database.set_metadata(META_WD_AUTO_HEAL, "true" if _parse_bool(patch["auto_heal"]) else "false")
    if "auto_dns_fallback" in patch:
        database.set_metadata(META_WD_AUTO_DNS, "true" if _parse_bool(patch["auto_dns_fallback"]) else "false")
    if "notify_telegram_on_recovery" in patch:
        database.set_metadata(
            META_WD_TG_RECOVERY,
            "true" if _parse_bool(patch["notify_telegram_on_recovery"]) else "false",
        )
    return get_watchdog_settings()


def parse_active_wifi_connection(nmcli_output: str, target_iface: str = "wlan0") -> str:
    """Tách tên kết nối Wi-Fi active từ `nmcli -t -f NAME,DEVICE,TYPE con show --active`."""
    for line in (nmcli_output or "").splitlines():
        if not line.strip():
            continue
        # nmcli escape dấu ':' trong tên bằng '\:'
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 3:
            name = parts[0].replace("\\:", ":").strip()
            dev = parts[1].strip()
            ctype = parts[2].strip().lower()
            if dev == target_iface or "wireless" in ctype or "wifi" in ctype:
                return name
    return ""


def parse_saved_wifi_connection(nmcli_output: str) -> str:
    """Chọn tên profile Wi-Fi đã lưu đầu tiên từ `nmcli -t -f NAME,TYPE con show`."""
    for line in (nmcli_output or "").splitlines():
        if not line.strip():
            continue
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 2:
            name = parts[0].replace("\\:", ":").strip()
            ctype = parts[1].strip().lower()
            if name and ("wireless" in ctype or "wifi" in ctype):
                return name
    return ""


async def _run_wifi_cmd(*args: str, timeout: float = 10.0) -> Tuple[int, str]:
    """Chạy lệnh hệ thống liên quan Wi-Fi (`iw`, `wpa_cli`, `nmcli`) an toàn với timeout."""
    if not args:
        return -1, "empty_cmd"
    cmd_name = str(args[0])
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return int(proc.returncode or 0), out.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return -1, "timeout"
    except FileNotFoundError:
        return -1, f"{cmd_name}_not_found"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


async def _run_nmcli(*args: str, timeout: float = 12.0) -> Tuple[int, str]:
    return await _run_wifi_cmd("nmcli", *args, timeout=timeout)


async def _rescan_and_reconnect_wifi(
    iface: str,
    con_name: str,
    hard_reset: bool = False,
) -> Tuple[int, str]:
    """Ép regdom VN, xóa BSS cache cũ, chủ động quét lại kênh 1-13 và kích hoạt kết nối."""
    await _run_wifi_cmd("iw", "reg", "set", "VN", timeout=4.0)
    if hard_reset:
        await _run_nmcli("radio", "wifi", "off", timeout=6.0)
        await asyncio.sleep(2.0)
        await _run_nmcli("radio", "wifi", "on", timeout=8.0)
        await asyncio.sleep(3.0)
        await _run_wifi_cmd("iw", "reg", "set", "VN", timeout=4.0)

    await _run_wifi_cmd("wpa_cli", "-i", iface, "bss_flush", "0", timeout=4.0)
    rc_scan, scan_out = await _run_nmcli("dev", "wifi", "rescan", "ifname", iface, timeout=10.0)
    if rc_scan != 0:
        logger.debug(f"Watchdog nmcli rescan trả rc={rc_scan} ({scan_out}); thử fallback wpa_cli scan")
        await _run_wifi_cmd("wpa_cli", "-i", iface, "scan", timeout=5.0)

    await asyncio.sleep(2.5)
    if con_name:
        rc_up, up_out = await _run_nmcli("con", "up", con_name, timeout=15.0)
    else:
        rc_up, up_out = await _run_nmcli("dev", "connect", iface, timeout=15.0)
    return rc_up, up_out


async def _check_dns_resolution(timeout_sec: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    for host in _DNS_TEST_HOSTS:
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM),
                timeout=timeout_sec,
            )
            return True
        except Exception:
            continue
    return False


async def _ping_host(host: str, timeout_sec: int = 2, retries: int = 1) -> bool:
    """Ping host với retry nhanh để tránh báo giả do rớt 1 gói ICMP trên Wi-Fi."""
    attempts = max(1, 1 + int(retries))
    for attempt in range(attempts):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", str(max(1, int(timeout_sec))), host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout_sec + 1.5)
            if rc == 0:
                return True
        except Exception:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        if attempt < attempts - 1:
            await asyncio.sleep(0.4)
    return False


def get_watchdog_snapshot() -> Dict[str, Any]:
    snap = dict(_WD_STATE)
    snap["settings"] = get_watchdog_settings()
    return snap


async def run_watchdog_check(bot=None, target_user_id: int = 0) -> Dict[str, Any]:
    """Chạy 1 chu kỳ kiểm tra (có khóa để không chạy song song với Dashboard)."""
    global _WD_LOCK
    if _WD_LOCK is None:
        _WD_LOCK = asyncio.Lock()
    async with _WD_LOCK:
        return await _run_watchdog_check_impl(bot=bot, target_user_id=target_user_id)


async def _handle_recovery_transition(
    prev_status: str,
    now_ts: float,
    settings: Dict[str, bool],
    bot=None,
    target_user_id: int = 0,
) -> None:
    started = float(_WD_STATE.get("outage_started_at") or now_ts)
    dur_sec = max(0, int(now_ts - started))
    cause_prev = _WD_STATE.get("outage_cause") or prev_status
    _WD_STATE["outage_started_at"] = 0.0
    _WD_STATE["outage_cause"] = ""
    heal_note = _WD_STATE.get("last_heal_action") or "Mạng tự ổn định"
    summary = f"Kết nối Internet/DNS đã phục hồi sau {dur_sec}s (nguyên nhân: {cause_prev}; xử lý: {heal_note})."
    database.log_system_event("watchdog", "info", "Mạng đã phục hồi", summary)
    lan_events.emit_notify(
        title="Watchdog đã phục hồi kết nối",
        body=summary,
        level="info",
        source="watchdog",
    )
    if dur_sec >= 60 and bot is not None and target_user_id > 0 and settings["notify_telegram_on_recovery"]:
        vn_str = datetime.datetime.fromtimestamp(now_ts, tz=VN_TZ).strftime("%H:%M")
        mins = max(1, round(dur_sec / 60.0))
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=f"🛠 [Watchdog • {vn_str}] Mạng OpenStick vừa gián đoạn ~{mins} phút ({cause_prev}) và đã tự phục hồi ổn định.",
                disable_notification=True,
            )
        except Exception as e:
            logger.debug(f"Không gửi được báo cáo watchdog Telegram: {e}")


async def _run_watchdog_check_impl(bot=None, target_user_id: int = 0) -> Dict[str, Any]:
    """Chạy 1 chu kỳ kiểm tra 3 tầng (Gateway -> Internet -> DNS) & tự phục hồi nếu cần."""
    now_ts = time.time()
    uid = int(target_user_id or 0)
    if uid <= 0:
        uid = int(config.ALLOWED_USER_ID or 0) or int(database.get_primary_user_id() or 0)
    settings = get_watchdog_settings()
    net = await lan_presence.get_local_network_info(_WD_STATE.get("iface") or "wlan0")
    iface = net["iface"]
    gw_ip = net["gateway_ip"]

    rc_con, nm_out = await _run_nmcli("-t", "-f", "NAME,DEVICE,TYPE", "con", "show", "--active", timeout=4.0)
    con_name = parse_active_wifi_connection(nm_out, iface) if rc_con == 0 else ""
    if not con_name:
        con_name = str(_WD_STATE.get("wifi_connection") or "").strip()
    if not con_name:
        rc_saved, saved_out = await _run_nmcli("-t", "-f", "NAME,TYPE", "con", "show", timeout=4.0)
        if rc_saved == 0:
            con_name = parse_saved_wifi_connection(saved_out)

    gw_ok = await _ping_host(gw_ip, timeout_sec=2)
    if not gw_ok:
        # Xác nhận lại sau 2.5s để phân biệt rớt 1 gói ngẫu nhiên với AP đổi kênh / mất kết nối thật
        await asyncio.sleep(_GW_CONFIRM_DELAY_SEC)
        gw_ok = await _ping_host(gw_ip, timeout_sec=2)

    inet_ok = False
    if gw_ok:
        inet_ok = await _ping_host("1.1.1.1", timeout_sec=2) or await _ping_host("8.8.8.8", timeout_sec=2)
    dns_ok = await _check_dns_resolution(timeout_sec=3.0) if (gw_ok and inet_ok) else False

    _WD_STATE["gateway_ip"] = gw_ip
    _WD_STATE["iface"] = iface
    if con_name:
        _WD_STATE["wifi_connection"] = con_name
    _WD_STATE["gateway_ok"] = gw_ok
    _WD_STATE["internet_ok"] = inet_ok
    _WD_STATE["dns_ok"] = dns_ok
    _WD_STATE["last_check_at"] = now_ts

    _WD_STATE["consecutive_gw_fails"] = 0 if gw_ok else _WD_STATE["consecutive_gw_fails"] + 1
    _WD_STATE["consecutive_inet_fails"] = 0 if inet_ok else _WD_STATE["consecutive_inet_fails"] + 1
    _WD_STATE["consecutive_dns_fails"] = 0 if dns_ok else (_WD_STATE["consecutive_dns_fails"] + 1 if inet_ok else 0)

    prev_status = _WD_STATE["status"]
    if gw_ok and inet_ok and dns_ok:
        new_status = "healthy"
    elif _WD_STATE["consecutive_gw_fails"] >= 2:
        new_status = "gateway_down"
    elif gw_ok and _WD_STATE["consecutive_inet_fails"] >= 2:
        new_status = "internet_down"
    elif gw_ok and inet_ok and _WD_STATE["consecutive_dns_fails"] >= 2:
        new_status = "dns_degraded"
    else:
        # Nhiễu 1 chu kỳ đơn lẻ: giữ nguyên trạng thái trước đó (hoặc 'healthy' nếu mới khởi kiểm)
        new_status = "healthy" if prev_status == "starting" else prev_status
        logger.debug(
            f"Watchdog jitter bỏ qua (gw={gw_ok}, inet={inet_ok}, dns={dns_ok}, "
            f"fails=({_WD_STATE['consecutive_gw_fails']},{_WD_STATE['consecutive_inet_fails']},{_WD_STATE['consecutive_dns_fails']}))"
        )

    _WD_STATE["status"] = new_status

    con_label = con_name or iface

    # Ghi nhận bắt đầu / kết thúc sự cố
    if prev_status in ("starting", "healthy") and new_status != "healthy":
        _WD_STATE["outage_started_at"] = now_ts
        _WD_STATE["outage_cause"] = new_status
        _WD_STATE["outages_count"] += 1
        cause_label = {
            "gateway_down": f"Mất Wi-Fi / Gateway ({gw_ip})",
            "internet_down": "Mất kết nối Internet (Gateway vẫn sống)",
            "dns_degraded": "Lỗi phân giải DNS dù Internet IP thông",
        }.get(new_status, new_status)
        database.log_system_event("watchdog", "warning", cause_label, f"iface={iface}, con={con_name or '-'}")
        lan_events.emit_notify(
            title="Watchdog mạng LAN",
            body=cause_label,
            level="warning",
            source="watchdog",
        )

    elif prev_status not in ("starting", "healthy") and new_status == "healthy":
        await _handle_recovery_transition(prev_status, now_ts, settings, bot=bot, target_user_id=uid)

    # Tự phục hồi ngay ở lần rớt xác nhận kép đầu tiên (>= 1) để theo kịp khi Wi-Fi đổi kênh,
    # hoặc hard reset radio + rescan + con up khi kẹt >= 3 chu kỳ, có cooldown 90s.
    heal_ready = (now_ts - float(_WD_STATE.get("last_heal_at") or 0.0)) >= _HEAL_COOLDOWN_SEC
    if settings["auto_heal"] and heal_ready and _WD_STATE["consecutive_gw_fails"] >= 1:
        _WD_STATE["last_heal_at"] = now_ts
        _WD_STATE["heals_count"] += 1
        hard_reset = _WD_STATE["consecutive_gw_fails"] >= 3
        if hard_reset:
            action = f"Reset Wi-Fi radio, quét lại kênh 1-13 & kết nối `{con_label}` trên {iface}"
            _WD_STATE["last_heal_action"] = action
            logger.warning(f"🛠 [Watchdog]: {action}")
            database.log_system_event("watchdog", "warning", "Watchdog reset Wi-Fi radio", action)
        else:
            action = f"Xóa BSS cache, quét lại kênh 1-13 & kết nối `{con_label}` trên {iface}"
            _WD_STATE["last_heal_action"] = action
            logger.warning(f"🛠 [Watchdog]: {action}")

        rc_up, up_out = await _rescan_and_reconnect_wifi(iface, con_name, hard_reset=hard_reset)
        database.log_system_event("watchdog", "info" if rc_up == 0 else "warning", action, up_out[:300])
        if rc_up == 0:
            await asyncio.sleep(2.0)
            gw_ok_after = await _ping_host(gw_ip, timeout_sec=2)
            _WD_STATE["gateway_ok"] = gw_ok_after
            if gw_ok_after:
                inet_ok_after = await _ping_host("1.1.1.1", timeout_sec=2) or await _ping_host("8.8.8.8", timeout_sec=2)
                dns_ok_after = await _check_dns_resolution(timeout_sec=3.0) if inet_ok_after else False
                _WD_STATE["internet_ok"] = inet_ok_after
                _WD_STATE["dns_ok"] = dns_ok_after
                _WD_STATE["consecutive_gw_fails"] = 0
                if inet_ok_after:
                    _WD_STATE["consecutive_inet_fails"] = 0
                if dns_ok_after:
                    _WD_STATE["consecutive_dns_fails"] = 0
                if inet_ok_after and dns_ok_after:
                    new_status = "healthy"
                    _WD_STATE["status"] = "healthy"
                    if prev_status not in ("starting", "healthy"):
                        await _handle_recovery_transition(prev_status, time.time(), settings, bot=bot, target_user_id=uid)
                elif not inet_ok_after:
                    new_status = "internet_down"
                    _WD_STATE["status"] = new_status
                elif not dns_ok_after:
                    new_status = "dns_degraded"
                    _WD_STATE["status"] = new_status

    elif settings["auto_dns_fallback"] and heal_ready and _WD_STATE["consecutive_dns_fails"] >= 2:
        _WD_STATE["last_heal_at"] = now_ts
        _WD_STATE["heals_count"] += 1
        cur_dns = ""
        if con_name:
            _, cur_dns = await _run_nmcli("-g", "ipv4.dns", "con", "show", con_name, timeout=4.0)
        if con_name and "1.1.1.1" not in cur_dns:
            action = f"Bổ sung DNS dự phòng 1.1.1.1, 8.8.8.8 vào `{con_name}`"
            _WD_STATE["last_heal_action"] = action
            logger.warning(f"🛠 [Watchdog]: {action}")
            await _run_nmcli("con", "mod", con_name, "ipv4.dns", "1.1.1.1 8.8.8.8", timeout=6.0)
            rc_re, re_out = await _run_nmcli("dev", "reapply", iface, timeout=10.0)
            if rc_re != 0:
                await _run_nmcli("con", "up", con_name, timeout=15.0)
            database.log_system_event("watchdog", "warning", action, re_out[:300])
        else:
            action = f"Làm mới NetworkManager `{iface}` do DNS kẹt"
            _WD_STATE["last_heal_action"] = action
            _, re_out = await _run_nmcli("dev", "reapply", iface, timeout=10.0)
            database.log_system_event("watchdog", "warning", action, re_out[:300] or "DNS dự phòng đã cấu hình sẵn")

    if new_status != prev_status:
        lan_events.publish("watchdog_update", get_watchdog_snapshot())
    return get_watchdog_snapshot()


async def watchdog_loop(bot=None, target_user_id: int = 0, interval_sec: float = 60.0):
    """Vòng lặp Watchdog mạng LAN chạy nền 24/7 trên OpenStick."""
    logger.info("Khởi động LAN Watchdog (Gateway / Internet / DNS Self-Heal)...")
    await asyncio.sleep(5.0)
    while True:
        try:
            await run_watchdog_check(bot=bot, target_user_id=target_user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Lỗi trong watchdog_loop: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_sec)
