import pandas as pd
import sys
import os

# Find the most recent CSV in iq_datasets
dataset_dir = 'iq_datasets'
files = [os.path.join(dataset_dir, f) for f in os.listdir(dataset_dir) if f.endswith('.csv')]
if not files:
    print("No CSV files found.")
    sys.exit(1)

latest_file = max(files, key=os.path.getctime)
print(f"Analyzing: {latest_file}")

df = pd.read_csv(latest_file)

# Focus on effective_size_bytes
print("\n--- Statistics for 'effective_size_bytes' ---")
print(df['effective_size_bytes'].describe())

print("\n--- First 10 rows ---")
print(df[['timestamp', 'effective_size_bytes', 'active_ratio']].head(10))

# Check consistency (Coefficient of Variation)
mean = df['effective_size_bytes'].mean()
std = df['effective_size_bytes'].std()
cv = std / mean if mean != 0 else 0
print(f"\nCoefficient of Variation: {cv:.4f} (Lower means more consistent)")

if cv < 0.2:
    print(">> Data looks fairly consistent for a single state.")
else:
    print(">> Data shows high variance. This might be due to TDD bursting (some windows capture noise, some capture signal).")

# Quick histogram approximation
print("\n--- Distribution ---")
try:
    print(pd.cut(df['effective_size_bytes'], bins=5).value_counts().sort_index())
except:
    pass
