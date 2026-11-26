# Capture Console — parameter control + live drone radar

A local web dashboard for the thesis RF work. It does two things:

1. **Capture Console** — pick a capture/analysis script, set its parameters in a
   form, press **Start**, and watch the real script's output stream live.
2. **Live Radar** — a polar (PPI) display that plots nearby drones by **bearing
   and distance** in real time, while capturing.

Stdlib + numpy only (no Flask, nothing to `pip install`). Everything runs on
`127.0.0.1` — local machine only, not exposed to the network.

## Run it
```bash
~/.venvs/main/bin/python app.py
```
Then open:
- Console: **http://127.0.0.1:8099**
- Live radar: **http://127.0.0.1:8099/radar** (or the "Live Radar →" link)

## How it works
The server launches each selected script as a **subprocess** (with the exact
arguments from the form) and streams its stdout/stderr to the browser. A live
**command preview** shows the exact `python …` line it will run. Only one job
runs at a time; refreshing reattaches to a run already in progress.

## Available profiles

| Profile | Script | Purpose |
|---|---|---|
| ReplayAttack – IQ Capture (v3) | `iq_size_capture_v3.py capture` | Live IQ capture + feature/distance extraction (needs B210) |
| DroneTraffic – RF Capture | `capture/rf_capture.py` | DJI RF traffic capture (needs B210) |
| DroneTraffic – Simple RF Capture | `capture/rf_capture_simple.py` | Minimal RF capture (needs B210) |
| Live Capture → Radar (B210) | `live_radar_capture.py` | Real SDR: MUSIC bearing + distance → radar |
| Radar Replay (captured CSV) | `radar_replay.py` | Replay a captured CSV onto the radar (range track) |
| MUSIC – DoA (synthetic validation) | `music.py synthetic` | Validate MUSIC on a known angle |
| MUSIC – DoA (accuracy sweep) | `music.py sweep` | Error-vs-angle/SNR table |
| MUSIC – DoA (from .npy array IQ) | `music.py file` | Run MUSIC on a saved 2-D complex array |

Capture forms show the B210 ranges inline (freq 70e6–6e9, rate ≤ ~56e6, gain 0–76).
Required fields are marked `*`; blank optional fields fall back to the script's
own default.

## The Live Radar
A **semicircle** (−90°…+90°) PPI, because a single B210 is a 2-element array —
it resolves direction in the forward half-plane only. `0°` = boresight, radius =
distance. Each track shows bearing, distance, confidence, with a fading trail.

Three ways detections reach the radar:

| Source | How | Needs hardware? |
|---|---|---|
| **Simulator** | "Start sim" button on `/radar` | No |
| **Replay** | Radar Replay profile, or `radar_replay.py` | No |
| **Live** | Live Capture → Radar profile, or `live_radar_capture.py` | **Yes (B210)** |

From your own code, push a detection with one line:
```python
from emit import emit_detection
emit_detection(angle=bearing_deg, distance=dist_m, conf=0.9, id="drone1", label="DJI Mavic")
```
This appends a JSON line to `feed.jsonl`, which the server tails and draws.

## Phone ground truth (USRP Beacon Android app)
`~/AndroidStudioProjects/UsrpBeacon` turns an Android phone into a known BLE source:
it advertises manufacturer data `0xFFFF "USRP"+tag`, can blink on/off, and streams
its state + tape-measured MARK (true bearing/range) over UDP to port **5005**.

While the console runs it listens on 5005 itself (no need for `phone_truth_listener.py`,
and don't run both, they'd share the port) and logs every packet to `phone_logs/`.
On `/radar`:
- the decoder recognises the beacon's packets, so the phone shows up as a cyan
  **PHONE · USRP Beacon #tag** track (keyed on the tag, so Android's rotating address doesn't split it);
- the cyan diamond is the measured spot from the latest MARK, and the side card shows
  the USRP's direction/distance error against it;
- the bottom strip shows **what the USRP hears**: spectrum (average + peak-hold), per-antenna
  power, noise floor, and every burst over the last 20 s, with green bands where the
  phone says it was transmitting, plus ON vs OFF packet rates.

`live_radar_capture.py` sends that signal view to `127.0.0.1:5006` (~4x/s, dropped if the console isn't running).

## Files
- `app.py` — the server: profiles, subprocess runner, `/radar`, feed endpoints.
- `live_radar_capture.py` — B210 → MUSIC bearing + path-loss distance → radar (real SDR path).
- `radar_replay.py` — replay a captured feature CSV's distance track onto the radar.
- `emit.py` — `emit_detection()` helper; appends to `feed.jsonl`.
- `feed.jsonl` — runtime detection feed (created on use; safe to delete).
- `phone_logs/` — CSV of every phone telemetry packet, one file per console run.
- `phone_truth_listener.py` — standalone phone listener for when the console isn't running.

## Notes / honest caveats
- The capture and live-radar scripts need **UHD + a B210**. Without one they
  print a clear "UHD not available" message — they do **not** fake data. Use the
  simulator or replay for a no-hardware demo.
- **Distance is uncalibrated** — the path-loss model gives a *relative* range
  until calibrated against known distances. `radar_replay.py` has a
  `--distance-scale` just to make the track visible.
- **Bearing needs 2 coherent channels.** Single-channel captures have no
  direction information, so replayed captures sit at 0° (range-only). Real
  bearing comes only from a live `--channels 0,1` B210 capture.
- Launch with the venv interpreter (`~/.venvs/main/bin/python`) so subprocesses
  use the same environment.

## Live BLE tracking + calibration presets (B210)

1. Open http://127.0.0.1:8099/calibrate → **+ New calibration**: name, BLE channel, gain, antenna spacing.
2. Phone 1 m straight ahead → **Start measuring** (phase offset between the RX paths + 1 m reference power).
3. Phone ~45° to your right → **Measure** (detects mirrored left/right and stores a flip).
4. **Save & use** → preset saved in `cal_presets/<name>.json` and set active.
5. On /radar pick any saved preset and press **Start live**. Use a preset again from either page.

Packets are decoded in software (`ble_decode.py`: GFSK demod, access address, de-whitening, CRC-24), so every track is a real advertiser address + maker (e.g. `Apple Nearby C6:31`). Undecodable bursts (Classic BT hopping, Wi-Fi) are dropped unless `--anonymous`. Default channel is 39 (2.480 GHz), above most Wi-Fi.

### Automatic calibration from the phone (USRP Beacon app 1.2)
The app's calibration screen has a method chooser. Left on **Automatic** it uses the AR walk when the phone supports ARCore, otherwise the sweep; the two-position method is still there as **Manual**.

- **AR walk** — tap START AR, hold the phone between the two antennas and tap SET ORIGIN, then START WALK and walk a wide arc (1-3 m, 20-30 s). The phone streams its ARCore position; the console knows the true direction and range of every packet and fits the phase offset, the effective antenna spacing, left/right, the 1 m power and the path-loss exponent, and reports the per-packet bearing error.
- **Sweep** — carry the phone once in a half circle from in line with the antennas on the left to in line on the right. Offset = middle of the phase trace, spacing = half its swing, left/right = its direction. An incomplete sweep is refused.

Both record the phone's packets with `live_radar_capture.py --calibrate N --dump-packets FILE` and are fitted by `cal_fit.py` (`python3 cal_fit.py selftest` checks both fits on simulated data). The last recording stays in `cal_last_packets.jsonl` / `cal_last_poses.json`. The saved preset stores the fitted spacing (the ruler value is kept as `ruler_spacing_m`) and, for AR, `path_loss_exp`, which live tracking then uses.

### Reliability notes (what makes the phone show up every time)
- **No lost samples.** `b210_stream` sends 12-bit samples over USB (`--otw sc12`, the ADC's full resolution) and hands blocks to a writer thread through a ~2 s FIFO. With 16-bit samples this Mac overflowed ~9 times a second at 2 ch x 4 MS/s, and each overflow threw away up to a whole 20 ms block. If the console prints `LOSING SAMPLES`, something is wrong again (USB 2 port/cable, CPU starved).
- **Packets are found by their content, not their power.** `ble_decode.BleScanner` band-limits both antennas to the 1 MHz channel, combines them, and correlates with the preamble + access address. It needs ~8 dB less signal than the old power-threshold detector, and a packet that misses its CRC by a few bits still counts if it matches what that device sent a moment ago (shown as "below CRC limit").
- `python3 ble_bench.py synth` measures detection rate vs signal level without hardware; `python3 ble_bench.py file dump.fc32` compares old vs new on a recording.
- The status line warns when packets hit the ADC limit (device very close, lower the gain) and when processing lags behind the radio.

IQ comes from `native/b210_stream` (C++ UHD streamer; build line inside the .cpp) because this machine's UHD has no Python bindings.

## Checking DoA against the phone's GPS (USRP Beacon app 1.3)

1. **Full telemetry.** The app now sends GPS (position, altitude, accuracies, speed, course, fix time, satellites),
   compass (true + magnetic heading and its accuracy), pitch/roll, magnetic field strength and the barometer.
   The console keeps every field in `phone_logs/phone_<start>.jsonl` (the `.csv` next to it has the main columns).
2. **USRP position** (app card 4, outdoors):
   - *Record position*: the phone lies on the USRP; GPS is averaged for 30 s.
   - *Record facing*: the phone is held flat ~30 cm in front, top edge pointing out of the antennas' front (radar 0°).
     Refused when the phone is tilted, moving, or the magnetic field says metal is bending the compass.
   Saved in `usrp_site.json` (history in `usrp_site_history.jsonl`).
3. **Recording.** Every live run writes `recordings/<start>/doa.jsonl` (each packet: raw phase, bearing, power,
   distance; each radar update) and `meta.json` (the calibration used + the USRP position). `--no-record` turns it off.
4. **Compare + tune.** `python3 doa_compare.py` (newest recording; `--all` combines them) prints the USRP's error
   against GPS, fits offset / spacing / flip / the antennas' real facing / 1 m power / path-loss exponent, and with
   `--save-preset NAME --activate` saves it. `compare.csv` in the recording has every packet with its truth and errors.
   `python3 doa_compare.py selftest` checks the fit on a simulated walk.

**GPS limits:** phone GPS is off by 3–5 m outdoors (worse indoors), so the true direction is only known well
when the phone is far away: 4 m error at 10 m = 22°, at 40 m = 6°. Packets are only used when that uncertainty is
under 12° (`--max-truth-err`), so walk 15–60 m from the USRP in the open. For a room, use the AR walk calibration.

### Offline buffering (USRP Beacon app 1.4)

The app saves every message on the phone first (`files/telemetry/<start>_<sid>.jsonl`, each line
stamped with `sid` + `seq`). Whatever the laptop didn't confirm - phone or laptop without internet,
ngrok down, console stopped - is uploaded automatically to `<link>/phone/backlog` (UDP mode:
`http://<laptop-ip>:5080/phone/backlog`) once the laptop answers again. The console skips copies it
already has (also after a restart) and writes the rest into `phone_logs/` with `recv_time` set to when
the phone made them, so `doa_compare.py` uses them like live data. The app's "Sent to laptop" line
shows how many are still waiting. The laptop side needs nothing: its USRP recordings are local anyway.
