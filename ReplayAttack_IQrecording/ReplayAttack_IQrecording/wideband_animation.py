#!/usr/bin/env python3
"""
Animated Wideband Spectrogram — sliding-window view
====================================================
Takes a wideband .iq file and produces a scrolling MP4/GIF animation.
The animation shows a narrow time window (0.15 s by default) sliding across
the full recording, so the TDD burst structure is clearly visible throughout.

Usage:
  python3 wideband_animation.py [--iq FILE] [--output FILE]

Defaults to the file used in the thesis zoomed-in plot:
  drone_present_20260424_141455.iq  (1-second recording)
"""

import argparse
import json
import sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg
import matplotlib.ticker as ticker
from matplotlib.animation import FuncAnimation, FFMpegWriter, PillowWriter
from pathlib import Path
from scipy.signal import stft as scipy_stft

# ── Same STFT parameters as wideband_spectrogram.py ──────────────────────────
STFT_NPERSEG  = 1024
STFT_NOVERLAP = 512
STFT_WINDOW   = 'hann'
DENOISE_K     = 1.5     # soft threshold (amplitude mode, matches zoomed plot)
DEFAULT_RATE  = 40e6
OUTPUT_DIR    = Path(__file__).parent / "wideband_iq"

DEFAULT_IQ    = OUTPUT_DIR / "drone_present_20260424_141455.iq"

# ── Animation parameters ──────────────────────────────────────────────────────
WINDOW_S   = 0.15    # width of the sliding time window (seconds) — matches the zoomed plot
N_FRAMES   = 120     # number of animation frames
FPS        = 20      # frames per second → 6-second animation


def load_meta(iq_path: Path, default_rate: float) -> dict:
    sidecar = iq_path.with_suffix('.json')
    if sidecar.exists():
        return json.loads(sidecar.read_text())
    return {"rate_hz": default_rate, "freq_hz": 5.7875e9}


def compute_full_stft(iq_path: Path, rate: float, max_seconds: float = 1.0):
    """
    Memory-efficient STFT over the full recording.
    Returns (freqs_mhz, times_s, Pdb_amplitude) where Pdb is 20·log10(|X|).
    """
    import scipy.signal as _ss

    n_file  = iq_path.stat().st_size // 8
    n_samps = min(n_file, int(rate * max_seconds))

    nperseg = STFT_NPERSEG
    hop     = STFT_NPERSEG - STFT_NOVERLAP  # 512
    window  = _ss.get_window(STFT_WINDOW, nperseg).astype(np.float64)

    n_frames = max(0, (n_samps - nperseg) // hop + 1)

    sig = np.memmap(iq_path, dtype=np.complex64, mode='r', shape=(n_samps,))
    Pdb = np.empty((nperseg, n_frames), dtype=np.float32)

    shift_idx = np.fft.fftshift(np.arange(nperseg)).astype(np.int64)
    BATCH     = 2048
    out_off   = 0

    print(f"Computing STFT: {n_samps/rate:.2f} s  |  {n_frames} frames  |  {nperseg} freq bins")
    for b_start in range(0, n_frames, BATCH):
        b_end   = min(b_start + BATCH, n_frames)
        n_b     = b_end - b_start
        s_start = b_start * hop
        s_end   = min((b_end - 1) * hop + nperseg, n_samps)
        chunk   = np.array(sig[s_start:s_end])

        rel     = np.arange(n_b, dtype=np.int64) * hop
        idx     = rel[:, np.newaxis] + np.arange(nperseg, dtype=np.int64)
        idx     = np.minimum(idx, len(chunk) - 1)
        frames  = chunk[idx] * window
        del chunk

        fft_out = np.fft.fft(frames, axis=1)
        del frames
        mag = np.abs(fft_out[:, shift_idx]).astype(np.float32)
        del fft_out

        mag /= float(window.sum())
        mag  = np.maximum(mag, 1e-10)
        Pdb[:, out_off: out_off + n_b] = (20.0 * np.log10(mag)).T.astype(np.float32)
        out_off += n_b
        del mag
        if b_start % (BATCH * 10) == 0:
            pct = 100 * out_off / n_frames
            print(f"  {pct:5.1f}%", end='\r', flush=True)

    print(f"  100.0% — done           ")
    freqs_mhz = np.fft.fftshift(np.fft.fftfreq(nperseg, 1.0 / rate)) / 1e6
    times_s   = (np.arange(n_frames) * hop + nperseg / 2.0) / rate
    return freqs_mhz, times_s, Pdb


def auto_denoise(Pdb: np.ndarray, k: float = DENOISE_K) -> np.ndarray:
    """
    Same auto-denoising as wideband_spectrogram.py:
    use the quietest 20 % of columns as the per-bin noise reference.
    """
    col_power  = Pdb.mean(axis=0)
    n_noise    = max(1, int(0.20 * Pdb.shape[1]))
    noise_cols = np.argsort(col_power)[:n_noise]
    noise_mean = Pdb[:, noise_cols].mean(axis=1)
    noise_std  = Pdb[:, noise_cols].std(axis=1)
    threshold  = noise_mean + k * noise_std

    masked = Pdb.astype(float).copy()
    masked[masked <= threshold[:, np.newaxis]] = np.nan
    return masked


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--iq',      type=Path, default=DEFAULT_IQ,
                        help='Input .iq file (default: drone_present_20260424_141455.iq)')
    parser.add_argument('--output',  type=Path, default=None,
                        help='Output file path (.mp4 or .gif). Default: same stem as IQ file.')
    parser.add_argument('--window',  type=float, default=WINDOW_S,
                        help=f'Sliding window width in seconds (default {WINDOW_S})')
    parser.add_argument('--frames',  type=int,   default=N_FRAMES,
                        help=f'Number of animation frames (default {N_FRAMES})')
    parser.add_argument('--fps',     type=int,   default=FPS,
                        help=f'Frames per second (default {FPS})')
    parser.add_argument('--seconds', type=float, default=1.0,
                        help='Seconds of IQ to use (default 1.0)')
    args = parser.parse_args()

    iq_path = args.iq
    if not iq_path.exists():
        sys.exit(f"ERROR: IQ file not found: {iq_path}")

    meta     = load_meta(iq_path, DEFAULT_RATE)
    rate     = float(meta.get('rate_hz', DEFAULT_RATE))
    fc_ghz   = float(meta.get('freq_hz', 5.7875e9)) / 1e9
    bw_mhz   = rate / 1e6

    # ── Determine output path ─────────────────────────────────────────────────
    if args.output is None:
        out_path = OUTPUT_DIR / (iq_path.stem + "_animation.mp4")
    else:
        out_path = args.output

    print("=" * 65)
    print("ANIMATED WIDEBAND SPECTROGRAM")
    print("=" * 65)
    print(f"Input  : {iq_path.name}")
    print(f"Output : {out_path.name}")
    print(f"Rate   : {bw_mhz:.0f} MS/s  |  Centre: {fc_ghz:.4f} GHz")
    print(f"Window : {args.window:.2f} s  |  {args.frames} frames @ {args.fps} fps")
    print(f"Duration of animation: {args.frames/args.fps:.1f} s")
    print("=" * 65)

    # ── 1. Compute STFT ───────────────────────────────────────────────────────
    freqs_mhz, times_s, Pdb = compute_full_stft(iq_path, rate,
                                                  max_seconds=args.seconds)

    total_duration = times_s[-1]
    print(f"Recording duration: {total_duration:.3f} s  |  "
          f"STFT matrix: {Pdb.shape[1]} × {Pdb.shape[0]} (time × freq)")

    # ── 2. Denoise ────────────────────────────────────────────────────────────
    print("Denoising …")
    masked = auto_denoise(Pdb, k=DENOISE_K)

    # ── 3. Colour scale from signal-present bins ──────────────────────────────
    sig_vals = masked[~np.isnan(masked)].ravel()
    if sig_vals.size > 0:
        vmin = np.percentile(sig_vals, 2)
        vmax = np.percentile(sig_vals, 98)
    else:
        vmin, vmax = -65, -35
    print(f"Colour scale: {vmin:.1f} – {vmax:.1f} dBFS")

    cmap = plt.cm.plasma.copy()
    cmap.set_bad(color='black')

    # ── 4. Build figure ───────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(12, 5.5))
    FigureCanvasAgg(fig)   # ensure Agg canvas is attached
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')

    fig.suptitle(
        f"OcuSync RF Spectrogram — DJI Matrice 300 RTK\n"
        f"centre {fc_ghz:.4f} GHz  ·  {bw_mhz:.0f} MHz BW  ·  "
        f"STFT L={STFT_NPERSEG} hop={STFT_NPERSEG - STFT_NOVERLAP}  ·  "
        f"amplitude, noise floor + {DENOISE_K}σ",
        fontsize=11, fontweight='bold', color='white',
    )
    fig.subplots_adjust(left=0.09, right=0.88, top=0.83, bottom=0.13)

    # Plot the entire masked matrix; we will update xlim each frame
    im = ax.imshow(
        masked,
        aspect        = 'auto',
        origin        = 'lower',
        extent        = [times_s[0], times_s[-1], freqs_mhz[0], freqs_mhz[-1]],
        cmap          = cmap,
        vmin          = vmin,
        vmax          = vmax,
        interpolation = 'nearest',
    )

    ax.axhline(0, color='white', lw=0.5, ls='--', alpha=0.4)
    ax.yaxis.set_major_locator(ticker.MultipleLocator(5))
    ax.set_ylabel("Frequency offset from centre (MHz)", fontsize=10, color='white')
    ax.set_xlabel("Time (s)", fontsize=10, color='white')
    ax.tick_params(colors='white')
    for spine in ax.spines.values():
        spine.set_edgecolor('white')

    # Colourbar
    cbar_ax = fig.add_axes([0.895, 0.13, 0.018, 0.70])
    cb = fig.colorbar(im, cax=cbar_ax, label='Amplitude (dBFS)')
    cb.ax.yaxis.label.set_color('white')
    cb.ax.tick_params(colors='white', labelsize=9)

    # Title annotation that shows current time position
    time_text = ax.text(
        0.01, 0.97, '',
        transform=ax.transAxes,
        fontsize=10, color='white', va='top',
        bbox=dict(boxstyle='round,pad=0.3', fc='black', alpha=0.6),
    )

    # ── 5. Progress indicator (thin vertical line at window centre) ───────────
    vline = ax.axvline(x=0, color='cyan', lw=1.0, alpha=0.6, ls=':')

    # ── 6. Animation function ─────────────────────────────────────────────────
    window   = args.window
    n_frames = args.frames

    # Window slides so its leading edge goes from window_s to total_duration
    # Centre starts at window/2, ends at total_duration - window/2
    t_centres = np.linspace(window / 2, total_duration - window / 2, n_frames)

    def update(frame_idx):
        tc      = t_centres[frame_idx]
        t_start = tc - window / 2
        t_end   = tc + window / 2
        ax.set_xlim(t_start, t_end)
        vline.set_xdata([tc])
        time_text.set_text(
            f"{iq_path.stem}\n"
            f"t = {t_start:.3f} – {t_end:.3f} s  "
            f"(frame {frame_idx + 1}/{n_frames})"
        )
        return im, vline, time_text

    anim = FuncAnimation(fig, update, frames=n_frames, interval=1000 / args.fps,
                         blit=False)

    # ── 7. Save ───────────────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = out_path.suffix.lower()

    if suffix == '.gif':
        print(f"Saving GIF ({n_frames} frames @ {args.fps} fps) …")
        writer = PillowWriter(fps=args.fps)
        anim.save(str(out_path), writer=writer, dpi=120)
    else:
        # Try imageio-ffmpeg first (bundled binary, no system ffmpeg required)
        saved = False
        try:
            import imageio
            import imageio.v2 as iio2
            print(f"Saving MP4 via imageio ({n_frames} frames @ {args.fps} fps) …")
            # Render each frame to a numpy RGB array, stream to imageio writer
            dpi = 120
            fig.canvas.draw()
            with iio2.get_writer(str(out_path), format='ffmpeg', fps=args.fps,
                                 codec='libx264', quality=7,
                                 output_params=['-pix_fmt', 'yuv420p']) as video_writer:
                for fi in range(n_frames):
                    update(fi)
                    fig.canvas.draw()
                    buf = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
                    w, h = fig.canvas.get_width_height()
                    frame_arr = buf.reshape(h, w, 3)
                    # yuv420p requires even dimensions
                    h2 = h - (h % 2)
                    w2 = w - (w % 2)
                    video_writer.append_data(frame_arr[:h2, :w2])
                    if fi % 10 == 0:
                        print(f"  frame {fi+1}/{n_frames}", end='\r', flush=True)
            print(f"  frame {n_frames}/{n_frames} — done     ")
            saved = True
        except Exception as e:
            print(f"imageio MP4 failed ({e}), trying matplotlib FFMpegWriter …")

        if not saved:
            try:
                import shutil
                if shutil.which('ffmpeg') is None:
                    raise RuntimeError("ffmpeg not found")
                print(f"Saving MP4 via ffmpeg ({n_frames} frames @ {args.fps} fps) …")
                writer = FFMpegWriter(fps=args.fps, bitrate=2000,
                                      metadata={"title": "OcuSync Spectrogram Animation"})
                anim.save(str(out_path), writer=writer, dpi=120)
                saved = True
            except Exception as e:
                print(f"MP4 failed ({e}), falling back to GIF …")

        if not saved:
            out_path = out_path.with_suffix('.gif')
            print(f"Saving GIF ({n_frames} frames @ {args.fps} fps) …")
            writer = PillowWriter(fps=args.fps)
            anim.save(str(out_path), writer=writer, dpi=120)

    plt.close()
    size_mb = out_path.stat().st_size / 1e6
    print(f"\nSaved: {out_path}  ({size_mb:.1f} MB)")
    print(f"Animation: {n_frames} frames, {args.fps} fps = {n_frames/args.fps:.1f} s playback")


if __name__ == '__main__':
    main()
