# Updating an existing Docker / NAS installation

If you already have a working UniFi Protect-First Dashboard container, the safest upgrade is to **keep the proven Docker configuration and replace only the application code**.

This is especially useful on NAS devices where the Docker image has already been built successfully and you do not want to disturb credentials, persistent logs, ports, restart policy, or other working settings.

## Before updating

Make a backup of the existing project folder.

Example for the ASUSTOR layout used during development:

```bash
cd /volume1/Docker
cp -a UniFi-Dashboard UniFi-Dashboard_backup_$(date +%d-%m-%Y)
```

Your path may be different.

## Files that normally need replacing

For a normal code-only update, replace:

```text
app/unifi_dashboard.py
app/unifi_live_bridge.mjs
```

Keep these existing files unless the release notes explicitly say they changed:

```text
config/Unifi.ini
docker-compose.yml
Dockerfile
start.sh
logs/
```

Do **not** overwrite your real `Unifi.ini` with the example file from GitHub. This also preserves optional remote-camera settings such as `Remote_API_Key` and `Remote_Camera_Name`.

## Rebuild and recreate the container

Replacing files in the project directory does not update an already-built Docker image by itself.

Run:

```bash
cd /path/to/UniFi-Dashboard
docker compose build
docker compose up -d
```

`docker compose up -d` recreates the container from the newly built image while keeping the normal bind mounts / volumes defined by Compose.

You normally do **not** need to run `docker compose down` first.

## Check the result

```bash
docker compose ps
docker compose logs --tail=50
```

Then confirm dashboard diagnostics:

```bash
curl -s http://127.0.0.1:8088/api/live/diagnostics
```

The reported version should match the release you installed.

## Upgrading from an older repository layout

Older builds used a versioned Python filename such as:

```text
app/unifi_dashboard_v3.py
```

If your existing `Dockerfile` or `start.sh` still refers to that old filename, you have two choices:

1. **Quick/safe upgrade:** copy the new `app/unifi_dashboard.py` over the old filename expected by your existing Docker setup.
2. **Clean migration:** update the Dockerfile/start script to use `app/unifi_dashboard.py`, then rebuild.

For a working NAS installation, option 1 is often the lowest-risk update. Once the new version is confirmed working, you can migrate the filenames later if you want the folder to match the current GitHub layout exactly.

## When dependency layers also need rebuilding

A rebuild is required whenever application source files change because those files are copied into the Docker image.

If `requirements.txt`, `package.json`, `package-lock.json`, the base image, or system dependencies change, Docker may also need to rebuild dependency layers. The release notes should call this out.

## Rollback

If an update causes a problem, restore the backup project folder and rebuild it:

```bash
cd /volume1/Docker
rm -rf UniFi-Dashboard
mv UniFi-Dashboard_backup_DD-MM-YYYY UniFi-Dashboard
cd UniFi-Dashboard
docker compose build
docker compose up -d
```

Adjust the path and backup name to match your system.

## Security

The dashboard includes relay controls and is intended for a trusted LAN or trusted VPN.

Do not expose its HTTP port directly to the public Internet.
