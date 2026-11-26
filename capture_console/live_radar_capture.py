#!/usr/bin/env python3
"""
live_radar_capture.py - LIVE tracking on the USRP B210, straight to /radar.

Works for bursty emitters such as Bluetooth LE advertising (default: BLE adv
channel 38 @ 2.426 GHz) as well as continuous ones (drone video at 5.8 GHz).

Pipeline (per received block):
  1. stream both coherent RX channels (shared LO) continuously via native/b210_stream
     (C++ UHD; this machine's UHD has no Python bindings). The streamer and a reader
     thread here buffer ~2 s each, so a busy moment in Python no longer drops samples,
  2. find BLE packets with ble_decode.BleScanner: both antennas band-limited to the
     1 MHz channel and combined, packets located by correlating with the preamble +
     access address (no power threshold), then CRC-checked. Packets that miss the CRC
     by a few bits are still accepted if they match what that device just sent.
     (The old power detector - smoothed power > floor + --snr-db - is kept only for
     the signal view and for --anonymous bursts.)
  3. per packet: remove the calibrated inter-channel phase offset and run MUSIC
     (find_angle from music.py, M=2, 1 source)           -> bearing (deg),
  4. log-distance path loss vs the 1 m reference power   -> distance (m),
  5. group bursts into per-device tracks by (power, raw inter-channel phase),
     emit one smoothed detection per track to the Live Radar feed.

Calibration presets (capture_console/cal_presets/<name>.json):
  Easiest: the /calibrate page in the Capture Console (step-by-step wizard).
  CLI: put a BLE device 1 m in front of the array, broadside (0 deg), and run
      --calibrate 10 --save-preset desk
  It measures the RX-path phase offset and the 1 m reference power. Live runs use
  --preset NAME (blank = the active preset, "none" = uncalibrated). A preset also
  carries freq/gain/spacing and a 'flip' flag (set by the wizard's direction check).

Run:
    python3 capture_console/live_radar_capture.py --calibrate 10 --save-preset desk
    python3 capture_console/live_radar_capture.py --preset desk   # live -> /radar
"""

import argparse
import contextlib
import io
import json
import os
import signal
import socket
import queue
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "DroneTrafficAnalysis", "DroneTrafficAnalysis", "capture"))

from emit import emit_detection            # noqa: E402
from music import (find_angle, SPEED_OF_LIGHT, getEigenValues, steeringVector,  # noqa: E402
                   evaluate_spectrum, get_peak_detection)
from collections import deque  # noqa: E402
from ble_decode import BleScanner, describe, channel_for_freq  # noqa: E402

STREAMER = os.path.join(HERE, "native", "b210_stream")
PRESET_DIR = os.path.join(HERE, "cal_presets")
ACTIVE_FILE = os.path.join(PRESET_DIR, "_active.json")
DEFAULT_PATH_LOSS_EXP = 2.2
DEFAULT_FREQ, DEFAULT_GAIN = 2.480e9, 50.0   # ch39 sits above most Wi-Fi
SIGNAL_ADDR = ("127.0.0.1", 5006)            # Capture Console listens here for the signal view
SPEC_NFFT = 128
RECORD_DIR = os.path.join(HERE, "recordings")
LIVE_CTL = os.path.join(HERE, "live_ctl.json")          # radar buttons write commands here while live
LIVE_CTL_RESULT = os.path.join(HERE, "live_ctl_result.json")


BLE_ADV_FREQ = {37: 2.402e9, 38: 2.426e9, 39: 2.480e9}


def _pick_track(tracks, match):
    cands = [t for t in tracks.values() if match.lower() in (t.id + " " + t.label).lower() and len(t.hist) >= 1]
    return max(cands, key=lambda t: len(t.hist)) if cands else None


def chan_power(hist, since):
    """Power over the advertising channels: median per channel, then the mean of those medians.
    A reflection that cancels the signal on one channel rarely does it on all three, so this
    takes most of the fading out. Returns (power_db, spread_between_channels_db, n, per_channel)."""
    by = {}
    for h in hist:
        if h[0] >= since:
            by.setdefault(h[4], []).append(h[2])
    if not by:
        return None, 0.0, 0, {}
    med = {c: float(np.median(v)) for c, v in by.items()}
    vals = np.array(list(med.values()))
    return float(vals.mean()), float(vals.std()), sum(len(v) for v in by.values()), med


def gain_at(table, angle):
    """Direction correction (dB) at this bearing: linear between the measured points, flat outside."""
    if not table:
        return 0.0
    pts = sorted(table)
    a = [p[0] for p in pts]
    g = [p[1] for p in pts]
    return float(np.interp(angle, a, g))


def dist_from_power(p_db, ref_p, n_exp):
    return 10 ** ((ref_p - p_db) / (10.0 * n_exp))


def live_range(tracks, match, dist_m, n_exp, ref_p, table, have_ref):
    """'Set distance': the phone is dist_m away NOW, at the bearing the radar shows.
    First press (no reference yet): sets the 1 m reference power from it.
    Later presses: store a direction correction for this bearing (how much stronger/weaker this
    direction is than the reference predicts), replacing any point within 8 deg."""
    t = _pick_track(tracks, match)
    if t is None:
        return None, f"no packets from {match} in the last few seconds - is the beacon on?"
    p, ch_spread, n, per = chan_power(t.hist, t.last - 5.0)
    if n < 5:
        return None, f"only {n} packets from {t.id} in the last 5 s - wait a moment and press again"
    angle = getattr(t, "shown", None)
    if angle is None:
        angle = t.bearing
    pw = [h[2] for h in t.hist if h[0] >= t.last - 5.0]
    out = {"power_db": round(p, 1), "dist_m": dist_m, "n": n, "device": t.id, "angle": round(float(angle), 1),
           "power_spread_db": round(float(np.percentile(pw, 80) - np.percentile(pw, 20)), 1),
           "channel_spread_db": round(ch_spread, 1), "per_channel_db": {str(c): round(v, 1) for c, v in per.items()}}
    if not have_ref:
        ref_p = p + 10 * n_exp * np.log10(dist_m)
        table = [[round(float(angle), 1), 0.0]]
        out["mode"] = "reference"
    else:
        g = p - (ref_p - 10 * n_exp * np.log10(dist_m))
        table = [q for q in table if abs(q[0] - angle) > 8.0] + [[round(float(angle), 1), round(float(g), 2)]]
        out["mode"] = "direction"
        out["gain_db"] = round(float(g), 1)
    out["ref_power_db"] = round(float(ref_p), 2)
    out["gain_table"] = sorted(table)
    return out, None


def map_angle(points, a):
    """Angle teach points: piecewise-linear map from the measured bearing to the true one.
    points = [[measured, true], ...]; (0, 0) is implied (Set middle). Straight-line
    extension past the outermost points, so nothing gets clamped."""
    if not points or a is None or not np.isfinite(a):
        return a
    pts = sorted([list(map(float, q)) for q in points], key=lambda q: q[0])
    if not any(abs(q[1]) < 1.0 for q in pts):
        pts = sorted(pts + [[0.0, 0.0]], key=lambda q: q[0])
    m = np.array([q[0] for q in pts])
    t = np.array([q[1] for q in pts])
    if len(pts) == 1:
        return a
    if a <= m[0]:
        k = (t[1] - t[0]) / max(1e-6, m[1] - m[0])
        out = t[0] + k * (a - m[0])
    elif a >= m[-1]:
        k = (t[-1] - t[-2]) / max(1e-6, m[-1] - m[-2])
        out = t[-1] + k * (a - m[-1])
    else:
        out = float(np.interp(a, m, t))
    return float(np.clip(out, -90.0, 90.0))


def angle_weight(points, a):
    """How much to trust a channel's corrected bearing: 1/slope^2 of its teach map (a channel that
    barely moves between taught points has its noise blown up by the slope). A channel with ANY taught
    point on the wrong side, or too close to the middle, gets 0 everywhere: a reflection owns it."""
    if not points or a is None or not np.isfinite(a):
        return 1.0
    pts = [list(map(float, q)) for q in points]
    if any(abs(t) >= 1.0 and (np.sign(m) != np.sign(t) or abs(m) < 3.0) for m, t in pts):
        return 0.0
    pts = sorted(pts + [[0.0, 0.0]], key=lambda q: q[0])
    m = np.array([q[0] for q in pts])
    t = np.array([q[1] for q in pts])
    if np.any(np.diff(m) <= 0) or np.any(np.diff(t) <= 0):
        return 0.0
    i = int(np.clip(np.searchsorted(m, a) - 1, 0, len(m) - 2))
    k = (t[i + 1] - t[i]) / (m[i + 1] - m[i])
    return float(1.0 / max(1.0, abs(k)) ** 2)


def wmedian(v, w):
    v, w = np.asarray(v, float), np.asarray(w, float)
    if w.sum() <= 0:
        return float(np.median(v))
    o = np.argsort(v)
    c = np.cumsum(w[o])
    return float(v[o][np.searchsorted(c, c[-1] / 2.0)])


def live_angle(tracks, match, true_deg, table, chans):
    """'Set angle': the phone is at true_deg NOW. For each channel, the median of its recent
    (uncorrected) bearings becomes a teach point measured -> true_deg."""
    t = _pick_track(tracks, match)
    if t is None:
        return None, f"no packets from {match} in the last few seconds - is the beacon on?"
    new = {k: [q for q in v if abs(q[1] - true_deg) > 5.0] for k, v in (table or {}).items()}
    info, allb = {}, []
    for ch in chans:
        b = [h[5] for h in t.hist if h[4] == ch and h[0] >= t.last - 6.0 and np.isfinite(h[5])]
        if len(b) < 3:
            continue
        med = float(np.median(b))
        allb += b
        if abs(med) < 1.0 and abs(true_deg) > 5.0:
            return None, f"ch{ch} reads {med:.1f} deg here - too close to the middle to learn from"
        new.setdefault(str(ch), []).append([round(med, 2), float(true_deg)])
        info[str(ch)] = {"measured": round(med, 1), "n": len(b),
                         "spread": round(float(1.4826 * np.median(np.abs(np.array(b) - med))), 1)}
    if not info:
        return None, f"too few packets from {t.id} in the last 6 s - keep it still a moment and press again"
    new.setdefault("all", []).append([round(float(np.median(allb)), 2), float(true_deg)])
    warn = []
    for k, v in new.items():
        v.sort(key=lambda q: q[1])
        if k != "all" and angle_weight(v, v[0][0]) == 0.0:
            warn.append("ch" + k)
    return {"angle_table": new, "true_deg": true_deg, "channels": info, "device": t.id,
            "n": sum(v["n"] for v in info.values()),
            "warning": (f"{', '.join(warn)} ignored from now on: a reflection makes it read the wrong side "
                        f"(the other channels carry the angle)" if warn else "")}, None


def live_zero(tracks, match, cal_by_ch, chans, spacing, mount, flip):
    """'Set middle': the phone is at site angle 0 NOW (the measured 'straight ahead' point, e.g. A).
    If the antennas are turned by `mount` deg, that point is at -mount in the antennas' own frame,
    so the offset is chosen to make it read exactly that. One offset per advertising channel."""
    t = _pick_track(tracks, match)
    if t is None:
        return None, f"no packets from {match} in the last few seconds - is the beacon on?"
    alpha = np.radians(-flip * mount)                    # antenna-frame angle of the site's 0 deg
    new, info, missing = dict(cal_by_ch), {}, []
    for ch in chans:
        ph = np.radians([h[3] for h in t.hist if h[4] == ch and h[0] >= t.last - 6.0])
        if len(ph) < 3:
            missing.append(ch)
            continue
        z = np.exp(1j * ph).mean()
        K = 360.0 * spacing * BLE_ADV_FREQ.get(ch, 2.44e9) / SPEED_OF_LIGHT
        new[ch] = round(float(wrap_deg(np.degrees(np.angle(z)) - K * np.sin(alpha))), 2)
        info[str(ch)] = {"n": int(len(ph)), "spread_deg": round(float(np.degrees(np.sqrt(-2 * np.log(max(abs(z), 1e-9))))), 1)}
    if not info:
        return None, f"too few packets from {t.id} in the last 6 s - keep it still a moment and press again"
    return {"phase_by_ch": {str(c): v for c, v in new.items()}, "channels": info, "missing": missing,
            "n": sum(v["n"] for v in info.values()), "spread_deg": max(v["spread_deg"] for v in info.values()),
            "device": t.id, "mount_deg": mount}, None

SITE_FILE = os.path.join(HERE, "usrp_site.json")   # written by the console from the phone app


def open_recording(args, cal, extra):
    """Live runs: recordings/<start>/meta.json + doa.jsonl (one line per packet, one per radar update).
    meta.json holds everything needed to redo the bearing math later with a different calibration."""
    from datetime import datetime
    d = os.path.join(args.record_dir, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    site = None
    try:
        with open(SITE_FILE) as f:
            site = json.load(f)
    except (OSError, ValueError):
        pass
    meta = dict(extra, started=time.time(), args=vars(args), preset=cal, site=site,
                fields={"pkt": "t=Mac clock s, id=track (PHONE-<tag> = the app), p=power dB, "
                               "ph=raw phase x0*conj(x1) deg (before calibration), b=this packet's bearing deg "
                               "(after calibration, + = right), tb=track bearing, d=distance m from this packet",
                        "upd": "what the radar was sent: a=angle, d=distance, bs=bearing spread, n=packets"})
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return d, open(os.path.join(d, "doa.jsonl"), "a", buffering=1)


def preset_path(name):
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("bad preset name")
    return os.path.join(PRESET_DIR, safe + ".json")


def load_preset(name):
    """name '' -> active preset (if any); 'none' -> None."""
    if name.lower() == "none":
        return None
    if not name:
        if not os.path.exists(ACTIVE_FILE):
            return None
        with open(ACTIVE_FILE) as f:
            name = json.load(f).get("name", "")
        if not name:
            return None
    path = preset_path(name)
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: calibration preset '{name}' not found ({path})")
    with open(path) as f:
        return json.load(f)


def cal_estimate(rows):
    """rows: [(power_db, raw_phase_deg)]. Pick the device at 1 m = the strongest
    cluster: bursts within 6 dB of the 95th-pct power, then the dominant phase
    mode among those (+-25 deg). Returns dict or None."""
    if len(rows) < 4:
        return None
    r = np.asarray(rows, dtype=float)
    pmax = np.percentile(r[:, 0], 95)
    strong = r[r[:, 0] >= pmax - 6.0]
    ph = strong[:, 1]
    # dominant circular mode: test each burst's phase as a centre, keep the best
    d = np.abs((ph[None, :] - ph[:, None] + 180) % 360 - 180)
    centre = ph[np.argmax((d <= 25).sum(1))]
    sel = strong[np.abs((ph - centre + 180) % 360 - 180) <= 25]
    z = np.mean(np.exp(1j * np.radians(sel[:, 1])))
    return {"phase_deg": float(np.degrees(np.angle(z))),
            "ref_power_db": float(np.median(sel[:, 0])),
            "phase_spread_deg": float(np.degrees(np.sqrt(-2 * np.log(max(abs(z), 1e-9))))),
            "n_bursts": int(len(sel)), "n_total": int(len(r))}


def wrap_deg(a):
    return (a + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------- IQ source
def _sigterm_to_interrupt(signum, frame):
    raise KeyboardInterrupt


class RawWriter:
    """--save-raw: the COMPLETE 2-channel stream, every sample, uncompressed.
    raw.sc16 = int16 [I0 Q0 I1 Q1] per sample (ch0, ch1 interleaved), value = fc32 * 32767.
    The B210 sends sc12/sc16 over USB, so the fc32 samples are exact int16 / 32767 and this is
    lossless (checked on the first block; if not exact it writes raw.c64 complex64 instead).
    raw_blocks.csv = one row per block: block number, Mac time of the block, sample offset.
    Runs in its own thread so disk writes never stall the radar."""
    SCALE = 32767.0

    def __init__(self, rec_dir, args, channels, block, min_free_gb=3.0):
        import shutil
        self._disk = shutil.disk_usage
        self.dir, self.block, self.nch = rec_dir, block, len(channels)
        self.min_free = min_free_gb * 1e9
        self.q = queue.Queue(maxsize=max(16, int(4.0 * args.rate / block)))   # ~4 s of slack
        self.f = self.fmt = None
        self.idx = open(os.path.join(rec_dir, "raw_blocks.csv"), "w", buffering=1 << 16)
        self.idx.write("block,t_mac,sample_offset,freq_hz\n")
        self.meta = {"rate": args.rate, "freq": args.freq, "gain": args.gain, "channels": channels,
                     "antenna": args.antenna, "block_samples": block, "filtered": False}
        self.n_blocks = self.bytes = self.dropped = 0
        self.stopped_reason = ""
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def put(self, now, X, freq=0.0):
        if self.stopped_reason:
            return
        try:
            self.q.put_nowait((now, X, freq))
        except queue.Full:                    # disk too slow: count it, the gap shows in raw_blocks.csv
            self.dropped += 1

    def _open(self, X):
        y = X.T.reshape(-1).view(np.float32) * self.SCALE
        exact = float(np.abs(y - np.round(y)).max()) < 0.05
        self.fmt = "sc16" if exact else "c64"
        self.f = open(os.path.join(self.dir, "raw." + self.fmt), "wb", buffering=8 << 20)
        self.meta.update(file="raw." + self.fmt, lossless_check_max_frac=float(np.abs(y - np.round(y)).max()),
                         format=("int16 little-endian, per sample [ch0 I, ch0 Q, ch1 I, ch1 Q]; "
                                 "complex value = int16 / 32767 (exactly what the radar processed)") if exact else
                                "complex64 little-endian, per sample [ch0, ch1]",
                         load_python=("np.fromfile(f, np.int16).reshape(-1, %d, 2) -> x = (a[...,0] + 1j*a[...,1]) / 32767"
                                      % self.nch) if exact else
                                     "np.fromfile(f, np.complex64).reshape(-1, %d)" % self.nch)
        with open(os.path.join(self.dir, "raw_meta.json"), "w") as f:
            json.dump(self.meta, f, indent=2)

    def _run(self):
        last_chk = 0.0
        while True:
            item = self.q.get()
            if item is None:
                break
            now, X, freq = item
            if self.stopped_reason:
                continue
            if self.f is None:
                self._open(X)
            if now - last_chk > 1.0:
                last_chk = now
                if self._disk(self.dir).free < self.min_free:
                    self.stopped_reason = f"disk almost full (< {self.min_free/1e9:.0f} GB free)"
                    print(f"[raw] STOPPED saving raw: {self.stopped_reason}. Radar keeps running.", flush=True)
                    continue
            if self.fmt == "sc16":
                buf = np.round(X.T.reshape(-1).view(np.float32) * self.SCALE).astype("<i2")
            else:
                buf = np.ascontiguousarray(X.T)
            self.f.write(buf.tobytes())
            self.idx.write(f"{self.n_blocks},{now:.6f},{self.n_blocks * self.block},{freq:.0f}\n")
            self.n_blocks += 1
            self.bytes += buf.nbytes

    def status(self):
        free = self._disk(self.dir).free
        rate = self.bytes / max(1, self.n_blocks) * (self.meta["rate"] / self.block)
        left = (free - self.min_free) / rate / 60 if rate else 0
        return (f"[raw] {self.bytes/1e9:.2f} GB saved ({self.n_blocks * self.block / self.meta['rate']:.0f} s)  "
                f"disk free {free/1e9:.1f} GB (~{max(0, left):.0f} min left)"
                + (f"  WRITER DROPPED {self.dropped} blocks" if self.dropped else "")
                + (f"  STOPPED: {self.stopped_reason}" if self.stopped_reason else ""))

    def close(self):
        self.q.put(None)
        self.th.join(timeout=30)
        if self.f:
            self.f.close()
        self.idx.close()
        self.meta.update(n_blocks=self.n_blocks, n_samples=self.n_blocks * self.block, bytes=self.bytes,
                         writer_dropped_blocks=self.dropped, stopped_reason=self.stopped_reason)
        with open(os.path.join(self.dir, "raw_meta.json"), "w") as f:
            json.dump(self.meta, f, indent=2)
        print(self.status().replace("[raw]", "[raw] done:"), flush=True)


def native_blocks(args, channels, block):
    """Yield (t_received, X) with X = (n_ch, block) complex64 from the C++ streamer.
    A reader thread drains the pipe into a queue (~2 s deep) so the streamer is never
    kept waiting while a block is being processed."""
    if not os.path.exists(STREAMER):
        raise SystemExit(f"ERROR: {STREAMER} not built. See build line in native/b210_stream.cpp")
    cmd = [STREAMER, "--freq", str(args.freq), "--rate", str(args.rate), "--gain", str(args.gain),
           "--block", str(block), "--channels", ",".join(map(str, channels)), "--antenna", args.antenna]
    if getattr(args, "hop_freqs", None):
        cmd += ["--hop", ",".join(str(f) for f in args.hop_freqs), "--dwell", str(args.dwell_blocks)]
    if args.device_args:
        cmd += ["--args", args.device_args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, bufsize=0)
    q = queue.Queue(maxsize=max(8, int(2.0 * args.rate / block)))

    def reader():
        try:
            while True:
                arr = np.empty((len(channels), block), dtype=np.complex64)
                mv, got = memoryview(arr).cast("B"), 0
                while got < len(mv):
                    n = proc.stdout.readinto(mv[got:])
                    if not n:
                        return
                    got += n
                q.put((time.time(), arr))
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    # Stop button / kill -> SIGTERM. Turn it into KeyboardInterrupt so the finally below
    # always stops the streamer; otherwise it outlives us and keeps the B210 locked.
    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            item = q.get()
            if item is None:
                raise SystemExit("[live] streamer exited (is the B210 plugged in / busy?)")
            yield item
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        while not q.empty():                 # unblock the reader if it is waiting on a full queue
            q.get_nowait()


# --------------------------------------------------------------------------- signal view
class SignalView:
    """Raw what-the-USRP-sees stats, sent ~4x/s over local UDP to the Capture Console
    (/radar signal panel): spectrum (average + peak-hold), per-channel power, noise
    floor and every detected burst. Dropped silently if the console isn't running."""

    def __init__(self, args, ble_chan):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.meta = {"freq": args.freq, "rate": args.rate, "gain": args.gain, "chan": ble_chan}
        self.win = np.hanning(SPEC_NFFT).astype(np.float32)
        self.wnorm = float(np.sum(self.win ** 2))
        self.reset()

    def reset(self):
        self.avg, self.mx, self.nblk, self.bursts, self.ch = None, None, 0, [], None

    def add_block(self, X):
        x = X[0]
        n = (len(x) // SPEC_NFFT) * SPEC_NFFT
        P = np.abs(np.fft.fft(x[:n].reshape(-1, SPEC_NFFT) * self.win, axis=1)) ** 2 / self.wnorm
        P = np.fft.fftshift(P, axes=1)
        a, m = P.mean(0), P.max(0)              # max over 32 us slices catches short BLE packets
        ch = (np.abs(X) ** 2).mean(1)
        if self.avg is None:
            self.avg, self.mx, self.ch = a, m, ch
        else:
            self.avg, self.mx, self.ch = self.avg + a, np.maximum(self.mx, m), self.ch + ch
        self.nblk += 1

    def send(self, now, floor_db, n_bursts, n_decoded, elapsed, calibrating):
        if self.nblk == 0:
            return
        db = lambda v: [round(float(z), 1) for z in 10 * np.log10(np.asarray(v) + 1e-20)]
        msg = dict(self.meta, type="signal", t=now, floor_db=round(float(floor_db), 1),
                   ch_db=db(self.ch / self.nblk), spec_avg=db(self.avg / self.nblk), spec_max=db(self.mx),
                   bursts=self.bursts[-300:], n_bursts=n_bursts, n_decoded=n_decoded,
                   elapsed=round(elapsed, 1), calibrating=bool(calibrating))
        try:
            self.sock.sendto(json.dumps(msg, separators=(",", ":")).encode(), SIGNAL_ADDR)
        except OSError:
            pass
        self.reset()


# --------------------------------------------------------------------------- bursts
def find_bursts(X, rate, snr_db, min_us, max_us, smooth_us=10.0):
    """Return list of (start, stop) sample indices of bursts in block X (n_ch, N)."""
    env = (X.real ** 2 + X.imag ** 2).sum(axis=0, dtype=np.float64)
    k = max(1, int(rate * smooth_us * 1e-6))
    cs = np.concatenate(([0.0], np.cumsum(env)))               # moving average, same as a boxcar convolve
    sm = np.empty(len(env))
    h = k // 2
    sm[h:h + len(env) - k + 1] = (cs[k:] - cs[:-k]) / k
    sm[:h], sm[h + len(env) - k + 1:] = sm[h], sm[h + len(env) - k]
    floor = np.median(sm)
    on = sm > floor * 10 ** (snr_db / 10.0)
    d = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    lo, hi = rate * min_us * 1e-6, rate * max_us * 1e-6
    guard = k  # trim smoothing edges
    out = []
    for s, e in zip(starts, stops):
        if s == 0 or e >= X.shape[1]:
            continue                      # cut by block edge -> skip partial packet
        if lo <= (e - s) <= hi and (e - s) > 3 * guard:
            out.append((s + guard, e - guard))
    return out, 10 * np.log10(floor + 1e-20)


# --------------------------------------------------------------------------- tracks
def music_from_cov(R, wavelength, spacing, n_src=1):
    """MUSIC pseudo-spectrum peak from a (time-averaged) covariance matrix."""
    _, vecs = getEigenValues(R)
    angles, p = evaluate_spectrum(steeringVector(wavelength, spacing, R.shape[0]), vecs[:, n_src:])
    return float(get_peak_detection(angles, p))


class Track:
    """One device. Stability comes from averaging, not from single packets:
    - R:  power-normalised spatial covariance, exponentially averaged over ~tau s,
          MUSIC runs on R  -> bearing (equivalent to MUSIC over all recent packets)
    - hist: per-packet (t, bearing, power) over the last `window` s
          -> spread (how much single packets disagree), median power -> distance."""

    def __init__(self, tid, label, now, tau, window):
        self.id, self.label = tid, label
        self.tau, self.window = tau, window
        self.R = None
        self.hist = deque()
        self.hits, self.first, self.last, self.last_emit = 0, now, now, 0.0
        self.p = self.phase = self.bearing = 0.0
        self.group = tid

    def add(self, Rpkt, p_db, phase, pkt_bearing, now, wavelength, spacing, flip, ch=0, mount=0.0,
            raw_bearing=None, amap=None, weight=1.0):
        """Rpkt/pkt_bearing = None: packet on a channel without a phase calibration -> counts for
        power (distance) only. hist rows: (t, bearing or nan, power dB, raw phase deg, BLE channel)."""
        if Rpkt is not None:
            Rn = Rpkt / (np.real(np.trace(Rpkt)) + 1e-20)
            a = 1.0 if self.R is None else 1.0 - np.exp(-(now - self.last) / self.tau)
            a = max(a, 0.08)                                   # every packet counts a little
            self.R = Rn if self.R is None else (1 - a) * self.R + a * Rn
        self.hist.append((now, np.nan if pkt_bearing is None else pkt_bearing, p_db, phase, ch,
                          np.nan if raw_bearing is None else raw_bearing, weight))
        while self.hist and now - self.hist[0][0] > self.window:
            self.hist.popleft()
        self.hits += 1
        self.last = now
        if self.R is not None:
            self.bearing = map_angle(amap, mount + flip * music_from_cov(self.R, wavelength, spacing))
        self.p = float(np.median([h[2] for h in self.hist]))
        self.phase = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians([h[3] for h in self.hist]))))))

    def stats(self):
        b = np.array([h[1] for h in self.hist], dtype=float)
        b = b[np.isfinite(b)]
        p = np.array([h[2] for h in self.hist])
        n = len(self.hist)
        span = max(1e-3, self.hist[-1][0] - self.hist[0][0]) if n > 1 else 1.0
        # robust spread: median absolute deviation of per-packet bearings
        spread = float(1.4826 * np.median(np.abs(b - np.median(b)))) if len(b) >= 3 else 99.0
        return {"bearing_std": round(spread, 1), "p_lo": round(float(np.percentile(p, 20)), 1),
                "p_hi": round(float(np.percentile(p, 80)), 1), "rate": round((n - 1) / span, 1) if n > 1 else 0.0,
                "n_window": n}


def group_tracks(tracks, min_hits):
    """Phones (esp. iPhones) advertise from several random addresses at once.
    Tracks from the same maker with near-identical bearing and power, seen at the
    same time, are flagged as one physical device (group = strongest member id)."""
    live = sorted([t for t in tracks.values() if t.hits >= min_hits], key=lambda t: -t.hits)
    for t in live:
        t.group = t.id
    for i, a in enumerate(live):
        if a.group != a.id:
            continue
        vend_a = a.label.split(" ")[0]
        for b in live[i + 1:]:
            vend_b = b.label.split(" ")[0]
            same_maker = vend_a == vend_b or "BLE" in (vend_a, vend_b)   # unknown maker may match
            if b.group == b.id and same_maker and \
               abs(a.bearing - b.bearing) <= 8 and abs(a.p - b.p) <= 3 and abs(a.last - b.last) < 4:
                b.group = a.id


def associate(tracks, p_db, phase, p_gate, ph_gate):
    """Anonymous (undecoded) bursts: nearest track in (power, phase)."""
    best, best_cost = None, 1e9
    for t in tracks.values():
        if not t.id.startswith("anon-"):
            continue
        dp, dph = abs(p_db - t.p), abs(wrap_deg(phase - t.phase))
        if dp <= p_gate and dph <= ph_gate:
            cost = dp / p_gate + dph / ph_gate
            if cost < best_cost:
                best, best_cost = t, cost
    return best


def device_table(rows):
    """rows: [(p, phase, addr, desc)] -> per-device summary, strongest first."""
    by = {}
    for p_db, ph, addr, desc in rows:
        if addr:
            by.setdefault(addr, {"addr": addr, "desc": desc, "p": []})["p"].append(p_db)
    out = [{"addr": a, "desc": d["desc"], "n": len(d["p"]), "p": round(float(np.median(d["p"])), 1)}
           for a, d in by.items()]
    out.sort(key=lambda r: (r["n"] >= 5, r["p"]), reverse=True)
    return out


def pick_cal_device(rows, target):
    devs = device_table(rows)
    if target:
        for d in devs:
            if d["addr"].upper() == target.upper():
                return d, devs
        return None, devs
    return (devs[0] if devs and devs[0]["n"] >= 5 else None), devs


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Live B210 2-ch capture -> BLE decode + MUSIC bearing + distance -> Live Radar.")
    ap.add_argument("--freq", type=float, default=None,
                    help="Center Hz (BLE adv: 2.402e9 / 2.426e9 / 2.480e9). Default: preset or 2.480e9")
    ap.add_argument("--rate", type=float, default=4e6, help="Sample rate Hz (default 4e6 = 4 samples/bit)")
    ap.add_argument("--gain", type=float, default=None, help="RX gain dB (B210 0..76). Default: preset or 50")
    ap.add_argument("--window", type=float, default=20.0, help="Block length ms (default 20)")
    ap.add_argument("--duration", type=float, default=0.0, help="Seconds to run (0 = until stopped)")
    ap.add_argument("--channels", default="0,1", help="RX channels (0,1 = 2-element array)")
    ap.add_argument("--antenna", default=None,
                    help="RX port the antennas are on: TX/RX or RX2 (default: preset or TX/RX)")
    ap.add_argument("--device-args", default="", help="UHD device args, e.g. 'serial=321E86A'")
    ap.add_argument("--spacing", type=float, default=0.0,
                    help="Antenna spacing m (0 = preset, else lambda/2 at --freq; MEASURE your antennas)")
    ap.add_argument("--snr-db", type=float, default=12.0, help="Burst threshold above noise floor (dB)")
    ap.add_argument("--min-us", type=float, default=60.0, help="Min burst length us")
    ap.add_argument("--max-us", type=float, default=600.0, help="Max burst length us (BLE legacy adv <= ~380)")
    ap.add_argument("--path-loss-exp", type=float, default=None,
                    help=f"Path-loss exponent n (default: preset's fitted value, else {DEFAULT_PATH_LOSS_EXP})")
    ap.add_argument("--ref-power-db", type=float, default=None, help="Burst power at 1 m (overrides calibration)")
    ap.add_argument("--anonymous", action="store_true",
                    help="Also track bursts that are not decodable BLE adv packets (Classic BT, data, noise)")
    ap.add_argument("--only", default="", help="Only show devices whose label/address contains this text, e.g. Apple")
    ap.add_argument("--target", default="", help="Calibration: use only this advertiser address")
    ap.add_argument("--p-gate", type=float, default=5.0, help="Anonymous association power gate dB")
    ap.add_argument("--phase-gate", type=float, default=25.0, help="Anonymous association phase gate deg")
    ap.add_argument("--min-hits", type=int, default=2, help="Packets before a track is shown")
    ap.add_argument("--track-timeout", type=float, default=15.0, help="Drop track after N s unseen")
    ap.add_argument("--update-s", type=float, default=2.0,
                    help="Radar update period s: each update = median of the packets in that window (0.3 = old fast mode)")
    ap.add_argument("--hold-spread", type=float, default=10.0,
                    help="If packets in an update window disagree more than this (deg), hold the last good bearing")
    ap.add_argument("--avg-time", type=float, default=3.0,
                    help="Averaging time constant s (bigger = steadier but slower to follow motion)")
    ap.add_argument("--calibrate", type=float, default=0.0,
                    help="Calibrate for N s: device at 1 m, broadside (0 deg). Prints CAL_RESULT")
    ap.add_argument("--save-preset", default="", help="With --calibrate: save the result as this preset")
    ap.add_argument("--dump-packets", default="",
                    help="With --calibrate: append every --target packet as a JSON line {t, p, ph} to this file "
                         "(input for cal_fit.py: sweep / AR calibration from the phone app)")
    ap.add_argument("--preset", default="", help="Calibration preset for live runs ('' = active, 'none' = off)")
    ap.add_argument("--record-dir", default=RECORD_DIR,
                    help="Live runs: save every packet's DoA to <dir>/<start time>/ (doa.jsonl + meta.json) "
                         "for doa_compare.py")
    ap.add_argument("--no-record", action="store_true", help="Live runs: don't save the DoA recording")
    ap.add_argument("--save-iq", action="store_true",
                    help="Live runs: also save every packet's raw 2-channel IQ (unfiltered, +-margin) to the recording: "
                         "iq.c64 (complex64) + iq_index.jsonl (where each packet is in iq.c64). ~25 kB per packet")
    ap.add_argument("--save-raw", action="store_true",
                    help="Live runs: save the COMPLETE 2-channel stream, every sample, lossless int16 "
                         "(raw.sc16, 32 MB/s at 4 MS/s). Stops by itself when < 3 GB disk is left")
    ap.add_argument("--iq-only", default="",
                    help="With --save-iq: only packets whose track id/label contains this text (e.g. PHONE)")
    ap.add_argument("--iq-margin-us", type=float, default=40.0, help="With --save-iq: extra samples kept before/after each packet")
    ap.add_argument("--label", default="BLE", help="Label prefix for anonymous tracks")
    ap.add_argument("--hop", default="37,38,39",
                    help="BLE advertising channels to cycle through (default 37,38,39; 'none' = stay on --freq). "
                         "Each channel gets its own phase offset (Set middle measures all of them)")
    ap.add_argument("--dwell-ms", type=float, default=300.0, help="With --hop: time on each channel (ms)")
    ap.add_argument("--mount-deg", type=float, default=None,
                    help="How far the antenna pair is turned to the right of the site's 0 deg (default: preset or 0)")
    ap.add_argument("--dist-tau", type=float, default=4.0,
                    help="Distance smoothing time constant s (bigger = steadier, slower)")
    args = ap.parse_args()

    channels = [int(c) for c in str(args.channels).split(",") if c.strip()]
    cal = None if args.calibrate else load_preset(args.preset)
    if args.freq is None:
        args.freq = cal["freq"] if cal else DEFAULT_FREQ
    if args.gain is None:
        args.gain = cal.get("gain", DEFAULT_GAIN) if cal else DEFAULT_GAIN
    if args.path_loss_exp is None:
        args.path_loss_exp = float(cal.get("path_loss_exp") or DEFAULT_PATH_LOSS_EXP) if cal else DEFAULT_PATH_LOSS_EXP
    if args.antenna is None:
        args.antenna = cal.get("antenna", "TX/RX") if cal else "TX/RX"
    if cal and cal.get("antenna", "TX/RX") != args.antenna:
        print(f"[live] WARNING: preset was calibrated on port {cal.get('antenna')}, running on {args.antenna}")
    # channel hopping over the BLE advertising channels (not during --calibrate)
    hop_chans = []
    if not args.calibrate and args.hop.strip().lower() not in ("", "none", "off", "no"):
        hop_chans = [int(c) for c in args.hop.split(",") if c.strip()]
        bad = [c for c in hop_chans if c not in BLE_ADV_FREQ]
        if bad:
            raise SystemExit(f"ERROR: --hop takes BLE advertising channels 37,38,39 (got {bad})")
    args.hop_freqs = [BLE_ADV_FREQ[c] for c in hop_chans] if len(hop_chans) > 1 else []
    args.dwell_blocks = max(2, int(round(args.dwell_ms / args.window))) if args.hop_freqs else 0
    if args.hop_freqs:
        args.freq = args.hop_freqs[0]
    run_chans = hop_chans if args.hop_freqs else [channel_for_freq(args.freq)]
    # phase offset per advertising channel; an old single-channel preset covers only its own channel
    cal_by_ch = {}
    if cal:
        for k, v in (cal.get("phase_by_ch") or {}).items():
            cal_by_ch[int(k)] = float(v)
        if not cal_by_ch and cal.get("phase_deg") is not None:
            cal_by_ch[channel_for_freq(cal.get("freq", args.freq))] = float(cal["phase_deg"])
    if not cal_by_ch:
        cal_by_ch = {c: 0.0 for c in run_chans}          # uncalibrated: raw phase, all channels
    block = int(args.rate * args.window / 1000)
    sps = int(round(args.rate / 1e6))
    ble_chan = channel_for_freq(args.freq)
    wavelength = SPEED_OF_LIGHT / (float(np.mean(args.hop_freqs)) if args.hop_freqs else args.freq)
    spacing = args.spacing if args.spacing > 0 else (cal.get("spacing_m") if cal and cal.get("spacing_m")
                                                     else wavelength / 2)
    if cal and not args.hop_freqs and abs(cal.get("freq", 0) - args.freq) > 5e6:
        print(f"[live] WARNING: preset '{cal['name']}' was calibrated at {cal['freq']/1e9:.3f} GHz, "
              f"running at {args.freq/1e9:.3f} GHz -> phase offset may be wrong. Recalibrate on this channel.")
    if cal and abs(cal.get("gain", args.gain) - args.gain) > 0.1:
        print(f"[live] WARNING: preset gain {cal['gain']} dB != {args.gain} dB -> distances shifted")
    cal_phase = cal["phase_deg"] if cal else 0.0
    flip = -1.0 if cal and cal.get("flip") else 1.0
    ref_p = args.ref_power_db if args.ref_power_db is not None else (cal["ref_power_db"] if cal else -20.0)
    only = args.only.lower().strip()

    mount = args.mount_deg if args.mount_deg is not None else float((cal or {}).get("mount_deg") or 0.0)
    gain_table = [list(map(float, q)) for q in (cal or {}).get("gain_table") or []]
    angle_table = dict((cal or {}).get("angle_table") or {})
    have_ref = args.ref_power_db is not None or bool(gain_table) or bool((cal or {}).get("range_set"))

    print("=" * 64)
    print("LIVE B210 BLE TRACKING" + ("  [CALIBRATION]" if args.calibrate else ""))
    print("=" * 64)
    print(f"Freq {args.freq/1e9:.3f} GHz (BLE ch {ble_chan})  rate {args.rate/1e6:.1f} MHz  gain {args.gain} dB  port {args.antenna}")
    print(f"Array: M={len(channels)}  lambda={wavelength*100:.2f} cm  d={spacing*100:.2f} cm  d/lambda={spacing/wavelength:.3f}")
    if spacing / wavelength > 0.5 + 1e-6:
        lim = np.degrees(np.arcsin(min(1.0, wavelength / (2 * spacing))))
        print(f"NOTE: d > lambda/2 -> unambiguous only within +-{lim:.0f} deg; sources further out alias to the other side")
    print(f"Packets: BLE adv decode + CRC check{' + anonymous bursts' if args.anonymous else ' (undecodable bursts ignored)'}"
          f"{'  filter: ' + args.only if only else ''}")
    if args.hop_freqs:
        print(f"Hopping BLE ch {','.join(map(str, hop_chans))}: {args.dwell_blocks * args.window:.0f} ms each "
              f"(power averaged over channels against fading)")
    if not args.calibrate:
        print(f"Antennas turned {mount:+.1f} deg (right = +).  Phase offset per channel: "
              + ", ".join(f"ch{c} {v:+.1f}" for c, v in sorted(cal_by_ch.items()))
              + ("  (channels without an offset count for distance only until Set middle)"
                 if set(run_chans) - set(cal_by_ch) else ""))
        print(f"Distance: 1 m level {ref_p:.1f} dB, n={args.path_loss_exp}, "
              + (f"direction points {gain_table}" if gain_table else "not calibrated yet (use Set distance)"))
        print("Calibration: " + (f"preset '{cal['name']}'  phase {cal_phase:+.1f} deg  ref {ref_p:.1f} dB @1 m"
                                 f"{'  (flipped)' if flip < 0 else ''}"
                                 f"{'  device ' + cal['device'] if cal.get('device') else ''}" if cal
                                 else "NONE - bearing offset unknown, distance relative"))
    rec = None
    if not args.calibrate and not args.no_record:
        try:
            rec_dir, rec = open_recording(args, cal, {
                "freq": args.freq, "gain": args.gain, "spacing_m": spacing, "wavelength_m": wavelength,
                "cal_phase_deg": cal_phase, "flip": flip < 0, "ref_power_db": ref_p,
                "path_loss_exp": args.path_loss_exp, "channels": channels, "hop_channels": hop_chans if args.hop_freqs else [],
                "dwell_ms": args.dwell_blocks * args.window if args.hop_freqs else None,
                "phase_by_ch": {str(c): v for c, v in cal_by_ch.items()}, "mount_deg": mount, "gain_table": gain_table,
                "angle_table": angle_table})
            print(f"Recording: {os.path.relpath(rec_dir, ROOT)}  (compare later: python3 capture_console/doa_compare.py)")
        except OSError as e:
            print(f"[live] WARNING: cannot record DoA ({e})")
    iq_bin = iq_idx = prevX = None
    iq_off = 0
    if rec and args.save_iq:
        iq_bin = open(os.path.join(rec_dir, "iq.c64"), "ab")
        iq_idx = open(os.path.join(rec_dir, "iq_index.jsonl"), "a", buffering=1)
        with open(os.path.join(rec_dir, "iq_meta.json"), "w") as f:
            json.dump({"format": "complex64 little-endian; per packet: channel 0 samples then channel 1 samples",
                       "index": "iq_index.jsonl: off = byte offset in iq.c64, n = samples PER CHANNEL, "
                                "pkt_start/pkt_stop = packet edges inside the n samples, t = Mac time of sample 0",
                       "rate": args.rate, "freq": args.freq, "gain": args.gain, "channels": channels,
                       "antenna": args.antenna, "spacing_m": spacing, "wavelength_m": wavelength,
                       "cal_phase_deg": cal_phase, "flip": flip < 0, "filtered": False}, f, indent=2)
        print(f"Raw IQ: saving {'every packet' if not args.iq_only else 'packets matching ' + repr(args.iq_only)} "
              f"to {os.path.relpath(rec_dir, ROOT)}/iq.c64")
    raw_w = None
    if rec and args.save_raw:
        raw_w = RawWriter(rec_dir, args, channels, block)
        mbps = args.rate * len(channels) * 4 / 1e6
        print(f"Raw stream: saving EVERY sample to {os.path.relpath(rec_dir, ROOT)}/raw.sc16  "
              f"({mbps:.0f} MB/s, ~{mbps*3.6:.0f} GB per hour)")
    elif args.save_raw:
        print("[live] WARNING: --save-raw needs recording on (remove --no-record); raw stream NOT saved")
    print("=" * 64, flush=True)
    ctl_seen = None
    try:
        ctl_seen = json.load(open(LIVE_CTL)).get("id")          # ignore a command left from an earlier run
    except (OSError, ValueError):
        pass
    last_ctl_check = 0.0

    start = time.time()
    tracks, next_anon, emitted, n_bursts, n_decoded = {}, 1, 0, 0, 0
    cal_rows = []
    last_status = last_sig = start
    sig = SignalView(args, ble_chan)
    scanners = {c: BleScanner(args.rate, c) for c in run_chans}
    blk_no = 0
    n_known = n_clip = 0
    block_s = block / args.rate
    dump = open(args.dump_packets, "a", buffering=1) if args.calibrate and args.dump_packets else None

    try:
        first = True
        for now, X in native_blocks(args, channels, block):
            # which advertising channel this block is on (the streamer hops every dwell_blocks)
            if args.hop_freqs:
                cur_ch = run_chans[(blk_no // args.dwell_blocks) % len(run_chans)]
                if blk_no % args.dwell_blocks == 0:          # fresh dwell: samples are not continuous
                    scanners[cur_ch].tail = None
                    prevX = None
            else:
                cur_ch = run_chans[0]
            blk_no += 1
            scanner = scanners[cur_ch]
            ch_freq = BLE_ADV_FREQ.get(cur_ch, args.freq)
            ch_wl = SPEED_OF_LIGHT / ch_freq
            if first:                        # clock starts at the first real block (B210 boot ~4 s)
                start = last_status = now
                first = False
                print("[live] receiving", flush=True)
            if args.calibrate and now - start >= args.calibrate:
                break
            if args.duration and now - start >= args.duration:
                break

            if raw_w:
                raw_w.put(now, X, ch_freq)
            # Xf = channel-filtered [carry-over of last block (L samples) | this block]
            Xf, L, pkts = scanner.feed(X, now)
            if iq_bin:
                # unfiltered samples on the same index as Xf: [last L samples of previous block | this block]
                prevX = X[:, :0] if prevX is None else prevX
                Xraw = np.concatenate([prevX[:, prevX.shape[1] - L:] if prevX.shape[1] >= L
                                       else np.zeros((X.shape[0], L), X.dtype), X], axis=1)
                prevX = X
            bursts, floor_db = find_bursts(X, args.rate, args.snr_db, args.min_us, args.max_us)
            sig.add_block(X)
            blk_t0 = now - block_s
            items = [(pk["start"], pk["stop"], pk) for pk in pkts]
            for s, e in bursts:                  # power bursts that are not a decoded packet
                if not any(s + L < pk["stop"] and e + L > pk["start"] for pk in pkts):
                    items.append((s + L, e + L, None))
            items.sort(key=lambda it: it[0])
            for s, e, pk in items:
                seg = Xf[:, s:e]
                n_bursts += 1
                addr = pk["addr"] if pk else None
                desc = describe(pk) if pk else None
                phone_tag = pk.get("usrp_tag") if pk else None
                if phone_tag is not None:
                    addr = f"PHONE-{phone_tag}"   # Android rotates its random address; key on the app's tag

                # power for distance: mean of both antennas (one may sit in a pattern null)
                p_db = float(10 * np.log10(np.mean(np.abs(seg) ** 2) + 1e-20))
                raw_phase = (float(np.degrees(np.angle(np.vdot(seg[1], seg[0]))))  # angle(x0 * conj(x1))
                             if seg.shape[0] > 1 else 0.0)
                if pk and float(np.max(np.abs(seg))) > 0.9:
                    n_clip += 1                   # ADC near full scale -> power (distance) reads low
                sig.bursts.append({"t": round(blk_t0 + (s - L) / args.rate, 4), "p": round(p_db, 1),
                                   "ph": round(raw_phase, 1), "us": int((e - s) / args.rate * 1e6),
                                   "dev": desc, "phone": phone_tag is not None})

                if pk:
                    n_decoded += 1
                    n_known += not pk["crc_ok"]
                elif not args.anonymous and not args.calibrate:
                    continue

                if args.calibrate:
                    if dump and addr and (not args.target or addr.upper() == args.target.upper()):
                        dump.write(json.dumps({"t": round(blk_t0 + (s - L) / args.rate, 4), "p": round(p_db, 2),
                                               "ph": round(raw_phase, 2)}) + "\n")
                    cal_rows.append((p_db, raw_phase, addr, desc))
                    continue

                Y = seg.T.copy()                                       # (N, M)
                Rpkt = None
                raw_bearing = None
                pkt_w = 1.0
                if cur_ch not in cal_by_ch:
                    pkt_bearing = None              # no offset for this channel yet: distance only
                elif Y.shape[1] >= 2:
                    Y[:, 1] *= np.exp(1j * np.radians(cal_by_ch[cur_ch]))   # remove this channel's RX offset
                    with contextlib.redirect_stdout(io.StringIO()):
                        pkt_bearing = mount + flip * float(find_angle(Y, wave_length=ch_wl,
                                                                      distance_d=spacing, number_of_drones=1))
                    raw_bearing = pkt_bearing
                    pkt_w = angle_weight(angle_table.get(str(cur_ch)), raw_bearing)
                    pkt_bearing = map_angle(angle_table.get(str(cur_ch)), pkt_bearing)
                    Rpkt = (Y.T @ Y.conj()) / len(Y)                   # same convention as music.py
                else:
                    pkt_bearing = 0.0
                if pkt_bearing is not None:
                    sig.bursts[-1]["b"] = round(pkt_bearing, 1)

                if addr:
                    t = tracks.get(addr)
                    if t is None:
                        t = tracks[addr] = Track(addr, f"{desc} {addr[-5:]}", now, args.avg_time, max(3.0, 2 * args.avg_time))
                    t.label = desc if phone_tag is not None else f"{desc} {addr[-5:]}"
                    t.desc = desc
                else:
                    t = associate(tracks, p_db, raw_phase, args.p_gate, args.phase_gate)
                    if t is None:
                        tid = f"anon-{next_anon}"
                        next_anon += 1
                        t = tracks[tid] = Track(tid, f"{args.label} ? {tid[5:]}", now, args.avg_time, max(3.0, 2 * args.avg_time))
                        t.desc = "unknown"
                t.add(Rpkt, p_db, raw_phase, pkt_bearing, now, wavelength, spacing, flip, cur_ch, mount,
                      raw_bearing, angle_table.get("all"), pkt_w)
                if rec:
                    rec.write(json.dumps({"k": "pkt", "t": round(blk_t0 + (s - L) / args.rate, 4), "id": t.id,
                                          "desc": getattr(t, "desc", ""), "crc": bool(pk and pk["crc_ok"]), "p": round(p_db, 2),
                                          "ph": round(raw_phase, 2), "ch": cur_ch,
                                          "b": None if pkt_bearing is None else round(pkt_bearing, 2),
                                          "br": None if raw_bearing is None else round(raw_bearing, 2),
                                          "tb": round(t.bearing, 2),
                                          "d": round(10 ** ((ref_p - p_db) / (10.0 * args.path_loss_exp)), 3)}) + "\n")
                if iq_bin and (not args.iq_only or args.iq_only.lower() in (t.id + " " + t.label).lower()):
                    mg = int(args.iq_margin_us * 1e-6 * args.rate)
                    a, b = max(0, s - mg), min(Xraw.shape[1], e + mg)
                    chunk = np.ascontiguousarray(Xraw[:, a:b], dtype=np.complex64)
                    iq_bin.write(chunk.tobytes())
                    iq_idx.write(json.dumps({"off": iq_off, "n": b - a, "pkt_start": s - a, "pkt_stop": e - a,
                                             "t": round(blk_t0 + (a - L) / args.rate, 6), "id": t.id,
                                             "desc": getattr(t, "desc", ""), "crc": bool(pk and pk["crc_ok"]),
                                             "p": round(p_db, 2), "ph": round(raw_phase, 2),
                                             "ch": cur_ch, "b": None if pkt_bearing is None else round(pkt_bearing, 2)}) + "\n")
                    iq_off += chunk.nbytes

            # radar buttons ("Set middle", "Swap left/right") - checked twice a second
            if not args.calibrate and now - last_ctl_check > 0.5:
                last_ctl_check = now
                try:
                    cmd = json.load(open(LIVE_CTL))
                except (OSError, ValueError):
                    cmd = None
                if cmd and cmd.get("id") != ctl_seen:
                    ctl_seen = cmd.get("id")
                    res, err = None, None
                    if cmd.get("cmd") == "zero":
                        res, err = live_zero(tracks, cmd.get("device") or "PHONE", cal_by_ch, run_chans,
                                             spacing, mount, flip)
                        if res:
                            cal_by_ch = {int(c): v for c, v in res["phase_by_ch"].items()}
                            if angle_table:
                                res["note"] = "angle points cleared (they belonged to the old offsets) - teach B and C again"
                            angle_table = {}
                            cal_phase = cal_by_ch.get(run_chans[0], cal_phase)
                    elif cmd.get("cmd") == "mount":
                        try:
                            mount = float(cmd.get("mount_deg"))
                            assert -90 <= mount <= 90
                            res = {"mount_deg": mount, "note": "press Set middle again at the 0 deg point"}
                        except (TypeError, ValueError, AssertionError):
                            err = "rotation must be a number of degrees between -90 and 90"
                    elif cmd.get("cmd") == "angle":
                        try:
                            td = float(cmd.get("true_deg"))
                            assert -90 <= td <= 90 and abs(td) >= 5
                        except (TypeError, ValueError, AssertionError):
                            td = None; err = "true angle must be between -90 and 90 and at least 5 deg from the middle"
                        if td is not None:
                            res, err = live_angle(tracks, cmd.get("device") or "PHONE", td, angle_table, run_chans)
                            if res:
                                angle_table = res["angle_table"]
                    elif cmd.get("cmd") == "aclear":
                        angle_table = {}
                        res = {"angle_table": {}, "note": "angle points cleared"}
                    elif cmd.get("cmd") == "dclear":
                        gain_table, have_ref = [], False
                        res = {"gain_table": [], "note": "distance points cleared - next Set distance sets the reference"}
                    elif cmd.get("cmd") == "range":
                        try:
                            dm = float(cmd.get("dist_m"))
                            assert 0.3 <= dm <= 200
                        except (TypeError, ValueError, AssertionError):
                            dm = None; err = "distance must be a number of metres (0.3-200)"
                        if dm:
                            res, err = live_range(tracks, cmd.get("device") or "PHONE", dm, args.path_loss_exp,
                                                  ref_p, gain_table, have_ref)
                            if res:
                                ref_p, gain_table, have_ref = res["ref_power_db"], res["gain_table"], True
                    elif cmd.get("cmd") == "flip":
                        flip = -flip
                        res = {"flip": flip < 0}
                    else:
                        err = f"unknown command {cmd.get('cmd')!r}"
                    if res:
                        res.update(cal_phase_deg=round(cal_phase, 2), flip=flip < 0, ref_power_db_now=round(ref_p, 2),
                                   mount_deg=mount, gain_table=gain_table, angle_table=angle_table)
                        if cmd.get("cmd") in ("zero", "flip", "mount", "angle", "aclear"):
                            tracks.clear()                        # forget bearings made with the old offset
                        # remember it: the next Start live begins from this offset (preset "live")
                        try:
                            os.makedirs(PRESET_DIR, exist_ok=True)
                            p = dict(cal or {}, name="live", phase_deg=cal_phase, flip=flip < 0, freq=args.freq,
                                     gain=args.gain, spacing_m=spacing, antenna=args.antenna, t=time.time(),
                                     created=time.strftime("%Y-%m-%d %H:%M"),
                                     notes="set live with the radar's Set middle button")
                            p["ref_power_db"] = ref_p; p["path_loss_exp"] = args.path_loss_exp
                            p.update(phase_by_ch={str(c): v for c, v in cal_by_ch.items()}, mount_deg=mount,
                                     gain_table=gain_table, angle_table=angle_table, hop=args.hop)
                            with open(preset_path("live"), "w") as f:
                                json.dump(p, f, indent=2)
                            with open(ACTIVE_FILE, "w") as f:
                                json.dump({"name": "live"}, f)
                        except OSError as e:
                            print(f"[live] could not save preset 'live': {e}")
                        if rec:
                            rec.write(json.dumps(dict(res, k="cal", t=round(now, 3), cmd=cmd.get("cmd"))) + "\n")
                    out = {"id": cmd.get("id"), "ok": bool(res), "t": time.time(), "cmd": cmd.get("cmd"),
                           **(res or {}), **({"error": err} if err else {})}
                    with open(LIVE_CTL_RESULT, "w") as f:
                        json.dump(out, f)
                    if res:
                        msg = (f"{cmd.get('cmd')}: offsets " + ", ".join(f"ch{c} {v:+.1f}" for c, v in sorted(cal_by_ch.items()))
                               + f", antennas turned {mount:+.1f} deg, distance points {gain_table}, "
                               + f"angle points {angle_table.get('all', [])}, "
                               f"{'left/right SWAPPED' if flip < 0 else 'normal left/right'}, 1 m level {ref_p:.1f} dB")
                        if "n" in res:
                            msg += f" (from {res['n']} packets of {res.get('device', '?')})"
                    else:
                        msg = f"{cmd.get('cmd')} FAILED: {err}"
                    print("[live] " + msg, flush=True)

            tracks = {k: t for k, t in tracks.items() if now - t.last < args.track_timeout}
            group_tracks(tracks, args.min_hits)
            for t in tracks.values():
                if only and only not in (t.label + " " + t.id).lower():
                    continue
                if t.hits >= args.min_hits and t.last > t.last_emit and now - t.last_emit >= args.update_s:
                    st = t.stats()
                    angle, p_now, held, raw = t.bearing, t.p, False, None
                    if args.update_s >= 1.0:
                        # Summary of this update window: median of the per-packet bearings
                        # (ignores single bad packets). If the packets disagree a lot (a person
                        # moving, reflections changing), keep showing the last good bearing.
                        win = [h for h in t.hist if h[0] >= now - args.update_s and np.isfinite(h[1])]
                        if len(win) >= 2:
                            b = np.array([h[1] for h in win])
                            raw = wmedian(b, [h[6] for h in win])
                            spread = float(1.4826 * np.median(np.abs(b - raw))) if len(win) >= 3 else 99.0
                            p_now = float(np.median([h[2] for h in win]))
                            st.update(bearing_std=round(spread, 1), n_window=len(win),
                                      p_lo=round(float(np.percentile([h[2] for h in win], 20)), 1),
                                      p_hi=round(float(np.percentile([h[2] for h in win], 80)), 1))
                            held = (spread > args.hold_spread and getattr(t, "shown", None) is not None
                                    and getattr(t, "held_n", 0) < 3)      # never freeze longer than 3 updates
                            t.held_n = getattr(t, "held_n", 0) + 1 if held else 0
                            angle = t.shown if held else raw
                    t.shown = angle
                    # distance: power averaged over the advertising channels (last few s, so every
                    # channel is in), minus this direction's correction, then smoothed in log-distance
                    pw_win = max(4.0, 1.5 * args.dwell_blocks * args.window / 1000.0 * len(run_chans))
                    p_ch, ch_spread, _, _ = chan_power(t.hist, now - pw_win)
                    if p_ch is None:
                        p_ch = p_now
                    g = gain_at(gain_table, angle)
                    d_inst = dist_from_power(p_ch - g, ref_p, args.path_loss_exp)
                    prev = getattr(t, "dlog", None)
                    dt = now - getattr(t, "d_t", now)
                    if prev is None:
                        t.dlog = np.log(d_inst)
                    else:
                        a = 1.0 - np.exp(-dt / max(0.1, args.dist_tau))
                        step = np.clip(np.log(d_inst) - prev, -0.25, 0.25)     # one update can't jump > ~28 %
                        t.dlog = prev + a * step
                    t.d_t = now
                    dist = float(np.exp(t.dlog))
                    # band: fading left over between channels + power jitter + direction-model error
                    near = min([abs(angle - q[0]) for q in gain_table], default=180.0)
                    sigma = float(np.sqrt(max(ch_spread, 0.5 * (st["p_hi"] - st["p_lo"])) ** 2
                                          + 2.5 ** 2 + (3.0 if near > 15 else 0.0) ** 2))
                    k = 10 ** (sigma / (10.0 * args.path_loss_exp))
                    d_lo, d_hi = dist / k, dist * k
                    st.update(dist_sigma_db=round(sigma, 1), dir_gain_db=round(g, 1), power_ch_db=round(p_ch, 1),
                              dist_raw=round(d_inst, 2), dist_cal=bool(have_ref))
                    stab = ("uncertain" if held else
                            "stable" if st["bearing_std"] <= 5 and st["n_window"] >= 5 else
                            "ok" if st["bearing_std"] <= 12 and st["n_window"] >= 3 else "jumpy")
                    conf = float(np.clip((p_now - floor_db) / 40.0, 0.05, 1.0)) * (1.0 if stab in ("stable", "ok") else 0.5)
                    if raw is not None:
                        st["raw_angle"] = round(raw, 1)
                    st["held"] = held
                    emit_detection(angle=round(angle, 1), distance=round(dist, 2), conf=round(conf, 2),
                                   id=t.id, label=f"{t.label}", source="live",
                                   addr=t.id, desc=getattr(t, "desc", ""), power_db=round(p_now, 1),
                                   dist_lo=round(d_lo, 2), dist_hi=round(d_hi, 2), stability=stab,
                                   pkts=t.hits, first_seen=round(t.first, 1), group=t.group,
                                   phone=t.id.startswith("PHONE-"), **st)
                    t.last_emit = now
                    emitted += 1
                    if rec:
                        rec.write(json.dumps({"k": "upd", "t": round(now, 3), "id": t.id, "a": round(angle, 2),
                                              "d": round(dist, 3), "d_lo": round(d_lo, 3), "d_hi": round(d_hi, 3),
                                              "d_raw": round(d_inst, 3), "g": round(g, 2), "pch": round(p_ch, 2), "bs": st["bearing_std"], "n": st["n_window"],
                                              "held": held}) + "\n")

            if now - last_sig >= 0.25:
                last_sig = now
                sig.send(now, floor_db, n_bursts, n_decoded, now - start, args.calibrate)

            if now - last_status >= (1.0 if args.calibrate else 2.0):
                last_status = now
                if args.calibrate:
                    dev, devs = pick_cal_device(cal_rows, args.target)
                    rows = [(p, ph) for p, ph, a, _ in cal_rows if dev and a == dev["addr"]]
                    est = cal_estimate(rows) if rows else None
                    prog = {"t": round(now - start, 1), "dur": args.calibrate, "bursts": n_bursts,
                            "decoded": n_decoded, "floor_db": round(floor_db, 1), "devices": devs[:6],
                            "device": dev}
                    if est:
                        prog.update({k: round(v, 1) if isinstance(v, float) else v for k, v in est.items()})
                    print("CAL_PROGRESS " + json.dumps(prog), flush=True)
                    continue
                live = sorted([t for t in tracks.values() if t.hits >= args.min_hits], key=lambda t: -t.p)
                lag = time.time() - now
                print(f"[{now-start:6.1f}s] floor {floor_db:6.1f} dB  bursts {n_bursts:5d}  decoded {n_decoded:5d}"
                      f" (+{n_known} below CRC limit)  devices {len(live)}"
                      f"{f'  LAGGING {lag:.1f} s' if lag > 1.0 else ''}", flush=True)
                if raw_w:
                    print(raw_w.status(), flush=True)
                if n_clip:
                    print(f"    NOTE: {n_clip} packets hit the ADC limit (a device is very close): "
                          f"lower --gain if that is the one you are measuring", flush=True)
                    n_clip = 0
                for t in live[:10]:
                    dist = float(np.exp(t.dlog)) if getattr(t, "dlog", None) is not None else \
                        dist_from_power(t.p - gain_at(gain_table, t.bearing), ref_p, args.path_loss_exp)
                    st = t.stats()
                    same = f"  ~same as {t.group[-5:]}" if t.group != t.id else ""
                    print(f"    {t.id:17s} {t.label[:22]:22s} p {t.p:6.1f} dB  bearing {t.bearing:+6.1f} "
                          f"+-{st['bearing_std']:4.1f} deg  dist {dist:6.2f} m  {st['rate']:4.1f} pkt/s{same}", flush=True)

    except KeyboardInterrupt:
        print("\n[live] stopped by user")

    if args.calibrate:
        dev, devs = pick_cal_device(cal_rows, args.target)
        rows = [(p, ph) for p, ph, a, _ in cal_rows if dev and a == dev["addr"]]
        est = cal_estimate(rows) if rows else None
        if not est or est["n_bursts"] < 6:
            why = (f"Device {args.target} sent too few packets." if args.target else
                   "No BLE device was decoded often enough. Unlock the phone and keep its screen on, "
                   "move it closer, or try another advertising channel.")
            print("CAL_RESULT " + json.dumps({"ok": False, "n_total": len(cal_rows), "decoded": n_decoded,
                                              "devices": devs[:6], "error": why}))
            return False
        res = dict(est, ok=True, freq=args.freq, gain=args.gain, spacing_m=spacing, antenna=args.antenna, t=time.time(),
                   device=dev["addr"], device_desc=dev["desc"], devices=devs[:6], decoded=n_decoded)
        res["quality"] = "good" if est["phase_spread_deg"] <= 12 else ("ok" if est["phase_spread_deg"] <= 25 else "poor")
        print(f"[cal] {est['n_bursts']} packets from {dev['desc']} {dev['addr']}: phase offset {est['phase_deg']:+.1f} deg "
              f"(spread {est['phase_spread_deg']:.1f}, {res['quality']})  ref power {est['ref_power_db']:.1f} dB")
        print("CAL_RESULT " + json.dumps(res), flush=True)
        if args.save_preset:
            os.makedirs(PRESET_DIR, exist_ok=True)
            res.update(name=args.save_preset, flip=False)
            with open(preset_path(args.save_preset), "w") as f:
                json.dump(res, f, indent=2)
            with open(ACTIVE_FILE, "w") as f:
                json.dump({"name": args.save_preset}, f)
            print(f"[cal] saved preset '{args.save_preset}' (now active)")
        return True

    if raw_w:
        raw_w.close()
    if rec:
        rec.close()
    print(f"[live] done. bursts={n_bursts} decoded={n_decoded} (of which {n_known} below CRC limit) emitted={emitted}")
    return True


if __name__ == "__main__":
    main()
