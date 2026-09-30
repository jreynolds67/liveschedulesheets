FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/data/config.yaml \
    WEB_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Seed config used to initialize /data/config.yaml on first run.
COPY config.example.yaml ./config.example.yaml

RUN mkdir -p /data
VOLUME ["/data"]

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app /data
USER appuser

EXPOSE 8080

# Unhealthy when the sync loop has died or a pass has been stuck for a long
# time (see /healthz). slim images have no curl, so use Python.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT', '8080'), timeout=8)"

# Web UI + background sync loop in one process.
CMD ["python", "-m", "app.webui"]
