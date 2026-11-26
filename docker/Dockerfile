# USRP Capture Console: live B210 BLE radar + phone ground truth + phone calibration.
# Build context = the Thesis folder:   docker build -f docker/Dockerfile -t usrp-console .
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CONSOLE_HOST=0.0.0.0

# UHD (driver + C++ headers + Python bindings), numpy, a compiler for the streamer
RUN apt-get update && apt-get install -y --no-install-recommends \
        uhd-host libuhd-dev python3-uhd python3 python3-numpy \
        g++ libboost-dev usbutils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# B200/B210 FPGA + firmware images (UHD loads them on first open)
RUN uhd_images_downloader -t b2xx

WORKDIR /app
COPY . /app

# Rebuild the coherent 2-channel streamer for Linux (the repo copy is a macOS binary)
RUN rm -f capture_console/native/b210_stream \
 && g++ -std=c++17 -O2 capture_console/native/b210_stream.cpp -o capture_console/native/b210_stream -luhd \
 && mkdir -p capture_console/cal_presets capture_console/phone_logs

# 8099 console + radar (web) · 5080 phone-only HTTP ingest (tunnel this one) · 5005/udp phone on the LAN
EXPOSE 8099 5080 5005/udp
WORKDIR /app/capture_console
CMD ["python3", "app.py"]
