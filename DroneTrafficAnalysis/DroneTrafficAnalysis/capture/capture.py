#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple RF Capture - Fixed state for entire duration
Used by simple_recorder.py
"""

import numpy as np
import time
import csv
import argparse
from datetime import datetime

try:
    from gnuradio import gr, uhd
    HAS_GNURADIO = True
except ImportError:
    HAS_GNURADIO = False
    print("Warning: GNU Radio not available. Running in simulation mode.")


class SimpleRFCapture:
    """Simplified RF capture with fixed state label"""

    def __init__(self, center_freq, sample_rate, gain, state, output_file):
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.gain = gain
        self.state = state
        self.output_file = output_file
        self.running = False

        # Open CSV file
        self.csv_file = open(output_file, 'w', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            'timestamp', 'center_freq_mhz', 'state',
            'mean_power_db', 'std_power_db', 'max_power_db', 'min_power_db',
            'peak_freq_offset_hz', 'bandwidth_3db_hz',
            'spectral_entropy', 'spectral_flatness',
            'power_variance_rate', 'signal_presence'
        ])

        # Initialize USRP if available
        if HAS_GNURADIO:
            self.usrp = uhd.usrp_source(
                ",".join(("", "")),
                uhd.stream_args(cpu_format="fc32", args='', channels=[0]),
            )
            self.usrp.set_samp_rate(sample_rate)
            self.usrp.set_center_freq(center_freq, 0)
            self.usrp.set_gain(gain, 0)
            self.usrp.set_antenna("RX2", 0)
        else:
            self.usrp = None

    def compute_features(self, samples):
        """Extract RF features from samples"""
        fft_size = 1024
        spectrum = np.fft.fftshift(np.fft.fft(samples[:fft_size], fft_size))
        power_spectrum = 20 * np.log10(np.abs(spectrum) + 1e-10)

        # Power statistics
        mean_power = np.mean(power_spectrum)
        std_power = np.std(power_spectrum)
        max_power = np.max(power_spectrum)
        min_power = np.min(power_spectrum)

        # Peak frequency
        peak_idx = np.argmax(power_spectrum)
        freq_resolution = self.sample_rate / fft_size
        peak_freq_offset = (peak_idx - fft_size // 2) * freq_resolution

        # 3dB bandwidth
        threshold = max_power - 3
        above_threshold = power_spectrum > threshold
        bandwidth_3db = np.sum(above_threshold) * freq_resolution

        # Spectral entropy
        power_linear = np.abs(spectrum) ** 2
        power_normalized = power_linear / (np.sum(power_linear) + 1e-10)
        spectral_entropy = -np.sum(power_normalized * np.log2(power_normalized + 1e-10))

        # Spectral flatness
        geometric_mean = np.exp(np.mean(np.log(power_linear + 1e-10)))
        arithmetic_mean = np.mean(power_linear)
        spectral_flatness = geometric_mean / (arithmetic_mean + 1e-10)

        # Power variance rate
        power_variance_rate = std_power / (np.abs(mean_power) + 1e-10)

        # Signal presence
        noise_floor = np.percentile(power_spectrum, 10)
        signal_presence = 1 if (max_power - noise_floor) > 10 else 0

        return {
            'mean_power_db': mean_power,
            'std_power_db': std_power,
            'max_power_db': max_power,
            'min_power_db': min_power,
            'peak_freq_offset_hz': peak_freq_offset,
            'bandwidth_3db_hz': bandwidth_3db,
            'spectral_entropy': spectral_entropy,
            'spectral_flatness': spectral_flatness,
            'power_variance_rate': power_variance_rate,
            'signal_presence': signal_presence,
        }

    def capture_sample(self):
        """Capture one sample and write to CSV"""
        if self.usrp:
            # Real capture from USRP
            num_samples = int(self.sample_rate * 0.1)  # 100ms
            samples = self.usrp.finite_acquisition(num_samples)
            samples = np.array(samples)
        else:
            # Simulation mode
            num_samples = int(self.sample_rate * 0.1)
            # Generate fake signal based on state
            if self.state in ['hover', 'idle']:
                noise_level = 0.1
            elif self.state in ['fly_slow', 'fly_forward', 'fly_backward']:
                noise_level = 0.3
            elif self.state in ['fly_fast']:
                noise_level = 0.5
            elif self.state == 'under_jamming':
                noise_level = 2.0
            else:
                noise_level = 0.2

            samples = noise_level * (np.random.randn(num_samples) + 1j * np.random.randn(num_samples))

        # Compute features
        features = self.compute_features(samples)

        # Write to CSV
        timestamp = datetime.now().isoformat()
        self.csv_writer.writerow([
            timestamp, self.center_freq / 1e6, self.state,
            f"{features['mean_power_db']:.2f}",
            f"{features['std_power_db']:.2f}",
            f"{features['max_power_db']:.2f}",
            f"{features['min_power_db']:.2f}",
            f"{features['peak_freq_offset_hz']:.0f}",
            f"{features['bandwidth_3db_hz']:.0f}",
            f"{features['spectral_entropy']:.4f}",
            f"{features['spectral_flatness']:.6f}",
            f"{features['power_variance_rate']:.4f}",
            features['signal_presence']
        ])
        self.csv_file.flush()

        return features

    def run(self, duration):
        """Run capture for specified duration"""
        self.running = True
        start_time = time.time()
        sample_count = 0

        print(f"Capturing '{self.state}' at {self.center_freq/1e9:.1f} GHz...")

        try:
            while self.running and (time.time() - start_time) < duration:
                features = self.capture_sample()
                sample_count += 1

                # Print status every 10 samples
                if sample_count % 10 == 0:
                    elapsed = time.time() - start_time
                    print(f"\r  Samples: {sample_count} | "
                          f"Time: {elapsed:.1f}s | "
                          f"Power: {features['mean_power_db']:.1f} dB",
                          end='', flush=True)

                time.sleep(0.1)  # 100ms between samples

        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.csv_file.close()

        print(f"\n  Done! Captured {sample_count} samples.")
        return sample_count

    def stop(self):
        self.running = False


def main():
    parser = argparse.ArgumentParser(description='Simple RF Capture')
    parser.add_argument('--freq', type=float, default=5.8e9)
    parser.add_argument('--sample-rate', type=float, default=10e6)
    parser.add_argument('--gain', type=float, default=40)
    parser.add_argument('--state', type=str, required=True)
    parser.add_argument('--duration', type=int, default=180)
    parser.add_argument('--output', type=str, required=True)
    args = parser.parse_args()

    capture = SimpleRFCapture(
        center_freq=args.freq,
        sample_rate=args.sample_rate,
        gain=args.gain,
        state=args.state,
        output_file=args.output
    )

    capture.run(args.duration)


if __name__ == '__main__':
    main()
