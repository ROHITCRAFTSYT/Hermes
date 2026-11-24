/*
 * Hermes Voice Satellite — CLASSIC ESP32 (ESP-WROOM-32) build
 * ----------------------------------------------------------------------------
 * Push-to-talk: press the button and speak. Each press records a fixed window
 * (RECORD_SECONDS, default 3 s) of 16 kHz mono audio from an INMP441 I2S mic,
 * POSTs it to the Hermes bridge on your PC, and STREAMS the spoken reply
 * straight to a MAX98357A I2S amp + speaker. Releasing early does not stop it.
 *
 * WiFi credentials come from secrets.h (WIFI_SSID / WIFI_PASS), compiled in.
 * To change networks, edit secrets.h and reflash. Don't hold the BOOT button
 * (GPIO0) while powering on: that puts the chip into ROM download mode.
 *
 * Bridge address: with USE_MDNS 1 the satellite looks for _hermes._tcp on the
 * LAN and falls back to BRIDGE_HOST. With USE_MDNS 0 it only uses BRIDGE_HOST
 * (safer: no other LAN device can pose as the bridge and receive the token).
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

#ifndef USE_MDNS
#define USE_MDNS 1
#endif

// Resolved at runtime by mDNS (if USE_MDNS); otherwise / on a miss, BRIDGE_HOST.
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

// Recording buffer: allocated ONCE in setup() (before WiFi takes its heap) and
// kept for the device's lifetime, so heap fragmentation can't shrink it later.
static uint8_t* g_wav = nullptr;

// Amp DMA ring = 8 bufs x 256 frames; 4 bytes/frame covers 16-bit stereo.
#define AMP_DMA_BUF_COUNT 8
#define AMP_DMA_BUF_LEN   256
#define AMP_DMA_RING_MAX_BYTES (AMP_DMA_BUF_COUNT * AMP_DMA_BUF_LEN * 4)

// postAndPlay() results: 0 = played; <0 = couldn't reach the bridge
// (transport error → worth re-discovering); >0 = bridge answered but the
// request or reply was bad (HTTP status, or PLAY_BAD_REPLY).
#define PLAY_OK          0
#define PLAY_NO_WIFI     -1000
#define PLAY_BAD_REPLY   1

// ── forward decls ───────────────────────────────────────────────────────────
void setupWiFi();
void discoverBridge(bool keepCurrentOnMiss);
bool setupMicI2S();
bool setupAmpI2S();
void buildWAVHeader(uint8_t* h, uint32_t pcmBytes, uint32_t sr);
void recordWAV(uint8_t* buf);
int  postAndPlay(const uint8_t* wav, size_t len);
bool allocRecordBuffer();
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

  // Grab the big buffer before WiFi/lwIP carve up the heap.
  if (!allocRecordBuffer()) {
    Serial.println("[MEM] can't allocate recording buffer — rebooting");
    blinkError(6); delay(1000); ESP.restart();
  }

  setupWiFi();
#if USE_MDNS
  MDNS.begin("hermes-satellite");
#endif
  discoverBridge(false); // find the PC brain by name; falls back to the fixed IP

  if (!setupMicI2S()) { Serial.println("[ERR] mic I2S"); while (true){ blinkError(3); delay(1500);} }
  if (!setupAmpI2S()) { Serial.println("[ERR] amp I2S"); while (true){ blinkError(5); delay(1500);} }

  Serial.println("[Hermes] ready — hold the button and speak.");
  for (int i=0;i<2;i++){ led(true); delay(80); led(false); delay(80); }
}

// ── loop ────────────────────────────────────────────────────────────────────
void loop() {
  if (digitalRead(PTT_PIN) != LOW) { delay(5); return; }   // idle: don't spin the core flat out
  delay(30);
  if (digitalRead(PTT_PIN) != LOW) return;

  // Check the link BEFORE recording, so a dropped WiFi doesn't waste the utterance.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] not connected — reconnecting, try again in a moment");
    WiFi.reconnect();
    blinkError(2);
    while (digitalRead(PTT_PIN) == LOW) delay(10);
    return;
  }

  Serial.println("[PTT] recording…");
  led(true);
  recordWAV(g_wav);
  led(false);

  int rc = postAndPlay(g_wav, WAV_TOTAL);
  if (rc != PLAY_OK) {
    blinkError(4);
    // Only a transport failure suggests the bridge moved. An HTTP status
    // (e.g. 401 bad token, 500) means we reached it — re-discovering would
    // just block the button for ~10 s.
    if (rc < 0 && rc != PLAY_NO_WIFI) discoverBridge(true);
  }

  Serial.println("[PTT] done — release button");
  while (digitalRead(PTT_PIN) == LOW) delay(10);
}

// ── recording buffer (once, at boot) ────────────────────────────────────────
bool allocRecordBuffer() {
  g_wav = (uint8_t*)malloc(WAV_TOTAL);
  if (g_wav) return true;
  Serial.printf("[MEM] %u s buffer (%u bytes) failed, trying 2 s\n",
                (unsigned)RECORD_SECONDS, (unsigned)WAV_TOTAL);
  TOTAL_SAMPLES = SAMPLE_RATE * 2 * NUM_CHANNELS;
  PCM_BYTES     = TOTAL_SAMPLES * (BITS_PER_SAMPLE / 8);
  WAV_TOTAL     = WAV_HEADER_SIZE + PCM_BYTES;
  g_wav = (uint8_t*)malloc(WAV_TOTAL);
  return g_wav != nullptr;
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
  WiFi.scanDelete();   // free the scan results — they'd otherwise sit on the heap forever
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
// keepCurrentOnMiss: on a re-discovery, a transient mDNS miss keeps the host we
// already have instead of overwriting a good mDNS-found address with BRIDGE_HOST.
void discoverBridge(bool keepCurrentOnMiss) {
#if USE_MDNS
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
  if (keepCurrentOnMiss) {
    Serial.printf("[mDNS] not found — keeping %s:%u\n", g_bridgeHost.c_str(), g_bridgePort);
    return;
  }
  Serial.printf("[mDNS] not found — using fixed %s:%u\n", BRIDGE_HOST, BRIDGE_PORT);
#else
  (void)keepCurrentOnMiss;
  Serial.printf("[Bridge] mDNS off — using fixed %s:%u\n", BRIDGE_HOST, BRIDGE_PORT);
#endif
  g_bridgeHost = BRIDGE_HOST;
  g_bridgePort = BRIDGE_PORT;
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
    .dma_buf_count = AMP_DMA_BUF_COUNT, .dma_buf_len = AMP_DMA_BUF_LEN,
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
// Returns PLAY_OK, a negative transport error, or a positive HTTP status /
// PLAY_BAD_REPLY (see the PLAY_* defines at the top).
int postAndPlay(const uint8_t* wav, size_t len) {
  if (WiFi.status() != WL_CONNECTED) { Serial.println("[HTTP] no wifi"); return PLAY_NO_WIFI; }
  HTTPClient http;
  char url[96];
  snprintf(url, sizeof(url), "http://%s:%u%s", g_bridgeHost.c_str(), g_bridgePort, BRIDGE_PATH);
  http.begin(url);
  http.setReuse(false);              // ask the bridge to close the socket after the reply
  http.setTimeout(BRIDGE_TIMEOUT_MS);
  http.addHeader("Content-Type", "audio/wav");
  if (BRIDGE_TOKEN[0] != '\0') http.addHeader("X-Hermes-Token", BRIDGE_TOKEN);  // "" = no token
  Serial.printf("[HTTP] POST %u bytes -> %s\n", (unsigned)len, url);

  int code = http.POST((uint8_t*)wav, len);
  if (code != HTTP_CODE_OK) {
    Serial.printf("[HTTP] failed: %d (%s)\n", code, http.errorToString(code).c_str());
    http.end();
    return code;                     // <0 transport error, >0 HTTP status
  }

  WiFiClient* s = http.getStreamPtr();
  int bodySize = http.getSize();     // Content-Length, or -1 if the server didn't send one

  // read + parse the 44-byte WAV header (deadlines use elapsed time, so
  // they survive the millis() wrap every ~49.7 days)
  uint8_t hdr[WAV_HEADER_SIZE]; int hp = 0;
  unsigned long t0 = millis();
  while (hp < WAV_HEADER_SIZE && millis() - t0 < BRIDGE_TIMEOUT_MS) {
    if (s->available()) hdr[hp++] = s->read();
    else if (!http.connected()) break;
    else delay(1);                   // don't spin the core while waiting
  }
  if (hp < WAV_HEADER_SIZE) { Serial.println("[Play] short reply"); http.end(); return PLAY_BAD_REPLY; }

  uint32_t sr = hdr[24]|(hdr[25]<<8)|(hdr[26]<<16)|((uint32_t)hdr[27]<<24);
  uint16_t bits = hdr[34]|(hdr[35]<<8);
  uint16_t ch = hdr[22]|(hdr[23]<<8);
  if (memcmp(hdr, "RIFF", 4) != 0 || memcmp(hdr + 8, "WAVE", 4) != 0 ||
      bits != 16 || (ch != 1 && ch != 2)) {
    Serial.printf("[Play] not a 16-bit mono/stereo WAV (bits=%u ch=%u) — skipping\n", bits, ch);
    http.end(); return PLAY_BAD_REPLY;
  }
  if (sr < 8000 || sr > 48000) sr = SAMPLE_RATE;
  Serial.printf("[Play] %u Hz %u-bit %u-ch (streaming)\n", sr, bits, ch);
  if (i2s_set_clk(I2S_AMP_PORT, sr, I2S_BITS_PER_SAMPLE_16BIT,
                  ch == 2 ? I2S_CHANNEL_STEREO : I2S_CHANNEL_MONO) != ESP_OK) {
    Serial.println("[Play] i2s_set_clk failed");
    http.end(); return PLAY_BAD_REPLY;
  }

  // stream the rest straight to the amp. Stop when Content-Length bytes have
  // arrived (don't wait for a keep-alive socket to close), the server closes,
  // or nothing arrives for 3 s once audio has started.
  long remaining = bodySize > 0 ? (long)bodySize - WAV_HEADER_SIZE : -1;
  uint8_t chunk[1024]; size_t w;
  unsigned long idleLimit = BRIDGE_TIMEOUT_MS;   // first audio bytes may take a while
  t0 = millis();
  while (remaining != 0 && millis() - t0 < idleLimit) {
    size_t avail = s->available();
    if (avail) {
      size_t want = avail > sizeof(chunk) ? sizeof(chunk) : avail;
      if (remaining > 0 && (long)want > remaining) want = (size_t)remaining;
      int n = s->readBytes(chunk, want);
      if (n > 0) {
        i2s_write(I2S_AMP_PORT, chunk, n, &w, portMAX_DELAY);
        if (remaining > 0) remaining -= n;
        t0 = millis();
        idleLimit = 3000;
      }
    } else if (!http.connected()) break;
    else delay(2);
  }

  // i2s_write returns once data is in the DMA ring, not once it has played.
  // Push a full ring of silence so the real tail drains to the speaker before
  // we clear the buffers and retune the clock — otherwise the last ~100 ms
  // (often the final syllable) gets cut off.
  memset(chunk, 0, sizeof(chunk));
  for (size_t pushed = 0; pushed < AMP_DMA_RING_MAX_BYTES; pushed += sizeof(chunk)) {
    i2s_write(I2S_AMP_PORT, chunk, sizeof(chunk), &w, portMAX_DELAY);
  }
  i2s_zero_dma_buffer(I2S_AMP_PORT);
  i2s_set_clk(I2S_AMP_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  http.end();
  Serial.println("[Play] done");
  return PLAY_OK;
}

void blinkError(int n){ for(int i=0;i<n;i++){ led(true);delay(120);led(false);delay(120);} }
