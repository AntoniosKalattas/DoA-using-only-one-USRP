#!/usr/bin/env python3
"""
Power-Distance Correlation with Aligned Flat Point
Same plot as detailed_analysis_camera.py Part 1, applied to April 16 flights.
One subplot row per flight.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

BASE = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT  = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

FLIGHTS = {
    "Apr16 Flight-1 (17s, gain=70, 2MHz)":  BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr16 Flight-2 (39s, gain=70, 2MHz)":  BASE / "real_flight_10ms_20260416_111305.csv",
}
SMOOTH_W = 20

def flat_point(series_sm):
    """Return index where smoothed series first exceeds 90% of its range."""
    valid = series_sm[~np.isnan(series_sm)]
    if len(valid) == 0:
        return len(series_sm) // 2
    d_min = np.percentile(valid, 2)
    d_max = np.percentile(valid, 98)
    threshold = d_min + 0.9 * (d_max - d_min)
    idxs = np.where(series_sm > threshold)[0]
    return int(idxs[0]) if len(idxs) else len(series_sm) // 2

fig, axes = plt.subplots(len(FLIGHTS) * 2, 1,
                         figsize=(14, 5 * len(FLIGHTS)),
                         sharex=False)

row = 0
for label, path in FLIGHTS.items():
    df = pd.read_csv(path)

    power_raw = df['mean_power_db'].values
    if 'relative_distance' in df.columns:
        dist_raw  = df['relative_distance'].values
        dist_ylab = 'Relative Distance'
    elif 'est_distance_m' in df.columns:
        dist_raw  = df['est_distance_m'].values
        dist_ylab = 'Est. Distance (m)'
    else:
        dist_raw  = df['amp_mean'].values * -1
        dist_ylab = 'Distance proxy'

    # clip extreme outliers for display
    dist_raw = np.clip(dist_raw, np.nanpercentile(dist_raw, 1),
                                 np.nanpercentile(dist_raw, 99))

    power_sm = (pd.Series(power_raw)
                .rolling(SMOOTH_W, center=True, min_periods=1).mean()
                .ffill().bfill().values)
    dist_sm  = (pd.Series(dist_raw)
                .rolling(SMOOTH_W, center=True, min_periods=1).mean()
                .ffill().bfill().values)

    fp = flat_point(dist_sm)
    win = np.arange(len(power_raw))

    # --- power panel ---
    ax1 = axes[row]
    ax1.plot(win, power_raw, color='steelblue', alpha=0.3, lw=0.5, label='Power raw')
    ax1.plot(win, power_sm,  color='navy',      lw=2.0,   label='Power smoothed')
    ax1.axvline(fp, color='navy', linestyle='--', lw=1.5, label=f'Power flat @ {fp}')
    ax1.axhline(-51.9, color='black', linestyle=':', lw=1.2, alpha=0.7,
                label='Noise floor (−51.9 dB)')
    ax1.set_ylabel('Power (dB)')
    ax1.set_title(f'Power–Distance Correlation with Aligned Flat Point\n{label}', fontsize=11)
    ax1.legend(loc='upper right', fontsize=8)
    ax1.grid(True, alpha=0.3)

    # --- distance panel ---
    ax2 = axes[row + 1]
    ax2.plot(win, dist_raw, color='lightcoral', alpha=0.3, lw=0.5, label='Distance raw')
    ax2.plot(win, dist_sm,  color='darkred',    lw=2.0,   label='Distance smoothed')
    ax2.axvline(fp, color='darkred', linestyle='--', lw=1.5,
                label=f'Distance flat (aligned) @ {fp}')
    ax2.set_ylabel(dist_ylab)
    ax2.set_xlabel('Window Index')
    ax2.legend(loc='lower right', fontsize=8)
    ax2.grid(True, alpha=0.3)

    row += 2

plt.tight_layout()
out_path = OUT / "power_distance_correlation_april16.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"Saved → {out_path}")
