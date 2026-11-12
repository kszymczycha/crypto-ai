# syntax=docker/dockerfile:1.7
# Test image: okxbot:local + pytest. Used by `make test-docker` when you do not
# want to install development dependencies locally (or have no pip).
#
# Requires a prior `make build` (the okxbot:local image).
# tests/ is in .dockerignore, so we mount it at runtime. We also mount the whole repo
# as /repo (read-only) and point to it via OKXBOT_REPO_ROOT — the security tests
# then read the real .gitignore/.dockerignore/secrets/, not copies from the image.
#
# Use `make test-docker` (correct mounts) instead of a manual docker run.

FROM okxbot:local

USER root
RUN python -m pip install --no-cache-dir \
        "pytest>=8.0,<9" "pytest-asyncio>=0.23,<1"

# The pytest configuration must be in the image: it contains asyncio_mode=auto and testpaths.
# Without it, async tests (collector, CoinGecko) would be skipped.
COPY pytest.ini /app/pytest.ini

# test_backtest_rotation.py imports /app/scripts/backtest.py directly.
COPY scripts/ /app/scripts/

USER bot
WORKDIR /app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
