# 🤖 Smart Butler AI & Smart Home Hub

Trợ lý AI Quản gia cá nhân 24/7 kiêm trung tâm điều khiển nhà thông minh chạy nhẹ trên máy biên (Linux SBC, OpenStick ARM64, Raspberry Pi hoặc Windows/PC) với Telegram Bot, Web Dashboard nội bộ, giao thức **MCP (Model Context Protocol)**, **Bộ tự học thói quen thiết bị**, và bộ nhớ Vector dài hạn.

## ✨ Tính năng nổi bật

- **🎭 Persona cấu hình linh hoạt (`persona.py`):**
  - Chuyển đổi nhanh giữa 3 phong cách: `butler` (Quản gia lịch thiệp), `assistant` (Trợ lý súc tích) và `companion` (Bạn đồng hành thân thiện).
  - Tùy chỉnh tên Bot (`BUTLER_NAME`), tên chủ nhà (`OWNER_NAME`), thành phố thời tiết (`WEATHER_CITY`) qua `.env` hoặc trực tiếp trên tab **Persona & Hồ sơ** của Web Dashboard mà không cần khởi động lại.
- **🧠 Pipeline AI 3-Pass + Bộ nhớ Vector SQLite:**
  - **Pass 1 (Chiến lược):** Đọc ngữ cảnh, trạng thái phiên, tự quyết định gọi **MCP `fetch_tools`** khi cần tra cứu thiết bị, hoặc tra cứu Web / nhắc việc.
  - **Pass 2 (Chuốt câu trả lời):** Tạo câu trả lời tự nhiên theo persona đã chọn; hỗ trợ gửi **Voice Note** qua TTS Server trên PC.
  - **Pass 3 (Logic Audit):** Rà soát mâu thuẫn thời gian/trạng thái trước khi gửi (fail-open).
  - **Vector Memory:** Lưu trữ Fact Book lâu dài + Episodic Memory với Gemini Embedding `512`-dim, tự động kết tinh lúc `23:30` hằng ngày.
- **🔌 MCP Server tích hợp sẵn (`mcp_tools.py` + `POST /mcp`):**
  - Chuẩn **JSON-RPC 2.0** (`initialize`, `tools/list`, `tools/call`, `ping`) tại `http://<HOST_IP>:8080/mcp` (bảo vệ bằng `Bearer <DASHBOARD_PIN>`).
  - Cho phép cả AI nội bộ lẫn IDE/Desktop client bên ngoài (Antigravity, Cursor, Claude Desktop) đọc trạng thái & điều khiển trực tiếp 11 tools:
    - Đọc: `get_fan_state`, `get_plugs_state`, `get_presence_state`, `get_room_climate`, `get_speaker_state`, `get_room_overview`, `get_learned_habits`
    - Điều khiển: `set_fan`, `set_plug`, `control_speaker`, `manage_learned_habit`
- **🔄 Bộ tự học thói quen ngầm (`habit_engine.py`, 0 Gemini call phụ):**
  - Ghi nhận thao tác thật từ người dùng (`device_events`), tự phát hiện 4 nhóm thói quen: **đèn theo khung giờ**, **hạn mức thời gian tắt quạt thông gió**, **bật/tắt quạt theo nhiệt độ DHT11**, và **đồng bộ theo PC bật/tắt**.
  - Cơ chế an toàn: chỉ chạy khi đủ độ tin cậy, kiểm tra hiện diện Wi-Fi (`lan_presence.py`) & giờ ngủ, tự hạ điểm / tạm khóa nếu chủ nhà thao tác ngược lại trong 10 phút sau khi tự chạy.
- **🌐 Web Dashboard nội bộ (`http://<IP>:8080`):**
  - Bảo vệ bằng mã PIN + HMAC cookie, cập nhật realtime qua **SSE (`/api/events`)**.
  - Quản lý: Thiết bị Smart Home, Persona, Ký ức Vector, Lịch hẹn, Nhịp chủ động, Thói quen tự học, Sao lưu/Khôi phục DB (`.db.gz`) và LAN Watchdog.
- **🔧 Tích hợp phần cứng tùy chọn (Plug & Play):**
  - **Ổ cắm BroadLink SPmini (`smart_plugs.py`):** Điều khiển qua UDP AES-128-CBC không cần Cloud ngoài hay thư viện bên thứ 3; bấm **Quét LAN** trên Dashboard để nhận diện và đặt tên thiết bị.
  - **Quạt BLDC Wi-Fi (`firmware/esp01s_bldc_fan/`):** Firmware ESP-01S điều khiển xung 1005–3000 Hz êm ái, có bước lùi giờ ngủ (`sleep_ramp`).
  - **Loa Wi-Fi + Cảm biến phòng (`firmware/esp32_c3_speaker/`):** ESP32-C3 stream nhạc Opus + WebSocket, phát giọng nói TTS, tích hợp cảm biến nhiệt/ẩm DHT11, nút chạm, EQ/DSP (`esp32_dsp.py`), phiên Tập trung / Chợp mắt / Báo thức.
  - **Cầu nối PC tùy chọn (`tools/`):** `pc_media_bridge.py` (đồng bộ nhạc đang phát trên Windows) và `music_download_server.py` (hỗ trợ tải nhạc LAN cho máy biên ít RAM).

## 📋 Yêu cầu hệ thống

- Python `>= 3.9` (tối ưu cho thiết bị biên RAM thấp như OpenStick ARM64 / Raspberry Pi).
- 1 Telegram Bot Token từ `@BotFather`.
- Ít nhất 1 Google Gemini API Key từ Google AI Studio (`https://aistudio.google.com/apikey`).

## 🚀 Cài đặt nhanh

1. **Tạo môi trường ảo & cài thư viện:**
   ```bash
   python3 -m venv venv
   # Linux / macOS:
   source venv/bin/activate
   # Windows PowerShell:
   # .\venv\Scripts\Activate.ps1

   pip install -r requirements.txt
   ```

2. **Tạo file `.env` từ mẫu:**
   ```bash
   cp .env.example .env
   ```
   Điền các thông số tối thiểu trong `.env`:
   - `BOT_TOKEN`: Token từ BotFather
   - `GEMINI_API_KEY` (hoặc `GEMINI_API_KEYS` nhiều key cách nhau dấu phẩy)
   - `ALLOWED_USER_ID`: Telegram User ID của bạn (nếu để `0`, bot sẽ tự khóa vào tài khoản đầu tiên gửi `/start`)
   - `BUTLER_NAME`, `OWNER_NAME`, `BUTLER_STYLE`, `WEATHER_CITY`: Tùy chỉnh nhân vật quản gia (cũng có thể chỉnh sau trên Dashboard)
   - `DASHBOARD_PIN`: Đặt mã PIN cho Web Dashboard và Bearer Auth `/mcp`

3. **Chạy Bot:**
   ```bash
   python3 bot.py
   ```

4. **Cài chạy nền bằng systemd (Linux):**
   Điều chỉnh đường dẫn `WorkingDirectory` / `ExecStart` trong `smart_butler.service` nếu cần (mặc định `/opt/smart_butler`), sau đó:
   ```bash
   sudo cp smart_butler.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now smart_butler
   sudo journalctl -u smart_butler -f
   ```

## 💬 Các lệnh Telegram chính

- `/start` hoặc `/help` — Giới thiệu trợ lý & danh sách lệnh
- `/status` — Xem nhanh trạng thái phòng (DHT11, Quạt, Ổ cắm, Loa), thói quen, việc đang mở và Fact Book
- `/habits` — Xem các thói quen thiết bị AI đã học
- `/music [tên bài]` — Phát nhạc trên loa ESP32-C3 (`/skip`, `/stop`, `/vol`, `/eq`, `/focus`, `/nap`, `/alarm`)
- `/trend` — Xem thời tiết khu vực `WEATHER_CITY` và tin tức nổi bật hôm nay
- `/review` — Tổng kết hội thoại, thói quen và lịch hẹn gần đây
- `/dashboard` — Xem URL Web Dashboard, URL endpoint `/mcp` và mã PIN hiện tại
- `/backup` — Nén bản sao lưu SQLite `.db.gz` và gửi ngay lên Telegram
- `/reset` — Dọn lịch sử chat gần đây (giữ Fact Book & cấu hình thiết bị)
- `/reset_all` — Xóa trắng dữ liệu chat & ký ức để bắt đầu lại

## 🔌 Kết nối MCP từ PC / IDE ngoài

Endpoint mặc định: `http://<IP_MÁY_CHẠY_BOT>:8080/mcp`  
Header xác thực: `Authorization: Bearer <DASHBOARD_PIN>`

Ví dụ cấu hình `mcp_config.json`:
```json
{
  "mcpServers": {
    "smart-butler": {
      "serverUrl": "http://192.168.1.100:8080/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_DASHBOARD_PIN"
      }
    }
  }
}
```

## 🧪 Kiểm thử nội bộ

Toàn bộ test chạy offline (không gọi mạng thật):
```bash
python3 -m compileall -q .
python3 test_smart_butler.py -v
```

## 📁 Cấu trúc thư mục chính

- `bot.py` — Điểm khởi chạy Telegram Bot & các tác vụ nền
- `config.py`, `persona.py` — Cấu hình môi trường & nhân vật Quản gia
- `gemini_client.py`, `character_bible.py` — Pipeline AI 3-Pass & prompt quản gia
- `database.py`, `vector_memory.py` — SQLite schema, Vector Embeddings, Fact Book & `device_events` / `learned_habits`
- `mcp_tools.py` — Bộ 11 MCP Tools + JSON-RPC 2.0 handler
- `habit_engine.py` — Động cơ phân tích thói quen & thực thi tự động an toàn
- `web_dashboard.py`, `static/dashboard.html` — Web Server `aiohttp`, API REST, SSE & giao diện điều khiển
- `fan_controller.py`, `smart_plugs.py`, `esp32_audio.py` — Điều khiển phần cứng LAN
- `firmware/` — Mã nguồn mạch `esp01s_bldc_fan` & `esp32_c3_speaker`
- `tools/` — Script hỗ trợ chạy trên PC (`pc_media_bridge.py`, `music_download_server.py`)
