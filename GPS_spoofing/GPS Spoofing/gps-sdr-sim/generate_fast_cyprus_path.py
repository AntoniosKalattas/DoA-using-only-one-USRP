#!/usr/bin/env python3

import math
import numpy as np

def llh_to_ecef(lat, lon, h):
    # Convert lat/lon to radians
    lat = lat * math.pi/180
    lon = lon * math.pi/180
    
    # WGS84 parameters
    a = 6378137.0
    f = 1/298.257223563
    b = a*(1-f)
    e2 = 1 - (b*b)/(a*a)
    
    # Calculate ECEF coordinates
    N = a / math.sqrt(1 - e2*math.sin(lat)*math.sin(lat))
    x = (N + h) * math.cos(lat) * math.cos(lon)
    y = (N + h) * math.cos(lat) * math.sin(lon)
    z = (N*(1-e2) + h) * math.sin(lat)
    
    return x, y, z

# Start from user's exact location
start_lat = 35.150314
start_lon = 33.406979
start_h = 10  # 10 meters above ground

# Convert to ECEF
x, y, z = llh_to_ecef(start_lat, start_lon, start_h)

# Create a fast path
duration = 300  # 5 minutes
samples = duration * 10  # 10Hz sampling
time = np.linspace(0, duration, samples)

# Fast speed - 50 km/h (13.89 m/s)
max_speed = 13.89  # 50 km/h in m/s
# Quick acceleration to full speed
speed = max_speed * (1 - np.exp(-time/5))  # Reach full speed in about 5 seconds

# Create a straight path heading north (along a major road)
angle = np.pi/2  # 90 degrees (pure north)
dx = np.cos(angle) * speed * 0.1  # 0.1 seconds between points
dy = np.sin(angle) * speed * 0.1

# Generate the path with quick acceleration
with open('fast_cyprus_path.csv', 'w') as f:
    # First 2 seconds stationary
    for i in range(20):  # First 2 seconds
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")
    
    # Then rapid acceleration and movement
    for i in range(20, samples):
        x += dx[i] * 1.5  # 50% faster than normal
        y += dy[i] * 1.5
        z += np.random.normal(0, 0.01)  # Very small height variations
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")