#!/bin/sh
set -eu

CONFIG="${UNIFI_CONFIG:-/config/Unifi.ini}"
PORT="${DASHBOARD_PORT:-8088}"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: UniFi config not found: $CONFIG"
  echo "Copy config/Unifi.ini.example to config/Unifi.ini and edit it."
  exit 1
fi

echo "Starting UniFi Protect-First Dashboard on port ${PORT}"
echo "Config: ${CONFIG}"

exec /opt/venv/bin/python /app/unifi_dashboard.py \
  --lan --port "${PORT}" --config "${CONFIG}"
