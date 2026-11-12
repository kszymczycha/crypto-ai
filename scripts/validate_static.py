"""Static project validation without Docker: YAML, AST, compose and config consistency.

Usage:
    python3 scripts/validate_static.py
Returns exit code 1 if anything is inconsistent. Requires no dependencies beyond
the stdlib + PyYAML (if PyYAML is missing, YAML validation is skipped with a warning).
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERRORS: list[str] = []
WARNINGS: list[str] = []


def check_yaml() -> None:
    """Checks parseability of all YAML files and the key values."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        WARNINGS.append("PyYAML unavailable — YAML validation skipped")
        return
    files = sorted(ROOT.glob("config/*.yaml")) + sorted(ROOT.glob("compose*.yaml")) + [ROOT / "Makefile"]
    for path in files:
        if path.name == "Makefile" or path.suffix != ".yaml":
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except Exception as exc:
            ERRORS.append(f"YAML not parseable: {path.relative_to(ROOT)}: {exc}")
            continue
        if not isinstance(data, dict):
            ERRORS.append(f"YAML is not a mapping: {path.relative_to(ROOT)}")
            continue
        if path.name.startswith("compose"):
            services = data.get("services", {})
            missing = {"executor", "collector", "opencode"} - set(services)
            if missing:
                ERRORS.append(f"{path.name}: missing services {sorted(missing)}")
    limits = _load_yaml("config/risk_limits.yaml")
    if limits:
        if limits["min_order_usdt"] >= limits["max_order_usdt"]:
            ERRORS.append("risk_limits: min_order_usdt >= max_order_usdt")
        if limits["max_order_usdt"] > limits["require_confirmation_above_usdt"]:
            ERRORS.append("risk_limits: max_order_usdt > require_confirmation_above_usdt")
    symbols = _load_yaml("config/symbols.yaml")
    for pair in symbols.get("pairs", []):
        if limits and pair["max_order_usdt"] > limits["max_order_usdt"]:
            ERRORS.append(f"symbols: {pair['symbol']} limit > global max_order_usdt")


def _load_yaml(relative: str) -> dict:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    path = ROOT / relative
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def check_compose_topology() -> None:
    """Guards the secrets boundary and the network topology in the compose files.

    These rules are security, not style: if `opencode` or `collector` got a
    `secrets/` mount, the agent or the collector could place an order bypassing
    the risk engine.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return

    base = _load_yaml("compose.yaml")
    services = base.get("services", {})
    if not services:
        return

    # Secrets are mounted by the native compose mechanism (`secrets:`), not a bind mount:
    # a bind mount inherits host permissions (chmod 600 = uid 1000), while the process
    # in the container runs as uid 10001 and would not be able to read the file.
    for name in ("collector", "opencode"):
        service = services.get(name, {})
        volumes = service.get("volumes", []) or []
        leaked = [volume for volume in volumes if "secrets" in str(volume)]
        if leaked:
            ERRORS.append(f"compose.yaml: service '{name}' must not mount secrets: {leaked}")
        if service.get("secrets"):
            ERRORS.append(f"compose.yaml: service '{name}' must not have a 'secrets' section")

    executor_service = services.get("executor", {})
    executor_secrets = executor_service.get("secrets", []) or []
    targets = [
        str(entry.get("target")) if isinstance(entry, dict) else str(entry)
        for entry in executor_secrets
    ]
    if not any(target.endswith("okx.toml") for target in targets):
        ERRORS.append(f"compose.yaml: executor must have a secret whose target ends with 'okx.toml' (found: {targets})")
    if not base.get("secrets", {}).get("okx_credentials"):
        ERRORS.append("compose.yaml: missing secret definition 'okx_credentials'")
    else:
        default_file = str(base.get("secrets", {}).get("okx_credentials", {}).get("file", ""))
        if "live" in default_file:
            ERRORS.append("compose.yaml: the default secret must not point to live keys")

    if services.get("opencode", {}).get("ports"):
        ERRORS.append("compose.yaml: service 'opencode' must not publish ports")
    if services.get("executor", {}).get("ports"):
        ERRORS.append("compose.yaml: the executor port must not be published to the host")

    networks = base.get("networks", {})
    if not networks.get("botnet", {}).get("internal"):
        ERRORS.append("compose.yaml: network 'botnet' must have internal: true")

    for name in ("executor", "collector", "opencode"):
        service_networks = services.get(name, {}).get("networks", [])
        if "botnet" not in service_networks:
            ERRORS.append(f"compose.yaml: service '{name}' must be on the 'botnet' network")

    opencode_env = services.get("opencode", {}).get("environment", {}) or {}
    if "EXECUTOR_URL" not in opencode_env:
        ERRORS.append("compose.yaml: 'opencode' must have EXECUTOR_URL")

    # `opencode` needs Node + the OpenCode CLI. The base image does not have it, so
    # building this service from the Dockerfile ends with `opencode: not found`.
    opencode_dockerfile = str(services.get("opencode", {}).get("build", {}).get("dockerfile", ""))
    if opencode_dockerfile != "docker/opencode.Dockerfile":
        ERRORS.append(
            f"compose.yaml: 'opencode' must build from docker/opencode.Dockerfile (found: {opencode_dockerfile!r})"
        )
    if services.get("opencode", {}).get("image") == services.get("executor", {}).get("image"):
        ERRORS.append("compose.yaml: 'opencode' must not share an image with executor/collector")

    # Mode overlays: they point to WHICH secrets file reaches the executor. They must
    # not attach secrets to other services or mix modes (demo must not take live).
    for overlay, secret_file in (
        ("compose.demo.yaml", "okx_demo.toml"),
        ("compose.live.yaml", "okx_live.toml"),
    ):
        data = _load_yaml(overlay)
        if not data:
            continue
        overlay_services = data.get("services", {})
        for name, service in overlay_services.items():
            volumes = service.get("volumes", []) or []
            secret_mounts = [volume for volume in volumes if "secrets" in str(volume)]
            if secret_mounts:
                ERRORS.append(
                    f"{overlay}: mount secrets via the 'secrets' section, not via volumes ({name})"
                )
            if name != "executor" and service.get("secrets"):
                ERRORS.append(f"{overlay}: only the executor may have secrets (found: {name})")
        overlay_secret = (
            data.get("secrets", {}).get("okx_credentials", {}).get("file", "")
        )
        if secret_file not in str(overlay_secret):
            ERRORS.append(f"{overlay}: secret okx_credentials should point to {secret_file} (found: {overlay_secret!r})")


def check_python_syntax() -> None:
    """Compiles every .py file to AST (syntax + no empty files)."""
    for path in sorted(ROOT.glob("src/**/*.py")) + sorted(ROOT.glob("tests/**/*.py")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            ERRORS.append(f"empty file: {path.relative_to(ROOT)}")
            continue
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            ERRORS.append(f"syntax error: {path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")


def check_pyproject() -> None:
    """Checks pyproject.toml (TOML + package declarations)."""
    path = ROOT / "src" / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:
        ERRORS.append(f"pyproject.toml not parseable: {exc}")
        return
    deps = " ".join(data.get("project", {}).get("dependencies", []))
    for required in ("python-okx", "httpx", "pydantic", "PyYAML", "fastapi"):
        if required.lower() not in deps.lower():
            ERRORS.append(f"pyproject: missing dependency {required}")


def check_required_files() -> None:
    """Checks that every file required by the specification is present."""
    required = [
        "AGENTS.md", "PLAN.md", "opencode.jsonc", "README.md", ".env.example", ".gitignore", ".dockerignore",
        "compose.yaml", "compose.demo.yaml", "compose.live.yaml", "Dockerfile", "Makefile",
        "config/settings.yaml", "config/risk_limits.yaml", "config/symbols.yaml",
        "config/env/demo.env", "config/env/live.env.example",
        "secrets/.gitkeep", "secrets/okx_demo.toml.example",
        "src/pyproject.toml", "src/okxbot/__init__.py", "src/okxbot/config.py",
        "src/okxbot/client/okx_client.py", "src/okxbot/client/coingecko_client.py",
        "src/okxbot/client/rate_limiter.py", "src/okxbot/client/__init__.py",
        "src/okxbot/data/market.py", "src/okxbot/data/indicators.py",
        "src/okxbot/data/collector.py", "src/okxbot/data/__init__.py",
        "src/okxbot/strategy/signals.py", "src/okxbot/strategy/rules.py",
        "src/okxbot/executor/orders.py", "src/okxbot/executor/risk_engine.py",
        "src/okxbot/executor/position_manager.py", "src/okxbot/executor/http_server.py",
        "src/okxbot/storage/db.py", "src/okxbot/storage/models.py",
        "src/okxbot/logging/setup.py", "src/okxbot/logging/__init__.py",
        "src/cli/status.py", "src/cli/balance.py", "src/cli/market_snapshot.py",
        "src/cli/signal.py", "src/cli/place_order.py", "src/cli/cancel_order.py",
        "src/cli/positions.py",
        "scripts/entrypoint-collector.sh", "scripts/entrypoint-executor.sh",
        "scripts/bootstrap.sh",
        "docker/executor.Dockerfile", "docker/collector.Dockerfile", "docker/opencode.Dockerfile",
        ".opencode/skills/request-scope/SKILL.md",
        "tests/conftest.py", "tests/unit/test_risk_engine.py", "tests/unit/test_indicators.py",
        "tests/integration/test_okx_demo.py",
        "docs/architecture.md", "docs/risk_policy.md", "docs/runbook.md",
    ]
    for relative in required:
        if not (ROOT / relative).exists():
            ERRORS.append(f"missing required file: {relative}")


def check_no_secrets_tracked() -> None:
    """Checks that local secrets files are NOT tracked by git.

    The existence of `secrets/okx_demo.toml` is normal — bootstrap creates it. It is
    only an error when git would commit it. That is why we ask git
    (`check-ignore`/`ls-files`), not the file's mere existence.
    """
    import subprocess

    is_git = (ROOT / ".git").exists()
    for path in sorted((ROOT / "secrets").glob("*.toml")):
        if path.name.endswith(".example"):
            continue
        relative = path.relative_to(ROOT)
        if not is_git:
            WARNINGS.append(f"local secrets file ({relative}) — no git repo, cannot confirm it is ignored")
            continue
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(relative)], cwd=ROOT, capture_output=True, check=False
        )
        if ignored.returncode != 0:
            ERRORS.append(f"secrets: {relative} is NOT ignored by git!")
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(relative)], cwd=ROOT, capture_output=True, check=False
        )
        if tracked.returncode == 0:
            ERRORS.append(f"secrets: {relative} IS tracked by git!")

    for path in list(ROOT.glob("**/*.db")) + list(ROOT.glob("**/*.env")):
        relative = path.relative_to(ROOT)
        # config/env/*.env are intentional, non-secret mode switches — not reported.
        if ".example" in path.name or relative.parts[:2] == ("config", "env"):
            continue
        WARNINGS.append(f"runtime file in the repo: {relative}")


def check_agents_md_length() -> None:
    """AGENTS.md must have <= 160 lines."""
    lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    if len(lines) > 160:
        ERRORS.append(f"AGENTS.md has {len(lines)} lines (limit 160)")


def check_scripts_executable() -> None:
    """.sh scripts in scripts/ and docker/ must be executable."""
    import os

    for path in list((ROOT / "scripts").glob("*.sh")) + list((ROOT / "docker").glob("*.sh")):
        if not os.access(path, os.X_OK):
            ERRORS.append(f"script without execute permission: {path.relative_to(ROOT)}")


def main() -> int:
    """Runs all checks and prints the report."""
    check_required_files()
    check_yaml()
    check_compose_topology()
    check_python_syntax()
    check_pyproject()
    check_no_secrets_tracked()
    check_agents_md_length()
    check_scripts_executable()
    for warning in WARNINGS:
        print(f"WARNING: {warning}")
    if ERRORS:
        print(f"\nERRORS ({len(ERRORS)}):")
        for error in ERRORS:
            print(f"  - {error}")
        return 1
    print("Static validation: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
