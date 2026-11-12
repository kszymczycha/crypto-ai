# Separate Dockerfile for the executor — used when you want to build only that role:
#   docker build -f docker/executor.Dockerfile -t okxbot-executor .
# The default stack (compose.yaml) builds the image from ./Dockerfile with the same start command.
# This file does NOT copy secrets/ or data/ (.dockerignore).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/pyproject.toml /app/src/pyproject.toml
RUN python -m pip install --no-cache-dir \
        "python-okx>=0.3.5,<0.4" "httpx>=0.27,<0.29" "pydantic>=2.6,<3" \
        "PyYAML>=6.0,<7" "fastapi>=0.110,<1" "uvicorn[standard]>=0.29,<1" \
        "structlog>=24.1,<26"

COPY src/ /app/src/
COPY config/ /app/config/

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs /run/secrets \
    && chown -R bot:bot /app/data /app/logs
USER bot

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "okxbot.executor.service"]
