// Pumpkin face. ESP32 DevKit V1 (WROOM-32) is the only servo and LED clock.
// Pi UART is JSON lines. This file never starts Wi-Fi. One WS2812 strip on GPIO 18.

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <Preferences.h>
#include <Adafruit_PWMServoDriver.h>

#define FASTLED_ALLOW_INTERRUPTS 0
#include <FastLED.h>

#ifndef FULL_WHITE_TEST
#define FULL_WHITE_TEST 0
#endif
// platformio.ini passes -DFACE_LEDS=231. Keep the same fallback.
#ifndef FACE_LEDS
#define FACE_LEDS 231
#endif

static constexpr int PIN_FACE = 18;
static constexpr int PIN_SDA = 22;
static constexpr int PIN_SCL = 23;
static constexpr int PIN_RX = 16;  // from Pi GPIO 14 TX
static constexpr int PIN_TX = 17;  // to Pi GPIO 15 RX
static constexpr uint8_t PCA_ADDR = 0x40;

static const uint8_t CH_BROW_L = 0;
static const uint8_t CH_BROW_R = 1;
static const uint8_t CH_HAT = 2;
static const uint8_t CH_MUSTACHE = 3;

static const int ANG_LEVEL = 90;
static const int ANG_UP = 58;
static const int ANG_DIP = 122;
static const int ANG_IN_L = 124;
static const int ANG_IN_R = 56;
static const int ANG_HAT_REST = 90;
static const int ANG_HAT_DOWN = 128;
static const int ANG_MUSTACHE_REST = 90;

static const uint32_t TIP_MS = 600;
static const uint32_t TWIRL_MS = 900;
static const uint32_t LINK_QUIET_MS = 2000;
// Default cap is 40% of 255. Speech still tops out at floor+span (102) and
// is then clamped to the runtime cap, so a higher cap does not brighten speech.
static const uint8_t DEFAULT_BRIGHT_CAP = 102;
static const uint8_t DEFAULT_BASE_R = 255;
static const uint8_t DEFAULT_BASE_G = 140;
static const uint8_t DEFAULT_BASE_B = 20;
static const uint8_t SPEECH_FLOOR = 50;
static const uint8_t SPEECH_SPAN = 52;  // floor + span reaches the default cap
// typical Piper en_US-danny-low frame RMS peaks ~0.3-0.4
static const float RMS_REF = 0.30f;
// Per 16 ms frame. Attack tracks a louder syllable; decay is slower so the
// strip does not strobe and so speech can glide down into the idle band.
static const float GLOW_ATTACK = 0.55f;
static const float GLOW_DECAY = 0.12f;
static const uint8_t IDLE_LO = 36;
static const uint8_t IDLE_HI = 64;

static const int MAX_SWATCHES = 16;

static uint8_t baseR = DEFAULT_BASE_R;
static uint8_t baseG = DEFAULT_BASE_G;
static uint8_t baseB = DEFAULT_BASE_B;
static uint8_t brightCap = DEFAULT_BRIGHT_CAP;
static bool testMode = false;

static Preferences prefs;
static const char* PREF_NS = "face";

CRGB faceLeds[FACE_LEDS];

Adafruit_PWMServoDriver pca(PCA_ADDR);
static bool servoOk = false;

enum Pose : uint8_t {
  POSE_NEUTRAL, POSE_TIP, POSE_BEAM, POSE_RECKON, POSE_CHUCKLE, POSE_ATTEND, POSE_TWIRL
};

static Pose pose = POSE_NEUTRAL;
static uint32_t poseMs = 0;
static char visemeId[8] = "rest";
static float visemeRms = 0.0f;
static bool visemeLive = false;
// Shared by speech and idle. goIdle does not clear it, so a quiet link glides.
static float glowLevel = (float)SPEECH_FLOOR;
static uint32_t lastUartMs = 0;
// 16 full-scale swatches encode to about 232 bytes. 512 leaves room for the NUL.
static char lineBuf[512];
static uint16_t lineLen = 0;
static bool lineDrop = false;

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
  static int16_t lastTick[4] = {-1, -1, -1, -1};
  if (!servoOk || channel > 3) return;
  if (us < 600) us = 600;
  if (us > 2400) us = 2400;
  int16_t tick = (int16_t)((us * 4096L) / 20000L);
  if (lastTick[channel] == tick) return;
  lastTick[channel] = tick;
  pca.setPWM(channel, 0, (uint16_t)tick);
}

static void writeDeg(uint8_t channel, int deg) {
  writeUs(channel, degToUs(deg));
}

// Normal-mode pixels. scale is 0–255 and is limited to the brightness cap.
// Test mode does not call this: those pixels are the exact commanded RGB.
static CRGB faceColor(uint8_t scale) {
#if FULL_WHITE_TEST
  if (scale == 0) return CRGB::Black;
  return CRGB(255, 255, 255);
#else
  if (scale > brightCap) scale = brightCap;
  return CRGB(
      (uint8_t)(((uint16_t)scale * baseR) / 255),
      (uint8_t)(((uint16_t)scale * baseG) / 255),
      (uint8_t)(((uint16_t)scale * baseB) / 255));
#endif
}

static float approach(float current, float target) {
  float rate = (target > current) ? GLOW_ATTACK : GLOW_DECAY;
  return current + (target - current) * rate;
}

// Speech ceiling. If the cap is below SPEECH_FLOOR, this returns the floor.
// faceColor still multiplies by the real cap, so pixels get dimmer, but the
// smoothed level never drops below the floor and cannot be pulled to 0.
static float speechHi() {
  float cap = (float)brightCap;
  if (cap < (float)SPEECH_FLOOR) return (float)SPEECH_FLOOR;
  return cap;
}

// Idle band is [36, 64], tightened to the cap. A cap below 1 collapses the
// band to 1. faceColor still paints black when the cap itself is 0.
static float idleHi() {
  float hi = (float)IDLE_HI;
  float cap = (float)brightCap;
  if (cap < hi) hi = cap;
  if (hi < 1.0f) hi = 1.0f;
  return hi;
}

static float idleLo(float hi) {
  float lo = (float)IDLE_LO;
  if (lo > hi) lo = hi;
  if (lo < 1.0f) lo = 1.0f;
  return lo;
}

static void advanceSpeech() {
  float rms = visemeRms;
  if (rms < 0.0f) rms = 0.0f;
  if (rms > 1.0f) rms = 1.0f;
  float loud = rms / RMS_REF;
  if (loud < 0.0f) loud = 0.0f;
  if (loud > 1.0f) loud = 1.0f;
  float target = (float)SPEECH_FLOOR + loud * (float)SPEECH_SPAN;
  float hi = speechHi();
  if (target > hi) target = hi;
  if (target < (float)SPEECH_FLOOR) target = (float)SPEECH_FLOOR;
  glowLevel = approach(glowLevel, target);
  if (glowLevel < (float)SPEECH_FLOOR) glowLevel = (float)SPEECH_FLOOR;
  if (glowLevel > hi) glowLevel = hi;
  // visemeId is parsed and stored. Brightness does not branch on it.
  (void)visemeId;
}

static float idleTarget(uint32_t now) {
  // Gentle noise (about 2 s per cell), then a second smoother, then the idle band.
  uint8_t n = inoise8((uint16_t)(now >> 3));
  float raw = (float)IDLE_LO + ((float)n * (float)(IDLE_HI - IDLE_LO)) / 255.0f;
  static float wander = (float)SPEECH_FLOOR;
  wander += (raw - wander) * GLOW_DECAY;
  float hi = idleHi();
  float lo = idleLo(hi);
  if (wander < lo) wander = lo;
  if (wander > hi) wander = hi;
  return wander;
}

static void advanceIdle(uint32_t now) {
  float target = idleTarget(now);
  glowLevel = approach(glowLevel, target);
  float hi = idleHi();
  // Still above the band: this is the tail of speech. Keep decaying.
  // Snapping down to the band would skip the glide; snapping to 0 is worse.
  if (glowLevel <= hi) {
    float lo = idleLo(hi);
    if (glowLevel < lo) glowLevel = lo;
  }
  if (glowLevel < 1.0f) glowLevel = 1.0f;
}

static uint8_t levelToScale(float level) {
  if (level < 1.0f) level = 1.0f;
  if (level > 255.0f) level = 255.0f;
  int rounded = (int)(level + 0.5f);
  if (rounded < 1) rounded = 1;
  if (rounded > 255) rounded = 255;
  return (uint8_t)rounded;
}

static void paintGlow() {
  CRGB color = faceColor(levelToScale(glowLevel));
  fill_solid(faceLeds, FACE_LEDS, color);
}

// 90 -> 60 -> 120 -> 60 -> 90, four equal linear segments.
static int mustacheTwirlDeg(uint32_t dt) {
  static const int fromDeg[4] = {90, 60, 120, 60};
  static const int toDeg[4] = {60, 120, 60, 90};
  if (dt >= TWIRL_MS) return ANG_MUSTACHE_REST;
  uint32_t segMs = TWIRL_MS / 4;
  uint32_t seg = dt / segMs;
  if (seg > 3) seg = 3;
  uint32_t into = dt - seg * segMs;
  int span = toDeg[seg] - fromDeg[seg];
  return fromDeg[seg] + (span * (int)into) / (int)segMs;
}

static void applyServos(uint32_t now) {
  if (pose == POSE_TIP && (uint32_t)(now - poseMs) >= TIP_MS) {
    pose = POSE_NEUTRAL;
  }
  if (pose == POSE_TWIRL && (uint32_t)(now - poseMs) >= TWIRL_MS) {
    pose = POSE_NEUTRAL;
  }
  int browL = ANG_LEVEL;
  int browR = ANG_LEVEL;
  int hat = ANG_HAT_REST;
  int mustache = ANG_MUSTACHE_REST;
  if (pose == POSE_TIP) {
    uint32_t dt = (uint32_t)(now - poseMs);
    if (dt > TIP_MS) dt = TIP_MS;
    int swing = dt <= (TIP_MS / 2) ? (int)dt : (int)(TIP_MS - dt);
    int half = (int)(TIP_MS / 2);
    hat = ANG_HAT_REST + ((ANG_HAT_DOWN - ANG_HAT_REST) * swing) / half;
    browL = ANG_LEVEL + ((ANG_DIP - ANG_LEVEL) * swing) / half;
    browR = browL;
  } else if (pose == POSE_TWIRL) {
    browL = ANG_UP;
    mustache = mustacheTwirlDeg((uint32_t)(now - poseMs));
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
  writeDeg(CH_MUSTACHE, mustache);
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

static bool isDelim(char c) {
  return c == 0 || c == ',' || c == '}' || c == ']' || c == ' ' || c == '\t';
}

// Integer field. Missing key → false. Present but not a bare integer → false
// and *bad set, so the caller can drop the whole line.
static bool copyLong(const char* src, const char* key, long* out, bool* bad) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ' || *p == '\t' || *p == ':') p++;
  char* end = nullptr;
  long value = strtol(p, &end, 10);
  if (end == p || !isDelim(*end)) {
    *bad = true;
    return false;
  }
  *out = value;
  return true;
}

static bool copyU8(const char* src, const char* key, uint8_t* out) {
  bool bad = false;
  long value = 0;
  if (!copyLong(src, key, &value, &bad) || bad) return false;
  if (value < 0 || value > 255) return false;
  *out = (uint8_t)value;
  return true;
}

static void loadPrefs() {
  if (!prefs.begin(PREF_NS, true)) return;
  baseR = prefs.getUChar("r", DEFAULT_BASE_R);
  baseG = prefs.getUChar("g", DEFAULT_BASE_G);
  baseB = prefs.getUChar("b", DEFAULT_BASE_B);
  brightCap = prefs.getUChar("cap", DEFAULT_BRIGHT_CAP);
  prefs.end();
}

static void savePrefs() {
  if (!prefs.begin(PREF_NS, false)) {
    Serial.println("face prefs save failed");
    return;
  }
  prefs.putUChar("r", baseR);
  prefs.putUChar("g", baseG);
  prefs.putUChar("b", baseB);
  prefs.putUChar("cap", brightCap);
  prefs.end();
}

static void clearFace() {
  fill_solid(faceLeds, FACE_LEDS, CRGB::Black);
}

// Index 0..FACE_LEDS-1 on the one strip. Same split as swatch_layout().
static void paintRange(int start, int count, uint8_t r, uint8_t g, uint8_t b) {
  CRGB color(r, g, b);
  for (int i = 0; i < count; i++) {
    int idx = start + i;
    if (idx < 0 || idx >= FACE_LEDS) continue;
    faceLeds[idx] = color;
  }
}

static void beginTest() {
  if (testMode) return;
  clearFace();
  testMode = true;
}

// Earlier groups take the remainder so counts differ by at most one.
// 10 LEDs into 3 groups → 4, 3, 3.
static bool paintSwatches(const uint8_t* rgb, int n) {
  if (n < 1 || n > MAX_SWATCHES || n > FACE_LEDS) return false;
  beginTest();
  int base = FACE_LEDS / n;
  int extra = FACE_LEDS % n;
  int start = 0;
  for (int i = 0; i < n; i++) {
    int count = base + (i < extra ? 1 : 0);
    paintRange(start, count, rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]);
    start += count;
  }
  return true;
}

static bool parseRgb(const char* src, uint8_t* out, int maxTriples, int* nOut) {
  const char* p = strstr(src, "\"rgb\"");
  if (!p) return false;
  p = strchr(p, '[');
  if (!p) return false;
  p++;
  int n = 0;
  int limit = maxTriples * 3;
  while (*p && *p != ']') {
    while (*p == ' ' || *p == '\t' || *p == ',') p++;
    if (*p == ']') break;
    if (*p == '-') return false;
    if (*p == '+') p++;
    char* end = nullptr;
    long value = strtol(p, &end, 10);
    if (end == p || value < 0 || value > 255 || !isDelim(*end)) return false;
    if (n >= limit) return false;
    out[n++] = (uint8_t)value;
    p = end;
  }
  if (*p != ']' || n == 0 || (n % 3) != 0) return false;
  *nOut = n / 3;
  return true;
}

static bool startPose(const char* name) {
  Pose next;
  if (strcmp(name, "tip") == 0) next = POSE_TIP;
  else if (strcmp(name, "beam") == 0) next = POSE_BEAM;
  else if (strcmp(name, "reckon") == 0) next = POSE_RECKON;
  else if (strcmp(name, "chuckle") == 0) next = POSE_CHUCKLE;
  else if (strcmp(name, "attend") == 0) next = POSE_ATTEND;
  else if (strcmp(name, "twirl") == 0) next = POSE_TWIRL;
  else return false;  // retired names included: do not stall the mouth
  pose = next;
  poseMs = millis();
  return true;
}

static void handleLine(const char* line) {
  // "set_brightness_cap" is 19 characters; keep the buffer larger than that.
  char op[32];
  if (!copyField(line, "op", op, sizeof(op))) return;
  if (strcmp(op, "ping") == 0) {
    Serial2.print("{\"op\":\"pong\"}\n");
    return;
  }
  if (strcmp(op, "set_color") == 0) {
    uint8_t r, g, b;
    if (!copyU8(line, "r", &r) || !copyU8(line, "g", &g) || !copyU8(line, "b", &b)) return;
    if (r == baseR && g == baseG && b == baseB) return;
    baseR = r;
    baseG = g;
    baseB = b;
    savePrefs();
    return;
  }
  if (strcmp(op, "set_brightness_cap") == 0) {
    uint8_t cap;
    if (!copyU8(line, "cap", &cap)) return;
    if (cap == brightCap) return;
    brightCap = cap;
    savePrefs();
    return;
  }
  if (strcmp(op, "test_fill") == 0) {
    uint8_t r, g, b;
    if (!copyU8(line, "r", &r) || !copyU8(line, "g", &g) || !copyU8(line, "b", &b)) return;
    bool bad = false;
    long start = 0;
    long count = 0;
    bool hasStart = copyLong(line, "start", &start, &bad);
    bool hasCount = copyLong(line, "count", &count, &bad);
    if (bad || hasStart != hasCount) return;
    if (!hasStart) {
      start = 0;
      count = FACE_LEDS;
    } else if (start < 0 || count < 1 || start > FACE_LEDS || count > FACE_LEDS ||
               start + count > FACE_LEDS) {
      return;
    }
    beginTest();
    // Exact RGB. Not multiplied by the brightness cap. The 4 A limit still
    // applies inside FastLED.show().
    paintRange((int)start, (int)count, r, g, b);
    return;
  }
  if (strcmp(op, "test_swatches") == 0) {
    uint8_t n = 0;
    if (!copyU8(line, "n", &n)) return;
    uint8_t rgb[MAX_SWATCHES * 3];
    int parsed = 0;
    if (!parseRgb(line, rgb, MAX_SWATCHES, &parsed)) return;
    if (parsed != (int)n) return;
    paintSwatches(rgb, parsed);
    return;
  }
  if (strcmp(op, "test_exit") == 0) {
    testMode = false;
    return;
  }
  // Gestures and mouth frames must not paint over a test pattern.
  if (testMode) return;
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
      if (lineDrop || lineLen + 1 >= sizeof(lineBuf)) {
        lineDrop = true;
        lineLen = 0;
      } else {
        lineBuf[lineLen++] = c;
      }
      continue;
    }
    lineBuf[lineLen] = 0;
    if (!lineDrop && lineLen > 0) handleLine(lineBuf);
    lineLen = 0;
    lineDrop = false;
  }
  if ((uint32_t)(millis() - lastUartMs) >= LINK_QUIET_MS) {
    visemeLive = false;
    pose = POSE_NEUTRAL;
  }
}

static void render(uint32_t now) {
  applyServos(now);
  if (testMode) return;
  if (visemeLive) advanceSpeech();
  else advanceIdle(now);
  paintGlow();
}

void setup() {
  radioOff();
  Serial.begin(115200);
  loadPrefs();
  Serial2.begin(115200, SERIAL_8N1, PIN_RX, PIN_TX);
  Wire.begin(PIN_SDA, PIN_SCL);
  pca.begin();
  // begin() may call Wire.begin() with no pins and take the default I2C
  // pins (SDA GPIO 21, SCL GPIO 22). Put SDA/SCL back on 22/23.
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.beginTransmission(PCA_ADDR);
  servoOk = (Wire.endTransmission() == 0);
  if (servoOk) {
    pca.setPWMFreq(50);
    writeDeg(CH_BROW_L, ANG_LEVEL);
    writeDeg(CH_BROW_R, ANG_LEVEL);
    writeDeg(CH_HAT, ANG_HAT_REST);
    writeDeg(CH_MUSTACHE, ANG_MUSTACHE_REST);
  } else {
    Serial.println("pca9685 missing, servos off");
  }
  // 5 V LED rail, 4 A budget; branch fuses are 5 A.
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 4000);
  FastLED.addLeds<WS2812B, PIN_FACE, GRB>(faceLeds, FACE_LEDS);
  FastLED.clear(true);
  goIdle();
  lastUartMs = millis();
  Serial.printf("face up, wifi off, colour %u,%u,%u cap %u\n", baseR, baseG, baseB, brightCap);
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
