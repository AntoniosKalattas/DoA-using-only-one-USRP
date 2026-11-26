# Keeps every USRP (USB vendor 2500) attached to WSL, whatever port it's on.
# The B210 shows up as TWO different USB devices: "WestBridge" (bootloader) and, after UHD
# loads its firmware, "USRP B200/B210" - often on a new bus id. Windows needs each one shared
# (needs admin, so this window runs as administrator) and attached again after every reboot.
# Started minimized by setup.ps1. Leave it open while using the radar.
$Host.UI.RawUI.WindowTitle = "USRP USB link (keep open)"
$u = "$env:ProgramFiles\usbipd-win\usbipd.exe"
Write-Host "Watching for the USRP and forwarding it to WSL. Keep this window open." -ForegroundColor Green
$last = ""
while ($true) {
    $rows = & $u list 2>$null | Select-String -Pattern "^\s*\d+-\d+\s+2500:"
    foreach ($r in $rows) {
        $t = $r.ToString().Trim()
        $busid = ($t -split "\s+")[0]
        if ($t -match "Not shared") {
            Write-Host "$(Get-Date -f HH:mm:ss)  sharing $busid  ($t)"
            & $u bind --busid $busid --force 2>&1 | Out-Null
            Start-Sleep 1
            $t = (& $u list 2>$null | Select-String -Pattern "^\s*$busid\s").ToString()
        }
        if ($t -notmatch "Attached") {
            Write-Host "$(Get-Date -f HH:mm:ss)  attaching $busid to WSL"
            & $u attach --wsl --busid $busid 2>&1 | ForEach-Object { if ($_ -match "error") { Write-Host "   $_" -ForegroundColor Yellow } }
        }
    }
    $now = ($rows | ForEach-Object { $_.ToString().Trim() }) -join " | "
    if ($now -ne $last) { Write-Host "$(Get-Date -f HH:mm:ss)  $(if ($now) { $now } else { 'no USRP plugged in' })" -ForegroundColor DarkGray; $last = $now }
    Start-Sleep 2
}
