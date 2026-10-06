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

PCA9685 channels: 0 left brow, 1 right brow, 2 hat tip, 3 mustache (center at rest; `twirl` swings it). Gestures: `tip`, `beam`, `reckon`, `chuckle`, `attend`, `twirl`. Mouth length is `MOUTH_COLS` × `MOUTH_ROWS` (default 32×8, snaked). Each eye is its own strip, `EYE_LEDS` default 16 (keep it in 12–20). Do not chain the three data lines.

Amber only: base `R=255, G=80, B=0`. Speech brightness is the viseme mask times a floor plus loudness, still capped at 40%. `FULL_WHITE_TEST` forces lit pixels to white for a bench check. Default is off. No blue channel.

If the UART is quiet for 2 seconds, or an `idle` line arrives, the mouth goes back to candle flicker and the eyes to a slow blink. A Pi reboot does not freeze the face. Unknown gesture names, including `stamp`, `wave`, `think`, `laugh`, `bow`, and `listen`, are ignored and do not stall the mouth.

## Flash

From this directory, on a machine with PlatformIO and the DevKit on USB:

```bash
pio run -t upload --upload-port /dev/ttyUSB0
```

`/dev/ttyUSB0` is the DevKit's USB UART (flash and the one boot line). The face protocol is the other UART, GPIO 16/17, which the Pi sees as `/dev/serial0`.

On the Pi, free that UART from the login console (raspi-config → Interface → Serial → hardware on, shell off) so the crate client owns it. 115200 8N1, 3.3 V, no shifter.

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
