# DJI 5.8GHz Jammer Project

## Overview
This project implements a simple RF jammer targeting the 5.8GHz band used by DJI drones for video transmission. It uses GNU Radio with a USRP B210 software-defined radio.

⚠️ **LEGAL WARNING**: RF jamming is ILLEGAL in most countries without proper authorization. This is for educational/research purposes in controlled lab environments only. Ensure you have proper authorization and comply with local regulations.

## Hardware Requirements
- USRP B210 (detected: serial=321E87F)
- 5.8GHz capable antennas
- DJI drone operating on 5.8GHz

## Files
- `dji_jammer_5_8ghz.grc` - GNU Radio Companion jammer flowgraph
- `dji_jammer_5_8ghz.py` - Generated Python jammer script
- `dji_spectrum_monitor.grc` - GNU Radio Companion spectrum analyzer flowgraph
- `dji_spectrum_monitor.py` - Generated Python spectrum analyzer script

## How to Use

### Step 1: Monitor the Spectrum (BEFORE Jamming)
First, observe the baseline 5.8GHz spectrum to identify the drone's signal:

```bash
cd /root/projects
python3 dji_spectrum_monitor.py
```

**What to look for:**
- Turn on your DJI drone and establish video link
- Look for a strong signal spike around 5.8GHz on the spectrum analyzer
- Note the exact frequency (you may need to adjust the frequency offset slider)
- The waterfall display will show signal activity over time

### Step 2: Run the Jammer
Open a new terminal and start the jammer:

```bash
cd /root/projects
python3 dji_jammer_5_8ghz.py
```

**Controls available:**
- **TX Gain** (0-89 dB): Start LOW (around 30-40) and increase gradually
- **Noise Amplitude** (0-1): Controls jamming power (start at 0.3)
- **Frequency Offset** (-50 to +50 MHz): Fine-tune to match drone frequency

### Step 3: Observe the Effect
Keep the spectrum monitor running while the jammer operates:

**Expected observations:**
1. **Spectrum Monitor**: You should see a strong noise floor rise across the 5.8GHz band
2. **Drone Behavior**: Video transmission may degrade or disconnect
3. **Waterfall Display**: Shows the jamming signal as a continuous bright band

### Step 4: Adjust Parameters
For effective jamming:
- Increase TX Gain gradually (max 89 dB on B210)
- Adjust Frequency Offset to center on drone's frequency
- Monitor for overflows (underruns) - reduce sample rate if needed

## Technical Details

### Jammer Specifications
- **Center Frequency**: 5.8 GHz (5800 MHz)
- **Sample Rate**: 20 MHz
- **Bandwidth**: 20 MHz (covers most DJI video channels)
- **Modulation**: Gaussian white noise
- **Max TX Power**: ~50mW (depends on gain and antenna)

### DJI Frequency Bands
DJI drones typically use:
- 5.725 - 5.850 GHz (FPV video transmission)
- Multiple channels within this range
- 20-40 MHz channel bandwidth

### Safety Considerations
1. **Start with LOW power** - begin at TX gain = 30-40 dB
2. **Use proper antennas** - avoid damaging USRP with mismatched loads
3. **Monitor temperature** - B210 can get hot during continuous TX
4. **Limit duration** - keep tests short to avoid hardware damage
5. **Distance matters** - effective range depends on TX power and antenna gain

## Troubleshooting

### No GUI appears
If you're running headless, you'll need X11 forwarding or VNC. Alternatively, modify the flowgraphs to use `no_gui` output and analyze data files.

### Underruns (UUU messages)
- Reduce sample rate to 10 MHz or 5 MHz
- Close other applications
- Use `sudo` for better scheduling priority

### No effect on drone
- Verify USRP is transmitting (check TX LED)
- Increase TX gain gradually
- Ensure frequency offset matches drone's actual frequency
- Check antenna connections
- Move USRP closer to drone

### USRP errors
```bash
# Reset USRP if needed
sudo usrp_burn_mb_eeprom --values="serial=321E87F" --init
```

## Alternative Usage (GUI)

You can also open and modify the flowgraphs in GNU Radio Companion:

```bash
gnuradio-companion /root/projects/dji_jammer_5_8ghz.grc
```

This allows you to:
- Visualize the flowgraph
- Modify parameters
- Add additional blocks (filters, modulators, etc.)
- Generate updated Python scripts

## Advanced Options

### Swept Jamming
To jam multiple frequencies, you could modify the flowgraph to add:
- VCO (Voltage Controlled Oscillator) with sawtooth sweep
- Multiple parallel noise sources at different frequencies

### Tone Jamming
Replace noise source with:
- Signal source (sine wave)
- Multiple tones at different offsets

### Record and Replay
Add file sinks/sources to record legitimate signals and replay them.

## Research Notes

Document your observations:
- TX gain settings used
- Observed effects on drone video
- Distance between USRP and drone
- Any frequency offsets needed
- Power consumption
- Temperature rise in USRP

This data is valuable for your research report.

## Clean Up

When done, stop all GNU Radio processes:
```bash
pkill -f "dji_jammer"
pkill -f "dji_spectrum"
```

## Next Steps

For more sophisticated jamming:
1. Analyze DJI's actual protocol (OcuSync, Lightbridge)
2. Implement protocol-specific attacks
3. Use directional antennas for focused jamming
4. Experiment with different modulation schemes
5. Study anti-jamming countermeasures

Good luck with your research! 📡
