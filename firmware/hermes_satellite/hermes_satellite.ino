/*
 * Hermes Voice Satellite — CLASSIC ESP32 (ESP-WROOM-32) build
 * ----------------------------------------------------------------------------
 * Push-to-talk: hold the button, speak, release. Records ~3 s of 16 kHz mono
 * audio from an INMP441 I2S mic, POSTs it to the Hermes bridge on your PC, and
 * STREAMS the spoken reply straight to a MAX98357A I2S amp + speaker.
 *
 * WiFi is configured on first boot via a captive portal (no password in code):
 *   1. Power on. The board makes a WiFi network called "Hermes-Setup".
 *   2. Connect your phone to it, a page opens, pick your WiFi + enter password.
 *   3. It reboots and connects. (To redo: hold the button while powering on.)
 *
 * NOTE: This chip has no PSRAM, so on-device wake word is not available —
 * push-to-talk only. Pins avoid GPIO6-11 (wired to flash on classic ESP32).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include "secrets.h"              // WIFI_SSID / WIFI_PASS (generated locally)
#include "config.h"               // bridge/token/behavior settings
#include <driver/i2s.h>

// Resolved at runtime by mDNS; falls back to the fixed IP above if not found.
String   g_bridgeHost = BRIDGE_HOST;
uint16_t g_bridgePort = BRIDGE_PORT;

// ── Pins — CLASSIC ESP32 safe map (also printed on the expansion board) ─────
// Bottom edge of the 30-pin board: … D14 D27 D26 | D25 D33 D32 …
// Mic = first trio (D14/D27/D26), Amp = next trio (D25/D33/D32) — 6 pins in a row.
#undef MIC_BCLK_PIN
#undef MIC_WS_PIN
#undef MIC_DATA_PIN
#undef AMP_BCLK_PIN
#undef AMP_WS_PIN
#undef AMP_DATA_PIN
#undef STATUS_LED_PIN
#define MIC_SCK_PIN   14   // INMP441 SCK  → D14
#define MIC_WS_PIN    27   // INMP441 WS   → D27
#define MIC_SD_PIN    26   // INMP441 SD   → D26
#define AMP_BCLK_PIN  25   // MAX98357A BCLK → D25
#define AMP_WS_PIN    33   // MAX98357A LRC  → D33
#define AMP_DIN_PIN   32   // MAX98357A DIN  → D32
#define PTT_PIN        0   // onboard BOOT button (GPIO0) — press & hold to talk, no wiring
#define STATUS_LED_PIN 2   // status LED via 220 ohm

// ── Audio ───────────────────────────────────────────────────────────────────
#ifndef RECORD_SECONDS
#define RECORD_SECONDS MAX_RECORD_SECONDS
#endif
#define WAV_HEADER_SIZE 44

#define I2S_MIC_PORT  I2S_NUM_0
#define I2S_AMP_PORT  I2S_NUM_1

static uint32_t TOTAL_SAMPLES = SAMPLE_RATE * RECORD_SECONDS * NUM_CHANNELS;
static uint32_t PCM_BYTES     = TOTAL_SAMPLES * (BITS_PER_SAMPLE / 8);
static uint32_t WAV_TOTAL     = WAV_HEADER_SIZE + PCM_BYTES;

// ── forward decls ───────────────────────────────────────────────────────────
void setupWiFi();
void discoverBridge();
bool setupMicI2S();
bool setupAmpI2S();
void buildWAVHeader(uint8_t* h, uint32_t pcmBytes, uint32_t sr);
void recordWAV(uint8_t* buf);
bool postAndPlay(const uint8_t* wav, size_t len);
void blinkError(int n);
void led(bool on) { digitalWrite(STATUS_LED_PIN, on ? HIGH : LOW); }

// ── setup ───────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[Hermes] booting (classic ESP32)…");

  pinMode(STATUS_LED_PIN, OUTPUT);
  led(false);
  pinMode(PTT_PIN, INPUT_PULLUP);

  setupWiFi();
  MDNS.begin("hermes-satellite");
  discoverBridge();      // find the PC brain by name; falls back to the fixed IP

  if (!setupMicI2S()) { Serial.println("[ERR] mic I2S"); while (true){ blinkError(3); delay(1500);} }
  if (!setupAmpI2S()) { Serial.println("[ERR] amp I2S"); while (true){ blinkError(5); delay(1500);} }

  Serial.println("[Hermes] ready — hold the button and speak.");
  for (int i=0;i<2;i++){ led(true); delay(80); led(false); delay(80); }
}

// ── loop ────────────────────────────────────────────────────────────────────
void loop() {
  if (digitalRead(PTT_PIN) != LOW) return;
  delay(30);
  if (digitalRead(PTT_PIN) != LOW) return;
  Serial.println("[PTT] recording…");

  uint8_t* wav = (uint8_t*)malloc(WAV_TOTAL);
  if (!wav) {                        // low RAM fallback: try 2 seconds
    Serial.println("[MEM] 3s failed, trying 2s");
    TOTAL_SAMPLES = SAMPLE_RATE * 2; PCM_BYTES = TOTAL_SAMPLES * 2; WAV_TOTAL = WAV_HEADER_SIZE + PCM_BYTES;
    wav = (uint8_t*)malloc(WAV_TOTAL);
  }
  if (!wav) { Serial.println("[MEM] out of memory"); blinkError(6); return; }

  led(true);
  recordWAV(wav);
  led(false);

  bool ok = postAndPlay(wav, WAV_TOTAL);
  free(wav);
  if (!ok) { blinkError(4); discoverBridge(); }   // couldn't reach it — maybe its IP moved; re-find

  Serial.println("[PTT] done — release button");
  while (digitalRead(PTT_PIN) == LOW) delay(10);
}

// ── WiFi (direct connect) ───────────────────────────────────────────────────
void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.onEvent([](WiFiEvent_t e, WiFiEventInfo_t info){
    Serial.printf("[WiFi] DISCONNECT reason=%d\n", info.wifi_sta_disconnected.reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  // Diagnostic: list every 2.4 GHz network the ESP32 can see
  Serial.println("[WiFi] scanning for 2.4 GHz networks…");
  int n = WiFi.scanNetworks();
  bool foundTarget = false;
  for (int i = 0; i < n; i++) {
    String s = WiFi.SSID(i);
    if (s == WIFI_SSID) foundTarget = true;
    Serial.printf("   %2d) ch%2d %4d dBm  %s  \"%s\"\n", i + 1, WiFi.channel(i),
                  WiFi.RSSI(i), (WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "enc "), s.c_str());
  }
  Serial.printf("[WiFi] target \"%s\" %s in 2.4 GHz scan\n",
                WIFI_SSID, foundTarget ? "FOUND ✓" : "NOT found ✗ (still 5 GHz / out of range)");

  Serial.printf("[WiFi] connecting to \"%s\"…\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    led(true); delay(120); led(false); delay(120);
    Serial.print('.');
    if (millis() - t0 > 30000) {
      Serial.println("\n[WiFi] failed — rebooting to retry");
      delay(1000); ESP.restart();
    }
  }
  Serial.printf("\n[WiFi] connected, IP %s\n", WiFi.localIP().toString().c_str());
}

// ── Find the PC brain by name (mDNS: _hermes._tcp) ──────────────────────────
void discoverBridge() {
  Serial.println("[mDNS] looking for the Hermes brain…");
  for (int attempt = 0; attempt < 3; attempt++) {
    int n = MDNS.queryService("hermes", "tcp");   // matches the bridge's _hermes._tcp
    if (n > 0) {
      g_bridgeHost = MDNS.address(0).toString();
      g_bridgePort = MDNS.port(0);
      Serial.printf("[mDNS] found brain at %s:%u\n", g_bridgeHost.c_str(), g_bridgePort);
      return;
    }
    delay(600);
  }
  g_bridgeHost = BRIDGE_HOST;
  g_bridgePort = BRIDGE_PORT;
  Serial.printf("[mDNS] not found — using fixed %s:%u\n", BRIDGE_HOST, BRIDGE_PORT);
}

// ── mic (I2S RX) ────────────────────────────────────────────────────────────
bool setupMicI2S() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,   // INMP441 = 32-bit frames
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8, .dma_buf_len = 256,
    .use_apll = false, .tx_desc_auto_clear = false, .fixed_mclk = 0,
  };
  i2s_pin_config_t pins = {
    .mck_io_num = I2S_PIN_NO_CHANGE,   // no master clock (frees GPIO0 / BOOT button)
    .bck_io_num = MIC_SCK_PIN, .ws_io_num = MIC_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = MIC_SD_PIN,
  };
  if (i2s_driver_install(I2S_MIC_PORT, &cfg, 0, nullptr) != ESP_OK) return false;
  if (i2s_set_pin(I2S_MIC_PORT, &pins) != ESP_OK) return false;
  i2s_zero_dma_buffer(I2S_MIC_PORT);
  return true;
}

// ── amp (I2S TX) ────────────────────────────────────────────────────────────
bool setupAmpI2S() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8, .dma_buf_len = 256,
    .use_apll = false, .tx_desc_auto_clear = true, .fixed_mclk = 0,
  };
  i2s_pin_config_t pins = {
    .mck_io_num = I2S_PIN_NO_CHANGE,   // no master clock (frees GPIO0 / BOOT button)
    .bck_io_num = AMP_BCLK_PIN, .ws_io_num = AMP_WS_PIN,
    .data_out_num = AMP_DIN_PIN, .data_in_num = I2S_PIN_NO_CHANGE,
  };
  if (i2s_driver_install(I2S_AMP_PORT, &cfg, 0, nullptr) != ESP_OK) return false;
  if (i2s_set_pin(I2S_AMP_PORT, &pins) != ESP_OK) return false;
  i2s_zero_dma_buffer(I2S_AMP_PORT);
  return true;
}

// ── WAV header ──────────────────────────────────────────────────────────────
void buildWAVHeader(uint8_t* h, uint32_t pcmBytes, uint32_t sr) {
  uint32_t byteRate = sr * NUM_CHANNELS * (BITS_PER_SAMPLE/8);
  uint16_t blockAlign = NUM_CHANNELS * (BITS_PER_SAMPLE/8);
  uint32_t chunk = 36 + pcmBytes;
  memcpy(h, "RIFF", 4);
  h[4]=chunk; h[5]=chunk>>8; h[6]=chunk>>16; h[7]=chunk>>24;
  memcpy(h+8, "WAVEfmt ", 8);
  h[16]=16;h[17]=0;h[18]=0;h[19]=0; h[20]=1;h[21]=0;
  h[22]=NUM_CHANNELS;h[23]=0;
  h[24]=sr;h[25]=sr>>8;h[26]=sr>>16;h[27]=sr>>24;
  h[28]=byteRate;h[29]=byteRate>>8;h[30]=byteRate>>16;h[31]=byteRate>>24;
  h[32]=blockAlign;h[33]=0; h[34]=BITS_PER_SAMPLE;h[35]=0;
  memcpy(h+36, "data", 4);
  h[40]=pcmBytes;h[41]=pcmBytes>>8;h[42]=pcmBytes>>16;h[43]=pcmBytes>>24;
}

// ── record ──────────────────────────────────────────────────────────────────
void recordWAV(uint8_t* buf) {
  buildWAVHeader(buf, PCM_BYTES, SAMPLE_RATE);
  int16_t* dst = (int16_t*)(buf + WAV_HEADER_SIZE);
  const size_t CH = 256;
  int32_t raw[CH];
  uint32_t left = TOTAL_SAMPLES, idx = 0;
  int32_t peak = 0, rawPeak = 0; uint64_t sumSq = 0;
  while (left > 0) {
    size_t want = (left < CH ? left : CH), n = 0;
    i2s_read(I2S_MIC_PORT, raw, want*sizeof(int32_t), &n, portMAX_DELAY);
    size_t got = n / sizeof(int32_t);
    for (size_t i=0;i<got;i++) {
      int32_t s = raw[i] >> MIC_GAIN_SHIFT;
      if (s > 32767) s = 32767; else if (s < -32768) s = -32768;   // clip instead of wrapping
      dst[idx++] = (int16_t)s;
      int32_t a = s < 0 ? -s : s;           if (a > peak) peak = a;
      int32_t r = raw[i] < 0 ? -(raw[i]>>1) : (raw[i]>>1); if (r > rawPeak) rawPeak = r;
      sumSq += (uint64_t)((int64_t)s * s);
    }
    left -= got;
  }
  // Mic level check: speech is typically peak > 2000. Peak ~0 = mic not wired / no data.
  Serial.printf("[Rec] %u samples, peak %d, rms %u, raw peak 0x%08X\n", (unsigned)TOTAL_SAMPLES,
                (int)peak, (unsigned)sqrt((double)sumSq / TOTAL_SAMPLES), (unsigned)(rawPeak << 1));
}

// ── POST audio, STREAM reply straight to the speaker ────────────────────────
bool postAndPlay(const uint8_t* wav, size_t len) {
  if (WiFi.status() != WL_CONNECTED) { Serial.println("[HTTP] no wifi"); return false; }
  HTTPClient http;
  char url[96];
  snprintf(url, sizeof(url), "http://%s:%u%s", g_bridgeHost.c_str(), g_bridgePort, BRIDGE_PATH);
  http.begin(url);
  http.setTimeout(BRIDGE_TIMEOUT_MS);
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Hermes-Token", BRIDGE_TOKEN);
  Serial.printf("[HTTP] POST %u bytes -> %s\n", (unsigned)len, url);

  int code = http.POST((uint8_t*)wav, len);
  if (code != HTTP_CODE_OK) {
    Serial.printf("[HTTP] failed: %d (%s)\n", code, http.errorToString(code).c_str());
    http.end(); return false;
  }

  WiFiClient* s = http.getStreamPtr();

  // read + parse the 44-byte WAV header
  uint8_t hdr[WAV_HEADER_SIZE]; int hp = 0;
  unsigned long dl = millis() + BRIDGE_TIMEOUT_MS;
  while (hp < WAV_HEADER_SIZE && millis() < dl) {
    if (s->available()) hdr[hp++] = s->read();
    else if (!http.connected() && !s->available()) break;
  }
  if (hp < WAV_HEADER_SIZE) { Serial.println("[Play] short reply"); http.end(); return false; }

  uint32_t sr = hdr[24]|(hdr[25]<<8)|(hdr[26]<<16)|((uint32_t)hdr[27]<<24);
  uint16_t bits = hdr[34]|(hdr[35]<<8);
  uint16_t ch = hdr[22]|(hdr[23]<<8);
  if (sr < 8000 || sr > 48000) sr = SAMPLE_RATE;
  Serial.printf("[Play] %u Hz %u-bit %u-ch (streaming)\n", sr, bits, ch);
  i2s_set_clk(I2S_AMP_PORT, sr, (i2s_bits_per_sample_t)(bits?bits:16),
              ch==2 ? I2S_CHANNEL_STEREO : I2S_CHANNEL_MONO);

  // stream the rest straight to the amp
  uint8_t chunk[1024]; size_t w;
  dl = millis() + BRIDGE_TIMEOUT_MS;
  while (millis() < dl) {
    size_t avail = s->available();
    if (avail) {
      int n = s->readBytes(chunk, avail > sizeof(chunk) ? sizeof(chunk) : avail);
      if (n > 0) { i2s_write(I2S_AMP_PORT, chunk, n, &w, portMAX_DELAY); dl = millis() + 3000; }
    } else if (!http.connected()) break;
    else delay(2);
  }
  i2s_zero_dma_buffer(I2S_AMP_PORT);
  i2s_set_clk(I2S_AMP_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  http.end();
  Serial.println("[Play] done");
  return true;
}

void blinkError(int n){ for(int i=0;i<n;i++){ led(true);delay(120);led(false);delay(120);} }
