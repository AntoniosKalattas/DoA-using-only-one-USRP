#!/usr/bin/env python3
"""
radar_replay.py - replay a captured feature CSV through the Live Radar feed,
at real-time pace, so you can watch the capture "live" on /radar.

What it uses from the capture:
  - distance : the REAL per-window distance your pipeline estimated
               (column est_distance_m by default).
  - confidence: distance_confidence column.
  - bearing  : NOT available - these captures are single-channel, so there is
               no direction-of-arrival. Bearing is fixed at --angle (default 0,
               boresight) and the track is range-only. A 2-channel B210 capture
               plus MUSIC is what would fill in a real bearing.

Pacing: by the capture window (window_ms column) divided by --speed, because the
row timestamps contain large real gaps (idle periods) that would otherwise stall
playback.

Run directly:
    python3 capture_console/radar_replay.py --csv <file.csv> --speed 5
or pick "Radar Replay (captured CSV)" in the Capture Console.
"""

import argparse
import csv
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emit import emit_detection  # noqa: E402


def is_num(x):
    try:
        v = float(x)
        return not math.isnan(v)
    except (TypeError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser(description="Replay a captured CSV onto the Live Radar.")
    ap.add_argument("--csv", required=True, help="Captured feature CSV")
    ap.add_argument("--speed", type=float, default=5.0, help="Playback speed multiplier (default 5x)")
    ap.add_argument("--distance-col", default="est_distance_m", help="Distance column")
    ap.add_argument("--conf-col", default="distance_confidence", help="Confidence column")
    ap.add_argument("--distance-scale", type=float, default=1.0,
                    help="Multiply distance for visibility (data is uncalibrated; default 1)")
    ap.add_argument("--angle", type=float, default=0.0,
                    help="Fixed bearing in deg (default 0 = boresight; no DoA in single-channel data)")
    ap.add_argument("--label", default=None, help="Track label (default: state column)")
    ap.add_argument("--id", default="replay", help="Track id")
    ap.add_argument("--max-rows", type=int, default=0, help="Stop after N rows (0 = all)")
    args = ap.parse_args()

    # Resolve the CSV path: as given, or relative to the project root (so a
    # path relative to …/Thesis works even though we run from capture_console/).
    csv_path = args.csv
    if not os.path.exists(csv_path):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = os.path.join(root, args.csv)
        if os.path.exists(cand):
            csv_path = cand
    if not os.path.exists(csv_path):
        print(f"[replay] ERROR: file not found: {args.csv}")
        return
    args.csv = csv_path

    with open(args.csv) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("[replay] ERROR: no rows")
        return

    dcol, ccol = args.distance_col, args.conf_col
    if dcol not in rows[0]:
        print(f"[replay] ERROR: distance column '{dcol}' not in CSV. "
              f"Available: {', '.join(list(rows[0].keys())[:12])} ...")
        return

    print(f"[replay] {os.path.basename(args.csv)}  rows={len(rows)}  "
          f"speed={args.speed}x  distance x{args.distance_scale}")
    print(f"[replay] bearing fixed at {args.angle} deg (range-only: single-channel capture, no DoA)")
    print(f"[replay] distance column = {dcol}")
    print()

    emitted = skipped = 0
    for n, row in enumerate(rows):
        if args.max_rows and n >= args.max_rows:
            break

        dval = row.get(dcol, "")
        if not is_num(dval):
            skipped += 1
        else:
            dist = float(dval) * args.distance_scale
            conf = float(row[ccol]) if ccol in row and is_num(row[ccol]) else 1.0
            label = args.label or row.get("state", "drone")
            emit_detection(angle=args.angle, distance=dist, conf=conf,
                           id=args.id, label=label, source="replay")
            emitted += 1
            if emitted % 50 == 0:
                print(f"[replay] row {n}/{len(rows)}  dist={dist:.2f} m  conf={conf:.2f}")

        # pace by the capture window
        try:
            win_ms = float(row.get("window_ms", 10))
        except (TypeError, ValueError):
            win_ms = 10.0
        time.sleep(max(0.0, (win_ms / 1000.0) / max(0.01, args.speed)))

    print()
    print(f"[replay] done. emitted={emitted}  skipped(nan)={skipped}")


if __name__ == "__main__":
    main()
