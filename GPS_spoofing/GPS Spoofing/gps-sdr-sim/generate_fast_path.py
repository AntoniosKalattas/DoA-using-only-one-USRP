#!/usr/bin/env python3

import math

# Starting position (near sea level)
x = -2744196.460
y = 4421401.225
z = 3875810.147

# Movement deltas for faster speed (about 10 m/s)
dx = 3.75  # meters per 0.1 seconds
dy = 2.50
dz = 1.75

# Generate 3000 points (300 seconds at 10Hz)
with open('fast_path.csv', 'w') as f:
    for i in range(3000):
        time = i * 0.1  # 10Hz sampling
        f.write(f"{time:5.1f},{x + dx*i:11.3f}, {y + dy*i:11.3f}, {z + dz*i:11.3f}\n")