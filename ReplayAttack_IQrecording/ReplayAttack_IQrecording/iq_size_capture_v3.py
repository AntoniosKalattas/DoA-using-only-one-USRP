#!/usr/bin/env python3
"""
IQ Capture V3 — Energy + Amplitude/Phase + Inter-Window Distance Analysis

Extends V2 with inter-window analysis that compares each 10ms window to previous
windows to detect whether the drone is getting closer, farther, or stationary.

Three layers of features:
  V1: Energy/activity metrics (12 features) — "how much data?"
  V2: Amplitude/phase stats + fingerprint (27 scalar + 10 hist + fingerprint) — "what signature?"
  V3: Inter-window deltas & trends (7 features) — "is it moving closer/farther?"

New V3 capabilities:
  - Window-to-window amplitude change → approaching vs departing detection
  - Rolling amplitude slope → short/long term distance trend
  - Relative distance ratio from inverse-square law
  - Fingerprint cosine similarity → state stability detection
  - Phase drift acceleration → drone velocity change detection

Usage:
    Capture (same as V2, but adds inter-window features automatically):
        python iq_size_capture_v3.py capture --state idle --duration 60 --window 10
        python iq_size_capture_v3.py capture --state walking_away --duration 30 --window 10

    Analyze existing IQ file:
        python iq_size_capture_v3.py analyze --input recording.iq --state fly_fast --window 10

    Train ML model:
        python iq_size_capture_v3.py train --data ./iq_datasets

    Visualize (includes distance plots):
        python iq_size_capture_v3.py visualize --data ./iq_datasets

    Distance report (post-capture summary):
        python iq_size_capture_v3.py distance --data ./iq_datasets/walking_away_10ms_20260225.csv
"""

import argparse
import numpy as np
import time
import csv
import sys
from pathlib import Path
from datetime import datetime
from collections import deque


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

# Inter-window analysis config
TREND_SHORT = 5            # Short trend window (5 windows = 50ms)
TREND_LONG = 20            # Long trend window (20 windows = 200ms)

# Path-loss model defaults (used when calibration params are provided)
DEFAULT_PATH_LOSS_EXP = 2.2   # Indoor-ish free-space baseline for 5.8 GHz
DEFAULT_REF_DIST_M = 1.0      # Reference distance in meters

DATASET_DIR = Path(__file__).parent / "iq_datasets"


# ============================================================
# V1: Energy/activity metrics
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
# V2: Amplitude & Phase statistical features
# ============================================================
def compute_amp_phase_stats(samples):
    """
    Compute amplitude and phase statistical features from IQ samples.

    Amplitude = |I + jQ| = sqrt(I² + Q²)
    Phase = angle(I + jQ) = atan2(Q, I)

    Returns dict of amplitude/phase summary statistics.
    """
    amplitude = np.abs(samples)
    phase = np.angle(samples)

    # --- Amplitude statistics ---
    amp_mean = float(np.mean(amplitude))
    amp_std = float(np.std(amplitude))
    amp_max = float(np.max(amplitude))
    amp_min = float(np.min(amplitude))
    amp_median = float(np.median(amplitude))
    amp_p95 = float(np.percentile(amplitude, 95))
    amp_p05 = float(np.percentile(amplitude, 5))
    amp_dynamic_range = float(amp_p95 / (amp_p05 + 1e-20))
    amp_rms = float(np.sqrt(np.mean(amplitude**2)))
    amp_crest_factor = float(amp_max / (amp_rms + 1e-20))

    hist_counts, _ = np.histogram(amplitude, bins=AMP_HIST_BINS, range=(0, amp_max + 1e-20))
    hist_norm = hist_counts / (np.sum(hist_counts) + 1e-20)

    # --- Phase statistics ---
    phase_mean = float(np.mean(phase))
    phase_std = float(np.std(phase))
    phase_circ_mean = float(np.arctan2(np.mean(np.sin(phase)), np.mean(np.cos(phase))))
    phase_concentration = float(np.abs(np.mean(np.exp(1j * phase))))

    # Phase drift (Doppler indicator)
    phase_unwrapped = np.unwrap(phase)
    n = len(phase)
    t = np.arange(n, dtype=np.float64)
    t_mean = np.mean(t)
    phase_mean_uw = np.mean(phase_unwrapped)
    phase_drift = float(np.sum((t - t_mean) * (phase_unwrapped - phase_mean_uw)) /
                        (np.sum((t - t_mean)**2) + 1e-20))

    # Phase entropy
    phase_hist, _ = np.histogram(phase, bins=36, range=(-np.pi, np.pi))
    phase_probs = phase_hist / (np.sum(phase_hist) + 1e-20)
    phase_entropy = float(-np.sum(phase_probs * np.log2(phase_probs + 1e-20)))

    # --- Cross features ---
    phase_diff = np.diff(phase_unwrapped)
    if len(phase_diff) > 0 and len(amplitude[:-1]) > 0:
        amp_phase_corr = float(np.corrcoef(amplitude[:-1], np.abs(phase_diff))[0, 1])
        if np.isnan(amp_phase_corr):
            amp_phase_corr = 0.0
    else:
        amp_phase_corr = 0.0

    result = {
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
        'phase_mean': phase_mean,
        'phase_std': phase_std,
        'phase_circ_mean': phase_circ_mean,
        'phase_concentration': phase_concentration,
        'phase_drift': phase_drift,
        'phase_entropy': phase_entropy,
        'amp_phase_corr': amp_phase_corr,
    }

    for i, val in enumerate(hist_norm):
        result[f'amp_hist_{i}'] = float(val)

    return result


# ============================================================
# V2: Compressed Fingerprint Vector
# ============================================================
def compute_fingerprint(samples, n_bins=FINGERPRINT_BINS):
    """
    Create compressed amplitude/phase fingerprint by downsampling
    100,000 samples to n_bins points. Each bin = average amp/phase of a chunk.

    Returns:
        amp_fp_array: numpy array of n_bins amplitude values
        phase_fp_array: numpy array of n_bins phase values
        amp_fp_str: comma-separated string for CSV
        phase_fp_str: comma-separated string for CSV
    """
    amplitude = np.abs(samples)
    phase_unwrapped = np.unwrap(np.angle(samples))

    chunk_size = max(1, len(samples) // n_bins)

    amp_bins = np.zeros(n_bins)
    phase_bins = np.zeros(n_bins)

    for i in range(n_bins):
        start = i * chunk_size
        end = min(start + chunk_size, len(samples))
        if start < len(samples):
            amp_bins[i] = np.mean(amplitude[start:end])
            phase_bins[i] = np.mean(phase_unwrapped[start:end])

    amp_fp_str = ','.join(f'{v:.6f}' for v in amp_bins)
    phase_fp_str = ','.join(f'{v:.6f}' for v in phase_bins)

    return amp_bins, phase_bins, amp_fp_str, phase_fp_str


# ============================================================
# V3 NEW: Inter-Window Analysis (distance/movement detection)
# ============================================================
class WindowBuffer:
    """
    Rolling buffer that stores recent window metrics for inter-window analysis.
    Keeps the last TREND_LONG windows of amp_mean, phase_drift, and fingerprint vectors.
    """

    def __init__(self, max_size=TREND_LONG):
        self.max_size = max_size
        self.amp_means = deque(maxlen=max_size)
        self.phase_drifts = deque(maxlen=max_size)
        self.amp_fingerprints = deque(maxlen=max_size)  # numpy arrays, not strings

    def push(self, amp_mean, phase_drift, amp_fp_array):
        """Add current window's data to the buffer."""
        self.amp_means.append(amp_mean)
        self.phase_drifts.append(phase_drift)
        self.amp_fingerprints.append(amp_fp_array.copy())

    @property
    def count(self):
        return len(self.amp_means)

    @property
    def has_previous(self):
        return self.count >= 1

    @property
    def prev_amp_mean(self):
        return self.amp_means[-1] if self.has_previous else None

    @property
    def prev_phase_drift(self):
        return self.phase_drifts[-1] if self.has_previous else None

    @property
    def prev_amp_fingerprint(self):
        return self.amp_fingerprints[-1] if self.has_previous else None


def compute_inter_window_metrics(current_metrics, amp_fp_array, buffer, distance_cfg=None):
    """
    V3: Compare current window to previous windows for distance/movement analysis.

    Uses the rolling buffer to compute:
      1. amp_mean_delta     — change in mean amplitude vs previous window
                              Positive = drone getting closer (stronger signal)
                              Negative = drone getting farther (weaker signal)
      2. relative_distance  — distance ratio estimate: sqrt(A_prev / A_curr)
                              >1 = closer than before, <1 = farther, ~1 = same
      3. fp_cosine_sim      — cosine similarity of fingerprint vectors between
                              current and previous window (1.0 = identical pattern,
                              <0.9 = pattern changing = state/distance shift)
      4. phase_drift_delta  — change in phase drift between windows
                              Nonzero = drone accelerating/decelerating (Doppler change)
      5. amp_trend_short    — linear slope of amp_mean over last 5 windows (50ms)
                              Positive slope = approaching, negative = departing
      6. amp_trend_long     — linear slope of amp_mean over last 20 windows (200ms)
                              More stable trend, less noise
      7. amp_ema            — exponential moving average of amp_mean (α=0.2)
                              Smoothed amplitude for distance tracking over time
    8. path_loss_db       — path loss relative to reference power (dB)
    9. est_distance_m     — estimated absolute distance from log-distance model

    Args:
        current_metrics: dict with at least 'amp_mean' and 'phase_drift'
        amp_fp_array: numpy array of current fingerprint (128 bins)
        buffer: WindowBuffer with previous windows' data

    Returns:
        dict of 7 inter-window features
    """
    curr_amp = current_metrics['amp_mean']
    curr_drift = current_metrics['phase_drift']
    curr_power_db = current_metrics.get('mean_power_db', None)

    # --- 1. Amplitude delta (window-to-window change) ---
    if buffer.has_previous:
        amp_mean_delta = curr_amp - buffer.prev_amp_mean
    else:
        amp_mean_delta = 0.0

    # --- 2. Relative distance ratio (inverse-square law) ---
    # P ∝ 1/d², so d ∝ 1/√P, thus d2/d1 = √(P1/P2) = √(amp_prev/amp_curr)
    if buffer.has_previous and curr_amp > 1e-20 and buffer.prev_amp_mean > 1e-20:
        relative_distance = float(np.sqrt(buffer.prev_amp_mean / curr_amp))
    else:
        relative_distance = 1.0  # No change / first window

    # --- 3. Fingerprint cosine similarity ---
    if buffer.has_previous and buffer.prev_amp_fingerprint is not None:
        prev_fp = buffer.prev_amp_fingerprint
        # Cosine similarity = dot(A,B) / (||A|| * ||B||)
        dot = np.dot(amp_fp_array, prev_fp)
        norm_curr = np.linalg.norm(amp_fp_array)
        norm_prev = np.linalg.norm(prev_fp)
        if norm_curr > 1e-20 and norm_prev > 1e-20:
            fp_cosine_sim = float(dot / (norm_curr * norm_prev))
        else:
            fp_cosine_sim = 0.0
    else:
        fp_cosine_sim = 1.0  # First window = "same as self"

    # --- 4. Phase drift delta (Doppler acceleration) ---
    if buffer.has_previous:
        phase_drift_delta = curr_drift - buffer.prev_phase_drift
    else:
        phase_drift_delta = 0.0

    # --- 5. Short-term amplitude trend (last 5 windows = 50ms) ---
    amp_trend_short = _compute_slope(buffer.amp_means, TREND_SHORT)

    # --- 6. Long-term amplitude trend (last 20 windows = 200ms) ---
    amp_trend_long = _compute_slope(buffer.amp_means, TREND_LONG)

    # --- 7. Exponential moving average of amplitude ---
    alpha = 0.2
    if buffer.count >= 2:
        # EMA = α * current + (1-α) * previous_EMA
        # We approximate by using the last stored amp_mean as previous EMA
        # (true EMA would need storing the EMA itself, but this is close enough)
        prev_vals = list(buffer.amp_means)
        ema = prev_vals[0]
        for v in prev_vals[1:]:
            ema = alpha * v + (1 - alpha) * ema
        amp_ema = float(alpha * curr_amp + (1 - alpha) * ema)
    else:
        amp_ema = float(curr_amp)

    # --- 8/9. Path-loss based distance estimate (optional calibrated model) ---
    # Log-distance path loss model:
    #   d = d_ref * 10^((P_ref - P_rx)/(10*n))
    # where n is path-loss exponent (2 free space, ~2.2-3.5 indoors).
    path_loss_db = np.nan
    est_distance_m = np.nan
    if distance_cfg is not None and curr_power_db is not None:
        n = float(distance_cfg.get('path_loss_exp', DEFAULT_PATH_LOSS_EXP))
        d_ref = float(distance_cfg.get('ref_dist_m', DEFAULT_REF_DIST_M))
        p_ref = distance_cfg.get('ref_power_db', None)

        # If reference power was not provided, auto-calibrate from first valid window.
        if p_ref is None and np.isfinite(curr_power_db):
            distance_cfg['ref_power_db'] = float(curr_power_db)
            p_ref = distance_cfg['ref_power_db']

        if p_ref is not None and np.isfinite(curr_power_db) and n > 0 and d_ref > 0:
            path_loss_db = float(p_ref - curr_power_db)
            est_distance_m = float(d_ref * (10 ** (path_loss_db / (10.0 * n))))

    return {
        'amp_mean_delta': float(amp_mean_delta),
        'relative_distance': float(relative_distance),
        'fp_cosine_sim': float(fp_cosine_sim),
        'phase_drift_delta': float(phase_drift_delta),
        'amp_trend_short': float(amp_trend_short),
        'amp_trend_long': float(amp_trend_long),
        'amp_ema': float(amp_ema),
        'path_loss_db': float(path_loss_db) if np.isfinite(path_loss_db) else np.nan,
        'est_distance_m': float(est_distance_m) if np.isfinite(est_distance_m) else np.nan,
    }


def _compute_slope(values_deque, window_size):
    """
    Compute linear regression slope over the last window_size values.
    Returns 0 if not enough data.
    """
    vals = list(values_deque)
    n = min(len(vals), window_size)
    if n < 2:
        return 0.0

    y = np.array(vals[-n:], dtype=np.float64)
    x = np.arange(n, dtype=np.float64)
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    denominator = np.sum((x - x_mean)**2)
    if denominator < 1e-20:
        return 0.0
    slope = np.sum((x - x_mean) * (y - y_mean)) / denominator
    return float(slope)


# ============================================================
# Combined metrics: energy + amp/phase stats + fingerprint + inter-window
# ============================================================
def compute_all_metrics(samples, sample_rate, buffer, distance_cfg=None):
    """
    Compute ALL metrics for a single window:
      V1: Energy/activity metrics (12 features)
      V2: Amplitude & phase statistics (27 scalar + 10 histogram)
      V2: Compressed fingerprint vectors
      V3: Inter-window distance/movement features (7 features)

    Also updates the rolling buffer with current window data.

    Args:
        samples: complex64 numpy array
        sample_rate: Hz
        buffer: WindowBuffer instance (mutated — current window added after computation)

    Returns:
        dict of all features
    """
    # V1
    energy = compute_energy_metrics(samples, sample_rate)

    # V2 amp/phase stats
    amp_phase = compute_amp_phase_stats(samples)

    # V2 fingerprint (returns arrays AND strings)
    amp_fp_arr, phase_fp_arr, amp_fp_str, phase_fp_str = compute_fingerprint(samples)

    # V3 inter-window (uses buffer BEFORE updating it)
    inter = compute_inter_window_metrics(
        current_metrics={
            'amp_mean': amp_phase['amp_mean'],
            'phase_drift': amp_phase['phase_drift'],
            'mean_power_db': energy['mean_power_db'],
        },
        amp_fp_array=amp_fp_arr,
        buffer=buffer,
        distance_cfg=distance_cfg,
    )

    # Update buffer AFTER computing deltas (so next window can reference this one)
    buffer.push(amp_phase['amp_mean'], amp_phase['phase_drift'], amp_fp_arr)

    # Merge everything
    result = {}
    result.update(energy)
    result.update(amp_phase)
    result['amp_fingerprint'] = amp_fp_str
    result['phase_fingerprint'] = phase_fp_str
    result.update(inter)

    return result


# ============================================================
# CSV field names
# ============================================================
def get_fieldnames():
    """Return all CSV column names in order."""
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

    # V2 fingerprint vectors
    fingerprints = ['amp_fingerprint', 'phase_fingerprint']

    # V3 inter-window features
    inter_window = ['amp_mean_delta', 'relative_distance', 'fp_cosine_sim',
                    'phase_drift_delta', 'amp_trend_short', 'amp_trend_long',
                    'amp_ema', 'path_loss_db', 'est_distance_m']

    return meta + energy + amp_stats + phase_stats + cross + amp_hist + fingerprints + inter_window


def get_feature_columns():
    """Return feature columns for ML training (excludes metadata, fingerprint strings)."""
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

    # V3 inter-window (all numeric, good for ML)
    inter_window = ['amp_mean_delta', 'relative_distance', 'fp_cosine_sim',
                    'phase_drift_delta', 'amp_trend_short', 'amp_trend_long',
                    'amp_ema', 'path_loss_db', 'est_distance_m']

    return energy + amp_stats + phase_stats + cross + amp_hist + inter_window


# ============================================================
# Live Capture
# ============================================================
def capture_live(state, duration, window_ms, freq, rate, gain, output_dir):
    """
    Capture IQ windows from USRP, compute all metrics including inter-window,
    discard raw IQ.
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
    print("IQ CAPTURE V3 — Energy + Amp/Phase + Distance Analysis")
    print("=" * 60)
    print(f"State:          {state}")
    print(f"Frequency:      {freq/1e9:.3f} GHz")
    print(f"Sample Rate:    {rate/1e6:.1f} MHz")
    print(f"Gain:           {gain} dB")
    print(f"Window Size:    {window_ms} ms ({samples_per_window:,} samples)")
    print(f"Duration:       {duration} seconds")
    print(f"Total Windows:  {total_windows:,}")
    print(f"Fingerprint:    {FINGERPRINT_BINS} bins per window")
    print(f"Trend windows:  short={TREND_SHORT} ({TREND_SHORT*window_ms}ms), long={TREND_LONG} ({TREND_LONG*window_ms}ms)")
    print(f"Output:         {output_file}")
    print("=" * 60)

    # Initialize USRP
    usrp = uhd.usrp.MultiUSRP()

    fieldnames = get_fieldnames()

    # V3: Rolling buffer for inter-window analysis
    buffer = WindowBuffer(max_size=TREND_LONG)

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

                # V3: pass buffer for inter-window analysis
                metrics = compute_all_metrics(
                    samples[0] if samples.ndim > 1 else samples, rate, buffer
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
                    # V3: show distance info in progress
                    dist_str = f"dist={metrics['relative_distance']:.3f}" if buffer.count > 1 else "dist=---"
                    print(f"\r  [{bar}] {pct:5.1%} | "
                          f"Win {window_count}/{total_windows} | "
                          f"{int(elapsed//60)}m{int(elapsed%60):02d}s | "
                          f"ETA ~{eta_min}m{eta_s:02d}s | "
                          f"active={metrics['active_ratio']:.1%} "
                          f"amp={metrics['amp_mean']:.4f} {dist_str}",
                          end='', flush=True)

    except KeyboardInterrupt:
        print(f"\n\nStopped early after {window_count} windows")

    print()
    elapsed = time.time() - start_time
    n_features = len(get_feature_columns())
    print(f"\n✓ Capture complete!")
    print(f"  Windows recorded: {window_count}")
    print(f"  Time elapsed:     {elapsed:.1f}s")
    print(f"  Saved to:         {output_file}")
    print(f"  File size:        {output_file.stat().st_size / 1024:.1f} KB")
    print(f"  Features/window:  {n_features} stats + {FINGERPRINT_BINS}-bin fingerprint")
    print(f"  V3 inter-window:  7 distance/movement features included")

    return True


# ============================================================
# Analyze existing IQ file
# ============================================================
def analyze_iq_file(input_file, state, window_ms, rate, output_dir):
    """
    Process an existing .iq file into windowed metrics with inter-window analysis.
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
    print("IQ FILE ANALYSIS V3 — Energy + Amp/Phase + Distance")
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

    # V3: Rolling buffer
    buffer = WindowBuffer(max_size=TREND_LONG)

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

            # V3: pass buffer
            metrics = compute_all_metrics(samples, rate, buffer)

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
                      f"{int(elapsed//60)}m{int(elapsed%60):02d}s | "
                      f"ETA ~{eta_min}m{eta_s:02d}s",
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
    """Train ML model on collected datasets with all V3 features"""
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
    print("ML TRAINING V3 — Energy + Amp/Phase + Distance Features")
    print("=" * 60)

    all_data = []
    for f in csv_files:
        df = pd.read_csv(f)
        print(f"  Loaded {f.name}: {len(df)} windows, state='{df['state'].iloc[0]}'")
        all_data.append(df)

    data = pd.concat(all_data, ignore_index=True)
    print(f"\nTotal samples: {len(data)}")
    print(f"Class distribution:\n{data['state'].value_counts().to_string()}")

    # Auto-detect available features (backward compatible with V1/V2 CSVs)
    all_feature_cols = get_feature_columns()
    feature_cols = [c for c in all_feature_cols if c in data.columns]

    missing = set(all_feature_cols) - set(feature_cols)
    if missing:
        print(f"\n⚠ Missing features ({len(missing)} skipped — older CSV format?)")
        for m in sorted(missing):
            print(f"    - {m}")

    print(f"\nUsing {len(feature_cols)} features for training")

    # Show which feature groups are present
    v1_cols = [c for c in feature_cols if c in ['total_energy', 'mean_power_db', 'max_power_db',
               'active_samples', 'active_ratio', 'active_energy', 'active_mean_db',
               'burst_count', 'duty_cycle', 'power_std_db', 'bandwidth_hz', 'effective_size_bytes']]
    v2_cols = [c for c in feature_cols if c.startswith(('amp_', 'phase_')) and c not in
               ['amp_mean_delta', 'amp_trend_short', 'amp_trend_long', 'amp_ema']]
    v3_cols = [c for c in feature_cols if c in ['amp_mean_delta', 'relative_distance',
               'fp_cosine_sim', 'phase_drift_delta', 'amp_trend_short', 'amp_trend_long', 'amp_ema']]
    print(f"  V1 energy:       {len(v1_cols)} features")
    print(f"  V2 amp/phase:    {len(v2_cols)} features")
    print(f"  V3 inter-window: {len(v3_cols)} features")

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

    # Feature importance (top 25)
    print("Feature Importance (top 25):")
    importances = rf.feature_importances_
    top_idx = np.argsort(importances)[::-1][:25]
    for rank, idx in enumerate(top_idx, 1):
        # Tag which version the feature comes from
        fname = feature_cols[idx]
        if fname in [c for c in v3_cols]:
            tag = "[V3]"
        elif fname in [c for c in v2_cols]:
            tag = "[V2]"
        else:
            tag = "[V1]"
        print(f"  {rank:2d}. {fname:30s} {importances[idx]:.4f}  {tag}")

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
    model_dir = data_dir / "models_v3"
    model_dir.mkdir(exist_ok=True)
    with open(model_dir / "rf_model.pkl", 'wb') as f:
        pickle.dump(rf, f)
    with open(model_dir / "scaler.pkl", 'wb') as f:
        pickle.dump(scaler, f)
    with open(model_dir / "label_encoder.pkl", 'wb') as f:
        pickle.dump(le, f)
    with open(model_dir / "feature_columns.json", 'w') as f:
        json.dump(feature_cols, f)

    # Save evaluation results
    eval_results = {
        'accuracy': float(acc_rf),
        'n_features': len(feature_cols),
        'n_samples': len(data),
        'classes': list(le.classes_),
        'feature_importance': {feature_cols[i]: float(importances[i]) for i in top_idx},
        'v1_features': len(v1_cols),
        'v2_features': len(v2_cols),
        'v3_features': len(v3_cols),
    }
    with open(model_dir / "evaluation_results.json", 'w') as f:
        json.dump(eval_results, f, indent=2)

    print(f"\nModel saved to {model_dir}")


# ============================================================
# Visualization
# ============================================================
def visualize(data_dir):
    """Visualize datasets including V3 distance/movement features"""
    import pandas as pd
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_style("whitegrid")
    data_dir = Path(data_dir)
    fig_dir = data_dir / "figures_v3"
    fig_dir.mkdir(exist_ok=True)

    all_data = []
    for f in data_dir.glob("*.csv"):
        df = pd.read_csv(f)
        all_data.append(df)

    if not all_data:
        print(f"No CSV files found in {data_dir}")
        return

    data = pd.concat(all_data, ignore_index=True)

    order = sorted(data['state'].unique())
    default_colors = ['#3498db', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6',
                      '#1abc9c', '#e67e22', '#34495e']
    palette = {state: default_colors[i % len(default_colors)] for i, state in enumerate(order)}

    has_v2 = 'amp_mean' in data.columns
    has_v3 = 'amp_mean_delta' in data.columns

    print("=" * 60)
    print("VISUALIZATION V3 — Energy + Amp/Phase + Distance Analysis")
    print("=" * 60)

    for state in order:
        s = data[data['state'] == state]
        print(f"\n{state} ({len(s)} windows):")
        print(f"  Effective size:  {s['effective_size_bytes'].mean()/1024:.1f} ± {s['effective_size_bytes'].std()/1024:.1f} KB")
        print(f"  Active ratio:    {s['active_ratio'].mean():.1%} ± {s['active_ratio'].std():.1%}")
        if has_v2:
            print(f"  Amp mean:        {s['amp_mean'].mean():.6f} ± {s['amp_mean'].std():.6f}")
            print(f"  Phase drift:     {s['phase_drift'].mean():.6f} ± {s['phase_drift'].std():.6f}")
            print(f"  Phase entropy:   {s['phase_entropy'].mean():.2f} ± {s['phase_entropy'].std():.2f}")
        if has_v3:
            print(f"  Amp delta (avg): {s['amp_mean_delta'].mean():.8f} (+ = closer, - = farther)")
            print(f"  Rel distance:    {s['relative_distance'].mean():.4f} ± {s['relative_distance'].std():.4f}")
            print(f"  FP cosine sim:   {s['fp_cosine_sim'].mean():.4f} ± {s['fp_cosine_sim'].std():.4f}")
            print(f"  Amp trend (S):   {s['amp_trend_short'].mean():.8f}")
            print(f"  Amp trend (L):   {s['amp_trend_long'].mean():.8f}")

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

    fig.suptitle('V1: IQ Data Volume Analysis by Drone State', fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(fig_dir / 'v1_size_comparison.png', dpi=150, bbox_inches='tight')
    print(f"\nSaved: {fig_dir / 'v1_size_comparison.png'}")
    plt.close()

    # ---- PLOT 2: V2 Amplitude/Phase features ----
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

        fig.suptitle('V2: Amplitude & Phase Signature Features', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(fig_dir / 'v2_amp_phase_features.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'v2_amp_phase_features.png'}")
        plt.close()

    # ---- PLOT 3: V3 Distance/Movement features ----
    if has_v3:
        fig, axes = plt.subplots(2, 3, figsize=(16, 10))
        axes = axes.flatten()
        v3_features = ['amp_mean_delta', 'relative_distance', 'fp_cosine_sim',
                       'phase_drift_delta', 'amp_trend_short', 'amp_trend_long']

        for i, feat in enumerate(v3_features):
            if feat in data.columns:
                sns.violinplot(data=data, x='state', y=feat, order=order,
                              hue='state', palette=palette, ax=axes[i],
                              inner='quartile', legend=False)
                axes[i].set_title(feat.replace('_', ' ').title())
                axes[i].set_xlabel('')

        fig.suptitle('V3: Inter-Window Distance & Movement Features', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(fig_dir / 'v3_distance_features.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'v3_distance_features.png'}")
        plt.close()

    # ---- PLOT 4: Distance tracking over time (the key V3 plot) ----
    if has_v3:
        fig, axes = plt.subplots(4, 1, figsize=(16, 14), sharex=True)

        for state in order:
            s = data[data['state'] == state].reset_index(drop=True)
            x = s.index.values
            c = palette.get(state, 'gray')

            # 4a: Smoothed amplitude (EMA) — distance proxy
            axes[0].plot(x, s['amp_ema'].values, alpha=0.8, label=state, color=c, linewidth=1)

            # 4b: Amplitude delta — direction of movement
            axes[1].plot(x, s['amp_mean_delta'].values, alpha=0.5, label=state, color=c, linewidth=0.7)

            # 4c: Relative distance ratio
            axes[2].plot(x, s['relative_distance'].values, alpha=0.6, label=state, color=c, linewidth=0.7)

            # 4d: Fingerprint similarity — state stability
            axes[3].plot(x, s['fp_cosine_sim'].values, alpha=0.6, label=state, color=c, linewidth=0.7)

        axes[0].set_ylabel('Amp EMA')
        axes[0].set_title('Smoothed Amplitude Over Time (↑ = closer, ↓ = farther)')
        axes[0].legend(loc='upper right', fontsize=8)

        axes[1].set_ylabel('Amp Delta')
        axes[1].set_title('Window-to-Window Amplitude Change (+ = approaching, - = departing)')
        axes[1].axhline(y=0, color='black', linewidth=0.5, linestyle='--')
        axes[1].legend(loc='upper right', fontsize=8)

        axes[2].set_ylabel('Rel. Distance')
        axes[2].set_title('Relative Distance Ratio (>1 = getting closer, <1 = getting farther)')
        axes[2].axhline(y=1.0, color='black', linewidth=0.5, linestyle='--')
        axes[2].legend(loc='upper right', fontsize=8)

        axes[3].set_ylabel('Cosine Sim')
        axes[3].set_xlabel('Window Number (10ms each)')
        axes[3].set_title('Fingerprint Cosine Similarity (1.0 = stable, dropping = changing)')
        axes[3].legend(loc='upper right', fontsize=8)

        fig.suptitle('V3: Distance & Movement Tracking Over Time', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(fig_dir / 'v3_distance_tracking.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'v3_distance_tracking.png'}")
        plt.close()

    # ---- PLOT 5: Fingerprint heatmap ----
    if has_v2 and 'amp_fingerprint' in data.columns:
        fig, axes = plt.subplots(len(order), 1, figsize=(14, 4 * len(order)))
        if len(order) == 1:
            axes = [axes]

        for ax_i, state in enumerate(order):
            s = data[data['state'] == state]
            fp_matrix = []
            for fp_str in s['amp_fingerprint'].values[:100]:
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
        plt.savefig(fig_dir / 'v2_fingerprint_heatmap.png', dpi=150, bbox_inches='tight')
        print(f"Saved: {fig_dir / 'v2_fingerprint_heatmap.png'}")
        plt.close()

    # ---- PLOT 6: Effective size time series ----
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
    plt.savefig(fig_dir / 'v1_size_timeseries.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {fig_dir / 'v1_size_timeseries.png'}")
    plt.close()

    print(f"\nAll figures saved to {fig_dir}")


# ============================================================
# V3 NEW: Distance Report (post-capture analysis)
# ============================================================
def distance_report(csv_file):
    """
    Analyze a single capture CSV and print a distance/movement summary.
    Useful after capturing walking_away or walking_closer states.
    """
    import pandas as pd

    csv_file = Path(csv_file)
    if not csv_file.exists():
        print(f"File not found: {csv_file}")
        return

    df = pd.read_csv(csv_file)
    state = df['state'].iloc[0]
    n = len(df)
    duration_s = n * df['window_ms'].iloc[0] / 1000

    print("=" * 60)
    print("V3 DISTANCE REPORT")
    print("=" * 60)
    print(f"File:       {csv_file.name}")
    print(f"State:      {state}")
    print(f"Windows:    {n}")
    print(f"Duration:   {duration_s:.1f}s")

    if 'amp_mean_delta' not in df.columns:
        print("\n⚠ No V3 inter-window features found. Capture with V3 tool first.")
        return

    # Skip first window (no previous to compare)
    d = df.iloc[1:]

    print(f"\n--- Amplitude (distance proxy) ---")
    print(f"  First window amp:   {df['amp_mean'].iloc[0]:.6f}")
    print(f"  Last window amp:    {df['amp_mean'].iloc[-1]:.6f}")
    amp_change = df['amp_mean'].iloc[-1] - df['amp_mean'].iloc[0]
    amp_pct = (amp_change / (df['amp_mean'].iloc[0] + 1e-20)) * 100
    print(f"  Total change:       {amp_change:+.6f} ({amp_pct:+.1f}%)")

    if amp_change > 0:
        print(f"  Direction:          🟢 APPROACHING (amplitude increased)")
    elif amp_change < 0:
        print(f"  Direction:          🔴 DEPARTING (amplitude decreased)")
    else:
        print(f"  Direction:          ⚪ STATIONARY")

    # Inverse-square distance estimate
    if df['amp_mean'].iloc[0] > 1e-20 and df['amp_mean'].iloc[-1] > 1e-20:
        dist_ratio = np.sqrt(df['amp_mean'].iloc[0] / df['amp_mean'].iloc[-1])
        print(f"  Distance ratio:     {dist_ratio:.3f}x (end vs start)")
        if dist_ratio > 1:
            print(f"    → Drone is ~{dist_ratio:.1f}x FARTHER at end than start")
        elif dist_ratio < 1:
            print(f"    → Drone is ~{1/dist_ratio:.1f}x CLOSER at end than start")

    print(f"\n--- Movement detection ---")
    approaching = (d['amp_mean_delta'] > 0).sum()
    departing = (d['amp_mean_delta'] < 0).sum()
    print(f"  Windows approaching: {approaching}/{len(d)} ({approaching/len(d)*100:.1f}%)")
    print(f"  Windows departing:   {departing}/{len(d)} ({departing/len(d)*100:.1f}%)")

    print(f"\n--- Stability ---")
    print(f"  FP cosine sim (avg):  {d['fp_cosine_sim'].mean():.4f}")
    print(f"  FP cosine sim (min):  {d['fp_cosine_sim'].min():.4f}")
    if d['fp_cosine_sim'].mean() > 0.95:
        print(f"  → Signal pattern is STABLE (same state throughout)")
    elif d['fp_cosine_sim'].mean() > 0.80:
        print(f"  → Signal pattern is CHANGING (distance or state shift)")
    else:
        print(f"  → Signal pattern is VERY DIFFERENT between windows")

    print(f"\n--- Phase drift (Doppler) ---")
    print(f"  Phase drift (avg):      {d['phase_drift'].mean():.6f} rad/sample")
    print(f"  Phase drift delta (avg): {d['phase_drift_delta'].mean():.6f}")
    if abs(d['phase_drift'].mean()) > 0.001:
        print(f"  → Doppler detected: drone is MOVING")
    else:
        print(f"  → No significant Doppler: drone is approximately STATIONARY")

    print(f"\n--- Amplitude trends ---")
    print(f"  Short trend (50ms avg):  {d['amp_trend_short'].mean():.8f}")
    print(f"  Long trend (200ms avg):  {d['amp_trend_long'].mean():.8f}")
    print(f"  EMA range: {d['amp_ema'].min():.6f} → {d['amp_ema'].max():.6f}")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="IQ Capture V3 — Energy + Amp/Phase + Inter-Window Distance Analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Capture stationary states:
    python iq_size_capture_v3.py capture --state no_drone --duration 15 --window 10
    python iq_size_capture_v3.py capture --state idle_no_camera_move --duration 15 --window 10 --freq 5.845e9
    python iq_size_capture_v3.py capture --state idle_camera_move --duration 15 --window 10 --freq 5.845e9

  Capture distance states (walk the drone toward/away from USRP):
    python iq_size_capture_v3.py capture --state stationary_close --duration 15 --window 10 --freq 5.845e9
    python iq_size_capture_v3.py capture --state stationary_far --duration 15 --window 10 --freq 5.845e9
    python iq_size_capture_v3.py capture --state walking_away --duration 30 --window 10 --freq 5.845e9
    python iq_size_capture_v3.py capture --state walking_closer --duration 30 --window 10 --freq 5.845e9

  Analyze existing IQ file:
    python iq_size_capture_v3.py analyze --input recording.iq --state fly_fast

  Train model (backward compatible with V1/V2 CSVs):
    python iq_size_capture_v3.py train --data ./iq_datasets

  Visualize:
    python iq_size_capture_v3.py visualize --data ./iq_datasets

  Distance report (after capture):
    python iq_size_capture_v3.py distance --csv walking_away_10ms_20260225.csv
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

    # Distance report
    dist = subparsers.add_parser('distance', help='Distance/movement report for a capture')
    dist.add_argument('--csv', required=True, help='CSV file to analyze')

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
    elif args.command == 'distance':
        distance_report(args.csv)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
