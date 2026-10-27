# Packet Capture + Algorithm Tuning Demo

A web dashboard for a **simulated** capture pipeline that mirrors the thesis
flow (DJI / OcuSync RF attack detection). The simulator emits packets carrying
a **hidden ground-truth label** (benign / jammer / replay / spoof) plus
realistic features, so each tuning layer has something real to act on. It
touches **no real interface** and needs **no dependencies** — standard library
Python only.

## Run

```bash
cd demo
python3 app.py
```

Open <http://127.0.0.1:8777>. Press **Start** (top bar).

If the port is busy from an old run:

```bash
lsof -nP -iTCP:8777 -sTCP:LISTEN     # find the PID
kill -9 <PID>
```

## The four tabs

### Stream
Live packet table showing **truth vs. the rule detector's prediction** (✓/✗
per row), plus capture controls: interface, band/channel, substring filter,
snaplen, and **attack-injection %** (how often an attack is simulated).

### DSP Capture — *what gets captured, and how clean*
| Knob | Effect |
|------|--------|
| RSSI gate | Packets weaker than this are dropped before capture (watch "gated" count) |
| FFT size | Larger → less measurement noise on features |
| Sample rate | Freq-bin resolution |
| Hop window | Smoothing of the hop-rate estimate |
| Dwell | Longer → steadier duty-cycle estimate |

### Detection — *transparent rule-based classifier*
Tune **sensitivity** (flag threshold) and a **per-attack weight** for jammer /
replay / spoof. The panel recomputes **accuracy, per-attack recall, and a
confusion matrix live** over the captured buffer against ground truth, so you
see the precision/recall trade-off as you drag a slider.

### ML Model — *logistic-regression detector (attack vs. benign)*
Set learning rate, epochs, L2 regularization, and decision threshold, then
**Train on the captured buffer**. Results show accuracy / precision / recall /
F1, a confusion matrix on a held-out 30% test split, and the learned feature
weights. Pure-Python implementation — no scikit-learn needed.

## Making the problem harder (so metrics actually move)

The default profiles are cleanly separable, so the ML tab can hit ~100%. To
stress the detectors like real captures:

- Lower **FFT size**, **hop window**, and **dwell** (DSP tab) → noisier
  features → detection and ML accuracy drop.
- Lower the **RSSI gate** → captures weak, noisy packets.
- Push **attack injection %** toward the extremes → class imbalance.

## Architecture

- `app.py` — one file. `http.server` on `127.0.0.1:8777` + a background
  generator thread. Endpoints: `/api/state`, `/api/packets`, `/api/detstats`,
  `/api/capture`, `/api/dsp`, `/api/detection`, `/api/control`, `/api/train`.
- Ground-truth feature distributions live in `PROFILES`; the rule detector is
  `classify()`; the ML model is `train_logreg()` / `predict_proba()`.

## Swapping in real data / capture later

Replace `CaptureState.generate_one()` with a reader over `tshark`/`scapy` or
your thesis CSV/IQ recordings, emitting the same feature dict
(`FEATURE_ORDER`). The three tuning layers and the whole UI stay unchanged.
Only capture traffic/interfaces you are authorized to.
