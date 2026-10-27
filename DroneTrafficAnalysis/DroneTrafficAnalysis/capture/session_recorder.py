#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session Recorder for DJI Mini Traffic Analysis
Coordinates RF capture sessions with drone state labeling

Usage:
    python session_recorder.py --session experiment_1

This creates a structured recording session with:
- RF feature captures on both 2.4 GHz and 5.8 GHz
- Synchronized state labeling
- Session metadata and notes
"""

import os
import sys
import json
import time
import signal
import argparse
import subprocess
from datetime import datetime
from pathlib import Path


class SessionRecorder:
    """Coordinates drone traffic capture sessions"""
    
    STATES = [
        ('idle', 'Drone powered on, motors off'),
        ('hover', 'Hovering in place'),
        ('fly_slow', 'Flying slowly (<5 m/s)'),
        ('fly_fast', 'Flying fast (>5 m/s)'),
        ('fly_forward', 'Moving forward'),
        ('fly_backward', 'Moving backward'),
        ('fly_left', 'Moving left'),
        ('fly_right', 'Moving right'),
        ('ascend', 'Ascending'),
        ('descend', 'Descending'),
        ('rotate_cw', 'Rotating clockwise'),
        ('rotate_ccw', 'Rotating counter-clockwise'),
        ('under_jamming', 'During RF jamming attack'),
        ('under_gps_spoof', 'During GPS spoofing attack'),
        ('signal_weak', 'Weak signal conditions'),
        ('signal_lost', 'Lost connection'),
        ('rth', 'Return to home activated'),
    ]
    
    def __init__(self, session_name, data_dir="../dataset/data"):
        self.session_name = session_name
        self.data_dir = Path(data_dir)
        self.session_dir = self.data_dir / session_name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_state = "idle"
        self.state_log = []
        self.capture_process = None
        self.start_time = None
        self.notes = []
        
        # Session metadata
        self.metadata = {
            'session_name': session_name,
            'created': datetime.now().isoformat(),
            'drone_model': 'DJI Mini',
            'protocol': 'OcuSync 2.0',
            'hardware': 'USRP B210',
            'frequencies': ['2.4 GHz', '5.8 GHz'],
            'states_recorded': [],
            'total_duration_seconds': 0,
            'notes': []
        }
    
    def print_header(self):
        print("\n" + "="*70)
        print("   DJI MINI TRAFFIC ANALYSIS - SESSION RECORDER")
        print("="*70)
        print(f"   Session: {self.session_name}")
        print(f"   Output:  {self.session_dir}")
        print("="*70)
    
    def print_menu(self):
        print("\n" + "-"*70)
        print("AVAILABLE STATES:")
        for i, (state, desc) in enumerate(self.STATES):
            marker = " >>> " if state == self.current_state else "     "
            print(f"{marker}[{i:2d}] {state:15s} - {desc}")
        print("-"*70)
        print("COMMANDS:")
        print("   [s] Start/Stop RF capture    [n] Add note")
        print("   [m] Show session metadata    [q] Quit and save")
        print("-"*70)
        print(f"Current State: {self.current_state}")
        if self.capture_process:
            elapsed = time.time() - self.start_time if self.start_time else 0
            print(f"Capture Status: RUNNING ({elapsed:.0f}s elapsed)")
        else:
            print("Capture Status: STOPPED")
        print("-"*70)
    
    def set_state(self, state_idx):
        if 0 <= state_idx < len(self.STATES):
            old_state = self.current_state
            self.current_state = self.STATES[state_idx][0]
            
            timestamp = datetime.now().isoformat()
            self.state_log.append({
                'timestamp': timestamp,
                'from_state': old_state,
                'to_state': self.current_state
            })
            
            if self.current_state not in self.metadata['states_recorded']:
                self.metadata['states_recorded'].append(self.current_state)
            
            print(f"\n>>> State changed: {old_state} -> {self.current_state}")
            
            # If capture is running, update state file
            if self.capture_process:
                self._write_current_state()
    
    def _write_current_state(self):
        """Write current state to file for capture process to read"""
        state_file = self.session_dir / ".current_state"
        with open(state_file, 'w') as f:
            f.write(self.current_state)
    
    def start_capture(self, frequency="5.8"):
        if self.capture_process:
            print("Capture already running! Stop it first.")
            return
        
        freq_hz = 5.8e9 if frequency == "5.8" else 2.437e9
        output_file = self.session_dir / f"rf_{frequency}ghz_{datetime.now().strftime('%H%M%S')}.csv"
        
        # Start RF capture process
        capture_script = Path(__file__).parent / "rf_capture.py"
        cmd = [
            sys.executable, str(capture_script),
            '-f', str(freq_hz),
            '-o', str(output_file),
            '-g', '40'
        ]
        
        print(f"\nStarting RF capture at {frequency} GHz...")
        print(f"Output: {output_file}")
        
        try:
            self.capture_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            self.start_time = time.time()
            self._write_current_state()
            print("Capture started successfully!")
        except Exception as e:
            print(f"Failed to start capture: {e}")
            self.capture_process = None
    
    def stop_capture(self):
        if not self.capture_process:
            print("No capture running.")
            return
        
        print("\nStopping capture...")
        try:
            self.capture_process.stdin.write('q\n')
            self.capture_process.stdin.flush()
            self.capture_process.wait(timeout=5)
        except:
            self.capture_process.terminate()
        
        elapsed = time.time() - self.start_time
        self.metadata['total_duration_seconds'] += elapsed
        print(f"Capture stopped. Duration: {elapsed:.1f}s")
        
        self.capture_process = None
        self.start_time = None
    
    def add_note(self):
        note = input("Enter note: ").strip()
        if note:
            entry = {
                'timestamp': datetime.now().isoformat(),
                'note': note,
                'state': self.current_state
            }
            self.notes.append(entry)
            self.metadata['notes'].append(entry)
            print(f"Note added: {note}")
    
    def show_metadata(self):
        print("\n" + "="*50)
        print("SESSION METADATA:")
        print(json.dumps(self.metadata, indent=2))
        print("="*50)
    
    def save_session(self):
        """Save all session data"""
        # Stop capture if running
        if self.capture_process:
            self.stop_capture()
        
        # Save metadata
        metadata_file = self.session_dir / "metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(self.metadata, f, indent=2)
        
        # Save state log
        state_log_file = self.session_dir / "state_log.json"
        with open(state_log_file, 'w') as f:
            json.dump(self.state_log, f, indent=2)
        
        print(f"\nSession saved to: {self.session_dir}")
        print(f"  - metadata.json")
        print(f"  - state_log.json")
        print(f"  - rf_*.csv files")
    
    def run(self):
        """Main interactive loop"""
        self.print_header()
        
        while True:
            self.print_menu()
            
            try:
                cmd = input("\nEnter command: ").strip().lower()
                
                if cmd == 'q':
                    self.save_session()
                    break
                elif cmd == 's':
                    if self.capture_process:
                        self.stop_capture()
                    else:
                        freq = input("Frequency (2.4 or 5.8, default 5.8): ").strip() or "5.8"
                        self.start_capture(freq)
                elif cmd == 'n':
                    self.add_note()
                elif cmd == 'm':
                    self.show_metadata()
                elif cmd.isdigit():
                    self.set_state(int(cmd))
                else:
                    print("Unknown command. Try again.")
                    
            except KeyboardInterrupt:
                print("\n\nInterrupted!")
                self.save_session()
                break
            except Exception as e:
                print(f"Error: {e}")


def main():
    parser = argparse.ArgumentParser(description='DJI Mini Session Recorder')
    parser.add_argument('--session', '-s', type=str, 
                        default=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                        help='Session name')
    parser.add_argument('--data-dir', '-d', type=str,
                        default="../dataset/data",
                        help='Data directory')
    args = parser.parse_args()
    
    recorder = SessionRecorder(args.session, args.data_dir)
    recorder.run()


if __name__ == '__main__':
    main()
