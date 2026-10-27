#!/usr/bin/env python3
"""
April 16, 2026 Flight Analysis
Comprehensive visualizations, fingerprint analysis, and distance metrics
comparing April 16 improved-parameter flights vs April 6 camera flights.
"""

import csv, os, statistics
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from pathlib import Path

BASE = Path(__file__).parent

# ── File registry ──────────────────────────────────────────────────────────
FILES = {
    # April 16
    'APR16_baseline':  BASE / 'no_drone_baseline_10ms_20260416_105448.csv',
    'APR16_flight1':   BASE / 'real_flight_10ms_20260416_110239.csv',
    'APR16_flight2':   BASE / 'real_flight_10ms_20260416_111305.csv',
    'APR16_intheair':  BASE / 'intheair_10ms_20260416_111024.csv',
    # April 6 (camera, noise-reprocessed)
    'APR6_cam1':       BASE / 'real_flight_camera_10ms_20260406_114320_noise70_reprocessed.csv',
    'APR6_cam2':       BASE / 'real_flight_camera_10ms_20260406_114626_noise70_reprocessed.csv',
    'APR6_cam3':       BASE / 'real_flight_camera_10ms_20260406_114837_noise70_reprocessed.csv',
}

COLORS = {
    'APR16_baseline': '#888888',
    'APR16_flight1':  '#1f77b4',
    'APR16_flight2':  '#2ca02c',
    'APR16_intheair': '#9467bd',
    'APR6_cam1':      '#d62728',
    'APR6_cam2':      '#ff7f0e',
    'APR6_cam3':      '#e377c2',
}

LABELS = {
    'APR16_baseline': 'Apr16 No-Drone Baseline',
    'APR16_flight1':  'Apr16 Flight-1 (17s)',
    'APR16_flight2':  'Apr16 Flight-2 (39s)',
    'APR16_intheair': 'Apr16 In-the-Air (5s)',
    'APR6_cam1':      'Apr6 Camera Flight-1',
    'APR6_cam2':      'Apr6 Camera Flight-2',
    'APR6_cam3':      'Apr6 Camera Flight-3',
}

# ── Load helper ─────────────────────────────────────────────────────────────
def load(key):
    path = FILES[key]
    if not path.exists():
        print(f"  MISSING: {path}")
        return []
    rows = list(csv.DictReader(open(path)))
    return rows

def col(rows, name, default=float('nan')):
    out = []
    for r in rows:
        try:
            v = r.get(name, '')
            out.append(float(v) if v not in ('', 'nan', 'None') else default)
        except (ValueError, TypeError):
            out.append(default)
    return np.array(out)

def parse_fp(rows):
    """Parse amp_fingerprint from CSV rows → (N, 128) array."""
    fps = []
    for r in rows:
        raw = r.get('amp_fingerprint', '')
        if raw:
            try:
                fps.append(np.array([float(x) for x in raw.split(',')]))
            except Exception:
                pass
    return np.array(fps) if fps else None


# ═══════════════════════════════════════════════════════════════════════════
# Figure 1 — Power Comparison Overview
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 1: Power Comparison Overview ...")

data = {k: load(k) for k in FILES}

fig, axes = plt.subplots(1, 3, figsize=(16, 6))
fig.suptitle('April 16 vs April 6: Signal Power Comparison', fontsize=14, fontweight='bold')

# 1a) Boxplot: mean_power_db
ax = axes[0]
box_data, box_labels, box_colors = [], [], []
for k in FILES:
    r = data[k]
    if r:
        box_data.append(col(r, 'mean_power_db'))
        box_labels.append(LABELS[k].replace(' ', '\n'))
        box_colors.append(COLORS[k])

bp = ax.boxplot(box_data, patch_artist=True, notch=False,
                medianprops=dict(color='black', linewidth=2))
for patch, c in zip(bp['boxes'], box_colors):
    patch.set_facecolor(c)
    patch.set_alpha(0.7)
ax.axhline(-51.9, color='black', linestyle='--', linewidth=1.5, label='Apr16 noise floor (−51.9 dB)')
ax.axhline(-70.0, color='gray',  linestyle=':', linewidth=1.5, label='Apr6 noise floor (−70 dB)')
ax.set_xticklabels(box_labels, fontsize=7)
ax.set_ylabel('Mean Power (dB)', fontsize=11)
ax.set_title('(a) Mean Window Power Distribution', fontsize=11)
ax.legend(fontsize=8)
ax.grid(axis='y', alpha=0.3)

# 1b) Bar: % windows above actual noise floor (−51.9 dB for Apr16, −70 dB for Apr6)
ax = axes[1]
bars, blabels, bcolors = [], [], []
noise_refs = {
    'APR16_baseline': -51.9, 'APR16_flight1': -51.9, 'APR16_flight2': -51.9,
    'APR16_intheair': -51.9,
    'APR6_cam1': -70.0, 'APR6_cam2': -70.0, 'APR6_cam3': -70.0,
}
for k in FILES:
    r = data[k]
    if r:
        pw = col(r, 'mean_power_db')
        floor = noise_refs[k]
        pct = 100 * np.mean(pw > floor)
        bars.append(pct)
        blabels.append(LABELS[k].replace(' ', '\n'))
        bcolors.append(COLORS[k])

x = np.arange(len(bars))
ax.bar(x, bars, color=bcolors, alpha=0.8, edgecolor='black', linewidth=0.5)
ax.set_xticks(x)
ax.set_xticklabels(blabels, fontsize=7)
ax.set_ylabel('Windows above noise floor (%)', fontsize=11)
ax.set_title('(b) % Windows Above Own Noise Floor', fontsize=11)
ax.grid(axis='y', alpha=0.3)
for xi, v in zip(x, bars):
    ax.text(xi, v + 0.5, f'{v:.1f}%', ha='center', va='bottom', fontsize=8)

# 1c) SNR margin mean with error bars
ax = axes[2]
snr_means, snr_stds, slabels, scolors = [], [], [], []
for k in FILES:
    r = data[k]
    if r:
        snr = col(r, 'snr_margin_db')
        valid = snr[np.isfinite(snr)]
        if len(valid):
            snr_means.append(float(np.mean(valid)))
            snr_stds.append(float(np.std(valid)))
            slabels.append(LABELS[k].replace(' ', '\n'))
            scolors.append(COLORS[k])

x2 = np.arange(len(snr_means))
bars2 = ax.bar(x2, snr_means, yerr=snr_stds, color=scolors, alpha=0.8,
               edgecolor='black', linewidth=0.5, capsize=5)
ax.axhline(0, color='black', linewidth=1.5, linestyle='--', label='SNR = 0 dB (at floor)')
ax.set_xticks(x2)
ax.set_xticklabels(slabels, fontsize=7)
ax.set_ylabel('SNR Margin (dB)', fontsize=11)
ax.set_title('(c) Mean SNR Margin (mean ± 1σ)', fontsize=11)
ax.legend(fontsize=8)
ax.grid(axis='y', alpha=0.3)

plt.tight_layout()
out = BASE / 'april16_fig1_power_comparison.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 2 — Time-series power for Apr16 flights
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 2: Apr16 Power Timeseries ...")

NOISE_FLOOR_APR16 = -51.9

fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=False)
fig.suptitle('April 16, 2026 — Received Power Over Time', fontsize=14, fontweight='bold')

for i, (k, ax) in enumerate(zip(['APR16_flight1', 'APR16_flight2', 'APR16_intheair'], axes)):
    r = data[k]
    if not r:
        ax.set_title(f'{LABELS[k]} — no data')
        continue
    pw   = col(r, 'mean_power_db')
    snr  = col(r, 'snr_margin_db')
    rel  = col(r, 'distance_reliable').astype(bool)
    t    = np.arange(len(pw)) * 10 / 1000.0   # seconds

    ax.fill_between(t, NOISE_FLOOR_APR16, pw,
                    where=(pw > NOISE_FLOOR_APR16),
                    alpha=0.35, color=COLORS[k], label='Above noise floor')
    ax.plot(t, pw, color=COLORS[k], linewidth=0.8, alpha=0.9)
    ax.axhline(NOISE_FLOOR_APR16, color='black', linestyle='--',
               linewidth=1.2, label=f'Noise floor ({NOISE_FLOOR_APR16} dB)')
    ax.axhline(-45.0, color='red', linestyle=':', linewidth=1.0, label='−45 dB gate')

    # Mark reliable windows
    if np.any(rel):
        ax.scatter(t[rel], pw[rel], s=15, color='gold', zorder=5,
                   label=f'distance_reliable=1 ({np.sum(rel)})')

    pct_above = 100 * np.mean(pw > NOISE_FLOOR_APR16)
    ax.set_title(f'{LABELS[k]}  |  {len(r)} windows ({len(r)*10/1000:.0f}s)  '
                 f'|  {pct_above:.1f}% above floor  '
                 f'|  max = {np.nanmax(pw):.1f} dB', fontsize=10)
    ax.set_ylabel('Power (dB)', fontsize=10)
    ax.set_ylim(-75, -15)
    ax.legend(fontsize=8, loc='lower right')
    ax.grid(alpha=0.25)
    ax.set_xlabel('Time (s)', fontsize=10)

plt.tight_layout()
out = BASE / 'april16_fig2_timeseries.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 3 — CDF: Power distribution all configs
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 3: Power CDF ...")

fig, axes = plt.subplots(1, 2, figsize=(14, 6))
fig.suptitle('Signal Power Cumulative Distribution', fontsize=14, fontweight='bold')

ax = axes[0]
for k in FILES:
    r = data[k]
    if r:
        pw = np.sort(col(r, 'mean_power_db'))
        cdf = np.arange(1, len(pw)+1) / len(pw)
        ax.plot(pw, cdf, color=COLORS[k], linewidth=1.8, alpha=0.85,
                linestyle='-' if 'APR16' in k else '--',
                label=LABELS[k])
ax.axvline(-51.9, color='black', linestyle='--', linewidth=1.5, label='Apr16 noise floor')
ax.axvline(-70.0, color='gray',  linestyle=':', linewidth=1.5, label='Apr6 noise floor')
ax.set_xlabel('Mean Window Power (dB)', fontsize=11)
ax.set_ylabel('CDF', fontsize=11)
ax.set_title('(a) CDF of Mean Window Power', fontsize=11)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

ax = axes[1]
# Power histogram at burst-level — max_power_db shows peak signals
for k in ['APR16_flight1', 'APR16_flight2', 'APR6_cam3']:
    r = data[k]
    if r:
        pw = col(r, 'max_power_db')
        ax.hist(pw, bins=60, alpha=0.55, color=COLORS[k], label=LABELS[k], density=True)
ax.axvline(-51.9, color='black', linestyle='--', linewidth=1.5, label='Apr16 noise floor')
ax.axvline(-70.0, color='gray',  linestyle=':', linewidth=1.5, label='Apr6 noise floor')
ax.set_xlabel('Max Window Power (dB)', fontsize=11)
ax.set_ylabel('Density', fontsize=11)
ax.set_title('(b) Peak Power Distribution Per Window', fontsize=11)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)

plt.tight_layout()
out = BASE / 'april16_fig3_power_cdf.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 4 — Fingerprint Analysis
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 4: Fingerprint Analysis ...")

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle('RF Fingerprint Analysis — April 16 vs April 6', fontsize=14, fontweight='bold')

# 4a) Mean fingerprint vectors
ax = axes[0, 0]
fingerprint_sets = {}
for k in ['APR16_baseline', 'APR16_flight1', 'APR16_flight2', 'APR6_cam3']:
    r = data[k]
    if r:
        fps = parse_fp(r)
        if fps is not None and len(fps) > 0:
            fingerprint_sets[k] = fps
            mean_fp = np.mean(fps, axis=0)
            ax.plot(mean_fp, color=COLORS[k], linewidth=1.8, alpha=0.85,
                    label=LABELS[k], linestyle='-' if 'APR16' in k else '--')

ax.set_xlabel('Fingerprint Bin (0–127)', fontsize=11)
ax.set_ylabel('Mean Amplitude', fontsize=11)
ax.set_title('(a) Mean Amplitude Fingerprint', fontsize=11)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)

# 4b) Fingerprint standard deviation (variability)
ax = axes[0, 1]
for k in fingerprint_sets:
    fps = fingerprint_sets[k]
    std_fp = np.std(fps, axis=0)
    ax.plot(std_fp, color=COLORS[k], linewidth=1.5, alpha=0.85,
            label=LABELS[k], linestyle='-' if 'APR16' in k else '--')
ax.set_xlabel('Fingerprint Bin (0–127)', fontsize=11)
ax.set_ylabel('Std Dev Amplitude', fontsize=11)
ax.set_title('(b) Fingerprint Variability (σ per bin)', fontsize=11)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)

# 4c) Cosine similarity heatmap between mean fingerprints
ax = axes[1, 0]
keys_with_fp = list(fingerprint_sets.keys())
n = len(keys_with_fp)
sim_matrix = np.zeros((n, n))
mean_fps = {k: np.mean(fingerprint_sets[k], axis=0) for k in keys_with_fp}
for i, ki in enumerate(keys_with_fp):
    for j, kj in enumerate(keys_with_fp):
        fi = mean_fps[ki]
        fj = mean_fps[kj]
        cos_sim = np.dot(fi, fj) / (np.linalg.norm(fi) * np.linalg.norm(fj) + 1e-20)
        sim_matrix[i, j] = cos_sim

short_labels = [LABELS[k].replace('Apr16 ', '').replace('Apr6 ', 'Apr6 ')
                for k in keys_with_fp]
im = ax.imshow(sim_matrix, cmap='RdYlGn', vmin=0, vmax=1, aspect='auto')
plt.colorbar(im, ax=ax, label='Cosine Similarity')
ax.set_xticks(range(n))
ax.set_yticks(range(n))
ax.set_xticklabels(short_labels, rotation=30, ha='right', fontsize=9)
ax.set_yticklabels(short_labels, fontsize=9)
for i in range(n):
    for j in range(n):
        ax.text(j, i, f'{sim_matrix[i,j]:.3f}', ha='center', va='center',
                fontsize=10, color='black')
ax.set_title('(c) Cosine Similarity Between Mean Fingerprints', fontsize=11)

# 4d) Rolling fp_cosine_sim over time for APR16 flights
ax = axes[1, 1]
for k in ['APR16_flight1', 'APR16_flight2', 'APR16_intheair']:
    r = data[k]
    if r:
        sim = col(r, 'fp_cosine_sim')
        t = np.arange(len(sim)) * 10 / 1000.0
        valid = np.isfinite(sim)
        ax.plot(t[valid], sim[valid], color=COLORS[k], linewidth=1.0, alpha=0.85,
                label=LABELS[k])
ax.axhline(0.95, color='black', linestyle='--', linewidth=1, label='High stability (0.95)')
ax.axhline(0.5,  color='gray',  linestyle=':', linewidth=1, label='Low stability (0.5)')
ax.set_xlabel('Time (s)', fontsize=11)
ax.set_ylabel('Fingerprint Cosine Similarity', fontsize=11)
ax.set_title('(d) Window-to-Window Fingerprint Stability', fontsize=11)
ax.legend(fontsize=9)
ax.set_ylim(-0.1, 1.05)
ax.grid(alpha=0.3)

plt.tight_layout()
out = BASE / 'april16_fig4_fingerprint.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 5 — Distance Metrics
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 5: Distance Metrics ...")

fig = plt.figure(figsize=(16, 12))
gs = gridspec.GridSpec(3, 2, figure=fig)
fig.suptitle('Distance Estimation Metrics — April 16 vs April 6', fontsize=14, fontweight='bold')

# 5a) Est distance raw — flight1
ax = fig.add_subplot(gs[0, 0])
r = data['APR16_flight1']
if r:
    d_raw  = col(r, 'est_distance_m_raw')
    d_filt = col(r, 'est_distance_m')
    conf   = col(r, 'distance_confidence')
    rel    = col(r, 'distance_reliable').astype(bool)
    t      = np.arange(len(r)) * 10 / 1000.0
    ax.fill_between(t, 0, 50, where=rel, alpha=0.15, color='green', label='distance_reliable=1')
    ax.plot(t, np.clip(d_raw, 0, 60),  color='lightblue', linewidth=0.7, alpha=0.7, label='Raw dist est.')
    ax.plot(t, np.clip(d_filt, 0, 60), color=COLORS['APR16_flight1'], linewidth=1.5, label='Filtered dist est.')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Distance (m)')
    ax.set_title(f'(a) Apr16 Flight-1 — Distance Estimate\n(reliable={np.sum(rel)} windows = {100*np.mean(rel):.1f}%)')
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_ylim(0, 60)

# 5b) Est distance raw — flight2
ax = fig.add_subplot(gs[0, 1])
r = data['APR16_flight2']
if r:
    d_raw  = col(r, 'est_distance_m_raw')
    d_filt = col(r, 'est_distance_m')
    rel    = col(r, 'distance_reliable').astype(bool)
    t      = np.arange(len(r)) * 10 / 1000.0
    ax.fill_between(t, 0, 50, where=rel, alpha=0.15, color='green', label='distance_reliable=1')
    ax.plot(t, np.clip(d_raw, 0, 60),  color='lightgreen', linewidth=0.7, alpha=0.7, label='Raw dist est.')
    ax.plot(t, np.clip(d_filt, 0, 60), color=COLORS['APR16_flight2'], linewidth=1.5, label='Filtered dist est.')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Distance (m)')
    ax.set_title(f'(b) Apr16 Flight-2 — Distance Estimate\n(reliable={np.sum(rel)} windows = {100*np.mean(rel):.1f}%)')
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_ylim(0, 60)

# 5c) SNR-gated relative distance (only windows where SNR > 0)
ax = fig.add_subplot(gs[1, 0])
for k in ['APR16_flight1', 'APR16_flight2']:
    r = data[k]
    if r:
        snr  = col(r, 'snr_margin_db')
        rdist = col(r, 'relative_distance')
        t    = np.arange(len(r)) * 10 / 1000.0
        mask = (snr > 0) & np.isfinite(rdist) & (rdist > 0) & (rdist < 10)
        if np.any(mask):
            ax.scatter(t[mask], rdist[mask], s=12, color=COLORS[k], alpha=0.6,
                       label=LABELS[k])
ax.axhline(1.0, color='black', linestyle='--', linewidth=1.2, label='No change (1.0)')
ax.set_xlabel('Time (s)')
ax.set_ylabel('Relative Distance Ratio')
ax.set_title('(c) Relative Distance (SNR > 0 windows only)')
ax.legend(fontsize=9)
ax.grid(alpha=0.3)
ax.set_ylim(0, 5)

# 5d) Path-loss trajectory — SNR > 0 windows, flight 1
ax = fig.add_subplot(gs[1, 1])
r = data['APR16_flight1']
if r:
    snr  = col(r, 'snr_margin_db')
    pl   = col(r, 'path_loss_db')
    t    = np.arange(len(r)) * 10 / 1000.0
    mask = (snr > 0) & np.isfinite(pl)
    ax.scatter(t[mask], pl[mask], s=20, color=COLORS['APR16_flight1'], alpha=0.7,
               label='Apr16 Flight-1 (SNR>0)')
    if np.sum(mask) > 5:
        z = np.polyfit(t[mask], pl[mask], 1)
        p = np.poly1d(z)
        ax.plot(t[mask], p(t[mask]), 'r--', linewidth=1.5,
                label=f'Trend: {z[0]:+.2f} dB/s')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Path Loss (dB)')
    ax.set_title('(d) Path Loss Over Time (Apr16 Flight-1, SNR>0 windows)')
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

# 5e) Confidence score timeline — compare all
ax = fig.add_subplot(gs[2, :])
for k in ['APR16_flight1', 'APR16_flight2', 'APR16_baseline', 'APR6_cam3']:
    r = data[k]
    if r:
        conf = col(r, 'distance_confidence')
        t    = np.arange(len(r)) * 10 / 1000.0
        ax.plot(t, conf, color=COLORS[k], linewidth=1.0, alpha=0.75, label=LABELS[k])
ax.axhline(0.58, color='red', linestyle='--', linewidth=1.5, label='min_conf=0.58 gate')
ax.axhline(0.35, color='orange', linestyle=':', linewidth=1.2, label='old min_conf=0.35')
ax.set_xlabel('Time (s)')
ax.set_ylabel('Distance Confidence Score')
ax.set_title('(e) Distance Confidence Score Timeline')
ax.legend(fontsize=8, ncol=3)
ax.grid(alpha=0.3)
ax.set_ylim(0, 1.05)

plt.tight_layout()
out = BASE / 'april16_fig5_distance_metrics.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 6 — Amplitude histogram comparison (4-panel)
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 6: Amplitude Histogram / Feature Comparison ...")

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle('RF Feature Comparison — April 16 vs April 6', fontsize=14, fontweight='bold')

# 6a) amp_mean comparison
ax = axes[0, 0]
for k in FILES:
    r = data[k]
    if r:
        amp = col(r, 'amp_mean')
        ax.hist(amp, bins=60, alpha=0.5, color=COLORS[k], density=True,
                label=LABELS[k], linestyle='-' if 'APR16' in k else '--')
ax.set_xlabel('Amplitude Mean', fontsize=11)
ax.set_ylabel('Density', fontsize=11)
ax.set_title('(a) Amplitude Mean Distribution', fontsize=11)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

# 6b) amp_crest_factor
ax = axes[0, 1]
for k in FILES:
    r = data[k]
    if r:
        cf = col(r, 'amp_crest_factor')
        valid = cf[np.isfinite(cf) & (cf < 200)]
        ax.hist(valid, bins=60, alpha=0.5, color=COLORS[k], density=True,
                label=LABELS[k])
ax.set_xlabel('Crest Factor (peak/RMS)', fontsize=11)
ax.set_ylabel('Density', fontsize=11)
ax.set_title('(b) Amplitude Crest Factor (burstiness)', fontsize=11)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

# 6c) phase_entropy
ax = axes[1, 0]
for k in FILES:
    r = data[k]
    if r:
        pe = col(r, 'phase_entropy')
        valid = pe[np.isfinite(pe)]
        ax.hist(valid, bins=40, alpha=0.5, color=COLORS[k], density=True,
                label=LABELS[k])
ax.set_xlabel('Phase Entropy (bits)', fontsize=11)
ax.set_ylabel('Density', fontsize=11)
ax.set_title('(c) Phase Entropy Distribution', fontsize=11)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

# 6d) phase_concentration
ax = axes[1, 1]
for k in FILES:
    r = data[k]
    if r:
        pc = col(r, 'phase_concentration')
        valid = pc[np.isfinite(pc)]
        ax.hist(valid, bins=40, alpha=0.5, color=COLORS[k], density=True,
                label=LABELS[k])
ax.set_xlabel('Phase Concentration (0=random, 1=coherent)', fontsize=11)
ax.set_ylabel('Density', fontsize=11)
ax.set_title('(d) Phase Concentration (OcuSync carrier lock)', fontsize=11)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

plt.tight_layout()
out = BASE / 'april16_fig6_features.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Figure 7 — Summary comparison table figure
# ═══════════════════════════════════════════════════════════════════════════
print("Generating Figure 7: Summary Stats Table ...")

fig, ax = plt.subplots(figsize=(16, 5))
fig.suptitle('Statistical Summary — All Real Flight Configurations', fontsize=13, fontweight='bold')

table_rows = []
headers = ['Config', 'Windows', 'Mean Pwr (dB)', 'Max Pwr (dB)',
           'Actual SNR (dB)', '> floor (%)', 'Reliable (%)', 'Conf mean']

for k in FILES:
    r = data[k]
    if r:
        pw   = col(r, 'mean_power_db')
        mpw  = col(r, 'max_power_db')
        snr  = col(r, 'snr_margin_db')
        rel  = col(r, 'distance_reliable')
        conf = col(r, 'distance_confidence')
        floor = noise_refs[k]
        actual_snr = float(np.nanmean(pw)) - floor
        table_rows.append([
            LABELS[k][:28],
            str(len(r)),
            f'{np.nanmean(pw):.1f}',
            f'{np.nanmax(mpw):.1f}',
            f'{actual_snr:+.1f}',
            f'{100*np.mean(pw > floor):.1f}%',
            f'{100*np.nanmean(rel):.1f}%',
            f'{np.nanmean(conf):.3f}',
        ])

ax.axis('off')
tbl = ax.table(cellText=table_rows, colLabels=headers, loc='center', cellLoc='center')
tbl.auto_set_font_size(False)
tbl.set_fontsize(9.5)
tbl.scale(1.0, 2.0)

# Color header row
for j in range(len(headers)):
    tbl[(0, j)].set_facecolor('#2c3e50')
    tbl[(0, j)].set_text_props(color='white', fontweight='bold')

# Color rows by config type
for i, k in enumerate(k for k in FILES if data[k]):
    for j in range(len(headers)):
        if 'APR16' in k and 'baseline' not in k:
            tbl[(i+1, j)].set_facecolor('#d5e8d4')  # green for Apr16
        elif 'APR6' in k:
            tbl[(i+1, j)].set_facecolor('#ffe6cc')  # orange for Apr6

plt.tight_layout()
out = BASE / 'april16_fig7_summary_table.png'
plt.savefig(out, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Print per-file stats to console for thesis
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("STATISTICAL SUMMARY")
print("="*70)
for k in FILES:
    r = data[k]
    if not r:
        print(f"{k}: no data")
        continue
    pw   = col(r, 'mean_power_db')
    mpw  = col(r, 'max_power_db')
    snr  = col(r, 'snr_margin_db')
    rel  = col(r, 'distance_reliable')
    conf = col(r, 'distance_confidence')
    ar   = col(r, 'active_ratio')
    floor = noise_refs[k]
    actual_snr = float(np.nanmean(pw)) - floor
    print(f"\n{LABELS[k]}")
    print(f"  Windows: {len(r)} ({len(r)*10/1000:.0f}s)   Gain/Rate: {'70dB/2MHz' if 'APR16' in k else '40dB/10MHz'}")
    print(f"  mean_pwr: {np.nanmean(pw):.2f} dB  |  max_pwr: {np.nanmax(mpw):.1f} dB  |  std: {np.nanstd(pw):.2f} dB")
    print(f"  actual SNR above noise floor: {actual_snr:+.2f} dB")
    print(f"  > noise floor: {100*np.mean(pw > floor):.1f}%   |  reliable: {100*np.nanmean(rel):.1f}%   |  conf: {np.nanmean(conf):.3f}")
    print(f"  snr_margin mean: {np.nanmean(snr):+.2f} dB   |  active_ratio mean: {np.nanmean(ar):.4f}")

print("\nDone. All figures saved.")
