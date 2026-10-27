#!/usr/bin/env python3
"""
Packet size as a drone proximity proxy.
Focuses purely on effective_size_bytes over time:
  - Large size  → drone close (strong burst, many samples above threshold)
  - Small size  → drone far  (weak burst, fewer samples above threshold)

Only signal-detected windows (max_power_db > noise floor) are shown.
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.ndimage import uniform_filter1d
from pathlib import Path

BASE        = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT         = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

NOISE_FLOOR = -51.9   # dB
SMOOTH_WIN  = 150     # samples (~1.5 s)

FLIGHTS = {
    "Apr 16 — Flight 1": BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr 16 — Flight 2": BASE / "real_flight_10ms_20260416_111305.csv",
}

fig, axes = plt.subplots(len(FLIGHTS), 1, figsize=(14, 5 * len(FLIGHTS)), sharex=False)
if len(FLIGHTS) == 1:
    axes = [axes]

fig.suptitle(
    'Effective Packet Size as Drone Proximity Indicator\n'
    'OcuSync 2.0 — April 16, 2026  (gain=70 dB · 2 MHz BW · 5.845 GHz)',
    fontsize=13, fontweight='bold'
)

for ax, (label, path) in zip(axes, FLIGHTS.items()):
    rows     = list(csv.DictReader(open(path)))
    t        = np.arange(len(rows)) * 10 / 1000.0
    mxpw     = np.array([float(r['max_power_db'])        for r in rows])
    eff_sz   = np.array([float(r['effective_size_bytes']) for r in rows])
    detected = mxpw > NOISE_FLOOR

    # Gate to signal-only windows
    eff_gated = np.where(detected, eff_sz / 1000.0, np.nan)   # kB

    # Rolling mean ignoring NaNs
    def nanroll_mean(arr, w):
        out = np.full_like(arr, np.nan)
        half = w // 2
        for i in range(len(arr)):
            seg  = arr[max(0, i - half): i + half + 1]
            good = seg[~np.isnan(seg)]
            if len(good) > 0:
                out[i] = good.mean()
        return out

    sz_smooth = nanroll_mean(eff_gated, SMOOTH_WIN)

    # Percentile bands for "close" / "far" thresholds
    valid     = eff_gated[~np.isnan(eff_gated)]
    p25       = np.percentile(valid, 25)
    p75       = np.percentile(valid, 75)
    p_median  = np.percentile(valid, 50)

    # ── background gradient zones ────────────────────────────────────────────
    ax.set_facecolor('#f8f9fa')
    ax.axhspan(p75,    valid.max() + 10, color='#2ecc71', alpha=0.10, zorder=0, label='Close range zone')
    ax.axhspan(0,      p25,              color='#e74c3c', alpha=0.10, zorder=0, label='Far range zone')
    ax.axhspan(p25,    p75,              color='#f39c12', alpha=0.06, zorder=0, label='Mid range zone')

    # Threshold lines
    ax.axhline(p75,     color='#27ae60', lw=1.2, linestyle='--', alpha=0.7,
               label=f'75th pct ({p75:.0f} kB) — close')
    ax.axhline(p25,     color='#e74c3c', lw=1.2, linestyle='--', alpha=0.7,
               label=f'25th pct ({p25:.0f} kB) — far')
    ax.axhline(p_median, color='#7f8c8d', lw=1.0, linestyle=':', alpha=0.6,
               label=f'Median ({p_median:.0f} kB)')

    # Raw scatter — colour by position relative to median
    for i in range(len(t)):
        if not np.isnan(eff_gated[i]):
            c = '#27ae60' if eff_gated[i] > p75 else \
                '#e74c3c' if eff_gated[i] < p25 else '#95a5a6'
            ax.scatter(t[i], eff_gated[i], color=c, s=9,
                       alpha=0.35, zorder=2, linewidths=0)

    # Smoothed line
    ax.plot(t, sz_smooth, color='#2c3e50', lw=2.5, zorder=4,
            label='Rolling mean (signal windows only)')

    # Fill under the smoothed line, colour by zone
    ax.fill_between(t, 0, sz_smooth,
                    where=sz_smooth >= p75,
                    color='#2ecc71', alpha=0.20, zorder=3,
                    interpolate=True)
    ax.fill_between(t, 0, sz_smooth,
                    where=sz_smooth <= p25,
                    color='#e74c3c', alpha=0.20, zorder=3,
                    interpolate=True)
    ax.fill_between(t, 0, sz_smooth,
                    where=((sz_smooth > p25) & (sz_smooth < p75)),
                    color='#f39c12', alpha=0.10, zorder=3,
                    interpolate=True)

    # Annotate peaks (closest) and troughs (furthest)
    if not np.all(np.isnan(sz_smooth)):
        peak_idx  = int(np.nanargmax(sz_smooth))
        trough_idx = int(np.nanargmin(sz_smooth))

        ax.annotate(f'Closest\n({sz_smooth[peak_idx]:.0f} kB)',
                    xy=(t[peak_idx], sz_smooth[peak_idx]),
                    xytext=(t[peak_idx], sz_smooth[peak_idx] + 12),
                    ha='center', fontsize=9, color='#27ae60', fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='#27ae60', lw=1.5))

        ax.annotate(f'Furthest\n({sz_smooth[trough_idx]:.0f} kB)',
                    xy=(t[trough_idx], sz_smooth[trough_idx]),
                    xytext=(t[trough_idx], sz_smooth[trough_idx] - 18),
                    ha='center', fontsize=9, color='#e74c3c', fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='#e74c3c', lw=1.5))

    # Detection % annotation
    det_pct = 100 * detected.mean()
    ax.text(0.02, 0.97,
            f'Signal windows: {det_pct:.1f}%  |  '
            f'Noise floor: {NOISE_FLOOR} dB  |  '
            f'Each dot = 1 × 10 ms window',
            transform=ax.transAxes, fontsize=8.5,
            va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#ccc', alpha=0.85))

    # "Closer ↑ / Further ↓" label on y-axis
    y_max = valid.max() + 15
    ax.annotate('Drone closer ↑', xy=(0, y_max), xytext=(-0.07, y_max),
                xycoords=('axes fraction', 'data'),
                textcoords=('axes fraction', 'data'),
                fontsize=8, color='#27ae60', fontweight='bold', ha='left')
    ax.annotate('Drone further ↓', xy=(0, p25 - 10), xytext=(-0.07, p25 - 10),
                xycoords=('axes fraction', 'data'),
                textcoords=('axes fraction', 'data'),
                fontsize=8, color='#e74c3c', fontweight='bold', ha='left')

    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Effective Packet Size (kB per 10 ms window)', fontsize=11)
    ax.set_title(label, fontsize=12, fontweight='bold', pad=10)
    ax.set_xlim(t[0], t[-1])
    y_pad = (valid.max() - valid.min()) * 0.15
    ax.set_ylim(valid.min() - y_pad, valid.max() + y_pad + 20)
    ax.grid(True, alpha=0.18, zorder=0)
    ax.legend(loc='upper right', fontsize=8, framealpha=0.9, ncol=2)

plt.tight_layout(rect=[0, 0, 1, 0.95])
out_path = OUT / "packet_size_distance_proxy.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
print(f"Saved: {out_path}")
