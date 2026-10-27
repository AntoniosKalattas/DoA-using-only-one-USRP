#!/usr/bin/env python3
"""
Wideband Drone Detection Spectrogram
=====================================
Two commands — record once, plot once, see if there is a drone.

  RECORD  — captures raw IQ at wide bandwidth (USRP B210, up to ~56 MHz).
            Saves a flat complex64 binary file + JSON sidecar.

  PLOT    — loads a single .iq file, computes STFT, auto-estimates the noise
            floor from the quietest frames in the recording (no separate
            "no-drone" reference needed), masks noise bins black, and produces
            a paper-style spectrogram: time on X, RF frequency offset on Y,
            power in dB on Z.  If a drone is present you will see bright
            horizontal bands jumping up and down as OcuSync hops sub-channels.
            If there is no drone the plot will be uniformly black.

Why wideband?
  At 10 MHz (narrow-band) OcuSync's OFDM signal fills the entire bandwidth
  whenever the drone hops to our channel — all frequency bins light up and
  there is no spectral contrast to see.  At ≥40 MHz the drone's ~10–18 MHz
  sub-channel occupies only 25–45 % of the Y-axis; the rest stays at noise
  floor → bright bands floating in a dark background, exactly like Shi et al.
  (2026) Fig. 3.  You do NOT need to know OcuSync's frequency in advance;
  just point the USRP at the 5.8 GHz band and record.

Hardware limits (USRP B210 via USB 3.0):
  --rate 40e6  →  160 MB/s  (recommended, reliable over USB 3.0)
  --rate 56e6  →  225 MB/s  (max, needs fast NVMe + USB 3.0 controller)
  A 15-second recording at 40 MS/s ≈ 4.8 GB.
  Use --duration 5 for a quick 1.6 GB test.

Typical workflow — single recording:

  # 1. Record 15 seconds with drone flying nearby:
  python3 wideband_spectrogram.py record --duration 15

  # 2. Plot — drone bands appear as bright horizontal lines against black bg:
  python3 wideband_spectrogram.py plot --iq wideband_iq/recording_*.iq

  # 3. To compare two recordings side-by-side:
  python3 wideband_spectrogram.py plot \
      --iq wideband_iq/no_drone_*.iq wideband_iq/drone_*.iq \
      --labels "No Drone" "Drone Present"

If you have a clean "no drone" reference recording you can use it for a
more accurate per-bin noise floor:
  python3 wideband_spectrogram.py plot \
      --iq wideband_iq/drone_*.iq \
      --noise wideband_iq/no_drone_*.iq
"""

import argparse
import json
import sys
import time
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from pathlib import Path
from datetime import datetime
from scipy.signal import stft as scipy_stft

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_FREQ     = 5.7875e9   # Centre of OcuSync 5.725–5.850 GHz range (Hz)
DEFAULT_RATE     = 40e6       # 40 MHz — safe for USB 3.0 with sc8 mode (MS/s)
DEFAULT_GAIN     = 50         # RX gain (dB)
DEFAULT_DURATION = 15         # seconds
OUTPUT_DIR       = Path(__file__).parent / "wideband_iq"

# ── STFT parameters (matching Shi et al. 2026 proportionally) ─────────────────
# Shi et al.: L=1024 FFT, R=512 hop at 100 MS/s → Δt=5.12 µs, Δf=97.7 kHz
# At 40 MS/s with L=1024: Δt=12.8 µs, Δf=39.1 kHz — finer frequency resolution
STFT_NPERSEG  = 1024    # FFT size (number of samples per STFT frame)
STFT_NOVERLAP = 512     # Overlap between frames (50%)
STFT_WINDOW   = 'hann'

# Denoising threshold — number of σ above no-drone mean (Shi et al. approach)
DENOISE_K = 3.0

# Maximum seconds of IQ to load for plotting (limits RAM; full file used if shorter)
MAX_PLOT_SECONDS = 1.0   # keep STFT RAM < ~1.5 GB at 40 MS/s


# ══════════════════════════════════════════════════════════════════════════════
# CAPTURE
# ══════════════════════════════════════════════════════════════════════════════

def do_capture(state: str, duration: float, rate: float, freq: float,
               gain: int, output_dir: Path) -> None:
    """
    Record wideband IQ using the compiled C++ UHD example rx_samples_to_file.

    rx_samples_to_file is a pure C++ binary shipped with uhd-host.  It streams
    directly from the USRP to disk without any Python GIL involvement, which
    eliminates the O-U (overflow/underflow) errors that plague Python-based
    captures at high sample rates (≥ 20 MS/s).

    The binary writes interleaved signed 16-bit integers (sc16).  After
    recording this function converts to complex64 so the rest of the code
    can load the file normally.  The sc16 intermediate file is removed
    after conversion.
    """
    import subprocess
    import shutil

    RX_BIN = '/usr/lib/uhd/examples/rx_samples_to_file'
    if not Path(RX_BIN).exists():
        # Fall back to PATH search
        found = shutil.which('rx_samples_to_file')
        if not found:
            sys.exit(
                "ERROR: rx_samples_to_file not found.\n"
                "Install UHD examples: apt install uhd-host\n"
                "or locate the binary with: find /usr -name rx_samples_to_file"
            )
        RX_BIN = found

    output_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Stage the sc16 capture in /dev/shm (RAM) to avoid USB ↔ disk contention.
    # 40 MS/s × 4 bytes (sc16) × 15 s = 2.4 GB — fits comfortably in RAM.
    sc16_path = Path('/dev/shm') / f"uhd_{ts}.sc16"
    iq_path   = output_dir / f"{state}_{ts}.iq"
    meta_path = output_dir / f"{state}_{ts}.json"

    print("=" * 60)
    print("WIDEBAND IQ CAPTURE  (rx_samples_to_file — C++)")
    print("=" * 60)
    print(f"State:       {state}")
    print(f"Frequency:   {freq/1e9:.4f} GHz")
    print(f"Coverage:    {(freq-rate/2)/1e9:.4f} – {(freq+rate/2)/1e9:.4f} GHz")
    print(f"Sample rate: {rate/1e6:.0f} MS/s  →  {rate*4/1e6:.0f} MB/s (sc16 on wire)")
    print(f"Gain:        {gain} dB")
    print(f"Duration:    {duration} s")
    print(f"Output:      {iq_path}")
    print("=" * 60)
    print("\nStarting in 3 s …")
    time.sleep(3)
    print("Recording … (Ctrl-C to stop early)\n")

    # At 40 MS/s sc16 requires 160 MB/s over USB.  Use sc8 wire format (8-bit
    # I/Q pairs) to halve that to 80 MB/s while keeping 40 MHz spectral span.
    # The host converts sc8 → int16 before writing, so the file format is
    # identical to sc16.
    use_sc8 = (rate > 20e6)
    cmd = [
        RX_BIN,
        '--file',     str(sc16_path),
        '--type',     'short',        # output pairs of int16 (I, Q)
        '--duration', str(int(duration)),
        '--rate',     str(rate),
        '--freq',     str(freq),
        '--gain',     str(gain),
        '--spb',      '200000',       # large buffer → fewer ring-buffer stalls
        '--stats',                    # print final bandwidth summary
    ]
    if use_sc8:
        cmd += ['--wirefmt', 'sc8']   # halve USB bandwidth at high rates

    start_t = time.time()
    result  = subprocess.run(cmd, capture_output=False)
    elapsed = time.time() - start_t

    if result.returncode != 0:
        sys.exit(f"ERROR: rx_samples_to_file failed (exit {result.returncode})")

    if not sc16_path.exists() or sc16_path.stat().st_size == 0:
        sys.exit("ERROR: rx_samples_to_file produced an empty file.")

    # ── Convert sc16 → complex64 (chunked) ───────────────────────────────────
    # Process in 50 M-sample chunks so peak RAM stays ~600 MB regardless of
    # recording length.  Full-array conversion would require:
    #   int16 array:  N×4 B  +  float32 copy:  N×8 B  →  N×12 B peak
    # At N=400M (15 s): 4.8 GB peak — guaranteed OOM on most systems.
    # Chunked approach: only one 200 MB int16 chunk + 400 MB float32 ≈ 600 MB peak.
    print(f"\nConverting sc16 → complex64 …")
    CONV_CHUNK   = 50_000_000  # complex samples per chunk  (=200 MB int16 pairs)
    actual_samples = 0
    with open(sc16_path, 'rb') as f_in, open(iq_path, 'wb') as f_out:
        while True:
            raw_bytes = f_in.read(CONV_CHUNK * 4)   # 4 bytes per sc16 sample
            if not raw_bytes:
                break
            raw_i16  = np.frombuffer(raw_bytes, dtype=np.int16)
            raw_f32  = raw_i16.astype(np.float32);  del raw_i16
            raw_f32 /= 32768.0
            iq_chunk = raw_f32.view(np.complex64)
            iq_chunk.tofile(f_out)
            actual_samples += len(iq_chunk)
            del raw_f32
    sc16_path.unlink()   # remove the raw sc16 file
    metadata = {
        "state":              state,
        "freq_hz":            freq,
        "rate_hz":            rate,
        "gain_db":            gain,
        "duration_s":         duration,
        "actual_duration_s":  round(elapsed, 3),
        "n_samples":          actual_samples,
        "dtype":              "complex64",
        "timestamp":          ts,
    }
    meta_path.write_text(json.dumps(metadata, indent=2))

    size_mb = iq_path.stat().st_size / 1e6
    print(f"Done in {elapsed:.1f} s.  Saved {iq_path.name}  ({size_mb:.0f} MB, "
          f"{actual_samples/1e6:.1f} M samples)")
    print(f"Metadata:  {meta_path.name}")
    print(f"\nTo plot:  python3 wideband_spectrogram.py plot --iq {iq_path}")


# ══════════════════════════════════════════════════════════════════════════════
# IQ LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_iq(iq_path: Path, rate: float, max_seconds: float = MAX_PLOT_SECONDS):
    """
    Load complex64 IQ from file.  Loads at most max_seconds of data.
    Reads rate from companion JSON sidecar if present; falls back to `rate`.
    """
    sidecar = iq_path.with_suffix('.json')
    actual_rate = rate
    if sidecar.exists():
        meta        = json.loads(sidecar.read_text())
        actual_rate = float(meta.get('rate_hz', rate))

    max_samps = int(actual_rate * max_seconds)
    data      = np.fromfile(iq_path, dtype=np.complex64, count=max_samps)
    return data, actual_rate


# ══════════════════════════════════════════════════════════════════════════════
# STFT + DENOISING
# ══════════════════════════════════════════════════════════════════════════════

def compute_stft_power(samples: np.ndarray, rate: float, amplitude_mode: bool = False):
    """
    Compute STFT magnitude from complex IQ samples.

    amplitude_mode=False (default / power):
        Returns 10·log₁₀(|X|²)  — power spectral density in dBFS.
        Used for standard spectrogram plots.

    amplitude_mode=True:
        Returns 20·log₁₀(|X|)   — amplitude spectral density in dBFS.
        Mathematically identical but uses a factor-of-2 dB scale.
        More importantly: with show_noise=True the noise floor is NOT masked
        out, so the full dynamic range (signal + noise) is visible — useful
        for detection threshold illustrations in the thesis.

    Both modes use the same STFT pipeline:
        1. Divide IQ stream into Hann-windowed frames of L=1024 samples
        2. FFT each frame  →  complex matrix Z[f, t]
        3. Shift so DC is at centre of frequency axis (fftshift)
        4. |Z|  or  |Z|²  then convert to dB

    Returns:
        freqs_mhz : (Nf,) frequency offset from centre in MHz (DC at 0)
        times_s   : (Nt,) time axis in seconds
        Pdb       : (Nf, Nt) value in dB  (power or amplitude depending on mode)
    """
    freqs_hz, times_s, Zxx = scipy_stft(
        samples,
        fs        = rate,
        window    = STFT_WINDOW,
        nperseg   = STFT_NPERSEG,
        noverlap  = STFT_NOVERLAP,
        return_onesided = False,   # complex input → two-sided spectrum
    )
    # scipy returns freqs 0…rate/2, then -rate/2…0 (standard FFT order).
    # Shift so DC is in the centre — matches the Shi et al. frequency axis.
    # Use in-place shifts then free Zxx immediately to avoid keeping both
    # copies alive (each is ~1.3 GB at 2 s / 40 MS/s).
    shift_idx = np.fft.fftshift(np.arange(len(freqs_hz)))
    Zxx       = Zxx[shift_idx, :]             # index-based shift, still a copy
    freqs_hz  = freqs_hz[shift_idx]

    mag        = np.abs(Zxx)
    del Zxx                                    # free ~1.3 GB immediately
    mag        = np.maximum(mag, 1e-10)        # avoid log(0)
    if amplitude_mode:
        # 20·log₁₀(|X|) — amplitude in dBFS
        Pdb = 20.0 * np.log10(mag).astype(np.float32)
    else:
        # 10·log₁₀(|X|²) = 20·log₁₀(|X|) — same as above but conventional
        # power notation used in most RF/SDR literature
        Pdb = 10.0 * np.log10(mag ** 2).astype(np.float32)
    del mag
    freqs_mhz  = freqs_hz / 1e6
    return freqs_mhz, times_s, Pdb


def denoise(Pdb: np.ndarray, noise_Pdb: np.ndarray | None, k: float = DENOISE_K):
    """
    Per-frequency-bin adaptive denoising.

    If noise_Pdb is provided (matching shape (Nf, Nt_noise)):
        threshold[f] = mean over time of noise_Pdb[f] + k * std over time
    Otherwise estimate threshold from the lowest-power 10% of time columns
    in the signal itself (auto mode — less accurate).

    Bins at or below threshold are set to NaN (rendered black in the plot).
    """
    Nf = Pdb.shape[0]
    if noise_Pdb is not None:
        noise_mean = noise_Pdb.mean(axis=1)   # (Nf,)
        noise_std  = noise_Pdb.std(axis=1)    # (Nf,)
    else:
        # Auto: use lowest-power columns as noise reference.
        # FHSS reasoning: OcuSync hops across ~60 sub-channels spanning 125 MHz.
        # At 40 MHz BW we cover ~32% of the hop range, so the drone is absent
        # from our window for ~68% of frames → the quietest 20% of columns are
        # reliably drone-free and give a good per-bin noise estimate.
        col_power  = Pdb.mean(axis=0)
        n_noise    = max(1, int(0.20 * Pdb.shape[1]))
        noise_cols = np.argsort(col_power)[:n_noise]
        noise_mean = Pdb[:, noise_cols].mean(axis=1)
        noise_std  = Pdb[:, noise_cols].std(axis=1)

    threshold = noise_mean + k * noise_std    # (Nf,)
    masked     = Pdb.astype(float).copy()
    masked[masked <= threshold[:, np.newaxis]] = np.nan
    return masked


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY-EFFICIENT BATCHED STFT
# ══════════════════════════════════════════════════════════════════════════════

def _compute_stft_batched(iq_path: Path, actual_rate: float,
                           max_seconds: float,
                           amplitude_mode: bool = False,
                           target_mb: float = 350.0):
    """
    Memory-efficient STFT for arbitrarily long IQ files.

    Unlike scipy.stft — which allocates the full complex128 output matrix at
    once (16 B/element → ~1.3 GB per second at 40 MS/s / hop=512) — this
    function:

      • memory-maps the IQ file so the signal itself uses ~0 RAM
      • auto-selects a temporal stride so the float32 output stays ≤ target_mb
      • processes frames in batches of 2 048, keeping peak batch RAM < ~150 MB

    The frequency axis and resolution are identical to compute_stft_power
    (same STFT_NPERSEG / STFT_NOVERLAP / STFT_WINDOW constants).

    Returns: (freqs_mhz, times_s, Pdb_float32)
    """
    import scipy.signal as _ss

    n_file  = iq_path.stat().st_size // 8          # complex64 = 8 bytes
    n_samps = min(n_file, int(actual_rate * max_seconds))

    nperseg = STFT_NPERSEG                          # 1024
    hop     = STFT_NPERSEG - STFT_NOVERLAP          # 512
    window  = _ss.get_window(STFT_WINDOW, nperseg).astype(np.float64)

    n_frames_full = max(0, (n_samps - nperseg) // hop + 1)

    # Auto-stride: keeps float32 output matrix within target_mb
    out_bytes_full = n_frames_full * nperseg * 4    # float32
    stride = max(1, int(np.ceil(out_bytes_full / (target_mb * 1e6))))
    n_frames_out   = (n_frames_full + stride - 1) // stride
    frame_indices  = np.arange(0, n_frames_full, stride, dtype=np.int64)

    # Virtual memory map — no RAM allocation until pages are actually read
    sig = np.memmap(iq_path, dtype=np.complex64, mode='r', shape=(n_samps,))

    Pdb = np.empty((nperseg, n_frames_out), dtype=np.float32)

    # fftshift index (computed once, reused for every batch)
    shift_idx = np.fft.fftshift(np.arange(nperseg)).astype(np.int64)

    BATCH      = 2048
    out_offset = 0
    for b_start in range(0, len(frame_indices), BATCH):
        b_idxs = frame_indices[b_start: b_start + BATCH]
        n_b    = len(b_idxs)

        # Materialise only the signal samples needed for this batch
        s_start = int(b_idxs[0]) * hop
        s_end   = min(int(b_idxs[-1]) * hop + nperseg, n_samps)
        chunk   = np.array(sig[s_start:s_end])          # RAM: (s_end-s_start)×8 B

        # Build (n_b, nperseg) frame matrix via index arithmetic
        rel_starts = (b_idxs - b_idxs[0]) * hop
        idx    = rel_starts[:, np.newaxis] + np.arange(nperseg, dtype=np.int64)
        idx    = np.minimum(idx, len(chunk) - 1)         # clamp at boundary
        frames = chunk[idx] * window                     # (n_b, nperseg) windowed
        del chunk

        # Batch FFT — numpy always returns complex128 regardless of input type
        fft_out = np.fft.fft(frames, axis=1)             # (n_b, nperseg)
        del frames

        mag = np.abs(fft_out[:, shift_idx]).astype(np.float32)   # (n_b, nperseg)
        del fft_out
        # Normalise by window sum to match scipy.stft 'spectrum' scaling
        # (scipy divides by sum(window); without this values are ~54 dB too high)
        mag /= float(window.sum())
        mag = np.maximum(mag, 1e-10)

        if amplitude_mode:
            p = 20.0 * np.log10(mag)
        else:
            p = 10.0 * np.log10(mag ** 2)
        del mag

        Pdb[:, out_offset: out_offset + n_b] = p.T.astype(np.float32)
        out_offset += n_b
        del p

    freqs_hz  = np.fft.fftshift(np.fft.fftfreq(nperseg, 1.0 / actual_rate))
    freqs_mhz = freqs_hz / 1e6
    times_s   = (frame_indices * hop + nperseg / 2.0) / actual_rate
    dt_us     = stride * hop / actual_rate * 1e6

    effective_seconds = times_s[-1] if len(times_s) else 0.0
    print(f"  {effective_seconds:.1f} s  |  {actual_rate/1e6:.0f} MS/s  |  "
          f"{nperseg} freq bins × {n_frames_out} time frames  |  "
          f"Δf={actual_rate/nperseg/1e3:.1f} kHz  Δt={dt_us:.1f} µs"
          + (f"  (stride={stride})" if stride > 1 else ""))

    return freqs_mhz, times_s, Pdb


# ══════════════════════════════════════════════════════════════════════════════
# PLOT
# ══════════════════════════════════════════════════════════════════════════════

def plot_panels(panels: list[dict], noise_iq_path: Path | None,
                rate: float, out_path: Path, auto_noise: bool,
                amplitude_mode: bool = False,
                plot_seconds: float = 5.0,
                display_xlim: tuple | None = None,
                display_ylim: tuple | None = None) -> None:
    """
    Build and save a 1–N panel spectrogram figure.

    panels         : list of {"iq_path": Path, "label": str}
    amplitude_mode : if True, plot 20·log₁₀(|STFT|) with a softer denoise
                     threshold (1.5σ instead of 3σ) so the noise floor and
                     drone bursts are both visible — better for showing
                     detection SNR in the thesis.
    """
    # ── Load noise reference ──────────────────────────────────────────────────
    noise_Pdb_map = {}   # keyed by rate rounded to int
    if noise_iq_path is not None:
        print(f"Loading noise reference: {noise_iq_path.name} …")
        nd, nrate     = load_iq(noise_iq_path, rate)
        _, _, noise_P = compute_stft_power(nd, nrate)
        noise_Pdb_map[round(nrate)] = noise_P
        print(f"  {len(nd)/nrate:.1f} s  |  {noise_P.shape[0]} freq bins × {noise_P.shape[1]} time frames")

    # ── Load and process each panel ───────────────────────────────────────────
    panel_data = []
    for p in panels:
        print(f"Loading signal: {p['iq_path'].name} …")
        # Read actual rate from sidecar so _compute_stft_batched uses correct value
        sidecar = p['iq_path'].with_suffix('.json')
        srate   = rate
        if sidecar.exists():
            _meta = json.loads(sidecar.read_text())
            srate = float(_meta.get('rate_hz', rate))
        freqs, times, Pdb = _compute_stft_batched(
            p['iq_path'], srate, max_seconds=plot_seconds,
            amplitude_mode=amplitude_mode,
        )

        nPdb  = noise_Pdb_map.get(round(srate))
        # Resize noise_Pdb to match this signal's frequency bins if shapes differ
        if nPdb is not None and nPdb.shape[0] != Pdb.shape[0]:
            nPdb = None     # incompatible rate — fall back to auto

        # In amplitude mode use a softer threshold (1.5σ) so the noise floor
        # is NOT fully masked — the plot shows both the noise baseline and
        # the drone bursts rising above it, which is exactly what you want
        # when illustrating a detection scheme in the thesis.
        k = 1.5 if amplitude_mode else DENOISE_K
        masked = denoise(Pdb, nPdb if not auto_noise else None, k=k)
        panel_data.append({
            "label":  p['label'],
            "freqs":  freqs,
            "times":  times,
            "Pdb":    Pdb,
            "masked": masked,
        })

    # ── Shared colour scale (signal-present bins only, all panels) ────────────
    all_sig = np.concatenate([d["masked"][~np.isnan(d["masked"])].ravel() for d in panel_data])
    if all_sig.size > 0:
        vmin = np.percentile(all_sig, 2)
        vmax = np.percentile(all_sig, 98)
    else:
        vmin, vmax = -80, -30

    # ── Figure ────────────────────────────────────────────────────────────────
    n_panels  = len(panel_data)
    fig_w     = max(8, 5.5 * n_panels + 1.5)
    fig, axes = plt.subplots(1, n_panels, figsize=(fig_w, 5.2), sharey=True)
    if n_panels == 1:
        axes = [axes]

    # Determine centre frequency from sidecar of first file (for title)
    sidecar_0 = panels[0]['iq_path'].with_suffix('.json')
    if sidecar_0.exists():
        meta0  = json.loads(sidecar_0.read_text())
        fc_ghz = float(meta0.get('freq_hz', DEFAULT_FREQ)) / 1e9
        bw_mhz = float(meta0.get('rate_hz', rate)) / 1e6
    else:
        fc_ghz = DEFAULT_FREQ / 1e9
        bw_mhz = rate / 1e6

    k_used  = 1.5 if amplitude_mode else DENOISE_K
    z_label = "Amplitude (dBFS)" if amplitude_mode else "Power (dB)"
    mode_tag = f"amplitude, noise floor + {k_used}σ" if amplitude_mode else f"power, denoised (noise floor + {k_used}σ)"
    fig.suptitle(
        f"OcuSync RF Spectrogram — DJI Matrice 300 RTK\n"
        f"centre {fc_ghz:.4f} GHz  ·  {bw_mhz:.0f} MHz BW  ·  "
        f"STFT L={STFT_NPERSEG} hop={STFT_NPERSEG-STFT_NOVERLAP}  ·  {mode_tag}",
        fontsize=11, fontweight='bold',
    )
    fig.subplots_adjust(left=0.08, right=0.87, top=0.84, bottom=0.12, wspace=0.10)

    cmap = plt.cm.plasma.copy()
    cmap.set_bad(color='black')

    images = []
    for ax, d in zip(axes, panel_data):
        im = ax.imshow(
            d["masked"],
            aspect        = 'auto',
            origin        = 'lower',
            extent        = [d["times"][0], d["times"][-1],
                             d["freqs"][0],  d["freqs"][-1]],
            cmap          = cmap,
            vmin          = vmin,
            vmax          = vmax,
            interpolation = 'nearest',
        )
        images.append(im)

        if display_xlim is not None:
            ax.set_xlim(display_xlim)
        if display_ylim is not None:
            ax.set_ylim(display_ylim)

        ax.set_xlabel("Time (s)", fontsize=10)
        ax.set_title(d["label"], fontsize=11, pad=5)
        ax.yaxis.set_major_locator(ticker.MultipleLocator(5))
        ax.axhline(0, color='white', lw=0.5, ls='--', alpha=0.4)   # DC marker

    axes[0].set_ylabel("Frequency offset from centre (MHz)", fontsize=10)

    cbar_ax = fig.add_axes([0.895, 0.12, 0.018, 0.72])
    cb      = fig.colorbar(images[-1], cax=cbar_ax, label=z_label)
    cb.ax.tick_params(labelsize=9)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\nSaved: {out_path}")


def do_plot(signal_paths: list[Path], labels: list[str],
            no_drone_path: Path | None, rate: float,
            auto_noise: bool, out_path: Path,
            amplitude_mode: bool = False,
            plot_seconds: float = 5.0,
            display_xlim: tuple | None = None,
            display_ylim: tuple | None = None) -> None:
    """Entry point for plot mode."""
    if len(labels) < len(signal_paths):
        # Auto-generate labels from filenames
        labels = labels + [p.stem for p in signal_paths[len(labels):]]

    panels = [{"iq_path": p, "label": l} for p, l in zip(signal_paths, labels)]
    plot_panels(panels, no_drone_path, rate, out_path, auto_noise,
                amplitude_mode=amplitude_mode, plot_seconds=plot_seconds,
                display_xlim=display_xlim, display_ylim=display_ylim)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest='mode', required=True)

    # ── record ────────────────────────────────────────────────────────────────
    rec = sub.add_parser('record', help='Record wideband IQ (one recording)')
    rec.add_argument('--state',    default='recording',
                     help='Label for this recording (default: recording)')
    rec.add_argument('--duration', type=float, default=DEFAULT_DURATION,
                     help=f'Recording length in seconds (default {DEFAULT_DURATION})')
    rec.add_argument('--rate',     type=float, default=DEFAULT_RATE,
                     help=f'Sample rate Hz (default {DEFAULT_RATE/1e6:.0f}e6 = 40 MHz). '
                          'B210 max ~56e6; 40e6 recommended for USB 3.0.')
    rec.add_argument('--freq',     type=float, default=DEFAULT_FREQ,
                     help=f'Centre frequency Hz (default {DEFAULT_FREQ/1e9:.4f}e9 = '
                          'centre of OcuSync 5.725–5.850 GHz hop range)')
    rec.add_argument('--gain',     type=int,   default=DEFAULT_GAIN,
                     help=f'RX gain dB (default {DEFAULT_GAIN})')
    rec.add_argument('--output',   type=Path,  default=OUTPUT_DIR,
                     help=f'Output directory (default {OUTPUT_DIR})')

    # backward-compat alias so old --state=required still works
    cap = sub.add_parser('capture', help=argparse.SUPPRESS)
    cap.add_argument('--state',    default='recording')
    cap.add_argument('--duration', type=float, default=DEFAULT_DURATION)
    cap.add_argument('--rate',     type=float, default=DEFAULT_RATE)
    cap.add_argument('--freq',     type=float, default=DEFAULT_FREQ)
    cap.add_argument('--gain',     type=int,   default=DEFAULT_GAIN)
    cap.add_argument('--output',   type=Path,  default=OUTPUT_DIR)

    # ── plot ──────────────────────────────────────────────────────────────────
    pl = sub.add_parser('plot', help='STFT spectrogram — drone bands visible in result')
    pl.add_argument('--iq',        type=Path, nargs='+', metavar='FILE',
                    help='One or more .iq files to plot (up to 3 shown side-by-side). '
                         'Drone present = bright horizontal bands. No drone = black.')
    # keep old name as alias
    pl.add_argument('--signal',    type=Path, nargs='*', default=None, help=argparse.SUPPRESS)
    pl.add_argument('--labels',    type=str,  nargs='*', default=[],
                    help='Panel title(s) — auto-derived from filename if omitted')
    pl.add_argument('--noise',     type=Path, default=None, metavar='NOISE_FILE',
                    help='Optional separate no-drone .iq file for more accurate noise floor '
                         '(auto-estimated from recording itself if omitted)')
    pl.add_argument('--no_drone',  type=Path, default=None, help=argparse.SUPPRESS)
    pl.add_argument('--rate',      type=float, default=DEFAULT_RATE,
                    help='Fallback sample rate if no JSON sidecar is present')
    pl.add_argument('--output',    type=Path,
                    default=Path(__file__).parent / 'wideband_iq' / 'spectrogram.png',
                    help='Output PNG path')
    pl.add_argument('--amplitude', action='store_true',
                    help='Plot 20·log₁₀(|STFT|) amplitude instead of power, '
                         'with softer denoising (1.5σ) so the noise floor '
                         'stays visible — better for illustrating detection SNR')
    pl.add_argument('--plot-seconds', type=float, default=5.0, metavar='SECS',
                    help='Seconds of IQ to include in the spectrogram (default 5.0). '
                         'A temporal stride is auto-applied to keep the output '
                         'matrix ≤ 350 MB regardless of duration.')
    pl.add_argument('--xlim', type=float, nargs=2, metavar=('T_START', 'T_END'),
                    default=None,
                    help='Crop the displayed time axis (seconds). '
                         'e.g. --xlim 0 0.15 shows first 150 ms.')
    pl.add_argument('--ylim', type=float, nargs=2, metavar=('F_LOW', 'F_HIGH'),
                    default=None,
                    help='Crop the displayed frequency axis (MHz offset from centre). '
                         'e.g. --ylim -12 8')

    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.mode in ('record', 'capture'):
        do_capture(
            state      = args.state,
            duration   = args.duration,
            rate       = args.rate,
            freq       = args.freq,
            gain       = args.gain,
            output_dir = args.output,
        )

    elif args.mode == 'plot':
        # --iq is primary; fall back to legacy --signal
        signal_paths = [Path(p) for p in (args.iq or args.signal or [])]
        if not signal_paths:
            sys.exit('ERROR: provide at least one .iq file with --iq   e.g. --iq wideband_iq/recording_*.iq')
        for sp in signal_paths:
            if not sp.exists():
                sys.exit(f'ERROR: file not found: {sp}')

        # --noise is primary; fall back to legacy --no_drone
        noise_path = args.noise or args.no_drone
        if noise_path:
            noise_path = Path(noise_path)
            if not noise_path.exists():
                sys.exit(f'ERROR: noise reference file not found: {noise_path}')

        xlim = tuple(args.xlim) if args.xlim is not None else None
        ylim = tuple(args.ylim) if args.ylim is not None else None
        do_plot(
            signal_paths   = signal_paths,
            labels         = args.labels,
            no_drone_path  = noise_path,
            rate           = args.rate,
            auto_noise     = (noise_path is None),   # always auto when no reference given
            out_path       = args.output,
            amplitude_mode = getattr(args, 'amplitude', False),
            plot_seconds   = getattr(args, 'plot_seconds', 5.0),
            display_xlim   = xlim,
            display_ylim   = ylim,
        )


if __name__ == '__main__':
    main()
