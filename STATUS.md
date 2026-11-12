# STATUS.md — session handoff

**Last updated:** 2026-11-12
**Repository:** `main` at `25da3ec` (single initial commit), working tree clean.

This file is the first thing a new session should read. It records where the work
stopped, what is blocked, and what to do next. Update it at the end of each session.

## Current stage

The infrastructure is migrated to **OpenCode** and the working tree is translated
to **English**. Docker images build and unit tests pass. The demo stack is **not
running**: the executor cannot start until the demo OKX keys are supplied.

## Done

- Migrated from Claude Code to OpenCode:
  - `opencode` service in `compose.yaml`,
  - `docker/opencode.Dockerfile` (Node 22 + `@opencode/cli@2`),
  - `docker/entrypoint-opencode.sh`,
  - `opencode.jsonc` (model/provider/permissions),
  - `AGENTS.md` and `.opencode/skills/request-scope/SKILL.md`,
  - removed `CLAUDE.md`, `.claude/`, `docker/claude.Dockerfile`,
    `docker/entrypoint-claude.sh`.
- Translated the whole working tree to English (docs, comments, docstrings,
  messages, tests, config comments). Strategy hint values are now:
  `consider_buy`, `watch`, `no_data`.
- Built Docker images successfully:
  - `okxbot:local`,
  - `okxbot-opencode:local` (`opencode v2.2.7`),
  - `okxbot-test`.
- Unit tests pass in `okxbot-test` (exit 0); 4 integration tests are skipped
  (they require demo OKX and opt-in).
- Fixed `docker/test.Dockerfile` to include `scripts/backtest.py`, which the
  backtest rotation tests import.
- `python3 scripts/validate_static.py` passes; `python3 -m compileall` passes.

## Blockers

1. **Demo OKX keys are missing.** `secrets/okx_demo.toml` still contains
   `CHANGE_ME`, so the executor exits with:
   `FATAL: secrets contain CHANGE_ME — fill in the OKX API keys`.
2. **OpenCode model variables are missing from `.env`:**
   - `OPENCODE_BASE_URL`
   - `OPENCODE_API_KEY`
   - `OPENCODE_MODEL_ID`
   Without them the `opencode` agent has no model.
3. `make` is not installed in this WSL environment. Use the underlying
   `docker compose` commands directly (examples below).

## Next steps

1. Fill `secrets/okx_demo.toml` with demo keys and `flag = "1"`; run `chmod 600`.
2. Fill `OPENCODE_*` variables in `.env`.
3. Start executor and collector:
   ```bash
   docker compose -f compose.yaml -f compose.demo.yaml up -d executor collector
   docker compose -f compose.yaml -f compose.demo.yaml exec -T executor \
     curl -fsS http://127.0.0.1:8080/health
   ```
   Expected: `{"ok":true,"mode":"demo","flag":"1"}`.
4. **P0.1:** place a demo market order with TP/SL and verify in the OKX panel
   whether algo TP/SL orders are attached. If not, switch to `attachAlgoOrds`.
5. Then follow `PLAN.md` in order: P0.2 reconciliation, P0.3 idempotency,
   P0.4 authorization/selling/mandatory TP/SL, P0.5 cleanup.
6. Only after P0: P1 quality/CI, then P2 strategy.

## Live blockers (do not go live)

From `PLAN.md`:

- TP/SL for market orders is not verified on demo.
- `max_open_positions` is not enforced for market orders.
- `max_daily_loss_pct` does not see TP/SL fills from OKX.
- No reconciliation of `orders` with OKX.
- Idempotency can block a legitimate retry.
- `POST /signal` has no authentication.
- Selling has no exact position size.
- The current strategy has negative expectancy (B1).

## Useful commands

```bash
# Static checks
python3 scripts/validate_static.py
python3 -m compileall -q src tests scripts

# Unit tests in the test image
docker build -f docker/test.Dockerfile -t okxbot-test .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/:/repo:ro" \
  -e OKXBOT_REPO_ROOT=/repo \
  okxbot-test

# OpenCode image / CLI
docker run --rm --entrypoint opencode okxbot-opencode:local --version
docker run --rm --entrypoint python okxbot-opencode:local -m pytest --version
```

## Notes

- `opencode.jsonc` is intentionally restrictive: `src/` is RW, but `config/`,
  `docs/`, `tests/`, `scripts/`, `AGENTS.md`, `PLAN.md`, `opencode.jsonc` and
  `.opencode/` are read-only for the agent. Changes there must be proposed as a
  diff and applied on the host.
- `ruff` is not clean: 65 findings, mostly `E501` line length plus test-related
  `S603`/`S607`. `make lint` still masks errors with `|| true`.
- The old Git history (before the single initial commit) is backed up outside the
  repo at `/tmp/opencode/crypto-ai-history-20261112-203914.bundle`. `/tmp` may
  not survive a reboot; keep a copy elsewhere if it matters.
