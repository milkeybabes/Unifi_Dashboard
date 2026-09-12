@echo off
setlocal
cd /d "%~dp0"
title UniFi Dashboard - Setup Check

echo.
echo [Python environment]
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version
    ".venv\Scripts\python.exe" -c "import flask, flask_sock, requests, urllib3, websocket; print('Python modules: OK')"
) else (
    echo MISSING: .venv - run SETUP_WINDOWS.bat
)

echo.
echo [Node]
where node >nul 2>&1
if errorlevel 1 (
    echo MISSING: node
) else (
    node --version
)

echo.
echo [Node packages]
if exist "node_modules\unifi-protect\package.json" (
    node -e "console.log('unifi-protect:', require('./node_modules/unifi-protect/package.json').version)"
) else (
    echo MISSING: node_modules - run SETUP_WINDOWS.bat
)
if exist "node_modules\ws\package.json" (
    node -e "console.log('ws:', require('./node_modules/ws/package.json').version)"
)

echo.
echo [Configuration]
if exist "config\Unifi.ini" (
    echo config\Unifi.ini: FOUND
    findstr /C:"YOUR_UNIFI_INTEGRATION_API_KEY" /C:"YOUR_LOCAL_PROTECT_USERNAME" /C:"YOUR_LOCAL_PROTECT_PASSWORD" "config\Unifi.ini" >nul
    if not errorlevel 1 (
        echo WARNING: configuration still contains example placeholders.
    ) else (
        echo Configuration placeholders: none detected.
    )
) else (
    echo MISSING: config\Unifi.ini
)

echo.
pause
