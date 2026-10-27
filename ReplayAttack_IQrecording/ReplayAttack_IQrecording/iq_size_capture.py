#!/usr/bin/env python3
"""
IQ Energy/Activity Capture for Drone State Classification

Instead of saving raw IQ samples (huge files), this captures small time windows
(e.g., 10ms) of IQ data, computes "effective data volume" metrics from each window,
discards the raw IQ, and saves only the metrics.

The hypothesis: different drone states (idle, hover, fly_fast) transmit different
amounts of data, which manifests as different signal energy/activity levels.

Usage:
    Capture:
        python iq_size_capture.py capture --state idle --duration 60 --window 10
        python iq_size_capture.py capture --state hover --duration 60 --window 10
        python iq_size_capture.py capture --state fly_fast --duration 60 --window 10

    Analyze existing IQ file:
        python iq_size_capture.py analyze --input mavic2_simulation.iq --state fly_fast --window 10

    Train ML model:
        python iq_size_capture.py train --data ./iq_datasets

    Visualize:
        python iq_size_capture.py visualize --data ./iq_datasets
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

DATASET_DIR = Path(__file__).parent / "iq_datasets"


def compute_window_metrics(samples, sample_rate):
    """
    Compute energy/activity metrics from a small window of IQ samples.
    These metrics represent the 'effective data volume' without keeping raw IQ.
    
    Args:
        samples: complex64 numpy array of IQ samples
        sample_rate: sample rate in Hz
    
    Returns:
        dict of computed metrics
    """
    # 1. Total signal energy (sum of all |sample|^2)
    #    This is the "total data volume" equivalent
    power = np.abs(samples)**2
    total_energy = np.sum(power)
    
    # 2. Mean power in dB
    mean_power = np.mean(power)
    mean_power_db = 10 * np.log10(mean_power + 1e-20)
    
    # 3. Max power (peak)
    max_power_db = 10 * np.log10(np.max(power) + 1e-20)
    
    # 4. Active samples count - samples with power above noise floor
    #    This is the closest to your "data size" idea
    sample_power_db = 10 * np.log10(power + 1e-20)
    active_count = int(np.sum(sample_power_db > NOISE_FLOOR_DB))
    active_ratio = active_count / len(samples)
    
    # 5. Active energy - energy ONLY from above-noise samples
    #    This is the "useful data volume" - ignoring silence
    active_mask = sample_power_db > NOISE_FLOOR_DB
    if np.any(active_mask):
        active_energy = float(np.sum(power[active_mask]))
        active_mean_db = 10 * np.log10(np.mean(power[active_mask]) + 1e-20)
    else:
        active_energy = 0.0
        active_mean_db = -100.0
    
    # 6. Burst count - number of distinct transmission bursts
    #    (transitions from below to above noise floor)
    above_noise = (sample_power_db > NOISE_FLOOR_DB).astype(int)
    transitions = np.diff(above_noise)
    burst_count = int(np.sum(transitions == 1))  # Rising edges
    
    # 7. Burst duty cycle - fraction of time the signal is active
    duty_cycle = active_ratio
    
    # 8. Power variance (stability indicator)
    power_std_db = float(np.std(sample_power_db))
    
    # 9. Spectral energy (bandwidth usage)
    spectrum = np.abs(np.fft.fft(samples))**2
    max_spec = np.max(spectrum)
    if max_spec > 0:
        bandwidth_bins = int(np.sum(spectrum > max_spec / 2))
        bandwidth_hz = bandwidth_bins * (sample_rate / len(samples))
    else:
        bandwidth_hz = 0
    
    # 10. Effective data size estimate (bytes equivalent)
    #     Theoretical: active_samples * bytes_per_sample
    #     This is the "virtual file size" if you only saved active data
    effective_size_bytes = active_count * 8  # 8 bytes per complex64 sample
    
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


def capture_live(state, duration, window_ms, freq, rate, gain, output_dir):
    """
    Capture IQ windows from USRP, compute metrics, discard raw IQ.
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
    print("IQ ENERGY CAPTURE")
    print("=" * 60)
    print(f"State:          {state}")
    print(f"Frequency:      {freq/1e9:.3f} GHz")
    print(f"Sample Rate:    {rate/1e6:.1f} MHz")
    print(f"Gain:           {gain} dB")
    print(f"Window Size:    {window_ms} ms ({samples_per_window:,} samples)")
    print(f"Duration:       {duration} seconds")
    print(f"Total Windows:  {total_windows:,}")
    print(f"Output:         {output_file}")
    print("=" * 60)
    
    # Initialize USRP
    usrp = uhd.usrp.MultiUSRP()
    
    # CSV header
    fieldnames = ['timestamp', 'window_ms', 'state',
                  'total_energy', 'mean_power_db', 'max_power_db',
                  'active_samples', 'active_ratio', 'active_energy',
                  'active_mean_db', 'burst_count', 'duty_cycle',
                  'power_std_db', 'bandwidth_hz', 'effective_size_bytes']
    
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
                # Capture one window of IQ samples
                samples = usrp.recv_num_samps(
                    num_samps=samples_per_window,
                    freq=freq,
                    rate=rate,
                    channels=[0],
                    gain=gain
                )
                
                # Compute metrics (the "size" equivalent)
                metrics = compute_window_metrics(samples[0] if samples.ndim > 1 else samples, rate)
                
                # Add metadata
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
                          f"active={metrics['active_ratio']:.1%}",
                          end='', flush=True)
    
    except KeyboardInterrupt:
        print(f"\n\nStopped early after {window_count} windows")
    
    print()  # Newline after progress bar
    elapsed = time.time() - start_time
    print(f"\n✓ Capture complete!")
    print(f"  Windows recorded: {window_count}")
    print(f"  Time elapsed:     {elapsed:.1f}s")
    print(f"  Saved to:         {output_file}")
    print(f"  File size:        {output_file.stat().st_size / 1024:.1f} KB (vs ~{window_count * samples_per_window * 8 / 1e6:.0f} MB of raw IQ!)")
    
    return True


def analyze_iq_file(input_file, state, window_ms, rate, output_dir):
    """
    Process an existing .iq file into windowed metrics.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    input_path = Path(input_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"{state}_{window_ms}ms_{timestamp}.csv"
    
    samples_per_window = int(rate * window_ms / 1000)
    
    # Get total samples
    file_size = input_path.stat().st_size
    total_samples = file_size // 8  # complex64 = 8 bytes
    total_windows = total_samples // samples_per_window
    
    print("=" * 60)
    print("IQ FILE ANALYSIS")
    print("=" * 60)
    print(f"Input:          {input_file}")
    print(f"File Size:      {file_size/1e6:.1f} MB")
    print(f"Total Samples:  {total_samples:,}")
    print(f"Window Size:    {window_ms} ms ({samples_per_window:,} samples)")
    print(f"Total Windows:  {total_windows:,}")
    print(f"State Label:    {state}")
    print("=" * 60)
    
    fieldnames = ['timestamp', 'window_ms', 'state',
                  'total_energy', 'mean_power_db', 'max_power_db',
                  'active_samples', 'active_ratio', 'active_energy',
                  'active_mean_db', 'burst_count', 'duty_cycle',
                  'power_std_db', 'bandwidth_hz', 'effective_size_bytes']
    
    window_count = 0
    start_time = time.time()
    
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for i in range(total_windows):
            # Read one window at a time (memory efficient)
            offset = i * samples_per_window
            samples = np.fromfile(input_file, dtype=np.complex64,
                                  count=samples_per_window, offset=offset * 8)
            
            if len(samples) < samples_per_window:
                break
            
            metrics = compute_window_metrics(samples, rate)
            
            row = {
                'timestamp': f"window_{i}",
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
                      f"ETA: ~{eta_min}m{eta_s:02d}s",
                      end='', flush=True)
    
    print()  # Newline after progress bar
    print(f"\n✓ Analysis complete!")
    print(f"  Windows processed: {window_count}")
    print(f"  Output: {output_file}")
    print(f"  Output size: {output_file.stat().st_size / 1024:.1f} KB")


def train_model(data_dir):
    """Train ML model on collected IQ energy datasets"""
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import LabelEncoder, StandardScaler
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
    import pickle
    import json
    
    data_dir = Path(data_dir)
    
    # Load all CSV files
    csv_files = list(data_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {data_dir}")
        return
    
    print("=" * 60)
    print("ML TRAINING - IQ ENERGY CLASSIFICATION")
    print("=" * 60)
    
    all_data = []
    for f in csv_files:
        df = pd.read_csv(f)
        print(f"  Loaded {f.name}: {len(df)} windows, state='{df['state'].iloc[0]}'")
        all_data.append(df)
    
    data = pd.concat(all_data, ignore_index=True)
    print(f"\nTotal samples: {len(data)}")
    print(f"Class distribution:\n{data['state'].value_counts().to_string()}")
    
    # Feature columns
    feature_cols = ['total_energy', 'mean_power_db', 'max_power_db',
                    'active_samples', 'active_ratio', 'active_energy',
                    'active_mean_db', 'burst_count', 'duty_cycle',
                    'power_std_db', 'bandwidth_hz', 'effective_size_bytes']
    
    X = data[feature_cols].values
    y = data['state'].values
    
    # Encode labels
    le = LabelEncoder()
    y_encoded = le.fit_transform(y)
    
    # Split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y_encoded, test_size=0.2, stratify=y_encoded, random_state=42
    )
    
    # Scale
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)
    
    # Train Random Forest
    print("\n--- Random Forest ---")
    rf = RandomForestClassifier(n_estimators=100, max_depth=20, random_state=42, n_jobs=-1)
    rf.fit(X_train, y_train)
    y_pred_rf = rf.predict(X_test)
    acc_rf = accuracy_score(y_test, y_pred_rf)
    print(f"Accuracy: {acc_rf:.4f}")
    print(classification_report(y_test, y_pred_rf, target_names=le.classes_))
    
    # Feature importance
    print("Feature Importance:")
    importances = rf.feature_importances_
    for idx in np.argsort(importances)[::-1]:
        print(f"  {feature_cols[idx]:30s} {importances[idx]:.4f}")
    
    # Confusion Matrix
    print(f"\nConfusion Matrix:")
    cm = confusion_matrix(y_test, y_pred_rf)
    print(f"{'':12}", end='')
    for c in le.classes_:
        print(f"{c:>10}", end='')
    print()
    for i, c in enumerate(le.classes_):
        print(f"{c:12}", end='')
        for j in range(len(le.classes_)):
            print(f"{cm[i][j]:10d}", end='')
        print()
    
    # Save model
    model_dir = data_dir / "models"
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


def visualize(data_dir):
    """Visualize the collected datasets"""
    import pandas as pd
    import matplotlib.pyplot as plt
    import seaborn as sns
    
    sns.set_style("whitegrid")
    data_dir = Path(data_dir)
    fig_dir = data_dir / "figures"
    fig_dir.mkdir(exist_ok=True)
    
    # Load all CSV files
    all_data = []
    for f in data_dir.glob("*.csv"):
        df = pd.read_csv(f)
        all_data.append(df)
    
    data = pd.concat(all_data, ignore_index=True)
    
    order = sorted(data['state'].unique())
    # Dynamic palette - assign colors to whatever states exist
    default_colors = ['#3498db', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22', '#34495e']
    palette = {state: default_colors[i % len(default_colors)] for i, state in enumerate(order)}
    
    print("=" * 60)
    print("VISUALIZATION - IQ ENERGY ANALYSIS")
    print("=" * 60)
    
    # Print summary
    for state in order:
        s = data[data['state'] == state]
        print(f"\n{state}:")
        print(f"  Effective size:  {s['effective_size_bytes'].mean()/1024:.1f} ± {s['effective_size_bytes'].std()/1024:.1f} KB")
        print(f"  Active ratio:    {s['active_ratio'].mean():.1%} ± {s['active_ratio'].std():.1%}")
        print(f"  Total energy:    {s['total_energy'].mean():.4f} ± {s['total_energy'].std():.4f}")
        print(f"  Burst count:     {s['burst_count'].mean():.1f} ± {s['burst_count'].std():.1f}")
    
    # PLOT 1: Effective size comparison (the core of the hypothesis)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    
    sns.boxplot(data=data, x='state', y='effective_size_bytes', order=order,
                palette=palette, ax=axes[0])
    axes[0].set_ylabel('Effective Size (bytes)')
    axes[0].set_title('Effective IQ Data Size per Window\n(Active samples × 8 bytes)')
    
    sns.boxplot(data=data, x='state', y='active_ratio', order=order,
                palette=palette, ax=axes[1])
    axes[1].set_ylabel('Active Ratio')
    axes[1].set_title('Fraction of Samples Above Noise\n(Signal Activity)')
    
    sns.boxplot(data=data, x='state', y='total_energy', order=order,
                palette=palette, ax=axes[2])
    axes[2].set_ylabel('Total Energy')
    axes[2].set_title('Total Signal Energy per Window\n(Sum of |IQ|²)')
    
    fig.suptitle('IQ Data Volume Analysis by Drone State', fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(fig_dir / 'iq_size_comparison.png', dpi=150, bbox_inches='tight')
    print(f"\nSaved: {fig_dir / 'iq_size_comparison.png'}")
    plt.close()
    
    # PLOT 2: All features
    features = ['effective_size_bytes', 'active_ratio', 'total_energy',
                'burst_count', 'mean_power_db', 'bandwidth_hz']
    
    fig, axes = plt.subplots(2, 3, figsize=(16, 10))
    axes = axes.flatten()
    
    for i, feat in enumerate(features):
        sns.violinplot(data=data, x='state', y=feat, order=order,
                      palette=palette, ax=axes[i], inner='quartile')
        axes[i].set_title(feat.replace('_', ' ').title())
        axes[i].set_xlabel('')
    
    fig.suptitle('All IQ Energy Features by Drone State', fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(fig_dir / 'iq_all_features.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {fig_dir / 'iq_all_features.png'}")
    plt.close()
    
    # PLOT 3: Time series of effective size
    fig, ax = plt.subplots(figsize=(14, 5))
    for state in order:
        s = data[data['state'] == state].reset_index(drop=True)
        # Fix for potential pandas/matplotlib indexing issue: convert to numpy values
        x_vals = s.index[:500].values 
        y_vals = s['effective_size_bytes'].values[:500] / 1024
        
        ax.plot(x_vals, y_vals,
                alpha=0.7, label=state, color=palette.get(state, 'gray'))
    
    ax.set_xlabel('Window Number')
    ax.set_ylabel('Effective Size (KB)')
    ax.set_title('Effective IQ Data Size Over Time (first 500 windows)')
    ax.legend()
    plt.tight_layout()
    plt.savefig(fig_dir / 'iq_size_timeseries.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {fig_dir / 'iq_size_timeseries.png'}")
    plt.close()
    
    print(f"\nAll figures saved to {fig_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="IQ Energy Capture & Classification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Live capture (10ms windows, 60 seconds):
    python iq_size_capture.py capture --state idle --duration 60 --window 10
    python iq_size_capture.py capture --state hover --duration 60 --window 10
    python iq_size_capture.py capture --state fly_fast --duration 60 --window 10

  Analyze existing IQ file:
    python iq_size_capture.py analyze --input mavic2_simulation.iq --state fly_fast

  Train model:
    python iq_size_capture.py train --data ./iq_datasets

  Visualize:
    python iq_size_capture.py visualize --data ./iq_datasets
        """
    )
    
    subparsers = parser.add_subparsers(dest='command')
    
    # Capture
    cap = subparsers.add_parser('capture', help='Live capture from USRP')
    cap.add_argument('--state', required=True, help='Drone state label (idle, hover, fly_fast)')
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
