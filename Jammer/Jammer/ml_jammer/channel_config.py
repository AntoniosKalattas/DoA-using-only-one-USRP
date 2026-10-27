#!/usr/bin/env python3
"""
OcuSync 2.0 Channel Configuration
===================================
IMPORTANT: There are TWO separate signal types on OcuSync:

1. DroneID Remote ID beacon — 10 MHz OFDM, sent every ~600ms
   Frequencies observed by proto17: 5.7565, 5.7765, 5.7965 GHz
   These are BROADCAST beacons for regulatory compliance.

2. Data Link (video/control) — 20 MHz wide channels using AFH
   These are the actual communication channels between drone and controller.
   The controller UI shows the exact data link frequency.
   VERIFIED from controller: drone was at 5731.5-5751.5 MHz (CH1, center 5741.5 MHz)
   This is the signal we need to JAM to force hops.

Previous channel map was WRONG — it used DroneID beacon frequencies (10 MHz)
instead of the actual data link channels (20 MHz). This caused the jammer to
miss the drone by 55+ MHz.

OcuSync AFH mechanism:
1. Drone periodically scans RSSI across all available channels
2. Ranks channels by interference level (lowest noise = best)
3. Hops to cleanest channel; re-scans if current channel degrades
4. Can use up to 3 simultaneous channels (OcuSync 2.0+)
5. Can switch between 2.4 GHz and 5.8 GHz bands entirely

References:
- proto17/dji_droneid (DroneID beacon analysis)
- DJI Mavic 2 controller spectrum display (verified data link channels)
- ISM 5.8 GHz band: 5725 - 5850 MHz (125 MHz total)
"""

import numpy as np

# =============================================================================
# 5.8 GHz Band — OcuSync DATA LINK Channel Map
# =============================================================================
# Each channel is 20 MHz wide (verified from controller display: 5731.5-5751.5 MHz)
# ISM band: 5725 - 5850 MHz = 125 MHz total
# 6 non-overlapping 20 MHz channels fit the band:
#   5725-5745, 5745-5765, 5765-5785, 5785-5805, 5805-5825, 5825-5845
#
# Controller showed drone at 5731.5-5751.5 MHz — the slight offset from the
# clean 5725-5745 boundary suggests DJI's channels may be offset by ~6.5 MHz
# from the band edge, centering at: 5741.5, 5761.5, 5781.5, 5801.5, 5821.5, 5841.5
#
# NOTE: Channel numbering is based on physical position in the band (1=lowest)

OCUSYNC_5GHZ_CHANNELS = {
    0:  5735.5e6,   # 5725.5 - 5745.5 MHz — OBSERVED: drone escaped here during blanket jam
    1:  5741.5e6,   # 5731.5 - 5751.5 MHz — VERIFIED from controller display
    2:  5767.5e6,   # 5757.5 - 5777.5 MHz — VERIFIED from controller display 2026-03-06
    3:  5781.5e6,   # 5771.5 - 5791.5 MHz
    4:  5801.5e6,   # 5791.5 - 5811.5 MHz
    5:  5821.5e6,   # 5811.5 - 5831.5 MHz
    6:  5841.5e6,   # 5831.5 - 5851.5 MHz
}

# DroneID Beacon frequencies (for reference only — NOT the data link)
DRONEID_BEACON_FREQS = [
    5756.5e6,   # proto17 confirmed
    5776.5e6,   # proto17 confirmed
    5796.5e6,   # proto17 confirmed
]

# 2.4 GHz Band — OcuSync Channel Map (for reference / dual-band tracking)
OCUSYNC_2GHZ_CHANNELS = {
    11: 2399.5e6,   # 2.3995 GHz — confirmed by proto17
    12: 2414.5e6,   # 2.4145 GHz — confirmed by proto17
    13: 2429.5e6,   # 2.4295 GHz — confirmed by proto17
    14: 2444.5e6,   # 2.4445 GHz — confirmed by proto17
    15: 2459.5e6,   # 2.4595 GHz — confirmed by proto17
}

# =============================================================================
# Scanning / Detection Parameters
# =============================================================================

CHANNEL_BANDWIDTH = 20e6       # 20 MHz per OcuSync data link channel (verified)
GUARD_BANDWIDTH = 20e6         # Same as channel BW for data link

# USRP B210 can do up to 56 MHz instantaneous bandwidth
# Our 6 channels span 5731.5 - 5851.5 MHz = 120 MHz
# With 56 MHz BW we can cover ~3 channels per center position
# To cover all 6 channels, center at 5791.5 MHz:
#   Coverage: 5763.5 - 5819.5 MHz → CH2, CH3, CH4, CH5 fully
#   CH1 (5741.5) and CH6 (5841.5) are at edges / outside
#
# BETTER: Center at 5791.5 MHz covers CH1-CH6 band edges:
#   5791.5 ± 28 = 5763.5 to 5819.5 — misses CH1 and CH6
#
# BEST: Center at 5787 MHz with 56 MHz:
#   5759 - 5815 → covers CH2 center to CH5 center
#   OR center at 5791.5 to balance

# Single wideband setting — covers the middle of the band
# CH1 and CH6 are at the extremes and may need separate sweeps
WIDEBAND_CENTER_FREQ = 5791.5e6  # Centered between CH1 and CH6
WIDEBAND_SAMP_RATE = 56e6        # Maximum B210 BW

# Wideband single-shot: 56 MHz centered at 5791.5 GHz
# Covers: 5763.5 - 5819.5 MHz → CH2(5761.5), CH3(5781.5), CH4(5801.5), CH5(5821.5 edge)
# CH1(5741.5) is 22 MHz below band edge — NEED to shift down to catch it
# ACTUALLY: center at 5781.5 to catch CH1 better:
#   5753.5 - 5809.5 → CH1 partially, CH2 fully, CH3 fully, CH4 fully
#
# COMPROMISE: center at 5785 MHz to catch CH1-CH5:
#   5757 - 5813 → CH1(5741.5) at -15.5 from edge (partially covered)
#                  CH2-CH4 fully covered, CH5(5821.5) at edge
#
# To cover ALL 6 channels with one shot, we need the full 120 MHz range
# which is impossible with 56 MHz BW. Best compromise:
# Center at 5791.5 covers CH2-CH5, then note CH1 and CH6 are edge cases.

SINGLE_CENTER_FREQ = 5791.5e6   # Best compromise for CH1-CH6 coverage
SINGLE_SAMP_RATE = 56e6

# =============================================================================
# Sweep-Jam TX Configuration
# =============================================================================
# 3 TX positions to cover the full 5725-5850 MHz band with 56 MHz BW each.
# Each position covers ~56 MHz. Hopping between them at ~5ms dwell gives
# the drone constant interference across ALL channels (~33% duty cycle each).
#
# Position 1: 5745 MHz → covers 5717-5773 MHz → CH1(100%), CH2(100%), CH3(8%)
# Position 2: 5788 MHz → covers 5760-5816 MHz → CH2(57%), CH3(100%), CH4(100%), CH5(22%)
# Position 3: 5831 MHz → covers 5803-5859 MHz → CH4(42%), CH5(100%), CH6(100%)
#
# Result: every channel gets 100% coverage from at least one position.

SWEEP_JAM_POSITIONS = [
    5745.0e6,   # Low band:  covers CH1 + CH2 fully
    5788.0e6,   # Mid band:  covers CH3 + CH4 fully
    5831.0e6,   # High band: covers CH5 + CH6 fully
]

SWEEP_JAM_TX_BW = 56e6         # Maximum B210 TX bandwidth per position
SWEEP_JAM_TX_RATE = 56e6       # TX sample rate = BW for wideband noise
SWEEP_DWELL_MS = 2.0           # Dwell time per position (ms) — fast cycle
SWEEP_RETUNE_MS = 1.0          # PLL retune settling time (ms) — aggressive

# =============================================================================
# BLANKET JAM — 3-Position Full Coverage
# =============================================================================
# With CH0 at 5735.5 MHz, the band spans 5725.5-5851.5 = 126 MHz.
# 2 × 56 MHz = 112 MHz — leaves a 14 MHz gap. NOT enough.
# 3 × 56 MHz = 168 MHz — covers everything with generous overlap.
#
# Drone escaped to CH0 (5725.5-5745.5) during 2-position test because
# old Pos A (5759) only reached down to 5731 MHz.
#
# Position A: 5745 MHz → covers 5717–5773 → CH0(5735.5)✅ CH1(5741.5)✅ CH2(5761.5)✅
# Position B: 5791 MHz → covers 5763–5819 → CH2(overlap)  CH3(5781.5)✅ CH4(5801.5)✅
# Position C: 5835 MHz → covers 5807–5863 → CH4(overlap)  CH5(5821.5)✅ CH6(5841.5)✅
#
# 3-hop cycle: 2ms dwell + 1ms retune × 3 = 9ms total
# Each position gets ~22% duty. Every channel fully covered.

BLANKET_JAM_POSITIONS = [
    5745.0e6,   # Low band:   covers 5717-5773 → CH0 + CH1 + CH2
    5791.0e6,   # Mid band:   covers 5763-5819 → CH3 + CH4 (overlaps CH2, CH5 edge)
    5835.0e6,   # High band:  covers 5807-5863 → CH5 + CH6 (overlaps CH4)
]

BLANKET_DWELL_MS = 2.0         # Dwell time per position (ms)
BLANKET_RETUNE_MS = 1.0        # PLL retune settling (ms)

# =============================================================================
# Detection Thresholds
# =============================================================================

# Power threshold above noise floor to consider a channel "active"
# With 20 MHz channels and the data link (not beacon), SNR is typically 15-25 dB
# Set threshold at 4 dB to catch weak signals at band edges
DETECTION_THRESHOLD_DB = 4.0

# Minimum dwell time to confirm a hop (filters glitches)
MIN_DWELL_MS = 50.0

# OcuSync data link is continuous (not bursty like DroneID beacon)
# DroneID beacon appears every ~600ms, but data link is always-on
BURST_INTERVAL_MS = 600.0  # DroneID beacon interval (reference only)

# FFT parameters for channel power measurement
FFT_SIZE = 4096
AVERAGING_WINDOWS = 16

# =============================================================================
# Hop Timing Constants
# =============================================================================

# Estimated time for drone to detect interference and initiate hop
HOP_REACTION_TIME_MS = 200.0   # ~200ms from jam start to hop decision

# Estimated time to complete frequency switch
HOP_SWITCH_TIME_MS = 50.0      # ~50ms for PLL to lock on new freq

# Total window: we need to predict and switch within this
PREDICTION_WINDOW_MS = HOP_REACTION_TIME_MS + HOP_SWITCH_TIME_MS  # ~250ms

# =============================================================================
# USRP Hardware Config
# =============================================================================

USRP_SERIAL = "serial=321E87F"
RX_ANTENNA = "RX2"
TX_ANTENNA = "TX/RX"
DEFAULT_RX_GAIN = 60
DEFAULT_TX_GAIN = 89.75

# RX sweep positions — to cover all channels (CH0-CH6) we need 2 RX positions
# since 56 MHz BW can't span the full 5725-5850 MHz range
# Shifted Pos 1 down to catch CH0 at 5735.5 MHz
RX_SWEEP_POSITIONS = [
    5760.0e6,   # Covers CH0(5735.5) + CH1(5741.5) + CH2(5767.5) + CH3(5781.5) — low band
    5820.0e6,   # Covers CH4(5801.5) + CH5(5821.5) + CH6(5841.5) — high band
]


def get_channel_from_freq(freq_hz):
    """Given a frequency, return the closest OcuSync channel number."""
    all_channels = {**OCUSYNC_5GHZ_CHANNELS, **OCUSYNC_2GHZ_CHANNELS}
    closest_ch = min(all_channels.keys(), key=lambda ch: abs(all_channels[ch] - freq_hz))
    if abs(all_channels[closest_ch] - freq_hz) < CHANNEL_BANDWIDTH:
        return closest_ch
    return None


def get_freq_from_channel(ch_num):
    """Given a channel number, return center frequency in Hz."""
    all_channels = {**OCUSYNC_5GHZ_CHANNELS, **OCUSYNC_2GHZ_CHANNELS}
    return all_channels.get(ch_num, None)


def get_channel_edges(ch_num):
    """Return (low_freq, high_freq) for a given channel."""
    center = get_freq_from_channel(ch_num)
    if center is None:
        return None
    return (center - CHANNEL_BANDWIDTH / 2, center + CHANNEL_BANDWIDTH / 2)


def freq_to_fft_bins(center_freq, samp_rate, fft_size, target_freq, bandwidth):
    """
    Convert a target frequency + bandwidth to FFT bin indices.
    Returns (start_bin, end_bin) within the FFT output.
    """
    freq_resolution = samp_rate / fft_size
    # FFT bins go from (center - samp_rate/2) to (center + samp_rate/2)
    freq_start = center_freq - samp_rate / 2
    
    target_low = target_freq - bandwidth / 2
    target_high = target_freq + bandwidth / 2
    
    bin_low = int((target_low - freq_start) / freq_resolution)
    bin_high = int((target_high - freq_start) / freq_resolution)
    
    bin_low = max(0, min(fft_size - 1, bin_low))
    bin_high = max(0, min(fft_size - 1, bin_high))
    
    return (bin_low, bin_high)


def print_channel_map():
    """Print the channel map for reference."""
    print("=" * 60)
    print("OcuSync 2.0 — 5.8 GHz Channel Map")
    print("=" * 60)
    for ch, freq in sorted(OCUSYNC_5GHZ_CHANNELS.items()):
        low, high = get_channel_edges(ch)
        print(f"  CH{ch:2d}  |  {freq/1e9:.4f} GHz  |  {low/1e9:.4f} - {high/1e9:.4f} GHz")
    print("=" * 60)
    print("OcuSync 2.0 — 2.4 GHz Channel Map")
    print("=" * 60)
    for ch, freq in sorted(OCUSYNC_2GHZ_CHANNELS.items()):
        low, high = get_channel_edges(ch)
        print(f"  CH{ch:2d}  |  {freq/1e9:.4f} GHz  |  {low/1e9:.4f} - {high/1e9:.4f} GHz")
    print("=" * 60)


if __name__ == "__main__":
    print_channel_map()
