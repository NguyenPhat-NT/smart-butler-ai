# 🌀 Firmware Quạt BLDC Wi-Fi (`ESP-01S`)

Mã nguồn điều khiển quạt BLDC thông qua xung tần số PWM (`1005 Hz` – `3000 Hz`) trên chân `GPIO 2` (`#define FAN_PIN 2`), tích hợp khởi động êm 5 giây (`1200 Hz`), giữ cài đặt vào EEPROM và Web/Arduino OTA.

## ⚙️ Cấu hình trước khi nạp

Mở `esp01s_bldc_fan.ino` và cập nhật:
- `ssid` = `"YOUR_WIFI_SSID"`
- `password` = `"YOUR_WIFI_PASSWORD"`

Tùy chọn:
- `ap_ssid`: Tên Wi-Fi dự phòng khi mất kết nối (`"ESP-BLDC-Fan"`)
- `ota_hostname`: Hostname mDNS (`"esp01s-bldc-fan"`)

## 🌐 API HTTP trên ESP-01S (Cổng `80`)

- `GET /` — Giao diện điều khiển đơn giản
- `GET /api/state` — Đọc trạng thái (`is_on`, `speed_pct`, `mode`, `output_freq`)
- `GET /api/power?val=0|1` — Bật hoặc tắt quạt
- `GET /api/speed?val=0..100` — Chỉnh tốc độ `%` (ánh xạ tuyến tính `1005`–`3000 Hz`)
- `GET /api/mode?val=0|1` — `0`: Cố định, `1`: Gió tự nhiên
- `/update` — Cập nhật firmware qua trình duyệt (HTTP OTA)

Sau khi nạp firmware và cấp nguồn, mở **Web Dashboard Smart Butler -> Quạt BLDC -> Quét tìm quạt** (hoặc nhập trực tiếp IP / MAC) để kết nối tự động.
