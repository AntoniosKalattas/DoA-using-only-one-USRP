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

# Create a very gradual path
duration = 300  # 5 minutes
samples = duration * 10  # 10Hz sampling
time = np.linspace(0, duration, samples)

# Super slow start, gradually building up to walking speed (5 km/h)
max_speed = 1.4  # 5 km/h in m/s
speed = max_speed * (1 - np.exp(-time/30))  # Very gradual acceleration

# Create a path that follows local roads (heading slightly north-east)
angle = np.pi/6  # 30-degree angle to match local road
dx = np.cos(angle) * speed * 0.1  # 0.1 seconds between points
dy = np.sin(angle) * speed * 0.1

# Generate the path with very small initial movements
with open('cyprus_path.csv', 'w') as f:
    # First 10 seconds almost stationary (tiny movements)
    for i in range(100):  # First 10 seconds
        x += dx[i] * 0.01  # Only 1% of normal movement
        y += dy[i] * 0.01
        z += np.random.normal(0, 0.01)  # Tiny height variations
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")
    
    # Then gradually increase movement
    for i in range(100, samples):
        x += dx[i] * min((i-100)/900, 1.0)  # Gradually increase to full speed
        y += dy[i] * min((i-100)/900, 1.0)
        z += np.random.normal(0, 0.05)  # Small height variations
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")