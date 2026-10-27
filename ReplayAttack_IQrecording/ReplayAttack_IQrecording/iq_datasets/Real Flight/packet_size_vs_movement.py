#!/usr/bin/env python3
"""
Packet size vs. drone movement plot.
Correlates effective_size_bytes (proxy for data volume per 10 ms window)
with approaching / departing movement direction derived from rolling-peak power.

Core hypothesis: as the drone approaches the receiver and SNR rises, the
received burst is captured more completely → larger apparent effective_size.
As it departs, the burst gets weaker and partially falls below the ADC
threshold → smaller effective_size.
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from scipy.ndimage import uniform_filter1d
from pathlib import Path

BASE       = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT        = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

NOISE_FLOOR = -51.9   # dB — measured baseline, April 16
ROLL_WIN    = 100     # 1 s rolling window (100 × 10 ms)
TREND_WIN   = 200     # 2 s window for slope calculation
SMOOTH_SIZE = 150     # smoothing for effective_size rolling mean

FLIGHTS = {
    "Apr 16 — Flight 1": BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr 16 — Flight 2": BASE / "real_flight_10ms_20260416_111305.csv",
}

# ── helpers ──────────────────────────────────────────────────────────────────

def rolling_max(arr, w):
    out = np.empty_like(arr)
    half = w // 2
    for i in range(len(arr)):
        out[i] = np.max(arr[max(0, i - half): i + half + 1])
    return out

def movement_slope(rmax_sm, win=200):
    slope = np.zeros(len(rmax_sm))
    for i in range(len(rmax_sm)):
        lo = max(0, i - win // 2)
        hi = min(len(rmax_sm), i + win // 2)
        x  = np.arange(hi - lo, dtype=float)
        y  = rmax_sm[lo:hi]
        if len(x) > 3:
            slope[i] = np.polyfit(x, y, 1)[0]
    return slope

# ── figure setup ─────────────────────────────────────────────────────────────

fig, axes = plt.subplots(
    len(FLIGHTS), 1,
    figsize=(14, 5 * len(FLIGHTS)),
    sharex=False
)
if len(FLIGHTS) == 1:
    axes = [axes]

fig.suptitle(
    'Packet Size vs. Drone Proximity — OcuSync 2.0, April 16, 2026\n'
    'gain=70 dB · 2 MHz BW · 5.845 GHz',
    fontsize=13, fontweight='bold'
)

# ── per-flight ────────────────────────────────────────────────────────────────

for ax, (label, path) in zip(axes, FLIGHTS.items()):
    rows    = list(csv.DictReader(open(path)))
    t       = np.arange(len(rows)) * 10 / 1000.0           # seconds
    mxpw    = np.array([float(r['max_power_db'])       for r in rows])
    eff_sz  = np.array([float(r['effective_size_bytes']) for r in rows])
    detected = mxpw > NOISE_FLOOR

    # Zero out size for windows where no signal was detected — those windows
    # contain only noise and their effective_size is meaningless for the
    # proximity hypothesis.
    eff_sz_gated = np.where(detected, eff_sz, np.nan)

    # Rolling mean of gated size (ignore NaNs)
    def nanrolling_mean(arr, w):
        out = np.full_like(arr, np.nan)
        half = w // 2
        for i in range(len(arr)):
            seg = arr[max(0, i - half): i + half + 1]
            good = seg[~np.isnan(seg)]
            if len(good) > 0:
                out[i] = good.mean()
        return out

    sz_smooth = nanrolling_mean(eff_sz_gated, SMOOTH_SIZE)

    # Movement direction from rolling-max power slope
    rmax     = rolling_max(mxpw, ROLL_WIN)
    rmax_sm  = uniform_filter1d(rmax, size=150)
    slope    = movement_slope(rmax_sm, win=TREND_WIN)
    slope_sm = uniform_filter1d(slope, size=100)
    max_abs  = np.nanpercentile(np.abs(slope_sm), 95) or 1.0
    slope_norm = np.clip(slope_sm / max_abs, -1, 1)

    # ── Plot ─────────────────────────────────────────────────────────────────
    ax.set_facecolor('#f8f9fa')

    # --- background shading: approach (green) / depart (red) / transitional
    for i in range(len(t) - 1):
        if slope_norm[i] > 0.05:
            ax.axvspan(t[i], t[i+1], color='#2ecc71', alpha=0.12, zorder=0)
        elif slope_norm[i] < -0.05:
            ax.axvspan(t[i], t[i+1], color='#e74c3c', alpha=0.12, zorder=0)
        else:
            ax.axvspan(t[i], t[i+1], color='#f39c12', alpha=0.06, zorder=0)

    # --- raw scatter: only detected windows, coloured by direction
    for i in range(len(t)):
        if not np.isnan(eff_sz_gated[i]):
            c = ('#27ae60' if slope_norm[i] > 0.05
                 else '#e74c3c' if slope_norm[i] < -0.05
                 else '#f39c12')
            ax.scatter(t[i], eff_sz_gated[i] / 1000.0,
                       color=c, s=8, alpha=0.35, zorder=2, linewidths=0)

    # --- smoothed size line (solid, black)
    ax.plot(t, sz_smooth / 1000.0, color='#2c3e50', lw=2.2, zorder=4,
            label='Rolling mean (signal windows only)')

    # --- secondary axis: rolling-max power (dashed, grey)
    ax2 = ax.twinx()
    ax2.plot(t, rmax_sm, color='#8e44ad', lw=1.5, linestyle='--',
             alpha=0.7, zorder=3, label='Rolling-max power (dB)')
    ax2.axhline(NOISE_FLOOR, color='#8e44ad', lw=1, linestyle=':',
                alpha=0.5, label=f'Noise floor ({NOISE_FLOOR} dB)')
    ax2.set_ylabel('Rolling-max power (dB)', fontsize=10, color='#8e44ad')
    ax2.tick_params(axis='y', labelcolor='#8e44ad')
    ax2.set_ylim(NOISE_FLOOR - 8, 5)

    # Annotate longest approach / depart segments
    def longest_run(mask):
        runs, start = [], None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append((start, i)); start = None
        if start is not None:
            runs.append((start, len(mask)))
        return max(runs, key=lambda r: r[1] - r[0]) if runs else (0, 0)

    app_run = longest_run(slope_norm > 0.1)
    dep_run = longest_run(slope_norm < -0.1)

    if app_run[1] - app_run[0] > 50:
        mid = (app_run[0] + app_run[1]) // 2
        if not np.isnan(sz_smooth[mid]):
            ax.annotate('← Approaching',
                        xy=(t[mid], sz_smooth[mid] / 1000.0),
                        xytext=(t[mid], sz_smooth[mid] / 1000.0 + 15),
                        ha='center', fontsize=10, color='#27ae60', fontweight='bold',
                        arrowprops=dict(arrowstyle='->', color='#27ae60', lw=1.5))

    if dep_run[1] - dep_run[0] > 50:
        mid = (dep_run[0] + dep_run[1]) // 2
        if not np.isnan(sz_smooth[mid]):
            ax.annotate('Departing →',
                        xy=(t[mid], sz_smooth[mid] / 1000.0),
                        xytext=(t[mid], sz_smooth[mid] / 1000.0 + 15),
                        ha='center', fontsize=10, color='#e74c3c', fontweight='bold',
                        arrowprops=dict(arrowstyle='->', color='#e74c3c', lw=1.5))

    # ── labels ───────────────────────────────────────────────────────────────
    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Effective Packet Size (kB / 10 ms window)', fontsize=11)
    ax.set_title(label, fontsize=12, fontweight='bold', pad=10)
    ax.set_xlim(t[0], t[-1])
    ax.grid(True, alpha=0.2, zorder=0)

    # Detection fraction annotation
    det_pct = 100 * detected.mean()
    ax.text(0.02, 0.96, f'Signal windows: {det_pct:.1f}%',
            transform=ax.transAxes, fontsize=9,
            va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#ccc', alpha=0.8))

    # Legend (primary axis)
    legend_elements = [
        mpatches.Patch(color='#2ecc71', alpha=0.4, label='Approaching zone'),
        mpatches.Patch(color='#e74c3c', alpha=0.4, label='Departing zone'),
        mpatches.Patch(color='#f39c12', alpha=0.3, label='Stable / transitional'),
        plt.Line2D([0], [0], color='#2c3e50', lw=2.2,
                   label='Rolling mean packet size (signal only)'),
        plt.scatter([], [], color='#27ae60', s=20, alpha=0.6,
                    label='Detected window — approaching'),
        plt.scatter([], [], color='#e74c3c', s=20, alpha=0.6,
                    label='Detected window — departing'),
    ]
    ax.legend(handles=legend_elements, loc='lower right',
              fontsize=8, framealpha=0.9)

plt.tight_layout(rect=[0, 0, 1, 0.96])
out_path = OUT / "packet_size_vs_movement.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
print(f"Saved: {out_path}")
