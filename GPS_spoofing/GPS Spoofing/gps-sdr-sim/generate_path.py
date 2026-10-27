#!/usr/bin/env python3

import math

# Starting position (near sea level)
x = -2744196.460
y = 4421401.225
z = 3875810.147

# Movement deltas for walking speed (about 1.4 m/s)
dx = 0.525  # meters per 0.1 seconds
dy = 0.350
dz = 0.250

# Generate 3000 points (300 seconds at 10Hz)
with open('walking_path.csv', 'w') as f:
    for i in range(3000):
        time = i * 0.1  # 10Hz sampling
        f.write(f"{time:5.1f},{x + dx*i:11.3f}, {y + dy*i:11.3f}, {z + dz*i:11.3f}\n")