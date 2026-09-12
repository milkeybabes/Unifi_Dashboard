# UniFi Protect-First Dashboard

A small, LAN-focused custom dashboard for UniFi Network + Protect.

It was built as a hobby project to put the information and controls that matter most on one screen: cameras, UP Sense data, SuperLink relays, and a compact Network drawer.

> **Unofficial project.** This is not a Ubiquiti product and is not affiliated with Ubiquiti.

## Features

- Protect-first dashboard layout
- Native Protect live video in the browser
- Automatic snapshot fallback
- iPhone / iPad / desktop responsive layouts
- Full-screen camera view
- G6 Entry dual-camera support: live package-camera inset plus tap-to-enlarge package view
- Optional single remote Protect camera as a cloud snapshot tile
- UP Sense temperature, humidity, light, battery and contact state
- One or many SuperLink Relays, each shown by its real relay name
- Large touch-friendly relay output controls
- UniFi Network status and attached-client drawer
- Special handling for 5G/LTE backup devices
- Windows test/development setup
- Docker/NAS deployment
- No FFmpeg, go2rtc or video transcoding required

## Architecture

```text
Browser / touchscreen
        |
        | HTTP + WebSocket
        v
Python / Flask dashboard :8088
        |
        +-- UniFi Network Integration API
        +-- UniFi Protect Integration API
        +-- relay controls / sensors / snapshots
        |
        +-- private Node.js helper on 127.0.0.1
        |       |
        |       +-- local Protect login
        |       +-- native fragmented-MP4 livestream
        |
        +-- optional Ubiquiti cloud connector
                |
                +-- one explicitly selected remote Protect snapshot camera
```

The browser never connects directly to the private Node helper.

## Quick start — Windows

Windows is the easiest place to experiment with the code first.

1. Download or clone the repository.
2. Double-click `SETUP_WINDOWS.bat`.
3. Edit `config\Unifi.ini`.
4. Start with `RUN_DASHBOARD_LOCAL.bat`.
5. Browse to `http://127.0.0.1:8088`.

When you want an iPhone/iPad or another PC to connect, use:

```text
RUN_DASHBOARD_LAN.bat
```

Then browse to:

```text
http://WINDOWS-PC-IP:8088
```

If Windows Firewall prompts, normally allow **Private networks only**.

## Quick start — Docker / NAS

Copy the example configuration:

```bash
cp config/Unifi.ini.example config/Unifi.ini
```

Edit `config/Unifi.ini`, then:

```bash
docker compose up -d --build
```

Check it:

```bash
docker compose ps
docker compose logs --tail=100
```

Open:

```text
http://DOCKER-HOST-IP:8088
```

The Compose file uses `restart: unless-stopped`, so the dashboard returns automatically after a normal server/NAS reboot.

See `docs/DOCKER_SETUP.md` for the full deployment notes.

## Configuration

```ini
DeviceIP=192.168.1.1
API_Key=YOUR_UNIFI_INTEGRATION_API_KEY

Protect_User=YOUR_LOCAL_PROTECT_USERNAME
Protect_Password=YOUR_LOCAL_PROTECT_PASSWORD

# Optional: one remote Protect snapshot camera
# Remote_API_Key=YOUR_UI_CLOUD_API_KEY
# Remote_Camera_Name=Front Garden
```

`API_Key` is used for the local UniFi Integration APIs.

`Protect_User` / `Protect_Password` are used only by the private native-live-video helper. Use a **local UniFi account** and give it only the permissions you actually need.

Never commit the real `config/Unifi.ini`.

## iPhone / iPad live video

The dashboard supports Apple's WebKit media path as well as normal desktop MediaSource playback.

Practical camera compatibility points discovered during testing:

> If an iPhone/iPad camera tile reports `codec unsupported: av01...`, change that camera's **Protect Recording Quality → Encoding** to **Standard**.

A G6 Instant using Advanced/AV1 fell back to snapshots on iPad; changing the camera to Standard made native live video work immediately.

### G6 Entry package camera

The G6 Entry exposes a second, downward-facing package camera. The tested device reports `hasPackageCamera: true`, and the dashboard displays it as a small live inset on the main G6 Entry tile. Tapping the inset enlarges the package view.

On the tested G6 Entry, the package livestream is Protect secondary **lens 2**. The package stream is 3264×2448 at 3 FPS, which is appropriate for checking whether a parcel has been left at the door.

The dashboard does **not** transcode video, so browser codec support still matters.

## Native-video diagnostics

While the dashboard is running:

```text
http://HOST-IP:8088/api/live/diagnostics
```

A healthy helper reports values similar to:

```json
{
  "helper_exit_code": null,
  "helper_running": true,
  "live_enabled": true
}
```

The dashboard event log is written beside the Python application in `app/logs/` on Windows and is volume-mounted to `./logs/` in Docker.



## Optional remote snapshot camera

The dashboard supports **one optional remote Protect camera as a snapshot-only tile**. This is useful for filling a fourth camera position with a camera at another UniFi console without trying to turn the dashboard into a remote live-video service.

The feature uses Ubiquiti's cloud connector from the dashboard server. The cloud API key remains server-side and is never sent to the browser.

Add to your private `Unifi.ini`:

```ini
Remote_API_Key=YOUR_UI_CLOUD_API_KEY
Remote_Camera_Name=Front Garden
```

`Remote_Camera_ID` can be used instead of the name. `Remote_Console_ID` is optional and is only needed to disambiguate matching cameras across multiple consoles.

If `Remote_API_Key` is absent, the dashboard makes **no remote-cloud requests and adds no remote tile**. If a key exists but no camera name/ID is configured, the feature is also skipped rather than guessing which camera to show.

The remote tile refreshes approximately every **10 seconds**, can be tapped to enlarge like a local camera, and does not attempt native live video. For remote live viewing, the official UniFi applications are the better tool.

For accessing this dashboard itself away from home, use a trusted VPN rather than exposing the dashboard port directly to the public Internet.

## Updating an existing Docker / NAS installation

If you already have a working container, **do not blindly replace the whole project directory**.

For a normal code-only upgrade:

1. Back up the existing project folder.
2. Replace `app/unifi_dashboard.py` and `app/unifi_live_bridge.mjs`.
3. Keep your real `config/Unifi.ini`, logs and proven Compose settings.
4. Run:

```bash
docker compose build
docker compose up -d
```

Simply copying the Python/Node files into the NAS project folder is not enough: those files are copied into the Docker image during `docker compose build`.

Older installs may still expect a filename such as `unifi_dashboard_v3.py`. In that case, either copy the new dashboard over the filename your existing Dockerfile/start script expects, or migrate those scripts to the current `unifi_dashboard.py` name.

See [`docs/UPGRADING.md`](docs/UPGRADING.md) for the complete safe-update, verification and rollback procedure.

## Security

This project is designed for a trusted LAN.

- Do not expose port `8088` directly to the Internet.
- The UI can operate physical relay outputs.
- Anyone who can reach the dashboard may be able to operate those controls.
- Keep `Unifi.ini` private.
- Use minimal UniFi permissions.
- For remote access, use a properly secured VPN or another trusted private-access method.

See `SECURITY.md`.

## Notes on APIs

Most status/control functions use the local UniFi Network and Protect Integration APIs.

Native live video uses the community `unifi-protect` Node package and a private/undocumented Protect livestream interface. That makes the live-video portion more likely to need maintenance after future Protect updates.

## Tested setup

The completed dashboard was tested with:

- UniFi Network 10.6.101
- UniFi Protect 7.2.105
- Windows development/testing
- ASUSTOR AS6404T Docker host
- Desktop Chrome
- iPhone
- iPad landscape
- multiple SuperLink Relays
- UP Sense
- G4 Instant / G6 Instant cameras
- G6 Entry main + package camera
- Remote Protect snapshot through the Ubiquiti cloud connector

## Repository layout

```text
app/
  unifi_dashboard.py
  unifi_live_bridge.mjs

config/
  Unifi.ini.example

docs/
  WINDOWS_SETUP.md
  DOCKER_SETUP.md
  CAMERA_COMPATIBILITY.md
  REMOTE_CAMERA.md
  UPGRADING.md

Dockerfile
docker-compose.yml
requirements.txt
package.json
package-lock.json

SETUP_WINDOWS.bat
RUN_DASHBOARD_LOCAL.bat
RUN_DASHBOARD_LAN.bat
CHECK_WINDOWS_SETUP.bat
```

## License

No license file is included in this package. Before publishing the repository publicly, choose the license you want to use. MIT is a common choice for a small sample/hobby project intended for others to modify.
