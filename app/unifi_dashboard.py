#!/usr/bin/env python3
"""
UniFi Protect-First Dashboard v3.3.1

Uses the official local UniFi Network + Protect Integration APIs.

Main screen:
  - Protect / camera status
  - UP Sense environment data
  - SuperLink status
  - Touch-friendly relay controls
  - Native Protect live video with automatic snapshot fallback
  - Network hidden in a bottom drawer
  - Clicking a Network device shows only clients whose uplinkDeviceId
    matches that device, with scrolling and Name/IP sorting.

Configuration:
    Unifi.ini beside this script:

        DeviceIP=192.168.1.1
        API_Key=YOUR_API_KEY

Install:
    py -m pip install flask requests

Run locally:
    py unifi_dashboard_v3.py

Run for other devices on the LAN:
    py unifi_dashboard_v3.py --lan

Open:
    http://127.0.0.1:8088
"""

import logging
from logging.handlers import RotatingFileHandler

import atexit
import os
import secrets
import subprocess
from urllib.parse import quote, urlsplit

import argparse
import json
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import websocket
from flask_sock import Sock
from flask import Flask, Response, jsonify, render_template_string, request

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 20, 'max_message_size': 1024}
sock = Sock(app)
BRIDGE_PORT = None
BRIDGE_TOKEN = secrets.token_urlsafe(32)
BRIDGE_PROCESS = None


LOG = logging.getLogger('unifi.dashboard')
LOG.addHandler(logging.NullHandler())
API_HEALTH = {}
LOG_SECRETS = []


def setup_logging(config_path):
    global LOG_SECRETS
    values = {}
    for line in config_path.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith(('#', ';')):
            key, value = line.split('=', 1)
            values[key.strip().lower()] = value.strip()
    LOG_SECRETS = sorted([values.get(k, '') for k in
                          ('api_key', 'protect_password', 'protect_user', 'remote_api_key') if values.get(k)], key=len, reverse=True)
    folder = Path(__file__).resolve().parent / 'logs'
    try:
        folder.mkdir(exist_ok=True)
        handler = RotatingFileHandler(folder / 'unifi-dashboard.log',
                                      maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
    except OSError as error:
        print(f'Could not create log file ({type(error).__name__}); dashboard will continue.')
        return
    class RedactedFormatter(logging.Formatter):
        def format(self, record):
            message = super().format(record)
            for secret in LOG_SECRETS:
                message = message.replace(secret, '[REDACTED]')
            return message
    handler.setFormatter(RedactedFormatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    LOG.info('Dashboard v3.0.3 started; timestamps use PC local time')
    print(f'Event log:  {folder / "unifi-dashboard.log"}')


def record_health(service, error=None):
    previous = API_HEALTH.get(service)
    if error is None:
        if previous and previous['error']:
            LOG.info('%s API recovered after %.1f seconds', service, time.monotonic() - previous['since'])
        elif previous is None:
            LOG.info('%s API connected', service)
        API_HEALTH[service] = {'error': None, 'since': time.monotonic()}
        return
    # Retain error category, HTTP status and endpoint, never response bodies/headers.
    detail = type(error).__name__
    response = getattr(error, 'response', None)
    if response is not None:
        detail += f' HTTP {response.status_code}'
    req = getattr(error, 'request', None)
    if req is not None and getattr(req, 'url', None):
        detail += ' endpoint=' + urlsplit(req.url).path
    if not previous or previous['error'] != detail:
        LOG.warning('%s API refresh failed: %s', service, detail)
    API_HEALTH[service] = {'error': detail, 'since': previous['since'] if previous and previous['error'] else time.monotonic()}


def capture_bridge_output(process):
    try:
        for line in process.stdout:
            message = line.strip()
            if message:
                LOG.info('Node: %s', message[:2000])
        code = process.wait()
        LOG.info('Node helper exited: code %s', code)
    finally:
        process.stdout.close()


@app.route('/api/live/diagnostics')
def live_diagnostics():
    running = BRIDGE_PROCESS is not None and BRIDGE_PROCESS.poll() is None
    return jsonify({
        'version': '3.3.1',
        'helper_running': running,
        'helper_exit_code': BRIDGE_PROCESS.poll() if BRIDGE_PROCESS else None,
        'live_enabled': BRIDGE_PORT is not None,
        'next_step': 'Check terminal Live view messages and browser WebSocket errors.' if running else 'Restart dashboard after npm install; check terminal for Node startup errors.',
    })


def stop_bridge():
    global BRIDGE_PROCESS
    if BRIDGE_PROCESS and BRIDGE_PROCESS.poll() is None:
        BRIDGE_PROCESS.terminate()
        try:
            BRIDGE_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            BRIDGE_PROCESS.kill()
            BRIDGE_PROCESS.wait(timeout=5)
    BRIDGE_PROCESS = None


def start_bridge(config_path):
    global BRIDGE_PORT, BRIDGE_PROCESS
    # Allocate a local port; a bind race fails closed because the proxy requires
    # our per-launch secret and never exposes it to browser clients.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        BRIDGE_PORT = listener.getsockname()[1]
    env = os.environ.copy()
    env.update(UNIFI_CONFIG=str(config_path), UNIFI_BRIDGE_PORT=str(BRIDGE_PORT),
               UNIFI_BRIDGE_TOKEN=BRIDGE_TOKEN)
    try:
        BRIDGE_PROCESS = subprocess.Popen(
            ['node', str(Path(__file__).with_name('unifi_live_bridge.mjs'))],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        LOG.info('Node helper started')
        threading.Thread(target=capture_bridge_output, args=(BRIDGE_PROCESS,), daemon=True).start()
        atexit.register(stop_bridge)
    except OSError as error:
        LOG.error('Node helper launch failed: %s', type(error).__name__)
        print('Node helper unavailable. Cameras will use snapshots. Run npm install and check Node is installed.')


@sock.route('/camera/<camera_id>/live')
def camera_live(ws, camera_id):
    # Browser uses the dashboard origin, including when opened from another LAN PC.
    origin = request.headers.get('Origin', '')
    if urlsplit(origin).netloc != request.host:
        ws.close(reason=1008, message='Origin rejected')
        return
    with LOCK:
        camera = next((c for c in CACHE['cameras'] if c['id'] == camera_id), None)
    if not camera or camera['state'] != 'CONNECTED' or not BRIDGE_PROCESS or BRIDGE_PROCESS.poll() is not None:
        ws.send(json.dumps({'type': 'error', 'message': 'camera offline' if not camera or camera['state'] != 'CONNECTED' else 'Node helper not running; check terminal'}))
        ws.close(reason=1011, message='Native stream unavailable')
        return
    lens = 1 if request.args.get('lens') == '1' else 0
    if lens and not camera.get('hasPackageCamera'):
        ws.send(json.dumps({'type': 'error', 'message': 'package camera unavailable'}))
        ws.close(reason=1008, message='Package camera unavailable')
        return

    upstream = None
    done = threading.Event()
    try:
        upstream = websocket.create_connection(
            f'ws://127.0.0.1:{BRIDGE_PORT}/live/{camera_id}?lens={lens}',
            header={'Authorization': f'Bearer {BRIDGE_TOKEN}'},
            timeout=20, http_no_proxy=['127.0.0.1'])
        def watch_browser():
            try:
                while not done.is_set():
                    if ws.receive(timeout=1) is not None:
                        break  # Viewer protocol is receive-only.
            except Exception:
                pass
            finally:
                done.set()
                upstream.close()
        threading.Thread(target=watch_browser, daemon=True).start()
        while not done.is_set():
            message = upstream.recv()
            if message == '' or message == b'':
                break
            ws.send(message)
    except Exception as error:
        reason = 'helper connection timed out' if isinstance(error, (TimeoutError, websocket.WebSocketTimeoutException)) else 'helper connection failed'
        LOG.warning('Live view: %s (%s)', reason, type(error).__name__)
        try:
            ws.send(json.dumps({'type': 'error', 'message': reason}))
        except Exception:
            pass
    finally:
        done.set()
        if upstream:
            upstream.close()
        try:
            ws.close()
        except Exception:
            pass


CONTROLLER = ""
API_KEY = ""
NETWORK_BASE = ""
PROTECT_BASE = ""
SITE_ID = None
SITE_NAME = "Default"

# Optional cloud-connected Protect camera.  This is intentionally snapshot-only:
# the normal UniFi apps are better suited to remote live video, while a periodic
# snapshot is ideal for filling an otherwise unused dashboard tile.
REMOTE_API = "https://api.ui.com"
REMOTE_API_KEY = ""
REMOTE_CAMERA_NAME = ""
REMOTE_CAMERA_ID = ""
REMOTE_CONSOLE_ID = ""
REMOTE_CAMERA = None
REMOTE_BASE = ""
REMOTE_ERROR = None
REMOTE_LAST_RESOLVE = 0.0
REMOTE_RESOLVE_RETRY_SECONDS = 60
REMOTE_SNAPSHOT_REFRESH_SECONDS = 10
REMOTE_SNAPSHOT_CACHE_SECONDS = 5
REMOTE_SNAPSHOT_CACHE = {"data": None, "content_type": "image/jpeg", "time": 0.0}
REMOTE_SESSION = requests.Session()
REMOTE_LOCK = threading.Lock()

REFRESH_SECONDS = 5

SESSION = requests.Session()
LOCK = threading.Lock()

CACHE = {
    "network_ok": False,
    "protect_ok": False,
    "network_error": None,
    "protect_error": None,
    "network_version": None,
    "protect_version": None,
    "devices": [],
    "clients": [],
    "sensors": [],
    "bridges": [],
    "relays": [],
    "cameras": [],
    "nvr": {},
    "last_refresh": None,
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_unifi_config(config_path=None):
    """Read a simple sectionless Unifi.ini."""
    if config_path:
        path = Path(config_path).expanduser().resolve()
    else:
        path = Path(__file__).resolve().with_name("Unifi.ini")

    if not path.exists():
        path.write_text(
            "# UniFi local API configuration\n"
            "# Keep this file private - it contains your API key.\n\n"
            "DeviceIP=192.168.1.1\n"
            "API_Key=YOUR_API_KEY_HERE\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"Created config template:\n  {path}\n\n"
            "Edit API_Key in that file, then run the script again."
        )

    values = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()

    device_ip = values.get("deviceip", "")
    api_key = values.get("api_key", "")

    if not device_ip:
        raise SystemExit(f"DeviceIP is missing from {path}")

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        raise SystemExit(f"API_Key has not been set in {path}")

    remote = {
        "api_key": values.get("remote_api_key", ""),
        "camera_name": values.get("remote_camera_name", ""),
        "camera_id": values.get("remote_camera_id", ""),
        "console_id": values.get("remote_console_id", ""),
    }

    return device_ip, api_key, path, remote


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def headers():
    return {
        "X-API-Key": API_KEY,
        "Accept": "application/json",
    }


def api_get(base, path, timeout=12):
    response = SESSION.get(
        base + path,
        headers=headers(),
        verify=False,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def api_get_response(base, path, timeout=15):
    response = SESSION.get(
        base + path,
        headers=headers(),
        verify=False,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def api_post(base, path, body, timeout=12):
    h = headers()
    h["Content-Type"] = "application/json"
    response = SESSION.post(
        base + path,
        headers=h,
        json=body,
        verify=False,
        timeout=timeout,
    )
    if response.status_code not in (200, 201, 202, 204):
        response.raise_for_status()
    return response


def unwrap(result):
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def choose(*values, default=""):
    for value in values:
        if value is not None and value != "":
            return value
    return default


# ---------------------------------------------------------------------------
# Optional remote Protect snapshot camera
# ---------------------------------------------------------------------------

def remote_enabled():
    """Remote camera is opt-in. A key alone is never enough to guess a camera."""
    return bool(REMOTE_API_KEY and (REMOTE_CAMERA_NAME or REMOTE_CAMERA_ID))


def remote_label(value):
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "Unnamed")
    return str(value or "Unnamed")


def remote_rows(data):
    value = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(value, list):
        raise RuntimeError("Unexpected remote list response format")
    return value


def remote_get(path, accept="application/json", timeout=(10, 30), limit=10 * 1024 * 1024):
    """GET through api.ui.com without ever forwarding the key on redirects."""
    headers = {"X-API-Key": REMOTE_API_KEY, "Accept": accept}
    response = REMOTE_SESSION.get(
        REMOTE_API + path,
        headers=headers,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Remote HTTP {response.status_code}")

    data = bytearray()
    for chunk in response.iter_content(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise RuntimeError("Remote response exceeded 10 MB")
    return bytes(data), response.headers.get("Content-Type", "")


def remote_get_json(path):
    raw, _ = remote_get(path)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError("Remote API returned non-JSON data") from None


def remote_camera_list(console_id):
    """Return (base, cameras), preserving the cloud connector fallback proved by the probe."""
    prefix = "/v1/connector/consoles/" + quote(str(console_id), safe="")
    bases = [
        prefix + "/proxy/protect/integration/v1",
        prefix + "/protect/integration/v1",
    ]
    last_error = None
    for index, base in enumerate(bases):
        try:
            return base, remote_rows(remote_get_json(base + "/cameras"))
        except RuntimeError as error:
            last_error = error
            # The probe established that published connector variants differ by
            # /proxy. Only a missing route should fall through to the alternate.
            if index == 0 and str(error) == "Remote HTTP 404":
                continue
            raise
    raise last_error or RuntimeError("Remote camera list unavailable")


def resolve_remote_camera(force=False):
    """Resolve one configured remote camera. Failure never affects the local dashboard."""
    global REMOTE_CAMERA, REMOTE_BASE, REMOTE_ERROR, REMOTE_LAST_RESOLVE

    if not remote_enabled():
        return False

    now = time.time()
    with REMOTE_LOCK:
        if not force and REMOTE_CAMERA and REMOTE_BASE:
            return True
        if not force and now - REMOTE_LAST_RESOLVE < REMOTE_RESOLVE_RETRY_SECONDS:
            return False
        REMOTE_LAST_RESOLVE = now

    try:
        if REMOTE_CONSOLE_ID:
            console_ids = [REMOTE_CONSOLE_ID]
        else:
            hosts = remote_rows(remote_get_json("/v1/hosts"))
            console_ids = [str(host.get("id")) for host in hosts if host.get("id")]
            if not console_ids:
                raise RuntimeError("Remote API returned no consoles")

        matches = []
        errors = []
        wanted_name = REMOTE_CAMERA_NAME.casefold().strip()
        wanted_id = REMOTE_CAMERA_ID.strip()

        for console_id in console_ids:
            try:
                base, cameras = remote_camera_list(console_id)
            except Exception as error:
                errors.append(f"{console_id}: {error}")
                continue

            for camera in cameras:
                camera_id = str(camera.get("id") or "")
                camera_name = remote_label(camera.get("name"))
                id_match = bool(wanted_id and camera_id == wanted_id)
                name_match = bool(wanted_name and camera_name.casefold().strip() == wanted_name)
                if id_match or name_match:
                    matches.append((console_id, base, camera))

        if not matches:
            detail = f" ({'; '.join(errors[:2])})" if errors else ""
            raise RuntimeError("Configured remote camera not found" + detail)
        if len(matches) > 1 and not REMOTE_CONSOLE_ID:
            raise RuntimeError("Remote camera name/ID matched more than one console; set Remote_Console_ID")

        console_id, base, camera = matches[0]
        resolved = {
            "id": str(camera.get("id") or ""),
            "name": remote_label(camera.get("name")),
            "state": str(camera.get("state") or "CONNECTED").upper(),
            "console_id": str(console_id),
        }
        if not resolved["id"]:
            raise RuntimeError("Remote camera has no ID")

        with REMOTE_LOCK:
            REMOTE_CAMERA = resolved
            REMOTE_BASE = base
            REMOTE_ERROR = None
        LOG.info("Remote snapshot camera ready: %s", resolved["name"])
        return True
    except Exception as error:
        with REMOTE_LOCK:
            REMOTE_CAMERA = None
            REMOTE_BASE = ""
            REMOTE_ERROR = str(error)
        LOG.warning("Remote snapshot camera unavailable: %s", type(error).__name__)
        return False


def remote_camera_status():
    """Synthetic camera record appended to /api/status; contains no cloud credentials."""
    if not remote_enabled():
        return None
    with REMOTE_LOCK:
        camera = dict(REMOTE_CAMERA) if REMOTE_CAMERA else None
    if camera:
        return {
            "id": "remote:" + camera["id"],
            "name": camera["name"],
            "model": "Remote Protect snapshot",
            "type": "REMOTE",
            "state": "CONNECTED" if camera.get("state") == "CONNECTED" else camera.get("state", "CONNECTED"),
            "remote": True,
            "hasPackageCamera": False,
        }
    return {
        "id": "remote:pending",
        "name": REMOTE_CAMERA_NAME or "Remote camera",
        "model": "Remote Protect snapshot",
        "type": "REMOTE",
        "state": "DISCONNECTED",
        "remote": True,
        "hasPackageCamera": False,
    }


def get_remote_snapshot():
    """Fetch the selected remote snapshot, with a tiny server cache for multiple dashboard clients."""
    now = time.time()
    with REMOTE_LOCK:
        cached = dict(REMOTE_SNAPSHOT_CACHE)
        camera = dict(REMOTE_CAMERA) if REMOTE_CAMERA else None
        base = REMOTE_BASE

    if cached.get("data") and now - cached.get("time", 0) < REMOTE_SNAPSHOT_CACHE_SECONDS:
        return cached["data"], cached["content_type"]

    if not camera or not base:
        resolve_remote_camera()
        with REMOTE_LOCK:
            camera = dict(REMOTE_CAMERA) if REMOTE_CAMERA else None
            base = REMOTE_BASE
    if not camera or not base:
        raise RuntimeError("Remote camera unresolved")

    path = base + "/cameras/" + quote(camera["id"], safe="") + "/snapshot"
    data, content_type = remote_get(path, accept="image/jpeg, image/png")

    if data.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    else:
        raise RuntimeError("Remote snapshot was not JPEG/PNG")

    with REMOTE_LOCK:
        REMOTE_SNAPSHOT_CACHE.update({"data": bytes(data), "content_type": content_type, "time": time.time()})
    return bytes(data), content_type


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def normalize_device(d):
    return {
        "id": str(choose(d.get("id"), default="")),
        "name": str(choose(d.get("name"), d.get("model"), d.get("macAddress"), default="Unknown")),
        "model": str(choose(d.get("model"), d.get("shortname"), d.get("displayName"), default="")),
        "state": str(choose(d.get("state"), d.get("status"), default="UNKNOWN")).upper(),
        "ip": str(choose(d.get("ipAddress"), d.get("ip"), d.get("managementIp"), default="")),
        "mac": str(choose(d.get("macAddress"), d.get("mac"), default="")),
        "firmware": str(choose(d.get("firmwareVersion"), d.get("version"), default="")),
    }


def normalize_client(c):
    return {
        "id": str(choose(c.get("id"), default="")),
        "name": str(choose(c.get("name"), c.get("hostname"), c.get("macAddress"), default="Unknown")),
        "type": str(choose(c.get("type"), default="UNKNOWN")).upper(),
        "ip": str(choose(c.get("ipAddress"), c.get("ip"), default="")),
        "mac": str(choose(c.get("macAddress"), c.get("mac"), default="")),
        "uplinkDeviceId": str(choose(c.get("uplinkDeviceId"), default="")),
        "connectedAt": str(choose(c.get("connectedAt"), default="")),
    }


def normalize_sensor(s):
    stats = s.get("stats") or {}
    wireless = s.get("wirelessConnectionState") or {}
    signal = wireless.get("signalState") or {}
    battery = s.get("batteryStatus") or wireless.get("batteryStatus") or {}

    def stat_value(name):
        item = stats.get(name) or {}
        return item.get("value")

    return {
        "id": str(choose(s.get("id"), default="")),
        "name": str(choose(s.get("name"), default="UP Sense")),
        "type": str(choose(s.get("type"), default="")),
        "state": str(choose(s.get("state"), default="UNKNOWN")).upper(),
        "mountType": str(choose(s.get("mountType"), default="")),
        "isOpened": s.get("isOpened"),
        "isMotionDetected": s.get("isMotionDetected"),
        "temperature": stat_value("temperature"),
        "humidity": stat_value("humidity"),
        "light": stat_value("light"),
        "battery": battery.get("percentage"),
        "batteryLow": bool(battery.get("isLow", False)),
        "signalQuality": signal.get("signalQuality"),
        "signalStrength": signal.get("signalStrength"),
    }


def normalize_bridge(b):
    clients = b.get("clients") or []
    return {
        "id": str(choose(b.get("id"), default="")),
        "name": str(choose(b.get("name"), default="SuperLink Gateway")),
        "type": str(choose(b.get("type"), default="")),
        "state": str(choose(b.get("state"), default="UNKNOWN")).upper(),
        "clientCount": len(clients),
        "maxClients": b.get("maxClients"),
    }


def normalize_relay(r):
    wireless = r.get("wirelessConnectionState") or {}
    signal = wireless.get("signalState") or {}

    outputs = []
    for output in (r.get("outputs") or []):
        outputs.append({
            "id": int(output.get("id", 0)),
            "name": str(choose(output.get("name"), default=f"Relay {int(output.get('id', 0)) + 1}")),
            "state": str(choose(output.get("state"), default="unknown")).lower(),
            "pulseDuration": output.get("pulseDuration"),
        })

    return {
        "id": str(choose(r.get("id"), default="")),
        "name": str(choose(r.get("name"), default="Relay")),
        "type": str(choose(r.get("type"), default="")),
        "state": str(choose(r.get("state"), default="UNKNOWN")).upper(),
        "outputs": outputs,
        "signalQuality": signal.get("signalQuality"),
        "signalStrength": signal.get("signalStrength"),
    }


def normalize_camera(c):
    features = c.get("featureFlags") or {}
    name = str(choose(c.get("name"), default="Camera"))
    model = str(choose(c.get("model"), c.get("displayName"), c.get("type"), default=""))
    fingerprint = f"{name} {model}".lower()

    # Protect exposes hasPackageCamera on dual-lens doorbells.  Keep a G6 Entry
    # fallback as this test is specifically for that model in case the
    # Integration API omits the feature flag on a given firmware build.
    has_package_camera = bool(features.get("hasPackageCamera", False)) or "g6 entry" in fingerprint

    return {
        "id": str(choose(c.get("id"), default="")),
        "name": name,
        "model": model,
        "type": str(choose(c.get("type"), default="")),
        "state": str(choose(c.get("state"), default="UNKNOWN")).upper(),
        "hasMic": bool(features.get("hasMic", False)),
        "hasSpeaker": bool(features.get("hasSpeaker", False)),
        "hasPackageCamera": has_package_camera,
        "fullHdSnapshot": bool(features.get("supportFullHdSnapshot", False)),
        "smartDetectTypes": features.get("smartDetectTypes") or [],
    }


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh_network():
    global SITE_ID, SITE_NAME

    info = api_get(NETWORK_BASE, "/info")
    sites = unwrap(api_get(NETWORK_BASE, "/sites"))
    if not sites:
        raise RuntimeError("No UniFi Network sites returned.")

    if SITE_ID is None:
        SITE_ID = sites[0].get("id")
        SITE_NAME = choose(sites[0].get("name"), default="Default")

    # Explicit limit ensures large client lists are returned in one request.
    devices_raw = unwrap(api_get(NETWORK_BASE, f"/sites/{SITE_ID}/devices?limit=200"))
    clients_raw = unwrap(api_get(NETWORK_BASE, f"/sites/{SITE_ID}/clients?limit=200"))

    devices = [normalize_device(x) for x in (devices_raw or [])]
    clients = [normalize_client(x) for x in (clients_raw or [])]

    devices.sort(key=lambda x: (x["state"] != "ONLINE", x["name"].lower()))
    clients.sort(key=lambda x: x["name"].lower())

    with LOCK:
        CACHE["network_ok"] = True
        CACHE["network_error"] = None
        CACHE["network_version"] = choose(info.get("applicationVersion"), default="Unknown")
        CACHE["devices"] = devices
        CACHE["clients"] = clients


def refresh_protect():
    info = api_get(PROTECT_BASE, "/meta/info")
    sensors_raw = unwrap(api_get(PROTECT_BASE, "/sensors"))
    bridges_raw = unwrap(api_get(PROTECT_BASE, "/bridges"))
    relays_raw = unwrap(api_get(PROTECT_BASE, "/relays"))
    cameras_raw = unwrap(api_get(PROTECT_BASE, "/cameras"))

    try:
        nvr_raw = api_get(PROTECT_BASE, "/nvrs")
        nvr = unwrap(nvr_raw)
        if isinstance(nvr, list):
            nvr = nvr[0] if nvr else {}
    except Exception:
        nvr = {}

    sensors = [normalize_sensor(x) for x in (sensors_raw or [])]
    bridges = [normalize_bridge(x) for x in (bridges_raw or [])]
    relays = [normalize_relay(x) for x in (relays_raw or [])]
    cameras = [normalize_camera(x) for x in (cameras_raw or [])]

    sensors.sort(key=lambda x: x["name"].lower())
    bridges.sort(key=lambda x: x["name"].lower())
    relays.sort(key=lambda x: x["name"].lower())
    cameras.sort(key=lambda x: (x["state"] != "CONNECTED", x["name"].lower()))

    with LOCK:
        CACHE["protect_ok"] = True
        CACHE["protect_error"] = None
        CACHE["protect_version"] = choose(info.get("applicationVersion"), default="Unknown")
        CACHE["sensors"] = sensors
        CACHE["bridges"] = bridges
        CACHE["relays"] = relays
        CACHE["cameras"] = cameras
        CACHE["nvr"] = nvr or {}


def refresh_all():
    network_error = None
    protect_error = None

    try:
        refresh_network()
        record_health('Network')
    except Exception as exc:
        record_health('Network', exc)
        network_error = str(exc)
        with LOCK:
            CACHE["network_ok"] = False
            CACHE["network_error"] = network_error

    try:
        refresh_protect()
        record_health('Protect')
    except Exception as exc:
        record_health('Protect', exc)
        protect_error = str(exc)
        with LOCK:
            CACHE["protect_ok"] = False
            CACHE["protect_error"] = protect_error

    with LOCK:
        CACHE["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def refresh_loop():
    while True:
        refresh_all()
        time.sleep(REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    with LOCK:
        data = json.loads(json.dumps(CACHE))

    devices = data["devices"]
    clients = data["clients"]
    cameras = data["cameras"]
    remote_camera = remote_camera_status()
    if remote_camera:
        cameras.append(remote_camera)
    sensors = data["sensors"]
    bridges = data["bridges"]
    relays = data["relays"]

    online_devices = sum(1 for d in devices if d["state"] == "ONLINE")
    connected_cameras = sum(1 for c in cameras if c["state"] == "CONNECTED")
    connected_sensors = sum(1 for s in sensors if s["state"] == "CONNECTED")
    connected_bridges = sum(1 for b in bridges if b["state"] == "CONNECTED")
    connected_relays = sum(1 for r in relays if r["state"] == "CONNECTED")

    # Attach client count to each infrastructure device.
    client_counts = {}
    for client in clients:
        uplink = client.get("uplinkDeviceId")
        if uplink:
            client_counts[uplink] = client_counts.get(uplink, 0) + 1

    for device in devices:
        device["attachedClients"] = client_counts.get(device["id"], 0)

    data["summary"] = {
        "site": SITE_NAME,
        "devices_total": len(devices),
        "devices_online": online_devices,
        "clients_total": len(clients),
        "cameras_total": len(cameras),
        "cameras_connected": connected_cameras,
        "sensors_total": len(sensors),
        "sensors_connected": connected_sensors,
        "bridges_total": len(bridges),
        "bridges_connected": connected_bridges,
        "relays_total": len(relays),
        "relays_connected": connected_relays,
    }

    return jsonify(data)


@app.route("/api/network/device/<device_id>/clients")
def api_device_clients(device_id):
    with LOCK:
        devices = [dict(x) for x in CACHE["devices"]]
        clients = [dict(x) for x in CACHE["clients"]]

    device = next((d for d in devices if d["id"] == device_id), None)
    if not device:
        return jsonify({"ok": False, "error": "Device not found"}), 404

    attached = [c for c in clients if c.get("uplinkDeviceId") == device_id]
    attached.sort(key=lambda x: x["name"].lower())

    stats = {}
    try:
        stats = api_get(
            NETWORK_BASE,
            f"/sites/{SITE_ID}/devices/{device_id}/statistics/latest"
        ) or {}
        stats = unwrap(stats)
        if not isinstance(stats, dict):
            stats = {}
    except Exception:
        stats = {}

    return jsonify({
        "ok": True,
        "device": device,
        "clients": attached,
        "count": len(attached),
        "stats": stats,
    })


@app.route("/api/relay/<relay_id>/<int:output_id>", methods=["POST"])
def api_relay_action(relay_id, output_id):
    payload = request.get_json(silent=True) or {}
    state = str(payload.get("state", "")).lower()

    if state not in ("on", "off"):
        return jsonify({"ok": False, "error": "state must be on or off"}), 400

    if output_id not in (0, 1):
        return jsonify({"ok": False, "error": "output must be 0 or 1"}), 400

    with LOCK:
        relay = next((r for r in CACHE["relays"] if r["id"] == relay_id), None)

    if not relay:
        return jsonify({"ok": False, "error": "Relay not found"}), 404

    try:
        api_post(
            PROTECT_BASE,
            f"/relays/{relay_id}/outputs/{output_id}/activate",
            {"state": state, "pulseDuration": 0},
        )
        # Give Protect a moment to update its reported state.
        time.sleep(0.25)
        try:
            refresh_protect()
        except Exception:
            pass
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/remote-camera/snapshot")
def remote_camera_snapshot():
    if not remote_enabled():
        return Response(status=404)
    try:
        data, content_type = get_remote_snapshot()
        response = Response(data, mimetype=content_type)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception:
        return Response(status=502)


@app.route("/camera/<camera_id>/snapshot")
def camera_snapshot(camera_id):
    with LOCK:
        camera = next((c for c in CACHE["cameras"] if c["id"] == camera_id), None)

    if not camera:
        return Response(status=404)

    if camera["state"] != "CONNECTED":
        return Response(status=503)

    try:
        # Official Protect "Get camera snapshot" endpoint.
        r = api_get_response(PROTECT_BASE, f"/cameras/{camera_id}/snapshot", timeout=15)

        content_type = r.headers.get("content-type", "image/jpeg")
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"

        response = Response(r.content, mimetype=content_type)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception:
        return Response(status=502)


@app.route("/camera/<camera_id>/package-snapshot")
def camera_package_snapshot(camera_id):
    with LOCK:
        camera = next((c for c in CACHE["cameras"] if c["id"] == camera_id), None)

    if not camera:
        return Response(status=404)
    if camera["state"] != "CONNECTED" or not camera.get("hasPackageCamera"):
        return Response(status=503)
    if not BRIDGE_PROCESS or BRIDGE_PROCESS.poll() is not None or not BRIDGE_PORT:
        return Response(status=503)

    try:
        r = requests.get(
            f"http://127.0.0.1:{BRIDGE_PORT}/snapshot/{camera_id}?lens=1",
            headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
            timeout=12,
        )
        r.raise_for_status()
        response = Response(r.content, mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception:
        return Response(status=502)


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>UniFi Home</title>
<style>
:root {
    --bg:#090d12;
    --panel:#111821;
    --panel2:#151e29;
    --border:#263241;
    --text:#eef4fa;
    --muted:#8f9dac;
    --green:#33d17a;
    --amber:#ffba55;
    --red:#ff6377;
    --blue:#5ba7ff;
    --shadow:0 18px 55px rgba(0,0,0,.34);
}
* { box-sizing:border-box; }
html, body { width:100%; height:100%; overflow:hidden; }
body {
    margin:0;
    font-family:Inter,Segoe UI,Arial,sans-serif;
    background:var(--bg);
    color:var(--text);
    -webkit-tap-highlight-color:transparent;
}
button, summary { touch-action:manipulation; }
.app {
    height:100vh;
    height:100dvh;
    display:grid;
    grid-template-rows:calc(72px + env(safe-area-inset-top)) 1fr calc(66px + env(safe-area-inset-bottom));
}
.topbar {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:18px;
    padding:env(safe-area-inset-top) max(24px, env(safe-area-inset-right)) 0 max(24px, env(safe-area-inset-left));
    background:#0d1219;
    border-bottom:1px solid var(--border);
}
.brand { display:flex; align-items:baseline; gap:12px; white-space:nowrap; }
.brand h1 { margin:0; font-size:25px; font-weight:700; letter-spacing:-.4px; }
.site { color:var(--muted); font-size:13px; }
.statusbar {
    flex:1;
    display:flex;
    justify-content:center;
    gap:10px;
    overflow:hidden;
}
.status-pill {
    display:flex;
    align-items:center;
    gap:8px;
    padding:9px 12px;
    border:1px solid var(--border);
    background:var(--panel);
    border-radius:999px;
    white-space:nowrap;
    font-size:13px;
}
.dot {
    width:9px; height:9px; border-radius:50%;
    background:var(--green); flex:0 0 auto;
    box-shadow:0 0 12px rgba(51,209,122,.28);
}
.dot.bad { background:var(--red); box-shadow:0 0 12px rgba(255,99,119,.28); }
.dot.warn { background:var(--amber); }
.clock {
    text-align:right;
    flex:0 0 180px;
    width:180px;
    min-width:180px;
    white-space:nowrap;
}
.clock-time { font-size:23px; font-weight:650; font-variant-numeric:tabular-nums; }
.clock-date { color:var(--muted); font-size:11px; margin-top:2px; }

.main {
    min-height:0;
    display:grid;
    grid-template-columns:minmax(310px, 33%) 1fr;
    gap:16px;
    padding:16px 18px;
}
.left {
    min-height:0;
    display:flex;
    flex-direction:column;
    gap:14px;
    overflow:auto;
    scrollbar-width:thin;
}
.panel {
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:16px;
    overflow:hidden;
}
.panel-head {
    height:48px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    padding:0 15px;
    border-bottom:1px solid var(--border);
}
.panel-title { font-size:15px; font-weight:680; }
.small-state {
    display:flex; align-items:center; gap:7px;
    color:var(--muted); font-size:11px;
}
.sensor {
    padding:15px;
}
.sensor-top {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px;
    margin-bottom:13px;
}
.sensor-name { font-size:17px; font-weight:680; }
.door {
    padding:7px 11px;
    border-radius:9px;
    font-size:12px;
    font-weight:700;
    letter-spacing:.4px;
    background:#17231d;
    color:var(--green);
}
.door.open { background:#301b20; color:#ff8796; }
.metric-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:9px;
}
.metric {
    min-height:71px;
    padding:11px 12px;
    border:1px solid #202b38;
    background:#0d131b;
    border-radius:12px;
}
.metric-label {
    color:var(--muted);
    font-size:10px;
    text-transform:uppercase;
    letter-spacing:.65px;
}
.metric-value { font-size:22px; font-weight:700; margin-top:5px; }
.metric-unit { font-size:12px; color:var(--muted); font-weight:500; margin-left:3px; }

.super-body { padding:12px; }
.super-row {
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    margin-bottom:13px;
}
.super-name { font-weight:680; font-size:16px; }
.device-heading { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
details summary { cursor:pointer; padding:4px 0; }
.super-detail { color:var(--muted); font-size:12px; margin-top:4px; }

.relay-device + .relay-device {
    margin-top:12px;
    padding-top:12px;
    border-top:1px solid var(--border);
}
.relay-device-head {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:8px;
    margin-bottom:8px;
}
.relay-device-name {
    font-weight:680;
    font-size:16px;
}
.relay-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
}
.relay {
    height:76px;
    border-radius:12px;
    border:1px solid #344252;
    background:#121a24;
    color:var(--text);
    cursor:pointer;
    font:inherit;
}
.relay:active { transform:scale(.985); }
.relay .relay-name {
    display:block;
    color:var(--muted);
    font-size:11px;
    text-transform:uppercase;
    letter-spacing:.65px;
}
.relay .relay-state {
    display:block;
    font-size:20px;
    font-weight:750;
    margin-top:4px;
}
.relay.on {
    background:#12301f;
    border-color:#286c48;
}
.relay.on .relay-state { color:var(--green); }
.relay.off .relay-state { color:#bdc7d2; }
.relay.busy { opacity:.5; pointer-events:none; }

.camera-area {
    min-width:0; min-height:0;
    display:grid;
    grid-template-columns:repeat(2, minmax(0,1fr));
    grid-auto-rows:minmax(0,1fr);
    gap:14px;
}

/* Desktop polish: when there are 3+ camera tiles, align Environment and
   Relays exactly with the two camera rows to form a clean 3 x 2 grid. */
@media (min-width:901px) {
    .main.two-camera-rows .left {
        display:grid;
        grid-template-rows:repeat(2,minmax(0,1fr));
        gap:14px;
        overflow:hidden;
    }
    .main.two-camera-rows .left > .panel {
        min-height:0;
        height:100%;
    }
}
.camera-area.single-camera { grid-template-columns:minmax(0,1fr); }
.camera {
    min-width:0; min-height:0;
    position:relative;
    background:#05080c;
    border:1px solid var(--border);
    border-radius:16px;
    overflow:hidden;
}
.camera { cursor:pointer; touch-action:manipulation; }
.camera:focus-visible { outline:3px solid #5caeff; outline-offset:-3px; }
.camera.expanded {
    position:fixed; inset:0; width:100vw; height:100dvh;
    z-index:10000; border:0; border-radius:0; background:#000;
}
.camera-hint {
    position:absolute; bottom:16px; left:50%; transform:translateX(-50%);
    z-index:3; padding:8px 14px; border-radius:20px; color:#fff;
    background:rgba(0,0,0,.65); font-size:14px; pointer-events:none;
    white-space:nowrap;
}
.camera.expanded .camera-hint { font-size:18px; bottom:24px; }
body.camera-focused { overflow:hidden; }
.camera img, .camera video {
    width:100%;
    height:100%;
    object-fit:contain;
    display:block;
}
.camera video { position:absolute; inset:0; }
.camera img { position:absolute; inset:0; z-index:1; }
.camera-overlay {
    z-index:2;
    position:absolute;
    left:0; right:0; top:0;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
    padding:12px 14px;
    background:linear-gradient(rgba(0,0,0,.72),rgba(0,0,0,0));
    pointer-events:none;
}
.cam-name {
    font-size:15px;
    font-weight:700;
    text-shadow:0 1px 4px #000;
}
.cam-state {
    display:flex; align-items:center; gap:6px;
    font-size:11px; font-weight:650;
    text-shadow:0 1px 4px #000;
}

/* G6 Entry / dual-lens package camera. */
.package-inset {
    position:absolute;
    right:14px;
    bottom:14px;
    z-index:5;
    width:clamp(120px,34%,210px);
    aspect-ratio:4/3;
    overflow:hidden;
    border:2px solid rgba(255,255,255,.78);
    border-radius:12px;
    background:#020406;
    box-shadow:0 8px 26px rgba(0,0,0,.55);
    cursor:pointer;
    touch-action:manipulation;
}
.package-inset img,
.package-inset video {
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
}
.package-inset img { z-index:1; }
.package-inset video { z-index:0; }
.package-overlay {
    position:absolute;
    z-index:3;
    left:0; right:0; top:0;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:6px;
    padding:6px 7px 18px;
    color:#fff;
    background:linear-gradient(rgba(0,0,0,.78),rgba(0,0,0,0));
    pointer-events:none;
    font-size:9px;
    font-weight:750;
    text-shadow:0 1px 3px #000;
}
.package-title { letter-spacing:.5px; }
.package-state { display:flex; align-items:center; gap:4px; white-space:nowrap; }
.package-state .dot { width:7px; height:7px; }
.package-inset-hint {
    position:absolute;
    left:50%; bottom:5px;
    z-index:4;
    transform:translateX(-50%);
    padding:3px 7px;
    border-radius:999px;
    background:rgba(0,0,0,.7);
    color:#fff;
    font-size:9px;
    white-space:nowrap;
    pointer-events:none;
}
.package-inset.expanded-package {
    position:fixed;
    inset:0;
    width:100vw;
    height:100dvh;
    aspect-ratio:auto;
    z-index:10020;
    border:0;
    border-radius:0;
    background:#000;
}
.package-inset.expanded-package img,
.package-inset.expanded-package video {
    object-fit:contain;
}
.package-inset.expanded-package .package-overlay {
    padding:max(14px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 40px max(16px,env(safe-area-inset-left));
    font-size:15px;
}
.package-inset.expanded-package .package-state .dot { width:9px; height:9px; }
.package-inset.expanded-package .package-inset-hint {
    bottom:max(18px,calc(env(safe-area-inset-bottom) + 10px));
    padding:8px 14px;
    font-size:14px;
}
body.package-focused { overflow:hidden; }
.camera-offline {
    position:absolute;
    inset:0;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    gap:10px;
    color:var(--muted);
    background:
        radial-gradient(circle at center,#151d27 0,#090d12 68%);
}
.offline-title { color:var(--red); font-size:17px; font-weight:720; }
.no-camera {
    grid-column:1/-1;
    display:flex;
    align-items:center;
    justify-content:center;
    color:var(--muted);
    border:1px dashed var(--border);
    border-radius:16px;
}

/* Network bottom bar + drawer */
.network-bar {
    position:relative;
    z-index:20;
    display:flex;
    align-items:center;
    gap:10px;
    padding:9px max(18px, env(safe-area-inset-right)) calc(9px + env(safe-area-inset-bottom)) max(18px, env(safe-area-inset-left));
    background:#0d1219;
    border-top:1px solid var(--border);
    overflow:hidden;
}
.network-toggle {
    height:44px;
    min-width:150px;
    padding:0 15px;
    border:1px solid var(--border);
    border-radius:11px;
    background:var(--panel);
    color:var(--text);
    font-weight:700;
    cursor:pointer;
}
.device-pills {
    display:flex;
    gap:8px;
    overflow:auto;
    scrollbar-width:none;
}
.device-pills::-webkit-scrollbar { display:none; }
.device-pill {
    height:44px;
    display:flex;
    align-items:center;
    gap:7px;
    padding:0 12px;
    border:1px solid var(--border);
    border-radius:11px;
    background:#111821;
    color:var(--text);
    white-space:nowrap;
    cursor:pointer;
}
.device-pill:hover { background:#17212d; }
.device-pill .count {
    color:var(--muted);
    font-size:11px;
}

.drawer {
    position:fixed;
    left:0; right:0; bottom:66px;
    height:min(48vh, 570px);
    z-index:19;
    background:#0d131b;
    border-top:1px solid #314052;
    box-shadow:0 -22px 60px rgba(0,0,0,.45);
    transform:translateY(calc(100% + 12px));
    transition:transform .22s ease;
    display:flex;
    flex-direction:column;
}
.drawer.open { transform:translateY(0); }
.drawer-head {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:16px;
    padding:14px 19px;
    border-bottom:1px solid var(--border);
}
.drawer-title { font-size:17px; font-weight:720; }
.drawer-close {
    width:44px; height:40px;
    border:1px solid var(--border);
    border-radius:9px;
    background:var(--panel);
    color:var(--text);
    font-size:22px;
    cursor:pointer;
}
.drawer-body {
    flex:1;
    min-height:0;
    display:grid;
    grid-template-columns:290px 1fr;
}
.device-detail {
    padding:18px;
    border-right:1px solid var(--border);
    overflow:auto;
}
.device-detail .model { color:var(--muted); margin-top:6px; }
.detail-kv {
    margin-top:18px;
    display:grid;
    gap:9px;
}
.kv {
    display:flex;
    justify-content:space-between;
    gap:14px;
    padding-bottom:8px;
    border-bottom:1px solid #1d2733;
    font-size:12px;
}
.kv span:first-child { color:var(--muted); }

.clients-panel {
    min-width:0;
    min-height:0;
    display:flex;
    flex-direction:column;
}
.clients-head {
    padding:13px 16px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    border-bottom:1px solid var(--border);
}
.client-count { color:var(--muted); font-size:12px; }
.client-list { flex:1; min-height:0; overflow:auto; scrollbar-gutter:stable; }
.client-row {
    height:48px;
    display:grid;
    grid-template-columns:minmax(170px,2fr) 90px 140px 170px;
    align-items:center;
    gap:12px;
    padding:0 16px;
    border-bottom:1px solid #1d2733;
    font-size:12px;
}
.client-row .cname { font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.client-row .muted { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.client-type {
    font-size:10px;
    width:max-content;
    padding:4px 7px;
    border-radius:6px;
    background:#1b2734;
    color:#b8c8d8;
}
.toast {
    position:fixed;
    top:82px;
    right:20px;
    z-index:100;
    padding:11px 14px;
    border-radius:10px;
    background:#17202b;
    border:1px solid var(--border);
    box-shadow:var(--shadow);
    opacity:0;
    transform:translateY(-8px);
    pointer-events:none;
    transition:.18s ease;
    font-size:13px;
}
.toast.show { opacity:1; transform:translateY(0); }
.toast.error { border-color:#6b303a; color:#ff9baa; }

@media (max-width:1050px) {
    .main { grid-template-columns:320px 1fr; }
    .statusbar .optional { display:none; }
}

/* Tablet portrait / small-window layout: cameras first, controls below. */
@media (max-width:900px) {
    html,body { height:auto; min-height:100%; overflow:auto; overscroll-behavior-y:contain; }
    .app {
        height:auto;
        min-height:100vh;
        min-height:100dvh;
        grid-template-rows:auto auto auto;
    }
    .topbar { min-height:64px; }
    .main {
        grid-template-columns:1fr;
        grid-template-rows:auto auto;
        padding:12px 14px;
    }
    .camera-area {
        grid-row:1;
        min-height:0;
        grid-template-columns:repeat(2,minmax(0,1fr));
        grid-auto-rows:auto;
        gap:12px;
    }
    .camera:not(.expanded) { aspect-ratio:16/9; min-height:0; }
    .left {
        grid-row:2;
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        align-items:start;
        overflow:visible;
    }
    .drawer {
        bottom:calc(66px + env(safe-area-inset-bottom));
        height:min(72dvh,650px);
    }
    .drawer-body { grid-template-columns:1fr; overflow:auto; }
    .device-detail { border-right:0; border-bottom:1px solid var(--border); }
    .client-row { grid-template-columns:minmax(120px,2fr) 75px 110px; }
    .client-row .maccol { display:none; }
}

/* iPad / touch-tablet spacing polish only.
   Desktop PC and phone layouts remain unchanged. */
@media (hover:none) and (pointer:coarse) and (min-width:601px) and (max-width:1400px) {
    .panel-head {
        height:42px;
        padding-top:0;
        padding-bottom:0;
    }
    
        /* iPad/tablet is a simple control view: hide technical Details rows
       in both left-side panels to save space and avoid accidental taps. */
    .left details.super-detail {
        display:none;
    }
    
    .sensor { padding:12px; }
    .super-body { padding:9px 11px; }

    .relay-device-head { margin-bottom:6px; }
    .relay-device + .relay-device {
        margin-top:8px;
        padding-top:8px;
    }
    .relay-grid { gap:7px; }
    .relay { height:68px; }
    .relay .relay-state {
        font-size:19px;
        margin-top:3px;
    }
    .relay .relay-name { font-size:10px; }
    .relay-device details.super-detail { margin-top:4px !important; }
}

/* iPad/tablet portrait polish: contain tiny horizontal overflow without
   changing the landscape/desktop layout or tablet grid. */
@media (min-width:601px) and (max-width:900px) {
    html, body {
        width:100%;
        max-width:100vw;
        overflow-x:hidden;
    }
    .app, .topbar, .main, .left, .camera-area, .panel,
    .network-bar, .device-pills, .drawer, .drawer-body {
        min-width:0;
        max-width:100%;
    }
    .statusbar { min-width:0; }
}

/* Phone layout: one camera per row, large touch controls, compact header. */
@media (max-width:600px) {
    .topbar {
        min-height:54px;
        gap:8px;
        padding:env(safe-area-inset-top) max(10px, env(safe-area-inset-right)) 0 max(10px, env(safe-area-inset-left));
    }
    .brand { gap:0; }
    .brand h1 { font-size:18px; letter-spacing:-.2px; }
    .site { display:none; }
    .statusbar { justify-content:center; gap:4px; overflow:visible; }
    .status-pill {
        width:28px; height:28px; padding:0;
        justify-content:center; gap:0;
        border-color:#202a36;
    }
    .status-pill span:not(.dot) { display:none; }
    .statusbar .optional { display:flex; }
    .clock { flex:0 0 auto; width:auto; min-width:44px; }
    .clock-time { font-size:14px; font-weight:700; }
    .clock-date { display:none; }

    .main { gap:10px; padding:10px; }
    .camera-area { grid-template-columns:1fr; gap:10px; }
    .camera:not(.expanded) { aspect-ratio:16/9; border-radius:14px; }
    .camera-overlay { padding:10px 11px; }
    .cam-name { font-size:14px; }
    .camera-hint { bottom:9px; padding:6px 10px; font-size:11px; }
    .package-inset {
        right:8px;
        bottom:8px;
        width:clamp(108px,35%,150px);
        border-radius:10px;
    }

    .left { grid-template-columns:1fr; gap:10px; }
    #relayPanel { order:-1; }
    .panel { border-radius:14px; }
    .panel-head { height:42px; padding:0 12px; }
    .panel-title { font-size:14px; }
    .sensor, .super-body { padding:12px; }
    .metric-grid { gap:7px; }
    .metric { min-height:62px; padding:9px 10px; }
    .metric-value { font-size:20px; }
    .relay-grid { gap:7px; }
    .relay { height:76px; border-radius:12px; }
    .relay .relay-state { font-size:20px; }

    .network-bar {
        position:sticky;
        bottom:0;
        z-index:30;
        flex-direction:column;
        align-items:stretch;
        gap:6px;
        min-height:calc(100px + env(safe-area-inset-bottom));
        padding:6px max(10px, env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-left));
    }
    .network-toggle { width:100%; min-width:0; height:42px; flex:0 0 auto; }
    .device-pills {
        display:flex;
        width:100%;
        gap:6px;
        overflow-x:auto;
        overflow-y:hidden;
        padding:0 0 1px;
        scroll-snap-type:x proximity;
        -webkit-overflow-scrolling:touch;
    }
    .device-pill {
        flex:0 0 auto;
        height:40px;
        min-width:max-content;
        padding:0 10px;
        font-size:12px;
        scroll-snap-align:start;
    }
    .device-pill .count { font-size:10px; }

    .drawer {
        bottom:calc(100px + env(safe-area-inset-bottom));
        height:auto;
        max-height:min(74dvh,650px);
    }
    .drawer-head { padding:10px 12px; flex:0 0 auto; }
    .drawer-body {
        flex:0 1 auto;
        display:flex;
        flex-direction:column;
        min-height:0;
        overflow:hidden;
    }
    .device-detail {
        flex:0 0 auto;
        padding:14px;
        max-height:34dvh;
        overflow:auto;
    }
    .clients-panel {
        flex:0 1 auto;
        min-height:0;
        max-height:38dvh;
    }
    .client-list {
        min-height:0;
        max-height:31dvh;
        overflow:auto;
    }
    .drawer.no-clients .clients-panel { display:none !important; }
    .drawer.no-clients .device-detail { max-height:62dvh; }
    .drawer.no-clients .drawer-body { overflow:auto; }

    /* Cellular backup devices are a true one-pane drawer on mobile.
       Give the detail pane the full drawer height instead of leaving the
       lower half reserved for a client list that will never exist. */
    .drawer.cellular-only {
        height:min(74dvh,650px);
        max-height:min(74dvh,650px);
    }
    .drawer.cellular-only .drawer-body {
        flex:1 1 auto;
        min-height:0;
        overflow:hidden;
    }
    .drawer.cellular-only .device-detail {
        flex:1 1 auto;
        max-height:none;
        min-height:0;
        overflow:auto;
        padding-bottom:22px;
    }
    .drawer.cellular-only .clients-panel { display:none !important; }

    .clients-head { padding:10px 12px; gap:8px; }
    .client-row {
        height:50px;
        grid-template-columns:minmax(110px,2fr) 74px 88px;
        gap:8px; padding:0 10px; font-size:11px;
    }
    .toast {
        top:calc(62px + env(safe-area-inset-top));
        left:10px; right:10px;
        text-align:center;
    }

    .camera.expanded { width:100vw; height:100dvh; }
    .camera.expanded .camera-overlay {
        padding-top:max(12px, env(safe-area-inset-top));
        padding-left:max(12px, env(safe-area-inset-left));
        padding-right:max(12px, env(safe-area-inset-right));
    }
    .camera.expanded .camera-hint {
        bottom:max(18px, calc(env(safe-area-inset-bottom) + 10px));
    }
}
</style>
</head>
<body>
<div class="app">
    <header class="topbar">
        <div class="brand">
            <h1>UniFi Home</h1>
            <div class="site" id="siteName">Connecting…</div>
        </div>

        <div class="statusbar">
            <div class="status-pill"><span class="dot" id="networkDot"></span><span id="networkStatus">Network —</span></div>
            <div class="status-pill"><span class="dot" id="protectDot"></span><span id="protectStatus">Protect —</span></div>
            <div class="status-pill"><span class="dot" id="cameraDot"></span><span id="cameraStatus">Cameras —</span></div>
            <div class="status-pill optional"><span class="dot" id="sensorDot"></span><span id="sensorStatus">Sensors —</span></div>
        </div>

        <div class="clock">
            <div class="clock-time" id="clockTime">--:--</div>
            <div class="clock-date" id="clockDate">—</div>
        </div>
    </header>

    <main class="main">
        <section class="left">
            <div class="panel" id="sensorPanel">
                <div class="panel-head">
                    <div class="panel-title">Environment</div>
                </div>
                <div id="sensors"></div>
            </div>

            <div class="panel" id="relayPanel">
                <div class="panel-head">
                    <div class="panel-title">Relays</div>
                </div>
                <div class="super-body" id="superLink"></div>
            </div>
        </section>

        <section class="camera-area" id="cameras"></section>
    </main>

    <footer class="network-bar">
        <button class="network-toggle" onclick="toggleNetwork()">▸ NETWORK <span id="networkCount"></span></button>
        <div class="device-pills" id="devicePills"></div>
    </footer>
</div>

<div class="drawer" id="networkDrawer">
    <div class="drawer-head">
        <div class="drawer-title" id="drawerTitle">Network</div>
        <button class="drawer-close" onclick="closeNetwork()">×</button>
    </div>
    <div class="drawer-body">
        <div class="device-detail" id="deviceDetail"></div>
        <div class="clients-panel">
            <div class="clients-head">
                <strong>Attached clients</strong>
                <label class="client-count">Sort by
                    <select id="clientSort" onchange="clientSort=this.value; renderClientPage()" style="background:var(--panel);color:var(--text);padding:8px;border:1px solid var(--border);border-radius:6px">
                        <option value="ip">IP address</option><option value="name">Name</option>
                    </select>
                </label>
                <span class="client-count" id="attachedCount">—</span>
            </div>
            <div class="client-list" id="clientList"></div>

        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let latest = null;
let currentDevice = null;
let currentClients = [];
let currentStats = {};
let clientSort = "ip";
let cameraSignature = "";

function esc(v) {
    return String(v ?? "")
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}

function setDot(id, good, warning=false) {
    const e = document.getElementById(id);
    e.className = "dot" + (good ? "" : (warning ? " warn" : " bad"));
}

function toast(message, error=false) {
    const t = document.getElementById("toast");
    t.textContent = message;
    t.className = "toast show" + (error ? " error" : "");
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => t.className = "toast", 2600);
}

function updateClock() {
    const now = new Date();
    document.getElementById("clockTime").textContent =
        now.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
    document.getElementById("clockDate").textContent =
        now.toLocaleDateString([], {weekday:"short", day:"2-digit", month:"short"});
}
updateClock();
setInterval(updateClock, 1000);

function numberOrDash(v, decimals=0) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Number(v).toFixed(decimals);
}

function deviceConnection(state) {
    const connected = state === 'CONNECTED';
    return `<span class="dot ${connected ? '' : 'bad'}" role="img" aria-label="${connected ? 'Connected' : 'Disconnected'}" title="${connected ? 'Connected' : 'Disconnected'}"></span>${connected ? '' : '<span class="small-state" style="color:var(--red)">DISCONNECTED</span>'}`;
}
function renderSensors(sensors) {
    const holder = document.getElementById("sensors");

    if (!sensors.length) {
        holder.innerHTML = `<div class="sensor" style="color:var(--muted)">No Protect sensors found.</div>`;
        return;
    }

    holder.innerHTML = sensors.map(s => {
        const opened = s.isOpened === true;
        const mount = String(s.mountType || '').trim().toLowerCase();
        const contactLabel = ({door:'DOOR', window:'WINDOW', garage:'GARAGE'})[mount] || 'CONTACT';
        const doorText = s.isOpened === null || s.isOpened === undefined
            ? "UNKNOWN"
            : (opened ? "OPEN" : "CLOSED");

        return `
        <div class="sensor">
            <div class="sensor-top">
                <div>
                    <div class="sensor-name device-heading">${esc(s.name)} ${deviceConnection(s.state)}</div>
                </div>
                <div class="door ${opened ? "open" : ""}">${contactLabel} ${doorText}</div>
            </div>
            <div class="metric-grid">
                <div class="metric">
                    <div class="metric-label">Temperature</div>
                    <div class="metric-value">${numberOrDash(s.temperature,1)}<span class="metric-unit">°C</span></div>
                </div>
                <div class="metric">
                    <div class="metric-label">Humidity</div>
                    <div class="metric-value">${numberOrDash(s.humidity)}<span class="metric-unit">%</span></div>
                </div>
                <div class="metric">
                    <div class="metric-label">Light</div>
                    <div class="metric-value">${numberOrDash(s.light)}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Battery</div>
                    <div class="metric-value">${numberOrDash(s.battery)}<span class="metric-unit">%</span></div>
                </div>
            </div>
        </div>`;
    }).join("");
}

function renderRelays(relays) {
    const holder = document.getElementById("superLink");

    if (!relays.length) {
        holder.innerHTML = `<div style="color:var(--muted)">No SuperLink relays found.</div>`;
        return;
    }

    holder.innerHTML = relays.map(relay => {
        const outputs = Array.isArray(relay.outputs) ? relay.outputs : [];

        const buttons = outputs.length
            ? `<div class="relay-grid">${outputs.map(o => `
                <button class="relay ${o.state === "on" ? "on" : "off"}"
                        data-relay-id="${esc(relay.id)}"
                        data-output-id="${o.id}"
                        data-state="${esc(o.state)}"
                        onclick="relayClick(this)">
                    <span class="relay-name">${esc(o.name || ("Relay " + (o.id+1)))}</span>
                    <span class="relay-state">${esc(String(o.state || "unknown").toUpperCase())}</span>
                </button>
            `).join("")}</div>`
            : `<div class="super-detail" style="margin-bottom:8px">No relay outputs reported.</div>`;

        return `
            <div class="relay-device">
                <div class="relay-device-head">
                    <div class="relay-device-name device-heading">
                        ${esc(relay.name || "SuperLink Relay")} ${deviceConnection(relay.state)}
                    </div>
                </div>
                ${buttons}
            </div>
        `;
    }).join("");
}

async function relayClick(button) {
    const relayId = button.dataset.relayId;
    const outputId = Number(button.dataset.outputId);
    const currentState = button.dataset.state;
    const desired = currentState === "on" ? "off" : "on";
    const label = button.querySelector(".relay-name").textContent;

    button.classList.add("busy");

    try {
        const r = await fetch(`/api/relay/${encodeURIComponent(relayId)}/${outputId}`, {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({state:desired})
        });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);

        toast(`${label}: ${desired.toUpperCase()}`);
        await update();
    } catch (e) {
        toast(`Relay failed: ${e.message}`, true);
    } finally {
        button.classList.remove("busy");
    }
}

let focusedCameraId = null;
function applyCameraFocus() {
    const tiles = [...document.querySelectorAll('#cameras > .camera')];
    if (!tiles.some(tile => tile.dataset.cameraId === focusedCameraId)) focusedCameraId = null;
    document.body.classList.toggle('camera-focused', focusedCameraId !== null);
    tiles.forEach(tile => {
        const expanded = tile.dataset.cameraId === focusedCameraId;
        tile.classList.toggle('expanded', expanded);
        tile.setAttribute('aria-expanded', String(expanded));
        tile.querySelector('.camera-hint').textContent = expanded ? 'Tap anywhere to return' : 'Tap to enlarge';
    });
}
function toggleCameraFocus(tile) {
    focusedCameraId = focusedCameraId === tile.dataset.cameraId ? null : tile.dataset.cameraId;
    applyCameraFocus();
    const player = livePlayers.get(tile.dataset.cameraId);
    if (focusedCameraId === tile.dataset.cameraId && player) player.playFromGesture();
    tile.focus({preventScroll:true});
}
let focusedPackageKey = null;
function applyPackageFocus() {
    const insets = [...document.querySelectorAll('.package-inset')];
    if (!insets.some(inset => inset.dataset.packageKey === focusedPackageKey)) focusedPackageKey = null;
    document.body.classList.toggle('package-focused', focusedPackageKey !== null);
    insets.forEach(inset => {
        const expanded = inset.dataset.packageKey === focusedPackageKey;
        inset.classList.toggle('expanded-package', expanded);
        inset.setAttribute('aria-expanded', String(expanded));
        const hint = inset.querySelector('.package-inset-hint');
        if (hint) hint.textContent = expanded ? 'Tap to return' : 'Tap package';
    });
}
function togglePackageFocus(inset) {
    focusedPackageKey = focusedPackageKey === inset.dataset.packageKey ? null : inset.dataset.packageKey;
    applyPackageFocus();
    const player = livePlayers.get(inset.dataset.packageKey);
    if (focusedPackageKey && player) player.playFromGesture();
}

document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && focusedPackageKey !== null) {
        focusedPackageKey = null; applyPackageFocus(); return;
    }
    if (event.key === 'Escape' && focusedCameraId !== null) {
        focusedCameraId = null; applyCameraFocus();
    }
});
const livePlayers = new Map();

function hasBrowserMediaSource() {
    return !!(window.MediaSource || window.ManagedMediaSource);
}
function chooseMediaSource(mime) {
    // Preserve regular MSE where it already works. iPhone WebKit uses
    // ManagedMediaSource, which requires remote playback to be disabled.
    if (window.MediaSource && typeof window.MediaSource.isTypeSupported === 'function'
            && window.MediaSource.isTypeSupported(mime)) {
        return {ctor:window.MediaSource, managed:false};
    }
    if (window.ManagedMediaSource && typeof window.ManagedMediaSource.isTypeSupported === 'function'
            && window.ManagedMediaSource.isTypeSupported(mime)) {
        return {ctor:window.ManagedMediaSource, managed:true};
    }
    return null;
}

class LiveCamera {
    constructor(tile, id, options={}) {
        this.tile = tile; this.id = id;
        this.lens = options.lens === 1 ? 1 : 0;
        this.packageView = this.lens === 1;
        this.video = tile.querySelector('video');
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.disableRemotePlayback = true;
        this.img = tile.querySelector('img');
        this.label = tile.querySelector('.stream-status');
        this.waitingForGesture = false;
        this.generation = 0; this.disposed = false; this.failures = 0;
        this.fallback = true; this.snapshot(); this.connect();
        this.snapshotTimer = setInterval(() => { if (this.fallback) this.snapshot(); }, 2000);
    }
    snapshot() {
        if (this.loading || this.disposed) return;
        const image = new Image(); this.loading = true;
        image.onload = () => {
            this.loading = false;
            if (!this.disposed && this.fallback) this.img.src = image.src;
        };
        image.onerror = () => { this.loading = false; };
        const path = this.packageView ? 'package-snapshot' : 'snapshot';
        image.src = `/camera/${encodeURIComponent(this.id)}/${path}?t=${Date.now()}`;
    }
    resetMedia() {
        this.mediaGeneration = (this.mediaGeneration || 0) + 1;
        this.video.onplaying = this.video.onerror = this.video.ontimeupdate = null;
        this.video.pause(); this.video.removeAttribute('src'); this.video.load();
        if (this.objectURL) URL.revokeObjectURL(this.objectURL);
        this.objectURL = null; this.media = null; this.buffer = null; this.queue = []; this.bytes = 0;
        this.waitingForGesture = false;
    }
    stopStream() {
        this.generation++;
        clearInterval(this.watchdog);
        if (this.socket) {
            this.socket.onopen = this.socket.onmessage = this.socket.onerror = this.socket.onclose = null;
            this.socket.close(); this.socket = null;
        }
        this.resetMedia();
    }
    fail(reason) {
        if (this.disposed) return;
        this.stopStream(); this.fallback = true;
        this.video.style.display = 'block'; this.img.style.display = 'block';
        this.label.textContent = `SNAPSHOT 路 ${reason}`;
        this.snapshot(); clearTimeout(this.retry);
        if (hasBrowserMediaSource()) this.retry = setTimeout(() => this.connect(), Math.min(60000, 5000 * 2 ** Math.min(this.failures++, 4)));
    }
    connect() {
        if (this.disposed) return;
        if (!hasBrowserMediaSource()) { this.fail('live video unsupported'); return; }
        this.stopStream();
        const gen = this.generation;
        this.label.textContent = 'CONNECTING 路 snapshot';
        this.lastData = this.lastProgress = Date.now(); this.previousTime = -1;
        try {
            const lensQuery = this.packageView ? '?lens=1' : '';
            this.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/camera/${encodeURIComponent(this.id)}/live${lensQuery}`);
            this.socket.binaryType = 'arraybuffer';
            this.socket.onmessage = event => {
                if (gen !== this.generation) return;
                try {
                    this.lastData = Date.now();
                    if (typeof event.data === 'string') {
                        const meta = JSON.parse(event.data);
                        if (meta.type === 'error') { this.fail(meta.message); return; }
                        if (meta.type === 'status') { this.label.textContent = meta.message; return; }
                        if (meta.type !== 'init' || typeof meta.codec !== 'string') throw Error('invalid stream');
                        this.initialize(meta.codec, gen);
                    } else {
                        if (!this.media) throw Error('missing stream header');
                        this.queue.push(event.data); this.bytes += event.data.byteLength;
                        if (this.bytes > 16 * 1024 * 1024) throw Error('playback too slow');
                        this.pump();
                    }
                } catch (error) { this.fail(error.message || 'playback failed'); }
            };
            this.socket.onerror = () => { if (gen === this.generation) this.fail('dashboard WebSocket failed; see /api/live/diagnostics'); };
            this.socket.onclose = () => { if (gen === this.generation) this.fail('live disconnected'); };
            this.watchdog = setInterval(() => {
                if (Date.now() - this.lastData > 12000 ||
                    (!this.waitingForGesture && Date.now() - this.lastProgress > 15000)) this.fail('live stalled');
            }, 2000);
        } catch { this.fail('live unavailable'); }
    }
    initialize(codec, gen) {
        const mime = codec.includes('/') ? codec : `video/mp4; codecs="${codec}"`;
        const sourceChoice = chooseMediaSource(mime);
        if (!sourceChoice) throw Error(`codec unsupported: ${codec}`);
        this.resetMedia();
        const mg = this.mediaGeneration;
        this.fallback = true; this.img.style.display = 'block'; this.video.style.display = 'block';
        this.video.disableRemotePlayback = true;
        this.media = new sourceChoice.ctor();
        this.objectURL = URL.createObjectURL(this.media); this.video.src = this.objectURL;
        this.video.onplaying = () => {
            this.fallback = false; this.failures = 0; this.waitingForGesture = false;
            this.video.style.display = 'block'; this.img.style.display = 'none';
            this.label.textContent = this.packageView ? 'LIVE' : 'LIVE';
        };
        this.video.ontimeupdate = () => {
            if (this.video.currentTime !== this.previousTime) {
                this.lastProgress = Date.now(); this.previousTime = this.video.currentTime;
            }
        };
        this.video.onerror = () => this.fail('decode failed');
        this.media.addEventListener('sourceopen', () => {
            if (gen !== this.generation || mg !== this.mediaGeneration) return;
            try {
                this.buffer = this.media.addSourceBuffer(mime);
                this.buffer.addEventListener('updateend', () => { if (gen === this.generation && mg === this.mediaGeneration) this.pump(); });
                this.buffer.addEventListener('error', () => { if (gen === this.generation && mg === this.mediaGeneration) this.fail('decode failed'); });
                this.pump();
                this.video.play().catch(() => {
                    if (gen === this.generation && mg === this.mediaGeneration) {
                        this.waitingForGesture = true;
                        this.label.textContent = 'TAP FOR LIVE';
                    }
                });
            } catch { this.fail('codec unsupported'); }
        }, { once: true });
    }
    playFromGesture() {
        if (this.disposed || !this.video) return;
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.disableRemotePlayback = true;
        const attempt = this.video.play();
        if (attempt && typeof attempt.catch === 'function') {
            attempt.catch(() => {
                this.waitingForGesture = true;
                this.label.textContent = 'TAP FOR LIVE';
            });
        }
    }
    pump() {
        const sb = this.buffer;
        if (!sb || sb.updating || this.disposed) return;
        try {
            if (sb.buffered.length) {
                const end = sb.buffered.end(sb.buffered.length - 1);
                if (end - this.video.currentTime > 3) this.video.currentTime = Math.max(sb.buffered.start(sb.buffered.length - 1), end - 0.5);
                const cutoff = this.video.currentTime - 15;
                if (cutoff > sb.buffered.start(0) + 2) { sb.remove(0, cutoff); return; }
            }
            if (this.queue.length) {
                const data = this.queue.shift(); this.bytes -= data.byteLength; sb.appendBuffer(data);
            }
        } catch { this.fail('buffer reset'); }
    }
    dispose() {
        this.disposed = true; clearTimeout(this.retry); clearInterval(this.snapshotTimer); this.stopStream();
    }
}
class RemoteSnapshotCamera {
    constructor(tile, id) {
        this.tile = tile;
        this.id = id;
        this.img = tile.querySelector('img');
        this.label = tile.querySelector('.stream-status');
        this.dot = tile.querySelector('.cam-state .dot');
        this.disposed = false;
        this.loading = false;
        this.snapshot();
        this.snapshotTimer = setInterval(() => this.snapshot(), 10000);
    }
    snapshot() {
        if (this.loading || this.disposed) return;
        const image = new Image();
        this.loading = true;
        image.onload = () => {
            this.loading = false;
            if (this.disposed) return;
            this.img.src = image.src;
            this.img.style.display = 'block';
            this.label.textContent = 'REMOTE';
            if (this.dot) this.dot.classList.remove('bad');
        };
        image.onerror = () => {
            this.loading = false;
            if (this.disposed) return;
            this.label.textContent = 'REMOTE OFFLINE';
            if (this.dot) this.dot.classList.add('bad');
        };
        image.src = `/remote-camera/snapshot?t=${Date.now()}`;
    }
    playFromGesture() { this.snapshot(); }
    dispose() {
        this.disposed = true;
        clearInterval(this.snapshotTimer);
    }
}

function renderCameras(cameras) {
    const holder = document.getElementById('cameras');
    document.querySelector('.main')?.classList.toggle('two-camera-rows', cameras.length >= 3);
    const signature = cameras.map(c => `${c.id}:${c.state}:${c.name}:${c.remote ? 1 : 0}:${c.hasPackageCamera ? 1 : 0}`).join('|');
    if (signature === cameraSignature) return;
    cameraSignature = signature;
    for (const player of livePlayers.values()) player.dispose();
    livePlayers.clear();
    if (!cameras.length) { holder.innerHTML = '<div class="no-camera">No Protect cameras found.</div>'; applyCameraFocus(); return; }
    holder.classList.toggle('single-camera', cameras.length === 1);
    holder.innerHTML = cameras.map(c => {
        const online = c.state === 'CONNECTED';
        const remote = !!c.remote;
        const packageInset = online && !remote && c.hasPackageCamera ? `
            <div class="package-inset"
                 data-package-key="${esc(c.id)}:package"
                 role="button"
                 tabindex="0"
                 aria-label="${esc(c.name)} package camera: enlarge or return"
                 aria-expanded="false">
                <img alt="${esc(c.name)} package camera">
                <video autoplay muted playsinline webkit-playsinline disableRemotePlayback></video>
                <div class="package-overlay">
                    <span class="package-title">PACKAGE</span>
                    <span class="package-state"><span class="dot"></span><span class="stream-status">CONNECTING</span></span>
                </div>
                <span class="package-inset-hint">Tap package</span>
            </div>` : '';

        return `<div class="camera" data-camera-id="${esc(c.id)}" role="button" tabindex="0" aria-label="${esc(c.name)}: enlarge or return" aria-expanded="false">
            <span class="camera-hint">Tap to enlarge</span>
            ${online ? (remote ? `<img alt="${esc(c.name)}">` : `<img alt="${esc(c.name)}"><video autoplay muted playsinline webkit-playsinline disableRemotePlayback></video>`) :
            (remote ? `<img alt="${esc(c.name)}"><div class="camera-offline"><div class="offline-title">REMOTE</div><div>Snapshot unavailable</div></div>` : `<div class="camera-offline"><div class="offline-title">DISCONNECTED</div><div>${esc(c.name)}</div></div>`)}
            <div class="camera-overlay"><div class="cam-name">${esc(c.name)}</div>
            <div class="cam-state"><span class="dot ${online ? '' : 'bad'}"></span><span class="stream-status">${remote ? (online ? 'REMOTE' : 'REMOTE OFFLINE') : esc(c.state)}</span></div></div>
            ${packageInset}
        </div>`;
    }).join('');

    [...holder.children].forEach((tile, i) => {
        const camera = cameras[i];
        tile.addEventListener('click', () => toggleCameraFocus(tile));
        tile.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleCameraFocus(tile); }
        });

        if (camera.remote) {
            livePlayers.set(camera.id, new RemoteSnapshotCamera(tile, camera.id));
        } else if (camera.state === 'CONNECTED') {
            livePlayers.set(camera.id, new LiveCamera(tile, camera.id));

            const inset = tile.querySelector('.package-inset');
            if (inset) {
                const packageKey = `${camera.id}:package`;
                inset.addEventListener('click', event => {
                    event.stopPropagation();
                    togglePackageFocus(inset);
                });
                inset.addEventListener('keydown', event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        event.stopPropagation();
                        togglePackageFocus(inset);
                    }
                });
                livePlayers.set(packageKey, new LiveCamera(inset, camera.id, {lens:1}));
            }
        }
    });
    applyCameraFocus();
    applyPackageFocus();
}
window.addEventListener('pagehide', () => { for (const p of livePlayers.values()) p.dispose(); });
window.addEventListener('pageshow', event => { if (event.persisted) { cameraSignature = ''; update(); } });

function renderNetworkDevices(devices) {
    const holder = document.getElementById("devicePills");

    holder.innerHTML = devices.map(d => `
        <button class="device-pill" onclick="openDevice('${esc(d.id)}')">
            <span class="dot ${d.state === "ONLINE" ? "" : "bad"}"></span>
            <span>${esc(d.name)}</span>
            <span class="count">${d.attachedClients || 0}</span>
        </button>
    `).join("");
}

function toggleNetwork() {
    const drawer = document.getElementById("networkDrawer");
    if (drawer.classList.contains("open")) {
        closeNetwork();
    } else {
        drawer.classList.add("open");
        if (!currentDevice && latest && latest.devices.length) {
            openDevice(latest.devices[0].id);
        }
    }
}

function closeNetwork() {
    document.getElementById("networkDrawer").classList.remove("open");
}

async function openDevice(deviceId) {
    document.getElementById("networkDrawer").classList.add("open");
    document.getElementById("drawerTitle").textContent = "Loading…";

    try {
        const r = await fetch(`/api/network/device/${encodeURIComponent(deviceId)}/clients`, {cache:"no-store"});
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);

        currentDevice = data.device;
        currentClients = data.clients;
        currentStats = data.stats || {};

        document.getElementById("drawerTitle").textContent = currentDevice.name;
        renderDeviceDetail();
        renderClientPage();
    } catch (e) {
        toast(`Device details failed: ${e.message}`, true);
    }
}

function isCellularDevice(d) {
    if (!d) return false;
    const text = `${d.name || ""} ${d.model || ""}`.toLowerCase();
    return (
        text.includes("u5g") ||
        text.includes("5g backup") ||
        text.includes("5g max") ||
        text.includes("lte backup") ||
        text.includes("u-lte")
    );
}

function formatUptime(seconds) {
    if (seconds === null || seconds === undefined || isNaN(Number(seconds))) return "—";
    let s = Math.max(0, Math.floor(Number(seconds)));
    const days = Math.floor(s / 86400);
    s %= 86400;
    const hours = Math.floor(s / 3600);
    s %= 3600;
    const minutes = Math.floor(s / 60);

    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

function formatRate(bps) {
    if (bps === null || bps === undefined || isNaN(Number(bps))) return "—";
    const n = Number(bps);
    if (n >= 1e9) return `${(n/1e9).toFixed(2)} Gbps`;
    if (n >= 1e6) return `${(n/1e6).toFixed(1)} Mbps`;
    if (n >= 1e3) return `${(n/1e3).toFixed(1)} Kbps`;
    return `${Math.round(n)} bps`;
}

function renderDeviceDetail() {
    const d = currentDevice;
    if (!d) return;

    const cellular = isCellularDevice(d);
    const s = currentStats || {};
    const uplink = s.uplink || {};

    let extra = "";

    if (cellular) {
        extra = `
            <div style="margin-top:17px;padding:13px;border:1px solid #254833;background:#102019;border-radius:11px">
                <div style="font-weight:700;color:var(--green)">CELLULAR BACKUP</div>
                <div class="super-detail" style="margin-top:5px">
                    This device is treated as a connectivity/backup link, not a client-serving AP or switch.
                </div>
            </div>

            <div class="detail-kv">
                <div class="kv"><span>Backup health</span><strong style="color:${d.state === "ONLINE" ? "var(--green)" : "var(--red)"}">${d.state === "ONLINE" ? "READY" : esc(d.state)}</strong></div>
                <div class="kv"><span>Uptime</span><span>${formatUptime(s.uptimeSec)}</span></div>
                <div class="kv"><span>Uplink receive</span><span>${formatRate(uplink.rxRateBps)}</span></div>
                <div class="kv"><span>Uplink transmit</span><span>${formatRate(uplink.txRateBps)}</span></div>
                <div class="kv"><span>CPU</span><span>${s.cpuUtilizationPct == null ? "—" : Number(s.cpuUtilizationPct).toFixed(1) + "%"}</span></div>
                <div class="kv"><span>Memory</span><span>${s.memoryUtilizationPct == null ? "—" : Number(s.memoryUtilizationPct).toFixed(1) + "%"}</span></div>
            </div>
        `;
    } else {
        extra = `
            <div class="detail-kv">
                <div class="kv"><span>Uptime</span><span>${formatUptime(s.uptimeSec)}</span></div>
                <div class="kv"><span>Uplink receive</span><span>${formatRate(uplink.rxRateBps)}</span></div>
                <div class="kv"><span>Uplink transmit</span><span>${formatRate(uplink.txRateBps)}</span></div>
                <div class="kv"><span>CPU</span><span>${s.cpuUtilizationPct == null ? "—" : Number(s.cpuUtilizationPct).toFixed(1) + "%"}</span></div>
                <div class="kv"><span>Memory</span><span>${s.memoryUtilizationPct == null ? "—" : Number(s.memoryUtilizationPct).toFixed(1) + "%"}</span></div>
            </div>
        `;
    }

    document.getElementById("deviceDetail").innerHTML = `
        <div style="display:flex;align-items:center;gap:8px">
            <span class="dot ${d.state === "ONLINE" ? "" : "bad"}"></span>
            <strong style="font-size:19px">${esc(d.name)}</strong>
        </div>
        <div class="model">${esc(d.model)}</div>

        <div class="detail-kv">
            <div class="kv"><span>Status</span><strong>${esc(d.state)}</strong></div>
            <div class="kv"><span>IP address</span><span>${esc(d.ip || "—")}</span></div>
            <div class="kv"><span>MAC</span><span>${esc(d.mac || "—")}</span></div>
            <div class="kv"><span>Firmware</span><span>${esc(d.firmware || "—")}</span></div>
            ${cellular ? "" : `<div class="kv"><span>Attached now</span><strong>${currentClients.length}</strong></div>`}
        </div>

        ${extra}
    `;

    // Cellular backup devices never need a client browser.  Also mark any
    // zero-client device so the mobile layout can omit an empty second pane.
    const noClientBrowser = cellular || currentClients.length === 0;
    const drawer = document.getElementById("networkDrawer");
    if (drawer) {
        drawer.classList.toggle("no-clients", noClientBrowser);
        drawer.classList.toggle("cellular-only", cellular);
    }

    const clientsPanel = document.querySelector(".clients-panel");
    if (clientsPanel) {
        // Preserve the existing desktop behaviour for ordinary zero-client
        // devices; mobile CSS hides those via .drawer.no-clients.
        clientsPanel.style.display = cellular ? "none" : "flex";
    }
}

function ipv4Number(ip) {
    const parts = String(ip || '').split('.');
    if (parts.length !== 4 || parts.some(p => !/^\d{1,3}$/.test(p) || Number(p) > 255)) return null;
    return parts.reduce((n, p) => n * 256 + Number(p), 0);
}
function compareClients(a, b) {
    const name = () => String(a.name || '').localeCompare(String(b.name || ''), undefined, {numeric:true, sensitivity:'base'});
    if (clientSort === 'name') return name() || String(a.ip || '').localeCompare(String(b.ip || ''), undefined, {numeric:true});
    const ai = ipv4Number(a.ip), bi = ipv4Number(b.ip);
    if (ai !== null && bi !== null) return ai - bi || name();
    if (ai !== null) return -1;
    if (bi !== null) return 1;
    const missing = ip => !ip || ip === '—' || ip === '-';
    if (missing(a.ip) !== missing(b.ip)) return missing(a.ip) ? 1 : -1;
    return String(a.ip || '').localeCompare(String(b.ip || ''), undefined, {numeric:true}) || name();
}

function renderClientPage() {
    const rows = [...currentClients].sort(compareClients);

    document.getElementById("attachedCount").textContent =
        `${currentClients.length} attached`;

    const list = document.getElementById("clientList");

    if (!rows.length) {
        list.innerHTML = `<div style="padding:28px;color:var(--muted)">
            No connected clients currently report this device as their uplink.
        </div>`;
    } else {
        list.innerHTML = rows.map(c => `
            <div class="client-row">
                <div class="cname">${esc(c.name)}</div>
                <div><span class="client-type">${esc(c.type)}</span></div>
                <div class="muted">${esc(c.ip)}</div>
                <div class="muted maccol">${esc(c.mac)}</div>
            </div>
        `).join("");
    }

    list.scrollTop = 0;
}

async function update() {
    try {
        const r = await fetch("/api/status", {cache:"no-store"});
        const data = await r.json();
        latest = data;
        const s = data.summary;

        document.getElementById("siteName").textContent =
            `${s.site} · Network ${data.network_version || "—"} · Protect ${data.protect_version || "—"}`;

        const netGood = data.network_ok && s.devices_online === s.devices_total;
        setDot("networkDot", netGood);
        document.getElementById("networkStatus").textContent =
            `Network ${s.devices_online}/${s.devices_total}`;

        const protectGood = data.protect_ok;
        setDot("protectDot", protectGood);
        document.getElementById("protectStatus").textContent =
            protectGood ? "Protect Online" : "Protect Error";

        const camsGood = s.cameras_total > 0 && s.cameras_connected === s.cameras_total;
        setDot("cameraDot", camsGood, s.cameras_connected > 0);
        document.getElementById("cameraStatus").textContent =
            `Cameras ${s.cameras_connected}/${s.cameras_total}`;

        const sensorsGood = s.sensors_total > 0 && s.sensors_connected === s.sensors_total;
        setDot("sensorDot", sensorsGood, s.sensors_connected > 0);
        document.getElementById("sensorStatus").textContent =
            `Sensors ${s.sensors_connected}/${s.sensors_total}`;

        document.getElementById("networkCount").textContent =
            `${s.devices_online}/${s.devices_total}`;

        renderSensors(data.sensors);
        renderRelays(data.relays);
        renderCameras(data.cameras);
        renderNetworkDevices(data.devices);

        // Keep open drawer counts reasonably fresh.
        if (currentDevice && document.getElementById("networkDrawer").classList.contains("open")) {
            const stillThere = data.devices.find(d => d.id === currentDevice.id);
            if (stillThere) {
                // Reload only if attached client count changed.
                const expected = stillThere.attachedClients || 0;
                if (expected !== currentClients.length) {
                    openDevice(currentDevice.id);
                }
            }
        }
    } catch (e) {
        toast(`Dashboard update failed: ${e.message}`, true);
    }
}

update();
setInterval(update, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        value = s.getsockname()[0]
        s.close()
        return value
    except Exception:
        return "YOUR-PC-IP"


def main():
    global CONTROLLER, API_KEY, NETWORK_BASE, PROTECT_BASE, REMOTE_API_KEY, REMOTE_CAMERA_NAME, REMOTE_CAMERA_ID, REMOTE_CONSOLE_ID

    parser = argparse.ArgumentParser(description="UniFi Protect-first dashboard v3")
    parser.add_argument("--lan", action="store_true", help="Allow other LAN devices to open the dashboard")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--config", help="Path to Unifi.ini (default: beside this script)")
    parser.add_argument("--no-live", action="store_true", help="Use snapshots without starting Node")
    args = parser.parse_args()

    print()
    print("UniFi Protect-First Dashboard v3")
    print("--------------------------------")

    CONTROLLER, API_KEY, config_path, remote_config = load_unifi_config(args.config)
    REMOTE_API_KEY = remote_config["api_key"]
    REMOTE_CAMERA_NAME = remote_config["camera_name"]
    REMOTE_CAMERA_ID = remote_config["camera_id"]
    REMOTE_CONSOLE_ID = remote_config["console_id"]
    setup_logging(config_path)

    NETWORK_BASE = f"https://{CONTROLLER}/proxy/network/integration/v1"
    PROTECT_BASE = f"https://{CONTROLLER}/proxy/protect/integration/v1"

    print(f"Config:     {config_path}")
    print(f"Controller: {CONTROLLER}")
    print()
    print("Testing Network + Protect APIs...")

    refresh_all()

    if remote_enabled():
        print("Remote:     configured (snapshot-only)")
        if not resolve_remote_camera(force=True):
            print("            unavailable now; local dashboard will continue and retry on demand")
    elif REMOTE_API_KEY:
        print("Remote:     key found, but no Remote_Camera_Name/Remote_Camera_ID; skipped")
    else:
        print("Remote:     disabled")

    with LOCK:
        print(f"Network:    {'OK' if CACHE['network_ok'] else 'FAILED'}  {CACHE['network_version'] or ''}")
        if CACHE["network_error"]:
            print(f"            {CACHE['network_error']}")

        print(f"Protect:    {'OK' if CACHE['protect_ok'] else 'FAILED'}  {CACHE['protect_version'] or ''}")
        if CACHE["protect_error"]:
            print(f"            {CACHE['protect_error']}")

        print(f"Devices:    {len(CACHE['devices'])}")
        print(f"Clients:    {len(CACHE['clients'])} (hidden from main screen)")
        print(f"Sensors:    {len(CACHE['sensors'])}")
        print(f"Bridges:    {len(CACHE['bridges'])}")
        print(f"Relays:     {len(CACHE['relays'])}")
        print(f"Local cams: {len(CACHE['cameras'])}")

    if not CACHE["network_ok"] and not CACHE["protect_ok"]:
        raise SystemExit("Both UniFi APIs failed. Check Unifi.ini and controller reachability.")

    if not args.no_live:
        start_bridge(config_path)

    thread = threading.Thread(target=refresh_loop, daemon=True)
    thread.start()

    host = "0.0.0.0" if args.lan else "127.0.0.1"

    print()
    print(f"Dashboard:  http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"LAN:        http://{local_ip()}:{args.port}")
        print("NOTE: Relay controls are reachable by devices that can access this dashboard.")
    print()
    print("Press Ctrl+C to stop.")
    print()

    try:
        app.run(host=host, port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        LOG.info("Dashboard stopping")
        stop_bridge()


if __name__ == "__main__":
    main()
