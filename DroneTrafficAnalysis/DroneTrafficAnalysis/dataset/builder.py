#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dataset Builder for DJI Drone Traffic Analysis
Combines multiple capture sessions into ML-ready datasets

Usage:
    python builder.py --sessions session1 session2 --output train_data.csv
"""

import pandas as pd
import numpy as np
from pathlib import Path
import argparse
import json
from sklearn.model_selection import train_test_split
from collections import Counter


def load_session(session_dir):
    """Load all data from a capture session"""
    session_path = Path(session_dir)
    
    # Load metadata
    metadata_file = session_path / "metadata.json"
    if metadata_file.exists():
        with open(metadata_file) as f:
            metadata = json.load(f)
        print(f"Session: {metadata.get('session_name', session_path.name)}")
    else:
        metadata = {}
    
    # Load all RF capture files
    csv_files = list(session_path.glob("rf_*.csv"))
    if not csv_files:
        print(f"  No RF capture files found in {session_dir}")
        return None
    
    dfs = []
    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        df['source_file'] = csv_file.name
        df['session'] = session_path.name
        dfs.append(df)
        print(f"  Loaded: {csv_file.name} ({len(df)} samples)")
    
    combined = pd.concat(dfs, ignore_index=True)
    return combined


def balance_dataset(df, target_col='state', strategy='undersample'):
    """Balance classes for ML training"""
    class_counts = Counter(df[target_col])
    print(f"\nOriginal class distribution: {dict(class_counts)}")
    
    if strategy == 'undersample':
        min_count = min(class_counts.values())
        balanced_dfs = []
        for cls in class_counts.keys():
            cls_df = df[df[target_col] == cls]
            sampled = cls_df.sample(n=min_count, random_state=42)
            balanced_dfs.append(sampled)
        balanced = pd.concat(balanced_dfs, ignore_index=True)
    elif strategy == 'oversample':
        max_count = max(class_counts.values())
        balanced_dfs = []
        for cls in class_counts.keys():
            cls_df = df[df[target_col] == cls]
            if len(cls_df) < max_count:
                sampled = cls_df.sample(n=max_count, replace=True, random_state=42)
            else:
                sampled = cls_df
            balanced_dfs.append(sampled)
        balanced = pd.concat(balanced_dfs, ignore_index=True)
    else:
        balanced = df
    
    new_counts = Counter(balanced[target_col])
    print(f"Balanced class distribution: {dict(new_counts)}")
    
    return balanced.sample(frac=1, random_state=42).reset_index(drop=True)


def create_train_test_split(df, test_size=0.2, val_size=0.1, target_col='state'):
    """Create stratified train/val/test splits"""
    # First split: train+val and test
    train_val, test = train_test_split(
        df, test_size=test_size, stratify=df[target_col], random_state=42
    )
    
    # Second split: train and val
    val_ratio = val_size / (1 - test_size)
    train, val = train_test_split(
        train_val, test_size=val_ratio, stratify=train_val[target_col], random_state=42
    )
    
    print(f"\nDataset splits:")
    print(f"  Train: {len(train)} samples")
    print(f"  Val:   {len(val)} samples")
    print(f"  Test:  {len(test)} samples")
    
    return train, val, test


def build_dataset(session_dirs, output_dir, balance=True):
    """Build complete dataset from multiple sessions"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Load all sessions
    all_data = []
    for session_dir in session_dirs:
        df = load_session(session_dir)
        if df is not None:
            all_data.append(df)
    
    if not all_data:
        print("No data loaded!")
        return None
    
    combined = pd.concat(all_data, ignore_index=True)
    print(f"\nTotal combined samples: {len(combined)}")
    
    # Filter out unknown or rare states
    state_counts = Counter(combined['state'])
    min_samples = 100  # Minimum samples per state
    valid_states = [s for s, c in state_counts.items() if c >= min_samples]
    print(f"States with >= {min_samples} samples: {valid_states}")
    
    combined = combined[combined['state'].isin(valid_states)]
    print(f"After filtering: {len(combined)} samples")
    
    # Balance if requested
    if balance:
        combined = balance_dataset(combined)
    
    # Create splits
    train, val, test = create_train_test_split(combined)
    
    # Save datasets
    train.to_csv(output_path / "train.csv", index=False)
    val.to_csv(output_path / "val.csv", index=False)
    test.to_csv(output_path / "test.csv", index=False)
    
    # Save full dataset
    combined.to_csv(output_path / "full_dataset.csv", index=False)
    
    # Save dataset info
    info = {
        'total_samples': len(combined),
        'train_samples': len(train),
        'val_samples': len(val),
        'test_samples': len(test),
        'states': list(combined['state'].unique()),
        'state_distribution': dict(Counter(combined['state'])),
        'features': list(combined.columns),
        'sessions': session_dirs
    }
    
    with open(output_path / "dataset_info.json", 'w') as f:
        json.dump(info, f, indent=2)
    
    print(f"\nDataset saved to: {output_path}")
    print(f"  - train.csv")
    print(f"  - val.csv")
    print(f"  - test.csv")
    print(f"  - full_dataset.csv")
    print(f"  - dataset_info.json")
    
    return combined


def main():
    parser = argparse.ArgumentParser(description='Dataset Builder')
    parser.add_argument('--sessions', '-s', nargs='+', required=True,
                        help='Session directories to combine')
    parser.add_argument('--output', '-o', type=str, default='./processed',
                        help='Output directory for processed dataset')
    parser.add_argument('--no-balance', action='store_true',
                        help='Skip class balancing')
    args = parser.parse_args()
    
    build_dataset(args.sessions, args.output, balance=not args.no_balance)


if __name__ == '__main__':
    main()
