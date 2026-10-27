#!/usr/bin/env python3
"""
Same fingerprint analysis layout as detailed_analysis_camera.py
applied to April 16, 2026 improved-parameter flights.

Columns: Apr16 Flight-1 | Apr16 No-Drone Baseline | Apr16 In-the-Air
Extra panel: Apr16 Flight-2 added to the cosine stability row for comparison.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from sklearn.metrics.pairwise import cosine_similarity

BASE = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT  = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

PATHS = {
    "Apr16 Flight-1 (~50m)":  BASE / "real_flight_10ms_20260416_110239.csv",
    "No Drone (Baseline)":    BASE / "no_drone_baseline_10ms_20260416_105448.csv",
    "Apr16 In-the-Air":       BASE / "intheair_10ms_20260416_111024.csv",
}
EXTRA = {
    "Apr16 Flight-2 (~50m)":  BASE / "real_flight_10ms_20260416_111305.csv",
}

COLORS = {
    "Apr16 Flight-1 (~50m)": "#1f77b4",
    "No Drone (Baseline)":   "#888888",
    "Apr16 In-the-Air":      "#9467bd",
    "Apr16 Flight-2 (~50m)": "#2ca02c",
}

def parse_fp(s):
    try:
        if pd.isna(s):
            return None
        return np.array([float(x) for x in s.split(',')])
    except Exception:
        return None

def load(path):
    df = pd.read_csv(path)
    df['amp_fingerprint_arr']   = df['amp_fingerprint'].apply(parse_fp)
    df['phase_fingerprint_arr'] = df['phase_fingerprint'].apply(parse_fp)
    return df

print("Loading data ...")
datasets = {}
for title, path in PATHS.items():
    if path.exists():
        datasets[title] = load(path)
        print(f"  {title}: {len(datasets[title])} windows")
    else:
        print(f"  MISSING: {path}")

extras = {}
for title, path in EXTRA.items():
    if path.exists():
        extras[title] = load(path)
        print(f"  {title}: {len(extras[title])} windows")

titles = list(datasets.keys())
all_titles_for_cosine = list(datasets.keys()) + list(extras.keys())

# ─────────────────────────────────────────────────────────────────────────────
# Main figure: same 5-row layout as detailed_analysis_camera.py
# ─────────────────────────────────────────────────────────────────────────────
print("Building fingerprint analysis figure ...")
fig = plt.figure(figsize=(20, 26))
gs  = fig.add_gridspec(5, 3, hspace=0.45, wspace=0.3)

# ── Row 1: Random amplitude fingerprints ─────────────────────────────────────
for i, title in enumerate(titles):
    ax  = fig.add_subplot(gs[0, i])
    df  = datasets[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    if len(arrs) > 0:
        rng  = np.random.default_rng(42)
        idx  = rng.choice(len(arrs), min(6, len(arrs)), replace=False)
        for v in idx:
            ax.plot(arrs[v], lw=0.9, alpha=0.75, label=f'W{v}')
        # also draw the mean
        mean_fp = np.mean(np.stack(arrs), axis=0)
        ax.plot(mean_fp, lw=1.8, color='black', linestyle='--', alpha=0.6, label='mean')
    ax.set_title(f'{title}\nAmplitude Fingerprints (random windows)', fontweight='bold', fontsize=10)
    ax.set_xlabel('Bin (0–127)')
    ax.set_ylabel('Amplitude')
    ax.legend(fontsize=6, ncol=2)
    ax.grid(alpha=0.3)

# ── Row 2: Amplitude FP Heatmap (first 120 windows) ──────────────────────────
for i, title in enumerate(titles):
    ax   = fig.add_subplot(gs[1, i])
    df   = datasets[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    n_show = min(120, len(arrs))
    if n_show > 0:
        mat = np.stack(arrs[:n_show])
        im  = ax.imshow(mat, aspect='auto', cmap='viridis',
                        vmin=0, vmax=np.percentile(mat, 99))
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(f'{title}\nAmplitude FP Heatmap (first {n_show} windows)',
                 fontweight='bold', fontsize=10)
    ax.set_xlabel('Bin')
    ax.set_ylabel('Window #')

# ── Row 3: Cosine similarity — consecutive windows (full width) ───────────────
ax_cos = fig.add_subplot(gs[2, :])
all_cos_titles = all_titles_for_cosine
all_cos_dfs    = {**datasets, **extras}
for title in all_cos_titles:
    df   = all_cos_dfs[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    if len(arrs) > 1:
        mat  = np.stack(arrs)
        sims = [cosine_similarity([mat[j]], [mat[j+1]])[0][0]
                for j in range(len(mat)-1)]
        mean_s = np.mean(sims)
        std_s  = np.std(sims)
        ax_cos.plot(sims[:600], lw=1.0, alpha=0.85,
                    color=COLORS.get(title, 'k'),
                    label=f'{title}  (mean={mean_s:.4f}, std={std_s:.4f})')

ax_cos.set_title('Fingerprint Stability: Consecutive Window Cosine Similarity',
                 fontweight='bold', fontsize=11)
ax_cos.set_xlabel('Window pair #')
ax_cos.set_ylabel('Cosine Similarity')
ax_cos.set_ylim(0.85, 1.01)
ax_cos.axhline(0.99, color='black', linestyle=':', lw=1, alpha=0.5, label='0.99 reference')
ax_cos.grid(alpha=0.3)
ax_cos.legend(loc='lower right', fontsize=9)

# ── Row 4: Phase FP Heatmap (first 120 windows) ──────────────────────────────
for i, title in enumerate(titles):
    ax   = fig.add_subplot(gs[3, i])
    df   = datasets[title]
    arrs = df['phase_fingerprint_arr'].dropna().values
    n_show = min(120, len(arrs))
    if n_show > 0:
        mat = np.stack(arrs[:n_show])
        im  = ax.imshow(mat, aspect='auto', cmap='coolwarm')
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(f'{title}\nPhase FP Heatmap (first {n_show} windows)',
                 fontweight='bold', fontsize=10)
    ax.set_xlabel('Bin')
    ax.set_ylabel('Window #')

# ── Row 5: Normalized feature bar + amp_mean timeline ────────────────────────
ax_bar = fig.add_subplot(gs[4, :2])
features    = ['active_ratio', 'amp_mean', 'amp_std', 'power_std_db']
feat_labels = ['active_ratio', 'amp_mean', 'amp_std', 'power_std_db']
norm_vals   = {t: [] for t in titles}

for feat in features:
    max_val = max(
        (datasets[t][feat].mean() for t in titles if feat in datasets[t].columns),
        default=1.0
    )
    max_val = max_val if max_val > 0 else 1.0
    for t in titles:
        if feat in datasets[t].columns:
            norm_vals[t].append(datasets[t][feat].mean() / max_val)
        else:
            norm_vals[t].append(0)

x     = np.arange(len(features))
width = 0.25
for i, title in enumerate(titles):
    offset = (i - 1) * width
    ax_bar.bar(x + offset, norm_vals[title], width,
               label=title, color=COLORS.get(title, 'k'), alpha=0.85, edgecolor='black', lw=0.4)

ax_bar.set_xticks(x)
ax_bar.set_xticklabels(feat_labels, fontsize=10)
ax_bar.set_title('Normalized Feature Comparison', fontweight='bold', fontsize=11)
ax_bar.set_ylabel('Normalized mean value (relative to max)')
ax_bar.grid(alpha=0.3, axis='y')
ax_bar.legend(fontsize=9)

ax_tl = fig.add_subplot(gs[4, 2])
for title in all_cos_titles:
    df = all_cos_dfs[title]
    if 'amp_mean' in df.columns:
        ax_tl.plot(df['amp_mean'].values[:600], lw=0.9, alpha=0.8,
                   color=COLORS.get(title, 'k'), label=title)
ax_tl.set_title('amp_mean Timeline (first 600 windows)', fontweight='bold', fontsize=10)
ax_tl.set_xlabel('Window #')
ax_tl.set_ylabel('amp_mean')
ax_tl.grid(alpha=0.3)
ax_tl.legend(fontsize=7)

fig.suptitle('RF Fingerprint Analysis — April 16, 2026 Improved-Parameter Flights\n'
             '(gain=70 dB, rate=2 MHz, noise-floor=−45 dB)',
             fontsize=13, fontweight='bold', y=0.995)

out_path = OUT / "fingerprint_analysis_april16_vs_baseline.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"  → {out_path}")
print("Done.")
