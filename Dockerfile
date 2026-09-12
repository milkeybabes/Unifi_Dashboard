FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY package.json package-lock.json /app/
RUN npm ci --omit=dev && npm cache clean --force

COPY app/unifi_dashboard.py /app/unifi_dashboard.py
COPY app/unifi_live_bridge.mjs /app/unifi_live_bridge.mjs
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh && mkdir -p /app/logs /config

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
 CMD /opt/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/api/status', timeout=3).read()" || exit 1

ENTRYPOINT ["/app/start.sh"]
