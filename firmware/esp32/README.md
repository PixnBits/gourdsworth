# ESP32 face

ESP32 DevKit V1, WROOM-32, 30-pin. Not S3, not C3. Arduino framework via PlatformIO.

The ESP32 is the only device that pulses servos or clocks LEDs. Wi-Fi and Bluetooth stay off. The Pi writes JSON lines on the UART; this firmware turns them into a pose and a mouth frame.

## Libraries

- [FastLED](https://github.com/FastLED/FastLED) 3.6, the default ESP32 RMT driver (do not define `FASTLED_ESP32_I2S`). Three `WS2812B` controllers, one pin each. The Pi does not bit-bang the strips.
- [Adafruit PWM Servo Driver](https://github.com/adafruit/Adafruit-PWM-Servo-Driver-Library) for the PCA9685 at `0x40`. Servo power is the board V+ rail, not the ESP32 5 V pin.

## Pins

| Function | GPIO |
|---|---|
| UART RX from Pi GPIO 14 TX | 16 |
| UART TX to Pi GPIO 15 RX | 17 |
| Mouth data into the 74AHCT125 | 18 |
| Eye L data | 19 |
| Eye R data | 21 |
| I2C SDA / SCL to PCA9685 | 22 / 23 |

PCA9685 channels: 0 left brow, 1 right brow, 2 hat tip, 3 mustache (center at rest; `twirl` swings it). Gestures: `tip`, `beam`, `reckon`, `chuckle`, `attend`, `twirl`. Mouth is one 8×32 panel, `MOUTH_COLS` × `MOUTH_ROWS` (32×8, 256 px) on GPIO 18. `mouthIndex()` snakes 32-pixel rows (even rows left to right, odd rows right to left). Many 8×32 panels snake in 8-pixel columns instead, so light a test pattern first and change `mouthIndex()` if the mouth is mirrored or scrambled. Each eye is its own run, `EYE_LEDS` 16, GPIO 19 and 21. Do not chain the three data lines.

Base colour defaults to yellow-gold `R=255, G=140, B=20`. Speech brightness is still `50 + loudness * 52`, then clamped to the brightness cap (default 102, 40% of 255). A higher cap does not brighten speech past 102. Wide eyes use the cap. Every other normal-mode scale is also limited to the cap; at 102 that does not change flicker or a resting eye, because those levels were already below 102. `FULL_WHITE_TEST` still forces normal-mode lit pixels to white for a bench check (on the order of 6 A before the power limit). Default is off. `FastLED.setMaxPowerInVoltsAndMilliamps(5, 4000)` holds the LED rail at 4 A.

`set_color` and `set_brightness_cap` are stored in NVS namespace `face` (keys `r`, `g`, `b`, `cap`) and loaded in `setup` before the first frame.

## UART lines

One JSON object per line, 115200 8N1, no checksum. The same quoted-key scan that reads `op` reads the new fields. A bad field drops the line.

| op | fields | effect |
|---|---|---|
| `ping` | | reply `{"op":"pong"}` |
| `idle` | | candle flicker, neutral pose. Ignored in test mode |
| `gesture` | `name` | one of `tip`, `beam`, `reckon`, `chuckle`, `attend`, `twirl`. Ignored in test mode |
| `viseme` | `id`, `rms` | mouth shape. Ignored in test mode |
| `set_color` | `r`, `g`, `b` (0–255) | base colour, saved |
| `set_brightness_cap` | `cap` (0–255) | normal-mode cap, saved |
| `test_fill` | `r`, `g`, `b`, optional `start`, `count` | enter test mode and paint exact RGB |
| `test_swatches` | `n`, `rgb` (flat triples) | `n` contiguous groups, each one colour. `n` is 1–16 |
| `test_exit` | | leave test mode; the next frame is the normal face |

LED index 0 is `mouth[0]` in FastLED wire order, then the left eye (16), then the right eye (16). `test_fill` with no range paints all of them. With `start` and `count` it paints `[start, start+count)`. The first test command clears the face to black; a later range fill leaves the other test pixels alone.

Swatch groups use the same split as `swatch_layout` in `clients/pi/face_link.py`: earlier groups get one extra LED when the count does not divide. 10 LEDs into 3 groups is 4, 3, 3. `n` must match the number of RGB triples. A mismatch is ignored.

Test-mode pixels are the commanded RGB. They do not pass through the brightness cap and they ignore `FULL_WHITE_TEST`. The 4 A limit still applies at `FastLED.show()`, so a full-bright frame can be scaled down evenly. Hue relationships inside one frame stay put.

If the UART is quiet for 2 seconds, or an `idle` line arrives, the mouth goes back to candle flicker and the eyes to a slow blink. A quiet link does not leave test mode, and an `idle` line is ignored while test mode is on, so the swatches stay until `test_exit`. A Pi reboot does not freeze the face. Unknown gesture names, including `stamp`, `wave`, `think`, `laugh`, `bow`, and `listen`, are ignored and do not stall the mouth.

## Flash

Disconnect the LED-brick lead from the ESP32 5 V pin, or leave that brick off, before plugging USB in. The PC's USB 5 V must not tie to the LED rail. From this directory, on a machine with PlatformIO:

```bash
pio run -t upload --upload-port /dev/ttyUSB0
```

`/dev/ttyUSB0` is the DevKit's USB UART (flash and the one boot line). The face protocol is the other UART, GPIO 16/17, which the Pi sees as `/dev/serial0`.

On the Pi, set `enable_uart=1` and free the login console (raspi-config → Interface → Serial → hardware on, shell off) so `/dev/serial0` is the mini UART on GPIO 14/15. Fine at 115200. Do not use `dtoverlay=disable-bt`; the JBL speaker is on Bluetooth. 3.3 V, no shifter. 1 kΩ in series from GPIO 14 TX to ESP32 GPIO 16 RX.

## Confirm pong

ESP32 plugged into the Pi UART, firmware flashed, Wi-Fi still off. From the Pi:

```bash
python3 - <<'PY'
import serial
port = serial.Serial("/dev/serial0", 115200, timeout=1)
port.write(b'{"op":"ping"}\n')
print(port.readline().decode("utf-8", "replace"))
PY
```

Expect `{"op":"pong"}`. No pong means the console still owns `/dev/serial0`, TX/RX are swapped, or the brick is off. The face should already be flickering before this line is sent.
