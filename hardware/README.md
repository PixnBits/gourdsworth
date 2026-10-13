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

Put the camera on the lit face. Capture is 640×480, the same frame the ROI was measured on. `--width` and `--height` override that (defaults 640 and 480). The script asks the camera for that size, drops about ten warm-up frames, and each grab reads a few buffered frames. `frame_size` is the size the camera actually delivered. An ROI that falls outside that frame is an error. The default target is sRGB `255,120,0`.

Swatch groups follow LED wire order: mouth, then the left eye, then the right eye. A left-to-right split of one camera ROI reads a different LED in each slot, so the measured red channel follows the slot and the colour is lost. One `--roi x,y,w,h` therefore uses one swatch. The whole face shows one colour and the ROI is that colour. `--swatches N` opts into a left-to-right split of that single ROI. Pass `--roi` once per group when each group needs its own box.

After each swatch command the script waits `--settle` (default 0.25 s), then grabs and drops frames until at least `--drain-frames` (default 3) have finished after the command time plus that wait, then takes the per-pixel median of `--median-frames` (default 3) grabs. It never reads the serial port. The ESP32 does not reply to these lines.

Before the coarse pass the script lights a bright probe. If that frame is black, or an ROI mean is under about 2 on the 0–255 scale, it stops. Unless `--skip-sanity` is set, it then checks that the camera is following the LEDs. Off `(0,0,0)` versus the target must raise ROI total brightness (the sum of the channel means) by at least 15. A ramp of the target at 0, 0.25, 0.5, 0.75, and 1 must stay non-decreasing within max(3, 5%) and the last step minus the first must clear that same rise. Total brightness is the signal, because the orange shell filters green down to about 1 and a green-only check would reject a good frame. If the sweep fails, the frames look stale or lagging, or the exposure is bad or clipped, and the script stops. The same black check runs after the search: when every swatch's brightest channel is under that floor, it stops. Either way it leaves test mode and does not store a colour. A dark surround around a small lit ROI is fine; the ROI itself has to show the glow. The sweep values are stored in the JSON.

`--exposure` defaults to `auto`. With v4l2, auto lights a mid swatch and measures 250, 500, 1000, then 2000 (100 µs units) with the same settle and drain. A step whose clip fraction is over 0.02 on any channel is rejected. Among the rest it keeps the step with the highest ROI max-channel mean that is still at most 200. If every step is clipped it keeps the lowest and warns. The per-step stats are stored in the JSON. `--exposure 1000` locks that manual value instead (1000 is the built-in manual exposure). `--skip-v4l2` leaves the camera's own auto exposure. White balance is locked to 4000 K when v4l2 works, trying both the old and the new control names, and a missing name is skipped.

```bash
python3 clients/pi/face_calibrate.py \
  --port /dev/serial0 \
  --camera 0 \
  --device /dev/video0 \
  --width 640 \
  --height 480 \
  --roi 230,300,140,30 \
  --apply
```

`--apply` writes the winning RGB with `set_color` after leaving test mode. A black capture or a failed sanity sweep does not, and the process exits nonzero. Without `--apply`, the face returns to its previous colour. The JSON result and a labelled contact sheet go in `logs/` (that directory is not committed). `--refine none` skips the local search.
