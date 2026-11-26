@echo off
REM Stops the radar (and ngrok), the USB link window and the background Ubuntu/Docker.
set WSL_UTF8=1
wsl -d Ubuntu -u root -- sh -c "docker ps -q --filter name=usrp-console | xargs -r docker stop"
taskkill /fi "WINDOWTITLE eq USRP USB link (keep open)" >nul 2>&1
wsl -t Ubuntu >nul 2>&1
echo Stopped.
pause
