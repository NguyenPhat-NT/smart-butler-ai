# -*- coding: utf-8 -*-
"""
Điều khiển & tự động hóa Ổ Cắm Thông Minh BroadLink SPmini (LAN UDP Port 80).

Hỗ trợ giao thức cục bộ BroadLink SP2/SPmini (`0x2728`, `0x2711`, `0x2720`, `0x753e`, ...):
  - Zero pip dependency: ưu tiên OpenSSL `libcrypto` qua `ctypes` (đặt `argtypes` chuẩn 64-bit)
    và có sẵn AES-128-CBC thuần Python làm fallback trên Windows / môi trường tối giản.
  - Quản lý danh sách nhiều ổ cắm lưu trong `app_metadata` (`smart_plugs_v1`), tự bám IP mới
    qua MAC (`ip neigh` / UDP discovery) mà không mất tên hay lịch tự động hóa.
  - Phối hợp nhịp đi vắng / về phòng và giờ đi ngủ cùng `fan_controller.py`, cộng thêm hẹn giờ riêng
    cho từng ổ và điều khiển qua Dashboard / Smart Butler AI (`plug_action`).
"""

import re
import os
import json
import time
import tempfile
import socket
import struct
import random
import asyncio
import datetime
import logging
import ctypes
import ctypes.util
from typing import Any, Dict, List, Optional, Tuple

import database
import lan_events
import lan_presence

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))
_PLUGS_META_KEY = "smart_plugs_v1"

BROADLINK_UDP_PORT = 80
BROADLINK_TIMEOUT_SEC = 1.6
BROADLINK_INIT_KEY = bytes.fromhex("097628343fe99e23765c1513accf8b02")
BROADLINK_INIT_IV = bytes.fromhex("562e17996d093d28ddb3ba695a2e6f58")

# Nhóm devtype BroadLink SP2 / SPmini / SP3 dùng cùng lệnh 0x6a (payload[0]=1 đọc, payload[0]=2 ghi)
SP2_DEVTYPES = {
    0x2711, 0x2719, 0x271A, 0x2720, 0x2728, 0x2733, 0x2736, 0x273E,
    0x7530, 0x7539, 0x753E, 0x7540, 0x7544, 0x7546, 0x7547, 0x7918,
    0x7919, 0x791A, 0x7D00, 0x9479, 0x947A,
}
SP1_DEVTYPES = {0x0000}

# Seed ổ cắm mặc định để trống cho bản public; người dùng bấm Quét LAN trên Dashboard để tự thêm ổ cắm BroadLink của mình.
DEFAULT_SEED_PLUGS: List[Dict[str, Any]] = []

# ==================== AES-128-CBC ZERO-DEP ENGINE ====================

_LIBCRYPTO_HANDLE = None
_LIBCRYPTO_CHECKED = False


def _init_libcrypto():
    global _LIBCRYPTO_HANDLE, _LIBCRYPTO_CHECKED
    if _LIBCRYPTO_CHECKED:
        return _LIBCRYPTO_HANDLE
    _LIBCRYPTO_CHECKED = True
    try:
        lib_name = ctypes.util.find_library("crypto") or "libcrypto.so.1.1"
        lib = ctypes.CDLL(lib_name)
        c_vp = ctypes.c_void_p
        c_ip = ctypes.POINTER(ctypes.c_int)
        if not (hasattr(lib, "EVP_CIPHER_CTX_new") and hasattr(lib, "EVP_aes_128_cbc")):
            return None
        lib.EVP_CIPHER_CTX_new.restype = c_vp
        lib.EVP_CIPHER_CTX_free.argtypes = [c_vp]
        lib.EVP_aes_128_cbc.restype = c_vp
        lib.EVP_EncryptInit_ex.argtypes = [c_vp, c_vp, c_vp, ctypes.c_char_p, ctypes.c_char_p]
        lib.EVP_DecryptInit_ex.argtypes = [c_vp, c_vp, c_vp, ctypes.c_char_p, ctypes.c_char_p]
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [c_vp, ctypes.c_int]
        lib.EVP_EncryptUpdate.argtypes = [c_vp, ctypes.c_char_p, c_ip, ctypes.c_char_p, ctypes.c_int]
        lib.EVP_DecryptUpdate.argtypes = [c_vp, ctypes.c_char_p, c_ip, ctypes.c_char_p, ctypes.c_int]
        lib.EVP_EncryptFinal_ex.argtypes = [c_vp, c_vp, c_ip]
        lib.EVP_DecryptFinal_ex.argtypes = [c_vp, c_vp, c_ip]
        _LIBCRYPTO_HANDLE = lib
    except Exception:
        _LIBCRYPTO_HANDLE = None
    return _LIBCRYPTO_HANDLE


# Pure-Python AES-128-CBC fallback (dùng khi không có OpenSSL libcrypto, vd test Windows)
_SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)
_INV_SBOX = tuple(_SBOX.index(i) for i in range(256))
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else ((a << 1) & 0xFF)


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key_128(key: bytes) -> List[List[int]]:
    rk = [[key[4 * i], key[4 * i + 1], key[4 * i + 2], key[4 * i + 3]] for i in range(4)]
    for i in range(4, 44):
        t = list(rk[i - 1])
        if i % 4 == 0:
            t = [_SBOX[t[1]] ^ _RCON[(i // 4) - 1], _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
        rk.append([rk[i - 4][j] ^ t[j] for j in range(4)])
    return rk


def _aes_encrypt_block(block: bytes, rk: List[List[int]]) -> bytes:
    st = list(block)
    for c in range(4):
        for r in range(4):
            st[4 * c + r] ^= rk[c][r]
    for rnd in range(1, 11):
        st = [_SBOX[b] for b in st]
        # ShiftRows
        st[1], st[5], st[9], st[13] = st[5], st[9], st[13], st[1]
        st[2], st[6], st[10], st[14] = st[10], st[14], st[2], st[6]
        st[3], st[7], st[11], st[15] = st[15], st[3], st[7], st[11]
        if rnd < 10:
            for c in range(4):
                i = 4 * c
                a0, a1, a2, a3 = st[i], st[i + 1], st[i + 2], st[i + 3]
                t = a0 ^ a1 ^ a2 ^ a3
                st[i] ^= t ^ _xtime(a0 ^ a1)
                st[i + 1] ^= t ^ _xtime(a1 ^ a2)
                st[i + 2] ^= t ^ _xtime(a2 ^ a3)
                st[i + 3] ^= t ^ _xtime(a3 ^ a0)
        for c in range(4):
            for r in range(4):
                st[4 * c + r] ^= rk[4 * rnd + c][r]
    return bytes(st)


def _aes_decrypt_block(block: bytes, rk: List[List[int]]) -> bytes:
    st = list(block)
    for c in range(4):
        for r in range(4):
            st[4 * c + r] ^= rk[40 + c][r]
    for rnd in range(9, -1, -1):
        # InvShiftRows
        st[1], st[5], st[9], st[13] = st[13], st[1], st[5], st[9]
        st[2], st[6], st[10], st[14] = st[10], st[14], st[2], st[6]
        st[3], st[7], st[11], st[15] = st[7], st[11], st[15], st[3]
        st = [_INV_SBOX[b] for b in st]
        for c in range(4):
            for r in range(4):
                st[4 * c + r] ^= rk[4 * rnd + c][r]
        if rnd > 0:
            for c in range(4):
                i = 4 * c
                a0, a1, a2, a3 = st[i], st[i + 1], st[i + 2], st[i + 3]
                st[i] = _mul(a0, 0x0E) ^ _mul(a1, 0x0B) ^ _mul(a2, 0x0D) ^ _mul(a3, 0x09)
                st[i + 1] = _mul(a0, 0x09) ^ _mul(a1, 0x0E) ^ _mul(a2, 0x0B) ^ _mul(a3, 0x0D)
                st[i + 2] = _mul(a0, 0x0D) ^ _mul(a1, 0x09) ^ _mul(a2, 0x0E) ^ _mul(a3, 0x0B)
                st[i + 3] = _mul(a0, 0x0B) ^ _mul(a1, 0x0D) ^ _mul(a2, 0x09) ^ _mul(a3, 0x0E)
    return bytes(st)


def aes_128_cbc_crypt(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    """Mã hóa/giải mã AES-128-CBC (zero padding; OpenSSL `libcrypto` nếu có, hoặc thuần Python)."""
    if len(data) % 16:
        data = data + b"\x00" * (16 - len(data) % 16)
    lib = _init_libcrypto()
    if lib is not None:
        ctx = lib.EVP_CIPHER_CTX_new()
        try:
            cipher = lib.EVP_aes_128_cbc()
            out = ctypes.create_string_buffer(len(data) + 16)
            ol = ctypes.c_int(0)
            fl = ctypes.c_int(0)
            if encrypt:
                lib.EVP_EncryptInit_ex(ctx, cipher, None, key, iv)
                lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
                lib.EVP_EncryptUpdate(ctx, out, ctypes.byref(ol), data, len(data))
                lib.EVP_EncryptFinal_ex(ctx, ctypes.addressof(out) + ol.value, ctypes.byref(fl))
            else:
                lib.EVP_DecryptInit_ex(ctx, cipher, None, key, iv)
                lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
                lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(ol), data, len(data))
                lib.EVP_DecryptFinal_ex(ctx, ctypes.addressof(out) + ol.value, ctypes.byref(fl))
            return out.raw[: ol.value + fl.value]
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    rk = _expand_key_128(key[:16])
    prev = iv[:16]
    out_chunks: List[bytes] = []
    for pos in range(0, len(data), 16):
        blk = data[pos : pos + 16]
        if encrypt:
            xored = bytes(blk[i] ^ prev[i] for i in range(16))
            enc = _aes_encrypt_block(xored, rk)
            out_chunks.append(enc)
            prev = enc
        else:
            dec = _aes_decrypt_block(blk, rk)
            out_chunks.append(bytes(dec[i] ^ prev[i] for i in range(16)))
            prev = blk
    return b"".join(out_chunks)


# ==================== BROADLINK UDP LAN PROTOCOL ====================

def build_discovery_packet(local_ip: str, local_port: int, tz_offset_hours: int = 7) -> bytes:
    """Tạo gói UDP BroadLink discovery `0x06`."""
    try:
        ip_bytes = bytes(int(x) for x in local_ip.split("."))
        if len(ip_bytes) != 4:
            ip_bytes = b"\x00\x00\x00\x00"
    except Exception:
        ip_bytes = b"\x00\x00\x00\x00"
    now = datetime.datetime.now(VN_TZ)
    pkt = bytearray(0x30)
    pkt[0x08:0x0C] = struct.pack("<i", tz_offset_hours * 3600)
    pkt[0x0C:0x0E] = struct.pack("<H", now.year)
    pkt[0x0E] = now.minute
    pkt[0x0F] = now.hour
    pkt[0x10] = now.year - 2000
    pkt[0x11] = now.isoweekday()
    pkt[0x12] = now.day
    pkt[0x13] = now.month
    pkt[0x18:0x1C] = ip_bytes[::-1]
    pkt[0x1C:0x1E] = struct.pack("<H", local_port)
    pkt[0x26] = 6
    cs = (0xBEAF + sum(pkt)) & 0xFFFF
    pkt[0x20:0x22] = struct.pack("<H", cs)
    return bytes(pkt)


def parse_discovery_response(data: bytes, remote_ip: str) -> Optional[Dict[str, Any]]:
    if not data or len(data) < 0x40:
        return None
    devtype = data[0x34] | (data[0x35] << 8)
    if devtype not in SP2_DEVTYPES and devtype not in SP1_DEVTYPES:
        return None
    mac = lan_presence.normalize_mac(":".join(f"{b:02x}" for b in reversed(data[0x3A:0x40])))
    if not mac:
        return None
    locked = bool(data[0x7F]) if len(data) > 0x7F else False
    raw_name = data[0x40:0x7E].split(b"\x00", 1)[0]
    hw_name = raw_name.decode("utf-8", errors="ignore").strip()
    return {
        "ip": remote_ip,
        "mac": mac,
        "devtype": devtype,
        "locked": locked,
        "hw_name": hw_name,
    }


def build_command_packet(
    mac_str: str,
    devtype: int,
    cmd: int,
    payload: bytes,
    *,
    key: bytes = BROADLINK_INIT_KEY,
    dev_id: bytes = b"\x00\x00\x00\x00",
    count: Optional[int] = None,
) -> bytes:
    mac_rev = bytes(int(x, 16) for x in mac_str.split(":"))[::-1]
    cnt = count if count is not None else random.randint(1, 0xFFFF)
    pkt = bytearray(0x38)
    pkt[0x00:0x08] = bytes.fromhex("5aa5aa555aa5aa55")
    pkt[0x24:0x26] = struct.pack("<H", devtype)
    pkt[0x26:0x28] = struct.pack("<H", cmd)
    pkt[0x28:0x2A] = struct.pack("<H", cnt)
    pkt[0x2A:0x30] = mac_rev
    pkt[0x30:0x34] = dev_id[:4]
    if len(payload) % 16:
        payload = payload + b"\x00" * (16 - len(payload) % 16)
    pkt[0x34:0x36] = struct.pack("<H", (0xBEAF + sum(payload)) & 0xFFFF)
    enc = aes_128_cbc_crypt(key, BROADLINK_INIT_IV, payload, True)
    full = bytearray(pkt) + enc
    full[0x20:0x22] = struct.pack("<H", (0xBEAF + sum(full)) & 0xFFFF)
    return bytes(full)


def decode_command_response(resp: bytes, key: bytes) -> Tuple[int, bytes]:
    if not resp or len(resp) < 0x38:
        raise ValueError("Gói phản hồi BroadLink quá ngắn.")
    err = struct.unpack("<h", resp[0x22:0x24])[0]
    enc = resp[0x38:]
    dec = aes_128_cbc_crypt(key, BROADLINK_INIT_IV, enc, False) if enc else b""
    return err, dec


def _build_auth_payload() -> bytes:
    ap = bytearray(0x50)
    ap[0x04:0x14] = b"\x31" * 16
    ap[0x1E] = 0x01
    ap[0x2D] = 0x01
    ap[0x30:0x37] = b"OpenStk"
    return bytes(ap)


def _udp_exchange_sync(ip: str, pkt: bytes, timeout_sec: float = BROADLINK_TIMEOUT_SEC) -> bytes:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_sec)
        sock.sendto(pkt, (ip, BROADLINK_UDP_PORT))
        data, _ = sock.recvfrom(2048)
        return data
    finally:
        sock.close()


def _udp_discover_sync(
    targets: List[str],
    local_ip: str = "",
    timeout_sec: float = 1.8,
) -> List[Dict[str, Any]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: Dict[str, Dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout_sec)
        bind_ip = local_ip or "0.0.0.0"
        try:
            sock.bind((bind_ip, 0))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        sock_ip, sock_port = sock.getsockname()[:2]
        pkt = build_discovery_packet(local_ip or sock_ip or "0.0.0.0", sock_port)
        sent_to = set()
        for t in targets:
            if not t or t in sent_to:
                continue
            sent_to.add(t)
            try:
                sock.sendto(pkt, (t, BROADLINK_UDP_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + timeout_sec
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            sock.settimeout(rem)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            parsed = parse_discovery_response(data, addr[0])
            if parsed:
                found[parsed["mac"]] = parsed
    finally:
        sock.close()
    return list(found.values())


# ==================== QUẢN LÝ & LƯU CẤU HÌNH Ổ CẮM ====================

def _parse_bool(val: Any, default: bool) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def _normalize_plug_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mac = lan_presence.normalize_mac(raw.get("mac", ""))
    if not mac:
        return None
    name = str(raw.get("name") or "").strip() or f"Ổ cắm {mac[-5:]}"
    icon = str(raw.get("icon") or "🔌").strip() or "🔌"
    aliases_raw = raw.get("aliases")
    if isinstance(aliases_raw, list):
        aliases = [str(a).strip().lower() for a in aliases_raw if str(a).strip()]
    elif isinstance(aliases_raw, str):
        aliases = [a.strip().lower() for a in aliases_raw.split(",") if a.strip()]
    else:
        aliases = [name.lower()]
    if name.lower() not in aliases:
        aliases.insert(0, name.lower())

    try:
        devtype = int(raw.get("devtype") or 0x2728)
    except (TypeError, ValueError):
        devtype = 0x2728

    return {
        "mac": mac,
        "ip": str(raw.get("ip") or "").strip(),
        "devtype": devtype,
        "name": name,
        "icon": icon,
        "aliases": aliases,
        "auto_away": _parse_bool(raw.get("auto_away"), False),
        "auto_home": _parse_bool(raw.get("auto_home"), False),
        "home_only_evening": _parse_bool(raw.get("home_only_evening"), False),
        "auto_sleep": _parse_bool(raw.get("auto_sleep"), False),
    }


def _should_seed_defaults() -> bool:
    try:
        import config
        dbp = str(getattr(config, "DB_PATH", "") or "")
        low = dbp.lower()
        tmp_root = tempfile.gettempdir().lower()
        if "test_" in os.path.basename(low) or (tmp_root and tmp_root in low):
            return False
    except Exception:
        pass
    return True


def load_plugs_config() -> List[Dict[str, Any]]:
    try:
        raw_txt = database.get_metadata(_PLUGS_META_KEY)
        if raw_txt:
            items = json.loads(raw_txt)
            if isinstance(items, list):
                out: List[Dict[str, Any]] = []
                for it in items:
                    if isinstance(it, dict):
                        norm = _normalize_plug_record(it)
                        if norm:
                            out.append(norm)
                return out
    except Exception as e:
        logger.debug(f"Lỗi đọc smart_plugs_v1: {e}")

    if not _should_seed_defaults():
        return []
    seeded = [_normalize_plug_record(d) for d in DEFAULT_SEED_PLUGS]
    return [x for x in seeded if x is not None]


def save_plugs_config(plugs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for p in plugs:
        norm = _normalize_plug_record(p)
        if norm and norm["mac"] not in seen:
            seen.add(norm["mac"])
            cleaned.append(norm)
    try:
        database.set_metadata(_PLUGS_META_KEY, json.dumps(cleaned, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Lỗi lưu smart_plugs_v1: {e}")
    return cleaned


def _is_evening_hours(now_ts: Optional[float] = None) -> bool:
    ts = float(now_ts if now_ts is not None else time.time())
    vn = datetime.datetime.fromtimestamp(ts, tz=VN_TZ)
    # 17:30 tối đến 05:30 sáng
    mins = vn.hour * 60 + vn.minute
    return mins >= (17 * 60 + 30) or mins <= (5 * 60 + 30)


# ==================== NHẬN DIỆN Ý ĐỊNH CHAT TỰ NHIÊN ====================

def detect_natural_plug_command(text: str, plugs: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """
    Phát hiện lệnh điều khiển ổ cắm BroadLink (Đèn phòng / Quạt thông gió / ...) trong chat Telegram.
    Ưu tiên khớp cụm tên dài hơn trước (vd `quạt thông gió` trước `đèn` / `quạt`).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()

    plug_list = plugs if plugs is not None else load_plugs_config()
    if not plug_list:
        return None

    # Tìm tất cả ổ được nhắc tới trong câu (ưu tiên alias dài nhất trước)
    matched_macs: List[str] = []
    alias_pairs: List[Tuple[str, str]] = []
    for p in plug_list:
        for al in p.get("aliases") or [p["name"].lower()]:
            if al:
                alias_pairs.append((al, p["mac"]))
    alias_pairs.sort(key=lambda x: len(x[0]), reverse=True)

    masked = low
    for al, mac in alias_pairs:
        if re.search(rf"(?<!\w){re.escape(al)}(?!\w)", masked):
            if mac not in matched_macs:
                matched_macs.append(mac)
            masked = re.sub(rf"(?<!\w){re.escape(al)}(?!\w)", " ", masked)

    if not matched_macs:
        return None

    # Kiểm tra trạng thái ổ
    if re.search(r"(?:đèn|thông\s*gió|ổ\s*cắm).*(?:bật|tắt|sáng|sao|trạng\s*thái).*\?\s*$", low):
        if not re.search(r"\b(?:giùm|nha|nhé|đi|hộ|ngay|nào)\b", low):
            return {"action": "status", "targets": matched_macs}

    # Hẹn giờ: "30 phút nữa tắt đèn phòng", "tắt quạt thông gió sau 15 phút"
    delay_mins = 0
    m_delay = re.search(r"(\d{1,3})\s*(?:phút|p|min|tiếng|giờ|h)\b", low)
    if m_delay:
        val = int(m_delay.group(1))
        delay_mins = val * 60 if any(u in m_delay.group(0) for u in ("tiếng", "giờ", "h")) else val
        delay_mins = max(1, min(720, delay_mins))

    wants_off = bool(re.search(r"\b(tắt|ngắt|dừng|tắt\s*bớt)\b", low))
    wants_on = bool(re.search(r"\b(bật|mở|sáng)\b", low))

    if delay_mins > 0 and (wants_off or wants_on):
        return {
            "action": "schedule_off" if wants_off else "schedule_on",
            "targets": matched_macs,
            "delay_minutes": delay_mins,
        }

    if wants_off:
        return {"action": "power_off", "targets": matched_macs}
    if wants_on:
        return {"action": "power_on", "targets": matched_macs}

    return None


# ==================== CONTROLLER CHÍNH ====================

class SmartPlugController:
    """Quản lý xác thực, đọc/bật/tắt UDP LAN, hẹn giờ & tự động hóa các ổ BroadLink SPmini."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}  # mac -> {dev_id, key, ip}
        self._live: Dict[str, Dict[str, Any]] = {}      # mac -> {online, is_on, locked, last_error, checked_at, ip}
        self._timers: Dict[str, Dict[str, Any]] = {}    # mac -> {action: "on"|"off", due_at: float}
        self._last_cmd_at: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, mac: str) -> asyncio.Lock:
        if mac not in self._locks:
            self._locks[mac] = asyncio.Lock()
        return self._locks[mac]

    async def _resolve_ip(self, plug: Dict[str, Any]) -> str:
        mac = plug["mac"]
        try:
            out = await lan_presence._run_cmd("ip", "neigh", "show", timeout=1.5)
            for ent in lan_presence.parse_ip_neigh(out):
                if ent.get("mac") == mac and ent.get("ip"):
                    ip_addr = str(ent["ip"])
                    if ip_addr != plug.get("ip"):
                        self._update_plug_ip(mac, ip_addr)
                    return ip_addr
        except Exception:
            pass
        return str(plug.get("ip") or "").strip()

    def _update_plug_ip(self, mac: str, new_ip: str) -> None:
        plugs = load_plugs_config()
        changed = False
        for p in plugs:
            if p["mac"] == mac and new_ip and p.get("ip") != new_ip:
                p["ip"] = new_ip
                changed = True
        if changed:
            save_plugs_config(plugs)

    def _find_plugs(self, target: Any) -> List[Dict[str, Any]]:
        plugs = load_plugs_config()
        if target is None or target == "" or str(target).strip().lower() == "all":
            return plugs
        if isinstance(target, list):
            targets = [str(x).strip().lower() for x in target if str(x).strip()]
        else:
            targets = [str(target).strip().lower()]

        matched: List[Dict[str, Any]] = []
        seen = set()
        for tok in targets:
            for p in plugs:
                if p["mac"] in seen:
                    continue
                if (
                    p["mac"] == tok
                    or p.get("ip") == tok
                    or p["name"].lower() == tok
                    or tok in (p.get("aliases") or [])
                    or tok in p["name"].lower()
                ):
                    seen.add(p["mac"])
                    matched.append(p)
        return matched

    async def _ensure_auth(self, plug: Dict[str, Any], ip: str, force: bool = False) -> Optional[Dict[str, Any]]:
        mac = plug["mac"]
        cached = self._sessions.get(mac)
        if not force and cached and cached.get("ip") == ip:
            return cached

        devtype = int(plug.get("devtype") or 0x2728)
        pkt = build_command_packet(mac, devtype, 0x65, _build_auth_payload())
        try:
            resp = await asyncio.to_thread(_udp_exchange_sync, ip, pkt, BROADLINK_TIMEOUT_SEC)
            err, dec = decode_command_response(resp, BROADLINK_INIT_KEY)
            if err != 0 or len(dec) < 20:
                self._live[mac] = {
                    **self._live.get(mac, {}),
                    "online": False,
                    "locked": (err == -7),
                    "last_error": "Thiết bị đang bật Lock Device trên app BroadLink." if err == -7 else f"Auth lỗi (err={err})",
                    "checked_at": time.time(),
                    "ip": ip,
                }
                return None
            sess = {"dev_id": dec[0:4], "key": dec[4:20], "ip": ip}
            self._sessions[mac] = sess
            return sess
        except Exception as e:
            self._live[mac] = {
                **self._live.get(mac, {}),
                "online": False,
                "last_error": f"Không phản hồi ({type(e).__name__})",
                "checked_at": time.time(),
                "ip": ip,
            }
            return None

    async def _exchange_with_retry(self, plug: Dict[str, Any], payload: bytes, cmd: Optional[int] = None) -> Optional[bytes]:
        mac = plug["mac"]
        devtype = int(plug.get("devtype") or 0x2728)
        command = cmd if cmd is not None else (0x66 if devtype in SP1_DEVTYPES else 0x6A)
        ip = await self._resolve_ip(plug)
        if not ip:
            self._live[mac] = {
                **self._live.get(mac, {}),
                "online": False,
                "last_error": "Chưa biết IP ổ cắm.",
                "checked_at": time.time(),
            }
            return None

        for attempt in range(2):
            sess = await self._ensure_auth(plug, ip, force=(attempt > 0))
            if not sess:
                return None
            pkt = build_command_packet(
                mac, devtype, command, payload,
                key=sess["key"], dev_id=sess["dev_id"],
            )
            try:
                resp = await asyncio.to_thread(_udp_exchange_sync, ip, pkt, BROADLINK_TIMEOUT_SEC)
                err, dec = decode_command_response(resp, sess["key"])
                if err == 0:
                    return dec
                self._sessions.pop(mac, None)
            except Exception as e:
                self._sessions.pop(mac, None)
                if attempt == 1:
                    self._live[mac] = {
                        **self._live.get(mac, {}),
                        "online": False,
                        "last_error": f"UDP lỗi ({type(e).__name__})",
                        "checked_at": time.time(),
                        "ip": ip,
                    }
        return None

    async def refresh_plug(self, plug: Dict[str, Any]) -> Optional[bool]:
        mac = plug["mac"]
        devtype = int(plug.get("devtype") or 0x2728)
        if devtype in SP1_DEVTYPES:
            return None
        async with self._get_lock(mac):
            cp = bytearray(16)
            cp[0] = 1
            dec = await self._exchange_with_retry(plug, bytes(cp))
            if dec is None or len(dec) < 5:
                return None
            is_on = bool(dec[4] & 1)
            prev_live = self._live.get(mac) or {}
            prev_on = prev_live.get("is_on")
            prev_checked = float(prev_live.get("checked_at") or 0.0)
            now_v = time.time()
            ip = str(self._sessions.get(mac, {}).get("ip") or plug.get("ip") or "")
            self._live[mac] = {
                "online": True,
                "is_on": is_on,
                "locked": False,
                "last_error": "",
                "checked_at": now_v,
                "ip": ip,
            }
            if (
                prev_checked > 0.0
                and prev_on is not None
                and bool(prev_on) != is_on
                and (now_v - float(self._last_cmd_at.get(mac) or 0.0)) > 8.0
            ):
                import habit_engine
                habit_engine.notify_device_event(
                    "plug",
                    plug["name"],
                    "power_on" if is_on else "power_off",
                    source="user_physical",
                )
            return is_on

    async def refresh_all(self) -> List[Dict[str, Any]]:
        plugs = load_plugs_config()
        if plugs:
            await asyncio.gather(*(self.refresh_plug(p) for p in plugs), return_exceptions=True)
        return self.get_status_snapshot()

    async def set_power(self, target_id: str, on: bool, *, reason: str = "", event_source: str = "") -> Optional[bool]:
        plugs = self._find_plugs(target_id)
        if not plugs:
            return None
        plug = plugs[0]
        mac = plug["mac"]
        devtype = int(plug.get("devtype") or 0x2728)
        async with self._get_lock(mac):
            if devtype in SP1_DEVTYPES:
                sp = bytearray(16)
                sp[0] = 1 if on else 0
                dec = await self._exchange_with_retry(plug, bytes(sp), cmd=0x66)
                if dec is None:
                    return None
                new_state = bool(on)
            else:
                sp = bytearray(16)
                sp[0] = 2
                sp[4] = 1 if on else 0
                dec = await self._exchange_with_retry(plug, bytes(sp), cmd=0x6A)
                if dec is None:
                    return None
                cp = bytearray(16)
                cp[0] = 1
                chk = await self._exchange_with_retry(plug, bytes(cp), cmd=0x6A)
                new_state = bool(chk[4] & 1) if (chk and len(chk) >= 5) else bool(on)

            now_v = time.time()
            self._last_cmd_at[mac] = now_v
            ip = str(self._sessions.get(mac, {}).get("ip") or plug.get("ip") or "")
            self._live[mac] = {
                "online": True,
                "is_on": new_state,
                "locked": False,
                "last_error": "",
                "checked_at": now_v,
                "ip": ip,
            }
            src = event_source
            if not src:
                rl = reason.lower()
                if "dashboard" in rl:
                    src = "user_dashboard"
                elif "ai" in rl or "chat" in rl:
                    src = "user_telegram"
                elif any(k in rl for k in ("rời wi-fi", "về phòng", "giờ ngủ", "hẹn giờ")):
                    src = "system_auto"
                else:
                    src = "user_action"
            import habit_engine
            habit_engine.notify_device_event(
                "plug",
                plug["name"],
                "power_on" if new_state else "power_off",
                source=src,
            )
            if reason:
                logger.info(f"🔌 [BroadLink {plug['name']}] -> {'ON' if new_state else 'OFF'} ({reason})")
            self.publish_update()
            return new_state

    def schedule_timer(self, target_id: str, on: bool, delay_minutes: int) -> Optional[Dict[str, Any]]:
        plugs = self._find_plugs(target_id)
        if not plugs:
            return None
        plug = plugs[0]
        mins = max(1, min(720, int(delay_minutes)))
        due_at = time.time() + mins * 60.0
        self._timers[plug["mac"]] = {"action": "on" if on else "off", "due_at": due_at}
        self.publish_update()
        return {"mac": plug["mac"], "name": plug["name"], "action": "on" if on else "off", "due_at": due_at}

    def cancel_timer(self, target_id: str) -> bool:
        plugs = self._find_plugs(target_id)
        if not plugs:
            return False
        removed = self._timers.pop(plugs[0]["mac"], None) is not None
        if removed:
            self.publish_update()
        return removed

    async def discover_on_lan(self) -> Dict[str, Any]:
        """Quét UDP BroadLink trên toàn mạng LAN, tự cập nhật IP và giữ nguyên tên/cấu hình cũ."""
        local_info = await lan_presence.get_local_network_info()
        local_ip = str(local_info.get("local_ip") or "")
        plugs = load_plugs_config()
        targets: List[str] = []
        if local_ip and "." in local_ip:
            parts = local_ip.split(".")
            targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
        targets.append("255.255.255.255")
        for p in plugs:
            if p.get("ip"):
                targets.append(p["ip"])

        discovered = await asyncio.to_thread(_udp_discover_sync, targets, local_ip, 2.0)
        by_mac = {p["mac"]: p for p in plugs}

        added = 0
        for d in discovered:
            mac = d["mac"]
            if mac in by_mac:
                by_mac[mac]["ip"] = d["ip"]
                by_mac[mac]["devtype"] = d["devtype"]
            else:
                added += 1
                by_mac[mac] = _normalize_plug_record({
                    "mac": mac,
                    "ip": d["ip"],
                    "devtype": d["devtype"],
                    "name": d.get("hw_name") or f"SPmini {mac[-5:]}",
                    "icon": "🔌",
                })
            self._sessions.pop(mac, None)
            self._live[mac] = {
                **self._live.get(mac, {}),
                "online": True,
                "locked": d["locked"],
                "ip": d["ip"],
                "last_error": "Cần tắt Lock Device trên app BroadLink." if d["locked"] else "",
                "checked_at": time.time(),
            }

        saved = save_plugs_config(list(by_mac.values()))
        # Đọc trạng thái ON/OFF thật của các ổ đã mở khóa
        for p in saved:
            live_st = self._live.get(p["mac"], {})
            if not live_st.get("locked"):
                await self.refresh_plug(p)
        self.publish_update()
        return {
            "found_count": len(discovered),
            "added_count": added,
            "plugs": self.get_status_snapshot(),
        }

    def get_status_snapshot(self) -> List[Dict[str, Any]]:
        now_ts = time.time()
        plugs = load_plugs_config()
        out: List[Dict[str, Any]] = []
        for p in plugs:
            mac = p["mac"]
            live = self._live.get(mac, {})
            tm = self._timers.get(mac)
            timer_info = None
            if tm and float(tm.get("due_at") or 0.0) > now_ts:
                timer_info = {
                    "action": tm["action"],
                    "due_at": float(tm["due_at"]),
                    "remaining_sec": max(1, int(float(tm["due_at"]) - now_ts)),
                }
            out.append({
                "mac": mac,
                "ip": live.get("ip") or p.get("ip") or "",
                "devtype": p.get("devtype", 0x2728),
                "name": p["name"],
                "icon": p.get("icon", "🔌"),
                "aliases": list(p.get("aliases") or []),
                "auto_away": bool(p.get("auto_away")),
                "auto_home": bool(p.get("auto_home")),
                "home_only_evening": bool(p.get("home_only_evening")),
                "auto_sleep": bool(p.get("auto_sleep")),
                "online": bool(live.get("online")),
                "is_on": live.get("is_on"),
                "locked": bool(live.get("locked")),
                "last_error": str(live.get("last_error") or ""),
                "checked_at": float(live.get("checked_at") or 0.0),
                "timer": timer_info,
            })
        return out

    def publish_update(self) -> None:
        lan_events.publish("plugs_update", self.get_status_snapshot())

    # ==================== HOOK TỰ ĐỘNG HÓA CÙNG FAN_CONTROLLER ====================

    async def on_away_triggered(self) -> None:
        for p in load_plugs_config():
            if not p.get("auto_away"):
                continue
            cur = await self.refresh_plug(p)
            if cur is True:
                res = await self.set_power(p["mac"], False, reason="rời Wi-Fi > grace")
                if res is False:
                    lan_events.emit_notify(
                        title=f"🚪 Tự tắt {p['name']}",
                        body=f"Điện thoại rời Wi-Fi -> đã tắt {p['name']}.",
                        level="info",
                        source="plugs",
                        speak_tts=False,
                    )

    async def on_home_returned(self, now_ts: Optional[float] = None) -> None:
        for p in load_plugs_config():
            if not p.get("auto_home"):
                continue
            if p.get("home_only_evening") and not _is_evening_hours(now_ts):
                continue
            cur = await self.refresh_plug(p)
            if cur is False:
                res = await self.set_power(p["mac"], True, reason="về phòng (phone online)")
                if res is True:
                    lan_events.emit_notify(
                        title=f"🏠 Tự bật {p['name']}",
                        body=f"Điện thoại về Wi-Fi -> đã bật {p['name']}.",
                        level="info",
                        source="plugs",
                        speak_tts=False,
                    )

    async def get_active_sleep_plugs(self) -> List[Dict[str, Any]]:
        """Trả về các ổ có `auto_sleep=True` hiện đang BẬT (để Smart Butler nhắc khi hỏi đi ngủ)."""
        active: List[Dict[str, Any]] = []
        for p in load_plugs_config():
            if not p.get("auto_sleep"):
                continue
            cur = await self.refresh_plug(p)
            if cur is True:
                active.append(p)
        return active

    async def on_sleep_triggered(self) -> List[str]:
        turned_off: List[str] = []
        for p in load_plugs_config():
            if not p.get("auto_sleep"):
                continue
            cur = await self.refresh_plug(p)
            if cur is True:
                res = await self.set_power(p["mac"], False, reason="giờ ngủ khuya")
                if res is False:
                    turned_off.append(p["name"])
        if turned_off:
            lan_events.emit_notify(
                title="🌙 Tự tắt ổ cắm giờ ngủ",
                body="Đã tắt: " + ", ".join(turned_off),
                level="info",
                source="plugs",
                speak_tts=False,
            )
        return turned_off

    async def tick_timers(self, now_ts: Optional[float] = None) -> None:
        now_v = float(now_ts if now_ts is not None else time.time())
        due_items = [(mac, dict(tm)) for mac, tm in self._timers.items() if float(tm.get("due_at") or 0.0) <= now_v]
        for mac, tm in due_items:
            self._timers.pop(mac, None)
            want_on = tm.get("action") == "on"
            plugs = self._find_plugs(mac)
            if plugs:
                res = await self.set_power(mac, want_on, reason="hẹn giờ ổ cắm")
                if res is not None:
                    lan_events.emit_notify(
                        title=f"⏱️ Hẹn giờ {plugs[0]['name']}",
                        body=f"Đã tự {'bật' if res else 'tắt'} {plugs[0]['name']} đúng giờ hẹn.",
                        level="info",
                        source="plugs",
                        speak_tts=False,
                    )
            self.publish_update()

    # ==================== AI ACTION & NATURAL INTENT ====================

    async def execute_ai_plug_action(self, ai_action: Any, *, event_source: str = "user_telegram") -> str:
        if not isinstance(ai_action, dict):
            return ""
        act = str(ai_action.get("action") or "").strip().lower()
        if not act or act == "null":
            return ""

        targets_raw = ai_action.get("targets") if ai_action.get("targets") is not None else ai_action.get("target")
        plugs = self._find_plugs(targets_raw)
        if not plugs:
            return ""

        if act == "status":
            parts: List[str] = []
            for p in plugs:
                st = await self.refresh_plug(p)
                if st is None:
                    parts.append(f"{p['name']}: ngoại tuyến")
                else:
                    parts.append(f"{p['name']}: đang {'BẬT' if st else 'TẮT'}")
            return "[Hệ thống Ổ cắm: " + "; ".join(parts) + ".]"

        if act in ("schedule_off", "schedule_on"):
            mins = ai_action.get("delay_minutes")
            if mins is None:
                return ""
            want_on = act == "schedule_on"
            names: List[str] = []
            for p in plugs:
                res = self.schedule_timer(p["mac"], want_on, int(mins))
                if res:
                    names.append(res["name"])
            if not names:
                return ""
            return f"[Hệ thống Ổ cắm: Đã hẹn {int(mins)} phút nữa {'bật' if want_on else 'tắt'} {', '.join(names)}.]"

        if act in ("power_on", "on", "power_off", "off", "toggle"):
            results: List[str] = []
            for p in plugs:
                if act == "toggle":
                    cur = await self.refresh_plug(p)
                    want = not bool(cur) if cur is not None else True
                else:
                    want = act in ("power_on", "on")
                res = await self.set_power(p["mac"], want, reason="AI/Chat", event_source=event_source)
                if res is None:
                    results.append(f"không kết nối được {p['name']}")
                else:
                    results.append(f"đã {'BẬT' if res else 'TẮT'} {p['name']}")
            return "[Hệ thống Ổ cắm: " + "; ".join(results) + ".]"

        return ""

    async def handle_natural_plug_intent(self, text: str) -> str:
        plugs = load_plugs_config()
        cmd = detect_natural_plug_command(text, plugs=plugs)
        if not cmd:
            return ""
        return await self.execute_ai_plug_action(cmd)


_CONTROLLER: Optional[SmartPlugController] = None


def get_controller() -> SmartPlugController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = SmartPlugController()
    return _CONTROLLER
