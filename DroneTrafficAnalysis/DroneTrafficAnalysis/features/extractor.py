#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature Extractor for DJI Drone Traffic Analysis
Processes captured RF data and extracts ML-ready features

Usage:
    python extractor.py --input rf_capture.csv --output features.csv
"""

import pandas as pd
import numpy as np
from pathlib import Path
import argparse
from scipy import stats
from scipy.signal import find_peaks


def compute_rolling_features(df, window_sizes=[10, 50, 100]):
    """Compute rolling window statistics for temporal features"""
    features = df.copy()
    
    numeric_cols = ['mean_power_db', 'std_power_db', 'max_power_db', 
                    'spectral_entropy', 'spectral_flatness', 'power_variance_rate']
    
    for window in window_sizes:
        suffix = f"_w{window}"
        for col in numeric_cols:
            if col in df.columns:
                # Rolling mean
                features[f"{col}_mean{suffix}"] = df[col].rolling(window, min_periods=1).mean()
                # Rolling std
                features[f"{col}_std{suffix}"] = df[col].rolling(window, min_periods=1).std().fillna(0)
                # Rolling min/max
                features[f"{col}_min{suffix}"] = df[col].rolling(window, min_periods=1).min()
                features[f"{col}_max{suffix}"] = df[col].rolling(window, min_periods=1).max()
                # Rate of change
                features[f"{col}_roc{suffix}"] = df[col].diff(window).fillna(0)
    
    return features


def compute_derived_features(df):
    """Compute derived features from raw measurements"""
    features = df.copy()
    
    # Power dynamics
    features['power_range'] = df['max_power_db'] - df['min_power_db']
    features['power_skew'] = df['mean_power_db'].rolling(20, min_periods=1).apply(
        lambda x: stats.skew(x) if len(x) > 2 else 0, raw=True
    ).fillna(0)
    
    # Signal stability metrics
    features['signal_stability'] = 1 / (df['power_variance_rate'] + 0.01)
    features['bandwidth_power_ratio'] = df['bandwidth_3db_hz'] / (np.abs(df['max_power_db']) + 1)
    
    # Entropy-based features
    features['entropy_flatness_ratio'] = df['spectral_entropy'] / (df['spectral_flatness'] + 0.0001)
    
    # Frequency activity
    features['freq_offset_abs'] = np.abs(df['peak_freq_offset_hz'])
    features['freq_offset_change'] = df['peak_freq_offset_hz'].diff().abs().fillna(0)
    
    # Signal presence patterns
    features['signal_presence_ratio'] = df['signal_presence'].rolling(50, min_periods=1).mean()
    
    return features


def compute_state_transition_features(df):
    """Features related to state changes and patterns"""
    features = df.copy()
    
    # Time since state change (requires state column)
    if 'state' in df.columns:
        state_changes = df['state'] != df['state'].shift(1)
        state_change_idx = state_changes.cumsum()
        features['samples_in_state'] = state_change_idx.groupby(state_change_idx).cumcount() + 1
    
    return features


def extract_features(input_file, output_file=None):
    """Main feature extraction pipeline"""
    print(f"Loading data from: {input_file}")
    df = pd.read_csv(input_file)
    
    print(f"Loaded {len(df)} samples")
    print(f"States in data: {df['state'].unique().tolist() if 'state' in df.columns else 'N/A'}")
    
    # Apply feature extraction
    print("Computing rolling features...")
    df = compute_rolling_features(df)
    
    print("Computing derived features...")
    df = compute_derived_features(df)
    
    print("Computing state transition features...")
    df = compute_state_transition_features(df)
    
    # Remove rows with NaN (from rolling windows at start)
    df = df.dropna()
    
    print(f"Final feature count: {len(df.columns)}")
    print(f"Final sample count: {len(df)}")
    
    if output_file:
        print(f"Saving features to: {output_file}")
        df.to_csv(output_file, index=False)
    
    return df


def combine_session_data(session_dir, output_file):
    """Combine all CSV files from a session into one dataset"""
    session_path = Path(session_dir)
    csv_files = list(session_path.glob("rf_*.csv"))
    
    if not csv_files:
        print(f"No RF capture files found in {session_dir}")
        return None
    
    print(f"Found {len(csv_files)} capture files")
    
    dfs = []
    for csv_file in csv_files:
        print(f"  Loading: {csv_file.name}")
        df = pd.read_csv(csv_file)
        dfs.append(df)
    
    combined = pd.concat(dfs, ignore_index=True)
    print(f"Combined total: {len(combined)} samples")
    
    # Extract features from combined data
    features_df = extract_features_from_df(combined)
    
    if output_file:
        features_df.to_csv(output_file, index=False)
        print(f"Saved to: {output_file}")
    
    return features_df


def extract_features_from_df(df):
    """Extract features from DataFrame (already loaded)"""
    df = compute_rolling_features(df)
    df = compute_derived_features(df)
    df = compute_state_transition_features(df)
    df = df.dropna()
    return df


def main():
    parser = argparse.ArgumentParser(description='Feature Extractor for Drone Traffic')
    parser.add_argument('-i', '--input', type=str, required=True,
                        help='Input CSV file or session directory')
    parser.add_argument('-o', '--output', type=str,
                        help='Output features CSV file')
    args = parser.parse_args()
    
    input_path = Path(args.input)
    
    if input_path.is_dir():
        # Process entire session
        output = args.output or str(input_path / "features_combined.csv")
        combine_session_data(args.input, output)
    else:
        # Process single file
        output = args.output or str(input_path.with_suffix('.features.csv'))
        extract_features(args.input, output)


if __name__ == '__main__':
    main()
