# -*- coding: utf-8 -*-
"""
Điều khiển & Giám sát Router Wi-Fi OpenWrt qua `/ubus` JSON-RPC (Pure Python + aiohttp).

Thiết kế an toàn ("Read-Mostly + Guarded Actions"):
- Đọc sức khỏe Router (`system.info`, `system.board`), Multi-WAN (`mwan3`, `network.interface.dump`),
  Wi-Fi STA uplink & AP clients (`luci-rpc.getWirelessDevices`, `iwinfo.info`, `iwinfo.assoclist`),
  DHCP leases & host hints (`luci-rpc.getDHCPLeases`, `luci-rpc.getHostHints`).
- Hỗ trợ nhận diện thiết bị quen / cảnh báo MAC lạ vào Wi-Fi & tăng độ chính xác LAN Presence.
- Chỉ mở các thao tác an toàn qua AI/MCP:
  `refresh`, `wake_pc` (Wake-on-LAN), `kick_client` (cấm cứng không đụng MAC hệ thống/IoT/chủ nhà),
  `trust_mac` / `untrust_mac`.
- Tuyệt đối KHÔNG cho AI đổi SSID/mật khẩu, sửa Firewall/WAN, tắt radio Wi-Fi, hay reboot từ chat.
  (Nút Reboot chỉ gọi riêng từ Web Dashboard khi xác nhận tay.)
"""
import asyncio
import datetime
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import aiohttp

import config
import database
import lan_events
import lan_presence

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

META_ROUTER_ENABLED = "router_enabled"
META_ROUTER_URL = "router_url"
META_ROUTER_USER = "router_username"
META_ROUTER_PASS = "router_password"
META_ROUTER_NOTIFY_UNKNOWN = "router_notify_unknown"
META_ROUTER_BOOST_PRESENCE = "router_boost_presence"
META_ROUTER_KNOWN_MACS = "router_known_macs"

_FORBIDDEN_AI_ACTIONS = {
    "reboot", "restart", "poweroff", "shutdown", "reset", "factory_reset",
    "set_wifi", "wifi_off", "disable_wifi", "change_password", "set_password",
    "set_ssid", "firewall", "uci_set", "exec", "shell", "sysupgrade",
}

_UNKNOWN_ALERT_COOLDOWN_SEC = 6 * 3600.0
_DEFAULT_UNKNOWN_GRACE_BOOT = True


def normalize_router_url(raw_url: str) -> str:
    """Chuẩn hóa URL gốc router (`http://192.168.1.1`), loại bỏ trailing slash."""
    s = str(raw_url or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = f"http://{s}"
    try:
        parsed = urlparse(s)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return ""
        base = f"{parsed.scheme.lower()}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        return base
    except Exception:
        return ""


def _parse_bool(val: Any, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def _load_known_mac_aliases(raw_val: Optional[str] = None) -> Dict[str, str]:
    raw = raw_val if raw_val is not None else (database.get_metadata(META_ROUTER_KNOWN_MACS) or "")
    if not raw:
        return {}
    import json
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            out: Dict[str, str] = {}
            for k, v in obj.items():
                mk = lan_presence.normalize_mac(k)
                lbl = str(v or "").strip()[:60]
                if mk:
                    out[mk] = lbl or "Thiết bị quen"
            return out
    except Exception:
        pass
    return {}


def _save_known_mac_aliases(aliases: Dict[str, str]) -> Dict[str, str]:
    import json
    clean: Dict[str, str] = {}
    for k, v in (aliases or {}).items():
        mk = lan_presence.normalize_mac(k)
        if mk:
            clean[mk] = str(v or "").strip()[:60] or "Thiết bị quen"
    database.set_metadata(META_ROUTER_KNOWN_MACS, json.dumps(clean, ensure_ascii=False))
    return clean


def get_router_settings(mask_secret: bool = False) -> Dict[str, Any]:
    """Đọc cấu hình Router OpenWrt (ưu tiên SQLite metadata, fallback `.env`)."""
    meta = database.get_metadata_dict("router_")
    env_url = normalize_router_url(getattr(config, "ROUTER_URL", "") or os.getenv("ROUTER_URL", "http://192.168.1.1"))
    env_user = (getattr(config, "ROUTER_USER", "") or os.getenv("ROUTER_USER", "root") or "root").strip()
    env_pass = (getattr(config, "ROUTER_PASSWORD", "") or os.getenv("ROUTER_PASSWORD", "") or "").strip()

    raw_url = meta.get(META_ROUTER_URL)
    url = normalize_router_url(raw_url) if raw_url else env_url
    if not url:
        url = "http://192.168.1.1"

    user = str(meta.get(META_ROUTER_USER) or env_user or "root").strip() or "root"
    password = str(meta.get(META_ROUTER_PASS) if META_ROUTER_PASS in meta else env_pass).strip()
    has_password = bool(password)

    enabled_raw = meta.get(META_ROUTER_ENABLED)
    enabled = _parse_bool(enabled_raw, default=has_password)

    return {
        "enabled": bool(enabled and has_password),
        "raw_enabled": bool(enabled),
        "router_url": url,
        "username": user,
        "password": "" if mask_secret else password,
        "has_password": has_password,
        "notify_unknown_device": _parse_bool(meta.get(META_ROUTER_NOTIFY_UNKNOWN), True),
        "boost_presence": _parse_bool(meta.get(META_ROUTER_BOOST_PRESENCE), True),
        "known_macs": _load_known_mac_aliases(meta.get(META_ROUTER_KNOWN_MACS, "")),
    }


def save_router_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Lưu cấu hình Router OpenWrt và xóa session cache nếu thông số đăng nhập thay đổi."""
    changed_auth = False
    if "router_url" in patch:
        norm = normalize_router_url(patch["router_url"])
        database.set_metadata(META_ROUTER_URL, norm)
        changed_auth = True
    if "username" in patch:
        database.set_metadata(META_ROUTER_USER, str(patch.get("username") or "root").strip() or "root")
        changed_auth = True
    if "password" in patch:
        pw = str(patch.get("password") or "").strip()
        if pw:
            database.set_metadata(META_ROUTER_PASS, pw)
            changed_auth = True
    if "clear_password" in patch and patch["clear_password"]:
        database.set_metadata(META_ROUTER_PASS, "")
        changed_auth = True
    if "enabled" in patch:
        database.set_metadata(META_ROUTER_ENABLED, "true" if _parse_bool(patch["enabled"]) else "false")
    if "notify_unknown_device" in patch:
        database.set_metadata(
            META_ROUTER_NOTIFY_UNKNOWN,
            "true" if _parse_bool(patch["notify_unknown_device"]) else "false",
        )
    if "boost_presence" in patch:
        database.set_metadata(
            META_ROUTER_BOOST_PRESENCE,
            "true" if _parse_bool(patch["boost_presence"]) else "false",
        )
    if changed_auth:
        get_controller().invalidate_session()
    return get_router_settings(mask_secret=True)


def _get_local_host_macs() -> Set[str]:
    """Đọc MAC của chính OpenStick từ `/sys/class/net/*/address` (tránh tự kick mình)."""
    macs: Set[str] = set()
    sys_net = "/sys/class/net"
    try:
        if os.path.isdir(sys_net):
            for iface in os.listdir(sys_net):
                if iface == "lo":
                    continue
                addr_file = os.path.join(sys_net, iface, "address")
                if os.path.isfile(addr_file):
                    with open(addr_file, "r", encoding="utf-8", errors="ignore") as f:
                        norm = lan_presence.normalize_mac(f.read().strip())
                        if norm and norm != "00:00:00:00:00:00":
                            macs.add(norm)
    except Exception:
        pass
    return macs


def _collect_known_and_protected_macs() -> Tuple[Dict[str, str], Set[str]]:
    """
    Tổng hợp bảng ánh xạ MAC -> tên gợi nhớ và tập MAC được bảo vệ tuyệt đối không cho kick.
    """
    labels: Dict[str, str] = {}
    protected: Set[str] = set()

    # 1. Chính OpenStick & các thiết bị cấu hình tay
    for m in _get_local_host_macs():
        labels[m] = "OpenStick Bot"
        protected.add(m)

    lan_cfg = lan_presence.get_lan_settings()
    phone_mac = lan_presence.normalize_mac(lan_cfg.get("phone_mac", ""))
    pc_mac = lan_presence.normalize_mac(lan_cfg.get("pc_mac", ""))
    if phone_mac:
        labels[phone_mac] = "Điện thoại của anh"
        protected.add(phone_mac)
    if pc_mac:
        labels[pc_mac] = "PC của anh"
        protected.add(pc_mac)

    # 2. Quạt BLDC, Ổ cắm BroadLink, Loa ESP32
    try:
        import fan_controller
        fan_snap = fan_controller.get_controller().get_status_snapshot()
        fan_mac = lan_presence.normalize_mac((fan_snap.get("settings") or {}).get("mac", ""))
        if fan_mac:
            labels[fan_mac] = "Quạt phòng BLDC"
            protected.add(fan_mac)
    except Exception:
        pass

    try:
        import smart_plugs
        for p in smart_plugs.get_controller().get_status_snapshot():
            pm = lan_presence.normalize_mac(p.get("mac", ""))
            if pm:
                labels[pm] = str(p.get("name") or "Ổ cắm thông minh")
                protected.add(pm)
    except Exception:
        pass

    try:
        import esp32_audio
        esp = (esp32_audio.get_manager().get_status_snapshot().get("esp32") or {})
        em = lan_presence.normalize_mac(esp.get("mac", ""))
        if em:
            labels[em] = "Loa ESP32"
            protected.add(em)
    except Exception:
        pass

    # 3. Các MAC đã được chủ nhà đánh dấu quen biết
    for km, kalias in _load_known_mac_aliases().items():
        if km not in labels:
            labels[km] = kalias
        protected.add(km)

    return labels, protected


def _collect_known_ip_labels() -> Dict[str, str]:
    """Thu thập thêm IP nội bộ của Quạt BLDC, Loa ESP32, Ổ cắm BroadLink & PC Bridge (phòng khi chưa lưu MAC)."""
    ip_labels: Dict[str, str] = {}

    def _add_url_host(raw_url: str, label: str) -> None:
        if not raw_url:
            return
        try:
            host = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}").hostname or ""
            if host:
                ip_labels.setdefault(host, label)
        except Exception:
            pass

    try:
        import fan_controller
        fan_snap = fan_controller.get_controller().get_status_snapshot()
        _add_url_host((fan_snap.get("settings") or {}).get("base_url", ""), "Quạt phòng BLDC")
    except Exception:
        pass

    try:
        import smart_plugs
        for p in smart_plugs.get_controller().get_status_snapshot():
            pip = str(p.get("ip") or "").strip()
            if pip:
                ip_labels.setdefault(pip, str(p.get("name") or "Ổ cắm thông minh"))
    except Exception:
        pass

    try:
        import esp32_audio
        esp = (esp32_audio.get_manager().get_status_snapshot().get("esp32") or {})
        eip = str(esp.get("ip") or "").strip()
        if eip:
            ip_labels.setdefault(eip, "Loa ESP32")
    except Exception:
        pass

    _add_url_host(lan_presence.get_effective_tts_url() or getattr(config, "TTS_API_URL", ""), "PC Bridge (TTS/Nhạc)")
    _add_url_host(lan_presence.get_effective_music_dl_url() or getattr(config, "MUSIC_DL_API_URL", ""), "PC Bridge (TTS/Nhạc)")
    return ip_labels


def build_wol_magic_packet(mac_addr: str) -> bytes:
    """Tạo gói Wake-on-LAN Magic Packet (102 byte) từ địa chỉ MAC."""
    norm = lan_presence.normalize_mac(mac_addr)
    if not norm:
        raise ValueError(f"Địa chỉ MAC không hợp lệ: {mac_addr!r}")
    raw = bytes.fromhex(norm.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_udp_wol_magic_packet(
    mac_addr: str,
    broadcast_ips: Optional[List[str]] = None,
    port: int = 9,
) -> List[str]:
    """Gửi UDP Wake-on-LAN magic packet ra mạng LAN (không cần phụ thuộc gói ngoài)."""
    packet = build_wol_magic_packet(mac_addr)
    targets: List[str] = []
    seen = set()
    for ip in (broadcast_ips or ["255.255.255.255", "192.168.1.255"]):
        s = str(ip or "").strip()
        if s and s not in seen:
            seen.add(s)
            targets.append(s)

    sent_to: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.5)
        for target_ip in targets:
            try:
                sock.sendto(packet, (target_ip, int(port)))
                sent_to.append(target_ip)
            except OSError as e:
                logger.debug(f"WoL UDP sendto {target_ip}:{port} skipped: {e}")
    finally:
        sock.close()
    return sent_to


def _derive_subnet_broadcast(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        ip_obj = ipaddress.IPv4Address(host)
        net = ipaddress.IPv4Network(f"{ip_obj}/24", strict=False)
        return str(net.broadcast_address)
    except Exception:
        return "192.168.1.255"


def _fmt_uptime(seconds: int) -> str:
    sec = max(0, int(seconds or 0))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts: List[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


class OpenWrtRouterController:
    """Bộ điều phối đọc telemetry & lệnh an toàn cho Router OpenWrt qua `/ubus`."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._auth_lock: Optional[asyncio.Lock] = None
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._sid: str = ""
        self._sid_expires_mono: float = 0.0
        self._rpc_seq: int = 0
        self._ap_ifaces: List[str] = []
        self._snapshot: Dict[str, Any] = {
            "configured": False,
            "enabled": False,
            "online": False,
            "router_url": "http://192.168.1.1",
            "model": "",
            "hostname": "",
            "firmware": "",
            "uptime_sec": 0,
            "uptime_text": "",
            "load_1m": 0.0,
            "ram_used_mb": 0.0,
            "ram_total_mb": 0.0,
            "ram_available_mb": 0.0,
            "ram_used_pct": 0.0,
            "conntrack_count": None,
            "uplinks": [],
            "wisp": None,
            "radios": [],
            "clients": [],
            "clients_count": 0,
            "unknown_count": 0,
            "last_checked_at": 0.0,
            "last_online_at": 0.0,
            "last_error": "",
        }
        self._unknown_alert_at: Dict[str, float] = {}
        self._baseline_macs_seeded: bool = False

    def _get_auth_lock(self) -> asyncio.Lock:
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        return self._auth_lock

    def _get_refresh_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    def invalidate_session(self) -> None:
        self._sid = ""
        self._sid_expires_mono = 0.0

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=6, ttl_dns_cache=60)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _next_id(self) -> int:
        self._rpc_seq += 1
        return self._rpc_seq

    async def _post_ubus(self, url: str, payload: Any, timeout_sec: float = 3.5) -> Any:
        sess = await self._get_http_session()
        endpoint = f"{url.rstrip('/')}/ubus"
        async with sess.post(
            endpoint,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_sec, connect=2.0),
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"HTTP {resp.status}: {body}")
            return await resp.json()

    async def _ensure_sid(self, cfg: Dict[str, Any], force_relogin: bool = False) -> str:
        async with self._get_auth_lock():
            now_mono = time.monotonic()
            if not force_relogin and self._sid and now_mono < self._sid_expires_mono:
                return self._sid

            login_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "call",
                "params": [
                    "00000000000000000000000000000000",
                    "session",
                    "login",
                    {"username": cfg["username"], "password": cfg["password"]},
                ],
            }
            resp = await self._post_ubus(cfg["router_url"], login_req, timeout_sec=3.5)
            if not isinstance(resp, dict) or "error" in resp:
                err = (resp or {}).get("error") if isinstance(resp, dict) else resp
                raise RuntimeError(f"ubus login error: {err}")
            res = resp.get("result")
            if not (isinstance(res, list) and len(res) >= 2 and res[0] == 0 and isinstance(res[1], dict)):
                raise RuntimeError(f"ubus login rejected (code={res[0] if isinstance(res, list) and res else res})")
            data = res[1]
            sid = str(data.get("ubus_rpc_session") or "")
            if not sid:
                raise RuntimeError("Empty ubus_rpc_session")
            expires_sec = max(45, int(data.get("expires") or data.get("timeout") or 300))
            self._sid = sid
            self._sid_expires_mono = now_mono + float(expires_sec - 20)
            return sid

    async def _ubus_batch(
        self,
        cfg: Dict[str, Any],
        calls: List[Tuple[str, str, str, Dict[str, Any]]],
        timeout_sec: float = 4.0,
    ) -> Dict[str, Tuple[int, Any]]:
        """Thực thi lô JSON-RPC `/ubus`, tự re-login 1 lần nếu session hết hạn (`-32002` / `rc=6`)."""
        if not calls:
            return {}

        for attempt in range(2):
            sid = await self._ensure_sid(cfg, force_relogin=(attempt > 0))
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": key,
                    "method": "call",
                    "params": [sid, obj, method, args],
                }
                for key, obj, method, args in calls
            ]
            raw = await self._post_ubus(cfg["router_url"], payload, timeout_sec=timeout_sec)
            if isinstance(raw, dict):
                raw = [raw]
            if not isinstance(raw, list):
                raise RuntimeError("Invalid batch ubus JSON response")

            need_reauth = False
            parsed: Dict[str, Tuple[int, Any]] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id") or "")
                if "error" in item:
                    err = item["error"] or {}
                    if err.get("code") == -32002 and attempt == 0:
                        need_reauth = True
                        break
                    parsed[rid] = (-1, err)
                    continue
                res = item.get("result")
                if isinstance(res, list) and res:
                    rc = int(res[0])
                    if rc == 6 and attempt == 0 and rid in ("sys_info", "board", "net_dump"):
                        need_reauth = True
                        break
                    val = res[1] if len(res) > 1 else {}
                    parsed[rid] = (rc, val)
                else:
                    parsed[rid] = (-1, res)

            if need_reauth:
                self.invalidate_session()
                continue
            return parsed

        return {}

    def get_status_snapshot(self) -> Dict[str, Any]:
        cfg = get_router_settings(mask_secret=True)
        snap = dict(self._snapshot)
        snap["configured"] = bool(cfg["has_password"])
        snap["enabled"] = bool(cfg["enabled"])
        snap["router_url"] = cfg["router_url"]
        snap["settings"] = {
            "enabled": cfg["enabled"],
            "raw_enabled": cfg["raw_enabled"],
            "router_url": cfg["router_url"],
            "username": cfg["username"],
            "has_password": cfg["has_password"],
            "notify_unknown_device": cfg["notify_unknown_device"],
            "boost_presence": cfg["boost_presence"],
            "known_macs": cfg["known_macs"],
        }
        return snap

    def get_associated_mac_map(self, max_age_sec: float = 110.0) -> Dict[str, Dict[str, Any]]:
        """Trả map `{mac_lower: client_dict}` nếu router bật `boost_presence` và vừa poll gần đây."""
        cfg = get_router_settings(mask_secret=True)
        if not cfg["enabled"] or not cfg["boost_presence"]:
            return {}
        if not self._snapshot.get("online"):
            return {}
        checked_at = float(self._snapshot.get("last_online_at") or 0.0)
        if (time.time() - checked_at) > max_age_sec:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for c in (self._snapshot.get("clients") or []):
            mac = lan_presence.normalize_mac(c.get("mac", ""))
            if mac and c.get("authorized", True):
                out[mac] = dict(c)
        return out

    async def refresh_state(self, force: bool = False) -> Dict[str, Any]:
        cfg = get_router_settings(mask_secret=False)
        now_ts = time.time()
        if not cfg["enabled"]:
            self._snapshot.update({
                "configured": bool(cfg["has_password"]),
                "enabled": False,
                "online": False,
                "router_url": cfg["router_url"],
                "last_checked_at": now_ts,
                "last_error": "" if cfg["has_password"] else "Chưa cấu hình mật khẩu Router",
            })
            return self.get_status_snapshot()

        if not force and self._snapshot.get("online") and (now_ts - float(self._snapshot.get("last_online_at") or 0.0)) < 3.0:
            return self.get_status_snapshot()

        async with self._get_refresh_lock():
            try:
                b1 = await self._ubus_batch(
                    cfg,
                    [
                        ("sys_info", "system", "info", {}),
                        ("board", "system", "board", {}),
                        ("net_dump", "network.interface", "dump", {}),
                        ("mwan3", "mwan3", "status", {}),
                        ("wdevs", "luci-rpc", "getWirelessDevices", {}),
                        ("leases", "luci-rpc", "getDHCPLeases", {"family": 4}),
                        ("hints", "luci-rpc", "getHostHints", {}),
                        ("conntrack", "file", "read", {"path": "/proc/sys/net/netfilter/nf_conntrack_count"}),
                    ],
                )

                # Phân loại các radio Wi-Fi & tìm danh sách AP / STA interface động
                wdevs_rc, wdevs_val = b1.get("wdevs", (-1, {}))
                ap_ifaces: List[Dict[str, Any]] = []
                sta_ifaces: List[Dict[str, Any]] = []
                if wdevs_rc == 0 and isinstance(wdevs_val, dict):
                    for radio_name, rdata in wdevs_val.items():
                        if not isinstance(rdata, dict):
                            continue
                        r_disabled = bool(rdata.get("disabled"))
                        for iface in (rdata.get("interfaces") or []):
                            if not isinstance(iface, dict):
                                continue
                            ifname = str(iface.get("ifname") or "").strip()
                            icfg = iface.get("config") or {}
                            if r_disabled or str(icfg.get("disabled") or "0") == "1" or not ifname:
                                continue
                            mode = str(icfg.get("mode") or "").strip().lower()
                            iw_meta = iface.get("iwinfo") or {}
                            freq = int(iw_meta.get("frequency") or 0)
                            band = "5GHz" if freq >= 4900 else "2.4GHz"
                            raw_net = icfg.get("network")
                            net_name = str(raw_net[0] if isinstance(raw_net, list) and raw_net else (raw_net or "")).strip()
                            entry = {
                                "radio": radio_name,
                                "ifname": ifname,
                                "mode": mode,
                                "ssid": str(icfg.get("ssid") or iw_meta.get("ssid") or ""),
                                "network": net_name,
                                "channel": iw_meta.get("channel"),
                                "frequency": freq,
                                "band": band,
                            }
                            if mode == "ap":
                                ap_ifaces.append(entry)
                            elif mode == "sta":
                                sta_ifaces.append(entry)

                self._ap_ifaces = [a["ifname"] for a in ap_ifaces]

                b2_calls: List[Tuple[str, str, str, Dict[str, Any]]] = []
                for idx, ap in enumerate(ap_ifaces):
                    b2_calls.append((f"assoc_{idx}", "iwinfo", "assoclist", {"device": ap["ifname"]}))
                    b2_calls.append((f"apinfo_{idx}", "iwinfo", "info", {"device": ap["ifname"]}))
                for idx, sta in enumerate(sta_ifaces):
                    b2_calls.append((f"stainfo_{idx}", "iwinfo", "info", {"device": sta["ifname"]}))

                b2 = await self._ubus_batch(cfg, b2_calls) if b2_calls else {}

                snapshot_data = self._assemble_snapshot(
                    cfg=cfg,
                    b1=b1,
                    b2=b2,
                    ap_ifaces=ap_ifaces,
                    sta_ifaces=sta_ifaces,
                    now_ts=time.time(),
                )
                self._snapshot.update(snapshot_data)
                lan_events.publish("router_update", self.get_status_snapshot())
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                logger.warning(f"🌐 [Router OpenWrt] Lỗi đọc trạng thái: {err_msg}")
                self._snapshot.update({
                    "configured": True,
                    "enabled": True,
                    "online": False,
                    "router_url": cfg["router_url"],
                    "last_checked_at": time.time(),
                    "last_error": err_msg[:200],
                })
        return self.get_status_snapshot()

    def _assemble_snapshot(
        self,
        *,
        cfg: Dict[str, Any],
        b1: Dict[str, Tuple[int, Any]],
        b2: Dict[str, Tuple[int, Any]],
        ap_ifaces: List[Dict[str, Any]],
        sta_ifaces: List[Dict[str, Any]],
        now_ts: float,
    ) -> Dict[str, Any]:
        sys_rc, sys_val = b1.get("sys_info", (-1, {}))
        board_rc, board_val = b1.get("board", (-1, {}))
        dump_rc, dump_val = b1.get("net_dump", (-1, {}))
        mwan_rc, mwan_val = b1.get("mwan3", (-1, {}))
        lease_rc, lease_val = b1.get("leases", (-1, {}))
        hint_rc, hint_val = b1.get("hints", (-1, {}))
        ct_rc, ct_val = b1.get("conntrack", (-1, {}))

        uptime_sec = int(sys_val.get("uptime") or 0) if sys_rc == 0 and isinstance(sys_val, dict) else 0
        loads = (sys_val.get("load") or [0]) if sys_rc == 0 and isinstance(sys_val, dict) else [0]
        load_1m = round(float(loads[0]) / 65536.0, 2) if loads else 0.0

        mem = (sys_val.get("memory") or {}) if sys_rc == 0 and isinstance(sys_val, dict) else {}
        ram_total_mb = round(float(mem.get("total") or 0) / (1024 * 1024), 1)
        ram_avail_bytes = float(mem.get("available") or mem.get("free") or 0)
        ram_avail_mb = round(ram_avail_bytes / (1024 * 1024), 1)
        ram_used_mb = round(max(0.0, ram_total_mb - ram_avail_mb), 1)
        ram_used_pct = round((ram_used_mb / ram_total_mb) * 100.0, 1) if ram_total_mb > 0 else 0.0

        model = ""
        hostname = ""
        firmware = ""
        if board_rc == 0 and isinstance(board_val, dict):
            model = str(board_val.get("model") or "")
            hostname = str(board_val.get("hostname") or "")
            rel = board_val.get("release") or {}
            firmware = str(rel.get("description") or f"{rel.get('distribution', '')} {rel.get('version', '')}").strip()

        conntrack_count: Optional[int] = None
        if ct_rc == 0 and isinstance(ct_val, dict):
            raw_ct = str(ct_val.get("data") or "").strip()
            if raw_ct.isdigit():
                conntrack_count = int(raw_ct)

        # WISP uplink STA info
        wisp_info: Optional[Dict[str, Any]] = None
        for idx, sta in enumerate(sta_ifaces):
            rc, val = b2.get(f"stainfo_{idx}", (-1, {}))
            if rc == 0 and isinstance(val, dict) and val.get("ssid"):
                wisp_info = {
                    "ifname": sta["ifname"],
                    "network": sta.get("network") or "wwan",
                    "ssid": str(val.get("ssid") or sta["ssid"]),
                    "bssid": str(val.get("bssid") or ""),
                    "channel": val.get("channel") or sta.get("channel"),
                    "signal_dbm": val.get("signal"),
                    "quality": val.get("quality"),
                    "quality_max": val.get("quality_max"),
                    "bitrate_mbps": round(float(val.get("bitrate") or 0) / 1000.0, 1),
                }
                break

        # Uplinks / Multi-WAN
        mwan_ifaces = (mwan_val.get("interfaces") or {}) if mwan_rc == 0 and isinstance(mwan_val, dict) else {}
        uplinks: List[Dict[str, Any]] = []
        if dump_rc == 0 and isinstance(dump_val, dict):
            for iface in (dump_val.get("interface") or []):
                if not isinstance(iface, dict):
                    continue
                iname = str(iface.get("interface") or "")
                if not iname or iname in ("loopback", "lan") or iname.endswith("_6"):
                    continue
                is_up = bool(iface.get("up"))
                mwan_item = mwan_ifaces.get(iname) if isinstance(mwan_ifaces, dict) else None
                if not is_up and not mwan_item and iname not in ("wwan", "wan", "wan1"):
                    continue
                ipv4s = [
                    str(a.get("address") or "")
                    for a in (iface.get("ipv4-address") or [])
                    if isinstance(a, dict) and a.get("address")
                ]
                mwan_status = str((mwan_item or {}).get("status") or ("online" if is_up else "offline"))
                track_ips = (mwan_item or {}).get("track_ip") or []
                lat_ms: Optional[int] = None
                loss_pct: Optional[int] = None
                for tip in track_ips:
                    if isinstance(tip, dict) and tip.get("status") == "up":
                        lat_ms = int(tip.get("latency") or 0)
                        loss_pct = int(tip.get("packetloss") or 0)
                        break
                u_entry: Dict[str, Any] = {
                    "name": iname,
                    "up": is_up,
                    "proto": str(iface.get("proto") or ""),
                    "device": str(iface.get("l3_device") or iface.get("device") or ""),
                    "uptime_sec": int(iface.get("uptime") or 0),
                    "ipv4": ipv4s[0] if ipv4s else "",
                    "mwan_status": mwan_status,
                    "latency_ms": lat_ms,
                    "packet_loss": loss_pct,
                }
                if wisp_info and (
                    iname == wisp_info.get("network")
                    or u_entry["device"] == wisp_info.get("ifname")
                ):
                    u_entry["wisp_ssid"] = wisp_info["ssid"]
                    u_entry["signal_dbm"] = wisp_info["signal_dbm"]
                uplinks.append(u_entry)

        # Leases + HostHints lookup tables
        lease_by_mac: Dict[str, Dict[str, Any]] = {}
        if lease_rc == 0 and isinstance(lease_val, dict):
            for l_item in (lease_val.get("dhcp_leases") or []):
                if not isinstance(l_item, dict):
                    continue
                lm = lan_presence.normalize_mac(l_item.get("macaddr", ""))
                if lm:
                    lease_by_mac[lm] = {
                        "ip": str(l_item.get("ipaddr") or ""),
                        "hostname": str(l_item.get("hostname") or ""),
                        "expires_sec": int(l_item.get("expires") or 0),
                    }

        hint_by_mac: Dict[str, Dict[str, Any]] = {}
        if hint_rc == 0 and isinstance(hint_val, dict):
            for raw_mac, h_item in hint_val.items():
                if not isinstance(h_item, dict):
                    continue
                hm = lan_presence.normalize_mac(raw_mac)
                if hm:
                    ips = [str(x) for x in (h_item.get("ipaddrs") or []) if x]
                    hint_by_mac[hm] = {
                        "ip": ips[0] if ips else "",
                        "name": str(h_item.get("name") or ""),
                    }

        known_labels, protected_macs = _collect_known_and_protected_macs()
        known_ips = _collect_known_ip_labels()
        radios_out: List[Dict[str, Any]] = []
        clients_out: List[Dict[str, Any]] = []

        for idx, ap in enumerate(ap_ifaces):
            rc_assoc, val_assoc = b2.get(f"assoc_{idx}", (-1, {}))
            rc_info, val_info = b2.get(f"apinfo_{idx}", (-1, {}))
            ch_val = (val_info.get("channel") if rc_info == 0 and isinstance(val_info, dict) else None) or ap.get("channel")
            ap_clients_raw = (val_assoc.get("results") or []) if rc_assoc == 0 and isinstance(val_assoc, dict) else []

            radios_out.append({
                "ifname": ap["ifname"],
                "ssid": ap["ssid"],
                "band": ap["band"],
                "channel": ch_val,
                "clients_count": len(ap_clients_raw),
            })

            for cl in ap_clients_raw:
                if not isinstance(cl, dict):
                    continue
                mac = lan_presence.normalize_mac(cl.get("mac", ""))
                if not mac:
                    continue
                lease = lease_by_mac.get(mac, {})
                hint = hint_by_mac.get(mac, {})
                ip = str(lease.get("ip") or hint.get("ip") or "")
                hostname = str(lease.get("hostname") or hint.get("name") or "")
                known_lbl = known_labels.get(mac) or known_ips.get(ip, "")
                if not known_lbl:
                    hn_low = hostname.lower()
                    if hn_low.startswith("esp01s-bldc-fan"):
                        known_lbl = "Quạt phòng BLDC"
                    elif hn_low.startswith("esp32c3-"):
                        known_lbl = "Loa ESP32"
                is_known = bool(known_lbl)
                is_protected = (mac in protected_macs) or (ip in known_ips) or is_known and hostname.lower().startswith(("esp01s-bldc-fan", "esp32c3-"))
                rx_obj = cl.get("rx") or {}
                tx_obj = cl.get("tx") or {}
                rx_mb = round(float(rx_obj.get("bytes") or 0) / (1024 * 1024), 1)
                tx_mb = round(float(tx_obj.get("bytes") or 0) / (1024 * 1024), 1)
                rx_mbps = round(float(rx_obj.get("rate") or 0) / 1000.0, 1)
                tx_mbps = round(float(tx_obj.get("rate") or 0) / 1000.0, 1)
                clients_out.append({
                    "mac": mac,
                    "ip": ip,
                    "hostname": hostname,
                    "label": known_lbl or hostname or ip or mac,
                    "ifname": ap["ifname"],
                    "ssid": ap["ssid"],
                    "band": ap["band"],
                    "signal_dbm": cl.get("signal"),
                    "inactive_ms": int(cl.get("inactive") or 0),
                    "connected_sec": int(cl.get("connected_time") or 0),
                    "authorized": bool(cl.get("authorized", True)),
                    "rx_rate_mbps": rx_mbps,
                    "tx_rate_mbps": tx_mbps,
                    "rx_mb": rx_mb,
                    "tx_mb": tx_mb,
                    "is_known": is_known,
                    "protected": is_protected,
                })

        # Sắp xếp: máy lạ lên đầu hoặc theo băng tần / traffic dễ quan sát
        clients_out.sort(key=lambda x: (0 if not x["is_known"] else 1, -(x.get("rx_mb", 0) + x.get("tx_mb", 0))))
        unknown_cnt = sum(1 for c in clients_out if not c["is_known"])

        return {
            "configured": True,
            "enabled": True,
            "online": True,
            "router_url": cfg["router_url"],
            "model": model or "OpenWrt Router",
            "hostname": hostname,
            "firmware": firmware,
            "uptime_sec": uptime_sec,
            "uptime_text": _fmt_uptime(uptime_sec),
            "load_1m": load_1m,
            "ram_used_mb": ram_used_mb,
            "ram_total_mb": ram_total_mb,
            "ram_available_mb": ram_avail_mb,
            "ram_used_pct": ram_used_pct,
            "conntrack_count": conntrack_count,
            "uplinks": uplinks,
            "wisp": wisp_info,
            "radios": radios_out,
            "clients": clients_out,
            "clients_count": len(clients_out),
            "unknown_count": unknown_cnt,
            "last_checked_at": now_ts,
            "last_online_at": now_ts,
            "last_error": "",
        }

    def _resolve_client_target(self, target: str) -> Optional[Dict[str, Any]]:
        """Tìm client đang nối Wi-Fi theo MAC, IP hoặc tên máy/nhãn gợi nhớ."""
        q = str(target or "").strip()
        if not q:
            return None
        q_mac = lan_presence.normalize_mac(q)
        q_low = q.lower()
        clients = self._snapshot.get("clients") or []
        for c in clients:
            if q_mac and c.get("mac") == q_mac:
                return c
            if c.get("ip") and c["ip"] == q:
                return c
        for c in clients:
            if q_low in str(c.get("label") or "").lower() or q_low in str(c.get("hostname") or "").lower():
                return c
        return None

    async def kick_client(self, target: str, ban_sec: int = 60, reason_note: str = "") -> Dict[str, Any]:
        """Ngắt kết nối một thiết bị Wi-Fi qua `hostapd.<ifname>.del_client` (có bảo vệ MAC hệ thống)."""
        cfg = get_router_settings(mask_secret=False)
        if not cfg["enabled"]:
            return {"ok": False, "message": "Router OpenWrt chưa được cấu hình hoặc đang tắt."}

        await self.refresh_state(force=False)
        norm_mac = lan_presence.normalize_mac(target)
        matched = self._resolve_client_target(target)
        if not norm_mac and matched:
            norm_mac = matched["mac"]
        if not norm_mac:
            return {"ok": False, "message": f"Không tìm thấy thiết bị '{target}' đang kết nối Wi-Fi."}

        labels, protected = _collect_known_and_protected_macs()
        known_ips = _collect_known_ip_labels()
        matched_ip = str((matched or {}).get("ip") or "").strip()
        if norm_mac in protected or matched_ip in known_ips or bool((matched or {}).get("protected")):
            lbl = labels.get(norm_mac) or known_ips.get(matched_ip) or (matched or {}).get("label") or norm_mac
            return {
                "ok": False,
                "message": f"Từ chối ngắt thiết bị được bảo vệ '{lbl}' ({norm_mac}) để tránh mất kết nối hệ thống.",
            }

        ifname = str((matched or {}).get("ifname") or "").strip()
        if not ifname:
            return {"ok": False, "message": f"Thiết bị {norm_mac} hiện không thấy online trên giao diện Wi-Fi AP nào."}

        safe_ban_sec = max(0, min(3600, int(ban_sec or 0)))
        res = await self._ubus_batch(
            cfg,
            [
                (
                    "kick",
                    f"hostapd.{ifname}",
                    "del_client",
                    {
                        "addr": norm_mac,
                        "reason": 5,
                        "deauth": True,
                        "ban_time": safe_ban_sec * 1000,
                    },
                )
            ],
        )
        rc, val = res.get("kick", (-1, {}))
        if rc != 0:
            return {"ok": False, "message": f"Router từ chối lệnh del_client (rc={rc}): {val}"}

        lbl = (matched or {}).get("label") or norm_mac
        ban_txt = f" và chặn tạm {safe_ban_sec}s" if safe_ban_sec > 0 else ""
        msg = f"Đã ngắt kết nối '{lbl}' ({norm_mac}) khỏi {ifname}{ban_txt}."
        logger.info(f"🌐 [Router Kick]: {msg} {reason_note}".strip())
        lan_events.emit_notify("Ngắt thiết bị Wi-Fi", msg, level="warn", source="router")
        await self.refresh_state(force=True)
        return {"ok": True, "mac": norm_mac, "ifname": ifname, "ban_sec": safe_ban_sec, "message": msg}

    async def wake_pc(self, target: str = "") -> Dict[str, Any]:
        """Gửi Wake-on-LAN tới PC (hoặc MAC chỉ định) qua UDP LAN + `etherwake` trên Router."""
        cfg = get_router_settings(mask_secret=False)
        lan_cfg = lan_presence.get_lan_settings()
        mac = lan_presence.normalize_mac(target) if target else ""
        if not mac and target:
            matched = self._resolve_client_target(target)
            if matched:
                mac = matched.get("mac", "")
        if not mac:
            mac = lan_presence.normalize_mac(lan_cfg.get("pc_mac", ""))
        if not mac:
            return {"ok": False, "message": "Chưa cấu hình MAC máy tính PC để gửi Wake-on-LAN."}

        bcast = _derive_subnet_broadcast(cfg["router_url"])
        udp_sent = send_udp_wol_magic_packet(mac, broadcast_ips=["255.255.255.255", bcast], port=9)

        via_router = False
        if cfg["enabled"]:
            try:
                res = await self._ubus_batch(
                    cfg,
                    [
                        (
                            "wol",
                            "file",
                            "exec",
                            {"command": "/usr/bin/etherwake", "params": ["-b", "-i", "br-lan", mac.upper()]},
                        )
                    ],
                )
                rc, _ = res.get("wol", (-1, {}))
                via_router = rc == 0
            except Exception as e:
                logger.debug(f"Router etherwake fallback failed (UDP still sent): {e}")

        via_txt = "OpenStick UDP + Router etherwake" if via_router else f"UDP broadcast ({', '.join(udp_sent)})"
        msg = f"Đã gửi gói Wake-on-LAN tới PC ({mac}) qua {via_txt}."
        logger.info(f"🖥️ [Wake-on-LAN]: {msg}")
        return {"ok": True, "mac": mac, "via_router": via_router, "udp_sent": udp_sent, "message": msg}

    def trust_mac(self, target: str, alias: str = "") -> Dict[str, Any]:
        """Đánh dấu một địa chỉ MAC là thiết bị quen biết để không cảnh báo lạ nữa."""
        norm = lan_presence.normalize_mac(target)
        matched = self._resolve_client_target(target) if not norm else None
        if not norm and matched:
            norm = matched["mac"]
        if not norm:
            return {"ok": False, "message": f"Địa chỉ MAC '{target}' không hợp lệ."}
        aliases = _load_known_mac_aliases()
        label = str(alias or "").strip()[:60] or (matched.get("hostname") if matched else "") or "Thiết bị quen"
        aliases[norm] = label
        _save_known_mac_aliases(aliases)
        for c in (self._snapshot.get("clients") or []):
            if c.get("mac") == norm:
                c["is_known"] = True
                c["protected"] = True
                c["label"] = label
        self._snapshot["unknown_count"] = sum(1 for c in (self._snapshot.get("clients") or []) if not c.get("is_known"))
        lan_events.publish("router_update", self.get_status_snapshot())
        return {"ok": True, "mac": norm, "label": label, "message": f"Đã đánh dấu quen biết: {label} ({norm})."}

    def untrust_mac(self, target: str) -> Dict[str, Any]:
        norm = lan_presence.normalize_mac(target)
        if not norm:
            return {"ok": False, "message": f"Địa chỉ MAC '{target}' không hợp lệ."}
        aliases = _load_known_mac_aliases()
        if norm not in aliases:
            return {"ok": False, "message": f"MAC {norm} không nằm trong danh sách đánh dấu quen biết."}
        removed = aliases.pop(norm)
        _save_known_mac_aliases(aliases)
        return {"ok": True, "mac": norm, "message": f"Đã bỏ đánh dấu quen biết cho {removed} ({norm})."}

    async def reboot_router_dashboard_only(self, confirmed: bool = False) -> Dict[str, Any]:
        """Khởi động lại Router CHỈ dành riêng cho nút bấm tay xác nhận trên Web Dashboard."""
        if not confirmed:
            return {"ok": False, "message": "Thiếu xác nhận reboot router."}
        cfg = get_router_settings(mask_secret=False)
        if not cfg["enabled"]:
            return {"ok": False, "message": "Router OpenWrt chưa được cấu hình."}
        try:
            res = await self._ubus_batch(cfg, [("rb", "system", "reboot", {})], timeout_sec=4.0)
            rc, _ = res.get("rb", (-1, {}))
            if rc != 0:
                return {"ok": False, "message": f"Lệnh system.reboot trả mã lỗi {rc}."}
        except Exception as e:
            return {"ok": False, "message": f"Không gửi được lệnh reboot: {e}"}

        self.invalidate_session()
        msg = "Đã gửi lệnh khởi động lại Router OpenWrt. Mạng sẽ gián đoạn ~60–90 giây."
        logger.warning(f"🔄 [Router Dashboard Reboot]: {msg}")
        lan_events.emit_notify("Đang khởi động lại Router", msg, level="warn", source="router")
        return {"ok": True, "message": msg}

    async def execute_ai_router_action(self, ai_action: Any) -> str:
        """
        Thực thi `router_action` an toàn từ Pass 1 AI (chỉ whitelist tác vụ an toàn).
        KHÔNG bao giờ cho phép reboot / tắt Wi-Fi / đổi mật khẩu từ AI.
        """
        if not isinstance(ai_action, dict):
            return ""
        act = str(ai_action.get("action") or "").strip().lower()
        if not act or act == "null":
            return ""

        if act in _FORBIDDEN_AI_ACTIONS or "wifi" in act or "password" in act or "ssid" in act or "reboot" in act:
            return (
                "[Hệ thống Router: Từ chối thao tác rủi ro (không đổi mật khẩu/tắt Wi-Fi hay reboot qua chat tự động để tránh mất mạng hệ thống).]"
            )

        if act in ("refresh", "status", "check"):
            snap = await self.refresh_state(force=True)
            if not snap.get("online"):
                return f"[Hệ thống Router: Hiện không kết nối được tới router ({snap.get('last_error') or 'offline'}).]"
            return (
                f"[Hệ thống Router: {snap.get('model') or 'OpenWrt'} online • "
                f"{snap.get('clients_count', 0)} thiết bị Wi-Fi ({snap.get('unknown_count', 0)} lạ) • "
                f"uptime {snap.get('uptime_text') or '0m'}.]"
            )

        if act in ("wake_pc", "wol", "wake"):
            res = await self.wake_pc(str(ai_action.get("target") or "").strip())
            return f"[Hệ thống Router / WoL: {res.get('message')}]"

        if act in ("kick_client", "kick", "ban_client"):
            tgt = str(ai_action.get("target") or "").strip()
            ban_sec = int(ai_action.get("ban_sec") or ai_action.get("duration_sec") or 60)
            res = await self.kick_client(tgt, ban_sec=ban_sec, reason_note="(AI Pass 1)")
            return f"[Hệ thống Router: {res.get('message')}]"

        if act in ("trust_mac", "trust"):
            tgt = str(ai_action.get("target") or "").strip()
            alias = str(ai_action.get("alias") or ai_action.get("label") or "").strip()
            res = self.trust_mac(tgt, alias=alias)
            return f"[Hệ thống Router: {res.get('message')}]"

        return ""

    async def poll_unknown_device_alerts(self, bot: Any = None, target_user_id: int = 0) -> None:
        """Kiểm tra thiết bị lạ mới gia nhập Wi-Fi và thông báo nhẹ (bỏ qua lần baseline đầu)."""
        cfg = get_router_settings(mask_secret=True)
        if not cfg["enabled"] or not self._snapshot.get("online"):
            return
        clients = self._snapshot.get("clients") or []
        now_ts = time.time()

        if not self._baseline_macs_seeded:
            for c in clients:
                if not c.get("is_known") and c.get("mac"):
                    self._unknown_alert_at[c["mac"]] = now_ts
            self._baseline_macs_seeded = True
            return

        if not cfg["notify_unknown_device"]:
            return

        new_unknowns: List[Dict[str, Any]] = []
        for c in clients:
            if c.get("is_known"):
                continue
            mac = str(c.get("mac") or "")
            if not mac:
                continue
            last_alert = float(self._unknown_alert_at.get(mac) or 0.0)
            if (now_ts - last_alert) >= _UNKNOWN_ALERT_COOLDOWN_SEC:
                self._unknown_alert_at[mac] = now_ts
                new_unknowns.append(c)

        if not new_unknowns:
            return

        descs = [
            f"{u.get('hostname') or u.get('ip') or 'Thiết bị mới'} ({u['mac']}, {u.get('band')}, {u.get('signal_dbm')} dBm)"
            for u in new_unknowns[:3]
        ]
        body = "Phát hiện thiết bị lạ vừa vào Wi-Fi: " + "; ".join(descs)
        lan_events.emit_notify("Thiết bị lạ vào Wi-Fi", body, level="warn", source="router")

        if bot and target_user_id:
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=(
                        "🛡️ Em vừa thấy có máy lạ mới vào Wi-Fi nhà mình nè anh:\n- "
                        + "\n- ".join(descs)
                        + "\nNếu là máy quen anh bảo em 'đánh dấu quen biết', còn lạ thì bảo em 'ngắt khỏi Wi-Fi' hoặc xem trên Dashboard nhé!"
                    ),
                )
            except Exception as e:
                logger.warning(f"Không gửi được cảnh báo MAC lạ Telegram: {e}")


_ROUTER_CONTROLLER: Optional[OpenWrtRouterController] = None


def get_controller() -> OpenWrtRouterController:
    global _ROUTER_CONTROLLER
    if _ROUTER_CONTROLLER is None:
        _ROUTER_CONTROLLER = OpenWrtRouterController()
    return _ROUTER_CONTROLLER


async def router_monitor_loop(
    bot: Any = None,
    target_user_id: int = 0,
    interval_sec: float = 60.0,
) -> None:
    """Vòng nền theo dõi Router OpenWrt mỗi 60s (nhẹ RAM, dùng lại ClientSession)."""
    ctrl = get_controller()
    await asyncio.sleep(4.0)
    logger.info("🌐 Khởi động OpenWrt Router Monitor (ubus JSON-RPC)...")
    while True:
        try:
            await ctrl.refresh_state(force=True)
            await ctrl.poll_unknown_device_alerts(bot=bot, target_user_id=target_user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"router_monitor_loop error: {e}")
        await asyncio.sleep(max(15.0, float(interval_sec)))
