#!/bin/bash
# ============================================================
# V3 Capture Script — All states for Matrice 300 RTK
# ============================================================
#
# Prerequisites:
#   1. USRP B210 connected via USB 3.0
#   2. DJI M300 RTK powered ON, controller ON
#   3. Drone locked to custom channel 5840-5850 MHz
#   4. Run from: /root/projects/ReplayAttack/
#
# Usage:
#   chmod +x collect_v3.sh
#   ./collect_v3.sh           # Interactive — prompts before each capture
#   ./collect_v3.sh auto      # Runs all captures with pauses between
#
# Output: CSV files in ./iq_datasets/m300_v3/
# ============================================================

SCRIPT="python3 iq_size_capture_v3.py"
FREQ="5.845e9"
OUTPUT="./iq_datasets/m300_v3"
WINDOW=10

echo "============================================================"
echo "  V3 Capture Script — DJI Matrice 300 RTK"
echo "============================================================"
echo "  Frequency:    5.845 GHz (custom channel 5840-5850 MHz)"
echo "  Window:       ${WINDOW}ms"
echo "  Output:       ${OUTPUT}"
echo "============================================================"
echo ""

mkdir -p "$OUTPUT"

MODE="${1:-interactive}"

run_capture() {
    local state="$1"
    local duration="$2"
    local instructions="$3"

    echo ""
    echo "============================================================"
    echo "  NEXT: ${state} (${duration}s)"
    echo "============================================================"
    echo "  ${instructions}"
    echo ""

    if [ "$MODE" != "auto" ]; then
        read -p "  Press ENTER when ready (or 'skip' to skip): " response
        if [ "$response" = "skip" ]; then
            echo "  Skipped."
            return
        fi
    else
        echo "  Starting in 5 seconds..."
        sleep 5
    fi

    $SCRIPT capture \
        --state "$state" \
        --duration "$duration" \
        --window $WINDOW \
        --freq $FREQ \
        --output "$OUTPUT"

    echo ""
    echo "  ✓ ${state} capture done!"
    echo ""
}

# ============================================================
# Round 1: Stationary states (same as V1 but with V3 features)
# ============================================================
echo "========== ROUND 1: Stationary States =========="

run_capture "no_drone" 15 \
    "Drone OFF, controller OFF. Just background RF noise."

run_capture "idle_no_camera_move" 15 \
    "Drone ON, on the ground. DON'T touch the controller."

run_capture "idle_camera_move" 15 \
    "Drone ON, on the ground. ACTIVELY move the camera gimbal with the controller."

# ============================================================
# Round 2: Distance states (NEW in V3)
# ============================================================
echo ""
echo "========== ROUND 2: Distance States =========="
echo "  For these captures, you will physically carry/walk the drone"
echo "  at different distances from the USRP antenna."
echo ""

run_capture "stationary_close" 15 \
    "Drone ON, ~2 meters from USRP antenna. Don't move it."

run_capture "stationary_far" 15 \
    "Drone ON, ~10-15 meters from USRP antenna. Don't move it."

run_capture "walking_away" 30 \
    "Drone ON. Start ~2m from USRP, then SLOWLY walk it away to ~15m over 30 seconds."

run_capture "walking_closer" 30 \
    "Drone ON. Start ~15m from USRP, then SLOWLY walk it closer to ~2m over 30 seconds."

# ============================================================
# Done
# ============================================================
echo ""
echo "============================================================"
echo "  ALL CAPTURES COMPLETE!"
echo "============================================================"
echo ""
echo "  Output directory: ${OUTPUT}"
echo ""
echo "  Next steps:"
echo "    1. Train:     $SCRIPT train --data ${OUTPUT}"
echo "    2. Visualize: $SCRIPT visualize --data ${OUTPUT}"
echo "    3. Distance:  $SCRIPT distance --csv ${OUTPUT}/walking_away_*.csv"
echo ""
ls -la "$OUTPUT"/*.csv 2>/dev/null || echo "  (no CSV files found)"
