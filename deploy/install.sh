#!/usr/bin/env bash
# USRP Capture Console - setup wizard for macOS and Linux (native, no Docker).
#
#   curl -fsSL https://github.com/AntoniosKalattas/DoA-using-only-one-USRP/releases/latest/download/install.sh | bash
#   ./deploy/install.sh            (from a checkout: uses that checkout instead of downloading)
#   ... | bash -s -- --yes         (accept every default, no questions)
#
# Steps: 1 code  2 UHD driver + B210 firmware  3 Python env  4 B210 streamer  5 ngrok (optional)
#        6 launcher  7 phone app link. Safe to run again: it updates and re-checks.
set -euo pipefail

REPO="AntoniosKalattas/DoA-using-only-one-USRP"
DEST="${USRP_CONSOLE_DIR:-$HOME/usrp-console}"
YES=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --dir=*) DEST="${a#--dir=}" ;;
    -h|--help) sed -n '2,10p' "$0" 2>/dev/null || true; exit 0 ;;
  esac
done

c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_off=$'\033[0m'
step() { printf '\n%s== %s%s\n' "$c_cyan" "$1" "$c_off"; }
ok()   { printf '%s  ok%s  %s\n' "$c_green" "$c_off" "$1"; }
warn() { printf '%s  !%s   %s\n' "$c_yellow" "$c_off" "$1"; }
die()  { printf '%s  x%s   %s\n' "$c_red" "$c_off" "$1"; exit 1; }
# Questions read from the terminal, so they still work when this script is piped into bash.
ask() {   # ask "question" default -> echoes answer
  local q="$1" def="${2:-}" ans=""
  if [ "$YES" = 1 ] || [ ! -r /dev/tty ]; then echo "$def"; return; fi
  printf '%s [%s]: ' "$q" "$def" >/dev/tty; IFS= read -r ans </dev/tty || true
  echo "${ans:-$def}"
}
yes_no() { case "$(ask "$1 (y/n)" "${2:-y}")" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac; }

OS="$(uname -s)"
case "$OS" in Darwin) PLATFORM=mac ;; Linux) PLATFORM=linux ;; *) die "unsupported OS: $OS (Windows: use install.ps1)" ;; esac
printf '%sUSRP Capture Console setup%s  (%s, into %s)\n' "$c_cyan" "$c_off" "$PLATFORM" "$DEST"

# ------------------------------------------------------------------ 1. code
step "1/7  Code"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/../capture_console/app.py" ]; then
  ROOT="$(cd "$SELF_DIR/.." && pwd)"
  ok "using this checkout: $ROOT"
else
  ROOT="$DEST"
  mkdir -p "$ROOT"
  url="https://github.com/$REPO/releases/latest/download/usrp-console-src.tar.gz"
  curl -fsSL "$url" -o /tmp/usrp-console-src.tar.gz || die "download failed: $url"
  # keep local data (presets, recordings, logs) - the archive has code only
  tar -xzf /tmp/usrp-console-src.tar.gz -C "$ROOT" --strip-components=1
  rm -f /tmp/usrp-console-src.tar.gz
  ok "latest release unpacked into $ROOT"
fi
CC="$ROOT/capture_console"

# ------------------------------------------------------------------ 2. UHD + firmware
step "2/7  UHD driver (USRP B210)"
if ! command -v uhd_find_devices >/dev/null 2>&1; then
  if [ "$PLATFORM" = mac ]; then
    command -v brew >/dev/null 2>&1 || die "Homebrew is needed: https://brew.sh"
    yes_no "UHD is missing. Install it with Homebrew (brew install uhd boost)?" y && brew install uhd boost
  else
    yes_no "UHD is missing. Install it with apt (needs sudo)?" y && \
      sudo apt-get update -qq && sudo apt-get install -y uhd-host libuhd-dev libboost-dev g++ python3-venv
  fi
fi
command -v uhd_find_devices >/dev/null 2>&1 && ok "UHD $(uhd_config_info --version 2>/dev/null | head -1 || echo installed)" \
  || warn "UHD not installed: the radar UI works, live capture needs UHD"
if command -v uhd_images_downloader >/dev/null 2>&1; then
  uhd_images_downloader -t b2xx >/dev/null 2>&1 && ok "B210 firmware/FPGA images present" || warn "could not fetch B210 images (offline?)"
fi
if [ "$PLATFORM" = linux ] && [ ! -f /etc/udev/rules.d/uhd-usrp.rules ] && [ -f /usr/lib/uhd/utils/uhd-usrp.rules ]; then
  yes_no "Install the USB rule so the B210 works without sudo?" y && \
    sudo cp /usr/lib/uhd/utils/uhd-usrp.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger
fi

# ------------------------------------------------------------------ 3. Python
step "3/7  Python environment"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10+ needed (found $($PY -V))"
[ -d "$ROOT/.venv" ] || "$PY" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q -r "$CC/requirements.txt"
ok "venv $ROOT/.venv ($("$ROOT/.venv/bin/python" -V), numpy $("$ROOT/.venv/bin/python" -c 'import numpy; print(numpy.__version__)'))"

# ------------------------------------------------------------------ 4. streamer
step "4/7  B210 streamer (native/b210_stream)"
if command -v uhd_find_devices >/dev/null 2>&1; then
  cd "$CC/native"
  if [ "$PLATFORM" = mac ]; then
    clang++ -std=c++17 -O2 b210_stream.cpp -o b210_stream \
      -I"$(brew --prefix uhd)/include" -I"$(brew --prefix boost)/include" \
      -L"$(brew --prefix uhd)/lib" -luhd
  else
    g++ -std=c++17 -O2 b210_stream.cpp -o b210_stream -luhd
  fi
  cd - >/dev/null
  ok "built $CC/native/b210_stream"
  if uhd_find_devices 2>/dev/null | grep -qi "b2"; then ok "B210 found on USB"; else warn "no B210 plugged in right now (fine; plug it into a USB 3 port later)"; fi
else
  warn "skipped (needs UHD)"
fi

# ------------------------------------------------------------------ 5. ngrok
step "5/7  Phone link over the internet (ngrok, optional)"
NGROK=0
if command -v ngrok >/dev/null 2>&1; then
  if ngrok config check >/dev/null 2>&1 && grep -q authtoken "$(ngrok config check 2>/dev/null | sed -n 's/.*file at //p')" 2>/dev/null; then
    NGROK=1; ok "ngrok configured"
  else
    tok="$(ask "Paste your ngrok authtoken (https://dashboard.ngrok.com/get-started/your-authtoken), Enter to skip" "")"
    if [ -n "$tok" ]; then ngrok config add-authtoken "$tok" >/dev/null && NGROK=1 && ok "ngrok token saved"; fi
  fi
else
  warn "ngrok not installed: the phone can still use UDP on the same Wi-Fi ($( [ "$PLATFORM" = mac ] && echo 'brew install ngrok' || echo 'see https://ngrok.com/download'))"
fi

# ------------------------------------------------------------------ 6. launcher
step "6/7  Launcher"
cat > "$ROOT/start-console.sh" <<EOF
#!/usr/bin/env bash
# Starts the Capture Console (radar: http://127.0.0.1:8099/radar) and, if set up, the ngrok phone link.
cd "$CC"
if [ "$NGROK" = 1 ] && ! pgrep -f "ngrok http 5080" >/dev/null; then
  nohup ngrok http 5080 --log=stdout >"$ROOT/ngrok.log" 2>&1 &
fi
exec "$ROOT/.venv/bin/python" app.py
EOF
chmod +x "$ROOT/start-console.sh"
ok "start any time with: $ROOT/start-console.sh"

# ------------------------------------------------------------------ 7. phone
step "7/7  Phone app"
APK="https://github.com/$REPO/releases/latest/download/UsrpBeacon.apk"
echo "  Android: open this on the phone and install UsrpBeacon.apk:"
echo "    $APK"
command -v qrencode >/dev/null 2>&1 && qrencode -t ANSIUTF8 "$APK" || true
echo "  iPhone: build ios/UsrpBeacon in Xcode (see ios/UsrpBeacon/README.md)."

printf '\n%sDone.%s Radar: http://127.0.0.1:8099/radar\n' "$c_green" "$c_off"
if [ "${USRP_NO_START:-0}" != 1 ] && yes_no "Start the console now?" y; then exec "$ROOT/start-console.sh"; fi
