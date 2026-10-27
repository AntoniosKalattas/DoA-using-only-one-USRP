# DJI Mini Traffic Analysis System

Network traffic analysis on DJI drones using machine learning for behavioral classification.

## Overview

This system captures RF signals from your DJI Mini drone, extracts features, and uses ML to classify drone states:
- **idle** - Powered on, motors off
- **hover** - Hovering in place
- **fly_slow/fast** - Different flight speeds
- **under_jamming** - During RF jamming attack
- **under_gps_spoof** - During GPS spoofing attack

## Hardware Requirements

- **USRP B210** - Software Defined Radio for RF capture
- **DJI Mini** - Target drone (OcuSync 2.0, 2.4/5.8 GHz)
- Antennas for 2.4 GHz and 5.8 GHz bands

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Collect Data (Simple Method - Recommended!)
Record one state at a time - much easier than labeling while flying:
```bash
cd capture

# Record 3 minutes of hovering
python simple_recorder.py --state hover --duration 180

# Record 3 minutes of fast flying
python simple_recorder.py --state fly_fast --duration 180

# Record 2 minutes during jamming attack
python simple_recorder.py --state under_jamming --duration 120

# See all available states
python simple_recorder.py --list-states
```

### 3. Extract Features
```bash
cd features
python extractor.py -i ../dataset/data/my_first_session -o ../dataset/data/my_first_session/features.csv
```

### 4. Build Dataset
```bash
cd dataset
python builder.py --sessions data/session1 data/session2 --output data/processed
```

### 5. Train ML Model
```bash
cd ml
python train.py --data ../dataset/data/processed --model xgboost
```

### 6. Real-time Prediction
```bash
cd ml
python predict.py --model ./models --input ../capture/live_capture.csv
```

## Project Structure

```
DroneTrafficAnalysis/
├── capture/
│   ├── rf_capture.py          # USRP B210 RF capture
│   └── session_recorder.py    # Interactive session recording
├── features/
│   └── extractor.py           # Feature engineering
├── dataset/
│   ├── builder.py             # Dataset construction
│   └── data/                  # Captured data
├── ml/
│   ├── train.py               # Model training
│   ├── predict.py             # Real-time inference
│   └── models/                # Saved models
├── requirements.txt
└── README.md
```

## Features Extracted

| Feature | Description |
|---------|-------------|
| mean_power_db | Average signal power |
| std_power_db | Power variation |
| max_power_db | Peak signal strength |
| spectral_entropy | Signal complexity |
| spectral_flatness | Noise-like vs tonal |
| bandwidth_3db_hz | Signal bandwidth |
| power_variance_rate | Stability indicator |

Plus rolling window statistics (10, 50, 100 samples) for temporal patterns.

## Integrating Attack Scenarios

Use your existing tools during data collection:

### RF Jamming
```bash
# In one terminal: run jammer
cd /root/projects/Jammer
python dji_jammer_5_8ghz.py

# In another terminal: capture with state "under_jamming"
```

### GPS Spoofing
```bash
# Run GPS spoofer
cd /root/projects/GPS\ Spoofing/gps-sdr-sim
python gps-sdr-sim-uhd.py -t gpssim.bin

# Capture with state "under_gps_spoof"
```

## ML Models Available

- **Random Forest** - Good baseline, interpretable
- **XGBoost** - Best overall performance
- **Gradient Boosting** - Alternative to XGBoost
- **MLP** - Neural network option

## Thesis Notes

This system is designed for: **"Network Traffic Analysis on Remotely Operated Drones Using Machine Learning"**

Key contributions:
1. RF-based behavioral classification without decrypting encrypted protocols
2. Attack detection using ML (jamming, GPS spoofing)
3. Real-time classification system
