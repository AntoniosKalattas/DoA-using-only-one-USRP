#!/usr/bin/env python3
"""
live_radar_capture.py - LIVE drone tracking on the B210, straight to /radar.

Per IQ block it:
  1. pulls both coherent RX channels from the USRP B210,
  2. runs MUSIC on the 2-channel block            -> bearing (deg),
  3. runs the log-distance path-loss model on ch0 -> distance (m),
  4. emits a detection to the Live Radar feed.

This is the real-hardware path. It needs the UHD Python API and a B210 with two
RX antennas (A:A + A:B, same LO = phase coherent). With no SDR it prints a clear
message and exits - it does not fake data (use radar_replay.py for captured CSVs
or the radar's "Start sim" button for a no-hardware demo).

Reuses the distance model from iq_size_capture_v3.py and find_angle() from
music.py so bearings/distances are consistent with the rest of the pipeline.

Run:
    python3 capture_console/live_radar_capture.py --freq 5.8e9 --rate 10e6 --gain 40
or pick "Live Capture -> Radar (B210)" in the Capture Console, then open /radar.
"""

import argparse
import contextlib
import io
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "DroneTrafficAnalysis", "DroneTrafficAnalysis", "capture"))

from emit import emit_detection            # noqa: E402
from music import find_angle, B210_WAVELENGTH_M, B210_SPACING_M  # noqa: E402

# Log-distance path-loss defaults (match iq_size_capture_v3.py)
DEFAULT_PATH_LOSS_EXP = 2.2
DEFAULT_REF_DIST_M = 1.0


def power_db(block):
    """Mean power in dB of one complex channel."""
    p = np.abs(block) ** 2
    return 10.0 * np.log10(np.mean(p) + 1e-20)


def main():
    ap = argparse.ArgumentParser(description="Live B210 capture -> MUSIC bearing + distance -> Live Radar.")
    ap.add_argument("--freq", type=float, default=5.8e9, help="Center frequency Hz (default 5.8e9)")
    ap.add_argument("--rate", type=float, default=10e6, help="Sample rate Hz (default 10e6)")
    ap.add_argument("--gain", type=float, default=40, help="RX gain dB (default 40)")
    ap.add_argument("--window", type=float, default=10.0, help="Block length ms (default 10)")
    ap.add_argument("--duration", type=float, default=0.0, help="Seconds to run (0 = until Ctrl+C)")
    ap.add_argument("--channels", default="0,1", help="RX channels (default '0,1' for 2-element DoA)")
    ap.add_argument("--device-args", default="", help="UHD device args, e.g. 'type=b200'")
    ap.add_argument("--path-loss-exp", type=float, default=DEFAULT_PATH_LOSS_EXP, help="Path-loss exponent n")
    ap.add_argument("--ref-dist", type=float, default=DEFAULT_REF_DIST_M, help="Reference distance m")
    ap.add_argument("--wavelength", type=float, default=B210_WAVELENGTH_M, help="MUSIC wavelength m")
    ap.add_argument("--spacing", type=float, default=B210_SPACING_M, help="MUSIC element spacing m")
    ap.add_argument("--min-power-db", type=float, default=-75.0,
                    help="Only emit a detection when block power exceeds this (noise gate)")
    ap.add_argument("--id", default="live", help="Track id")
    ap.add_argument("--label", default="drone", help="Track label")
    args = ap.parse_args()

    try:
        import uhd
    except ImportError:
        print("ERROR: UHD Python API not available on this machine.")
        print("This is the LIVE hardware path and needs a USRP B210 + UHD.")
        print("No SDR here? Use radar_replay.py on a captured CSV, or the radar's 'Start sim'.")
        return False

    channels = [int(c) for c in str(args.channels).split(",") if c.strip() != ""]
    n_chan = len(channels)
    samples_per_window = int(args.rate * args.window / 1000)

    print("=" * 60)
    print("LIVE RADAR CAPTURE (USRP B210)")
    print("=" * 60)
    print(f"Freq:     {args.freq/1e9:.3f} GHz")
    print(f"Rate:     {args.rate/1e6:.2f} MHz")
    print(f"Gain:     {args.gain} dB")
    print(f"Window:   {args.window} ms ({samples_per_window:,} samples)")
    print(f"Channels: {channels}  ->  {'2-ch MUSIC bearing' if n_chan >= 2 else 'range-only (no DoA)'}")
    print(f"Geometry: d/lambda = {args.spacing/args.wavelength:.3f}")
    print(f"Noise gate: {args.min_power_db} dB")
    print("=" * 60)

    usrp = uhd.usrp.MultiUSRP(args.device_args)

    ref_power_db = None       # auto-set from first valid block (path-loss reference)
    start = time.time()
    emitted = 0

    print("Streaming... Press Ctrl+C to stop.\n")
    try:
        while True:
            if args.duration and (time.time() - start) >= args.duration:
                break

            samples = usrp.recv_num_samps(
                num_samps=samples_per_window, freq=args.freq, rate=args.rate,
                channels=channels, gain=args.gain)
            samples = np.atleast_2d(samples)          # (n_chan, N)

            p_db = power_db(samples[0])

            # noise gate: nothing above threshold -> no drone this block
            if p_db < args.min_power_db:
                continue

            # distance via log-distance path-loss (auto-reference on first block)
            if ref_power_db is None:
                ref_power_db = p_db
            path_loss_db = ref_power_db - p_db
            dist_m = args.ref_dist * (10 ** (path_loss_db / (10.0 * args.path_loss_exp)))

            # bearing via MUSIC when we have >= 2 coherent channels
            if samples.shape[0] >= 2:
                X = samples.T                          # (N, n_chan)
                with contextlib.redirect_stdout(io.StringIO()):
                    bearing = float(find_angle(X, wave_length=args.wavelength,
                                               distance_d=args.spacing, number_of_drones=1))
            else:
                bearing = 0.0                          # single channel: no DoA

            conf = float(np.clip((p_db - args.min_power_db) / 30.0, 0.0, 1.0))
            emit_detection(angle=bearing, distance=round(dist_m, 2), conf=round(conf, 2),
                           id=args.id, label=args.label, source="live")
            emitted += 1
            if emitted % 20 == 0:
                print(f"[live] p={p_db:6.1f} dB  bearing={bearing:+5.1f} deg  "
                      f"dist={dist_m:7.2f} m  conf={conf:.2f}")

    except KeyboardInterrupt:
        print("\n[live] stopped by user")

    print(f"[live] done. emitted={emitted} detections")
    return True


if __name__ == "__main__":
    main()
