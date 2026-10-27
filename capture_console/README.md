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

## Files
- `app.py` — the server: profiles, subprocess runner, `/radar`, feed endpoints.
- `live_radar_capture.py` — B210 → MUSIC bearing + path-loss distance → radar (real SDR path).
- `radar_replay.py` — replay a captured feature CSV's distance track onto the radar.
- `emit.py` — `emit_detection()` helper; appends to `feed.jsonl`.
- `feed.jsonl` — runtime detection feed (created on use; safe to delete).

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
