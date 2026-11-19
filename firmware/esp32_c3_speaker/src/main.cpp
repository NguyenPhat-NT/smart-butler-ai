/*
 * ESP32-C3 SuperMini - Loa Thong Minh & Cam Bien Phong Smart Butler (v2: Opus + i2s_std + WebSocket)
 * Ket noi WebSocket hai chieu toi OpenStick (ws://<BOT_HOST>:8080/api/esp32/ws):
 *   - Nhan am thanh nen Opus 24kHz mono (khung 60ms) -> giai ma fixed-point -> DMA i2s_std.h ra MAX98357-QFN
 *   - MAX98357-QFN (I2S Audio): BCLK=GPIO5, LRC=GPIO6, DIN=GPIO7, VIN=5V, GND=GND
 *   - Nut cam ung TTP223:       SIG=GPIO3, VCC=3.3V, GND=GND (single / double / long)
 *   - Cam bien DHT11:           DATA=GPIO4, VCC=3.3V, GND=GND (moi 60 giay)
 *   - Cap nhat khong day (OTA): http://<IP_ESP32>/ | POST http://<IP_ESP32>/update | ArduinoOTA port 3232
 *
 * Ho tro bien dich bang ca PlatformIO (src/main.cpp) va Arduino IDE (.ino).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <HTTPClient.h>
#include <ArduinoOTA.h>
#include <WebServer.h>
#include <Update.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <driver/i2s_std.h>
#include <driver/temperature_sensor.h>
#include <WebSocketsClient.h>
#include <opus.h>

// ==================== CAU HINH WI-FI & SERVER ====================
// Co the tao file `secrets.h` (duoc .gitignore) de ghi de thong tin nhay cam
// ma khong sua ma nguon: #define WIFI_SSID_PRIMARY "..." / #define OTA_AUTH_PASSWORD "..."
#if defined(__has_include)
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#endif

#ifndef WIFI_SSID_PRIMARY
#define WIFI_SSID_PRIMARY "YOUR_WIFI_SSID"
#endif
#ifndef WIFI_PASS_PRIMARY
#define WIFI_PASS_PRIMARY "YOUR_WIFI_PASSWORD"
#endif

#ifndef WIFI_SSID_BACKUP
#define WIFI_SSID_BACKUP  ""
#endif
#ifndef WIFI_PASS_BACKUP
#define WIFI_PASS_BACKUP  ""
#endif

#ifndef OPENSTICK_HOST
#define OPENSTICK_HOST    "192.168.1.100"
#endif
#ifndef OPENSTICK_PORT
#define OPENSTICK_PORT    8080
#endif
static const char* WS_AUDIO_PATH       = "/api/esp32/ws";

static const char* OTA_HOSTNAME        = "esp32-smart-butler";
static const uint16_t OTA_PORT         = 3232;
static const uint16_t WEB_OTA_PORT     = 80;

// ==================== SO DO CHAN ESP32-C3 SUPERMINI ====================
static const int PIN_I2S_BCLK          = 5;
static const int PIN_I2S_LRC           = 6;
static const int PIN_I2S_DIN           = 7;
static const int PIN_TOUCH_TTP223      = 3;
static const int PIN_DHT11_DATA        = 4;

// ==================== THONG SO AM THANH OPUS & I2S_STD ====================
static const uint32_t AUDIO_SAMPLE_RATE      = 24000;
static const uint32_t OPUS_FRAME_MS          = 60;
static const size_t   OPUS_FRAME_SAMPLES     = (AUDIO_SAMPLE_RATE * OPUS_FRAME_MS) / 1000; // 1440 mau (60ms)
static const size_t   OPUS_MAX_PACKET_BYTES  = 768;
static const size_t   OPUS_RING_BYTES        = 28672; // ~7-8 giay am thanh Opus 32kbps trong SRAM
static const uint32_t OPUS_PREFILL_PACKETS   = 5;     // 300ms khi bat dau phat moi
static const uint32_t OPUS_RESUME_PACKETS    = 4;     // 240ms khi phuc hoi sau rot goi
static const uint32_t OPUS_LIVE_PREFILL_PKTS = 2;     // 120ms khi chay che do Loa Live do tre thap
static const uint32_t OPUS_LIVE_RESUME_PKTS  = 2;
static const uint32_t OPUS_LIVE_MAX_QUEUE    = 5;     // Gioi han hang doi live (~300ms) chong tre tich luy
static const uint32_t STREAM_IDLE_SILENCE_MS = 600;
static const i2s_port_t I2S_PORT             = I2S_NUM_0;
static const int32_t  AUDIO_OUTPUT_GAIN_PCT  = 62;    // Giam nhe gain dau ra (~ -4.1 dB) cho MAX98357 em tai hon
static const int32_t  AUDIO_OUTPUT_GAIN_Q8   = (AUDIO_OUTPUT_GAIN_PCT * 256 + 50) / 100;

static const uint32_t TOUCH_DEBOUNCE_MS     = 30;
static const uint32_t TOUCH_LONG_PRESS_MS   = 1200;
static const uint32_t TOUCH_DOUBLE_GAP_MS   = 360;

struct WsTxMsg {
    char json[160];
};

WiFiMulti wifiMulti;
WebServer otaServer(WEB_OTA_PORT);
static WebSocketsClient s_wsClient;
static i2s_chan_handle_t s_txChan = nullptr;

static QueueHandle_t g_buttonQueue = nullptr;
static QueueHandle_t g_wsTxQueue   = nullptr;
static TaskHandle_t  s_i2sTaskHandle = nullptr;
static TaskHandle_t  s_wsTaskHandle  = nullptr;

static volatile bool     g_otaInProgress    = false;
static volatile bool     s_wsConnected      = false;
static volatile bool     s_lowLatencyMode   = false;
static volatile bool     s_wsReconfigureReq = false;
static String            s_serverHost       = OPENSTICK_HOST;
static uint16_t          s_serverPort       = OPENSTICK_PORT;
static volatile uint32_t s_lastAudioRxAt    = 0;
static volatile uint32_t s_flushEpoch       = 0;

static bool isValidServerHost(const String& host) {
    if (host.length() == 0 || host.length() > 64) {
        return false;
    }
    for (size_t i = 0; i < host.length(); ++i) {
        char c = host[i];
        bool ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  c == '.' || c == '-' || c == '_';
        if (!ok) {
            return false;
        }
    }
    return true;
}

static void loadServerConfigFromNvs() {
    Preferences prefs;
    if (prefs.begin("spk_cfg", true)) {
        String h = prefs.getString("host", "");
        uint16_t p = prefs.getUShort("port", 0);
        prefs.end();
        h.trim();
        if (isValidServerHost(h)) {
            s_serverHost = h;
        }
        if (p > 0) {
            s_serverPort = p;
        }
    }
    Serial.printf("[CFG] WebSocket Server: ws://%s:%u%s\n", s_serverHost.c_str(), (unsigned)s_serverPort, WS_AUDIO_PATH);
}

static void saveServerConfigToNvs(const String& host, uint16_t port) {
    Preferences prefs;
    if (prefs.begin("spk_cfg", false)) {
        prefs.putString("host", host);
        prefs.putUShort("port", port);
        prefs.end();
    }
}

// Telemetry chan doan truc tiep qua GET /debug
static volatile uint32_t s_dbgUnderruns     = 0;
static volatile uint32_t s_dbgEmptyDips     = 0;
static volatile uint32_t s_dbgReconnects    = 0;
static volatile uint32_t s_dbgRxBytes       = 0;
static volatile uint32_t s_dbgMaxRxGapMs    = 0;
static volatile uint32_t s_dbgPktMin        = 0xFFFFFFFFUL;
static volatile uint32_t s_dbgPktMax        = 0;
static volatile uint32_t s_dbgRingDrops     = 0;
static volatile uint32_t s_dbgOpusFrames    = 0;
static volatile uint32_t s_dbgOpusErrors    = 0;
static volatile uint32_t s_dbgOpusDecLastUs = 0;
static volatile uint32_t s_dbgOpusDecMaxUs  = 0;
static volatile uint32_t s_dbgI2sLoops      = 0;
static volatile uint32_t s_dbgWsLoops       = 0;
static volatile uint32_t s_dbgCtrlLoops     = 0;
static volatile bool     s_dbgPlaying       = false;

// ==================== QUAN LY NGUON & GIAM NHIET DONG ====================
static const uint32_t CPU_FREQ_ACTIVE_MHZ = 160;
static const uint32_t CPU_FREQ_IDLE_MHZ   = 80;
static const uint32_t IDLE_POWER_HOLD_MS  = 3500;
static const uint32_t I2S_IDLE_DISABLE_MS = 2500;

static SemaphoreHandle_t         s_pmMutex        = nullptr;
static portMUX_TYPE              s_perfMux        = portMUX_INITIALIZER_UNLOCKED;
static temperature_sensor_handle_t s_chipTempSensor = nullptr;
static volatile uint32_t         s_perfHoldUntilMs = 0;
static volatile bool             s_perfHoldActive  = false;
static volatile bool             s_wakeRequested   = false;
static volatile bool             s_powerEcoActive  = false;
static volatile bool             s_i2sEnabled      = false;
static wifi_power_t              s_curTxPower      = WIFI_POWER_15dBm;

static void initChipTempSensor() {
    temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    if (temperature_sensor_install(&cfg, &s_chipTempSensor) == ESP_OK) {
        if (temperature_sensor_enable(s_chipTempSensor) != ESP_OK) {
            temperature_sensor_uninstall(s_chipTempSensor);
            s_chipTempSensor = nullptr;
        }
    }
}

static float readChipTempC() {
    if (s_chipTempSensor == nullptr) {
        return NAN;
    }
    float tempC = NAN;
    if (temperature_sensor_get_celsius(s_chipTempSensor, &tempC) != ESP_OK) {
        return NAN;
    }
    return tempC;
}

static void setPowerMode(bool wantActive, TickType_t lockWaitTicks) {
    if (s_pmMutex != nullptr && xSemaphoreTake(s_pmMutex, lockWaitTicks) != pdTRUE) {
        return;
    }
    if (wantActive) {
        if (s_powerEcoActive || getCpuFrequencyMhz() != CPU_FREQ_ACTIVE_MHZ) {
            setCpuFrequencyMhz(CPU_FREQ_ACTIVE_MHZ);
        }
        if (WiFi.getSleep() != WIFI_PS_NONE) {
            WiFi.setSleep(WIFI_PS_NONE);
        }
        s_powerEcoActive = false;
    } else {
        if (!s_powerEcoActive) {
            if (WiFi.status() == WL_CONNECTED && WiFi.getSleep() != WIFI_PS_MIN_MODEM) {
                WiFi.setSleep(WIFI_PS_MIN_MODEM);
            }
            if (getCpuFrequencyMhz() != CPU_FREQ_IDLE_MHZ) {
                setCpuFrequencyMhz(CPU_FREQ_IDLE_MHZ);
            }
            s_powerEcoActive = true;
        }
    }
    if (s_pmMutex != nullptr) {
        xSemaphoreGive(s_pmMutex);
    }
}

static void markPerformanceActivity(uint32_t holdMs = IDLE_POWER_HOLD_MS, bool boostNow = false) {
    uint32_t target = millis() + holdMs;
    portENTER_CRITICAL(&s_perfMux);
    if (!s_perfHoldActive || (int32_t)(target - s_perfHoldUntilMs) > 0) {
        s_perfHoldUntilMs = target;
        s_perfHoldActive = true;
    }
    if (boostNow) {
        s_wakeRequested = true;
    }
    portEXIT_CRITICAL(&s_perfMux);
    if (boostNow && s_powerEcoActive) {
        setPowerMode(true, 0);
    }
}

static void updatePowerProfile(uint32_t nowMs) {
    bool wifiReady = (WiFi.status() == WL_CONNECTED);
    portENTER_CRITICAL(&s_perfMux);
    bool holdActive = s_perfHoldActive && ((int32_t)(s_perfHoldUntilMs - nowMs) > 0);
    if (s_perfHoldActive && !holdActive) {
        s_perfHoldActive = false;
    }
    bool wakeReq = s_wakeRequested;
    portEXIT_CRITICAL(&s_perfMux);

    bool wantActive = g_otaInProgress || s_dbgPlaying || !wifiReady || holdActive || wakeReq;
    if (wantActive != (!s_powerEcoActive)) {
        setPowerMode(wantActive, portMAX_DELAY);
    } else if (!wantActive && wifiReady && WiFi.getSleep() != WIFI_PS_MIN_MODEM) {
        setPowerMode(false, portMAX_DELAY);
    }
    if (wantActive && !s_powerEcoActive && wakeReq) {
        portENTER_CRITICAL(&s_perfMux);
        if (!s_powerEcoActive) {
            s_wakeRequested = false;
        }
        portEXIT_CRITICAL(&s_perfMux);
    }
}

static void updateAdaptiveTxPower(uint32_t nowMs) {
    static uint32_t lastCheckAt = 0;
    if (WiFi.status() != WL_CONNECTED || (uint32_t)(nowMs - lastCheckAt) < 15000U) {
        return;
    }
    lastCheckAt = nowMs;
    int rssi = WiFi.RSSI();
    wifi_power_t desired = (rssi >= -68) ? WIFI_POWER_15dBm : ((rssi >= -78) ? WIFI_POWER_17dBm : WIFI_POWER_19_5dBm);
    if (desired != s_curTxPower) {
        if (WiFi.setTxPower(desired)) {
            s_curTxPower = desired;
        }
    }
}

// ==================== KHOI TAO I2S MOI (driver/i2s_std.h) ====================
static bool setI2sActive(bool active) {
    if (s_txChan == nullptr) {
        s_i2sEnabled = false;
        return false;
    }
    if (active == s_i2sEnabled) {
        return true;
    }
    esp_err_t err = active ? i2s_channel_enable(s_txChan) : i2s_channel_disable(s_txChan);
    if (err != ESP_OK) {
        Serial.printf("[I2S_STD] i2s_channel_%s failed: %d\n", active ? "enable" : "disable", (int)err);
        return false;
    }
    s_i2sEnabled = active;
    return true;
}

static void initI2S() {
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240; // 6 * 240 = 1440 frames = 60ms DMA (khop dung 1 khung Opus 60ms)
    chan_cfg.auto_clear = true;

    esp_err_t err = i2s_new_channel(&chan_cfg, &s_txChan, nullptr);
    if (err != ESP_OK || s_txChan == nullptr) {
        Serial.printf("[I2S_STD] i2s_new_channel failed: %d\n", (int)err);
        return;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)PIN_I2S_BCLK,
            .ws = (gpio_num_t)PIN_I2S_LRC,
            .dout = (gpio_num_t)PIN_I2S_DIN,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    err = i2s_channel_init_std_mode(s_txChan, &std_cfg);
    if (err != ESP_OK) {
        Serial.printf("[I2S_STD] i2s_channel_init_std_mode failed: %d\n", (int)err);
        return;
    }

    if (!setI2sActive(true)) {
        return;
    }

    Serial.printf("[I2S_STD] MAX98357 initialized @ %u Hz (BCLK=%d, LRC=%d, DIN=%d, DMA=6x240)\n",
                  (unsigned)AUDIO_SAMPLE_RATE, PIN_I2S_BCLK, PIN_I2S_LRC, PIN_I2S_DIN);
}

static void writeMonoSamplesToI2S(const int16_t* mono, size_t sampleCount) {
    if (s_txChan == nullptr || !s_i2sEnabled || !mono || sampleCount == 0) {
        return;
    }
    static int16_t stereoOut[480]; // 240 cap L/R (960 bytes) = 1 DMA descriptor
    size_t pos = 0;
    while (pos < sampleCount) {
        size_t batch = sampleCount - pos;
        if (batch > 240) batch = 240;
        for (size_t i = 0; i < batch; ++i) {
            int16_t s = mono[pos + i];
            stereoOut[i * 2] = s;
            stereoOut[i * 2 + 1] = s;
        }
        size_t written = 0;
        i2s_channel_write(s_txChan, stereoOut, batch * 2 * sizeof(int16_t), &written, portMAX_DELAY);
        pos += batch;
    }
}

static void writeSilenceFramesToI2S(size_t frameCount) {
    if (s_txChan == nullptr || !s_i2sEnabled || frameCount == 0) {
        return;
    }
    static const int16_t zeros[480] = {0}; // 240 stereo frames (960 bytes)
    size_t rem = frameCount;
    while (rem > 0) {
        size_t batch = (rem > 240) ? 240 : rem;
        size_t written = 0;
        i2s_channel_write(s_txChan, zeros, batch * 2 * sizeof(int16_t), &written, 200);
        rem -= batch;
    }
}

static void fadeOutToZeroI2S(int16_t fromSample) {
    if (fromSample == 0) {
        return;
    }
    int16_t ramp[120];
    for (int i = 0; i < 120; ++i) {
        float g = 1.0f - ((float)(i + 1) / 120.0f);
        ramp[i] = (int16_t)((float)fromSample * g);
    }
    writeMonoSamplesToI2S(ramp, 120);
}

// ==================== DOC CAM BIEN DHT11 ====================
static uint32_t waitPinLevel(int pin, int expectedLevel, uint32_t timeoutUs) {
    uint32_t start = micros();
    while (digitalRead(pin) != expectedLevel) {
        if ((uint32_t)(micros() - start) >= timeoutUs) {
            return 0;
        }
    }
    return (uint32_t)(micros() - start) + 1;
}

static portMUX_TYPE s_dhtMux = portMUX_INITIALIZER_UNLOCKED;

static bool readDHT11Once(float& outTempC, float& outHumPct) {
    uint8_t data[5] = {0, 0, 0, 0, 0};

    pinMode(PIN_DHT11_DATA, OUTPUT);
    digitalWrite(PIN_DHT11_DATA, LOW);
    vTaskDelay(pdMS_TO_TICKS(20));

    // Giai doan bat tay (~200us): khoa ngat ngan de bat dung xung xac nhan
    taskENTER_CRITICAL(&s_dhtMux);
    pinMode(PIN_DHT11_DATA, INPUT_PULLUP);
    delayMicroseconds(40);

    bool handshakeOk = waitPinLevel(PIN_DHT11_DATA, LOW, 90) &&
                       waitPinLevel(PIN_DHT11_DATA, HIGH, 95) &&
                       waitPinLevel(PIN_DHT11_DATA, LOW, 95);
    taskEXIT_CRITICAL(&s_dhtMux);
    if (!handshakeOk) {
        return false;
    }

    // Do tung bit rieng le (~120us/bit) de nha ngat I2S/Wi-Fi giua moi bit thay vi khoa lien tuc 4-5ms
    for (int i = 0; i < 40; ++i) {
        taskENTER_CRITICAL(&s_dhtMux);
        bool bitHigh = waitPinLevel(PIN_DHT11_DATA, HIGH, 95);
        uint32_t tStart = micros();
        bool bitLow = bitHigh ? (waitPinLevel(PIN_DHT11_DATA, LOW, 110) != 0) : false;
        uint32_t highUs = (uint32_t)(micros() - tStart);
        taskEXIT_CRITICAL(&s_dhtMux);

        if (!bitHigh || !bitLow) {
            return false;
        }
        data[i / 8] <<= 1;
        if (highUs > 45) {
            data[i / 8] |= 1;
        }
    }

    uint8_t checksum = (uint8_t)((data[0] + data[1] + data[2] + data[3]) & 0xFF);
    if (data[4] != checksum) {
        return false;
    }

    float hum = (float)data[0] + ((float)data[1] * 0.1f);
    float temp = (float)data[2] + ((float)(data[3] & 0x7F) * 0.1f);
    if (data[3] & 0x80) {
        temp = -temp;
    }
    if (hum < 5.0f || hum > 100.0f || temp < -10.0f || temp > 65.0f) {
        return false;
    }

    outHumPct = hum;
    outTempC = temp;
    return true;
}

static bool readDHT11(float& outTempC, float& outHumPct) {
    int maxAttempts = s_dbgPlaying ? 1 : 3;
    for (int attempt = 0; attempt < maxAttempts; ++attempt) {
        if (readDHT11Once(outTempC, outHumPct)) {
            return true;
        }
        if (attempt + 1 < maxAttempts) {
            vTaskDelay(pdMS_TO_TICKS(150));
        }
    }
    return false;
}

// ==================== GUI SU KIEN QUA WEBSOCKET (DU PHONG HTTP POST) ====================
static bool postJsonToOpenStick(const char* path, const String& jsonBody) {
    if (WiFi.status() != WL_CONNECTED) {
        return false;
    }
    WiFiClient client;
    HTTPClient http;
    String url = String("http://") + OPENSTICK_HOST + ":" + String(OPENSTICK_PORT) + path;
    if (!http.begin(client, url)) {
        return false;
    }
    http.setConnectTimeout(1500);
    http.setTimeout(2000);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(jsonBody);
    http.end();
    return code >= 200 && code < 300;
}

static bool enqueueWsJson(const String& jsonText) {
    if (!s_wsConnected || g_wsTxQueue == nullptr || jsonText.length() >= sizeof(WsTxMsg::json)) {
        return false;
    }
    WsTxMsg msg = {};
    memcpy(msg.json, jsonText.c_str(), jsonText.length() + 1);
    return xQueueSend(g_wsTxQueue, &msg, 0) == pdTRUE;
}

// ==================== TASK NUT CHAM TTP223 + CAM BIEN DHT11 ====================
static bool pollTouchButton(uint32_t nowMs) {
    static bool lastRaw = false;
    static bool stableState = false;
    static uint32_t lastDebounceAt = 0;
    static uint32_t pressStartedAt = 0;
    static bool longSentForPress = false;
    static uint8_t clickCount = 0;
    static uint32_t lastReleaseAt = 0;

    bool raw = (digitalRead(PIN_TOUCH_TTP223) == HIGH);
    if (raw != lastRaw) {
        lastDebounceAt = nowMs;
        lastRaw = raw;
    }

    if ((nowMs - lastDebounceAt) >= TOUCH_DEBOUNCE_MS && raw != stableState) {
        stableState = raw;
        if (stableState) {
            pressStartedAt = nowMs;
            longSentForPress = false;
        } else {
            if (!longSentForPress) {
                clickCount++;
                lastReleaseAt = nowMs;
                if (clickCount >= 2) {
                    uint8_t evt = 2;
                    xQueueSend(g_buttonQueue, &evt, 0);
                    clickCount = 0;
                }
            } else {
                clickCount = 0;
            }
        }
    }

    if (stableState && !longSentForPress && (nowMs - pressStartedAt) >= TOUCH_LONG_PRESS_MS) {
        longSentForPress = true;
        clickCount = 0;
        uint8_t evt = 3;
        xQueueSend(g_buttonQueue, &evt, 0);
    }

    if (!stableState && clickCount == 1 && (nowMs - lastReleaseAt) >= TOUCH_DOUBLE_GAP_MS) {
        uint8_t evt = 1;
        xQueueSend(g_buttonQueue, &evt, 0);
        clickCount = 0;
    }

    return raw || stableState || (clickCount > 0);
}

static void controlTask(void* param) {
    (void)param;
    uint32_t lastDhtAt = 0;
    bool firstTelemetrySent = false;
    static const uint32_t DHT_INTERVAL_MS = 60000;

    for (;;) {
        s_dbgCtrlLoops = s_dbgCtrlLoops + 1;
        uint32_t nowMs = millis();
        bool touchBusy = pollTouchButton(nowMs);
        if (touchBusy) {
            markPerformanceActivity(2500);
        }

        uint8_t btnEvt = 0;
        if (xQueueReceive(g_buttonQueue, &btnEvt, 0) == pdTRUE) {
            markPerformanceActivity(3000, true);
            const char* action = (btnEvt == 3) ? "long" : (btnEvt == 2 ? "double" : "single");
            String wsPayload = String("{\"type\":\"button\",\"button\":\"") + action + "\"}";
            bool sentViaWs = enqueueWsJson(wsPayload);
            bool ok = sentViaWs;
            if (!sentViaWs) {
                String httpPayload = String("{\"button\":\"") + action + "\"}";
                ok = postJsonToOpenStick("/api/esp32/event", httpPayload);
            }
            Serial.printf("[TOUCH] %s -> %s (%s)\n", action, sentViaWs ? "WS" : "HTTP", ok ? "OK" : "FAIL");
        }

        bool dueInitial = (!firstTelemetrySent && WiFi.status() == WL_CONNECTED && nowMs > 4000 &&
                           (lastDhtAt == 0 || (nowMs - lastDhtAt) >= 5000U));
        bool duePeriodic = (firstTelemetrySent && WiFi.status() == WL_CONNECTED && (nowMs - lastDhtAt) >= DHT_INTERVAL_MS);
        if (!g_otaInProgress && (dueInitial || duePeriodic)) {
            lastDhtAt = nowMs;
            float tempC = 0.0f;
            float humPct = 0.0f;
            if (readDHT11(tempC, humPct)) {
                firstTelemetrySent = true;
                int rssi = WiFi.RSSI();
                String wsJson = "{\"type\":\"telemetry\",\"temperature_c\":" + String(tempC, 1) +
                                ",\"humidity_pct\":" + String(humPct, 1) +
                                ",\"rssi\":" + String(rssi) + "}";
                bool sentViaWs = enqueueWsJson(wsJson);
                bool ok = sentViaWs;
                if (!sentViaWs) {
                    String httpJson = "{\"temperature_c\":" + String(tempC, 1) +
                                      ",\"humidity_pct\":" + String(humPct, 1) +
                                      ",\"rssi\":" + String(rssi) + "}";
                    ok = postJsonToOpenStick("/api/esp32/telemetry", httpJson);
                }
                Serial.printf("[DHT11] %.1f C, %.1f%%, RSSI=%d dBm -> %s (%s)\n",
                              tempC, humPct, rssi, sentViaWs ? "WS" : "HTTP", ok ? "SENT" : "FAIL");
            } else {
                Serial.printf("[DHT11] Doc cam bien chua thanh cong (kiem tra GPIO%d/nguon 3.3V)\n",
                              PIN_DHT11_DATA);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(touchBusy ? 10 : 20));
    }
}

// ==================== OPUS PACKET RING BUFFER (LENGTH-PREFIXED) ====================
static uint8_t s_opusRing[OPUS_RING_BYTES];
static volatile size_t   s_ringHead     = 0;
static volatile size_t   s_ringTail     = 0;
static volatile uint32_t s_opusPktCount = 0;
static portMUX_TYPE s_ringMux = portMUX_INITIALIZER_UNLOCKED;

static inline size_t ringUsedBytesLocked() {
    return (s_ringHead >= s_ringTail)
        ? (s_ringHead - s_ringTail)
        : (OPUS_RING_BYTES - s_ringTail + s_ringHead);
}

static void ringWriteRawLocked(const uint8_t* src, size_t len) {
    size_t first = OPUS_RING_BYTES - s_ringHead;
    if (first > len) first = len;
    memcpy(&s_opusRing[s_ringHead], src, first);
    if (len > first) {
        memcpy(&s_opusRing[0], src + first, len - first);
    }
    s_ringHead = (s_ringHead + len) % OPUS_RING_BYTES;
}

static void ringReadRawLocked(uint8_t* dst, size_t len) {
    size_t first = OPUS_RING_BYTES - s_ringTail;
    if (first > len) first = len;
    memcpy(dst, &s_opusRing[s_ringTail], first);
    if (len > first) {
        memcpy(dst + first, &s_opusRing[0], len - first);
    }
    s_ringTail = (s_ringTail + len) % OPUS_RING_BYTES;
}

static void opusRingReset() {
    portENTER_CRITICAL(&s_ringMux);
    s_ringHead = 0;
    s_ringTail = 0;
    s_opusPktCount = 0;
    portEXIT_CRITICAL(&s_ringMux);
}

static uint32_t opusPacketCount() {
    portENTER_CRITICAL(&s_ringMux);
    uint32_t c = s_opusPktCount;
    portEXIT_CRITICAL(&s_ringMux);
    return c;
}

static size_t opusRingUsedBytes() {
    portENTER_CRITICAL(&s_ringMux);
    size_t used = ringUsedBytesLocked();
    portEXIT_CRITICAL(&s_ringMux);
    return used;
}

static bool opusRingPush(const uint8_t* pkt, uint16_t len) {
    if (!pkt || len == 0 || len > OPUS_MAX_PACKET_BYTES) {
        return false;
    }
    size_t need = 2U + (size_t)len;
    bool pushed = false;
    portENTER_CRITICAL(&s_ringMux);
    size_t freeSpace = (OPUS_RING_BYTES - 1U) - ringUsedBytesLocked();
    if (freeSpace >= need) {
        uint8_t hdr[2] = { (uint8_t)(len & 0xFFU), (uint8_t)((len >> 8) & 0xFFU) };
        ringWriteRawLocked(hdr, 2);
        ringWriteRawLocked(pkt, len);
        s_opusPktCount = s_opusPktCount + 1U;
        if (s_opusPktCount > s_dbgPktMax) {
            s_dbgPktMax = s_opusPktCount;
        }
        pushed = true;
    } else {
        s_dbgRingDrops = s_dbgRingDrops + 1U;
    }
    portEXIT_CRITICAL(&s_ringMux);
    return pushed;
}

static uint16_t opusRingPop(uint8_t* out, uint16_t maxLen) {
    if (!out || maxLen == 0) {
        return 0;
    }
    uint16_t pktLen = 0;
    portENTER_CRITICAL(&s_ringMux);
    if (s_opusPktCount > 0 && ringUsedBytesLocked() >= 2U) {
        uint8_t hdr[2] = {0, 0};
        ringReadRawLocked(hdr, 2);
        pktLen = (uint16_t)hdr[0] | ((uint16_t)hdr[1] << 8);
        size_t avail = ringUsedBytesLocked();
        if (pktLen == 0 || pktLen > maxLen || pktLen > avail) {
            // Mat dong bo khung -> reset an toan
            s_ringHead = 0;
            s_ringTail = 0;
            s_opusPktCount = 0;
            pktLen = 0;
        } else {
            ringReadRawLocked(out, pktLen);
            s_opusPktCount = s_opusPktCount - 1U;
        }
    }
    portEXIT_CRITICAL(&s_ringMux);
    return pktLen;
}

// ==================== TASK GIAI MA OPUS -> I2S_STD DMA ====================
static void audioI2sTask(void* param) {
    (void)param;
    static uint8_t opusPkt[OPUS_MAX_PACKET_BYTES];
    static int16_t pcmOut[OPUS_FRAME_SAMPLES];

    int decErr = OPUS_OK;
    OpusDecoder* dec = opus_decoder_create((opus_int32)AUDIO_SAMPLE_RATE, 1, &decErr);
    if (!dec || decErr != OPUS_OK) {
        Serial.printf("[OPUS] Khong tao duoc OpusDecoder (err=%d)\n", decErr);
        vTaskDelete(nullptr);
        return;
    }

    bool playing = false;
    bool inRecovery = false;
    bool needFadeIn = false;
    bool dmaCleared = true;
    int16_t lastSample = 0;
    uint32_t quietSinceAt = millis();
    uint32_t emptyStreakSince = 0;
    uint32_t seenFlushEpoch = s_flushEpoch;

    for (;;) {
        s_dbgI2sLoops = s_dbgI2sLoops + 1U;

        if (g_otaInProgress) {
            if (!dmaCleared) {
                fadeOutToZeroI2S(lastSample);
                writeSilenceFramesToI2S(1440);
                dmaCleared = true;
            }
            setI2sActive(false);
            playing = false;
            inRecovery = false;
            s_dbgPlaying = false;
            lastSample = 0;
            emptyStreakSince = 0;
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        if (s_flushEpoch != seenFlushEpoch) {
            seenFlushEpoch = s_flushEpoch;
            if (playing) {
                fadeOutToZeroI2S(lastSample);
                writeSilenceFramesToI2S(240);
            }
            lastSample = 0;
            playing = false;
            s_dbgPlaying = false;
            inRecovery = false;
            emptyStreakSince = 0;
            dmaCleared = !s_i2sEnabled;
            quietSinceAt = millis();
            opus_decoder_ctl(dec, OPUS_RESET_STATE);
        }

        uint32_t nowMs = millis();
        if (!playing) {
            uint32_t pkts = opusPacketCount();
            uint32_t quietFor = (uint32_t)(nowMs - s_lastAudioRxAt);
            uint32_t needPkts = s_lowLatencyMode
                ? (inRecovery ? OPUS_LIVE_RESUME_PKTS : OPUS_LIVE_PREFILL_PKTS)
                : (inRecovery ? OPUS_RESUME_PACKETS : OPUS_PREFILL_PACKETS);
            if (pkts >= 1) {
                if (!s_i2sEnabled && setI2sActive(true)) {
                    writeSilenceFramesToI2S(480);
                    dmaCleared = false;
                }
                markPerformanceActivity(IDLE_POWER_HOLD_MS);
            }
            if (pkts >= needPkts || (pkts >= 1 && quietFor > 180)) {
                setI2sActive(true);
                playing = true;
                s_dbgPlaying = true;
                needFadeIn = true;
                emptyStreakSince = 0;
                dmaCleared = false;
                continue;
            }
            if (!dmaCleared && quietSinceAt > 0 && (uint32_t)(nowMs - quietSinceAt) >= STREAM_IDLE_SILENCE_MS) {
                writeSilenceFramesToI2S(1440);
                dmaCleared = true;
                inRecovery = false;
            }
            if (dmaCleared && s_i2sEnabled && pkts == 0 && quietSinceAt > 0 &&
                (uint32_t)(nowMs - quietSinceAt) >= I2S_IDLE_DISABLE_MS) {
                setI2sActive(false);
            }
            vTaskDelay(pdMS_TO_TICKS((pkts > 0) ? 5 : (s_powerEcoActive ? 25 : 10)));
            continue;
        }

        uint32_t pktsBeforePop = opusPacketCount();
        if (pktsBeforePop < s_dbgPktMin) {
            s_dbgPktMin = pktsBeforePop;
        }

        if (s_lowLatencyMode) {
            while (opusPacketCount() > OPUS_LIVE_MAX_QUEUE) {
                if (opusRingPop(opusPkt, sizeof(opusPkt)) == 0) {
                    break;
                }
            }
        }

        uint16_t pktLen = opusRingPop(opusPkt, sizeof(opusPkt));
        if (pktLen == 0) {
            if (emptyStreakSince == 0) {
                emptyStreakSince = nowMs;
                s_dbgEmptyDips = s_dbgEmptyDips + 1U;
            }
            if ((uint32_t)(nowMs - emptyStreakSince) >= 45) {
                fadeOutToZeroI2S(lastSample);
                writeSilenceFramesToI2S(240);
                lastSample = 0;
                playing = false;
                s_dbgPlaying = false;
                inRecovery = true;
                s_dbgUnderruns = s_dbgUnderruns + 1U;
                quietSinceAt = nowMs;
                emptyStreakSince = 0;
                dmaCleared = false;
                markPerformanceActivity(IDLE_POWER_HOLD_MS);
                opus_decoder_ctl(dec, OPUS_RESET_STATE);
            } else {
                vTaskDelay(pdMS_TO_TICKS(2));
            }
            continue;
        }

        emptyStreakSince = 0;
        dmaCleared = false;

        uint32_t tDec0 = micros();
        int decoded = opus_decode(dec, opusPkt, (opus_int32)pktLen, pcmOut, (int)OPUS_FRAME_SAMPLES, 0);
        uint32_t decUs = (uint32_t)(micros() - tDec0);
        s_dbgOpusDecLastUs = decUs;
        if (decUs > s_dbgOpusDecMaxUs) {
            s_dbgOpusDecMaxUs = decUs;
        }

        if (decoded <= 0) {
            s_dbgOpusErrors = s_dbgOpusErrors + 1U;
            vTaskDelay(pdMS_TO_TICKS(2));
            continue;
        }

        s_dbgOpusFrames = s_dbgOpusFrames + 1U;
        size_t nSamples = (size_t)decoded;
        if (AUDIO_OUTPUT_GAIN_Q8 < 256) {
            for (size_t i = 0; i < nSamples; ++i) {
                int32_t scaled = ((int32_t)pcmOut[i] * AUDIO_OUTPUT_GAIN_Q8) / 256;
                pcmOut[i] = (int16_t)scaled;
            }
        }
        if (needFadeIn) {
            size_t fadeLen = (nSamples < 120) ? nSamples : 120;
            for (size_t i = 0; i < fadeLen; ++i) {
                float g = (float)(i + 1) / (float)fadeLen;
                pcmOut[i] = (int16_t)((float)pcmOut[i] * g);
            }
            needFadeIn = false;
        }

        lastSample = pcmOut[nSamples - 1];
        writeMonoSamplesToI2S(pcmOut, nSamples);
    }
}

// ==================== TASK WEBSOCKET HAI CHIEU (OPUS DOWNLINK + CONTROL) ====================
static volatile bool s_wsGotFrame = false;
static uint32_t s_lastAudioPktAt = 0;
static bool s_wsHadConnectedOnce = false;

static void sendHelloJsonToWs() {
    String hello = "{\"type\":\"hello\",\"device\":\"esp32-c3-linh-dan\",\"codec\":\"opus\",\"sample_rate\":";
    hello += String(AUDIO_SAMPLE_RATE);
    hello += ",\"frame_ms\":";
    hello += String(OPUS_FRAME_MS);
    hello += ",\"rssi\":";
    hello += String(WiFi.RSSI());
    hello += "}";
    s_wsClient.sendTXT(hello);
}

static void onWebSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
    s_wsGotFrame = true;
    uint32_t nowMs = millis();

    switch (type) {
        case WStype_CONNECTED:
            s_wsConnected = true;
            s_lastAudioRxAt = nowMs;
            s_lastAudioPktAt = 0;
            markPerformanceActivity(3000, true);
            if (s_wsHadConnectedOnce) {
                s_dbgReconnects = s_dbgReconnects + 1U;
            }
            s_wsHadConnectedOnce = true;
            Serial.printf("[WS] Da ket noi ws://%s:%u%s\n", OPENSTICK_HOST, (unsigned)OPENSTICK_PORT, WS_AUDIO_PATH);
            sendHelloJsonToWs();
            break;

        case WStype_DISCONNECTED:
            s_wsConnected = false;
            s_lowLatencyMode = false;
            s_lastAudioPktAt = 0;
            markPerformanceActivity(3000);
            Serial.println("[WS] Mat ket noi WebSocket, se tu dong noi lai...");
            break;

        case WStype_BIN:
            if (payload && length > 1 && payload[0] == 0x01) {
                markPerformanceActivity(IDLE_POWER_HOLD_MS, true);
                uint16_t opusLen = (uint16_t)(length - 1U);
                // Cap nhat moc nhan truoc khi push vao ring de task I2S (uu tien cao hon) cho du prefill 300ms
                s_lastAudioRxAt = nowMs;
                if (opusRingPush(payload + 1, opusLen)) {
                    s_dbgRxBytes = s_dbgRxBytes + (uint32_t)opusLen;
                    if (s_dbgPlaying && s_lastAudioPktAt != 0) {
                        uint32_t gap = (uint32_t)(nowMs - s_lastAudioPktAt);
                        if (gap > s_dbgMaxRxGapMs) {
                            s_dbgMaxRxGapMs = gap;
                        }
                    }
                    s_lastAudioPktAt = nowMs;
                }
            }
            break;

        case WStype_TEXT:
            if (payload && length > 0) {
                char txtBuf[384];
                size_t copyLen = (length < (sizeof(txtBuf) - 1U)) ? length : (sizeof(txtBuf) - 1U);
                memcpy(txtBuf, payload, copyLen);
                txtBuf[copyLen] = '\0';
                if (strstr(txtBuf, "\"latency_mode\"") != nullptr) {
                    s_lowLatencyMode = (
                        strstr(txtBuf, "\"low_latency\":true") != nullptr
                        || strstr(txtBuf, "\"low_latency\": true") != nullptr
                    );
                }
                if (strstr(txtBuf, "\"audio_reset\"") != nullptr) {
                    opusRingReset();
                    s_flushEpoch = s_flushEpoch + 1U;
                    s_lastAudioPktAt = 0;
                    s_lastAudioRxAt = nowMs;
                } else if (
                    (strstr(txtBuf, "\"status\"") != nullptr || strstr(txtBuf, "\"state\"") != nullptr)
                    && strstr(txtBuf, "\"playing\"") == nullptr
                ) {
                    s_lastAudioPktAt = 0;
                }
            }
            break;

        default:
            break;
    }
}

static void audioWsTask(void* param) {
    (void)param;
    bool wsConfigured = false;
    uint8_t drainStreak = 0;

    for (;;) {
        s_dbgWsLoops = s_dbgWsLoops + 1U;

        if (g_otaInProgress || WiFi.status() != WL_CONNECTED) {
            // Ngat ket noi ke ca khi handshake do (wsConfigured == true && !s_wsConnected)
            // de tranh ro ri socket/heap trong WebSocketsClient khi Wi-Fi tat hoac OTA
            if (wsConfigured || s_wsConnected) {
                s_wsClient.disconnect();
                s_wsConnected = false;
                wsConfigured = false;
            }
            opusRingReset();
            s_flushEpoch = s_flushEpoch + 1U;
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        if (s_wsReconfigureReq) {
            s_wsReconfigureReq = false;
            if (wsConfigured || s_wsConnected) {
                s_wsClient.disconnect();
                s_wsConnected = false;
                wsConfigured = false;
            }
            opusRingReset();
            s_flushEpoch = s_flushEpoch + 1U;
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        if (!wsConfigured) {
            s_wsClient.begin(s_serverHost.c_str(), s_serverPort, WS_AUDIO_PATH);
            s_wsClient.onEvent(onWebSocketEvent);
            s_wsClient.setReconnectInterval(1500);
            s_wsClient.enableHeartbeat(15000, 5000, 2);
            wsConfigured = true;
        }

        s_wsGotFrame = false;
        s_wsClient.loop();

        if (s_wsConnected && g_wsTxQueue != nullptr) {
            WsTxMsg tx = {};
            for (int i = 0; i < 4; ++i) {
                if (xQueueReceive(g_wsTxQueue, &tx, 0) != pdTRUE) {
                    break;
                }
                s_wsClient.sendTXT(tx.json);
            }
        }

        if (s_wsGotFrame && drainStreak < 8) {
            drainStreak++;
            taskYIELD();
        } else {
            drainStreak = 0;
            vTaskDelay(pdMS_TO_TICKS((s_powerEcoActive && !s_dbgPlaying) ? 15 : 3));
        }
    }
}

// ==================== CAP NHAT FIRMWARE KHONG DAY (OTA) & DEBUG ====================
static bool g_otaUploadSuccess = false;
#ifdef OTA_AUTH_PASSWORD
static bool g_otaAuthRejected = false;
static bool requireOtaAuth() {
    if (!otaServer.authenticate("admin", OTA_AUTH_PASSWORD)) {
        otaServer.requestAuthentication();
        return false;
    }
    return true;
}
#endif

static void handleWebOtaPage() {
#ifdef OTA_AUTH_PASSWORD
    if (!requireOtaAuth()) {
        return;
    }
#endif
    String html;
    html.reserve(2500);
    html += F("<!DOCTYPE html><html lang='vi'><head><meta charset='utf-8'>"
              "<meta name='viewport' content='width=device-width,initial-scale=1'>"
              "<title>ESP32-C3 Smart Butler Speaker - OTA</title>"
              "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}"
              ".card{max-width:480px;margin:24px auto;background:#1e293b;border:1px solid #334155;border-radius:14px;padding:20px}"
              "h1{font-size:1.25rem;margin:0 0 12px;color:#f8fafc}"
              ".meta{font-size:.9rem;line-height:1.6;color:#94a3b8;margin-bottom:16px}"
              ".badge{display:inline-block;background:#0ea5e9;color:#082f49;font-weight:700;padding:2px 8px;border-radius:999px;font-size:.78rem}"
              "input[type=file]{width:100%;padding:10px;background:#0f172a;border:1px dashed #475569;border-radius:8px;color:#e2e8f0;box-sizing:border-box}"
              "button{margin-top:12px;width:100%;padding:11px;border:none;border-radius:8px;background:#38bdf8;color:#082f49;font-weight:700;font-size:1rem;cursor:pointer}"
              "button:disabled{opacity:.6;cursor:wait}"
              "#status{margin-top:12px;font-size:.9rem;color:#cbd5e1}</style></head><body><div class='card'>"
              "<h1>Smart Butler Loa • Opus + WebSocket OTA <span class='badge'>Wi-Fi</span></h1><div class='meta'>"
              "<b>Hostname:</b> ");
    html += OTA_HOSTNAME;
    html += F(".local<br><b>IP:</b> ");
    html += WiFi.localIP().toString();
    html += F("<br><b>Wi-Fi:</b> ");
    html += WiFi.SSID();
    html += F(" (");
    html += String(WiFi.RSSI());
    html += F(" dBm)<br><b>Sơ đồ chân:</b> MAX98357 BCLK=");
    html += String(PIN_I2S_BCLK);
    html += F(", LRC=");
    html += String(PIN_I2S_LRC);
    html += F(", DIN=");
    html += String(PIN_I2S_DIN);
    html += F(" | DHT11=");
    html += String(PIN_DHT11_DATA);
    html += F(" | Touch=");
    html += String(PIN_TOUCH_TTP223);
    html += F("<br><b>Server WS:</b> ws://");
    html += s_serverHost;
    html += F(":");
    html += String((unsigned)s_serverPort);
    html += WS_AUDIO_PATH;
    html += F(" (");
    html += (s_wsConnected ? F("Đã nối") : F("Đang chờ"));
    html += F(")<br><b>Vùng OTA trống:</b> ");
    html += String(ESP.getFreeSketchSpace() / 1024);
    html += F(" KB | <b>Uptime:</b> ");
    html += String(millis() / 1000);
    float chipTempC = readChipTempC();
    html += F("s<br><b>Chip ESP32-C3:</b> ");
    html += isnan(chipTempC) ? String("N/A") : (String(chipTempC, 1) + " °C");
    html += F(" | ");
    html += String((unsigned)getCpuFrequencyMhz());
    html += F(" MHz | <b>Chế độ nguồn:</b> ");
    html += (s_powerEcoActive ? F("Eco Idle (Modem Sleep)") : F("Active (160 MHz)"));
    html += F("</div>"
              "<form id='f' method='POST' action='/update' enctype='multipart/form-data'>"
              "<input id='bin' type='file' name='firmware' accept='.bin' required>"
              "<button id='btn' type='submit'>Nạp Firmware (.bin) Qua Wi-Fi</button></form>"
              "<div id='status'>Chọn file <code>firmware.bin</code> để cập nhật không cần cắm cáp USB.</div>"
              "<script>const f=document.getElementById('f'),s=document.getElementById('status'),b=document.getElementById('btn');"
              "f.onsubmit=e=>{e.preventDefault();const file=document.getElementById('bin').files[0];if(!file)return;"
              "const x=new XMLHttpRequest();x.open('POST','/update');b.disabled=true;"
              "x.upload.onprogress=ev=>{if(ev.lengthComputable)s.textContent='Đang tải lên: '+Math.round(ev.loaded*100/ev.total)+'%';};"
              "x.onload=()=>{b.disabled=false;s.textContent=x.status===200?'Thành công! ESP32-C3 đang khởi động lại...':('Lỗi HTTP '+x.status+': '+x.responseText);};"
              "x.onerror=()=>{b.disabled=false;s.textContent='Mất kết nối trong khi nạp (nếu nạp xong, ESP32-C3 đang tự khởi động lại).';};"
              "const fd=new FormData();fd.append('firmware',file);x.send(fd);};</script></div></body></html>");
    otaServer.send(200, "text/html; charset=utf-8", html);
}

static void handleHealthJson() {
    float chipTempC = readChipTempC();
    String json = "{\"ok\":true,\"codec\":\"opus\",\"transport\":\"ws\",\"hostname\":\"";
    json += OTA_HOSTNAME;
    json += "\",\"ip\":\"";
    json += WiFi.localIP().toString();
    json += "\",\"mac\":\"";
    json += WiFi.macAddress();
    json += "\",\"server_host\":\"";
    json += s_serverHost;
    json += "\",\"server_port\":";
    json += String((unsigned)s_serverPort);
    json += ",\"ws_connected\":";
    json += (s_wsConnected ? "true" : "false");
    json += ",\"ssid\":\"";
    json += WiFi.SSID();
    json += "\",\"rssi\":";
    json += String(WiFi.RSSI());
    json += ",\"uptime_s\":";
    json += String(millis() / 1000);
    json += ",\"chip_temp_c\":";
    json += isnan(chipTempC) ? String("null") : String(chipTempC, 1);
    json += ",\"cpu_mhz\":";
    json += String((unsigned)getCpuFrequencyMhz());
    json += ",\"eco_idle\":";
    json += (s_powerEcoActive ? "true" : "false");
    json += ",\"wifi_sleep\":\"";
    json += (s_powerEcoActive ? "min_modem" : "none");
    json += "\",\"tx_power_dbm\":";
    json += String((float)s_curTxPower * 0.25f, 1);
    json += ",\"i2s_active\":";
    json += (s_i2sEnabled ? "true" : "false");
    json += ",\"output_gain_pct\":";
    json += String((int)AUDIO_OUTPUT_GAIN_PCT);
    json += ",\"free_ota_kb\":";
    json += String(ESP.getFreeSketchSpace() / 1024);
    json += ",\"pins\":{\"bclk\":";
    json += String(PIN_I2S_BCLK);
    json += ",\"lrc\":";
    json += String(PIN_I2S_LRC);
    json += ",\"din\":";
    json += String(PIN_I2S_DIN);
    json += ",\"dht11\":";
    json += String(PIN_DHT11_DATA);
    json += ",\"touch\":";
    json += String(PIN_TOUCH_TTP223);
    json += "}}";
    otaServer.send(200, "application/json", json);
}

static void handleDebugJson() {
    if (otaServer.hasArg("reset") && otaServer.arg("reset") == "1") {
        s_dbgUnderruns = 0;
        s_dbgEmptyDips = 0;
        s_dbgReconnects = 0;
        s_dbgRxBytes = 0;
        s_dbgMaxRxGapMs = 0;
        s_dbgPktMin = 0xFFFFFFFFUL;
        s_dbgPktMax = 0;
        s_dbgRingDrops = 0;
        s_dbgOpusFrames = 0;
        s_dbgOpusErrors = 0;
        s_dbgOpusDecLastUs = 0;
        s_dbgOpusDecMaxUs = 0;
        s_dbgI2sLoops = 0;
        s_dbgWsLoops = 0;
        s_dbgCtrlLoops = 0;
    }

    uint32_t pkts = opusPacketCount();
    uint32_t pktMin = (s_dbgPktMin == 0xFFFFFFFFUL) ? pkts : (uint32_t)s_dbgPktMin;
    size_t usedBytes = opusRingUsedBytes();
    uint32_t stackI2s = s_i2sTaskHandle ? (uint32_t)uxTaskGetStackHighWaterMark(s_i2sTaskHandle) : 0;
    uint32_t stackWs  = s_wsTaskHandle  ? (uint32_t)uxTaskGetStackHighWaterMark(s_wsTaskHandle)  : 0;

    float chipTempC = readChipTempC();
    String json;
    json.reserve(680);
    json += "{\"codec\":\"opus\",\"output_gain_pct\":";
    json += String((int)AUDIO_OUTPUT_GAIN_PCT);
    json += ",\"chip_temp_c\":";
    json += isnan(chipTempC) ? String("null") : String(chipTempC, 1);
    json += ",\"cpu_mhz\":";
    json += String((unsigned)getCpuFrequencyMhz());
    json += ",\"eco_idle\":";
    json += (s_powerEcoActive ? "true" : "false");
    json += ",\"wifi_sleep\":\"";
    json += (s_powerEcoActive ? "min_modem" : "none");
    json += "\",\"tx_power_dbm\":";
    json += String((float)s_curTxPower * 0.25f, 1);
    json += ",\"i2s_active\":";
    json += (s_i2sEnabled ? "true" : "false");
    json += ",\"ws_connected\":";
    json += (s_wsConnected ? "true" : "false");
    json += ",\"ring_pkts\":";
    json += String((unsigned)pkts);
    json += ",\"ring_pkts_min_play\":";
    json += String((unsigned)pktMin);
    json += ",\"ring_pkts_max\":";
    json += String((unsigned)s_dbgPktMax);
    json += ",\"ring_bytes\":";
    json += String((unsigned)usedBytes);
    json += ",\"ring_cap\":";
    json += String((unsigned)(OPUS_RING_BYTES - 1));
    json += ",\"ring_drops\":";
    json += String((unsigned)s_dbgRingDrops);
    json += ",\"underruns\":";
    json += String((unsigned)s_dbgUnderruns);
    json += ",\"empty_dips\":";
    json += String((unsigned)s_dbgEmptyDips);
    json += ",\"opus_frames\":";
    json += String((unsigned)s_dbgOpusFrames);
    json += ",\"opus_errors\":";
    json += String((unsigned)s_dbgOpusErrors);
    json += ",\"opus_dec_last_us\":";
    json += String((unsigned)s_dbgOpusDecLastUs);
    json += ",\"opus_dec_max_us\":";
    json += String((unsigned)s_dbgOpusDecMaxUs);
    json += ",\"reconnects\":";
    json += String((unsigned)s_dbgReconnects);
    json += ",\"rx_bytes\":";
    json += String((unsigned)s_dbgRxBytes);
    json += ",\"max_rx_gap_ms\":";
    json += String((unsigned)s_dbgMaxRxGapMs);
    json += ",\"playing\":";
    json += (s_dbgPlaying ? "true" : "false");
    json += ",\"i2s_loops\":";
    json += String((unsigned)s_dbgI2sLoops);
    json += ",\"ws_loops\":";
    json += String((unsigned)s_dbgWsLoops);
    json += ",\"ctrl_loops\":";
    json += String((unsigned)s_dbgCtrlLoops);
    json += ",\"stack_i2s_min\":";
    json += String((unsigned)stackI2s);
    json += ",\"stack_ws_min\":";
    json += String((unsigned)stackWs);
    json += ",\"free_heap\":";
    json += String((unsigned)ESP.getFreeHeap());
    json += ",\"rssi\":";
    json += String(WiFi.RSSI());
    json += ",\"uptime_s\":";
    json += String(millis() / 1000);
    json += "}";
    otaServer.send(200, "application/json", json);
}

static void handleSetServerJson() {
    String host = otaServer.hasArg("host") ? otaServer.arg("host") : "";
    String portStr = otaServer.hasArg("port") ? otaServer.arg("port") : "";
    if (host.length() == 0 && otaServer.hasArg("plain")) {
        String body = otaServer.arg("plain");
        int hIdx = body.indexOf("\"host\"");
        if (hIdx >= 0) {
            int col = body.indexOf(':', hIdx);
            int q1 = body.indexOf('"', col + 1);
            int q2 = body.indexOf('"', q1 + 1);
            if (col >= 0 && q1 >= 0 && q2 > q1) {
                host = body.substring(q1 + 1, q2);
            }
        }
        int pIdx = body.indexOf("\"port\"");
        if (pIdx >= 0) {
            int col = body.indexOf(':', pIdx);
            if (col >= 0) {
                String tail = body.substring(col + 1);
                tail.trim();
                portStr = String(tail.toInt());
            }
        }
    }
    host.trim();
    if (!isValidServerHost(host)) {
        otaServer.send(400, "application/json", "{\"ok\":false,\"error\":\"invalid_host\"}");
        return;
    }
    long parsedPort = portStr.toInt();
    uint16_t port = (parsedPort > 0 && parsedPort <= 65535) ? (uint16_t)parsedPort : (uint16_t)OPENSTICK_PORT;

    bool changed = (host != s_serverHost) || (port != s_serverPort);
    s_serverHost = host;
    s_serverPort = port;
    saveServerConfigToNvs(host, port);
    if (changed || !s_wsConnected) {
        s_wsReconfigureReq = true;
    }

    String resp = "{\"ok\":true,\"server_host\":\"";
    resp += s_serverHost;
    resp += "\",\"server_port\":";
    resp += String((unsigned)s_serverPort);
    resp += ",\"ip\":\"";
    resp += WiFi.localIP().toString();
    resp += "\",\"mac\":\"";
    resp += WiFi.macAddress();
    resp += "\",\"hostname\":\"";
    resp += OTA_HOSTNAME;
    resp += "\",\"reconnecting\":";
    resp += ((changed || !s_wsConnected) ? "true" : "false");
    resp += "}";
    otaServer.send(200, "application/json", resp);
}

static void setupOTA() {
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPort(OTA_PORT);
#ifdef OTA_AUTH_PASSWORD
    ArduinoOTA.setPassword(OTA_AUTH_PASSWORD);
#endif
    ArduinoOTA.onStart([]() {
        g_otaInProgress = true;
        markPerformanceActivity(60000, true);
        Serial.println("[OTA] Bat dau cap nhat firmware qua ArduinoOTA...");
    });
    ArduinoOTA.onEnd([]() {
        Serial.println("\n[OTA] Cap nhat thanh cong! Dang khoi dong lai...");
    });
    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
        static uint8_t lastPct = 255;
        markPerformanceActivity(15000);
        uint8_t pct = (total > 0) ? (uint8_t)((progress * 100U) / total) : 0;
        if (lastPct == 255 || pct == 100 || pct >= (uint8_t)(lastPct + 10)) {
            lastPct = pct;
            Serial.printf("[OTA] Tien do: %u%%\n", pct);
        }
    });
    ArduinoOTA.onError([](ota_error_t error) {
        g_otaInProgress = false;
        Serial.printf("[OTA] Loi[%u]\n", (unsigned)error);
    });
    ArduinoOTA.begin();

    otaServer.on("/", HTTP_GET, handleWebOtaPage);
    otaServer.on("/health", HTTP_GET, handleHealthJson);
    otaServer.on("/debug", HTTP_GET, handleDebugJson);
    otaServer.on("/set_server", HTTP_GET, handleSetServerJson);
    otaServer.on("/set_server", HTTP_POST, handleSetServerJson);
    otaServer.on(
        "/update",
        HTTP_POST,
        []() {
#ifdef OTA_AUTH_PASSWORD
            if (g_otaAuthRejected || !requireOtaAuth()) {
                if (!g_otaAuthRejected) {
                    // requireOtaAuth() da gui HTTP 401
                } else {
                    g_otaAuthRejected = false;
                    otaServer.requestAuthentication();
                }
                g_otaUploadSuccess = false;
                g_otaInProgress = false;
                return;
            }
#endif
            bool ok = g_otaUploadSuccess && !Update.hasError();
            otaServer.sendHeader("Connection", "close");
            otaServer.send(ok ? 200 : 500, "text/plain", ok ? "OK" : "OTA_UPDATE_FAILED");
            if (ok) {
                delay(500);
                ESP.restart();
            } else {
                g_otaInProgress = false;
            }
        },
        []() {
            HTTPUpload& upload = otaServer.upload();
#ifdef OTA_AUTH_PASSWORD
            if (upload.status == UPLOAD_FILE_START) {
                if (!otaServer.authenticate("admin", OTA_AUTH_PASSWORD)) {
                    g_otaAuthRejected = true;
                    g_otaUploadSuccess = false;
                    g_otaInProgress = false;
                    return;
                }
                g_otaAuthRejected = false;
            } else if (g_otaAuthRejected) {
                return;
            }
#endif
            if (upload.status == UPLOAD_FILE_START) {
                g_otaUploadSuccess = false;
                g_otaInProgress = true;
                markPerformanceActivity(60000, true);
                Serial.printf("[WEB-OTA] Bat dau nap: %s\n", upload.filename.c_str());
                if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
                    Update.printError(Serial);
                }
            } else if (upload.status == UPLOAD_FILE_WRITE) {
                markPerformanceActivity(15000);
                if (Update.isRunning()) {
                    if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
                        Update.printError(Serial);
                    }
                }
            } else if (upload.status == UPLOAD_FILE_END) {
                if (Update.end(true)) {
                    g_otaUploadSuccess = true;
                    Serial.printf("[WEB-OTA] Nap xong %u bytes. Khoi dong lai...\n",
                                  (unsigned)upload.totalSize);
                } else {
                    Update.printError(Serial);
                    g_otaUploadSuccess = false;
                    g_otaInProgress = false;
                }
            } else if (upload.status == UPLOAD_FILE_ABORTED) {
                Update.abort();
                g_otaUploadSuccess = false;
                g_otaInProgress = false;
                Serial.println("[WEB-OTA] Da huy nap firmware.");
            }
        }
    );
    otaServer.begin();
    Serial.printf("[OTA] San sang! Web OTA: http://%s/ | ArduinoOTA: %s.local:%u\n",
                  WiFi.localIP().toString().c_str(), OTA_HOSTNAME, (unsigned)OTA_PORT);
}

void setup() {
    Serial.begin(115200);
    delay(400);
    Serial.println("\n=== SMART BUTLER ESP32-C3 SPEAKER (OPUS + WS + I2S_STD) ===");

    pinMode(PIN_TOUCH_TTP223, INPUT_PULLDOWN);
    pinMode(PIN_DHT11_DATA, INPUT_PULLUP);

    s_pmMutex     = xSemaphoreCreateMutex();
    g_buttonQueue = xQueueCreate(8, sizeof(uint8_t));
    g_wsTxQueue   = xQueueCreate(12, sizeof(WsTxMsg));
    initChipTempSensor();
    initI2S();

    WiFi.mode(WIFI_STA);
    WiFi.setHostname(OTA_HOSTNAME);
    wifi_country_t vnCountry = {};
    memcpy(vnCountry.cc, "VN\0", 3);
    vnCountry.schan = 1;
    vnCountry.nchan = 13;
    vnCountry.max_tx_power = 20;
    vnCountry.policy = WIFI_COUNTRY_POLICY_MANUAL;
    esp_wifi_set_country(&vnCountry);
    WiFi.setTxPower(s_curTxPower);
    WiFi.setSleep(WIFI_PS_NONE);

    wifiMulti.addAP(WIFI_SSID_PRIMARY, WIFI_PASS_PRIMARY);
    if (strlen(WIFI_SSID_BACKUP) > 0) {
        wifiMulti.addAP(WIFI_SSID_BACKUP, WIFI_PASS_BACKUP);
    }

    Serial.print("[WIFI] Dang ket noi Wi-Fi");
    uint32_t wifiStart = millis();
    while (wifiMulti.run() != WL_CONNECTED && (millis() - wifiStart) < 15000) {
        delay(300);
        Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WIFI] Da ket noi! IP=%s, RSSI=%d dBm\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        updateAdaptiveTxPower(millis());
    } else {
        Serial.println("\n[WIFI] Chua ket noi duoc, se tiep tuc thu trong loop().");
    }

    setupOTA();
    loadServerConfigFromNvs();
    markPerformanceActivity(IDLE_POWER_HOLD_MS);

    // ESP32-C3 (1 nhan RISC-V): I2S+Opus muc 4, WebSocket muc 3, cam bien/nut cham muc 2
    xTaskCreate(controlTask, "ctrl_task", 6144, nullptr, 2, nullptr);
    xTaskCreate(audioWsTask, "ws_audio", 8192, nullptr, 3, &s_wsTaskHandle);
    xTaskCreate(audioI2sTask, "i2s_opus", 20480, nullptr, 4, &s_i2sTaskHandle);
}

void loop() {
    uint32_t nowMs = millis();
    static uint32_t lastWifiCheckAt = 0;
    if (WiFi.status() != WL_CONNECTED && (nowMs - lastWifiCheckAt) >= 1000) {
        lastWifiCheckAt = nowMs;
        wifiMulti.run();
    }
    updateAdaptiveTxPower(nowMs);
    updatePowerProfile(nowMs);
    ArduinoOTA.handle();
    otaServer.handleClient();
    delay(s_powerEcoActive ? 50 : 20);
}
