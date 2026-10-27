#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple Session Recorder - One State Per Session
Much easier: just specify the drone state when you start, then fly!

Usage:
    python simple_recorder.py --state hover --duration 300
    python simple_recorder.py --state fly_fast --duration 180
    python simple_recorder.py --state under_jamming --duration 120
"""

import os
import sys
import json
import time
import signal
import argparse
import subprocess
from datetime import datetime
from pathlib import Path


# Available states for your thesis
STATES = {
    'idle': 'Drone on, motors off',
    'hover': 'Hovering in place',
    'fly_slow': 'Flying slowly (<5 m/s)',
    'fly_fast': 'Flying fast (>5 m/s)',
    'fly_forward': 'Moving forward',
    'fly_backward': 'Moving backward',
    'ascend': 'Going up',
    'descend': 'Going down',
    'rotate': 'Rotating/spinning',
    'under_jamming': 'During RF jamming attack',
    'under_gps_spoof': 'During GPS spoofing',
    'signal_lost': 'Connection lost',
}


def print_header(state, duration, freq):
    print("\n" + "="*60)
    print("   DJI MINI - SIMPLE RF CAPTURE")
    print("="*60)
    print(f"   State:     {state}")
    print(f"   Duration:  {duration} seconds")
    print(f"   Frequency: {freq} GHz")
    print("="*60)
    print("\n   Just fly the drone in this state - labeling is automatic!")
    print("   Press Ctrl+C to stop early.\n")


def run_capture(state, duration, freq, output_dir):
    """Run RF capture for specified duration with fixed state"""
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_path / f"{state}_{freq}ghz_{timestamp}.csv"
    
    freq_hz = 5.8e9 if freq == "5.8" else 2.437e9
    
    # Build capture command
    capture_script = Path(__file__).parent / "rf_capture_simple.py"
    cmd = [
        sys.executable, str(capture_script),
        '--freq', str(freq_hz),
        '--state', state,
        '--duration', str(duration),
        '--output', str(output_file),
    ]
    
    print(f"Starting capture: {output_file.name}")
    print(f"Recording for {duration} seconds...\n")
    
    start_time = time.time()
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        
        while process.poll() is None:
            elapsed = time.time() - start_time
            remaining = max(0, duration - elapsed)
            
            # Progress bar
            progress = min(1.0, elapsed / duration)
            bar_len = 40
            filled = int(bar_len * progress)
            bar = '█' * filled + '░' * (bar_len - filled)
            
            print(f"\r   [{bar}] {elapsed:.0f}s / {duration}s ", end='', flush=True)
            
            if elapsed >= duration:
                process.terminate()
                break
            
            time.sleep(0.5)
        
        process.wait()
        
    except KeyboardInterrupt:
        print("\n\n   Stopped by user!")
        process.terminate()
    
    elapsed = time.time() - start_time
    print(f"\n\n   ✓ Captured {elapsed:.1f} seconds of '{state}' data")
    print(f"   ✓ Saved to: {output_file}")
    
    # Save session metadata
    metadata = {
        'state': state,
        'frequency_ghz': freq,
        'duration_seconds': elapsed,
        'timestamp': timestamp,
        'output_file': str(output_file),
    }
    
    meta_file = output_path / f"{state}_{freq}ghz_{timestamp}_meta.json"
    with open(meta_file, 'w') as f:
        json.dump(metadata, f, indent=2)
    
    return output_file


def list_states():
    print("\nAvailable states:")
    print("-" * 50)
    for state, desc in STATES.items():
        print(f"  {state:20s} - {desc}")
    print("-" * 50)


def main():
    parser = argparse.ArgumentParser(
        description='Simple RF Capture - One State Per Session',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python simple_recorder.py --state hover --duration 300
  python simple_recorder.py --state fly_fast --duration 180 --freq 2.4
  python simple_recorder.py --state under_jamming --duration 120
  python simple_recorder.py --list-states
        """
    )
    parser.add_argument('--state', '-s', type=str, 
                        help='Drone state for this recording session')
    parser.add_argument('--duration', '-d', type=int, default=180,
                        help='Recording duration in seconds (default: 180)')
    parser.add_argument('--freq', '-f', type=str, default='5.8', choices=['2.4', '5.8'],
                        help='Frequency band to capture (default: 5.8)')
    parser.add_argument('--output-dir', '-o', type=str, default='../dataset/data/captures',
                        help='Output directory for captures')
    parser.add_argument('--list-states', '-l', action='store_true',
                        help='List available states')
    
    args = parser.parse_args()
    
    if args.list_states:
        list_states()
        return
    
    if not args.state:
        print("Error: Please specify a state with --state")
        list_states()
        return
    
    if args.state not in STATES:
        print(f"Error: Unknown state '{args.state}'")
        list_states()
        return
    
    print_header(args.state, args.duration, args.freq)
    
    input("Press ENTER when ready to start recording...")
    
    run_capture(args.state, args.duration, args.freq, args.output_dir)
    
    print("\n" + "="*60)
    print("   Recording complete!")
    print("   Run more sessions with different states to build your dataset.")
    print("="*60 + "\n")


if __name__ == '__main__':
    main()
