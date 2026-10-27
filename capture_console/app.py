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
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST, PORT = "127.0.0.1", 8099

# Project root = parent of this file's directory (…/Thesis)
ROOT = Path(__file__).resolve().parent.parent

# Live detection feed file: capture code appends one JSON object per line, e.g.
#   {"id":"drone1","label":"DJI Mavic","angle":22.5,"distance":48.0,"conf":0.9}
# The server tails it and pushes detections to the radar page.
FEED_FILE = Path(__file__).resolve().parent / "feed.jsonl"


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
            P("freq", "--freq", "number", 5.8e9, False, "Center frequency Hz - B210 70e6..6e9"),
            P("rate", "--rate", "number", 10e6, False, "Sample rate Hz - B210 max ~56e6"),
            P("gain", "--gain", "number", 40, False, "RX gain dB - B210 0..76"),
            P("window", "--window", "number", 10, False, "Block length ms"),
            P("duration", "--duration", "number", 0, False, "Seconds to run (0 = until stopped)"),
            P("channels", "--channels", "text", "0,1", False, "RX channels (0,1 = 2-ch MUSIC bearing)"),
            P("device_args", "--device-args", "text", "", False, "UHD device args, e.g. type=b200"),
            P("min_power_db", "--min-power-db", "number", -75, False, "Noise gate: emit only above this dB"),
            P("label", "--label", "text", "drone", False, "Track label"),
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

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            self._emit("[console] stopping (SIGTERM)...")
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._emit("[console] force killing (SIGKILL)...")
                p.kill()
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
  .wrap{display:grid;grid-template-columns:1fr 300px;height:calc(100vh - 49px)}
  .scope{position:relative;min-width:0;display:flex;align-items:center;justify-content:center;padding:10px}
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
</style>
</head>
<body>
<header>
  <h1>Live Radar</h1>
  <span class="sub">bearing (MUSIC DoA) &middot; range (path-loss) &middot; forward half-plane, B210 2-ch</span>
  <span style="flex:1"></span>
  <a class="nav" href="/">&larr; Capture Console</a>
</header>
<div class="wrap">
  <div class="scope"><canvas id="ppi"></canvas></div>
  <div class="side">
    <div class="ctl">
      <label>Max range (m)</label>
      <input id="maxRange" value="100">
    </div>
    <div class="ctl">
      <label>Simulator (no SDR needed)</label>
      <div class="btns">
        <button class="go" id="simOn">Start sim</button>
        <button class="no" id="simOff">Stop</button>
      </div>
    </div>
    <div class="sect">Active tracks</div>
    <div id="tracks"><div class="empty">No detections yet.</div></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const cv = $("#ppi"), ctx = cv.getContext("2d");
let nextI = 0;
let tracks = {};           // id -> {label, source, points:[{t,angle,distance,conf}]}
const TRAIL = 18, TTL = 8; // keep last 18 points, drop tracks idle > 8s

function resize(){
  const box = $(".scope").getBoundingClientRect();
  const s = Math.min(box.width, box.height) - 20;
  const dpr = window.devicePixelRatio || 1;
  cv.width = s * dpr; cv.height = s * dpr;
  cv.style.width = s + "px"; cv.style.height = s + "px";
  ctx.setTransform(dpr,0,0,dpr,0,0);
}
window.addEventListener("resize", resize);

function maxRange(){ return Math.max(1, parseFloat($("#maxRange").value) || 100); }

function toXY(angle, dist, cx, cy, R){
  const r = Math.min(1, dist / maxRange()) * R;
  const rad = angle * Math.PI / 180;
  return [cx + r * Math.sin(rad), cy - r * Math.cos(rad)];
}

function draw(){
  const S = cv.clientWidth;             // square css px
  const cx = S/2, cy = S - 26, R = S - 52;
  ctx.clearRect(0,0,S,S);

  // range rings (semicircle) + labels
  ctx.strokeStyle = "#1d2836"; ctx.fillStyle = "#4a5a6e";
  ctx.font = "10px " + "ui-monospace,monospace"; ctx.textAlign = "left";
  const rings = 5;
  for (let k=1;k<=rings;k++){
    const r = R * k/rings;
    ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, 2*Math.PI); ctx.stroke();
    ctx.fillText(Math.round(maxRange()*k/rings) + " m", cx + 3, cy - r + 11);
  }
  // angle spokes every 30deg + labels
  ctx.textAlign = "center";
  for (let a=-90; a<=90; a+=30){
    const [x,y] = toXY(a, maxRange(), cx, cy, R);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(x,y); ctx.stroke();
    const [lx,ly] = toXY(a, maxRange()*1.07, cx, cy, R);
    ctx.fillText((a>0?"+":"") + a + "°", lx, ly);
  }
  // boresight marker
  ctx.fillStyle = "#4a5a6e"; ctx.textAlign="center";
  ctx.fillText("0° (boresight)", cx, cy + 16);

  // tracks
  const now = Date.now()/1000;
  for (const id in tracks){
    const tk = tracks[id];
    const pts = tk.points;
    if (!pts.length) continue;
    if (now - pts[pts.length-1].t > TTL){ delete tracks[id]; continue; }
    // trail
    for (let j=0;j<pts.length;j++){
      const p = pts[j];
      const [x,y] = toXY(p.angle, p.distance, cx, cy, R);
      const alpha = (j+1)/pts.length;
      ctx.fillStyle = tk.source==="sim"
        ? "rgba(210,153,34,"+(0.15+0.5*alpha)+")"
        : "rgba(63,185,80,"+(0.15+0.6*alpha)+")";
      ctx.beginPath(); ctx.arc(x,y, j===pts.length-1?5:2.2, 0, 7); ctx.fill();
    }
    // head label
    const h = pts[pts.length-1];
    const [hx,hy] = toXY(h.angle, h.distance, cx, cy, R);
    ctx.fillStyle = "#cfe3ff"; ctx.textAlign="left"; ctx.font="11px ui-monospace,monospace";
    ctx.fillText(tk.label + "  " + h.distance.toFixed(0) + "m  " + (h.angle>0?"+":"") + h.angle.toFixed(0) + "°",
                 hx + 8, hy - 6);
  }
}

function renderSide(){
  const box = $("#tracks");
  const ids = Object.keys(tracks);
  if (!ids.length){ box.innerHTML = '<div class="empty">No detections yet.</div>'; return; }
  box.innerHTML = "";
  ids.forEach(id => {
    const tk = tracks[id]; const h = tk.points[tk.points.length-1];
    const el = document.createElement("div"); el.className="track";
    el.innerHTML =
      '<div class="name">'+tk.label+'<span class="pill '+(tk.source==="sim"?"sim":"")+'">'+tk.source+'</span></div>'+
      '<div class="rowv"><span>bearing</span><b>'+(h.angle>0?"+":"")+h.angle.toFixed(1)+'&deg;</b></div>'+
      '<div class="rowv"><span>distance</span><b>'+h.distance.toFixed(1)+' m</b></div>'+
      '<div class="rowv"><span>confidence</span><b>'+(h.conf*100).toFixed(0)+'%</b></div>';
    box.appendChild(el);
  });
}

async function poll(){
  try{
    const r = await fetch("/api/feed?since="+nextI);
    const j = await r.json();
    nextI = j.next;
    j.items.forEach(d => {
      const tk = tracks[d.id] || (tracks[d.id] = {label:d.label, source:d.source, points:[]});
      tk.label = d.label; tk.source = d.source;
      tk.points.push({t:d.t, angle:d.angle, distance:d.distance, conf:d.conf});
      if (tk.points.length > TRAIL) tk.points.shift();
    });
    $("#simOn").disabled = j.sim_running;
    $("#simOff").disabled = !j.sim_running;
  }catch(e){}
  draw(); renderSide();
  setTimeout(poll, 300);
}

$("#simOn").addEventListener("click", () => fetch("/api/sim/start",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({drones:2, max_range:maxRange()})}));
$("#simOff").addEventListener("click", () => fetch("/api/sim/stop",{method:"POST"}));

resize(); poll();
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
    print(f"  Open         : http://{HOST}:{PORT}")
    print(f"  Live radar   : http://{HOST}:{PORT}/radar")
    print(f"  Feed file    : {FEED_FILE}  (append JSON lines to feed the radar)")
    print("  Ctrl+C to quit")
    print("=" * 60)
    threading.Thread(target=feed_file_tailer, args=(FEED, FEED_FILE), daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        if JOB.proc and JOB.proc.poll() is None:
            JOB.proc.terminate()


if __name__ == "__main__":
    main()
