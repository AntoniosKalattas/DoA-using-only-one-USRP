#!/bin/bash
set -e

# Configuration
DURATION=60
WINDOW=10
OUTPUT_DIR="./iq_datasets"

echo "============================================================"
echo "Drone RF State - Data Collection & Training Helper"
echo "============================================================"
echo "This script will help you collect data for each state."
echo "You need to manually set the drone state when prompted."
echo "============================================================"

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR"

# 1. IDLE State
echo ""
echo ">>> STEP 1: IDLE STATE"
echo "Please set the drone to IDLE mode (motors on, on ground)."
read -p "Press [Enter] when the drone is ready in IDLE state..."
echo "Capturing IDLE data for ${DURATION}s..."
python3 iq_size_capture.py capture --state idle --duration $DURATION --window $WINDOW --output "$OUTPUT_DIR"

# 2. HOVER State
echo ""
echo ">>> STEP 2: HOVER STATE"
echo "Please set the drone to HOVER mode (take off, hold position)."
read -p "Press [Enter] when the drone is HOVERING..."
echo "Capturing HOVER data for ${DURATION}s..."
python3 iq_size_capture.py capture --state hover --duration $DURATION --window $WINDOW --output "$OUTPUT_DIR"

# 3. FLY FAST State
echo ""
echo ">>> STEP 3: FLY FAST STATE"
echo "Please set the drone to FLY FAST mode (moving aggressively)."
read -p "Press [Enter] when the drone is FLYING FAST..."
echo "Capturing FLY FAST data for ${DURATION}s..."
python3 iq_size_capture.py capture --state fly_fast --duration $DURATION --window $WINDOW --output "$OUTPUT_DIR"

# 4. Train Model
echo ""
echo ">>> STEP 4: TRAINING MODEL"
echo "Training Random Forest classifier on collected data..."
python3 iq_size_capture.py train --data "$OUTPUT_DIR"

# 5. Visualize
echo ""
echo ">>> STEP 5: VISUALIZATION"
echo "Generating plots..."
python3 iq_size_capture.py visualize --data "$OUTPUT_DIR"

echo ""
echo "============================================================"
echo "DONE! Results are in $OUTPUT_DIR/models and $OUTPUT_DIR/figures"
echo "============================================================"
