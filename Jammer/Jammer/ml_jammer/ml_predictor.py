#!/usr/bin/env python3
"""
ML Channel Hop Predictor
==========================
Trains a machine learning model to predict which channel a DJI drone will hop
to next, based on:
- Current channel RSSI landscape (what the drone "sees")
- Recent hop history (momentum / patterns)
- Which channel is being jammed
- Channel visit frequency (drone avoids recently-visited noisy channels)

Two model architectures:
1. Random Forest (default) — Fast inference, good with small datasets
2. Sequence model — Uses last N hops as features (better with lots of data)

Usage:
    # Train on collected hop data
    python3 ml_predictor.py train --data-dir hop_data/

    # Predict next channel given current state
    python3 ml_predictor.py predict --model models/hop_predictor.pkl --current-channel 5

    # Evaluate model accuracy
    python3 ml_predictor.py evaluate --model models/hop_predictor.pkl --data-dir hop_data/
"""

import argparse
import csv
import glob
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd
from datetime import datetime

try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.metrics import (
        classification_report, confusion_matrix, accuracy_score
    )
    from sklearn.preprocessing import LabelEncoder
except ImportError:
    print("ERROR: scikit-learn not found. Install: pip install scikit-learn")
    sys.exit(1)

from channel_config import OCUSYNC_5GHZ_CHANNELS

# ─────────────────────────────────────────────────────────────
# Feature Engineering
# ─────────────────────────────────────────────────────────────

# ML features extracted from each hop event
FEATURE_COLUMNS = [
    # Current state
    'from_channel',           # Which channel the drone is leaving
    'dwell_ms',               # How long it stayed on current channel
    'jam_channel',            # Which channel we're jamming (0 = none)
    'from_ch_power',          # Signal strength on the channel being left
    'to_ch_power',            # Signal strength on the target (label leak — removed in training)

    # RSSI landscape at hop time (all 6 OcuSync data link channels)
    'ch1_power', 'ch2_power', 'ch3_power',
    'ch4_power', 'ch5_power', 'ch6_power',

    # Hop dynamics
    'hops_last_10s',          # How aggressively it's been hopping
    'hops_last_30s',
    'avg_dwell_last10',       # Recent average dwell time

    # Channel visit frequency (how often each channel was used recently)
    'ch1_visit_freq', 'ch2_visit_freq', 'ch3_visit_freq',
    'ch4_visit_freq', 'ch5_visit_freq', 'ch6_visit_freq',
]

# These features must NOT be used during training (they leak the answer)
LABEL_LEAK_COLUMNS = ['to_ch_power']

# The target variable
TARGET_COLUMN = 'to_channel'


def load_hop_data(data_dir):
    """Load all hop CSV files from a directory into a single DataFrame."""
    pattern = os.path.join(data_dir, '**/*.csv')
    files = glob.glob(pattern, recursive=True)

    if not files:
        # Try non-recursive
        files = glob.glob(os.path.join(data_dir, '*.csv'))

    if not files:
        print(f"[!] No CSV files found in {data_dir}")
        return None

    print(f"[*] Loading {len(files)} hop data files...")
    dfs = []
    for f in sorted(files):
        try:
            df = pd.read_csv(f)
            if TARGET_COLUMN in df.columns:
                df['source_file'] = os.path.basename(f)
                dfs.append(df)
                print(f"    {os.path.basename(f)}: {len(df)} hops")
        except Exception as e:
            print(f"    [!] Error loading {f}: {e}")

    if not dfs:
        print("[!] No valid hop data files found")
        return None

    combined = pd.concat(dfs, ignore_index=True)
    print(f"[+] Total: {len(combined)} hop events")
    return combined


def build_sequence_features(df, lookback=5):
    """
    Add sequence features: last N channels visited and their dwell times.
    This gives the model temporal context — what the drone has done recently.
    """
    for i in range(1, lookback + 1):
        df[f'prev_channel_{i}'] = df['from_channel'].shift(i).fillna(0).astype(int)
        df[f'prev_dwell_{i}'] = df['dwell_ms'].shift(i).fillna(0)
        df[f'prev_to_{i}'] = df[TARGET_COLUMN].shift(i).fillna(0).astype(int)

    # Binary: did the drone return to a previously visited channel?
    df['is_revisit'] = df.apply(
        lambda row: 1 if row[TARGET_COLUMN] in [
            row.get(f'prev_channel_{i}', 0) for i in range(1, lookback + 1)
        ] else 0,
        axis=1
    )

    # Hop direction (higher channel number = "up", lower = "down")
    df['hop_direction'] = df[TARGET_COLUMN] - df['from_channel']

    return df


def prepare_features(df, include_sequences=True, lookback=5):
    """Prepare feature matrix X and target vector y from hop data."""

    if include_sequences:
        df = build_sequence_features(df, lookback=lookback)

    # Determine which feature columns actually exist in the data
    available_features = [c for c in FEATURE_COLUMNS if c in df.columns and c not in LABEL_LEAK_COLUMNS]

    # Add sequence features if they exist
    if include_sequences:
        for i in range(1, lookback + 1):
            for prefix in ['prev_channel_', 'prev_dwell_', 'prev_to_']:
                col = f'{prefix}{i}'
                if col in df.columns:
                    available_features.append(col)
        if 'is_revisit' in df.columns:
            available_features.append('is_revisit')

    # Derived features
    # RSSI difference: from_channel power vs average of all other channels
    ch_power_cols = [c for c in df.columns if c.startswith('ch') and c.endswith('_power')]
    if ch_power_cols and 'from_ch_power' in df.columns:
        df['rssi_delta_from_avg'] = df['from_ch_power'] - df[ch_power_cols].mean(axis=1)
        available_features.append('rssi_delta_from_avg')

    # Minimum power channel (quietest = most likely target)
    if ch_power_cols:
        df['quietest_channel'] = df[ch_power_cols].idxmin(axis=1).str.extract(r'ch(\d+)').astype(float)
        available_features.append('quietest_channel')

        # Power rank of each channel (1 = quietest)
        power_df = df[ch_power_cols]
        ranks = power_df.rank(axis=1, method='min')
        for col in ch_power_cols:
            rank_col = col.replace('_power', '_rank')
            df[rank_col] = ranks[col]
            available_features.append(rank_col)

    # Is the from_channel currently being jammed?
    if 'jam_channel' in df.columns and 'from_channel' in df.columns:
        df['from_is_jammed'] = (df['jam_channel'] == df['from_channel']).astype(int)
        available_features.append('from_is_jammed')

    print(f"[*] Using {len(available_features)} features")

    X = df[available_features].fillna(0)
    y = df[TARGET_COLUMN]

    return X, y, available_features


# ─────────────────────────────────────────────────────────────
# Model Training
# ─────────────────────────────────────────────────────────────

def train_model(data_dir, model_dir='models', lookback=5):
    """Train a Random Forest to predict the next hop channel."""

    # Load data
    df = load_hop_data(data_dir)
    if df is None or len(df) < 10:
        print("[!] Not enough data to train (need at least 10 hop events)")
        return None

    # Prepare features
    X, y, feature_names = prepare_features(df, include_sequences=True, lookback=lookback)

    # Remove rows with NaN targets
    valid = y.notna()
    X = X[valid]
    y = y[valid].astype(int)

    print(f"\n[*] Training data: {len(X)} samples, {len(feature_names)} features")
    print(f"[*] Target distribution:")
    for ch, count in y.value_counts().sort_index().items():
        print(f"    CH{ch}: {count} ({count/len(y)*100:.1f}%)")

    # Split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if len(y.unique()) > 1 else None
    )

    # Train Random Forest
    print(f"\n[*] Training Random Forest...")
    rf = RandomForestClassifier(
        n_estimators=200,
        max_depth=15,
        min_samples_leaf=3,
        class_weight='balanced',  # Handle imbalanced channels
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_train, y_train)

    # Evaluate
    y_pred = rf.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    print(f"\n{'=' * 60}")
    print(f"  Model Evaluation")
    print(f"{'=' * 60}")
    print(f"  Accuracy: {accuracy:.2%}")
    print(f"\n  Classification Report:")
    print(classification_report(y_test, y_pred, zero_division=0))

    # Feature importance
    importances = pd.Series(rf.feature_importances_, index=feature_names).sort_values(ascending=False)
    print(f"\n  Top 15 Feature Importances:")
    for feat, imp in importances.head(15).items():
        bar = "█" * int(imp * 100)
        print(f"    {feat:30s} {imp:.4f} {bar}")

    # Cross-validation
    if len(y.unique()) > 1 and len(y) > 20:
        cv_scores = cross_val_score(rf, X, y, cv=min(5, len(y) // 5), scoring='accuracy')
        print(f"\n  Cross-validation: {cv_scores.mean():.2%} ± {cv_scores.std():.2%}")

    # Save model
    os.makedirs(model_dir, exist_ok=True)
    model_path = os.path.join(model_dir, 'hop_predictor.pkl')
    with open(model_path, 'wb') as f:
        pickle.dump({
            'model': rf,
            'feature_names': feature_names,
            'lookback': lookback,
            'accuracy': accuracy,
            'trained_on': datetime.now().isoformat(),
            'n_samples': len(X),
            'channel_classes': sorted(y.unique().tolist()),
        }, f)
    print(f"\n[+] Model saved to: {model_path}")

    # Save evaluation results
    eval_path = os.path.join(model_dir, 'evaluation_results.json')
    eval_results = {
        'accuracy': accuracy,
        'n_train': len(X_train),
        'n_test': len(X_test),
        'n_features': len(feature_names),
        'feature_importances': importances.head(20).to_dict(),
        'channel_distribution': y.value_counts().to_dict(),
        'trained_on': datetime.now().isoformat(),
    }
    with open(eval_path, 'w') as f:
        json.dump(eval_results, f, indent=2, default=str)
    print(f"[+] Evaluation saved to: {eval_path}")

    return rf


# ─────────────────────────────────────────────────────────────
# Prediction
# ─────────────────────────────────────────────────────────────

class HopPredictor:
    """
    Real-time hop predictor. Loads a trained model and predicts the next
    channel the drone will hop to.
    """

    def __init__(self, model_path):
        with open(model_path, 'rb') as f:
            data = pickle.load(f)

        self.model = data['model']
        self.feature_names = data['feature_names']
        self.lookback = data['lookback']
        self.accuracy = data['accuracy']
        self.channel_classes = data['channel_classes']

        # Internal state for sequence features
        self.hop_history = []

        print(f"[+] Loaded model: {model_path}")
        print(f"    Accuracy: {self.accuracy:.2%}")
        print(f"    Channels: {self.channel_classes}")
        print(f"    Features: {len(self.feature_names)}")

    def predict_next_channel(self, current_channel, channel_powers, jam_channel=0,
                             dwell_ms=0, hops_last_10s=0, hops_last_30s=0,
                             avg_dwell_last10=0):
        """
        Predict which channel the drone will hop to next.
        
        Args:
            current_channel: int, current active channel
            channel_powers: dict {ch_num: power_db}
            jam_channel: int, which channel is being jammed
            dwell_ms: float, how long drone has been on current channel
            
        Returns:
            (predicted_channel, confidence, all_probabilities)
        """
        # Build feature vector
        features = {}
        features['from_channel'] = current_channel
        features['dwell_ms'] = dwell_ms
        features['jam_channel'] = jam_channel
        features['from_ch_power'] = channel_powers.get(current_channel, -100)

        for ch in OCUSYNC_5GHZ_CHANNELS:
            features[f'ch{ch}_power'] = channel_powers.get(ch, -100)

        features['hops_last_10s'] = hops_last_10s
        features['hops_last_30s'] = hops_last_30s
        features['avg_dwell_last10'] = avg_dwell_last10

        # Visit frequency (approximate from hop history)
        total = max(1, len(self.hop_history))
        for ch in OCUSYNC_5GHZ_CHANNELS:
            count = sum(1 for h in self.hop_history[-20:] if h.get('to', 0) == ch)
            features[f'ch{ch}_visit_freq'] = count / min(total, 20)

        # Sequence features from hop history
        for i in range(1, self.lookback + 1):
            idx = len(self.hop_history) - i
            if idx >= 0:
                features[f'prev_channel_{i}'] = self.hop_history[idx].get('from', 0)
                features[f'prev_dwell_{i}'] = self.hop_history[idx].get('dwell', 0)
                features[f'prev_to_{i}'] = self.hop_history[idx].get('to', 0)
            else:
                features[f'prev_channel_{i}'] = 0
                features[f'prev_dwell_{i}'] = 0
                features[f'prev_to_{i}'] = 0

        features['is_revisit'] = 0

        # Derived features
        ch_power_vals = [features.get(f'ch{ch}_power', -100) for ch in OCUSYNC_5GHZ_CHANNELS
                         if f'ch{ch}_power' in features]
        if ch_power_vals:
            features['rssi_delta_from_avg'] = features['from_ch_power'] - np.mean(ch_power_vals)
            quietest_idx = np.argmin(ch_power_vals)
            ch_list = sorted([ch for ch in OCUSYNC_5GHZ_CHANNELS if f'ch{ch}_power' in features])
            features['quietest_channel'] = ch_list[quietest_idx] if ch_list else 0

            # Power ranks
            sorted_powers = sorted(enumerate(ch_power_vals), key=lambda x: x[1])
            for rank, (idx, _) in enumerate(sorted_powers, 1):
                if idx < len(ch_list):
                    features[f'ch{ch_list[idx]}_rank'] = rank

        features['from_is_jammed'] = 1 if jam_channel == current_channel else 0

        # Build feature vector in correct order
        X = np.zeros((1, len(self.feature_names)))
        for i, fname in enumerate(self.feature_names):
            X[0, i] = features.get(fname, 0)

        # Predict
        predicted = self.model.predict(X)[0]
        probabilities = self.model.predict_proba(X)[0]
        confidence = np.max(probabilities)

        # Map probabilities to channel numbers
        prob_dict = {}
        for cls_idx, cls in enumerate(self.model.classes_):
            prob_dict[int(cls)] = float(probabilities[cls_idx])

        # Update internal history
        self.hop_history.append({
            'from': current_channel,
            'to': int(predicted),
            'dwell': dwell_ms,
        })

        return int(predicted), confidence, prob_dict

    def update_actual(self, actual_channel):
        """
        Update the predictor with the actual channel the drone hopped to.
        Call this after observing the real hop to keep history accurate.
        """
        if self.hop_history:
            self.hop_history[-1]['to'] = actual_channel


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ML Channel Hop Predictor")
    subparsers = parser.add_subparsers(dest='command')

    # Train
    train_cmd = subparsers.add_parser('train', help='Train prediction model')
    train_cmd.add_argument('--data-dir', required=True, help='Directory with hop CSV files')
    train_cmd.add_argument('--model-dir', default='models', help='Output directory for model')
    train_cmd.add_argument('--lookback', type=int, default=5, help='Sequence lookback depth')

    # Evaluate
    eval_cmd = subparsers.add_parser('evaluate', help='Evaluate model on data')
    eval_cmd.add_argument('--model', required=True, help='Path to trained model .pkl')
    eval_cmd.add_argument('--data-dir', required=True, help='Directory with hop CSV files')

    # Predict (manual test)
    pred_cmd = subparsers.add_parser('predict', help='Predict next channel')
    pred_cmd.add_argument('--model', required=True, help='Path to trained model .pkl')
    pred_cmd.add_argument('--current-channel', type=int, required=True, help='Current channel')
    pred_cmd.add_argument('--jam-channel', type=int, default=0, help='Channel being jammed')

    args = parser.parse_args()

    if args.command == 'train':
        train_model(args.data_dir, args.model_dir, args.lookback)

    elif args.command == 'evaluate':
        predictor = HopPredictor(args.model)
        df = load_hop_data(args.data_dir)
        if df is not None:
            X, y, _ = prepare_features(df)
            y_pred = predictor.model.predict(X.fillna(0))
            print(f"\n  Accuracy: {accuracy_score(y, y_pred):.2%}")
            print(classification_report(y, y_pred, zero_division=0))

    elif args.command == 'predict':
        predictor = HopPredictor(args.model)
        # Simulate with flat RSSI
        powers = {ch: -70 for ch in OCUSYNC_5GHZ_CHANNELS}
        if args.jam_channel:
            powers[args.jam_channel] = -30  # Jammed channel is loud
        pred, conf, probs = predictor.predict_next_channel(
            args.current_channel, powers, args.jam_channel
        )
        print(f"\n  Current: CH{args.current_channel}")
        print(f"  Predicted next: CH{pred} (confidence: {conf:.1%})")
        print(f"  All probabilities:")
        for ch, p in sorted(probs.items(), key=lambda x: -x[1]):
            bar = "█" * int(p * 50)
            print(f"    CH{ch}: {p:.1%} {bar}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
