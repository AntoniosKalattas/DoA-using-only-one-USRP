# USRP Capture Console on Windows (plug and play)

Fastest install (PowerShell): `irm https://github.com/AntoniosKalattas/Thesis-/releases/latest/download/install.ps1 | iex`

## Use it
1. Plug the **USRP B210** into a **USB 3 (blue) port**.
2. Double-click **`START-HERE.bat`**.
3. The radar opens at **http://localhost:8099/radar**.

The **first time** with the B210 plugged in, Windows asks once for permission to share it with
Linux (click **Yes**). It may also ask for your **ngrok token** for the phone link
(https://dashboard.ngrok.com/get-started/your-authtoken). Press Enter to skip; the phone can
then use UDP on the same Wi-Fi. With a token it prints the phone link and copies it to the clipboard.

Stop everything with **`stop.bat`**. Calibrations and phone logs are kept between runs.

## How it works (no Docker Desktop)
- **Docker Engine runs inside the Ubuntu WSL2 distro.** A hidden window keeps Ubuntu and Docker
  running, because this WSL version has no systemd.
- **The radar runs in the `usrp-console` container** (Ubuntu 24.04, Python 3 + numpy, UHD 4.6 +
  B210 firmware, UHD Python bindings, the B210 streamer, the console/radar/calibration code).
- **usbipd-win forwards the B210 from Windows into WSL.** The B210 reboots itself when UHD loads
  its firmware, so a minimized **"USRP USB link (keep open)"** window re-attaches it. Leave it open.

First run on a new PC installs whatever is missing: Ubuntu for WSL, Docker Engine inside it, and
usbipd-win. Windows asks for permission for those.

## Files
| File | What it is |
|---|---|
| `START-HERE.bat` → `setup.ps1` | everything: install if needed, start Docker, start radar, connect B210 |
| `stop.bat` | stops the radar, the USB link and the background Ubuntu |
| `wsl/` | small Linux scripts the launcher runs inside Ubuntu |
| `usrp-console-image.tar.gz` | *(optional, offline only)* the Docker image; without it the image is pulled from `ghcr.io/antonioskalattas/usrp-console` |
| `docker-compose.yml` | ports, USB access, saved calibrations |
| phone app | `UsrpBeacon.apk` from the [latest release](https://github.com/AntoniosKalattas/Thesis-/releases/latest) |
| source | the [GitHub repo](https://github.com/AntoniosKalattas/Thesis-) |

## If something doesn't work
- Radar says **"USRP: not connected"**: run START-HERE.bat again with the B210 plugged in, and
  check the minimized "USRP USB link" window is open.
- **"USB 2.0 ⚠"**: use a USB 3 port. USB over usbipd is slower than native, so some overflows
  (`O` in the log) are normal.
- Status from inside Ubuntu: `wsl -d Ubuntu -u root -- bash wsl/status.sh`
- Logs: `wsl -d Ubuntu -u root -- docker logs -f usrp-console`

## Security
The radar (8099) only listens on this PC; it can start capture scripts, so it's never exposed.
The ngrok link only reaches the phone endpoint (telemetry + calibration). Keep it private.
