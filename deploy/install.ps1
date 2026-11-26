# USRP Capture Console - one-line installer for Windows 10/11.
#
#   irm https://github.com/AntoniosKalattas/Thesis-/releases/latest/download/install.ps1 | iex
#
# Downloads the latest Windows bundle from GitHub Releases into %USERPROFILE%\usrp-console,
# keeps your .env (ngrok token) from an earlier install, then runs the setup wizard (setup.ps1):
# Ubuntu for WSL2 -> Docker Engine -> radar container -> B210 over usbipd -> phone link.
$ErrorActionPreference = "Stop"
$Repo   = "AntoniosKalattas/Thesis-"
$Dest   = Join-Path $env:USERPROFILE "usrp-console"
$Bundle = "https://github.com/$Repo/releases/latest/download/usrp-console-windows.zip"

Write-Host "USRP Capture Console installer" -ForegroundColor Cyan
Write-Host "  from: $Bundle"
Write-Host "  into: $Dest"

$tmp = Join-Path $env:TEMP ("usrp-console-" + [guid]::NewGuid().ToString("N") + ".zip")
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Bundle -OutFile $tmp -UseBasicParsing
} catch {
    Write-Host "Download failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Check your internet connection, or download the zip by hand from https://github.com/$Repo/releases/latest"
    return
}

$keepEnv = $null
if (Test-Path (Join-Path $Dest ".env")) { $keepEnv = Get-Content (Join-Path $Dest ".env") -Raw }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Expand-Archive -Path $tmp -DestinationPath $Dest -Force
Remove-Item $tmp -Force
if ($keepEnv) { Set-Content -Path (Join-Path $Dest ".env") -Value $keepEnv -Encoding ascii -NoNewline }

# A desktop shortcut so next time it's one double-click.
try {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "USRP Radar.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $Dest "START-HERE.bat"
    $s.WorkingDirectory = $Dest
    $s.Save()
    Write-Host "Desktop shortcut: USRP Radar" -ForegroundColor Green
} catch {}

Write-Host "Starting the setup wizard..." -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dest "setup.ps1")
