# -*- coding: utf-8 -*-
"""
Giữ tham chiếu mạnh tới các task nền.

Python chỉ giữ tham chiếu yếu tới Task trong event loop, nên một
asyncio.create_task(...) không được lưu lại có thể bị garbage collector thu hồi
giữa chừng (task biến mất, tin nhắn không bao giờ được gửi).
"""
import asyncio
from typing import Dict, Set

_BACKGROUND_TASKS: Set[asyncio.Task] = set()
_DAEMON_TASKS: Set[asyncio.Task] = set()
_USER_LOCKS: Dict[int, asyncio.Lock] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    """Lấy asyncio.Lock theo user_id để xếp hàng tuần tự từng lượt hội thoại / gửi tin."""
    lock = _USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_LOCKS[user_id] = lock
    return lock


def spawn(coro, daemon: bool = False) -> asyncio.Task:
    """Tạo task nền và giữ tham chiếu cho tới khi task kết thúc."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    if daemon:
        _DAEMON_TASKS.add(task)
        task.add_done_callback(_DAEMON_TASKS.discard)
    return task


def pending_count() -> int:
    return len(_BACKGROUND_TASKS)


async def shutdown(timeout_sec: float = 4.0):
    """Huỷ các daemon loop và chờ ngắn các task ghi nhớ / gửi tin hoàn tất trước khi tắt bot."""
    for t in list(_DAEMON_TASKS):
        if not t.done():
            t.cancel()
    if _DAEMON_TASKS:
        await asyncio.gather(*list(_DAEMON_TASKS), return_exceptions=True)

    transient = [t for t in list(_BACKGROUND_TASKS) if not t.done()]
    if transient:
        _, pending = await asyncio.wait(transient, timeout=timeout_sec)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
