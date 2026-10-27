#!/usr/bin/env python3
"""
Power-Distance plot using rolling-max of max_power_db instead of relative_distance.
Rolling max captures the strongest burst in each 1-second window regardless of
OcuSync hopping gaps, recovering the actual flight trajectory.
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from scipy.ndimage import uniform_filter1d

BASE = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT  = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

NOISE_FLOOR = -51.9   # measured April 16 baseline

FLIGHTS = {
    "Apr16 Flight-1 (17s)": BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr16 Flight-2 (39s)": BASE / "real_flight_10ms_20260416_111305.csv",
}
COLORS = {"Apr16 Flight-1 (17s)": "#1f77b4", "Apr16 Flight-2 (39s)": "#2ca02c"}

ROLL_WIN = 100   # 100 windows = 1 second — captures peak burst per second

def rolling_max(arr, w):
    out = np.full_like(arr, np.nan)
    for i in range(len(arr)):
        out[i] = np.max(arr[max(0, i-w//2): i+w//2+1])
    return out

def flat_point(series, threshold_pct=0.9):
    valid = series[np.isfinite(series)]
    lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
    thresh = lo + threshold_pct * (hi - lo)
    idxs = np.where(series > thresh)[0]
    return int(idxs[0]) if len(idxs) else len(series)//2

# ── Path-loss distance from rolling-max ─────────────────────────────────────
# d ∝ 10^( (P_ref - P_rx) / (20 * n) )
# Use first window's rolling-max as p_ref anchor (drone on ground, ~3m from antenna)
PATH_LOSS_EXP = 2.2
REF_DIST_M    = 3.0

fig, axes = plt.subplots(len(FLIGHTS) * 3, 1,
                         figsize=(14, 7 * len(FLIGHTS)),
                         gridspec_kw={'height_ratios': [2, 1.5, 1] * len(FLIGHTS)})
fig.suptitle('Power–Distance Correlation Using Rolling-Max (1s window)\n'
             'Recovery of flight trajectory from OcuSync hopping data',
             fontsize=13, fontweight='bold')

row = 0
for label, path in FLIGHTS.items():
    rows = list(csv.DictReader(open(path)))
    mpw  = np.array([float(r['mean_power_db']) for r in rows])
    mxpw = np.array([float(r['max_power_db'])  for r in rows])
    t    = np.arange(len(rows)) * 10 / 1000.0   # seconds

    # Rolling max over 1s window
    rmax = rolling_max(mxpw, ROLL_WIN)

    # Smooth rolling max lightly for the distance curve
    rmax_sm = uniform_filter1d(rmax, size=50)

    # p_ref = rolling-max value at t=0 (drone on ground near antenna)
    p_ref_db = rmax_sm[0]
    print(f"{label}: p_ref = {p_ref_db:.1f} dB (first window)")

    # Distance estimate from rolling-max: d = ref_d * 10^((p_ref - p_rx)/(20*n))
    with np.errstate(invalid='ignore', divide='ignore'):
        dist_raw = REF_DIST_M * 10 ** ((p_ref_db - rmax) / (20 * PATH_LOSS_EXP))
        dist_sm  = REF_DIST_M * 10 ** ((p_ref_db - rmax_sm) / (20 * PATH_LOSS_EXP))

    dist_raw = np.clip(dist_raw, 0, 200)
    dist_sm  = np.clip(dist_sm,  0, 200)

    # Detect flat point (when drone has moved too far — rolling max plateaus)
    fp = flat_point(rmax_sm, threshold_pct=0.95)

    # ── Panel 1: rolling-max power ──────────────────────────────────────────
    ax1 = axes[row]
    ax1.plot(t, mxpw, color='lightsteelblue', lw=0.4, alpha=0.5, label='max_power_db (raw)')
    ax1.plot(t, mpw,  color='lightgray',       lw=0.4, alpha=0.4, label='mean_power_db (raw)')
    ax1.plot(t, rmax,    color=COLORS[label], lw=0.9, alpha=0.6, label=f'Rolling max (1s)')
    ax1.plot(t, rmax_sm, color='navy',         lw=2.0,            label='Rolling max (smoothed)')
    ax1.axhline(NOISE_FLOOR, color='black', linestyle=':', lw=1.2, label=f'Noise floor ({NOISE_FLOOR} dB)')
    ax1.axvline(t[fp], color='navy', linestyle='--', lw=1.5, label=f'Signal floor @ {t[fp]:.1f}s')
    ax1.set_ylabel('Power (dB)', fontsize=10)
    ax1.set_title(f'{label} — Rolling-Max Power', fontsize=11, fontweight='bold')
    ax1.legend(loc='upper right', fontsize=8, ncol=2)
    ax1.grid(True, alpha=0.25)
    ax1.set_ylim(-75, 5)

    # Shade the "in-flight" zone (before floor)
    ax1.axvspan(0, t[fp], alpha=0.06, color='green', label='_in-flight zone')

    # ── Panel 2: distance estimate ──────────────────────────────────────────
    ax2 = axes[row + 1]
    ax2.plot(t, dist_raw, color='lightcoral', lw=0.5, alpha=0.5, label='Distance raw')
    ax2.plot(t, dist_sm,  color='darkred',    lw=2.0,            label='Distance smoothed')
    ax2.axvline(t[fp], color='darkred', linestyle='--', lw=1.5,
                label=f'Distance flat aligned @ {t[fp]:.1f}s')
    ax2.axvspan(0, t[fp], alpha=0.06, color='green')
    ax2.set_ylabel('Est. Distance (m)', fontsize=10)
    ax2.set_title(f'{label} — Path-Loss Distance (from rolling-max, n={PATH_LOSS_EXP})', fontsize=11)
    ax2.legend(loc='upper left', fontsize=8)
    ax2.grid(True, alpha=0.25)
    ax2.set_ylim(0, min(200, np.nanpercentile(dist_sm[dist_sm < 200], 99) * 2))

    # Annotate takeoff / landing distance
    in_flight = dist_sm[:fp]
    if len(in_flight) > 0:
        peak_dist = np.nanmax(in_flight)
        peak_t    = t[np.nanargmax(in_flight)]
        ax2.annotate(f'Peak ~{peak_dist:.0f}m @ {peak_t:.1f}s',
                     xy=(peak_t, peak_dist), xytext=(peak_t + 0.5, peak_dist * 0.8),
                     arrowprops=dict(arrowstyle='->', color='darkred'),
                     fontsize=9, color='darkred')

    # ── Panel 3: per-second max power bar chart ─────────────────────────────
    ax3 = axes[row + 2]
    sec_bins = np.arange(0, t[-1] + 1, 1)
    sec_max  = []
    for s in sec_bins[:-1]:
        mask = (t >= s) & (t < s + 1)
        sec_max.append(np.max(mxpw[mask]) if np.any(mask) else NOISE_FLOOR)
    sec_max = np.array(sec_max)
    bar_colors = ['#2ca02c' if v > NOISE_FLOOR + 3 else '#d62728' for v in sec_max]
    ax3.bar(sec_bins[:-1] + 0.5, sec_max - NOISE_FLOOR,
            width=0.8, color=bar_colors, alpha=0.8, label='Peak burst above floor')
    ax3.axhline(0, color='black', lw=1.5, linestyle='--', label='Noise floor')
    ax3.set_ylabel('dB above floor', fontsize=10)
    ax3.set_xlabel('Time (s)', fontsize=10)
    ax3.set_title(f'{label} — Peak burst per second (green = signal present)', fontsize=11)
    ax3.legend(fontsize=8)
    ax3.grid(True, alpha=0.25)

    row += 3

plt.tight_layout(rect=[0, 0, 1, 0.97])
out_path = OUT / "power_distance_rolling_max_april16.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"\nSaved → {out_path}")
