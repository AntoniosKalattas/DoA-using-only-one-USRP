#!/usr/bin/env python3
"""
emit_detection() - one-line helper to push a drone detection to the Live Radar.

Use it from inside your capture loop (e.g. iq_size_capture_v3.py after you have
a MUSIC bearing + a path-loss distance for the current block):

    from capture_console.emit import emit_detection
    emit_detection(angle=bearing_deg, distance=dist_m, conf=0.9,
                   id="drone1", label="DJI Mavic")

It appends one JSON line to capture_console/feed.jsonl, which the running
Capture Console server tails and draws on /radar. No network dependency; if the
server is not running the line simply waits in the file.
"""

import json
import time
from pathlib import Path

FEED_FILE = Path(__file__).resolve().parent / "feed.jsonl"


def emit_detection(angle, distance, conf=1.0, id="drone", label=None,
                   source="live", feed_file=FEED_FILE, **extra):
    """Append one detection. angle in deg (-90..90), distance in meters."""
    rec = {
        "id": str(id),
        "label": str(label if label is not None else id),
        "angle": float(angle),
        "distance": float(distance),
        "conf": float(conf),
        "source": source,
        "t": time.time(),
    }
    rec.update(extra)          # optional summary fields (addr, bearing_std, rate, ...)
    with open(feed_file, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


if __name__ == "__main__":
    # quick manual test: emit a couple of detections
    emit_detection(angle=20, distance=50, conf=0.9, id="test1", label="DJI Mavic")
    emit_detection(angle=-30, distance=75, conf=0.8, id="test2", label="OcuSync")
    print(f"wrote 2 test detections to {FEED_FILE}")
