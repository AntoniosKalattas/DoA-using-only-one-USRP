#!/usr/bin/env python3
"""
OcuSync Channel Hop Tracker
==============================
Processes spectrum scanner CSV output (or real-time scanner data) to extract
discrete channel hopping events. Produces a clean hop log that serves as
training data for the ML predictor.

The tracker detects:
1. Which channel the drone is currently on
2. When a hop occurs (channel change)
3. Dwell time on each channel before hopping
4. RSSI landscape at the moment of the hop (what the drone "saw")
5. Whether jamming was active on any channel at hop time

Usage:
    # Process an existing scanner CSV into hop events
    python3 channel_tracker.py process --input hop_data/session1.csv --output hop_data/hops_session1.csv

    # Real-time tracking (runs scanner internally)
    python3 channel_tracker.py live --duration 120 --output hop_data/live_hops.csv
"""

import argparse
import csv
import numpy as np
import os
import sys
from datetime import datetime

from channel_config import (
    OCUSYNC_5GHZ_CHANNELS,
    MIN_DWELL_MS,
    DETECTION_THRESHOLD_DB,
)


class HopTracker:
    """
    Tracks channel hopping events from a stream of per-channel power readings.
    
    State machine:
        IDLE → (signal detected) → TRACKING → (channel change) → HOP_DETECTED → TRACKING
                                            → (signal lost) → IDLE
    """

    def __init__(self, min_dwell_ms=MIN_DWELL_MS):
        self.min_dwell_ms = min_dwell_ms

        # Current state
        self.current_channel = None
        self.channel_start_time = None
        self.hop_count = 0

        # History for ML features
        self.hop_history = []          # List of hop event dicts
        self.channel_sequence = []     # Ordered list of (channel, dwell_ms)
        self.power_history = []        # Rolling window of RSSI snapshots

    def process_scan(self, timestamp, active_channel, channel_powers, jam_channel=0):
        """
        Process one spectrum scan result and detect hop events.
        
        Args:
            timestamp: ISO format timestamp string
            active_channel: int, channel number with strongest signal (0 = none)
            channel_powers: dict {ch_num: power_db}
            jam_channel: int, channel being jammed (0 = none)
            
        Returns:
            hop_event dict if a hop was detected, None otherwise
        """
        hop_event = None
        now = datetime.fromisoformat(timestamp)

        # Store power snapshot
        self.power_history.append({
            'timestamp': timestamp,
            'powers': channel_powers.copy(),
            'active': active_channel,
        })
        # Keep last 1000 snapshots
        if len(self.power_history) > 1000:
            self.power_history.pop(0)

        if active_channel is None:
            # No drone detected
            if self.current_channel is not None:
                # Signal lost
                self.current_channel = None
                self.channel_start_time = None
            return None

        if self.current_channel is None:
            # First detection — start tracking
            self.current_channel = active_channel
            self.channel_start_time = now
            return None

        if active_channel != self.current_channel:
            # === HOP DETECTED ===
            dwell_ms = (now - self.channel_start_time).total_seconds() * 1000

            if dwell_ms < self.min_dwell_ms:
                # Too short — likely a glitch, ignore
                return None

            self.hop_count += 1

            # Build the hop event record
            hop_event = {
                'hop_number': self.hop_count,
                'timestamp': timestamp,
                'from_channel': self.current_channel,
                'to_channel': active_channel,
                'dwell_ms': round(dwell_ms, 1),
                'jam_channel': jam_channel,
                # RSSI landscape at hop time (what the drone saw)
                'from_ch_power': round(channel_powers.get(self.current_channel, -100), 2),
                'to_ch_power': round(channel_powers.get(active_channel, -100), 2),
            }

            # Add per-channel power at hop time
            for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                hop_event[f'ch{ch}_power'] = round(channel_powers.get(ch, -100), 2)

            # ML features: recent hop history
            hop_event['hops_last_10s'] = self._count_recent_hops(now, window_seconds=10)
            hop_event['hops_last_30s'] = self._count_recent_hops(now, window_seconds=30)

            # Channel visit frequency in last 30 seconds
            visit_freq = self._channel_visit_frequency(30)
            for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                hop_event[f'ch{ch}_visit_freq'] = round(visit_freq.get(ch, 0), 3)

            # Average dwell time in last 10 hops
            recent_dwells = [h['dwell_ms'] for h in self.hop_history[-10:]]
            hop_event['avg_dwell_last10'] = round(np.mean(recent_dwells), 1) if recent_dwells else 0

            # Save to history
            self.hop_history.append(hop_event)
            self.channel_sequence.append((self.current_channel, dwell_ms))

            # Update state
            self.current_channel = active_channel
            self.channel_start_time = now

            return hop_event

        return None

    def _count_recent_hops(self, now, window_seconds):
        """Count how many hops occurred in the last N seconds."""
        count = 0
        for hop in reversed(self.hop_history):
            hop_time = datetime.fromisoformat(hop['timestamp'])
            if (now - hop_time).total_seconds() > window_seconds:
                break
            count += 1
        return count

    def _channel_visit_frequency(self, window_seconds=30):
        """Calculate what fraction of recent time was spent on each channel."""
        if not self.channel_sequence:
            return {}

        total_ms = 0
        ch_time = {}
        for ch, dwell in reversed(self.channel_sequence):
            if total_ms > window_seconds * 1000:
                break
            ch_time[ch] = ch_time.get(ch, 0) + dwell
            total_ms += dwell

        if total_ms == 0:
            return {}

        return {ch: t / total_ms for ch, t in ch_time.items()}

    def get_summary(self):
        """Return summary statistics of tracked hops."""
        if not self.hop_history:
            return "No hops detected"

        dwells = [h['dwell_ms'] for h in self.hop_history]
        from_chs = [h['from_channel'] for h in self.hop_history]
        to_chs = [h['to_channel'] for h in self.hop_history]

        # Transition matrix
        transitions = {}
        for f, t in zip(from_chs, to_chs):
            key = (f, t)
            transitions[key] = transitions.get(key, 0) + 1

        summary = []
        summary.append("=" * 60)
        summary.append("  Channel Hop Tracking Summary")
        summary.append("=" * 60)
        summary.append(f"  Total hops detected:    {self.hop_count}")
        summary.append(f"  Average dwell time:     {np.mean(dwells):.0f} ms")
        summary.append(f"  Min dwell:              {np.min(dwells):.0f} ms")
        summary.append(f"  Max dwell:              {np.max(dwells):.0f} ms")
        summary.append(f"  Std dwell:              {np.std(dwells):.0f} ms")
        summary.append("")
        summary.append("  Channel visit counts:")
        from collections import Counter
        ch_counts = Counter(from_chs + to_chs)
        for ch in sorted(ch_counts.keys()):
            summary.append(f"    CH{ch:2d}: {ch_counts[ch]} visits")
        summary.append("")
        summary.append("  Top transitions (from → to):")
        for (f, t), count in sorted(transitions.items(), key=lambda x: -x[1])[:10]:
            summary.append(f"    CH{f} → CH{t}: {count} times")
        summary.append("=" * 60)

        return "\n".join(summary)


# ─────────────────────────────────────────────────────────────
# Process existing scanner CSV
# ─────────────────────────────────────────────────────────────

def process_scanner_csv(input_path, output_path):
    """Read a spectrum_scanner CSV and extract hop events."""
    tracker = HopTracker()

    print(f"[*] Processing: {input_path}")

    with open(input_path, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"[*] Loaded {len(rows)} scan records")

    hop_events = []
    for row in rows:
        # Reconstruct channel powers
        channel_powers = {}
        for ch in OCUSYNC_5GHZ_CHANNELS:
            key = f'ch{ch}_power'
            if key in row:
                channel_powers[ch] = float(row[key])

        active_ch = int(row.get('active_channel', 0))
        jam_ch = int(row.get('jam_channel', 0))

        event = tracker.process_scan(row['timestamp'], active_ch, channel_powers, jam_ch)
        if event:
            hop_events.append(event)

    print(f"[+] Detected {len(hop_events)} hop events")

    # Save hop events to CSV
    if hop_events and output_path:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=hop_events[0].keys())
            writer.writeheader()
            writer.writerows(hop_events)
        print(f"[+] Saved hop events to: {output_path}")

    # Print summary
    print(tracker.get_summary())

    return hop_events


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OcuSync Channel Hop Tracker")
    subparsers = parser.add_subparsers(dest='command')

    # Process command
    proc = subparsers.add_parser('process', help='Process existing scanner CSV')
    proc.add_argument('--input', '-i', required=True, help='Input scanner CSV')
    proc.add_argument('--output', '-o', required=True, help='Output hop events CSV')

    # Live command (imports scanner)
    live = subparsers.add_parser('live', help='Live tracking with USRP')
    live.add_argument('--duration', type=float, default=60, help='Duration in seconds')
    live.add_argument('--output', '-o', required=True, help='Output hop events CSV')
    live.add_argument('--jam-channel', type=int, default=None, help='Channel to jam')
    live.add_argument('--rx-gain', type=float, default=40, help='RX gain')
    live.add_argument('--tx-gain', type=float, default=50, help='TX gain for jamming')

    args = parser.parse_args()

    if args.command == 'process':
        process_scanner_csv(args.input, args.output)

    elif args.command == 'live':
        # Live mode — import scanner and run tracker in real-time
        from spectrum_scanner import SpectrumScanner
        from channel_config import SINGLE_CENTER_FREQ, SINGLE_SAMP_RATE, get_freq_from_channel
        import signal as sig
        import time

        running = True
        def stop(s, f):
            nonlocal running
            running = False
        sig.signal(sig.SIGINT, stop)

        scanner = SpectrumScanner(rx_gain=args.rx_gain)
        scanner.init_usrp()

        if args.jam_channel:
            jam_freq = get_freq_from_channel(args.jam_channel)
            if jam_freq:
                scanner.init_usrp_tx(jam_freq, tx_gain=args.tx_gain)

        tracker = HopTracker()
        hop_events = []
        start = time.time()

        try:
            while running and (time.time() - start) < args.duration:
                channel_power = scanner.compute_averaged_power()
                active = scanner.detect_active_channel(channel_power)
                active_ch = active[0][0] if active else 0

                event = tracker.process_scan(
                    datetime.now().isoformat(),
                    active_ch,
                    channel_power,
                    args.jam_channel or 0,
                )
                if event:
                    hop_events.append(event)
                    print(f"  HOP #{event['hop_number']}: CH{event['from_channel']} → CH{event['to_channel']} "
                          f"(dwell: {event['dwell_ms']:.0f}ms)")

                time.sleep(0.05)

        finally:
            scanner.stop()

            if hop_events:
                os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else '.', exist_ok=True)
                with open(args.output, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=hop_events[0].keys())
                    writer.writeheader()
                    writer.writerows(hop_events)
                print(f"\n[+] Saved {len(hop_events)} hops to {args.output}")

            print(tracker.get_summary())

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
