#!/usr/bin/env python3
"""
Packet Capture + Algorithm Tuning Demo  (SIMULATED backend, stdlib only).

Run:
    python3 app.py            # open http://127.0.0.1:8777

Nothing touches a real interface. The simulator emits packets that carry a
HIDDEN ground-truth label (benign / jammer / replay / spoof) plus realistic
features, so every tuning layer has something real to act on:

  1. DSP Capture  - RSSI gate + measurement-noise knobs that decide what is
                    captured at all and how clean the features are.
  2. Detection    - a transparent rule-based classifier (sensitivity + per
                    attack weights) scored live against ground truth.
  3. ML Model     - a pure-Python logistic-regression detector you retrain
                    on the captured buffer, with accuracy + confusion matrix.

Mirrors the thesis pipeline (DJI / OcuSync RF attack detection) without any
dependencies, so it runs anywhere.
"""

import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST, PORT = "127.0.0.1", 8777
MAX_PACKETS = 3000

LABELS = ["benign", "jammer", "replay", "spoof"]
ATTACKS = ["jammer", "replay", "spoof"]
BAND_CHANNELS = {"2.4 GHz": [1, 6, 11], "5.8 GHz": [149, 153, 157, 161, 165]}
FEATURE_ORDER = ["rssi", "rssi_var", "duty", "hop_rate", "dup_count",
                 "entropy", "src_mismatch"]

DEFAULTS = {
    "capture": {
        "interface": "wlan0mon", "band": "2.4 GHz", "channel": 6,
        "filter": "", "snaplen": 262144, "rate": 10,
        "attack_pct": 35, "running": False,
    },
    "dsp": {
        "rssi_threshold": -88,   # packets weaker than this are not captured
        "fft_size": 1024,        # bigger -> cleaner feature estimates
        "sample_rate": 20,       # MS/s (cosmetic + freq-bin resolution)
        "hop_window": 8,         # smoothing window for hop-rate estimate
        "dwell_ms": 50,          # longer dwell -> steadier duty estimate
    },
    "detection": {
        "sensitivity": 0.50,     # score needed to flag an attack
        "jammer_w": 1.0, "replay_w": 1.0, "spoof_w": 1.0,
    },
    "ml": {  # last-used hyperparameters, for display
        "lr": 0.5, "epochs": 300, "l2": 0.001, "threshold": 0.5,
    },
}

# Ground-truth feature distributions: (mean, std) per label per feature.
PROFILES = {
    "benign": {"rssi": (-70, 8), "rssi_var": (3, 1.2), "duty": (0.10, 0.05),
               "hop_rate": (12, 1.5), "dup_count": (0, 0.6), "entropy": (0.90, 0.05),
               "src_mismatch": (0.0, 0.05)},
    "jammer": {"rssi": (-46, 5), "rssi_var": (12, 3), "duty": (0.85, 0.08),
               "hop_rate": (2.5, 1.0), "dup_count": (0, 0.6), "entropy": (0.45, 0.1),
               "src_mismatch": (0.0, 0.05)},
    "replay": {"rssi": (-66, 6), "rssi_var": (3.5, 1.2), "duty": (0.30, 0.08),
               "hop_rate": (12, 1.5), "dup_count": (8, 2.0), "entropy": (0.88, 0.05),
               "src_mismatch": (0.1, 0.1)},
    "spoof":  {"rssi": (-60, 6), "rssi_var": (4, 1.5), "duty": (0.15, 0.06),
               "hop_rate": (11, 1.8), "dup_count": (1, 0.8), "entropy": (0.85, 0.06),
               "src_mismatch": (1.0, 0.08)},
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Detection (rule-based) — shared by live generation and the Detection tab.
# ---------------------------------------------------------------------------

def classify(feat, det):
    """Transparent rule-based detector. Returns (predicted_label, score)."""
    jam = (feat["duty"] + feat["rssi_var"] / 15.0
           + max(0.0, feat["rssi"] + 60) / 20.0) / 3.0
    rep = (feat["dup_count"] / 10.0) * 1.5
    spf = feat["src_mismatch"] * 1.2
    scores = {
        "jammer": det["jammer_w"] * jam,
        "replay": det["replay_w"] * rep,
        "spoof":  det["spoof_w"] * spf,
    }
    best = max(scores, key=scores.get)
    if scores[best] >= det["sensitivity"]:
        return best, round(scores[best], 3)
    return "benign", round(scores[best], 3)


# ---------------------------------------------------------------------------
# Pure-Python logistic regression for the ML tab.
# ---------------------------------------------------------------------------

def train_logreg(X, y, lr, epochs, l2):
    n, d = len(X), len(X[0])
    cols = list(zip(*X))
    means = [sum(c) / n for c in cols]
    stds = [(sum((v - means[j]) ** 2 for v in cols[j]) / n) ** 0.5 or 1.0
            for j in range(d)]
    Xs = [[(x[j] - means[j]) / stds[j] for j in range(d)] for x in X]
    w, b = [0.0] * d, 0.0
    for _ in range(epochs):
        gw, gb = [0.0] * d, 0.0
        for i in range(n):
            z = b + sum(w[j] * Xs[i][j] for j in range(d))
            p = 1.0 / (1.0 + math.exp(-clamp(z, -30, 30)))
            err = p - y[i]
            for j in range(d):
                gw[j] += err * Xs[i][j]
            gb += err
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return {"w": w, "b": b, "means": means, "stds": stds}


def predict_proba(model, x):
    z = model["b"] + sum(model["w"][j] * (x[j] - model["means"][j]) / model["stds"][j]
                         for j in range(len(x)))
    return 1.0 / (1.0 + math.exp(-clamp(z, -30, 30)))


# ---------------------------------------------------------------------------
# Capture state
# ---------------------------------------------------------------------------

class CaptureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.capture = dict(DEFAULTS["capture"])
        self.dsp = dict(DEFAULTS["dsp"])
        self.detection = dict(DEFAULTS["detection"])
        self.ml = dict(DEFAULTS["ml"])
        self.packets = []
        self.seq = 0
        self.total_seen = 0
        self.gated = 0
        self.started_at = None

    # ---- settings -------------------------------------------------------
    def update_capture(self, p):
        with self.lock:
            c = self.capture
            if "interface" in p: c["interface"] = str(p["interface"])[:32]
            if p.get("band") in BAND_CHANNELS:
                c["band"] = p["band"]
                if c["channel"] not in BAND_CHANNELS[c["band"]]:
                    c["channel"] = BAND_CHANNELS[c["band"]][0]
            for k, lo, hi in [("channel", 1, 200), ("snaplen", 64, 4194304),
                              ("rate", 1, 200), ("attack_pct", 0, 90)]:
                if k in p:
                    try: c[k] = int(clamp(int(p[k]), lo, hi))
                    except (TypeError, ValueError): pass
            if "filter" in p: c["filter"] = str(p["filter"])[:120]
            return dict(c)

    def update_dsp(self, p):
        with self.lock:
            d = self.dsp
            for k, lo, hi in [("rssi_threshold", -120, -20), ("fft_size", 64, 8192),
                              ("sample_rate", 1, 100), ("hop_window", 1, 64),
                              ("dwell_ms", 1, 500)]:
                if k in p:
                    try: d[k] = int(clamp(int(p[k]), lo, hi))
                    except (TypeError, ValueError): pass
            return dict(d)

    def update_detection(self, p):
        with self.lock:
            d = self.detection
            for k, lo, hi in [("sensitivity", 0.0, 3.0), ("jammer_w", 0.0, 5.0),
                              ("replay_w", 0.0, 5.0), ("spoof_w", 0.0, 5.0)]:
                if k in p:
                    try: d[k] = round(clamp(float(p[k]), lo, hi), 3)
                    except (TypeError, ValueError): pass
            return dict(d)

    def set_running(self, run):
        with self.lock:
            self.capture["running"] = bool(run)
            if run and self.started_at is None:
                self.started_at = time.time()

    def clear(self):
        with self.lock:
            self.packets.clear()
            self.total_seen = self.gated = 0
            self.started_at = time.time() if self.capture["running"] else None

    # ---- reads ----------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return {
                "capture": dict(self.capture), "dsp": dict(self.dsp),
                "detection": dict(self.detection), "ml": dict(self.ml),
                "stats": {
                    "captured": len(self.packets), "total_seen": self.total_seen,
                    "gated": self.gated, "last_seq": self.seq,
                    "uptime": (time.time() - self.started_at) if self.started_at else 0,
                },
            }

    def packets_since(self, since):
        with self.lock:
            out = []
            for p in self.packets:
                if p["seq"] > since:
                    q = {k: p[k] for k in ("seq", "ts", "proto", "src", "dst",
                                           "length", "rssi", "freq", "band",
                                           "iface", "info", "label", "pred", "score")}
                    out.append(q)
            return out

    def detection_stats(self):
        """Recompute the rule-based detector over the buffer with CURRENT
        weights, so tuning gives instant feedback."""
        with self.lock:
            det = dict(self.detection)
            rows = [(p["_feat"], p["label"]) for p in self.packets]
        matrix = {t: {pch: 0 for pch in LABELS} for t in LABELS}
        correct = 0
        for feat, truth in rows:
            pred, _ = classify(feat, det)
            matrix[truth][pred] += 1
            if pred == truth:
                correct += 1
        n = len(rows)
        recalls = {}
        for a in ATTACKS:
            tot = sum(matrix[a].values())
            recalls[a] = round(matrix[a][a] / tot, 3) if tot else None
        return {"n": n, "accuracy": round(correct / n, 3) if n else None,
                "matrix": matrix, "recall": recalls, "labels": LABELS}

    def train_ml(self, lr, epochs, l2, threshold):
        with self.lock:
            data = [([p["_feat"][f] for f in FEATURE_ORDER],
                     0 if p["label"] == "benign" else 1) for p in self.packets]
            self.ml = {"lr": lr, "epochs": epochs, "l2": l2, "threshold": threshold}
        if len(data) < 30:
            return {"error": "Need >=30 captured packets. Let the capture run."}
        pos = sum(y for _, y in data)
        if pos == 0 or pos == len(data):
            return {"error": "Need both attack and benign samples "
                             "(raise/lower attack %% and re-capture)."}
        rng = random.Random(42)
        rng.shuffle(data)
        split = int(len(data) * 0.7)
        train, test = data[:split], data[split:]
        model = train_logreg([x for x, _ in train], [y for _, y in train],
                             lr, epochs, l2)
        tp = tn = fp = fn = 0
        for x, y in test:
            pred = 1 if predict_proba(model, x) >= threshold else 0
            if y == 1 and pred == 1: tp += 1
            elif y == 0 and pred == 0: tn += 1
            elif y == 0 and pred == 1: fp += 1
            else: fn += 1
        acc = (tp + tn) / len(test)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        weights = {FEATURE_ORDER[j]: round(model["w"][j], 3)
                   for j in range(len(FEATURE_ORDER))}
        return {"n_train": len(train), "n_test": len(test),
                "accuracy": round(acc, 3), "precision": round(prec, 3),
                "recall": round(rec, 3), "f1": round(f1, 3),
                "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
                "weights": weights}

    # ---- generation -----------------------------------------------------
    def generate_one(self):
        with self.lock:
            c, dsp, det = dict(self.capture), dict(self.dsp), dict(self.detection)

        # 1. ground-truth label
        if random.random() * 100 < c["attack_pct"]:
            label = random.choice(ATTACKS)
        else:
            label = "benign"

        # 2. true features from the profile
        prof = PROFILES[label]
        true = {f: random.gauss(*prof[f]) for f in FEATURE_ORDER}

        # 3. DSP measurement effects: bigger FFT / hop window -> less noise
        noise_scale = 1.0 / math.sqrt(dsp["fft_size"] / 256.0)
        hop_noise = 1.0 / math.sqrt(dsp["hop_window"])
        dwell_noise = 1.0 / math.sqrt(dsp["dwell_ms"] / 10.0)
        feat = {
            "rssi": true["rssi"] + random.gauss(0, 2 * noise_scale),
            "rssi_var": clamp(true["rssi_var"] * (1 + random.gauss(0, 0.2 * noise_scale)), 0, 40),
            "duty": clamp(true["duty"] + random.gauss(0, 0.05 * dwell_noise), 0, 1),
            "hop_rate": clamp(true["hop_rate"] + random.gauss(0, 1.5 * hop_noise), 0, 30),
            "dup_count": max(0.0, true["dup_count"] + random.gauss(0, 0.4)),
            "entropy": clamp(true["entropy"] + random.gauss(0, 0.03), 0, 1),
            "src_mismatch": clamp(true["src_mismatch"], 0, 1),
        }

        with self.lock:
            self.total_seen += 1
            # 4. RSSI gate: too weak -> never captured
            if feat["rssi"] < dsp["rssi_threshold"]:
                self.gated += 1
                return

        # 5. cosmetic display fields
        band, ch = c["band"], c["channel"]
        freq = (2412 + (ch - 1) * 5) if band == "2.4 GHz" else (5000 + ch * 5)
        proto = random.choice(["OcuSync", "WiFi-MGMT", "WiFi-DATA", "UDP", "TCP"])
        src = f"AA:BB:{random.randint(0,255):02X}:{random.randint(0,255):02X}:" \
              f"{random.randint(0,255):02X}:{random.randint(0,255):02X}"
        dst = f"DJI:{random.randint(0,255):02X}:{random.randint(0,255):02X}"
        length = random.randint(42, min(c["snaplen"], 1514))
        info = {"benign": "nominal link", "jammer": "broadband energy burst",
                "replay": "repeated frame seq", "spoof": "mismatched source id"}[label]

        # 6. rule-based detection at capture time
        pred, score = classify(feat, det)

        pkt = {"proto": proto, "src": src, "dst": dst, "length": length,
               "rssi": round(feat["rssi"], 1), "freq": freq, "channel": ch,
               "band": band, "iface": c["interface"], "info": info,
               "label": label, "pred": pred, "score": score, "_feat": feat}

        # 7. display filter
        flt = c["filter"].lower()
        if flt:
            hay = f"{proto} {src} {dst} {info} {label} {pred}".lower()
            if flt not in hay:
                return

        with self.lock:
            self.seq += 1
            pkt["seq"] = self.seq
            pkt["ts"] = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
            self.packets.append(pkt)
            if len(self.packets) > MAX_PACKETS:
                del self.packets[: len(self.packets) - MAX_PACKETS]


STATE = CaptureState()


def generator_loop():
    while True:
        with STATE.lock:
            run, rate = STATE.capture["running"], STATE.capture["rate"]
        if run:
            STATE.generate_one()
            time.sleep(1.0 / max(1, rate))
        else:
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            self._json(STATE.snapshot())
        elif u.path == "/api/packets":
            since = int((parse_qs(u.query).get("since", ["0"]))[0] or 0)
            self._json({"packets": STATE.packets_since(since)})
        elif u.path == "/api/detstats":
            self._json(STATE.detection_stats())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/capture":
            self._json({"capture": STATE.update_capture(self._body())})
        elif u.path == "/api/dsp":
            self._json({"dsp": STATE.update_dsp(self._body())})
        elif u.path == "/api/detection":
            self._json({"detection": STATE.update_detection(self._body())})
        elif u.path == "/api/control":
            a = self._body().get("action")
            if a == "start": STATE.set_running(True)
            elif a == "stop": STATE.set_running(False)
            elif a == "clear": STATE.clear()
            self._json(STATE.snapshot())
        elif u.path == "/api/train":
            b = self._body()
            try:
                lr = clamp(float(b.get("lr", 0.5)), 0.001, 5)
                epochs = int(clamp(int(b.get("epochs", 300)), 10, 3000))
                l2 = clamp(float(b.get("l2", 0.001)), 0, 1)
                thr = clamp(float(b.get("threshold", 0.5)), 0.05, 0.95)
            except (TypeError, ValueError):
                return self._json({"error": "bad hyperparameters"}, 400)
            self._json(STATE.train_ml(lr, epochs, l2, thr))
        else:
            self._json({"error": "not found"}, 404)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture + Tuning Demo</title>
<style>
  :root{--bg:#0e1116;--panel:#161b22;--border:#2a3139;--text:#e6edf3;--muted:#8b949e;
        --accent:#2f81f7;--green:#3fb950;--red:#f85149;--amber:#d29922;--chip:#21262d}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
  header{display:flex;align-items:center;gap:14px;padding:12px 18px;border-bottom:1px solid var(--border);background:var(--panel);flex-wrap:wrap}
  header h1{font-size:15px;margin:0;font-weight:600;margin-right:auto}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--red)}
  .dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
  .mini{color:var(--muted);font-size:12px}
  button{padding:8px 14px;border:none;border-radius:7px;font:inherit;font-weight:600;cursor:pointer;color:#fff;background:var(--accent)}
  button.stop{background:var(--red)} button.ghost{background:var(--chip);color:var(--text)}
  button:active{transform:translateY(1px)}
  .tabs{display:flex;gap:4px;padding:10px 18px 0;border-bottom:1px solid var(--border);background:var(--panel);flex-wrap:wrap}
  .tab{padding:9px 16px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--muted);font-weight:600;border:1px solid transparent;border-bottom:none}
  .tab.active{color:var(--text);background:var(--bg);border-color:var(--border)}
  .view{padding:18px;display:none} .view.active{display:block}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
  .panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
  label{display:block} label span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
  label .val{float:right;color:var(--text);font-variant-numeric:tabular-nums}
  input,select{width:100%;padding:8px 10px;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:7px;font:inherit}
  input[type=range]{padding:0}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  .hint{color:var(--muted);font-size:12px;margin-top:6px}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);font-size:13px;white-space:nowrap}
  th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
  .scroll{max-height:calc(100vh - 320px);overflow:auto;border:1px solid var(--border);border-radius:10px}
  .tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;background:var(--chip)}
  .t-benign{background:#3fb95022;color:#7ee787}.t-jammer{background:#f8514922;color:#ff7b72}
  .t-replay{background:#d2992222;color:#e3b341}.t-spoof{background:#1f6feb22;color:#79c0ff}
  .ok{color:var(--green)}.bad{color:var(--red)}
  .stats{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);margin-bottom:10px}
  .stats b{color:var(--text)}
  .big{font-size:26px;font-weight:700}
  .cm{display:inline-grid;grid-template-columns:auto auto auto;gap:4px;margin-top:8px}
  .cm div{padding:8px 14px;background:var(--chip);border-radius:6px;text-align:center;min-width:70px}
  .cm .hd{background:transparent;color:var(--muted);font-size:12px}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span><h1>Packet Capture + Algorithm Tuning</h1>
  <label style="width:150px"><span>Rate <span class="val" id="rateV">10</span>/s</span>
    <input id="rate" type="range" min="1" max="120" value="10"></label>
  <button id="toggle">Start</button>
  <button id="clear" class="ghost">Clear</button>
</header>

<div class="tabs">
  <div class="tab active" data-v="stream">Stream</div>
  <div class="tab" data-v="dsp">DSP Capture</div>
  <div class="tab" data-v="detection">Detection</div>
  <div class="tab" data-v="ml">ML Model</div>
</div>

<!-- STREAM -->
<div class="view active" id="v-stream">
  <div class="panel">
    <h2>Capture</h2>
    <div class="grid">
      <label><span>Interface</span><input id="interface" value="wlan0mon"></label>
      <label><span>Band</span><select id="band"><option>2.4 GHz</option><option>5.8 GHz</option></select></label>
      <label><span>Channel</span><select id="channel"></select></label>
      <label><span>Filter (substring)</span><input id="filter" placeholder="OcuSync, jammer, spoof..."></label>
      <label><span>Snaplen</span><input id="snaplen" type="number" value="262144"></label>
      <label><span>Attack injection <span class="val" id="attackV">35</span>%</span><input id="attack_pct" type="range" min="0" max="90" value="35"></label>
    </div>
  </div>
  <div class="panel">
    <h2>Live stream — truth vs. rule detector</h2>
    <div class="stats">
      <div>Captured <b id="s-cap">0</b></div><div>Seen <b id="s-seen">0</b></div>
      <div>Gated by RSSI <b id="s-gate">0</b></div><div>Uptime <b id="s-up">0s</b></div>
    </div>
    <div class="scroll"><table><thead><tr>
      <th>#</th><th>Time</th><th>Proto</th><th>Freq</th><th>RSSI</th>
      <th>Truth</th><th>Detected</th><th>Score</th><th>Info</th>
    </tr></thead><tbody id="rows"></tbody></table></div>
  </div>
</div>

<!-- DSP -->
<div class="view" id="v-dsp">
  <div class="panel"><h2>DSP capture parameters</h2>
    <div class="grid">
      <label><span>RSSI gate <span class="val" id="rssiV">-88</span> dBm</span><input id="rssi_threshold" type="range" min="-120" max="-20" value="-88"></label>
      <label><span>FFT size <span class="val" id="fftV">1024</span></span><input id="fft_size" type="range" min="64" max="8192" step="64" value="1024"></label>
      <label><span>Sample rate <span class="val" id="srV">20</span> MS/s</span><input id="sample_rate" type="range" min="1" max="100" value="20"></label>
      <label><span>Hop window <span class="val" id="hopV">8</span></span><input id="hop_window" type="range" min="1" max="64" value="8"></label>
      <label><span>Dwell <span class="val" id="dwellV">50</span> ms</span><input id="dwell_ms" type="range" min="1" max="500" value="50"></label>
    </div>
    <div class="hint">RSSI gate drops weaker packets before capture. Larger FFT, hop window and dwell reduce measurement noise on the features the detectors see.</div>
  </div>
  <div class="panel"><h2>Capture gate effect</h2>
    <div class="stats"><div>Captured <b id="d-cap">0</b></div><div>Seen <b id="d-seen">0</b></div>
      <div>Gated out <b id="d-gate">0</b></div><div>Gate ratio <b id="d-ratio">0%</b></div></div>
    <div class="hint">Raise the RSSI gate toward 0 dBm to capture fewer, stronger packets; lower it to capture weak ones (and more noise).</div>
  </div>
</div>

<!-- DETECTION -->
<div class="view" id="v-detection">
  <div class="panel"><h2>Rule-based detector</h2>
    <div class="grid">
      <label><span>Sensitivity (flag threshold) <span class="val" id="senV">0.50</span></span><input id="sensitivity" type="range" min="0" max="2" step="0.05" value="0.5"></label>
      <label><span>Jammer weight <span class="val" id="jwV">1.0</span></span><input id="jammer_w" type="range" min="0" max="3" step="0.1" value="1"></label>
      <label><span>Replay weight <span class="val" id="rwV">1.0</span></span><input id="replay_w" type="range" min="0" max="3" step="0.1" value="1"></label>
      <label><span>Spoof weight <span class="val" id="swV">1.0</span></span><input id="spoof_w" type="range" min="0" max="3" step="0.1" value="1"></label>
    </div>
    <div class="hint">Lower sensitivity = more detections (more false positives). Per-attack weights scale each attack's signature. Stats below recompute live over the captured buffer.</div>
  </div>
  <div class="panel"><h2>Live performance vs. ground truth</h2>
    <div class="stats">
      <div>Accuracy <b id="det-acc" class="big">—</b></div>
      <div>Jammer recall <b id="rec-jammer">—</b></div>
      <div>Replay recall <b id="rec-replay">—</b></div>
      <div>Spoof recall <b id="rec-spoof">—</b></div>
      <div>Over <b id="det-n">0</b> pkts</div>
    </div>
    <div id="det-cm"></div>
  </div>
</div>

<!-- ML -->
<div class="view" id="v-ml">
  <div class="panel"><h2>Logistic-regression detector (attack vs. benign)</h2>
    <div class="grid">
      <label><span>Learning rate <span class="val" id="lrV">0.5</span></span><input id="lr" type="range" min="0.01" max="2" step="0.01" value="0.5"></label>
      <label><span>Epochs <span class="val" id="epV">300</span></span><input id="epochs" type="range" min="20" max="1500" step="20" value="300"></label>
      <label><span>L2 reg <span class="val" id="l2V">0.001</span></span><input id="l2" type="range" min="0" max="0.2" step="0.001" value="0.001"></label>
      <label><span>Decision threshold <span class="val" id="thV">0.50</span></span><input id="threshold" type="range" min="0.05" max="0.95" step="0.05" value="0.5"></label>
    </div>
    <button id="train" style="margin-top:14px">Train on captured buffer</button>
    <span class="hint" id="train-hint" style="margin-left:10px"></span>
  </div>
  <div class="panel"><h2>Results (held-out 30% test split)</h2>
    <div class="stats">
      <div>Accuracy <b id="ml-acc" class="big">—</b></div>
      <div>Precision <b id="ml-prec">—</b></div><div>Recall <b id="ml-rec">—</b></div>
      <div>F1 <b id="ml-f1">—</b></div><div>Train/Test <b id="ml-nn">—</b></div>
    </div>
    <div id="ml-cm"></div>
    <div class="hint" id="ml-weights" style="margin-top:12px"></div>
  </div>
</div>

<script>
const BAND_CHANNELS={"2.4 GHz":[1,6,11],"5.8 GHz":[149,153,157,161,165]};
const $=id=>document.getElementById(id);
let running=false,lastSeq=0;

// tabs
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); $("v-"+t.dataset.v).classList.add("active");
});

function fillChannels(band,sel){const s=$("channel");s.innerHTML="";
  BAND_CHANNELS[band].forEach(c=>{const o=document.createElement("option");
    o.value=c;o.textContent=c;if(c==sel)o.selected=true;s.appendChild(o);});}

async function post(url,body){return fetch(url,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}

// capture controls
function captureBody(){return{interface:$("interface").value,band:$("band").value,
  channel:parseInt($("channel").value),filter:$("filter").value,
  snaplen:parseInt($("snaplen").value)||262144,rate:parseInt($("rate").value),
  attack_pct:parseInt($("attack_pct").value)};}
["interface","filter","snaplen"].forEach(id=>$(id).onchange=()=>post("/api/capture",captureBody()));
$("band").onchange=()=>{fillChannels($("band").value);post("/api/capture",captureBody());};
$("channel").onchange=()=>post("/api/capture",captureBody());
$("rate").oninput=()=>{$("rateV").textContent=$("rate").value;};
$("rate").onchange=()=>post("/api/capture",captureBody());
$("attack_pct").oninput=()=>{$("attackV").textContent=$("attack_pct").value;};
$("attack_pct").onchange=()=>post("/api/capture",captureBody());

$("toggle").onclick=async()=>{await post("/api/capture",captureBody());
  await post("/api/control",{action:running?"stop":"start"});running=!running;renderStatus();};
$("clear").onclick=async()=>{await post("/api/control",{action:"clear"});
  $("rows").innerHTML="";lastSeq=0;};

// dsp
function dspBody(){return{rssi_threshold:+$("rssi_threshold").value,fft_size:+$("fft_size").value,
  sample_rate:+$("sample_rate").value,hop_window:+$("hop_window").value,dwell_ms:+$("dwell_ms").value};}
[["rssi_threshold","rssiV"],["fft_size","fftV"],["sample_rate","srV"],["hop_window","hopV"],["dwell_ms","dwellV"]]
  .forEach(([id,v])=>{$(id).oninput=()=>$(v).textContent=$(id).value;$(id).onchange=()=>post("/api/dsp",dspBody());});

// detection
function detBody(){return{sensitivity:+$("sensitivity").value,jammer_w:+$("jammer_w").value,
  replay_w:+$("replay_w").value,spoof_w:+$("spoof_w").value};}
[["sensitivity","senV"],["jammer_w","jwV"],["replay_w","rwV"],["spoof_w","swV"]]
  .forEach(([id,v])=>{$(id).oninput=()=>$(v).textContent=(+$(id).value).toFixed(2);
    $(id).onchange=()=>post("/api/detection",detBody());});

// ml
[["lr","lrV"],["epochs","epV"],["l2","l2V"],["threshold","thV"]]
  .forEach(([id,v])=>$(id).oninput=()=>$(v).textContent=$(id).value);
$("train").onclick=async()=>{
  $("train-hint").textContent="training...";
  const r=await post("/api/train",{lr:+$("lr").value,epochs:+$("epochs").value,
    l2:+$("l2").value,threshold:+$("threshold").value});
  const d=await r.json();
  if(d.error){$("train-hint").textContent=d.error;return;}
  $("train-hint").textContent="done";
  $("ml-acc").textContent=(d.accuracy*100).toFixed(1)+"%";
  $("ml-prec").textContent=d.precision;$("ml-rec").textContent=d.recall;$("ml-f1").textContent=d.f1;
  $("ml-nn").textContent=d.n_train+" / "+d.n_test;
  const c=d.confusion;
  $("ml-cm").innerHTML=`<div class="cm">
    <div class="hd"></div><div class="hd">pred benign</div><div class="hd">pred attack</div>
    <div class="hd">true benign</div><div>${c.tn}</div><div>${c.fp}</div>
    <div class="hd">true attack</div><div>${c.fn}</div><div>${c.tp}</div></div>`;
  $("ml-weights").textContent="Learned feature weights: "+
    Object.entries(d.weights).map(([k,v])=>k+"="+v).join("  ");
};

function renderStatus(){$("dot").classList.toggle("on",running);
  $("toggle").textContent=running?"Stop":"Start";$("toggle").classList.toggle("stop",running);}

function row(p){const c=t=>"t-"+t;
  const mark=p.label===p.pred?'<span class="ok">✓</span>':'<span class="bad">✗</span>';
  return `<tr><td>${p.seq}</td><td>${p.ts}</td><td>${p.proto}</td><td>${p.freq}</td>
    <td>${p.rssi}</td><td><span class="tag ${c(p.label)}">${p.label}</span></td>
    <td><span class="tag ${c(p.pred)}">${p.pred}</span> ${mark}</td>
    <td>${p.score}</td><td class="mini">${p.info}</td></tr>`;}

async function pollPackets(){try{
  const d=await (await fetch("/api/packets?since="+lastSeq)).json();
  if(d.packets&&d.packets.length){const tb=$("rows");
    d.packets.forEach(p=>{lastSeq=Math.max(lastSeq,p.seq);tb.insertAdjacentHTML("afterbegin",row(p));});
    while(tb.children.length>400)tb.removeChild(tb.lastChild);}
}catch(e){}}

async function pollState(){try{
  const d=await (await fetch("/api/state")).json();const st=d.stats;
  $("s-cap").textContent=st.captured;$("s-seen").textContent=st.total_seen;
  $("s-gate").textContent=st.gated;$("s-up").textContent=Math.floor(st.uptime)+"s";
  $("d-cap").textContent=st.captured;$("d-seen").textContent=st.total_seen;$("d-gate").textContent=st.gated;
  $("d-ratio").textContent=(st.total_seen?Math.round(st.gated/st.total_seen*100):0)+"%";
  running=d.capture.running;renderStatus();
}catch(e){}}

async function pollDet(){try{
  const d=await (await fetch("/api/detstats")).json();
  $("det-n").textContent=d.n;
  $("det-acc").textContent=d.accuracy==null?"—":(d.accuracy*100).toFixed(1)+"%";
  ["jammer","replay","spoof"].forEach(a=>$("rec-"+a).textContent=d.recall[a]==null?"—":d.recall[a]);
  if(d.n){const L=d.labels;let h='<div class="cm" style="grid-template-columns:repeat('+(L.length+1)+',auto)">';
    h+='<div class="hd">truth \\ pred</div>'+L.map(l=>'<div class="hd">'+l+'</div>').join("");
    L.forEach(t=>{h+='<div class="hd">'+t+'</div>'+L.map(p=>'<div>'+d.matrix[t][p]+'</div>').join("");});
    h+='</div>';$("det-cm").innerHTML=h;}
}catch(e){}}

(async()=>{const d=await(await fetch("/api/state")).json();
  const c=d.capture;$("interface").value=c.interface;$("band").value=c.band;fillChannels(c.band,c.channel);
  $("filter").value=c.filter;$("snaplen").value=c.snaplen;$("rate").value=c.rate;$("rateV").textContent=c.rate;
  $("attack_pct").value=c.attack_pct;$("attackV").textContent=c.attack_pct;
  running=c.running;renderStatus();
  setInterval(pollPackets,400);setInterval(pollState,1000);setInterval(pollDet,1200);
})();
</script>
</body></html>
"""


def main():
    threading.Thread(target=generator_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Capture + Tuning Demo on http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
