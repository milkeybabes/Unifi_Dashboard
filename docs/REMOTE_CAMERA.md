# Optional remote Protect snapshot camera

The remote-camera feature is deliberately **snapshot-only**. The official UniFi applications are better suited to remote live video, security, authentication and bandwidth management.

## Configuration

```ini
Remote_API_Key=YOUR_UI_CLOUD_API_KEY
Remote_Camera_Name=Front Garden
```

Instead of the camera name you may use:

```ini
Remote_Camera_ID=CAMERA_ID
```

If matching cameras can exist on more than one console, also set:

```ini
Remote_Console_ID=CONSOLE_ID
```

If `Remote_API_Key` is absent, the dashboard skips the entire remote feature. If the key exists without a camera name or ID, the dashboard also skips it rather than choosing a camera automatically.

## How it works

The dashboard server:

1. discovers consoles visible to the remote key (unless `Remote_Console_ID` is set),
2. opens the Protect Integration API through Ubiquiti's cloud connector,
3. finds the explicitly configured camera, and
4. requests its normal JPEG/PNG snapshot.

The browser never receives the remote API key. It only requests `/remote-camera/snapshot` from the dashboard itself.

The tile refreshes approximately every 10 seconds. A short server-side cache prevents several dashboard clients from needlessly requesting the same cloud snapshot at the same instant.

A remote outage is isolated to the remote tile; the local dashboard continues operating.

## Remote access to the dashboard

Do not forward the dashboard's HTTP port directly to the public Internet. The dashboard contains relay controls and does not provide an Internet-facing authentication layer.

Use a trusted VPN to reach the LAN. An authenticated HTTPS reverse proxy with correct WebSocket forwarding is another advanced option, but VPN access is the recommended approach.


## Why snapshot-only?

This feature is intended for a quick glance at another site, not as a replacement for the UniFi mobile/web applications.

Keeping the remote camera snapshot-only has several advantages:

- very low continuous bandwidth compared with remote live video,
- no additional public-facing stream endpoint,
- no need for remote Protect login credentials in the browser,
- failures at the remote site remain isolated from the local dashboard,
- the official UniFi applications remain the preferred choice when full remote live video, playback and authentication are required.

## Privacy and API-key handling

`Remote_API_Key` is treated as a secret:

- it is read only by the dashboard server,
- it is sent to `api.ui.com` in the server-side `X-API-Key` header,
- it is never embedded in the HTML/JavaScript sent to browsers,
- the dashboard log redaction list includes `Remote_API_Key`.

Do not commit a real remote API key to Git.
