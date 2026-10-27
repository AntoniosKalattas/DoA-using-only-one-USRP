#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Real-time Prediction for DJI Drone Traffic Classification
Loads trained model and classifies live RF data

Usage:
    python predict.py --model ../ml/models/xgboost_model.pkl --port 5000
"""

import pickle
import json
import numpy as np
import pandas as pd
from pathlib import Path
import argparse
from datetime import datetime
from collections import deque
import threading
import time

# Feature columns expected (will be loaded from model directory)
WINDOW_SIZE = 10  # Samples to consider for prediction


class DroneStatePredictor:
    """Real-time drone state classifier"""
    
    def __init__(self, model_dir):
        model_path = Path(model_dir)
        
        # Load model
        model_files = list(model_path.glob("*_model.pkl"))
        if not model_files:
            raise FileNotFoundError(f"No model found in {model_dir}")
        
        with open(model_files[0], 'rb') as f:
            self.model = pickle.load(f)
        print(f"Loaded model: {model_files[0].name}")
        
        # Load scaler
        with open(model_path / "scaler.pkl", 'rb') as f:
            self.scaler = pickle.load(f)
        
        # Load label encoder
        with open(model_path / "label_encoder.pkl", 'rb') as f:
            self.label_encoder = pickle.load(f)
        
        # Load feature columns
        with open(model_path / "feature_columns.json", 'r') as f:
            self.feature_cols = json.load(f)
        
        print(f"Classes: {list(self.label_encoder.classes_)}")
        print(f"Features: {len(self.feature_cols)}")
        
        # Prediction buffer
        self.sample_buffer = deque(maxlen=WINDOW_SIZE)
        self.prediction_history = deque(maxlen=100)
        
        # State
        self.current_prediction = None
        self.confidence = 0.0
        self.lock = threading.Lock()
    
    def predict_single(self, features_dict):
        """Predict state from single feature dictionary"""
        # Create feature vector
        features = []
        for col in self.feature_cols:
            val = features_dict.get(col, 0)
            features.append(float(val) if val is not None else 0.0)
        
        # Scale
        X = np.array(features).reshape(1, -1)
        X_scaled = self.scaler.transform(X)
        
        # Predict
        prediction = self.model.predict(X_scaled)[0]
        state = self.label_encoder.inverse_transform([prediction])[0]
        
        # Get probabilities if available
        if hasattr(self.model, 'predict_proba'):
            probs = self.model.predict_proba(X_scaled)[0]
            confidence = float(np.max(probs))
        else:
            confidence = 1.0
        
        return state, confidence
    
    def update(self, features_dict):
        """Update prediction with new sample"""
        state, confidence = self.predict_single(features_dict)
        
        with self.lock:
            self.sample_buffer.append(features_dict)
            self.prediction_history.append({
                'timestamp': datetime.now().isoformat(),
                'state': state,
                'confidence': confidence
            })
            self.current_prediction = state
            self.confidence = confidence
        
        return state, confidence
    
    def get_smoothed_prediction(self):
        """Get smoothed prediction based on recent history"""
        with self.lock:
            if len(self.prediction_history) < 3:
                return self.current_prediction, self.confidence
            
            # Majority vote from last N predictions
            recent = list(self.prediction_history)[-10:]
            states = [p['state'] for p in recent]
            
            # Count occurrences
            from collections import Counter
            counts = Counter(states)
            most_common = counts.most_common(1)[0]
            
            return most_common[0], most_common[1] / len(recent)
    
    def get_status(self):
        """Get current status"""
        with self.lock:
            return {
                'prediction': self.current_prediction,
                'confidence': self.confidence,
                'samples_processed': len(self.prediction_history),
                'smoothed_prediction': self.get_smoothed_prediction()[0]
            }


def monitor_csv_file(predictor, csv_file, interval=0.1):
    """Monitor a CSV file for new data and make predictions"""
    print(f"\nMonitoring: {csv_file}")
    print("Press Ctrl+C to stop\n")
    
    last_position = 0
    
    while True:
        try:
            with open(csv_file, 'r') as f:
                f.seek(last_position)
                lines = f.readlines()
                last_position = f.tell()
            
            if lines:
                # Parse new lines (skip header if first read)
                for line in lines:
                    if line.startswith('timestamp'):
                        continue
                    
                    parts = line.strip().split(',')
                    if len(parts) < 10:
                        continue
                    
                    # Create features dict (simplified - actual should match feature cols)
                    features = {
                        'mean_power_db': float(parts[3]) if parts[3] else 0,
                        'std_power_db': float(parts[4]) if parts[4] else 0,
                        'max_power_db': float(parts[5]) if parts[5] else 0,
                        'min_power_db': float(parts[6]) if parts[6] else 0,
                        'peak_freq_offset_hz': float(parts[7]) if parts[7] else 0,
                        'bandwidth_3db_hz': float(parts[8]) if parts[8] else 0,
                        'spectral_entropy': float(parts[9]) if parts[9] else 0,
                        'spectral_flatness': float(parts[10]) if parts[10] else 0,
                        'power_variance_rate': float(parts[11]) if parts[11] else 0,
                        'signal_presence': int(parts[12]) if parts[12] else 0,
                    }
                    
                    state, conf = predictor.update(features)
                    smoothed, _ = predictor.get_smoothed_prediction()
                    
                    print(f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Predicted: {state:15s} (conf: {conf:.2f}) | "
                          f"Smoothed: {smoothed:15s}", end='', flush=True)
            
            time.sleep(interval)
            
        except KeyboardInterrupt:
            print("\n\nStopped monitoring")
            break
        except Exception as e:
            print(f"\nError: {e}")
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Real-time drone state prediction')
    parser.add_argument('--model', '-m', type=str, required=True,
                        help='Path to model directory')
    parser.add_argument('--input', '-i', type=str,
                        help='CSV file to monitor for live predictions')
    args = parser.parse_args()
    
    # Load predictor
    predictor = DroneStatePredictor(args.model)
    
    if args.input:
        # Monitor CSV file
        monitor_csv_file(predictor, args.input)
    else:
        print("\nPredictor loaded and ready.")
        print("Use --input to monitor a CSV file for live predictions")
        print("Or import DroneStatePredictor in your own code")


if __name__ == '__main__':
    main()
