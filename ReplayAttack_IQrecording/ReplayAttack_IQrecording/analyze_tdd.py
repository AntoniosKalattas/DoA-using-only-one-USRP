#!/usr/bin/env python3
"""
TDD Pattern Analysis - Visualize Power vs Time
to try to separate Uplink (Controller) from Downlink (Drone)
"""

import numpy as np
import matplotlib.pyplot as plt
import sys
from pathlib import Path

# Settings
SAMPLE_RATE = 10e6  # 10 MHz
CHUNK_MS = 0.5      # Analyze in 0.5ms chunks (TDD slots are usually 1-10ms)

def analyze_tdd(filename, duration_ms=100):
    """Analyze TDD pattern in IQ file"""
    print("=" * 60)
    print("TDD PATTERN ANALYSIS")
    print("=" * 60)
    
    # Load only a portion (first N ms) to keep memory manageable
    samples_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    total_samples_needed = int(SAMPLE_RATE * duration_ms / 1000)
    
    print(f"Loading first {duration_ms}ms of data...")
    samples = np.fromfile(filename, dtype=np.complex64, count=total_samples_needed)
    print(f"Loaded {len(samples):,} samples")
    
    # Compute power in small time chunks
    num_chunks = len(samples) // samples_per_chunk
    times_ms = np.arange(num_chunks) * CHUNK_MS
    powers_db = np.zeros(num_chunks)
    bandwidths = np.zeros(num_chunks)
    
    for i in range(num_chunks):
        chunk = samples[i * samples_per_chunk : (i + 1) * samples_per_chunk]
        
        # Power
        power = np.mean(np.abs(chunk)**2)
        powers_db[i] = 10 * np.log10(power + 1e-20)
        
        # Bandwidth (3dB)
        spectrum = np.abs(np.fft.fft(chunk))**2
        max_pwr = np.max(spectrum)
        if max_pwr > 0:
            bw_bins = np.sum(spectrum > max_pwr / 2)
            bandwidths[i] = bw_bins * (SAMPLE_RATE / len(chunk))
        
    # Print stats
    print(f"\nPower Statistics (per {CHUNK_MS}ms chunk):")
    print(f"  Mean:  {np.mean(powers_db):.1f} dB")
    print(f"  Max:   {np.max(powers_db):.1f} dB")
    print(f"  Min:   {np.min(powers_db):.1f} dB")
    print(f"  Std:   {np.std(powers_db):.1f} dB")
    
    # Try to detect TDD pattern using power threshold
    threshold = np.mean(powers_db) + 3  # 3dB above mean
    high_power = powers_db > threshold
    
    # Find transitions
    transitions = np.diff(high_power.astype(int))
    rising = np.where(transitions == 1)[0]
    falling = np.where(transitions == -1)[0]
    
    print(f"\n  Signal threshold: {threshold:.1f} dB")
    print(f"  High-power chunks: {np.sum(high_power)} / {num_chunks}")
    print(f"  Transitions detected: {len(rising)} rising, {len(falling)} falling")
    
    if len(rising) > 2:
        intervals = np.diff(rising) * CHUNK_MS
        print(f"  Avg burst interval: {np.mean(intervals):.2f} ms")
        print(f"  This could indicate TDD slot timing!")
    
    # =====================
    # GENERATE PLOTS
    # =====================
    fig_dir = Path(__file__).parent
    
    # PLOT 1: Power vs Time (full view)
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    
    ax1 = axes[0]
    ax1.plot(times_ms, powers_db, linewidth=0.5, color='#2196F3')
    ax1.axhline(y=threshold, color='red', linestyle='--', alpha=0.7, label=f'Threshold: {threshold:.1f} dB')
    ax1.fill_between(times_ms, powers_db, threshold, 
                     where=(powers_db > threshold), alpha=0.3, color='red', label='Above threshold (strong TX)')
    ax1.fill_between(times_ms, powers_db, threshold,
                     where=(powers_db <= threshold), alpha=0.3, color='blue', label='Below threshold (weak/noise)')
    ax1.set_ylabel('Power (dB)', fontsize=11)
    ax1.set_title(f'Power vs Time - Looking for TDD Pattern\n({CHUNK_MS}ms resolution, first {duration_ms}ms)', fontsize=13)
    ax1.legend(loc='upper right', fontsize=9)
    ax1.grid(True, alpha=0.3)
    
    # PLOT 2: Bandwidth vs Time
    ax2 = axes[1]
    ax2.plot(times_ms, bandwidths / 1000, linewidth=0.5, color='#4CAF50')
    ax2.set_ylabel('Bandwidth (kHz)', fontsize=11)
    ax2.set_title('Bandwidth vs Time - Wide=Video(Downlink), Narrow=Commands(Uplink)', fontsize=13)
    ax2.grid(True, alpha=0.3)
    
    # PLOT 3: Zoomed in view (first 20ms to see TDD slots)
    zoom_ms = min(20, duration_ms)
    zoom_mask = times_ms <= zoom_ms
    ax3 = axes[2]
    ax3.plot(times_ms[zoom_mask], powers_db[zoom_mask], linewidth=1.0, color='#2196F3', marker='o', markersize=2)
    ax3.axhline(y=threshold, color='red', linestyle='--', alpha=0.7)
    ax3.fill_between(times_ms[zoom_mask], powers_db[zoom_mask], threshold,
                     where=(powers_db[zoom_mask] > threshold), alpha=0.3, color='red')
    ax3.set_ylabel('Power (dB)', fontsize=11)
    ax3.set_xlabel('Time (ms)', fontsize=11)
    ax3.set_title(f'Zoomed View (first {zoom_ms}ms) - Individual TDD Slots', fontsize=13)
    ax3.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(fig_dir / 'tdd_power_analysis.png', dpi=150, bbox_inches='tight')
    print(f"\nSaved: {fig_dir / 'tdd_power_analysis.png'}")
    plt.close()
    
    # PLOT 4: Power histogram - bimodal = TDD detected
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    
    ax1 = axes[0]
    ax1.hist(powers_db, bins=80, color='#2196F3', alpha=0.7, edgecolor='black', linewidth=0.5)
    ax1.axvline(x=threshold, color='red', linestyle='--', linewidth=2, label=f'Threshold: {threshold:.1f} dB')
    ax1.set_xlabel('Power (dB)', fontsize=11)
    ax1.set_ylabel('Count', fontsize=11)
    ax1.set_title('Power Distribution\n(Two peaks = TDD pattern detected!)', fontsize=13)
    ax1.legend()
    
    ax2 = axes[1]
    ax2.hist(bandwidths / 1000, bins=80, color='#4CAF50', alpha=0.7, edgecolor='black', linewidth=0.5)
    ax2.set_xlabel('Bandwidth (kHz)', fontsize=11)
    ax2.set_ylabel('Count', fontsize=11)
    ax2.set_title('Bandwidth Distribution\n(Two clusters = Uplink vs Downlink)', fontsize=13)
    
    plt.tight_layout()
    plt.savefig(fig_dir / 'tdd_histograms.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {fig_dir / 'tdd_histograms.png'}")
    plt.close()
    
    print("\n" + "=" * 60)
    print("INTERPRETATION GUIDE")
    print("=" * 60)
    print("Power vs Time plot:")
    print("  - RED regions = Strong signal (likely nearby Controller)")
    print("  - BLUE regions = Weak signal (likely distant Drone or silence)")
    print("  - Regular pattern = TDD slots detected!")
    print("")
    print("Histograms:")
    print("  - Two distinct peaks = Clear separation possible")
    print("  - One blob = Hard to separate uplink/downlink")
    print("=" * 60)


if __name__ == "__main__":
    iq_file = sys.argv[1] if len(sys.argv) > 1 else "mavic2_simulation.iq"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 100  # First 100ms
    analyze_tdd(iq_file, duration)
