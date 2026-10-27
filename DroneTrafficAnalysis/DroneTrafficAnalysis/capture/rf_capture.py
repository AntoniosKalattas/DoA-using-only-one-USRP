#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DJI Mini RF Signal Capture for Traffic Analysis
Uses USRP B210 to capture RF signal characteristics from OcuSync 2.0

Target Frequencies:
- 2.4 GHz band: 2400-2483 MHz (video downlink + control)
- 5.8 GHz band: 5725-5850 MHz (video downlink)

Output: CSV file with timestamped RF features for ML training
"""

import numpy as np
import time
import csv
import argparse
from datetime import datetime
from gnuradio import gr, uhd, blocks, fft
from gnuradio.fft import window
import threading
import sys


class RFFeatureExtractor(gr.sync_block):
    """Extract RF features from complex samples for ML"""
    
    def __init__(self, sample_rate, fft_size=1024, output_file=None, center_freq=5.8e9):
        gr.sync_block.__init__(
            self,
            name="RF Feature Extractor",
            in_sig=[np.complex64],
            out_sig=None
        )
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.center_freq = center_freq
        self.output_file = output_file
        
        # Feature buffers
        self.sample_buffer = []
        self.buffer_size = int(sample_rate * 0.1)  # 100ms windows
        
        # State label (set externally)
        self.current_state = "idle"
        self.state_lock = threading.Lock()
        
        # CSV writer
        if output_file:
            self.csv_file = open(output_file, 'w', newline='')
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow([
                'timestamp', 'center_freq_mhz', 'state',
                'mean_power_db', 'std_power_db', 'max_power_db', 'min_power_db',
                'peak_freq_offset_hz', 'bandwidth_3db_hz',
                'spectral_entropy', 'spectral_flatness',
                'power_variance_rate', 'signal_presence'
            ])
        else:
            self.csv_file = None
            self.csv_writer = None
    
    def set_state(self, state):
        with self.state_lock:
            self.current_state = state
            print(f"\n>>> State changed to: {state}")
    
    def get_state(self):
        with self.state_lock:
            return self.current_state
    
    def work(self, input_items, output_items):
        samples = input_items[0]
        self.sample_buffer.extend(samples)
        
        # Process when we have enough samples
        while len(self.sample_buffer) >= self.buffer_size:
            chunk = np.array(self.sample_buffer[:self.buffer_size])
            self.sample_buffer = self.sample_buffer[self.buffer_size:]
            self.process_chunk(chunk)
        
        return len(samples)
    
    def process_chunk(self, samples):
        """Extract RF features from sample chunk"""
        timestamp = datetime.now().isoformat()
        
        # Compute power spectrum
        spectrum = np.fft.fftshift(np.fft.fft(samples, self.fft_size))
        power_spectrum = 20 * np.log10(np.abs(spectrum) + 1e-10)
        
        # Power statistics
        mean_power = np.mean(power_spectrum)
        std_power = np.std(power_spectrum)
        max_power = np.max(power_spectrum)
        min_power = np.min(power_spectrum)
        
        # Peak frequency offset
        peak_idx = np.argmax(power_spectrum)
        freq_resolution = self.sample_rate / self.fft_size
        peak_freq_offset = (peak_idx - self.fft_size // 2) * freq_resolution
        
        # 3dB bandwidth estimation
        threshold = max_power - 3
        above_threshold = power_spectrum > threshold
        bandwidth_3db = np.sum(above_threshold) * freq_resolution
        
        # Spectral entropy (signal complexity)
        power_linear = np.abs(spectrum) ** 2
        power_normalized = power_linear / (np.sum(power_linear) + 1e-10)
        spectral_entropy = -np.sum(power_normalized * np.log2(power_normalized + 1e-10))
        
        # Spectral flatness (noise-like vs tonal)
        geometric_mean = np.exp(np.mean(np.log(power_linear + 1e-10)))
        arithmetic_mean = np.mean(power_linear)
        spectral_flatness = geometric_mean / (arithmetic_mean + 1e-10)
        
        # Power variance rate (stability indicator)
        power_variance_rate = std_power / (np.abs(mean_power) + 1e-10)
        
        # Signal presence detection (above noise floor)
        noise_floor_estimate = np.percentile(power_spectrum, 10)
        signal_presence = 1 if (max_power - noise_floor_estimate) > 10 else 0
        
        # Get current state
        state = self.get_state()
        
        # Write to CSV
        if self.csv_writer:
            self.csv_writer.writerow([
                timestamp, self.center_freq / 1e6, state,
                f"{mean_power:.2f}", f"{std_power:.2f}", 
                f"{max_power:.2f}", f"{min_power:.2f}",
                f"{peak_freq_offset:.0f}", f"{bandwidth_3db:.0f}",
                f"{spectral_entropy:.4f}", f"{spectral_flatness:.6f}",
                f"{power_variance_rate:.4f}", signal_presence
            ])
            self.csv_file.flush()
        
        # Print status periodically
        print(f"\r[{timestamp}] State: {state:12s} | Power: {mean_power:6.1f} dB | "
              f"Peak: {max_power:6.1f} dB | Signal: {'YES' if signal_presence else 'NO '}", 
              end='', flush=True)
    
    def close(self):
        if self.csv_file:
            self.csv_file.close()


class DJIMiniRFCapture(gr.top_block):
    """GNU Radio flowgraph for DJI Mini RF capture"""
    
    def __init__(self, center_freq=5.8e9, sample_rate=10e6, gain=40, 
                 output_file="rf_capture.csv"):
        gr.top_block.__init__(self, "DJI Mini RF Capture")
        
        self.sample_rate = sample_rate
        self.center_freq = center_freq
        
        # USRP Source
        self.usrp_source = uhd.usrp_source(
            ",".join(("", "")),
            uhd.stream_args(
                cpu_format="fc32",
                args='',
                channels=[0],
            ),
        )
        self.usrp_source.set_samp_rate(sample_rate)
        self.usrp_source.set_center_freq(center_freq, 0)
        self.usrp_source.set_gain(gain, 0)
        self.usrp_source.set_antenna("RX2", 0)
        
        # Feature extractor
        self.feature_extractor = RFFeatureExtractor(
            sample_rate=sample_rate,
            output_file=output_file,
            center_freq=center_freq
        )
        
        # Connect blocks
        self.connect(self.usrp_source, self.feature_extractor)
        
        print(f"RF Capture Initialized:")
        print(f"  Center Frequency: {center_freq/1e9:.3f} GHz")
        print(f"  Sample Rate: {sample_rate/1e6:.1f} MHz")
        print(f"  Gain: {gain} dB")
        print(f"  Output File: {output_file}")
    
    def set_state(self, state):
        self.feature_extractor.set_state(state)
    
    def set_center_freq(self, freq):
        self.center_freq = freq
        self.usrp_source.set_center_freq(freq, 0)
        self.feature_extractor.center_freq = freq
        print(f"\n>>> Frequency changed to: {freq/1e9:.3f} GHz")
    
    def cleanup(self):
        self.feature_extractor.close()


def state_control_thread(capture):
    """Thread to handle keyboard input for state labeling"""
    states = ['idle', 'hover', 'fly_slow', 'fly_fast', 'fly_forward', 
              'fly_backward', 'rotate', 'under_jamming', 'under_gps_spoof', 'signal_lost']
    
    print("\n" + "="*60)
    print("STATE CONTROL - Press number to change drone state:")
    for i, state in enumerate(states):
        print(f"  [{i}] {state}")
    print("  [f] Switch frequency (2.4 GHz <-> 5.8 GHz)")
    print("  [q] Quit")
    print("="*60 + "\n")
    
    current_freq = "5.8"
    
    while True:
        try:
            key = input()
            if key == 'q':
                print("\nStopping capture...")
                capture.stop()
                break
            elif key == 'f':
                if current_freq == "5.8":
                    capture.set_center_freq(2.437e9)  # 2.4 GHz center
                    current_freq = "2.4"
                else:
                    capture.set_center_freq(5.8e9)
                    current_freq = "5.8"
            elif key.isdigit() and int(key) < len(states):
                capture.set_state(states[int(key)])
        except EOFError:
            break
        except Exception as e:
            print(f"Input error: {e}")


def main():
    parser = argparse.ArgumentParser(description='DJI Mini RF Signal Capture')
    parser.add_argument('-f', '--freq', type=float, default=5.8e9,
                        help='Center frequency in Hz (default: 5.8 GHz)')
    parser.add_argument('-s', '--sample-rate', type=float, default=10e6,
                        help='Sample rate in Hz (default: 10 MHz)')
    parser.add_argument('-g', '--gain', type=float, default=40,
                        help='RX gain in dB (default: 40)')
    parser.add_argument('-o', '--output', type=str, 
                        default=f"rf_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        help='Output CSV file')
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("DJI MINI RF TRAFFIC CAPTURE FOR ML ANALYSIS")
    print("="*60)
    
    # Create and start flowgraph
    capture = DJIMiniRFCapture(
        center_freq=args.freq,
        sample_rate=args.sample_rate,
        gain=args.gain,
        output_file=args.output
    )
    
    # Start state control in separate thread
    control_thread = threading.Thread(target=state_control_thread, args=(capture,))
    control_thread.daemon = True
    control_thread.start()
    
    try:
        capture.start()
        control_thread.join()
    except KeyboardInterrupt:
        print("\n\nCapture interrupted by user")
    finally:
        capture.stop()
        capture.wait()
        capture.cleanup()
        print(f"\nCapture saved to: {args.output}")


if __name__ == '__main__':
    main()
