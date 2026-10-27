#!/bin/bash
# =============================================================================
# OcuSync Hop Data Collection Script
# =============================================================================
# Collects channel hopping training data in multiple phases:
#   Phase 1: Baseline — no jamming, observe natural hops
#   Phase 2: Single-channel jam — force hops by jamming one channel at a time
#   Phase 3: Rotating jam — cycle through jamming different channels
#
# Usage:
#   chmod +x collect_hops.sh
#   ./collect_hops.sh [duration_per_session] [output_dir]
#
# Default: 120 seconds per session, output to hop_data/
# =============================================================================

set -e

DURATION=${1:-120}
OUTPUT_DIR=${2:-hop_data}
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SESSION_DIR="${OUTPUT_DIR}/session_${TIMESTAMP}"

echo "=============================================="
echo "  OcuSync Hop Data Collection"
echo "=============================================="
echo "  Duration per phase: ${DURATION}s"
echo "  Output: ${SESSION_DIR}/"
echo "  Date: $(date)"
echo "=============================================="
echo ""

mkdir -p "${SESSION_DIR}"

# ─────────────────────────────────────────────────────────────
# Phase 1: Baseline — No Jamming
# ─────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════╗"
echo "║  PHASE 1: Baseline (no jamming)             ║"
echo "║  Turn on drone, let it establish link        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
read -p "Press ENTER when drone is powered on and connected..."

echo "[*] Recording baseline hops for ${DURATION}s..."
python3 spectrum_scanner.py \
    --duration "${DURATION}" \
    --output "${SESSION_DIR}/baseline_scan.csv" \
    --quiet

echo "[*] Extracting hop events..."
python3 channel_tracker.py process \
    --input "${SESSION_DIR}/baseline_scan.csv" \
    --output "${SESSION_DIR}/baseline_hops.csv"

echo ""
echo "[+] Phase 1 complete"
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 2: Single-Channel Jamming (each channel)
# ─────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════╗"
echo "║  PHASE 2: Single-channel jamming             ║"
echo "║  Jam one channel at a time to force hops     ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Jam channels 3-8 (the ones covered by our 40 MHz bandwidth)
for CH in 3 4 5 6 7 8; do
    echo "──────────────────────────────────────────────"
    echo "  Jamming CH${CH} for ${DURATION}s"
    echo "──────────────────────────────────────────────"
    
    python3 spectrum_scanner.py \
        --duration "${DURATION}" \
        --jam-channel "${CH}" \
        --output "${SESSION_DIR}/jam_ch${CH}_scan.csv" \
        --quiet

    python3 channel_tracker.py process \
        --input "${SESSION_DIR}/jam_ch${CH}_scan.csv" \
        --output "${SESSION_DIR}/jam_ch${CH}_hops.csv"

    echo "[+] CH${CH} jamming phase complete"
    echo ""
    
    # Brief pause between phases
    sleep 2
done

echo "[+] Phase 2 complete"
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 3: Rotating Jam (cycles through channels)
# ─────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════╗"
echo "║  PHASE 3: Rapid alternating jam              ║"
echo "║  Cycle through channels 3-8 quickly          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# This is handled by running the scanner multiple times with short durations
CYCLE_DURATION=$((DURATION / 6))
for CH in 3 4 5 6 7 8; do
    python3 spectrum_scanner.py \
        --duration "${CYCLE_DURATION}" \
        --jam-channel "${CH}" \
        --output "${SESSION_DIR}/rotate_ch${CH}_scan.csv" \
        --quiet
done

# Combine all rotate scans
echo "[*] Combining rotation data..."
head -1 "${SESSION_DIR}/rotate_ch3_scan.csv" > "${SESSION_DIR}/rotate_combined_scan.csv"
for CH in 3 4 5 6 7 8; do
    tail -n +2 "${SESSION_DIR}/rotate_ch${CH}_scan.csv" >> "${SESSION_DIR}/rotate_combined_scan.csv"
done

python3 channel_tracker.py process \
    --input "${SESSION_DIR}/rotate_combined_scan.csv" \
    --output "${SESSION_DIR}/rotate_combined_hops.csv"

echo "[+] Phase 3 complete"
echo ""

# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
echo "=============================================="
echo "  COLLECTION COMPLETE"
echo "=============================================="
echo "  Session: ${SESSION_DIR}/"
echo ""
echo "  Files:"
ls -la "${SESSION_DIR}"/*.csv 2>/dev/null | awk '{print "    " $NF " (" $5 " bytes)"}'
echo ""

# Count total hops
TOTAL_HOPS=$(cat "${SESSION_DIR}"/*_hops.csv 2>/dev/null | grep -c "^[0-9]" || echo 0)
echo "  Total hop events collected: ${TOTAL_HOPS}"
echo ""

if [ "$TOTAL_HOPS" -gt 20 ]; then
    echo "  ✅ Enough data to train ML model!"
    echo ""
    read -p "  Train model now? (y/n): " TRAIN
    if [ "$TRAIN" = "y" ] || [ "$TRAIN" = "Y" ]; then
        echo ""
        echo "  [*] Training ML predictor..."
        python3 ml_predictor.py train --data-dir "${SESSION_DIR}"
        echo ""
        echo "  [+] Model saved to models/hop_predictor.pkl"
        echo ""
        echo "  To run the ML jammer:"
        echo "    python3 reactive_jammer.py --model models/hop_predictor.pkl --tx-gain 50"
    fi
else
    echo "  ⚠️  Need more data (at least 20 hops). Run more sessions."
fi

echo ""
echo "=============================================="
