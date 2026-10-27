#!/usr/bin/env python3
"""
Clean drone detection + approach/departure visualization.
No absolute distance numbers — focuses purely on:
  1. Signal presence (drone detected vs not)
  2. Power trend (approaching = rising, departing = falling)
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from scipy.ndimage import uniform_filter1d
from pathlib import Path

BASE = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT  = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

NOISE_FLOOR = -51.9
ROLL_WIN    = 100   # 1 second
TREND_WIN   = 200   # 2 seconds for slope calculation

FLIGHTS = {
    "Apr 16 — Flight 1": BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr 16 — Flight 2": BASE / "real_flight_10ms_20260416_111305.csv",
}

def rolling_max(arr, w):
    out = np.empty_like(arr)
    half = w // 2
    for i in range(len(arr)):
        out[i] = np.max(arr[max(0, i-half): i+half+1])
    return out

def movement_direction(rmax_sm, win=200):
    """
    Returns slope at each point over a rolling window.
    Positive = power rising = approaching
    Negative = power falling = departing
    """
    slope = np.zeros(len(rmax_sm))
    for i in range(len(rmax_sm)):
        lo = max(0, i - win // 2)
        hi = min(len(rmax_sm), i + win // 2)
        x = np.arange(hi - lo, dtype=float)
        y = rmax_sm[lo:hi]
        if len(x) > 3:
            slope[i] = np.polyfit(x, y, 1)[0]
    return slope


fig, axes = plt.subplots(len(FLIGHTS), 1,
                         figsize=(14, 5 * len(FLIGHTS)),
                         sharex=False)
if len(FLIGHTS) == 1:
    axes = [axes]

fig.suptitle('Drone Detection and Movement Tracking via RF Power Analysis\n'
             'OcuSync 2.0 — April 16, 2026  (gain=70 dB, 2 MHz BW, 5.845 GHz)',
             fontsize=13, fontweight='bold')

for ax, (label, path) in zip(axes, FLIGHTS.items()):
    rows  = list(csv.DictReader(open(path)))
    mxpw  = np.array([float(r['max_power_db']) for r in rows])
    t     = np.arange(len(rows)) * 10 / 1000.0

    # Rolling max + smooth
    rmax    = rolling_max(mxpw, ROLL_WIN)
    rmax_sm = uniform_filter1d(rmax, size=150)

    # Detection: above noise floor
    detected = rmax > NOISE_FLOOR

    # Movement slope
    slope = movement_direction(rmax_sm, win=TREND_WIN)
    slope_sm = uniform_filter1d(slope, size=100)

    # Normalise slope for overlay on power axis
    # Map: approaching (slope > 0) = green zone, departing (slope < 0) = red zone
    max_abs_slope = np.nanpercentile(np.abs(slope_sm), 95) or 1.0
    slope_norm = np.clip(slope_sm / max_abs_slope, -1, 1)

    # ── Main power axes ──────────────────────────────────────────────────────
    ax.set_facecolor('#f8f9fa')

    # Raw max power — faint
    ax.plot(t, mxpw, color='#aec6cf', lw=0.4, alpha=0.5, zorder=1)

    # Fill: detected (above floor) green, not detected grey
    ax.fill_between(t, NOISE_FLOOR, rmax,
                    where=detected,
                    color='#2ecc71', alpha=0.25, zorder=2, label='Signal detected')
    ax.fill_between(t, NOISE_FLOOR, rmax,
                    where=~detected,
                    color='#bdc3c7', alpha=0.2, zorder=2, label='Below noise floor')

    # Rolling-max line coloured by direction
    # Draw in segments to colour approaching green, departing red
    for i in range(len(t) - 1):
        color = '#27ae60' if slope_norm[i] > 0.05 else \
                '#e74c3c' if slope_norm[i] < -0.05 else '#f39c12'
        ax.plot(t[i:i+2], rmax_sm[i:i+2], color=color, lw=2.5, solid_capstyle='round', zorder=4)

    ax.axhline(NOISE_FLOOR, color='#2c3e50', linestyle='--', lw=1.5,
               label=f'Noise floor ({NOISE_FLOOR} dB)', zorder=3)

    # ── Detection confidence bar at the bottom ───────────────────────────────
    # Per-second detection indicator
    sec_bins = np.arange(0, t[-1] + 1, 1.0)
    bar_h = 3.5   # dB height for the bar strip at the bottom
    bar_base = NOISE_FLOOR - bar_h - 1
    for s in sec_bins[:-1]:
        mask = (t >= s) & (t < s + 1)
        if np.any(mask):
            present = np.any(rmax[mask] > NOISE_FLOOR)
            ax.barh(bar_base, 1.0, left=s, height=bar_h * 0.7,
                    color='#27ae60' if present else '#e74c3c',
                    alpha=0.85, zorder=5)

    ax.text(-0.2, bar_base, 'Detected\nper sec', ha='right', va='center',
            fontsize=7.5, color='#2c3e50',
            transform=ax.get_yaxis_transform())

    # ── Annotations: movement direction ─────────────────────────────────────
    # Find longest sustained approaching / departing segments
    approaching = slope_norm > 0.1
    departing   = slope_norm < -0.1

    def longest_run(mask):
        runs, start, best = [], None, (0, 0)
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append((start, i))
                start = None
        if start is not None:
            runs.append((start, len(mask)))
        if runs:
            best = max(runs, key=lambda r: r[1]-r[0])
        return best

    app_run = longest_run(approaching)
    dep_run = longest_run(departing)

    if app_run[1] - app_run[0] > 50:
        mid = (app_run[0] + app_run[1]) // 2
        ax.annotate('← Approaching',
                    xy=(t[mid], rmax_sm[mid]),
                    xytext=(t[mid], rmax_sm[mid] + 8),
                    ha='center', fontsize=10, color='#27ae60', fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='#27ae60', lw=1.5))

    if dep_run[1] - dep_run[0] > 50:
        mid = (dep_run[0] + dep_run[1]) // 2
        ax.annotate('Departing →',
                    xy=(t[mid], rmax_sm[mid]),
                    xytext=(t[mid], rmax_sm[mid] + 8),
                    ha='center', fontsize=10, color='#e74c3c', fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='#e74c3c', lw=1.5))

    # ── Labels / legend ──────────────────────────────────────────────────────
    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Peak Received Power (dB)', fontsize=11)
    ax.set_title(label, fontsize=12, fontweight='bold', pad=10)
    ax.set_xlim(t[0], t[-1])
    ax.set_ylim(NOISE_FLOOR - bar_h - 3, 5)
    ax.grid(True, alpha=0.2, zorder=0)

    # Custom legend
    legend_elements = [
        mpatches.Patch(color='#2ecc71', alpha=0.5, label='Signal detected (above noise floor)'),
        mpatches.Patch(color='#bdc3c7', alpha=0.4, label='No signal (below noise floor)'),
        plt.Line2D([0], [0], color='#27ae60', lw=2.5, label='Approaching (power rising)'),
        plt.Line2D([0], [0], color='#e74c3c', lw=2.5, label='Departing (power falling)'),
        plt.Line2D([0], [0], color='#f39c12', lw=2.5, label='Stable / transitioning'),
        plt.Line2D([0], [0], color='#2c3e50', lw=1.5, linestyle='--',
                   label=f'Noise floor ({NOISE_FLOOR} dB)'),
        mpatches.Patch(color='#27ae60', alpha=0.85, label='Detected each second'),
        mpatches.Patch(color='#e74c3c', alpha=0.85, label='Not detected each second'),
    ]
    ax.legend(handles=legend_elements, loc='lower right', fontsize=8,
              ncol=2, framealpha=0.9)

    # Detection rate annotation
    det_pct = 100 * np.mean(detected)
    ax.text(0.01, 0.97,
            f'Detection rate: {det_pct:.0f}% of 10ms windows above noise floor',
            transform=ax.transAxes, fontsize=9, va='top',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8))

plt.tight_layout(rect=[0, 0, 1, 0.95])
out_path = OUT / "drone_detection_tracking.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"Saved → {out_path}")
