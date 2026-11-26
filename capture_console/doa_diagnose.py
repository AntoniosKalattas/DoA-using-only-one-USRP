#!/usr/bin/env python3
"""
doa_diagnose.py - WHY doesn't the USRP find the phone's angle?  Run it on a recording.

Uses what the radar already saved (no raw IQ needed):
  recordings/<run>/doa.jsonl    every packet: raw inter-antenna phase 'ph', power, bearing
  recordings/<run>/compare.csv  per phone packet: GPS range/azimuth from the USRP (doa_compare.py writes it)
  phone_logs/phone_*.csv        phone speed (to split standing-still vs walking)

It answers four questions, in order:
  1. Is the RECEIVER OK?   Stationary BLE devices (tags, laptops) should have a rock-steady phase.
                           If they don't, the B210 / cables / LO are the problem.
  2. Is the PHONE SIGNAL OK?  Phase stability of the phone standing still vs walking.
                           Walking through multipath scrambles the phase packet-to-packet.
  3. Does the radar's bearing TRACK the truth?  Correlation of live bearing with the GPS bearing,
                           and whether it beats a dumb "always say the same angle" guess.
  4. What does the geometry allow?  Stand-still stops -> phase vs GPS azimuth -> which array
                           facing / phase slope fit, and whether the walk even covered enough angle.

    python3 doa_diagnose.py                     # every recording that has a compare.csv
    python3 doa_diagnose.py recordings/20261119-171110
"""
import csv
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(HERE, "recordings")
PHONE_DIR = os.path.join(HERE, "phone_logs")
STILL_MPS = 0.25          # phone speed below this = standing still
MIN_STOP_S = 6.0          # a "stop" = standing still at least this long


def wrap(a):
    return (np.asarray(a, dtype=float) + 180.0) % 360.0 - 180.0


def coh(deg):
    """Circular mean of phases (deg): complex number, |z| = 1 steady, 0 random."""
    deg = np.asarray(deg, dtype=float)
    return np.exp(1j * np.radians(deg)).mean() if len(deg) else 0j


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def load_speed():
    rows = []
    for f in glob.glob(os.path.join(PHONE_DIR, "phone_*.csv")):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                t, s = num(r.get("t_ms")) / 1000, num(r.get("speed_mps"))
                if np.isfinite(t):
                    rows.append((t, s))
    return np.array(sorted(rows)) if rows else np.zeros((0, 2))


def speed_at(S, t):
    if len(S) == 0:
        return np.full(len(t), np.nan)
    i = np.clip(np.searchsorted(S[:, 0], t), 1, len(S) - 1)
    out = S[i, 1].copy()
    out[np.abs(S[i, 0] - t) > 1.5] = np.nan
    return out


def section(title):
    print(f"\n  {title}\n  " + "-" * len(title))


def diagnose(d, S):
    name = os.path.basename(d.rstrip("/"))
    meta = json.load(open(os.path.join(d, "meta.json")))
    pk = []
    with open(os.path.join(d, "doa.jsonl")) as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("k") == "pkt":
                pk.append(o)
    if not pk:
        print(f"\n### {name}: no packets"); return None
    t0 = pk[0]["t"]
    K_theory = 360.0 * meta["spacing_m"] / meta["wavelength_m"]
    print(f"\n### {name}  ({pk[-1]['t'] - t0:.0f} s, {len(pk)} packets, spacing {meta['spacing_m']*100:.1f} cm "
          f"-> phase slope K = {K_theory:.0f} deg per unit sin(angle))")
    verdict = []

    # 1. receiver health from stationary non-phone devices
    section("1. Receiver: phase of other (stationary) devices")
    by = {}
    for o in pk:
        by.setdefault(o["id"], []).append(o)
    refs = []
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if k.startswith("PHONE") or len(v) < 40:
            continue
        z = coh([o["ph"] for o in v])
        span = v[-1]["t"] - v[0]["t"]
        print(f"    {k:20s} {v[0].get('desc', '')[:16]:16s} n={len(v):4d} over {span:4.0f}s  phase {np.degrees(np.angle(z)):+5.0f}  "
              f"steadiness R={abs(z):.3f}")
        if span > 30:
            refs.append(abs(z))
    if refs and max(refs) > 0.98:
        print("    -> a stationary device holds its phase to a degree or two: the B210 + calibration path is STABLE.")
    elif refs:
        print("    -> no device is steady. Suspect the receiver (cables, loose SMA, LO, USB drops) before anything else.")
        verdict.append("receiver phase unstable")
    else:
        print("    (no long-lived stationary device in this run to check against)")

    # 2. phone stability: still vs moving
    ph = [o for o in pk if o["id"].startswith("PHONE")]
    if len(ph) < 30:
        print("    too few phone packets"); return None
    t = np.array([o["t"] for o in ph]); P = np.array([o["ph"] for o in ph])
    sp = speed_at(S, t)
    loc = np.full(len(t), np.nan)
    for k in range(len(t)):
        w = slice(max(0, k - 3), k + 4)
        if t[w][-1] - t[w][0] < 4 and len(P[w]) >= 5:
            loc[k] = abs(coh(P[w]))
    section("2. Phone: does its phase hold steady? (7 packets in a row)")
    for lab, m in [("standing still", sp < STILL_MPS), ("walking", sp >= STILL_MPS)]:
        m = m & np.isfinite(loc)
        if m.sum() >= 10:
            print(f"    {lab:15s} n={m.sum():4d}  median R={np.median(loc[m]):.2f}  steady (R>0.9) {np.mean(loc[m] > 0.9) * 100:3.0f}%")
    ms, mm = (sp < STILL_MPS) & np.isfinite(loc), (sp >= STILL_MPS) & np.isfinite(loc)
    if ms.sum() >= 10 and mm.sum() >= 10 and np.mean(loc[ms] > 0.9) - np.mean(loc[mm] > 0.9) > 0.2:
        print("    -> walking scrambles the phase (multipath / your body). Only stand-still packets carry a usable angle.")
        verdict.append("phase only usable while standing still")

    # 3. does the live bearing track GPS truth?
    cmp_path = os.path.join(d, "compare.csv")
    stops = []
    if os.path.exists(cmp_path):
        rows = list(csv.DictReader(open(cmp_path)))
        g = lambda k: np.array([num(r.get(k)) for r in rows])
        ct, rng, az, sig, cph, lb, tb = g("t"), g("range"), g("az"), g("truth_sigma"), g("ph"), g("live_b"), g("truth_b")
        good = (sig <= 12) & (rng >= 8)
        section("3. Live bearing vs GPS truth (packets where GPS direction is good to <=12 deg)")
        if good.sum() >= 30 and np.isfinite(tb).any():
            m = good & np.isfinite(lb) & np.isfinite(tb)
            span = np.percentile(tb[m], 95) - np.percentile(tb[m], 5)
            c = np.corrcoef(lb[m], tb[m])[0, 1]
            e_live = np.median(np.abs(wrap(lb[m] - tb[m])))
            e_const = np.median(np.abs(wrap(np.median(tb[m]) - tb[m])))
            print(f"    n={m.sum()}  true bearing moved over {span:.0f} deg   live error {e_live:.1f} deg   "
                  f"'always guess the same angle' error {e_const:.1f} deg   correlation {c:+.2f}")
            if abs(c) < 0.3:
                print("    -> the live bearing does NOT follow the phone. Its error is no better than a constant guess.")
                verdict.append("live bearing uncorrelated with truth")
            if span < 40:
                print(f"    -> the walk only covered {span:.0f} deg of direction: too little to calibrate or even to test angle tracking.")
                verdict.append(f"walk covered only {span:.0f} deg")
        else:
            print(f"    only {good.sum()} packets far enough away for GPS to give a trustworthy direction (need >= 8 m and <= 12 deg)")
            verdict.append("too close / too few packets for GPS truth")

        # 4. stops: phase vs azimuth
        csp = speed_at(S, ct)
        still = csp < STILL_MPS
        k, n = 0, len(ct)
        while k < n:
            if not still[k]:
                k += 1; continue
            e = k
            while e + 1 < n and still[e + 1] and ct[e + 1] - ct[e] < 3:
                e += 1
            if ct[e] - ct[k] >= MIN_STOP_S and e - k >= 8:
                z = coh(cph[k:e + 1])
                stops.append(dict(rec=name, t=ct[k] - ct[0], dur=ct[e] - ct[k], n=e - k + 1, ph=np.degrees(np.angle(z)),
                                  R=abs(z), az=float(np.degrees(np.angle(coh(az[k:e + 1]))) % 360),
                                  rng=float(np.median(rng[k:e + 1])), sig=float(np.median(sig[k:e + 1]))))
            k = e + 1
        section("4. Stand-still stops (phase averaged per stop)")
        if stops:
            for s in stops:
                flag = "" if s["rng"] >= 15 and s["R"] >= 0.85 else "   (not used: too close or unsteady)"
                print(f"    t={s['t']:5.0f}s {s['dur']:3.0f}s  range {s['rng']:5.1f} m  GPS azimuth {s['az']:5.0f}  "
                      f"phase {s['ph']:+5.0f}  R={s['R']:.2f}{flag}")
        else:
            print("    none: stand still for 10 s at each spot so there is something to measure")
    else:
        print("    (no compare.csv: run  python3 doa_compare.py " + d + "  first)")
    return dict(verdict=verdict, stops=stops, K=K_theory, facing=meta.get("facing_deg"))


def fit_stops(stops, K_theory, facing_rec):
    """Joint fit over stops from several runs: phase = offset_run + K*sin(azimuth - facing)."""
    S = [s for s in stops if s["rng"] >= 15 and s["R"] >= 0.85]
    runs = sorted({s["rec"] for s in S})
    print("\n" + "=" * 78)
    print(f"ARRAY FACING / PHASE SLOPE FIT on {len(S)} steady stops from {len(runs)} runs (each run gets its own offset)")
    print("=" * 78)
    if len(S) < 4 or np.ptp([s["az"] for s in S]) < 20:
        print("Not enough stops / angle spread. Do the stop-and-go walk below."); return
    az = np.radians([s["az"] for s in S]); ph = np.radians([s["ph"] for s in S])
    w = np.array([s["n"] * s["R"] for s in S]); ri = np.array([runs.index(s["rec"]) for s in S])

    def score(K, F):
        r = ph - np.radians(K) * np.sin(az - np.radians(F))
        return sum(abs((w[ri == j] * np.exp(1j * r[ri == j])).sum()) for j in range(len(runs))) / w.sum()

    grid = [(score(K, F), K, F) for K in range(0, 361, 4) for F in range(0, 360, 2)]
    base = score(0, 0)
    best = max(grid)
    at_theory = max((g for g in grid if abs(g[1] - K_theory) <= 2), default=None)
    print(f"  no angle information at all (K=0):            fit {base:.3f}")
    print(f"  best of everything:   K={best[1]:3d} facing {best[2]:3d}   fit {best[0]:.3f}")
    if at_theory:
        print(f"  with your spacing:    K={at_theory[1]:3d} facing {at_theory[2]:3d}   fit {at_theory[0]:.3f}")
    if facing_rec is not None:
        fr = int(round(facing_rec / 2) * 2) % 360
        rec_best = max((g for g in grid if g[2] == fr), default=None)
        if rec_best:
            print(f"  with the compass facing {facing_rec:.0f}: best K={rec_best[1]:3d}   fit {rec_best[0]:.3f}"
                  + ("   <- needs K~0: the recorded facing is inconsistent with the data" if rec_best[1] <= 20 else ""))
    print("  (fit 1.000 = every stop explained exactly; differences under ~0.01 are not meaningful with this few stops)")


PROTOCOL = """
WHAT TO DO NEXT (the recordings can't give an angle as collected)
  1. Fix the facing with tape, not the compass: put the phone/beacon at a spot you MEASURE to be straight out
     in front of the midpoint of the two antennas (perpendicular to the line joining them), 5-10 m away.
     That spot is 0 deg by definition. Calibrate there (MEASURE FRONT) so offset + facing are set together.
  2. Stop-and-go, in FRONT of the array: stand still 10-15 s at about -60, -30, 0, +30, +60 deg, 10-20 m out
     (tape/pace it, or GPS if >= 15 m). Walking packets are useless; stops are what the fit needs.
  3. Keep a stationary beacon (the FindMy tag already in every run works) next to the array as a live check:
     if its phase jumps, the receiver changed and the calibration must be redone (each B210 restart can move it).
  4. Avoid the array's ends (beyond ~60 deg): a 2-antenna array can barely tell angles apart there,
     and it can never tell front from back.
  Then:  python3 doa_compare.py <run> && python3 doa_diagnose.py <run>
"""


def main():
    S = load_speed()
    dirs = sys.argv[1:] or sorted(d for d in glob.glob(os.path.join(REC_DIR, "*")) if os.path.isdir(d))
    all_stops, K, facing = [], None, None
    for d in dirs:
        if not os.path.exists(os.path.join(d, "doa.jsonl")):
            continue
        r = diagnose(d, S)
        if r:
            all_stops += r["stops"]; K = r["K"]
            if facing is None and r["facing"] is not None:
                facing = r["facing"]
            if r["verdict"]:
                print("\n  VERDICT: " + "; ".join(r["verdict"]))
    site = os.path.join(HERE, "usrp_site.json")
    if os.path.exists(site):
        facing = json.load(open(site)).get("facing_deg", facing)
    if all_stops and K:
        fit_stops(all_stops, K, facing)
    print(PROTOCOL)


if __name__ == "__main__":
    main()
