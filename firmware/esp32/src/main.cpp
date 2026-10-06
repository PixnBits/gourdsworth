// Pumpkin face. ESP32 DevKit V1 (WROOM-32) is the only servo and LED clock.
// Pi UART is JSON lines. This file never starts Wi-Fi and never chains the strips.

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <Adafruit_PWMServoDriver.h>

#define FASTLED_ALLOW_INTERRUPTS 0
#include <FastLED.h>

#ifndef FULL_WHITE_TEST
#define FULL_WHITE_TEST 0
#endif
#ifndef MOUTH_COLS
#define MOUTH_COLS 32
#endif
#ifndef MOUTH_ROWS
#define MOUTH_ROWS 8
#endif
#ifndef EYE_LEDS
#define EYE_LEDS 16
#endif

static constexpr int PIN_MOUTH = 18;
static constexpr int PIN_EYE_L = 19;
static constexpr int PIN_EYE_R = 21;
static constexpr int PIN_SDA = 22;
static constexpr int PIN_SCL = 23;
static constexpr int PIN_RX = 16;  // from Pi GPIO 14 TX
static constexpr int PIN_TX = 17;  // to Pi GPIO 15 RX
static constexpr uint8_t PCA_ADDR = 0x40;

static const uint8_t CH_BROW_L = 0;
static const uint8_t CH_BROW_R = 1;
static const uint8_t CH_HAT = 2;
static const uint8_t CH_SPARE = 3;

static const int ANG_LEVEL = 90;
static const int ANG_UP = 58;
static const int ANG_DIP = 122;
static const int ANG_IN_L = 124;
static const int ANG_IN_R = 56;
static const int ANG_HAT_REST = 90;
static const int ANG_HAT_DOWN = 128;

static const uint32_t TIP_MS = 600;
static const uint32_t CHUCKLE_SHUT_MS = 220;
static const uint32_t BLINK_EVERY_MS = 4500;
static const uint32_t BLINK_SHUT_MS = 140;
static const uint32_t LINK_QUIET_MS = 2000;
static const uint8_t BRIGHT_CAP = 102;  // 40% of 255

static const int MOUTH_N = MOUTH_COLS * MOUTH_ROWS;

CRGB mouth[MOUTH_N];
CRGB eyeL[EYE_LEDS];
CRGB eyeR[EYE_LEDS];

Adafruit_PWMServoDriver pca(PCA_ADDR);
static bool servoOk = false;

enum Pose : uint8_t { POSE_NEUTRAL, POSE_TIP, POSE_BEAM, POSE_RECKON, POSE_CHUCKLE, POSE_ATTEND };
enum EyeShape : uint8_t { EYE_OPEN, EYE_WIDE, EYE_HALF, EYE_SHUT };

static Pose pose = POSE_NEUTRAL;
static uint32_t poseMs = 0;
static char visemeId[8] = "rest";
static float visemeRms = 0.0f;
static bool visemeLive = false;
static uint32_t lastUartMs = 0;
static char lineBuf[160];
static uint8_t lineLen = 0;

static void radioOff() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  WiFi.disconnect(true, true);
  btStop();
}

static int degToUs(int deg) {
  if (deg < 0) deg = 0;
  if (deg > 180) deg = 180;
  return 600 + (deg * 1800) / 180;
}

static void writeUs(uint8_t channel, int us) {
  if (!servoOk) return;
  if (us < 600) us = 600;
  if (us > 2400) us = 2400;
  uint16_t tick = (uint16_t)((us * 4096L) / 20000L);
  pca.setPWM(channel, 0, tick);
}

static void writeDeg(uint8_t channel, int deg) {
  writeUs(channel, degToUs(deg));
}

static CRGB amber(uint8_t scale) {
#if FULL_WHITE_TEST
  if (scale == 0) return CRGB::Black;
  return CRGB(255, 255, 255);
#else
  return CRGB(scale, (uint8_t)(((uint16_t)scale * 80) / 255), 0);
#endif
}

static int mouthIndex(int x, int y) {
  if (y & 1) return y * MOUTH_COLS + (MOUTH_COLS - 1 - x);
  return y * MOUTH_COLS + x;
}

// Shape mask 0–255. The ESP32, not the Pi, turns a viseme id into pixels.
static uint8_t mouthMask(const char* id, int x, int y) {
  const int cx = MOUTH_COLS / 2;
  const int cy = MOUTH_ROWS / 2;
  int dx = x - cx;
  if (dx < 0) dx = -dx;
  int dy = y - cy;
  if (dy < 0) dy = -dy;
  if (strcmp(id, "aa") == 0) {
    return (dx * 3 + dy * 10 <= 30) ? 255 : 0;
  }
  if (strcmp(id, "oh") == 0) {
    return (dx * 3 + dy * 8 <= 18) ? 255 : 0;
  }
  if (strcmp(id, "ee") == 0) {
    return (dy <= 1 && dx <= (MOUTH_COLS * 2) / 5) ? 255 : 0;
  }
  if (strcmp(id, "mbp") == 0) {
    return (dy == 0 && dx <= MOUTH_COLS / 4) ? 255 : 0;
  }
  return (dy == 0 && dx <= MOUTH_COLS / 8) ? 140 : 0;
}

static void renderMouthSpeech() {
  float rms = visemeRms;
  if (rms < 0.0f) rms = 0.0f;
  if (rms > 1.0f) rms = 1.0f;
  uint8_t cap = (uint8_t)(rms * (float)BRIGHT_CAP + 0.5f);
  for (int y = 0; y < MOUTH_ROWS; y++) {
    for (int x = 0; x < MOUTH_COLS; x++) {
      uint8_t mask = mouthMask(visemeId, x, y);
      uint8_t scale = (uint8_t)(((uint16_t)cap * mask) / 255);
      mouth[mouthIndex(x, y)] = amber(scale);
    }
  }
}

static void renderMouthFlicker(uint32_t now) {
  int cx = (MOUTH_COLS / 2) + (int)((now / 180) % 7) - 3;
  uint8_t bri = (uint8_t)(36 + ((now / 50) % 28));
  for (int y = 0; y < MOUTH_ROWS; y++) {
    for (int x = 0; x < MOUTH_COLS; x++) {
      int dx = x - cx;
      if (dx < 0) dx = -dx;
      int dy = y - (MOUTH_ROWS / 2);
      if (dy < 0) dy = -dy;
      bool on = (dx + dy * 2) < 8;
      mouth[mouthIndex(x, y)] = on ? amber(bri) : CRGB::Black;
    }
  }
}

static EyeShape eyeShape(uint32_t now) {
  if (pose == POSE_BEAM) return EYE_WIDE;
  if (pose == POSE_RECKON) return EYE_HALF;
  if (pose == POSE_CHUCKLE && (uint32_t)(now - poseMs) < CHUCKLE_SHUT_MS) return EYE_SHUT;
  return EYE_OPEN;
}

static bool blinkClosed(uint32_t now, EyeShape shape) {
  if (shape == EYE_SHUT) return true;
  return (now % BLINK_EVERY_MS) < BLINK_SHUT_MS;
}

static void renderEye(CRGB* leds, int count, EyeShape shape, uint32_t now) {
  bool shut = blinkClosed(now, shape);
  uint8_t bri = 80;
  if (shape == EYE_WIDE) bri = BRIGHT_CAP;
  else if (shape == EYE_HALF) bri = 70;
  for (int i = 0; i < count; i++) {
    bool on = !shut;
    if (on && shape == EYE_HALF) on = i >= (count / 2);
    leds[i] = on ? amber(bri) : CRGB::Black;
  }
}

static void applyServos(uint32_t now) {
  if (pose == POSE_TIP && (uint32_t)(now - poseMs) >= TIP_MS) {
    pose = POSE_NEUTRAL;
  }
  int browL = ANG_LEVEL;
  int browR = ANG_LEVEL;
  int hat = ANG_HAT_REST;
  if (pose == POSE_TIP) {
    uint32_t dt = (uint32_t)(now - poseMs);
    if (dt > TIP_MS) dt = TIP_MS;
    int swing = dt <= (TIP_MS / 2) ? (int)dt : (int)(TIP_MS - dt);
    int half = (int)(TIP_MS / 2);
    hat = ANG_HAT_REST + ((ANG_HAT_DOWN - ANG_HAT_REST) * swing) / half;
    browL = ANG_LEVEL + ((ANG_DIP - ANG_LEVEL) * swing) / half;
    browR = browL;
  } else if (pose == POSE_BEAM || pose == POSE_CHUCKLE) {
    browL = ANG_UP;
    browR = ANG_UP;
  } else if (pose == POSE_RECKON) {
    browL = ANG_IN_L;
    browR = ANG_IN_R;
  }
  writeDeg(CH_BROW_L, browL);
  writeDeg(CH_BROW_R, browR);
  writeDeg(CH_HAT, hat);
}

static void goIdle() {
  visemeLive = false;
  pose = POSE_NEUTRAL;
  strncpy(visemeId, "rest", sizeof(visemeId));
  visemeId[sizeof(visemeId) - 1] = 0;
  visemeRms = 0.0f;
}

static bool copyField(const char* src, const char* key, char* out, size_t outn) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ' || *p == '\t' || *p == ':') p++;
  if (*p != '"') return false;
  p++;
  size_t i = 0;
  while (*p && *p != '"' && i + 1 < outn) out[i++] = *p++;
  out[i] = 0;
  return i > 0;
}

static bool copyFloat(const char* src, const char* key, float* out) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ' || *p == '\t' || *p == ':') p++;
  char* end = nullptr;
  float value = strtof(p, &end);
  if (end == p) return false;
  *out = value;
  return true;
}

static bool startPose(const char* name) {
  Pose next;
  if (strcmp(name, "tip") == 0) next = POSE_TIP;
  else if (strcmp(name, "beam") == 0) next = POSE_BEAM;
  else if (strcmp(name, "reckon") == 0) next = POSE_RECKON;
  else if (strcmp(name, "chuckle") == 0) next = POSE_CHUCKLE;
  else if (strcmp(name, "attend") == 0) next = POSE_ATTEND;
  else return false;  // retired names included: do not stall the mouth
  pose = next;
  poseMs = millis();
  return true;
}

static void handleLine(const char* line) {
  char op[16];
  if (!copyField(line, "op", op, sizeof(op))) return;
  if (strcmp(op, "ping") == 0) {
    Serial2.print("{\"op\":\"pong\"}\n");
    return;
  }
  if (strcmp(op, "idle") == 0) {
    goIdle();
    return;
  }
  if (strcmp(op, "gesture") == 0) {
    char name[16];
    if (!copyField(line, "name", name, sizeof(name))) return;
    startPose(name);  // false → ignore, mouth untouched
    return;
  }
  if (strcmp(op, "viseme") == 0) {
    char id[8];
    if (!copyField(line, "id", id, sizeof(id))) return;
    if (strcmp(id, "rest") && strcmp(id, "aa") && strcmp(id, "ee") &&
        strcmp(id, "oh") && strcmp(id, "mbp")) {
      return;
    }
    float rms = 0.0f;
    copyFloat(line, "rms", &rms);
    if (rms < 0.0f) rms = 0.0f;
    if (rms > 1.0f) rms = 1.0f;
    strncpy(visemeId, id, sizeof(visemeId));
    visemeId[sizeof(visemeId) - 1] = 0;
    visemeRms = rms;
    visemeLive = true;
  }
}

static void pollUart() {
  while (Serial2.available() > 0) {
    char c = (char)Serial2.read();
    lastUartMs = millis();
    if (c == '\r') continue;
    if (c != '\n') {
      if (lineLen + 1 < sizeof(lineBuf)) lineBuf[lineLen++] = c;
      else lineLen = 0;
      continue;
    }
    lineBuf[lineLen] = 0;
    if (lineLen > 0) handleLine(lineBuf);
    lineLen = 0;
  }
  if ((uint32_t)(millis() - lastUartMs) >= LINK_QUIET_MS) {
    visemeLive = false;
    pose = POSE_NEUTRAL;
  }
}

static void render(uint32_t now) {
  applyServos(now);
  if (visemeLive) renderMouthSpeech();
  else renderMouthFlicker(now);
  EyeShape shape = eyeShape(now);
  renderEye(eyeL, EYE_LEDS, shape, now);
  renderEye(eyeR, EYE_LEDS, shape, now);
}

void setup() {
  radioOff();
  Serial.begin(115200);
  Serial2.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX);
  Wire.begin(PIN_SDA, PIN_SCL);
  pca.begin();
  // begin() may call Wire.begin() with no pins, which would steal GPIO 21
  // (eye R) as the default SDA. Put SDA/SCL back on 22/23.
  Wire.begin(PIN_SDA, PIN_SCL);
  servoOk = true;
  pca.setPWMFreq(50);
  writeDeg(CH_BROW_L, ANG_LEVEL);
  writeDeg(CH_BROW_R, ANG_LEVEL);
  writeDeg(CH_HAT, ANG_HAT_REST);
  writeUs(CH_SPARE, 1500);
  FastLED.addLeds<WS2812B, PIN_MOUTH, GRB>(mouth, MOUTH_N);
  FastLED.addLeds<WS2812B, PIN_EYE_L, GRB>(eyeL, EYE_LEDS);
  FastLED.addLeds<WS2812B, PIN_EYE_R, GRB>(eyeR, EYE_LEDS);
  FastLED.clear(true);
  goIdle();
  lastUartMs = millis();
  Serial.println("face up, wifi off");
}

void loop() {
  pollUart();
  static uint32_t lastFrame = 0;
  uint32_t now = millis();
  if ((uint32_t)(now - lastFrame) < 16) return;
  lastFrame = now;
  render(now);
  FastLED.show();
}
