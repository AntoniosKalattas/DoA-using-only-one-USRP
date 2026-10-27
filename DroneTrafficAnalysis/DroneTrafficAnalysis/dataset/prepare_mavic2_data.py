#!/usr/bin/env python3
"""
Prepare Mavic 2 data for ML training
Filters out DJI Mini data and dropped samples (-200 dB)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.model_selection import train_test_split
from collections import Counter

# Mavic 2 files only (134xxx and 140xxx sessions)
MAVIC2_FILES = [
    "idle_5.8ghz_20260130_134201.csv",
    "idle_5.8ghz_20260130_134347.csv",
    "hover_5.8ghz_20260130_134645.csv",
    "hover_5.8ghz_20260130_140503.csv",
    "fly_fast_5.8ghz_20260130_134908.csv",
    "fly_fast_5.8ghz_20260130_140951.csv",
]

def main():
    captures_dir = Path(__file__).parent / "data" / "captures"
    output_dir = Path(__file__).parent / "data" / "processed"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load all Mavic 2 files
    all_data = []
    for filename in MAVIC2_FILES:
        filepath = captures_dir / filename
        if filepath.exists():
            df = pd.read_csv(filepath)
            df['source_file'] = filename
            print(f"Loaded {filename}: {len(df)} rows")
            all_data.append(df)
        else:
            print(f"WARNING: File not found: {filename}")
    
    # Combine all data
    combined = pd.concat(all_data, ignore_index=True)
    print(f"\nTotal combined: {len(combined)} rows")
    
    # Filter out dropped samples (-200 dB indicates USB buffer overflow)
    valid_mask = combined['mean_power_db'] > -100
    filtered = combined[valid_mask].copy()
    dropped_count = len(combined) - len(filtered)
    print(f"Filtered out {dropped_count} dropped samples (-200 dB)")
    print(f"Valid samples: {len(filtered)}")
    
    # Show class distribution
    print("\nClass distribution:")
    class_counts = Counter(filtered['state'])
    for state, count in sorted(class_counts.items()):
        print(f"  {state}: {count}")
    
    # Balance classes by undersampling to smallest class
    min_count = min(class_counts.values())
    print(f"\nBalancing to {min_count} samples per class...")
    
    balanced_dfs = []
    for state in class_counts.keys():
        state_df = filtered[filtered['state'] == state]
        sampled = state_df.sample(n=min_count, random_state=42)
        balanced_dfs.append(sampled)
    
    balanced = pd.concat(balanced_dfs, ignore_index=True)
    balanced = balanced.sample(frac=1, random_state=42).reset_index(drop=True)  # Shuffle
    
    print(f"Balanced dataset: {len(balanced)} samples")
    
    # Create train/val/test splits (70/10/20)
    train_val, test = train_test_split(
        balanced, test_size=0.2, stratify=balanced['state'], random_state=42
    )
    train, val = train_test_split(
        train_val, test_size=0.125, stratify=train_val['state'], random_state=42
    )
    
    print(f"\nSplits:")
    print(f"  Train: {len(train)} samples")
    print(f"  Val:   {len(val)} samples")
    print(f"  Test:  {len(test)} samples")
    
    # Save datasets
    train.to_csv(output_dir / "train.csv", index=False)
    val.to_csv(output_dir / "val.csv", index=False)
    test.to_csv(output_dir / "test.csv", index=False)
    balanced.to_csv(output_dir / "full_dataset.csv", index=False)
    
    print(f"\nSaved to {output_dir}")
    print("Done!")

if __name__ == "__main__":
    main()
