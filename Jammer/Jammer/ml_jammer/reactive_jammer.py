#!/usr/bin/env python3
"""
ML-Powered Reactive Jammer for DJI OcuSync 2.0
=================================================
Combines sweep-jamming with intelligent channel weighting based on
real-time observation of the drone's hopping behavior.

IMPROVEMENTS (v2):
  - CHASE: Uses full 56 MHz wideband buffer (same as all other modes).
    TX thread now actively monitors current_jam_channel and retunes itself
    without waiting for the main loop, eliminating the 1-scan lag.
    Holds last known channel for up to CHASE_HOLD_SCANS scans on signal loss.
  - WEIGHTED: Bootstraps position weights immediately from first detection
    (no longer waits for 10+ samples). Weights update every scan.
  - PREDICTIVE: Dwell time on predicted channel now scales linearly with
    confidence (1× at 40%, up to 4× at 100%). Falls back to blanket-style
    sweep when confidence is below threshold.
  - FOLLOW: Scan interval adapts to drone hop rate — tightens to 0.5s when
    the drone is hopping frequently (>3 hops/min) to track more aggressively.
  - MAIN LOOP: Sleep reduced from 20ms → 5ms for 4× faster reactiontime.
    After any detected hop, immediately performs a second confirm scan to
    verify the new channel before resuming TX.

Six jamming modes:

0. FOLLOW (--mode follow) — DEFAULT, RECOMMENDED
   Locks TX on the drone's current channel at 56 MHz / full power.
   Nearly 100% TX duty — only pauses briefly every few seconds to
   re-scan and find the drone. When a hop is detected, instantly
   retunes TX to follow. The drone gets almost zero clean air time.

1. BLANKET (--mode blanket)
   Cycles through 3 TX positions (56 MHz each) that together
   cover ALL 7 channels (CH0-CH6) with zero gaps. 22% duty per position.
   Good for initial reconnaissance.

1. WEIGHTED SWEEP (--mode weighted) — NO MODEL NEEDED
   Sweep-jams all 3 TX positions but spends MORE time on positions
   that cover the drone's preferred channels. Learns in real-time from
   a live transition matrix. As the drone reveals its channel preferences,
   the jammer shifts TX dwell time proportionally.
   
   Example: If the drone favors CH5 (38%) and CH2 (14%), position 3
   (covering CH5+CH6) gets ~40% dwell while position 1 (CH1+CH2) gets ~20%.

2. PREDICTIVE (--mode predictive) — NO MODEL NEEDED
   Tracks the drone's transition matrix in real-time. When a hop is detected,
   looks up "from CH X, the drone usually goes to CH Y" and pre-positions
   TX on that channel with full 56 MHz wideband noise BEFORE the drone lands.
   Falls back to weighted sweep between hops.

3. ML (--mode ml) — REQUIRES TRAINED MODEL
   Uses a trained Random Forest model to predict the next hop channel.
   Combines ML prediction with weighted sweep as fallback.

4. CHASE (--mode chase)
   Improved reactive: jam whatever channel the drone is currently on using
   full 56 MHz bandwidth. TX thread self-retunes on channel change without
   waiting for next scan cycle. Holds last channel during brief signal loss.

All modes use pulsed TX/RX for clean measurements and full-band RX sweep.

Usage:
    # Weighted sweep (no model needed, learns in real-time)
    python3 reactive_jammer.py --mode weighted --duration 60

    # Predictive (transition matrix, no model needed)
    python3 reactive_jammer.py --mode predictive --duration 60

    # ML-powered (requires trained model)
    python3 reactive_jammer.py --mode ml --model models/hop_predictor.pkl

    # Simple chase
    python3 reactive_jammer.py --mode chase --duration 60

    # Log everything
    python3 reactive_jammer.py --mode weighted --duration 120 --log hop_data/reactive_session.csv
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
from collections import defaultdict

# Suppress UHD underflow/overflow console spam (U/O characters)
import os as _os
_os.environ['UHD_LOG_LEVEL'] = 'error'
_os.environ['UHD_LOG_FASTPATH_DISABLE'] = '1'

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
    SWEEP_JAM_POSITIONS, SWEEP_JAM_TX_BW, SWEEP_JAM_TX_RATE,
    SWEEP_DWELL_MS, SWEEP_RETUNE_MS,
    BLANKET_JAM_POSITIONS, BLANKET_DWELL_MS, BLANKET_RETUNE_MS,
    RX_SWEEP_POSITIONS,
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
# Channel → Sweep Position Mapping
# ─────────────────────────────────────────────────────────────
# Which sweep position(s) cover each channel at full power?
# Position 0: 5745 MHz (56 MHz BW) → covers 5717-5773 → CH1, CH2
# Position 1: 5788 MHz (56 MHz BW) → covers 5760-5816 → CH3, CH4
# Position 2: 5831 MHz (56 MHz BW) → covers 5803-5859 → CH5, CH6

CHANNEL_TO_BEST_POSITION = {
    1: 0,  # CH1 → position 0 (5745 MHz)
    2: 0,  # CH2 → position 0 (5745 MHz)
    3: 1,  # CH3 → position 1 (5788 MHz)
    4: 1,  # CH4 → position 1 (5788 MHz)
    5: 2,  # CH5 → position 2 (5831 MHz)
    6: 2,  # CH6 → position 2 (5831 MHz)
}

POSITION_CHANNELS = {
    0: [1, 2],   # Position 0 covers CH1, CH2
    1: [3, 4],   # Position 1 covers CH3, CH4
    2: [5, 6],   # Position 2 covers CH5, CH6
}

# BLANKET mode: 3 positions cover ALL channels (CH0-CH6) with zero gaps
# Pos A (5745 MHz, 56 BW): 5717-5773 → CH0, CH1, CH2
# Pos B (5791 MHz, 56 BW): 5763-5819 → CH3, CH4 (overlaps CH2, CH5)
# Pos C (5835 MHz, 56 BW): 5807-5863 → CH5, CH6 (overlaps CH4)
BLANKET_POSITION_CHANNELS = {
    0: [0, 1, 2],      # Low band — covers 5717-5773
    1: [2, 3, 4, 5],   # Mid band — covers 5763-5819
    2: [4, 5, 6],      # High band — covers 5807-5863
}


# ─────────────────────────────────────────────────────────────
# Reactive Jammer Engine
# ─────────────────────────────────────────────────────────────

class ReactiveJammer:
    """
    Full-duplex reactive jammer with weighted sweep and predictive modes.
    
    RX chain: Full-band sweep scanner monitoring all 6 channels
    TX chain: 56 MHz wideband noise, hopping across 3 positions with
              intelligent dwell weighting based on drone behavior.
    """

    def __init__(self, mode='weighted', model_path=None,
                 tx_gain=DEFAULT_TX_GAIN, rx_gain=DEFAULT_RX_GAIN,
                 scan_interval=3.0):
        self.mode = mode
        self.tx_gain = tx_gain
        self.rx_gain = rx_gain
        self.scan_interval = scan_interval

        # Follow mode state: TX freq to lock onto
        self.follow_freq = None  # Set after first scan
        self.follow_channel = None

        # Components
        self.scanner = SpectrumScanner(rx_gain=rx_gain)
        self.tracker = HopTracker()
        self.predictor = None

        # Load ML model if needed
        if mode == 'ml' and model_path:
            from ml_predictor import HopPredictor
            self.predictor = HopPredictor(model_path)

        # TX state
        self.tx_active = False
        self.tx_paused = False
        self.tx_thread = None
        self.current_jam_info = "initializing"

        # Chase mode: shared channel state so TX thread can self-retune
        self.current_jam_channel = None   # Set by main loop; read by TX thread
        self.chase_hold_scans = 0         # Scans to hold last channel on signal loss
        CHASE_HOLD_SCANS = 8              # ~40ms at 5ms sleep before giving up

        # === LIVE TRANSITION MATRIX ===
        # Tracks observed channel transitions in real-time
        # transitions[from_ch][to_ch] = count
        self.transitions = defaultdict(lambda: defaultdict(int))
        self.channel_detections = defaultdict(int)  # How often each channel seen
        self.total_hops = 0

        # === WEIGHTED SWEEP STATE ===
        # Position weights (0-2) — higher = more dwell time
        # Start uniform, adapt based on drone behavior
        self.position_weights = [1.0, 1.0, 1.0]
        self.current_sweep_pos = 0

        # === PREDICTIVE STATE ===
        self.predicted_channel = None
        self.prediction_confidence = 0
        self.last_active_channel = None

        # Noise buffers — large to prevent TX underflows (U warnings)
        # At 56 Msps, 131072 samples = ~2.3ms per send() call
        # Amplitude set to 1.0 for MAXIMUM TX power output across all modes.
        bufsize = 131072
        self.noise_buffer = (np.random.normal(0, 1.0, bufsize) +
                             1j * np.random.normal(0, 1.0, bufsize)).astype(np.complex64)
        self.noise_buffer = np.clip(self.noise_buffer.real, -1.0, 1.0) + \
                            1j * np.clip(self.noise_buffer.imag, -1.0, 1.0)
        self.noise_buffer = self.noise_buffer.astype(np.complex64)
        # Chase mode now also uses the full wideband buffer (56 MHz)
        # No separate narrow buffer needed.

        # Statistics
        num_pos = 3 if mode == 'blanket' else 3
        self.stats = {
            'total_scans': 0,
            'hops_detected': 0,
            'predictions_made': 0,
            'predictions_correct': 0,
            'jam_switches': 0,
            'start_time': None,
            'position_dwells': [0] * num_pos,
        }

    def init_hardware(self):
        """Initialize USRP for full-duplex RX + TX."""
        print("[*] Initializing USRP B210 (full-duplex)...")
        self.scanner.init_usrp()

        if self.mode == 'chase':
            # Chase mode: now uses full 56 MHz wideband TX (same as other modes)
            self.scanner.init_usrp_tx_sweep(tx_gain=self.tx_gain)
            print(f"[+] CHASE mode: 56 MHz wideband TX, follows active channel")
        elif self.mode == 'follow':
            # Follow mode: 56 MHz wideband TX, locked on one channel
            self.scanner.init_usrp_tx_sweep(tx_gain=self.tx_gain)
            print(f"[+] FOLLOW mode: 56 MHz TX locked on drone's channel")
            print(f"[+] Scan interval: {self.scan_interval}s (TX off ~11ms per scan)")
            print(f"[+] TX duty cycle: ~{100*(1 - 0.011/self.scan_interval):.1f}%")
        elif self.mode == 'blanket':
            # Blanket mode: same 56 MHz wideband TX, but only 2 positions
            self.scanner.init_usrp_tx_sweep(tx_gain=self.tx_gain)
            cycle_ms = len(BLANKET_JAM_POSITIONS) * (BLANKET_DWELL_MS + BLANKET_RETUNE_MS)
            print(f"[+] BLANKET mode: {len(BLANKET_JAM_POSITIONS)} positions, {BLANKET_DWELL_MS}ms dwell, {cycle_ms:.0f}ms cycle")
            for i, freq in enumerate(BLANKET_JAM_POSITIONS):
                lo = (freq - 28e6) / 1e6
                hi = (freq + 28e6) / 1e6
                chs = ','.join(f'CH{c}' for c in BLANKET_POSITION_CHANNELS[i])
                print(f"[+] Pos {i}: {freq/1e6:.0f} MHz ({lo:.0f}-{hi:.0f}) → {chs}")
            print(f"[+] Duty per position: ~{100*BLANKET_DWELL_MS/cycle_ms:.0f}%")
        elif self.mode == 'blind':
            # Blind blanket: pure TX cycling, NO RX, ZERO gaps
            self.scanner.init_usrp_tx_sweep(tx_gain=self.tx_gain)
            print(f"[+] BLIND BLANKET mode: {len(BLANKET_JAM_POSITIONS)} positions, 3ms dwell, ~10.5ms cycle")
            print(f"[+] NO RX SCANNING — pure TX, zero gaps!")
            for i, freq in enumerate(BLANKET_JAM_POSITIONS):
                lo = (freq - 28e6) / 1e6
                hi = (freq + 28e6) / 1e6
                print(f"[+] Pos {i}: {freq/1e6:.0f} MHz ({lo:.0f}-{hi:.0f})")
            print(f"[+] Duty per position: ~33%, cycle rate: ~96/sec")
        else:
            # All other modes: wideband 56 MHz TX for sweep positions
            self.scanner.init_usrp_tx_sweep(tx_gain=self.tx_gain)

        self.tx_active = True
        self.stats['start_time'] = time.time()
        print(f"[+] Hardware ready — mode: {self.mode.upper()}")

    def _update_transition_matrix(self, from_ch, to_ch):
        """Update the live transition matrix with an observed hop."""
        self.transitions[from_ch][to_ch] += 1
        self.total_hops += 1

    def _get_predicted_next(self, current_ch):
        """
        From the live transition matrix, predict the most likely next channel.
        Returns (predicted_channel, confidence) or (None, 0) if no data.
        """
        if current_ch not in self.transitions or not self.transitions[current_ch]:
            return None, 0

        trans = self.transitions[current_ch]
        total = sum(trans.values())
        if total == 0:
            return None, 0

        best_ch = max(trans, key=trans.get)
        confidence = trans[best_ch] / total
        return best_ch, confidence

    def _compute_position_weights(self):
        """
        Compute sweep position weights from channel detection frequency.
        Positions covering more-visited channels get more TX dwell time.
        Ensures minimum 15% weight per position (always cover everything).

        IMPROVED: Bootstrap from first detection — no longer waits for 10+
        samples. Even a single detection biases the weights immediately.
        Uses a blended approach: raw observed frequency blended with a
        uniform prior weighted by (10 / (10 + N)) so it converges quickly.
        """
        total_detections = sum(self.channel_detections.values())
        if total_detections == 0:
            return [1.0, 1.0, 1.0]

        weights = [0.0, 0.0, 0.0]
        for ch, count in self.channel_detections.items():
            if ch in CHANNEL_TO_BEST_POSITION:
                pos = CHANNEL_TO_BEST_POSITION[ch]
                weights[pos] += count / total_detections

        # Blend with uniform prior: prior_strength decays as N grows
        # At N=1 → 91% prior, N=5 → 67% prior, N=20 → 33% prior, N=50 → 17%
        prior_strength = 10.0 / (10.0 + total_detections)
        for i in range(3):
            weights[i] = prior_strength * (1.0 / 3.0) + (1 - prior_strength) * weights[i]

        # Ensure minimum weight of 15% per position
        min_weight = 0.15
        for i in range(3):
            weights[i] = max(weights[i], min_weight)

        # Normalize to sum to 1
        total = sum(weights)
        weights = [w / total for w in weights]

        return weights

    def _tx_worker_weighted_sweep(self):
        """
        TX thread: sweep across 3 positions with weighted dwell times.
        Positions covering the drone's preferred channels get more TX time.
        
        Dwell time per position = base_dwell * weight[pos] * 3
        This means the most-visited position can get up to ~45% of TX time.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        base_dwell = SWEEP_DWELL_MS / 1000.0
        retune_time = SWEEP_RETUNE_MS / 1000.0
        pos_idx = 0
        num_positions = len(SWEEP_JAM_POSITIONS)

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue

            # Get current weights
            weights = self.position_weights

            # Retune to current position
            target_freq = SWEEP_JAM_POSITIONS[pos_idx]
            try:
                self.scanner.usrp.set_tx_freq(
                    uhd.libpyuhd.types.tune_request(target_freq))
            except Exception:
                break

            time.sleep(retune_time)
            if not self.tx_active or self.tx_paused:
                continue

            # Weighted dwell: multiply base dwell by position weight * 3
            # So weight=0.5 → 3ms dwell, weight=0.33 → 2ms, weight=0.15 → ~0.9ms
            dwell_s = base_dwell * weights[pos_idx] * 3
            dwell_s = max(0.001, dwell_s)  # Minimum 1ms

            dwell_start = time.monotonic()
            while (time.monotonic() - dwell_start) < dwell_s:
                if not self.tx_active or self.tx_paused:
                    break
                try:
                    self.scanner.tx_streamer.send(self.noise_buffer, metadata)
                except Exception:
                    break

            self.stats['position_dwells'][pos_idx] += 1
            pos_idx = (pos_idx + 1) % num_positions

    def _tx_worker_predictive(self):
        """
        TX thread for predictive mode.
        When we have a prediction, camp on the predicted channel's position
        for longer. Otherwise, do weighted sweep.

        IMPROVED: Dwell time on predicted position now scales linearly with
        confidence: 1× at 40% → 4× at 100%. Below 40% confidence, falls
        back to a blanket-style 3-position sweep with equal dwell so the
        drone has nowhere to hide while we wait for better data.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        base_dwell = SWEEP_DWELL_MS / 1000.0
        retune_time = SWEEP_RETUNE_MS / 1000.0
        pos_idx = 0
        num_positions = len(SWEEP_JAM_POSITIONS)
        MIN_CONFIDENCE = 0.35   # Below this → blanket sweep, no prediction used
        MAX_DWELL_MULT = 4.0    # At 100% confidence, dwell 4× longer on target

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue

            pred_pos = None
            confidence = self.prediction_confidence
            if self.predicted_channel and confidence >= MIN_CONFIDENCE:
                pred_pos = CHANNEL_TO_BEST_POSITION.get(self.predicted_channel)

            # Choose position and compute dwell multiplier
            if pred_pos is not None:
                # Scale dwell with confidence: linear from 1× to MAX_DWELL_MULT
                dwell_mult = 1.0 + (MAX_DWELL_MULT - 1.0) * (
                    (confidence - MIN_CONFIDENCE) / (1.0 - MIN_CONFIDENCE)
                )
                dwell_mult = min(MAX_DWELL_MULT, max(1.0, dwell_mult))

                # Apportion cycle: predicted gets (dwell_mult) slots, others 1 each
                # e.g. mult=3 → cycle [pred, pred, pred, other1, other2]
                slots = max(1, round(dwell_mult))
                cycle_positions = [pred_pos] * slots
                for p in range(num_positions):
                    if p != pred_pos:
                        cycle_positions.append(p)
                target_pos = cycle_positions[pos_idx % len(cycle_positions)]
                dwell_s = base_dwell * (dwell_mult if target_pos == pred_pos else 1.0)
            else:
                # No reliable prediction → blanket sweep: all 3 positions equal
                target_pos = pos_idx % num_positions
                dwell_s = base_dwell

            target_freq = SWEEP_JAM_POSITIONS[target_pos]
            try:
                self.scanner.usrp.set_tx_freq(
                    uhd.libpyuhd.types.tune_request(target_freq))
            except Exception:
                break

            time.sleep(retune_time)
            if not self.tx_active or self.tx_paused:
                continue

            dwell_start = time.monotonic()
            while (time.monotonic() - dwell_start) < dwell_s:
                if not self.tx_active or self.tx_paused:
                    break
                try:
                    self.scanner.tx_streamer.send(self.noise_buffer, metadata)
                except Exception:
                    break

            self.stats['position_dwells'][target_pos] += 1
            pos_idx += 1

    def _tx_worker_chase(self):
        """
        TX thread for chase mode: transmit on current active channel.

        IMPROVED:
        - Uses full 56 MHz wideband noise buffer (same power as all other modes).
        - Actively monitors self.current_jam_channel and retunes TX immediately
          when the main loop updates it — no waiting for next scan cycle.
        - This eliminates the 1-scan lag that made chase mode slow to react.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        active_freq = None

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue

            # Check if main loop signalled a channel change
            target_ch = self.current_jam_channel
            if target_ch is not None:
                target_freq = get_freq_from_channel(target_ch)
                if target_freq and target_freq != active_freq:
                    try:
                        self.scanner.usrp.set_tx_freq(
                            uhd.libpyuhd.types.tune_request(target_freq))
                        active_freq = target_freq
                        time.sleep(0.001)  # PLL settle
                    except Exception:
                        break

            if active_freq is None:
                time.sleep(0.001)
                continue

            try:
                self.scanner.tx_streamer.send(self.noise_buffer, metadata)
            except Exception:
                break

    def _tx_worker_follow(self):
        """
        TX thread for FOLLOW mode: continuous 56 MHz noise on one frequency.
        
        The main loop sets self.follow_freq when it detects the drone.
        This thread just blasts noise non-stop, only pausing when told to
        for brief RX scans. Retunes when follow_freq changes.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        current_freq = None

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.0005)
                continue

            # Check if we need to retune
            target = self.follow_freq
            if target is not None and target != current_freq:
                try:
                    self.scanner.usrp.set_tx_freq(
                        uhd.libpyuhd.types.tune_request(target))
                    current_freq = target
                    time.sleep(0.001)  # PLL settle
                except Exception:
                    break

            if current_freq is None:
                # No target yet, wait for first scan
                time.sleep(0.001)
                continue

            # Blast noise continuously
            try:
                self.scanner.tx_streamer.send(self.noise_buffer, metadata)
            except Exception:
                break

    def _tx_worker_blanket(self):
        """
        TX thread for BLANKET mode: cycle through 3 positions.
        
        Pos A (5745 MHz, 56 BW) → 5717-5773 → CH0, CH1, CH2
        Pos B (5791 MHz, 56 BW) → 5763-5819 → CH3, CH4
        Pos C (5835 MHz, 56 BW) → 5807-5863 → CH5, CH6
        
        9ms cycle (2ms dwell + 1ms retune × 3).
        Full ISM coverage from 5717-5863 MHz. Nowhere to hide.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        dwell_s = BLANKET_DWELL_MS / 1000.0
        retune_s = BLANKET_RETUNE_MS / 1000.0
        num_positions = len(BLANKET_JAM_POSITIONS)
        pos_idx = 0

        while self.tx_active:
            if self.tx_paused:
                time.sleep(0.001)
                continue

            # Retune to current position
            target_freq = BLANKET_JAM_POSITIONS[pos_idx]
            try:
                self.scanner.usrp.set_tx_freq(
                    uhd.libpyuhd.types.tune_request(target_freq))
            except Exception:
                break

            time.sleep(retune_s)
            if not self.tx_active or self.tx_paused:
                continue

            # Transmit noise for dwell period
            dwell_start = time.monotonic()
            while (time.monotonic() - dwell_start) < dwell_s:
                if not self.tx_active or self.tx_paused:
                    break
                try:
                    self.scanner.tx_streamer.send(self.noise_buffer, metadata)
                except Exception:
                    break

            self.stats['position_dwells'][pos_idx] += 1
            pos_idx = (pos_idx + 1) % num_positions  # Cycle: 0→1→2→0→1→2...

    def _tx_worker_blind_blanket(self):
        """
        TX thread for BLIND BLANKET mode: cycle 3 positions with ZERO RX gaps.
        
        Pure TX — no scanning, no pauses, no gaps. The drone has nowhere to hide.
        Cycles through 3 positions covering 5717-5863 MHz continuously.
        
        Strategy: Send N noise buffers per position, then retune.
        At 56 Msps, each 131072-sample buffer = 2.3ms.
        3 buffers per position = ~7ms TX time per position.
        Retune takes ~1ms. Full cycle = 3 × (7+1) = ~24ms = ~42 cycles/sec.
        Each position duty: ~33% but cycling fast enough to disrupt OcuSync.
        """
        metadata = uhd.types.TXMetadata()
        metadata.has_time_spec = False
        SENDS_PER_POS = 3     # 3 × 2.3ms = ~7ms per position
        num_positions = len(BLANKET_JAM_POSITIONS)
        pos_idx = 0

        # Pre-tune to first position
        try:
            self.scanner.usrp.set_tx_freq(
                uhd.libpyuhd.types.tune_request(BLANKET_JAM_POSITIONS[0]))
            time.sleep(0.001)
        except Exception:
            return

        while self.tx_active:
            # Send N buffers on current position
            for _ in range(SENDS_PER_POS):
                if not self.tx_active:
                    return
                try:
                    self.scanner.tx_streamer.send(self.noise_buffer, metadata)
                except Exception:
                    return

            self.stats['position_dwells'][pos_idx] += 1

            # Move to next position
            pos_idx = (pos_idx + 1) % num_positions

            # Retune — with retry on PLL lock failure
            target_freq = BLANKET_JAM_POSITIONS[pos_idx]
            for attempt in range(3):
                try:
                    self.scanner.usrp.set_tx_freq(
                        uhd.libpyuhd.types.tune_request(target_freq))
                    break
                except Exception:
                    if attempt == 2:
                        return
                    time.sleep(0.001)

    def start_tx_thread(self):
        """Start the appropriate TX thread based on mode."""
        if self.mode == 'follow':
            worker = self._tx_worker_follow
            desc = "follow (locked on drone)"
        elif self.mode in ('blanket', 'blind'):
            if self.mode == 'blind':
                worker = self._tx_worker_blind_blanket
                desc = "BLIND BLANKET (NO RX, pure TX cycling, ZERO gaps)"
            else:
                worker = self._tx_worker_blanket
                desc = "blanket (3-pos cycle)"
        elif self.mode == 'weighted':
            worker = self._tx_worker_weighted_sweep
            desc = "weighted sweep"
        elif self.mode in ('predictive', 'ml'):
            worker = self._tx_worker_predictive
            desc = "predictive sweep"
        elif self.mode == 'chase':
            worker = self._tx_worker_chase
            desc = "chase"
        else:
            worker = self._tx_worker_weighted_sweep
            desc = "weighted sweep (default)"

        self.tx_thread = threading.Thread(target=worker, daemon=True)
        self.tx_thread.start()
        print(f"[+] TX thread started ({desc})")

    def pause_tx(self):
        """Pause TX for clean RX measurement."""
        self.tx_paused = True
        time.sleep(0.005)

    def resume_tx(self):
        """Resume TX after RX measurement."""
        self.tx_paused = False

    def _quick_scan_peak(self):
        """
        Fast RX scan using raw FFT peak finder.
        Returns (peak_freq_hz, peak_snr, band_power_dict).
        TX is paused briefly for clean RX.
        """
        self.tx_paused = True
        time.sleep(0.005)  # 5ms drain — let TX residual fully clear

        peak_freq, peak_snr, band_power = self.scanner.find_drone_peak(num_averages=4)

        self.tx_paused = False  # Resume TX immediately
        return peak_freq, peak_snr, band_power

    def _run_follow_loop(self, duration, start_time, csv_writer, csv_file, log_path):
        """
        FOLLOW mode main loop — RAW PEAK DETECTION (no channel map).
        
        Scans the full ISM band 5725.5-5849.5 MHz, finds the strongest
        20 MHz peak, and locks TX directly on that frequency.
        
        Strategy:
        1. Scan full band, find 20 MHz peak
        2. Lock TX on that exact frequency, 56 MHz BW, full power
        3. Re-scan every scan_interval seconds
        4. When drone hops, follow to new frequency
        5. Between scans, TX blasts continuously at ~100% duty
        """
        global running

        scan_count = 0
        last_scan_time = 0
        last_freq = None          # Last detected drone freq (Hz)
        hop_count = 0
        FOLLOW_SNR_MIN = 3.5      # Lowered — raw peak finder uses percentile noise, robust
        FREQ_TOLERANCE = 5e6      # Two detections within 5 MHz = same target
        HOP_THRESHOLD = 8e6       # Freq change > 8 MHz = a hop

        # Sliding window lock: track last N scans for confirmation
        WINDOW_SIZE = 5           # Look at last 5 scans
        LOCK_HITS_NEEDED = 2      # Need 2 hits within window to lock
        recent_detections = []    # List of (freq, snr) for last N scans, None if no detection

        # Time-based unlock: keep jamming even without seeing signal
        UNLOCK_TIMEOUT = 15.0     # Keep TX on last freq for 15s after signal loss
        last_detection_time = 0   # Monotonic time of last positive detection while locked

        print("[*] FOLLOW mode (raw peak): scanning 5725-5850 MHz for drone...")
        print(f"[*] Lock: {LOCK_HITS_NEEDED} detections in {WINDOW_SIZE} scans | Unlock: {UNLOCK_TIMEOUT}s timeout")

        try:
            while running:
                elapsed = time.time() - start_time
                if duration and elapsed >= duration:
                    print(f"\n[*] Duration reached ({duration}s)")
                    break

                time_since_scan = time.monotonic() - last_scan_time
                search_mode = self.follow_freq is None
                # IMPROVED: Adaptive scan interval based on hop rate.
                # If hopping frequently (>3 hops in last 60s), tighten to 0.5s
                # so we re-lock faster after each hop.
                recent_hops = self.stats['hops_detected']
                elapsed_follow = time.time() - self.stats['start_time'] if self.stats['start_time'] else 1
                hops_per_min = (recent_hops / max(elapsed_follow, 1)) * 60
                if search_mode:
                    interval = 0.3
                elif hops_per_min > 3:
                    interval = 0.5  # Drone is hopping a lot — scan more often
                else:
                    interval = 1.5  # Calm — reduce TX gaps

                if time_since_scan >= interval:
                    peak_freq, peak_snr, band_power = self._quick_scan_peak()
                    scan_count += 1
                    last_scan_time = time.monotonic()
                    self.stats['total_scans'] = scan_count
                    hop_this_scan = False

                    # Track detection in sliding window
                    if peak_freq is not None and peak_snr >= FOLLOW_SNR_MIN:
                        recent_detections.append((peak_freq, peak_snr))
                    else:
                        recent_detections.append(None)
                    # Keep window size
                    if len(recent_detections) > WINDOW_SIZE:
                        recent_detections.pop(0)

                    if peak_freq is not None and peak_snr >= FOLLOW_SNR_MIN:
                        # Good detection this scan
                        if self.follow_freq is None:
                            # Not locked — check sliding window for confirmation
                            # Count how many of last N scans had a detection near this freq
                            hits_near = sum(1 for d in recent_detections if d is not None
                                            and abs(d[0] - peak_freq) < FREQ_TOLERANCE)

                            if hits_near >= LOCK_HITS_NEEDED:
                                # LOCK! Use the best SNR detection's freq
                                best = max((d for d in recent_detections if d is not None
                                            and abs(d[0] - peak_freq) < FREQ_TOLERANCE),
                                           key=lambda x: x[1])
                                self.follow_freq = best[0]
                                self.follow_channel = None
                                last_freq = best[0]
                                last_detection_time = time.monotonic()
                                print(f"  *** LOCKED @ {best[0]/1e6:.1f} MHz "
                                      f"(SNR: {best[1]:.0f}dB, {hits_near}/{WINDOW_SIZE} hits) → TX blasting")
                            else:
                                print(f"  [CANDIDATE] {peak_freq/1e6:.1f} MHz "
                                      f"(SNR: {peak_snr:.0f}dB) — {hits_near}/{LOCK_HITS_NEEDED} in window")
                        else:
                            # Locked — update detection time and check for hop
                            last_detection_time = time.monotonic()
                            if last_freq is not None and abs(peak_freq - last_freq) > HOP_THRESHOLD:
                                hop_count += 1
                                hop_this_scan = True
                                self.stats['hops_detected'] = hop_count
                                print(f"  HOP #{hop_count}: {last_freq/1e6:.1f} → {peak_freq/1e6:.1f} MHz "
                                      f"(SNR: {peak_snr:.0f}dB, Δ={abs(peak_freq-last_freq)/1e6:.0f}MHz)")

                            last_freq = peak_freq
                            self.follow_freq = peak_freq

                    else:
                        # No detection this scan
                        if self.follow_freq is not None:
                            # Check time-based unlock
                            time_since_last = time.monotonic() - last_detection_time
                            if time_since_last >= UNLOCK_TIMEOUT:
                                print(f"  *** UNLOCKED — no signal for {time_since_last:.0f}s, back to SEARCH")
                                self.follow_freq = None
                                self.follow_channel = None
                                recent_detections.clear()
                            else:
                                remaining = UNLOCK_TIMEOUT - time_since_last
                                print(f"  [HOLDING] TX on {self.follow_freq/1e6:.1f} MHz — "
                                      f"no signal, {remaining:.0f}s until unlock")

                    # Status display — show band power as spectrum bar
                    nf = self.scanner.noise_floor_db
                    if band_power:
                        # Show key frequencies across the band
                        display_freqs = [5736, 5756, 5776, 5796, 5816, 5836]
                        bars = []
                        for f_mhz in display_freqs:
                            pwr = band_power.get(f_mhz, band_power.get(f_mhz-1, -100))
                            # Find closest key in band_power
                            if pwr == -100:
                                closest = min(band_power.keys(), key=lambda k: abs(k - f_mhz))
                                pwr = band_power[closest]
                            snr = pwr - nf
                            if peak_freq and abs(f_mhz * 1e6 - peak_freq) < 15e6 and peak_snr >= FOLLOW_SNR_MIN:
                                bars.append(f"\033[91m{f_mhz}:{snr:+.0f}\033[0m")
                            else:
                                bars.append(f"{f_mhz}:{snr:+.0f}")
                        spec_str = ' '.join(bars)
                    else:
                        spec_str = "no data"

                    if self.follow_freq is not None:
                        time_since = time.monotonic() - last_detection_time
                        state = f"TX→{self.follow_freq/1e6:.1f}MHz ({time_since:.0f}s ago)"
                    else:
                        hits = sum(1 for d in recent_detections if d is not None)
                        state = f"SEARCHING... ({hits}/{LOCK_HITS_NEEDED} in window)"
                    print(f"  [{elapsed:5.0f}s] {spec_str} "
                          f"| {state} "
                          f"| Hops:{hop_count} | Scans:{scan_count}")

                    # CSV log
                    if csv_writer:
                        row = {
                            'timestamp': datetime.now().isoformat(),
                            'scan_num': scan_count,
                            'active_channel': round(peak_freq/1e6, 1) if peak_freq else -1,
                            'active_snr': round(peak_snr, 2),
                            'predicted_channel': -1,
                            'pred_confidence': 0,
                            'hop_detected': int(hop_this_scan),
                            'hop_from': round(last_freq/1e6, 1) if last_freq and hop_this_scan else -1,
                            'hop_to': round(peak_freq/1e6, 1) if peak_freq and hop_this_scan else -1,
                            'position_weights': '0,0,0',
                            'mode': 'follow',
                        }
                        for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                            row[f'ch{ch}_power'] = round(band_power.get(int(OCUSYNC_5GHZ_CHANNELS[ch]/1e6), -100), 2)
                        csv_writer.writerow(row)

                else:
                    time.sleep(0.05)

        finally:
            self.tx_active = False
            if self.tx_thread and self.tx_thread.is_alive():
                self.tx_thread.join(timeout=2.0)
            self.scanner.stop()
            if csv_file:
                csv_file.close()
                print(f"[+] Log saved to: {log_path}")

            elapsed = time.time() - start_time
            print(f"\n{'=' * 65}")
            print(f"  JAMMING SESSION COMPLETE — FOLLOW MODE (RAW PEAK)")
            print(f"{'=' * 65}")
            print(f"  Duration:            {elapsed:.1f} seconds")
            print(f"  Total scans:         {scan_count} (every {self.scan_interval}s)")
            print(f"  TX duty cycle:       ~{100*(1 - scan_count*0.008/max(elapsed,1)):.1f}%")
            print(f"  Hops detected:       {hop_count}")
            if self.follow_freq is not None:
                print(f"  Last TX target:      {self.follow_freq/1e6:.1f} MHz")
            if last_freq:
                print(f"  Last drone seen at:  {last_freq/1e6:.1f} MHz")
            print(f"{'=' * 65}")

    def _run_blind_loop(self, duration, start_time, log_path):
        """
        BLIND BLANKET mode: TX only, no RX. Just wait and report.
        
        The TX thread cycles through 3 positions covering 5717-5863 MHz.
        This loop just monitors timing and prints status.
        """
        global running

        print("[*] BLIND BLANKET: TX cycling 3 positions, NO RX scanning")
        print("[*] The drone has nowhere to hide. Ctrl+C to stop.\n")

        try:
            while running:
                elapsed = time.time() - start_time
                if duration and elapsed >= duration:
                    print(f"\n[*] Duration reached ({duration}s)")
                    break

                # Status report every 5 seconds
                dwells = self.stats['position_dwells']
                total_dwells = sum(dwells) or 1
                cycle_count = min(dwells) if dwells else 0
                rates = [f"P{i}:{d}" for i, d in enumerate(dwells)]
                print(f"  [{elapsed:5.0f}s] Cycles: {cycle_count} | Dwells: {' '.join(rates)} "
                      f"| Rate: {total_dwells/max(elapsed,1):.0f}/s")
                time.sleep(5.0)

        finally:
            self.tx_active = False
            if self.tx_thread and self.tx_thread.is_alive():
                self.tx_thread.join(timeout=2.0)
            self.scanner.stop()

            elapsed = time.time() - start_time
            dwells = self.stats['position_dwells']
            total_dwells = sum(dwells)
            print(f"\n{'=' * 65}")
            print(f"  JAMMING SESSION COMPLETE — BLIND BLANKET MODE")
            print(f"{'=' * 65}")
            print(f"  Duration:            {elapsed:.1f} seconds")
            print(f"  Total TX dwells:     {total_dwells}")
            print(f"  Full cycles:         {min(dwells) if dwells else 0}")
            print(f"  Cycle rate:          {total_dwells/3/max(elapsed,1):.0f} cycles/sec")
            print(f"  TX duty:             100% (NO RX gaps)")
            for i, freq in enumerate(BLANKET_JAM_POSITIONS):
                lo = (freq - 28e6) / 1e6
                hi = (freq + 28e6) / 1e6
                print(f"  Pos {i}: {freq/1e6:.0f} MHz ({lo:.0f}-{hi:.0f}) — {dwells[i]} dwells")
            print(f"{'=' * 65}")

    def run(self, duration=None, log_path=None):
        """Main jamming + scanning loop."""
        global running

        # Print banner
        print(f"\n{'=' * 65}")
        print(f"  ML REACTIVE JAMMER — {self.mode.upper()} MODE")
        print(f"  TX: {self.tx_gain} dB | RX: {self.rx_gain} dB")
        if self.mode == 'follow':
            print(f"  Follow: TX locked on drone channel, 56 MHz BW")
            print(f"  Search scan: 0.3s | Locked scan: 1.5s | Unlock timeout: 15s")
        elif self.mode == 'blanket':
            cycle_ms = len(BLANKET_JAM_POSITIONS) * (BLANKET_DWELL_MS + BLANKET_RETUNE_MS)
            print(f"  Blanket: {len(BLANKET_JAM_POSITIONS)} positions @ 56 MHz, {BLANKET_DWELL_MS}ms dwell, {cycle_ms:.0f}ms cycle")
            print(f"  Coverage: 5717-5863 MHz (CH0-CH6), ~{100*BLANKET_DWELL_MS/cycle_ms:.0f}% duty/pos")
        elif self.mode == 'blind':
            print(f"  BLIND BLANKET: {len(BLANKET_JAM_POSITIONS)} positions @ 56 MHz, 3ms dwell")
            print(f"  NO RX SCANNING — 100% TX duty, ZERO gaps!")
            print(f"  Coverage: 5717-5863 MHz, ~33% duty per position, ~96 cycles/sec")
        else:
            print(f"  Sweep: 3 positions @ 56 MHz, {SWEEP_DWELL_MS}ms base dwell")
        if self.predictor:
            print(f"  ML Model accuracy: {self.predictor.accuracy:.1%}")
        print(f"{'=' * 65}\n")

        # Start TX
        self.start_tx_thread()

        # CSV log
        csv_file = None
        csv_writer = None
        if log_path:
            os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else '.', exist_ok=True)
            csv_file = open(log_path, 'w', newline='')
            fields = [
                'timestamp', 'scan_num', 'active_channel', 'active_snr',
                'predicted_channel', 'pred_confidence',
                'hop_detected', 'hop_from', 'hop_to',
                'position_weights', 'mode',
            ] + [f'ch{ch}_power' for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys())]
            csv_writer = csv.DictWriter(csv_file, fieldnames=fields)
            csv_writer.writeheader()

        start_time = time.time()

        # === FOLLOW MODE: completely different loop ===
        if self.mode == 'follow':
            self._run_follow_loop(duration, start_time, csv_writer, csv_file, log_path)
            return

        # === BLIND BLANKET MODE: TX only, no RX, just wait ===
        if self.mode == 'blind':
            self._run_blind_loop(duration, start_time, log_path)
            return

        # === PERIODIC SCANNING (like follow mode) ===
        # KEY INSIGHT: Follow mode achieves ~98.5% TX duty because it scans
        # only every 0.5-1.5s instead of every loop cycle. We apply the same
        # strategy here: TX runs uninterrupted, brief RX scans happen periodically.
        #
        # Old approach: scan every ~25ms → ~40% TX duty (bad!)
        # New approach: scan every 0.5-1.0s → ~98% TX duty (like follow mode)
        SCAN_INTERVALS = {
            'chase': 0.5,       # Needs fast tracking to follow hops
            'predictive': 0.5,  # Needs fast updates for prediction
            'ml': 0.5,          # Same as predictive
            'weighted': 1.0,    # Weights change slowly, less frequent OK
            'blanket': 2.0,     # Covers everything anyway, scan just for stats
        }
        scan_interval = SCAN_INTERVALS.get(self.mode, 0.5)
        last_scan_time = 0  # Force immediate first scan
        channel_power = {}  # Last known channel power (for display/logging)

        print(f"[+] Periodic scan interval: {scan_interval}s "
              f"(TX duty: ~{100*(1 - 0.011/scan_interval):.1f}%)")

        try:
            while running:
                elapsed = time.time() - start_time
                if duration and elapsed >= duration:
                    print(f"\n[*] Duration reached ({duration}s)")
                    break

                time_since_scan = time.monotonic() - last_scan_time

                if time_since_scan >= scan_interval:
                    # === BRIEF PAUSE → FAST RX SCAN → RESUME ===
                    # Uses _quick_scan_peak (4 averages, ~11ms total pause)
                    # instead of compute_full_band_power (8 avg, ~20ms).
                    # This is the same scan method follow mode uses.
                    peak_freq, peak_snr, band_power = self._quick_scan_peak()
                    last_scan_time = time.monotonic()
                    self.stats['total_scans'] += 1

                    # Convert peak frequency → channel number
                    from channel_config import get_channel_from_freq
                    active_ch = get_channel_from_freq(peak_freq) if peak_freq and peak_snr >= 3.5 else None
                    active_snr = peak_snr if peak_snr else 0

                    # Also get per-channel power for display if band_power available
                    if band_power:
                        channel_power = band_power

                    # Track channel detections
                    if active_ch is not None:
                        self.channel_detections[active_ch] += 1

                    # === DETECT HOPS via tracker ===
                    hop_event = self.tracker.process_scan(
                        datetime.now().isoformat(),
                        active_ch,
                        channel_power,
                        None,  # jam_channel — we're sweep jamming, not single
                    )

                    hop_detected = False
                    hop_from = 0
                    hop_to = 0

                    if hop_event:
                        hop_detected = True
                        hop_from = hop_event['from_channel']
                        hop_to = hop_event['to_channel']
                        self.stats['hops_detected'] += 1

                        # Update live transition matrix
                        self._update_transition_matrix(hop_from, hop_to)

                        # Check prediction accuracy
                        if self.predicted_channel is not None:
                            self.stats['predictions_made'] += 1
                            if self.predicted_channel == hop_to:
                                self.stats['predictions_correct'] += 1
                                marker = "✅"
                            else:
                                marker = "❌"
                            print(f"  HOP #{hop_event['hop_number']}: CH{hop_from} → CH{hop_to} "
                                  f"| Predicted: CH{self.predicted_channel} {marker} "
                                  f"({self.prediction_confidence:.0%})")
                        else:
                            print(f"  HOP #{hop_event['hop_number']}: CH{hop_from} → CH{hop_to} "
                                  f"(dwell: {hop_event['dwell_ms']:.0f}ms)")

                    # === UPDATE PREDICTIONS ===
                    if active_ch is not None:
                        if self.mode == 'predictive':
                            pred, conf = self._get_predicted_next(active_ch)
                            self.predicted_channel = pred
                            self.prediction_confidence = conf
                        elif self.mode == 'ml' and self.predictor:
                            dwell_ms = 0
                            if self.tracker.channel_start_time:
                                dwell_ms = (datetime.now() - self.tracker.channel_start_time).total_seconds() * 1000
                            pred, conf, _ = self.predictor.predict_next_channel(
                                current_channel=active_ch,
                                channel_powers=channel_power,
                                jam_channel=0,
                                dwell_ms=dwell_ms,
                                hops_last_10s=self.tracker._count_recent_hops(datetime.now(), 10),
                                hops_last_30s=self.tracker._count_recent_hops(datetime.now(), 30),
                            )
                            self.predicted_channel = pred
                            self.prediction_confidence = conf
                            if self.predictor and hop_detected:
                                self.predictor.update_actual(hop_to)
                        elif self.mode == 'weighted':
                            self.predicted_channel = None
                            self.prediction_confidence = 0

                    # === UPDATE SWEEP WEIGHTS ===
                    self.position_weights = self._compute_position_weights()

                    # === CHASE MODE: update shared channel state ===
                    if self.mode == 'chase':
                        if active_ch is not None:
                            if active_ch != self.current_jam_channel:
                                self.current_jam_channel = active_ch
                                self.stats['jam_switches'] += 1
                            self.chase_hold_scans = 0
                        else:
                            CHASE_HOLD_SCANS = 5
                            self.chase_hold_scans += 1
                            if self.chase_hold_scans >= CHASE_HOLD_SCANS:
                                self.current_jam_channel = None

                    self.last_active_channel = active_ch if active_ch is not None else self.last_active_channel

                    # === DISPLAY (every scan, since scans are now infrequent) ===
                    self._print_status(active_ch, active_snr, channel_power)

                    # === LOG ===
                    if csv_writer:
                        row = {
                            'timestamp': datetime.now().isoformat(),
                            'scan_num': self.stats['total_scans'],
                            'active_channel': active_ch if active_ch is not None else -1,
                            'active_snr': round(active_snr, 2),
                            'predicted_channel': self.predicted_channel if self.predicted_channel is not None else -1,
                            'pred_confidence': round(self.prediction_confidence, 3),
                            'hop_detected': int(hop_detected),
                            'hop_from': hop_from,
                            'hop_to': hop_to,
                            'position_weights': f"{self.position_weights[0]:.2f},{self.position_weights[1]:.2f},{self.position_weights[2]:.2f}",
                            'mode': self.mode,
                        }
                        for ch in sorted(OCUSYNC_5GHZ_CHANNELS.keys()):
                            row[f'ch{ch}_power'] = round(channel_power.get(ch, -100), 2)
                        csv_writer.writerow(row)

                    # After a hop, do a quick follow-up scan to confirm new channel
                    if hop_detected:
                        time.sleep(0.05)  # 50ms settle after hop
                        peak2, snr2, _ = self._quick_scan_peak()
                        confirm_ch = get_channel_from_freq(peak2) if peak2 and snr2 >= 3.5 else None
                        if confirm_ch and confirm_ch != active_ch:
                            if self.mode == 'chase':
                                self.current_jam_channel = confirm_ch
                            self.last_active_channel = confirm_ch

                else:
                    # === TX RUNS UNINTERRUPTED BETWEEN SCANS ===
                    # This is the key: instead of scanning every cycle,
                    # we just sleep and let the TX thread blast noise.
                    time.sleep(0.01)

        finally:
            self.tx_active = False
            self.scanner.stop()
            if csv_file:
                csv_file.close()
                print(f"[+] Log saved to: {log_path}")
            self._print_final_stats()

    def _print_status(self, active_ch, active_snr, channel_power):
        """Print live status."""
        elapsed = time.time() - self.stats['start_time']
        correct = self.stats['predictions_correct']
        total_pred = max(1, self.stats['predictions_made'])
        hit_rate = correct / total_pred if self.stats['predictions_made'] > 0 else 0

        pred_str = f"CH{self.predicted_channel}({self.prediction_confidence:.0%})" if self.predicted_channel is not None else "---"

        # Build channel power bar
        nf = self.scanner.noise_floor_db
        bars = []
        for ch in sorted(channel_power.keys()):
            snr = channel_power[ch] - nf
            if ch == active_ch and snr >= 4:
                bars.append(f"\033[91mCH{ch}:{snr:+.0f}\033[0m")
            else:
                bars.append(f"CH{ch}:{snr:+.0f}")

        if self.mode == 'blanket':
            d = self.stats['position_dwells']
            total_d = max(sum(d), 1)
            pos_strs = [f'P{i}:{100*d[i]/total_d:.0f}%' for i in range(len(BLANKET_JAM_POSITIONS))]
            print(f"\r  [{elapsed:5.0f}s] {' '.join(bars)} "
                  f"| {' '.join(pos_strs)} "
                  f"| Hops:{self.stats['hops_detected']} "
                  f"| Scans:{self.stats['total_scans']}")
        else:
            w = self.position_weights
            print(f"\r  [{elapsed:5.0f}s] {' '.join(bars)} "
                  f"| Wt:[{w[0]:.0%},{w[1]:.0%},{w[2]:.0%}] "
                  f"| Pred:{pred_str} "
                  f"| Hops:{self.stats['hops_detected']} "
                  f"| Hit:{hit_rate:.0%}({correct}/{total_pred})")

    def _print_final_stats(self):
        """Print final session statistics."""
        elapsed = time.time() - (self.stats['start_time'] or time.time())
        correct = self.stats['predictions_correct']
        total_pred = max(1, self.stats['predictions_made'])
        total_hops = self.stats['hops_detected']

        print(f"\n\n{'=' * 65}")
        print(f"  JAMMING SESSION COMPLETE — {self.mode.upper()} MODE")
        print(f"{'=' * 65}")
        print(f"  Duration:            {elapsed:.1f} seconds")
        print(f"  Total scans:         {self.stats['total_scans']}")
        print(f"  Hops detected:       {total_hops}")
        if self.stats['predictions_made'] > 0:
            print(f"  Predictions made:    {self.stats['predictions_made']}")
            print(f"  Predictions correct: {correct}/{total_pred} ({correct/total_pred:.1%})")

        # Position dwell stats
        total_dwells = sum(self.stats['position_dwells'])
        if total_dwells > 0:
            print(f"\n  TX Position Dwells:")
            if self.mode == 'blanket':
                positions = BLANKET_JAM_POSITIONS
                pos_ch = BLANKET_POSITION_CHANNELS
            else:
                positions = SWEEP_JAM_POSITIONS
                pos_ch = POSITION_CHANNELS
            for i, freq in enumerate(positions):
                d = self.stats['position_dwells'][i]
                pct = 100 * d / total_dwells
                chs = pos_ch[i]
                ch_str = ','.join(f'CH{c}' for c in chs)
                print(f"    Pos {i} ({freq/1e6:.0f} MHz → {ch_str}): "
                      f"{d} dwells ({pct:.1f}%)")

        # Channel detection stats
        if self.channel_detections:
            print(f"\n  Drone Channel Detections:")
            total_det = sum(self.channel_detections.values())
            for ch in sorted(self.channel_detections.keys()):
                count = self.channel_detections[ch]
                pct = 100 * count / total_det
                bar = "█" * int(pct / 2)
                print(f"    CH{ch}: {count:4d} ({pct:5.1f}%) {bar}")

        # Transition matrix
        if self.transitions:
            print(f"\n  Live Transition Matrix:")
            for from_ch in sorted(self.transitions.keys()):
                for to_ch in sorted(self.transitions[from_ch].keys()):
                    count = self.transitions[from_ch][to_ch]
                    total_from = sum(self.transitions[from_ch].values())
                    pct = 100 * count / total_from if total_from else 0
                    print(f"    CH{from_ch} → CH{to_ch}: {count} ({pct:.0f}%)")

        # Final weights
        if self.mode != 'blanket':
            print(f"\n  Final Position Weights: {[f'{w:.1%}' for w in self.position_weights]}")
        print(f"{'=' * 65}")

        # Tracker summary
        print(self.tracker.get_summary())


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ML-Powered Reactive Jammer for DJI OcuSync 2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  blanket     2-position ping-pong covering ALL channels with ~67% duty (RECOMMENDED)
  weighted    Sweep-jam all channels, dwell proportional to drone's preferences
  predictive  Use live transition matrix to predict + preemptively jam next channel
  ml          Use trained ML model for prediction (requires --model)
  chase       Simple reactive: jam current channel only

Examples:
  # Blanket jam — covers all 6 channels, nowhere to hide
  python3 reactive_jammer.py --mode blanket --duration 60

  # Weighted sweep (learns in real-time, no model needed)
  python3 reactive_jammer.py --mode weighted --duration 60

  # Predictive + logging
  python3 reactive_jammer.py --mode predictive --duration 120 --log hop_data/session.csv

  # ML-powered
  python3 reactive_jammer.py --mode ml --model models/hop_predictor.pkl

  # Chase baseline
  python3 reactive_jammer.py --mode chase --duration 60
        """
    )

    parser.add_argument('--mode', choices=['follow', 'blind', 'blanket', 'weighted', 'predictive', 'ml', 'chase'],
                        default='follow',
                        help='Jamming mode (default: follow). blind = pure TX, no RX gaps')
    parser.add_argument('--scan-interval', type=float, default=3.0,
                        help='Seconds between RX scans in follow mode (default: 3.0)')
    parser.add_argument('--model', type=str, default=None,
                        help='Path to trained ML model (.pkl) — for ml mode')
    parser.add_argument('--duration', type=float, default=None,
                        help='Session duration in seconds (default: infinite)')
    parser.add_argument('--tx-gain', type=float, default=DEFAULT_TX_GAIN,
                        help=f'TX gain in dB (default: {DEFAULT_TX_GAIN})')
    parser.add_argument('--rx-gain', type=float, default=DEFAULT_RX_GAIN,
                        help=f'RX gain in dB (default: {DEFAULT_RX_GAIN})')
    parser.add_argument('--log', type=str, default=None,
                        help='Path for session log CSV')

    args = parser.parse_args()

    if args.mode == 'ml' and not args.model:
        print("[!] ML mode requires --model. Use --mode weighted or predictive instead.")
        sys.exit(1)

    if args.model and not os.path.exists(args.model):
        print(f"[!] Model not found: {args.model}")
        sys.exit(1)

    jammer = ReactiveJammer(
        mode=args.mode,
        model_path=args.model,
        tx_gain=args.tx_gain,
        rx_gain=args.rx_gain,
        scan_interval=getattr(args, 'scan_interval', 3.0),
    )

    jammer.init_hardware()
    jammer.run(duration=args.duration, log_path=args.log)


if __name__ == '__main__':
    main()
