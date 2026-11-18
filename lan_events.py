# -*- coding: utf-8 -*-
"""
Hub sự kiện thời gian thực (Pub/Sub siêu nhẹ trong bộ nhớ) phục vụ SSE Dashboard
và đẩy thông báo tại chỗ không phụ thuộc PC.

Lưu ý thread-safety: `publish()` có thể được gọi từ worker thread (ví dụ backup chạy
trong asyncio.to_thread). asyncio.Queue KHÔNG thread-safe, nên khi phát từ thread khác
phải chuyển sang event loop bằng `loop.call_soon_threadsafe`.
"""
import time
import asyncio
import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SUBSCRIBERS = 8
MAX_QUEUE_SIZE = 50
MAX_RECENT_NOTIFICATIONS = 30

# Danh sách CÓ THỨ TỰ (cũ nhất ở đầu) để biết chính xác subscriber nào cần loại bỏ.
_SUBSCRIBERS: List[asyncio.Queue] = []
_RECENT_NOTIFICATIONS: deque = deque(maxlen=MAX_RECENT_NOTIFICATIONS)
_NOTIFY_SEQ = 0
_NOTIFY_LOCK = threading.Lock()
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def subscribe() -> asyncio.Queue:
    """Đăng ký 1 kết nối SSE mới; giới hạn tối đa MAX_SUBSCRIBERS để bảo vệ RAM."""
    global _LOOP
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass

    while len(_SUBSCRIBERS) >= MAX_SUBSCRIBERS:
        oldest = _SUBSCRIBERS.pop(0)
        _close_subscriber(oldest)

    q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    _SUBSCRIBERS.append(q)
    return q


def _close_subscriber(q: asyncio.Queue):
    """Đẩy sentinel None để vòng lặp SSE của subscriber bị loại tự thoát (tránh task treo vĩnh viễn)."""
    try:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(None)
    except Exception:
        pass


def unsubscribe(q: asyncio.Queue):
    """Hủy đăng ký kết nối SSE."""
    try:
        _SUBSCRIBERS.remove(q)
    except ValueError:
        pass


def _deliver(payload: Dict[str, Any], queues: Optional[List[asyncio.Queue]] = None):
    """Ghi payload vào từng queue; phải chạy trên event loop thread."""
    for q in (queues if queues is not None else list(_SUBSCRIBERS)):
        try:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)
        except Exception:
            continue


def publish(event_type: str, data: Dict[str, Any]):
    """Phát một sự kiện tới toàn bộ Dashboard đang mở (an toàn cả khi gọi từ thread khác)."""
    if not _SUBSCRIBERS:
        return
    payload = {
        "type": str(event_type),
        "data": data,
        "ts": time.time(),
    }
    queues = list(_SUBSCRIBERS)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Đang ở worker thread (backup, export...) -> chuyển về event loop
        loop = _LOOP
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(_deliver, payload, queues)
        except RuntimeError:
            return
        return

    _deliver(payload, queues)


def emit_notify(
    title: str,
    body: str,
    level: str = "info",
    source: str = "system",
    speak_tts: bool = False,
) -> Dict[str, Any]:
    """Lưu ring buffer & phát thông báo tại chỗ (Toast / Web Notification / Audio)."""
    global _NOTIFY_SEQ
    with _NOTIFY_LOCK:
        _NOTIFY_SEQ += 1
        seq_id = _NOTIFY_SEQ
        item = {
            "id": seq_id,
            "title": str(title or "")[:120],
            "body": str(body or "")[:500],
            "level": str(level or "info"),
            "source": str(source or "system"),
            "speak_tts": bool(speak_tts),
            "created_at": time.time(),
        }
        _RECENT_NOTIFICATIONS.appendleft(item)
    publish("local_notify", item)
    return item


def get_recent_notifications(limit: int = 25) -> List[Dict[str, Any]]:
    """Lấy danh sách thông báo tại chỗ gần nhất."""
    safe_limit = max(1, min(MAX_RECENT_NOTIFICATIONS, int(limit)))
    return list(_RECENT_NOTIFICATIONS)[:safe_limit]
