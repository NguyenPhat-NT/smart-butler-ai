# -*- coding: utf-8 -*-
"""
Smart LAN Presence (OpenStick wlan0):
- Theo dõi Điện thoại & PC bằng MAC address qua bảng ARP (`ip neigh`) + ping chọn lọc.
- Phân giải động IP mới của PC cho VieNeu-TTS khi router DHCP đổi IP.
- Giữ gọn ngữ cảnh phản hồi (chỉ tối ưu nhịp scheduler & kiểm tra TTS ngầm, không tự ý lộ thông số kỹ thuật LAN).
"""
import re
import time
import asyncio
import datetime
import ipaddress
import logging
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Tuple

import config
import database
import lan_events

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_IFACE_ADDR_RE = re.compile(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)")

META_PHONE_MAC = "lan_phone_mac"
META_PC_MAC = "lan_pc_mac"
META_PRESENCE_ENABLED = "lan_presence_enabled"
META_AWAY_GRACE_MINS = "lan_away_grace_mins"
META_QUIET_START = "lan_quiet_start_hour"
META_QUIET_END = "lan_quiet_end_hour"
META_NOTIFY_SOUND = "lan_notify_sound_enabled"
META_NOTIFY_TTS = "lan_notify_tts_enabled"
META_MUSIC_DL_URL = "lan_music_dl_url"

_STATE: Dict[str, Any] = {
    "state": "unconfigured",  # unconfigured | home_pc | home_idle | sleeping_late | away
    "iface": "wlan0",
    "local_ip": "",
    "gateway_ip": "",
    "phone_mac": "",
    "pc_mac": "",
    "phone_online": False,
    "pc_online": False,
    "phone_ip": "",
    "pc_ip": "",
    "phone_last_seen": 0.0,
    "pc_last_seen": 0.0,
    "last_home_seen": 0.0,
    "away_since": 0.0,
    "just_arrived_home_at": 0.0,
    "tts_url": config.TTS_API_URL,
    "tts_online": False,
    "tts_checked_at": 0.0,
    "music_dl_url": getattr(config, "MUSIC_DL_API_URL", ""),
    "music_dl_online": False,
    "music_dl_checked_at": 0.0,
    "updated_at": 0.0,
}

_TTS_PROBE_INTERVAL_SEC = 55.0
_SCAN_LOCK: Optional[asyncio.Lock] = None
_REFRESH_LOCK: Optional[asyncio.Lock] = None


def normalize_mac(mac: str) -> str:
    """Chuẩn hóa MAC address về dạng `aa:bb:cc:dd:ee:ff`; trả rỗng nếu không hợp lệ."""
    raw = str(mac or "").strip().lower().replace("-", ":").replace(".", ":")
    if not _MAC_RE.match(raw):
        return ""
    if raw in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return ""
    return raw


def _is_ipv4(ip_str: str) -> bool:
    try:
        return ipaddress.ip_address(ip_str).version == 4
    except ValueError:
        return False


def parse_ip_neigh(output: str) -> List[Dict[str, str]]:
    """Phân tích đầu ra `ip neigh show`, chỉ giữ IPv4 có MAC hợp lệ."""
    entries: List[Dict[str, str]] = []
    seen = set()
    for line in (output or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        ip_addr = parts[0].strip()
        if not _is_ipv4(ip_addr):
            continue
        state = parts[-1].upper()
        if state in ("FAILED", "INCOMPLETE"):
            continue
        dev = ""
        mac = ""
        for idx, tok in enumerate(parts):
            if tok == "dev" and idx + 1 < len(parts):
                dev = parts[idx + 1]
            elif tok == "lladdr" and idx + 1 < len(parts):
                mac = normalize_mac(parts[idx + 1])
        if not mac:
            continue
        key = (ip_addr, mac)
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "ip": ip_addr,
            "dev": dev,
            "mac": mac,
            "state": state,
        })
    return entries


async def _run_cmd(*args: str, timeout: float = 3.0) -> str:
    """Chạy lệnh hệ thống bất đồng bộ không làm block event loop."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return ""
    except Exception:
        return ""


async def _ping_one(ip_addr: str, timeout_sec: int = 1) -> bool:
    """Ping 1 gói ICMP nhanh tới IPv4 nội bộ."""
    if not _is_ipv4(ip_addr):
        return False
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, int(timeout_sec))), ip_addr,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout_sec + 1.5)
        return rc == 0
    except Exception:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return False


def _parse_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def _parse_bool(val: Any, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def normalize_service_url(raw_url: str) -> str:
    """Chuẩn hóa URL HTTP(S) cho dịch vụ LAN (trả rỗng nếu không hợp lệ)."""
    s = str(raw_url or "").strip().rstrip("/")
    if not s or any(ch.isspace() for ch in s):
        return ""
    if "://" not in s:
        s = f"http://{s}"
    try:
        parsed = urlparse(s)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.hostname}{port}{path}"


def get_lan_settings() -> Dict[str, Any]:
    """Đọc toàn bộ cấu hình LAN Presence & Notification từ app_metadata."""
    meta = database.get_metadata_dict("lan_")
    return {
        "phone_mac": normalize_mac(meta.get(META_PHONE_MAC, "")),
        "pc_mac": normalize_mac(meta.get(META_PC_MAC, "")),
        "presence_enabled": _parse_bool(meta.get(META_PRESENCE_ENABLED), True),
        "away_grace_mins": _parse_int(meta.get(META_AWAY_GRACE_MINS), 8, 2, 60),
        "quiet_start_hour": _parse_int(meta.get(META_QUIET_START), 0, 0, 23),
        "quiet_end_hour": _parse_int(meta.get(META_QUIET_END), 7, 0, 12),
        "notify_sound_enabled": _parse_bool(meta.get(META_NOTIFY_SOUND), True),
        "notify_tts_enabled": _parse_bool(meta.get(META_NOTIFY_TTS), True),
        "music_dl_url": normalize_service_url(meta.get(META_MUSIC_DL_URL, "")),
    }


def save_lan_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Cập nhật cấu hình LAN Presence & Notification xuống app_metadata."""
    if "phone_mac" in patch:
        database.set_metadata(META_PHONE_MAC, normalize_mac(patch.get("phone_mac", "")))
    if "pc_mac" in patch:
        database.set_metadata(META_PC_MAC, normalize_mac(patch.get("pc_mac", "")))
    if "presence_enabled" in patch:
        database.set_metadata(META_PRESENCE_ENABLED, "true" if _parse_bool(patch["presence_enabled"]) else "false")
    if "away_grace_mins" in patch:
        database.set_metadata(META_AWAY_GRACE_MINS, str(_parse_int(patch["away_grace_mins"], 8, 2, 60)))
    if "quiet_start_hour" in patch:
        database.set_metadata(META_QUIET_START, str(_parse_int(patch["quiet_start_hour"], 0, 0, 23)))
    if "quiet_end_hour" in patch:
        database.set_metadata(META_QUIET_END, str(_parse_int(patch["quiet_end_hour"], 7, 0, 12)))
    if "notify_sound_enabled" in patch:
        database.set_metadata(META_NOTIFY_SOUND, "true" if _parse_bool(patch["notify_sound_enabled"]) else "false")
    if "notify_tts_enabled" in patch:
        database.set_metadata(META_NOTIFY_TTS, "true" if _parse_bool(patch["notify_tts_enabled"]) else "false")
    if "music_dl_url" in patch:
        norm_dl_url = normalize_service_url(patch.get("music_dl_url", ""))
        database.set_metadata(META_MUSIC_DL_URL, norm_dl_url)
        _STATE["music_dl_url"] = norm_dl_url
        _STATE["music_dl_online"] = False
        _STATE["music_dl_checked_at"] = 0.0
    return get_lan_settings()


def is_quiet_hours_now(
    now_ts: Optional[float] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Kiểm tra xem thời điểm hiện tại (giờ VN) có đang nằm trong khung giờ yên lặng ban đêm hay không."""
    cfg = settings if settings is not None else get_lan_settings()
    ts = float(now_ts if now_ts is not None else time.time())
    vn_hour = datetime.datetime.fromtimestamp(ts, tz=VN_TZ).hour
    q_start = int(cfg.get("quiet_start_hour", 0))
    q_end = int(cfg.get("quiet_end_hour", 7))
    if q_start == q_end:
        return False
    if q_start < q_end:
        return q_start <= vn_hour < q_end
    return vn_hour >= q_start or vn_hour < q_end


async def get_local_network_info(preferred_iface: str = "wlan0") -> Dict[str, str]:
    """Lấy interface, IP nội bộ và Gateway hiện tại của OpenStick."""
    route_out = await _run_cmd("ip", "-4", "route", "show", "default", timeout=2.5)
    gw_ip = ""
    iface = preferred_iface
    for line in route_out.splitlines():
        parts = line.split()
        if "via" in parts:
            idx = parts.index("via")
            if idx + 1 < len(parts) and _is_ipv4(parts[idx + 1]):
                gw_ip = parts[idx + 1]
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                iface = parts[idx + 1]
        if gw_ip:
            break

    addr_out = await _run_cmd("ip", "-4", "-o", "addr", "show", "dev", iface, timeout=2.5)
    local_ip = ""
    prefix_len = 24
    m = _IFACE_ADDR_RE.search(addr_out)
    if m:
        local_ip = m.group(1)
        prefix_len = int(m.group(2))
    return {
        "iface": iface,
        "local_ip": local_ip,
        "prefix_len": str(prefix_len),
        "gateway_ip": gw_ip or "192.168.1.1",
    }


def _read_local_host_hints() -> Dict[str, str]:
    """Đọc hostname gợi ý từ dnsmasq.leases hoặc /etc/hosts nếu có."""
    hints: Dict[str, str] = {}
    for lease_path in ("/var/lib/misc/dnsmasq.leases", "/tmp/dhcp.leases"):
        try:
            with open(lease_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4:
                        mac = normalize_mac(parts[1])
                        ip = parts[2]
                        name = parts[3] if parts[3] != "*" else ""
                        if name:
                            if mac:
                                hints[mac] = name
                            if _is_ipv4(ip):
                                hints[ip] = name
        except OSError:
            pass
    try:
        with open("/etc/hosts", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                parts = line.split()
                if len(parts) >= 2 and _is_ipv4(parts[0]):
                    hints[parts[0]] = parts[1]
    except OSError:
        pass
    return hints


def _rewrite_url_host(base_url: str, host_ip: str = "", override_port: Optional[int] = None) -> str:
    cfg = str(base_url or "").strip().rstrip("/")
    if not cfg:
        return ""
    if not host_ip or not _is_ipv4(host_ip):
        if override_port is None:
            return cfg
    parsed = urlparse(cfg if "://" in cfg else f"http://{cfg}")
    scheme = parsed.scheme or "http"
    target_host = host_ip if (host_ip and _is_ipv4(host_ip)) else (parsed.hostname or "")
    if not target_host:
        return ""
    chosen_port = override_port if override_port is not None else parsed.port
    port = f":{chosen_port}" if chosen_port else ""
    path = parsed.path.rstrip("/")
    return f"{scheme}://{target_host}{port}{path}"


def resolve_tts_url_for_pc_ip(pc_ip: str = "", base_config_url: Optional[str] = None) -> str:
    """Xây URL VieNeu-TTS động theo IP mới của PC (giữ nguyên scheme & port trong .env)."""
    cfg = (config.TTS_API_URL if base_config_url is None else base_config_url).strip().rstrip("/")
    if not cfg:
        return ""
    if not pc_ip or not _is_ipv4(pc_ip):
        return cfg
    return _rewrite_url_host(cfg, pc_ip)


def get_effective_tts_url() -> str:
    """Trả về URL VieNeu-TTS hiệu lực hiện tại (đã tự cập nhật IP PC nếu gán MAC)."""
    return _STATE.get("tts_url") or config.TTS_API_URL


def is_tts_online() -> bool:
    """True nếu VieNeu-TTS gần nhất kiểm tra đang sẵn sàng."""
    return bool(_STATE.get("tts_online"))


def get_configured_music_dl_url() -> str:
    """Trả về URL Music Download Bridge cấu hình (ưu tiên Dashboard LAN settings -> .env)."""
    try:
        db_url = get_lan_settings().get("music_dl_url") or ""
        if db_url:
            return db_url
    except Exception:
        pass
    return normalize_service_url(getattr(config, "MUSIC_DL_API_URL", ""))


def get_effective_music_dl_url() -> str:
    """Trả về URL PC Music Download Bridge hiệu lực hiện tại."""
    return _STATE.get("music_dl_url") or get_configured_music_dl_url()


def is_music_dl_online() -> bool:
    """True nếu PC Music Download Bridge gần nhất kiểm tra đang sẵn sàng."""
    return bool(_STATE.get("music_dl_online"))


def get_presence_snapshot() -> Dict[str, Any]:
    """Bản chụp trạng thái Presence hiện tại (an toàn để serialize JSON)."""
    snap = dict(_STATE)
    snap["settings"] = get_lan_settings()
    return snap


async def scan_lan_devices(probe_subnet: bool = True) -> List[Dict[str, Any]]:
    """Quét nhanh mạng LAN /24 bằng ping nhẹ + đọc `ip neigh` để chọn 1-click trên Dashboard."""
    global _SCAN_LOCK
    if _SCAN_LOCK is None:
        _SCAN_LOCK = asyncio.Lock()
    async with _SCAN_LOCK:
        net = await get_local_network_info(_STATE.get("iface") or "wlan0")
        iface = net["iface"]
        local_ip = net["local_ip"]
        gw_ip = net["gateway_ip"]

        if probe_subnet and _is_ipv4(local_ip):
            try:
                network = ipaddress.IPv4Network(f"{local_ip}/{net['prefix_len']}", strict=False)
                if network.num_addresses <= 256:
                    sem = asyncio.Semaphore(28)

                    async def _probe(target_ip: str):
                        async with sem:
                            await _ping_one(target_ip, timeout_sec=1)

                    targets = [
                        str(ip) for ip in network.hosts()
                        if str(ip) != local_ip
                    ]
                    await asyncio.gather(*(_probe(t) for t in targets), return_exceptions=True)
            except Exception as e:
                logger.debug(f"Quét subnet bỏ qua: {e}")

        neigh_out = await _run_cmd("ip", "neigh", "show", "dev", iface, timeout=2.5)
        if not neigh_out:
            neigh_out = await _run_cmd("ip", "neigh", "show", timeout=2.5)
        entries = parse_ip_neigh(neigh_out)
        hints = _read_local_host_hints()
        settings = get_lan_settings()
        phone_mac = settings["phone_mac"]
        pc_mac = settings["pc_mac"]

        static_tts_host = ""
        if config.TTS_API_URL:
            try:
                static_tts_host = urlparse(config.TTS_API_URL).hostname or ""
            except Exception:
                static_tts_host = ""

        result: List[Dict[str, Any]] = []
        for item in entries:
            ip = item["ip"]
            mac = item["mac"]
            hostname = hints.get(mac) or hints.get(ip) or ""
            is_gw = (ip == gw_ip)
            if not hostname and is_gw:
                hostname = "Router / Gateway"
            elif not hostname and ip == static_tts_host:
                hostname = "VieNeu-TTS (.env)"
            result.append({
                "ip": ip,
                "mac": mac,
                "dev": item["dev"],
                "state": item["state"],
                "hostname": hostname,
                "is_gateway": is_gw,
                "is_phone": bool(phone_mac and mac == phone_mac),
                "is_pc": bool(pc_mac and mac == pc_mac),
            })

        result.sort(key=lambda x: (
            0 if (x["is_phone"] or x["is_pc"]) else 1,
            1 if x["is_gateway"] else 0,
            tuple(int(p) for p in x["ip"].split(".")),
        ))
        return result


def compute_presence_state(
    *,
    settings: Dict[str, Any],
    phone_online: bool,
    pc_online: bool,
    last_home_seen: float,
    now_ts: float,
    last_user_msg_ts: float = 0.0,
) -> str:
    """Tính trạng thái Presence chuẩn có hysteresis chống chập chờn Wi-Fi điện thoại."""
    has_targets = bool(settings.get("phone_mac") or settings.get("pc_mac"))
    if not has_targets or not settings.get("presence_enabled", True):
        return "unconfigured"

    grace_sec = float(settings.get("away_grace_mins", 8)) * 60.0
    if pc_online:
        return "home_pc"

    in_quiet = is_quiet_hours_now(now_ts=now_ts, settings=settings)

    effectively_home = phone_online or (last_home_seen > 0 and (now_ts - last_home_seen) < grace_sec)
    if effectively_home:
        if in_quiet and (now_ts - last_user_msg_ts) > 1200:
            return "sleeping_late"
        return "home_idle"

    if in_quiet and (now_ts - last_user_msg_ts) > 1200:
        return "sleeping_late"
    return "away"


def should_allow_autonomous_proactive(user_state: Dict[str, Any], now_ts: float) -> Tuple[bool, str]:
    """
    Quyết định xem nhịp Autonomous Proactive (Priority 3) có nên đánh thức AI ngay lúc này không.
    - KHÔNG chặn `user_reminder` (nhắc hẹn anh dặn).
    - Không bao giờ để lộ ra prompt bất kỳ từ khóa LAN/PC nào.
    """
    settings = get_lan_settings()
    if not settings.get("presence_enabled", True):
        return True, "presence_disabled"

    state = _STATE.get("state", "unconfigured")
    if state == "unconfigured":
        return True, "unconfigured"

    last_user_ts = float(user_state.get("last_user_message_time") or 0.0)
    last_bot_ts = float(user_state.get("last_bot_message_time") or 0.0)
    idle_since_last = now_ts - max(last_user_ts, last_bot_ts)

    if state == "sleeping_late":
        return False, "sleeping_late"

    if state == "away":
        away_since = float(_STATE.get("away_since") or 0.0)
        away_mins = (now_ts - away_since) / 60.0 if away_since > 0 else 0.0
        # Đang ra ngoài lâu: giãn nhịp chủ động (chờ về phòng hoặc tối đa rất thưa ban ngày)
        if away_mins >= 10 and idle_since_last < 10800:
            return False, "away_room"

    return True, f"presence:{state}"


async def refresh_presence_once(target_user_id: int = 0) -> Dict[str, Any]:
    """Thực hiện 1 chu kỳ kiểm tra (có khóa: presence_loop và nút Refresh trên Dashboard không chạy chồng)."""
    global _REFRESH_LOCK
    if _REFRESH_LOCK is None:
        _REFRESH_LOCK = asyncio.Lock()
    async with _REFRESH_LOCK:
        return await _refresh_presence_once_impl(target_user_id=target_user_id)


async def _tcp_port_reachable(host: str, port: int, timeout_sec: float = 0.8) -> bool:
    """Kiểm tra nhanh cổng TCP (dự phòng khi Windows Firewall chặn ICMP Ping nhưng HTTP vẫn mở)."""
    if not host or port <= 0:
        return False
    try:
        conn = asyncio.open_connection(host, int(port))
        reader, writer = await asyncio.wait_for(conn, timeout=timeout_sec)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _configured_pc_service_ports(settings: Dict[str, Any]) -> List[int]:
    ports: List[int] = []
    for raw_url in (
        getattr(config, "TTS_API_URL", "") or "",
        getattr(config, "MUSIC_DL_API_URL", "") or "",
        str(settings.get("music_dl_url") or ""),
    ):
        u = raw_url.strip()
        if not u:
            continue
        try:
            parsed = urlparse(u)
            if parsed.port and parsed.port not in ports:
                ports.append(int(parsed.port))
        except Exception:
            pass
    for default_p in (8090, 8091):
        if default_p not in ports:
            ports.append(default_p)
    return ports


async def _host_reachable_via_icmp_or_tcp(host: str, ports: List[int]) -> bool:
    if await _ping_one(host, timeout_sec=1):
        return True
    for p in ports:
        if await _tcp_port_reachable(host, p, timeout_sec=0.8):
            return True
    return False


async def _refresh_presence_once_impl(target_user_id: int = 0) -> Dict[str, Any]:
    """Thực hiện 1 chu kỳ kiểm tra `ip neigh`, làm mới ping/TCP có chọn lọc & check TTS health."""
    import tts_client

    now_ts = time.time()
    settings = get_lan_settings()
    phone_mac = settings["phone_mac"]
    pc_mac = settings["pc_mac"]
    pc_probe_ports = _configured_pc_service_ports(settings)

    net = await get_local_network_info(_STATE.get("iface") or "wlan0")
    iface = net["iface"]
    _STATE["iface"] = iface
    _STATE["local_ip"] = net["local_ip"]
    _STATE["gateway_ip"] = net["gateway_ip"]

    neigh_out = await _run_cmd("ip", "neigh", "show", "dev", iface, timeout=2.5)
    entries = parse_ip_neigh(neigh_out)
    by_mac = {e["mac"]: e for e in entries}

    async def _check_target(mac: str, last_ip: str, tcp_ports: Optional[List[int]] = None) -> Tuple[bool, str]:
        if not mac:
            return False, ""
        entry = by_mac.get(mac)
        if entry:
            if entry["state"] == "REACHABLE":
                return True, entry["ip"]
            # STALE/DELAY/PROBE -> ping 1 gói xác thực lại (nếu PC chặn ICMP thì thử thêm cổng TCP dịch vụ)
            if tcp_ports:
                if await _host_reachable_via_icmp_or_tcp(entry["ip"], tcp_ports):
                    return True, entry["ip"]
            elif await _ping_one(entry["ip"], timeout_sec=1):
                return True, entry["ip"]
            return False, entry["ip"]
        if last_ip and _is_ipv4(last_ip):
            reached = (
                await _host_reachable_via_icmp_or_tcp(last_ip, tcp_ports)
                if tcp_ports
                else await _ping_one(last_ip, timeout_sec=1)
            )
            if reached:
                # Đọc lại ip neigh để khớp đúng MAC trên IP đó
                refreshed = parse_ip_neigh(await _run_cmd("ip", "neigh", "show", last_ip, timeout=2.0))
                if any(r["mac"] == mac for r in refreshed):
                    return True, last_ip
        return False, last_ip

    (phone_online, phone_ip), (pc_online, pc_ip) = await asyncio.gather(
        _check_target(phone_mac, _STATE.get("phone_ip", "")),
        _check_target(pc_mac, _STATE.get("pc_ip", ""), tcp_ports=pc_probe_ports),
    )

    # Bổ sung từ bảng hostapd/assoclist của Router OpenWrt (điện thoại iOS/Android ngủ sâu vẫn bám Wi-Fi)
    if phone_mac and (not phone_online or not phone_ip):
        try:
            import router_controller
            assoc_map = router_controller.get_controller().get_associated_mac_map()
            p_assoc = assoc_map.get(phone_mac)
            if p_assoc:
                phone_online = True
                if p_assoc.get("ip"):
                    phone_ip = str(p_assoc["ip"])
        except Exception:
            pass

    if phone_online:
        _STATE["phone_last_seen"] = now_ts
    if pc_online:
        _STATE["pc_last_seen"] = now_ts

    any_online = phone_online or pc_online
    if any_online:
        _STATE["last_home_seen"] = now_ts

    uid = int(target_user_id or 0)
    if uid <= 0:
        uid = int(config.ALLOWED_USER_ID or 0) or int(database.get_primary_user_id() or 0)

    last_user_msg_ts = 0.0
    if uid > 0:
        try:
            u = database.get_or_create_user(uid)
            last_user_msg_ts = float(u.get("last_user_message_time") or 0.0)
        except Exception:
            pass

    prev_state = _STATE.get("state", "unconfigured")
    prev_phone_online = bool(_STATE.get("phone_online"))
    prev_pc_online = bool(_STATE.get("pc_online"))
    prev_pc_ip = _STATE.get("pc_ip", "")

    new_state = compute_presence_state(
        settings=settings,
        phone_online=phone_online,
        pc_online=pc_online,
        last_home_seen=float(_STATE.get("last_home_seen") or 0.0),
        now_ts=now_ts,
        last_user_msg_ts=last_user_msg_ts,
    )

    if new_state == "away":
        if not _STATE.get("away_since"):
            _STATE["away_since"] = now_ts
    else:
        if prev_state == "away" and new_state in ("home_pc", "home_idle"):
            away_dur = now_ts - float(_STATE.get("away_since") or now_ts)
            if away_dur >= 900:
                _STATE["just_arrived_home_at"] = now_ts
                logger.info(f"🏠 [LAN Presence]: vừa về phòng sau {int(away_dur // 60)} phút đi vắng.")
        _STATE["away_since"] = 0.0

    by_ip = {e["ip"]: e for e in entries}

    # Helper kiểm tra sức khỏe cho service LAN (dùng chung cho VieNeu-TTS & Music DL Bridge)
    async def _probe_candidates(
        candidates: List[str],
        default_url: str,
        checked_at_key: str,
        url_key: str,
        online_key: str,
        probe_fn,
        log_prefix: str,
    ) -> None:
        if not candidates:
            _STATE[online_key] = False
            _STATE[url_key] = default_url
            return
        should_probe = (
            (now_ts - float(_STATE.get(checked_at_key) or 0.0)) >= _TTS_PROBE_INTERVAL_SEC
            or (not prev_pc_online and pc_online)
            or bool(pc_ip and pc_ip != prev_pc_ip)
        )
        if not should_probe:
            return
        prev_ok = bool(_STATE.get(online_key))
        prev_url = _STATE.get(url_key, "")
        chosen_url = default_url
        svc_ok = False
        for cand_url in candidates:
            cand_host = ""
            try:
                cand_host = urlparse(cand_url).hostname or ""
            except Exception:
                cand_host = ""
            if cand_host == pc_ip and pc_mac and not pc_online:
                continue
            if cand_host and cand_host != pc_ip and _is_ipv4(cand_host):
                entry = by_ip.get(cand_host)
                if not entry or entry["state"] != "REACHABLE":
                    cand_port = 0
                    try:
                        cand_port = int(urlparse(cand_url).port or 0)
                    except Exception:
                        cand_port = 0
                    check_ports = [cand_port] if cand_port > 0 else pc_probe_ports
                    if not await _host_reachable_via_icmp_or_tcp(cand_host, check_ports):
                        continue
            if await probe_fn(cand_url):
                chosen_url = cand_url
                svc_ok = True
                break
        _STATE[url_key] = chosen_url
        _STATE[online_key] = svc_ok
        _STATE[checked_at_key] = now_ts
        if svc_ok != prev_ok or (svc_ok and chosen_url != prev_url):
            logger.info(f"{log_prefix}: {'ONLINE' if svc_ok else 'OFFLINE'} ({chosen_url})")

    # Xây danh sách ứng viên URL VieNeu-TTS: giữ URL trong .env (vd .132) + fallback IP mới của pc_mac
    candidates: List[str] = []
    if config.TTS_API_URL:
        candidates.append(config.TTS_API_URL)
    if pc_mac and pc_ip:
        pc_tts = resolve_tts_url_for_pc_ip(pc_ip)
        if pc_tts and pc_tts not in candidates:
            candidates.append(pc_tts)

    default_tts_url = config.TTS_API_URL or (resolve_tts_url_for_pc_ip(pc_ip) if (pc_mac and pc_ip) else "")
    _STATE["phone_mac"] = phone_mac
    _STATE["pc_mac"] = pc_mac
    _STATE["phone_online"] = phone_online
    _STATE["pc_online"] = pc_online
    _STATE["phone_ip"] = phone_ip
    _STATE["pc_ip"] = pc_ip
    _STATE["state"] = new_state
    if not _STATE.get("tts_url") or _STATE.get("tts_url") not in candidates:
        _STATE["tts_url"] = default_tts_url

    await _probe_candidates(
        candidates,
        default_tts_url,
        "tts_checked_at",
        "tts_url",
        "tts_online",
        lambda u: tts_client.check_tts_health(u, timeout_sec=2.5),
        "🎙️ [TTS Health]",
    )

    # Xây ứng viên URL Music Download Bridge: URL đã cấu hình -> IP mới của pc_mac -> tự dò cổng 8091 trên TTS/PC host
    cfg_dl_url = settings.get("music_dl_url") or normalize_service_url(getattr(config, "MUSIC_DL_API_URL", ""))
    dl_candidates: List[str] = []
    if cfg_dl_url:
        dl_candidates.append(cfg_dl_url)
        if pc_mac and pc_ip:
            pc_dl = _rewrite_url_host(cfg_dl_url, pc_ip)
            if pc_dl and pc_dl not in dl_candidates:
                dl_candidates.append(pc_dl)
    else:
        for base_src in (config.TTS_API_URL, _STATE.get("tts_url", "")):
            auto_u = _rewrite_url_host(base_src, override_port=8091) if base_src else ""
            if auto_u and auto_u not in dl_candidates:
                dl_candidates.append(auto_u)
        if pc_mac and pc_ip and pc_online:
            pc_auto = f"http://{pc_ip}:8091"
            if pc_auto not in dl_candidates:
                dl_candidates.append(pc_auto)

    default_dl_url = cfg_dl_url or (dl_candidates[0] if dl_candidates else "")
    if not _STATE.get("music_dl_url") or _STATE.get("music_dl_url") not in dl_candidates:
        _STATE["music_dl_url"] = default_dl_url

    async def _probe_music_dl(cand_u: str) -> bool:
        try:
            import esp32_audio
            return await esp32_audio.check_music_dl_health(cand_u, timeout_sec=2.0)
        except Exception:
            return False

    await _probe_candidates(
        dl_candidates,
        default_dl_url,
        "music_dl_checked_at",
        "music_dl_url",
        "music_dl_online",
        _probe_music_dl,
        "💿 [Music DL Bridge]",
    )

    _STATE["updated_at"] = now_ts
    database.clear_pc_now_playing_if_stale(force_pc_offline=bool(pc_mac and not pc_online))
    if new_state != prev_state or pc_online != prev_pc_online or phone_online != prev_phone_online or pc_ip != prev_pc_ip:
        lan_events.publish("presence_update", get_presence_snapshot())
    return get_presence_snapshot()


async def presence_loop(target_user_id: int = 0, interval_sec: float = 45.0):
    """Vòng lặp chạy nền theo dõi LAN Presence trên OpenStick."""
    logger.info("Khởi động Smart LAN Presence Monitor (MAC-based)...")
    await asyncio.sleep(2.0)
    while True:
        try:
            await refresh_presence_once(target_user_id=target_user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Lỗi trong presence_loop: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_sec)
