# -*- coding: utf-8 -*-
"""
Quản lý ổ USB gắn ngoài tùy chọn (lưu trữ không quan trọng: cache nhạc, yt-dlp cache, file tạm, bản sao phụ).
Thiết kế chống lỗi cho USB kém bền / có thể rút nóng hoặc hỏng bất cứ lúc nào:
- Chỉ chấp nhận USB khi có mặt thực sự trong `/proc/mounts` với quyền `rw` (không ghi nhầm lên eMMC).
- Kiểm tra ghi thực tế có cache ngắn (15s) và cooldown ngay 30s nếu gặp lỗi I/O / read-only (`EROFS`/`EIO`).
- Luôn rơi về thư mục dự phòng trên eMMC nội bộ mà không làm gián đoạn bot hay loa ESP32.
"""
import os
import time
import uuid
import shutil
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

APP_SUBDIR = "smart_butler"
MIN_USB_FREE_BYTES = 128 * 1024 * 1024
PROBE_TTL_SEC = 15.0
DEFAULT_FAIL_COOLDOWN_SEC = 30.0

_LOCK = threading.Lock()
_LAST_PROBE_OK: Optional[bool] = None
_LAST_PROBE_AT: float = 0.0
_LAST_PROBE_ROOT: str = ""
_COOLDOWN_UNTIL: float = 0.0
_LAST_ERROR_REASON: str = ""


def get_usb_mount_root() -> str:
    raw = str(getattr(config, "USB_STORAGE_DIR", "") or "/mnt/usb").strip() or "/mnt/usb"
    return os.path.normpath(os.path.abspath(raw))


def is_usb_path(path: str) -> bool:
    if not path:
        return False
    root = get_usb_mount_root()
    try:
        norm = os.path.normpath(os.path.abspath(str(path)))
    except Exception:
        return False
    return norm == root or norm.startswith(root + os.sep)


def _decode_mount_path(raw_path: str) -> str:
    return (
        raw_path.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _read_proc_mount_entry(mount_root: str) -> Optional[Dict[str, Any]]:
    if os.name != "posix" or not os.path.exists("/proc/mounts"):
        return None
    target = os.path.normpath(mount_root)
    matched: Optional[Dict[str, Any]] = None
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                dev = parts[0]
                mnt = os.path.normpath(_decode_mount_path(parts[1]))
                fstype = parts[2]
                opts = [o.strip() for o in parts[3].split(",") if o.strip()]
                if mnt == target and fstype != "autofs":
                    matched = {
                        "device": dev,
                        "mount_point": mnt,
                        "fstype": fstype,
                        "options": opts,
                        "readonly": ("ro" in opts),
                    }
    except OSError:
        return None
    return matched


def _get_mount_status(mount_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    root = os.path.normpath(mount_root or get_usb_mount_root())
    if os.name == "posix":
        return _read_proc_mount_entry(root)
    return None


def is_usb_mounted(mount_root: Optional[str] = None) -> bool:
    return _get_mount_status(mount_root) is not None


def invalidate_usb_health(reason: str = "", cooldown_sec: float = DEFAULT_FAIL_COOLDOWN_SEC) -> None:
    global _LAST_PROBE_OK, _LAST_PROBE_AT, _LAST_PROBE_ROOT, _COOLDOWN_UNTIL, _LAST_ERROR_REASON
    root = get_usb_mount_root()
    now_m = time.monotonic()
    clean_reason = str(reason or "io_error").strip()[:160]
    with _LOCK:
        was_ok = _LAST_PROBE_OK is True
        _LAST_PROBE_OK = False
        _LAST_PROBE_AT = now_m
        _LAST_PROBE_ROOT = root
        _COOLDOWN_UNTIL = now_m + max(1.0, float(cooldown_sec))
        _LAST_ERROR_REASON = clean_reason
    if was_ok:
        logger.warning(
            f"⚠️ [USB Storage] Phát hiện sự cố USB ({clean_reason}) -> tạm chuyển sang bộ nhớ trong trong {cooldown_sec:.0f}s"
        )


def reset_probe_cache() -> None:
    """Dùng cho unit test hoặc kiểm tra lại thủ công sau khi gắn lại USB."""
    global _LAST_PROBE_OK, _LAST_PROBE_AT, _LAST_PROBE_ROOT, _COOLDOWN_UNTIL, _LAST_ERROR_REASON
    with _LOCK:
        _LAST_PROBE_OK = None
        _LAST_PROBE_AT = 0.0
        _LAST_PROBE_ROOT = ""
        _COOLDOWN_UNTIL = 0.0
        _LAST_ERROR_REASON = ""


def is_usb_healthy(force_refresh: bool = False, respect_cooldown: Optional[bool] = None) -> bool:
    global _LAST_PROBE_OK, _LAST_PROBE_AT, _LAST_PROBE_ROOT, _COOLDOWN_UNTIL, _LAST_ERROR_REASON
    root = get_usb_mount_root()
    check_cooldown = (not force_refresh) if respect_cooldown is None else bool(respect_cooldown)

    mount_info = _get_mount_status(root)
    if mount_info is None:
        with _LOCK:
            was_ok = _LAST_PROBE_OK is True
            _LAST_PROBE_OK = False
            _LAST_PROBE_ROOT = root
            _LAST_ERROR_REASON = "not_mounted"
        if was_ok:
            logger.warning("🔌 [USB Storage] Ổ USB đã tháo/ngắt mount -> chuyển ngay về bộ nhớ trong")
        return False
    if mount_info.get("readonly"):
        invalidate_usb_health("filesystem remounted read-only")
        return False

    now_m = time.monotonic()
    with _LOCK:
        if _LAST_PROBE_ROOT == root:
            if check_cooldown and _LAST_PROBE_OK is False and now_m < _COOLDOWN_UNTIL:
                return False
            if not force_refresh and _LAST_PROBE_OK is True and (now_m - _LAST_PROBE_AT) < PROBE_TTL_SEC:
                return True

    try:
        usage = shutil.disk_usage(root)
        if usage.free < MIN_USB_FREE_BYTES:
            invalidate_usb_health(f"low_space free={usage.free}")
            return False

        app_dir = os.path.join(root, APP_SUBDIR)
        os.makedirs(app_dir, exist_ok=True)
        probe_file = os.path.join(app_dir, f".ds_probe_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(probe_file, "wb") as f:
                f.write(b"ok")
        finally:
            try:
                os.remove(probe_file)
            except FileNotFoundError:
                pass
    except OSError as e:
        invalidate_usb_health(str(e))
        return False

    with _LOCK:
        was_bad = _LAST_PROBE_OK is False
        first_seen = _LAST_PROBE_OK is None
        _LAST_PROBE_OK = True
        _LAST_PROBE_AT = time.monotonic()
        _LAST_PROBE_ROOT = root
        _COOLDOWN_UNTIL = 0.0
        _LAST_ERROR_REASON = ""
    if was_bad or first_seen:
        logger.info(f"💾 [USB Storage] Sẵn sàng lưu dữ liệu phụ trên USB ({root})")
    return True


def get_usb_subdir(rel_name: str, create: bool = True) -> Optional[str]:
    clean = str(rel_name or "").strip().strip("/\\")
    if not clean or ".." in clean or not is_usb_healthy():
        return None
    target = os.path.join(get_usb_mount_root(), APP_SUBDIR, clean)
    if create:
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            invalidate_usb_health(str(e))
            return None
    return target


def get_expendable_dir(rel_name: str, local_fallback_dir: str, create: bool = True) -> Tuple[str, bool]:
    """
    Trả về `(path, is_usb)` ưu tiên thư mục trên USB khi USB khỏe;
    nếu USB chưa gắn / hỏng / read-only sẽ tự rơi về `local_fallback_dir`.
    """
    usb_dir = get_usb_subdir(rel_name, create=create)
    if usb_dir:
        return usb_dir, True
    if create:
        os.makedirs(local_fallback_dir, exist_ok=True)
    return local_fallback_dir, False


def get_usb_storage_snapshot() -> Dict[str, Any]:
    root = get_usb_mount_root()
    mount_info = _get_mount_status(root)
    mounted = mount_info is not None
    healthy = is_usb_healthy() if mounted else False

    total_b = 0
    used_b = 0
    free_b = 0
    if mounted:
        try:
            u = shutil.disk_usage(root)
            total_b, used_b, free_b = int(u.total), int(u.used), int(u.free)
        except OSError:
            pass

    with _LOCK:
        cd_rem = max(0, int(round(_COOLDOWN_UNTIL - time.monotonic()))) if _COOLDOWN_UNTIL > time.monotonic() else 0
        last_err = _LAST_ERROR_REASON

    return {
        "mount_point": root,
        "mounted": mounted,
        "healthy": healthy,
        "readonly": bool(mount_info and mount_info.get("readonly")),
        "fstype": (mount_info or {}).get("fstype", ""),
        "device": (mount_info or {}).get("device", ""),
        "total_bytes": total_b,
        "used_bytes": used_b,
        "free_bytes": free_b,
        "cooldown_remain_sec": cd_rem,
        "last_error": last_err,
    }
