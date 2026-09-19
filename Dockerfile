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

# Web UI + background sync loop in one process.
CMD ["python", "-m", "app.webui"]
