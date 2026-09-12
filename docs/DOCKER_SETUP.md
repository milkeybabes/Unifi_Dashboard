# Docker / NAS deployment

This is the clean deployment path used after development/testing on Windows.

The tested permanent host was an ASUSTOR AS6404T, but the same layout is suitable for ordinary Docker-capable Linux/NAS hosts.

## 1. Prepare configuration

```bash
cp config/Unifi.ini.example config/Unifi.ini
```

Edit it:

```ini
DeviceIP=192.168.1.1
API_Key=YOUR_UNIFI_INTEGRATION_API_KEY
Protect_User=YOUR_LOCAL_PROTECT_USERNAME
Protect_Password=YOUR_LOCAL_PROTECT_PASSWORD
```

## 2. Optional timezone

Copy:

```bash
cp .env.example .env
```

Then set, for example:

```text
TZ=Europe/London
```

or:

```text
TZ=Asia/Shanghai
```

## 3. Build

```bash
docker compose build
```

or:

```bash
docker compose up -d --build
```

## 4. Start

```bash
docker compose up -d
```

## 5. Check

```bash
docker compose ps
docker compose logs --tail=100
```

Expected startup includes successful Network and Protect API checks.

## 6. Open

```text
http://NAS-IP:8088
```

## Native live-video check

```text
http://NAS-IP:8088/api/live/diagnostics
```

Healthy output should show:

```text
helper_running: true
live_enabled: true
```

## Reboot test

The Compose file uses:

```yaml
restart: unless-stopped
```

After a NAS/server reboot:

```bash
docker compose ps
```

The dashboard should already be running again.

## Persistent data

The real config is mounted read-only:

```text
./config -> /config
```

Logs are persistent:

```text
./logs -> /app/logs
```

The Node native-video helper listens only on container loopback. Only the dashboard's port `8088` is exposed to the LAN.

## Updating

After replacing application files:

```bash
docker compose up -d --build
```

## Security

Do not port-forward 8088 to the public Internet. This UI can operate relay outputs.


## G6 Entry package camera

No extra container, port, FFmpeg process or transcoder is required for the package camera.

The same private Node helper retrieves the G6 Entry's second lens and sends it through the dashboard's existing same-origin WebSocket path.


## Updating an existing installation

For an existing working NAS/container deployment, use the safe update procedure in [`UPGRADING.md`](UPGRADING.md).

The important point is that replacing source files alone does **not** update an already-built container image. Rebuild with:

```bash
docker compose build
docker compose up -d
```

Keep your real `config/Unifi.ini` and persistent logs unless a release specifically says otherwise.


## Optional remote snapshot camera

The remote camera feature requires normal outbound HTTPS access from the dashboard container to `api.ui.com`.

It does **not** require an additional inbound port. The browser still talks only to the dashboard on port 8088.

Configure the feature in the existing private `config/Unifi.ini`; see [`REMOTE_CAMERA.md`](REMOTE_CAMERA.md).
