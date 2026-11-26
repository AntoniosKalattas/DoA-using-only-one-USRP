#!/bin/bash
pgrep -x dockerd >/dev/null && echo "dockerd: running" || echo "dockerd: NOT running"
docker ps --format '{{.Names}}  {{.Status}}' 2>/dev/null
echo "usb devices in WSL:"; lsusb 2>/dev/null | grep -i -E "2500|ettus" || echo "  (no USRP attached to WSL)"
curl -s -m 3 localhost:8099/api/usrp && echo
