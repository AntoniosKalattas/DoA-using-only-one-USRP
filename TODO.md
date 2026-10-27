# TODO — next steps (from supervisor meeting, 2026-10-21)

Action items agreed in the meeting. Scope: detection/classification + DoA tracking + console graphs.

## 1. Validate the algorithms on a Bluetooth dataset
- Run the existing detection / MUSIC pipeline against a **Bluetooth RF dataset** to see how it behaves on non-drone 2.4/5.8 GHz signals.
- Purpose: check robustness and false-positive behaviour against a known interferer, not to prove drone detection on its own.
- Open: pick the exact dataset (public vs. locally captured) and get it on disk.

## 2. Classify / decide "is it even a drone?"
- Build a classifier that answers **drone vs. not-drone** (and ideally drone vs. Bluetooth/WiFi/noise), not just the flight-state model we already have.
- Reuse the existing feature stack (`DroneTrafficAnalysis/features/extractor.py`) and ML pipeline (`ml/train.py`).
- We already have `no_drone_*` and drone-state CSVs locally to start the binary label; Bluetooth data (item 1) extends it to a 3-class "drone / BT / background".

## 3. Multi-target tracking (track multiple drones)
- Extend `music.py` to estimate **multiple sources** (DoA for >1 drone at once).
- Add a tracker: associate detections across frames (nearest-neighbour / gating), keep per-track IDs, smooth bearing/distance over time.
- Feed tracked targets to the live radar in the capture console.

## 4. Capture console — add Doppler + signal graphs
- Add a **Doppler graph** (frequency shift / velocity over time) to the console.
- Add **signal graphs** (power / spectrum / time-domain magnitude) alongside the radar.
- Should update live while capturing (and work on replay of captured data for no-hardware demo).

---
_Status: not started — captured here as the agreed plan._
