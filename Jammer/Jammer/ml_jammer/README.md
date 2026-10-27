# ML-Enhanced Adaptive Jammer for DJI OcuSync 2.0

## Overview
This system defeats DJI's Adaptive Frequency Hopping (AFH) by monitoring the RF spectrum,
tracking channel hops in real-time, and using Machine Learning to predict which channel the
drone will escape to — jamming it **before** it arrives.

⚠️ **LEGAL WARNING**: RF jamming is ILLEGAL in most countries without proper authorization.
This is for educational/research purposes in controlled lab environments only.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Spectrum        │     │  Channel         │     │  ML             │
│  Scanner         │────>│  Tracker         │────>│  Predictor      │
│  (USRP RX)      │     │  (Hop Logger)    │     │  (Random Forest)│
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                                                          v
                                                 ┌─────────────────┐
                                                 │  Reactive        │
                                                 │  Jammer          │
                                                 │  (USRP TX)       │
                                                 └─────────────────┘
```

## OcuSync 2.0 Channel Map (5.8 GHz Band)

Based on reverse engineering research (proto17/dji_droneid, Bender 2022, Andonov 2025):

| Channel | Center Freq (GHz) | Range (GHz)         |
|---------|-------------------|---------------------|
| CH1     | 5.7565            | 5.7515 - 5.7615     |
| CH2     | 5.7665            | 5.7615 - 5.7715     |
| CH3     | 5.7765            | 5.7715 - 5.7815     |
| CH4     | 5.7865            | 5.7815 - 5.7915     |
| CH5     | 5.7965            | 5.7915 - 5.8015     |
| CH6     | 5.8065            | 5.8015 - 5.8115     |
| CH7     | 5.8165            | 5.8115 - 5.8215     |
| CH8     | 5.8265            | 5.8215 - 5.8315     |
| CH9     | 5.8365            | 5.8315 - 5.8415     |
| CH10    | 5.8465            | 5.8415 - 5.8515     |

Channels are ~10 MHz wide. The drone scans all channels and hops to the one
with the lowest interference (RSSI). Hops occur every ~600ms.

## Files

| File | Purpose |
|------|---------|
| `channel_config.py` | OcuSync channel definitions and constants |
| `spectrum_scanner.py` | USRP wideband RX — monitors all channels simultaneously |
| `channel_tracker.py` | Detects active channel, logs hop events to CSV |
| `ml_predictor.py` | Trains Random Forest / LSTM on hop data, predicts next channel |
| `reactive_jammer.py` | Full pipeline: scan → track → predict → jam |
| `collect_hops.sh` | Data collection script for training the ML model |

## Usage

### Phase 1: Collect Hopping Data (No Jamming)
```bash
# Monitor drone channel hops and save to CSV
python3 spectrum_scanner.py --duration 120 --output hop_data/session1.csv
```

### Phase 2: Collect Hops Under Jamming Pressure
```bash
# Jam a single channel to force hops, while recording where it goes
python3 spectrum_scanner.py --duration 120 --jam-channel 5 --output hop_data/session2_jammed.csv
```

### Phase 3: Train the ML Model
```bash
python3 ml_predictor.py train --data-dir hop_data/
```

### Phase 4: Run Reactive Jammer
```bash
python3 reactive_jammer.py --model models/hop_predictor.pkl --gain 50
```

## Hardware Requirements
- USRP B210 (serial=321E87F)
- 5.8GHz capable antennas (RX on RX2, TX on TX/RX)
- DJI drone operating on 5.8GHz OcuSync

## Dependencies
- UHD (USRP Hardware Driver)
- NumPy, SciPy, pandas
- scikit-learn (Random Forest)
- matplotlib (visualization)
