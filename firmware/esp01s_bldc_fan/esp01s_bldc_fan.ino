#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ArduinoOTA.h>
#include <EEPROM.h>

// ================= CẤU HÌNH PHẦN CỨNG =================
#define FAN_PIN 2            // GPIO 2 trên ESP-01S
#define EEPROM_SIZE 64
#define EEPROM_MAGIC 0xA5     // Byte đánh dấu dữ liệu đã lưu

// ================= THÔNG SỐ XUNG TẦN SỐ BLDC =================
// Dải tần số quạt BLDC: 1005Hz đến 3000Hz (1200Hz quay nhanh hơn 1005Hz)
const unsigned int MIN_FREQ = 1005;       // 0% tốc độ = 1005 Hz
const unsigned int MAX_FREQ = 3000;       // 100% tốc độ = 3000 Hz
const unsigned int STARTUP_FREQ = 1200;   // Tần số khởi động 1200 Hz (~10%)
const unsigned long STARTUP_TIME_MS = 5000; // Đề-pa trong 5 giây

// Tốc độ chuyển tần số mượt (mỗi bước cập nhật 30ms)
const unsigned long RAMP_TICK_MS = 30;
const float POST_START_DOWN_HZ_PER_SEC = 24.0f; // 1200 -> 1005 Hz trong ~8.1 giây thật êm
const float POST_START_UP_HZ_PER_SEC   = 300.0f; // Đề-pa xong tăng lên mức cao trong tối đa ~6s
const float FINE_STEP_HZ_PER_SEC       = 35.0f;  // Bước nhỏ <= 30 Hz (vd bước 1% từ OpenStick) lướt từng 1 Hz
const float MANUAL_SLIDE_HZ_PER_SEC    = 500.0f; // Kéo thanh trượt lớn mượt mà nhưng vẫn nhạy

// ================= CẤU HÌNH WIFI & OTA =================
const char* ssid         = "YOUR_WIFI_SSID";
const char* password     = "YOUR_WIFI_PASSWORD";
const char* ap_ssid      = "ESP-BLDC-Fan";
const char* ota_hostname = "esp01s-bldc-fan";

const unsigned long WIFI_CHECK_INTERVAL_MS = 25000;
const unsigned long WIFI_HARD_RECONNECT_MS = 120000;

// ================= BIẾN TRẠNG THÁI =================
struct FanConfig {
  uint8_t magic;
  bool is_on;
  uint8_t speed_pct;      // 0 - 100% (tương ứng 1005Hz - 3000Hz)
  uint8_t mode;           // 0: Cố định, 1: Gió tự nhiên
} config;

enum FanState {
  STATE_OFF,
  STATE_STARTING,
  STATE_POST_START_RAMP,
  STATE_RUNNING
};

FanState currentState = STATE_OFF;
unsigned long startupStartTime = 0;
unsigned long lastRampTick = 0;
unsigned long lastWindUpdate = 0;
unsigned long lastEepromSave = 0;
unsigned long lastWifiCheck = 0;
unsigned long wifiLostSince = 0;
bool needSaveEeprom = false;
bool apFallbackActive = false;
bool otaBusy = false;
float windAngle = 0.0f;
float smoothFreq = 0.0f;
unsigned int currentOutputFreq = 0;

ESP8266WebServer server(80);
ESP8266HTTPUpdateServer httpUpdater;

// ================= XUẤT XUNG =================
void setFanFrequency(unsigned int freq) {
  if (freq == 0) {
    if (currentOutputFreq != 0) {
      noTone(FAN_PIN);
      digitalWrite(FAN_PIN, LOW);
      currentOutputFreq = 0;
    }
    return;
  }
  // Tránh gọi tone() lặp lại khi tần số không đổi để giữ xung sạch không giật
  if (freq != currentOutputFreq) {
    currentOutputFreq = freq;
    tone(FAN_PIN, freq);
  }
}

unsigned int speedPctToFreq(float pct) {
  pct = constrain(pct, 0.0f, 100.0f);
  return (unsigned int)lroundf(MIN_FREQ + (pct / 100.0f) * (MAX_FREQ - MIN_FREQ));
}

// ================= EEPROM =================
void loadConfig() {
  EEPROM.begin(EEPROM_SIZE);
  EEPROM.get(0, config);
  if (config.magic != EEPROM_MAGIC) {
    config.magic = EEPROM_MAGIC;
    config.is_on = true;
    config.speed_pct = 10; // Mặc định 10% (~1200 Hz)
    config.mode = 0;
    EEPROM.put(0, config);
    EEPROM.commit();
  }
  config.speed_pct = constrain(config.speed_pct, 0, 100);
  config.mode = (config.mode == 1) ? 1 : 0;
}

void triggerSaveConfig() {
  needSaveEeprom = true;
  lastEepromSave = millis();
}

void handleDelayedEepromSave() {
  // Trì hoãn ghi EEPROM 2 giây để tránh chai Flash khi trượt thanh tốc độ
  if (needSaveEeprom && (millis() - lastEepromSave > 2000)) {
    EEPROM.put(0, config);
    EEPROM.commit();
    needSaveEeprom = false;
  }
}

// ================= ĐIỀU KHIỂN QUẠT =================
void startFan() {
  config.is_on = true;
  currentState = STATE_STARTING;
  startupStartTime = millis();
  smoothFreq = (float)STARTUP_FREQ;
  windAngle = 0.0f;
  setFanFrequency(STARTUP_FREQ); // Khởi động 1200 Hz trong 5s
  triggerSaveConfig();
}

void stopFan() {
  config.is_on = false;
  currentState = STATE_OFF;
  smoothFreq = 0.0f;
  setFanFrequency(0);
  triggerSaveConfig();
}

bool stepSmoothFrequency(float goalFreq, float upRateHzSec, float downRateHzSec, unsigned long now) {
  if (now - lastRampTick < RAMP_TICK_MS) {
    return false;
  }
  float dtSec = (float)(now - lastRampTick) / 1000.0f;
  if (dtSec > 0.25f) dtSec = 0.03f;
  lastRampTick = now;

  if (smoothFreq < (float)MIN_FREQ) {
    smoothFreq = (float)STARTUP_FREQ;
  }

  float diff = goalFreq - smoothFreq;
  if (fabsf(diff) <= 0.5f) {
    smoothFreq = goalFreq;
    setFanFrequency((unsigned int)lroundf(smoothFreq));
    return true;
  }

  float rate = (diff < 0.0f) ? downRateHzSec : upRateHzSec;
  float maxStep = rate * dtSec;
  if (fabsf(diff) <= maxStep) {
    smoothFreq = goalFreq;
    setFanFrequency((unsigned int)lroundf(smoothFreq));
    return true;
  }

  smoothFreq += (diff > 0.0f ? maxStep : -maxStep);
  setFanFrequency((unsigned int)lroundf(smoothFreq));
  return false;
}

void updateFan() {
  if (otaBusy) return;
  unsigned long now = millis();

  if (!config.is_on) {
    if (currentState != STATE_OFF || currentOutputFreq != 0) {
      setFanFrequency(0);
      currentState = STATE_OFF;
      smoothFreq = 0.0f;
    }
    return;
  }

  // 1) Giai đoạn khởi động: giữ đều 1200 Hz trong 5 giây
  if (currentState == STATE_STARTING) {
    setFanFrequency(STARTUP_FREQ);
    if (now - startupStartTime >= STARTUP_TIME_MS) {
      smoothFreq = (float)STARTUP_FREQ;
      lastRampTick = now;
      currentState = STATE_POST_START_RAMP;
    }
    return;
  }

  // 2) Giai đoạn hạ/tăng êm sau khi đề-pa 5s xong về tốc độ đã cài
  if (currentState == STATE_POST_START_RAMP) {
    float goalFreq = (float)speedPctToFreq((float)config.speed_pct);
    bool reached = stepSmoothFrequency(goalFreq, POST_START_UP_HZ_PER_SEC, POST_START_DOWN_HZ_PER_SEC, now);
    if (reached) {
      windAngle = 0.0f;
      lastWindUpdate = now;
      currentState = STATE_RUNNING;
    }
    return;
  }

  // 3) Giai đoạn chạy ổn định
  if (currentState == STATE_RUNNING) {
    if (config.mode == 0) {
      float goalFreq = (float)speedPctToFreq((float)config.speed_pct);
      float diffAbs = fabsf(goalFreq - smoothFreq);
      if (diffAbs <= 0.5f) {
        smoothFreq = goalFreq;
        setFanFrequency((unsigned int)lroundf(goalFreq));
      } else if (diffAbs <= 30.0f) {
        // Khoảng nhảy nhỏ (vd mỗi bước 1% từ OpenStick) -> lướt từng 1 Hz thật êm
        stepSmoothFrequency(goalFreq, FINE_STEP_HZ_PER_SEC, FINE_STEP_HZ_PER_SEC, now);
      } else {
        // Kéo thanh trượt lớn -> lướt nhanh mượt tay
        stepSmoothFrequency(goalFreq, MANUAL_SLIDE_HZ_PER_SEC, MANUAL_SLIDE_HZ_PER_SEC, now);
      }
    } else if (config.mode == 1) {
      // Chế độ gió tự nhiên: Dao động hình sin êm dịu ±5%, chu kỳ ~18 giây
      if (now - lastWindUpdate >= 30) {
        lastWindUpdate = now;
        windAngle += 0.0105f;
        if (windAngle > 6.283185f) windAngle -= 6.283185f;

        float offset = sin(windAngle) * 5.0f;
        float currentPct = constrain((float)config.speed_pct + offset, 0.0f, 100.0f);
        unsigned int targetFreq = speedPctToFreq(currentPct);
        smoothFreq = (float)targetFreq;
        setFanFrequency(targetFreq);
      }
    }
  }
}

// ================= DUY TRÌ KẾT NỐI WIFI =================
void setupWiFi() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP); // Giữ phản hồi HTTP <10ms, không rớt gói khi sleep beacon
  WiFi.setAutoReconnect(true);
  WiFi.setOutputPower(20.5f);
  WiFi.hostname(ota_hostname);
  WiFi.begin(ssid, password);

  Serial.printf("Connecting to WiFi: %s ", ssid);
  unsigned long startWait = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - startWait < 15000)) {
    delay(350);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    apFallbackActive = false;
    wifiLostSince = 0;
    Serial.print("[WiFi OK] IP: ");
    Serial.println(WiFi.localIP());
  } else {
    // Phát AP dự phòng ở chế độ AP+STA để nền vẫn tiếp tục tự kết nối lại Wi-Fi chính
    WiFi.mode(WIFI_AP_STA);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.softAP(ap_ssid);
    apFallbackActive = true;
    wifiLostSince = millis();
    WiFi.begin(ssid, password);
    Serial.print("[AP+STA WARN] Fallback AP started: ");
    Serial.println(ap_ssid);
  }
}

void maintainWiFi() {
  unsigned long now = millis();
  if (now - lastWifiCheck < WIFI_CHECK_INTERVAL_MS) {
    return;
  }
  lastWifiCheck = now;

  if (WiFi.status() == WL_CONNECTED) {
    wifiLostSince = 0;
    if (apFallbackActive) {
      WiFi.softAPdisconnect(true);
      WiFi.mode(WIFI_STA);
      WiFi.setSleepMode(WIFI_NONE_SLEEP);
      apFallbackActive = false;
      Serial.print("[WiFi RECOVERED] IP: ");
      Serial.println(WiFi.localIP());
    }
    return;
  }

  if (wifiLostSince == 0) {
    wifiLostSince = now;
  }
  if (!apFallbackActive && (now - wifiLostSince >= 30000)) {
    WiFi.mode(WIFI_AP_STA);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.softAP(ap_ssid);
    apFallbackActive = true;
  }
  if (now - wifiLostSince >= WIFI_HARD_RECONNECT_MS) {
    Serial.println("[WiFi WARN] Re-initializing STA connection...");
    WiFi.disconnect(false);
    WiFi.begin(ssid, password);
    wifiLostSince = now;
  } else {
    WiFi.reconnect();
  }
}

void setupOTA() {
  ArduinoOTA.setHostname(ota_hostname);
  ArduinoOTA.onStart([]() {
    otaBusy = true;
    noTone(FAN_PIN);
    digitalWrite(FAN_PIN, LOW);
    currentOutputFreq = 0;
    Serial.println("[OTA] Updating firmware...");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("\n[OTA] Done, rebooting...");
  });
  ArduinoOTA.onError([](ota_error_t error) {
    otaBusy = false;
    Serial.printf("[OTA ERROR] Code %u\n", error);
  });
  ArduinoOTA.begin();

  httpUpdater.setup(&server, "/update");
  MDNS.begin(ota_hostname);
  MDNS.addService("http", "tcp", 80);
}

// ================= GIAO DIỆN WEB =================
const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Quạt BLDC ESP-01S</title>
  <style>
    :root {
      --bg: #0b1329;
      --card: #1c2541;
      --accent: #00f2fe;
      --accent-grad: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
      --text: #ffffff;
      --text-muted: #8da9c4;
      --danger: #ef4444;
      --success: #10b981;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); min-height: 100vh; display: flex; justify-content: center; align-items: center; padding: 16px; }
    .card { background: var(--card); width: 100%; max-width: 400px; border-radius: 28px; padding: 30px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); }
    .top { text-align: center; margin-bottom: 24px; }
    .title { font-size: 1.3rem; font-weight: 700; letter-spacing: 0.5px; }
    .badge { display: inline-block; padding: 6px 14px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; margin-top: 10px; background: #273859; color: var(--text-muted); }
    .badge.active { background: rgba(16, 185, 129, 0.2); color: var(--success); border: 1px solid var(--success); }
    .badge.starting { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #fbbf24; animation: pulse 1s infinite alternate; }
    @keyframes pulse { from { opacity: 0.6; } to { opacity: 1; } }

    .meter { text-align: center; margin: 20px 0; }
    .meter-val { font-size: 2.4rem; font-weight: 800; color: #4facfe; }
    .meter-unit { font-size: 1rem; color: var(--text-muted); font-weight: 400; }

    .btn-power { width: 100%; padding: 15px; border: none; border-radius: 18px; font-size: 1.05rem; font-weight: 700; cursor: pointer; transition: 0.2s; margin-bottom: 24px; }
    .btn-on { background: #ef4444; color: white; box-shadow: 0 10px 20px rgba(239, 68, 68, 0.3); }
    .btn-off { background: var(--accent-grad); color: #0b1329; box-shadow: 0 10px 20px rgba(0, 242, 254, 0.3); }

    .box { background: #131c31; border-radius: 18px; padding: 18px; margin-bottom: 20px; }
    .box-hdr { display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 0.95rem; color: var(--text-muted); }
    .box-hdr strong { color: var(--text); }
    input[type=range] { width: 100%; -webkit-appearance: none; height: 8px; border-radius: 4px; background: #273859; outline: none; }
    input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 24px; height: 24px; border-radius: 50%; background: #00f2fe; cursor: pointer; box-shadow: 0 0 12px #00f2fe; }

    .modes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }
    .mode-btn { padding: 12px; border: 2px solid #273859; border-radius: 14px; background: transparent; color: var(--text-muted); font-weight: 600; cursor: pointer; transition: 0.2s; }
    .mode-btn.active { border-color: #00f2fe; color: #00f2fe; background: rgba(0, 242, 254, 0.1); }

    .footer { text-align: center; font-size: 0.78rem; color: var(--text-muted); line-height: 1.5; }
    .footer a { color: #4facfe; text-decoration: none; }
  </style>
</head>
<body>
  <div class="card">
    <div class="top">
      <div class="title">ĐIỀU KHIỂN QUẠT BLDC</div>
      <div id="badge" class="badge">Đang tải...</div>
    </div>

    <div class="meter">
      <div id="dispFreq" class="meter-val">1005</div>
      <div class="meter-unit">Hz (Tần số xung thực tế)</div>
    </div>

    <button id="pBtn" class="btn-power btn-off" onclick="togglePower()">BẬT QUẠT</button>

    <div class="box">
      <div class="box-hdr">
        <span>Tốc độ quạt</span>
        <strong id="pctVal">0%</strong>
      </div>
      <input type="range" id="slider" min="0" max="100" value="0" oninput="changeSpeed(this.value)">
    </div>

    <div class="modes">
      <button id="m0" class="mode-btn active" onclick="setMode(0)">Cố định</button>
      <button id="m1" class="mode-btn" onclick="setMode(1)">Gió tự nhiên</button>
    </div>

    <div class="footer">
      Dải tần: 1005Hz - 3000Hz | Đề-pa 1200Hz (5s) -> lướt êm<br>
      Wi-Fi Keep-Alive • <a href="/update">Cập nhật OTA (.bin)</a>
    </div>
  </div>

  <script>
    let currentData = { is_on: false, speed_pct: 0, mode: 0, output_freq: 0, status: 'OFF' };
    let pollBusy = false;

    function fetchStatus() {
      if (pollBusy) return;
      pollBusy = true;
      fetch('/api/state')
        .then(res => res.json())
        .then(data => {
          currentData = data;
          render();
        })
        .catch(() => {})
        .finally(() => { pollBusy = false; });
    }

    function render() {
      const pBtn = document.getElementById('pBtn');
      const badge = document.getElementById('badge');
      const slider = document.getElementById('slider');
      const pctVal = document.getElementById('pctVal');
      const dispFreq = document.getElementById('dispFreq');

      if (currentData.is_on) {
        pBtn.textContent = 'TẮT QUẠT';
        pBtn.className = 'btn-power btn-on';
      } else {
        pBtn.textContent = 'BẬT QUẠT';
        pBtn.className = 'btn-power btn-off';
      }

      badge.textContent = currentData.status_text;
      const isWarmup = currentData.status === 'STARTING' || currentData.status === 'RAMPING';
      badge.className = 'badge ' + (isWarmup ? 'starting' : (currentData.is_on ? 'active' : ''));

      dispFreq.textContent = currentData.output_freq;
      if (document.activeElement !== slider) {
        slider.value = currentData.speed_pct;
      }
      pctVal.textContent = currentData.speed_pct + '% (' + Math.round(1005 + currentData.speed_pct * 19.95) + ' Hz)';

      document.getElementById('m0').className = 'mode-btn ' + (currentData.mode === 0 ? 'active' : '');
      document.getElementById('m1').className = 'mode-btn ' + (currentData.mode === 1 ? 'active' : '');
    }

    function applyResp(res) {
      return res.json().then(d => { currentData = d; render(); });
    }

    function togglePower() {
      fetch('/api/power?state=' + (currentData.is_on ? '0' : '1')).then(applyResp);
    }

    let tOut;
    function changeSpeed(val) {
      document.getElementById('pctVal').textContent = val + '% (' + Math.round(1005 + val * 19.95) + ' Hz)';
      clearTimeout(tOut);
      tOut = setTimeout(() => {
        fetch('/api/speed?val=' + val).then(applyResp);
      }, 90);
    }

    function setMode(m) {
      fetch('/api/mode?val=' + m).then(applyResp);
    }

    fetchStatus();
    setInterval(fetchStatus, 1000);
  </script>
</body>
</html>
)rawliteral";

// ================= API ENDPOINTS =================
void handleRoot() {
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "text/html", INDEX_HTML);
}

void handleApiState() {
  String statusText = "Đang tắt";
  String statusCode = "OFF";
  if (config.is_on) {
    if (currentState == STATE_STARTING) {
      statusText = "Đề-pa khởi động (1200Hz - 5s)";
      statusCode = "STARTING";
    } else if (currentState == STATE_POST_START_RAMP) {
      statusText = "Đang lướt êm về tốc độ cài...";
      statusCode = "RAMPING";
    } else {
      statusText = (config.mode == 0) ? "Đang chạy cố định" : "Gió tự nhiên (±5%, 18s êm)";
      statusCode = "RUNNING";
    }
  }

  String json = "{";
  json += "\"is_on\":" + String(config.is_on ? "true" : "false") + ",";
  json += "\"speed_pct\":" + String(config.speed_pct) + ",";
  json += "\"mode\":" + String(config.mode) + ",";
  json += "\"output_freq\":" + String(currentOutputFreq) + ",";
  json += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  json += "\"status\":\"" + statusCode + "\",";
  json += "\"status_text\":\"" + statusText + "\"";
  json += "}";
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", json);
}

void handleApiPower() {
  if (server.hasArg("state")) {
    int s = server.arg("state").toInt();
    if (s == 1) startFan();
    else stopFan();
  }
  handleApiState();
}

void handleApiSpeed() {
  if (server.hasArg("val")) {
    int v = server.arg("val").toInt();
    config.speed_pct = constrain(v, 0, 100);
    // Nếu đang ở giai đoạn hạ êm sau đề-pa mà người dùng tự kéo thanh trượt -> chuyển sang chạy mượt ngay
    if (config.is_on && currentState == STATE_POST_START_RAMP) {
      currentState = STATE_RUNNING;
    }
    triggerSaveConfig();
  }
  handleApiState();
}

void handleApiMode() {
  if (server.hasArg("val")) {
    int m = server.arg("val").toInt();
    uint8_t nextMode = (m == 1) ? 1 : 0;
    if (nextMode != config.mode && nextMode == 1) {
      windAngle = 0.0f;
    }
    config.mode = nextMode;
    triggerSaveConfig();
  }
  handleApiState();
}

// ================= SETUP & LOOP =================
void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n\n=== ESP-01S BLDC FAN v2 (SOFT RAMP + KEEP-ALIVE + OTA) ===");

  pinMode(FAN_PIN, OUTPUT);
  digitalWrite(FAN_PIN, LOW);

  loadConfig();
  Serial.printf("Config loaded: Power=%d, Speed=%d%%, Mode=%d\n", config.is_on, config.speed_pct, config.mode);

  setupWiFi();
  setupOTA();

  server.enableCORS(true);
  server.on("/", handleRoot);
  server.on("/api/state", handleApiState);
  server.on("/api/power", handleApiPower);
  server.on("/api/speed", handleApiSpeed);
  server.on("/api/mode", handleApiMode);
  server.begin();
  Serial.println("[HTTP] Server started on port 80 (OTA at /update)");

  if (config.is_on) {
    Serial.println("[FAN] Starting fan with 1200Hz 5s startup -> smooth ramp...");
    startFan();
  } else {
    Serial.println("[FAN] Fan is currently OFF.");
    stopFan();
  }
}

void loop() {
  ArduinoOTA.handle();
  MDNS.update();
  server.handleClient();
  maintainWiFi();
  updateFan();
  handleDelayedEepromSave();
}
