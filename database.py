# -*- coding: utf-8 -*-
import os
import re
import json
import sqlite3
import time
import datetime
import contextlib
import logging
from typing import Optional, List, Dict, Any, Tuple
import config

logger = logging.getLogger(__name__)
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

PERSONALITY_BOUNDS = {
    "tease_level": (35, 80),     # Độ cà khịa / lầy
    "stubborn_level": (25, 75),  # Độ bướng / dỗi nhẹ
    "clingy_level": (35, 80),    # Độ nũng nịu
}
PERSONALITY_DEFAULTS = {
    "tease_level": 58,
    "stubborn_level": 48,
    "clingy_level": 62,
}
MAX_TRAIT_STEP_PER_UPDATE = 2

def get_connection():
    """Mở kết nối SQLite: chờ tối đa 10s nếu DB đang bị khóa (tránh 'database is locked')."""
    db_path = config.DB_PATH or "smart_butler.db"
    dir_part = os.path.dirname(db_path)
    if dir_part and not os.path.exists(dir_part):
        logger.warning(f"Không thấy thư mục của DB_PATH ('{dir_part}') -> tạm dùng 'smart_butler.db' ở thư mục hiện tại")
        db_path = "smart_butler.db"
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def _open_conn(commit: bool = False):
    """Mở kết nối SQLite và luôn đóng an toàn trong `finally`, tránh rò rỉ file descriptor."""
    conn = get_connection()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def init_db():
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()

        # WAL cho phép đọc/ghi song song, giảm nguy cơ khóa khi nhiều tin nhắn tới cùng lúc
        cursor.execute("PRAGMA journal_mode = WAL")
    
        # Bảng trạng thái người dùng
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            affinity_score INTEGER DEFAULT 55,
            mood TEXT DEFAULT 'vui vẻ',
            cooldown_until REAL DEFAULT 0.0,
            last_user_message_time REAL DEFAULT 0.0,
            last_bot_message_time REAL DEFAULT 0.0,
            status TEXT DEFAULT 'active',
            tease_level INTEGER DEFAULT 58,
            stubborn_level INTEGER DEFAULT 48,
            clingy_level INTEGER DEFAULT 62,
            mood_updated_at REAL DEFAULT 0.0,
            next_proactive_check_at REAL DEFAULT 0.0,
            unanswered_proactive_count INTEGER DEFAULT 0
        )
        """)

        cursor.execute("PRAGMA table_info(user_state)")
        us_cols = [col[1] for col in cursor.fetchall()]
        for col_name, col_def in (
            ("tease_level", "INTEGER DEFAULT 58"),
            ("stubborn_level", "INTEGER DEFAULT 48"),
            ("clingy_level", "INTEGER DEFAULT 62"),
            ("mood_updated_at", "REAL DEFAULT 0.0"),
            ("next_proactive_check_at", "REAL DEFAULT 0.0"),
            ("unanswered_proactive_count", "INTEGER DEFAULT 0"),
        ):
            if col_name not in us_cols:
                cursor.execute(f"ALTER TABLE user_state ADD COLUMN {col_name} {col_def}")
    
        # Bảng lịch sử trò chuyện
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            delta_affinity INTEGER DEFAULT 0,
            created_at REAL
        )
        """)
    
        # Bảng trí nhớ dài hạn (Fact Book + Episodic Turns) kèm vector embedding, category, importance và thời gian phai mờ
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            fact TEXT,
            category TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 2,
            embedding BLOB,
            expires_at REAL DEFAULT NULL,
            created_at REAL,
            updated_at REAL DEFAULT NULL
        )
        """)
    
        # Kiểm tra và thêm cột nếu bảng cũ chưa có
        cursor.execute("PRAGMA table_info(memories)")
        columns = [col[1] for col in cursor.fetchall()]
        if "embedding" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
        if "category" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general'")
        if "importance" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 2")
        if "expires_at" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN expires_at REAL DEFAULT NULL")
        if "updated_at" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN updated_at REAL DEFAULT NULL")

        # Bảng xu hướng đời thực mỗi ngày (Daily Trends)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_trends (
            date TEXT PRIMARY KEY,
            trends_text TEXT,
            updated_at REAL
        )
        """)
    
        # Bảng nhật ký đời thực mỗi ngày của Smart Butler (Daily Life Diary)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_diary (
            date TEXT PRIMARY KEY,
            diary_text TEXT,
            created_at REAL
        )
        """)

        # Bảng theo dõi lịch sử gửi ảnh để chống trùng lặp
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            photo_key TEXT,
            sent_at REAL
        )
        """)

        # Bảng lưu metadata vận hành (ví dụ: ngày gần nhất đã audit trí nhớ)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at REAL
        )
        """)

        # Bảng nhắc hẹn & lịch hỏi thăm tự chủ (user_reminder / ai_followup)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'user_reminder',
            title TEXT NOT NULL,
            context_note TEXT DEFAULT '',
            due_at REAL NOT NULL,
            orig_due_at REAL DEFAULT NULL,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            sent_at REAL DEFAULT NULL
        )
        """)
        cursor.execute("PRAGMA table_info(reminders)")
        rem_cols = [col[1] for col in cursor.fetchall()]
        if "orig_due_at" not in rem_cols:
            cursor.execute("ALTER TABLE reminders ADD COLUMN orig_due_at REAL DEFAULT NULL")
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_reminders_due
        ON reminders(user_id, status, due_at)
        """)

        # Bảng nhật ký sự cố & sự kiện hệ thống (Watchdog, Backup, Presence)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_system_events_ts
        ON system_events(created_at DESC)
        """)

        # Sổ từ chối (Rejection Ledger): ký ức user đã xóa tay để AI không trích xuất lại
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS rejected_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            norm_text TEXT NOT NULL,
            raw_fact TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            created_at REAL NOT NULL,
            UNIQUE(user_id, norm_text)
        )
        """)

        # Nhật ký quyết định ProactiveArbiter (dùng cho cap/cooldown & hiển thị Web Dashboard)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS proactive_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT DEFAULT '',
            category TEXT DEFAULT '',
            score REAL DEFAULT 0.0,
            details_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_proactive_decisions_ts
        ON proactive_decisions(user_id, created_at DESC)
        """)

        # Bảng ghi log sự kiện thao tác thiết bị thực tế (phục vụ tự học thói quen)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_type TEXT NOT NULL,
            target TEXT NOT NULL,
            action TEXT NOT NULL,
            value_num REAL DEFAULT NULL,
            value_text TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'user',
            hour_vn INTEGER NOT NULL,
            minute_vn INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            date_vn TEXT NOT NULL,
            temp_c REAL DEFAULT NULL,
            humidity_pct REAL DEFAULT NULL,
            presence_state TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_device_events_lookup
        ON device_events(device_type, target, created_at DESC)
        """)

        # Bảng thói quen tự học & quy tắc từ chat (không cần cấu hình tay)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS learned_habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_key TEXT NOT NULL UNIQUE,
            pattern_type TEXT NOT NULL,
            device_type TEXT NOT NULL,
            target TEXT NOT NULL,
            action TEXT NOT NULL,
            action_value REAL DEFAULT NULL,
            action_meta_json TEXT DEFAULT '{}',
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'behavior',
            status TEXT NOT NULL DEFAULT 'learning',
            confidence REAL NOT NULL DEFAULT 0.0,
            evidence_days INTEGER NOT NULL DEFAULT 0,
            occurrences INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            last_seen_at REAL DEFAULT 0.0,
            last_triggered_at REAL DEFAULT 0.0,
            last_triggered_date TEXT DEFAULT '',
            updated_at REAL NOT NULL
        )
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_learned_habits_status
        ON learned_habits(status, confidence DESC)
        """)


def get_metadata(key: str) -> Optional[str]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM app_metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


def set_metadata(key: str, value: str):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO app_metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, value, time.time()))


def get_primary_user_id() -> int:
    """Lấy user_id đầu tiên trong bảng user_state để phục vụ Proactive Scheduler."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM user_state ORDER BY last_user_message_time DESC LIMIT 1")
        row = cursor.fetchone()
        return int(row["user_id"]) if row else 0


def get_or_create_user(user_id: int) -> Dict[str, Any]:
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            now = time.time()
            cursor.execute("""
            INSERT INTO user_state (
                user_id, affinity_score, mood, cooldown_until,
                last_user_message_time, last_bot_message_time, status,
                tease_level, stubborn_level, clingy_level, mood_updated_at
            )
            VALUES (?, 80, 'sẵn sàng', 0.0, ?, ?, 'active', ?, ?, ?, ?)
            """, (
                user_id, now, now,
                PERSONALITY_DEFAULTS["tease_level"],
                PERSONALITY_DEFAULTS["stubborn_level"],
                PERSONALITY_DEFAULTS["clingy_level"],
                now,
            ))
            cursor.execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
        user_data = dict(row)
        for k, default_val in PERSONALITY_DEFAULTS.items():
            low, high = PERSONALITY_BOUNDS[k]
            raw_val = user_data.get(k)
            val = int(raw_val) if raw_val is not None else default_val
            user_data[k] = max(low, min(high, val))
        if user_data.get("mood_updated_at") is None:
            user_data["mood_updated_at"] = 0.0
        if user_data.get("next_proactive_check_at") is None:
            user_data["next_proactive_check_at"] = 0.0
        if user_data.get("unanswered_proactive_count") is None:
            user_data["unanswered_proactive_count"] = 0
        return user_data


def update_user_state(user_id: int, affinity_delta: int = 0, new_mood: Optional[str] = None, 
                      cooldown_until: Optional[float] = None, is_user_msg: bool = False,
                      is_bot_msg: bool = False, followup_grace_sec: float = 120.0):
    user = get_or_create_user(user_id)
    new_score = max(0, min(100, user["affinity_score"] + affinity_delta))
    mood = new_mood if new_mood else user["mood"]
    now = time.time()

    last_user_time = now if is_user_msg else user["last_user_message_time"]
    last_bot_time = now if is_bot_msg else user["last_bot_message_time"]
    cd_until = cooldown_until if cooldown_until is not None else user["cooldown_until"]
    mood_ts = now if (new_mood and new_mood != user["mood"]) or is_bot_msg else float(user.get("mood_updated_at") or 0.0)
    unanswered_cnt = 0 if is_user_msg else int(user.get("unanswered_proactive_count") or 0)
    next_check = 0.0 if is_user_msg else float(user.get("next_proactive_check_at") or 0.0)

    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE user_state
        SET affinity_score = ?, mood = ?, cooldown_until = ?, last_user_message_time = ?, last_bot_message_time = ?,
            mood_updated_at = ?, unanswered_proactive_count = ?, next_proactive_check_at = ?
        WHERE user_id = ?
        """, (new_score, mood, cd_until, last_user_time, last_bot_time, mood_ts, unanswered_cnt, next_check, user_id))

        if is_user_msg:
            # Chỉ tự huỷ các ai_followup cũ đã tạo trước >2 phút; tránh xoá nhầm hẹn vừa tạo khi user nhắn nhiều tin liên tiếp.
            if followup_grace_sec and followup_grace_sec > 0:
                cutoff = now - float(followup_grace_sec)
                cursor.execute("""
                UPDATE reminders
                SET status = 'cancelled'
                WHERE user_id = ? AND kind = 'ai_followup' AND status = 'pending' AND created_at < ?
                """, (user_id, cutoff))
            else:
                cursor.execute("""
                UPDATE reminders
                SET status = 'cancelled'
                WHERE user_id = ? AND kind = 'ai_followup' AND status = 'pending'
                """, (user_id,))
            if cursor.rowcount:
                logger.info(f"⏰ Tự huỷ {cursor.rowcount} lịch ai_followup cũ do user {user_id} nhắn mới.")

    return new_score, mood


def update_personality_traits(user_id: int, trait_deltas: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """
    Tiến hóa chậm các chỉ số cá tính trong khung an toàn tuyệt đối:
    - Mỗi bước cập nhật kẹp trong [-MAX_TRAIT_STEP_PER_UPDATE, +MAX_TRAIT_STEP_PER_UPDATE] (±2).
    - Giá trị cuối luôn bị khóa cứng trong PERSONALITY_BOUNDS.
    """
    user = get_or_create_user(user_id)
    updated = {k: int(user[k]) for k in PERSONALITY_BOUNDS}
    if not trait_deltas or not isinstance(trait_deltas, dict):
        return updated

    changed = False
    for k, (low, high) in PERSONALITY_BOUNDS.items():
        if k not in trait_deltas:
            continue
        try:
            raw_delta = int(trait_deltas[k])
        except (TypeError, ValueError):
            continue
        step = max(-MAX_TRAIT_STEP_PER_UPDATE, min(MAX_TRAIT_STEP_PER_UPDATE, raw_delta))
        new_val = max(low, min(high, updated[k] + step))
        if new_val != updated[k]:
            updated[k] = new_val
            changed = True

    if changed:
        with _open_conn(commit=True) as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE user_state
            SET tease_level = ?, stubborn_level = ?, clingy_level = ?
            WHERE user_id = ?
            """, (updated["tease_level"], updated["stubborn_level"], updated["clingy_level"], user_id))
    return updated


def add_chat_message(user_id: int, role: str, content: str, delta_affinity: int = 0):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO chat_history (user_id, role, content, delta_affinity, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (user_id, role, content, delta_affinity, time.time()))


def get_recent_history(user_id: int, limit: int = 12) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT role, content, delta_affinity, created_at 
        FROM chat_history 
        WHERE user_id = ? 
        ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        return [dict(r) for r in reversed(rows)]


def _default_importance(category: str) -> int:
    if category in ("general", "shared_moment"):
        return 3
    if category == "episode":
        return 1
    return 2


def add_memory(user_id: int, fact: str, category: str = "general", 
               embedding: Optional[bytes] = None, expires_at: Optional[float] = None,
               created_at: Optional[float] = None, importance: Optional[int] = None) -> Optional[int]:
    if not fact or len(fact.strip()) < 3:
        return None
    fact = fact.strip()
    imp = max(1, min(3, int(importance))) if importance is not None else _default_importance(category)
    now_ts = created_at if created_at is not None else time.time()
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        # Kiểm tra trùng lặp sơ bộ
        cursor.execute("SELECT id, embedding FROM memories WHERE user_id = ? AND fact = ?", (user_id, fact))
        row = cursor.fetchone()
        mem_id = None
        if not row:
            cursor.execute("""
            INSERT INTO memories (user_id, fact, category, importance, embedding, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, fact, category, imp, embedding, expires_at, now_ts, now_ts))
            mem_id = cursor.lastrowid
        elif embedding and not row["embedding"]:
            cursor.execute("UPDATE memories SET embedding = ?, category = ?, importance = ?, expires_at = ?, updated_at = ? WHERE id = ?", 
                           (embedding, category, imp, expires_at, time.time(), row["id"]))
            mem_id = row["id"]
        else:
            mem_id = row["id"]
        return mem_id


def update_memory_fact(memory_id: int, new_fact: str, category: Optional[str] = None,
                       embedding: Optional[bytes] = None, expires_at: Optional[float] = None,
                       importance: Optional[int] = None) -> bool:
    """Cập nhật nội dung, độ quan trọng và vector embedding của một ký ức đã tồn tại (Memory Evolution)."""
    if not new_fact or len(new_fact.strip()) < 3:
        return False
    now_ts = time.time()
    fields = ["fact = ?", "updated_at = ?"]
    params: List[Any] = [new_fact.strip(), now_ts]
    if category is not None:
        fields.append("category = ?")
        params.append(category)
    if importance is not None:
        fields.append("importance = ?")
        params.append(max(1, min(3, int(importance))))
    if embedding is not None:
        fields.append("embedding = ?")
        params.append(embedding)
    if expires_at is not None:
        fields.append("expires_at = ?")
        params.append(expires_at)
    params.append(memory_id)
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", tuple(params))
        return cursor.rowcount > 0


def delete_memory(memory_id: int, record_rejection: bool = False, source: str = "manual") -> Optional[Dict[str, Any]]:
    """Xóa một ký ức lỗi thời hoặc bị loại bỏ; tùy chọn ghi vào Rejection Ledger."""
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, fact, category FROM memories WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        deleted_info = dict(row) if row else None
        cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    if record_rejection and deleted_info and deleted_info.get("fact"):
        reject_memory(
            int(deleted_info["user_id"]),
            str(deleted_info["fact"]),
            source=source,
        )
    return deleted_info


def update_memory_embedding(memory_id: int, embedding: bytes):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE memories SET embedding = ? WHERE id = ?", (embedding, memory_id))


def get_memories_with_embeddings(
    user_id: int,
    include_expired: bool = False,
    exclude_categories: Optional[List[str]] = None,
    require_embedding: bool = False,
) -> List[Dict[str, Any]]:
    now = time.time()
    clauses = ["user_id = ?"]
    params: List[Any] = [user_id]
    if not include_expired:
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)
    if exclude_categories:
        placeholders = ",".join("?" for _ in exclude_categories)
        clauses.append(f"COALESCE(category, 'general') NOT IN ({placeholders})")
        params.extend(exclude_categories)
    if require_embedding:
        clauses.append("embedding IS NOT NULL")

    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT id, fact, category, COALESCE(importance, 2) AS importance, embedding, expires_at, created_at, updated_at
        FROM memories
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
        """, tuple(params))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_memories_by_categories(
    user_id: int,
    categories: Optional[List[str]] = None,
    exclude_categories: Optional[List[str]] = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """Lấy danh sách ký ức lọc theo category (phục vụ /status và đối chiếu)."""
    now = time.time()
    clauses = ["user_id = ?", "(expires_at IS NULL OR expires_at > ?)"]
    params: List[Any] = [user_id, now]

    if categories:
        placeholders = ",".join("?" for _ in categories)
        clauses.append(f"COALESCE(category, 'general') IN ({placeholders})")
        params.extend(categories)
    if exclude_categories:
        placeholders = ",".join("?" for _ in exclude_categories)
        clauses.append(f"COALESCE(category, 'general') NOT IN ({placeholders})")
        params.extend(exclude_categories)

    params.append(limit)
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT id, fact, COALESCE(category, 'general') AS category, COALESCE(importance, 2) AS importance, created_at, updated_at
        FROM memories
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
        LIMIT ?
        """, tuple(params))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_all_chat_history(user_id: int) -> List[Dict[str, Any]]:
    """Lấy toàn bộ lịch sử trò chuyện theo thứ tự thời gian để phục vụ backfill."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, role, content, delta_affinity, created_at
        FROM chat_history
        WHERE user_id = ?
        ORDER BY id ASC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_chat_history_since(user_id: int, since_ts: float, end_ts: Optional[float] = None, limit: int = 80) -> List[Dict[str, Any]]:
    """Lấy lịch sử trò chuyện trong một khung thời gian (phục vụ Nightly Memory Audit)."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        if end_ts is not None:
            cursor.execute("""
            SELECT id, role, content, delta_affinity, created_at
            FROM chat_history
            WHERE user_id = ? AND created_at >= ? AND created_at <= ?
            ORDER BY id DESC
            LIMIT ?
            """, (user_id, since_ts, end_ts, limit))
        else:
            cursor.execute("""
            SELECT id, role, content, delta_affinity, created_at
            FROM chat_history
            WHERE user_id = ? AND created_at >= ?
            ORDER BY id DESC
            LIMIT ?
            """, (user_id, since_ts, limit))
        rows = cursor.fetchall()
        rows.reverse()
        return [dict(r) for r in rows]


def get_memories_in_time_range(
    user_id: int,
    start_ts: float,
    end_ts: float,
    categories: Optional[List[str]] = None,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    """Lấy các ký ức phát sinh trong khung thời gian [start_ts, end_ts]."""
    clauses = ["user_id = ?", "created_at >= ?", "created_at <= ?"]
    params: List[Any] = [user_id, start_ts, end_ts]
    if categories:
        placeholders = ",".join("?" for _ in categories)
        clauses.append(f"COALESCE(category, 'general') IN ({placeholders})")
        params.extend(categories)
    params.append(limit)
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT id, fact, COALESCE(category, 'general') AS category, COALESCE(importance, 2) AS importance, created_at, updated_at
        FROM memories
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """, tuple(params))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_unembedded_memories() -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, fact FROM memories WHERE embedding IS NULL")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_memories_for_retrieval(user_id: int, max_episodes: int = 300) -> List[Dict[str, Any]]:
    """
    Lấy dữ liệu cho Vector Search: TOÀN BỘ Fact Book (thường ít) + tối đa `max_episodes`
    episode MỚI NHẤT. Nhờ vậy chi phí chấm điểm cosine không tăng vô hạn theo thời gian.
    """
    now = time.time()
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, fact, category, COALESCE(importance, 2) AS importance, embedding, expires_at, created_at, updated_at
        FROM memories
        WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?) AND COALESCE(category, 'general') <> 'episode'
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
        """, (user_id, now))
        facts = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
        SELECT id, fact, category, COALESCE(importance, 2) AS importance, embedding, expires_at, created_at, updated_at
        FROM memories
        WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?) AND category = 'episode'
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """, (user_id, now, max_episodes))
        episodes = [dict(r) for r in cursor.fetchall()]

    if len(episodes) >= max_episodes:
        logger.debug(f"Retrieval dùng {max_episodes} episode mới nhất (đã cắt bớt để giữ tốc độ).")
    return facts + episodes


def prune_episodes(user_id: int, older_than_days: int = 21, keep_max: int = 400) -> int:
    """
    Dọn episode cũ để kho ký ức không phình vô hạn:
    - Xoá episode mức quan trọng 1 cũ hơn `older_than_days` ngày.
    - Sau đó nếu vẫn còn quá `keep_max`, xoá các episode cũ nhất (ưu tiên imp=1).
    Trả về số dòng đã xoá.
    """
    cutoff = time.time() - older_than_days * 86400
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM memories
            WHERE user_id = ? AND category = 'episode'
              AND COALESCE(importance, 1) <= 1 AND created_at < ?
            """,
            (user_id, cutoff),
        )
        deleted = cursor.rowcount or 0

        cursor.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ? AND category = 'episode'",
            (user_id,),
        )
        total = int(cursor.fetchone()[0])
        if total > keep_max:
            cursor.execute(
                """
                DELETE FROM memories
                WHERE id IN (
                    SELECT id FROM memories
                    WHERE user_id = ? AND category = 'episode'
                    ORDER BY COALESCE(importance, 1) ASC, created_at ASC
                    LIMIT ?
                )
                """,
                (user_id, total - keep_max),
            )
            deleted += cursor.rowcount or 0

    if deleted:
        logger.info(f"🧹 [Prune Episodes]: đã xoá {deleted} episode cũ (còn tối đa {keep_max}).")
    return int(deleted)


def get_memories(user_id: int, limit: int = 25) -> List[str]:
    now = time.time()
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT fact FROM memories 
        WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY id DESC LIMIT ?
        """, (user_id, now, limit))
        rows = cursor.fetchall()
        return [r["fact"] for r in rows]


def reset_user(user_id: int, wipe_all: bool = False):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        if wipe_all:
            cursor.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM sent_photos WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM rejected_memories WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM proactive_decisions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM daily_diary")
            cursor.execute("DELETE FROM daily_trends")
            cursor.execute(
                "DELETE FROM app_metadata WHERE key IN (?, ?)",
                (
                    f"last_voice_ts:{user_id}",
                    f"nightly_audit_last_date:{user_id}",
                ),
            )
        else:
            cursor.execute("DELETE FROM memories WHERE user_id = ? AND expires_at IS NOT NULL", (user_id,))
            # Huỷ mọi nhắc hẹn đang chờ để ván mới không bị tin nhắn cũ chen ngang
            cursor.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            )
        cursor.execute(
            "DELETE FROM app_metadata WHERE key IN (?, ?, ?, ?, ?)",
            (
                f"session_state:{user_id}",
                f"recall_ledger:{user_id}",
                f"open_loops:{user_id}",
                f"jiwen_state:{user_id}",
                f"wake_lottery:{user_id}",
            ),
        )
        now = time.time()
        cursor.execute("""
        UPDATE user_state
        SET affinity_score = 88, mood = 'ngọt ngào', cooldown_until = 0.0, 
            last_user_message_time = ?, last_bot_message_time = ?, status = 'lovers',
            tease_level = ?, stubborn_level = ?, clingy_level = ?,
            mood_updated_at = ?, next_proactive_check_at = 0.0, unanswered_proactive_count = 0
        WHERE user_id = ?
        """, (
            now, now,
            PERSONALITY_DEFAULTS["tease_level"],
            PERSONALITY_DEFAULTS["stubborn_level"],
            PERSONALITY_DEFAULTS["clingy_level"],
            now, user_id,
        ))


def get_today_trends(date_str: str) -> Optional[str]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT trends_text FROM daily_trends WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        return row["trends_text"] if row else None


def save_daily_trends(date_str: str, trends_text: str):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO daily_trends (date, trends_text, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET trends_text = excluded.trends_text, updated_at = excluded.updated_at
        """, (date_str, trends_text, time.time()))


def get_today_diary(date_str: str) -> Optional[str]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT diary_text FROM daily_diary WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        return row["diary_text"] if row else None


def save_today_diary(date_str: str, diary_text: str):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO daily_diary (date, diary_text, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET diary_text = excluded.diary_text, created_at = excluded.created_at
        """, (date_str, diary_text, time.time()))


def record_sent_photo(user_id: int, photo_key: str):
    """Ghi nhận một bức ảnh đã được gửi cho người dùng."""
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO sent_photos (user_id, photo_key, sent_at)
        VALUES (?, ?, ?)
        """, (user_id, photo_key, time.time()))


def get_recently_sent_photos(user_id: int, within_seconds: float = 172800) -> List[str]:
    """Lấy danh sách các ảnh đã gửi cho user trong khoảng thời gian (mặc định 48 giờ)."""
    cutoff = time.time() - within_seconds
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT photo_key FROM sent_photos
        WHERE user_id = ? AND sent_at >= ?
        ORDER BY sent_at DESC
        """, (user_id, cutoff))
        rows = cursor.fetchall()
        return [r["photo_key"] for r in rows]


def get_recently_sent_photo_times(user_id: int, within_seconds: float = 172800) -> Dict[str, float]:
    """Trả về thời điểm gửi gần nhất {photo_key: latest_sent_at} trong khoảng within_seconds."""
    cutoff = time.time() - within_seconds
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT photo_key, MAX(sent_at) AS last_sent_at
        FROM sent_photos
        WHERE user_id = ? AND sent_at >= ?
        GROUP BY photo_key
        """, (user_id, cutoff))
        rows = cursor.fetchall()
        return {r["photo_key"]: float(r["last_sent_at"]) for r in rows if r["photo_key"]}


def get_last_sent_photo_time(user_id: int) -> float:
    """Lấy timestamp của bức ảnh gần nhất gửi cho user, hoặc 0.0 nếu chưa từng gửi."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT sent_at FROM sent_photos
        WHERE user_id = ?
        ORDER BY sent_at DESC LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        return float(row["sent_at"]) if row else 0.0


_VALID_REMINDER_KINDS = ("user_reminder", "ai_promise", "ai_followup")


def _normalize_reminder_kind(kind: str) -> str:
    clean = str(kind or "user_reminder").strip().lower()
    return clean if clean in _VALID_REMINDER_KINDS else "user_reminder"


def create_reminder(
    user_id: int,
    kind: str,
    title: str,
    due_at: float,
    context_note: str = "",
) -> int:
    """
    Tạo mới một lịch nhắc hẹn ('user_reminder'), tự giữ lời hứa ('ai_promise') hoặc lịch tự hỏi thăm ('ai_followup').
    - Với 'ai_followup', tự động hủy follow-up cũ đang chờ để tránh chồng chéo.
    - Với 'ai_promise', gộp/cập nhật nếu đã có lời hứa trùng chủ đề hoặc mới tạo trong vòng 3 phút.
    """
    clean_kind = _normalize_reminder_kind(kind)
    default_title = "Nhắn báo lại cho anh" if clean_kind == "ai_promise" else "Nhắn hỏi thăm"
    clean_title = (title or "").strip()[:200] or default_title
    clean_note = (context_note or "").strip()[:300]
    now = time.time()

    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        if clean_kind == "ai_followup":
            cursor.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE user_id = ? AND kind = 'ai_followup' AND status = 'pending'",
                (user_id,),
            )
        elif clean_kind == "ai_promise":
            cursor.execute(
                """
                SELECT id, title, created_at FROM reminders
                WHERE user_id = ? AND kind = 'ai_promise' AND status = 'pending'
                ORDER BY id DESC LIMIT 8
                """,
                (user_id,),
            )
            for row in cursor.fetchall():
                same_topic = _topics_match(str(row["title"] or ""), clean_title)
                same_turn = abs(float(row["created_at"] or 0.0) - now) <= 180.0
                if same_topic or same_turn:
                    cursor.execute(
                        "UPDATE reminders SET title = ?, due_at = ?, context_note = ? WHERE id = ?",
                        (clean_title, float(due_at), clean_note, row["id"]),
                    )
                    logger.info(f"⏰ [Promise DEDUPE #{row['id']}]: cập nhật '{clean_title}'")
                    return int(row["id"])
        else:
            # Chống trùng: cùng nội dung và giờ hẹn sát nhau thì cập nhật dòng cũ thay vì tạo thêm.
            cursor.execute(
                """
                SELECT id FROM reminders
                WHERE user_id = ? AND kind = 'user_reminder' AND status = 'pending'
                  AND lower(trim(title)) = lower(trim(?)) AND abs(due_at - ?) <= 600
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, clean_title, float(due_at)),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    "UPDATE reminders SET due_at = ?, context_note = ? WHERE id = ?",
                    (float(due_at), clean_note, existing["id"]),
                )
                logger.info(f"⏰ [Reminder DEDUPE #{existing['id']}]: '{clean_title}' (đã có lịch tương tự)")
                return int(existing["id"])
        cursor.execute(
            """
            INSERT INTO reminders (user_id, kind, title, context_note, due_at, orig_due_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, clean_kind, clean_title, clean_note, float(due_at), float(due_at), now),
        )
        return int(cursor.lastrowid)


def get_pending_reminders(
    user_id: int,
    kind: Optional[str] = None,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Lấy danh sách các lịch hẹn/follow-up đang ở trạng thái 'pending'."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        if kind:
            cursor.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'pending' AND kind = ?
                ORDER BY due_at ASC, id ASC LIMIT ?
                """,
                (user_id, kind, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'pending'
                ORDER BY due_at ASC, id ASC LIMIT ?
                """,
                (user_id, limit),
            )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_due_reminders(user_id: int, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """Lấy các lịch đã tới hạn (due_at <= now_ts), ưu tiên user_reminder -> ai_promise -> ai_followup."""
    now = now_ts if now_ts is not None else time.time()
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM reminders
            WHERE user_id = ? AND status = 'pending' AND due_at <= ?
            ORDER BY CASE kind WHEN 'user_reminder' THEN 0 WHEN 'ai_promise' THEN 1 ELSE 2 END, due_at ASC, id ASC
            """,
            (user_id, now),
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_reminder_by_id(user_id: int, reminder_id: int) -> Optional[Dict[str, Any]]:
    """Lấy một bản ghi reminder cụ thể thuộc user để kiểm tra trạng thái mới nhất."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
            (int(reminder_id), user_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def update_reminder(
    user_id: int,
    reminder_id: int,
    due_at: Optional[float] = None,
    title: Optional[str] = None,
    context_note: Optional[str] = None,
    status: Optional[str] = None,
    sent_at: Optional[float] = None,
    preserve_orig_due: bool = True,
) -> bool:
    """Cập nhật giờ, nội dung hoặc trạng thái một lịch hẹn theo ID thuộc user."""
    fields = []
    params: List[Any] = []
    if due_at is not None:
        fields.append("due_at = ?")
        params.append(float(due_at))
        if not preserve_orig_due:
            fields.append("orig_due_at = ?")
            params.append(float(due_at))
    if title is not None and title.strip():
        fields.append("title = ?")
        params.append(title.strip()[:200])
    if context_note is not None:
        fields.append("context_note = ?")
        params.append(context_note.strip()[:300])
    if status in ("pending", "sent", "cancelled"):
        fields.append("status = ?")
        params.append(status)
    if sent_at is not None:
        fields.append("sent_at = ?")
        params.append(float(sent_at))
    if not fields:
        return False

    params.extend([reminder_id, user_id])
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE reminders SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            tuple(params),
        )
        return cursor.rowcount > 0


def cancel_pending_ai_followups(user_id: int) -> int:
    """Hủy toàn bộ ai_followup đang chờ của user (giữ nguyên user_reminder)."""
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE user_id = ? AND kind = 'ai_followup' AND status = 'pending'",
            (user_id,),
        )
        return int(cursor.rowcount or 0)


def set_proactive_schedule(user_id: int, next_check_at: float):
    """Lưu mốc thời gian đánh thức AI cân nhắc chủ động tiếp theo."""
    get_or_create_user(user_id)
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_state SET next_proactive_check_at = ? WHERE user_id = ?",
            (float(next_check_at), user_id),
        )


def increment_unanswered_proactive(user_id: int) -> int:
    """Tăng bộ đếm tin chủ động liên tiếp chưa có phản hồi từ anh."""
    user = get_or_create_user(user_id)
    new_cnt = int(user.get("unanswered_proactive_count") or 0) + 1
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_state SET unanswered_proactive_count = ? WHERE user_id = ?",
            (new_cnt, user_id),
        )
    return new_cnt


def get_metadata_dict(prefix: str = "") -> Dict[str, str]:
    """Đọc nhiều khóa cấu hình từ app_metadata theo tiền tố."""
    with _open_conn() as conn:
        cursor = conn.cursor()
        if prefix:
            cursor.execute("SELECT key, value FROM app_metadata WHERE key LIKE ?", (f"{prefix}%",))
        else:
            cursor.execute("SELECT key, value FROM app_metadata")
        rows = cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}


def log_system_event(
    event_type: str,
    level: str,
    title: str,
    detail: str = "",
    max_keep: int = 300,
) -> int:
    """Ghi nhận sự kiện vận hành (watchdog, backup, presence) và tự dọn log quá cũ."""
    now_ts = time.time()
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO system_events (event_type, level, title, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(event_type or "system")[:40],
                str(level or "info")[:20],
                str(title or "")[:200],
                str(detail or "")[:1000],
                now_ts,
            ),
        )
        row_id = int(cursor.lastrowid or 0)
        cursor.execute(
            """
            DELETE FROM system_events
            WHERE id NOT IN (SELECT id FROM system_events ORDER BY id DESC LIMIT ?)
            """,
            (max(50, int(max_keep)),),
        )
        return row_id


def get_system_events(event_type: Optional[str] = None, limit: int = 80) -> List[Dict[str, Any]]:
    """Lấy danh sách sự kiện hệ thống mới nhất cho Web Dashboard."""
    safe_limit = max(1, min(300, int(limit)))
    with _open_conn() as conn:
        cursor = conn.cursor()
        if event_type:
            cursor.execute(
                """
                SELECT id, event_type, level, title, detail, created_at
                FROM system_events
                WHERE event_type = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (event_type, safe_limit),
            )
        else:
            cursor.execute(
                """
                SELECT id, event_type, level, title, detail, created_at
                FROM system_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def list_memories_filtered(
    user_id: int,
    search: str = "",
    category: Optional[str] = None,
    limit: int = 60,
    offset: int = 0,
) -> Dict[str, Any]:
    """Phân trang & lọc danh sách ký ức còn hiệu lực phục vụ Web Dashboard."""
    now = time.time()
    clauses = ["user_id = ?", "(expires_at IS NULL OR expires_at > ?)"]
    params: List[Any] = [user_id, now]

    if category and category != "all":
        clauses.append("COALESCE(category, 'general') = ?")
        params.append(category)
    if search and search.strip():
        clauses.append("fact LIKE ?")
        params.append(f"%{search.strip()}%")

    where_sql = " AND ".join(clauses)
    safe_limit = max(1, min(200, int(limit)))
    safe_offset = max(0, int(offset))
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM memories WHERE {where_sql}", tuple(params))
        total = int(cursor.fetchone()[0])

        cursor.execute(
            f"""
            SELECT id, fact, COALESCE(category, 'general') AS category,
                   COALESCE(importance, 2) AS importance,
                   expires_at, created_at, updated_at,
                   CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END AS has_embedding
            FROM memories
            WHERE {where_sql}
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [safe_limit, safe_offset]),
        )
        items = [dict(r) for r in cursor.fetchall()]
    return {"items": items, "total": total}


def get_chat_history_paginated(
    user_id: int,
    limit: int = 60,
    offset: int = 0,
    search: str = "",
) -> Dict[str, Any]:
    """Lấy lịch sử trò chuyện phân trang (mới nhất trước) cho Web Dashboard."""
    clauses = ["user_id = ?"]
    params: List[Any] = [user_id]
    if search and search.strip():
        clauses.append("content LIKE ?")
        params.append(f"%{search.strip()}%")
    where_sql = " AND ".join(clauses)

    safe_limit = max(1, min(200, int(limit)))
    safe_offset = max(0, int(offset))
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM chat_history WHERE {where_sql}", tuple(params))
        total = int(cursor.fetchone()[0])

        cursor.execute(
            f"""
            SELECT id, role, content, delta_affinity, created_at
            FROM chat_history
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [safe_limit, safe_offset]),
        )
        items = [dict(r) for r in cursor.fetchall()]
    return {"items": items, "total": total}


def get_reminders_all(
    user_id: int,
    status: Optional[str] = None,
    limit: int = 60,
) -> List[Dict[str, Any]]:
    """Lấy danh sách reminders theo user (tùy chọn lọc theo status)."""
    safe_limit = max(1, min(200, int(limit)))
    with _open_conn() as conn:
        cursor = conn.cursor()
        if status and status != "all":
            cursor.execute(
                """
                SELECT id, kind, title, context_note, due_at, status, created_at, sent_at
                FROM reminders
                WHERE user_id = ? AND status = ?
                ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, due_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, status, safe_limit),
            )
        else:
            cursor.execute(
                """
                SELECT id, kind, title, context_note, due_at, status, created_at, sent_at
                FROM reminders
                WHERE user_id = ?
                ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, due_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def delete_reminder(user_id: int, reminder_id: int) -> bool:
    """Xóa hẳn một bản ghi reminder thuộc user trên Dashboard."""
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        return cursor.rowcount > 0


def update_user_profile_manual(
    user_id: int,
    affinity_score: Optional[int] = None,
    mood: Optional[str] = None,
    tease_level: Optional[int] = None,
    stubborn_level: Optional[int] = None,
    clingy_level: Optional[int] = None,
    unanswered_proactive_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Cập nhật trực tiếp chỉ số tình cảm/tính cách từ Web Dashboard (giữ kẹp ngưỡng an toàn)."""
    get_or_create_user(user_id)
    fields: List[str] = []
    params: List[Any] = []
    if affinity_score is not None:
        fields.append("affinity_score = ?")
        params.append(max(0, min(100, int(affinity_score))))
    if mood is not None and str(mood).strip():
        fields.append("mood = ?")
        fields.append("mood_updated_at = ?")
        params.extend([str(mood).strip()[:40], time.time()])
    trait_inputs = {
        "tease_level": tease_level,
        "stubborn_level": stubborn_level,
        "clingy_level": clingy_level,
    }
    for key, val in trait_inputs.items():
        if val is not None:
            lo, hi = PERSONALITY_BOUNDS[key]
            fields.append(f"{key} = ?")
            params.append(max(lo, min(hi, int(val))))
    if unanswered_proactive_count is not None:
        fields.append("unanswered_proactive_count = ?")
        params.append(max(0, int(unanswered_proactive_count)))

    if fields:
        params.append(user_id)
        with _open_conn(commit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE user_state SET {', '.join(fields)} WHERE user_id = ?", tuple(params))
    return get_or_create_user(user_id)


_PLAN_MATCH_STOPWORDS = {
    "anh", "em", "đan", "phát", "đi", "ăn", "uống", "học", "làm", "nấu",
    "nay", "hôm", "tối", "chiều", "sáng", "trưa", "đêm", "của", "với", "cho",
    "về", "nhà", "một", "chút", "tí", "lát", "nữa", "nhé", "nha", "nè",
}
_VALID_CALL_STATUSES = ("none", "planned", "in_call", "ended")
_VALID_PLAN_STATUSES = ("planned", "done", "cancelled")
_VALID_PLAN_KINDS = ("info", "promise")


def _vn_date_str(ts: Optional[float] = None) -> str:
    base_ts = ts if ts and ts > 0 else time.time()
    return datetime.datetime.fromtimestamp(base_ts, tz=VN_TZ).strftime("%Y-%m-%d")


def _vn_hm_str(ts: Optional[float] = None) -> str:
    base_ts = ts if ts and ts > 0 else time.time()
    return datetime.datetime.fromtimestamp(base_ts, tz=VN_TZ).strftime("%H:%M")


def _default_session_state(today_vn: Optional[str] = None) -> Dict[str, Any]:
    return {
        "date": today_vn or _vn_date_str(),
        "user_activity": "",
        "dan_activity": "",
        "call_status": "none",
        "call_note": "",
        "today_plans": [],
        "updated_at": 0.0,
    }


def _normalize_topic_tokens(text: str) -> List[str]:
    clean = re.sub(r"[^\w\s]", " ", str(text or "").lower(), flags=re.UNICODE)
    return [t for t in clean.split() if len(t) >= 2 and t not in _PLAN_MATCH_STOPWORDS]


_CALL_IDLE_TIMEOUT_SEC = 5400.0        # Tự kết thúc 'in_call' sau 90 phút không cập nhật
_ACTIVITY_STALE_CLEAR_SEC = 10800.0   # Tự làm rỗng hoạt động tức thời quá 3 giờ
_USER_SLEEP_STATE_RE = re.compile(
    r"\b(?:ngủ|ngủ\s*nướng|đi\s*ngủ|buồn\s*ngủ|khò)\b",
    re.IGNORECASE,
)
_USER_GOING_TO_SLEEP_RE = re.compile(
    r"\b(?:đi\s*ngủ|ngủ\s*đây|ngủ\s*nha|ngủ\s*nhé|ngủ\s*ngon|ngủ\s*trước|ngủ\s*luôn|khò\s*đây|g9|good\s*night)\b",
    re.IGNORECASE,
)
_ASKING_IF_ASLEEP_RE = re.compile(
    r"\bngủ\s*(?:chưa|rồi\s*(?:à|hả|sao|ha))\b",
    re.IGNORECASE,
)


def _normalize_session_state(raw: Any, today_vn: str, now_ts: float) -> Dict[str, Any]:
    base = _default_session_state(today_vn)
    if not isinstance(raw, dict):
        return base

    saved_date = str(raw.get("date") or "").strip()
    saved_updated = float(raw.get("updated_at") or 0.0)
    age_sec = max(0.0, float(now_ts) - saved_updated) if saved_updated > 0 else 999999.0

    if saved_date and saved_date != today_vn:
        # Sang ngày mới: xóa lịch trình tạm hôm qua; chỉ giữ 'in_call' nếu mới cập nhật < 90 phút trước
        call_st = str(raw.get("call_status") or "none").strip().lower()
        if call_st == "in_call" and saved_updated > 0 and age_sec < _CALL_IDLE_TIMEOUT_SEC:
            base["call_status"] = "in_call"
            base["call_note"] = str(raw.get("call_note") or "").strip()[:180]
            base["updated_at"] = saved_updated
        return base

    if saved_updated > 0 and age_sec >= _ACTIVITY_STALE_CLEAR_SEC:
        base["user_activity"] = ""
        base["dan_activity"] = ""
    else:
        base["user_activity"] = str(raw.get("user_activity") or "").strip()[:180]
        base["dan_activity"] = str(raw.get("dan_activity") or "").strip()[:180]

    c_st = str(raw.get("call_status") or "none").strip().lower()
    base["call_status"] = c_st if c_st in _VALID_CALL_STATUSES else "none"
    base["call_note"] = str(raw.get("call_note") or "").strip()[:180]
    base["updated_at"] = saved_updated

    # Tự kết thúc cuộc gọi nếu cùng ngày nhưng im lặng quá 90 phút hoặc bước sang buổi sáng sau cuộc gọi ban đêm
    if base["call_status"] == "in_call":
        now_dt = datetime.datetime.fromtimestamp(float(now_ts), tz=VN_TZ)
        saved_dt = datetime.datetime.fromtimestamp(saved_updated, tz=VN_TZ) if saved_updated > 0 else now_dt
        overnight_to_morning = (saved_dt.hour < 5) and (now_dt.hour >= 6)
        if age_sec >= _CALL_IDLE_TIMEOUT_SEC or (overnight_to_morning and age_sec >= 2700.0):
            base["call_status"] = "ended"
            base["call_note"] = "Cuộc gọi trước đã kết thúc (tự đóng sau khoảng nghỉ)"

    plans: List[Dict[str, Any]] = []
    for item in (raw.get("today_plans") or [])[:8]:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()[:100]
        if not topic:
            continue
        p_st = str(item.get("status") or "planned").strip().lower()
        p_kind = str(item.get("kind") or "info").strip().lower()
        plans.append({
            "topic": topic,
            "status": p_st if p_st in _VALID_PLAN_STATUSES else "planned",
            "kind": p_kind if p_kind in _VALID_PLAN_KINDS else "info",
            "time_hint": str(item.get("time_hint") or "").strip()[:40],
            "note": str(item.get("note") or "").strip()[:180],
            "updated_at": float(item.get("updated_at") or saved_updated or now_ts),
        })
    base["today_plans"] = plans
    return base


def get_session_state(user_id: int, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Đọc Trạng thái phiên tức thời (Live Session State) của user, tự làm mới khi qua ngày VN."""
    now = now_ts if now_ts is not None else time.time()
    today_vn = _vn_date_str(now)
    raw_json = get_metadata(f"session_state:{user_id}")
    if not raw_json:
        return _default_session_state(today_vn)
    try:
        parsed = json.loads(raw_json)
    except Exception:
        return _default_session_state(today_vn)
    return _normalize_session_state(parsed, today_vn, now)


def save_session_state(user_id: int, state: Dict[str, Any], now_ts: Optional[float] = None) -> Dict[str, Any]:
    now = now_ts if now_ts is not None else time.time()
    today_vn = _vn_date_str(now)
    normalized = _normalize_session_state(state, today_vn, now)
    normalized["date"] = today_vn
    normalized["updated_at"] = float(normalized.get("updated_at") or now)
    set_metadata(f"session_state:{user_id}", json.dumps(normalized, ensure_ascii=False))
    return normalized


def clear_session_state(user_id: int):
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM app_metadata WHERE key = ?", (f"session_state:{user_id}",))


def _find_matching_plan_index(plans: List[Dict[str, Any]], topic: str) -> Optional[int]:
    t_low = str(topic or "").strip().lower()
    if not t_low:
        return None
    t_tokens = set(_normalize_topic_tokens(t_low))
    for idx, p in enumerate(plans):
        p_low = str(p.get("topic") or "").strip().lower()
        if not p_low:
            continue
        if t_low == p_low or t_low in p_low or p_low in t_low:
            return idx
        p_tokens = set(_normalize_topic_tokens(p_low))
        if t_tokens and p_tokens:
            overlap = t_tokens & p_tokens
            if len(overlap) >= 2 or (len(overlap) == 1 and min(len(t_tokens), len(p_tokens)) <= 2 and len(next(iter(overlap))) >= 3):
                return idx
    return None


def clear_stale_user_sleep_state_on_message(
    user_id: int,
    user_text: str = "",
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Khi Chủ nhà chủ động nhắn tin mới:
    - Tự gỡ trạng thái 'đang ngủ / ngủ nướng' cũ (trừ khi Chủ nhà đang nhắn chúc ngủ ngon).
    - Tự đóng 'in_call' nếu Chủ nhà hỏi 'em ngủ chưa' sau khi đã im lặng lâu.
    """
    now = float(now_ts if now_ts is not None else time.time())
    state = get_session_state(user_id, now_ts=now)
    changed = False
    clean_text = (user_text or "").strip()

    u_act = state.get("user_activity") or ""
    if u_act and _USER_SLEEP_STATE_RE.search(u_act):
        is_saying_goodnight = bool(clean_text and _USER_GOING_TO_SLEEP_RE.search(clean_text) and "chưa tới" not in clean_text.lower())
        if not is_saying_goodnight:
            state["user_activity"] = "vừa thức dậy / đang nhắn tin" if "dậy" in clean_text.lower() else ""
            changed = True

    if state.get("call_status") == "in_call" and clean_text and _ASKING_IF_ASLEEP_RE.search(clean_text):
        age_sec = max(0.0, now - float(state.get("updated_at") or 0.0))
        if age_sec >= 1800.0:
            state["call_status"] = "ended"
            state["call_note"] = "Cuộc gọi trước đã kết thúc"
            changed = True

    if changed:
        state["updated_at"] = now
        return save_session_state(user_id, state, now_ts=now)
    return state


def merge_session_state_update(
    base_state: Optional[Dict[str, Any]],
    update: Optional[Dict[str, Any]],
    now_ts: Optional[float] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Hợp nhất cập nhật Live Session State thuần trong bộ nhớ (không ghi DB)."""
    now = now_ts if now_ts is not None else time.time()
    state = dict(base_state or {})
    state["today_plans"] = [dict(p) for p in (state.get("today_plans") or []) if isinstance(p, dict)]
    if not update or not isinstance(update, dict):
        return state, False

    changed = False
    for field in ("user_activity", "dan_activity"):
        val = update.get(field)
        if isinstance(val, str):
            clean = val.strip()
            if clean and clean.lower() not in ("null", "none"):
                state[field] = clean[:180]
                changed = True

    raw_call_st = update.get("call_status")
    if isinstance(raw_call_st, str):
        c_st = raw_call_st.strip().lower()
        if c_st in _VALID_CALL_STATUSES and c_st != state.get("call_status"):
            state["call_status"] = c_st
            changed = True
            if c_st == "in_call" and not (update.get("call_note") and str(update.get("call_note")).strip().lower() not in ("null", "none")):
                state["call_note"] = f"Trợ lýg trong cuộc gọi từ {_vn_hm_str(now)}"

    raw_call_note = update.get("call_note")
    if isinstance(raw_call_note, str):
        c_note = raw_call_note.strip()
        if c_note and c_note.lower() not in ("null", "none"):
            state["call_note"] = c_note[:180]
            changed = True

    plan_updates = update.get("plan_updates")
    if isinstance(plan_updates, list):
        plans = list(state.get("today_plans") or [])
        for pu in plan_updates:
            if not isinstance(pu, dict):
                continue
            topic = str(pu.get("topic") or "").strip()[:100]
            if not topic or topic.lower() in ("null", "none"):
                continue
            p_st = str(pu.get("status") or "planned").strip().lower()
            if p_st not in _VALID_PLAN_STATUSES:
                p_st = "planned"
            p_kind = str(pu.get("kind") or "info").strip().lower()
            if p_kind not in _VALID_PLAN_KINDS:
                p_kind = "info"
            time_hint = str(pu.get("time_hint") or "").strip()[:40]
            if time_hint.lower() in ("null", "none"):
                time_hint = ""
            note = str(pu.get("note") or "").strip()[:180]
            if note.lower() in ("null", "none"):
                note = ""

            match_idx = _find_matching_plan_index(plans, topic)
            if match_idx is not None:
                existing = dict(plans[match_idx])
                existing["status"] = p_st
                # Chỉ giữ 'promise' nếu thực sự là lời hứa, cho phép nâng/hạ khi chỉnh rõ
                existing["kind"] = p_kind if pu.get("kind") else existing.get("kind", "info")
                if time_hint:
                    existing["time_hint"] = time_hint
                if note:
                    existing["note"] = note
                existing["updated_at"] = now
                plans[match_idx] = existing
            else:
                plans.append({
                    "topic": topic,
                    "status": p_st,
                    "kind": p_kind,
                    "time_hint": time_hint,
                    "note": note,
                    "updated_at": now,
                })
            changed = True

        plans.sort(key=lambda x: (0 if x.get("status") == "planned" else 1, -float(x.get("updated_at") or 0.0)))
        state["today_plans"] = plans[:8]

    if changed:
        state["updated_at"] = now
    return state, changed


def apply_session_state_update(
    user_id: int,
    update: Optional[Dict[str, Any]],
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Hợp nhất cập nhật Live Session State từ Pass 1 theo nguyên tắc trạng thái mới nhất thắng."""
    now = now_ts if now_ts is not None else time.time()
    state = get_session_state(user_id, now_ts=now)
    merged, changed = merge_session_state_update(state, update, now_ts=now)
    if changed:
        return save_session_state(user_id, merged, now_ts=now)
    return merged


def format_session_state_for_prompt(
    state: Optional[Dict[str, Any]],
    now_ts: Optional[float] = None,
) -> str:
    """Định dạng Live Session State thành khối ngữ cảnh ưu tiên cao cho Pass 1, Pass 2 và Proactive."""
    if not state or not isinstance(state, dict):
        return ""

    now_val = float(now_ts) if now_ts is not None else time.time()
    age_note = ""
    updated_at = state.get("updated_at")
    try:
        if updated_at:
            updated_ts = float(updated_at)
            age_mins = int((now_val - updated_ts) // 60)
            hm_str = datetime.datetime.fromtimestamp(updated_ts, tz=VN_TZ).strftime("%H:%M")
            if age_mins >= 90:
                age_note = (
                    f" (TRẠNG THÁI CŨ từ {hm_str}, cách đây {age_mins} phút — ĐÃ KHÁ LÂU, "
                    "KHÔNG mặc định việc này vẫn đang diễn ra lúc này)"
                )
            elif age_mins >= 45:
                age_note = (
                    f" (ghi nhận lúc {hm_str}, cách đây {age_mins} phút — "
                    "nếu việc ngắn hạn này đã qua, coi như đã làm xong)"
                )
    except Exception:
        age_note = ""

    lines: List[str] = []
    if state.get("user_activity"):
        lines.append(f"- Chủ nhà hiện đang: {state['user_activity']}{age_note}")
    if state.get("dan_activity"):
        lines.append(f"- Trợ lý hiện đang (ưu tiên hơn Nhật ký ngày): {state['dan_activity']}{age_note}")

    call_status = state.get("call_status") or "none"
    call_note = state.get("call_note") or ""
    if call_status == "in_call":
        lines.append(
            f"- Trạng thái cuộc gọi: ĐANG TRONG CUỘC GỌI ({call_note or 'hai đứa đã bấm gọi nhau rồi'}) "
            "-> TUYỆT ĐỐI KHÔNG rủ 'tí nữa call' hay 'lát gọi sau'."
        )
    elif call_status == "planned":
        lines.append(f"- Trạng thái cuộc gọi: Đã có dự định gọi ({call_note or 'chờ đến giờ'}).")
    elif call_status == "ended":
        lines.append(f"- Trạng thái cuộc gọi: Đã gọi xong ({call_note or 'mới cúp máy'}).")

    plans = state.get("today_plans") or []
    if plans:
        lines.append("- Lịch trình / Dự định trong ngày (cập nhật mới nhất):")
        for p in plans[:6]:
            st = p.get("status", "planned")
            kind = p.get("kind", "info")
            if st == "done":
                badge = "ĐÃ XONG"
            elif st == "cancelled":
                badge = "ĐÃ HỦY"
            elif kind == "promise":
                badge = "KÈO / LỜI HỨA ĐÃ CHỐT"
            else:
                badge = "THÔNG BÁO / DỰ ĐỊNH (chỉ chia sẻ, KHÔNG phải lời hứa)"
            t_hint = f" ({p['time_hint']})" if p.get("time_hint") else ""
            note_txt = f" — {p['note']}" if p.get("note") else ""
            lines.append(f"  • [{badge}] {p.get('topic', '')}{t_hint}{note_txt}")

    if not lines:
        return ""
    return (
        "=== TRẠNG THÁI PHIÊN HIỆN TẠI (ƯU TIÊN CAO NHẤT - GHI ĐÈ NHẬT KÝ / KÝ ỨC CŨ) ===\n"
        + "\n".join(lines)
    )


def _is_call_reminder_title(title: str, note: str = "") -> bool:
    combined = f"{title or ''} {note or ''}".lower()
    if "dậy" in combined and "gọi anh dậy" in combined:
        return False
    return bool(re.search(r"\b(call|cuộc gọi|gọi cho em|gọi tâm sự|gọi điện|lên app gọi)\b", combined))


def reminder_conflicts_with_session_state(rem: Dict[str, Any], session_state: Optional[Dict[str, Any]]) -> bool:
    """True nếu reminder (nhất là ai_followup hoặc nhắc call cũ) đã lỗi thời do xong việc hoặc đang call."""
    if not rem or not session_state:
        return False
    title = str(rem.get("title") or "")
    note = str(rem.get("context_note") or "")
    kind = str(rem.get("kind") or "user_reminder")

    if session_state.get("call_status") == "in_call" and _is_call_reminder_title(title, note):
        return True

    title_tokens = set(_normalize_topic_tokens(f"{title} {note}"))
    if not title_tokens:
        return False

    for p in (session_state.get("today_plans") or []):
        if p.get("status") not in ("done", "cancelled"):
            continue
        p_topic = str(p.get("topic") or "").strip().lower()
        if not p_topic:
            continue
        p_note_low = str(p.get("note") or "").strip().lower()
        # Nếu plan chỉ nói về việc của Chủ nhà đã xong -> không tự hủy lời hứa Trợ lý tự báo lại (vd Trợ lý ăn xong)
        if (
            kind == "ai_promise"
            and any(k in p_note_low for k in ("anh phát", "anh đã", "phát đã"))
            and not any(k in p_note_low for k in ("đan", "em "))
        ):
            continue
        if p_topic in f"{title} {note}".lower():
            return True
        p_tokens = set(_normalize_topic_tokens(p_topic))
        if p_tokens and len(p_tokens & title_tokens) >= min(2, len(p_tokens)):
            if kind in ("ai_followup", "ai_promise") or len(p_tokens & title_tokens) >= 2:
                return True
    return False


def reconcile_reminders_with_session_state(user_id: int, session_state: Optional[Dict[str, Any]]) -> List[int]:
    """Tự động hủy các reminder cũ mâu thuẫn với trạng thái phiên vừa cập nhật (đã xong / đang call)."""
    if not session_state:
        return []
    pending = get_pending_reminders(user_id, limit=25)
    cancelled_ids: List[int] = []
    for rem in pending:
        if reminder_conflicts_with_session_state(rem, session_state):
            rid = int(rem["id"])
            if update_reminder(user_id, rid, status="cancelled"):
                cancelled_ids.append(rid)
                logger.info(f"⏰ [Reminder RECONCILED #{rid}]: hủy lịch '{rem.get('title')}' do trạng thái phiên đã cập nhật")
    return cancelled_ids


# ==============================================================================
# ĐỢT 1 / ĐỢT 2 / ĐỢT 3 COMPANION HELPERS
# (Rejection Ledger, Recall Ledger, Open Loops, Night Dreams, Proactive Engine,
#  LAN PC Now Playing, Journal Aggregator)
# ==============================================================================

def _get_json_meta(key: str, default: Any = None) -> Any:
    raw = get_metadata(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _set_json_meta(key: str, value: Any) -> None:
    set_metadata(key, json.dumps(value, ensure_ascii=False))


# --- 1. Rejection Ledger ---

_REJECT_DATE_PREFIX_RE = re.compile(r"^\[\d{2,4}[-/]\d{1,2}(?:[-/]\d{1,4})?\]\s*")
_REJECT_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_memory_fingerprint(text: str) -> str:
    """Chuẩn hóa câu ký ức thành vân tay so sánh không phân biệt dấu câu / khoảng trắng."""
    s = str(text or "").strip().lower()
    s = _REJECT_DATE_PREFIX_RE.sub("", s)
    s = _REJECT_PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def reject_memory(user_id: int, fact: str, source: str = "manual") -> bool:
    norm = normalize_memory_fingerprint(fact)
    if not norm or len(norm) < 3:
        return False
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO rejected_memories (user_id, norm_text, raw_fact, source, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, norm_text) DO UPDATE SET
            raw_fact = excluded.raw_fact,
            source = excluded.source,
            created_at = excluded.created_at
        """, (user_id, norm, str(fact or "").strip()[:300], source, time.time()))
    return True


def unreject_memory(user_id: int, fact: str) -> int:
    norm = normalize_memory_fingerprint(fact)
    if not norm:
        return 0
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM rejected_memories WHERE user_id = ? AND norm_text = ?",
            (user_id, norm),
        )
        return cursor.rowcount


def unreject_memory_by_id(user_id: int, rejection_id: int) -> bool:
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM rejected_memories WHERE user_id = ? AND id = ?",
            (user_id, int(rejection_id)),
        )
        return cursor.rowcount > 0


def list_rejected_memories(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, user_id, norm_text, raw_fact, source, created_at
        FROM rejected_memories
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """, (user_id, max(1, int(limit))))
        return [dict(r) for r in cursor.fetchall()]


_NEGATION_TOKENS = frozenset({"không", "hông", "chưa", "chẳng", "đừng", "hết", "ngừng"})


def is_memory_rejected(user_id: int, candidate_fact: str) -> bool:
    """True nếu candidate khớp chính xác hoặc đồng nghĩa cao (>=85% containment) với ký ức từng bị xóa."""
    norm = normalize_memory_fingerprint(candidate_fact)
    if not norm or len(norm) < 3:
        return False
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT norm_text FROM rejected_memories WHERE user_id = ?",
            (user_id,),
        )
        rows = [r["norm_text"] for r in cursor.fetchall() if r["norm_text"]]

    cand_tokens = set(norm.split())
    cand_neg = bool(cand_tokens & _NEGATION_TOKENS)
    for rej in rows:
        if rej == norm:
            return True
        rej_tokens = set(rej.split())
        rej_neg = bool(rej_tokens & _NEGATION_TOKENS)
        if cand_neg != rej_neg:
            continue
        if len(cand_tokens) >= 4 and len(rej_tokens) >= 4:
            inter = len(cand_tokens & rej_tokens)
            union = len(cand_tokens | rej_tokens)
            min_len = min(len(cand_tokens), len(rej_tokens))
            if (union > 0 and (inter / union) >= 0.78) or (min_len > 0 and (inter / min_len) >= 0.85):
                return True
    return False


# --- 2. Recall Ledger ---

_RECALL_LEDGER_MAX_TURNS = 6


def record_recalled_memories(
    user_id: int,
    memory_ids: List[int],
    now_ts: Optional[float] = None,
) -> None:
    clean_ids = [int(mid) for mid in (memory_ids or []) if mid]
    if not clean_ids:
        return
    now = float(now_ts if now_ts is not None else time.time())
    key = f"recall_ledger:{user_id}"
    turns = _get_json_meta(key, [])
    if not isinstance(turns, list):
        turns = []
    # Loại bỏ bản ghi quá cũ (> 3 giờ) và chỉ giữ 6 lượt gần nhất
    turns = [t for t in turns if isinstance(t, dict) and (now - float(t.get("ts") or 0)) < 10800]
    turns.append({"ids": clean_ids[:16], "ts": now})
    turns = turns[-_RECALL_LEDGER_MAX_TURNS:]
    _set_json_meta(key, turns)


def get_recall_penalties(
    user_id: int,
    max_age_sec: float = 7200.0,
    now_ts: Optional[float] = None,
) -> Dict[int, float]:
    """Trả map {memory_id: penalty} giảm điểm tạm thời các ký ức vừa nhắc gần đây."""
    now = float(now_ts if now_ts is not None else time.time())
    turns = _get_json_meta(f"recall_ledger:{user_id}", [])
    if not isinstance(turns, list) or not turns:
        return {}
    recent = [
        t for t in turns
        if isinstance(t, dict) and 0 <= (now - float(t.get("ts") or 0)) <= max_age_sec
    ]
    penalties: Dict[int, float] = {}
    for rank, turn in enumerate(reversed(recent)):
        p = 0.12 if rank == 0 else (0.08 if rank <= 2 else 0.05)
        for mid in (turn.get("ids") or []):
            try:
                iid = int(mid)
            except (TypeError, ValueError):
                continue
            if p > penalties.get(iid, 0.0):
                penalties[iid] = p
    return penalties


# --- 3. Open Loops ---

_OPEN_LOOP_MAX_ITEMS = 8
_OPEN_LOOP_TTL_SEC = 7 * 86400


def get_open_loops(
    user_id: int,
    include_resolved: bool = False,
    now_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    now = float(now_ts if now_ts is not None else time.time())
    raw = _get_json_meta(f"open_loops:{user_id}", [])
    if not isinstance(raw, list):
        return []
    loops = [
        item for item in raw
        if isinstance(item, dict)
        and (now - float(item.get("updated_at") or item.get("created_at") or 0)) <= _OPEN_LOOP_TTL_SEC
    ]
    if not include_resolved:
        loops = [l for l in loops if l.get("status", "open") == "open"]
    loops.sort(key=lambda x: float(x.get("updated_at") or x.get("created_at") or 0), reverse=True)
    return loops


_LOOP_STOPWORDS = {
    "cho", "anh", "em", "dan", "linh", "phat", "cua", "va", "voi", "ve",
    "xem", "coi", "lam", "xong", "nhe", "nha", "nay", "kia", "do", "day",
    "roi", "dang", "se", "di", "o", "tai", "trong", "khi", "nao", "bao",
    "nhung", "cac", "mot", "an", "ngay", "hom", "mai",
}


def _fold_loop_token(token: str) -> str:
    import unicodedata
    decomposed = unicodedata.normalize("NFD", token.lower().replace("đ", "d").replace("Đ", "d"))
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _topics_match(a: str, b: str) -> bool:
    na = normalize_memory_fingerprint(a)
    nb = normalize_memory_fingerprint(b)
    if not na or not nb:
        return False
    if na == nb or (len(na) >= 4 and na in nb) or (len(nb) >= 4 and nb in na):
        return True
    ta = {_fold_loop_token(t) for t in na.split() if t}
    tb = {_fold_loop_token(t) for t in nb.split() if t}
    ca = {t for t in ta if t not in _LOOP_STOPWORDS} or ta
    cb = {t for t in tb if t not in _LOOP_STOPWORDS} or tb
    if ca and cb:
        shared = ca & cb
        if len(shared) / max(1, min(len(ca), len(cb))) >= 0.6:
            return True
        if len(shared) == 1 and min(len(ca), len(cb)) <= 2 and any(len(tok) >= 5 for tok in shared):
            return True
    if len(ta) >= 2 and len(tb) >= 2:
        return len(ta & tb) / max(1, len(ta | tb)) >= 0.6
    return False


def _infer_loop_target_info(item: Dict[str, Any]) -> tuple[Optional[float], str]:
    """Lấy (target_ts, target_label) từ open_loop hoặc tự suy ra an toàn cho loop cũ."""
    raw_t = item.get("target_ts")
    raw_lbl = str(item.get("target_label") or "").strip()
    if raw_t:
        try:
            t_val = float(raw_t)
            if t_val > 0:
                lbl = raw_lbl or datetime.datetime.fromtimestamp(t_val, tz=VN_TZ).strftime("%H:%M %d/%m")
                return t_val, lbl
        except (TypeError, ValueError):
            pass

    created = float(item.get("created_at") or item.get("updated_at") or 0.0)
    if created <= 0:
        return None, ""
    try:
        import diary_helper
        created_dt = datetime.datetime.fromtimestamp(created, tz=VN_TZ)
        combined = f"{item.get('topic', '')} {item.get('note', '')}"
        cands = diary_helper.extract_vn_clock_candidates(
            combined, created_dt, assume_same_day=(created_dt.hour < 5)
        )
        if len(cands) == 1:
            t_dt = cands[0][0]
            return t_dt.timestamp(), t_dt.strftime("%H:%M %d/%m")
    except Exception:
        pass
    return None, ""


def upsert_open_loop(
    user_id: int,
    topic: str,
    note: str = "",
    now_ts: Optional[float] = None,
    target_ts: Optional[float] = None,
    target_label: str = "",
) -> Optional[Dict[str, Any]]:
    clean_topic = str(topic or "").strip()[:100]
    clean_note = str(note or "").strip()[:240]
    if len(clean_topic) < 3:
        return None
    now = float(now_ts if now_ts is not None else time.time())
    all_loops = get_open_loops(user_id, include_resolved=True, now_ts=now)

    clean_t_ts: Optional[float] = None
    if target_ts is not None:
        try:
            t_val = float(target_ts)
            if t_val > 0:
                clean_t_ts = t_val
        except (TypeError, ValueError):
            clean_t_ts = None
    clean_t_lbl = str(target_label or "").strip()[:40]
    if clean_t_ts and not clean_t_lbl:
        clean_t_lbl = datetime.datetime.fromtimestamp(clean_t_ts, tz=VN_TZ).strftime("%H:%M %d/%m")

    found = None
    for item in all_loops:
        if _topics_match(item.get("topic", ""), clean_topic):
            item["topic"] = clean_topic
            if clean_note:
                item["note"] = clean_note
            if clean_t_ts is not None:
                item["target_ts"] = clean_t_ts
                item["target_label"] = clean_t_lbl
            item["status"] = "open"
            item["updated_at"] = now
            found = item
            break

    if not found:
        lid = f"ol_{int(now)}_{abs(hash(clean_topic)) % 10000}"
        found = {
            "id": lid,
            "topic": clean_topic,
            "note": clean_note or clean_topic,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "last_surfaced_at": 0.0,
        }
        if clean_t_ts is not None:
            found["target_ts"] = clean_t_ts
            found["target_label"] = clean_t_lbl
        all_loops.insert(0, found)

    # Giữ tối đa _OPEN_LOOP_MAX_ITEMS (ưu tiên giữ open trước, mới trước)
    all_loops.sort(
        key=lambda x: (0 if x.get("status", "open") == "open" else 1, -float(x.get("updated_at") or 0))
    )
    all_loops = all_loops[:_OPEN_LOOP_MAX_ITEMS]
    _set_json_meta(f"open_loops:{user_id}", all_loops)
    return found


def resolve_open_loop(
    user_id: int,
    topic: str,
    now_ts: Optional[float] = None,
) -> bool:
    clean_topic = str(topic or "").strip()
    if not clean_topic:
        return False
    now = float(now_ts if now_ts is not None else time.time())
    all_loops = get_open_loops(user_id, include_resolved=True, now_ts=now)
    changed = False
    for item in all_loops:
        if item.get("status", "open") == "open" and _topics_match(item.get("topic", ""), clean_topic):
            item["status"] = "resolved"
            item["updated_at"] = now
            changed = True
    if changed:
        _set_json_meta(f"open_loops:{user_id}", all_loops)
    return changed


def resolve_open_loop_by_id(
    user_id: int,
    loop_id: str,
    now_ts: Optional[float] = None,
) -> bool:
    now = float(now_ts if now_ts is not None else time.time())
    all_loops = get_open_loops(user_id, include_resolved=True, now_ts=now)
    changed = False
    for item in all_loops:
        if str(item.get("id")) == str(loop_id):
            item["status"] = "resolved"
            item["updated_at"] = now
            changed = True
            break
    if changed:
        _set_json_meta(f"open_loops:{user_id}", all_loops)
    return changed


def resolve_open_loops_for_promise(
    user_id: int,
    title: str,
    note: str = "",
    now_ts: Optional[float] = None,
) -> int:
    """Đánh dấu đã hoàn tất các open_loop trùng lời hứa Trợ lý vừa thực hiện xong."""
    now = float(now_ts if now_ts is not None else time.time())
    all_loops = get_open_loops(user_id, include_resolved=True, now_ts=now)
    if not all_loops:
        return 0
    try:
        import commitment_helper
        p_topic, _, _, _ = commitment_helper._detect_activity_profile(f"{title or ''} {note or ''}")
    except Exception:
        p_topic = ""
        commitment_helper = None

    resolved_cnt = 0
    for item in all_loops:
        if item.get("status", "open") != "open":
            continue
        l_topic = str(item.get("topic") or "")
        l_note = str(item.get("note") or "")
        matched = _topics_match(l_topic, title)
        if not matched and commitment_helper is not None and p_topic and p_topic != "khi xong việc":
            l_act, _, _, _ = commitment_helper._detect_activity_profile(f"{l_topic} {l_note}")
            is_dan_checkin = any(k in f"{l_topic} {l_note}".lower() for k in ("em hứa", "báo cáo", "nhắn lại", "nhắn cho anh", "em báo"))
            if l_act == p_topic and is_dan_checkin:
                matched = True
        if matched:
            item["status"] = "resolved"
            item["updated_at"] = now
            resolved_cnt += 1
    if resolved_cnt:
        _set_json_meta(f"open_loops:{user_id}", all_loops)
    return resolved_cnt


def get_eligible_open_loops(
    user_id: int,
    limit: int = 2,
    cooldown_sec: float = 7200.0,
    min_age_sec: float = 0.0,
    now_ts: Optional[float] = None,
    respect_target_time: bool = False,
) -> List[Dict[str, Any]]:
    now = float(now_ts if now_ts is not None else time.time())
    loops = get_open_loops(user_id, include_resolved=False, now_ts=now)
    out: List[Dict[str, Any]] = []
    for item in loops:
        last_surf = float(item.get("last_surfaced_at") or 0.0)
        created = float(item.get("created_at") or 0.0)
        if last_surf > 0 and (now - last_surf) < cooldown_sec:
            continue
        if min_age_sec > 0 and (now - created) < min_age_sec:
            continue
        if respect_target_time:
            t_ts, _ = _infer_loop_target_info(item)
            if t_ts and now < (t_ts - 120.0):
                continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def mark_open_loops_surfaced(
    user_id: int,
    loop_ids: List[str],
    now_ts: Optional[float] = None,
) -> None:
    if not loop_ids:
        return
    now = float(now_ts if now_ts is not None else time.time())
    id_set = {str(x) for x in loop_ids}
    all_loops = get_open_loops(user_id, include_resolved=True, now_ts=now)
    changed = False
    for item in all_loops:
        if str(item.get("id")) in id_set:
            item["last_surfaced_at"] = now
            changed = True
    if changed:
        _set_json_meta(f"open_loops:{user_id}", all_loops)


def format_open_loops_for_prompt(
    user_id: int,
    limit: int = 2,
    cooldown_sec: float = 7200.0,
    mark_surfaced: bool = False,
    now_ts: Optional[float] = None,
    respect_target_time: bool = False,
) -> str:
    now = float(now_ts if now_ts is not None else time.time())
    loops = get_eligible_open_loops(
        user_id,
        limit=limit,
        cooldown_sec=cooldown_sec,
        now_ts=now,
        respect_target_time=respect_target_time,
    )
    if not loops:
        return ""
    if mark_surfaced:
        mark_open_loops_surfaced(user_id, [str(l["id"]) for l in loops], now_ts=now)

    import diary_helper
    lines = ["=== CHUYỆN CÒN ĐANG BỎ NGỎ (PHẢI KIỂM TRA ĐÚNG MỐC THỜI GIAN TRƯỚC KHI NHẮC) ==="]
    for item in loops:
        created_ts = float(item.get("created_at") or item.get("updated_at") or 0.0)
        created_lbl = diary_helper.format_relative_vn_ts(created_ts, now_ts=now) if created_ts else ""
        meta_parts = [f"tạo lúc {created_lbl}"] if created_lbl else []

        t_ts, _ = _infer_loop_target_info(item)
        if t_ts:
            t_hm = datetime.datetime.fromtimestamp(t_ts, tz=VN_TZ).strftime("%H:%M %d/%m")
            diff_sec = t_ts - now
            if diff_sec > 120.0:
                meta_parts.append(f"CHƯA TỚI HẠN {t_hm} — còn {diary_helper.format_vn_duration(diff_sec)}")
            elif diff_sec >= -120.0:
                meta_parts.append(f"ĐANG ĐÚNG MỐC {t_hm}")
            else:
                meta_parts.append(f"ĐÃ QUA MỐC {t_hm} được {diary_helper.format_vn_duration(-diff_sec)}")

        tag = f" [{' | '.join(meta_parts)}]" if meta_parts else ""
        lines.append(f"- {item['topic']}{tag}: {item.get('note') or item['topic']}")
    return "\n".join(lines)


# --- 5. Proactive Engine State & Arbiter Logs ---

def get_jiwen_state(user_id: int) -> Dict[str, Any]:
    raw = _get_json_meta(f"jiwen_state:{user_id}")
    if isinstance(raw, dict):
        return raw
    return {}


def save_jiwen_state(user_id: int, state: Dict[str, Any]) -> None:
    _set_json_meta(f"jiwen_state:{user_id}", state)


def get_wake_lottery_state(user_id: int) -> Dict[str, Any]:
    raw = _get_json_meta(f"wake_lottery:{user_id}")
    if isinstance(raw, dict):
        return raw
    return {}


def save_wake_lottery_state(user_id: int, state: Dict[str, Any]) -> None:
    _set_json_meta(f"wake_lottery:{user_id}", state)


def record_proactive_decision(
    user_id: int,
    decision: str,
    reason: str = "",
    category: str = "",
    score: float = 0.0,
    details: Optional[Dict[str, Any]] = None,
    now_ts: Optional[float] = None,
) -> None:
    now = float(now_ts if now_ts is not None else time.time())
    det_json = json.dumps(details or {}, ensure_ascii=False)
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO proactive_decisions (user_id, decision, reason, category, score, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            str(decision)[:40],
            str(reason)[:240],
            str(category)[:60],
            float(score or 0.0),
            det_json[:1000],
            now,
        ))
        cursor.execute("""
        DELETE FROM proactive_decisions
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM proactive_decisions WHERE user_id = ? ORDER BY created_at DESC LIMIT 200
        )
        """, (user_id, user_id))


def get_proactive_decisions(user_id: int, limit: int = 35) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, user_id, decision, reason, category, score, details_json, created_at
        FROM proactive_decisions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """, (user_id, max(1, int(limit))))
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            try:
                d["details"] = json.loads(d.pop("details_json") or "{}")
            except Exception:
                d["details"] = {}
            rows.append(d)
        return rows


def count_proactive_sent_today(user_id: int, now_ts: Optional[float] = None) -> int:
    now = float(now_ts if now_ts is not None else time.time())
    vn_now = datetime.datetime.fromtimestamp(now, tz=VN_TZ)
    start_of_day = vn_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT COUNT(*) AS cnt FROM proactive_decisions
        WHERE user_id = ? AND decision = 'sent' AND created_at >= ?
        """, (user_id, start_of_day))
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0


# --- 6. LAN PC Now Playing Bridge ---

_META_PC_NOW_PLAYING = "lan_pc_now_playing"
_META_ESP32_DHT11 = "esp32_dht11"


def set_esp32_sensor_data(
    temp_c: float,
    humidity_pct: float,
    rssi: Optional[int] = None,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    now = float(now_ts if now_ts is not None else time.time())
    data = {
        "temperature_c": round(float(temp_c), 1),
        "humidity_pct": round(float(humidity_pct), 1),
        "rssi": int(rssi) if rssi is not None else None,
        "updated_at": now,
    }
    _set_json_meta(_META_ESP32_DHT11, data)
    return data


def get_esp32_sensor_data(max_age_sec: float = 1800.0, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    now = float(now_ts if now_ts is not None else time.time())
    data = _get_json_meta(_META_ESP32_DHT11)
    if not isinstance(data, dict):
        return None
    updated_at = float(data.get("updated_at") or 0.0)
    if updated_at <= 0 or (now - updated_at) > max_age_sec:
        return None
    if data.get("temperature_c") is None or data.get("humidity_pct") is None:
        return None
    return data


def set_pc_now_playing(payload: Dict[str, Any], pc_ip: str = "", now_ts: Optional[float] = None) -> Dict[str, Any]:
    now = float(now_ts if now_ts is not None else time.time())
    status_raw = str(payload.get("playback_status") or payload.get("status") or "").strip().lower()
    title = str(payload.get("title") or "").strip()[:140]
    artist = str(payload.get("artist") or "").strip()[:100]
    album = str(payload.get("album") or "").strip()[:100]
    app_name = str(payload.get("app_name") or payload.get("app") or "").strip()[:60]
    active = bool(title) and status_raw in ("playing", "playing_media", "active", "open", "")
    data = {
        "active": active,
        "title": title,
        "artist": artist,
        "album": album,
        "app_name": app_name,
        "playback_status": status_raw or ("playing" if title else "stopped"),
        "pc_ip": pc_ip,
        "updated_at": now,
    }
    _set_json_meta(_META_PC_NOW_PLAYING, data)
    return data


def get_pc_now_playing(max_age_sec: float = 360.0, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    now = float(now_ts if now_ts is not None else time.time())
    data = _get_json_meta(_META_PC_NOW_PLAYING)
    if not isinstance(data, dict):
        return None
    updated_at = float(data.get("updated_at") or 0.0)
    if (now - updated_at) > max_age_sec:
        return None
    if not data.get("active") or not data.get("title"):
        return None
    return data


def clear_pc_now_playing_if_stale(force_pc_offline: bool = False, max_age_sec: float = 360.0) -> None:
    data = _get_json_meta(_META_PC_NOW_PLAYING)
    if not isinstance(data, dict) or not data.get("active"):
        return
    now = time.time()
    if force_pc_offline or (now - float(data.get("updated_at") or 0.0)) > max_age_sec:
        data["active"] = False
        data["playback_status"] = "offline" if force_pc_offline else "stale"
        data["updated_at"] = now
        _set_json_meta(_META_PC_NOW_PLAYING, data)


def format_now_playing_for_prompt(now_ts: Optional[float] = None) -> str:
    parts: List[str] = []

    try:
        import esp32_audio
        jukebox_line = esp32_audio.get_jukebox_status_for_prompt()
        if jukebox_line:
            parts.append(jukebox_line)
    except Exception:
        pass

    np = get_pc_now_playing(now_ts=now_ts)
    if np:
        artist = f" — {np['artist']}" if np.get("artist") else ""
        app_str = f" ({np['app_name']})" if np.get("app_name") else ""
        parts.append(
            f"[CHỦ NHÀ ĐANG NGHE NHẠC TRÊN MÁY TÍNH: "
            f"\"{np['title']}\"{artist}{app_str} — nhắc khéo tự nhiên nếu đúng gu Indie/chill, "
            f"TUYỆT ĐỐI KHÔNG nhắc tới LAN/API/IP/trình bridge.]"
        )

    dht = get_esp32_sensor_data(max_age_sec=1800.0, now_ts=now_ts)
    if dht:
        parts.append(
            f"[CẢM BIẾN CẢM BIẾN PHÒNG (DHT11): Nhiệt độ phòng hiện tại {dht['temperature_c']}°C, "
            f"Độ ẩm {dht['humidity_pct']}% — biết ngầm để quan tâm nóng/lạnh/hanh khô tự nhiên nếu hợp mạch chuyện, "
            f"TUYỆT ĐỐI KHÔNG nhắc tới ESP32/DHT11/cảm biến/API.]"
        )

    return "\n".join(parts)


_ROOM_DEVICE_TRIGGER_RE = re.compile(
    r"\b(quạt|fan|đèn|bóng\s*đèn|thông\s*gió|hút\s*mùi|ổ\s*cắm|thiết\s*bị|"
    r"phòng|nhà|về|ra\s*ngoài|đi\s*vắng|đi\s*đâu|nóng|lạnh|oi|ngợp|bí|mát|ấm|"
    r"tối|sáng|chói|ngủ|thức|hz|mức|bật|tắt|vặn|hạ|tăng|giảm)\b",
    re.IGNORECASE,
)


def should_include_room_devices_context(text: str, has_media: bool = False) -> bool:
    """
    Chỉ nạp trạng thái Quạt / Ổ cắm BroadLink / Hiện diện khi:
      - Lượt gửi có Voice/Ảnh (chưa biết trước nội dung audio/hình), hoặc
      - Tin nhắn chạm từ khóa thiết bị, thời tiết phòng, đi ngủ hoặc đi vắng/về nhà.
    """
    if has_media:
        return True
    raw = (text or "").strip()
    if not raw:
        return False
    if _ROOM_DEVICE_TRIGGER_RE.search(raw) or re.search(r"\d+\s*%", raw):
        return True
    try:
        import smart_plugs
        low = raw.lower()
        for p in smart_plugs.load_plugs_config():
            for al in p.get("aliases") or [p.get("name", "").lower()]:
                if al and re.search(rf"(?<!\w){re.escape(al)}(?!\w)", low):
                    return True
    except Exception:
        pass
    return False


def _format_presence_summary(pres: Dict[str, Any], now_ts: Optional[float] = None) -> str:
    st = str(pres.get("state") or "unconfigured")
    if st == "unconfigured":
        return ""
    if st == "home_pc":
        return "Ở phòng (PC đang bật, điện thoại có mặt)"
    if st == "home_idle":
        return "Ở phòng (điện thoại đang kết nối Wi-Fi)"
    if st == "away":
        away_since = float(pres.get("away_since") or 0.0)
        now_v = float(now_ts if now_ts is not None else time.time())
        mins = max(1, int((now_v - away_since) / 60.0)) if away_since > 0 else 0
        return f"Trợ lýg ra ngoài ({mins} phút rồi)" if mins > 0 else "Trợ lýg ra ngoài"
    if st == "sleeping_late":
        return "Khuya ở phòng (điện thoại còn Wi-Fi)" if pres.get("phone_online") else "Khuya (điện thoại đang offline)"
    return ""


def _format_fan_summary(fan_snap: Dict[str, Any], include_detail: bool = True) -> str:
    if not fan_snap or not fan_snap.get("configured"):
        return ""
    st = fan_snap.get("state")
    if not fan_snap.get("online") or not isinstance(st, dict):
        return "Quạt phòng: Ngoại tuyến / chưa phản hồi"
    if st.get("is_on"):
        if not include_detail:
            return f"Quạt phòng BẬT {st['speed_pct']}%"
        extra = ""
        if fan_snap.get("ramping"):
            extra = " (đang giảm dần về 0%)"
        elif (fan_snap.get("automation") or {}).get("sleep_prompt_status") == "waiting":
            extra = " (đã hỏi ngủ, chờ anh trả lời)"
        return f"Quạt phòng: Trợ lýg BẬT {st['speed_pct']}% ({st['output_freq']} Hz), chế độ {st.get('mode_label', 'Cố định')}{extra}"
    return f"Quạt phòng: Trợ lýg TẮT (nhớ mức {st.get('speed_pct', 0)}%)" if include_detail else "Quạt phòng TẮT"


def _format_plugs_summary(plugs_snap: List[Dict[str, Any]], include_detail: bool = True) -> List[str]:
    out: List[str] = []
    for p in plugs_snap or []:
        name = str(p.get("name") or "Ổ cắm").strip()
        if p.get("locked"):
            st_txt = "Trợ lýg khóa LAN"
        elif not p.get("online") or p.get("is_on") is None:
            st_txt = "Ngoại tuyến"
        else:
            st_txt = "Trợ lýg BẬT" if p["is_on"] else "Trợ lýg TẮT"
        tm = p.get("timer") if include_detail else None
        tm_txt = ""
        if isinstance(tm, dict) and tm.get("remaining_sec"):
            mins = max(1, int((int(tm["remaining_sec"]) + 59) // 60))
            tm_txt = f" (hẹn {'bật' if tm.get('action') == 'on' else 'tắt'} sau {mins}p)"
        out.append(f"{name}: {st_txt}{tm_txt}" if include_detail else f"{name} {'BẬT' if p.get('is_on') else ('TẮT' if p.get('is_on') is False else 'Ngoại tuyến')}")
    return out


def format_compact_proactive_room_context(now_ts: Optional[float] = None) -> str:
    """Bản tóm tắt 1 dòng từ cache RAM (0ms) cho lượt Trợ lý chủ động nhắn (`evaluate_proactive_turn`)."""
    segs: List[str] = []
    try:
        import lan_presence
        pres_txt = _format_presence_summary(lan_presence.get_presence_snapshot(), now_ts=now_ts)
        if pres_txt:
            segs.append(pres_txt)
    except Exception:
        pass
    try:
        import fan_controller
        fan_txt = _format_fan_summary(fan_controller.get_controller().get_status_snapshot(), include_detail=False)
        if fan_txt:
            segs.append(fan_txt)
    except Exception:
        pass
    try:
        import smart_plugs
        segs.extend(_format_plugs_summary(smart_plugs.get_controller().get_status_snapshot(), include_detail=False))
    except Exception:
        pass
    if not segs:
        return ""
    return "[Bối cảnh phòng lúc này (tham chiếu nhanh, giữ tự nhiên): " + " | ".join(segs) + "]"


async def build_live_room_devices_context(
    *,
    refresh: bool = True,
    max_age_sec: float = 5.0,
    timeout_sec: float = 1.3,
    now_ts: Optional[float] = None,
) -> str:
    """
    Làm mới nhanh (~1s) trạng thái thực tế của Quạt BLDC + Ổ cắm BroadLink nếu cache cũ (>5s)
    và ghép với Hiện diện LAN thành block ngữ cảnh chân thực cho AI Pass 1/2/3.
    """
    import asyncio
    now_v = float(now_ts if now_ts is not None else time.time())
    refresh_tasks = []

    fan_ctrl = None
    fan_snap: Dict[str, Any] = {}
    try:
        import fan_controller
        fan_ctrl = fan_controller.get_controller()
        fan_snap = fan_ctrl.get_status_snapshot()
        if refresh and fan_snap.get("configured"):
            last_at = float(fan_snap.get("last_state_at") or 0.0)
            if (now_v - last_at) > max_age_sec:
                refresh_tasks.append(fan_ctrl.get_state(refresh=True))
    except Exception:
        fan_ctrl = None

    plug_ctrl = None
    plugs_snap: List[Dict[str, Any]] = []
    try:
        import smart_plugs
        plug_ctrl = smart_plugs.get_controller()
        plugs_snap = plug_ctrl.get_status_snapshot()
        if refresh and plugs_snap:
            any_stale = any((now_v - float(p.get("checked_at") or 0.0)) > max_age_sec for p in plugs_snap)
            if any_stale:
                refresh_tasks.append(plug_ctrl.refresh_all())
    except Exception:
        plug_ctrl = None

    if refresh_tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*refresh_tasks, return_exceptions=True), timeout=timeout_sec)
        except Exception:
            pass
        if fan_ctrl is not None:
            fan_snap = fan_ctrl.get_status_snapshot()
        if plug_ctrl is not None:
            plugs_snap = plug_ctrl.get_status_snapshot()

    lines: List[str] = []
    try:
        import lan_presence
        pres_txt = _format_presence_summary(lan_presence.get_presence_snapshot(), now_ts=now_v)
        if pres_txt:
            lines.append(f"- Hiện diện: {pres_txt}")
    except Exception:
        pass

    fan_txt = _format_fan_summary(fan_snap, include_detail=True)
    if fan_txt:
        lines.append(f"- {fan_txt}")

    for p_txt in _format_plugs_summary(plugs_snap, include_detail=True):
        lines.append(f"- {p_txt}")

    if not lines:
        return ""

    return (
        "[TRẠNG THÁI THIẾT BỊ & PHÒNG LÚC NÀY:\n"
        + "\n".join(lines)
        + "\nQuy tắc trả lời: Trả lời tự nhiên đúng phong cách trợ lý (chỉ nêu % hoặc Hz của quạt khi chủ nhà hỏi rõ mức/tần số; "
        "nếu thiết bị ghi 'Ngoại tuyến' thì báo thật là chưa thấy phản hồi, TUYỆT ĐỐI KHÔNG tự bịa trạng thái hay nhắc từ kỹ thuật như ESP-01S/BroadLink/IP/MAC/API/UDP/EEPROM).]"
    )


# --- 7. Journal Aggregator ---

_WEEKDAY_VI = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")


def get_journal_days(user_id: int, limit_days: int = 14) -> List[Dict[str, Any]]:
    """Gom nhật ký từng ngày (daily_diary + memories + sent_photos + chat_count)."""
    days: Dict[str, Dict[str, Any]] = {}

    def _ensure_day(d_str: str) -> Dict[str, Any]:
        if d_str not in days:
            weekday_vi = ""
            try:
                dt = datetime.datetime.strptime(d_str, "%Y-%m-%d")
                weekday_vi = _WEEKDAY_VI[dt.weekday()]
            except Exception:
                pass
            days[d_str] = {
                "date": d_str,
                "weekday_vi": weekday_vi,
                "daily_diary": "",
                "memories": [],
                "photos": [],
                "chat_count": 0,
            }
        return days[d_str]

    cutoff = time.time() - (max(1, limit_days) * 86400)
    with _open_conn() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT date, diary_text FROM daily_diary ORDER BY date DESC LIMIT ?", (limit_days,))
        for r in cursor.fetchall():
            entry = _ensure_day(r["date"])
            entry["daily_diary"] = r["diary_text"] or ""

        cursor.execute("""
        SELECT id, fact, category, created_at FROM memories
        WHERE user_id = ? AND created_at >= ?
        ORDER BY created_at DESC LIMIT 250
        """, (user_id, cutoff))
        for r in cursor.fetchall():
            d_str = datetime.datetime.fromtimestamp(float(r["created_at"]), tz=VN_TZ).strftime("%Y-%m-%d")
            _ensure_day(d_str)["memories"].append({
                "id": r["id"],
                "fact": r["fact"],
                "category": r["category"] or "general",
                "time_vn": datetime.datetime.fromtimestamp(float(r["created_at"]), tz=VN_TZ).strftime("%H:%M"),
            })

        cursor.execute("""
        SELECT photo_key, sent_at FROM sent_photos
        WHERE user_id = ? AND sent_at >= ?
        ORDER BY sent_at DESC LIMIT 120
        """, (user_id, cutoff))
        for r in cursor.fetchall():
            d_str = datetime.datetime.fromtimestamp(float(r["sent_at"]), tz=VN_TZ).strftime("%Y-%m-%d")
            _ensure_day(d_str)["photos"].append({
                "photo_key": r["photo_key"],
                "time_vn": datetime.datetime.fromtimestamp(float(r["sent_at"]), tz=VN_TZ).strftime("%H:%M"),
            })

        cursor.execute("""
        SELECT created_at FROM chat_history
        WHERE user_id = ? AND created_at >= ?
        """, (user_id, cutoff))
        for r in cursor.fetchall():
            d_str = datetime.datetime.fromtimestamp(float(r["created_at"]), tz=VN_TZ).strftime("%Y-%m-%d")
            _ensure_day(d_str)["chat_count"] += 1

    sorted_days = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    return sorted_days[:limit_days]


# ==================== DEVICE EVENTS & LEARNED HABITS ====================

def insert_device_event(
    device_type: str,
    target: str,
    action: str,
    *,
    value_num: Optional[float] = None,
    value_text: str = "",
    source: str = "user_dashboard",
    hour_vn: int,
    minute_vn: int,
    weekday: int,
    date_vn: str,
    temp_c: Optional[float] = None,
    humidity_pct: Optional[float] = None,
    presence_state: str = "",
    created_at: float,
    prune_before_ts: Optional[float] = None,
) -> int:
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO device_events (
                device_type, target, action, value_num, value_text, source,
                hour_vn, minute_vn, weekday, date_vn, temp_c, humidity_pct,
                presence_state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_type, target, action, value_num, str(value_text or ""),
                source, int(hour_vn), int(minute_vn), int(weekday), date_vn,
                temp_c, humidity_pct, str(presence_state or ""), float(created_at),
            ),
        )
        row_id = int(cursor.lastrowid or 0)
        if prune_before_ts is not None and (row_id % 50 == 0):
            cursor.execute("DELETE FROM device_events WHERE created_at < ?", (float(prune_before_ts),))
        return row_id


def get_device_events_since(since_ts: float, sources: Optional[List[str]] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        if sources:
            placeholders = ",".join("?" for _ in sources)
            cursor.execute(
                f"SELECT * FROM device_events WHERE created_at >= ? AND source IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
                [float(since_ts), *sources, int(limit)],
            )
        else:
            cursor.execute(
                "SELECT * FROM device_events WHERE created_at >= ? ORDER BY created_at ASC LIMIT ?",
                (float(since_ts), int(limit)),
            )
        return [dict(r) for r in cursor.fetchall()]


def get_recent_device_events(
    device_type: str,
    target: str,
    since_ts: float,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM device_events
            WHERE device_type = ? AND lower(target) = lower(?) AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (device_type, target, float(since_ts), int(limit)),
        )
        return [dict(r) for r in cursor.fetchall()]


def list_learned_habits(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            cursor.execute(
                f"SELECT * FROM learned_habits WHERE status IN ({placeholders}) ORDER BY confidence DESC, evidence_days DESC, updated_at DESC",
                list(statuses),
            )
        else:
            cursor.execute(
                "SELECT * FROM learned_habits ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'learning' THEN 1 ELSE 2 END, confidence DESC, updated_at DESC"
            )
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("action_meta_json") or "{}")
            except Exception:
                d["meta"] = {}
            rows.append(d)
        return rows


def get_learned_habit(habit_key: str) -> Optional[Dict[str, Any]]:
    with _open_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM learned_habits WHERE habit_key = ?", (habit_key,))
        r = cursor.fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("action_meta_json") or "{}")
        except Exception:
            d["meta"] = {}
        return d


def upsert_learned_habit(
    *,
    habit_key: str,
    pattern_type: str,
    device_type: str,
    target: str,
    action: str,
    action_value: Optional[float] = None,
    meta: Optional[Dict[str, Any]] = None,
    title: str,
    source: str = "behavior",
    status: str = "learning",
    confidence: float = 0.0,
    evidence_days: int = 0,
    occurrences: int = 0,
    last_seen_at: float = 0.0,
    now_ts: Optional[float] = None,
    preserve_muted: bool = True,
) -> Dict[str, Any]:
    now = float(now_ts if now_ts is not None else time.time())
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM learned_habits WHERE habit_key = ?", (habit_key,))
        existing = cursor.fetchone()
        if existing:
            ex = dict(existing)
            final_status = ex["status"] if (preserve_muted and ex.get("status") == "muted") else status
            neg_cnt = int(ex.get("negative_count") or 0)
            # Khi user từng bấm ngược lại trước đó, giữ hình phạt confidence (không để miner auto-tăng vượt mức khóa)
            final_conf = max(0.0, min(0.99, float(confidence) - 0.15 * neg_cnt)) if source == "behavior" else float(confidence)
            if final_conf < 0.45 and final_status == "active" and source == "behavior":
                final_status = "learning"
            cursor.execute(
                """
                UPDATE learned_habits SET
                    pattern_type = ?, device_type = ?, target = ?, action = ?,
                    action_value = ?, action_meta_json = ?, title = ?, source = ?,
                    status = ?, confidence = ?, evidence_days = ?, occurrences = ?,
                    last_seen_at = ?, updated_at = ?
                WHERE habit_key = ?
                """,
                (
                    pattern_type, device_type, target, action,
                    action_value, meta_json, title, source,
                    final_status, round(final_conf, 3), int(evidence_days), int(occurrences),
                    float(last_seen_at), now, habit_key,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO learned_habits (
                    habit_key, pattern_type, device_type, target, action,
                    action_value, action_meta_json, title, source, status,
                    confidence, evidence_days, occurrences, negative_count,
                    last_seen_at, last_triggered_at, last_triggered_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0.0, '', ?)
                """,
                (
                    habit_key, pattern_type, device_type, target, action,
                    action_value, meta_json, title, source, status,
                    round(max(0.0, min(0.99, float(confidence))), 3),
                    int(evidence_days), int(occurrences), float(last_seen_at), now,
                ),
            )
    return get_learned_habit(habit_key) or {}


def update_habit_runtime(
    habit_key: str,
    *,
    status: Optional[str] = None,
    confidence: Optional[float] = None,
    negative_count: Optional[int] = None,
    last_triggered_at: Optional[float] = None,
    last_triggered_date: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    now = float(now_ts if now_ts is not None else time.time())
    sets: List[str] = ["updated_at = ?"]
    vals: List[Any] = [now]
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if confidence is not None:
        sets.append("confidence = ?")
        vals.append(round(max(0.0, min(0.99, float(confidence))), 3))
    if negative_count is not None:
        sets.append("negative_count = ?")
        vals.append(int(negative_count))
    if last_triggered_at is not None:
        sets.append("last_triggered_at = ?")
        vals.append(float(last_triggered_at))
    if last_triggered_date is not None:
        sets.append("last_triggered_date = ?")
        vals.append(str(last_triggered_date))
    vals.append(habit_key)
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE learned_habits SET {', '.join(sets)} WHERE habit_key = ?", vals)
    return get_learned_habit(habit_key)


def delete_learned_habit(habit_key_or_id: Any) -> bool:
    key_str = str(habit_key_or_id or "").strip()
    if not key_str:
        return False
    with _open_conn(commit=True) as conn:
        cursor = conn.cursor()
        if key_str.isdigit():
            cursor.execute("DELETE FROM learned_habits WHERE id = ?", (int(key_str),))
        else:
            cursor.execute("DELETE FROM learned_habits WHERE habit_key = ?", (key_str,))
        return cursor.rowcount > 0







