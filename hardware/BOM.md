# Porch hardware BOM

Prices are rough street prices, not a quote.

Three rails, never shared. LED 5 V does not feed servos. The Pi has its own 5.1 V USB-C brick. Grounds meet at one star point.

## Face

| Qty | Part | Notes |
|---|---|---|
| 1 | BTF-LIGHTING WS2812B 8×32 flexible panel, 256 px, 10 mm | Mouth. About 32 × 8 cm. A 32-column cut of 60/m strip is ~53 cm, too wide for the ~40 cm face, and would use 256 of the reel's 300 px. Serpentine. Power leads at both ends. Amber may differ a little from the eye reel. |
| 1 | BTF-LIGHTING WS2812B, 60 LED/m, 5 m, IP30 | Eyes only. 2 × 16 px = 32 px (~0.55 m). Rest of the reel is spare. |
| 1 | 1/8 in white opal or frosted acrylic | Local plastics shop, cut oversized for an inside flange. Not glued to the polyethylene shell. |
| 4 | Acrylic or printed tabs | Screw to the acrylic only. Capture the 1–2 mm shell from inside. |
| 1 | Neutral-cure silicone | Weather bead on the overlap. Not the structure. |

The panel is serpentine-wired, and many 8×32 panels snake in columns of 8 rather than rows of 32. `mouthIndex()` walks 32-pixel rows, even rows left to right and odd rows right to left. Light a test pattern before closing the face, check the first-pixel corner and the snake direction, and change `mouthIndex()` to match if the mouth comes out mirrored or scrambled.

## Control

| Qty | Part | Notes |
|---|---|---|
| 1 | Raspberry Pi 4 | Audio and the crate client. No LED data, no servo pulses. |
| 1 | ESP32 DevKit V1, WROOM-32, 30-pin | UART from the Pi. Mouth on GPIO 18, eyes on GPIO 19 and 21. I2C to the servo board. Wi-Fi off. |
| 1 | 74AHCT125 quad level shifter | 3.3 V in, 5 V out. One channel each for mouth, eye L, eye R. OE1–OE3 to GND. Not a MOSFET I2C shifter. |
| 3 | 330 Ω resistor | Series on each shifter Y output. |
| 3 | 10 kΩ resistor | Pull-downs from A1–A3 to GND. Strips see a low while the ESP32 boots or resets. |
| 1 | 0.1 µF ceramic | Decoupling at the shifter VCC pin to GND. |
| 1 | 1 kΩ resistor | Series, Pi GPIO 14 TX to ESP32 GPIO 16 RX. Protects both if only one side is powered. |
| 1 | PCA9685 16-channel board | VCC from ESP32 3V3. V+ from the servo brick only. |
| 4 | Hobby micro servos (SG90 / MG90S) | ch0 brow L, ch1 brow R, ch2 hat, ch3 mustache (`twirl`). |

`enable_uart=1` on the Pi, and free the serial console, so `/dev/serial0` is the mini UART on GPIO 14/15. Fine at 115200. Do not use `dtoverlay=disable-bt`. The JBL speaker is on Bluetooth.

Do not flash or plug USB into the DevKit while the LED brick feeds the ESP32 5 V pin. Disconnect that 5 V lead, or leave the brick off, before USB. The PC's USB 5 V must never tie to the LED rail.

Spare shifter input A4 ties to GND. Tie OE4 to GND or to VCC. An unused input must not float.

## Power

| Qty | Part | Notes |
|---|---|---|
| 1 | Mean Well LRS-150-5 or LRS-150F-5 | LED rail. 5 V, 22 A, 110 W. Trim about 5.1 V at the terminals. Two 5 A branch fuses on the out (mouth, eyes). One 7.5 A is the alternative if you keep a single feed. FG is protective earth. |
| 1 | 5 V, 8–10 A brick | Servo rail. 8 A fuse on the 18 AWG pair to PCA9685 V+. Sized for four micros stalled at once. Too small for MG996R-class servos. |
| 1 | Raspberry Pi 15 W USB-C supply | 5.1 V, 3 A. Into the Pi only. |
| 1 | 40 mm 5 V fan, ~0.1 A | Across the three bricks. From the LED brick positive. Not on the pixel branches. |
| 4 | 1000 µF, 16 V, 105 °C electrolytic | Mouth left, mouth right, eye L, eye R. At the feed, not at the brick. Polarity matters. Porch box heat. |
| 1 | 470 µF, 16 V, 105 °C electrolytic | At the ESP32 5 V pin. Porch box heat. |
| 2 | Inline fuse holder + 5 A | LED branches. One mouth, one eyes. Each on an 18 AWG pair, + and GND run together. |
| 1 | Inline fuse holder + 8 A | Servo positive, on the pair to PCA9685 V+. |
| ~2 m | 18 AWG pair | LED 5 V. One pair for the mouth branch (split to both ends of the panel), one pair for the eyes. |
| ~1 m | 18 AWG pair | Servo brick to the PCA9685 V+ terminal. 8 A fuse on this run. |
| 1 m | 22 AWG | Data, UART, and I2C. A GND return with each, twisted or in the same cable: mouth, eye L, eye R, UART, I2C. |
| 1 | Grounded 3-wire cord and cord grip | Earth conductor to the LRS FG terminal. Strain relief on the cord. |
| 1 | Fused IEC inlet (C14 with fuse drawer) or a fused switch | ~3 A slow-blow (time-delay), ahead of all three bricks. |
| 1 | Terminal cover or closed enclosure | Over the LRS screw terminals. No exposed 120 V. |

Under ~1.5 A typical at the 40% amber cap. A full-white bench test (`FULL_WHITE_TEST=1`) is about 6 A, which the firmware holds down: `FastLED.setMaxPowerInVoltsAndMilliamps(5, 4000)` caps the LED rail at 4 A.

The box stays under cover, off the ground, out of sprinkler spray and direct afternoon sun. Drip loop on every cable that enters.

The Anker Solix AC inverter output must stay on. If it sleeps or auto-off, everything browns out. Keep AC output always on. Disable eco/auto-off.

## Pins

| From | To | Signal |
|---|---|---|
| Pi GPIO 14 TX | ESP32 GPIO 16 RX | UART, 3.3 V, 1 kΩ series. No shifter. GND return alongside. |
| Pi GPIO 15 RX | ESP32 GPIO 17 TX | UART, 3.3 V. GND return alongside. |
| ESP32 GPIO 18 | 74AHCT125 A1, Y1 to mouth DIN | LED data after 330 Ω. 10 kΩ A1 to GND. GND return with the data. |
| ESP32 GPIO 19 | A2, Y2 to eye L | LED data. 10 kΩ A2 to GND. GND return with the data. |
| ESP32 GPIO 21 | A3, Y3 to eye R | LED data. 10 kΩ A3 to GND. GND return with the data. |
| 74AHCT125 A4 | GND | Spare input. OE4 to GND or VCC. Do not float. |
| 74AHCT125 VCC | LED 5 V and 0.1 µF to GND | OE1–OE3 to GND. |
| ESP32 GPIO 22 / 23 | PCA9685 SDA / SCL | I2C. GND return alongside. |
| ESP32 3V3 | PCA9685 VCC | Logic only. |
| ESP32 5V pin | LED brick terminal | ~100 mA, Wi-Fi off. Not from the end of a strip. Disconnect before USB. |
| LED brick 5 V | panel both ends, eyes, shifter VCC, fan | 5 A on the mouth pair. 5 A on the eyes pair. |
| Servo brick 5 V | PCA9685 V+ | 18 AWG pair. 8 A fuse. Servo power only. |
| PCA9685 ch0 | brow L | Servo signal. |
| PCA9685 ch1 | brow R | Servo signal. |
| PCA9685 ch2 | hat | Servo signal. |
| PCA9685 ch3 | mustache | Servo signal. Used by `twirl`. |
| Pi USB-C | Pi brick | Not the LED brick. |

Bring the bricks up before data. Stop data before killing the bricks. Do not hot-plug a strip into a live brick without the capacitor already across it.

Diagram: [wiring.svg](wiring.svg).
