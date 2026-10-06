# Porch hardware BOM

Ordered October 2026 for the Gilbert porch greeter. Prices are street prices from that week, not a quote. Acrylic is the only part not in the Amazon order.

Three rails, never shared. LED 5 V does not feed servos. The Pi has its own 5.1 V USB-C brick. Grounds meet at one star point.

## Face

| Qty | Part | Notes |
|---|---|---|
| 1 | BTF-LIGHTING WS2812B, 60 LED/m, 5 m, IP30 | One reel for mouth and both eyes so the amber matches. Cut on the marks. Mouth ~200–256 px, snaked. Each eye 12–20 px. |
| 1 | 1/8 in white opal or frosted acrylic | Laird Plastics, Tempe, cut oversized for an inside flange. Not glued to the polyethylene shell. |
| 4 | Acrylic or printed tabs | Screw to the acrylic only. Capture the 1–2 mm shell from inside. |
| 1 | Neutral-cure silicone | Weather bead on the overlap. Not the structure. |

## Control

| Qty | Part | Notes |
|---|---|---|
| 1 | Raspberry Pi 4 | Audio and the crate client. No LED data, no servo pulses. |
| 1 | ESP32 DevKit V1, WROOM-32, 30-pin | UART from the Pi. Three RMT LED channels. I2C to the servo board. Wi-Fi off. |
| 1 | 74AHCT125 quad level shifter | 3.3 V in, 5 V out. One channel each for mouth, eye L, eye R. OE to ground. Not a MOSFET I2C shifter. |
| 3 | 330 Ω resistor | Series on each shifter Y output. |
| 1 | PCA9685 16-channel board | VCC from ESP32 3V3. V+ from the servo brick only. |
| 4 | Hobby micro servos (SG90 / MG90S) | Brows, hat tip, moustache. Channels 0–3. |

## Power

| Qty | Part | Notes |
|---|---|---|
| 1 | Mean Well LRS-150-5 or LRS-150F-5 | LED rail. 5 V, 22 A, 110 W. Trim about 5.1 V at the terminals. 15 A fuse on the positive out. |
| 1 | 5 V, 8–10 A brick | Servo rail. 8 A fuse. Sized for four micros stalled at once. Too small for MG996R-class servos. |
| 1 | Raspberry Pi 15 W USB-C supply | 5.1 V, 3 A. Into the Pi only. |
| 1 | 40 mm 5 V fan, ~0.1 A | Across the three bricks. From the LED brick positive. |
| 4 | 1000 µF, 16 V electrolytic | Mouth left, mouth right, eye L, eye R. At the strip feed, not at the brick. Polarity matters. |
| 1 | 470 µF, 16 V | At the ESP32 5 V pin. |
| 1 | Inline fuse holder + 15 A | LED positive. |
| 1 | Inline fuse holder + 8 A | Servo positive. |
| 2 m | 16 or 18 AWG | LED 5 V run, both ends of the mouth. |
| 1 m | 22 AWG | Data, UART, I2C. |
| 1 | 120 V cord grip | Drip loop outside the shell. |

## Pins

| From | To | Signal |
|---|---|---|
| Pi GPIO 14 TX | ESP32 GPIO 16 RX | UART, 3.3 V, no shifter |
| Pi GPIO 15 RX | ESP32 GPIO 17 TX | UART, 3.3 V |
| ESP32 GPIO 18 | 74AHCT125 A1, Y1 to mouth DIN | LED data after 330 Ω |
| ESP32 GPIO 19 | A2, Y2 to eye L | LED data |
| ESP32 GPIO 21 | A3, Y3 to eye R | LED data |
| ESP32 GPIO 22 / 23 | PCA9685 SDA / SCL | I2C |
| ESP32 3V3 | PCA9685 VCC | Logic only |
| ESP32 5V pin | LED brick terminal | ~100 mA, Wi-Fi off. Not from the end of a strip. |
| LED brick 5 V | strips, shifter VCC, fan | Inject mouth at both ends |
| Servo brick 5 V | PCA9685 V+ | Servo power only |
| Pi USB-C | Pi brick | Not the LED brick |

Bring the bricks up before data. Stop data before killing the bricks. Do not hot-plug a strip into a live brick without the capacitor already across it.

Diagram: [wiring.svg](wiring.svg).
