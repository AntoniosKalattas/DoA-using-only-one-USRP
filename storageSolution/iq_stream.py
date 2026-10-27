#!/usr/bin/env python3
"""
iq_stream.py - sender side. Streams raw IQ blocks over TCP to iq_server.py
running on a remote desktop, while you capture.

Use as a library from a capture loop:

    from iq_stream import IQStreamWriter
    w = IQStreamWriter("192.168.1.50", 9999,
                       meta={"state": "flight", "freq": 5.8e9, "rate": 10e6,
                             "gain": 40, "hw": "USRP B210"},
                       channels=2)
    for block in capture_blocks():      # block: np.ndarray, (N,) or (channels, N)
        w.write(block)
    w.close()

Or run it directly in synthetic mode to test the link without an SDR:

    python3 iq_stream.py --host 127.0.0.1 --port 9999 --synthetic \
                         --channels 2 --blocks 20 --block-size 100000

Wire protocol (framed over a single TCP connection):
    [4 bytes MAGIC "IQS1"]
    [4 bytes big-endian uint32 = header length]
    [header length bytes = UTF-8 JSON metadata]
    [raw IQ bytes ... until the sender closes the connection]
Multi-channel blocks are interleaved per sample (c0,c1,c0,c1,...), which is the
SigMF multi-channel convention the server records.
"""

import argparse
import json
import socket
import struct
import sys
import time

import numpy as np

MAGIC = b"IQS1"


class IQStreamWriter:
    def __init__(self, host, port, meta, channels=1, dtype="complex64", timeout=15):
        self.dtype = np.dtype(dtype)
        self.channels = int(channels)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        header = dict(meta)
        header.update({"dtype": dtype, "channels": self.channels})
        raw = json.dumps(header).encode("utf-8")
        self.sock.sendall(MAGIC + struct.pack(">I", len(raw)) + raw)
        self.bytes_sent = 0
        self.blocks_sent = 0

    def write(self, samples):
        """Send one IQ block. Accepts shape (N,) for single channel or
        (channels, N) for multi-channel (interleaved per sample on the wire)."""
        arr = np.ascontiguousarray(samples)
        if arr.ndim == 2:
            # (channels, N) -> [c0_0, c1_0, c0_1, c1_1, ...]
            arr = np.ascontiguousarray(arr.T).reshape(-1)
        arr = arr.astype(self.dtype, copy=False)
        data = arr.tobytes()
        self.sock.sendall(data)
        self.bytes_sent += len(data)
        self.blocks_sent += 1
        return len(data)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _synthetic(args):
    meta = {"state": args.state, "freq": args.freq, "rate": args.rate,
            "gain": 0, "hw": "synthetic", "description": "synthetic test stream"}
    print(f"[sender] connecting to {args.host}:{args.port} ...")
    rng = np.random.default_rng(0)
    t0 = time.time()
    with IQStreamWriter(args.host, args.port, meta, channels=args.channels) as w:
        for b in range(args.blocks):
            block = (rng.standard_normal((args.channels, args.block_size))
                     + 1j * rng.standard_normal((args.channels, args.block_size)))
            w.write(block.astype(np.complex64))
            if (b + 1) % 5 == 0:
                mb = w.bytes_sent / 1e6
                print(f"[sender] block {b+1}/{args.blocks}  sent {mb:.1f} MB")
    dt = time.time() - t0
    print(f"[sender] done: {w.blocks_sent} blocks, {w.bytes_sent/1e6:.1f} MB in {dt:.1f}s "
          f"({w.bytes_sent/1e6/max(dt,1e-9):.1f} MB/s)")


def main():
    ap = argparse.ArgumentParser(description="Stream raw IQ to a remote iq_server.py.")
    ap.add_argument("--host", required=True, help="Remote desktop IP/hostname")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--synthetic", action="store_true", help="Send random IQ to test the link")
    ap.add_argument("--channels", type=int, default=1)
    ap.add_argument("--blocks", type=int, default=20)
    ap.add_argument("--block-size", type=int, default=100000, help="Samples per channel per block")
    ap.add_argument("--rate", type=float, default=10e6)
    ap.add_argument("--freq", type=float, default=5.8e9)
    ap.add_argument("--state", default="synthetic")
    args = ap.parse_args()
    if args.synthetic:
        _synthetic(args)
    else:
        print("Nothing to do. Use --synthetic to test, or import IQStreamWriter in your capture.")
        sys.exit(1)


if __name__ == "__main__":
    main()
