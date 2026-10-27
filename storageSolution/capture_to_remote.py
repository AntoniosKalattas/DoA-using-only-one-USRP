#!/usr/bin/env python3
"""
capture_to_remote.py - capture raw IQ from the USRP B210 and stream it LIVE to
a remote desktop (iq_server.py), while capturing.

This is the real-hardware path: it needs the UHD Python API and a B210. With no
SDR it prints a clear message and exits (use iq_stream.py --synthetic to test
the link without hardware).

Capture --channels 0,1 to store 2-channel coherent IQ - that is the recording
you need to later run MUSIC/DoA on (the existing single-channel datasets cannot).

Example (on the capture machine):
    python3 capture_to_remote.py --host 192.168.1.50 --port 9999 \
        --freq 5.8e9 --rate 10e6 --gain 40 --channels 0,1 \
        --state flight --duration 30
"""

import argparse
import sys
import time

import numpy as np

from iq_stream import IQStreamWriter


def main():
    ap = argparse.ArgumentParser(description="B210 capture -> stream raw IQ to remote storage.")
    ap.add_argument("--host", required=True, help="Remote desktop IP running iq_server.py")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--freq", type=float, default=5.8e9, help="Center frequency Hz")
    ap.add_argument("--rate", type=float, default=10e6, help="Sample rate Hz (B210 max ~56e6)")
    ap.add_argument("--gain", type=float, default=40, help="RX gain dB (B210 0..76)")
    ap.add_argument("--window", type=float, default=50.0, help="Block length ms (default 50)")
    ap.add_argument("--duration", type=float, default=0.0, help="Seconds (0 = until Ctrl+C)")
    ap.add_argument("--channels", default="0", help="RX channels, e.g. '0' or '0,1' for DoA")
    ap.add_argument("--device-args", default="", help="UHD device args, e.g. type=b200")
    ap.add_argument("--state", default="capture", help="Label for the stored file")
    args = ap.parse_args()

    try:
        import uhd
    except ImportError:
        print("ERROR: UHD Python API not available on this machine.")
        print("This is the live B210 path. To test the network link without an SDR, run:")
        print("  python3 iq_stream.py --host <remote> --synthetic --channels 2")
        return False

    channels = [int(c) for c in str(args.channels).split(",") if c.strip() != ""]
    n_chan = len(channels)
    samples_per_block = int(args.rate * args.window / 1000)

    # bandwidth sanity note
    mbps = args.rate * 8 * n_chan / 1e6  # complex64 = 8 bytes/sample
    print("=" * 60)
    print("CAPTURE -> REMOTE STORAGE (USRP B210)")
    print("=" * 60)
    print(f"Remote:   {args.host}:{args.port}")
    print(f"Freq:     {args.freq/1e9:.3f} GHz   Rate: {args.rate/1e6:.2f} MHz   Gain: {args.gain} dB")
    print(f"Channels: {channels}   Block: {args.window} ms ({samples_per_block:,} samp/chan)")
    print(f"Data rate ~{mbps:.0f} MB/s (cf32) - make sure the network/disk can keep up")
    print("=" * 60)

    usrp = uhd.usrp.MultiUSRP(args.device_args)

    meta = {"state": args.state, "freq": args.freq, "rate": args.rate,
            "gain": args.gain, "hw": "USRP B210", "device_args": args.device_args,
            "description": f"B210 live capture, channels={channels}"}

    writer = IQStreamWriter(args.host, args.port, meta, channels=n_chan, dtype="complex64")
    print("Streaming... Press Ctrl+C to stop.\n")
    start = time.time()
    blocks = 0
    try:
        while True:
            if args.duration and (time.time() - start) >= args.duration:
                break
            samples = usrp.recv_num_samps(
                num_samps=samples_per_block, freq=args.freq, rate=args.rate,
                channels=channels, gain=args.gain)
            samples = np.atleast_2d(samples)          # (n_chan, N)
            writer.write(samples if n_chan > 1 else samples[0])
            blocks += 1
            if blocks % 10 == 0:
                mb = writer.bytes_sent / 1e6
                print(f"[capture] {blocks} blocks  {mb:.1f} MB sent "
                      f"({mb/max(time.time()-start,1e-9):.1f} MB/s)")
    except KeyboardInterrupt:
        print("\n[capture] stopped by user")
    finally:
        writer.close()
    print(f"[capture] done: {blocks} blocks, {writer.bytes_sent/1e6:.1f} MB streamed to {args.host}")
    return True


if __name__ == "__main__":
    main()
