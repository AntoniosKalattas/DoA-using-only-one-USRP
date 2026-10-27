# storageSolution — stream raw IQ to a remote desktop while capturing

A small client/server system to record the B210's raw IQ onto another machine
("remote desktop") **live, as you capture** — so you aren't limited by the
capture laptop's disk, and you keep the full-rate samples (which the existing
feature-CSV datasets throw away).

Stdlib + numpy only. Output is **SigMF 1.0.0** compatible.

```
capture machine (B210)                 remote desktop (storage)
┌───────────────────────┐   TCP        ┌────────────────────────────┐
│ capture_to_remote.py   │ ───────────▶ │ iq_server.py               │
│   UHD recv_num_samps   │  raw IQ      │   writes <name>.sigmf-data │
│   IQStreamWriter       │  stream      │          <name>.sigmf-meta │
└───────────────────────┘              └────────────────────────────┘
```

## Files
- `iq_server.py` — **run on the remote desktop.** Listens on TCP, writes each
  incoming stream to a SigMF pair. One thread per connection.
- `iq_stream.py` — sender library (`IQStreamWriter`) + a `--synthetic` test mode.
- `capture_to_remote.py` — **run on the capture machine.** B210 → stream live.

## Quick start

1. On the **remote desktop** (storage):
   ```bash
   python3 iq_server.py --outdir ~/iq_store --port 9999
   ```
   Note its LAN IP (e.g. `192.168.1.50`) and open port 9999 in its firewall.

2. On the **capture machine** (B210 attached):
   ```bash
   python3 capture_to_remote.py --host 192.168.1.50 --port 9999 \
       --freq 5.8e9 --rate 10e6 --gain 40 --channels 0,1 \
       --state flight --duration 30
   ```
   Each capture lands on the desktop as `flight_<timestamp>.sigmf-data` +
   `.sigmf-meta`.

3. Test the link **without an SDR** (any two machines, or loopback):
   ```bash
   # remote:
   python3 iq_server.py --outdir ./iq_store
   # sender:
   python3 iq_stream.py --host <remote-ip> --synthetic --channels 2 --blocks 20
   ```

## Reading the data back
```python
import json, numpy as np
meta = json.load(open("flight_20261019_180315.sigmf-meta"))
nch  = meta["global"]["core:num_channels"]
raw  = np.fromfile("flight_20261019_180315.sigmf-data", dtype=np.complex64)
iq   = raw.reshape(-1, nch).T          # (channels, N)  — c0,c1 interleaved per sample
```
For a 2-channel capture, `iq` is exactly the `(channels, N)` array MUSIC needs:
`iq.T` → `(N, channels)` feeds `music.py`'s `find_angle`.

## ⚠️ Bandwidth is the real constraint
Raw IQ is big. At complex64 (8 bytes/sample):

| Sample rate | 1 channel | 2 channels |
|------------:|----------:|-----------:|
| 5 MS/s      | 40 MB/s   | 80 MB/s    |
| 10 MS/s     | 80 MB/s   | 160 MB/s   |
| 20 MS/s     | 160 MB/s  | 320 MB/s   |

- **1 GbE tops out ~110 MB/s.** For 10 MS/s × 2ch (160 MB/s) you need **10 GbE**,
  or lower the rate, or capture one channel.
- The remote **disk** must sustain the write rate too (NVMe SSD for high rates).
- If the link can't keep up, TCP back-pressures the capture loop → the USRP
  overflows (`O` / overruns). Drop the sample rate until it's stable.
- To halve the bandwidth, capture in `sc16` (int16 I/Q) instead of `cf32`. This
  version streams cf32; switching to sc16 is a small change if you need it.

## Alternatives (when "live streaming" isn't required)
- **Capture local, then `rsync`/`scp`** the files to the desktop afterwards —
  simplest and most robust; use when you don't need it live.
- **SSHFS / NFS mount** the desktop's directory on the capture machine and let
  the existing capture scripts write straight to it — no code change, but a
  network stall becomes a capture stall (same back-pressure caveat).

This socket solution is the right choice specifically for **live** streaming
with explicit metadata and SigMF output.
