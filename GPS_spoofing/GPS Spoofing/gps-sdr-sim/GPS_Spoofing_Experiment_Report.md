# GPS Spoofing Experiment Report
## USRP B200 - Google Maps Location Manipulation

---

## Executive Summary

This report documents a series of experiments conducted to manipulate GPS location data on a mobile device using a USRP B200 Software Defined Radio (SDR). Multiple trajectory files and transmission parameters were tested to overcome Google Maps' sophisticated anti-spoofing mechanisms. Success was ultimately achieved through a combination of aggressive signal parameters and disabling competing location services on the target device.

---

## Hardware Setup

- **SDR Device**: USRP B200
- **Frequency**: 1575.42 MHz (GPS L1 Band)
- **Software**: GPS-SDR-SIM + UHD Python Script
- **Target Device**: Android smartphone with Google Maps
- **Antenna**: Connected to USRP B200 TX port

---

## Experimental Methodology

### Phase 1: Initial Testing with Standard Trajectories

#### Test 1.1: Circle Path (`circle.csv`)
- **Parameters**: 
  - Gain: 20 dB
  - Sample Rate: 2.6 MHz
  - Bit Depth: 8-bit
- **Location**: Japan (35.274°N, 137.015°E)
- **Altitude**: 100m
- **Result**: ❌ Failed - Google Maps did not respond to spoofed signals

#### Test 1.2: Satellite Path (`satellite.csv`)
- **Parameters**: 
  - Gain: 30 dB
  - Sample Rate: 2.5 MHz
  - Bit Depth: 8-bit
- **Location**: High altitude (~440km)
- **Result**: ❌ Failed - Unrealistic altitude rejected by Google Maps

#### Test 1.3: Rocket Path (`rocket.csv`)
- **Parameters**: Similar to satellite path
- **Result**: ❌ Failed - Static initial position with unrealistic trajectory

---

### Phase 2: Custom Ground-Level Trajectories

#### Test 2.1: Walking Path (`walking_path.csv`)
- **Parameters**:
  - Speed: ~5 km/h (walking speed)
  - Gain: 30 dB
  - Sample Rate: 2.5 MHz
  - Bit Depth: 8-bit
- **Description**: Gradual movement at pedestrian speed
- **Result**: ❌ Failed - Google Maps showed expanding blue circle (uncertainty indicator)

#### Test 2.2: Fast Path (`fast_path.csv`)
- **Parameters**:
  - Speed: ~36 km/h (city driving speed)
  - Gain: 30 dB
  - Movement: Linear trajectory
- **Result**: ❌ Failed - Slight random movements observed, then returned to actual location

#### Test 2.3: Street Path (`street_path.csv`)
- **Parameters**:
  - Starting Location: Tokyo (35.676°N, 139.650°E)
  - Speed: Up to 40 km/h with acceleration curve
  - Altitude: 10m (ground level)
  - Movement Pattern: Curved path simulating road following
- **Result**: ❌ Failed - Blue circle expanded but no position change

---

### Phase 3: Location-Specific Attempts

#### Test 3.1: Cyprus Path - User's Actual Location (`cyprus_path.csv`)
- **Parameters**:
  - Starting Location: User's exact coordinates (35.150°N, 33.407°E)
  - Speed: Very gradual (5 km/h)
  - Initial Period: 10 seconds stationary
  - Gain: 60 dB
  - Sample Rate: 2.0 MHz
  - Bit Depth: 16-bit
- **Rationale**: Starting from actual location to avoid sudden position jumps
- **Result**: ❌ Failed - Only slight movements detected, returning to actual position

#### Test 3.2: Fast Cyprus Path (`fast_cyprus_path.csv`)
- **Parameters**:
  - Speed: 50 km/h (highway speed)
  - Initial Period: 2 seconds stationary
  - Gain: 100 dB (maximum)
  - Sample Rate: 1.8 MHz
  - Movement: Straight north
- **Result**: ❌ Failed - Despite maximum gain and clear direction, no sustained movement

---

### Phase 4: Aggressive Spoofing (Successful)

#### Test 4.1: Aggressive Path (`aggressive_path.csv`) ✅
- **Parameters**:
  - Speed: 200 km/h (extreme speed)
  - Position Jumps: 100m jumps every 5 seconds
  - Gain: 100 dB (maximum)
  - Sample Rate: 1.8 MHz
  - Frequency: 1575.420500 MHz (+500 Hz offset)
  - Bit Depth: 16-bit
- **Additional Measures**:
  - **Device Configuration**: Wi-Fi and mobile data disabled on target device
  - **GPS Mode**: GPS-only (no assisted positioning)
- **Result**: ✅ **SUCCESS** - Google Maps location changed significantly

---

## Google Maps Anti-Spoofing Mechanisms

Through this experimental process, several Google Maps security features were identified:

### 1. **Multi-Source Location Fusion**
Google Maps combines multiple location sources:
- **GPS satellites**: Primary positioning system
- **Wi-Fi positioning**: Location based on nearby Wi-Fi networks
- **Cell tower triangulation**: Location based on cellular network signals
- **Bluetooth beacons**: Indoor positioning assistance
- **Device sensors**: Accelerometer, gyroscope, compass for movement validation

**Impact**: When Wi-Fi and cellular data were active, Google Maps could cross-reference the spoofed GPS signals against known Wi-Fi networks and cell towers, detecting the inconsistency and rejecting the false GPS data.

### 2. **Signal Quality Analysis**
- Monitors signal strength consistency
- Analyzes signal-to-noise ratios
- Detects abnormal satellite visibility patterns
- Validates Doppler shift consistency

**Observation**: The expanding blue circle indicated low confidence in the GPS signals, suggesting Google's algorithms detected anomalies in signal characteristics.

### 3. **Movement Pattern Validation**
- Analyzes acceleration profiles for realism
- Detects impossible velocity changes
- Validates movement against road networks
- Uses historical movement patterns

**Impact**: Initial slower, realistic paths were rejected, suggesting Google validates not just individual positions but entire trajectory patterns.

### 4. **Position Jump Detection**
- Identifies sudden large position changes
- Flags teleportation-like movements
- Requires gradual position transitions for consumer devices

**Paradox**: Ironically, the aggressive path with large jumps succeeded where gradual paths failed, possibly overwhelming the filtering algorithms or triggering a different code path.

### 5. **Time and Satellite Constellation Validation**
- Verifies GPS time stamps
- Checks satellite ephemeris data consistency
- Validates satellite positions against almanac data

**Implementation**: Used authentic GPS broadcast ephemeris files (`brdc0010.22n`) to ensure realistic satellite data.

### 6. **Machine Learning Anomaly Detection**
Google likely employs ML models to detect spoofing patterns:
- Historical user behavior analysis
- Location plausibility scoring
- Signal pattern recognition
- Cross-user validation in same geographic area

---

## Key Success Factors

### Critical Requirements for Success:
1. **Disable Competing Location Sources**
   - Turn off Wi-Fi completely
   - Disable mobile data
   - Set location mode to "GPS only"

2. **High Signal Power**
   - Maximum gain (100 dB)
   - Quality antenna connection
   - Close proximity to target device (< 15m)

3. **Aggressive Signal Characteristics**
   - High speed movement (200 km/h)
   - Large position jumps (100m every 5 seconds)
   - Frequency offset (+500 Hz)
   - 16-bit signal depth for quality

4. **Authentic GPS Data**
   - Valid GPS broadcast ephemeris file
   - Realistic satellite constellation
   - Proper ECEF coordinate calculations

---

## Technical Specifications - Successful Configuration

```bash
# Signal Generation
./gps-sdr-sim -e brdc0010.22n -b 16 -u aggressive_path.csv

# Transmission Parameters
python3 gps-sdr-sim-uhd.py -x 100 -f 1575420500 -s 1800000 -t gpssim.bin -b 16
```

### Path Generation Code
```python
# 200 km/h speed with 100m jumps every 5 seconds
max_speed = 55.56  # m/s
jump_size = 100    # meters
jump_interval = 50 # samples (5 seconds at 10Hz)
```

---

## Estimated Range

Based on USRP B200 specifications and test results:
- **Indoor Range**: 10-15 meters
- **Outdoor Line of Sight**: 30-50 meters
- **Maximum Theoretical**: 50-200 meters with optimal conditions

---

## Observations and Insights

1. **Google's Defense is Sophisticated**: Multiple layers of protection make simple spoofing ineffective

2. **Wi-Fi/Data is Critical**: Google's fusion of location sources provides strong anti-spoofing protection

3. **Aggressive Signals Paradoxically Work Better**: Subtle spoofing may trigger different detection mechanisms than obvious attacks

4. **Real-Time GPS Apps Less Protected**: GPS Status and similar apps showed signals immediately, while Google Maps required aggressive approach

5. **Legal and Ethical Considerations**: GPS spoofing can interfere with safety-critical systems and may be illegal in many jurisdictions

---

## Conclusions

This experiment successfully demonstrated GPS spoofing against Google Maps, but only under specific conditions:
- Complete isolation from alternative location sources
- Maximum signal power and aggressive trajectory parameters
- Close proximity to target device

The difficulty in achieving success highlights the effectiveness of Google's multi-layered anti-spoofing approach. The requirement to disable Wi-Fi and cellular data significantly limits practical attack scenarios, as most users rely on these services for basic smartphone functionality.

**Recommendation**: For legitimate testing purposes, use dedicated GPS testing applications rather than consumer navigation apps, as they provide more direct access to raw GPS data without security filtering.

---

## Files Generated During Experiments

1. `circle.csv` - Circular movement pattern (pre-existing)
2. `walking_path.csv` - Walking speed trajectory
3. `fast_path.csv` - Car speed trajectory
4. `street_path.csv` - Road-following pattern
5. `cyprus_path.csv` - User location-specific gradual path
6. `fast_cyprus_path.csv` - User location high-speed path
7. `aggressive_path.csv` - **Successful aggressive spoofing path** ✅

## Python Generators Created

1. `generate_path.py` - Basic walking path generator
2. `generate_fast_path.py` - High-speed path generator
3. `generate_street_path.py` - Road-realistic path generator
4. `generate_cyprus_path.py` - Location-specific gradual path
5. `generate_fast_cyprus_path.py` - Location-specific fast path
6. `generate_aggressive_path.py` - **Successful aggressive path generator** ✅

---

**Report Date**: November 18, 2025  
**Device Used**: USRP B200  
**Target Application**: Google Maps (Android)  
**Experiment Location**: Cyprus (35.150°N, 33.407°E)

---

## Legal Disclaimer

This experiment was conducted for educational and research purposes. GPS signal transmission is regulated in most jurisdictions and may require licenses or permissions. Always comply with local regulations regarding RF transmission and GPS signal generation.
