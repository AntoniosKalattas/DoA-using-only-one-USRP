# IQ Capture V3 — Complete Feature Documentation

## Overview

This document describes every variable collected and computed by `iq_size_capture_v3.py` for drone RF traffic analysis using a USRP Software Defined Radio.

**Total: 46 ML features + 2 fingerprint vectors (128 values each)**

---

## PART A — Raw Data From USRP

The USRP gives us exactly **one thing**: a stream of complex IQ samples.

Each sample is a complex number:

    z_k = I_k + j * Q_k

where I is the in-phase component and Q is the quadrature component, sampled at 10 MHz (10,000,000 samples per second).

Every 10ms capture window contains **100,000 complex samples**. That is the only raw data. Everything below is computed from these samples.

---

## PART B — V1: Energy/Activity Features (12 variables)

**Purpose:** Answer "How much RF energy is present? Is a drone transmitting?"

These are computed from the power of each sample: P_k = |z_k|^2 = I_k^2 + Q_k^2

### 1. total_energy

    E = SUM( |z_k|^2 ) for k = 1 to N

Total power across all 100,000 samples. High value = drone transmitting, low value = silence.

### 2. mean_power_db

    P_mean_db = 10 * log10( (1/N) * SUM( |z_k|^2 ) )

Average power in decibels. Human-readable power level. Allows comparison across different gain settings.

### 3. max_power_db

    P_max_db = 10 * log10( max( |z_k|^2 ) )

Loudest single sample in dB. Detects peak burst power — even a single strong packet shows up here.

### 4. active_samples

    Count of samples where: 10 * log10( |z_k|^2 ) > -60 dB

Number of samples above the noise floor threshold (-60 dB). More active samples = more data being transmitted by the drone.

### 5. active_ratio

    active_ratio = active_samples / N

Fraction of the 10ms window that is above the noise floor.
- No drone: ~1.5% (only thermal noise spikes)
- Drone ON: ~72% (OcuSync is transmitting most of the time)

This is the simplest and most reliable drone detection feature.

### 6. active_energy

    active_energy = SUM( |z_k|^2 ) for only the samples where 10*log10(|z_k|^2) > -60 dB

Energy of just the signal portion, ignoring silence gaps. Isolates the drone's actual transmit power from the background noise.

### 7. active_mean_db

    active_mean_db = 10 * log10( (1/active_count) * SUM_active( |z_k|^2 ) )

Average power of only the active (above noise floor) samples in dB. Answers: "How loud is the drone when it is actually transmitting?" Removes bias from silence gaps between bursts.

### 8. burst_count

    Count of transitions from below to above the -60 dB noise floor threshold

Number of separate transmission bursts in the 10ms window. Each transition from silence to signal = one burst. Counts individual OcuSync data packets.

### 9. duty_cycle

    duty_cycle = active_ratio (same value)

Standard RF metric representing the fraction of time the channel is occupied. Identical to active_ratio but named differently for RF engineering convention.

### 10. power_std_db

    power_std_db = std( 10 * log10( |z_k|^2 ) ) for all k

Standard deviation of per-sample power in decibels. Measures power variability:
- Low std = flat signal (pure noise)
- High std = bursty signal (drone with on/off transmission pattern)

### 11. bandwidth_hz (uses FFT)

    spectrum = |FFT(z)|^2
    max_spectrum = max(spectrum)
    bandwidth_bins = count of bins where spectrum > max_spectrum / 2
    bandwidth_hz = bandwidth_bins * (sample_rate / N)

**This is the only place where FFT is used.** We compute the power spectrum via Fast Fourier Transform, find the peak, then count how many frequency bins are within 3 dB of the peak (half-power bandwidth). This gives the occupied bandwidth in Hz. OcuSync typically uses ~10 MHz channels.

### 12. effective_size_bytes

    effective_size_bytes = active_samples * 8

Converts the active sample count to a simulated "data volume" in bytes (8 bytes per complex sample = 4 bytes I + 4 bytes Q). Represents how much data the drone effectively transmitted in this window.

---

## PART C — V2: Amplitude & Phase Statistics (17 variables)

**Purpose:** Answer "What does the signal's shape and modulation look like?"

First, from each complex sample we extract two real values:

    Amplitude:  a_k = |z_k| = sqrt(I_k^2 + Q_k^2)
    Phase:      phi_k = atan2(Q_k, I_k)
    Unwrapped phase: phi_hat_k = unwrap(phi_k)  (removes 2*pi discontinuities)

### 13. amp_mean

    amp_mean = (1/N) * SUM( a_k )

Average signal strength across the 100,000 samples.

**Primary distance indicator.** Signal power drops with the inverse-square law: P is proportional to 1/d^2. As the drone flies further away, amp_mean decreases. In our lab data:
- No drone: 0.000434
- Drone at ~1m: 0.005225 (12x stronger than noise)

### 14. amp_std

    amp_std = sqrt( (1/N) * SUM( (a_k - amp_mean)^2 ) )

Standard deviation of amplitude. High std = signal alternates between loud bursts and silence (drone). Low std = flat constant level (noise).

### 15. amp_max

    amp_max = max(a_k)

Strongest single sample amplitude in the window. Represents the peak transmit power of the loudest moment.

### 16. amp_min

    amp_min = min(a_k)

Weakest single sample amplitude. Reveals the noise floor level between bursts.

### 17. amp_median

    amp_median = median(a_k)

Middle value of all amplitudes when sorted. More robust than the mean — not skewed by extreme peaks or outliers.

### 18. amp_p95

    amp_p95 = 95th percentile of a_k

"Typical peak" amplitude — 95% of samples are below this value. Represents the loudest bursts without being sensitive to single-sample outliers.

### 19. amp_p05

    amp_p05 = 5th percentile of a_k

"Typical floor" amplitude — 95% of samples are above this value. Represents the quietest moments (silence gaps between bursts).

### 20. amp_dynamic_range

    amp_dynamic_range = amp_p95 / amp_p05

Ratio of the typical loud level to the typical quiet level.
- No drone: 7.7 (noise is fairly flat, small range)
- Drone ON: 55.2 (huge contrast between active bursts and silence gaps)

This single number captures the "burstiness" of the signal.

### 21. amp_crest_factor

    amp_crest_factor = amp_max / amp_rms

Peak-to-RMS ratio. Standard RF engineering metric.
- High crest factor = impulsive, spiky bursts
- Low crest factor = continuous, flat signal

### 22. amp_rms

    amp_rms = sqrt( (1/N) * SUM( a_k^2 ) )

Root mean square of amplitude. Power-weighted average that gives more weight to loud samples than quiet ones. More representative of signal power than simple mean.

### 23. phase_mean

    phase_mean = (1/N) * SUM( phi_k )

Average phase angle (in radians, range -pi to +pi). Baseline phase offset that changes with distance and drone orientation relative to antenna.

### 24. phase_std

    phase_std = std(phi_k)

Standard deviation of phase. Pure noise has uniformly random phase, giving std ~1.81 (close to pi/sqrt(3)). A coherent signal can have different values depending on modulation.

### 25. phase_circ_mean

    phase_circ_mean = atan2( mean(sin(phi_k)), mean(cos(phi_k)) )

Circular mean of phase. This is the mathematically correct way to average angles — it handles the wrap-around at -pi/+pi properly. Simple linear averaging would give wrong results when phases straddle the -pi/+pi boundary.

### 26. phase_concentration

    phase_concentration = | (1/N) * SUM( e^(j * phi_k) ) |

The magnitude of the mean phasor (Von Mises concentration parameter).
- Near 0 = phase is uniformly random in all directions (noise, or complex modulation)
- Near 1 = all phases point in the same direction (strong unmodulated carrier)

### 27. phase_drift

    Computed via linear regression of unwrapped phase over sample index:

    beta = SUM( (t_k - t_mean) * (phi_hat_k - phi_hat_mean) ) / SUM( (t_k - t_mean)^2 )

    where t_k = sample index (0, 1, 2, ..., N-1)

This is the slope of a linear fit to the unwrapped phase vs time. It represents the rate of phase rotation, which equals the frequency offset between the drone's carrier and our USRP's tuned frequency.

**Doppler indicator:** If the drone is moving toward/away from the antenna, its carrier frequency shifts slightly (Doppler effect), changing this slope. In our data:
- No drone: phase_drift std = 0.006 (negligible random drift)
- Drone still: phase_drift std = 0.022 (OcuSync modulation causes some drift)
- Camera moving: phase_drift std = 0.048 (2.2x more — physical movement modulates phase)

**This was the #1 most important feature in ML training (10.9% importance).**

### 28. phase_entropy

    Divide phase range [-pi, +pi] into 36 bins (each 10 degrees wide)
    Count samples in each bin to get probabilities: p_i = count_i / N
    H = -SUM( p_i * log2(p_i) )

Shannon entropy of the phase distribution. Measures randomness:
- Maximum entropy (~5.17) = phase is uniformly distributed = noise or spread-spectrum
- Lower entropy = phase is concentrated in certain angles = structured modulation

### 29. amp_phase_corr

    Compute phase_diff_k = |phi_hat_(k+1) - phi_hat_k| for each consecutive pair
    amp_phase_corr = Pearson correlation between a_k and phase_diff_k

Correlation between amplitude and rate of phase change. This is a cross-domain feature:
- If amplitude and phase change together, it indicates coherent modulation (the drone's digital signal)
- Camera/gimbal movement makes this correlation 2.2x more variable (std: 0.009 vs 0.019)
- This decorrelation is caused by physical vibrations from the gimbal motors

---

## PART D — V2: Amplitude Histogram (10 variables)

**Purpose:** Compact representation of the amplitude probability distribution.

### 30-39. amp_hist_0 through amp_hist_9

    Divide amplitude range [0, amp_max] into 10 equal-width bins
    Count samples in each bin
    Normalize: amp_hist_i = count_i / N

Each value represents the fraction of samples falling in that amplitude range:
- amp_hist_0: fraction of samples with very low amplitude (noise floor)
- amp_hist_9: fraction of samples with maximum amplitude (strongest bursts)

No drone: nearly all samples in amp_hist_0 (everything is noise-level).
Drone ON: samples spread across multiple bins (bursts create a wide amplitude distribution).

This is essentially a discretized probability density function (PDF) of the signal strength.

---

## PART E — V2: Fingerprint Vectors (2 string columns, 128 values each)

**Purpose:** Map the temporal burst structure within each 10ms window.

### Why 128 bins?

100,000 samples / 128 bins = 781 samples per bin = ~78 microseconds per bin.

This choice balances three factors:
1. **Resolution vs OcuSync burst width:** OcuSync transmissions have ~50us periodicity (measured from .iq analysis). At 128 bins (~78us each), we can resolve individual burst cycles. Going to 256 bins (~39us) would split bursts across bins, adding noise. Going to 64 bins (~156us) would merge multiple bursts into one blob.
2. **Statistical stability:** 781 samples averaged per bin gives a stable mean. Fewer samples per bin (more bins) = more random noise in each bin value.
3. **Computational efficiency:** 128 is a power of 2, which is efficient for storage and processing.

### 40. amp_fingerprint (128 values)

    For each bin i (from 0 to 127):
        start = i * 781
        end = (i + 1) * 781
        amp_fingerprint[i] = (1/781) * SUM( a_k ) for k = start to end

Each bin contains the average amplitude during that specific ~78 microsecond time slice.

- High value = the drone was actively transmitting during this time slice
- Low value (near noise floor ~0.0004) = silence gap, drone was listening or idle

The pattern of high/low bins across the 128 positions reveals the OcuSync Time-Division Duplex (TDD) schedule — when the drone transmits vs when it listens.

### 41. phase_fingerprint (128 values)

    For each bin i (from 0 to 127):
        start = i * 781
        end = (i + 1) * 781
        phase_fingerprint[i] = (1/781) * SUM( phi_hat_k ) for k = start to end

    where phi_hat_k is the unwrapped phase

Each bin contains the average cumulative phase rotation during that time slice. Because phase is unwrapped (no 2*pi jumps), this creates a generally increasing curve across the 128 bins, with the slope determined by the drone's carrier frequency offset.

In our lab data:
- No drone: nearly flat (slope ~0.5 per bin) — random phase, no coherent rotation
- Drone still: strong ramp (slope ~78 per bin) — consistent carrier offset from drone's oscillator
- Camera moving: disrupted ramp (slope ~4.6 per bin average, but std = 3,150) — physical movement causes wild phase jumps between windows

---

## PART F — V3: Inter-Window Features (7 variables)

**Purpose:** Compare window T to window T-1 (and rolling history) to detect movement.

These features are what enable approaching/departing detection.

### 42. amp_mean_delta

    amp_mean_delta = amp_mean[T] - amp_mean[T-1]

Simple difference in mean amplitude between the current window and the previous window (10ms ago).

- Positive = signal getting stronger = drone approaching
- Negative = signal getting weaker = drone departing
- ~0 = stationary (or no drone)

### 43. relative_distance

    relative_distance = sqrt( amp_mean[T-1] / amp_mean[T] )

Distance ratio estimated from the inverse-square law. Since received power P is proportional to 1/d^2:

    P1/P2 = d2^2/d1^2
    d2/d1 = sqrt(P1/P2) = sqrt(amp_mean[T-1] / amp_mean[T])

Interpretation:
- Greater than 1.0 = drone moved closer since last window
- Less than 1.0 = drone moved farther away
- ~1.0 = same distance (stationary)

For the first window (no previous data), this defaults to 1.0.

### 44. fp_cosine_sim

    fp_cosine_sim = dot(F_T, F_{T-1}) / ( ||F_T|| * ||F_{T-1}|| )

    where F_T and F_{T-1} are the 128-element amp_fingerprint vectors of 
    the current and previous windows

Cosine similarity between consecutive fingerprints. Measures how similar the burst timing pattern is from one 10ms window to the next.

- ~0.999 = identical pattern (noise — every window looks the same)
- ~0.75 = pattern is reshuffling (active drone — OcuSync changes time slots adaptively)
- Lower values would indicate major state transitions

In our lab data:
- No drone: 0.9993 (perfectly stable — boring flat noise)
- Drone still: 0.7451 (77% of consecutive pairs fall below 0.8)
- Camera moving: 0.7580 (similar instability level)

### 45. phase_drift_delta

    phase_drift_delta = phase_drift[T] - phase_drift[T-1]

Change in the phase drift slope between consecutive windows. Since phase_drift represents frequency offset (related to Doppler shift):

- Non-zero value = the Doppler shift is changing = drone is accelerating or decelerating
- ~0 = constant velocity (or stationary)

Camera movement causes 2.2x more variability in this metric compared to a still drone, because the gimbal motors create vibrations that modulate the carrier phase.

### 46. amp_trend_short

    Linear regression slope of amp_mean over the last 5 windows (50ms)

    If we have amp_mean values [a1, a2, a3, a4, a5], fit a line y = mx + b
    amp_trend_short = m (the slope)

Short-term amplitude trend. Captures fast movements:
- Positive slope = signal strength increasing over last 50ms = approaching quickly
- Negative slope = signal strength decreasing = departing
- ~0 = stationary

Reacts within 50ms but can be noisy.

### 47. amp_trend_long

    Linear regression slope of amp_mean over the last 20 windows (200ms)

    Same computation as amp_trend_short but using 20 windows instead of 5.

Long-term amplitude trend. Filters out window-to-window noise for sustained movement detection. More reliable than the short trend but slower to react to sudden changes.

### 48. amp_ema

    EMA[T] = alpha * amp_mean[T] + (1 - alpha) * EMA[T-1]

    where alpha = 0.2

Exponential Moving Average of amplitude. Gives 20% weight to the current window and 80% weight to the accumulated history. This creates a smoothed amplitude signal that:
- Removes per-window jitter from random OcuSync scheduling variations
- Still reacts to real distance changes (with ~5 window delay)
- Provides a stable "distance proxy" for continuous tracking

---

## Summary Table

| Layer | # | Variables | Computed From | Purpose |
|-------|---|-----------|---------------|---------|
| Raw | - | Complex IQ stream (z_k) | USRP hardware | Source of everything |
| V1 | 1-12 | 12 scalar features | \|z_k\|^2 (power) | Energy/detection |
| V2 | 13-29 | 17 scalar features | \|z_k\| (amplitude) and angle(z_k) (phase) | Signal signature |
| V2 | 30-39 | 10 histogram bins | \|z_k\| distribution | Amplitude PDF |
| V2 | 40-41 | 2 x 128 fingerprint vectors | Temporal downsampling of \|z_k\| and unwrap(angle(z_k)) | Burst timing structure |
| V3 | 42-48 | 7 scalar features | Comparing V1/V2 features across consecutive 10ms windows | Movement/distance |
| **Total** | | **46 ML features + 2 x 128 fingerprint arrays** | | **Full drone characterization** |

## FFT Usage

FFT is used in exactly one place: computing `bandwidth_hz` (feature #11). The power spectrum is calculated via:

    spectrum = |FFT(z)|^2

Then the -3dB bandwidth is estimated by counting frequency bins above half the peak power and converting to Hz.

No other feature uses FFT. All amplitude/phase features work directly in the time domain.

## Key Lab Results (March 3, 2026)

| Feature | No Drone | Drone Still | Drone + Camera | Conclusion |
|---------|----------|-------------|----------------|------------|
| active_ratio | 1.5% | 72.2% | 71.5% | Drone detection is trivial |
| amp_mean | 0.000434 | 0.005225 | 0.005204 | 12x signal above noise (+21.6 dB) |
| phase_drift std | 0.006 | 0.022 | 0.048 | Camera causes 2.2x more phase instability |
| amp_phase_corr std | 0.004 | 0.009 | 0.019 | Camera decorrelates amplitude-phase |
| fp_cosine_sim | 0.999 | 0.745 | 0.758 | Drone bursts reshape every 10ms |
| phase_fp between-window std | 294 | 1,243 | 3,150 | Camera disrupts coherent phase ramp |

**ML Accuracy: 96% across 3 classes (no drone, drone still, drone + camera movement)**

Top 5 features by Random Forest importance:
1. phase_drift (10.9%) [V2]
2. amp_ema (8.1%) [V3]
3. active_ratio (6.4%) [V1]
4. amp_p95 (6.0%) [V2]
5. amp_phase_corr (5.8%) [V2]
