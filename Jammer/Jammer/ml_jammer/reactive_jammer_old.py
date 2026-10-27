#!/usr/bin/env python3
"""
Reactive ML-Powered Jammer
==============================
The full pipeline: Scan → Track → Predict → Jam

This is the main executable that ties everything together:
1. Spectrum Scanner monitors all OcuSync channels via USRP RX
2. Channel Tracker detects when the drone hops
3. ML Predictor forecasts the next channel
4. USRP TX preemptively jams the predicted channel

The key insight: Since OcuSync uses Adaptive Frequency Hopping (AFH) that
always escapes to the "cleanest" channel, the ML model learns the drone's
channel selection logic and jams the predicted escape route BEFORE the drone
arrives — effectively cornering it.

Usage:
    # Run with trained ML model
    python3 reactive_jammer.py --model models/hop_predictor.pkl --gain 50

    # Run with higher TX power and custom RX gain
    python3 reactive_jammer.py --model models/hop_predictor.pkl --tx-gain 70 --rx-gain 50

    # Fallback: jam without ML (just chase the active channel)
    python3 reactive_jammer.py --mode chase --gain 50

    # Log everything for analysis
    python3 reactive_jammer.py --model models/hop_predictor.pkl --log jammer_log.csv
"""

import argparse
import csv
import numpy as np
import os
import signal
import sys
import time
from datetime import datetime

try:
    import uhd
except ImportError:
    print("ERROR: UHD Python API not found. Install with: sudo apt install python3-uhd")
    sys.exit(1)

from channel_config import (
    OCUSYNC_5GHZ_CHANNELS,
    SINGLE_CENTER_FREQ, SINGLE_SAMP_RATE,
    CHANNEL_BANDWIDTH,
    USRP_SERIAL, RX_ANTENNA, TX_ANTENNA,
    DEFAULT_RX_GAIN, DEFAULT_TX_GAIN,
    PREDICTION_WINDOW_MS,
    get_freq_from_channel,
)
from spectrum_scanner import SpectrumScanner
from channel_tracker import HopTracker

# ─────────────────────────────────────────────────────────────
# Globals
# ─────────────────────────────────────────────────────────────
running = True


def signal_handler(sig, frame):
    global running
    print("\n[!] Shutting down jammer...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ─────────────────────────────────────────────────────────────
# Jammer Engine
# ─────────────────────────────────────────────────────────────

class ReactiveJammer:
    """
    Full-duplex reactive jammer using USRP B210.
    
    RX chain: Wideband spectrum scanner monitoring all channels
    TX chain: Narrowband noise jammer aimed at predicted channel
    
    Operating modes:
        'ml'    — Use ML model to predict and preemptively jam
        'chase' — Simple reactive: jam whatever channel the drone is currently on
        'sweep' — Brute force: sweep noise across all channels rapidly
    """

    def __init__(self, mode='ml', model_path=None, tx_gain=DEFAULT_TX_GAIN,
                 rx_gain=DEFAULT_RX_GAIN):
        self.mode = mode
        self.tx_gain = tx_gain
        self.rx_gain = rx_gain

        # Components
        self.scanner = SpectrumScanner(rx_gain=rx_gain)
        self.tracker = HopTracker()
        self.predictor = None

        # Load ML model if in ML mode
        if mode == 'ml' and model_path:
            from ml_predictor import HopPredictor
            self.predictor = HopPredictor(model_path)

        # USRP state
        self.usrp = None
        self.tx_streamer = None
        self.current_jam_channel = None
        self.current_jam_freq = None

        # Statistics
        self.stats = {
            'total_scans': 0,
            'hops_detected': 0,
            'predictions_made': 0,
            'predictions_correct': 0,
            'jam_switches': 0,
            'time_on_target': 0,
            'start_time': None,
        }

        # Noise buffer for TX (pre-generated Gaussian noise, near full-scale)
        self.noise_buffer = (np.random.normal(0, 0.9, 16384) +
                             1j * np.random.normal(0, 0.9, 16384)).astype(np.complex64)
        # Clip to prevent DAC overflow
        self.noise_buffer = np.clip(self.noise_buffer.real, -1.0, 1.0) + \
                            1j * np.clip(self.noise_buffer.imag, -1.0, 1.0)
        self.noise_buffer = self.noise_buffer.astype(np.complex64)

    def init_hardware(self):
        """Initialize USRP for simultaneous RX and TX."""
        print("[*] Initializing USRP B210 (full-duplex)...")

        # Initialize scanner (RX chain)
        self.scanner.init_usrp()
        self.usrp = self.scanner.usrp

        # Initialize TX chain — 20 MHz to cover full OcuSync channel + guard carriers
        self.usrp.set_tx_rate(20e6)
        self.usrp.set_tx_antenna(TX_ANTENNA)
        self.usrp.set_tx_gain(self.tx_gain)
        self.usrp.set_tx_bandwidth(20e6)

        # Create TX streamer
        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        self.tx_streamer = self.usrp.get_tx_stream(st_args)

        self.stats['start_time'] = time.time()
        print("[+] Hardware ready (RX + TX)")

    def set_jam_frequency(self, channel_num):
        """Switch TX to jam a specific channel."""
        if channel_num == self.current_jam_channel:
            return  # Already jamming this channel

        freq = get_freq_from_channel(channel_num)
        if freq is None:
            return

        self.usrp.set_tx_freq(uhd.libpyuhd.types.tune_request(freq))
        self.current_jam_channel = channel_num
        self.current_jam_freq = freq
        self.stats['jam_switches'] += 1

    def transmit_noise(self):
        """Send one buffer of noise on the TX chain."""
        if self.tx_streamer is None:
            return

        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        self.tx_streamer.send(self.noise_buffer, metadata)

    def run_ml_mode(self, log_path=None):
        """
        ML-powered reactive jamming loop.
        
        1. Scan all channels
        2. Detect active channel
        3. If hop detected → ML predicts next channel
        4. Preemptively switch TX to predicted channel
        5. Continuously transmit noise on jammed channel
        """
        global running

        print(f"\n{'=' * 60}")
        print("  ML REACTIVE JAMMER — ACTIVE")
        print(f"  Mode: {self.mode.upper()}")
        print(f"  TX Gain: {self.tx_gain} dB")
        if self.predictor:
            print(f"  Model accuracy: {self.predictor.accuracy:.1%}")
        print(f"{'=' * 60}\n")

        # CSV log
        csv_file = None
        csv_writer = None
        if log_path:
            os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else '.', exist_ok=True)
            csv_file = open(log_path, 'w', newline='')
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow([
                'timestamp', 'scan_num', 'active_channel', 'jam_channel',
                'predicted_channel', 'prediction_confidence',
                'hop_detected', 'prediction_correct', 'mode'
            ])

        last_active = None
        predicted_next = None
        prediction_confidence = 0
        dwell_start = time.time()

        try:
            while running:
                # 1. SCAN — get channel power levels
                channel_power = self.scanner.compute_averaged_power(num_averages=4)
                active = self.scanner.detect_active_channel(channel_power)
                self.stats['total_scans'] += 1

                active_ch = active[0][0] if active else 0
                hop_detected = False
                prediction_correct = False

                # 2. TRACK — detect hops
                event = self.tracker.process_scan(
                    datetime.now().isoformat(),
                    active_ch,
                    channel_power,
                    self.current_jam_channel or 0,
                )

                if event:
                    # === HOP DETECTED ===
                    hop_detected = True
                    self.stats['hops_detected'] += 1
                    actual_to = event['to_channel']

                    # Check if our prediction was correct
                    if predicted_next is not None:
                        if predicted_next == actual_to:
                            self.stats['predictions_correct'] += 1
                            prediction_correct = True
                            marker = "✅"
                        else:
                            marker = "❌"
                        print(f"  HOP #{event['hop_number']}: CH{event['from_channel']} → CH{actual_to} "
                              f"| Predicted: CH{predicted_next} {marker} "
                              f"(conf: {prediction_confidence:.0%})")

                    # Update predictor with actual result
                    if self.predictor:
                        self.predictor.update_actual(actual_to)

                # 3. PREDICT — where will it go next?
                if active_ch and self.predictor:
                    dwell_ms = (time.time() - dwell_start) * 1000

                    predicted_next, prediction_confidence, probs = self.predictor.predict_next_channel(
                        current_channel=active_ch,
                        channel_powers=channel_power,
                        jam_channel=self.current_jam_channel or 0,
                        dwell_ms=dwell_ms,
                        hops_last_10s=self.tracker._count_recent_hops(datetime.now(), 10),
                        hops_last_30s=self.tracker._count_recent_hops(datetime.now(), 30),
                    )
                    self.stats['predictions_made'] += 1

                    if hop_detected:
                        dwell_start = time.time()

                # 4. JAM — based on mode
                if self.mode == 'ml' and predicted_next:
                    # Preemptive: jam where we think the drone will go
                    self.set_jam_frequency(predicted_next)
                elif self.mode == 'chase' and active_ch:
                    # Reactive: jam current channel
                    self.set_jam_frequency(active_ch)

                # 5. TRANSMIT noise on jammed channel
                if self.current_jam_channel:
                    self.transmit_noise()

                # Display status
                if self.stats['total_scans'] % 20 == 0:
                    self._print_status(active_ch, predicted_next, prediction_confidence)

                # Log
                if csv_writer:
                    csv_writer.writerow([
                        datetime.now().isoformat(),
                        self.stats['total_scans'],
                        active_ch,
                        self.current_jam_channel or 0,
                        predicted_next or 0,
                        round(prediction_confidence, 3),
                        int(hop_detected),
                        int(prediction_correct),
                        self.mode,
                    ])

                # Small sleep
                time.sleep(0.02)

        finally:
            # Stop TX
            if self.tx_streamer:
                md = uhd.types.TXMetadata()
                md.end_of_burst = True
                self.tx_streamer.send(np.zeros(0, dtype=np.complex64), md)

            self.scanner.stop()
            if csv_file:
                csv_file.close()

            self._print_final_stats()

    def run_sweep_mode(self):
        """Brute force sweep across all channels."""
        global running
        print("[*] SWEEP MODE — Jamming all channels sequentially")

        channels = sorted(OCUSYNC_5GHZ_CHANNELS.keys())
        idx = 0

        try:
            while running:
                ch = channels[idx % len(channels)]
                self.set_jam_frequency(ch)

                # Dwell on each channel briefly
                for _ in range(10):
                    self.transmit_noise()
                    time.sleep(0.001)

                idx += 1
        finally:
            self.scanner.stop()
            self._print_final_stats()

    def _print_status(self, active_ch, predicted, confidence):
        """Print live status line."""
        elapsed = time.time() - self.stats['start_time']
        correct = self.stats['predictions_correct']
        total = max(1, self.stats['hops_detected'])
        hit_rate = correct / total

        print(f"\r  [{elapsed:.0f}s] Drone: CH{active_ch or '?'} | "
              f"Jam: CH{self.current_jam_channel or '?'} | "
              f"Predict: CH{predicted or '?'} ({confidence:.0%}) | "
              f"Hops: {self.stats['hops_detected']} | "
              f"Hit rate: {hit_rate:.0%} ({correct}/{total})", end='', flush=True)

    def _print_final_stats(self):
        """Print final jamming statistics."""
        elapsed = time.time() - (self.stats['start_time'] or time.time())
        correct = self.stats['predictions_correct']
        total_hops = max(1, self.stats['hops_detected'])

        print(f"\n\n{'=' * 60}")
        print(f"  JAMMING SESSION COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Duration:          {elapsed:.1f} seconds")
        print(f"  Total scans:       {self.stats['total_scans']}")
        print(f"  Hops detected:     {self.stats['hops_detected']}")
        print(f"  Predictions made:  {self.stats['predictions_made']}")
        print(f"  Predictions correct: {correct}/{total_hops} ({correct/total_hops:.1%})")
        print(f"  Jam switches:      {self.stats['jam_switches']}")
        print(f"  Mode:              {self.mode}")
        print(f"{'=' * 60}")

        # Print tracker summary
        print(self.tracker.get_summary())


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ML-Powered Reactive Jammer for DJI OcuSync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  ml      Use ML model to predict and preemptively jam the escape channel
  chase   Simple reactive: jam whatever channel the drone is currently on
  sweep   Brute force: sweep noise across all channels rapidly

Examples:
  # ML-powered jamming (requires trained model)
  python3 reactive_jammer.py --model models/hop_predictor.pkl --tx-gain 50

  # Simple chase mode (no ML needed)
  python3 reactive_jammer.py --mode chase --tx-gain 50

  # Log session for analysis
  python3 reactive_jammer.py --model models/hop_predictor.pkl --log session1.csv
        """
    )

    parser.add_argument('--mode', choices=['ml', 'chase', 'sweep'], default='ml',
                        help='Jamming mode (default: ml)')
    parser.add_argument('--model', type=str, default=None,
                        help='Path to trained ML model (.pkl)')
    parser.add_argument('--tx-gain', type=float, default=DEFAULT_TX_GAIN,
                        help=f'TX gain in dB (default: {DEFAULT_TX_GAIN})')
    parser.add_argument('--rx-gain', type=float, default=DEFAULT_RX_GAIN,
                        help=f'RX gain in dB (default: {DEFAULT_RX_GAIN})')
    parser.add_argument('--log', type=str, default=None,
                        help='Path for session log CSV')

    args = parser.parse_args()

    # Validate
    if args.mode == 'ml' and not args.model:
        print("[!] ML mode requires --model. Use --mode chase for no-model jamming.")
        sys.exit(1)

    if args.model and not os.path.exists(args.model):
        print(f"[!] Model not found: {args.model}")
        sys.exit(1)

    # Create jammer
    jammer = ReactiveJammer(
        mode=args.mode,
        model_path=args.model,
        tx_gain=args.tx_gain,
        rx_gain=args.rx_gain,
    )

    # Initialize hardware
    jammer.init_hardware()

    # Run
    if args.mode in ('ml', 'chase'):
        jammer.run_ml_mode(log_path=args.log)
    elif args.mode == 'sweep':
        jammer.run_sweep_mode()


if __name__ == '__main__':
    main()
