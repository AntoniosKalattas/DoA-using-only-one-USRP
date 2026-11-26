#!/usr/bin/env python3
"""
Capture Console - a local web UI to set parameters and run the thesis
capture scripts live.

Run:
    python3 capture_console/app.py
    # then open http://127.0.0.1:8099

What it does
------------
- Renders a form from each capture script's REAL command-line parameters
  (defaults taken straight from the scripts).
- On "Start", it launches the chosen script as a subprocess with exactly the
  arguments you set, and streams its stdout/stderr to the browser live.
- "Stop" terminates the running process.

It is stdlib-only (no Flask, no pip installs) and runs the scripts with the
SAME Python interpreter that runs this server. The capture scripts themselves
need the SDR stack (uhd / gnuradio) on the machine you run this on; if that is
missing you will see the import error live in the log, which is the honest
behaviour.
"""

import json
import math
import os
import random
import re
import csv
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Local machine only by default. The Docker image sets CONSOLE_HOST=0.0.0.0 (inside the
# container) and docker-compose publishes the port to the host's 127.0.0.1 only.
HOST, PORT = os.environ.get("CONSOLE_HOST", "127.0.0.1"), int(os.environ.get("CONSOLE_PORT", "8099"))

# Project root = parent of this file's directory (…/Thesis)
ROOT = Path(__file__).resolve().parent.parent

# Live detection feed file: capture code appends one JSON object per line, e.g.
#   {"id":"drone1","label":"DJI Mavic","angle":22.5,"distance":48.0,"conf":0.9}
# The server tails it and pushes detections to the radar page.
FEED_FILE = Path(__file__).resolve().parent / "feed.jsonl"

# Calibration presets for the live B210 tracker (written by the /calibrate wizard).
PRESET_DIR = Path(__file__).resolve().parent / "cal_presets"
ACTIVE_FILE = PRESET_DIR / "_active.json"


def _preset_file(name):
    safe = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Preset name must contain letters or digits")
    return PRESET_DIR / (safe + ".json"), safe


def presets_list():
    out = []
    if PRESET_DIR.exists():
        for f in sorted(PRESET_DIR.glob("*.json")):
            if f.name.startswith("_"):
                continue
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
    active = ""
    try:
        active = json.loads(ACTIVE_FILE.read_text()).get("name", "")
    except (OSError, json.JSONDecodeError):
        pass
    out.sort(key=lambda p: -float(p.get("t", 0)))
    return {"presets": out, "active": active}


def preset_set_active(name):
    PRESET_DIR.mkdir(exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps({"name": name}))


def preset_save(p):
    path, safe = _preset_file(p.get("name", ""))
    keep = ("phase_deg", "ref_power_db", "phase_spread_deg", "n_bursts", "n_total", "quality",
            "freq", "gain", "spacing_m", "flip", "direction_check", "notes", "device", "device_desc", "antenna", "rfa_side",
            "method", "path_loss_exp", "ruler_spacing_m", "bearing_err_median_deg", "bearing_err_rms_deg", "span_deg")
    rec = {k: p[k] for k in keep if k in p}
    for k in ("phase_deg", "ref_power_db", "freq", "gain", "spacing_m"):
        if k not in rec:
            raise ValueError(f"missing field {k}")
    rec.update(name=safe, flip=bool(rec.get("flip", False)), t=time.time(),
               created=datetime.now().strftime("%Y-%m-%d %H:%M"))
    PRESET_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(rec, indent=2))
    preset_set_active(safe)
    return rec


# ----------------------------------------------------------------------------
# Capture profiles: each maps to a real script + its actual CLI parameters.
# "flag" is how the value is passed; None means a leading positional token.
# ----------------------------------------------------------------------------
def P(name, flag, type_, default="", required=False, help=""):
    return {"name": name, "flag": flag, "type": type_,
            "default": default, "required": required, "help": help}


PROFILES = {
    "replay_iq_v3": {
        "label": "ReplayAttack - IQ Capture (v3)",
        "script": "ReplayAttack_IQrecording/ReplayAttack_IQrecording/iq_size_capture_v3.py",
        "subcommand": "capture",   # positional token before the flags
        "params": [
            P("state", "--state", "text", "", True, "Drone state label, e.g. hovering / flight / no_drone"),
            P("duration", "--duration", "number", 60, False, "Total capture duration (seconds)"),
            P("window", "--window", "number", 10, False, "Window size (ms)"),
            P("freq", "--freq", "number", 5.8e9, False, "Center frequency (Hz) - B210 range 70e6..6e9"),
            P("rate", "--rate", "number", 10e6, False, "Sample rate (Hz) - B210 max ~56e6"),
            P("gain", "--gain", "number", 40, False, "RX gain (dB) - B210 range 0..76"),
            P("output", "--output", "text", "", False, "Output directory (blank = script default iq_datasets/)"),
        ],
    },
    "dronetraffic_rf": {
        "label": "DroneTraffic - RF Capture",
        "script": "DroneTrafficAnalysis/DroneTrafficAnalysis/capture/rf_capture.py",
        "subcommand": None,
        "params": [
            P("freq", "--freq", "number", 5.8e9, False, "Center frequency (Hz)"),
            P("sample_rate", "--sample-rate", "number", 10e6, False, "Sample rate (Hz)"),
            P("gain", "--gain", "number", 40, False, "RX gain (dB)"),
            P("output", "--output", "text", "", False, "Output CSV file (blank = timestamped default)"),
        ],
    },
    "dronetraffic_simple": {
        "label": "DroneTraffic - Simple RF Capture",
        "script": "DroneTrafficAnalysis/DroneTrafficAnalysis/capture/rf_capture_simple.py",
        "subcommand": None,
        "params": [
            P("state", "--state", "text", "", True, "State label (required)"),
            P("output", "--output", "text", "capture.csv", True, "Output CSV file (required)"),
            P("duration", "--duration", "number", 180, False, "Duration (seconds)"),
            P("freq", "--freq", "number", 5.8e9, False, "Center frequency (Hz)"),
            P("sample_rate", "--sample-rate", "number", 10e6, False, "Sample rate (Hz)"),
            P("gain", "--gain", "number", 40, False, "RX gain (dB)"),
        ],
    },
    "music_doa_synthetic": {
        "label": "MUSIC - DoA (synthetic validation)",
        "script": "DroneTrafficAnalysis/DroneTrafficAnalysis/capture/music.py",
        "subcommand": "synthetic",
        "params": [
            P("antennas", "--antennas", "number", 2, False, "Array elements M (B210 has 2 RX channels)"),
            P("angle", "--angle", "number", 20, False, "True source angle (deg), -90..90"),
            P("snapshots", "--snapshots", "number", 1000, False, "Number of snapshots N"),
            P("snr", "--snr", "number", 10, False, "SNR (dB)"),
            P("sources", "--sources", "number", 1, False, "Number of sources / drones (<= M-1)"),
            P("wavelength", "--wavelength", "number", 0.0517, False, "Wavelength m (0.0517 = 5.8 GHz)"),
            P("spacing", "--spacing", "number", 0.02585, False, "Element spacing d m (lambda/2 = 0.02585)"),
            P("seed", "--seed", "number", "", False, "RNG seed (blank = random)"),
        ],
    },
    "live_radar": {
        "label": "Live Capture -> Radar (B210, real SDR)",
        "script": "capture_console/live_radar_capture.py",
        "subcommand": None,
        "params": [
            P("preset", "--preset", "text", "", False, "Calibration preset (blank = active one, none = off). Make one at /calibrate"),
            P("freq", "--freq", "number", "", False, "Center Hz (blank = preset or 2.480e9). BLE adv 2.402e9/2.426e9/2.480e9"),
            P("rate", "--rate", "number", 4e6, False, "Sample rate Hz"),
            P("gain", "--gain", "number", "", False, "RX gain dB (blank = preset or 50) - B210 0..76"),
            P("window", "--window", "number", 20, False, "Block length ms"),
            P("duration", "--duration", "number", 0, False, "Seconds to run (0 = until stopped)"),
            P("calibrate", "--calibrate", "number", "", False, "Calibration measurement N s (easier: the /calibrate page)"),
            P("target", "--target", "text", "", False, "Calibration: only use this advertiser (PHONE-<tag> = USRP Beacon app)"),
            P("dump_packets", "--dump-packets", "text", "", False, "Calibration: log every target packet to this file (used by the phone's sweep / AR calibration)"),
            P("spacing", "--spacing", "number", "", False, "Antenna spacing m (blank = preset or lambda/2)"),
            P("snr_db", "--snr-db", "number", 12, False, "Burst threshold above noise floor dB"),
            P("max_us", "--max-us", "number", 600, False, "Max burst length us (BLE adv <= ~380)"),
            P("min_hits", "--min-hits", "number", 2, False, "Packets before a track is shown"),
            P("update_s", "--update-s", "number", 2, False, "Radar update period s (median of packets per window; 0.3 = fast/jittery)"),
            P("hold_spread", "--hold-spread", "number", 10, False, "Hold last good bearing when packets disagree more than this (deg)"),
            P("device_args", "--device-args", "text", "", False, "UHD device args, e.g. serial=321E86A"),
            P("antenna", "--antenna", "text", "", False, "RX port with the antennas: TX/RX or RX2 (blank = preset or TX/RX)"),
            P("only", "--only", "text", "", False, "Only show devices whose label/address contains this, e.g. Apple"),
            P("label", "--label", "text", "BLE", False, "Label prefix for anonymous tracks"),
            P("no_record", "--no-record", "switch", "", False, "yes = don't save anything to recordings/ (default: save every packet's DoA)"),
            P("save_iq", "--save-iq", "switch", "", False, "yes = also save each packet's raw 2-channel IQ to the recording (iq.c64)"),
            P("save_raw", "--save-raw", "switch", "", False, "yes = save the COMPLETE raw 2-channel stream, lossless (raw.sc16, 32 MB/s)"),
            P("hop", "--hop", "text", "37,38,39", False, "BLE adv channels to cycle through (none = stay on one frequency)"),
            P("dwell_ms", "--dwell-ms", "number", 300, False, "Time on each hop channel, ms"),
            P("mount_deg", "--mount-deg", "number", "", False, "Antenna pair turned this many deg right of the site 0 (blank = preset)"),
            P("dist_tau", "--dist-tau", "number", 4, False, "Distance smoothing time constant s"),
        ],
    },
    "doa_compare": {
        "label": "DoA vs phone GPS - compare + tune calibration",
        "script": "capture_console/doa_compare.py",
        "subcommand": None,
        "params": [
            P("recording", None, "text", "", False, "Blank = newest live recording, 'all' = combine every recording, "
              "or a folder name from capture_console/recordings/ (e.g. 20261118-101500). Stop live tracking first."),
            P("save_preset", "--save-preset", "text", "outdoor-gps", False,
              "Save the fitted calibration under this name (blank = only compare, save nothing)"),
            P("activate", "--activate", "text", "yes", False, "yes = make the saved preset active (Start live then uses it)"),
            P("tag", "--tag", "number", 1, False, "The app's tag number"),
            P("max_truth_err", "--max-truth-err", "number", 12, False,
              "Only use packets whose GPS direction is known within this many degrees"),
            P("min_range", "--min-range", "number", 8, False, "Ignore packets closer than this (m)"),
            P("facing", "--facing", "number", "", False, "Override the antennas' facing (deg true north; blank = the one "
              "recorded with the app)"),
            P("force", "--force", "text", "", False, "Leave blank. Save even an unusable fit: type yes"),
        ],
    },
    "radar_replay": {
        "label": "Radar Replay (captured CSV -> Live Radar)",
        "script": "capture_console/radar_replay.py",
        "subcommand": None,
        "params": [
            P("csv", "--csv", "text", "", True, "Path to a captured feature CSV (has est_distance_m)"),
            P("speed", "--speed", "number", 5, False, "Playback speed multiplier"),
            P("distance_scale", "--distance-scale", "number", 1, False, "Scale distance for visibility (uncalibrated data)"),
            P("angle", "--angle", "number", 0, False, "Fixed bearing deg (no DoA in single-channel data)"),
            P("label", "--label", "text", "", False, "Track label (blank = state column)"),
        ],
    },
    "music_doa_sweep": {
        "label": "MUSIC - DoA (accuracy sweep)",
        "script": "DroneTrafficAnalysis/DroneTrafficAnalysis/capture/music.py",
        "subcommand": "sweep",
        "params": [
            P("antennas", "--antennas", "number", 2, False, "Array elements M (B210 has 2 RX channels)"),
            P("start", "--start", "number", -60, False, "First true angle (deg)"),
            P("stop", "--stop", "number", 60, False, "Last true angle (deg)"),
            P("step", "--step", "number", 10, False, "Angle step (deg)"),
            P("trials", "--trials", "number", 20, False, "Noise realisations per angle"),
            P("snapshots", "--snapshots", "number", 1000, False, "Number of snapshots N"),
            P("snr", "--snr", "number", 10, False, "SNR (dB)"),
            P("sources", "--sources", "number", 1, False, "Number of sources / drones (<= M-1)"),
            P("wavelength", "--wavelength", "number", 0.0517, False, "Wavelength m (0.0517 = 5.8 GHz)"),
            P("spacing", "--spacing", "number", 0.02585, False, "Element spacing d m (lambda/2 = 0.02585)"),
            P("seed", "--seed", "number", "", False, "Base RNG seed (blank = random)"),
        ],
    },
    "music_doa_file": {
        "label": "MUSIC - DoA (from .npy array IQ)",
        "script": "DroneTrafficAnalysis/DroneTrafficAnalysis/capture/music.py",
        "subcommand": "file",
        "params": [
            P("input", "--input", "text", "", True, ".npy of shape (N_snapshots, M_antennas), complex"),
            P("sources", "--sources", "number", 1, False, "Number of sources / drones (<= M-1)"),
            P("wavelength", "--wavelength", "number", 0.0517, False, "Wavelength m (0.0517 = 5.8 GHz)"),
            P("spacing", "--spacing", "number", 0.02585, False, "Element spacing d m (lambda/2 = 0.02585)"),
        ],
    },
}


# ----------------------------------------------------------------------------
# Job manager: at most one capture process at a time, with a live log buffer.
# ----------------------------------------------------------------------------
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
LIVE_CTL = Path(__file__).resolve().parent / "live_ctl.json"
LIVE_CTL_RESULT = Path(__file__).resolve().parent / "live_ctl_result.json"


def live_command(cmd, device="PHONE", **extra):
    """Hand a command to the running live_radar_capture.py (it checks twice a second)."""
    if not (JOB.running and "live_radar_capture.py" in (JOB.cmd_str or "") and "--calibrate" not in JOB.cmd_str):
        raise ValueError("live tracking isn't running - press Start live first")
    cid = f"{time.time():.3f}"
    tmp = LIVE_CTL.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(extra, id=cid, cmd=cmd, device=device)))
    tmp.replace(LIVE_CTL)
    for _ in range(40):                       # wait up to ~4 s for the answer
        time.sleep(0.1)
        try:
            r = json.loads(LIVE_CTL_RESULT.read_text())
            if r.get("id") == cid:
                return r
        except (OSError, ValueError):
            pass
    return {"ok": False, "error": "the live script didn't answer (is it still receiving?)"}
RAW_FILES = ("raw.sc16", "raw.c64", "raw_blocks.csv")      # the full raw stream; everything else is kept


def raw_list():
    """Recordings that still hold a full raw IQ stream, newest first."""
    out = []
    busy = _busy_recording()
    if RECORDINGS_DIR.is_dir():
        for d in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
            files = [d / f for f in RAW_FILES if (d / f).exists()]
            if not d.is_dir() or not files:
                continue
            out.append({"name": d.name, "bytes": sum(f.stat().st_size for f in files),
                        "seconds": round((d / "raw.sc16").stat().st_size / 8 / 4e6, 1) if (d / "raw.sc16").exists() else None,
                        "busy": d.name == busy})
    import shutil
    return {"items": out, "free_bytes": shutil.disk_usage(RECORDINGS_DIR if RECORDINGS_DIR.is_dir() else ".").free}


def _busy_recording():
    """Name of the recording a running live capture is writing (the newest folder), else None."""
    if not (JOB.running and "live_radar_capture.py" in (JOB.cmd_str or "")):
        return None
    dirs = sorted(d.name for d in RECORDINGS_DIR.iterdir() if d.is_dir()) if RECORDINGS_DIR.is_dir() else []
    return dirs[-1] if dirs else None


def raw_delete(name):
    """Delete ONLY the full raw stream of one recording (doa.jsonl, meta.json, packet IQ are kept)."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", name or ""):
        raise ValueError("bad recording name")
    if name == _busy_recording():
        raise ValueError("this recording is still being written - press Stop first")
    d = RECORDINGS_DIR / name
    freed = 0
    for f in RAW_FILES:
        p = d / f
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
    m = d / "raw_meta.json"
    if m.exists():
        try:
            meta = json.loads(m.read_text()); meta["deleted"] = time.strftime("%Y-%m-%d %H:%M:%S")
            m.write_text(json.dumps(meta, indent=2))
        except (OSError, ValueError):
            pass
    print(f"[raw] deleted raw IQ of {name}: {freed/1e9:.2f} GB freed", flush=True)
    return {"ok": True, "freed": freed}


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.lines = deque(maxlen=5000)   # (index, text)
        self.counter = 0
        self.running = False
        self.returncode = None
        self.cmd_str = ""
        self.started_at = None

    def _emit(self, text):
        with self.lock:
            self.lines.append((self.counter, text))
            self.counter += 1

    def start(self, profile_key, values):
        with self.lock:
            if self.running:
                return False, "A capture is already running. Stop it first."

        prof = PROFILES.get(profile_key)
        if not prof:
            return False, f"Unknown profile: {profile_key}"

        script = (ROOT / prof["script"]).resolve()
        if not script.exists():
            return False, f"Script not found: {script}"

        # Build argv from the profile's real parameters.
        argv = [sys.executable, "-u", str(script)]
        if prof["subcommand"]:
            argv.append(prof["subcommand"])

        for p in prof["params"]:
            raw = str(values.get(p["name"], "")).strip()
            if p["type"] == "switch":          # on/off flag with no value
                if raw.lower() in ("1", "yes", "true", "on"):
                    argv.append(p["flag"])
                continue
            if raw == "":
                if p["required"]:
                    return False, f"Missing required parameter: {p['name']}"
                continue  # let the script use its own default
            if p["flag"] is None:
                argv.append(raw)
            else:
                argv.extend([p["flag"], raw])

        # Reset state for the new run.
        with self.lock:
            self.lines.clear()
            self.counter = 0
            self.returncode = None
            self.running = True
            self.cmd_str = " ".join(shlex.quote(a) for a in argv)
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._emit(f"$ {self.cmd_str}")
        self._emit(f"[console] cwd = {script.parent}")
        self._emit(f"[console] started {self.started_at}")
        self._emit("")

        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(script.parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                start_new_session=True,   # own process group: Stop also kills helpers (b210_stream)
            )
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.running = False
            self._emit(f"[console] FAILED to launch: {e}")
            return False, str(e)

        threading.Thread(target=self._reader, daemon=True).start()
        return True, "started"

    def _reader(self):
        for line in self.proc.stdout:
            self._emit(line.rstrip("\n"))
        self.proc.wait()
        with self.lock:
            self.returncode = self.proc.returncode
            self.running = False
        self._emit("")
        self._emit(f"[console] process exited with code {self.returncode}")

    def _signal_group(self, p, sig):
        try:
            os.killpg(p.pid, sig)          # the script AND anything it launched (e.g. b210_stream)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self):
        p = self.proc
        if p and (p.poll() is None or self.running):
            self._emit("[console] stopping (SIGTERM)...")
            self._signal_group(p, signal.SIGTERM)
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._emit("[console] force killing (SIGKILL)...")
                self._signal_group(p, signal.SIGKILL)
                p.wait(timeout=3)
            # helpers that ignored SIGTERM (or were still releasing the USB device)
            time.sleep(0.5)
            self._signal_group(p, signal.SIGKILL)
            return True, "stopped"
        return False, "nothing running"

    def status(self, since):
        with self.lock:
            new = [{"i": i, "t": t} for (i, t) in self.lines if i >= since]
            return {
                "running": self.running,
                "returncode": self.returncode,
                "cmd": self.cmd_str,
                "started_at": self.started_at,
                "next": self.counter,
                "lines": new,
            }


JOB = Job()


# ----------------------------------------------------------------------------
# Live detection feed (drives the radar page)
# ----------------------------------------------------------------------------
class Feed:
    """In-memory ring buffer of drone detections.
    A detection is {id, label, angle(deg -90..90), distance(m), conf, source, t}."""
    def __init__(self):
        self.lock = threading.Lock()
        self.items = deque(maxlen=5000)
        self.counter = 0

    def add(self, d):
        try:
            rec = {
                "id": str(d.get("id", d.get("label", "live"))),
                "label": str(d.get("label", d.get("id", "drone"))),
                "angle": float(d["angle"]),
                "distance": float(d["distance"]),
                "conf": float(d.get("conf", 1.0)),
                "source": str(d.get("source", "live")),
                "t": float(d.get("t", time.time())),
            }
        except (KeyError, TypeError, ValueError):
            return False
        for k in ("addr", "desc", "power_db", "dist_lo", "dist_hi", "stability", "pkts", "first_seen",
                  "group", "bearing_std", "rate", "n_window", "p_lo", "p_hi", "phone", "held", "raw_angle"):
            if k in d:
                rec[k] = d[k]
        rec["angle"] = max(-90.0, min(90.0, rec["angle"]))
        rec["distance"] = max(0.0, rec["distance"])
        with self.lock:
            rec["i"] = self.counter
            self.counter += 1
            self.items.append(rec)
        return True

    def since(self, i):
        with self.lock:
            return {"next": self.counter,
                    "items": [r for r in self.items if r["i"] >= i]}


FEED = Feed()


class Sim:
    """Generates smoothly-moving fake drone tracks so the radar is usable with
    no SDR attached. Each track is a forward-half-plane bearing + varying range."""
    def __init__(self, feed):
        self.feed = feed
        self.running = False
        self.thread = None

    def start(self, n_drones=2, max_range=100.0):
        if self.running:
            return False
        self.running = True
        self.thread = threading.Thread(
            target=self._run, args=(int(n_drones), float(max_range)), daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False

    def _run(self, n, max_range):
        rng = random.Random()
        drones = []
        for k in range(max(1, n)):
            drones.append({
                "id": f"sim-{k+1}",
                "label": rng.choice(["DJI Mavic 3", "DJI Mini 4", "DJI Air 3"]),
                "a0": rng.uniform(-55, 55), "aamp": rng.uniform(8, 28),
                "aw": rng.uniform(0.05, 0.22), "aph": rng.uniform(0, 6.28),
                "d0": rng.uniform(20, max(25, max_range * 0.8)),
                "damp": rng.uniform(5, 18), "dw": rng.uniform(0.03, 0.1),
            })
        t0 = time.time()
        while self.running:
            tt = time.time() - t0
            for d in drones:
                angle = d["a0"] + d["aamp"] * math.sin(d["aw"] * tt + d["aph"])
                dist = d["d0"] + d["damp"] * math.sin(d["dw"] * tt)
                self.feed.add({"id": d["id"], "label": d["label"],
                               "angle": round(angle, 1), "distance": round(max(1, dist), 1),
                               "conf": round(rng.uniform(0.75, 0.97), 2),
                               "source": "sim"})
            time.sleep(0.3)


SIM = Sim(FEED)


# ----------------------------------------------------------------------------
# Phone ground truth (USRP Beacon Android app, UDP :5005) and the raw USRP
# signal view (live_radar_capture.py, UDP 127.0.0.1:5006)
# ----------------------------------------------------------------------------
PHONE_PORT, SIGNAL_PORT = 5005, 5006
PHONE_HTTP_PORT = 5080     # phone-only HTTP ingest (safe to expose with `ngrok http 5080`)
PHONE_LOG_DIR = Path(__file__).resolve().parent / "phone_logs"
PHONE_LOG_FIELDS = ["recv_time", "src", "t_ms", "tag", "adv_running", "adv_on", "interval_ms", "tx_dbm",
                    "lat", "lon", "alt", "acc_m", "gps_age_s", "heading_deg",
                    "mark", "truth_bearing_deg", "truth_range_m",
                    "fix_ms", "provider", "speed_mps", "course_deg", "vacc_m", "sats_used", "sats_seen",
                    "heading_mag_deg", "heading_acc_deg", "pitch_deg", "roll_deg", "mag_field_ut", "mag_status",
                    "pressure_hpa", "sid", "seq", "uploaded"]
SITE_FILE = Path(__file__).resolve().parent / "usrp_site.json"
SITE_HISTORY = Path(__file__).resolve().parent / "usrp_site_history.jsonl"


class UsrpSite:
    """Where the USRP is, recorded from the phone app:
    usrp_site   = phone lying on top of the USRP, GPS averaged for ~30 s  -> lat/lon/alt
    usrp_facing = phone held level, top edge pointing where the antennas face -> facing_deg (true north)
    Saved to usrp_site.json (live_radar_capture.py copies it into every recording) and appended to
    usrp_site_history.jsonl so old measurements are never lost."""
    POS_KEYS = ("lat", "lon", "alt", "acc_m", "vacc_m", "n_fixes", "spread_m", "duration_s", "sats_used")
    FACE_KEYS = ("facing_deg", "facing_spread_deg", "mag_field_ut", "mag_status", "n_samples")

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.site = json.loads(SITE_FILE.read_text())
        except (OSError, ValueError):
            self.site = {}

    def update(self, msg, src):
        kind = msg.get("type")
        keys = self.POS_KEYS if kind == "usrp_site" else self.FACE_KEYS
        vals = {k: msg[k] for k in keys if msg.get(k) is not None}
        if kind == "usrp_site" and not all(isinstance(vals.get(k), (int, float)) for k in ("lat", "lon")):
            raise ValueError("usrp_site needs lat and lon")
        if kind == "usrp_facing" and not isinstance(vals.get("facing_deg"), (int, float)):
            raise ValueError("usrp_facing needs facing_deg")
        now = time.time()
        # When it was MEASURED: the phone's t_ms (phones run NTP time), so a measurement that sat in the
        # phone's offline outbox keeps its real time. Fall back to now if t_ms is missing or nonsense.
        t_ms = msg.get("t_ms")
        meas = t_ms / 1000.0 if isinstance(t_ms, (int, float)) and abs(t_ms / 1000.0 - now) < 30 * 86400 else now
        tkey = "pos_time" if kind == "usrp_site" else "facing_time"
        with self.lock:
            vals[tkey] = meas
            if msg.get("id") and msg.get("id") == self.site.get("last_id"):
                return                                   # the app sends each one 3x over UDP
            newer = meas >= float(self.site.get(tkey) or 0) - 1.0
            if newer:                                    # an older one arriving late only goes to the history
                self.site.update(vals, last_id=msg.get("id"), src=src)
            try:
                if newer:
                    tmp = SITE_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.site, indent=2))
                    tmp.replace(SITE_FILE)
                with open(SITE_HISTORY, "a") as f:
                    f.write(json.dumps(dict(msg, recv_time=now, src=src)) + "\n")
            except OSError as e:                         # never let a disk error kill the phone listener
                print(f"[site] could not save: {e}", flush=True)
        if not newer:
            print(f"[site] {kind} from {src} is older than the current one: kept in history only", flush=True)
            return
        print(f"[site] {kind} saved: " + ", ".join(f"{k}={v}" for k, v in vals.items()), flush=True)

    def get(self):
        with self.lock:
            return dict(self.site)


SITE = UsrpSite()


class PhoneLink:
    """Latest telemetry from the phone + adv_on history (for the blink timeline).
    Every packet is also logged to phone_logs/phone_<start>.csv."""
    def __init__(self):
        self.lock = threading.Lock()
        self.latest, self.recv, self.src = None, 0.0, None
        self.hist = deque(maxlen=3000)          # (recv_time, adv_on) ~10 min at 5 Hz
        self.marks = deque(maxlen=200)          # (recv_time, mark#, bearing, range)
        self.error = None
        self._csv = None
        self.seen = {}                          # sid -> seqs already logged (live or backlog)
        self.clock = deque(maxlen=100)          # Mac time - phone time, from live messages

    def _seen_set(self, sid):
        """seqs of this phone session already in the logs. First use of an sid after a console
        restart scans recent logs, so a backlog never duplicates what arrived live."""
        if sid in self.seen:
            return self.seen[sid]
        got = set()
        cutoff = time.time() - 7 * 86400
        for fn in PHONE_LOG_DIR.glob("phone_*.jsonl"):
            try:
                if fn.stat().st_mtime < cutoff:
                    continue
                with open(fn) as f:
                    for line in f:
                        if sid in line:
                            try:
                                m = json.loads(line)
                            except ValueError:
                                continue
                            if m.get("sid") == sid and m.get("seq") is not None:
                                got.add(m["seq"])
            except OSError:
                continue
        if len(self.seen) > 50:                 # forget the oldest sessions
            self.seen.pop(next(iter(self.seen)))
        self.seen[sid] = got
        return got

    def _mark_seen(self, msg):
        """True if this (sid, seq) was already logged. Messages from apps before 1.4 have no sid."""
        sid, seq = msg.get("sid"), msg.get("seq")
        if not isinstance(sid, str) or not isinstance(seq, int):
            return False
        s = self._seen_set(sid)
        if seq in s:
            return True
        s.add(seq)
        return False

    def backlog(self, items, src):
        """Messages the phone saved while it couldn't reach this laptop, uploaded later.
        They go into the phone logs (for doa_compare.py) with recv_time = when the phone made them,
        on this Mac's clock. They don't touch the live view."""
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        now = time.time()
        with self.lock:
            off = sorted(self.clock)[len(self.clock) // 2] if self.clock else 0.0
        stored = dup = bad = 0
        for m in items:
            if not isinstance(m, dict):
                bad += 1
                continue
            with self.lock:
                if self._mark_seen(m):
                    dup += 1
                    continue
            if m.get("type") in ("usrp_site", "usrp_facing"):
                try:
                    SITE.update(m, src + " (backlog)")
                    stored += 1
                except ValueError:
                    bad += 1
                continue
            if m.get("type") != "usrp_beacon" or not isinstance(m.get("t_ms"), (int, float)):
                bad += 1
                continue
            rt = m["t_ms"] / 1000.0 + off
            with self.lock:
                try:
                    self._log(dict(m, recv_time=f"{rt:.3f}", src=src + " (backlog)", uploaded=f"{now:.3f}"))
                    stored += 1
                except OSError:
                    bad += 1
        if stored:
            print(f"[phone] backlog from {src}: {stored} stored, {dup} already had, {bad} bad", flush=True)
        return {"ok": True, "stored": stored, "dup": dup, "bad": bad}

    def _log(self, msg):
        """CSV (main columns, for a spreadsheet) + JSONL (every field the app sends, for doa_compare.py)."""
        if self._csv is None:
            PHONE_LOG_DIR.mkdir(exist_ok=True)
            base = PHONE_LOG_DIR / f"phone_{datetime.now():%Y%m%d-%H%M%S}"
            fh = open(base.with_suffix(".csv"), "w", newline="")
            self._csv = (fh, csv.DictWriter(fh, fieldnames=PHONE_LOG_FIELDS, extrasaction="ignore"),
                         open(base.with_suffix(".jsonl"), "a", buffering=1))
            self._csv[1].writeheader()
        self._csv[1].writerow(msg)
        self._csv[0].flush()
        self._csv[2].write(json.dumps(msg) + "\n")

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("0.0.0.0", PHONE_PORT))
        except OSError as e:
            self.error = f"cannot listen on UDP {PHONE_PORT}: {e}"
            print("[phone] " + self.error)
            return
        while True:
            data, addr = sock.recvfrom(8192)
            try:
                self.ingest(json.loads(data.decode()), addr[0])
            except ValueError:
                continue

    def ingest(self, msg, src):
        """One telemetry message from the phone (UDP on the LAN, or HTTP via ngrok)."""
        if isinstance(msg, dict) and msg.get("type") in ("usrp_site", "usrp_facing"):
            with self.lock:
                self._mark_seen(msg)
            return SITE.update(msg, src)
        if not isinstance(msg, dict) or msg.get("type") != "usrp_beacon":
            raise ValueError("not a usrp_beacon message")
        now = time.time()
        with self.lock:
            if isinstance(msg.get("t_ms"), (int, float)):
                self.clock.append(now - msg["t_ms"] / 1000.0)
            if self._mark_seen(msg):
                return                           # a copy the backlog already delivered
            prev = self.latest
            self.latest, self.recv, self.src = msg, now, src
            self.hist.append((now, bool(msg.get("adv_on"))))
            if msg.get("mark") and (prev is None or prev.get("mark") != msg.get("mark")):
                self.marks.append((now, msg["mark"], msg.get("truth_bearing_deg"), msg.get("truth_range_m")))
            try:
                self._log(dict(msg, recv_time=f"{now:.3f}", src=src))
            except OSError:
                pass

    def state(self, since):
        with self.lock:
            now = time.time()
            return {"error": self.error, "port": PHONE_PORT,
                    "latest": self.latest, "age": (now - self.recv) if self.latest else None, "src": self.src,
                    "hist": [h for h in self.hist if h[0] > since],
                    "marks": list(self.marks), "site": SITE.get()}


class SignalLink:
    """Latest spectrum/levels + recent bursts from live_radar_capture.py."""
    def __init__(self):
        self.lock = threading.Lock()
        self.latest, self.recv = None, 0.0
        self.bursts = deque(maxlen=20000)

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", SIGNAL_PORT))
        except OSError as e:
            print(f"[signal] cannot listen on UDP {SIGNAL_PORT}: {e}")
            return
        while True:
            data, _ = sock.recvfrom(65535)
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            with self.lock:
                self.bursts.extend(msg.pop("bursts", []))
                self.latest, self.recv = msg, time.time()

    def state(self, since):
        with self.lock:
            return {"latest": self.latest, "age": (time.time() - self.recv) if self.latest else None,
                    "bursts": [b for b in self.bursts if b["t"] > since]}


PHONE = PhoneLink()


# ----------------------------------------------------------------------------
# B210 USB link (shown on /radar and in the phone app)
# ----------------------------------------------------------------------------
USB_SPEEDS = {   # macOS system_profiler device_speed -> (label, rate, ok for 2-ch streaming)
    "low_speed": ("USB 1.0", "1.5 Mb/s", False), "full_speed": ("USB 1.1", "12 Mb/s", False),
    "high_speed": ("USB 2.0", "480 Mb/s", False), "super_speed": ("USB 3.0", "5 Gb/s", True),
    "super_speed_plus": ("USB 3.1+", "10 Gb/s", True),
}


class UsbInfo:
    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"connected": None, "checked": 0}

    def _scan(self):
        if sys.platform.startswith("linux"):
            return self._scan_linux()
        out = subprocess.run(["system_profiler", "SPUSBDataType", "-json"],
                             capture_output=True, text=True, timeout=20).stdout
        found = []

        def walk(items):
            for it in items or []:
                if str(it.get("vendor_id", "")).lower().startswith("0x2500"):
                    found.append(it)
                walk(it.get("_items"))
        walk(json.loads(out or "{}").get("SPUSBDataType"))
        if not found:
            return {"connected": False}
        d = found[0]
        label, rate, ok = USB_SPEEDS.get(d.get("device_speed", ""), (d.get("device_speed", "?"), "", None))
        return {"connected": True, "product": d.get("_name", "USRP"), "serial": d.get("serial_num", ""),
                "speed": d.get("device_speed", ""), "usb": label, "rate": rate, "fast_enough": ok}

    @staticmethod
    def _scan_linux():
        """Linux / Docker: read sysfs (needs the B210 passed into the container)."""
        speeds = {"1.5": "low_speed", "12": "full_speed", "480": "high_speed",
                  "5000": "super_speed", "10000": "super_speed_plus", "20000": "super_speed_plus"}
        for dev in Path("/sys/bus/usb/devices").glob("*"):
            try:
                if (dev / "idVendor").read_text().strip().lower() != "2500":
                    continue
                rd = lambda n: (dev / n).read_text().strip() if (dev / n).exists() else ""
                sp = speeds.get(rd("speed"), rd("speed"))
                label, rate, ok = USB_SPEEDS.get(sp, (sp, "", None))
                return {"connected": True, "product": rd("product") or "USRP", "serial": rd("serial"),
                        "speed": sp, "usb": label, "rate": rate, "fast_enough": ok}
            except OSError:
                continue
        return {"connected": False}

    def run(self):
        while True:
            try:
                info = self._scan()
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                info = {"connected": None, "error": str(e)}
            # Bootloader state ("WestBridge", before UHD loads firmware) always enumerates at
            # USB 2 speed, even on a USB 3 port, so the speed isn't meaningful yet.
            if info.get("connected") and "westbridge" in str(info.get("product", "")).lower():
                info.update(fast_enough=None, booting=True)
                # A freshly plugged-in B210 stays in this state until some UHD program loads its
                # firmware. Do that here so the page shows the real device a few seconds later.
                if not JOB.running and time.time() - getattr(self, "_fw_try", 0) > 30:
                    self._fw_try = time.time()
                    try:
                        subprocess.run(["uhd_find_devices"], capture_output=True, timeout=40)
                    except (OSError, subprocess.SubprocessError):
                        pass
            # UHD itself says which bus it negotiated once streaming starts
            with JOB.lock:
                uhd = [t for _, t in JOB.lines if "Operating over USB" in t]
            if uhd and JOB.running:
                info["uhd"] = uhd[-1].split("]")[-1].strip()
            info["checked"] = time.time()
            with self.lock:
                self.info = info
            time.sleep(5)

    def get(self):
        with self.lock:
            return dict(self.info)


USB = UsbInfo()


# ----------------------------------------------------------------------------
# ngrok public link for the phone (read from ngrok's local API)
# Mac: ngrok runs on this machine (127.0.0.1:4040). Docker: the "ngrok" service (ngrok:4040).
# ----------------------------------------------------------------------------
NGROK_APIS = [a for a in os.environ.get("NGROK_API", "http://127.0.0.1:4040,http://ngrok:4040").split(",") if a]


class NgrokInfo:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache, self.t = {"url": None}, 0.0

    def _fetch(self):
        import urllib.request
        for api in NGROK_APIS:
            try:
                with urllib.request.urlopen(api.rstrip("/") + "/api/tunnels", timeout=1.5) as r:
                    tunnels = json.load(r).get("tunnels", [])
            except (OSError, ValueError):
                continue
            # prefer the tunnel that points at the phone ingest port
            tunnels.sort(key=lambda t: (str(PHONE_HTTP_PORT) not in str(t.get("config", {}).get("addr", "")),
                                        not str(t.get("public_url", "")).startswith("https")))
            if tunnels:
                t = tunnels[0]
                addr = str(t.get("config", {}).get("addr", ""))
                return {"url": t.get("public_url"), "target": addr,
                        "phone_port_ok": str(PHONE_HTTP_PORT) in addr}
            return {"url": None, "note": "ngrok is running but has no tunnel"}
        return {"url": None, "note": "ngrok not running"}

    def get(self):
        with self.lock:
            if time.time() - self.t > 5:
                self.cache, self.t = self._fetch(), time.time()
            return dict(self.cache)


NGROK = NgrokInfo()


# ----------------------------------------------------------------------------
# Calibration driven from the phone (USRP Beacon app -> /cal on the ingest port)
# Manual: the same two measurements as the /calibrate wizard:
#   front: phone 1 m straight ahead -> RX phase offset + 1 m reference power
#   side : phone ~45 deg to the right -> checks left/right isn't mirrored (flip)
# Automatic (the app picks AR when the phone supports ARCore, else the sweep):
#   sweep: phone carried once in an arc from the left side to the right side
#   ar   : phone reports its ARCore position while it is walked around freely
#   Both record every packet of the phone (live_radar_capture.py --dump-packets) and
#   cal_fit.py turns the recording into offset + effective spacing + flip (+ path loss for AR).
# ----------------------------------------------------------------------------
AUTO_MAX_S = 180.0
AUTO_PACKETS = Path(__file__).resolve().parent / "cal_last_packets.jsonl"
AUTO_POSES = Path(__file__).resolve().parent / "cal_last_poses.json"
ADV_FREQS = {37: 2.402e9, 38: 2.426e9, 39: 2.480e9}


class PhoneCal:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.stage, self.kind = "idle", None
        self.progress, self.front, self.side, self.saved, self.error = None, None, None, None, None
        self.cfg, self.tail = {}, []
        self.auto = None             # {"kind", "t_begin", "t_end", "origin", "poses": [[t,x,y,z]]}

    def _cfg_from(self, body):
        d = self.defaults()
        ch = int(body.get("channel", 39))
        cfg = {"tag": int(body.get("tag", 1)) & 0xFF,
               "name": str(body.get("name", "")).strip() or f"phone-{datetime.now():%m%d-%H%M}",
               "freq": ADV_FREQS.get(ch, 2.480e9), "channel": ch if ch in ADV_FREQS else 39,
               "gain": max(0.0, min(76.0, float(body.get("gain", d["gain"])))),
               "spacing_m": max(0.01, min(0.30, float(body.get("spacing_cm", d["spacing_m"] * 100)) / 100.0)),
               "seconds": max(3.0, min(30.0, float(body.get("seconds", 10)))),
               "antenna": d["antenna"], "rfa_side": body.get("rfa_side") if body.get("rfa_side") in ("left", "right") else d["rfa_side"]}
        _preset_file(cfg["name"])          # validates the name
        return cfg

    def defaults(self):
        act = presets_list()
        cur = next((p for p in act["presets"] if p.get("name") == act["active"]), {})
        return {"freq": cur.get("freq", 2.480e9), "gain": cur.get("gain", 50.0),
                "spacing_m": cur.get("spacing_m") or 0.06, "antenna": cur.get("antenna", "TX/RX"),
                "rfa_side": cur.get("rfa_side", "left"), "active": act["active"]}

    def state(self):
        with self.lock:
            busy = JOB.running
            kind = ("calibration" if "--calibrate" in JOB.cmd_str else "live tracking"
                    if "live_radar_capture.py" in JOB.cmd_str else "another capture") if busy else None
            au = self.auto
            auto = None if not au else {"kind": au["kind"], "poses": len(au["poses"]), "origin": au["origin"] is not None,
                                        "begun": au["t_begin"] is not None,
                                        "elapsed": round(time.time() - au["t_begin"], 1) if au["t_begin"] else 0.0}
            return {"now": time.time(), "auto": auto,
                    "stage": self.stage, "measuring": self.kind, "progress": self.progress,
                    "front": self.front, "side": self.side, "saved": self.saved, "error": self.error,
                    "tail": self.tail[-6:], "busy": busy, "busy_kind": kind,
                    "cfg": self.cfg, "defaults": self.defaults(), "usb": USB.get()}

    def command(self, body):
        a = str(body.get("action", ""))
        if a in ("front", "side"):
            return self._measure(a, body)
        if a == "auto_start":
            return self._auto_start(body)
        if a == "auto_poses":                      # AR: [[t (console clock, s), x, y, z], ...]
            with self.lock:
                if self.auto and self.auto["kind"] == "ar" and len(self.auto["poses"]) < 20000:
                    for q in body.get("poses", [])[:600]:
                        self.auto["poses"].append([float(q[0]), float(q[1]), float(q[2]), float(q[3])])
                    if body.get("origin"):
                        self.auto["origin"] = [float(v) for v in body["origin"][:3]]
            return {"ok": True}
        if a == "auto_begin":                      # the walk starts now
            with self.lock:
                if not self.auto:
                    raise ValueError("start the measurement first")
                self.auto["t_begin"] = time.time()
            return {"ok": True}
        if a == "auto_stop":
            with self.lock:
                if not self.auto or self.stage != self.auto["kind"] + "_running":
                    raise ValueError("no automatic measurement is running")
                self.auto["t_end"] = time.time()
                if body.get("origin"):
                    self.auto["origin"] = [float(v) for v in body["origin"][:3]]
                self.stage = "fitting"
            threading.Thread(target=self._auto_finish, daemon=True).start()
            return {"ok": True}
        if a == "cancel":
            if JOB.running and "--calibrate" in JOB.cmd_str:
                JOB.stop()
            with self.lock:
                self.stage, self.kind, self.error = ("idle" if not self.front else "front_done"), None, "cancelled"
                self.auto = None
            return {"ok": True}
        if a == "skip_side":
            with self.lock:
                self.side = {"flip": False, "check": "skipped (sides not verified)"}
                self.stage = "side_done"
            return {"ok": True}
        if a == "save":
            return self._save()
        if a == "live":
            name = (self.saved or {}).get("name") or self.defaults()["active"] or "none"
            if JOB.running:
                JOB.stop()
                time.sleep(1.0)
            ok, msg = JOB.start("live_radar", {"preset": name})
            return {"ok": ok, "msg": msg}
        if a == "reset":
            with self.lock:
                self.reset()
            return {"ok": True}
        raise ValueError("unknown action")

    def _measure(self, kind, body):
        if kind == "side" and not self.front:
            raise ValueError("measure the front position first")
        if JOB.running:
            if not body.get("stop_other"):
                return {"ok": False, "busy": True, "msg": "The B210 is busy (" + (self.state()["busy_kind"] or "?") + ")."}
            JOB.stop()
            time.sleep(1.2)
        if kind == "front":
            cfg = self._cfg_from(body)
            with self.lock:
                self.reset()
                self.cfg = cfg
        cfg = self.cfg
        vals = {"calibrate": cfg["seconds"], "freq": cfg["freq"], "gain": cfg["gain"], "preset": "none",
                "antenna": cfg["antenna"], "spacing": cfg["spacing_m"], "target": f"PHONE-{cfg['tag']}"}
        ok, msg = JOB.start("live_radar", vals)
        if not ok:
            return {"ok": False, "msg": msg}
        with self.lock:
            self.stage, self.kind, self.error, self.progress = kind + "_running", kind, None, None
            if kind == "side":
                self.side = None
        threading.Thread(target=self._watch, args=(kind,), daemon=True).start()
        return {"ok": True}

    def _auto_start(self, body):
        kind = body.get("method")
        if kind not in ("sweep", "ar"):
            raise ValueError("method must be sweep or ar")
        if JOB.running:
            if not body.get("stop_other"):
                return {"ok": False, "busy": True, "msg": "The B210 is busy (" + (self.state()["busy_kind"] or "?") + ")."}
            JOB.stop()
            time.sleep(1.2)
        cfg = self._cfg_from(body)
        AUTO_PACKETS.write_text("")
        vals = {"calibrate": AUTO_MAX_S, "freq": cfg["freq"], "gain": cfg["gain"], "preset": "none",
                "antenna": cfg["antenna"], "spacing": cfg["spacing_m"], "target": f"PHONE-{cfg['tag']}",
                "dump_packets": str(AUTO_PACKETS)}
        ok, msg = JOB.start("live_radar", vals)
        if not ok:
            return {"ok": False, "msg": msg}
        with self.lock:
            self.reset()
            self.cfg = cfg
            self.auto = {"kind": kind, "t_begin": None, "t_end": None, "origin": None, "poses": []}
            self.stage, self.kind = kind + "_running", kind
        threading.Thread(target=self._watch, args=(kind,), daemon=True).start()
        return {"ok": True}

    def _auto_finish(self):
        import cal_fit
        if JOB.running and "--calibrate" in JOB.cmd_str:
            JOB.stop()
        with self.lock:
            au, cfg = self.auto, dict(self.cfg)
        pk = cal_fit.load_packets(AUTO_PACKETS)
        t0 = au["t_begin"] or 0.0
        pk = pk[(pk[:, 0] >= t0) & (pk[:, 0] <= au["t_end"])] if len(pk) else pk
        try:
            if au["kind"] == "sweep":
                res = cal_fit.fit_sweep(pk, cfg["freq"], cfg["spacing_m"])
            else:
                import numpy as np
                AUTO_POSES.write_text(json.dumps({"origin": au["origin"], "poses": au["poses"], "cfg": cfg}))
                if au["origin"] is None:
                    res = {"ok": False, "error": "The origin was not set. Hold the phone between the two antennas and tap SET ORIGIN."}
                else:
                    res = cal_fit.fit_ar(pk, np.array(au["poses"], dtype=float).reshape(-1, 4), au["origin"],
                                         cfg["freq"], cfg["spacing_m"])
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"Could not fit the recording: {e}"}
        with self.lock:
            self.kind = None
            if not res.get("ok"):
                self.stage, self.error, self.auto = "idle", res.get("error", "The fit failed."), None
                return
            res.update(device=f"PHONE-{cfg['tag']}", device_desc=f"USRP Beacon #{cfg['tag']}",
                       ruler_spacing_m=cfg["spacing_m"])
            self.cfg["spacing_m"] = res["spacing_m"]       # the fitted (effective) spacing is what the radar uses
            self.front = res
            self.side = {"flip": res["flip"], "check": res["direction_check"], "auto": True}
            self.stage, self.error = "side_done", None

    def _watch(self, kind):
        since, result = 0, None
        while True:
            time.sleep(0.4)
            st = JOB.status(since)
            since = st["next"]
            for o in st["lines"]:
                t = o["t"]
                with self.lock:
                    self.tail = (self.tail + [t])[-12:]
                if t.startswith("CAL_PROGRESS "):
                    try:
                        p = json.loads(t[13:])
                    except ValueError:
                        continue
                    dev = p.get("device") or {}
                    with self.lock:
                        self.progress = {"t": p.get("t"), "dur": p.get("dur"), "phone_pkts": dev.get("n", 0),
                                         "decoded": p.get("decoded"), "phase": p.get("phase_deg"),
                                         "spread": p.get("phase_spread_deg"), "power": p.get("ref_power_db"),
                                         "others": [x.get("desc") for x in p.get("devices", [])[:4]]}
                elif t.startswith("CAL_RESULT "):
                    try:
                        result = json.loads(t[11:])
                    except ValueError:
                        pass
            if not st["running"]:
                break
        if kind in ("sweep", "ar"):
            with self.lock:                        # normally ended by auto_stop; here it ended by itself
                if self.stage == kind + "_running":
                    self.stage, self.kind, self.auto = "idle", None, None
                    self.error = (f"The measurement ran for {AUTO_MAX_S:.0f} s without being finished, or the B210 "
                                  "stopped (busy or unplugged?). Start again.")
            return
        with self.lock:
            self.kind = None
            if not result:
                self.stage = "front_done" if (kind == "side" and self.front) else "idle"
                self.error = self.error or "The measurement didn't finish (B210 busy or unplugged?)."
                return
            if not result.get("ok"):
                self.stage = "front_done" if (kind == "side" and self.front) else "idle"
                self.error = ("Not enough packets from the phone. Is the beacon running and 1 m away? "
                              f"({result.get('decoded', 0)} packets decoded in total)")
                return
            if kind == "front":
                self.front = result
                self.stage = "front_done"
                return
            diff = (result["phase_deg"] - self.front["phase_deg"] + 180.0) % 360.0 - 180.0
            lam = 299792458.0 / self.cfg["freq"]
            ang = math.degrees(math.asin(max(-1.0, min(1.0, diff / 360.0 * lam / self.cfg["spacing_m"]))))
            if abs(ang) < 12:
                self.side = None
                self.stage = "front_done"
                self.error = (f"The phone read only {abs(ang):.0f}° from straight ahead. Move it further to "
                              "your right and measure again.")
            else:
                self.side = {"angle": round(ang, 1), "flip": ang < 0,
                             "check": (f"ok: {ang:.0f}° right" if ang > 0 else f"flipped: read {abs(ang):.0f}° left")}
                self.stage = "side_done"

    def _save(self):
        with self.lock:
            if not self.front:
                raise ValueError("nothing measured yet")
            f, c, sd = self.front, self.cfg, self.side or {"flip": False, "check": "skipped (sides not verified)"}
            rec = preset_save({
                "name": c["name"], "phase_deg": f["phase_deg"], "ref_power_db": f["ref_power_db"],
                "phase_spread_deg": f["phase_spread_deg"], "n_bursts": f["n_bursts"], "n_total": f["n_total"],
                "quality": f["quality"], "freq": c["freq"], "gain": c["gain"], "spacing_m": c["spacing_m"],
                "flip": sd["flip"], "direction_check": sd["check"], "device": f.get("device"),
                "device_desc": f.get("device_desc"), "antenna": c["antenna"], "rfa_side": c["rfa_side"],
                **{k: f[k] for k in ("method", "path_loss_exp", "ruler_spacing_m", "bearing_err_median_deg",
                                     "bearing_err_rms_deg", "span_deg") if f.get(k) is not None},
                "notes": "made with the USRP Beacon phone app" + {"sweep": " (automatic: sweep)", "ar": " (automatic: AR positions)"}.get(f.get("method"), "")})
            self.saved, self.stage = rec, "saved"
            return {"ok": True, "preset": rec}


PHONE_CAL = PhoneCal()


class PhoneIngestHandler(BaseHTTPRequestHandler):
    """Accepts ONLY phone telemetry (POST JSON). Nothing else of the console is
    reachable through it, so this port can be tunnelled to the internet."""
    def log_message(self, *a):
        pass

    def _reply(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path == "/cal":
            return self._json(200, PHONE_CAL.state())
        self._reply(200, "USRP Beacon ingest: POST phone telemetry JSON here.\n")

    def do_POST(self):
        if urlparse(self.path).path == "/cal":
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 262144)   # AR pose batches
                res = PHONE_CAL.command(json.loads(self.rfile.read(n) or b"{}"))
                return self._json(200, dict(res, state=PHONE_CAL.state()))
            except (ValueError, TypeError, KeyError) as e:
                return self._json(400, {"ok": False, "msg": str(e), "state": PHONE_CAL.state()})
        src = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        if urlparse(self.path).path == "/phone/backlog":   # what the phone saved while offline
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 8 * 1024 * 1024:
                    return self._json(413, {"ok": False, "msg": "batch too big"})
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._json(200, PHONE.backlog(body.get("items"), src + " (http)"))
            except (ValueError, TypeError, AttributeError) as e:
                return self._json(400, {"ok": False, "msg": str(e)})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            msg = json.loads(self.rfile.read(n) or b"{}")
            src = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            PHONE.ingest(msg, src + " (http)")
            self._reply(200, "ok")
        except (ValueError, TypeError):
            self._reply(400, "bad telemetry")
SIGNAL = SignalLink()


def _q_float(path, key, default=0.0):
    for kv in urlparse(path).query.split("&"):
        if kv.startswith(key + "="):
            try:
                return float(kv[len(key) + 1:])
            except ValueError:
                pass
    return default


def feed_file_tailer(feed, path):
    """Follow a JSONL file; push each newly-appended detection line to the feed."""
    pos = 0
    while True:
        try:
            if path.exists():
                with open(path, "r") as f:
                    f.seek(pos)
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                feed.add(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    pos = f.tell()
        except OSError:
            pass
        time.sleep(0.5)


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")     # always show the newest page after a restart
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/radar":
            self._send(200, RADAR_HTML, "text/html; charset=utf-8")
        elif path == "/calibrate":
            self._send(200, CAL_HTML, "text/html; charset=utf-8")
        elif path == "/api/cal/presets":
            self._json(200, presets_list())
        elif path == "/api/feed":
            q = urlparse(self.path).query
            since = 0
            for kv in q.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except ValueError:
                        since = 0
            res = FEED.since(since)
            res["sim_running"] = SIM.running
            self._json(200, res)
        elif path == "/api/ngrok":
            self._json(200, NGROK.get())
        elif path == "/api/raw":
            self._json(200, raw_list())
        elif path == "/api/usrp":
            self._json(200, USB.get())
        elif path == "/api/phone":
            self._json(200, PHONE.state(_q_float(self.path, "since")))
        elif path == "/api/signal":
            self._json(200, SIGNAL.state(_q_float(self.path, "since")))
        elif path == "/api/profiles":
            self._json(200, {"profiles": {
                k: {"label": v["label"], "script": v["script"],
                    "subcommand": v["subcommand"], "params": v["params"]}
                for k, v in PROFILES.items()}})
        elif path == "/api/status":
            q = urlparse(self.path).query
            since = 0
            for kv in q.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except ValueError:
                        since = 0
            self._json(200, JOB.status(since))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if path == "/api/start":
            ok, msg = JOB.start(body.get("profile", ""), body.get("values", {}))
            self._json(200 if ok else 400, {"ok": ok, "msg": msg})
        elif path == "/api/stop":
            ok, msg = JOB.stop()
            self._json(200, {"ok": ok, "msg": msg})
        elif path == "/api/detections":
            dets = body.get("detections", body if isinstance(body, list) else [body])
            n = sum(1 for d in dets if FEED.add(d))
            self._json(200, {"ok": True, "added": n})
        elif path == "/api/sim/start":
            ok = SIM.start(body.get("drones", 2), body.get("max_range", 100))
            self._json(200, {"ok": ok, "running": SIM.running})
        elif path == "/api/sim/stop":
            SIM.stop()
            self._json(200, {"ok": True, "running": SIM.running})
        elif path == "/api/cal/save":
            try:
                rec = preset_save(body)
                self._json(200, {"ok": True, "preset": rec})
            except (ValueError, TypeError, OSError) as e:
                self._json(400, {"ok": False, "msg": str(e)})
        elif path == "/api/cal/set_side":
            try:
                f, _ = _preset_file(body.get("name", ""))
                side = body.get("rfa_side")
                if side not in ("left", "right") or not f.exists():
                    raise ValueError("bad preset or side")
                rec = json.loads(f.read_text())
                rec["rfa_side"] = side
                f.write_text(json.dumps(rec, indent=2))
                self._json(200, {"ok": True})
            except (ValueError, OSError, json.JSONDecodeError) as e:
                self._json(400, {"ok": False, "msg": str(e)})
        elif path == "/api/cal/activate":
            name = str(body.get("name", ""))
            if name and not _preset_file(name)[0].exists():
                return self._json(404, {"ok": False, "msg": "no such preset"})
            preset_set_active(name)
            self._json(200, {"ok": True})
        elif path in ("/api/live/zero", "/api/live/flip", "/api/live/range", "/api/live/mount", "/api/live/dclear",
                      "/api/live/angle", "/api/live/aclear"):
            try:
                return self._json(200, live_command(path.rsplit("/", 1)[1], body.get("device") or "PHONE",
                                                    dist_m=body.get("dist_m"), mount_deg=body.get("mount_deg"),
                                                    true_deg=body.get("true_deg")))
            except (ValueError, OSError) as e:
                return self._json(200, {"ok": False, "error": str(e)})
        elif path == "/api/raw/delete":
            try:
                return self._json(200, raw_delete(body.get("name", "")))
            except (ValueError, OSError) as e:
                return self._json(400, {"ok": False, "msg": str(e)})
        elif path == "/api/cal/delete":
            try:
                f, safe = _preset_file(body.get("name", ""))
            except ValueError as e:
                return self._json(400, {"ok": False, "msg": str(e)})
            if f.exists():
                f.unlink()
            if presets_list()["active"] == safe:
                preset_set_active("")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture Console</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --border:#30363d; --fg:#e6edf3;
    --muted:#8b949e; --accent:#2f81f7; --green:#3fb950; --red:#f85149;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:14px 20px;border-bottom:1px solid var(--border);
         display:flex;align-items:baseline;gap:12px}
  header h1{font-size:16px;margin:0}
  header .sub{color:var(--muted);font-size:12px}
  .wrap{display:grid;grid-template-columns:380px 1fr;gap:0;height:calc(100vh - 51px)}
  .form{padding:18px 20px;border-right:1px solid var(--border);overflow:auto}
  .console{display:flex;flex-direction:column;min-width:0}
  label{display:block;font-size:12px;color:var(--muted);margin:12px 0 4px}
  label .req{color:var(--red)}
  select,input{width:100%;background:#0d1117;border:1px solid var(--border);
    color:var(--fg);border-radius:6px;padding:8px 10px;font:13px var(--mono)}
  select:focus,input:focus{outline:none;border-color:var(--accent)}
  .hint{font-size:11px;color:var(--muted);margin-top:3px}
  .btns{display:flex;gap:10px;margin-top:18px}
  button{flex:1;border:1px solid var(--border);border-radius:6px;padding:10px;
    font-size:13px;font-weight:600;cursor:pointer;background:#21262d;color:var(--fg)}
  button.start{background:var(--green);color:#03160a;border-color:transparent}
  button.stop{background:var(--red);color:#2b0505;border-color:transparent}
  button:disabled{opacity:.4;cursor:not-allowed}
  .cmdbox{margin-top:16px;background:#0d1117;border:1px solid var(--border);
    border-radius:6px;padding:10px;font:12px var(--mono);color:var(--muted);
    word-break:break-all;white-space:pre-wrap}
  .console .bar{padding:10px 16px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:10px;font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
  .dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
  .dot.err{background:var(--red)}
  pre#log{flex:1;margin:0;padding:14px 16px;overflow:auto;background:#0a0d12;
    font:12.5px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
  .ln-cmd{color:var(--accent)}
  .ln-sys{color:var(--muted)}
  .sect{font-size:11px;text-transform:uppercase;letter-spacing:.5px;
    color:var(--muted);margin:22px 0 2px;border-top:1px solid var(--border);padding-top:14px}
</style>
</head>
<body>
<header>
  <h1>Capture Console</h1>
  <span class="sub">set parameters &rarr; run the real capture script &rarr; watch it live</span>
  <span style="flex:1"></span>
  <a href="/calibrate" style="color:var(--accent);text-decoration:none;font-size:13px;margin-right:16px">Calibrate</a>
  <a href="/radar" style="color:var(--accent);text-decoration:none;font-size:13px">Live Radar &rarr;</a>
</header>
<div class="wrap">
  <div class="form">
    <label>Capture script</label>
    <select id="profile"></select>
    <div class="hint" id="scriptPath"></div>

    <div class="sect">Parameters</div>
    <div id="params"></div>

    <div class="btns">
      <button class="start" id="startBtn">Start capture</button>
      <button class="stop" id="stopBtn" disabled>Stop</button>
    </div>

    <div class="sect">Command preview</div>
    <div class="cmdbox" id="cmdPreview">&mdash;</div>
  </div>

  <div class="console">
    <div class="bar">
      <span class="dot" id="dot"></span>
      <span id="statusText">idle</span>
      <span style="flex:1"></span>
      <button style="flex:0 0 auto;padding:4px 12px" id="clearBtn">Clear</button>
    </div>
    <pre id="log"></pre>
  </div>
</div>

<script>
let PROFILES = {};
let nextIndex = 0;
let polling = null;

const $ = s => document.querySelector(s);

function fmtNum(v){
  // keep scientific defaults readable in a number input
  if (typeof v === "number" && Math.abs(v) >= 1e6) return String(v);
  return v;
}

function buildForm(){
  const key = $("#profile").value;
  const prof = PROFILES[key];
  $("#scriptPath").textContent = prof.script;
  const box = $("#params");
  box.innerHTML = "";
  prof.params.forEach(p => {
    const id = "p_" + p.name;
    const lab = document.createElement("label");
    lab.innerHTML = p.name + (p.required ? ' <span class="req">*</span>' : "");
    const inp = document.createElement("input");
    inp.id = id; inp.dataset.name = p.name;
    inp.type = (p.type === "number") ? "text" : "text"; // text so 5.8e9 stays intact
    inp.value = fmtNum(p.default);
    inp.placeholder = p.required ? "required" : "default";
    inp.addEventListener("input", preview);
    const hint = document.createElement("div");
    hint.className = "hint"; hint.textContent = p.help + (p.flag ? "   ("+p.flag+")" : "");
    box.appendChild(lab); box.appendChild(inp); box.appendChild(hint);
  });
  preview();
}

function collect(){
  const vals = {};
  document.querySelectorAll("#params input").forEach(i => vals[i.dataset.name] = i.value);
  return vals;
}

function preview(){
  const key = $("#profile").value;
  const prof = PROFILES[key];
  const vals = collect();
  let parts = ["python -u", prof.script.split("/").pop()];
  if (prof.subcommand) parts.push(prof.subcommand);
  prof.params.forEach(p => {
    const v = (vals[p.name] || "").trim();
    if (v === "") return;
    if (p.flag === null) parts.push(v);
    else parts.push(p.flag, v);
  });
  $("#cmdPreview").textContent = parts.join(" ");
}

async function start(){
  const r = await fetch("/api/start", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({profile: $("#profile").value, values: collect()})});
  const j = await r.json();
  if (!j.ok){ alert(j.msg); return; }
  nextIndex = 0; $("#log").textContent = "";
  setRunning(true); poll();
}

async function stop(){
  await fetch("/api/stop", {method:"POST"});
}

function setRunning(on){
  $("#startBtn").disabled = on;
  $("#stopBtn").disabled = !on;
  $("#dot").className = "dot" + (on ? " on" : "");
  $("#statusText").textContent = on ? "running" : "idle";
}

function appendLines(lines){
  const log = $("#log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  lines.forEach(o => {
    const span = document.createElement("span");
    let cls = "";
    if (o.t.startsWith("$ ")) cls = "ln-cmd";
    else if (o.t.startsWith("[console]")) cls = "ln-sys";
    span.className = cls;
    span.textContent = o.t + "\n";
    log.appendChild(span);
    nextIndex = o.i + 1;
  });
  if (atBottom) log.scrollTop = log.scrollHeight;
}

async function poll(){
  if (polling) clearTimeout(polling);
  const r = await fetch("/api/status?since=" + nextIndex);
  const j = await r.json();
  if (j.lines.length) appendLines(j.lines);
  if (j.running){
    setRunning(true);
    polling = setTimeout(poll, 500);
  } else {
    setRunning(false);
    if (j.returncode !== null && j.returncode !== 0){
      $("#dot").className = "dot err";
      $("#statusText").textContent = "exited (code " + j.returncode + ")";
    } else if (j.returncode === 0){
      $("#statusText").textContent = "finished";
    }
    // keep polling slowly in case a run starts elsewhere / finishes flushing
    polling = setTimeout(poll, 1500);
  }
}

$("#profile").addEventListener("change", buildForm);
$("#startBtn").addEventListener("click", start);
$("#stopBtn").addEventListener("click", stop);
$("#clearBtn").addEventListener("click", () => { $("#log").textContent=""; });

(async function init(){
  const r = await fetch("/api/profiles");
  const j = await r.json();
  PROFILES = j.profiles;
  const sel = $("#profile");
  Object.entries(PROFILES).forEach(([k,v]) => {
    const o = document.createElement("option");
    o.value = k; o.textContent = v.label; sel.appendChild(o);
  });
  buildForm();
  poll();   // pick up an already-running job on refresh
})();
</script>
</body>
</html>
"""


RADAR_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Radar</title>
<style>
  :root{
    --bg:#0a0d12; --panel:#121820; --border:#243040; --fg:#e6edf3;
    --muted:#7d8da0; --accent:#2f81f7; --green:#3fb950; --amber:#d29922;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:12px 20px;border-bottom:1px solid var(--border);
         display:flex;align-items:center;gap:12px}
  header h1{font-size:16px;margin:0}
  header .sub{color:var(--muted);font-size:12px}
  a.nav{color:var(--accent);text-decoration:none;font-size:13px}
  .wrap{display:grid;grid-template-columns:1fr 400px;height:calc(100vh - 49px)}
  .left{display:grid;grid-template-rows:minmax(0,1fr) 290px;min-width:0;min-height:0}
  .scope{position:relative;min-width:0;min-height:0;display:flex;align-items:center;justify-content:center;padding:10px}
  .sig{border-top:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:10px 14px;min-height:0}
  .sigpane{display:flex;flex-direction:column;min-width:0;min-height:0}
  .sighead{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);display:flex;gap:8px;align-items:baseline}
  .sighead b{color:var(--fg);font:600 11px var(--mono);text-transform:none;letter-spacing:0}
  .sigpane .cvbox{flex:1;min-height:0;position:relative}
  .sigpane .cvbox canvas{position:absolute;inset:0;width:100%;height:100%;max-width:none;max-height:none}
  .signote{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.35}
  .key{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 8px;vertical-align:-1px}
  .stats{display:flex;flex-wrap:wrap;gap:4px 14px;font:11px var(--mono);color:var(--muted);margin:4px 0 2px}
  .stats b{color:var(--fg)}
  .phone{border-color:#1d5b78;background:#0f1a22}
  .phone .big{font-size:15px;font-weight:700}
  .phone .note{font-size:11px;color:var(--muted);margin:2px 0 6px;line-height:1.35}
  .err{font:600 13px var(--mono)}
  .recbar{display:inline-flex;align-items:center;gap:4px;margin-right:16px;font:600 12px var(--mono)}
  .recbar .reclbl{color:var(--muted);margin-right:2px}
  .recbar button{font:600 12px var(--mono);padding:4px 10px;border-radius:12px;border:1px solid var(--muted);
                 background:transparent;color:var(--fg);cursor:pointer}
  .recbar button.on{background:#d1242f;border-color:#d1242f;color:#fff}
  .recbar button[data-rec="iq"].on{background:#9a6700;border-color:#9a6700}
  .recbar button[data-rec="off"].on{background:var(--muted);border-color:var(--muted)}
  .recbar button:disabled{opacity:.55;cursor:not-allowed}
  .recbar .reclive{margin-left:6px;color:#d1242f}
  .usb{font:600 12px var(--mono);padding:3px 10px;border-radius:12px;margin-right:16px;
       background:#1b2330;color:var(--muted);border:1px solid var(--border);white-space:nowrap}
  canvas{max-width:100%;max-height:100%}
  .side{border-left:1px solid var(--border);padding:16px;overflow:auto}
  .ctl{margin-bottom:14px}
  label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px}
  input{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--fg);
        border-radius:6px;padding:7px 9px;font:13px var(--mono)}
  .btns{display:flex;gap:8px;margin-top:8px}
  button{flex:1;border:1px solid var(--border);border-radius:6px;padding:8px;
          font-size:12px;font-weight:600;cursor:pointer;background:#1b2330;color:var(--fg)}
  button.go{background:var(--green);color:#03160a;border-color:transparent}
  button.no{background:#30242a;color:#f0a0a0}
  button:disabled{opacity:.35;cursor:not-allowed}
  .sect{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);
        margin:18px 0 8px;border-top:1px solid var(--border);padding-top:12px}
  .track{background:var(--panel);border:1px solid var(--border);border-radius:8px;
          padding:9px 11px;margin-bottom:8px}
  .track .name{font-weight:600;font-size:13px}
  .track .rowv{display:flex;justify-content:space-between;font:12px var(--mono);color:var(--muted);margin-top:3px}
  .track .rowv b{color:var(--fg);font-weight:600}
  .pill{font-size:10px;padding:1px 6px;border-radius:10px;background:#1b2b1b;color:var(--green);margin-left:6px}
  .pill.sim{background:#2b2616;color:var(--amber)}
  .empty{color:var(--muted);font-size:13px}
  select{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--fg);
        border-radius:6px;padding:7px 9px;font:13px var(--mono)}
  .hint2{font-size:11px;color:var(--muted);margin:6px 0 4px;font-family:var(--mono)}
</style>
</head>
<body>
<header>
  <h1>Live Radar</h1>
  <span class="sub">bearing (MUSIC DoA) &middot; range (path-loss) &middot; forward half-plane, B210 2-ch</span>
  <span style="flex:1"></span>
  <span class="recbar" id="recBar" title="What Start live saves on this Mac (capture_console/recordings/). Change it before pressing Start live.">
    <span class="reclbl">Record:</span>
    <button data-rec="raw" title="Every sample from both antennas, lossless. ~32 MB/s (~115 GB/hour). Stops saving by itself before the disk fills.">● Full raw IQ</button>
    <button data-rec="iq" title="Each decoded packet's bearing + its IQ snippet. ~0.4 MB/s">Packets + IQ</button>
    <button data-rec="off" title="Radar only, nothing saved">Off</button>
    <span id="recLive" class="reclive"></span>
  </span>
  <span id="ngrokBox" class="usb" style="display:none;cursor:pointer" title="Click to copy. Paste it into the phone app's 'Laptop IP address or link'.">
    📱 phone link: <b id="ngrokUrl" style="color:var(--fg)"></b> <span id="ngrokCopy" style="color:var(--accent)">copy</span></span>
  <span id="usbInfo" class="usb" title="USB link of the USRP, as reported by macOS (and by UHD while streaming)">USRP: checking&hellip;</span>
  <a class="nav" href="/calibrate" style="margin-right:14px">Calibrate</a>
  <a class="nav" href="/">&larr; Capture Console</a>
</header>
<div class="wrap">
  <div class="left">
  <div class="scope"><canvas id="ppi"></canvas></div>
  <div class="sig">
    <div class="sigpane">
      <div class="sighead">What the USRP hears &middot; spectrum <b id="specInfo"></b></div>
      <div class="stats" id="sigStats"><span>no signal data yet &mdash; press <b>Start live</b></span></div>
      <div class="cvbox"><canvas id="spec"></canvas></div>
      <div class="signote"><span class="key" style="background:#4f8cff;margin-left:0"></span>average power
        <span class="key" style="background:#e3a33a"></span>peak (catches short packets)
        &middot; the BLE channel is the shaded 2&nbsp;MHz in the middle. Spikes rising above the blue line are packets.</div>
    </div>
    <div class="sigpane">
      <div class="sighead">Packets over time (last 20 s) <b id="tlInfo"></b></div>
      <div class="stats" id="blinkStats"><span>&nbsp;</span></div>
      <div class="cvbox"><canvas id="tl"></canvas></div>
      <div class="signote">Each dot is one packet the USRP caught; higher = stronger.
        <span class="key" style="background:#39c5ff;margin-left:0"></span>decoded as your phone
        <span class="key" style="background:#7d8da0"></span>other devices
        <span class="key" style="background:#3fb95055;border:1px solid #3fb950"></span>phone says it is transmitting.
        If the dots come and go with the green bands, the USRP is hearing your phone.</div>
    </div>
  </div>
  </div>
  <div class="side">
    <div class="sect" style="margin-top:0;border-top:0;padding-top:0">Phone &middot; USRP Beacon app (ground truth)</div>
    <div id="phoneBox" class="track phone"><div class="empty">Waiting for the phone&hellip;</div></div>
    <div class="ctl">
      <label>Max range (m)</label>
      <input id="maxRange" value="20">
    </div>
    <div class="ctl">
      <label>Show only (name / address contains)</label>
      <input id="filt" placeholder="e.g. Apple  or  C6:31">
    </div>
    <div class="ctl">
      <label>Live tracking (B210) &middot; calibration</label>
      <select id="presetSel"></select>
      <div class="hint2">Recording mode: <b>Record</b> buttons at the top of the page</div>
      <div class="btns">
        <button class="go" id="liveOn">Start live</button>
        <button class="no" id="liveOff">Stop</button>
      </div>
      <div class="hint2" id="liveStatus">&nbsp;</div>
      <div class="btns" style="margin-top:6px">
        <button class="go" id="zeroBtn" title="Put the phone (beacon on) straight in front of the antennas, keep it still 3-4 s, then press">&#9678; Set middle</button>
        <button id="flipBtn" title="Use if right shows as left">&#8646; Swap left/right</button>
      </div>
      <div class="btns" style="margin-top:6px;align-items:center">
        <input id="mountDeg" value="0" style="width:56px" title="How far the whole USRP (antenna pair) is turned to the RIGHT of your 0-degree point (negative = left)"> <span class="hint2" style="margin:0">&deg; turned</span>
        <button id="mountBtn" title="Tell the radar how the antenna pair is turned; then press Set middle at the 0-degree point">&#8635; Set rotation</button>
      </div>
      <div class="btns" style="margin-top:6px;align-items:center">
        <input id="angleDeg" value="-28.5" style="width:56px" title="The TRUE angle of the point you are standing on (left = negative), e.g. B = -28.5, C = 44.5"> <span class="hint2" style="margin:0">&deg; true</span>
        <button class="go" id="angleBtn" title="Stand still on a measured point, type its true angle, press. Teaches the radar this direction.">&#10003; Set angle</button>
        <button id="aclearBtn" title="Forget all angle teach points">Clear</button>
      </div>
      <div class="hint2" id="angleStatus">After <b>Set middle</b> at A: stand at B, type its true angle &rarr; <b>Set angle</b>; same at C.</div>
      <div class="hint2" id="zeroStatus">Phone at your 0&deg; point (e.g. A) &rarr; <b>Set middle</b> (measures all 3 channels). If the USRP is turned, <b>Set rotation</b> first.</div>
      <div class="btns" style="margin-top:6px;align-items:center">
        <input id="rangeM" value="5" style="width:56px" title="Tape-measured distance from the antennas to the phone, metres"> <span class="hint2" style="margin:0">m</span>
        <button class="go" id="rangeBtn" title="Phone (beacon on) at exactly this distance, held still 3-4 s, then press">&#8596; Set distance</button>
      </div>
      <div class="hint2" id="rangeStatus">1st press (at A, e.g. 8 m) sets the reference. Each later press at another point (e.g. B, C = 9 m) teaches that direction.</div>
      <div class="btns" style="margin-top:4px"><button id="dclearBtn" title="Forget the distance reference and all direction points">Clear distance points</button></div>
      <a class="nav" href="/calibrate" style="font-size:12px">+ New calibration&hellip;</a>
    </div>
    <div class="ctl">
      <label>Simulator (no SDR needed)</label>
      <div class="btns">
        <button class="go" id="simOn">Start sim</button>
        <button class="no" id="simOff">Stop</button>
      </div>
    </div>
    <div class="ctl">
      <label>Raw IQ on disk <span id="rawFree" style="font-weight:400"></span></label>
      <div id="rawList" class="hint2">loading&hellip;</div>
      <div class="hint2">Delete removes only the full raw stream (raw.sc16). Packets, bearings and settings stay.</div>
    </div>
    <div class="sect" style="display:flex;align-items:center;gap:8px">Device summary
      <span style="flex:1"></span>
      <button id="csvBtn" style="flex:0 0 auto;padding:3px 9px;font-size:11px">Export CSV</button></div>
    <div class="hint2" id="sumHint">One card per physical device. Addresses the same phone rotates through are grouped. Click a card to focus it.</div>
    <div id="tracks"><div class="empty">No detections yet.</div></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const cv = $("#ppi"), ctx = cv.getContext("2d");
let nextI = 0;
let tracks = {};           // id -> {label, source, last:{...latest record}, points:[...]}
let focus = null;          // group id being focused
let RFA = null;            // "left"/"right": side RF A is on, from the selected preset
const sideAnt = side => RFA ? ((side === RFA) ? "RF A" : "RF B") : null;
function dirWord(a){
  if (Math.abs(a) < 3) return "straight ahead";
  const side = a > 0 ? "right" : "left", ant = sideAnt(side);
  return Math.abs(a).toFixed(0) + "° " + side + (ant ? " (" + ant + " side)" : "");
}
const TRAIL = 10, TTL = 15;
const esc = t => String(t ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STAB = {stable:"#3fb950", ok:"#d29922", jumpy:"#f85149", uncertain:"#a371f7"};
const PHONE_COL = "#39c5ff", TL_SPAN = 20;
let PH = {latest:null, age:null, src:null, error:null, hist:[], marks:[]};   // phone telemetry
let SG = {latest:null, age:null, bursts:[]};                                 // USRP signal view
let phSince = Date.now()/1000 - 120, sgSince = Date.now()/1000 - TL_SPAN - 2;
const fx = (v, d=1) => (v == null || isNaN(v)) ? "—" : Number(v).toFixed(d);
function truth(){                       // latest tape-measured calibration mark from the phone
  const L = PH.latest;
  if (!L || !L.mark || L.truth_bearing_deg == null) return null;
  return {bearing: L.truth_bearing_deg, range: L.truth_range_m, mark: L.mark};
}
function phoneDev(devs){ return devs.find(d => d.lead.phone || d.members.some(m => m.last.phone)); }
function advOnAt(t){                    // was the phone transmitting at time t (per its own telemetry)?
  const h = PH.hist; if (!h.length || t < h[0][0] - 0.5) return null;
  let lo = 0, hi = h.length - 1;
  while (lo < hi){ const mid = (lo + hi + 1) >> 1; if (h[mid][0] <= t) lo = mid; else hi = mid - 1; }
  return (t - h[lo][0] > 1.5) ? null : h[lo][1];
}
function advStable(t){ const a = advOnAt(t - 0.4), b = advOnAt(t), c = advOnAt(t + 0.4);
  return (a === b && b === c) ? b : null; }    // skip blink edges (Wi-Fi delay makes them ambiguous)

function resize(){
  const box = $(".scope").getBoundingClientRect();
  const s = Math.min(box.width, box.height) - 20;
  const dpr = window.devicePixelRatio || 1;
  cv.width = s * dpr; cv.height = s * dpr;
  cv.style.width = s + "px"; cv.style.height = s + "px";
  ctx.setTransform(dpr,0,0,dpr,0,0);
}
window.addEventListener("resize", () => { resize(); draw(devices()); });

function maxRange(){ return Math.max(1, parseFloat($("#maxRange").value) || 20); }
function toXY(angle, dist, cx, cy, R){
  const r = Math.min(1, dist / maxRange()) * R;
  const rad = angle * Math.PI / 180;
  return [cx + r * Math.sin(rad), cy - r * Math.cos(rad)];
}
function filtered(id){
  const fq = ($("#filt").value || "").toLowerCase().trim();
  return fq && !(tracks[id].label + " " + id).toLowerCase().includes(fq);
}

// physical devices = groups of tracks (live tracker groups rotating addresses)
function devices(){
  const now = Date.now()/1000, groups = {};
  for (const id in tracks){
    const tk = tracks[id]; const L = tk.last;
    if (!L || now - L.t > TTL){ delete tracks[id]; continue; }
    if (filtered(id)) continue;
    const g = L.group || id;
    (groups[g] = groups[g] || []).push(tk);
  }
  return Object.entries(groups).map(([g, mem]) => {
    mem.sort((a,b) => (b.last.pkts||0) - (a.last.pkts||0));
    const lead = (mem.find(m => m.id === g) || mem[0]).last;
    return {gid:g, lead, members:mem,
            rate: mem.reduce((s,m) => s + (m.last.rate||0), 0),
            pkts: mem.reduce((s,m) => s + (m.last.pkts||0), 0),
            first: Math.min(...mem.map(m => m.last.first_seen || m.last.t)),
            seen: Math.max(...mem.map(m => m.last.t))};
  }).sort((a,b) => (b.lead.power_db ?? b.lead.conf*100) - (a.lead.power_db ?? a.lead.conf*100));
}

function draw(devs){
  const S = cv.clientWidth;
  const cx = S/2, cy = S - 26, R = S - 52;
  ctx.clearRect(0,0,S,S);
  ctx.strokeStyle = "#1d2836"; ctx.fillStyle = "#4a5a6e";
  ctx.font = "10px ui-monospace,monospace"; ctx.textAlign = "left";
  for (let k=1;k<=5;k++){
    const r = R * k/5;
    ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, 2*Math.PI); ctx.stroke();
    ctx.fillText((maxRange()*k/5).toFixed(maxRange() < 10 ? 1 : 0) + " m", cx + 3, cy - r + 11);
  }
  ctx.textAlign = "center";
  for (let a=-90; a<=90; a+=30){
    const [x,y] = toXY(a, maxRange(), cx, cy, R);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(x,y); ctx.stroke();
    const [lx,ly] = toXY(a, maxRange()*1.07, cx, cy, R);
    ctx.fillText((a>0?"+":"") + a + "°", lx, ly);
  }
  ctx.fillText("0° (straight ahead)", cx, cy + 16);
  ctx.font = "bold 12px ui-monospace,monospace"; ctx.fillStyle = "#6d86a3";
  ctx.textAlign = "left";  ctx.fillText("◀ LEFT" + (RFA ? " · " + sideAnt("left") : ""), 8, cy + 16);
  ctx.textAlign = "right"; ctx.fillText((RFA ? sideAnt("right") + " · " : "") + "RIGHT ▶", S - 8, cy + 16);

  const T = truth();
  if (T){
    const [tx,ty] = toXY(T.bearing, T.range ?? maxRange()*0.5, cx, cy, R);
    ctx.save(); ctx.strokeStyle = PHONE_COL; ctx.setLineDash([5,5]); ctx.globalAlpha = .55;
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(tx,ty); ctx.stroke(); ctx.restore();
    ctx.strokeStyle = PHONE_COL; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(tx,ty-9); ctx.lineTo(tx+9,ty); ctx.lineTo(tx,ty+9); ctx.lineTo(tx-9,ty); ctx.closePath(); ctx.stroke();
    ctx.lineWidth = 1; ctx.fillStyle = PHONE_COL; ctx.font = "bold 11px ui-monospace,monospace"; ctx.textAlign = "left";
    ctx.fillText("TRUE phone spot (mark #" + T.mark + ")", tx + 12, ty + 14);
  }

  devs.forEach(d => {
    const L = d.lead, live = L.source !== "sim";
    const dim = focus && focus !== d.gid;
    const col = L.phone ? PHONE_COL : live ? (STAB[L.stability] || "#3fb950") : "#d29922";
    ctx.globalAlpha = dim ? 0.18 : 1;
    // uncertainty wedge: bearing +- spread, distance lo..hi
    if (live && L.bearing_std != null && L.bearing_std < 90){
      const sd = Math.max(1.5, L.bearing_std), r0 = Math.min(1, (L.dist_lo ?? L.distance)/maxRange())*R,
            r1 = Math.min(1, (L.dist_hi ?? L.distance)/maxRange())*R;
      const a0 = (L.angle - sd - 90) * Math.PI/180, a1 = (L.angle + sd - 90) * Math.PI/180;
      ctx.fillStyle = col + "33"; ctx.strokeStyle = col + "88";
      ctx.beginPath(); ctx.arc(cx, cy, Math.max(r1, r0 + 3), a0, a1); ctx.arc(cx, cy, r0, a1, a0, true); ctx.closePath();
      ctx.fill(); ctx.stroke();
    }
    // short smoothed trail of the lead address
    const pts = (d.members.find(m => m.id === d.gid) || d.members[0]).points;
    pts.forEach((p,j) => {
      const [x,y] = toXY(p.angle, p.distance, cx, cy, R);
      ctx.fillStyle = col; ctx.globalAlpha = (dim ? 0.1 : 0.15 + 0.5*(j+1)/pts.length);
      ctx.beginPath(); ctx.arc(x,y, j===pts.length-1 ? 5.5 : 2, 0, 7); ctx.fill();
    });
    ctx.globalAlpha = dim ? 0.25 : 1;
    const [hx,hy] = toXY(L.angle, L.distance, cx, cy, R);
    ctx.fillStyle = L.phone ? PHONE_COL : "#cfe3ff"; ctx.textAlign = "left";
    ctx.font = (focus === d.gid || L.phone ? "bold " : "") + "11px ui-monospace,monospace";
    ctx.fillText((L.phone ? "PHONE · " : "") + L.label + (d.members.length > 1 ? " +" + (d.members.length-1) : "") + "  " +
                 (Math.abs(L.angle) < 3 ? "ahead" : Math.abs(L.angle).toFixed(0) + "° " + (L.angle > 0 ? "R" : "L")) +
                 "  " + L.distance.toFixed(1) + " m", hx + 9, hy - 7);
    ctx.globalAlpha = 1;
  });
}

function fmtAge(s){ return s < 60 ? s.toFixed(0) + " s" : (s/60).toFixed(1) + " min"; }

function renderSide(devs){
  const box = $("#tracks");
  if (!devs.length){ box.innerHTML = '<div class="empty">No detections yet.</div>'; return; }
  const now = Date.now()/1000;
  box.innerHTML = "";
  devs.forEach(d => {
    const L = d.lead, live = L.source !== "sim";
    const el = document.createElement("div");
    el.className = "track"; el.style.cursor = "pointer";
    if (focus === d.gid) el.style.borderColor = "var(--accent)";
    else if (L.phone) el.style.borderColor = PHONE_COL;
    const stab = live ? (L.stability || "?") : "sim";
    const sd = (L.bearing_std != null && L.bearing_std < 90) ? "±" + L.bearing_std.toFixed(1) + "°" : "±?";
    const dist = (L.dist_lo != null) ? L.distance.toFixed(1) + " m <span style=\"color:var(--muted)\">(" + L.dist_lo.toFixed(1) + "–" + L.dist_hi.toFixed(1) + ")</span>"
                                     : L.distance.toFixed(1) + " m";
    el.innerHTML =
      '<div class="name" style="display:flex;align-items:center;gap:6px">' + esc(L.label) +
        '<span class="pill" style="background:#1b2330;color:' + (STAB[stab] || "var(--amber)") + '">' + stab + '</span></div>' +
      '<div style="font:11px var(--mono);color:var(--muted)">' + d.members.map(m => esc(m.id)).join(" · ") + '</div>' +
      '<div class="rowv"><span>direction</span><b>' + dirWord(L.angle) + ' ' + sd + '</b></div>' +
      (L.held ? '<div style="font-size:11px;color:#a371f7;margin-top:2px">uncertain: packets disagree (reflections or movement). Holding the last good direction; latest raw ' + (L.raw_angle != null ? L.raw_angle.toFixed(0) + '°' : '?') + '</div>' : '') +
      '<div class="rowv"><span>distance</span><b>' + dist + '</b></div>' +
      (live ? '<div class="rowv"><span>signal</span><b>' + (L.power_db ?? 0).toFixed(1) + ' dB · ' + d.rate.toFixed(1) + ' pkt/s</b></div>' +
              '<div class="rowv"><span>seen</span><b>' + fmtAge(now - d.first) + ' · ' + d.pkts + ' pkts · last ' + fmtAge(Math.max(0, now - d.seen)) + ' ago</b></div>'
            : '<div class="rowv"><span>confidence</span><b>' + (L.conf*100).toFixed(0) + '%</b></div>');
    el.onclick = () => { focus = (focus === d.gid) ? null : d.gid; };
    box.appendChild(el);
  });
}

function exportCSV(){
  const now = Date.now()/1000;
  const rows = [["device","addresses","direction","bearing_deg","bearing_spread_deg","distance_m","dist_lo_m","dist_hi_m",
                 "power_db","pkt_per_s","packets","stability","seen_for_s","last_seen_s_ago"]];
  devices().forEach(d => { const L = d.lead;
    rows.push([L.label, d.members.map(m => m.id).join(" "), dirWord(L.angle), L.angle, L.bearing_std ?? "", L.distance, L.dist_lo ?? "",
               L.dist_hi ?? "", L.power_db ?? "", d.rate.toFixed(1), d.pkts, L.stability ?? "",
               (now - d.first).toFixed(0), (now - d.seen).toFixed(1)]); });
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g,'""') + '"').join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type:"text/csv"}));
  a.download = "ble_devices_" + new Date().toISOString().slice(0,19).replace(/[:T]/g,"-") + ".csv";
  a.click();
}
$("#csvBtn").addEventListener("click", exportCSV);

async function poll(){
  try{
    const r = await fetch("/api/feed?since="+nextI);
    const j = await r.json();
    nextI = j.next;
    j.items.forEach(d => {
      const tk = tracks[d.id] || (tracks[d.id] = {id:d.id, points:[]});
      tk.label = d.label; tk.source = d.source; tk.last = d;
      tk.points.push({t:d.t, angle:d.angle, distance:d.distance});
      if (tk.points.length > TRAIL) tk.points.shift();
    });
    $("#simOn").disabled = j.sim_running;
    $("#simOff").disabled = !j.sim_running;
  }catch(e){}
  const devs = devices();
  draw(devs); renderSide(devs);
  setTimeout(poll, 400);
}

// ---- phone ground-truth card
function renderPhone(devs){
  const box = $("#phoneBox"), L = PH.latest;
  if (PH.error){ box.innerHTML = '<div class="empty" style="color:#f85149">' + esc(PH.error) + '</div>'; return; }
  if (!L){
    box.innerHTML = '<div class="big" style="color:var(--muted)">○ no phone data yet</div>' +
      '<div class="note">Open the USRP Beacon app, put ' +
      (NGROK_URL ? 'this link in <b>Laptop IP address or link</b>:<br><b style="font-family:var(--mono);color:var(--fg);user-select:all">' + esc(NGROK_URL) + '</b><br>' : 'this computer\'s IP in <b>Laptop IP address or link</b>, ') +
      'then turn on <b>Send info to laptop</b>. Also listening on UDP port ' + PH.port + ' for the same Wi-Fi.</div>';
    return;
  }
  const stale = PH.age > 3;
  const st = !L.adv_running ? ["STOPPED", "#7d8da0", "The beacon is off. Press START BEACON in the app."]
           : L.adv_on ? ["TRANSMITTING", "#3fb950", "The phone is sending Bluetooth packets right now."]
           : ["PAUSED (blink)", "#d29922", "Quiet part of the blink cycle: the USRP should hear nothing from the phone now."];
  const T = truth(), pd = phoneDev(devs);
  let cmp = '<div class="note">Not seen by the USRP yet. Start live tracking, and make sure the phone is close and the beacon is running.</div>';
  if (pd){
    const P = pd.lead;
    const be = T ? P.angle - T.bearing : null, re = (T && T.range) ? P.distance - T.range : null;
    const ec = v => v == null ? "var(--muted)" : Math.abs(v) <= 5 ? "#3fb950" : Math.abs(v) <= 15 ? "#d29922" : "#f85149";
    cmp = '<div class="rowv"><span>USRP says direction</span><b>' + dirWord(P.angle) + ' ±' + fx(P.bearing_std) + '°</b></div>' +
          '<div class="rowv"><span>USRP says distance</span><b>' + fx(P.distance, 2) + ' m</b></div>' +
          '<div class="rowv"><span>signal</span><b>' + fx(P.power_db) + ' dB · ' + fx(pd.rate) + ' pkt/s</b></div>' +
          (T ? '<div class="rowv"><span>direction error</span><b class="err" style="color:' + ec(be) + '">' + (be>0?"+":"") + fx(be) + '°</b></div>' +
               '<div class="rowv"><span>distance error</span><b class="err" style="color:' + ec(re == null ? null : re*5) + '">' + (re == null ? "—" : (re>0?"+":"") + fx(re,2) + " m") + '</b></div>' +
               '<div class="note">Error = USRP estimate minus the measured spot. Green = within 5°. A steady offset means the calibration needs redoing; a jumping one means reflections or a weak signal.</div>'
             : '<div class="note">Tap <b>MARK</b> in the app (with a measured angle and distance) to see how far off the USRP is.</div>');
  }
  box.innerHTML =
    '<div class="big" style="color:' + (stale ? "#f85149" : st[1]) + '">● ' + (stale ? "PHONE SILENT " + fx(PH.age,0) + " s" : st[0]) + '</div>' +
    '<div class="note">' + (stale ? "No telemetry arriving. Check Wi-Fi and the app's \"Send info\" switch." : st[2]) + '</div>' +
    '<div class="rowv"><span>beacon</span><b>tag #' + L.tag + (L.platform === "ios" ? ' · iPhone (interval/power set by iOS)' : ' · every ' + L.interval_ms + ' ms · ' + (L.tx_dbm == null ? "?" : L.tx_dbm) + ' dBm') + '</b></div>' +
    '<div class="rowv"><span>from</span><b>' + esc(PH.src) + ' · ' + fx(PH.age, 1) + ' s ago</b></div>' +
    '<div class="sect" style="margin:10px 0 4px">Where the phone really is</div>' +
    (T ? '<div class="rowv"><span>measured spot</span><b>' + dirWord(T.bearing) + ' · ' + fx(T.range, 2) + ' m</b></div>' +
         '<div class="note">From MARK #' + T.mark + ' in the app (tape-measured). Drawn as the cyan diamond on the radar.</div>'
       : '<div class="note">No MARK yet.</div>') +
    '<div class="rowv"><span>GPS</span><b>' + (L.lat != null ? fx(L.lat,6) + ', ' + fx(L.lon,6) + ' ±' + fx(L.acc_m,0) + ' m' : "no fix") + '</b></div>' +
    '<div class="rowv"><span>phone facing</span><b>' + (L.heading_deg != null ? fx(L.heading_deg,0) + '° from north' : "—") + '</b></div>' +
    '<div class="note">GPS is only a rough cross-check (±3–10 m). Use the measured spot for calibration.</div>' +
    '<div class="sect" style="margin:10px 0 4px">What the USRP sees of it</div>' + cmp;
}

// ---- signal panes
function fit(c){
  const r = c.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(r.width*dpr) || c.height !== Math.round(r.height*dpr)){
    c.width = Math.round(r.width*dpr); c.height = Math.round(r.height*dpr);
  }
  const g = c.getContext("2d"); g.setTransform(dpr,0,0,dpr,0,0);
  return [g, r.width, r.height];
}
function axisY(g, W, H, lo, hi, pl, pb, unit){
  g.strokeStyle = "#1d2836"; g.fillStyle = "#5d6e83"; g.font = "10px ui-monospace,monospace"; g.textAlign = "right";
  const step = (hi - lo) > 50 ? 20 : 10;
  for (let v = Math.ceil(lo/step)*step; v <= hi; v += step){
    const y = (H - pb) - (v - lo) / (hi - lo) * (H - pb - 4);
    g.beginPath(); g.moveTo(pl, y); g.lineTo(W, y); g.stroke(); g.fillText(v + unit, pl - 4, y + 3);
  }
}
let specLo = null, specHi = null;
function drawSpectrum(){
  const [g, W, H] = fit($("#spec")); g.clearRect(0,0,W,H);
  const S = SG.latest; if (!S){ g.fillStyle = "#5d6e83"; g.font = "12px sans-serif"; g.fillText("waiting for live capture…", 50, H/2); return; }
  const a = S.spec_avg, m = S.spec_max, n = a.length, pl = 44, pb = 16;
  const lo = Math.min(...a) - 5, hi = Math.max(...m) + 5;
  specLo = specLo == null ? lo : specLo + 0.15*(lo - specLo); specHi = specHi == null ? hi : specHi + 0.15*(hi - specHi);
  axisY(g, W, H, specLo, specHi, pl, pb, "");
  const X = i => pl + i / (n-1) * (W - pl - 4), Y = v => (H - pb) - (v - specLo)/(specHi - specLo) * (H - pb - 4);
  const half = S.rate/2e6, bw = Math.min(1, half);                 // shade the 2 MHz BLE channel
  g.fillStyle = "#4f8cff14"; g.fillRect(X((0.5 - bw/(2*half))*(n-1)), 0, (bw/half)*(W - pl - 4), H - pb);
  g.fillStyle = "#5d6e83"; g.textAlign = "center"; g.font = "10px ui-monospace,monospace";
  for (const f of [-half/2, 0, half/2]) g.fillText(((S.freq/1e6) + f).toFixed(1), X((f + half)/(2*half)*(n-1)), H - 3);
  const line = (arr, col) => { g.strokeStyle = col; g.lineWidth = 1.3; g.beginPath();
    arr.forEach((v,i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); g.lineWidth = 1; };
  line(m, "#e3a33a"); line(a, "#4f8cff");
  g.fillStyle = "#5d6e83"; g.textAlign = "right"; g.fillText("MHz", W - 2, H - 3);
}
function drawTimeline(){
  const [g, W, H] = fit($("#tl")); g.clearRect(0,0,W,H);
  const now = Date.now()/1000, t0 = now - TL_SPAN, pl = 44, pb = 16;
  const B = SG.bursts.filter(b => b.t >= t0);
  const ps = B.map(b => b.p), fl = SG.latest ? SG.latest.floor_db : -90;
  const lo = Math.min(fl, ...ps) - 3, hi = Math.max(fl + 20, ...ps) + 3;
  const X = t => pl + (t - t0) / TL_SPAN * (W - pl - 4), Y = v => (H - pb) - (v - lo)/(hi - lo) * (H - pb - 4);
  // phone "transmitting" bands
  g.fillStyle = "#3fb95026";
  let on = null;
  const h = PH.hist.filter(x => x[0] >= t0 - 2);
  h.forEach((x, i) => {
    if (x[1] && on == null) on = x[0];
    if ((!x[1] || i === h.length - 1) && on != null){ const e = x[1] ? Math.min(now, x[0] + 0.3) : x[0];
      g.fillRect(X(Math.max(on, t0)), 0, X(e) - X(Math.max(on, t0)), H - pb); on = null; }
  });
  axisY(g, W, H, lo, hi, pl, pb, "");
  g.fillStyle = "#5d6e83"; g.textAlign = "center"; g.font = "10px ui-monospace,monospace";
  for (let k = 0; k <= TL_SPAN; k += 5){ g.textAlign = k === TL_SPAN ? "right" : "center";
    g.fillText(k === TL_SPAN ? "now" : "-" + (TL_SPAN - k) + "s", X(t0 + k), H - 3); }
  g.strokeStyle = "#5d6e8380"; g.setLineDash([3,4]); g.beginPath(); g.moveTo(pl, Y(fl)); g.lineTo(W, Y(fl)); g.stroke(); g.setLineDash([]);
  g.textAlign = "left"; g.fillText("noise floor", pl + 4, Y(fl) - 3);
  B.forEach(b => {
    g.fillStyle = b.phone ? PHONE_COL : b.dev ? "#7d8da0" : "#4a5566";
    g.beginPath(); g.arc(X(b.t), Y(b.p), b.phone ? 3.5 : 2, 0, 7); g.fill();
  });
}
function renderSigStats(){
  const S = SG.latest;
  if (S){
    const stale = SG.age > 2;
    $("#specInfo").textContent = (S.freq/1e9).toFixed(3) + " GHz · BLE ch " + S.chan + " · gain " + S.gain + " dB" + (stale ? " · STALE" : "");
    const now = Date.now()/1000, recent = SG.bursts.filter(b => b.t >= now - 5);
    $("#sigStats").innerHTML =
      '<span>noise floor <b>' + fx(S.floor_db) + ' dB</b></span>' +
      '<span>antenna A <b>' + fx(S.ch_db[0]) + '</b> / B <b>' + fx(S.ch_db[1]) + ' dB</b></span>' +
      '<span>packets <b>' + fx(recent.length/5) + '/s</b></span>' +
      '<span>decoded <b>' + fx(recent.filter(b => b.dev).length/5) + '/s</b></span>' +
      '<span>phone <b style="color:' + PHONE_COL + '">' + fx(recent.filter(b => b.phone).length/5) + '/s</b></span>';
  }
  // blink check: packet rate while phone ON vs OFF
  const now = Date.now()/1000, B = SG.bursts.filter(b => b.t >= now - TL_SPAN);
  let onT = 0, offT = 0, onN = 0, offN = 0, phOn = 0, phOff = 0;
  for (let t = now - TL_SPAN; t < now - 0.4; t += 0.1){ const s = advStable(t); if (s === true) onT += .1; else if (s === false) offT += .1; }
  B.forEach(b => { const s = advStable(b.t); if (s === true){ onN++; if (b.phone) phOn++; } else if (s === false){ offN++; if (b.phone) phOff++; } });
  if (onT > 1 && offT > 1){
    const ron = onN/onT, roff = offN/offT, extra = ron - roff;
    $("#blinkStats").innerHTML =
      '<span>phone ON: <b>' + fx(ron) + ' pkt/s</b></span><span>phone OFF: <b>' + fx(roff) + ' pkt/s</b></span>' +
      '<span>difference <b style="color:' + (extra > 1 ? "#3fb950" : "#d29922") + '">' + (extra>0?"+":"") + fx(extra) + '/s</b></span>' +
      '<span>phone pkts during OFF <b style="color:' + (phOff ? "#f85149" : "#3fb950") + '">' + phOff + '</b></span>';
    $("#tlInfo").textContent = extra > 1 ? "· USRP is hearing the phone's blink" : "· no clear blink pattern yet";
  } else {
    $("#blinkStats").innerHTML = '<span>' + (PH.latest ? "turn on Blink in the app to compare ON vs OFF packet rates" : "needs phone telemetry for the ON/OFF comparison") + '</span>';
    $("#tlInfo").textContent = "";
  }
}
async function pollPhone(){
  try{
    const j = await (await fetch("/api/phone?since=" + phSince)).json();
    PH.latest = j.latest; PH.age = j.age; PH.src = j.src; PH.error = j.error; PH.port = j.port; PH.marks = j.marks;
    j.hist.forEach(h => PH.hist.push(h));
    if (PH.hist.length) phSince = PH.hist[PH.hist.length - 1][0];
    const cut = Date.now()/1000 - 120; while (PH.hist.length && PH.hist[0][0] < cut) PH.hist.shift();
  }catch(e){}
  setTimeout(pollPhone, 500);
}
async function pollSignal(){
  try{
    const j = await (await fetch("/api/signal?since=" + sgSince)).json();
    SG.latest = j.latest; SG.age = j.age;
    j.bursts.forEach(b => SG.bursts.push(b));
    if (SG.bursts.length) sgSince = SG.bursts[SG.bursts.length - 1].t;
    const cut = Date.now()/1000 - TL_SPAN - 2; while (SG.bursts.length && SG.bursts[0].t < cut) SG.bursts.shift();
  }catch(e){}
  setTimeout(pollSignal, 300);
}
function sigLoop(){ drawSpectrum(); drawTimeline(); renderSigStats(); renderPhone(devices()); setTimeout(sigLoop, 300); }

$("#simOn").addEventListener("click", () => fetch("/api/sim/start",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({drones:2, max_range:maxRange()})}));
$("#simOff").addEventListener("click", () => fetch("/api/sim/stop",{method:"POST"}));

// ---- live B210 tracking with a calibration preset
async function loadPresets(){
  try{
    const j = await (await fetch("/api/cal/presets")).json();
    const sel = $("#presetSel"); sel.innerHTML = "";
    j.presets.forEach(p => {
      const o = document.createElement("option"); o.value = p.name;
      o.textContent = p.name + "  (" + (p.freq/1e9).toFixed(3) + " GHz, " + (p.quality||"?") + ")";
      if (p.name === j.active) o.selected = true;
      sel.appendChild(o);
    });
    window._presets = j.presets;
    const pickRFA = () => { const p = j.presets.find(q => q.name === sel.value); RFA = p ? (p.rfa_side || null) : null; };
    sel.onchange = pickRFA; pickRFA();
    const o = document.createElement("option"); o.value = "none";
    o.textContent = "none (uncalibrated)"; if (!j.active) o.selected = true; sel.appendChild(o);
    pickRFA();
  }catch(e){}
}
var REC_MODE = "raw";
async function liveState(){
  try{
    const j = await (await fetch("/api/status?since=999999999")).json();
    const live = j.running && j.cmd.includes("live_radar_capture.py") && !j.cmd.includes("--calibrate");
    $("#liveOn").disabled = j.running; $("#liveOff").disabled = !live;
    document.querySelectorAll("#recBar button").forEach(b => b.disabled = j.running);
    $("#recLive").textContent = !live ? "" : j.cmd.includes("--no-record") ? "" :
                                j.cmd.includes("--save-raw") ? "● REC FULL RAW" : "● REC";
    $("#liveStatus").textContent = live ? "● running  " + (j.cmd.match(/--preset (\S+)/)||["","active"])[1] +
                                          (j.cmd.includes("--no-record") ? "  · not recording" :
                                           j.cmd.includes("--save-raw") ? "  · ● REC FULL RAW" :
                                           j.cmd.includes("--save-iq") ? "  · ● REC packets + IQ" : "  · REC packets")
                                 : (j.running ? "another capture is running" : "idle");
  }catch(e){}
  setTimeout(liveState, 1500);
}
$("#liveOn").addEventListener("click", async () => {
  const name = $("#presetSel").value;
  if (name !== "none") await fetch("/api/cal/activate",{method:"POST",headers:{"Content-Type":"application/json"},
                                   body:JSON.stringify({name})});
  const r = await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({profile:"live_radar", values:Object.assign({preset:name},
      {raw:{save_raw:"yes", save_iq:"yes"}, iq:{save_iq:"yes"}, off:{no_record:"yes"}}[REC_MODE] || {})})});
  const j = await r.json(); if (!j.ok) $("#liveStatus").textContent = j.msg;
});
$("#liveOff").addEventListener("click", () => fetch("/api/stop",{method:"POST"}));
async function liveCmd(which){
  const st = $("#zeroStatus"); st.textContent = which === "zero" ? "measuring the phone…" : "swapping…";
  try{
    const r = await (await fetch("/api/live/" + which,{method:"POST",headers:{"Content-Type":"application/json"},
                                 body:JSON.stringify({device:"PHONE"})})).json();
    if (!r.ok){ st.innerHTML = '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>'; return; }
    st.innerHTML = (which === "zero"
      ? '✓ Middle set from ' + r.n + ' packets of ' + esc(r.device) + ' · offsets ' +
        Object.entries(r.phase_by_ch || {}).map(([c, v]) => 'ch' + c + ' ' + v.toFixed(0) + '°').join(', ') +
        (r.mount_deg ? ' · antennas turned ' + r.mount_deg + '°' : '') +
        (r.missing && r.missing.length ? ' <span style="color:#d29922">(no packets on ch ' + r.missing.join(',') + ' - press again)</span>' : '') +
        (r.spread_deg > 25 ? ' <span style="color:#d29922">(phone signal was jumpy: ' + r.spread_deg + '° - keep it still and press again)</span>' : '')
      : '✓ Left/right ' + (r.flip ? 'swapped' : 'back to normal')) +
      '. Saved as preset <b>live</b> for the next Start.';
  }catch(e){ st.textContent = "✗ " + e; }
}
$("#zeroBtn").addEventListener("click", () => liveCmd("zero"));
$("#rangeBtn").addEventListener("click", async () => {
  const st = $("#rangeStatus"), dm = parseFloat($("#rangeM").value);
  if (!(dm > 0)){ st.textContent = "✗ type the distance in metres first"; return; }
  st.textContent = "measuring the phone…";
  try{
    const r = await (await fetch("/api/live/range",{method:"POST",headers:{"Content-Type":"application/json"},
                                 body:JSON.stringify({device:"PHONE", dist_m:dm})})).json();
    st.innerHTML = r.ok
      ? (r.mode === "direction"
          ? '✓ Direction ' + r.angle + '° learned: ' + (r.gain_db >= 0 ? '+' : '') + r.gain_db + ' dB vs reference (' + r.n + ' packets). Points: ' + r.gain_table.length
          : '✓ Reference set at ' + r.angle + '°: ' + r.power_db + ' dB at ' + r.dist_m + ' m (' + r.n + ' packets) &rarr; 1 m level ' + r.ref_power_db.toFixed(1) + ' dB') +
        (r.power_spread_db > 6 ? ' <span style="color:#d29922">(signal varied ' + r.power_spread_db + ' dB: hold the phone still and press again)</span>' : '') +
        '. Saved in preset <b>live</b>.'
      : '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>';
  }catch(e){ st.textContent = "✗ " + e; }
});
$("#flipBtn").addEventListener("click", () => liveCmd("flip"));
$("#mountBtn").addEventListener("click", async () => {
  const st = $("#zeroStatus"), v = parseFloat($("#mountDeg").value);
  if (!(v >= -90 && v <= 90)){ st.textContent = "✗ rotation must be between -90 and 90 degrees"; return; }
  const r = await (await fetch("/api/live/mount",{method:"POST",headers:{"Content-Type":"application/json"},
                               body:JSON.stringify({device:"PHONE", mount_deg:v})})).json();
  st.innerHTML = r.ok ? '✓ Rotation ' + r.mount_deg + '° saved. Now stand at the 0° point and press <b>Set middle</b>.'
                      : '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>';
});
$("#angleBtn").addEventListener("click", async () => {
  const st = $("#angleStatus"), v = parseFloat($("#angleDeg").value);
  if (!(v >= -90 && v <= 90) || Math.abs(v) < 5){ st.textContent = "✗ type the true angle (-90..90, not the middle)"; return; }
  st.textContent = "measuring the phone…";
  const r = await (await fetch("/api/live/angle",{method:"POST",headers:{"Content-Type":"application/json"},
                               body:JSON.stringify({device:"PHONE", true_deg:v})})).json();
  st.innerHTML = r.ok
    ? '✓ ' + v + '° learned (' + r.n + ' packets): measured ' +
      Object.entries(r.channels || {}).map(([c, x]) => 'ch' + c + ' ' + x.measured + '°').join(', ') +
      (r.warning ? ' <span style="color:#d29922">' + esc(r.warning) + '</span>' : '') + '. Saved in preset <b>live</b>.'
    : '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>';
});
$("#aclearBtn").addEventListener("click", async () => {
  const r = await (await fetch("/api/live/aclear",{method:"POST",headers:{"Content-Type":"application/json"},
                               body:JSON.stringify({device:"PHONE"})})).json();
  $("#angleStatus").innerHTML = r.ok ? '✓ Angle points cleared.' : '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>';
});
$("#dclearBtn").addEventListener("click", async () => {
  const r = await (await fetch("/api/live/dclear",{method:"POST",headers:{"Content-Type":"application/json"},
                               body:JSON.stringify({device:"PHONE"})})).json();
  $("#rangeStatus").innerHTML = r.ok ? '✓ Distance points cleared. Next <b>Set distance</b> sets the reference.'
                                     : '<span style="color:#d1242f">✗ ' + esc(r.error || "failed") + '</span>';
});

async function loadRaw(){
  try{
    const j = await (await fetch("/api/raw")).json(), box = $("#rawList");
    $("#rawFree").textContent = "· " + (j.free_bytes/1e9).toFixed(1) + " GB free (~" +
                                Math.max(0, Math.floor((j.free_bytes - 3e9) / 32e6 / 60)) + " min of full raw)";
    if (!j.items.length){ box.textContent = "no raw IQ stored"; }
    else if (!box.querySelector("[data-armed]")){          // don't redraw while a delete is armed
      box.innerHTML = "";
      j.items.forEach(it => {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:6px;margin:3px 0;font:12px var(--mono)";
        row.innerHTML = '<span style="flex:1">' + it.name + ' · ' + (it.bytes/1e9).toFixed(1) + ' GB' +
                        (it.seconds ? ' · ' + Math.round(it.seconds) + ' s' : '') + (it.busy ? ' · ● recording' : '') + '</span>';
        const b = document.createElement("button");
        b.className = "no"; b.textContent = "Delete"; b.disabled = it.busy;
        b.style.cssText = "flex:0 0 auto;padding:2px 8px;font-size:11px";
        b.onclick = async () => {
          if (!b.dataset.armed){
            b.dataset.armed = 1; b.textContent = "Click again to delete " + (it.bytes/1e9).toFixed(1) + " GB";
            setTimeout(() => { if (b.isConnected){ delete b.dataset.armed; b.textContent = "Delete"; } }, 4000);
            return;
          }
          b.disabled = true; b.textContent = "deleting…";
          const r = await (await fetch("/api/raw/delete",{method:"POST",headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({name:it.name})})).json();
          if (!r.ok) b.textContent = r.msg;
          delete b.dataset.armed; loadRaw();
        };
        row.appendChild(b); box.appendChild(row);
      });
    }
  }catch(e){}
}
loadRaw(); setInterval(loadRaw, 5000);
function setRec(m){
  REC_MODE = m;
  document.querySelectorAll("#recBar button").forEach(b => b.classList.toggle("on", b.dataset.rec === m));
  try{ localStorage.setItem("recMode", m); }catch(e){}
}
document.querySelectorAll("#recBar button").forEach(b => b.addEventListener("click", () => setRec(b.dataset.rec)));
let _rm = null; try{ _rm = localStorage.getItem("recMode"); }catch(e){}
setRec(["raw","iq","off"].includes(_rm) ? _rm : "raw");

let NGROK_URL = null;
async function pollNgrok(){
  try{
    const n = await (await fetch("/api/ngrok")).json(), box = $("#ngrokBox");
    NGROK_URL = n.url || null;
    if (n.url){
      box.style.display = ""; $("#ngrokUrl").textContent = n.url.replace(/^https?:\/\//, "");
      box.style.color = n.phone_port_ok === false ? "#d29922" : "var(--muted)";
      if (n.phone_port_ok === false) box.title = "ngrok points at " + n.target + ", not the phone port 5080. Run: ngrok http 5080";
    } else {
      box.style.display = ""; $("#ngrokUrl").textContent = "off"; $("#ngrokCopy").textContent = "";
      box.title = (n.note || "ngrok not running") + ". Start it with: ngrok http 5080 (Docker: --profile ngrok)";
    }
  }catch(e){}
  setTimeout(pollNgrok, 5000);
}
$("#ngrokBox").addEventListener("click", async () => {
  if (!NGROK_URL) return;
  try{ await navigator.clipboard.writeText(NGROK_URL); $("#ngrokCopy").textContent = "copied ✓"; }
  catch(e){ window.prompt("Copy the phone link:", NGROK_URL); }
  setTimeout(() => { $("#ngrokCopy").textContent = "copy"; }, 1500);
});

async function pollUsb(){
  try{
    const u = await (await fetch("/api/usrp")).json(), el = $("#usbInfo");
    if (u.connected === false){
      el.textContent = "USRP: not connected"; el.style.color = "#f85149"; el.style.borderColor = "#f8514966";
    } else if (u.booting){
      el.textContent = "USRP plugged in · firmware not loaded yet (real USB speed shows after Start live)";
      el.style.color = "#7d8da0"; el.style.borderColor = "#243040";
      el.title = "Before UHD loads its firmware the B210 always connects at USB 2 speed, even on a USB 3 port.";
    } else if (u.connected){
      el.textContent = (u.product || "USRP").replace("USRP ", "") + " · " + u.usb + (u.rate ? " (" + u.rate + ")" : "") +
                       (u.fast_enough === false ? " ⚠ plug into a USB 3 port" : "") + (u.uhd ? " · UHD: " + u.uhd : "");
      const c = u.fast_enough ? "#3fb950" : "#d29922";
      el.style.color = c; el.style.borderColor = c + "66";
      el.title = "serial " + u.serial + " · macOS link speed: " + u.speed +
                 (u.fast_enough === false ? "\nUSB 2 can't keep up with 2 channels at 4 MHz reliably: expect overflows (O's in the log)." : "");
    } else {
      el.textContent = "USRP: unknown";
    }
  }catch(e){}
  setTimeout(pollUsb, 4000);
}

resize(); poll(); loadPresets(); liveState(); pollPhone(); pollSignal(); sigLoop(); pollUsb(); pollNgrok();
</script>
</body>
</html>
"""


CAL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calibration</title>
<style>
  :root{
    --bg:#0a0d12; --panel:#121820; --panel2:#0d1117; --border:#243040; --fg:#e6edf3;
    --muted:#7d8da0; --accent:#2f81f7; --green:#3fb950; --amber:#d29922; --red:#f85149;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:12px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
  header h1{font-size:16px;margin:0}
  header .sub{color:var(--muted);font-size:12px}
  a.nav{color:var(--accent);text-decoration:none;font-size:13px;margin-left:14px}
  .wrap{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 49px)}
  .side{border-right:1px solid var(--border);padding:16px;overflow:auto}
  .main{padding:24px 32px;max-width:860px}
  .sect{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:0 0 10px}
  .preset{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px}
  .preset.active{border-color:var(--green)}
  .preset .nm{font-weight:600;display:flex;align-items:center;gap:8px}
  .preset .kv{font:12px var(--mono);color:var(--muted);margin-top:4px}
  .badge{font-size:10px;padding:1px 7px;border-radius:10px;background:#1b2330;color:var(--muted);font-weight:600}
  .badge.good,.badge.on{background:#163020;color:var(--green)}
  .badge.ok{background:#2b2616;color:var(--amber)}
  .badge.poor{background:#331a1a;color:var(--red)}
  button{border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-size:13px;font-weight:600;
         cursor:pointer;background:#1b2330;color:var(--fg)}
  button.go{background:var(--green);color:#03160a;border-color:transparent}
  button.pri{background:var(--accent);color:#fff;border-color:transparent}
  button.no{background:#30242a;color:#f0a0a0}
  button.sm{padding:4px 10px;font-size:12px}
  button:disabled{opacity:.4;cursor:not-allowed}
  .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .steps{display:flex;gap:6px;margin-bottom:22px}
  .steps div{flex:1;font-size:12px;color:var(--muted);border-top:3px solid var(--border);padding-top:6px}
  .steps div.cur{color:var(--fg);border-color:var(--accent)}
  .steps div.done{color:var(--green);border-color:var(--green)}
  h2{font-size:20px;margin:0 0 6px}
  p.lead{color:var(--muted);margin:0 0 18px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px 18px}
  label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px}
  input,select{width:100%;background:var(--panel2);border:1px solid var(--border);color:var(--fg);
        border-radius:6px;padding:8px 10px;font:13px var(--mono)}
  .hint{font-size:11px;color:var(--muted);margin-top:3px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;margin:16px 0}
  .tip{border-left:3px solid var(--accent);background:#0f1724;padding:10px 14px;border-radius:4px;color:#b8c7d9;font-size:13px;margin:14px 0}
  .warn{border-left-color:var(--amber)} .err{border-left-color:var(--red);color:#f0b0b0}
  .bar{height:8px;background:var(--panel2);border-radius:4px;overflow:hidden;margin:10px 0}
  .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .4s}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  .stat{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px}
  .stat .k{font-size:11px;color:var(--muted)} .stat .v{font:600 18px var(--mono)}
  .scene{display:flex;justify-content:center;margin:6px 0 4px}
  .empty{color:var(--muted);font-size:13px}
  .hidden{display:none}
  pre.log{background:#070a0e;border:1px solid var(--border);border-radius:6px;padding:8px;font:11px var(--mono);
          color:var(--muted);max-height:140px;overflow:auto;white-space:pre-wrap}
</style>
</head>
<body>
<header>
  <h1>Calibration</h1>
  <span class="sub">B210 2-antenna array &middot; phase offset + 1 m reference power &middot; saved as reusable presets</span>
  <span style="flex:1"></span>
  <a class="nav" href="/">Capture Console</a>
  <a class="nav" href="/radar">Live Radar &rarr;</a>
</header>
<div class="wrap">
  <div class="side">
    <div class="sect">Saved presets</div>
    <div id="presets"><div class="empty">Loading&hellip;</div></div>
    <button class="pri" style="width:100%;margin-top:6px" id="newBtn">+ New calibration</button>
  </div>

  <div class="main">
    <div class="steps" id="steps">
      <div data-s="1">1 &middot; Setup</div><div data-s="2">2 &middot; Phone in front</div>
      <div data-s="3">3 &middot; Direction check</div><div data-s="4">4 &middot; Save</div>
    </div>

    <!-- STEP 1 -->
    <section data-step="1">
      <h2>New calibration</h2>
      <p class="lead">One preset per antenna setup. Make a new one whenever you move or swap the antennas or cables, or change the band or gain.</p>
      <div class="grid">
        <div><label>Preset name</label><input id="f_name" placeholder="e.g. lab-desk"></div>
        <div><label>BLE advertising channel</label>
          <select id="f_freq">
            <option value="2402000000">ch 37 &middot; 2.402 GHz</option>
            <option value="2426000000">ch 38 &middot; 2.426 GHz</option>
            <option value="2480000000" selected>ch 39 &middot; 2.480 GHz (best, above Wi-Fi)</option>
          </select></div>
        <div><label>RX gain (dB)</label><input id="f_gain" value="50"><div class="hint">Use the same gain for live runs. 40–60 is typical.</div></div>
        <div><label>Antenna port (both RF A and RF B)</label>
          <select id="f_ant"><option value="TX/RX" selected>TX/RX</option><option value="RX2">RX2</option></select>
          <div class="hint">The SMA port your two antennas are screwed onto.</div></div>
        <div><label>Antenna spacing (cm)</label><input id="f_spacing" value="7"><div class="hint" id="spHint">Center to center. Blank = &lambda;/2.</div></div>
        <div><label>Measure time (s)</label><input id="f_dur" value="10"></div>
        <div><label>Standing behind the antennas, looking out, RF A is on my</label>
          <select id="f_rfa"><option value="left" selected>LEFT  (RF B on the right)</option>
                             <option value="right">RIGHT (RF B on the left)</option></select>
          <div class="hint">Check the labels next to the SMA ports on the B210.</div></div>
      </div>
      <div class="tip">The B210 decodes real BLE packets, so every device shows up with its address and maker (e.g. <b>Apple Nearby</b>). <b>Unlock your iPhone and keep the screen on</b>; it advertises far more often then. Keep other Apple devices a few metres away so yours is clearly the strongest.</div>
      <div class="row"><button class="pri" id="toStep2">Next &rarr;</button></div>
    </section>

    <!-- STEP 2 -->
    <section data-step="2" class="hidden">
      <h2>Place the phone 1 m straight ahead</h2>
      <p class="lead">Stand the phone upright <b>1 m</b> from the middle of the two antennas, on the line perpendicular to them (0&deg;). Keep it still and stay out of the line between them.</p>
      <div class="scene"><svg width="300" height="170" viewBox="0 0 300 170">
        <line x1="150" y1="150" x2="150" y2="22" stroke="#2f81f7" stroke-dasharray="4 4"/>
        <text x="158" y="90" fill="#7d8da0" font-size="12" font-family="monospace">1 m</text>
        <rect x="100" y="146" width="100" height="14" rx="3" fill="#1b2330" stroke="#243040"/>
        <text x="150" y="157" fill="#7d8da0" font-size="10" text-anchor="middle" font-family="monospace">B210</text>
        <line x1="125" y1="146" x2="125" y2="122" stroke="#e6edf3" stroke-width="3"/><circle cx="125" cy="120" r="3" fill="#e6edf3"/>
        <line x1="175" y1="146" x2="175" y2="122" stroke="#e6edf3" stroke-width="3"/><circle cx="175" cy="120" r="3" fill="#e6edf3"/>
        <text class="lblL" x="100" y="118" fill="#7d8da0" font-size="10" font-family="monospace">RF A</text>
        <text class="lblR" x="182" y="118" fill="#7d8da0" font-size="10" font-family="monospace">RF B</text>
        <text x="20" y="160" fill="#4a5a6e" font-size="10" font-family="monospace">◀ your left</text>
        <text x="280" y="160" fill="#4a5a6e" font-size="10" text-anchor="end" font-family="monospace">your right ▶</text>
        <rect x="141" y="4" width="18" height="30" rx="3" fill="#3fb950"/>
        <text x="170" y="22" fill="#3fb950" font-size="12" font-family="monospace">phone (0°)</text>
      </svg></div>
      <div class="card">
        <div class="row" style="margin-top:0">
          <button class="go" id="measureFront">Start measuring</button>
          <span id="frontState" class="empty" style="align-self:center"></span>
        </div>
        <div class="bar"><i id="frontBar"></i></div>
        <div class="stats">
          <div class="stat"><div class="k">BLE packets decoded</div><div class="v" id="s_bursts">&ndash;</div></div>
          <div class="stat"><div class="k">from locked device</div><div class="v" id="s_n">&ndash;</div></div>
          <div class="stat"><div class="k">phone power</div><div class="v" id="s_p">&ndash;</div></div>
          <div class="stat"><div class="k">phase stability</div><div class="v" id="s_spread">&ndash;</div></div>
        </div>
        <div id="devBox" style="margin-top:12px"></div>
        <div id="frontMsg"></div>
      </div>
      <div class="row">
        <button id="back1">&larr; Back</button>
        <button class="pri" id="toStep3" disabled>Next &rarr;</button>
      </div>
    </section>

    <!-- STEP 3 -->
    <section data-step="3" class="hidden">
      <h2>Direction check</h2>
      <p class="lead">Stand behind the antennas facing out. Move the phone about <b>45&deg; to your RIGHT</b>, toward the <b id="sideAnt">RF B</b> antenna, still about 1 m away. This checks that left and right are not mirrored and fixes it if they are.</p>
      <div class="scene"><svg width="300" height="170" viewBox="0 0 300 170">
        <line x1="150" y1="150" x2="150" y2="22" stroke="#243040" stroke-dasharray="4 4"/>
        <line x1="150" y1="150" x2="240" y2="60" stroke="#2f81f7" stroke-dasharray="4 4"/>
        <rect x="100" y="146" width="100" height="14" rx="3" fill="#1b2330" stroke="#243040"/>
        <line x1="125" y1="146" x2="125" y2="122" stroke="#e6edf3" stroke-width="3"/>
        <line x1="175" y1="146" x2="175" y2="122" stroke="#e6edf3" stroke-width="3"/>
        <text class="lblL" x="100" y="118" fill="#7d8da0" font-size="10" font-family="monospace">RF A</text>
        <text class="lblR" x="182" y="118" fill="#7d8da0" font-size="10" font-family="monospace">RF B</text>
        <text x="20" y="160" fill="#4a5a6e" font-size="10" font-family="monospace">◀ your left</text>
        <text x="280" y="160" fill="#4a5a6e" font-size="10" text-anchor="end" font-family="monospace">your right ▶</text>
        <rect x="236" y="40" width="18" height="30" rx="3" fill="#3fb950"/>
        <text id="sidePhoneLbl" x="228" y="30" fill="#3fb950" font-size="12" text-anchor="end" font-family="monospace">phone (right, RF B side)</text>
        <text x="150" y="16" fill="#7d8da0" font-size="10" text-anchor="middle" font-family="monospace">0°</text>
      </svg></div>
      <div class="card">
        <div class="row" style="margin-top:0">
          <button class="go" id="measureSide">Measure</button>
          <span id="sideState" class="empty" style="align-self:center"></span>
        </div>
        <div class="bar"><i id="sideBar"></i></div>
        <div id="sideMsg"></div>
      </div>
      <div class="row">
        <button id="back2">&larr; Back</button>
        <button id="skip3">Skip</button>
        <button class="pri" id="toStep4" disabled>Next &rarr;</button>
      </div>
    </section>

    <!-- STEP 4 -->
    <section data-step="4" class="hidden">
      <h2>Save preset</h2>
      <p class="lead">This preset becomes the active one, and live tracking uses it automatically.</p>
      <div class="card" id="summary"></div>
      <div class="row">
        <button id="back3">&larr; Back</button>
        <button class="go" id="saveBtn">Save &amp; use</button>
      </div>
      <div id="savedMsg"></div>
    </section>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const C = 299792458;
let W = {};          // wizard state: setup + front result + side result

function showStep(n){
  document.querySelectorAll("section[data-step]").forEach(s => s.classList.toggle("hidden", +s.dataset.step !== n));
  document.querySelectorAll("#steps div").forEach(d => {
    const s = +d.dataset.s; d.className = s < n ? "done" : (s === n ? "cur" : "");
  });
}
function esc(t){ return String(t).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
const wrap = a => ((a + 180) % 360 + 360) % 360 - 180;

// ---------------- presets list
async function loadPresets(){
  const j = await (await fetch("/api/cal/presets")).json();
  const box = $("#presets");
  if (!j.presets.length){ box.innerHTML = '<div class="empty">No presets yet. Create one &rarr;</div>'; return; }
  box.innerHTML = "";
  j.presets.forEach(p => {
    const act = p.name === j.active;
    const el = document.createElement("div"); el.className = "preset" + (act ? " active" : "");
    el.innerHTML =
      '<div class="nm">' + esc(p.name) + (act ? ' <span class="badge on">active</span>' : '') +
      ' <span class="badge ' + (p.quality||"") + '">' + (p.quality||"?") + '</span>' +
      (p.flip ? ' <span class="badge">L/R flipped</span>' : '') + '</div>' +
      '<div class="kv">' + (p.freq/1e9).toFixed(3) + ' GHz &middot; gain ' + p.gain + ' dB &middot; d ' + (p.spacing_m*100).toFixed(2) + ' cm &middot; ' + esc(p.antenna || "RX2 (old)") + '</div>' +
      '<div class="kv">offset ' + p.phase_deg.toFixed(1) + '&deg; (&plusmn;' + (p.phase_spread_deg||0).toFixed(0) + ') &middot; 1 m ' + p.ref_power_db.toFixed(1) + ' dB</div>' +
      '<div class="kv">RF A on my <select data-side style="width:auto;padding:1px 4px;font-size:11px">' +
        '<option value="">?</option><option value="left"' + (p.rfa_side === "left" ? " selected" : "") + '>left</option>' +
        '<option value="right"' + (p.rfa_side === "right" ? " selected" : "") + '>right</option></select>' +
        (p.rfa_side ? ' · left = ' + antOn("left", p.rfa_side) + ', right = ' + antOn("right", p.rfa_side) : ' (set this)') + '</div>' +
      (p.device ? '<div class="kv">phone ' + esc(p.device_desc||"") + ' ' + esc(p.device) + '</div>' : '') +
      '<div class="kv">' + esc(p.created||"") + (p.direction_check ? ' &middot; dir ' + esc(p.direction_check) : '') + '</div>' +
      '<div class="row">' +
        (act ? '' : '<button class="sm go" data-use>Use</button>') +
        '<button class="sm" data-radar>Track live &rarr;</button>' +
        '<button class="sm no" data-del>Delete</button></div>';
    el.querySelector("[data-side]").addEventListener("change", async ev => {
      if (ev.target.value) { await post("/api/cal/set_side", {name: p.name, rfa_side: ev.target.value}); loadPresets(); }
    });
    el.querySelector("[data-use]")?.addEventListener("click", async () => { await post("/api/cal/activate",{name:p.name}); loadPresets(); });
    el.querySelector("[data-radar]").addEventListener("click", async () => {
      await post("/api/cal/activate",{name:p.name});
      const r = await post("/api/start",{profile:"live_radar", values:{preset:p.name}});
      if (!r.ok && !confirmBusy(r.msg)) return;
      location.href = "/radar";
    });
    const del = el.querySelector("[data-del]");
    del.addEventListener("click", async () => {
      if (del.dataset.armed){ await post("/api/cal/delete",{name:p.name}); loadPresets(); }
      else { del.dataset.armed = 1; del.textContent = "Click again to delete";
             setTimeout(() => { delete del.dataset.armed; del.textContent = "Delete"; }, 3000); }
    });
    box.appendChild(el);
  });
}
function confirmBusy(msg){ return /already running/.test(msg); }   // radar shows the running job anyway
async function post(url, body){
  const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  return r.json();
}

// ---------------- measurement runner (reuses the live script in --calibrate mode)
async function measure(kind){
  const dur = Math.max(3, parseFloat(W.dur) || 10);
  const bar = $("#" + kind + "Bar"), state = $("#" + kind + "State"), msg = $("#" + kind + "Msg");
  const btn = $(kind === "front" ? "#measureFront" : "#measureSide");
  msg.innerHTML = ""; bar.style.width = "0";
  const vals = {calibrate: dur, freq: W.freq, gain: W.gain, preset: "none", antenna: W.antenna};
  if (W.spacing_m) vals.spacing = W.spacing_m;
  if (kind === "side" && W.front && W.front.device) vals.target = W.front.device;
  else if (kind === "front" && W.target) vals.target = W.target;
  let r = await post("/api/start", {profile: "live_radar", values: vals});
  if (!r.ok && /already running/.test(r.msg)){
    msg.innerHTML = '<div class="tip warn">Another capture (probably live tracking) is using the B210. ' +
                    '<button class="sm no" id="stopOther">Stop it and measure</button></div>';
    $("#stopOther").onclick = async () => { await post("/api/stop"); await new Promise(z => setTimeout(z, 1200)); measure(kind); };
    return null;
  }
  if (!r.ok){ msg.innerHTML = '<div class="tip err">' + esc(r.msg) + '</div>'; return null; }
  btn.disabled = true; state.textContent = "starting the B210 (about 4 s)…";
  let since = 0, result = null, tail = [];
  while (true){
    await new Promise(z => setTimeout(z, 500));
    const j = await (await fetch("/api/status?since=" + since)).json();
    since = j.next;
    for (const o of j.lines){
      const t = o.t; tail.push(t); if (tail.length > 12) tail.shift();
      if (t.startsWith("CAL_PROGRESS ")){
        const p = JSON.parse(t.slice(13));
        state.textContent = "measuring… " + p.t.toFixed(0) + " / " + p.dur.toFixed(0) + " s";
        bar.style.width = Math.min(100, 100 * p.t / p.dur) + "%";
        if (kind === "front") showStats(p);
      } else if (t.startsWith("CAL_RESULT ")){
        result = JSON.parse(t.slice(11));
      }
    }
    if (!j.running) break;
  }
  btn.disabled = false; bar.style.width = "100%";
  if (!result){
    state.textContent = "failed";
    msg.innerHTML = '<div class="tip err">The measurement did not finish. Last output:</div><pre class="log">' + esc(tail.join("\n")) + '</pre>';
    return null;
  }
  if (!result.ok){
    state.textContent = "not enough signal";
    msg.innerHTML = '<div class="tip err">' + esc(result.error) + ' (' + (result.decoded ?? 0) + ' BLE packets decoded)</div>';
    if (kind === "front") showDevices(result);
    return null;
  }
  state.textContent = "done";
  if (kind === "front"){ result.final = true; showStats(result); }
  return result;
}
function showDevices(p){
  const devs = p.devices || [];
  if (!devs.length){ $("#devBox").innerHTML = '<div class="empty">No BLE advertisers decoded yet…</div>'; return; }
  const locked = p.device ? p.device.addr || p.device : null;
  $("#devBox").innerHTML = '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Devices heard (strongest first) &middot; pick yours</div>' +
    devs.map(d => '<div style="display:flex;align-items:center;gap:10px;font:12px var(--mono);padding:5px 8px;border:1px solid ' +
      (d.addr === locked ? 'var(--green)' : 'var(--border)') + ';border-radius:6px;margin-bottom:5px">' +
      '<span style="flex:1">' + esc(d.desc) + ' &middot; ' + esc(d.addr) + '</span><span>' + d.n + ' pkts</span><span>' + d.p.toFixed(1) + ' dB</span>' +
      (d.addr === locked ? '<span class="badge on">locked</span>' :
        (p.final ? '<button class="sm" data-pick="' + esc(d.addr) + '">Use this</button>' : '')) + '</div>').join("");
  document.querySelectorAll("[data-pick]").forEach(b => b.onclick = () => { W.target = b.dataset.pick; $("#measureFront").click(); });
}
function showStats(p){
  showDevices(p);
  $("#s_bursts").textContent = p.decoded ?? "–";
  $("#s_n").textContent = p.n_bursts ?? "–";
  $("#s_p").textContent = p.ref_power_db != null ? p.ref_power_db.toFixed(1) + " dB" : "–";
  $("#s_spread").textContent = p.phase_spread_deg != null ? "±" + p.phase_spread_deg.toFixed(0) + "°" : "–";
}

// ---------------- wizard wiring
function lambda(){ return C / parseFloat($("#f_freq").value); }
// which antenna sits on the user's left/right (looking out from behind the array)
function antOn(side, rfa){ rfa = rfa || W.rfa_side || $("#f_rfa").value; return (side === rfa) ? "RF A" : "RF B"; }
function applyOrientation(){
  document.querySelectorAll(".lblL").forEach(t => t.textContent = antOn("left"));
  document.querySelectorAll(".lblR").forEach(t => t.textContent = antOn("right"));
  $("#sideAnt").textContent = antOn("right");
  $("#sidePhoneLbl").textContent = "phone (right, " + antOn("right") + " side)";
}
$("#f_rfa").addEventListener("change", applyOrientation);
function updSpHint(){ $("#spHint").textContent = "Center to center. Blank = λ/2 = " + (lambda()*50).toFixed(2) + " cm."; }
$("#f_freq").addEventListener("change", updSpHint);

$("#newBtn").onclick = () => { W = {}; showStep(1); $("#f_name").focus(); };
$("#toStep2").onclick = () => {
  const name = $("#f_name").value.trim();
  if (!/[A-Za-z0-9]/.test(name)){ $("#f_name").focus(); $("#f_name").style.borderColor = "var(--red)"; return; }
  $("#f_name").style.borderColor = "";
  const sp = parseFloat($("#f_spacing").value);
  W = {name, freq: parseFloat($("#f_freq").value), gain: parseFloat($("#f_gain").value) || 50,
       spacing_m: sp > 0 ? sp / 100 : lambda() / 2, dur: $("#f_dur").value, antenna: $("#f_ant").value,
       rfa_side: $("#f_rfa").value};
  applyOrientation();
  W.front = null; W.side = null; W.target = null; $("#devBox").innerHTML = "";
  $("#toStep3").disabled = true; $("#frontMsg").innerHTML = ""; $("#frontState").textContent = "";
  $("#frontBar").style.width = "0"; ["#s_bursts","#s_n","#s_p","#s_spread"].forEach(s => $(s).textContent = "–");
  showStep(2);
};
$("#measureFront").onclick = async () => {
  const r = await measure("front"); if (!r) return;
  W.front = r;
  const q = r.quality;
  const txt = q === "good" ? "Clean, stable measurement."
            : q === "ok" ? "Usable. To improve it, move the phone so nothing is in the way, or move other Bluetooth devices further off."
            : "Phase is unstable, likely reflections or a second device nearby. Retry before saving.";
  $("#frontMsg").innerHTML = '<div class="tip ' + (q === "poor" ? "warn" : "") + '"><span class="badge ' + q + '">' + q + '</span> ' +
     txt + '<br>Locked on <b>' + esc(r.device_desc) + ' ' + esc(r.device) + '</b>. If that is not your phone, press <b>Use this</b> on the right one.' +
     '<br><span style="font-family:var(--mono);font-size:12px">offset ' + r.phase_deg.toFixed(1) + '° · 1 m power ' + r.ref_power_db.toFixed(1) + ' dB</span></div>';
  if (r.ref_power_db > -12) $("#frontMsg").innerHTML += '<div class="tip warn">The phone is very loud (' + r.ref_power_db.toFixed(1) +
     ' dB, near the B210 maximum). Go back and lower the gain to about ' + Math.round(W.gain - (r.ref_power_db + 20)) + ' dB, then measure again.</div>';
  $("#measureFront").textContent = "Measure again";
  $("#toStep3").disabled = false;
};
$("#back1").onclick = () => showStep(1);
$("#toStep3").onclick = () => { $("#sideMsg").innerHTML = ""; $("#sideState").textContent = ""; $("#sideBar").style.width = "0";
                                $("#toStep4").disabled = !W.side; showStep(3); };
$("#measureSide").onclick = async () => {
  const r = await measure("side"); if (!r) return;
  const diff = wrap(r.phase_deg - W.front.phase_deg);
  const s = Math.max(-1, Math.min(1, diff / 360 * lambda() / W.spacing_m));
  const ang = Math.asin(s) * 180 / Math.PI;
  let html;
  const R_ANT = antOn("right"), L_ANT = antOn("left");
  if (Math.abs(ang) < 12){
    html = '<div class="tip warn">The phone read ' + Math.abs(ang).toFixed(0) + '° from straight ahead, too close to the centre to judge. ' +
           'Move it further to your right, toward ' + R_ANT + ', and measure again.</div>';
    W.side = null;
  } else if (ang > 0){
    W.side = {flip: false, check: "ok: " + ang.toFixed(0) + "° right (" + R_ANT + " side)"};
    html = '<div class="tip">The phone was located <b>' + ang.toFixed(0) + '° to the RIGHT, on the ' + R_ANT + ' side</b>, where you put it. ' +
           'Left (' + L_ANT + ') and right (' + R_ANT + ') are correct.</div>';
  } else {
    W.side = {flip: true, check: "flipped: read " + Math.abs(ang).toFixed(0) + "° left (" + L_ANT + " side)"};
    html = '<div class="tip warn">The phone was located ' + Math.abs(ang).toFixed(0) + '° to the <b>LEFT, the ' + L_ANT + ' side</b>, ' +
           'but you put it on the right (' + R_ANT + ' side), so the picture is mirrored. The preset will flip it: ' +
           R_ANT + ' side = right on the radar.</div>';
  }
  $("#sideMsg").innerHTML = html;
  $("#measureSide").textContent = "Measure again";
  $("#toStep4").disabled = !W.side;
};
$("#back2").onclick = () => showStep(2);
$("#skip3").onclick = () => { W.side = {flip: false, check: "skipped (sides not verified)"}; toSummary(); };
$("#toStep4").onclick = toSummary;
$("#back3").onclick = () => showStep(3);
function toSummary(){
  const f = W.front;
  $("#summary").innerHTML =
    '<div style="font:13px/1.9 var(--mono)">' +
    'name&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>' + esc(W.name) + '</b><br>' +
    'band&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;' + (W.freq/1e9).toFixed(3) + ' GHz &middot; gain ' + W.gain + ' dB &middot; d ' + (W.spacing_m*100).toFixed(2) + ' cm &middot; port ' + esc(W.antenna) + '<br>' +
    'device&nbsp;&nbsp;&nbsp;&nbsp;' + esc(f.device_desc) + ' ' + esc(f.device) + '<br>' +
    'offset&nbsp;&nbsp;&nbsp;&nbsp;' + f.phase_deg.toFixed(1) + '° (±' + f.phase_spread_deg.toFixed(0) + '°, ' + f.quality + ')<br>' +
    '1 m power ' + f.ref_power_db.toFixed(1) + ' dB<br>' +
    'sides&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;left = ' + antOn("left") + ' · right = ' + antOn("right") + '<br>' +
    'direction ' + esc(W.side.check) + (W.side.flip ? ' → bearings flipped' : '') + '</div>';
  $("#savedMsg").innerHTML = ""; $("#saveBtn").disabled = false;
  showStep(4);
}
$("#saveBtn").onclick = async () => {
  const f = W.front;
  const r = await post("/api/cal/save", {
    name: W.name, phase_deg: f.phase_deg, ref_power_db: f.ref_power_db, phase_spread_deg: f.phase_spread_deg,
    n_bursts: f.n_bursts, n_total: f.n_total, quality: f.quality, freq: W.freq, gain: W.gain,
    spacing_m: W.spacing_m, flip: W.side.flip, direction_check: W.side.check,
    device: f.device, device_desc: f.device_desc, antenna: W.antenna, rfa_side: W.rfa_side});
  if (!r.ok){ $("#savedMsg").innerHTML = '<div class="tip err">' + esc(r.msg) + '</div>'; return; }
  $("#saveBtn").disabled = true;
  $("#savedMsg").innerHTML = '<div class="tip">Saved <b>' + esc(r.preset.name) + '</b> and set it active. ' +
     '<button class="sm go" id="goLive">Start live tracking &rarr;</button></div>';
  $("#goLive").onclick = async () => { await post("/api/start",{profile:"live_radar", values:{preset:r.preset.name}}); location.href = "/radar"; };
  loadPresets();
};

updSpHint(); applyOrientation(); showStep(1); loadPresets();
</script>
</body>
</html>
"""


def main():
    print("=" * 60)
    print("  Capture Console")
    print("=" * 60)
    print(f"  Project root : {ROOT}")
    print(f"  Interpreter  : {sys.executable}")
    shown = "localhost" if HOST in ("0.0.0.0", "") else HOST
    print(f"  Open         : http://{shown}:{PORT}")
    print(f"  Live radar   : http://{shown}:{PORT}/radar")
    print(f"  Calibration  : http://{shown}:{PORT}/calibrate")
    print(f"  Feed file    : {FEED_FILE}  (append JSON lines to feed the radar)")
    print("  Ctrl+C to quit")
    print("=" * 60)
    print(f"  Phone (UDP)  : port {PHONE_PORT}  (USRP Beacon app -> /radar, logs in {PHONE_LOG_DIR.name}/)")
    threading.Thread(target=feed_file_tailer, args=(FEED, FEED_FILE), daemon=True).start()
    threading.Thread(target=PHONE.run, daemon=True).start()
    threading.Thread(target=SIGNAL.run, daemon=True).start()
    threading.Thread(target=USB.run, daemon=True).start()
    try:
        # Phone-only port: also reachable from the same Wi-Fi (http://<this Mac's IP>:5080), which is
        # steadier than going out through ngrok and back. PHONE_INGEST_HOST=127.0.0.1 turns that off.
        ingest = ThreadingHTTPServer((os.environ.get("PHONE_INGEST_HOST", "0.0.0.0"), PHONE_HTTP_PORT), PhoneIngestHandler)
        threading.Thread(target=ingest.serve_forever, daemon=True).start()
        print(f"  Phone (HTTP) : http://127.0.0.1:{PHONE_HTTP_PORT}/phone  (tunnel with: ngrok http {PHONE_HTTP_PORT})")
    except OSError as e:
        print(f"[phone] HTTP ingest not started: {e}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        if JOB.proc and JOB.proc.poll() is None:
            JOB.stop()


if __name__ == "__main__":
    main()
