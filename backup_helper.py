# -*- coding: utf-8 -*-
"""
Kho sao lưu SQLite an toàn không phụ thuộc PC:
- Online backup qua `sqlite3.Connection.backup()` nén `.db.gz` xoay vòng tại OpenStick.
- Gửi bản sao lưu `.db` định kỳ hằng ngày qua Telegram DM (`disable_notification=True`).
- Khôi phục DB có kiểm tra tính toàn vẹn (`PRAGMA integrity_check`) + tạo snapshot `pre_restore` trước khi ghi đè.
"""
import os
import io
import gzip
import time
import shutil
import sqlite3
import tempfile
import datetime
import asyncio
import logging
import threading
from typing import Dict, Any, List, Optional

import config
import database
import lan_events
import storage_helper
import task_helper

logger = logging.getLogger(__name__)

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

META_BKP_TG_DAILY = "bkp_daily_telegram_enabled"
META_BKP_DAILY_HOUR = "bkp_daily_hour"
META_BKP_KEEP_MAX = "bkp_keep_max"

MAX_RESTORE_UPLOAD_BYTES = 32 * 1024 * 1024
# Chặn "gzip bomb": file nén 32MB có thể giải nén ra hàng GB làm nổ RAM thiết bị 512MB
MAX_RESTORE_DB_BYTES = 64 * 1024 * 1024
_REQUIRED_TABLES = {"user_state", "chat_history", "memories"}
# Serialize việc tạo backup: scheduler có thể spawn 2 bản song song -> trùng tên/tmp
_CREATE_LOCK = threading.Lock()
_RESTORE_LOCK: Optional[asyncio.Lock] = None


def _resolve_db_path() -> str:
    return str(config.DB_PATH or "").strip() or "smart_butler.db"


def get_backup_dir() -> str:
    if config.BACKUP_DIR:
        bdir = config.BACKUP_DIR
    else:
        db_abs = os.path.abspath(_resolve_db_path())
        db_dir = os.path.dirname(db_abs)
        os.makedirs(db_dir, exist_ok=True)
        bdir = os.path.join(db_dir, "backups")
    os.makedirs(bdir, exist_ok=True)
    return bdir


def _parse_bool(val: Any, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def _parse_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def get_backup_settings() -> Dict[str, Any]:
    meta = database.get_metadata_dict("bkp_")
    return {
        "daily_telegram_enabled": _parse_bool(meta.get(META_BKP_TG_DAILY), True),
        "daily_hour": _parse_int(meta.get(META_BKP_DAILY_HOUR), 3, 0, 23),
        "keep_max": _parse_int(meta.get(META_BKP_KEEP_MAX), 7, 3, 30),
    }


def save_backup_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    if "daily_telegram_enabled" in patch:
        database.set_metadata(
            META_BKP_TG_DAILY,
            "true" if _parse_bool(patch["daily_telegram_enabled"]) else "false",
        )
    if "daily_hour" in patch:
        database.set_metadata(META_BKP_DAILY_HOUR, str(_parse_int(patch["daily_hour"], 3, 0, 23)))
    if "keep_max" in patch:
        database.set_metadata(META_BKP_KEEP_MAX, str(_parse_int(patch["keep_max"], 7, 3, 30)))
    return get_backup_settings()


def _safe_backup_path(filename: str) -> Optional[str]:
    clean = os.path.basename(str(filename or "").strip())
    if not clean or clean != str(filename or "").strip():
        return None
    if not clean.endswith(".db.gz") or ".." in clean:
        return None
    full_path = os.path.join(get_backup_dir(), clean)
    return full_path if os.path.isfile(full_path) else None


def _collect_db_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"chats": 0, "memories": 0, "reminders": 0}
    try:
        cur = conn.cursor()
        for key, table in (("chats", "chat_history"), ("memories", "memories"), ("reminders", "reminders")):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            stats[key] = int(cur.fetchone()[0])
    except Exception:
        pass
    return stats


def create_backup_sync(tag: str = "manual", keep_max: Optional[int] = None) -> Dict[str, Any]:
    """Tạo snapshot SQLite online (an toàn khi bot đang hoạt động) và nén `.db.gz`."""
    with _CREATE_LOCK:
        return _create_backup_sync_impl(tag=tag, keep_max=keep_max)


def _mirror_backup_to_usb_best_effort(src_gz_path: str, keep_mirrors: int = 14) -> Optional[str]:
    """Sao chép bản dự phòng phụ lên USB ngoài (best-effort, bỏ qua êm nếu USB lỗi/ngắt)."""
    if not src_gz_path or not os.path.isfile(src_gz_path):
        return None
    usb_bdir = storage_helper.get_usb_subdir("backup_mirror", create=True)
    if not usb_bdir:
        return None
    dst = os.path.join(usb_bdir, os.path.basename(src_gz_path))
    tmp_dst = dst + ".tmp"
    try:
        shutil.copyfile(src_gz_path, tmp_dst)
        os.replace(tmp_dst, dst)
        mirrors = []
        for name in os.listdir(usb_bdir):
            if not name.endswith(".db.gz"):
                continue
            full = os.path.join(usb_bdir, name)
            if os.path.isfile(full):
                mirrors.append((os.path.getmtime(full), full))
        mirrors.sort(key=lambda x: x[0], reverse=True)
        for _, old_full in mirrors[max(1, keep_mirrors):]:
            try:
                os.remove(old_full)
            except OSError:
                pass
        return dst
    except OSError as e:
        storage_helper.invalidate_usb_health(str(e))
        try:
            if os.path.isfile(tmp_dst):
                os.remove(tmp_dst)
        except OSError:
            pass
        return None


def _create_backup_sync_impl(tag: str = "manual", keep_max: Optional[int] = None) -> Dict[str, Any]:
    settings = get_backup_settings()
    limit = int(keep_max if keep_max is not None else settings["keep_max"])
    bdir = get_backup_dir()
    db_path = _resolve_db_path()
    safe_tag = "".join(c for c in str(tag or "manual") if c.isalnum() or c in ("_", "-"))[:20] or "manual"
    stamp = datetime.datetime.now(VN_TZ).strftime("%Y%m%d_%H%M%S")
    final_name = f"butler_{stamp}_{safe_tag}.db.gz"
    final_path = os.path.join(bdir, final_name)
    seq = 1
    while os.path.exists(final_path) and seq <= 50:
        final_name = f"butler_{stamp}_{safe_tag}_{seq}.db.gz"
        final_path = os.path.join(bdir, final_name)
        seq += 1

    fd, tmp_db = tempfile.mkstemp(prefix="ds_bkp_", suffix=".db", dir=bdir)
    os.close(fd)
    tmp_gz = f"{final_path}.tmp"

    stats: Dict[str, Any] = {}
    try:
        src = database.get_connection()
        try:
            dst = sqlite3.connect(tmp_db, timeout=10)
            try:
                src.backup(dst)
                stats = _collect_db_stats(dst)
            finally:
                dst.close()
        finally:
            src.close()

        with open(tmp_db, "rb") as f_in, gzip.open(tmp_gz, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out, length=65536)
        os.replace(tmp_gz, final_path)
    finally:
        for p in (tmp_db, tmp_gz):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # Xoay vòng giữ lại tối đa `limit` bản mới nhất
    entries = list_backups()
    for old in entries[max(1, limit):]:
        old_path = _safe_backup_path(old["filename"])
        if old_path:
            try:
                os.remove(old_path)
            except OSError:
                pass

    usb_mirror = _mirror_backup_to_usb_best_effort(final_path)

    size_bytes = os.path.getsize(final_path)
    raw_db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    info = {
        "filename": final_name,
        "path": final_path,
        "usb_mirror_path": usb_mirror,
        "size_bytes": size_bytes,
        "raw_db_bytes": raw_db_size,
        "created_at": time.time(),
        "stats": stats,
        "tag": safe_tag,
    }
    database.log_system_event(
        "backup",
        "info",
        f"Đã tạo bản sao lưu {final_name}",
        f"{size_bytes / 1024:.1f} KB nén | {stats.get('chats', 0)} chat, {stats.get('memories', 0)} ký ức",
    )
    lan_events.publish("backup_update", get_backup_summary())
    return info


async def create_backup(tag: str = "manual", keep_max: Optional[int] = None) -> Dict[str, Any]:
    return await asyncio.to_thread(create_backup_sync, tag, keep_max)


def list_backups() -> List[Dict[str, Any]]:
    bdir = get_backup_dir()
    items: List[Dict[str, Any]] = []
    for name in os.listdir(bdir):
        if not name.endswith(".db.gz"):
            continue
        full = os.path.join(bdir, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        items.append({
            "filename": name,
            "size_bytes": st.st_size,
            "created_at": st.st_mtime,
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def get_backup_summary() -> Dict[str, Any]:
    items = list_backups()
    db_path = _resolve_db_path()
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return {
        "count": len(items),
        "latest": items[0] if items else None,
        "db_size_bytes": db_size,
        "settings": get_backup_settings(),
        "usb_storage": storage_helper.get_usb_storage_snapshot(),
    }


def export_decompressed_temp_sync(filename: Optional[str] = None) -> tuple[str, str]:
    """Giải nén 1 file `.db.gz` ra file `.db` tạm (caller chịu trách nhiệm xóa file tạm)."""
    if filename:
        gz_path = _safe_backup_path(filename)
        if not gz_path:
            raise FileNotFoundError("Không tìm thấy file sao lưu hợp lệ.")
        out_name = os.path.basename(filename)[:-3]  # bỏ .gz
    else:
        items = list_backups()
        if not items:
            created = create_backup_sync(tag="export")
            gz_path = created["path"]
            out_name = created["filename"][:-3]
        else:
            gz_path = _safe_backup_path(items[0]["filename"])
            out_name = items[0]["filename"][:-3]

    fd, tmp_db = tempfile.mkstemp(prefix="ds_export_", suffix=".db", dir=get_backup_dir())
    os.close(fd)
    with gzip.open(gz_path, "rb") as f_in, open(tmp_db, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=65536)
    return tmp_db, out_name


async def send_backup_to_telegram(
    bot,
    user_id: int,
    reason: str = "Hàng ngày",
    create_new: bool = True,
) -> Dict[str, Any]:
    """Tạo bản backup mới (tùy chọn) và gửi file `.db` trực tiếp lên Telegram DM."""
    if user_id <= 0 or bot is None:
        raise ValueError("Chưa xác định được user_id Telegram để gửi sao lưu.")

    info = await create_backup(tag="tg") if create_new else {"filename": None, "stats": {}}
    tmp_db, out_name = await asyncio.to_thread(export_decompressed_temp_sync, info.get("filename"))
    try:
        from aiogram.types import FSInputFile
        vn_str = datetime.datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M")
        stats = info.get("stats") or {}
        raw_kb = os.path.getsize(tmp_db) / 1024.0
        caption = (
            f"💾 Sao lưu Database Smart Butler ({reason})\n"
            f"🕒 Lúc {vn_str} | Dung lượng {raw_kb:.1f} KB\n"
            f"📊 {stats.get('chats', '?')} tin nhắn • {stats.get('memories', '?')} ký ức • {stats.get('reminders', '?')} lịch hẹn"
        )
        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(tmp_db, filename=out_name),
            caption=caption,
            disable_notification=True,
        )
        database.log_system_event("backup", "info", f"Đã gửi backup lên Telegram ({reason})", out_name)
        lan_events.emit_notify(
            title="Đã gửi bản sao lưu Telegram",
            body=f"{out_name} ({raw_kb:.1f} KB)",
            level="info",
            source="backup",
        )
        return info
    finally:
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass


def _validate_and_restore_from_path_sync(src_path: str) -> Dict[str, Any]:
    if not src_path or not os.path.exists(src_path):
        raise ValueError("Không tìm thấy dữ liệu sao lưu.")
    file_size = os.path.getsize(src_path)
    if file_size <= 0:
        raise ValueError("Dữ liệu sao lưu rỗng.")
    if file_size > MAX_RESTORE_UPLOAD_BYTES:
        raise ValueError("File vượt quá giới hạn 32MB.")

    with open(src_path, "rb") as f:
        header = f.read(16)

    bdir = get_backup_dir()
    fd, tmp_in = tempfile.mkstemp(prefix="ds_restore_", suffix=".db", dir=bdir)
    os.close(fd)
    try:
        if header[:2] == b"\x1f\x8b":
            try:
                total = 0
                checked_magic = False
                with gzip.GzipFile(filename=src_path, mode="rb") as gz, open(tmp_in, "wb") as out_f:
                    while True:
                        chunk = gz.read(65536)
                        if not chunk:
                            break
                        if not checked_magic:
                            checked_magic = True
                            if not chunk.startswith(b"SQLite format 3\x00"):
                                raise ValueError("Không đúng định dạng file SQLite (.db hoặc .db.gz).")
                        total += len(chunk)
                        if total > MAX_RESTORE_DB_BYTES:
                            raise ValueError(
                                f"File giải nén vượt {MAX_RESTORE_DB_BYTES // (1024 * 1024)}MB — từ chối để bảo vệ RAM."
                            )
                        out_f.write(chunk)
                if total == 0:
                    raise ValueError("Dữ liệu sao lưu rỗng.")
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"File .gz bị hỏng: {e}")
        else:
            if file_size > MAX_RESTORE_DB_BYTES:
                raise ValueError(
                    f"File vượt {MAX_RESTORE_DB_BYTES // (1024 * 1024)}MB — từ chối để bảo vệ RAM."
                )
            if not header.startswith(b"SQLite format 3\x00"):
                raise ValueError("Không đúng định dạng file SQLite (.db hoặc .db.gz).")
            with open(src_path, "rb") as in_f, open(tmp_in, "wb") as out_f:
                while True:
                    chunk = in_f.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)

        chk_conn = sqlite3.connect(tmp_in, timeout=5)
        try:
            res = chk_conn.execute("PRAGMA integrity_check").fetchone()
            if not res or str(res[0]).lower() != "ok":
                raise ValueError("File SQLite lỗi toàn vẹn (integrity_check failed).")
            tables = {
                row[0]
                for row in chk_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            missing = _REQUIRED_TABLES - tables
            if missing:
                raise ValueError(f"File thiếu bảng dữ liệu bắt buộc: {', '.join(sorted(missing))}")
            stats = _collect_db_stats(chk_conn)
        finally:
            chk_conn.close()

        # Tạo bản dự phòng trước khi ghi đè
        pre_info = create_backup_sync(tag="pre_restore")

        src = sqlite3.connect(tmp_in, timeout=10)
        try:
            dst = database.get_connection()
            try:
                src.backup(dst)
                # Gộp WAL về file chính để trạng thái DB trên đĩa nhất quán ngay sau restore
                try:
                    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
            finally:
                dst.close()
        finally:
            src.close()

        database.init_db()
        try:
            chk = database.get_connection()
            try:
                ok_row = chk.execute("PRAGMA integrity_check").fetchone()
                if not ok_row or str(ok_row[0]).lower() != "ok":
                    raise ValueError("DB sau khi khôi phục không vượt qua integrity_check.")
            finally:
                chk.close()
        except sqlite3.Error as e:
            raise ValueError(f"Không kiểm tra được DB sau khôi phục: {e}")

        # Đồng bộ lại cache volume / EQ / alarms / timed_session trên RAM từ DB vừa khôi phục
        try:
            import esp32_audio
            esp32_audio.get_manager().reload_persisted_state()
        except Exception as e:
            logger.debug(f"Không reload được trạng thái esp32_audio sau restore: {e}")

        database.log_system_event(
            "backup",
            "warning",
            "Đã khôi phục Database từ bản sao lưu",
            f"Dự phòng trước restore: {pre_info['filename']} | {stats.get('chats', 0)} chat, {stats.get('memories', 0)} ký ức",
        )
        lan_events.publish("backup_update", get_backup_summary())
        return {"restored": True, "pre_restore_backup": pre_info["filename"], "stats": stats}
    finally:
        if os.path.exists(tmp_in):
            try:
                os.remove(tmp_in)
            except OSError:
                pass


def _validate_and_restore_sync(raw_bytes: bytes) -> Dict[str, Any]:
    if not raw_bytes:
        raise ValueError("Dữ liệu sao lưu rỗng.")
    if len(raw_bytes) > MAX_RESTORE_UPLOAD_BYTES:
        raise ValueError("File vượt quá giới hạn 32MB.")
    bdir = get_backup_dir()
    fd, tmp_raw = tempfile.mkstemp(prefix="ds_raw_", suffix=".bin", dir=bdir)
    os.close(fd)
    try:
        with open(tmp_raw, "wb") as f:
            f.write(raw_bytes)
        return _validate_and_restore_from_path_sync(tmp_raw)
    finally:
        if os.path.exists(tmp_raw):
            try:
                os.remove(tmp_raw)
            except OSError:
                pass


async def _run_restore_locked(user_id: int, fn, arg) -> Dict[str, Any]:
    global _RESTORE_LOCK
    if _RESTORE_LOCK is None:
        _RESTORE_LOCK = asyncio.Lock()
    uid = int(user_id or 0)
    if uid <= 0:
        try:
            uid = int(getattr(config, "ALLOWED_USER_ID", 0) or 0) or int(database.get_primary_user_id() or 0)
        except Exception:
            uid = 0
    async with _RESTORE_LOCK:
        if uid > 0:
            async with task_helper.get_user_lock(uid):
                return await asyncio.to_thread(fn, arg)
        return await asyncio.to_thread(fn, arg)


async def restore_backup_file(src_path: str, user_id: int = 0) -> Dict[str, Any]:
    return await _run_restore_locked(user_id, _validate_and_restore_from_path_sync, src_path)


async def restore_backup_bytes(raw_bytes: bytes, user_id: int = 0) -> Dict[str, Any]:
    return await _run_restore_locked(user_id, _validate_and_restore_sync, raw_bytes)


async def restore_backup_snapshot(filename: str, user_id: int = 0) -> Dict[str, Any]:
    gz_path = _safe_backup_path(filename)
    if not gz_path:
        raise FileNotFoundError("Không tìm thấy snapshot hợp lệ.")
    return await restore_backup_file(gz_path, user_id=user_id)
