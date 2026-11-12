"""Config file tests: YAML validity and threshold consistency."""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

import yaml

from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config"


def load(name: str) -> dict:
    with (CONFIG / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _rules(text: str) -> list[str]:
    """Returns the non-empty, non-comment rules of an ignore file."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def ignored_by_rules(rules: list[str], path: str) -> bool:
    """Emulate gitignore semantics: the last matching rule wins.

    Anchoring is the part that matters here, and it is easy to get backwards:

    - A pattern WITHOUT a slash matches the basename at ANY depth, so `data/`
      also matches `src/okxbot/data/...`. This is why `.gitignore` had to be
      fixed to `/data/` — the unanchored form silently excluded the whole
      analytics module from the repository.
    - A pattern WITH a slash is anchored to the repo root.
    - A trailing slash marks a directory rule, which also covers its contents.

    Note that `.dockerignore` does NOT need the same fix: Docker anchors every
    pattern itself, so `data/` there already means the root-level directory
    only. The two files look alike but do not share this rule.
    """
    result = False
    for rule in rules:
        negate = rule.startswith("!")
        raw = (rule[1:] if negate else rule).strip()
        if not raw:
            continue
        # A leading slash is an explicit "anchor to root" marker; otherwise a
        # slash anywhere in the pattern anchors it to the root too.
        if raw.startswith("/"):
            anchored, pattern = True, raw[1:]
        else:
            anchored, pattern = "/" in raw, raw
        pattern = pattern.rstrip("/")
        if anchored:
            # Root-anchored: the path itself, or anything inside that directory.
            matched = fnmatch.fnmatch(path, pattern) or path.startswith(f"{pattern}/")
        else:
            # Unanchored: the pattern applies to any segment at any depth, and a
            # matching directory also covers everything beneath it.
            segments = path.split("/")
            matched = any(fnmatch.fnmatch(segment, pattern) for segment in segments)
            matched = matched or any(
                fnmatch.fnmatch(segment, pattern) for segment in segments[:-1]
            )
        if matched:
            result = not negate
    return result


def test_risk_limits_yaml_valid() -> None:
    limits = load("risk_limits.yaml")
    required = {
        "max_position_pct", "max_daily_loss_pct", "max_portfolio_drawdown_pct",
        "max_open_positions", "max_order_usdt", "min_order_usdt",
        "require_confirmation_above_usdt", "allowed_sides", "allowed_order_types",
        "cooldown_seconds_between_signals", "max_signals_per_day",
    }
    assert required <= set(limits)


def test_risk_limits_consistent() -> None:
    limits = load("risk_limits.yaml")
    assert limits["min_order_usdt"] < limits["max_order_usdt"]
    assert limits["max_order_usdt"] <= limits["require_confirmation_above_usdt"]


def test_symbols_yaml_valid() -> None:
    symbols = load("symbols.yaml")
    assert symbols["pairs"], "no pairs in symbols.yaml"
    for pair in symbols["pairs"]:
        assert pair["symbol"].endswith("-USDT")
        assert pair["coingecko"]


def test_symbols_limits_not_above_global() -> None:
    global_max = load("risk_limits.yaml")["max_order_usdt"]
    for pair in load("symbols.yaml")["pairs"]:
        assert pair["max_order_usdt"] <= global_max


def test_settings_yaml_valid() -> None:
    settings = load("settings.yaml")
    assert settings["executor"]["port"] == 8080
    assert settings["collector"]["interval_seconds"] > 0


def test_env_examples_do_not_contain_real_keys() -> None:
    """Env files may *mention* secrets but must not carry values.

    We check for a real leak (a value assigned to a key/secret), not just the
    occurrence of the word — a comment "there are no secrets in .env" is desirable.
    """
    import re

    assignment = re.compile(
        r"^\s*(api_key|secret_key|secret|passphrase|api_secret)\s*=\s*[\"']?[^\"'\s#]+",
        re.IGNORECASE | re.MULTILINE,
    )
    for path in (CONFIG / "env").glob("*.env*"):
        text = path.read_text(encoding="utf-8")
        assert not assignment.search(text), f"{path.name} contains a secret assignment"


def test_readme_and_runbook_have_no_secret_assignments() -> None:
    """Documentation must not contain working key-value pairs."""
    import re

    assignment = re.compile(r"\b(api_key|secret_key|passphrase)\s*=\s*[\"'][A-Za-z0-9]{8,}[\"']")
    for name in (
        "README.md",
        "AGENTS.md",
        "docs/runbook.md",
        "docs/architecture.md",
        "docs/risk_policy.md",
    ):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert not assignment.search(text), f"{name} contains something that looks like a real secret"


def test_secret_template_has_placeholders() -> None:
    template = (REPO_ROOT / "secrets" / "okx_demo.toml.example").read_text(encoding="utf-8")
    assert "CHANGE_ME" in template


def test_dockerignore_excludes_all_secret_files() -> None:
    """Tests the MEANING of the .dockerignore rules, not just the pattern occurrence.

    .dockerignore works like gitignore (the last matching rule wins), so we ask
    about specific paths. The whole `secrets/` directory must be excluded —
    including the `.example` templates: secrets in the image are useless, and
    every file in that directory is a potential leak into the image layer.
    """
    rules = _rules((REPO_ROOT / ".dockerignore").read_text(encoding="utf-8"))
    for path in (
        "secrets/okx_demo.toml",
        "secrets/okx_live.toml",
        "secrets/any.toml",
        "secrets/okx_demo.toml.example",
    ):
        assert ignored_by_rules(rules, path), f"{path} would end up in the Docker image!"


def test_dockerignore_excludes_runtime_data() -> None:
    """Runtime and repo data must not bloat the image or leak into it."""
    rules = _rules((REPO_ROOT / ".dockerignore").read_text(encoding="utf-8"))
    for path in ("data/state.db", "logs/executor.log", ".git/config", "tests/conftest.py"):
        assert ignored_by_rules(rules, path), f"{path} is not excluded from the image"


def test_gitignore_ignores_real_secrets_but_not_templates() -> None:
    """Secrets and runtime data ignored; templates and .gitkeep tracked."""
    rules = _rules((REPO_ROOT / ".gitignore").read_text(encoding="utf-8"))
    for path in (
        "secrets/okx_demo.toml",
        "secrets/okx_live.toml",
        "data/state.db",
        "logs/executor.log",
        ".env",
    ):
        assert ignored_by_rules(rules, path), f"git would track {path}!"
    for path in ("secrets/.gitkeep", "secrets/okx_demo.toml.example", ".env.example", "config/env/demo.env"):
        assert not ignored_by_rules(rules, path), f"{path} should not be ignored"


def test_secrets_present_locally_are_ignored_by_git() -> None:
    """Secret files created by bootstrap must not be tracked.

    Two-track verification: the real `git check-ignore` (if the repo and the git
    binary are available) and the rule matcher — so the test also runs before
    `git init` and in the test image, which has no git.
    """
    rules = _rules((REPO_ROOT / ".gitignore").read_text(encoding="utf-8"))
    can_run_git = (REPO_ROOT / ".git").exists() and shutil.which("git") is not None
    for path in sorted((REPO_ROOT / "secrets").glob("*.toml")):
        if path.name.endswith(".example"):
            continue
        assert ignored_by_rules(rules, f"secrets/{path.name}"), f"secrets/{path.name} is not ignored"
        if can_run_git:
            import subprocess

            result = subprocess.run(
                ["git", "check-ignore", "-q", f"secrets/{path.name}"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, f"git does NOT ignore secrets/{path.name}"
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", f"secrets/{path.name}"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=False,
            )
            assert tracked.returncode != 0, f"secrets/{path.name} IS tracked by git!"


def test_paths_in_config_are_relative_or_secrets_mount() -> None:
    """Paths in the config must be portable (relative), outside the secrets mount."""
    settings = load("settings.yaml")
    for key, value in settings["paths"].items():
        if not isinstance(value, str):
            continue
        if value.startswith("/"):
            assert key == "secrets_file" and value.startswith("/run/secrets"), (
                f"{key}={value}: absolute path outside /run/secrets"
            )
            continue
        assert not Path(value).is_absolute(), value


def test_opencode_config_protects_secrets_and_strategy() -> None:
    """The OpenCode config must explicitly block secrets and allow the strategy."""
    text = (REPO_ROOT / "opencode.jsonc").read_text(encoding="utf-8")
    assert '"$schema": "https://opencode.ai/config.json"' in text
    assert '"resource": "secrets/*"' in text
    assert '"resource": ".env"' in text
    assert '"resource": "src/okxbot/strategy/**"' in text
    assert '"effect": "deny"' in text
    assert '"effect": "allow"' in text


def test_agents_and_opencode_skill_exist() -> None:
    """OpenCode reads AGENTS.md and skills from .opencode/skills."""
    assert (REPO_ROOT / "AGENTS.md").is_file()
    assert (REPO_ROOT / ".opencode" / "skills" / "request-scope" / "SKILL.md").is_file()
