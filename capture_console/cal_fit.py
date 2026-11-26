#!/usr/bin/env python3
"""
cal_fit.py - turn a recording of the phone's packets into a calibration, automatically.

live_radar_capture.py --calibrate N --dump-packets FILE writes one line per packet from the
phone: {"t": unix time, "p": power dB, "ph": raw phase between the antennas (deg)}.
Two ways to fit it (both started from the USRP Beacon app):

  fit_sweep : the phone was carried once in an arc around the antennas, from in line with
              them on the LEFT, through the front, to in line with them on the RIGHT.
              raw phase = offset + K*sin(bearing), K = 360*d/lambda. Over a full sweep
              sin() runs -1..+1, so: offset = middle of the phase trace, K (= effective
              antenna spacing) = half its swing, and rising/falling tells left from right.

  fit_ar    : the phone also reported where it was (ARCore position, a few cm accurate),
              with the origin set by holding it at the middle of the antennas.
              raw phase = offset + k . u, where u is the unit vector antennas -> phone and
              k is a horizontal vector (length K, pointing along the antenna pair). k and
              the offset are found by maximising how well all packets agree, so the walk can
              be any shape, and the array's orientation does not have to be measured.
              Knowing the true range of every packet also gives the 1 m reference power and
              the path-loss exponent, and the bearing error of each packet.

Both return a dict with the fields of a preset (phase_deg, spacing_m, flip, ref_power_db, ...)
or {"ok": False, "error": "..."} when the recording cannot be trusted.

    python3 cal_fit.py selftest      # simulated sweep + simulated AR walk
"""
import json

import numpy as np

C = 299792458.0


def wrap(a):
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def load_packets(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                    rows.append((float(o["t"]), float(o["p"]), float(o["ph"])))
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return np.array(rows, dtype=float).reshape(-1, 3)


def _fail(msg, **kw):
    return dict(ok=False, error=msg, **kw)


def _quality(spread):
    return "good" if spread <= 12 else ("ok" if spread <= 25 else "poor")


# --------------------------------------------------------------------------- sweep
def _smooth_unwrap(ph_deg, k=7):
    """Circular moving average (kills single wild packets), then unwrap over time."""
    z = np.exp(1j * np.radians(ph_deg))
    k = max(1, min(k, len(z) // 4 * 2 + 1))
    zs = np.convolve(np.concatenate((np.repeat(z[0], k // 2), z, np.repeat(z[-1], k // 2))), np.ones(k), "valid")
    return np.degrees(np.unwrap(np.angle(zs)))


def fit_sweep(pk, freq, spacing_ruler, start_side="left"):
    """pk: (n, 3) [t, p_db, phase_deg] of the sweep only, in time order."""
    n = len(pk)
    if n < 30:
        return _fail(f"Only {n} packets from the phone during the sweep. Sweep more slowly (10-15 s) "
                     "and keep the phone unlocked.", n_total=n)
    lam = C / freq
    u = _smooth_unwrap(pk[:, 2])
    lo, hi = np.percentile(u, [2, 98])
    K = (hi - lo) / 2.0                                   # deg of phase per unit sin(bearing)
    mid = (hi + lo) / 2.0
    d_eff = K * lam / 360.0
    ratio = d_eff / spacing_ruler
    q = max(3, n // 10)
    rising = np.median(u[-q:]) > np.median(u[:q])
    travel = abs(np.median(u[-q:]) - np.median(u[:q])) / max(2 * K, 1e-9)
    spread = float(1.4826 * np.median(np.abs(wrap(pk[:, 2] - u))))   # robust: ignores wild packets
    info = dict(n_total=n, swing_deg=round(2 * K, 1), fitted_spacing_m=round(d_eff, 4),
                spacing_ratio=round(ratio, 2), phase_spread_deg=round(spread, 1))
    if travel < 0.7:
        return _fail("The phase did not go steadily from one side to the other. Start in line with the "
                     "antennas on one side and walk the arc once, without going back.", **info)
    if ratio < 0.65:
        return _fail(f"The sweep only covered part of the arc (phase swing {2 * K:.0f}°, expected about "
                     f"{720 * spacing_ruler / lam:.0f}° for {spacing_ruler * 100:.1f} cm spacing). Start and end "
                     "right in line with the two antennas.", **info)
    if ratio > 1.4:
        return _fail(f"The phase swing ({2 * K:.0f}°) is bigger than {spacing_ruler * 100:.1f} cm spacing allows. "
                     "Check the antenna spacing you entered, or reflections are too strong here.", **info)
    centre = np.abs(u - mid) < 0.5 * K                    # within +-30 deg of straight ahead
    ref_p = float(np.median(pk[centre, 1])) if centre.sum() >= 5 else float(np.median(pk[:, 1]))
    flip = bool(rising != (start_side == "left"))         # left -> right must read as rising phase
    quality = _quality(spread)
    if not 0.8 <= ratio <= 1.2 and quality == "good":
        quality = "ok"
    return dict(info, ok=True, method="sweep", phase_deg=float(wrap(mid)), spacing_m=float(d_eff),
                ref_power_db=ref_p, flip=flip, n_bursts=n, quality=quality,
                direction_check=("mirrored: fixed by the saved flip" if flip else "ok") + " (from the sweep)")


# --------------------------------------------------------------------------- AR
def _interp_poses(poses, t, max_gap=0.6):
    """Position at each packet time; NaN where the phone was not tracking around that moment."""
    pt = poses[:, 0]
    i = np.searchsorted(pt, t)
    ok = (i > 0) & (i < len(pt))
    i0, i1 = np.clip(i - 1, 0, len(pt) - 1), np.clip(i, 0, len(pt) - 1)
    ok &= (pt[i1] - pt[i0]) <= max_gap
    w = np.where(pt[i1] > pt[i0], (t - pt[i0]) / np.maximum(pt[i1] - pt[i0], 1e-9), 0.0)
    pos = poses[i0, 1:4] * (1 - w[:, None]) + poses[i1, 1:4] * w[:, None]
    pos[~ok] = np.nan
    return pos


def _coherence(ph_rad, ux, uz, K_deg, alpha_deg):
    """|mean exp(j(phase - k.u))| for every (K, alpha) pair. Shapes broadcast to (nK, nA)."""
    K = np.radians(np.asarray(K_deg))[:, None, None]
    a = np.radians(np.asarray(alpha_deg))[None, :, None]
    model = K * (np.cos(a) * ux[None, None, :] + np.sin(a) * uz[None, None, :])
    return np.abs(np.mean(np.exp(1j * (ph_rad[None, None, :] - model)), axis=2))


def fit_ar(pk, poses, origin, freq, spacing_ruler, min_range=0.4):
    """pk: (n,3) [t, p_db, phase_deg]; poses: (m,4) [t, x, y, z] ARCore world (y up, metres),
    times on the same clock as pk; origin: phone position when held at the middle of the antennas."""
    lam = C / freq
    if len(pk) < 30:
        return _fail(f"Only {len(pk)} packets from the phone. Walk around for 20-30 s.", n_total=len(pk))
    if len(poses) < 20:
        return _fail("The phone sent almost no positions. Keep the camera looking at the room (not covered, "
                     "not a blank wall) while you walk.", n_total=len(pk))
    poses = poses[np.argsort(poses[:, 0])]
    pos = _interp_poses(poses, pk[:, 0])
    v = pos - np.asarray(origin, dtype=float)[None, :]
    rng_h = np.hypot(v[:, 0], v[:, 2])                    # horizontal range
    rng = np.linalg.norm(v, axis=1)
    use = np.isfinite(rng) & (rng_h >= min_range)
    if use.sum() < 30:
        return _fail(f"Only {int(use.sum())} packets had a known position at least {min_range:.1f} m from the "
                     "antennas. Step back and walk a wide arc in front of them.", n_total=len(pk))
    ph = np.radians(pk[use, 2])
    ux, uz = v[use, 0] / rng[use], v[use, 2] / rng[use]   # includes cos(elevation), as the physics does
    K0 = 360.0 * spacing_ruler / lam
    Ks = np.linspace(0.5 * K0, 1.5 * K0, 41)
    als = np.arange(0.0, 360.0, 2.0)
    R = _coherence(ph, ux, uz, Ks, als)
    iK, iA = np.unravel_index(np.argmax(R), R.shape)
    Ks2 = np.linspace(Ks[max(iK - 1, 0)], Ks[min(iK + 1, len(Ks) - 1)], 21)
    als2 = np.linspace(als[iA] - 2.0, als[iA] + 2.0, 41)
    R2 = _coherence(ph, ux, uz, Ks2, als2)
    jK, jA = np.unravel_index(np.argmax(R2), R2.shape)
    K, alpha, coh = float(Ks2[jK]), float(np.radians(als2[jA])), float(R2[jK, jA])
    kx, kz = np.cos(alpha), np.sin(alpha)                 # unit vector along the antenna pair
    resid = ph - np.radians(K) * (kx * ux + kz * uz)
    phi0 = float(np.degrees(np.angle(np.mean(np.exp(1j * resid)))))
    spread = float(np.degrees(np.sqrt(-2 * np.log(max(coh, 1e-9)))))

    # front = the side the phone walked on; right = to the right when looking that way
    fx, fz = kz, -kx
    if np.mean(ux * fx + uz * fz) < 0:
        fx, fz = -fx, -fz
    rx, rz = -fz, fx
    flip = bool(kx * rx + kz * rz < 0)
    truth = np.degrees(np.arctan2(ux * rx + uz * rz, ux * fx + uz * fz))
    span = float(np.percentile(truth, 97) - np.percentile(truth, 3))
    d_eff = K * lam / 360.0
    info = dict(n_total=int(len(pk)), n_used=int(use.sum()), span_deg=round(span, 0),
                fitted_spacing_m=round(d_eff, 4), spacing_ratio=round(d_eff / spacing_ruler, 2),
                phase_spread_deg=round(spread, 1),
                range_min_m=round(float(rng[use].min()), 2), range_max_m=round(float(rng[use].max()), 2))
    if span < 50:
        return _fail(f"The walk only covered {span:.0f}° of directions. Walk from one side of the antennas "
                     "to the other (at least 60°).", **info)
    if coh < 0.45:
        return _fail("The radio signal did not follow the phone's position. This is about the Bluetooth signal, "
                     "not the camera: it is bouncing off walls, the desk or metal, so the antennas hear echoes "
                     "instead of the direct path. Lift the antennas off the desk, move away from walls and metal "
                     "(or go outside), keep a clear line of sight, and try again. Also check the origin was set "
                     "with the phone right between the two antennas.", **info)

    # what the radar would have shown for each packet with this calibration
    s = np.degrees(wrap(np.degrees(resid) - phi0) * np.pi / 180.0) / K * (-1.0 if flip else 1.0)
    s_true = np.sin(np.radians(truth))
    est = np.degrees(np.arcsin(np.clip(s_true + s, -1, 1)))
    inner = np.abs(truth) <= 60
    err = wrap(est - truth)[inner] if inner.sum() >= 10 else wrap(est - truth)
    info.update(bearing_err_median_deg=round(float(np.median(np.abs(err))), 1),
                bearing_err_rms_deg=round(float(np.sqrt(np.mean(err ** 2))), 1))

    # power vs true range: p = P(1 m) - 10 n log10(r)
    p, lr = pk[use, 1], np.log10(rng[use])
    n_exp = None
    if rng[use].max() / rng[use].min() >= 1.8:
        A = np.stack([np.ones_like(lr), -10.0 * lr], axis=1)
        sol = np.linalg.lstsq(A, p, rcond=None)[0]
        keep = np.abs(p - A @ sol) < 2.5 * np.std(p - A @ sol) + 1e-9      # one pass of outlier removal
        sol = np.linalg.lstsq(A[keep], p[keep], rcond=None)[0]
        if 1.0 <= sol[1] <= 5.0:
            ref_p, n_exp = float(sol[0]), float(sol[1])
    if n_exp is None:
        ref_p = float(np.median(p + 22.0 * lr))            # default exponent 2.2
    out = dict(info, ok=True, method="ar", phase_deg=phi0, spacing_m=float(d_eff), ref_power_db=ref_p,
               flip=flip, n_bursts=int(use.sum()), quality=_quality(spread),
               direction_check=("mirrored: fixed by the saved flip" if flip else "ok") + " (from AR positions)")
    if n_exp is not None:
        out["path_loss_exp"] = round(n_exp, 2)
    return out


# --------------------------------------------------------------------------- self test
def _selftest():
    rng = np.random.default_rng(3)
    freq, d = 2.480e9, 0.055
    lam = C / freq
    K = 360 * d / lam
    print(f"truth: spacing {d * 100:.1f} cm (ruler says 6.0), K = {K:.0f} deg")
    for offset, mirrored in ((28.0, False), (-140.0, True)):
        sign = -1.0 if mirrored else 1.0
        # sweep: 12 s, left -> right, 7 packets/s, 12 deg phase noise, a few wild packets
        t = np.sort(rng.uniform(0, 12, 85))
        th = np.radians(-90 + 180 * t / 12)
        ph = offset + sign * K * np.sin(th) + rng.normal(0, 12, len(t))
        ph[rng.integers(0, len(t), 4)] += 150
        r = fit_sweep(np.stack([t, -20 + rng.normal(0, 1, len(t)), wrap(ph)], 1), freq, 0.06)
        print(f"sweep  offset {offset:+6.1f} mirrored={mirrored}:  ->", {k: r.get(k) for k in
              ("ok", "phase_deg", "spacing_m", "flip", "quality", "phase_spread_deg", "error")})
        assert r["ok"] and abs(wrap(r["phase_deg"] - offset)) < 12 and r["flip"] == mirrored and abs(r["spacing_m"] - d) < 0.008

        # AR: array at (2, 1, -3), broadside pointing 40 deg off the world axes, random walk in front
        org = np.array([2.0, 1.0, -3.0])
        beta = np.radians(40.0)
        f, rt = np.array([np.sin(beta), 0, -np.cos(beta)]), np.array([np.cos(beta), 0, np.sin(beta)])
        tp = np.arange(0, 30, 1 / 15)
        bearing = np.radians(70 * np.sin(2 * np.pi * tp / 17))
        dist = 1.6 + 1.0 * np.sin(2 * np.pi * tp / 11)
        pos = org + dist[:, None] * (np.cos(bearing)[:, None] * f + np.sin(bearing)[:, None] * rt)
        pos[:, 1] += 0.1 * np.sin(tp)
        poses = np.column_stack([tp, pos + rng.normal(0, 0.01, pos.shape)])
        t = np.sort(rng.uniform(0.5, 29.5, 200))
        b, dd = np.interp(t, tp, bearing), np.interp(t, tp, dist)
        ph = offset + sign * K * np.sin(b) + rng.normal(0, 12, len(t))
        p = -18.0 - 10 * 2.6 * np.log10(dd) + rng.normal(0, 1.5, len(t))
        r = fit_ar(np.stack([t, p, wrap(ph)], 1), poses, org, freq, 0.06)
        print(f"AR     offset {offset:+6.1f} mirrored={mirrored}:  ->", {k: r.get(k) for k in
              ("ok", "phase_deg", "spacing_m", "flip", "ref_power_db", "path_loss_exp", "quality",
               "bearing_err_median_deg", "span_deg", "error")})
        assert r["ok"] and abs(wrap(r["phase_deg"] - offset)) < 8 and r["flip"] == mirrored
        assert abs(r["spacing_m"] - d) < 0.006 and abs(r["path_loss_exp"] - 2.6) < 0.3 and abs(r["ref_power_db"] + 18) < 1.5
    # half a sweep must be refused
    t = np.sort(rng.uniform(0, 12, 85))
    r = fit_sweep(np.stack([t, -20 + 0 * t, wrap(28 + K * np.sin(np.radians(-30 + 60 * t / 12)))], 1), freq, 0.06)
    print("partial sweep ->", r["ok"], "|", r["error"][:70])
    assert not r["ok"]
    print("selftest ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["selftest"]:
        _selftest()
    else:
        print(__doc__)
