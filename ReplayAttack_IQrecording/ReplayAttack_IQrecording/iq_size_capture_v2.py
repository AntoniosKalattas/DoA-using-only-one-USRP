#!/usr/bin/env python3
"""
IQ Energy/Activity + Amplitude/Phase Fingerprint Capture for Drone State Classification

V2 — Extends the original iq_size_capture.py with:
  1. Amplitude & Phase statistical features (Option A)
  2. Compressed amplitude/phase fingerprint vectors (Option B)
  3. All original energy/activity metrics preserved

The hypothesis: different drone states produce different RF signatures measurable
through energy levels, amplitude patterns, and phase characteristics — without
decoding the encrypted payload.

New capabilities in V2:
  - Drone distance estimation (amplitude trends over time)
  - RF fingerprinting (unique phase/amplitude signatures per device)
  - Doppler/motion detection (phase drift between windows)
  - Camera resolution/zoom detection (burst amplitude envelope changes)

Usage:
    Capture:
        python iq_size_capture_v2.py capture --state idle --duration 60 --window 10
        python iq_size_capture_v2.py capture --state hover --duration 60 --window 10

    Analyze existing IQ file:
        python iq_size_capture_v2.py analyze --input mavic2_simulation.iq --state fly_fast --window 10

    Train ML model:
        python iq_size_capture_v2.py train --data ./iq_datasets

    Visualize:
        python iq_size_capture_v2.py visualize --data ./iq_datasets
"""

import argparse
import numpy as np
import time
import csv
import sys
from pathlib import Path
from datetime import datetime


# ============================================================
# Configuration
# ============================================================
DEFAULT_FREQ = 5.8e9       # 5.8 GHz
DEFAULT_RATE = 10e6        # 10 MHz sample rate
DEFAULT_GAIN = 40          # RX gain
DEFAULT_WINDOW_MS = 10     # 10ms capture window
DEFAULT_DURATION = 60      # 60 seconds total capture
NOISE_FLOOR_DB = -60       # Below this = noise (adjust based on environment)

FINGERPRINT_BINS = 128     # Number of bins for compressed fingerprint
AMP_HIST_BINS = 10         # Number of amplitude histogram bins

DATASET_DIR = Path(__file__).parent / "iq_datasets"


# ============================================================
# Original energy/activity metrics (from v1)
# ============================================================
def compute_energy_metrics(samples, sample_rate):
    """
    Compute energy/activity metrics from a small window of IQ samples.
    These metrics represent the 'effective data volume' without keeping raw IQ.
    (Identical to v1 compute_window_metrics)
    """
    power = np.abs(samples)**2
    total_energy = np.sum(power)

    mean_power = np.mean(power)
    mean_power_db = 10 * np.log10(mean_power + 1e-20)

    max_power_db = 10 * np.log10(np.max(power) + 1e-20)

    sample_power_db = 10 * np.log10(power + 1e-20)
    active_count = int(np.sum(sample_power_db > NOISE_FLOOR_DB))
    active_ratio = active_count / len(samples)

    active_mask = sample_power_db > NOISE_FLOOR_DB
    if np.any(active_mask):
        active_energy = float(np.sum(power[active_mask]))
        active_mean_db = 10 * np.log10(np.mean(power[active_mask]) + 1e-20)
    else:
        active_energy = 0.0
        active_mean_db = -100.0

    above_noise = (sample_power_db > NOISE_FLOOR_DB).astype(int)
    transitions = np.diff(above_noise)
    burst_count = int(np.sum(transitions == 1))

    duty_cycle = active_ratio

    power_std_db = float(np.std(sample_power_db))

    spectrum = np.abs(np.fft.fft(samples))**2
    max_spec = np.max(spectrum)
    if max_spec > 0:
        bandwidth_bins = int(np.sum(spectrum > max_spec / 2))
        bandwidth_hz = bandwidth_bins * (sample_rate / len(samples))
    else:
        bandwidth_hz = 0

    effective_size_bytes = active_count * 8

    return {
        'total_energy': float(total_energy),
        'mean_power_db': float(mean_power_db),
        'max_power_db': float(max_power_db),
        'active_samples': active_count,
        'active_ratio': float(active_ratio),
        'active_energy': float(active_energy),
        'active_mean_db': float(active_mean_db),
        'burst_count': burst_count,
        'duty_cycle': float(duty_cycle),
        'power_std_db': float(power_std_db),
        'bandwidth_hz': float(bandwidth_hz),
        'effective_size_bytes': effective_size_bytes,
    }


# ============================================================
# NEW: Amplitude & Phase statistical features (Option A)
# ============================================================
def compute_amp_phase_stats(samples):
    """
    Compute amplitude and phase statistical features from IQ samples.

    Amplitude = |I + jQ| = sqrt(I² + Q²)
    Phase = angle(I + jQ) = atan2(Q, I)

    Returns dict of amplitude/phase summary statistics.
    """
    amplitude = np.abs(samples)
    phase = np.angle(samples)  # returns radians [-π, π]

    # --- Amplitude statistics ---
    amp_mean = float(np.mean(amplitude))
    amp_std = float(np.std(amplitude))
    amp_max = float(np.max(amplitude))
    amp_min = float(np.min(amplitude))
    amp_median = float(np.median(amplitude))
    amp_p95 = float(np.percentile(amplitude, 95))
    amp_p05 = float(np.percentile(amplitude, 5))
    # Dynamic range: ratio of strong to weak signals
    amp_dynamic_range = float(amp_p95 / (amp_p05 + 1e-20))
    # Crest factor: peak-to-RMS ratio (indicates signal type)
    amp_rms = float(np.sqrt(np.mean(amplitude**2)))
    amp_crest_factor = float(amp_max / (amp_rms + 1e-20))

    # Amplitude histogram (distribution shape — different drones/modes have different shapes)
    hist_counts, _ = np.histogram(amplitude, bins=AMP_HIST_BINS, range=(0, amp_max + 1e-20))
    hist_norm = hist_counts / (np.sum(hist_counts) + 1e-20)

    # --- Phase statistics ---
    phase_mean = float(np.mean(phase))
    phase_std = float(np.std(phase))
    # Circular mean (proper average for angular data)
    phase_circ_mean = float(np.arctan2(np.mean(np.sin(phase)), np.mean(np.cos(phase))))
    # Circular spread (0=concentrated, 1=uniform) — measures phase coherence
    phase_concentration = float(np.abs(np.mean(np.exp(1j * phase))))

    # Phase drift: linear regression slope over the window
    # Rising phase = Doppler from approaching drone, falling = departing
    n = len(phase)
    # Unwrap phase to avoid ±π jumps
    phase_unwrapped = np.unwrap(phase)
    t = np.arange(n, dtype=np.float64)
    # Fast linear regression (slope only)
    t_mean = np.mean(t)
    phase_mean_uw = np.mean(phase_unwrapped)
    phase_drift = float(np.sum((t - t_mean) * (phase_unwrapped - phase_mean_uw)) /
                        (np.sum((t - t_mean)**2) + 1e-20))

    # Phase entropy (high = random/encrypted data, low = beacon/repetitive)
    phase_hist, _ = np.histogram(phase, bins=36, range=(-np.pi, np.pi))
    phase_probs = phase_hist / (np.sum(phase_hist) + 1e-20)
    phase_entropy = float(-np.sum(phase_probs * np.log2(phase_probs + 1e-20)))

    # --- Cross features (amplitude-phase relationship) ---
    # Correlation between amplitude and phase rate of change
    phase_diff = np.diff(phase_unwrapped)
    if len(phase_diff) > 0 and len(amplitude[:-1]) > 0:
        # Covariance of amplitude with phase change rate
        amp_phase_corr = float(np.corrcoef(amplitude[:-1], np.abs(phase_diff))[0, 1])
        if np.isnan(amp_phase_corr):
            amp_phase_corr = 0.0
    else:
        amp_phase_corr = 0.0

    result = {
        # Amplitude stats
        'amp_mean': amp_mean,
        'amp_std': amp_std,
        'amp_max': amp_max,
        'amp_min': amp_min,
        'amp_median': amp_median,
        'amp_p95': amp_p95,
        'amp_p05': amp_p05,
        'amp_dynamic_range': amp_dynamic_range,
        'amp_crest_factor': amp_crest_factor,
        'amp_rms': amp_rms,
        # Phase stats
        'phase_mean': phase_mean,
        'phase_std': phase_std,
        'phase_circ_mean': phase_circ_mean,
        'phase_concentration': phase_concentration,
        'phase_drift': phase_drift,
        'phase_entropy': phase_entropy,
        # Cross features
        'amp_phase_corr': amp_phase_corr,
    }

    # Amplitude histogram bins as separate columns
    for i, val in enumerate(hist_norm):
        result[f'amp_hist_{i}'] = float(val)

    return result


# ============================================================
# NEW: Compressed Fingerprint Vector (Option B)
# ============================================================
def compute_fingerprint(samples, n_bins=FINGERPRINT_BINS):
    """
    Create a compressed amplitude/phase fingerprint by downsampling
    the 100,000-sample window to n_bins points.

    Each bin = average amplitude and average phase of a chunk of samples.
    This preserves the temporal structure (burst patterns) at lower resolution.

    Returns:
        amp_fingerprint: string of n_bins comma-separated amplitude values
        phase_fingerprint: string of n_bins comma-separated phase values
    """
    amplitude = np.abs(samples)
    phase_unwrapped = np.unwrap(np.angle(samples))

    # Split into n_bins equal chunks and take the mean of each
    chunk_size = len(samples) // n_bins
    if chunk_size < 1:
        chunk_size = 1

    amp_bins = []
    phase_bins = []

    for i in range(n_bins):
        start = i * chunk_size
        end = start + chunk_size
        if end > len(samples):
            end = len(samples)
        if start >= len(samples):
            amp_bins.append(0.0)
            phase_bins.append(0.0)
        else:
            amp_bins.append(float(np.mean(amplitude[start:end])))
            phase_bins.append(float(np.mean(phase_unwrapped[start:end])))

    # Convert to comma-separated strings for CSV storage
    amp_fp = ','.join(f'{v:.6f}' for v in amp_bins)
    phase_fp = ','.join(f'{v:.6f}' for v in phase_bins)

    return amp_fp, phase_fp


# ============================================================
# Combined metrics: energy + amp/phase stats + fingerprint
# ============================================================
def compute_all_metrics(samples, sample_rate):
    """
    Compute ALL metrics for a single window:
      - Original energy/activity metrics (v1)
      - Amplitude & phase statistics (Option A)
      - Compressed fingerprint vectors (Option B)
    """
    # V1 energy metrics
    energy = compute_energy_metrics(samples, sample_rate)

    # Option A: amp/phase stats
    amp_phase = compute_amp_phase_stats(samples)

    # Option B: compressed fingerprint
    amp_fp, phase_fp = compute_fingerprint(samples)

    # Merge everything
    result = {}
    result.update(energy)
    result.update(amp_phase)
    result['amp_fingerprint'] = amp_fp
    result['phase_fingerprint'] = phase_fp

    return result


# ============================================================
# CSV field names
# ============================================================
def get_fieldnames():
    """Return all CSV column names in order."""
    # Metadata
    meta = ['timestamp', 'window_ms', 'state']

    # V1 energy features
    energy = ['total_energy', 'mean_power_db', 'max_power_db',
              'active_samples', 'active_ratio', 'active_energy',
              'active_mean_db', 'burst_count', 'duty_cycle',
              'power_std_db', 'bandwidth_hz', 'effective_size_bytes']

    # V2 amplitude stats
    amp_stats = ['amp_mean', 'amp_std', 'amp_max', 'amp_min', 'amp_median',
                 'amp_p95', 'amp_p05', 'amp_dynamic_range', 'amp_crest_factor', 'amp_rms']

    # V2 phase stats
    phase_stats = ['phase_mean', 'phase_std', 'phase_circ_mean',
                   'phase_concentration', 'phase_drift', 'phase_entropy']

    # V2 cross features
    cross = ['amp_phase_corr']

    # V2 amplitude histogram
    amp_hist = [f'amp_hist_{i}' for i in range(AMP_HIST_BINS)]

    # V2 fingerprint vectors (stored as comma-separated strings)
    fingerprints = ['amp_fingerprint', 'phase_fingerprint']

    return meta + energy + amp_stats + phase_stats + cross + amp_hist + fingerprints


def get_feature_columns():
    """Return only the feature columns used for ML training (excludes metadata and fingerprints)."""
    energy = ['total_energy', 'mean_power_db', 'max_power_db',
              'active_samples', 'active_ratio', 'active_energy',
              'active_mean_db', 'burst_count', 'duty_cycle',
              'power_std_db', 'bandwidth_hz', 'effective_size_bytes']

    amp_stats = ['amp_mean', 'amp_std', 'amp_max', 'amp_min', 'amp_median',
                 'amp_p95', 'amp_p05', 'amp_dynamic_range', 'amp_crest_factor', 'amp_rms']

    phase_stats = ['phase_mean', 'phase_std', 'phase_circ_mean',
                   'phase_concentration', 'phase_drift', 'phase_entropy']

    cross = ['amp_phase_corr']

    amp_hist = [f'amp_hist_{i}' for i in range(AMP_HIST_BINS)]

    return energy + amp_stats + phase_stats + cross + amp_hist


# ============================================================
# Live Capture
# ============================================================
def capture_live(state, duration, window_ms, freq, rate, gain, output_dir):
    """
    Capture IQ windows from USRP, compute all metrics, discard raw IQ.
    """
    try:
        import uhd
    except ImportError:
        print("ERROR: UHD Python API not available")
        print("Use 'analyze' mode to process existing .iq files instead")
        return False

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"{state}_{window_ms}ms_{timestamp}.csv"

    samples_per_window = int(rate * window_ms / 1000)
    total_windows = int(duration * 1000 / window_ms)

    print("=" * 60)
    print("IQ CAPTURE V2 — Energy + Amplitude/Phase Fingerprint")
    print("=" * 60)
    print(f"State:          {state}")
    print(f"Frequency:      {freq/1e9:.3f} GHz")
    print(f"Sample Rate:    {rate/1e6:.1f} MHz")
    print(f"Gain:           {gain} dB")
    print(f"Window Size:    {window_ms} ms ({samples_per_window:,} samples)")
    print(f"Duration:       {duration} seconds")
    print(f"Total Windows:  {total_windows:,}")
    print(f"Fingerprint:    {FINGERPRINT_BINS} bins per window")
    print(f"Output:         {output_file}")
    print("=" * 60)

    # Initialize USRP
    usrp = uhd.usrp.MultiUSRP()

    fieldnames = get_fieldnames()

    print(f"\nStarting capture in 3 seconds...")
    time.sleep(3)
    print("Recording... Press Ctrl+C to stop early.\n")

    start_time = time.time()
    window_count = 0

    try:
        with open(output_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for i in range(total_windows):
                samples = usrp.recv_num_samps(
                    num_samps=samples_per_window,
                    freq=freq,
                    rate=rate,
                    channels=[0],
                    gain=gain
                )

                metrics = compute_all_metrics(
                    samples[0] if samples.ndim > 1 else samples, rate
                )

                row = {
                    'timestamp': datetime.now().isoformat(),
                    'window_ms': window_ms,
                    'state': state,
                    **metrics
                }

                writer.writerow(row)
                window_count += 1

                # Progress bar every 10 windows
                if window_count % 10 == 0:
                    elapsed = time.time() - start_time
                    pct = window_count / total_windows
                    filled = int(30 * pct)
                    bar = '█' * filled + '░' * (30 - filled)
                    rate_w = window_count / elapsed if elapsed > 0 else 1
                    eta_sec = (total_windows - window_count) / rate_w
                    eta_min, eta_s = divmod(int(eta_sec), 60)
                    print(f"\r  [{bar}] {pct:5.1%} | "
                          f"Window {window_count}/{total_windows} | "
                          f"Elapsed: {int(elapsed//60)}m{int(elapsed%60):02d}s | "
                          f"ETA: ~{eta_min}m{eta_s:02d}s | "
                          f"active={metrics['active_ratio']:.1%} amp={metrics['amp_mean']:.4f}",
                          end='', flush=True)

    except KeyboardInterrupt:
        print(f"\n\nStopped early after {window_count} windows")

    print()
    elapsed = time.time() - start_time
    print(f"\n✓ Capture complete!")
    print(f"  Windows recorded: {window_count}")
    print(f"  Time elapsed:     {elapsed:.1f}s")
    print(f"  Saved to:         {output_file}")
    print(f"  File size:        {output_file.stat().st_size / 1024:.1f} KB")
    print(f"  Features per window: {len(get_feature_columns())} stats + {FINGERPRINT_BINS}-bin fingerprint")

    return True


# ============================================================
# Analyze existing IQ file
# ============================================================
def analyze_iq_file(input_file, state, window_ms, rate, output_dir):
    """
    Process an existing .iq file into windowed metrics with amp/phase.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"{state}_{window_ms}ms_{timestamp}.csv"

    samples_per_window = int(rate * window_ms / 1000)

    file_size = input_path.stat().st_size
    total_samples = file_size // 8
    total_windows = total_samples // samples_per_window

    print("=" * 60)
    print("IQ FILE ANALYSIS V2 — Energy + Amplitude/Phase Fingerprint")
    print("=" * 60)
    print(f"Input:          {input_file}")
    print(f"File Size:      {file_size/1e6:.1f} MB")
    print(f"Total Samples:  {total_samples:,}")
    print(f"Window Size:    {window_ms} ms ({samples_per_window:,} samples)")
    print(f"Total Windows:  {total_windows:,}")
    print(f"State Label:    {state}")
    print(f"Fingerprint:    {FINGERPRINT_BINS} bins per window")
    print("=" * 60)

    fieldnames = get_fieldnames()

    window_count = 0
    start_time = time.time()

    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for i in range(total_windows):
            offset = i * samples_per_window
            samples = np.fromfile(input_file, dtype=np.complex64,
                                  count=samples_per_window, offset=offset * 8)

            if len(samples) < samples_per_window:
                break

            metrics = compute_all_metrics(samples, rate)

            row = {
                'timestamp': f"window_{i}",
                'window_ms': window_ms,
                'state': state,
                **metrics
            }

            writer.writerow(row)
            window_count += 1

            if window_count % 10 == 0:
                elapsed = time.time() - start_time
                pct = window_count / total_windows
                filled = int(30 * pct)
                bar = '█' * filled + '░' * (30 - filled)
                rate_w = window_count / elapsed if elapsed > 0 else 1
                eta_sec = (total_windows - window_count) / rate_w
                eta_min, eta_s = divmod(int(eta_sec), 60)
                print(f"\r  [{bar}] {pct:5.1%} | "
                      f"Window {window_count}/{total_windows} | "
                      f"Elapsed: {int(elapsed//60)}m{int(elapsed%60):02d}s | "
                      f"ETA: ~{eta_min}m{eta_s:02d}s",
                      end='', flush=True)

    print()
    print(f"\n✓ Analysis complete!")
    print(f"  Windows processed: {window_count}")
    print(f"  Output: {output_file}")
    print(f"  Output size: {output_file.stat().st_size / 1024:.1f} KB")


# ============================================================
# Train ML Model
# ============================================================
def train_model(data_dir):
    """Train ML model on collected datasets with all V2 features"""
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import LabelEncoder, StandardScaler
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
    import pickle
    import json

    data_dir = Path(data_dir)

    csv_files = list(data_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {data_dir}")
        return

    print("=" * 60)
    print("ML TRAINING V2 — Energy + Amplitude/Phase Features")
    print("=" * 60)

    all_data = []
    for f in csv_files:
        df = pd.read_csv(f)
        print(f"  Loaded {f.name}: {len(df)} windows, state='{df['state'].iloc[0]}'")
        all_data.append(df)

    data = pd.concat(all_data, ignore_index=True)
    print(f"\nTotal samples: {len(data)}")
    print(f"Class distribution:\n{data['state'].value_counts().to_string()}")

    # Determine which features are available in the data
    all_feature_cols = get_feature_columns()
    feature_cols = [c for c in all_feature_cols if c in data.columns]

    # Warn about missing features (backward compat with v1 CSVs)
    missing = set(all_feature_cols) - set(feature_cols)
    if missing:
        print(f"\n⚠ Missing V2 features (using V1 CSVs?): {len(missing)} features skipped")
        print(f"  Missing: {', '.join(sorted(missing)[:5])}...")

    print(f"\nUsing {len(feature_cols)} features for training")

    X = data[feature_cols].values
    y = data['state'].values

    le = LabelEncoder()
    y_encoded = le.fit_transform(y)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y_encoded, test_size=0.2, stratify=y_encoded, random_state=42
    )

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)

    print("\n--- Random Forest ---")
    rf = RandomForestClassifier(n_estimators=100, max_depth=20, random_state=42, n_jobs=-1)
    rf.fit(X_train, y_train)
    y_pred_rf = rf.predict(X_test)
    acc_rf = accuracy_score(y_test, y_pred_rf)
    print(f"Accuracy: {acc_rf:.4f}")
    print(classification_report(y_test, y_pred_rf, target_names=le.classes_))

    # Feature importance (top 20)
    print("Feature Importance (top 20):")
    importances = rf.feature_importances_
    top_idx = np.argsort(importances)[::-1][:20]
    for idx in top_idx:
        print(f"  {feature_cols[idx]:30s} {importances[idx]:.4f}")

    # Confusion Matrix
    print(f"\nConfusion Matrix:")
    cm = confusion_matrix(y_test, y_pred_rf)
    print(f"{'':20}", end='')
    for c in le.classes_:
        print(f"{c:>20}", end='')
    print()
    for i, c in enumerate(le.classes_):
        print(f"{c:20}", end='')
        for j in range(len(le.classes_)):
            print(f"{cm[i][j]:20d}", end='')
        print()

    # Save model
    model_dir = data_dir / "models_v2"
    model_dir.mkdir(exist_ok=True)
    with open(model_dir / "rf_model.pkl", 'wb') as f:
        pickle.dump(rf, f)
    with open(model_dir / "scaler.pkl", 'wb') as f:
        pickle.dump(scaler, f)
    with open(model_dir / "label_encoder.pkl", 'wb') as f:
        pickle.dump(le, f)
    with open(model_dir / "feature_columns.json", 'w') as f:
        json.dump(feature_cols, f)

    print(f"\nModel saved to {model_dir}")


# ============================================================
# Visualization
# ============================================================
def visualize(data_dir):
    """Visualize datasets including new amplitude/phase features"""
    import pandas as pd
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_style("whitegrid")
    data_dir = Path(data_dir)
    fig_dir = data_dir / "figures_v2"
    fig_dir.mkdir(exist_ok=True)

    all_data = []
    for f in data_dir.glob("*.csv"):
        df = pd.read_csv(f)
        all_data.append(df)

    data = pd.concat(all_data, ignore_index=True)

    order = sorted(data['state'].unique())
    default_colors = ['#3498db', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6',
                      '#1abc9c', '#e67e22', '#34495e']
    palette = {state: default_colors[i % len(default_colors)] for i, state in enumerate(order)}

    has_v2 = 'amp_mean' in data.columns

    print("=" * 60)
    print("VISUALIZATION V2 — Energy + Amplitude/Phase Analysis")
    print("=" * 60)

    for state in order:
        s = data[data['state'] == state]
        print(f"\n{state}:")
        print(f"  Effective size:  {s['effective_size_bytes'].mean()/1024:.1f} ± {s['effective_size_bytes'].std()/1024:.1f} KB")
        print(f"  Active ratio:    {s['active_ratio'].mean():.1%} ± {s['active_ratio'].std():.1%}")
        print(f"  Total energy:    {s['total_energy'].mean():.4f} ± {s['total_energy'].std():.4f}")
        print(f"  Burst count:     {s['burst_count'].mean():.1f} ± {s['burst_count'].std():.1f}")
        if has_v2:
            print(f"  Amp mean:        {s['amp_mean'].mean():.6f} ± {s['amp_mean'].std():.6f}")
            print(f"  Amp max:         {s['amp_max'].mean():.6f} ± {s['amp_max'].std():.6f}")
            print(f"  Phase drift:     {s['phase_drift'].mean():.6f} ± {s['phase_drift'].std():.6f}")
            print(f"  Phase entropy:   {s['phase_entropy'].mean():.2f} ± {s['phase_entropy'].std():.2f}")
            print(f"  Phase conc.:     {s['phase_concentration'].mean():.4f} ± {s['phase_concentration'].std():.4f}")

    # ---- PLOT 1: Original effective size comparison ----
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    sns.boxplot(data=data, x='state', y='effective_size_bytes', order=order,
                hue='state', palette=palette, ax=axes[0], legend=False)
    axes[0].set_ylabel('Effective Size (bytes)')
    axes[0].set_title('Effective IQ Data Size per Window')

    sns.boxplot(data=data, x='state', y='active_ratio', order=order,
                hue='state', palette=palette, ax=axes[1], legend=False)
    axes[1].set_ylabel('Active Ratio')
    axes[1].set_title('Signal Activity (above noise)')

    sns.boxplot(data=data, x='state', y='total_energy', order=order,
                hue='state', palette=palette, ax=axes[2], legend=False)
    axes[2].set_ylabel('Total Energy')
    axes[2].set_title('Total Signal Energy')

    fig.suptitle('IQ Data Volume Analysis by Drone State', fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(fig_dir / 'iq_size_comparison.png', dpi=150, bbox_inches='tight')
    print(f"\nSaved: {fig_dir / 'iq_size_comparison.png'}")
    plt.close()

    # ---- PLOT 2: Amplitude/Phase features (V2 only) ----
    if has_v2:
        fig, axes = plt.subplots(2, 3, figsize=(16, 10))
        axes = axes.flatten()
        v2_features = ['amp_mean', 'amp_std', 'amp_crest_factor',
                       'phase_drift', 'phase_entropy', 'phase_concentration']

        for i, feat in enumerate(v2_features):
            if feat in data.columns:
                sns.violinplot(data=data, x='state', y=feat, order=order,
                              hue='state', palette=palette, ax=axes[i],
                              inner='quartile', legend=False)
                axes[i].set_title(feat.replace('_', ' ').title())
                axes[i].set_xlabel('')

        fig.suptitle('Amplitude & Phase Signature Features', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(fig_dir / 'amp_phase_features.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'amp_phase_features.png'}")
        plt.close()

        # ---- PLOT 3: Amplitude over time (distance proxy) ----
        fig, axes = plt.subplots(2, 1, figsize=(14, 8))

        for state in order:
            s = data[data['state'] == state].reset_index(drop=True)
            x_vals = s.index[:500].values
            # Amplitude over time
            axes[0].plot(x_vals, s['amp_mean'].values[:500],
                        alpha=0.7, label=state, color=palette.get(state, 'gray'))
            # Phase drift over time
            axes[1].plot(x_vals, s['phase_drift'].values[:500],
                        alpha=0.7, label=state, color=palette.get(state, 'gray'))

        axes[0].set_ylabel('Mean Amplitude')
        axes[0].set_title('Mean Amplitude Over Time (distance proxy — rising = closer)')
        axes[0].legend()
        axes[1].set_xlabel('Window Number (10ms each)')
        axes[1].set_ylabel('Phase Drift (rad/sample)')
        axes[1].set_title('Phase Drift Over Time (Doppler proxy — nonzero = movement)')
        axes[1].legend()

        plt.tight_layout()
        plt.savefig(fig_dir / 'amp_phase_timeseries.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'amp_phase_timeseries.png'}")
        plt.close()

        # ---- PLOT 4: Fingerprint heatmap (one per state) ----
        if 'amp_fingerprint' in data.columns:
            fig, axes = plt.subplots(len(order), 1, figsize=(14, 4 * len(order)))
            if len(order) == 1:
                axes = [axes]

            for ax_i, state in enumerate(order):
                s = data[data['state'] == state]
                # Parse fingerprint strings into arrays
                fp_matrix = []
                for fp_str in s['amp_fingerprint'].values[:100]:  # First 100 windows
                    try:
                        vals = [float(x) for x in str(fp_str).split(',')]
                        fp_matrix.append(vals)
                    except (ValueError, AttributeError):
                        continue

                if fp_matrix:
                    fp_matrix = np.array(fp_matrix)
                    im = axes[ax_i].imshow(fp_matrix, aspect='auto', cmap='viridis',
                                           interpolation='nearest')
                    axes[ax_i].set_title(f'Amplitude Fingerprint — {state} (first 100 windows)')
                    axes[ax_i].set_ylabel('Window #')
                    axes[ax_i].set_xlabel(f'Fingerprint bin (1 bin = {10/FINGERPRINT_BINS*1000:.1f} μs)')
                    plt.colorbar(im, ax=axes[ax_i], label='Amplitude')

            plt.tight_layout()
            plt.savefig(fig_dir / 'fingerprint_heatmap.png', dpi=150, bbox_inches='tight')
            print(f"Saved: {fig_dir / 'fingerprint_heatmap.png'}")
            plt.close()

    # ---- PLOT 5: Time series of effective size (same as v1) ----
    fig, ax = plt.subplots(figsize=(14, 5))
    for state in order:
        s = data[data['state'] == state].reset_index(drop=True)
        x_vals = s.index[:500].values
        y_vals = s['effective_size_bytes'].values[:500] / 1024
        ax.plot(x_vals, y_vals, alpha=0.7, label=state, color=palette.get(state, 'gray'))

    ax.set_xlabel('Window Number')
    ax.set_ylabel('Effective Size (KB)')
    ax.set_title('Effective IQ Data Size Over Time (first 500 windows)')
    ax.legend()
    plt.tight_layout()
    plt.savefig(fig_dir / 'iq_size_timeseries.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {fig_dir / 'iq_size_timeseries.png'}")
    plt.close()

    print(f"\nAll figures saved to {fig_dir}")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="IQ Capture & Classification V2 — Energy + Amplitude/Phase Fingerprint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Live capture (10ms windows, 60 seconds):
    python iq_size_capture_v2.py capture --state idle --duration 60 --window 10
    python iq_size_capture_v2.py capture --state hover --duration 60 --window 10

  Analyze existing IQ file:
    python iq_size_capture_v2.py analyze --input mavic2_simulation.iq --state fly_fast

  Train model (works with both V1 and V2 CSVs):
    python iq_size_capture_v2.py train --data ./iq_datasets

  Visualize:
    python iq_size_capture_v2.py visualize --data ./iq_datasets
        """
    )

    subparsers = parser.add_subparsers(dest='command')

    # Capture
    cap = subparsers.add_parser('capture', help='Live capture from USRP')
    cap.add_argument('--state', required=True, help='Drone state label')
    cap.add_argument('--duration', type=float, default=DEFAULT_DURATION, help='Total duration (seconds)')
    cap.add_argument('--window', type=float, default=DEFAULT_WINDOW_MS, help='Window size (ms)')
    cap.add_argument('--freq', type=float, default=DEFAULT_FREQ, help='Center frequency (Hz)')
    cap.add_argument('--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    cap.add_argument('--gain', type=float, default=DEFAULT_GAIN, help='RX gain (dB)')
    cap.add_argument('--output', default=str(DATASET_DIR), help='Output directory')

    # Analyze
    ana = subparsers.add_parser('analyze', help='Analyze existing IQ file')
    ana.add_argument('--input', required=True, help='Input .iq file')
    ana.add_argument('--state', required=True, help='State label')
    ana.add_argument('--window', type=float, default=DEFAULT_WINDOW_MS, help='Window size (ms)')
    ana.add_argument('--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    ana.add_argument('--output', default=str(DATASET_DIR), help='Output directory')

    # Train
    trn = subparsers.add_parser('train', help='Train ML model')
    trn.add_argument('--data', default=str(DATASET_DIR), help='Dataset directory')

    # Visualize
    viz = subparsers.add_parser('visualize', help='Visualize datasets')
    viz.add_argument('--data', default=str(DATASET_DIR), help='Dataset directory')

    args = parser.parse_args()

    if args.command == 'capture':
        capture_live(args.state, args.duration, args.window,
                     args.freq, args.rate, args.gain, args.output)
    elif args.command == 'analyze':
        analyze_iq_file(args.input, args.state, args.window, args.rate, args.output)
    elif args.command == 'train':
        train_model(args.data)
    elif args.command == 'visualize':
        visualize(args.data)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
