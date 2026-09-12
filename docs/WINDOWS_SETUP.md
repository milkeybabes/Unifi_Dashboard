# Windows test / development setup

The recommended workflow is to get the dashboard working on a normal Windows PC first, then move it to Docker/NAS later.

## One-time setup

Double-click:

```text
SETUP_WINDOWS.bat
```

It:

- checks for Python
- checks for Node.js 22.20+
- attempts to install missing Python/Node through `winget`
- creates `.venv`
- installs the pinned Python requirements
- runs `npm ci`
- creates `config\Unifi.ini` from the example if required

Python packages remain inside the project virtual environment rather than being installed globally.

## First test

Use:

```text
RUN_DASHBOARD_LOCAL.bat
```

Browse to:

```text
http://127.0.0.1:8088
```

## Test from phone / iPad / another PC

Use:

```text
RUN_DASHBOARD_LAN.bat
```

Browse to:

```text
http://WINDOWS-PC-IP:8088
```

If Windows Firewall prompts, normally allow Private networks only.

## Diagnostics

Run:

```text
CHECK_WINDOWS_SETUP.bat
```

For native video:

```text
http://127.0.0.1:8088/api/live/diagnostics
```

Event log:

```text
app\logs\unifi-dashboard.log
```

## Camera codec note

If iPhone/iPad reports a camera as:

```text
SNAPSHOT · codec unsupported: av01...
```

set that camera's UniFi Protect Recording Quality / Encoding to **Standard** and reload the dashboard.

This keeps the dashboard free of transcoding software.


## G6 Entry package camera

The completed dashboard supports the G6 Entry's second/package camera.

The main camera remains the normal tile view. A small `PACKAGE` live inset appears on the G6 Entry tile, and tapping that inset enlarges the package camera.

The tested G6 Entry maps the package camera to Protect secondary `lens 2`.

If a camera reports `av01` as unsupported on iPhone/iPad, change that camera's Protect Encoding setting to **Standard**.
