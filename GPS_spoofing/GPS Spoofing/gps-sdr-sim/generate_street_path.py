#!/usr/bin/env python3

import math
import numpy as np

def ecef_to_llh(x, y, z):
    # WGS84 parameters
    a = 6378137.0  # semi-major axis
    f = 1/298.257223563  # flattening
    b = a*(1-f)  # semi-minor axis
    e2 = 1 - (b*b)/(a*a)  # first eccentricity squared
    
    # Calculate latitude, longitude, height
    r = math.sqrt(x*x + y*y)
    lat = math.atan2(z, r*(1-e2))
    lon = math.atan2(y, x)
    h = 0  # We want ground level
    
    # Iterative latitude calculation
    for _ in range(5):
        N = a / math.sqrt(1 - e2*math.sin(lat)*math.sin(lat))
        h = r/math.cos(lat) - N
        lat = math.atan2(z, r*(1-e2*N/(N+h)))
    
    return lat*180/math.pi, lon*180/math.pi, h

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

# Start position (somewhere in a city)
start_lat = 35.6762  # Tokyo
start_lon = 139.6503
start_h = 10  # 10 meters above ground

# Convert to ECEF
x, y, z = llh_to_ecef(start_lat, start_lon, start_h)

# Create a realistic path with acceleration
duration = 300  # 5 minutes
samples = duration * 10  # 10Hz sampling
time = np.linspace(0, duration, samples)

# Create smooth speed profile (accelerate to 40 km/h)
max_speed = 11.11  # 40 km/h in m/s
speed = max_speed * (1 - np.exp(-time/10))  # Smooth acceleration

# Create a curved path
angle = np.pi/4  # 45-degree turn over the duration
dx = np.cos(time/duration * angle) * speed * 0.1  # 0.1 seconds between points
dy = np.sin(time/duration * angle) * speed * 0.1

# Generate the path
with open('street_path.csv', 'w') as f:
    for i in range(samples):
        x += dx[i]
        y += dy[i]
        # Keep z relatively constant (ground level)
        z += np.random.normal(0, 0.1)  # Tiny random variations in height
        
        # Write ECEF coordinates
        f.write(f"{time[i]:5.1f},{x:11.3f}, {y:11.3f}, {z:11.3f}\n")