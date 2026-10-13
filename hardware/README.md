# Porch hardware

Wiring and parts for the pumpkin face. The crate software is unchanged.

- [BOM](BOM.md)
- [Wiring diagram](wiring.svg)

The Pi talks to an ESP32 DevKit V1 over UART at 3.3 V, no level shifter. Pi GPIO 14 (header pin 8) goes through 1 kΩ into ESP32 GPIO 16. ESP32 GPIO 17 goes to Pi GPIO 15 (header pin 10). The two boards share a ground. The ESP32 owns the three WS2812 data lines and the PCA9685. Gesture names still come from the desktop as JSON. The ESP32 is what turns a name into a pulse.

Three rails, never shared: LED 5 V (Mean Well LRS-150-5, 22 A) with a 5 A fuse on the mouth branch and a 5 A fuse on the eyes branch, servo 5 V (8–10 A, 8 A fuse), Pi 5.1 V USB-C. Firmware holds the LED rail at 4 A. Grounds meet at one star point. Bricks on before data. Data off before the bricks.

Mains is a grounded 3-wire cord, earth on the LRS FG screw, and a fused IEC inlet (~3 A slow-blow) ahead of all three bricks. Cover the LRS terminals. The box stays under cover, off the ground, out of spray and direct afternoon sun, with a drip loop on every cable. Anker Solix AC output stays on; eco/auto-off browns the rails out.

Mouth is one WS2812B 8×32 panel (256 px). Each eye is 16 px from the 60/m reel. Shifter inputs A1–A3 have 10 kΩ pull-downs. Servo channels: brow L, brow R, hat, mustache (`twirl`). On the Pi, `enable_uart=1` only — do not set `dtoverlay=disable-bt` (the JBL speaker is on Bluetooth). Disconnect the ESP32 5 V lead before USB.

The Pi writes one JSON line per command at 115200 8N1: `{"op":"viseme","id":"rest|aa|ee|oh|mbp","rms":0.0}` at about 30 Hz while audio plays, `{"op":"gesture","name":"tip|beam|reckon|chuckle|attend|twirl"}`, `{"op":"idle"}` when playback ends, and `{"op":"ping"}` (the ESP32 replies `{"op":"pong"}`). A gesture line does not carry pixels. Unknown names, including stamp, wave, think, laugh, bow, and listen, are ignored. If the UART is quiet for two seconds the ESP32 stays on candle flicker and a slow blink, so a Pi reboot does not freeze the face.

## Face colour calibration

The stored face colour defaults to yellow-gold `255,140,20`. A brightness cap (default 102, about 40%) limits normal speech and eye levels. Both are saved on the ESP32 and reloaded at boot. Test mode paints exact RGB and does not apply that cap, so a swatch is the colour that was commanded. The firmware still holds the LED rail at 4 A, which can dim a very bright frame evenly. Gestures and mouth frames are ignored until test mode exits, so they do not paint over the swatches.

Put the camera on the lit face. One `--roi x,y,w,h` is split left to right into the swatch count (default 8), in the same order as the LED groups: mouth wire order, then the left eye, then the right eye. Pass `--roi` once per swatch to place the regions yourself. The default target is sRGB `255,120,0`. The script locks exposure and white balance with `v4l2-ctl` when it can (manual exposure, temperature 4000 K) and keeps going if a control name is missing.

```bash
python3 clients/pi/face_calibrate.py \
  --port /dev/serial0 \
  --camera 0 \
  --device /dev/video0 \
  --roi 400,300,960,200 \
  --apply
```

`--apply` writes the winning RGB with `set_color` after leaving test mode. Without it, the face returns to its previous colour. The JSON result and a labelled contact sheet go in `logs/` (that directory is not committed). `--exposure 250` is the manual exposure in 100 µs units. `--refine none` skips the local search.
