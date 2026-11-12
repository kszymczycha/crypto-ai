# syntax=docker/dockerfile:1.7
# Shared base image for all roles (executor, collector, opencode, cli).
# The roles differ only in the start command and the mounted directories.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

# Minimal tooling: curl for healthchecks, tini for proper SIGTERM handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer (cached as long as pyproject.toml does not change).
COPY src/pyproject.toml /app/src/pyproject.toml
RUN python -m pip install --no-cache-dir \
        "python-okx>=0.3.5,<0.4" "httpx>=0.27,<0.29" "pydantic>=2.6,<3" \
        "PyYAML>=6.0,<7" "fastapi>=0.110,<1" "uvicorn[standard]>=0.29,<1" \
        "structlog>=24.1,<26"

# Application code (no secrets, no data, no tests — see .dockerignore).
COPY src/ /app/src/
COPY config/ /app/config/

# Non-root user. The container starts as root ONLY so that the entrypoint
# can copy the secret from the bind-mount (host permissions, e.g. 600 uid 1000) to
# a location readable by "bot" and immediately drop privileges via setpriv.
# The application process never runs as root — see docker/entrypoint-base.sh.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs /run/secrets \
    && chown -R bot:bot /app/data /app/logs

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/docker/entrypoint-executor.sh"]
