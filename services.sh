#!/usr/bin/env bash
# services.sh: start / stop / check every Thesis service on this Mac.
#
#   ./services.sh                      start everything (same as "start")
#   ./services.sh start [svc...]       start all, or only the named services
#   ./services.sh stop  [svc...]       stop them (capture jobs and b210_stream included)
#   ./services.sh restart [svc...]
#   ./services.sh status               what is running, links, phone URL
#   ./services.sh logs <svc>           follow a service's log
#
# Services
#   console  capture_console/app.py      web 127.0.0.1:8099 · phone HTTP :5080 · phone UDP :5005
#   ngrok    ngrok http 5080             public link for the USRP Beacon phone (API on :4040)
#   demo     demo/app.py                 simulated capture + tuning demo, 127.0.0.1:8777
#   storage  storageSolution/iq_server.py  raw IQ receiver on :9999 -> storageSolution/iq_store
#
# Environment overrides
#   PYTHON=/path/to/python        interpreter (default ~/.venvs/main/bin/python, then python3)
#   SERVICES="console ngrok"      default service set for start/stop/restart
#   STORAGE_HOST=0.0.0.0          let other machines send IQ (default: this Mac only)
#   STORAGE_OUTDIR=/big/disk      where the IQ server writes captures
#
# Safe to run repeatedly: a healthy service is left alone; a leftover copy of one of
# these services that is only half up (the "port 5080 already in use" case) is
# stopped and started again. A port held by anything else is reported and never killed.
# Logs and pid files go in .run/ next to this script.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUN="$ROOT/.run"
LOGS="$RUN/logs"
ALL_SERVICES="console ngrok demo storage"
DEFAULT_SERVICES="${SERVICES:-$ALL_SERVICES}"
STORAGE_HOST="${STORAGE_HOST:-127.0.0.1}"
STORAGE_OUTDIR="${STORAGE_OUTDIR:-$ROOT/storageSolution/iq_store}"
NGROK_API="http://127.0.0.1:4040"
START_TIMEOUT=20

mkdir -p "$LOGS"

if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'
else
  B=; G=; Y=; R=; D=; N=
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
err()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; }
info() { printf '    %s%s%s\n' "$D" "$*" "$N"; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------- interpreter
pick_python() {
  local c
  for c in "${PYTHON:-}" "$HOME/.venvs/main/bin/python" "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && { PY="$c"; return 0; }
  done
  die "no Python found (set PYTHON=/path/to/python)"
}

# ---------------------------------------------------------------- service table
svc_known() { case " $ALL_SERVICES " in *" $1 "*) return 0;; esac; return 1; }

svc_dir() {
  case "$1" in
    console) echo "$ROOT/capture_console" ;;
    demo)    echo "$ROOT/demo" ;;
    storage) echo "$ROOT/storageSolution" ;;
    ngrok)   echo "$ROOT" ;;
  esac
}

svc_script() {
  case "$1" in
    console) echo "$ROOT/capture_console/app.py" ;;
    demo)    echo "$ROOT/demo/app.py" ;;
    storage) echo "$ROOT/storageSolution/iq_server.py" ;;
    ngrok)   echo "" ;;
  esac
}

# Ports the service must own once it is up (tcp:N / udp:N).
svc_ports() {
  case "$1" in
    console) echo "tcp:8099 tcp:5080 udp:5005" ;;
    demo)    echo "tcp:8777" ;;
    storage) echo "tcp:9999" ;;
    ngrok)   echo "tcp:4040" ;;
  esac
}

# Fills the CMD array with the command line for a service.
svc_cmd() {
  case "$1" in
    console) CMD=("$PY" -u "$(svc_script console)") ;;
    demo)    CMD=("$PY" -u "$(svc_script demo)") ;;
    storage) CMD=("$PY" -u "$(svc_script storage)" --host "$STORAGE_HOST" --port 9999 --outdir "$STORAGE_OUTDIR") ;;
    ngrok)   CMD=(ngrok http 5080 --log stdout --log-format logfmt) ;;
  esac
}

# ---------------------------------------------------------------- process helpers
alive()    { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
cmdline()  { ps -o command= -p "$1" 2>/dev/null; }
cwd_of()   { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1; }
pgid_of()  { ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '; }
pidfile()  { echo "$RUN/$1.pid"; }
read_pid() { local f; f="$(pidfile "$1")"; [ -f "$f" ] && tr -dc '0-9' <"$f"; }

port_pids() {  # port_pids tcp|udp N -> pids holding it (listening, for tcp)
  if [ "$1" = tcp ]; then lsof -nP -iTCP:"$2" -sTCP:LISTEN -t 2>/dev/null | sort -u
  else lsof -nP -iUDP:"$2" -t 2>/dev/null | sort -u; fi
}

# Is this pid an instance of service $2 (ours, from this script or started by hand)?
is_instance() {
  local pid="$1" svc="$2" cmd script
  cmd="$(cmdline "$pid")" || return 1
  [ -n "$cmd" ] || return 1
  if [ "$svc" = ngrok ]; then
    case "$cmd" in *ngrok*http*5080*) return 0;; esac
    return 1
  fi
  script="$(svc_script "$svc")"
  case "$cmd" in *"$script"*) return 0;; esac
  # started by hand as "python app.py" from the service folder
  case "$cmd" in *python*" $(basename "$script")"*) [ "$(cwd_of "$pid")" = "$(svc_dir "$svc")" ] && return 0;; esac
  return 1
}

# All pids that are instances of a service: from the pid file and from its ports.
instances() {
  local svc="$1" pid p
  {
    pid="$(read_pid "$svc")"; alive "$pid" && is_instance "$pid" "$svc" && echo "$pid"
    for p in $(svc_ports "$svc"); do
      for pid in $(port_pids "${p%%:*}" "${p##*:}"); do
        is_instance "$pid" "$svc" && echo "$pid"
      done
    done
    if [ "$svc" = ngrok ]; then pgrep -f 'ngrok.*http.*5080' 2>/dev/null; fi
  } | sort -u
}

# Healthy = exactly one instance, and it owns every port of the service.
healthy_pid() {
  local svc="$1" ids p proto port holders
  ids="$(instances "$svc")"
  [ "$(printf '%s\n' "$ids" | grep -c .)" = 1 ] || return 1
  for p in $(svc_ports "$svc"); do
    proto="${p%%:*}"; port="${p##*:}"
    holders="$(port_pids "$proto" "$port")"
    printf '%s\n' "$holders" | grep -qx "$ids" || return 1
  done
  [ "$svc" = ngrok ] && { ngrok_url >/dev/null || return 1; }
  echo "$ids"
}

# Ports of $svc held by processes that are NOT instances of it.
foreign_holders() {
  local svc="$1" p proto port pid
  for p in $(svc_ports "$svc"); do
    proto="${p%%:*}"; port="${p##*:}"
    for pid in $(port_pids "$proto" "$port"); do
      is_instance "$pid" "$svc" || echo "$proto/$port pid $pid: $(cmdline "$pid" | sed 's#^[^ ]*/##' | cut -c1-100)"
    done
  done
}

# Stop one pid: SIGINT (lets the console stop its capture job), then TERM, then KILL.
# Signals the whole process group only when the pid leads its own group (what this
# script starts), so a hand-started copy never takes your terminal's group with it.
stop_pid() {
  local pid="$1" target="$1" i sig
  [ "$(pgid_of "$pid")" = "$pid" ] && target="-$pid"
  for sig in INT TERM KILL; do
    kill -s "$sig" -- "$target" 2>/dev/null || kill -s "$sig" "$pid" 2>/dev/null
    for i in $(seq 1 25); do alive "$pid" || return 0; sleep 0.2; done
  done
  alive "$pid" && return 1 || return 0
}

# Capture jobs the console launched run in their own session (start_new_session),
# so they outlive the console unless stopped here. b210_stream is the one that keeps
# the B210 busy afterwards, so also sweep any orphaned copy from an earlier crash.
stop_capture_jobs() {
  local console_pid="${1:-}" j pid
  if [ -n "$console_pid" ]; then
    for j in $(pgrep -P "$console_pid" 2>/dev/null); do
      kill -TERM -- "-$j" 2>/dev/null || kill -TERM "$j" 2>/dev/null
    done
  fi
  sleep 0.5
  for pid in $(pgrep -f "capture_console/native/b210_stream|live_radar_capture.py|radar_replay.py" 2>/dev/null); do
    [ "$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" = 1 ] || continue
    case "$(cwd_of "$pid")" in "$ROOT"*) ;; *) continue ;; esac
    info "stopping orphaned capture process $pid: $(cmdline "$pid" | cut -c1-80)"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    sleep 1; alive "$pid" && kill -KILL "$pid" 2>/dev/null
  done
}

ngrok_url() {  # prints the https tunnel URL pointing at :5080
  curl -fsS --max-time 2 "$NGROK_API/api/tunnels" 2>/dev/null | "$PY" -c '
import json, sys
try: t = json.load(sys.stdin).get("tunnels", [])
except ValueError: sys.exit(1)
t = [x for x in t if "5080" in str(x.get("config", {}).get("addr", ""))]
t.sort(key=lambda x: not str(x.get("public_url", "")).startswith("https"))
if not t: sys.exit(1)
print(t[0]["public_url"])'
}

# ---------------------------------------------------------------- preflight
preflight() {  # preflight svc -> 0 ok, 2 skip (optional dependency missing), 1 fail
  local svc="$1"
  case "$svc" in
    console|storage)
      "$PY" -c 'import numpy' 2>/dev/null || { err "$svc: numpy missing for $PY"; return 1; } ;;
  esac
  case "$svc" in
    console)
      [ -x "$ROOT/capture_console/native/b210_stream" ] \
        || warn "console: native/b210_stream not built; live B210 capture will fail (see build line in b210_stream.cpp)"
      command -v uhd_find_devices >/dev/null \
        || warn "console: UHD not on PATH; B210 firmware auto-load and capture won't work"
      local p
      for p in $(pgrep -f phone_truth_listener.py 2>/dev/null); do
        warn "console: phone_truth_listener.py (pid $p) also listens on UDP 5005; the two split the phone's packets. Stop it."
      done ;;
    ngrok)
      command -v ngrok >/dev/null || { warn "ngrok: not installed, skipped (phone still works on the same Wi-Fi)"; return 2; }
      ngrok config check >/dev/null 2>&1 && grep -q authtoken "$(ngrok config check 2>/dev/null | sed -n 's/.*file at //p')" 2>/dev/null \
        || { warn "ngrok: no authtoken configured, skipped (run: ngrok config add-authtoken <token>)"; return 2; } ;;
  esac
  return 0
}

# ---------------------------------------------------------------- start / stop
wait_up() {  # wait_up svc pid -> 0 when healthy, 1 when it died or timed out
  local svc="$1" pid="$2" i
  for i in $(seq 1 $((START_TIMEOUT * 4))); do
    alive "$pid" || return 1
    [ "$(healthy_pid "$svc")" = "$pid" ] && return 0
    sleep 0.25
  done
  return 1
}

start_one() {
  local svc="$1" pid foreign log rc
  printf '%s%s%s\n' "$B" "$svc" "$N"

  pid="$(healthy_pid "$svc")"
  if [ -n "$pid" ]; then
    echo "$pid" >"$(pidfile "$svc")"   # adopt a hand-started copy so stop works on it
    ok "already running (pid $pid)"
    return 0
  fi

  preflight "$svc"; rc=$?
  [ $rc = 2 ] && return 0
  [ $rc = 1 ] && return 1

  foreign="$(foreign_holders "$svc")"
  if [ -n "$foreign" ]; then
    err "port in use by another program; not touching it:"
    printf '%s\n' "$foreign" | while read -r l; do info "$l"; done
    return 1
  fi

  # Half-up or duplicate copies of this same service: clear them before starting.
  for pid in $(instances "$svc"); do
    info "stopping stale $svc instance (pid $pid)"
    [ "$svc" = console ] && stop_capture_jobs "$pid"
    stop_pid "$pid" || { err "could not stop pid $pid"; return 1; }
  done
  [ "$svc" = console ] && stop_capture_jobs

  [ "$svc" = storage ] && mkdir -p "$STORAGE_OUTDIR"
  log="$LOGS/$svc.log"
  [ -f "$log" ] && mv -f "$log" "$log.1"
  svc_cmd "$svc"
  info "\$ ${CMD[*]}"

  # Job control gives each service its own process group (clean group kill on stop)
  # and keeps SIGINT deliverable, which the console needs to stop its capture job.
  set -m
  ( cd "$(svc_dir "$svc")" && PYTHONUNBUFFERED=1 exec nohup "${CMD[@]}" >>"$log" 2>&1 </dev/null ) &
  pid=$!
  set +m
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$(pidfile "$svc")"

  if wait_up "$svc" "$pid"; then
    ok "started (pid $pid) · log: ${log#"$ROOT/"}"
    return 0
  fi
  if alive "$pid"; then
    err "did not come up within ${START_TIMEOUT}s; stopping it"
    stop_pid "$pid"
  else
    err "exited during startup"
  fi
  rm -f "$(pidfile "$svc")"
  info "last lines of ${log#"$ROOT/"}:"
  tail -n 15 "$log" 2>/dev/null | sed 's/^/      /'
  return 1
}

stop_one() {
  local svc="$1" ids pid failed=0
  printf '%s%s%s\n' "$B" "$svc" "$N"
  ids="$(instances "$svc")"
  if [ -z "$ids" ]; then
    [ "$svc" = console ] && stop_capture_jobs
    ok "not running"
    rm -f "$(pidfile "$svc")"
    return 0
  fi
  for pid in $ids; do
    [ "$svc" = console ] && stop_capture_jobs "$pid"
    if stop_pid "$pid"; then ok "stopped (pid $pid)"; else err "pid $pid would not stop"; failed=1; fi
  done
  [ "$svc" = console ] && stop_capture_jobs
  rm -f "$(pidfile "$svc")"
  return $failed
}

# ---------------------------------------------------------------- status
status_all() {
  local svc pid lan url
  for svc in $ALL_SERVICES; do
    pid="$(healthy_pid "$svc")"
    if [ -n "$pid" ]; then
      printf '  %s●%s %-8s running  pid %s\n' "$G" "$N" "$svc" "$pid"
    elif [ -n "$(instances "$svc")" ]; then
      printf '  %s●%s %-8s DEGRADED pid %s (not holding all of: %s); run: %s restart %s\n' \
        "$Y" "$N" "$svc" "$(instances "$svc" | tr '\n' ' ')" "$(svc_ports "$svc")" "$0" "$svc"
    else
      printf '  %s○%s %-8s stopped\n' "$D" "$N" "$svc"
    fi
  done
  echo
  healthy_pid console >/dev/null && {
    echo "  Console      http://127.0.0.1:8099"
    echo "  Live radar   http://127.0.0.1:8099/radar"
    echo "  Calibration  http://127.0.0.1:8099/calibrate"
    lan="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
    [ -n "$lan" ] && echo "  Phone (Wi-Fi) http://$lan:5080   ·  UDP $lan:5005"
    if /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null | grep -q enabled; then
      local real; real="$("$PY" -c 'import os,sys;print(os.path.realpath(sys.executable))')"
      /usr/libexec/ApplicationFirewall/socketfilterfw --getappblocked "$real" 2>/dev/null | grep -q permitted \
        || warn "macOS firewall may block the phone on Wi-Fi: allow $real in System Settings → Network → Firewall"
    fi
  }
  url="$(ngrok_url)" && echo "  Phone (ngrok) $url   (inspector http://127.0.0.1:4040)"
  healthy_pid demo    >/dev/null && echo "  Demo         http://127.0.0.1:8777"
  healthy_pid storage >/dev/null && echo "  IQ storage   $STORAGE_HOST:9999 → ${STORAGE_OUTDIR#"$ROOT/"}"
  return 0
}

# ---------------------------------------------------------------- main
usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; }

pick_python
action="${1:-start}"; [ $# -gt 0 ] && shift
targets="${*:-$DEFAULT_SERVICES}"
for s in $targets; do svc_known "$s" || die "unknown service '$s' (known: $ALL_SERVICES)"; done

# Start order matters: ngrok tunnels to the console. Stop in reverse.
order() { local s want=" $1 " out=""; for s in $2; do case "$want" in *" $s "*) out="$out $s";; esac; done; echo $out; }
start_order="$(order "$targets" "console ngrok demo storage")"
stop_order="$(order "$targets" "storage demo ngrok console")"

rc=0
case "$action" in
  start)
    for s in $start_order; do start_one "$s" || rc=1; done
    echo; status_all ;;
  stop)
    for s in $stop_order; do stop_one "$s" || rc=1; done ;;
  restart)
    for s in $stop_order;  do stop_one "$s"  || rc=1; done
    for s in $start_order; do start_one "$s" || rc=1; done
    echo; status_all ;;
  status)
    status_all ;;
  logs)
    [ $# -eq 1 ] || die "usage: $0 logs <service>"
    [ -f "$LOGS/$1.log" ] || die "no log yet for $1"
    exec tail -n 50 -F "$LOGS/$1.log" ;;
  -h|--help|help)
    usage ;;
  *)
    usage; exit 2 ;;
esac
exit $rc
