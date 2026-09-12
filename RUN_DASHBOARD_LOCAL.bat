@echo off
setlocal
cd /d "%~dp0"
title UniFi Dashboard - Local Test

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
echo Starting dashboard for THIS PC only...
echo Open http://127.0.0.1:8088
echo Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" "app\unifi_dashboard.py" --config "config\Unifi.ini"
pause
