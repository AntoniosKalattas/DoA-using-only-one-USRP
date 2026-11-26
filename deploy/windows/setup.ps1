# USRP Capture Console - one-click start for Windows 10/11 (no Docker Desktop needed).
# Double-click START-HERE.bat.
#
# How it works: Docker Engine runs inside the Ubuntu WSL2 distro, the B210 is forwarded into
# WSL with usbipd-win, and the radar is reachable on http://localhost:8099/radar.
# First run installs what's missing (Ubuntu for WSL, Docker Engine in it, usbipd-win);
# Windows shows "Allow changes?" prompts for the parts that need admin rights.

$ErrorActionPreference = "Continue"   # PS 5.1: "Stop" turns any stderr from wsl/docker into a crash
Set-Location -Path $PSScriptRoot
Write-Host "Don't click inside this window while it runs: Windows pauses it (the title then starts with 'Select'). Press Esc if that happens." -ForegroundColor DarkGray
$Host.UI.RawUI.WindowTitle = "USRP Capture Console"
$env:WSL_UTF8 = "1"
$Distro = "Ubuntu"
$W = "/mnt/" + $PSScriptRoot.Substring(0, 1).ToLower() + ($PSScriptRoot.Substring(2) -replace "\\", "/")  # WSL path of this folder

function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Done($code) { Write-Host ""; Read-Host "Press Enter to close"; exit $code }
function InUbuntu($cmd) { wsl -d $Distro -u root -- bash -c $cmd }
function Usbipd { $p = "$env:ProgramFiles\usbipd-win\usbipd.exe"; if (Test-Path $p) { return $p }; $c = Get-Command usbipd -ErrorAction SilentlyContinue; if ($c) { return $c.Source }; return $null }

# ---------------------------------------------------------------- 1. Ubuntu in WSL2
Step "1/5  Ubuntu (WSL2)"
$distros = (wsl -l -q) -replace "`0", "" | Where-Object { $_.Trim() }
if (-not ($distros -contains $Distro)) {
    Say "Installing Ubuntu for WSL2 (Windows asks for permission; may need a restart)..." Yellow
    Start-Process wsl -ArgumentList "--install -d $Distro --no-launch" -Verb RunAs -Wait
    Say "If Windows asked to restart, restart and run START-HERE.bat again." Yellow
    Done 0
}
Say "Ubuntu: OK" Green

# ---------------------------------------------------------------- 2. Docker Engine inside Ubuntu
Step "2/5  Docker Engine inside Ubuntu"
Get-ChildItem "$PSScriptRoot\wsl\*.sh" | ForEach-Object { InUbuntu "sed -i 's/\r$//' '$W/wsl/$($_.Name)'" }
InUbuntu "command -v docker >/dev/null && docker compose version >/dev/null 2>&1"
if ($LASTEXITCODE -ne 0) {
    Say "Installing Docker Engine in Ubuntu (first time, ~1 minute)..." Yellow
    InUbuntu "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq docker.io docker-compose-v2 usbutils >/tmp/apt.log 2>&1 || tail -20 /tmp/apt.log"
}
# Keep Ubuntu (and dockerd + the radar) running: WSL stops an idle distro a few seconds after
# the last command, which would also stop the radar and break the USB link.
$alive = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -match "keepalive.sh" }
if (-not $alive) {
    Start-Process -WindowStyle Hidden -FilePath "wsl.exe" -ArgumentList "-d $Distro -u root -- bash $W/wsl/keepalive.sh"
    Start-Sleep 6
}
Say "Docker: running" Green

# ---------------------------------------------------------------- 3. ngrok token (optional)
Step "3/5  Phone link (ngrok)"
$token = ""
if (Test-Path .env) {
    $m = Select-String -Path .env -Pattern '^NGROK_AUTHTOKEN=(.+)$'
    if ($m) { $token = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $token -and -not (Test-Path .env)) {
    Say "Paste your ngrok authtoken for the phone link (https://dashboard.ngrok.com/get-started/your-authtoken),"
    Say "or press Enter to skip (the phone can then use UDP on the same Wi-Fi):"
    $token = (Read-Host "ngrok token").Trim()
    "NGROK_AUTHTOKEN=$token" | Set-Content -Encoding ascii .env
}
if ($token) { Say "ngrok: configured" Green } else { Say "ngrok: skipped (delete .env to be asked again)" Yellow }

# ---------------------------------------------------------------- 4. start the console
Step "4/5  Starting the radar"
InUbuntu "bash $W/wsl/start-console.sh"
if ($LASTEXITCODE -ne 0) { Say "Starting the console failed (see above)." Red; Done 1 }

# ---------------------------------------------------------------- 5. USRP B210 -> WSL
Step "5/5  Connecting the USRP B210"
$u = Usbipd
if (-not $u) {
    Say "Installing usbipd-win (Windows asks for permission)..." Yellow
    Start-Process winget -ArgumentList "install -e --id dorssel.usbipd-win --accept-package-agreements --accept-source-agreements --silent" -Wait
    $u = Usbipd
}
if (-not $u) {
    Say "usbipd-win is not installed, so the B210 can't be connected. Install it and run again." Red
} else {
    $line = & $u list | Select-String -Pattern "2500:"
    if (-not $line -and (& $u list | Select-String -Pattern "Descriptor Request Failed")) {
        Say "Windows can't read a USB device (Device Descriptor Request Failed). Unplug the B210, wait 5 s, plug it back in, then run START-HERE.bat again." Red
    } elseif (-not $line) {
        Say "No USRP found (USB ID 2500). Plug the B210 into a USB 3 (blue) port and run START-HERE.bat again." Yellow
    }
    # The USB link window watches for the USRP on any port, before and after its firmware
    # reboot, and shares + attaches it. Sharing needs admin: ONE "Yes" prompt per start.
    $running = Get-Process | Where-Object { $_.MainWindowTitle -eq "USRP USB link (keep open)" }
    if ($running) {
        Say "USB link already running" Green
    } else {
        Say "Starting the USB link (Windows asks for permission once)..." Yellow
        Start-Process powershell -Verb RunAs -WindowStyle Minimized -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\usb-link.ps1`""
        Start-Sleep 10
    }
    $now = & $u list | Select-String -Pattern "2500:"
    if ($now -match "Attached") { Say "USRP forwarded to Linux (keep the minimized 'USRP USB link' window open)" Green }
    elseif ($line) { Say "USRP found; the USB link is still attaching it (give it ~10 s)" Yellow }
}

# ---------------------------------------------------------------- done
Start-Sleep 3
$usb = $null
try { $usb = Invoke-RestMethod http://localhost:8099/api/usrp -TimeoutSec 5 } catch {}
Say ""
Say "Radar:  http://localhost:8099/radar" Green
if ($usb -and $usb.connected) { Say ("USRP:   " + $usb.product + " on " + $usb.usb + " (" + $usb.rate + ")") Green }
elseif ($usb) { Say "USRP:   not visible in the radar yet (give it ~10 s and refresh)" Yellow }
if ($token) {
    $url = $null
    for ($i = 0; $i -lt 10 -and -not $url; $i++) {
        try { $url = (Invoke-RestMethod http://localhost:4040/api/tunnels -TimeoutSec 3).tunnels[0].public_url } catch { Start-Sleep 2 }
    }
    if ($url) {
        Say ""; Say "PHONE LINK (paste into the app's 'Laptop IP address or link'):" Cyan; Say "   $url" Cyan
        Set-Clipboard -Value $url; Say "   (copied to the clipboard)"
    }
}
Start-Process "http://localhost:8099/radar"
Say ""; Say "Stop with stop.bat. Run START-HERE.bat again any time." White
Done 0
