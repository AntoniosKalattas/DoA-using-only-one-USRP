"""
OcuSync TDD Frame Occupancy Spectrogram  (denoised version)
------------------------------------------------------------
Produces a 3-panel figure styled after Shi et al. (2026) Fig. 3/7.

Key difference from the reference paper
----------------------------------------
Shi et al. capture 100 MHz of bandwidth → STFT → Y-axis is RF frequency.
The drone occupies ~3-4 MHz of that 100 MHz, so most frequency bins are at
noise floor, giving a clean dark background with narrow bright signal bands.

We capture 10 MHz centred at 5.845 GHz and store the 128-bin amplitude
fingerprint.  When OcuSync hops TO our channel its OFDM signal fills all 128
time-domain bins simultaneously (OFDM is spectrally flat).  When it hops AWAY
every bin drops to the noise floor.  There is therefore no partial-occupancy
structure per row — the raw plot looks uniformly noisy/colourful.

Fix (Shi et al. Fig. 7 approach)
----------------------------------
1. Compute per-bin noise floor from the no-drone recording:
       noise_floor[b] = mean_b + THRESH_K × std_b
2. For every drone panel, set each bin value below noise_floor[b] to NaN.
3. Use a dark-background colourmap (plasma) with bad-colour = black.

Result: clean black background everywhere the drone has hopped away,
bright vertical stripes wherever OcuSync occupies our channel — the
OcuSync FHSS hopping pattern becomes immediately visible.

Outputs:
  figures_v3/ocusync_spectrogram.png         – 3 states, 300-window (3 s) view
  figures_v3/ocusync_spectrogram_full.png    – 3 states, all 1500 windows (~15 s)
"""

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(BASE, "figures_v3")
os.makedirs(OUT, exist_ok=True)

FILES = [
    ("(a)  No Drone (Noise)",          "no_drone_10ms_20260303_121416.csv"),
    ("(b)  Drone ON — No Camera",       "no_camera_movement_10ms_20260303_122213.csv"),
    ("(c)  Drone ON — Camera Moving",   "with_camera_movement_10ms_20260303_122657.csv"),
]

# ── Constants ─────────────────────────────────────────────────────────────────
N_BINS        = 128
WINDOW_MS     = 10.0                              # ms per capture window
BIN_MS        = WINDOW_MS / N_BINS                # ≈ 0.078 ms per bin
WIN_S         = WINDOW_MS / 1000.0               # 0.010 s
# Centre the Y-axis so that the midpoint of the TDD frame is at 0 ms
BIN_AXIS_MS   = (np.arange(N_BINS) - N_BINS / 2) * BIN_MS   # −5 to +5 ms

# ── Denoising threshold ───────────────────────────────────────────────────────
# Number of standard deviations above no-drone mean to treat as "signal present"
THRESH_K = 3.0

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_fingerprints(csv_path, n_windows=None):
    df = pd.read_csv(csv_path)
    rows = df['amp_fingerprint'] if n_windows is None else df['amp_fingerprint'].head(n_windows)
    return np.array([list(map(float, r.split(','))) for r in rows])   # (N, 128)


def to_db(arr, floor=1e-10):
    return 20.0 * np.log10(np.maximum(arr, floor))


def make_figure(n_windows, out_filename, title_suffix=""):
    """Build and save a 3-panel spectrogram figure."""

    # ── Load data ─────────────────────────────────────────────────────────────
    datasets   = {}
    act_ratios = {}
    burst_cnts = {}
    for label, fname in FILES:
        df  = pd.read_csv(os.path.join(BASE, fname))
        raw = np.array([list(map(float, r.split(',')))
                        for r in df['amp_fingerprint'].head(n_windows)])
        datasets[label]   = to_db(raw)
        act_ratios[label] = df['active_ratio'].head(n_windows).mean()
        burst_cnts[label] = df['burst_count'].head(n_windows).mean()

    # ── Denoising (Shi et al. 2026 Fig. 7 approach) ───────────────────────────
    # Compute per-bin noise statistics from the no-drone recording
    Z_noise     = datasets[FILES[0][0]]            # (N, 128)
    noise_mean  = Z_noise.mean(axis=0)             # (128,)
    noise_std   = Z_noise.std(axis=0)              # (128,)
    noise_floor = noise_mean + THRESH_K * noise_std  # (128,) broadcast threshold

    # Mask all bins at or below the noise floor (shown as black background)
    masked = {}
    for label, _ in FILES:
        Z = datasets[label].astype(float).copy()   # (N, 128)
        Z[Z <= noise_floor[np.newaxis, :]] = np.nan
        masked[label] = Z

    # ── Colour scale: signal-present bins only, shared across all panels ──────
    signal_vals = np.concatenate([
        masked[k][~np.isnan(masked[k])].ravel() for k in masked
    ])
    if signal_vals.size == 0:
        vmin, vmax = -70, -40
    else:
        vmin = np.percentile(signal_vals, 2)
        vmax = np.percentile(signal_vals, 98)

    # ── Figure layout ─────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.2), sharey=True)
    fig.suptitle(
        "OcuSync TDD Frame Occupancy — DJI Matrice 300 RTK\n"
        f"5.845 GHz · 10 MHz BW · gain = 60 dB · stationary lab{title_suffix}",
        fontsize=12, fontweight='bold'
    )
    fig.subplots_adjust(left=0.07, right=0.87, top=0.85, bottom=0.12, wspace=0.12)

    images = []
    for ax, (label, _) in zip(axes, FILES):
        data   = masked[label]             # (N, 128) with NaN for noise
        n      = data.shape[0]
        t_end  = n * WIN_S                 # seconds

        # Use a dark-background colourmap so NaN bins appear black
        cmap = plt.cm.plasma.copy()
        cmap.set_bad(color='black')

        # Transpose so Y = bin position, X = time
        im = ax.imshow(
            data.T,                        # (128, N)
            aspect='auto',
            extent=[0, t_end, BIN_AXIS_MS[-1], BIN_AXIS_MS[0]],
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            origin='upper',
            interpolation='nearest',       # nearest keeps hard edges between hops
        )
        images.append(im)

        # Annotate with real metrics from CSV
        ar = act_ratios[label]
        bc = burst_cnts[label]
        ax.text(0.02, 0.03,
                f"Active: {ar*100:.1f}%  |  Bursts/10ms: {bc:.0f}",
                transform=ax.transAxes, fontsize=8, color='white',
                va='bottom', bbox=dict(boxstyle='round,pad=0.2', fc='black', alpha=0.5))

        # ── Axes decoration ───────────────────────────────────────────────────
        ax.set_xlabel("Time (s)", fontsize=10)
        ax.set_title(label, fontsize=11, pad=5)
        ax.yaxis.set_major_locator(ticker.MultipleLocator(2))
        ax.axhline(0, color='white', lw=0.6, ls='--', alpha=0.5)   # midframe marker

    axes[0].set_ylabel("TDD Frame Position (ms from frame midpoint)", fontsize=10)

    # ── Dedicated colour bar axes (avoids overlap with panel title) ────────────
    cbar_ax = fig.add_axes([0.895, 0.12, 0.018, 0.73])
    cbar = fig.colorbar(images[-1], cax=cbar_ax, label='Amplitude (dBFS)')
    cbar.ax.tick_params(labelsize=9)
    path = os.path.join(OUT, out_filename)
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Saved: {path}")


# ── Run ────────────────────────────────────────────────────────────────────────
# 3-second zoom (300 windows) — matches the paper's time-scale style
make_figure(300,  "ocusync_spectrogram.png",      " · first 3 s shown")

# Full-dataset view (~15 s, all 1500 windows)
make_figure(None, "ocusync_spectrogram_full.png", " · full 15 s recording")

print("Done.")
