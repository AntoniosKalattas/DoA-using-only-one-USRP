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
start_h = 10

# Convert to ECEF
x, y, z = llh_to_ecef(start_lat, start_lon, start_h)

# Create an aggressive path with large jumps
duration = 300  # 5 minutes
samples = duration * 10  # 10Hz sampling
time = np.linspace(0, duration, samples)

# Very high speed - 200 km/h (55.56 m/s)
max_speed = 55.56
speed = max_speed * np.ones_like(time)  # Constant max speed

# Create large position jumps
with open('aggressive_path.csv', 'w') as f:
    # First second stationary to establish position
    for i in range(10):
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")
    
    # Then aggressive movement with large jumps
    jump_size = 100  # meters
    for i in range(10, samples):
        if i % 50 == 0:  # Create a jump every 5 seconds
            x += jump_size * 2
            y += jump_size * 2
        else:
            x += speed[i] * 0.1  # Normal high-speed movement between jumps
            y += speed[i] * 0.1
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")