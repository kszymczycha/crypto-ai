#!/usr/bin/env bash
# Bootstrap: creates .env, runtime directories and validates the YAML configuration.
# It does not create files with secrets — only a template to fill in.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "== okx-bot bootstrap ($ROOT)"

# 1. Runtime directories
mkdir -p data/cache logs secrets
echo "-- directories: data/, logs/ OK"

# 2. .env from the template
if [ ! -f .env ]; then
  cp .env.example .env
  echo "-- created .env (no secrets)"
else
  echo "-- .env already exists (skipping)"
fi

# 3. Demo secrets template
if [ ! -f secrets/okx_demo.toml ]; then
  cp secrets/okx_demo.toml.example secrets/okx_demo.toml
  chmod 600 secrets/okx_demo.toml
  echo "!! FILL IN secrets/okx_demo.toml (api_key, secret_key, passphrase)"
else
  echo "-- secrets/okx_demo.toml already exists"
fi

# 4. YAML validation (if Python with PyYAML is available)
if python3 -c "import yaml" 2>/dev/null; then
  python3 - <<'PY'
import glob, sys, yaml
for path in sorted(glob.glob("config/*.yaml")):
    with open(path, encoding="utf-8") as handle:
        yaml.safe_load(handle)
    print(f"-- YAML OK: {path}")
PY
else
  echo "-- PyYAML unavailable: skipping YAML validation"
fi

# 5. Safety check: secrets outside git
if [ -d .git ]; then
  if git check-ignore -q secrets/okx_demo.toml 2>/dev/null; then
    echo "-- secrets/ ignored by git OK"
  else
    echo "!! WARNING: secrets/ is NOT ignored by git — check .gitignore" >&2
  fi
fi

cat <<'EOF'
== Done. Next:
   1. fill in secrets/okx_demo.toml
   2. make build
   3. OKX_FLAG=1 docker compose -f compose.yaml -f compose.demo.yaml up -d
   4. docker compose exec opencode python -m cli.status
EOF
