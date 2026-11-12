# Developer shortcuts. Requires a working Docker Compose.
SHELL := /bin/bash
# -o pipefail: the pipeline status is the status of the earliest failed command, not
# the last one. Without it, `docker build ... | tail -3` reports success when the build
# failed — `tail` returns 0. The same class of bug as `docker compose config`.
# -e: the first error aborts the recipe instead of plowing on with a nonexistent image.
.SHELLFLAGS := -eu -o pipefail -c
PYTHON := python3
PYTHONPATH := src

# Operating mode: demo (sandbox) or live. Controls the secrets file and the OKX flag.
# Defaults to demo — the safer default (see compose.yaml: demo secret).
MODE ?= demo
COMPOSE := docker compose -f compose.yaml -f compose.$(MODE).yaml

.PHONY: help bootstrap build up down logs status health test test-docker lint shell demo live check-config validate clean

help:            ## List of targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

bootstrap:       ## Creates .env, directories and the secrets template (no real keys)
	./scripts/bootstrap.sh

build:           ## Builds the images (okxbot:local + okxbot-opencode:local)
	$(COMPOSE) build

up:              ## Starts executor + collector in $(MODE) mode (demo by default)
	$(COMPOSE) up -d executor collector

down:            ## Stops and removes the containers (data is kept)
	$(COMPOSE) down

logs:            ## Logs from all services (follow)
	$(COMPOSE) logs -f --tail=100

status:          ## Container status
	$(COMPOSE) ps

health:          ## Executor healthcheck from the host (via docker exec)
	$(COMPOSE) exec executor curl -fsS http://127.0.0.1:8080/health

test:            ## Unit tests locally (requires pytest; integration only when OKX_FLAG=1)
	$(PYTHON) -m pytest tests/unit -q

test-docker: build ## Unit tests in the container (no local pip/pytest)
	# The dependency on `build` is MANDATORY: docker/test.Dockerfile does FROM
	# okxbot:local, and src/ is baked into the image (only tests/
	# and /repo are mounted). Without it, the tests silently check pre-change code.
	docker build -f docker/test.Dockerfile -t okxbot-test .
	docker run --rm \
		-v "$$PWD/tests:/app/tests:ro" \
		-v "$$PWD/:/repo:ro" \
		-e OKXBOT_REPO_ROOT=/repo \
		okxbot-test

lint:            ## Ruff + syntax compilation
	$(PYTHON) -m ruff check src tests scripts || true
	$(PYTHON) -m compileall -q src tests

shell:           ## Shell in the opencode container
	$(COMPOSE) run --rm --entrypoint sh opencode

demo:            ## Starts demo: executor + collector + opencode (OKX sandbox)
	$(COMPOSE) -f compose.yaml -f compose.demo.yaml up -d executor collector opencode

live:            ## Starts live (requires the checklist from docs/runbook.md)
	@echo "WARNING: live. Make sure you have completed the checklist in docs/runbook.md"
	$(COMPOSE) -f compose.yaml -f compose.live.yaml up -d executor collector

check-config:    ## Validates YAML files and compose syntax (no Docker: YAML only)
	python -c "import yaml,glob;[yaml.safe_load(open(f,encoding='utf-8')) for f in glob.glob('config/*.yaml')];print('YAML OK')"
	# --no-interpolate: does NOT substitute environment variables, so no
	# secrets reach stdout — even if someone removed the redirect. `>/dev/null` alone
	# is not enough: it protects only until someone deletes it "because it is unnecessary",
	# and `docker compose config` without the flag prints keys verbatim.
	$(COMPOSE) config --no-interpolate >/dev/null && echo "compose OK" || echo "compose: skipped (no Docker)"

validate:        ## Full static validation (files, YAML, AST, secret boundaries, composition)
	python3 scripts/validate_static.py

clean:           ## Removes the Python cache and test artifacts
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache 2>/dev/null || true
