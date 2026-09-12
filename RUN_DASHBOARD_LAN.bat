@echo off
setlocal
cd /d "%~dp0"
title UniFi Dashboard - LAN Test

if not exist ".venv\Scripts\python.exe" (
    echo Run SETUP_WINDOWS.bat first.
    pause
    exit /b 1
)
if not exist "config\Unifi.ini" (
    echo config\Unifi.ini is missing.
    pause
    exit /b 1
)

echo.
echo Starting dashboard for LAN access...
echo Local: http://127.0.0.1:8088
echo.
echo Windows Firewall may ask for access.
echo Allow PRIVATE networks only unless you specifically require otherwise.
echo Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" "app\unifi_dashboard.py" --lan --config "config\Unifi.ini"
pause
