#!/usr/bin/env python3
"""
Comprehensive analysis of real flight IQ datasets (with camera, April 6 2026).
Compares against stationary lab tests and previous no-camera flight.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# ── Configuration ──────────────────────────────────────────────────────────
BASE = Path("/root/projects/ReplayAttack/iq_datasets")
OUT = BASE / "camera_flight_analysis"
OUT.mkdir(exist_ok=True)

FLIGHTS = {
    "Flight1_11:43": BASE / "real_flight_camera_10ms_20260406_114320.csv",
    "Flight2_11:46": BASE / "real_flight_camera_10ms_20260406_114626.csv",
    "Flight3_11:48": BASE / "real_flight_camera_10ms_20260406_114837.csv",
    "TakeOff_12:11": BASE / "take_off_camera_10ms_20260406_121142.csv",
}

STATIONARY_DRONE = BASE / "lab_20260303_stationary/with_camera_movement_10ms_20260303_122657.csv"
STATIONARY_NO_CAM = BASE / "lab_20260303_stationary/no_camera_movement_10ms_20260303_122213.csv"
NO_DRONE = BASE / "lab_20260303_stationary/no_drone_10ms_20260303_121416.csv"
PREV_FLIGHT = BASE / "Real Flight/fly_away_nocamera_10.0ms_20260326_105417.csv"

plt.rcParams.update({'font.size': 10, 'figure.dpi': 150, 'savefig.bbox': 'tight'})

# Helper: always use numpy arrays for matplotlib
def V(s):
    return s.values if hasattr(s, 'values') else s

def smooth(series, w):
    if w > 1:
        return series.rolling(w, center=True).mean()
    return series

# ── Load Data ──────────────────────────────────────────────────────────────
print("=" * 70)
print("LOADING DATASETS")
print("=" * 70)

flights = {}
for name, path in FLIGHTS.items():
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['elapsed_s'] = (df['timestamp'] - df['timestamp'].iloc[0]).dt.total_seconds()
    flights[name] = df
    print(f"  {name}: {len(df)} rows, {df['elapsed_s'].iloc[-1]:.1f}s duration")

comparisons = {}
for label, path in [("Stationary+Cam", STATIONARY_DRONE),
                     ("Stationary-NoCam", STATIONARY_NO_CAM),
                     ("NoDrone", NO_DRONE),
                     ("PrevFlight-NoCam", PREV_FLIGHT)]:
    if path.exists():
        df = pd.read_csv(path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['elapsed_s'] = (df['timestamp'] - df['timestamp'].iloc[0]).dt.total_seconds()
        comparisons[label] = df
        print(f"  {label}: {len(df)} rows")

# ── 1. DATA QUALITY ───────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("1. DATA QUALITY ASSESSMENT")
print("=" * 70)

quality_report = []
for name, df in flights.items():
    t = df['elapsed_s'].diff().dropna()
    key_cols = ['mean_power_db', 'amp_mean', 'phase_mean', 'phase_drift', 'amp_std', 'bandwidth_hz', 'total_energy']
    nans = sum(df[c].isna().sum() for c in key_cols if c in df.columns)
    active_pct = (df['active_ratio'] > 0).mean() * 100
    r = {
        'Dataset': name, 'Rows': len(df),
        'Duration_s': round(df['elapsed_s'].iloc[-1], 1),
        'Mean_Interval_ms': round(t.mean() * 1000, 1),
        'Max_Gap_ms': round(t.max() * 1000, 1),
        'NaN_Count': nans,
        'Power_Range_dB': round(df['mean_power_db'].max() - df['mean_power_db'].min(), 2),
        'Active%': round(active_pct, 1),
    }
    quality_report.append(r)
    print(f"\n  {name}: {r['Rows']} rows, {r['Duration_s']}s, interval={r['Mean_Interval_ms']}ms, active={r['Active%']}%, range={r['Power_Range_dB']}dB")

pd.DataFrame(quality_report).to_csv(OUT / "01_data_quality.csv", index=False)

# ── 2. SIGNAL POWER OVER TIME ─────────────────────────────────────────────
print("\n" + "=" * 70)
print("2. SIGNAL POWER ANALYSIS")
print("=" * 70)

fig, axes = plt.subplots(len(flights), 1, figsize=(16, 4 * len(flights)))
for ax, (name, df) in zip(axes, flights.items()):
    w = min(20, len(df) // 5)
    x = V(df['elapsed_s'])
    ax.plot(x, V(df['mean_power_db']), alpha=0.3, color='steelblue', lw=0.5, label='Raw')
    ax.plot(x, V(smooth(df['mean_power_db'], w)), color='navy', lw=1.5, label=f'Smoothed (w={w})')
    m = df['active_ratio'] > 0
    if m.any():
        ax.scatter(x[m], V(df['mean_power_db'])[m], c='red', s=8, zorder=5, label=f'Active ({m.sum()})')
    ax.set_ylabel('Mean Power (dB)')
    ax.set_title(f'{name} — Power over Time', fontweight='bold')
    ax.legend(loc='upper right', fontsize=8); ax.grid(True, alpha=0.3)
axes[-1].set_xlabel('Elapsed Time (s)')
plt.tight_layout(); plt.savefig(OUT / "02_power_over_time.png"); plt.close()
print("  Saved: 02_power_over_time.png")

# ── 3. AMPLITUDE ANALYSIS ─────────────────────────────────────────────────
print("\n" + "=" * 70)
print("3. AMPLITUDE ANALYSIS (Distance Proxy)")
print("=" * 70)

fig, axes = plt.subplots(2, 2, figsize=(16, 12))
for idx, (name, df) in enumerate(flights.items()):
    ax = axes[idx // 2][idx % 2]
    w = min(20, len(df) // 5)
    x = V(df['elapsed_s'])
    am = V(smooth(df['amp_mean'], w)) * 1000
    astd = V(smooth(df['amp_std'], w)) * 1000
    ax.plot(x, am, color='teal', lw=1.2, label='Amp Mean (×1000)')
    ax.fill_between(x, am - astd, am + astd, alpha=0.2, color='teal')
    ax.set_xlabel('Time (s)'); ax.set_ylabel('Amp Mean ± Std (×1000)')
    ax.set_title(f'{name}', fontweight='bold'); ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
plt.suptitle('Amplitude Analysis — Higher = Closer Drone', fontsize=14, fontweight='bold')
plt.tight_layout(); plt.savefig(OUT / "03_amplitude_analysis.png"); plt.close()
print("  Saved: 03_amplitude_analysis.png")

# ── 4. PHASE DRIFT ANALYSIS ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("4. PHASE DRIFT ANALYSIS (Movement Indicator)")
print("=" * 70)

fig, axes = plt.subplots(len(flights), 1, figsize=(16, 4 * len(flights)))
for ax, (name, df) in zip(axes, flights.items()):
    w = min(20, len(df) // 5)
    x = V(df['elapsed_s'])
    ds = V(smooth(df['phase_drift'], w))
    ax.plot(x, V(df['phase_drift']), alpha=0.25, color='purple', lw=0.5)
    ax.plot(x, ds, color='darkviolet', lw=1.5, label='Phase Drift (smoothed)')
    ax.axhline(0, color='gray', ls='--', alpha=0.5)
    ax.set_ylabel('Phase Drift')
    ax.set_title(f'{name} — Phase Drift (Radial Movement)', fontweight='bold')
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
axes[-1].set_xlabel('Time (s)')
plt.tight_layout(); plt.savefig(OUT / "04_phase_drift.png"); plt.close()
print("  Saved: 04_phase_drift.png")

# ── 5. BANDWIDTH & ENERGY ────────────────────────────────────────────────
print("\n" + "=" * 70)
print("5. BANDWIDTH & ENERGY ANALYSIS")
print("=" * 70)

fig, axes = plt.subplots(2, 2, figsize=(16, 12))
for idx, (name, df) in enumerate(flights.items()):
    ax = axes[idx // 2][idx % 2]
    w = min(20, len(df) // 5)
    x = V(df['elapsed_s'])
    ax.plot(x, V(smooth(df['bandwidth_hz'], w)), color='forestgreen', lw=1, label='Bandwidth (Hz)')
    ax2 = ax.twinx()
    ax2.plot(x, V(smooth(df['total_energy'], w)) * 1e6, color='crimson', alpha=0.7, lw=1, label='Energy (×1e6)')
    ax2.set_ylabel('Energy (×1e6)', color='crimson')
    ax.set_xlabel('Time (s)'); ax.set_ylabel('Bandwidth (Hz)', color='forestgreen')
    ax.set_title(f'{name}', fontweight='bold'); ax.legend(loc='upper left', fontsize=8)
    ax2.legend(loc='upper right', fontsize=8); ax.grid(True, alpha=0.3)
plt.suptitle('Bandwidth & Energy Over Time', fontsize=14, fontweight='bold')
plt.tight_layout(); plt.savefig(OUT / "05_bandwidth_energy.png"); plt.close()
print("  Saved: 05_bandwidth_energy.png")

# ── 6. FINGERPRINT ANALYSIS ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("6. FINGERPRINT ANALYSIS")
print("=" * 70)

def parse_fp(s):
    try:
        if pd.isna(s): return None
        return np.array([float(x) for x in s.split(',')])
    except: return None

def mean_fp(df, col='amp_fingerprint'):
    fps = df[col].apply(parse_fp).dropna()
    if len(fps) == 0: return None
    return np.array(fps.tolist()).mean(axis=0)

fp_results = {}
for name, df in flights.items():
    fp = mean_fp(df, 'amp_fingerprint')
    if fp is not None: fp_results[name] = fp
for name, df in comparisons.items():
    if 'amp_fingerprint' in df.columns:
        fp = mean_fp(df, 'amp_fingerprint')
        if fp is not None: fp_results[name] = fp

if fp_results:
    fig, axes = plt.subplots(2, 1, figsize=(16, 12))
    ax = axes[0]
    for name in flights.keys():
        if name in fp_results:
            ax.plot(fp_results[name] * 1000, lw=1.5, label=f'Camera: {name}')
    for name in comparisons.keys():
        if name in fp_results:
            ax.plot(fp_results[name] * 1000, lw=1.5, ls='--', label=name)
    ax.set_xlabel('Frequency Bin'); ax.set_ylabel('Mean Amplitude (×1000)')
    ax.set_title('Amplitude Fingerprint Comparison', fontweight='bold')
    ax.legend(fontsize=8, ncol=2); ax.grid(True, alpha=0.3)

    ax = axes[1]
    all_names = list(fp_results.keys()); n = len(all_names)
    sim_matrix = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            a, b = fp_results[all_names[i]], fp_results[all_names[j]]
            sim_matrix[i, j] = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)) if len(a) == len(b) else np.nan
    im = ax.imshow(sim_matrix, cmap='RdYlGn', vmin=0.95, vmax=1.0, aspect='auto')
    ax.set_xticks(range(n)); ax.set_yticks(range(n))
    ax.set_xticklabels(all_names, rotation=45, ha='right', fontsize=8)
    ax.set_yticklabels(all_names, fontsize=8)
    for i in range(n):
        for j in range(n):
            if not np.isnan(sim_matrix[i, j]):
                ax.text(j, i, f'{sim_matrix[i,j]:.4f}', ha='center', va='center', fontsize=7)
    plt.colorbar(im, ax=ax, label='Cosine Similarity')
    ax.set_title('Fingerprint Cosine Similarity Matrix', fontweight='bold')
    plt.tight_layout(); plt.savefig(OUT / "06_fingerprint_analysis.png"); plt.close()
    pd.DataFrame(sim_matrix, index=all_names, columns=all_names).to_csv(OUT / "06_fingerprint_similarity.csv")
    print("  Saved: 06_fingerprint_analysis.png + csv")

# ── 7. PHASE FINGERPRINT ─────────────────────────────────────────────────
print("\n" + "=" * 70)
print("7. PHASE FINGERPRINT ANALYSIS")
print("=" * 70)

phase_fp = {}
for name, df in {**flights, **comparisons}.items():
    if 'phase_fingerprint' in df.columns:
        fp = mean_fp(df, 'phase_fingerprint')
        if fp is not None: phase_fp[name] = fp

if phase_fp:
    fig, axes = plt.subplots(2, 1, figsize=(16, 12))
    ax = axes[0]
    for name, fp in phase_fp.items():
        ax.plot(fp, lw=1.2, ls='--' if name in comparisons else '-', label=name)
    ax.set_xlabel('Bin'); ax.set_ylabel('Phase'); ax.set_title('Phase Fingerprint Comparison', fontweight='bold')
    ax.legend(fontsize=8, ncol=2); ax.grid(True, alpha=0.3)

    ax = axes[1]
    for name, df in flights.items():
        fps = df['phase_fingerprint'].apply(parse_fp).dropna()
        if len(fps) > 0:
            ax.plot(np.array(fps.tolist()).std(axis=0), lw=1.2, label=f'{name} (std)')
    ax.set_xlabel('Bin'); ax.set_ylabel('Phase Std')
    ax.set_title('Phase Fingerprint Variability (Higher = More Movement)', fontweight='bold')
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    plt.tight_layout(); plt.savefig(OUT / "07_phase_fingerprint.png"); plt.close()
    print("  Saved: 07_phase_fingerprint.png")

# ── 8. COMPARISON BOXPLOTS ───────────────────────────────────────────────
print("\n" + "=" * 70)
print("8. COMPARISON WITH STATIONARY TESTS")
print("=" * 70)

fig, axes = plt.subplots(2, 3, figsize=(18, 12))
metrics = ['mean_power_db', 'amp_mean', 'phase_drift', 'bandwidth_hz', 'total_energy', 'power_std_db']
mlabels = ['Mean Power (dB)', 'Amp Mean', 'Phase Drift', 'Bandwidth (Hz)', 'Total Energy', 'Power Std (dB)']
all_data = {**flights, **comparisons}

for ai, (metric, ml) in enumerate(zip(metrics, mlabels)):
    ax = axes[ai // 3][ai % 3]
    data, labels = [], []
    for name, df in all_data.items():
        if metric in df.columns:
            data.append(df[metric].dropna().values)
            labels.append(name)
    bp = ax.boxplot(data, labels=labels, patch_artist=True)
    for i, p in enumerate(bp['boxes']):
        p.set_facecolor('lightblue' if labels[i] in flights else ('lightcoral' if 'NoDrone' in labels[i] else 'lightgreen'))
    ax.set_title(ml, fontweight='bold'); ax.tick_params(axis='x', rotation=45); ax.grid(True, alpha=0.3)

plt.suptitle('Camera Flights (Blue) vs Stationary (Green) vs No Drone (Red)', fontsize=14, fontweight='bold')
plt.tight_layout(); plt.savefig(OUT / "08_comparison_boxplots.png"); plt.close()
print("  Saved: 08_comparison_boxplots.png")

# Stats table
comp_stats = []
for name, df in all_data.items():
    comp_stats.append({
        'Dataset': name,
        'Type': 'CameraFlight' if name in flights else 'Comparison',
        'mean_power_db': round(df['mean_power_db'].mean(), 2),
        'power_std': round(df['mean_power_db'].std(), 3),
        'amp_mean': f"{df['amp_mean'].mean():.6f}",
        'phase_drift_mean': round(df['phase_drift'].mean(), 5),
        'bandwidth_mean': round(df['bandwidth_hz'].mean(), 1),
        'active_ratio': f"{df['active_ratio'].mean():.6f}",
    })
    print(f"  {name}: power={comp_stats[-1]['mean_power_db']}dB")
pd.DataFrame(comp_stats).to_csv(OUT / "08_comparison_stats.csv", index=False)

# ── 9. VS PREVIOUS NO-CAMERA FLIGHT ──────────────────────────────────────
print("\n" + "=" * 70)
print("9. COMPARISON WITH PREVIOUS NO-CAMERA FLIGHT")
print("=" * 70)

if 'PrevFlight-NoCam' in comparisons:
    prev = comparisons['PrevFlight-NoCam']
    fig, axes = plt.subplots(3, 1, figsize=(16, 14))
    wp = min(20, len(prev) // 5)

    for ai, (metric, yl) in enumerate([('mean_power_db', 'Mean Power (dB)'),
                                         ('amp_mean', 'Amp Mean (×1000)'),
                                         ('phase_drift', 'Phase Drift')]):
        ax = axes[ai]
        for name, df in flights.items():
            w = min(20, len(df) // 5)
            y = V(smooth(df[metric], w))
            if 'amp' in metric: y = y * 1000
            ax.plot(V(df['elapsed_s']), y, lw=1.2, label=f'Camera: {name}')
        yp = V(smooth(prev[metric], wp))
        if 'amp' in metric: yp = yp * 1000
        ax.plot(V(prev['elapsed_s']), yp, lw=2, color='black', ls='--', label='Prev: No Camera')
        ax.set_ylabel(yl); ax.set_title(f'{yl}: Camera vs No-Camera', fontweight='bold')
        ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel('Elapsed Time (s)')
    plt.tight_layout(); plt.savefig(OUT / "09_vs_prev_flight.png"); plt.close()
    print("  Saved: 09_vs_prev_flight.png")

# ── 10. DISTANCE ESTIMATION ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("10. DISTANCE ESTIMATION & PATH LOSS")
print("=" * 70)

from scipy.signal import find_peaks

fig, axes = plt.subplots(len(flights), 1, figsize=(16, 4 * len(flights)))
distance_stats = []
for ax, (name, df) in zip(axes, flights.items()):
    w = min(20, len(df) // 5)
    x = V(df['elapsed_s'])
    if 'relative_distance' in df.columns:
        dist = V(smooth(df['relative_distance'], w))
        ax.plot(x, dist, color='darkorange', lw=1.5, label='Relative Distance')
        ax.axhline(1.0, color='gray', ls='--', alpha=0.5)
        valid = ~np.isnan(dist)
        dv = dist[valid]
        try:
            peaks, _ = find_peaks(dv, distance=20, prominence=0.005)
            valleys, _ = find_peaks(-dv, distance=20, prominence=0.005)
            xv = x[valid]
            if len(peaks) > 0:
                ax.scatter(xv[peaks], dv[peaks], c='red', s=30, zorder=5, marker='^', label=f'Far ({len(peaks)})')
            if len(valleys) > 0:
                ax.scatter(xv[valleys], dv[valleys], c='green', s=30, zorder=5, marker='v', label=f'Close ({len(valleys)})')
        except:
            peaks, valleys = [], []
        distance_stats.append({
            'Dataset': name,
            'Min': round(np.nanmin(dist), 4), 'Max': round(np.nanmax(dist), 4),
            'Mean': round(np.nanmean(dist), 4), 'Range': round(np.nanmax(dist) - np.nanmin(dist), 4),
            'FarPeaks': len(peaks) if hasattr(peaks, '__len__') else 0,
            'ClosePeaks': len(valleys) if hasattr(valleys, '__len__') else 0,
        })
    ax.set_ylabel('Relative Distance')
    ax.set_title(f'{name} — Distance Estimation', fontweight='bold')
    ax.legend(loc='upper left', fontsize=8); ax.grid(True, alpha=0.3)
axes[-1].set_xlabel('Time (s)')
plt.tight_layout(); plt.savefig(OUT / "10_distance_estimation.png"); plt.close()
print("  Saved: 10_distance_estimation.png")

if distance_stats:
    pd.DataFrame(distance_stats).to_csv(OUT / "10_distance_stats.csv", index=False)
    for r in distance_stats:
        print(f"  {r['Dataset']}: range={r['Range']:.4f}, far={r['FarPeaks']}, close={r['ClosePeaks']}")

# ── 11. LANDING DETECTION ────────────────────────────────────────────────
print("\n" + "=" * 70)
print("11. LANDING DETECTION ANALYSIS")
print("=" * 70)

fig, axes = plt.subplots(len(flights), 1, figsize=(16, 5 * len(flights)))
landing_results = []
for ax, (name, df) in zip(axes, flights.items()):
    w = min(30, len(df) // 5)
    x = V(df['elapsed_s'])
    ps = V(smooth(df['mean_power_db'], w))
    ds = V(smooth(df['phase_drift'], w))
    ax.plot(x, ps, color='navy', lw=1.5, label='Power (smoothed)')
    ax2 = ax.twinx()
    ax2.plot(x, np.abs(ds), color='red', alpha=0.6, lw=1, label='|Phase Drift|')
    ax2.set_ylabel('|Phase Drift|', color='red')

    cut = int(len(df) * 0.85)
    lp_std = df['mean_power_db'].iloc[cut:].std()
    ep_std = df['mean_power_db'].iloc[:cut].std()
    ld_mean = np.abs(df['phase_drift'].iloc[cut:]).mean()
    ed_mean = np.abs(df['phase_drift'].iloc[:cut]).mean()
    lp_mean = df['mean_power_db'].iloc[cut:].mean()
    ep_mean = df['mean_power_db'].iloc[:cut].mean()

    reasons = []
    if lp_std < ep_std * 0.5: reasons.append("power_stabilized")
    if ld_mean < ed_mean * 0.5: reasons.append("drift_reduced")
    if lp_mean > ep_mean + 0.5: reasons.append("power_increased")
    landed = len(reasons) >= 2
    if landed:
        ax.axvline(x[cut], color='green', ls='--', lw=2, label='Landing zone')

    landing_results.append({
        'Dataset': name, 'Landing': landed, 'Reasons': ', '.join(reasons) or 'none',
        'Late_Power_Std': round(lp_std, 4), 'Early_Power_Std': round(ep_std, 4),
        'Late_Drift': round(ld_mean, 6), 'Early_Drift': round(ed_mean, 6),
    })
    status = "LANDING" if landed else "No landing"
    ax.set_title(f'{name} — {status} [{", ".join(reasons)}]', fontweight='bold')
    ax.set_ylabel('Mean Power (dB)'); ax.legend(loc='upper left', fontsize=8); ax.grid(True, alpha=0.3)

axes[-1].set_xlabel('Time (s)')
plt.tight_layout(); plt.savefig(OUT / "11_landing_detection.png"); plt.close()
pd.DataFrame(landing_results).to_csv(OUT / "11_landing_detection.csv", index=False)
for r in landing_results:
    print(f"  {r['Dataset']}: {'YES' if r['Landing'] else 'NO'} ({r['Reasons']})")

# ── 12. MULTI-FLIGHT OVERLAY ─────────────────────────────────────────────
print("\n" + "=" * 70)
print("12. MULTI-FLIGHT OVERLAY")
print("=" * 70)

colors = plt.cm.tab10(np.linspace(0, 1, len(flights)))
fig, axes = plt.subplots(3, 1, figsize=(16, 14))

for ai, (metric, yl) in enumerate([('mean_power_db', 'Mean Power (dB)'),
                                     ('amp_mean', 'Amp Mean (×1000)'),
                                     ('relative_distance', 'Relative Distance')]):
    ax = axes[ai]
    for (name, df), c in zip(flights.items(), colors):
        if metric not in df.columns: continue
        w = min(20, len(df) // 5)
        y = V(smooth(df[metric], w))
        if 'amp' in metric: y = y * 1000
        ax.plot(V(df['elapsed_s']), y, lw=1.5, label=name, color=c)
    if 'distance' in metric: ax.axhline(1.0, color='gray', ls='--', alpha=0.5)
    ax.set_ylabel(yl); ax.set_title(f'All Flights — {yl}', fontweight='bold')
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
axes[-1].set_xlabel('Elapsed Time (s)')
plt.tight_layout(); plt.savefig(OUT / "12_multi_flight_overlay.png"); plt.close()
print("  Saved: 12_multi_flight_overlay.png")

# ── 13. AMPLITUDE HISTOGRAM EVOLUTION ─────────────────────────────────────
print("\n" + "=" * 70)
print("13. AMPLITUDE HISTOGRAM EVOLUTION")
print("=" * 70)

hist_cols = [c for c in flights[list(flights.keys())[0]].columns if c.startswith('amp_hist_')]
if hist_cols:
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    for idx, (name, df) in enumerate(flights.items()):
        ax = axes[idx // 2][idx % 2]
        ns = min(6, len(df) // 10)
        for si in np.linspace(0, len(df) - 1, ns, dtype=int):
            vals = [df[c].iloc[si] for c in hist_cols]
            ax.plot(range(len(vals)), vals, lw=1.2, label=f't={df["elapsed_s"].iloc[si]:.1f}s', alpha=0.7)
        ax.set_xlabel('Bin'); ax.set_ylabel('Density')
        ax.set_title(f'{name}', fontweight='bold'); ax.legend(fontsize=7); ax.grid(True, alpha=0.3)
    plt.suptitle('Amplitude Histogram Evolution', fontsize=14, fontweight='bold')
    plt.tight_layout(); plt.savefig(OUT / "13_amp_histogram_evolution.png"); plt.close()
    print("  Saved: 13_amp_histogram_evolution.png")

# ── 14. COSINE SIMILARITY OVER TIME ──────────────────────────────────────
print("\n" + "=" * 70)
print("14. FINGERPRINT STABILITY")
print("=" * 70)

if 'fp_cosine_sim' in flights[list(flights.keys())[0]].columns:
    fig, ax = plt.subplots(figsize=(16, 6))
    for (name, df), c in zip(flights.items(), colors):
        w = min(20, len(df) // 5)
        ax.plot(V(df['elapsed_s']), V(smooth(df['fp_cosine_sim'], w)), lw=1.2, label=name, color=c)
    ax.axhline(1.0, color='gray', ls='--', alpha=0.5)
    ax.set_ylabel('Cosine Similarity'); ax.set_xlabel('Elapsed Time (s)')
    ax.set_title('Fingerprint Cosine Similarity Over Time', fontweight='bold')
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
    plt.tight_layout(); plt.savefig(OUT / "14_cosine_similarity.png"); plt.close()
    print("  Saved: 14_cosine_similarity.png")

# ── 15. DETAILED DASHBOARDS ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("15. DETAILED DASHBOARDS")
print("=" * 70)

for name, df in list(flights.items())[-2:]:
    fig, axes = plt.subplots(5, 1, figsize=(18, 22), sharex=True)
    w = min(25, len(df) // 5)
    x = V(df['elapsed_s'])
    
    axes[0].plot(x, V(smooth(df['mean_power_db'], w)), color='navy', lw=1.5)
    m = df['active_ratio'] > 0
    if m.any(): axes[0].scatter(x[m], V(df['mean_power_db'])[m], c='red', s=5, zorder=5)
    axes[0].set_ylabel('Power (dB)'); axes[0].set_title(f'{name} — Dashboard', fontsize=14, fontweight='bold')

    axes[1].plot(x, V(smooth(df['amp_mean'], w)) * 1000, color='teal', lw=1.5)
    axes[1].set_ylabel('Amp (×1000)')

    axes[2].plot(x, V(smooth(df['phase_drift'], w)), color='darkviolet', lw=1.5)
    axes[2].axhline(0, color='gray', ls='--', alpha=0.5); axes[2].set_ylabel('Phase Drift')

    if 'relative_distance' in df.columns:
        axes[3].plot(x, V(smooth(df['relative_distance'], w)), color='darkorange', lw=1.5)
        axes[3].axhline(1.0, color='gray', ls='--', alpha=0.5)
    axes[3].set_ylabel('Rel Distance')

    axes[4].plot(x, V(smooth(df['bandwidth_hz'], w)), color='forestgreen', lw=1.5)
    axes[4].set_ylabel('Bandwidth (Hz)'); axes[4].set_xlabel('Time (s)')

    for a in axes: a.grid(True, alpha=0.3)
    sn = name.replace(':', '-')
    plt.tight_layout(); plt.savefig(OUT / f"15_dashboard_{sn}.png"); plt.close()
    print(f"  Saved: 15_dashboard_{sn}.png")

# ── FINAL SUMMARY ────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("ANALYSIS COMPLETE — SUMMARY")
print("=" * 70)

lines = []
lines.append("REAL FLIGHT CAMERA IQ ANALYSIS — APRIL 6, 2026")
lines.append("=" * 60)
lines.append("\nDATASETS:")
for name, df in flights.items():
    lines.append(f"  {name}: {len(df)} samples, {df['elapsed_s'].iloc[-1]:.1f}s")

lines.append("\nDATA QUALITY:")
for r in quality_report:
    lines.append(f"  {r['Dataset']}: {r['Mean_Interval_ms']}ms interval, {r['Active%']}% active, {r['Power_Range_dB']}dB range")

lines.append("\nDISTANCE ANALYSIS:")
for r in distance_stats:
    lines.append(f"  {r['Dataset']}: range={r['Min']:.4f}-{r['Max']:.4f} (Δ={r['Range']:.4f}), far={r['FarPeaks']}, close={r['ClosePeaks']}")

lines.append("\nLANDING DETECTION:")
for r in landing_results:
    lines.append(f"  {r['Dataset']}: {'DETECTED' if r['Landing'] else 'NOT DETECTED'} ({r['Reasons']})")

lines.append("\nFINGERPRINT CONSISTENCY:")
if fp_results:
    fnames = [n for n in flights.keys() if n in fp_results]
    sims = []
    for i in range(len(fnames)):
        for j in range(i + 1, len(fnames)):
            a, b = fp_results[fnames[i]], fp_results[fnames[j]]
            if len(a) == len(b):
                s = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
                sims.append(s)
                lines.append(f"  {fnames[i]} <-> {fnames[j]}: {s:.6f}")
    if sims:
        lines.append(f"  Mean cross-flight similarity: {np.mean(sims):.6f}")

summary = "\n".join(lines)
with open(OUT / "00_analysis_summary.txt", "w") as f:
    f.write(summary)
print(summary)
print(f"\nAll outputs saved to: {OUT}")
print(f"Total plots: {len(list(OUT.glob('*.png')))}")
