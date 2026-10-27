#!/usr/bin/env python3
"""
iq_server.py - receiver side. Runs on the REMOTE DESKTOP. Listens on TCP,
accepts raw IQ streams from iq_stream.py, and writes each capture to disk as a
SigMF pair (<name>.sigmf-data + <name>.sigmf-meta).

Run on the remote desktop:
    python3 iq_server.py --outdir ./iq_store --port 9999

Then on the capture machine point iq_stream.py / capture_to_remote.py at this
host's IP. Each incoming connection becomes one capture file. Multiple captures
(sequential or concurrent) are handled - one thread per connection.

Output is SigMF 1.0.0 compatible:
  - .sigmf-data : raw interleaved float32 I/Q (cf32_le); multi-channel is
                  interleaved per sample (c0,c1,c0,c1,...).
  - .sigmf-meta : JSON with sample_rate, frequency, num_channels, datetime, etc.
"""

import argparse
import json
import os
import socket
import struct
import threading
import time
from datetime import datetime, timezone

import numpy as np

MAGIC = b"IQS1"
CHUNK = 1 << 20  # 1 MiB disk writes


def recvall(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            return None
        buf.extend(part)
    return bytes(buf)


def handle(conn, addr, outdir):
    try:
        magic = recvall(conn, 4)
        if magic != MAGIC:
            print(f"[server] {addr}: bad magic, dropping")
            return
        (hlen,) = struct.unpack(">I", recvall(conn, 4))
        meta = json.loads(recvall(conn, hlen).decode("utf-8"))
    except (OSError, ValueError, TypeError) as e:
        print(f"[server] {addr}: header error: {e}")
        conn.close()
        return

    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    state = str(meta.get("state", "capture")).replace("/", "_")
    base = os.path.join(outdir, f"{state}_{ts}")
    data_path = base + ".sigmf-data"
    print(f"[server] {addr} -> {data_path}")
    print(f"[server]   meta: freq={meta.get('freq')} rate={meta.get('rate')} "
          f"gain={meta.get('gain')} channels={meta.get('channels')} dtype={meta.get('dtype')}")

    total = 0
    t0 = time.time()
    last = t0
    with open(data_path, "wb") as f:
        while True:
            try:
                chunk = conn.recv(CHUNK)
            except OSError:
                break
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            now = time.time()
            if now - last >= 1.0:
                mb = total / 1e6
                print(f"[server]   {state}: {mb:.1f} MB  ({mb/(now-t0+1e-9):.1f} MB/s)")
                last = now
    conn.close()
    dur = time.time() - t0

    dtype = meta.get("dtype", "complex64")
    nch = int(meta.get("channels", 1) or 1)
    itemsize = np.dtype(dtype).itemsize
    n_complex = total // itemsize if itemsize else 0
    n_per_chan = n_complex // max(1, nch)

    sigmf = {
        "global": {
            "core:datatype": "cf32_le",
            "core:sample_rate": meta.get("rate"),
            "core:version": "1.0.0",
            "core:num_channels": nch,
            "core:description": meta.get("description", ""),
            "core:hw": meta.get("hw", ""),
        },
        "captures": [{
            "core:sample_start": 0,
            "core:frequency": meta.get("freq"),
            "core:datetime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }],
        "annotations": [],
    }
    # carry through any extra sender metadata (gain, device args, etc.)
    for k, v in meta.items():
        if k not in ("dtype", "channels", "rate", "freq", "hw", "description", "state"):
            sigmf["global"][f"capture:{k}"] = v

    with open(base + ".sigmf-meta", "w") as m:
        json.dump(sigmf, m, indent=2)

    print(f"[server] DONE {state}: {total/1e6:.1f} MB, {n_per_chan:,} samples/chan, "
          f"{dur:.1f}s -> {base}.sigmf-data/.sigmf-meta")


def main():
    ap = argparse.ArgumentParser(description="Receive raw IQ streams and store as SigMF.")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default all interfaces)")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--outdir", default="./iq_store", help="Where to write captures")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(8)
    print("=" * 60)
    print("  IQ Storage Server")
    print(f"  Listening : {args.host}:{args.port}")
    print(f"  Output    : {os.path.abspath(args.outdir)}")
    print("  Ctrl+C to quit")
    print("=" * 60)
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle, args=(conn, addr, args.outdir), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[server] bye")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
