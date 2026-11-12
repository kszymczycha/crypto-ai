# Builder for the collector (data role only) — it needs neither secrets nor FastAPI.
#   docker build -f docker/collector.Dockerfile -t okxbot-collector .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/pyproject.toml /app/src/pyproject.toml
RUN python -m pip install --no-cache-dir \
        "python-okx>=0.3.5,<0.4" "httpx>=0.27,<0.29" \
        "PyYAML>=6.0,<7" "structlog>=24.1,<26"

COPY src/ /app/src/
COPY config/ /app/config/

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app/data /app/logs
USER bot

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "okxbot.data.collector"]
