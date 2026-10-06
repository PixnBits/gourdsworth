# Porch hardware

Wiring and parts for the pumpkin face. The crate software is unchanged.

- [BOM](BOM.md)
- [Wiring diagram](wiring.svg)

The Pi talks to an ESP32 DevKit V1 over UART (GPIO 14/15 to ESP32 GPIO 16/17, 3.3 V, no shifter). The ESP32 owns the three WS2812 data lines and the PCA9685. Gesture names still come from the desktop as JSON. The ESP32 is what turns a name into a pulse.

Three rails, never shared: LED 5 V (Mean Well LRS-150-5, 22 A), servo 5 V (8–10 A), Pi 5.1 V USB-C. Grounds meet at one star point. Bricks on before data. Data off before the bricks.
