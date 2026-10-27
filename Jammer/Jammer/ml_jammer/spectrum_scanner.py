#!/usr/bin/env python3
"""
OcuSync Wideband Spectrum Scanner + Sweep Jammer
====================================================
Uses USRP B210 to monitor the full 5.8 GHz OcuSync band and optionally
jam ALL channels by sweep-hopping TX across 3 positions.

Sweep-Jam Mode (--sweep-jam):
  TX hops between 3 center frequencies (5745, 5788, 5831 MHz) at 56 MHz BW,
  dwelling ~5ms per position. This covers ALL 6 OcuSync channels with 100%
  coverage each. The drone has NOWHERE to hide in the 5.8 GHz band.

Single-Channel Jam Mode (--jam-channel N):
  TX stays fixed on one channel's center frequency with 20 MHz BW.
  Used for targeted jamming or ML training data collection.

In both modes, TX pauses briefly for clean RX measurements (pulsed TX/RX).

Usage:
    # Monitor only (no jamming)
    python3 spectrum_scanner.py --duration 60

    # Sweep-jam ALL channels while recording
    python3 spectrum_scanner.py --duration 120 --sweep-jam --output hop_data/sweep_jam.csv

    # Jam single channel while recording
    python3 spectrum_scanner.py --duration 120 --jam-channel 1 --output hop_data/jam_ch1.csv
"""

import argparse
import csv
import numpy as np
import os
import signal
import sys
import time
import threading
from datetime import datetime

try:
    import uhd
except ImportError:
    print("ERROR: UHD Python API not found. Install with: sudo apt install python3-uhd")
    sys.exit(1)

from channel_config import (
    OCUSYNC_5GHZ_CHANNELS,
    SINGLE_CENTER_FREQ, SINGLE_SAMP_RATE,
    FFT_SIZE, AVERAGING_WINDOWS,
    DETECTION_THRESHOLD_DB,
    CHANNEL_BANDWIDTH,
    USRP_SERIAL, RX_ANTENNA, TX_ANTENNA,
    DEFAULT_RX_GAIN, DEFAULT_TX_GAIN,
    SWEEP_JAM_POSITIONS, SWEEP_JAM_TX_BW, SWEEP_JAM_TX_RATE,
    SWEEP_DWELL_MS, SWEEP_RETUNE_MS,
    RX_SWEEP_POSITIONS,
    freq_to_fft_bins, get_freq_from_channel,
)

# ─────────────────────────────────────────────────────────────
# Globals for clean shutdown
# ─────────────────────────────────────────────────────────────
running = True


def signal_handler(sig, frame):
    global running
    print("\n[!] Stopping scanner...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ─────────────────────────────────────────────────────────────
# Core Scanner Class
# ─────────────────────────────────────────────────────────────
class SpectrumScanner:
    """
    Wideband spectrum scanner for OcuSync 5.8 GHz channels.
    Supports sweep-jamming across 3 TX positions to cover all 6 channels.
    """

    def __init__(self, center_freq=SINGLE_CENTER_FREQ, samp_rate=SINGLE_SAMP_RATE,
                 rx_gain=DEFAULT_RX_GAIN, fft_size=FFT_SIZE):
        self.center_freq = center_freq
        self.samp_rate = samp_rate
        self.rx_gain = rx_gain
        self.fft_size = fft_size

        # Pre-compute FFT bin ranges for each OcuSync channel
        self.channel_bins = {}
        self._recompute_channel_bins(center_freq)

        # Window function for FFT
        self.window = np.hanning(fft_size)

        # USRP handle (initialized on start)
        self.usrp = None
        self.streamer = None
        self.tx_streamer = None
        self.tx_active = False
        self.tx_paused = False
        self.tx_thread = None
        self.tx_lock = threading.Lock()

        # Sweep-jam state
        self.sweep_jam_enabled = False
        self.sweep_positions = SWEEP_JAM_POSITIONS
        self.sweep_dwell_s = SWEEP_DWELL_MS / 1000.0
        self.sweep_retune_s = SWEEP_RETUNE_MS / 1000.0
        self.current_sweep_pos = 0
        self.sweep_stats = {f: 0 for f in SWEEP_JAM_POSITIONS}

        # Pre-generate WIDEBAND noise buffer for sweep-jam (56 MHz BW)
        bufsize = 32768
        self.noise_buffer_wide = (np.random.normal(0, 0.9, bufsize) +
                                  1j * np.random.normal(0, 0.9, bufsize)).astype(np.complex64)
        self.noise_buffer_wide = np.clip(self.noise_buffer_wide.real, -1.0, 1.0) + \
                                 1j * np.clip(self.noise_buffer_wide.imag, -1.0, 1.0)
        self.noise_buffer_wide = self.noise_buffer_wide.astype(np.complex64)

        # Narrower buffer for single-channel jam (20 MHz)
        self.noise_buffer_narrow = (np.random.normal(0, 0.9, 16384) +
                                    1j * np.random.normal(0, 0.9, 16384)).astype(np.complex64)
        self.noise_buffer_narrow = np.clip(self.noise_buffer_narrow.real, -1.0, 1.0) + \
                                   1j * np.clip(self.noise_buffer_narrow.imag, -1.0, 1.0)
        self.noise_buffer_narrow = self.noise_buffer_narrow.astype(np.complex64)

        # Noise floor estimate (updated dynamically)
        self.noise_floor_db = -80.0

    def _recompute_channel_bins(self, center_freq):
        """Recompute FFT bin ranges for a given RX center frequency."""
        self.channel_bins = {}
        for ch_num, ch_freq in OCUSYNC_5GHZ_CHANNELS.items():
            bins = freq_to_fft_bins(center_freq, self.samp_rate, self.fft_size,
                                    ch_freq, CHANNEL_BANDWIDTH)
            freq_low = center_freq - self.samp_rate / 2
            freq_high = center_freq + self.samp_rate / 2
            # Include channel if its center is within our BW (with some margin)
            if ch_freq >= (freq_low + CHANNEL_BANDWIDTH/4) and ch_freq <= (freq_high - CHANNEL_BANDWIDTH/4):
                self.channel_bins[ch_num] = bins

    def init_usrp(self):
        """Initialize USRP B210 for RX."""
        print(f"[*] Initializing USRP ({USRP_SERIAL})...")
        self.usrp = uhd.usrp.MultiUSRP(USRP_SERIAL)
        self.usrp.set_rx_rate(self.samp_rate)
        self.usrp.set_rx_freq(uhd.libpyuhd.types.tune_request(self.center_freq))
        self.usrp.set_rx_antenna(RX_ANTENNA)
        self.usrp.set_rx_gain(self.rx_gain)
        self.usrp.set_rx_bandwidth(self.samp_rate)

        # Create RX streamer
        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        self.streamer = self.usrp.get_rx_stream(st_args)

        # Issue stream command
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        stream_cmd.stream_now = True
        self.streamer.issue_stream_cmd(stream_cmd)

        print(f"[+] USRP RX initialized: {self.center_freq/1e9:.4f} GHz, {self.samp_rate/1e6:.0f} MHz BW")
        print(f"[*] Scanner covers channels: {sorted(self.channel_bins.keys())}")

    def init_usrp_tx_single(self, tx_freq, tx_gain=DEFAULT_TX_GAIN):
        """Initialize USRP TX chain for jamming a SINGLE channel (20 MHz)."""
        print(f"[*] Initializing TX (single): {tx_freq/1e9:.4f} GHz, gain={tx_gain}")
        self.usrp.set_tx_rate(20e6)
        self.usrp.set_tx_freq(uhd.libpyuhd.types.tune_request(tx_freq))
        self.usrp.set_tx_antenna(TX_ANTENNA)
        self.usrp.set_tx_gain(tx_gain)
        self.usrp.set_tx_bandwidth(20e6)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        self.tx_streamer = self.usrp.get_tx_stream(st_args)
        self.tx_active = True
        self.sweep_jam_enabled = False
        print(f"[+] TX initialized: {tx_freq/1e9:.4f} GHz, {tx_gain} dB, 20 MHz BW")

    def init_usrp_tx_sweep(self, tx_gain=DEFAULT_TX_GAIN):
        """Initialize USRP TX chain for SWEEP jamming (56 MHz, hops 3 positions)."""
        first_pos = self.sweep_positions[0]
        print(f"[*] Initializing TX (sweep-jam): starting at {first_pos/1e9:.4f} GHz, gain={tx_gain}")

        # Set TX to max bandwidth for wideband noise
        self.usrp.set_tx_rate(SWEEP_JAM_TX_RATE)
        self.usrp.set_tx_freq(uhd.libpyuhd.types.tune_request(first_pos))
        self.usrp.set_tx_antenna(TX_ANTENNA)
        self.usrp.set_tx_gain(tx_gain)
        self.usrp.set_tx_bandwidth(SWEEP_JAM_TX_BW)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        self.tx_streamer = self.usrp.get_tx_stream(st_args)
        self.tx_active = True
        self.sweep_jam_enabled = True
        self.current_sweep_pos = 0

        cycle_ms = len(self.sweep_positions) * (SWEEP_DWELL_MS + SWEEP_RETUNE_MS)
        print(f"[+] TX sweep-jam initialized: {tx_gain} dB, {SWEEP_JAM_TX_BW/1e6:.0f} MHz BW")
        print(f"[+] Sweep positions: {[f'{f/1e6:.1f} MHz' for f in self.sweep_positions]}")
        print(f"[+] Dwell: {SWEEP_DWELL_MS}ms/pos, cycle: {cycle_ms:.0f}ms")

    def _tx_worker_single(self):
        """Background thread: transmit noise on a fixed frequency."""
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue
            try:
                self.tx_streamer.send(self.noise_buffer_narrow, metadata)
            except Exception:
                break

    def _tx_worker_sweep(self):
        """Background thread: sweep-jam across 3 TX positions.

        Hops TX frequency every SWEEP_DWELL_MS milliseconds, cycling through
        all positions. Each position gets 56 MHz of wideband noise.
        Pauses during RX measurement windows.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False

        pos_idx = 0
        num_positions = len(self.sweep_positions)

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue

            # Retune to current sweep position
            target_freq = self.sweep_positions[pos_idx]
            try:
                self.usrp.set_tx_freq(uhd.libpyuhd.types.tune_request(target_freq))
            except Exception:
                break

            # Brief settling time for PLL lock
            time.sleep(self.sweep_retune_s)

            if not self.tx_active or self.tx_paused:
                continue

            # Transmit noise for dwell period
            dwell_start = time.monotonic()
            while (time.monotonic() - dwell_start) < self.sweep_dwell_s:
                if not self.tx_active or self.tx_paused:
                    break
                try:
                    self.tx_streamer.send(self.noise_buffer_wide, metadata)
                except Exception:
                    break

            self.sweep_stats[target_freq] = self.sweep_stats.get(target_freq, 0) + 1

            # Move to next position
            pos_idx = (pos_idx + 1) % num_positions

    def start_tx_thread(self):
        """Start TX in a background thread."""
        if not self.tx_active or self.tx_streamer is None:
            return
        self.tx_paused = False

        if self.sweep_jam_enabled:
            worker = self._tx_worker_sweep
            mode = "sweep-jam (3 positions)"
        else:
            worker = self._tx_worker_single
            mode = "single-channel"

        self.tx_thread = threading.Thread(target=worker, daemon=True)
        self.tx_thread.start()
        print(f"[+] TX thread started ({mode})")

    def pause_tx(self):
        """Pause TX transmission for clean RX measurement."""
        self.tx_paused = True
        time.sleep(0.005)  # Wait 5ms for TX buffers to drain

    def resume_tx(self):
        """Resume TX transmission after RX measurement."""
        self.tx_paused = False

    def receive_samples(self):
        """Receive one FFT-sized block of IQ samples."""
        num_samps = self.fft_size
        buffer = np.zeros(num_samps, dtype=np.complex64)
        metadata = uhd.types.RXMetadata()

        received = 0
        while received < num_samps:
            samps = self.streamer.recv(buffer[received:], metadata)
            if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                if metadata.error_code == uhd.types.RXMetadataErrorCode.overflow:
                    continue
                else:
                    print(f"[!] RX Error: {metadata.error_code}")
                    break
            received += samps

        return buffer

    def retune_rx(self, new_center):
        """Retune the RX to a new center frequency and recompute channel bins."""
        for attempt in range(3):
            try:
                self.usrp.set_rx_freq(uhd.libpyuhd.types.tune_request(new_center))
                break
            except RuntimeError as e:
                if 'PLL' in str(e) and attempt < 2:
                    time.sleep(0.005)  # Wait for PLL to settle
                    continue
                raise
        self.center_freq = new_center
        self._recompute_channel_bins(new_center)
        # Flush stale samples after retune
        buffer = np.zeros(self.fft_size, dtype=np.complex64)
        metadata = uhd.types.RXMetadata()
        for _ in range(3):
            self.streamer.recv(buffer, metadata)

    def compute_channel_power(self, samples):
        """Compute power spectral density and return per-channel power in dB."""
        windowed = samples * self.window
        spectrum = np.fft.fftshift(np.fft.fft(windowed, self.fft_size))
        power_spectrum = np.abs(spectrum) ** 2 / self.fft_size
        power_db = 10 * np.log10(power_spectrum + 1e-20)

        channel_power = {}
        for ch_num, (bin_low, bin_high) in self.channel_bins.items():
            if bin_high > bin_low:
                ch_power = np.mean(power_db[bin_low:bin_high])
                channel_power[ch_num] = ch_power

        return channel_power

    def compute_averaged_power(self, num_averages=AVERAGING_WINDOWS):
        """Peak-hold detection across multiple FFT frames."""
        accumulated = None
        for _ in range(num_averages):
            samples = self.receive_samples()
            power = self.compute_channel_power(samples)
            if accumulated is None:
                accumulated = {ch: [] for ch in power}
            for ch, pwr in power.items():
                if ch not in accumulated:
                    accumulated[ch] = []
                accumulated[ch].append(pwr)

        peak_power = {}
        for ch, powers in accumulated.items():
            peak_power[ch] = np.max(powers)

        self._last_mean_power = {ch: np.mean(powers) for ch, powers in accumulated.items()}
        return peak_power

    def compute_full_band_power(self, num_averages=AVERAGING_WINDOWS):
        """
        Sweep RX across 2 positions to cover all 6 channels.
        Returns power for ALL channels by combining two RX measurements.
        """
        all_power = {}
        all_mean = {}

        for rx_center in RX_SWEEP_POSITIONS:
            self.retune_rx(rx_center)
            time.sleep(0.002)  # PLL settle

            accumulated = None
            for _ in range(num_averages):
                samples = self.receive_samples()
                power = self.compute_channel_power(samples)
                if accumulated is None:
                    accumulated = {ch: [] for ch in power}
                for ch, pwr in power.items():
                    if ch not in accumulated:
                        accumulated[ch] = []
                    accumulated[ch].append(pwr)

            for ch, powers in accumulated.items():
                peak = np.max(powers)
                mean = np.mean(powers)
                if ch not in all_power or peak > all_power[ch]:
                    all_power[ch] = peak
                    all_mean[ch] = mean

        self._last_mean_power = all_mean
        return all_power

    def update_noise_floor(self, channel_power):
        """Dynamically estimate noise floor from mean power of quietest channels."""
        ref_power = getattr(self, '_last_mean_power', channel_power)
        if len(ref_power) >= 3:
            sorted_powers = sorted(ref_power.values())
            self.noise_floor_db = np.mean(sorted_powers[:3])

    def find_drone_peak(self, num_averages=2):
        """
        Scan full ISM band (5725.5-5849.5 MHz) and find the strongest 20 MHz
        peak using raw FFT — no predefined channel map.
        
        Uses 2 RX positions at 56 MHz BW each to cover the full 124 MHz band:
          Pos 1: 5760 MHz → covers 5732-5788 MHz
          Pos 2: 5820 MHz → covers 5792-5848 MHz
        
        IMPORTANT: Discards the outer 15% of each position's FFT to avoid
        band-edge rolloff artifacts that create fake peaks at 5735/5845 MHz.
        
        Slides a 20 MHz window across the stitched spectrum, finds the peak.
        
        Returns:
            (peak_freq_hz, peak_snr_db, band_power_dbm)
            peak_freq_hz: center of the 20 MHz window with highest power
            peak_snr_db: SNR of that window above noise floor
            band_power_dbm: dict mapping freq_mhz → power_db for display
            Returns (None, 0, {}) if no peak found above threshold.
        """
        BAND_LOW = 5725.5e6
        BAND_HIGH = 5849.5e6
        SCAN_POSITIONS = RX_SWEEP_POSITIONS  # [5760e6, 5820e6]
        WINDOW_BW = 20e6  # Drone uses 20 MHz wide channels
        STEP_HZ = 1e6     # 1 MHz steps for finer resolution
        EDGE_TRIM = 0.15  # Discard outer 15% of FFT bins (band-edge rolloff)
        
        # Collect raw FFT spectra from each position
        spectra = {}  # freq_hz → power_db
        freq_res = self.samp_rate / self.fft_size
        
        # Usable bin range (trim edges)
        trim_bins = int(self.fft_size * EDGE_TRIM)
        bin_start = trim_bins
        bin_end = self.fft_size - trim_bins
        
        for rx_center in SCAN_POSITIONS:
            self.retune_rx(rx_center)
            time.sleep(0.001)  # 1ms settle — PLL locks fast
            
            # Average multiple FFT frames (mean, not peak hold)
            power_acc = None
            for _ in range(num_averages):
                samples = self.receive_samples()
                windowed = samples * self.window
                spectrum = np.fft.fftshift(np.fft.fft(windowed, self.fft_size))
                power = np.abs(spectrum) ** 2 / self.fft_size
                power_db = 10 * np.log10(power + 1e-20)
                if power_acc is None:
                    power_acc = power_db.copy()
                else:
                    power_acc += power_db
            power_acc /= num_averages  # Mean averaging — more stable than peak hold
            
            # Map only USABLE (trimmed) FFT bins to actual frequencies
            for i in range(bin_start, bin_end):
                freq_hz = rx_center + (i - self.fft_size / 2) * freq_res
                # Only keep bins within the ISM band
                if BAND_LOW <= freq_hz <= BAND_HIGH:
                    # Keep the best reading if overlapping positions
                    if freq_hz not in spectra or power_acc[i] > spectra[freq_hz]:
                        spectra[freq_hz] = power_acc[i]
        
        if not spectra:
            return None, 0, {}
        
        # Sort by frequency for sliding window
        sorted_freqs = sorted(spectra.keys())
        sorted_powers = np.array([spectra[f] for f in sorted_freqs])
        sorted_freqs = np.array(sorted_freqs)
        
        # Compute noise floor from quietest 10% of bins (avoid drone signal)
        noise_floor = np.percentile(sorted_powers, 10)
        self.noise_floor_db = noise_floor
        
        # Slide 20 MHz window across the band with edge guard
        # Don't allow peak centers within 15 MHz of band edges (artifact zone)
        EDGE_GUARD = 15e6
        SEARCH_LOW = BAND_LOW + EDGE_GUARD   # 5740.5 MHz
        SEARCH_HIGH = BAND_HIGH - EDGE_GUARD  # 5834.5 MHz
        
        best_center = None
        best_power = -999
        best_snr = 0
        
        window_center = SEARCH_LOW
        while window_center <= SEARCH_HIGH:
            mask = (sorted_freqs >= window_center - WINDOW_BW / 2) & \
                   (sorted_freqs <= window_center + WINDOW_BW / 2)
            if np.sum(mask) > 10:  # Need enough bins for a meaningful average
                window_power = np.mean(sorted_powers[mask])
                snr = window_power - noise_floor
                if window_power > best_power:
                    best_power = window_power
                    best_center = window_center
                    best_snr = snr
            window_center += STEP_HZ
        
        # Build simplified band power for display (10 MHz bins)
        band_power = {}
        for f_mhz in range(5726, 5850, 10):
            f_hz = f_mhz * 1e6
            mask = (sorted_freqs >= f_hz - 5e6) & (sorted_freqs <= f_hz + 5e6)
            if np.sum(mask) > 0:
                band_power[f_mhz] = float(np.mean(sorted_powers[mask]))
        
        return best_center, best_snr, band_power

    def detect_active_channel(self, channel_power):
        """Determine which channel(s) the drone is actively using."""
        self.update_noise_floor(channel_power)

        active = []
        for ch, pwr in channel_power.items():
            snr = pwr - self.noise_floor_db
            if snr >= DETECTION_THRESHOLD_DB:
                active.append((ch, pwr, snr))

        active.sort(key=lambda x: x[1], reverse=True)
        return active

    def stop(self):
        """Stop USRP RX and TX streaming."""
        if self.tx_active:
            self.tx_active = False
            if self.tx_thread and self.tx_thread.is_alive():
                self.tx_thread.join(timeout=2.0)
                print("[*] TX thread stopped")
            if self.tx_streamer:
                try:
                    md = uhd.types.TXMetadata()
                    md.end_of_burst = True
                    self.tx_streamer.send(np.zeros(1, dtype=np.complex64), md)
                except Exception as e:
                    print(f"[!] TX cleanup: {e}")
            if self.sweep_jam_enabled:
                print("[*] Sweep-jam stats:")
                for freq, count in sorted(self.sweep_stats.items()):
                    print(f"    {freq/1e6:.1f} MHz: {count} dwells")
            print("[*] TX stopped")
        if self.streamer:
            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
            self.streamer.issue_stream_cmd(stream_cmd)
        print("[*] Scanner stopped")


# ─────────────────────────────────────────────────────────────
# Display Helpers
# ─────────────────────────────────────────────────────────────

def print_channel_bar(channel_power, active_channels, noise_floor, jam_info=""):
    """Print a visual bar chart of channel power levels."""
    active_ch_nums = {ch for ch, _, _ in active_channels}

    print("\033[2J\033[H")  # Clear terminal
    print("=" * 70)
    print("  OcuSync 5.8 GHz Spectrum Scanner — Real-Time Channel Monitor")
    print(f"  Noise Floor: {noise_floor:.1f} dB  |  Threshold: +{DETECTION_THRESHOLD_DB:.0f} dB")
    if jam_info:
        print(f"  Jamming: {jam_info}")
    print("=" * 70)

    for ch in sorted(channel_power.keys()):
        pwr = channel_power[ch]
        snr = pwr - noise_floor

        bar_len = max(0, int(snr * 2))
        bar_len = min(bar_len, 50)

        if ch in active_ch_nums:
            marker = ">>> "
            bar = "█" * bar_len
            color = "\033[91m"  # Red for active
        else:
            marker = "    "
            bar = "▒" * bar_len
            color = "\033[90m"  # Gray for inactive

        reset = "\033[0m"
        freq_ghz = OCUSYNC_5GHZ_CHANNELS[ch] / 1e9
        edges = f"{(OCUSYNC_5GHZ_CHANNELS[ch]-10e6)/1e6:.1f}-{(OCUSYNC_5GHZ_CHANNELS[ch]+10e6)/1e6:.1f}"
        print(f"  {marker}CH{ch} ({edges}) | {color}{bar}{reset} {pwr:.1f} dB (SNR: {snr:+.1f})")

    print("=" * 70)

    if active_channels:
        primary = active_channels[0]
        print(f"  🎯 ACTIVE: CH{primary[0]} @ {OCUSYNC_5GHZ_CHANNELS[primary[0]]/1e9:.4f} GHz "
              f"(SNR: {primary[2]:.1f} dB)")
    else:
        print("  ⚠️  No drone signal detected")

    print(f"  Time: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    print("=" * 70)


# ─────────────────────────────────────────────────────────────
# Main Scanner Loop
# ─────────────────────────────────────────────────────────────

def run_scanner(args):
    """Main scanning loop."""
    global running

    scanner = SpectrumScanner(
        center_freq=args.center_freq,
        samp_rate=args.samp_rate,
        rx_gain=args.rx_gain,
        fft_size=args.fft_size,
    )

    # Initialize USRP
    scanner.init_usrp()

    # Determine jam mode
    jam_info = ""
    is_jamming = False

    if args.sweep_jam:
        scanner.init_usrp_tx_sweep(tx_gain=args.tx_gain)
        scanner.start_tx_thread()
        jam_info = f"SWEEP-JAM all channels @ {args.tx_gain} dB"
        is_jamming = True
        print(f"[!] SWEEP-JAM ACTIVE — covering ALL 6 channels!")
    elif args.jam_channel:
        jam_freq = get_freq_from_channel(args.jam_channel)
        if jam_freq:
            scanner.init_usrp_tx_single(jam_freq, tx_gain=args.tx_gain)
            scanner.start_tx_thread()
            jam_info = f"CH{args.jam_channel} @ {jam_freq/1e9:.4f} GHz, {args.tx_gain} dB"
            is_jamming = True
            print(f"[!] JAMMING CH{args.jam_channel} @ {jam_freq/1e9:.4f} GHz")
        else:
            print(f"[!] Invalid channel: {args.jam_channel}")

    # Determine if we need RX sweeping
    use_rx_sweep = args.rx_sweep

    # CSV output
    csv_file = None
    csv_writer = None
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else '.', exist_ok=True)
        csv_file = open(args.output, 'w', newline='')
        fieldnames = ['timestamp', 'active_channel', 'active_snr',
                      'jam_mode'] + [f'ch{ch}_power' for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys())]
        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        csv_writer.writeheader()
        print(f"[*] Saving data to: {args.output}")

    start_time = time.time()
    scan_count = 0

    try:
        while running:
            elapsed = time.time() - start_time
            if args.duration and elapsed >= args.duration:
                print(f"\n[*] Duration reached ({args.duration}s)")
                break

            # Pulsed TX/RX: pause TX → clean RX scan → resume TX
            if is_jamming:
                scanner.pause_tx()

            # Get channel power
            if use_rx_sweep:
                channel_power = scanner.compute_full_band_power(num_averages=args.averages)
            else:
                channel_power = scanner.compute_averaged_power(num_averages=args.averages)

            active = scanner.detect_active_channel(channel_power)
            scan_count += 1

            if is_jamming:
                scanner.resume_tx()

            # Display
            if not args.quiet:
                print_channel_bar(channel_power, active, scanner.noise_floor_db, jam_info)

            # Log to CSV
            if csv_writer:
                jam_mode_str = "sweep" if args.sweep_jam else (f"ch{args.jam_channel}" if args.jam_channel else "none")
                row = {
                    'timestamp': datetime.now().isoformat(),
                    'active_channel': active[0][0] if active else 0,
                    'active_snr': round(active[0][2], 2) if active else 0,
                    'jam_mode': jam_mode_str,
                }
                for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                    row[f'ch{ch}_power'] = round(channel_power.get(ch, -100), 2)
                csv_writer.writerow(row)

            time.sleep(args.interval)

    finally:
        scanner.stop()
        if csv_file:
            csv_file.close()
            print(f"[+] Saved {scan_count} scans to {args.output}")

    return scan_count


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OcuSync 5.8 GHz Spectrum Scanner + Sweep Jammer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic monitoring (live display)
  python3 spectrum_scanner.py --duration 60

  # Sweep-jam ALL channels (covers entire 5.8 GHz band)
  python3 spectrum_scanner.py --duration 30 --sweep-jam --rx-sweep -q

  # Jam single channel for ML training data
  python3 spectrum_scanner.py --duration 120 --jam-channel 1 --output hop_data/jam_ch1.csv

  # Full-band monitoring with RX sweep (covers all 6 channels)
  python3 spectrum_scanner.py --duration 60 --rx-sweep
        """
    )

    parser.add_argument('--duration', type=float, default=None,
                        help='Scan duration in seconds (default: infinite)')
    parser.add_argument('--output', '-o', type=str, default=None,
                        help='Output CSV file for channel power data')
    parser.add_argument('--center-freq', type=float, default=SINGLE_CENTER_FREQ,
                        help=f'Center frequency in Hz (default: {SINGLE_CENTER_FREQ/1e9:.4f} GHz)')
    parser.add_argument('--samp-rate', type=float, default=SINGLE_SAMP_RATE,
                        help=f'Sample rate in Hz (default: {SINGLE_SAMP_RATE/1e6:.0f} MHz)')
    parser.add_argument('--rx-gain', type=float, default=DEFAULT_RX_GAIN,
                        help=f'RX gain in dB (default: {DEFAULT_RX_GAIN})')
    parser.add_argument('--tx-gain', type=float, default=DEFAULT_TX_GAIN,
                        help=f'TX gain for jamming in dB (default: {DEFAULT_TX_GAIN})')
    parser.add_argument('--fft-size', type=int, default=FFT_SIZE,
                        help=f'FFT size (default: {FFT_SIZE})')
    parser.add_argument('--averages', type=int, default=AVERAGING_WINDOWS,
                        help=f'FFT averages per scan (default: {AVERAGING_WINDOWS})')
    parser.add_argument('--interval', type=float, default=0.05,
                        help='Seconds between scans (default: 0.05)')
    parser.add_argument('--jam-channel', type=int, default=None,
                        help='Single channel to jam (for targeted/training)')
    parser.add_argument('--sweep-jam', action='store_true',
                        help='Sweep-jam ALL channels (hops 3 positions, full band)')
    parser.add_argument('--rx-sweep', action='store_true',
                        help='Sweep RX across 2 positions to cover all 6 channels')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress live display (only log to file)')

    args = parser.parse_args()

    if args.sweep_jam and args.jam_channel:
        print("[!] Cannot use both --sweep-jam and --jam-channel. Pick one.")
        sys.exit(1)

    run_scanner(args)


if __name__ == '__main__':
    main()
