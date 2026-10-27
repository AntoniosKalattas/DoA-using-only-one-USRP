#!/usr/bin/env python3
"""
OcuSync Wideband Spectrum Scanner
====================================
Uses USRP B210 to monitor the full 5.8 GHz OcuSync band simultaneously.
Computes per-channel power levels via FFT and detects which channel the drone
is currently occupying.

This is the "eyes" of the ML jammer — it feeds real-time channel occupancy
data to the channel tracker and ML predictor.

Usage:
    # Monitor and print channel power levels in real-time
    python3 spectrum_scanner.py --duration 60

    # Monitor and save per-channel RSSI to CSV
    python3 spectrum_scanner.py --duration 120 --output hop_data/session1.csv

    # Monitor while jamming channel 5 (to force hops for training data)
    python3 spectrum_scanner.py --duration 120 --jam-channel 5 --output hop_data/jammed.csv
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
    
    Uses a single USRP B210 RX chain centered at 5.8 GHz with 40 MHz bandwidth
    to cover OcuSync channels 3-8 simultaneously. For full 10-channel coverage,
    the scanner can sweep between two center frequencies.
    """

    def __init__(self, center_freq=SINGLE_CENTER_FREQ, samp_rate=SINGLE_SAMP_RATE,
                 rx_gain=DEFAULT_RX_GAIN, fft_size=FFT_SIZE):
        self.center_freq = center_freq
        self.samp_rate = samp_rate
        self.rx_gain = rx_gain
        self.fft_size = fft_size

        # Pre-compute FFT bin ranges for each OcuSync channel
        self.channel_bins = {}
        for ch_num, ch_freq in OCUSYNC_5GHZ_CHANNELS.items():
            bins = freq_to_fft_bins(center_freq, samp_rate, fft_size,
                                    ch_freq, CHANNEL_BANDWIDTH)
            # Only include channels that fall within our bandwidth
            freq_low = center_freq - samp_rate / 2
            freq_high = center_freq + samp_rate / 2
            if ch_freq >= freq_low and ch_freq <= freq_high:
                self.channel_bins[ch_num] = bins

        print(f"[*] Scanner covers channels: {sorted(self.channel_bins.keys())}")
        print(f"[*] Center: {center_freq/1e9:.4f} GHz, BW: {samp_rate/1e6:.0f} MHz")

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

        # Pre-generate noise buffer for TX jamming (Gaussian noise, near full-scale)
        self.noise_buffer = (np.random.normal(0, 0.9, 16384) +
                             1j * np.random.normal(0, 0.9, 16384)).astype(np.complex64)
        # Clip to prevent DAC overflow
        self.noise_buffer = np.clip(self.noise_buffer.real, -1.0, 1.0) + \
                            1j * np.clip(self.noise_buffer.imag, -1.0, 1.0)
        self.noise_buffer = self.noise_buffer.astype(np.complex64)

        # Noise floor estimate (updated dynamically)
        self.noise_floor_db = -80.0

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

        print("[+] USRP initialized and streaming")

    def init_usrp_tx(self, tx_freq, tx_gain=DEFAULT_TX_GAIN):
        """Initialize USRP TX chain for jamming a specific channel."""
        print(f"[*] Initializing TX on {tx_freq/1e9:.4f} GHz, gain={tx_gain}")
        # 20 MHz TX rate to cover the full OcuSync channel + OFDM guard carriers
        self.usrp.set_tx_rate(20e6)
        self.usrp.set_tx_freq(uhd.libpyuhd.types.tune_request(tx_freq))
        self.usrp.set_tx_antenna(TX_ANTENNA)
        self.usrp.set_tx_gain(tx_gain)
        self.usrp.set_tx_bandwidth(20e6)

        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        self.tx_streamer = self.usrp.get_tx_stream(st_args)
        self.tx_active = True
        print(f"[+] TX initialized: {tx_freq/1e9:.4f} GHz, {tx_gain} dB, 20 MHz BW")

    def _tx_worker(self):
        """Background thread: transmit noise in duty-cycled bursts.
        
        Sends noise continuously until tx_paused is set, then waits.
        This allows clean RX measurements between TX bursts.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)  # Yield while paused
                continue
            try:
                self.tx_streamer.send(self.noise_buffer, metadata)
            except Exception:
                break

    def start_tx_thread(self):
        """Start TX in a background thread with pause/resume control."""
        if not self.tx_active or self.tx_streamer is None:
            return
        self.tx_paused = False
        self.tx_thread = threading.Thread(target=self._tx_worker, daemon=True)
        self.tx_thread.start()
        print("[+] TX thread started (pulsed jamming)")

    def pause_tx(self):
        """Pause TX transmission for clean RX measurement."""
        self.tx_paused = True
        time.sleep(0.005)  # Wait 5ms for TX buffers to drain

    def resume_tx(self):
        """Resume TX transmission after RX measurement."""
        self.tx_paused = False

    def transmit_noise(self):
        """Send one buffer of Gaussian noise on the TX chain (manual call)."""
        if not self.tx_active or self.tx_streamer is None:
            return
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        self.tx_streamer.send(self.noise_buffer, metadata)

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
                    continue  # Overflow is ok, just skip
                else:
                    print(f"[!] RX Error: {metadata.error_code}")
                    break
            received += samps

        return buffer

    def compute_channel_power(self, samples):
        """
        Compute power spectral density and return per-channel power in dB.
        
        Returns:
            dict: {channel_number: power_db}
        """
        # Apply window and compute FFT
        windowed = samples * self.window
        spectrum = np.fft.fftshift(np.fft.fft(windowed, self.fft_size))
        power_spectrum = np.abs(spectrum) ** 2 / self.fft_size

        # Convert to dB
        power_db = 10 * np.log10(power_spectrum + 1e-20)

        # Extract per-channel average power
        channel_power = {}
        for ch_num, (bin_low, bin_high) in self.channel_bins.items():
            if bin_high > bin_low:
                ch_power = np.mean(power_db[bin_low:bin_high])
                channel_power[ch_num] = ch_power

        return channel_power

    def compute_averaged_power(self, num_averages=AVERAGING_WINDOWS):
        """
        Peak-hold detection across multiple FFT frames.
        OcuSync uses bursty transmissions (~600ms intervals) so we take the
        MAXIMUM power per channel across frames rather than the mean.
        This prevents averaging away short bursts.
        """
        accumulated = None
        for _ in range(num_averages):
            samples = self.receive_samples()
            power = self.compute_channel_power(samples)
            if accumulated is None:
                accumulated = {ch: [] for ch in power}
            for ch, pwr in power.items():
                accumulated[ch].append(pwr)

        # Peak-hold: use max to catch bursts, but also track mean for noise floor
        peak_power = {}
        for ch, powers in accumulated.items():
            peak_power[ch] = np.max(powers)

        # Store mean for noise floor estimation
        self._last_mean_power = {ch: np.mean(powers) for ch, powers in accumulated.items()}

        return peak_power

    def update_noise_floor(self, channel_power):
        """Dynamically estimate noise floor from mean power of quietest channels."""
        # Use mean power (not peak) for noise floor to avoid inflating it
        ref_power = getattr(self, '_last_mean_power', channel_power)
        if len(ref_power) >= 3:
            sorted_powers = sorted(ref_power.values())
            # Noise floor = average of 3 quietest channels (using mean, not peak)
            self.noise_floor_db = np.mean(sorted_powers[:3])

    def detect_active_channel(self, channel_power):
        """
        Determine which channel(s) the drone is actively using.
        
        Returns:
            list of (channel_number, power_db, snr_db) tuples, sorted by power descending
        """
        self.update_noise_floor(channel_power)

        active = []
        for ch, pwr in channel_power.items():
            snr = pwr - self.noise_floor_db
            if snr >= DETECTION_THRESHOLD_DB:
                active.append((ch, pwr, snr))

        # Sort by power (strongest first)
        active.sort(key=lambda x: x[1], reverse=True)
        return active

    def stop(self):
        """Stop USRP RX and TX streaming."""
        # Stop TX thread first
        if self.tx_active:
            self.tx_active = False  # Signal thread to stop
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
            print("[*] TX stopped")
        # Stop RX
        if self.streamer:
            stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
            self.streamer.issue_stream_cmd(stream_cmd)
        print("[*] Scanner stopped")


# ─────────────────────────────────────────────────────────────
# Display Helpers
# ─────────────────────────────────────────────────────────────

def print_channel_bar(channel_power, active_channels, noise_floor):
    """Print a visual bar chart of channel power levels."""
    active_ch_nums = {ch for ch, _, _ in active_channels}

    # Clear screen and print header
    print("\033[2J\033[H")  # Clear terminal
    print("=" * 70)
    print("  OcuSync 5.8 GHz Spectrum Scanner — Real-Time Channel Monitor")
    print(f"  Noise Floor: {noise_floor:.1f} dB  |  Threshold: +{DETECTION_THRESHOLD_DB:.0f} dB")
    print("=" * 70)

    for ch in sorted(channel_power.keys()):
        pwr = channel_power[ch]
        snr = pwr - noise_floor

        # Bar width proportional to SNR
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
        print(f"  {marker}CH{ch:2d} ({freq_ghz:.3f}) | {color}{bar}{reset} {pwr:.1f} dB (SNR: {snr:+.1f})")

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

    # Optional: init TX for jamming a channel during data collection
    if args.jam_channel:
        jam_freq = get_freq_from_channel(args.jam_channel)
        if jam_freq:
            scanner.init_usrp_tx(jam_freq, tx_gain=args.tx_gain)
            scanner.start_tx_thread()  # Continuous TX in background
            print(f"[!] JAMMING CH{args.jam_channel} @ {jam_freq/1e9:.4f} GHz (continuous)")

    # CSV output
    csv_file = None
    csv_writer = None
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else '.', exist_ok=True)
        csv_file = open(args.output, 'w', newline='')
        fieldnames = ['timestamp', 'active_channel', 'active_snr',
                      'jam_channel'] + [f'ch{ch}_power' for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys())]
        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        csv_writer.writeheader()
        print(f"[*] Saving data to: {args.output}")

    start_time = time.time()
    scan_count = 0

    try:
        while running:
            # Check duration
            elapsed = time.time() - start_time
            if args.duration and elapsed >= args.duration:
                print(f"\n[*] Duration reached ({args.duration}s)")
                break

            # Pulsed TX/RX: pause TX → clean RX scan → resume TX
            # The drone sees constant noise (TX runs ~90% of the time),
            # but our RX measurements are taken with TX paused to avoid self-interference
            is_jamming = args.jam_channel and scanner.tx_active
            if is_jamming:
                scanner.pause_tx()

            # Get averaged channel power (clean RX measurement)
            channel_power = scanner.compute_averaged_power(num_averages=args.averages)
            active = scanner.detect_active_channel(channel_power)
            scan_count += 1

            if is_jamming:
                scanner.resume_tx()

            # Display
            if not args.quiet:
                print_channel_bar(channel_power, active, scanner.noise_floor_db)

            # Log to CSV
            if csv_writer:
                row = {
                    'timestamp': datetime.now().isoformat(),
                    'active_channel': active[0][0] if active else 0,
                    'active_snr': round(active[0][2], 2) if active else 0,
                    'jam_channel': args.jam_channel or 0,
                }
                for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                    row[f'ch{ch}_power'] = round(channel_power.get(ch, -100), 2)
                csv_writer.writerow(row)

            # Small delay to avoid 100% CPU
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
        description="OcuSync 5.8 GHz Wideband Spectrum Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic monitoring (live display)
  python3 spectrum_scanner.py --duration 60

  # Save channel data for ML training
  python3 spectrum_scanner.py --duration 120 --output hop_data/baseline.csv

  # Jam CH5 while recording hops
  python3 spectrum_scanner.py --duration 120 --jam-channel 5 --output hop_data/jammed.csv

  # High-resolution scanning
  python3 spectrum_scanner.py --fft-size 8192 --averages 16
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
                        help='Channel number to jam during scanning (for training data)')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress live display (only log to file)')

    args = parser.parse_args()
    run_scanner(args)


if __name__ == '__main__':
    main()
