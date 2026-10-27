// Idle candle maths for the face strip. Host-testable: this header includes
// only <stdint.h>. main.cpp samples noise and smooths; the numbers live here.
//
// Height 0 is the bottom of the face and 255 is the top. Brightness is a
// band interpolated over that height, plus a rare flare or gutter from a
// second noise sample. Colour shifts G and B off the saved base (R stays).
// A cap of 0 does not apply here as a paint: callers still paint black.

#ifndef FLAME_H
#define FLAME_H

#include <stdint.h>

// Normal band at the bottom of the face, before the cap shrinks it.
static constexpr uint8_t FLAME_BOT_LO = 40;
static constexpr uint8_t FLAME_BOT_HI = 90;
// Normal band at the top. 102 is also the default brightness cap, so the
// top of the flame already sits on the cap and a higher cap does not
// brighten idle.
static constexpr uint8_t FLAME_TOP_LO = 20;
static constexpr uint8_t FLAME_TOP_HI = 102;

// inoise8 spends almost all of its time near 128 and tops out around 230,
// so a threshold near 250 would never fire. These sit in that real tail:
// about 2% of samples cross the onset, and the full lift is reached by
// the time the sample hits the practical peak. On a uniform 0–255 sweep
// the window is wider; it is still only the outer end of the byte.
static constexpr uint8_t FLAME_FLARE_AT = 200;
static constexpr uint8_t FLAME_FLARE_FULL = 216;
static constexpr uint8_t FLAME_GUTTER_AT = 56;
static constexpr uint8_t FLAME_GUTTER_FULL = 40;
// A full gutter aims at this fraction of the band floor, and never at 0.
static constexpr uint8_t FLAME_GUTTER_NUM = 1;
static constexpr uint8_t FLAME_GUTTER_DEN = 2;

// G and B as multiples of the saved channels, in units of 1/1024.
// Dim is half of G and none of B. Bright is 9/7 of G and 5/2 of B, so the
// default base (140, 20) lands on (70, 0) and (180, 50) when height does
// not lean. 1317 is 9/7 of 1024 rounded so the integer divide yields 180.
static constexpr int FLAME_Q = 1024;
static constexpr int FLAME_DIM_G_Q = 512;
static constexpr int FLAME_BASE_Q = 1024;
static constexpr int FLAME_BRIGHT_G_Q = 1317;
static constexpr int FLAME_DIM_B_Q = 0;
static constexpr int FLAME_BRIGHT_B_Q = 2560;
// Where inside the band the saved colour sits, on a 0–255 position.
static constexpr int FLAME_POS_MID = 128;
static constexpr int FLAME_POS_HI = 255;

// Height lean, in units of 1/256. 256 is no lean. The top band is wider
// than the bottom, so a small lean would leave a dim bottom LED with less
// G than a top LED at the same level. These two are the smallest pair
// (C++ truncates toward zero) that keeps height 128 untinted and still
// makes the bottom yellower at every equal level, while a bright top LED
// stays above the saved G.
static constexpr int FLAME_HEIGHT_Q = 256;
static constexpr int FLAME_HEIGHT_BOT = 311;
static constexpr int FLAME_HEIGHT_TOP = 201;

static_assert(FLAME_BOT_LO < FLAME_BOT_HI, "bottom band");
static_assert(FLAME_TOP_LO < FLAME_TOP_HI, "top band");
static_assert(FLAME_TOP_HI >= FLAME_BOT_HI, "top reaches at least the bottom hi");
static_assert(FLAME_GUTTER_FULL < FLAME_GUTTER_AT, "gutter has a range");
static_assert(FLAME_FLARE_AT < FLAME_FLARE_FULL, "flare has a range");
static_assert(FLAME_GUTTER_AT < FLAME_POS_MID && FLAME_FLARE_AT > FLAME_POS_MID,
              "events stay off the middle of the noise byte");
static_assert((255 - FLAME_FLARE_AT + 1) * 4 < 256, "flares are a minority of the byte");
static_assert((FLAME_GUTTER_AT + 1) * 4 < 256, "gutters are a minority of the byte");
static_assert(FLAME_HEIGHT_BOT + (FLAME_HEIGHT_TOP - FLAME_HEIGHT_BOT) * 128 / 255 == FLAME_HEIGHT_Q,
              "height 128 is untinted");
static_assert(140 * FLAME_DIM_G_Q / FLAME_Q == 70, "default dim G");
static_assert(140 * FLAME_BRIGHT_G_Q / FLAME_Q == 180, "default bright G");
static_assert(20 * FLAME_BRIGHT_B_Q / FLAME_Q == 50, "default bright B");

struct FlameBand {
  uint8_t lo;
  uint8_t hi;
};

struct FlameRgb {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

// Normal band for this height, shrunk so it fits in 1..cap when cap >= 1.
// cap 0 collapses to 1; the pixel is still painted black by the caller.
inline FlameBand flameBand(uint8_t height, uint8_t cap) {
  int lo = (int)FLAME_BOT_LO + ((int)FLAME_TOP_LO - (int)FLAME_BOT_LO) * (int)height / 255;
  int hi = (int)FLAME_BOT_HI + ((int)FLAME_TOP_HI - (int)FLAME_BOT_HI) * (int)height / 255;
  if (cap == 0) {
    return FlameBand{1, 1};
  }
  if (hi > (int)cap) hi = (int)cap;
  if (hi < 1) hi = 1;
  if (lo > hi) lo = hi;
  if (lo < 1) lo = 1;
  return FlameBand{(uint8_t)lo, (uint8_t)hi};
}

// Unsmoothed idle target. noise is the main flame sample, event is the
// flare/gutter sample. Result is in [1, cap] when cap >= 1, and is 1 when
// cap is 0 (the paint path turns that into black).
inline uint8_t flameLevel(uint8_t height, uint8_t noise, uint8_t event, uint8_t cap) {
  FlameBand band = flameBand(height, cap);
  int lo = (int)band.lo;
  int hi = (int)band.hi;
  int level = lo + (hi - lo) * (int)noise / 255;

  if (cap >= 1 && event >= FLAME_FLARE_AT) {
    int ceil = (int)cap < (int)FLAME_TOP_HI ? (int)cap : (int)FLAME_TOP_HI;
    if (ceil > hi) {
      int span = (int)FLAME_FLARE_FULL - (int)FLAME_FLARE_AT;
      int s = (int)event - (int)FLAME_FLARE_AT;
      if (s > span) s = span;
      int lift = ( (ceil - hi) * s + span / 2 ) / span;
      if (lift < 1) lift = 1;
      int flared = hi + lift;
      if (flared > ceil) flared = ceil;
      if (flared > level) level = flared;
    }
  } else if (cap >= 1 && event <= FLAME_GUTTER_AT) {
    int floor = lo * (int)FLAME_GUTTER_NUM / (int)FLAME_GUTTER_DEN;
    if (floor < 1) floor = 1;
    if (floor < lo) {
      int span = (int)FLAME_GUTTER_AT - (int)FLAME_GUTTER_FULL;
      int s = (int)FLAME_GUTTER_AT - (int)event;
      if (s > span) s = span;
      int dip = ( (lo - floor) * s + span / 2 ) / span;
      if (dip < 1) dip = 1;
      int gut = lo - dip;
      if (gut < floor) gut = floor;
      if (gut < 1) gut = 1;
      if (gut < level) level = gut;
    }
  }

  if (level < 1) level = 1;
  if (cap >= 1 && level > (int)cap) level = (int)cap;
  if (level > 255) level = 255;
  return (uint8_t)level;
}

// 0 at the dim end of the band, 255 at the bright end. Flares and gutters
// clamp to those ends so the colour stops at hot yellow and deep orange.
inline int flamePos(uint8_t level, uint8_t height, uint8_t cap) {
  FlameBand band = flameBand(height, cap);
  if (band.hi <= band.lo) return FLAME_POS_MID;
  if (level <= band.lo) return 0;
  if (level >= band.hi) return FLAME_POS_HI;
  return ((int)level - (int)band.lo) * FLAME_POS_HI / ((int)band.hi - (int)band.lo);
}

inline int flameHeightScale(uint8_t height) {
  return FLAME_HEIGHT_BOT + (FLAME_HEIGHT_TOP - FLAME_HEIGHT_BOT) * (int)height / 255;
}

// Adjusted RGB before the brightness multiply. R is the saved red.
inline FlameRgb flameColor(uint8_t baseR, uint8_t baseG, uint8_t baseB,
                           uint8_t level, uint8_t height, uint8_t cap) {
  int pos = flamePos(level, height, cap);
  int gq;
  int bq;
  if (pos <= FLAME_POS_MID) {
    gq = FLAME_DIM_G_Q + (FLAME_BASE_Q - FLAME_DIM_G_Q) * pos / FLAME_POS_MID;
    bq = FLAME_DIM_B_Q + (FLAME_BASE_Q - FLAME_DIM_B_Q) * pos / FLAME_POS_MID;
  } else {
    int rise = pos - FLAME_POS_MID;
    int span = FLAME_POS_HI - FLAME_POS_MID;
    gq = FLAME_BASE_Q + (FLAME_BRIGHT_G_Q - FLAME_BASE_Q) * rise / span;
    bq = FLAME_BASE_Q + (FLAME_BRIGHT_B_Q - FLAME_BASE_Q) * rise / span;
  }
  int lean = flameHeightScale(height);
  int g = (int)((uint32_t)baseG * (uint32_t)gq * (uint32_t)lean / (uint32_t)(FLAME_Q * FLAME_HEIGHT_Q));
  int b = (int)((uint32_t)baseB * (uint32_t)bq * (uint32_t)lean / (uint32_t)(FLAME_Q * FLAME_HEIGHT_Q));
  if (g > 255) g = 255;
  if (b > 255) b = 255;
  if (g < 0) g = 0;
  if (b < 0) b = 0;
  return FlameRgb{baseR, (uint8_t)g, (uint8_t)b};
}

// Same multiply as faceColor: scale is clamped to cap, then channel * scale / 255.
// cap 0 (or a zero scale) is black. scale is the flicker level.
inline FlameRgb flamePixel(uint8_t baseR, uint8_t baseG, uint8_t baseB,
                           uint8_t level, uint8_t height, uint8_t cap) {
  if (cap == 0 || level == 0) return FlameRgb{0, 0, 0};
  uint8_t scale = level > cap ? cap : level;
  FlameRgb tint = flameColor(baseR, baseG, baseB, level, height, cap);
  return FlameRgb{
      (uint8_t)(((uint16_t)scale * tint.r) / 255),
      (uint8_t)(((uint16_t)scale * tint.g) / 255),
      (uint8_t)(((uint16_t)scale * tint.b) / 255),
  };
}

#endif
