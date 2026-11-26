#!/usr/bin/env python3
"""
doa_compare.py - check the USRP's direction-of-arrival against the phone's GPS, and re-tune it.

Inputs (all written automatically):
  recordings/<start>/doa.jsonl   every packet of every live run: raw phase, bearing, power
  recordings/<start>/meta.json   the calibration that run used + where the USRP was
  phone_logs/phone_*.jsonl       everything the USRP Beacon app sent (GPS, compass, ...)
  usrp_site.json                 USRP position (phone lying on it) + which way the antennas face

For every packet from the phone it works out where GPS says the phone was, the true direction
from the USRP to the phone, and compares that with what the USRP measured. Then it fits a new
calibration (phase offset, effective antenna spacing, left/right flip, the antennas' real facing,
1 m power and path-loss exponent) straight from the raw phases, and can save it as a preset.

    python3 doa_compare.py                     # newest recording
    python3 doa_compare.py recordings/20261118-101500 --save-preset field-gps --activate
    python3 doa_compare.py --all               # every recording that has a phone in it
    python3 doa_compare.py selftest

GPS LIMITS (read this): a phone's GPS is off by ~3-5 m outdoors and far worse indoors. The true
direction is only known well when the phone is much further away than that error:
    bearing uncertainty ~ atan(GPS error / distance)  ->  4 m error at 10 m = 22 deg, at 40 m = 6 deg
so packets are only used when that uncertainty is below --max-truth-err (default 12 deg). Do the
walk OUTSIDE, in the open, 15-60 m from the USRP. For short range (a room) use the AR walk
calibration in the app instead: ARCore positions are accurate to centimetres.
"""
import argparse
import glob
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
C = 299792458.0
EARTH_R = 6371008.8
REC_DIR = os.path.join(HERE, "recordings")
PHONE_DIR = os.path.join(HERE, "phone_logs")
SITE_FILE = os.path.join(HERE, "usrp_site.json")
PRESET_DIR = os.path.join(HERE, "cal_presets")


def wrap(a):
    return (np.asarray(a, dtype=float) + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------- loading
def load_recording(d, tag):
    with open(os.path.join(d, "meta.json")) as f:
        meta = json.load(f)
    pid, rows = f"PHONE-{tag}", []
    with open(os.path.join(d, "doa.jsonl")) as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue                                  # a run killed mid-line
            if o.get("k") == "pkt" and o.get("id") == pid:
                rows.append((o["t"], o["p"], o["ph"], o["b"], o.get("d", np.nan)))
    return meta, np.array(rows, dtype=float).reshape(-1, 5)


def load_fixes(t0, t1, phone_dir=PHONE_DIR, pad=120.0):
    """GPS fixes from the phone logs around [t0, t1]: (fix time on the Mac clock, lat, lon, acc_m).
    The app sends the same fix several times a second; each fix is kept once."""
    fixes = {}
    for fn in sorted(glob.glob(os.path.join(phone_dir, "phone_*.jsonl"))):
        with open(fn) as f:
            for line in f:
                try:
                    m = json.loads(line)
                    rt = float(m["recv_time"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not (t0 - pad <= rt <= t1 + pad) or m.get("lat") is None or m.get("lon") is None:
                    continue
                if m.get("fix_ms"):                      # GPS time of the fix (app 1.3+)
                    ft = float(m["fix_ms"]) / 1000.0
                    if abs(ft - rt) > 30:                # phone clock badly off: fall back to age
                        ft = rt - float(m.get("gps_age_s") or 0.0)
                else:
                    ft = rt - float(m.get("gps_age_s") or 0.0)
                key = (round(m["lat"], 8), round(m["lon"], 8))
                if key not in fixes or fixes[key][0] > ft:
                    fixes[key] = (ft, float(m["lat"]), float(m["lon"]), float(m.get("acc_m") or 10.0))
    arr = np.array(sorted(fixes.values()), dtype=float).reshape(-1, 4)
    return arr


def enu(lat, lon, lat0, lon0):
    """metres east, north of (lat0, lon0). Flat-earth: fine within a few km."""
    e = np.radians(np.asarray(lon) - lon0) * EARTH_R * math.cos(math.radians(lat0))
    n = np.radians(np.asarray(lat) - lat0) * EARTH_R
    return e, n


def phone_positions(fixes, t, max_gap=3.0):
    """Phone lat/lon/acc at each packet time (linear between fixes); NaN when no fix is close."""
    out = np.full((len(t), 3), np.nan)
    if len(fixes) < 2:
        return out
    ft = fixes[:, 0]
    i = np.searchsorted(ft, t)
    ok = (i > 0) & (i < len(ft))
    i0, i1 = np.clip(i - 1, 0, len(ft) - 1), np.clip(i, 0, len(ft) - 1)
    ok &= (ft[i1] - ft[i0]) <= max_gap
    w = np.where(ft[i1] > ft[i0], (t - ft[i0]) / np.maximum(ft[i1] - ft[i0], 1e-9), 0.0)
    for c in (1, 2):
        out[:, c - 1] = fixes[i0, c] * (1 - w) + fixes[i1, c] * w
    out[:, 2] = np.maximum(fixes[i0, 3], fixes[i1, 3])
    out[~ok] = np.nan
    return out


def truth_geometry(pk, fixes, site):
    """Per packet: range (m), azimuth from the USRP (deg, true north, clockwise), and how uncertain
    that azimuth is because of GPS error (deg)."""
    pos = phone_positions(fixes, pk[:, 0])
    e, n = enu(pos[:, 0], pos[:, 1], site["lat"], site["lon"])
    rng = np.hypot(e, n)
    az = np.degrees(np.arctan2(e, n)) % 360.0
    err_m = np.hypot(pos[:, 2], float(site.get("acc_m") or 3.0))
    sig = np.degrees(np.arctan2(err_m, np.maximum(rng, 1e-3)))
    return rng, az, sig


def array_angle(az, facing):
    """Bearing a 2-antenna array can see: + = right of where the antennas face. A source behind
    the array looks exactly like its mirror image in front (sin is the same), so fold it there."""
    rel = wrap(az - facing)
    return np.degrees(np.arcsin(np.sin(np.radians(rel)))), np.abs(rel) > 90.0


# --------------------------------------------------------------------------- fit
def _coh(ph, az, w, K, F, s):
    """weighted |mean exp(j(phase - s K sin(az - F)))| over packets, for arrays of K and F."""
    model = s * np.radians(K)[:, None, None] * np.sin(np.radians(az[None, None, :] - F[None, :, None]))
    z = np.exp(1j * (np.radians(ph)[None, None, :] - model))
    return np.abs((z * w).sum(axis=2) / w.sum())


def fit_gps(ph, az, sig, rng_m, p_db, freq, spacing_ruler, facing=None, facing_window=45.0, spacing_fixed=None):
    """raw phase = offset + s*K*sin(az - F). Unknowns: offset, K (-> spacing), s (-> flip), F (the
    antennas' real facing). F is searched +-facing_window around the compass value, or all round
    when there is none. Packets are weighted by how well GPS knows their direction."""
    lam = C / freq
    K0 = 360.0 * spacing_ruler / lam
    w = 1.0 / (sig ** 2 + 4.0 ** 2)
    Ks = (np.array([360.0 * spacing_fixed / lam]) if spacing_fixed else np.linspace(0.5 * K0, 1.5 * K0, 41))
    Fs = (np.arange(facing - facing_window, facing + facing_window + 0.1, 1.0) if facing is not None
          else np.arange(0.0, 360.0, 2.0))
    best = None
    for s in (1.0, -1.0):
        R = _coh(ph, az, w, Ks, Fs, s)
        iK, iF = np.unravel_index(np.argmax(R), R.shape)
        if best is None or R[iK, iF] > best[0]:
            best = (float(R[iK, iF]), float(Ks[iK]), float(Fs[iF]), s)
    _, K, F, s = best
    Ks2 = np.array([K]) if len(Ks) == 1 else np.linspace(K - (Ks[1] - Ks[0]), K + (Ks[1] - Ks[0]), 21)
    Fs2 = np.linspace(F - 1.0, F + 1.0, 21)
    R2 = _coh(ph, az, w, Ks2, Fs2, s)
    iK, iF = np.unravel_index(np.argmax(R2), R2.shape)
    coh, K, F = float(R2[iK, iF]), float(Ks2[iK]), float(Fs2[iF])
    if facing is None:
        # F and F+180 with the opposite sign fit identically; "front" = where most packets came from
        if np.mean(np.cos(np.radians(az - F)) * w) < 0:
            F, s = (F + 180.0) % 360.0, -s
    model = s * K * np.sin(np.radians(az - F))
    offset = float(np.degrees(np.angle(np.sum(w * np.exp(1j * np.radians(ph - model))))))
    out = dict(phase_deg=offset, K_deg=K, spacing_m=K * lam / 360.0, flip=bool(s < 0), facing_deg=F % 360.0,
               coherence=coh, phase_spread_deg=float(np.degrees(np.sqrt(-2 * np.log(max(coh, 1e-9))))))
    # 1 m power + path-loss exponent from the GPS range
    lr = np.log10(np.maximum(rng_m, 0.5))
    if rng_m.max() / max(rng_m.min(), 0.5) >= 1.8:
        A = np.stack([np.ones_like(lr), -10.0 * lr], axis=1)
        sol = np.linalg.lstsq(A, p_db, rcond=None)[0]
        keep = np.abs(p_db - A @ sol) < 2.5 * np.std(p_db - A @ sol) + 1e-9
        sol = np.linalg.lstsq(A[keep], p_db[keep], rcond=None)[0]
        if 1.0 <= sol[1] <= 5.0:
            out.update(ref_power_db=float(sol[0]), path_loss_exp=float(sol[1]))
    if "ref_power_db" not in out:
        out["ref_power_db"] = float(np.median(p_db + 22.0 * lr))
    return out


def predict(ph, cal, K):
    """The bearing the live tracker would show for these raw phases with calibration `cal`."""
    x = wrap(ph - cal["phase_deg"]) / K * (-1.0 if cal.get("flip") else 1.0)
    return np.degrees(np.arcsin(np.clip(x, -1.0, 1.0)))


def err_stats(err):
    err = np.asarray(err, dtype=float)
    err = err[np.isfinite(err)]
    if len(err) == 0:
        return {"n": 0}
    return {"n": int(len(err)), "median_abs_deg": round(float(np.median(np.abs(err))), 1),
            "rms_deg": round(float(np.sqrt(np.mean(err ** 2))), 1), "bias_deg": round(float(np.median(err)), 1),
            "within_10_deg_pct": round(100.0 * float(np.mean(np.abs(err) <= 10)), 0)}


# --------------------------------------------------------------------------- main analysis
def analyse(dirs, tag, site_override, max_truth_err, min_range, phone_dir=PHONE_DIR):
    metas, parts = [], []
    for d in dirs:
        meta, pk = load_recording(d, tag)
        if len(pk):
            metas.append((d, meta))
            parts.append(pk)
    if not parts:
        raise SystemExit(f"No packets from PHONE-{tag} in {', '.join(dirs)}. Was the beacon running during the live run?")
    pk = np.concatenate(parts)
    pk = pk[np.argsort(pk[:, 0])]
    d0, meta = metas[-1]

    site = dict(meta.get("site") or {})
    if site_override:
        site.update(site_override)
    if site.get("lat") is None:
        try:
            with open(SITE_FILE) as f:
                site = dict(json.load(f), **(site_override or {}))
        except (OSError, ValueError):
            pass
    if site.get("lat") is None:
        raise SystemExit("The USRP position is unknown. In the app: USRP POSITION -> put the phone on the USRP -> "
                         "RECORD POSITION (or pass --site-lat/--site-lon).")

    fixes = load_fixes(pk[0, 0], pk[-1, 0], phone_dir)
    if len(fixes) < 2:
        raise SystemExit("No phone GPS fixes during this recording. The app needs 'Send info to laptop' on and a GPS "
                         "fix (outdoors).")
    rng_m, az, sig = truth_geometry(pk, fixes, site)
    have = np.isfinite(rng_m)
    use = have & (rng_m >= min_range) & (sig <= max_truth_err)

    freq = float(meta.get("freq") or 2.48e9)
    lam = C / freq
    cal_used = {"phase_deg": float(meta.get("cal_phase_deg") or 0.0), "flip": bool(meta.get("flip"))}
    spacing_used = float(meta.get("spacing_m") or lam / 2)
    preset = meta.get("preset") or {}
    ruler = float(preset.get("ruler_spacing_m") or spacing_used)
    facing = site.get("facing_deg")

    rep = {"recordings": [os.path.relpath(d, HERE) for d, _ in metas], "tag": tag,
           "preset_used": preset.get("name", "none"), "site": {k: site.get(k) for k in ("lat", "lon", "acc_m", "facing_deg")},
           "packets": int(len(pk)), "with_gps_position": int(have.sum()), "usable": int(use.sum()),
           "range_m": [round(float(np.nanmin(rng_m[have])), 1), round(float(np.nanmax(rng_m[have])), 1)] if have.any() else None,
           "gps_fixes": int(len(fixes)), "median_truth_uncertainty_deg": round(float(np.nanmedian(sig[have])), 1) if have.any() else None}
    rows = {"t": pk[:, 0], "p": pk[:, 1], "ph": pk[:, 2], "live_b": pk[:, 3], "live_d": pk[:, 4],
            "range": rng_m, "az": az, "truth_sigma": sig, "used": use}

    if facing is not None and use.any():
        tb, behind = array_angle(az, facing)
        rows["truth_b"] = tb
        e = wrap(pk[:, 3] - tb)
        rep["before"] = dict(err_stats(e[use]), calibration=rep["preset_used"],
                             note="live bearings vs GPS, with the calibration the run used and the compass facing")
        rep["behind_array_pct"] = round(100.0 * float(np.mean(behind[use])), 0)
        ok_d = use & np.isfinite(pk[:, 4])
        if ok_d.sum() >= 5:
            rep["distance_ratio_median"] = round(float(np.median(pk[ok_d, 4] / rng_m[ok_d])), 2)
        rows["err_before"] = e

    if use.sum() < 30:
        rep["fit"] = {"ok": False, "error": (
            f"Only {int(use.sum())} packets had a direction GPS can vouch for (uncertainty <= {max_truth_err:.0f} deg, "
            f"range >= {min_range:.0f} m). Walk further away from the USRP, outdoors, and longer.")}
        return rep, rows, None

    # Two models. "offset": spacing as measured (ruler / preset) and facing from the compass (+-8 deg);
    # only the phase offset and flip are fitted, which even a short walk pins down. "full": spacing and
    # facing are fitted too, which needs a wide walk. The one that predicts the unseen half of the walk
    # better wins, and it is only "usable" if it beats the calibration the run already had.
    blk = ((pk[:, 0] - pk[0, 0]) // 20).astype(int) % 2
    models = {"offset": dict(facing_window=8.0, spacing_fixed=spacing_used)} if facing is not None else {}
    models["full"] = dict(facing_window=45.0)

    def run(kw):
        f = fit_gps(pk[use, 2], az[use], sig[use], rng_m[use], pk[use, 1], freq, ruler, facing, **kw)
        cv = []
        for k in (0, 1):
            tr, te = use & (blk == k), use & (blk != k)
            if tr.sum() >= 20 and te.sum() >= 10:
                f2 = fit_gps(pk[tr, 2], az[tr], sig[tr], rng_m[tr], pk[tr, 1], freq, ruler, facing, **kw)
                t2, _ = array_angle(az[te], f2["facing_deg"])
                cv.append(wrap(predict(pk[te, 2], f2, f2["K_deg"]) - t2))
        cvs = err_stats(np.concatenate(cv)) if cv else {"n": 0, "note": "not enough data to split"}
        return f, cvs

    tried = {name: run(kw) for name, kw in models.items()}
    model = min(tried, key=lambda n: tried[n][1].get("median_abs_deg", 1e9))
    fit, cvs = tried[model]
    K = fit["K_deg"]
    tb_fit, _ = array_angle(az, fit["facing_deg"])
    span = float(np.percentile(tb_fit[use], 97) - np.percentile(tb_fit[use], 3))   # in array angles: no 0/360 wrap
    pred = predict(pk[:, 2], fit, K)
    e_after = wrap(pred - tb_fit)
    rows.update(fit_b=pred, err_after=e_after)

    need_span = 20.0 if model == "offset" else 40.0
    before_med = (rep.get("before") or {}).get("median_abs_deg")
    cv_med = cvs.get("median_abs_deg")
    better = before_med is None or (cv_med is not None and cv_med < before_med)
    ok = fit["coherence"] >= 0.45 and span >= need_span and better
    fit.update(ok=ok, method="gps", model=model, span_deg=round(span, 0), spacing_ratio=round(fit["spacing_m"] / ruler, 2),
               after=err_stats(e_after[use]), after_crossval=cvs,
               models_tried={n: tried[n][1].get("median_abs_deg") for n in tried})
    if facing is not None:
        fit["compass_facing_error_deg"] = round(float(wrap(fit["facing_deg"] - facing)), 1)
    if not ok:
        fit["error"] = ("The walk covered too few directions (need %.0f deg or more, got %.0f)." % (need_span, span)
                        if span < need_span else
                        "The phases don't follow the GPS directions (reflections, or the USRP position/facing is wrong)."
                        if fit["coherence"] < 0.45 else
                        "The fit is not better than the calibration the run already used (%s deg vs %s deg on "
                        "the unseen half). Keep the current one." % (cv_med, before_med))
    rep["fit"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fit.items()}
    return rep, rows, (fit, meta, ruler)


def save_preset(name, fit, meta, ruler, tag, activate):
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise SystemExit("bad preset name")
    a = meta.get("args") or {}
    used = meta.get("preset") or {}
    q = fit["phase_spread_deg"]
    rec = {"name": safe, "method": "gps", "phase_deg": fit["phase_deg"], "spacing_m": fit["spacing_m"],
           "ruler_spacing_m": ruler, "flip": fit["flip"], "ref_power_db": fit["ref_power_db"],
           "phase_spread_deg": round(q, 1), "quality": "good" if q <= 12 else ("ok" if q <= 25 else "poor"),
           "freq": meta.get("freq"), "gain": meta.get("gain"), "antenna": a.get("antenna") or used.get("antenna", "TX/RX"),
           "rfa_side": used.get("rfa_side", "left"), "device": f"PHONE-{tag}", "device_desc": f"USRP Beacon #{tag}",
           "facing_deg": round(fit["facing_deg"], 1), "span_deg": fit["span_deg"],
           "bearing_err_median_deg": fit["after"].get("median_abs_deg"), "bearing_err_rms_deg": fit["after"].get("rms_deg"),
           "n_bursts": fit["after"].get("n"), "n_total": fit["after"].get("n"),
           "direction_check": ("mirrored: fixed by the saved flip" if fit["flip"] else "ok") + " (from GPS)",
           "notes": "fitted by doa_compare.py from the phone's GPS", "created": __import__("time").strftime("%Y-%m-%d %H:%M"),
           "t": __import__("time").time()}
    if fit.get("path_loss_exp"):
        rec["path_loss_exp"] = round(fit["path_loss_exp"], 2)
    os.makedirs(PRESET_DIR, exist_ok=True)
    with open(os.path.join(PRESET_DIR, safe + ".json"), "w") as f:
        json.dump(rec, f, indent=2)
    if activate:
        with open(os.path.join(PRESET_DIR, "_active.json"), "w") as f:
            json.dump({"name": safe}, f)
    return rec


def write_csv(path, rows):
    keys = [k for k in ("t", "range", "az", "truth_sigma", "used", "truth_b", "live_b", "err_before", "fit_b",
                        "err_after", "p", "ph", "live_d") if k in rows]
    with open(path, "w") as f:
        f.write(",".join(keys) + "\n")
        for i in range(len(rows["t"])):
            f.write(",".join("" if (isinstance(rows[k][i], float) and not np.isfinite(rows[k][i]))
                             else (f"{rows[k][i]:.4f}" if isinstance(rows[k][i], (float, np.floating)) else str(rows[k][i]))
                             for k in keys) + "\n")


def print_report(rep):
    print("=" * 66)
    print("  USRP DoA vs phone GPS")
    print("=" * 66)
    print(f"  recordings   : {', '.join(rep['recordings'])}")
    print(f"  calibration  : {rep['preset_used']}")
    s = rep["site"]
    print(f"  USRP at      : {s['lat']:.7f}, {s['lon']:.7f}  (± {s.get('acc_m') or '?'} m)"
          f"   facing {s['facing_deg']:.1f}°" if s.get("facing_deg") is not None else
          f"  USRP at      : {s['lat']:.7f}, {s['lon']:.7f}   facing UNKNOWN (fitted from the data)")
    print(f"  phone packets: {rep['packets']}  with a GPS position: {rep['with_gps_position']}  "
          f"usable: {rep['usable']}  (range {rep['range_m']} m, GPS direction uncertainty "
          f"median {rep['median_truth_uncertainty_deg']}°)")
    if "before" in rep:
        b = rep["before"]
        print(f"\n  AS RECORDED  : median error {b.get('median_abs_deg')}°  rms {b.get('rms_deg')}°  "
              f"bias {b.get('bias_deg')}°  within 10°: {b.get('within_10_deg_pct')}%  (n={b['n']})")
        if rep.get("behind_array_pct"):
            print(f"                 {rep['behind_array_pct']:.0f}% of packets came from BEHIND the antennas "
                  "(a 2-antenna array sees them mirrored to the front)")
        if rep.get("distance_ratio_median"):
            print(f"  distance     : USRP / GPS = {rep['distance_ratio_median']} (1.0 = right)")
    f = rep["fit"]
    print()
    if not f.get("phase_deg"):
        print(f"  FIT          : not possible. {f.get('error')}")
        return
    print(f"  FITTED       : [{f.get('model', 'full')} model; tried {f.get('models_tried')}]")
    print(f"                 offset {f['phase_deg']:+.1f}°  spacing {f['spacing_m'] * 100:.2f} cm "
          f"(x{f['spacing_ratio']} of ruler)  flip {f['flip']}  facing {f['facing_deg']:.1f}°"
          + (f" (compass off by {f['compass_facing_error_deg']:+.1f}°)" if "compass_facing_error_deg" in f else ""))
    print(f"                 1 m power {f['ref_power_db']:.1f} dB" +
          (f"  path-loss exponent {f['path_loss_exp']:.2f}" if f.get("path_loss_exp") else "") +
          f"  phase spread {f['phase_spread_deg']:.1f}°  span {f['span_deg']:.0f}°")
    a, cv = f["after"], f["after_crossval"]
    print(f"  WITH FIT     : median error {a.get('median_abs_deg')}°  rms {a.get('rms_deg')}°  within 10°: "
          f"{a.get('within_10_deg_pct')}%")
    if cv.get("n"):
        print(f"  cross-checked: median error {cv.get('median_abs_deg')}°  rms {cv.get('rms_deg')}°  "
              "(fitted on half the walk, tested on the other half: the realistic number)")
    print("  verdict      : " + ("usable" if f["ok"] else "NOT usable: " + f.get("error", "")))


# --------------------------------------------------------------------------- self test
def _selftest():
    """Simulated outdoor walk: USRP facing 70 deg, phone walks a 20-50 m arc, GPS noise 3 m
    (slowly drifting, like real GPS), phase noise 15 deg, a few wild packets."""
    import tempfile
    import time as _t
    r = np.random.default_rng(7)
    freq, d = 2.48e9, 0.055
    lam = C / freq
    K = 360 * d / lam
    lat0, lon0, facing, offset = 35.0197, 33.3886, 70.0, -35.0
    for flip in (False, True):
        tmp = tempfile.mkdtemp()
        rec, ph_dir = os.path.join(tmp, "rec"), os.path.join(tmp, "phone")
        os.makedirs(rec)
        os.makedirs(ph_dir)
        t0 = _t.time() - 600
        T = np.arange(0, 240, 0.5)                      # GPS 2 Hz for 4 min
        brg = facing + 75 * np.sin(2 * np.pi * T / 160)
        dist = 35 + 15 * np.sin(2 * np.pi * T / 97)
        e_true, n_true = dist * np.sin(np.radians(brg)), dist * np.cos(np.radians(brg))
        drift = np.cumsum(r.normal(0, 0.25, (len(T), 2)), axis=0)
        drift -= drift.mean(axis=0)
        drift = drift / max(np.std(drift), 1e-9) * 2.5
        e_g, n_g = e_true + drift[:, 0], n_true + drift[:, 1]
        lat = lat0 + np.degrees(n_g / EARTH_R)
        lon = lon0 + np.degrees(e_g / (EARTH_R * math.cos(math.radians(lat0))))
        with open(os.path.join(ph_dir, "phone_sim.jsonl"), "w") as f:
            for i, tt in enumerate(T):
                f.write(json.dumps({"type": "usrp_beacon", "recv_time": t0 + tt + 0.3, "fix_ms": (t0 + tt) * 1000,
                                    "lat": lat[i], "lon": lon[i], "acc_m": 3.5}) + "\n")
        tp = np.sort(r.uniform(1, 239, 1500))
        b_true = np.interp(tp, T, brg)
        dd = np.interp(tp, T, dist)
        rel = np.radians(b_true - facing)
        s = -1.0 if flip else 1.0
        ph = wrap(offset + s * K * np.sin(rel) + r.normal(0, 15, len(tp)))
        ph[r.integers(0, len(tp), 40)] = r.uniform(-180, 180, 40)
        p = -20 - 10 * 2.4 * np.log10(dd) + r.normal(0, 2, len(tp))
        with open(os.path.join(rec, "doa.jsonl"), "w") as f:
            for i in range(len(tp)):
                f.write(json.dumps({"k": "pkt", "t": t0 + tp[i], "id": "PHONE-1", "p": p[i], "ph": ph[i],
                                    "b": float(np.degrees(np.arcsin(np.clip(ph[i] / K, -1, 1)))), "d": 5.0}) + "\n")
        site = {"lat": lat0, "lon": lon0, "acc_m": 2.0, "facing_deg": facing + 12.0}   # compass 12 deg off
        with open(os.path.join(rec, "meta.json"), "w") as f:
            json.dump({"freq": freq, "spacing_m": 0.06, "cal_phase_deg": 0.0, "flip": False, "site": site}, f)
        rep, rows, res = analyse([rec], 1, None, 12.0, 8.0, phone_dir=ph_dir)
        print_report(rep)
        fit = rep["fit"]
        assert fit["ok"], fit
        assert abs(wrap(fit["phase_deg"] - offset)) < 10, fit["phase_deg"]
        assert fit["flip"] == flip
        assert abs(fit["spacing_m"] - d) < 0.008, fit["spacing_m"]
        assert abs(wrap(fit["facing_deg"] - facing)) < 6, fit["facing_deg"]
        assert abs(fit["path_loss_exp"] - 2.4) < 0.5, fit.get("path_loss_exp")
        assert fit["after_crossval"]["median_abs_deg"] < 10
        print()
    print("selftest ok")


def main():
    if sys.argv[1:] == ["selftest"]:
        return _selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", nargs="*", help="recordings/<start> folder(s), or 'all'; default: the newest")
    ap.add_argument("--all", action="store_true", help="use every recording (combine them in one fit)")
    ap.add_argument("--tag", type=int, default=1, help="the app's tag number (default 1)")
    ap.add_argument("--max-truth-err", type=float, default=12.0,
                    help="only use packets whose GPS direction is known to within this many degrees (default 12)")
    ap.add_argument("--min-range", type=float, default=8.0, help="ignore packets closer than this (m, default 8)")
    ap.add_argument("--site-lat", type=float, help="override the USRP latitude")
    ap.add_argument("--site-lon", type=float, help="override the USRP longitude")
    ap.add_argument("--facing", type=float, help="override where the antennas face (deg true north)")
    ap.add_argument("--no-facing", action="store_true", help="ignore the compass facing, fit it from the data")
    ap.add_argument("--save-preset", default="", help="save the fitted calibration as this preset")
    ap.add_argument("--activate", nargs="?", const="yes", default="no",
                    help="with --save-preset: make it the active preset ('--activate' or '--activate yes')")
    ap.add_argument("--force", nargs="?", const="yes", default="no",
                    help="save the preset even when the fit is marked not usable ('--force' or '--force yes')")
    ap.add_argument("--csv", default="", help="write every packet with its truth and errors here "
                                              "(default: <recording>/compare.csv)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args()
    yes = ("yes", "y", "true", "1", "on")
    a.activate = str(a.activate).strip().lower() in yes
    a.force = str(a.force).strip().lower() in yes
    if [r.lower() for r in a.recording] == ["all"]:
        a.all, a.recording = True, []
    elif [r.lower() for r in a.recording] in (["latest"], ["newest"]):
        a.recording = []
    a.recording = [r if os.path.isabs(r) or os.path.exists(r) else os.path.join(REC_DIR, os.path.basename(r))
                   for r in a.recording]

    if a.all:
        dirs = sorted(d for d in glob.glob(os.path.join(REC_DIR, "*")) if os.path.exists(os.path.join(d, "doa.jsonl")))
    elif a.recording:
        dirs = [os.path.abspath(d) for d in a.recording]
    else:
        dirs = sorted(d for d in glob.glob(os.path.join(REC_DIR, "*")) if os.path.exists(os.path.join(d, "doa.jsonl")))[-1:]
    if not dirs:
        raise SystemExit("No recordings yet. Start live tracking (it records automatically), walk with the phone, "
                         "then run this again.")
    over = {}
    if a.site_lat is not None and a.site_lon is not None:
        over.update(lat=a.site_lat, lon=a.site_lon)
    if a.facing is not None:
        over["facing_deg"] = a.facing
    if a.no_facing:
        over["facing_deg"] = None

    rep, rows, res = analyse(dirs, a.tag, over, a.max_truth_err, a.min_range)
    out_csv = a.csv or os.path.join(dirs[-1], "compare.csv")
    write_csv(out_csv, rows)
    with open(os.path.join(dirs[-1], "compare.json"), "w") as f:
        json.dump(rep, f, indent=2)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)
        print(f"\n  per-packet table: {os.path.relpath(out_csv)}")
    if a.save_preset:
        if not res:
            raise SystemExit("Nothing to save: there was no fit.")
        if not res[0]["ok"] and not a.force:
            raise SystemExit("Not saved: the fit is not usable (see above). --force saves it anyway.")
        rec = save_preset(a.save_preset, *res, a.tag, a.activate)
        print(f"  saved preset : cal_presets/{rec['name']}.json" + ("  (now active)" if a.activate else "")
              + "\n  use it      : /radar -> pick the preset -> Start live")


if __name__ == "__main__":
    main()
