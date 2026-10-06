# Porch hardware

Wiring and parts for the pumpkin face. The crate software is unchanged.

- [BOM](BOM.md)
- [Wiring diagram](wiring.svg)

The Pi talks to an ESP32 DevKit V1 over UART (GPIO 14/15 to ESP32 GPIO 16/17, 3.3 V, no shifter). The ESP32 owns the three WS2812 data lines and the PCA9685. Gesture names still come from the desktop as JSON. The ESP32 is what turns a name into a pulse.

Three rails, never shared: LED 5 V (Mean Well LRS-150-5, 22 A), servo 5 V (8–10 A), Pi 5.1 V USB-C. Grounds meet at one star point. Bricks on before data. Data off before the bricks.

The Pi writes one JSON line per command at 115200 8N1: `{"op":"viseme","id":"rest|aa|ee|oh|mbp","rms":0.0}` at about 30 Hz while audio plays, `{"op":"gesture","name":"tip|beam|reckon|chuckle|attend"}`, `{"op":"idle"}` when playback ends, and `{"op":"ping"}` (the ESP32 replies `{"op":"pong"}`). A gesture line does not carry pixels. Unknown names, including stamp, wave, think, laugh, bow, and listen, are ignored. If the UART is quiet for two seconds the ESP32 stays on candle flicker and a slow blink, so a Pi reboot does not freeze the face.
