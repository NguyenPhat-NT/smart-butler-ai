# AGENTS.md

Hướng dẫn cho coding agent làm việc trong repository `smart_butler`.

## Kiến trúc tổng quan

- **Entrypoint:** `bot.py` khởi chạy `aiogram` polling cùng các background loop (`lan_presence`, `lan_watchdog`, `fan_controller`, `habit_engine`, `scheduler`) và Web Server + MCP endpoint (`web_dashboard.py` cổng `8080`).
- **AI Pipeline (`gemini_client.py`, `character_bible.py`, `persona.py`):**
  - 3-Pass pipeline (`Pass 1` Strategy/MCP `fetch_tools` -> `Pass 2` Polisher -> `Pass 3` Logic Audit).
  - Toàn bộ danh tính/xưng hô đọc động từ `persona.get_persona_config()`. Không hardcode tên người, địa danh riêng hay dữ liệu cá nhân vào code/prompt.
- **MCP (`mcp_tools.py`):** Registry 11 tool dùng chung cho `fetch_tools` nội bộ và route `POST /mcp` JSON-RPC 2.0 (Bearer auth dùng `DASHBOARD_PIN`).
- **Tự học thói quen (`habit_engine.py`):** Thuần Python, 0 gọi thêm Gemini API; đọc `device_events`, sinh `learned_habits`, thực thi có safety gate và negative feedback cooldown.
- **Database (`database.py`):** SQLite với context manager `_open_conn(commit=...)`.

## Ràng buộc kỹ thuật bắt buộc

1. **Python 3.9+ compatibility:** Luôn dùng `Optional[...]`, `List[...]`, `Dict[...]`, `Tuple[...]` từ `typing` thay cho cú pháp `X | Y`.
2. **Bảo mật & Quyền riêng tư:** Tuyệt đối không commit khóa API, mật khẩu Wi-Fi, địa chỉ MAC/IP thật hoặc seed thiết bị cứng (`DEFAULT_SEED_PLUGS = []` trong `smart_plugs.py`).
3. **Database discipline:** Mọi truy vấn SQLite mới trong `database.py` phải mở qua `with _open_conn(...) as conn:`.
4. **Kiểm tra trước khi kết thúc:**
   ```bash
   python3 -m compileall -q .
   python3 test_smart_butler.py -v
   ```
