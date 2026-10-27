import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from sklearn.metrics.pairwise import cosine_similarity

# Paths
BASE = Path("/root/projects/ReplayAttack/iq_datasets")
OUT = BASE / "camera_detailed_analysis"
OUT.mkdir(exist_ok=True)

MAIN_FLIGHT = BASE / "real_flight_camera_10ms_20260406_114320.csv"
BASELINES = {
    "No Drone (Noise)": BASE / "lab_20260303_stationary/no_drone_10ms_20260303_121416.csv",
    "Idle Drone (Stationary)": BASE / "lab_20260303_stationary/no_camera_movement_10ms_20260303_122213.csv"
}

def parse_fp(s):
    try:
        if pd.isna(s): return None
        return np.array([float(x) for x in s.split(',')])
    except: return None

def smooth(series, w=20):
    if w > 1:
        return series.rolling(w, center=True).min().fillna(method='bfill').fillna(method='ffill') if 'power' in series.name else series.rolling(w, center=True).mean().fillna(method='bfill').fillna(method='ffill')
    return series

def load_data(path):
    df = pd.read_csv(path)
    df['amp_fingerprint_arr'] = df['amp_fingerprint'].apply(parse_fp)
    if 'phase_fingerprint' in df.columns:
        df['phase_fingerprint_arr'] = df['phase_fingerprint'].apply(parse_fp)
    return df

print("Loading data...")
df_main = load_data(MAIN_FLIGHT)
baseline_dfs = {k: load_data(v) for k, v in BASELINES.items() if v.exists()}

# ---------------------------------------------------------
# Part 1: Power vs Distance Correlation with Aligned Flat
# ---------------------------------------------------------
print("Generating Power-Distance Correlation Plot...")
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

power_raw = df_main['mean_power_db'].values
dist_raw = df_main['relative_distance'].values if 'relative_distance' in df_main.columns else df_main['amp_mean'].values * -1 # fallback

# Compute smooth
w = 20
power_sm = pd.Series(power_raw).rolling(w, center=True).mean().fillna(method='bfill').fillna(method='ffill').values
dist_sm = pd.Series(dist_raw).rolling(w, center=True).mean().fillna(method='bfill').fillna(method='ffill').values

# Find the 'flat' point: where distance reaches 95% of its max value for the first time
d_max = np.percentile(dist_sm[~np.isnan(dist_sm)], 98)
d_min = np.percentile(dist_sm[~np.isnan(dist_sm)], 2)
threshold = d_min + 0.9 * (d_max - d_min)

flat_idx = np.where(dist_sm > threshold)[0]
if len(flat_idx) > 0:
    flat_idx = flat_idx[0]
else:
    flat_idx = len(dist_sm) // 2

ax1.plot(power_raw, color='steelblue', alpha=0.3, lw=0.5, label='Power raw')
ax1.plot(power_sm, color='navy', lw=2, label='Power smoothed')
ax1.axvline(flat_idx, color='navy', linestyle='--', lw=1.5, label=f'Power flat @ {flat_idx}')
ax1.set_ylabel('Power (dB)')
ax1.legend(loc='upper right')
ax1.grid(True, alpha=0.3)

ax2.plot(dist_raw, color='lightcoral', alpha=0.3, lw=0.5, label='Distance raw')
ax2.plot(dist_sm, color='darkred', lw=2, label='Distance smoothed')
ax2.axvline(flat_idx, color='darkred', linestyle='--', lw=1.5, label=f'Distance flat (aligned) @ {flat_idx}')
ax2.set_ylabel('Distance (m)' if 'relative_distance' in df_main.columns else 'Distance proxy')
ax2.set_xlabel('Window Index')
ax2.legend(loc='lower right')
ax2.grid(True, alpha=0.3)

plt.suptitle('Power-Distance Correlation with Aligned Flat Point (Camera Flight)', fontsize=14)
plt.tight_layout()
plt.savefig(OUT / "power_distance_correlation_plot_aligned.png")
plt.close()


# ---------------------------------------------------------
# Part 2: Comprehensive Fingerprint Analysis
# ---------------------------------------------------------
print("Generating Fingerprint Analysis Plot...")
fig = plt.figure(figsize=(20, 24))
gs = fig.add_gridspec(5, 3)

datasets = {"Camera Flight": df_main}
datasets.update(baseline_dfs)

colors = {'Camera Flight': 'coral', 'No Drone (Noise)': 'steelblue', 'Idle Drone (Stationary)': 'mediumseagreen'}
titles = list(datasets.keys())

# Row 1: Random Amp FP
for i, title in enumerate(titles):
    ax = fig.add_subplot(gs[0, i])
    df = datasets[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    if len(arrs) > 0:
        valid_idx = np.random.choice(len(arrs), min(5, len(arrs)), replace=False)
        for v in valid_idx:
            ax.plot(arrs[v], lw=0.8, alpha=0.7, label=f'W{v}')
    ax.set_title(f'{title}\nAmplitude Fingerprints (random windows)', fontweight='bold')
    ax.set_xlabel('Bin (0-127)')
    ax.set_ylabel('Amplitude')
    ax.legend(fontsize=6, ncol=2)
    ax.grid(alpha=0.3)

# Row 2: Amp FP Heatmap (first 120)
for i, title in enumerate(titles):
    ax = fig.add_subplot(gs[1, i])
    df = datasets[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    if len(arrs) > 0:
        mat = np.stack(arrs[:120])
        im = ax.imshow(mat, aspect='auto', cmap='viridis')
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(f'{title}\nAmplitude FP Heatmap (first 120 windows)', fontweight='bold')
    ax.set_xlabel('Bin')
    ax.set_ylabel('Window #')

# Row 3: Cosine Similarity
ax_cos = fig.add_subplot(gs[2, :])
for title in titles:
    df = datasets[title]
    arrs = df['amp_fingerprint_arr'].dropna().values
    if len(arrs) > 1:
        mat = np.stack(arrs)
        # Cosine sim of consecutive windows
        sims = [cosine_similarity([mat[i]], [mat[i+1]])[0][0] for i in range(len(mat)-1)]
        mean_sim = np.mean(sims)
        std_sim = np.std(sims)
        ax_cos.plot(sims[:500], lw=1, alpha=0.8, color=colors.get(title, 'k'), 
                    label=f'{title} (mean={mean_sim:.4f}, std={std_sim:.4f})')
ax_cos.set_title('Fingerprint Stability: Consecutive Window Cosine Similarity', fontweight='bold')
ax_cos.set_xlabel('Window pair #')
ax_cos.set_ylabel('Cosine Similarity')
ax_cos.set_ylim(0.85, 1.01)
ax_cos.grid(alpha=0.3)
ax_cos.legend(loc='center right')

# Row 4: Phase FP Heatmap (first 120)
for i, title in enumerate(titles):
    ax = fig.add_subplot(gs[3, i])
    df = datasets[title]
    if 'phase_fingerprint_arr' in df.columns:
        arrs = df['phase_fingerprint_arr'].dropna().values
        if len(arrs) > 0:
            mat = np.stack(arrs[:120])
            im = ax.imshow(mat, aspect='auto', cmap='coolwarm')
            plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(f'{title}\nPhase FP Heatmap (first 120 windows)', fontweight='bold')
    ax.set_xlabel('Bin')
    ax.set_ylabel('Window #')

# Row 5: Normalized feature comparison and amp timeline
ax_bar = fig.add_subplot(gs[4, :2])
features = ['active_ratio', 'amp_mean', 'amp_std', 'power_std_db']
norm_vals = {t: [] for t in titles}
for feat in features:
    max_val = max([datasets[t][feat].mean() for t in titles if feat in datasets[t].columns])
    for t in titles:
        if feat in datasets[t].columns and max_val > 0:
            norm_vals[t].append(datasets[t][feat].mean() / max_val)
        else:
            norm_vals[t].append(0)

x = np.arange(len(features))
width = 0.25
for i, title in enumerate(titles):
    ax_bar.bar(x + i*width - width, norm_vals[title], width, label=title, color=colors.get(title, 'k'), alpha=0.8)
ax_bar.set_xticks(x)
ax_bar.set_xticklabels(features)
ax_bar.set_title('Normalized Feature Comparison', fontweight='bold')
ax_bar.set_ylabel('Normalized mean value')
ax_bar.grid(alpha=0.3)
ax_bar.legend()

ax_timeline = fig.add_subplot(gs[4, 2])
for title in titles:
    df = datasets[title]
    if 'amp_mean' in df.columns:
        ax_timeline.plot(df['amp_mean'].values[:600], lw=0.8, alpha=0.7, color=colors.get(title), label=title)
ax_timeline.set_title('amp_mean Timeline (first 600 windows)', fontweight='bold')
ax_timeline.set_xlabel('Window #')
ax_timeline.set_ylabel('amp_mean')
ax_timeline.grid(alpha=0.3)
ax_timeline.legend(fontsize=6)

plt.tight_layout()
plt.savefig(OUT / "fingerprint_analysis_main_flight_vs_baselines.png")
plt.close()

print("Analyses complete! Images saved in:", OUT)
