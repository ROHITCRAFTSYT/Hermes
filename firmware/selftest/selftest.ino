/* Hermes self-test — verifies mic + I2S + button without WiFi or a speaker. */
#include <Arduino.h>
#include <driver/i2s.h>
#include <math.h>

#define MIC_SCK_PIN 14
#define MIC_WS_PIN  27
#define MIC_SD_PIN  26
#define AMP_BCLK_PIN 25
#define AMP_WS_PIN   33
#define AMP_DIN_PIN  32
#define PTT_PIN       0
#define SR 16000
#define I2S_MIC I2S_NUM_0
#define I2S_AMP I2S_NUM_1

bool setupMic() {
  i2s_config_t c = { .mode=(i2s_mode_t)(I2S_MODE_MASTER|I2S_MODE_RX), .sample_rate=SR,
    .bits_per_sample=I2S_BITS_PER_SAMPLE_32BIT, .channel_format=I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format=I2S_COMM_FORMAT_STAND_I2S, .intr_alloc_flags=ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count=8, .dma_buf_len=256, .use_apll=false, .tx_desc_auto_clear=false, .fixed_mclk=0 };
  i2s_pin_config_t p = { .bck_io_num=MIC_SCK_PIN, .ws_io_num=MIC_WS_PIN,
    .data_out_num=I2S_PIN_NO_CHANGE, .data_in_num=MIC_SD_PIN };
  if (i2s_driver_install(I2S_MIC,&c,0,nullptr)!=ESP_OK) return false;
  if (i2s_set_pin(I2S_MIC,&p)!=ESP_OK) return false;
  return true;
}
bool setupAmp() {
  i2s_config_t c = { .mode=(i2s_mode_t)(I2S_MODE_MASTER|I2S_MODE_TX), .sample_rate=SR,
    .bits_per_sample=I2S_BITS_PER_SAMPLE_16BIT, .channel_format=I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format=I2S_COMM_FORMAT_STAND_I2S, .intr_alloc_flags=ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count=8, .dma_buf_len=256, .use_apll=false, .tx_desc_auto_clear=true, .fixed_mclk=0 };
  i2s_pin_config_t p = { .bck_io_num=AMP_BCLK_PIN, .ws_io_num=AMP_WS_PIN,
    .data_out_num=AMP_DIN_PIN, .data_in_num=I2S_PIN_NO_CHANGE };
  if (i2s_driver_install(I2S_AMP,&c,0,nullptr)!=ESP_OK) return false;
  if (i2s_set_pin(I2S_AMP,&p)!=ESP_OK) return false;
  return true;
}

void setup() {
  Serial.begin(115200); delay(500);
  Serial.println("\n===== HERMES SELF-TEST =====");
  pinMode(PTT_PIN, INPUT_PULLUP);
  bool micOK = setupMic();
  bool ampOK = setupAmp();
  Serial.printf("ESP32 alive     : YES\n");
  Serial.printf("INMP441 mic I2S : %s\n", micOK ? "INIT OK" : "FAIL (check D14/D27/D26 + 3V3/GND)");
  Serial.printf("MAX98357A amp   : %s\n", ampOK ? "INIT OK" : "FAIL (check D25/D33/D32)");
  Serial.println("Now TAP the mic hole or talk to it — 'rms' should jump from a low");
  Serial.println("idle value into the hundreds/thousands. Press BOOT to see it register.\n");
}

void loop() {
  const int N = 512;
  int32_t buf[N]; size_t n = 0;
  i2s_read(I2S_MIC, buf, N*sizeof(int32_t), &n, portMAX_DELAY);
  int got = n / 4;
  long peak = 0; double sumsq = 0;
  for (int i=0;i<got;i++){ int32_t s = buf[i] >> 14; long a = labs(s); if(a>peak) peak=a; sumsq += (double)s*s; }
  int rms = got ? (int)sqrt(sumsq/got) : 0;
  Serial.printf("mic  rms=%5d  peak=%6ld   BOOT=%s\n", rms, peak,
                digitalRead(PTT_PIN)==LOW ? "PRESSED" : "up");
  delay(200);
}
