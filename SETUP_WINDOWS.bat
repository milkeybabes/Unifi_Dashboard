@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title UniFi Dashboard - Windows Setup

echo.
echo ============================================================
echo   UniFi Protect-First Dashboard - Windows Setup
echo ============================================================
echo.

call :find_python
if not defined PY_CMD (
    echo Python 3.10 or newer was not found.
    where winget >nul 2>&1
    if errorlevel 1 goto :manual_python
    echo Installing Python 3.12 with winget...
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :manual_python
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    call :find_python
    if not defined PY_CMD goto :rerun
)

call :find_node
if not defined NODE_OK (
    echo Node.js 22.20 or newer was not found.
    where winget >nul 2>&1
    if errorlevel 1 goto :manual_node
    echo Installing/updating Node.js LTS with winget...
    winget upgrade --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements >nul 2>&1
    if errorlevel 1 winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :manual_node
    set "PATH=C:\Program Files\nodejs;%PATH%"
    call :find_node
    if not defined NODE_OK goto :rerun
)

echo.
echo Python:
%PY_CMD% --version
echo Node:
node --version
echo npm:
call npm --version

echo.
echo [1/4] Creating Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
    %PY_CMD% -m venv ".venv"
    if errorlevel 1 goto :fail
) else (
    echo Existing .venv found - keeping it.
)

echo.
echo [2/4] Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [3/4] Installing Node packages from package-lock.json...
call npm ci
if errorlevel 1 goto :fail

echo.
echo [4/4] Preparing configuration...
if not exist "config" mkdir "config"
if not exist "app\logs" mkdir "app\logs"
if not exist "config\Unifi.ini" (
    copy /Y "config\Unifi.ini.example" "config\Unifi.ini" >nul
    echo Created config\Unifi.ini
    set "NEW_CONFIG=1"
) else (
    echo Existing config\Unifi.ini kept unchanged.
)

echo.
echo ============================================================
echo   SETUP COMPLETE
echo ============================================================
echo.
echo For first testing use RUN_DASHBOARD_LOCAL.bat.
echo For phones/tablets on your LAN use RUN_DASHBOARD_LAN.bat.
echo.

if defined NEW_CONFIG (
    echo IMPORTANT: Edit config\Unifi.ini with your UniFi details.
    choice /C YN /N /M "Open Unifi.ini in Notepad now? [Y/N] "
    if errorlevel 2 goto :done
    start "" notepad "%~dp0config\Unifi.ini"
)

:done
pause
exit /b 0

:find_python
set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py -3.12"
        goto :eof
    )
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py -3"
        goto :eof
    )
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
goto :eof

:find_node
set "NODE_OK="
where node >nul 2>&1
if errorlevel 1 goto :eof
node -e "const v=process.versions.node.split('.').map(Number); process.exit(v[0] > 22 || (v[0] === 22 && v[1] >= 20) ? 0 : 1)" >nul 2>&1
if not errorlevel 1 set "NODE_OK=1"
goto :eof

:manual_python
echo.
echo ERROR: Install Python 3.12 or newer, then run this file again.
echo https://www.python.org/downloads/windows/
goto :fail

:manual_node
echo.
echo ERROR: Install Node.js 22.20 or newer, then run this file again.
echo https://nodejs.org/
goto :fail

:rerun
echo.
echo Installation completed, but Windows has not refreshed PATH yet.
echo Close this window and run SETUP_WINDOWS.bat again.
pause
exit /b 2

:fail
echo.
echo Setup did not complete. Correct the error above and run it again.
pause
exit /b 1
