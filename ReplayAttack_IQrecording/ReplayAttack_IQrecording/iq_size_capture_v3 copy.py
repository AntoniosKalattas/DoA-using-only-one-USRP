#!/usr/bin/env python3
"""
IQ Capture V3 — Energy + Amplitude/Phase + Inter-Window Distance Analysis

Extends V2 with inter-window analysis that compares each 10ms window to previous
windows to detect whether the drone is getting closer, farther, or stationary.

Three layers of features:
  V1: Energy/activity metrics (12 features) — "how much data?"
  V2: Amplitude/phase stats + fingerprint (27 scalar + 10 hist + fingerprint) — "what signature?"
    V3: Inter-window deltas & trends (9 features) — "is it moving closer/farther?"

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

# Noise masking config
SNR_MASK_DB = 6.0          # Only keep FFT bins > noise_floor + this (dB)
                           # Higher = more aggressive noise removal
                           # 6 dB = ~4x above noise power

# Path-loss model defaults (used when calibration params are provided)
DEFAULT_PATH_LOSS_EXP = 2.2   # Indoor-ish free-space baseline for 5.8 GHz
DEFAULT_REF_DIST_M = 1.0      # Reference distance in meters
DEFAULT_CALIB_WINDOWS = 30    # Robust median calibration windows for ref power
DEFAULT_MIN_DISTANCE_CONF = 0.35
DEFAULT_HOLD_LAST_RELIABLE = True

DATASET_DIR = Path(__file__).parent / "iq_datasets"


# ============================================================
# Spectral Noise Masking
# ============================================================
def spectral_noise_mask(samples, snr_threshold_db=SNR_MASK_DB):
    """
    Remove noise from IQ samples using spectral masking.

    How it works:
    1. FFT the IQ window into frequency domain
    2. Estimate the noise floor as the median power across all bins
       (median is robust to signal peaks — most bins are noise)
    3. Create a binary mask: keep bins where power > noise_floor + threshold
    4. Zero out all noise-only bins
    5. IFFT back to time domain → cleaned IQ signal

    This preserves the drone signal's amplitude and phase while suppressing
    thermal noise, dramatically improving feature extraction at long range
    where the raw SNR is low.

    Args:
        samples: complex64 IQ array (one 10ms window)
        snr_threshold_db: dB above noise floor to keep (default: 6 dB)

    Returns:
        cleaned_samples: complex64 array, same length, noise suppressed
        mask_ratio: fraction of bins that passed the mask (0-1)
                    Low ratio = mostly noise, high ratio = strong signal
    """
    n = len(samples)
    if n == 0:
        return samples, 0.0

    # 1. Forward FFT
    spectrum = np.fft.fft(samples)
    power_spectrum = np.abs(spectrum) ** 2

    # 2. Estimate noise floor from median (robust to signal peaks)
    # Median is better than mean because signal peaks skew the mean upward.
    # Even a strong drone signal occupies only ~20% of the 10 MHz bandwidth,
    # so the median bin is almost certainly noise-only.
    noise_floor_power = np.median(power_spectrum)
    if noise_floor_power <= 0:
        noise_floor_power = 1e-20

    # 3. Compute threshold in linear power
    threshold_linear = noise_floor_power * (10 ** (snr_threshold_db / 10.0))

    # 4. Create mask: 1 for signal bins, 0 for noise bins
    mask = (power_spectrum > threshold_linear).astype(np.float32)

    # 5. Apply mask in frequency domain and IFFT back
    cleaned_spectrum = spectrum * mask
    cleaned_samples = np.fft.ifft(cleaned_spectrum).astype(np.complex64)

    mask_ratio = float(np.mean(mask))

    return cleaned_samples, mask_ratio


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
    curr_active_ratio = float(current_metrics.get('active_ratio', 0.0))
    curr_burst_count = float(current_metrics.get('burst_count', 0.0))

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

    # --- 8/9. Path-loss based distance estimate with reliability gating ---
    # Log-distance path loss model:
    #   d = d_ref * 10^((P_ref - P_rx)/(10*n))
    # where n is path-loss exponent (2 free space, ~2.2-3.5 indoors).
    path_loss_db = np.nan
    est_distance_m_raw = np.nan
    est_distance_m = np.nan
    distance_mode = 'unreliable'
    snr_margin_db = np.nan
    distance_confidence = 0.0
    distance_reliable = 0

    if distance_cfg is not None and curr_power_db is not None:
        n = float(distance_cfg.get('path_loss_exp', DEFAULT_PATH_LOSS_EXP))
        d_ref = float(distance_cfg.get('ref_dist_m', DEFAULT_REF_DIST_M))
        noise_floor_db = float(distance_cfg.get('noise_floor_db', NOISE_FLOOR_DB))
        min_conf = float(distance_cfg.get('min_distance_conf', DEFAULT_MIN_DISTANCE_CONF))
        hold_last = bool(distance_cfg.get('hold_last_reliable', DEFAULT_HOLD_LAST_RELIABLE))
        calib_windows = int(distance_cfg.get('calib_windows', DEFAULT_CALIB_WINDOWS))
        p_ref = distance_cfg.get('ref_power_db', None)

        # Confidence tracks whether absolute path-loss ranging is meaningful.
        if np.isfinite(curr_power_db):
            snr_margin_db = float(curr_power_db - noise_floor_db)
            snr_score = float(np.clip((snr_margin_db - 1.5) / 8.0, 0.0, 1.0))
            active_score = float(np.clip((curr_active_ratio - 0.01) / 0.08, 0.0, 1.0))
            burst_score = float(np.clip((curr_burst_count - 50.0) / 1500.0, 0.0, 1.0))
            distance_confidence = 0.55 * snr_score + 0.30 * active_score + 0.15 * burst_score
            distance_reliable = 1 if distance_confidence >= min_conf else 0

        # If reference power was not provided, calibrate from median of first N windows.
        if p_ref is None and np.isfinite(curr_power_db):
            samples = distance_cfg.setdefault('ref_power_samples', [])
            if len(samples) < max(1, calib_windows):
                samples.append(float(curr_power_db))
            if len(samples) >= max(1, calib_windows):
                distance_cfg['ref_power_db'] = float(np.median(samples))
                p_ref = distance_cfg['ref_power_db']
                distance_cfg['calibrated'] = True

        if p_ref is not None and np.isfinite(curr_power_db) and n > 0 and d_ref > 0:
            path_loss_db = float(p_ref - curr_power_db)
            est_distance_m_raw = float(d_ref * (10 ** (path_loss_db / (10.0 * n))))

            if distance_reliable:
                est_distance_m = est_distance_m_raw
                distance_mode = 'path_loss'
                distance_cfg['last_reliable_distance'] = est_distance_m_raw
            elif hold_last and np.isfinite(distance_cfg.get('last_reliable_distance', np.nan)):
                est_distance_m = float(distance_cfg['last_reliable_distance'])
                distance_mode = 'hold_last'
            else:
                est_distance_m = np.nan
                distance_mode = 'unreliable'

    return {
        'amp_mean_delta': float(amp_mean_delta),
        'relative_distance': float(relative_distance),
        'fp_cosine_sim': float(fp_cosine_sim),
        'phase_drift_delta': float(phase_drift_delta),
        'amp_trend_short': float(amp_trend_short),
        'amp_trend_long': float(amp_trend_long),
        'amp_ema': float(amp_ema),
        'path_loss_db': float(path_loss_db) if np.isfinite(path_loss_db) else np.nan,
        'est_distance_m_raw': float(est_distance_m_raw) if np.isfinite(est_distance_m_raw) else np.nan,
        'est_distance_m': float(est_distance_m) if np.isfinite(est_distance_m) else np.nan,
        'snr_margin_db': float(snr_margin_db) if np.isfinite(snr_margin_db) else np.nan,
        'distance_confidence': float(distance_confidence),
        'distance_reliable': int(distance_reliable),
        'distance_mode': str(distance_mode),
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
def compute_all_metrics(samples, sample_rate, buffer, distance_cfg=None,
                        noise_mask=True, snr_threshold_db=SNR_MASK_DB):
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
        distance_cfg: optional dict with path-loss model parameters
        noise_mask: if True, apply spectral noise masking before feature extraction
        snr_threshold_db: dB above noise floor for mask threshold (default: 6)

    Returns:
        dict of all features (includes mask_ratio when masking is enabled)
    """
    # Apply spectral noise masking if enabled
    mask_ratio = np.nan
    if noise_mask:
        samples, mask_ratio = spectral_noise_mask(samples, snr_threshold_db)

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
            'active_ratio': energy['active_ratio'],
            'burst_count': energy['burst_count'],
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
    if noise_mask:
        result['mask_ratio'] = float(mask_ratio)

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
                    'amp_ema', 'path_loss_db', 'est_distance_m_raw', 'est_distance_m',
                    'snr_margin_db', 'mask_ratio', 'distance_confidence', 'distance_reliable',
                    'distance_mode']

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
                    'amp_ema', 'path_loss_db', 'est_distance_m_raw', 'est_distance_m',
                    'snr_margin_db', 'mask_ratio', 'distance_confidence', 'distance_reliable']

    return energy + amp_stats + phase_stats + cross + amp_hist + inter_window


# ============================================================
# Live Capture
# ============================================================
def capture_live(state, duration, window_ms, freq, rate, gain, output_dir,
                 path_loss_exp=DEFAULT_PATH_LOSS_EXP,
                 ref_dist_m=DEFAULT_REF_DIST_M,
                 ref_power_db=None,
                 calib_windows=DEFAULT_CALIB_WINDOWS,
                 min_distance_conf=DEFAULT_MIN_DISTANCE_CONF,
                 hold_last_reliable=DEFAULT_HOLD_LAST_RELIABLE,
                 noise_floor_db=NOISE_FLOOR_DB,
                 noise_mask=True,
                 snr_threshold_db=SNR_MASK_DB):
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
    print(f"Path-loss n:    {path_loss_exp:.2f}")
    print(f"Reference dist: {ref_dist_m:.2f} m")
    print(f"Calibration N:  {int(calib_windows)} windows")
    print(f"Min conf:       {min_distance_conf:.2f}")
    print(f"Noise floor:    {noise_floor_db:.1f} dB")
    print(f"Noise mask:     {'ENABLED (SNR threshold: ' + str(snr_threshold_db) + ' dB)' if noise_mask else 'DISABLED'}")
    if ref_power_db is None:
        print("Reference power:auto-calibrate from early-window median")
    else:
        print(f"Reference power:{ref_power_db:.2f} dB")
    print(f"Output:         {output_file}")
    print("=" * 60)

    # Initialize USRP
    usrp = uhd.usrp.MultiUSRP()

    fieldnames = get_fieldnames()

    # V3: Rolling buffer for inter-window analysis
    buffer = WindowBuffer(max_size=TREND_LONG)
    distance_cfg = {
        'path_loss_exp': float(path_loss_exp),
        'ref_dist_m': float(ref_dist_m),
        'ref_power_db': None if ref_power_db is None else float(ref_power_db),
        'calib_windows': int(calib_windows),
        'min_distance_conf': float(min_distance_conf),
        'hold_last_reliable': bool(hold_last_reliable),
        'noise_floor_db': float(noise_floor_db),
        'ref_power_samples': [],
        'last_reliable_distance': np.nan,
        'calibrated': ref_power_db is not None,
    }

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
                    samples[0] if samples.ndim > 1 else samples,
                    rate,
                    buffer,
                    distance_cfg=distance_cfg,
                    noise_mask=noise_mask,
                    snr_threshold_db=snr_threshold_db,
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
                    if np.isfinite(metrics.get('est_distance_m', np.nan)):
                        dist_str += f" est={metrics['est_distance_m']:.2f}m"
                    dist_str += f" conf={metrics.get('distance_confidence', 0.0):.2f}"
                    if noise_mask and 'mask_ratio' in metrics:
                        dist_str += f" mask={metrics['mask_ratio']:.0%}"
                    if metrics.get('distance_reliable', 0) == 0:
                        dist_str += " !"
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
    print(f"  V3 inter-window:  reliability-gated distance features included")

    if distance_cfg.get('ref_power_db') is not None:
        print(f"  Calibration:      ref_power={distance_cfg['ref_power_db']:.2f} dB @ {distance_cfg['ref_dist_m']:.2f} m, n={distance_cfg['path_loss_exp']:.2f}")

    return True


# ============================================================
# Analyze existing IQ file
# ============================================================
def analyze_iq_file(input_file, state, window_ms, rate, output_dir,
                    path_loss_exp=DEFAULT_PATH_LOSS_EXP,
                    ref_dist_m=DEFAULT_REF_DIST_M,
                    ref_power_db=None,
                    calib_windows=DEFAULT_CALIB_WINDOWS,
                    min_distance_conf=DEFAULT_MIN_DISTANCE_CONF,
                    hold_last_reliable=DEFAULT_HOLD_LAST_RELIABLE,
                    noise_floor_db=NOISE_FLOOR_DB,
                    noise_mask=True,
                    snr_threshold_db=SNR_MASK_DB):
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
    print(f"Path-loss n:    {path_loss_exp:.2f}")
    print(f"Reference dist: {ref_dist_m:.2f} m")
    print(f"Calibration N:  {int(calib_windows)} windows")
    print(f"Min conf:       {min_distance_conf:.2f}")
    print(f"Noise floor:    {noise_floor_db:.1f} dB")
    if ref_power_db is None:
        print("Reference power:auto-calibrate from early-window median")
    else:
        print(f"Reference power:{ref_power_db:.2f} dB")
    print("=" * 60)

    fieldnames = get_fieldnames()

    # V3: Rolling buffer
    buffer = WindowBuffer(max_size=TREND_LONG)
    distance_cfg = {
        'path_loss_exp': float(path_loss_exp),
        'ref_dist_m': float(ref_dist_m),
        'ref_power_db': None if ref_power_db is None else float(ref_power_db),
        'calib_windows': int(calib_windows),
        'min_distance_conf': float(min_distance_conf),
        'hold_last_reliable': bool(hold_last_reliable),
        'noise_floor_db': float(noise_floor_db),
        'ref_power_samples': [],
        'last_reliable_distance': np.nan,
        'calibrated': ref_power_db is not None,
    }

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
            metrics = compute_all_metrics(samples, rate, buffer,
                                          distance_cfg=distance_cfg,
                                          noise_mask=noise_mask,
                                          snr_threshold_db=snr_threshold_db)

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
    if distance_cfg.get('ref_power_db') is not None:
        print(f"  Calibration used: ref_power={distance_cfg['ref_power_db']:.2f} dB @ {distance_cfg['ref_dist_m']:.2f} m, n={distance_cfg['path_loss_exp']:.2f}")


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

    if 'distance_reliable' in d.columns:
        reliable = pd.to_numeric(d['distance_reliable'], errors='coerce').fillna(0)
        conf = pd.to_numeric(d.get('distance_confidence', np.nan), errors='coerce')
        print(f"\n--- Distance reliability ---")
        print(f"  Reliable windows:   {int(reliable.sum())}/{len(d)} ({reliable.mean()*100:.1f}%)")
        if conf.notna().any():
            print(f"  Confidence (mean):  {conf.mean():.3f}")
            print(f"  Confidence (p05):   {conf.quantile(0.05):.3f}")

    if 'est_distance_m' in d.columns and d['est_distance_m'].notna().any():
        print(f"\n--- Path-loss distance estimate ---")
        print(f"  Est distance (mean): {d['est_distance_m'].mean():.2f} m")
        print(f"  Est distance (min):  {d['est_distance_m'].min():.2f} m")
        print(f"  Est distance (max):  {d['est_distance_m'].max():.2f} m")
        if 'path_loss_db' in d.columns and d['path_loss_db'].notna().any():
            print(f"  Path loss (mean):    {d['path_loss_db'].mean():.2f} dB")


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
    python iq_size_capture_v3.py capture --state walking_away --duration 30 --window 10 --path-loss-exp 2.4 --ref-dist-m 1.0 --ref-power-db -33.2

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
    cap.add_argument('--path-loss-exp', type=float, default=DEFAULT_PATH_LOSS_EXP,
                     help='Path-loss exponent n for log-distance model (default: 2.2)')
    cap.add_argument('--ref-dist-m', type=float, default=DEFAULT_REF_DIST_M,
                     help='Reference distance in meters (default: 1.0)')
    cap.add_argument('--ref-power-db', type=float, default=None,
                     help='Reference received power at ref distance in dB (default: auto from first window)')
    cap.add_argument('--calib-windows', type=int, default=DEFAULT_CALIB_WINDOWS,
                     help='Windows used for robust median ref-power calibration (default: 30)')
    cap.add_argument('--min-distance-conf', type=float, default=DEFAULT_MIN_DISTANCE_CONF,
                     help='Minimum confidence to trust absolute distance (default: 0.35)')
    cap.add_argument('--noise-floor-db', type=float, default=NOISE_FLOOR_DB,
                     help='Noise floor in dB for confidence scoring (default: -60)')
    cap.add_argument('--noise-mask', action=argparse.BooleanOptionalAction,
                     default=True,
                     help='Enable spectral noise masking (default: enabled). Use --no-noise-mask to disable.')
    cap.add_argument('--snr-threshold-db', type=float, default=SNR_MASK_DB,
                     help=f'SNR threshold for noise mask in dB (default: {SNR_MASK_DB})')
    cap.add_argument('--hold-last-reliable', action=argparse.BooleanOptionalAction,
                     default=DEFAULT_HOLD_LAST_RELIABLE,
                     help='Hold last reliable distance when confidence drops (default: enabled)')

    # Analyze
    ana = subparsers.add_parser('analyze', help='Analyze existing IQ file')
    ana.add_argument('--input', required=True, help='Input .iq file')
    ana.add_argument('--state', required=True, help='State label')
    ana.add_argument('--window', type=float, default=DEFAULT_WINDOW_MS, help='Window size (ms)')
    ana.add_argument('--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    ana.add_argument('--output', default=str(DATASET_DIR), help='Output directory')
    ana.add_argument('--path-loss-exp', type=float, default=DEFAULT_PATH_LOSS_EXP,
                     help='Path-loss exponent n for log-distance model (default: 2.2)')
    ana.add_argument('--ref-dist-m', type=float, default=DEFAULT_REF_DIST_M,
                     help='Reference distance in meters (default: 1.0)')
    ana.add_argument('--ref-power-db', type=float, default=None,
                     help='Reference received power at ref distance in dB (default: auto from first window)')
    ana.add_argument('--calib-windows', type=int, default=DEFAULT_CALIB_WINDOWS,
                     help='Windows used for robust median ref-power calibration (default: 30)')
    ana.add_argument('--min-distance-conf', type=float, default=DEFAULT_MIN_DISTANCE_CONF,
                     help='Minimum confidence to trust absolute distance (default: 0.35)')
    ana.add_argument('--noise-floor-db', type=float, default=NOISE_FLOOR_DB,
                     help='Noise floor in dB for confidence scoring (default: -60)')
    ana.add_argument('--noise-mask', action=argparse.BooleanOptionalAction,
                     default=True,
                     help='Enable spectral noise masking (default: enabled). Use --no-noise-mask to disable.')
    ana.add_argument('--snr-threshold-db', type=float, default=SNR_MASK_DB,
                     help=f'SNR threshold for noise mask in dB (default: {SNR_MASK_DB})')
    ana.add_argument('--hold-last-reliable', action=argparse.BooleanOptionalAction,
                     default=DEFAULT_HOLD_LAST_RELIABLE,
                     help='Hold last reliable distance when confidence drops (default: enabled)')

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
                     args.freq, args.rate, args.gain, args.output,
                     path_loss_exp=args.path_loss_exp,
                     ref_dist_m=args.ref_dist_m,
                     ref_power_db=args.ref_power_db,
                     calib_windows=args.calib_windows,
                     min_distance_conf=args.min_distance_conf,
                     hold_last_reliable=args.hold_last_reliable,
                     noise_floor_db=args.noise_floor_db,
                     noise_mask=args.noise_mask,
                     snr_threshold_db=args.snr_threshold_db)
    elif args.command == 'analyze':
        analyze_iq_file(args.input, args.state, args.window, args.rate, args.output,
                        path_loss_exp=args.path_loss_exp,
                        ref_dist_m=args.ref_dist_m,
                        ref_power_db=args.ref_power_db,
                        calib_windows=args.calib_windows,
                        min_distance_conf=args.min_distance_conf,
                        hold_last_reliable=args.hold_last_reliable,
                        noise_floor_db=args.noise_floor_db,
                        noise_mask=args.noise_mask,
                        snr_threshold_db=args.snr_threshold_db)
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
