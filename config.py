# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token (lấy từ @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Google Gemini API Key (Google AI Studio)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
_raw_keys = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if GEMINI_API_KEY and GEMINI_API_KEY not in GEMINI_API_KEYS:
    GEMINI_API_KEYS.insert(0, GEMINI_API_KEY)
if GEMINI_API_KEYS and not GEMINI_API_KEY:
    GEMINI_API_KEY = GEMINI_API_KEYS[0]

# Telegram User ID của chủ nhà (để 0 nếu muốn bot tự khóa vào người đầu tiên bấm /start)
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

# Cấu hình nhân vật Quản gia / Trợ lý AI
BUTLER_NAME = os.getenv("BUTLER_NAME", "Atlas").strip() or "Atlas"
OWNER_NAME = os.getenv("OWNER_NAME", "bạn").strip() or "bạn"
# Style hỗ trợ: "butler" (Quản gia lịch thiệp) | "assistant" (Trợ lý súc tích) | "companion" (Bạn đồng hành thân thiện)
BUTLER_STYLE = (os.getenv("BUTLER_STYLE", "butler").strip().lower() or "butler")
WEATHER_CITY = os.getenv("WEATHER_CITY", "Ho_Chi_Minh_City").strip() or "Ho_Chi_Minh_City"
TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "7") or 7)

# Model Gemini
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.1-flash-lite")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "gemini-3.5-flash-lite")
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "gemini-3.1-flash-lite")


def get_api_keys(role: str = "chat") -> list:
    """Trả về danh sách API key theo thứ tự ưu tiên cho từng vai trò ('chat' hoặc 'memory')."""
    if not GEMINI_API_KEYS:
        return []
    if role == "memory" and len(GEMINI_API_KEYS) > 1:
        return [GEMINI_API_KEYS[1], GEMINI_API_KEYS[0]] + GEMINI_API_KEYS[2:]
    return list(GEMINI_API_KEYS)


# Đường dẫn Database SQLite
DB_PATH = os.getenv("DB_PATH", "smart_butler.db")

# VieNeu-TTS Server (tùy chọn, chạy trên PC LAN / Tailscale để gửi Voice Note)
TTS_API_URL = os.getenv("TTS_API_URL", "").strip().rstrip("/")
TTS_API_KEY = os.getenv("TTS_API_KEY", "").strip()

# PC LAN Music Download Bridge (tùy chọn)
MUSIC_DL_API_URL = os.getenv("MUSIC_DL_API_URL", "").strip().rstrip("/")
MUSIC_DL_API_KEY = os.getenv("MUSIC_DL_API_KEY", "").strip() or TTS_API_KEY

# Web Dashboard, MCP JSON-RPC & LAN Management
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080") or 8080)
except ValueError:
    DASHBOARD_PORT = 8080
DASHBOARD_PIN = os.getenv("DASHBOARD_PIN", "").strip()

# Thư mục lưu snapshot sao lưu SQLite nén (.db.gz)
BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip()

# Lượt kiểm toán logic trước khi gửi (Pass 3 Auditor - Hybrid Gate, Fail-open)
LOGIC_AUDIT_ENABLED = os.getenv("LOGIC_AUDIT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
LOGIC_AUDIT_MODEL = os.getenv("LOGIC_AUDIT_MODEL", MEMORY_MODEL).strip() or MEMORY_MODEL
try:
    LOGIC_AUDIT_TIMEOUT_SEC = max(2.0, float(os.getenv("LOGIC_AUDIT_TIMEOUT_SEC", "6.0") or 6.0))
except ValueError:
    LOGIC_AUDIT_TIMEOUT_SEC = 6.0

# Ổ cứng / USB gắn ngoài tùy chọn (cache nhạc, bản sao lưu phụ)
USB_STORAGE_DIR = os.getenv("USB_STORAGE_DIR", "/mnt/usb").strip() or "/mnt/usb"
try:
    USB_MUSIC_CACHE_MAX_MB = max(256, int(os.getenv("USB_MUSIC_CACHE_MAX_MB", "4096") or 4096))
except ValueError:
    USB_MUSIC_CACHE_MAX_MB = 4096
try:
    INTERNAL_MUSIC_CACHE_MAX_MB = max(50, int(os.getenv("INTERNAL_MUSIC_CACHE_MAX_MB", "150") or 150))
except ValueError:
    INTERNAL_MUSIC_CACHE_MAX_MB = 150

# Router Wi-Fi OpenWrt / LuCI ubus JSON-RPC (tùy chọn)
ROUTER_URL = os.getenv("ROUTER_URL", "http://192.168.1.1").strip().rstrip("/") or "http://192.168.1.1"
ROUTER_USER = os.getenv("ROUTER_USER", "root").strip() or "root"
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "").strip()
