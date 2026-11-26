#!/usr/bin/env python3
"""Receive ground-truth telemetry from the USRP Beacon Android app.

The phone streams one JSON object per UDP packet (5 Hz + one extra on every MARK):
    t_ms, tag, adv_running, adv_on, interval_ms, tx_dbm,
    lat, lon, alt, acc_m, gps_age_s, heading_deg,
    mark, truth_bearing_deg, truth_range_m

Everything is logged to CSV with the laptop receive time (time.time()), which is
the clock to align against B210 burst timestamps.

If you give the array position (--array-lat/--array-lon/--array-boresight), the
GPS-derived bearing/range of the phone relative to array boresight is added too.
Only trust that beyond ~30-50 m: phone GPS is +-3-10 m.

    python capture_console/phone_truth_listener.py --out phone_truth.csv
"""
import argparse
import csv
import json
import math
import socket
import sys
import time

FIELDS = ["recv_time", "t_ms", "tag", "adv_running", "adv_on", "interval_ms", "tx_dbm",
          "lat", "lon", "alt", "acc_m", "gps_age_s", "heading_deg",
          "mark", "truth_bearing_deg", "truth_range_m", "gps_bearing_deg", "gps_range_m"]


def gps_bearing_range(lat0, lon0, lat1, lon1):
    """Initial great-circle bearing (deg from true N) and distance (m) from point 0 to 1."""
    r = 6371000.0
    p0, p1 = math.radians(lat0), math.radians(lat1)
    dl = math.radians(lon1 - lon0)
    y = math.sin(dl) * math.cos(p1)
    x = math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    return brg, 2 * r * math.asin(math.sqrt(a))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--out", default="phone_truth.csv")
    ap.add_argument("--array-lat", type=float)
    ap.add_argument("--array-lon", type=float)
    ap.add_argument("--array-boresight", type=float, default=0.0,
                    help="true-north bearing the array broadside faces (deg)")
    args = ap.parse_args()
    have_array = args.array_lat is not None and args.array_lon is not None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    print(f"listening on UDP :{args.port} -> {args.out}", file=sys.stderr)

    last_mark = None
    last_adv = None
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        while True:
            data, addr = sock.recvfrom(4096)
            now = time.time()
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            if msg.get("type") != "usrp_beacon":
                continue
            msg["recv_time"] = f"{now:.3f}"
            if have_array and "lat" in msg:
                brg, rng = gps_bearing_range(args.array_lat, args.array_lon, msg["lat"], msg["lon"])
                rel = (brg - args.array_boresight + 180.0) % 360.0 - 180.0
                msg["gps_bearing_deg"] = round(rel, 2)
                msg["gps_range_m"] = round(rng, 2)
            w.writerow(msg)
            fh.flush()

            if msg.get("mark") != last_mark:
                if last_mark is not None:
                    print(f"[{now:.3f}] MARK #{msg['mark']}  bearing={msg.get('truth_bearing_deg')}  "
                          f"range={msg.get('truth_range_m')} m  from {addr[0]}", flush=True)
                last_mark = msg.get("mark")
            if msg.get("adv_on") != last_adv:
                print(f"[{now:.3f}] beacon {'ON ' if msg.get('adv_on') else 'off'}  "
                      f"tag={msg.get('tag')} {msg.get('interval_ms')} ms {msg.get('tx_dbm')} dBm", flush=True)
                last_adv = msg.get("adv_on")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
