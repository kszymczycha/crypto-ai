# Builder for the OpenCode container (Python + OpenCode CLI, WITHOUT OKX secrets).
#   docker build -f docker/opencode.Dockerfile -t okxbot-opencode .
# Node 22 is used to install @opencode/cli; Python runs `python -m cli.*`.

FROM python:3.12-slim AS bot-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl tini ca-certificates gnupg util-linux git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (runtime for the OpenCode CLI installer).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# V2: the @opencode/cli package installs the native `opencode` binary.
RUN npm install -g @opencode/cli@2

WORKDIR /app

COPY src/pyproject.toml /app/src/pyproject.toml
RUN python -m pip install --no-cache-dir \
        "python-okx>=0.3.5,<0.4" "httpx>=0.27,<0.29" "pydantic>=2.6,<3" "PyYAML>=6.0,<7"
# Development tools: the agent can run tests and lint without leaving the container.
RUN python -m pip install --no-cache-dir \
        "pytest>=8.0,<9" "pytest-asyncio>=0.23,<1" "ruff>=0.4,<1"

COPY pytest.ini /app/pytest.ini
COPY ruff.toml /app/ruff.toml

COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/
COPY AGENTS.md /app/AGENTS.md
COPY PLAN.md /app/PLAN.md
COPY opencode.jsonc /app/opencode.jsonc
COPY .opencode /app/.opencode

# Non-root user; home directory on a volume (OpenCode sessions and data).
# Starts as root only so that the shared entrypoint can drop privileges (setpriv).
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app/data /app/logs /home/bot

# Note: this image has no OKX secrets and should not have any — the opencode container
# talks only to the executor (EXECUTOR_URL). REQUIRE_SECRETS=0 is set by compose.

WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/docker/entrypoint-opencode.sh"]
