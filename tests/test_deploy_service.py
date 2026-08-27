"""Tests for the systemd unit template and its installer.

`deploy/kbot.service` and `deploy/install-service.sh` are coupled by an
untyped contract: the unit carries `__PLACEHOLDER__` tokens and the script
substitutes them by name. Nothing else checks that. Add a placeholder to
the unit and forget the matching `sed` and you get a service whose
ExecStart is the literal string `__REPO_DIR__/.venv/bin/python` -- which
fails at boot, on the one machine nobody is watching.

The `[Unit]`-vs-`[Service]` section test guards a failure mode that is
worse than loud: systemd *ignores* `StartLimitIntervalSec` under
`[Service]` with only a journal warning, silently restoring the default
"5 restarts in 10 seconds, then stay dead forever" -- exactly the
behaviour this unit exists to prevent, and invisible until the day it
matters.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
UNIT_TEMPLATE = DEPLOY_DIR / "kbot.service"
INSTALLER = DEPLOY_DIR / "install-service.sh"

PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A minimal tree that satisfies the installer's preconditions, so the
    dry run exercises real substitution without needing a real venv."""
    repo = tmp_path / "crypto-arb-engine"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(UNIT_TEMPLATE, repo / "deploy" / "kbot.service")
    shutil.copy(INSTALLER, repo / "deploy" / "install-service.sh")
    (repo / "main.py").write_text("")
    python = repo / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return repo


def _dry_run(repo: Path, *args: str, env_mode: str | None = None) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}
    if env_mode is not None:
        env["ARB_MODE"] = env_mode
    result = subprocess.run(
        ["bash", str(repo / "deploy" / "install-service.sh"), "--dry-run", *args],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _rendered_unit(stdout: str) -> configparser.ConfigParser:
    body = stdout[stdout.index("[Unit]") :]
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string(body)
    return parser


def test_every_placeholder_in_the_unit_is_substituted(fake_repo: Path):
    assert PLACEHOLDER_RE.search(UNIT_TEMPLATE.read_text()), "template has no placeholders?"

    stdout = _dry_run(fake_repo)

    body = stdout[stdout.index("[Unit]") :]
    assert PLACEHOLDER_RE.search(body) is None, f"unsubstituted: {PLACEHOLDER_RE.findall(body)}"


def test_execstart_points_at_the_venv_interpreter(fake_repo: Path):
    """Not `python main.py`. Relying on PATH is what made several manual
    restarts silently start nothing at all."""
    unit = _rendered_unit(_dry_run(fake_repo))

    assert unit["Service"]["ExecStart"] == f"{fake_repo}/.venv/bin/python main.py"
    assert unit["Service"]["WorkingDirectory"] == str(fake_repo)


@pytest.mark.parametrize("mode", ["monitor", "paper", "live"])
def test_an_explicit_mode_argument_is_baked_into_the_unit(fake_repo: Path, mode: str):
    # Checked against the raw text, not the parsed unit: systemd allows
    # `Environment=` to repeat and merges the assignments, whereas
    # configparser keeps only the last one.
    body = _dry_run(fake_repo, mode)

    assert f"Environment=ARB_MODE={mode}" in body
    assert "Environment=PYTHONUNBUFFERED=1" in body


def test_mode_precedence_is_argument_then_environment_then_dotenv(fake_repo: Path):
    (fake_repo / ".env").write_text('# comment\nARB_MODE = "monitor"\nDASHBOARD_PORT=8420\n')

    assert "mode:   monitor" in _dry_run(fake_repo)
    assert "mode:   live" in _dry_run(fake_repo, env_mode="live")
    assert "mode:   paper" in _dry_run(fake_repo, "paper", env_mode="live")


def test_mode_defaults_to_paper_when_nothing_specifies_one(fake_repo: Path):
    """Never `live`. A default that arms real orders is the wrong default
    even if every caller happens to pass a mode today."""
    assert "mode:   paper" in _dry_run(fake_repo)


def test_an_unrecognised_mode_is_rejected_rather_than_installed(fake_repo: Path):
    result = subprocess.run(
        ["bash", str(fake_repo / "deploy" / "install-service.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=fake_repo,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fake_repo), "ARB_MODE": "papper"},
        check=False,
    )

    assert result.returncode != 0
    assert "papper" in result.stderr


def test_memory_limits_are_set_and_high_is_below_max(fake_repo: Path):
    unit = _rendered_unit(_dry_run(fake_repo))

    def mb(value: str) -> int:
        assert value.endswith("M"), value
        return int(value[:-1])

    high, hard = mb(unit["Service"]["MemoryHigh"]), mb(unit["Service"]["MemoryMax"])
    # MemoryHigh throttles via reclaim; MemoryMax is the OOM boundary. High at
    # or above Max makes the soft limit dead weight.
    assert 0 < high < hard


def test_start_rate_limiting_is_disabled_in_the_unit_section(fake_repo: Path):
    """systemd only warns -- in the journal -- when these are misplaced
    under [Service], and then silently keeps the default limit that gives
    up after a few fast restarts."""
    unit = _rendered_unit(_dry_run(fake_repo))

    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert "StartLimitIntervalSec" not in unit["Service"]


def test_the_service_restarts_and_starts_at_boot(fake_repo: Path):
    unit = _rendered_unit(_dry_run(fake_repo))

    # `on-failure` would not restart after a clean-but-unintended exit.
    assert unit["Service"]["Restart"] == "always"
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_installer_refuses_a_tree_with_no_venv(tmp_path: Path):
    """The precondition that matters most: a unit installed against a
    missing interpreter fails only at boot, in a log nobody reads."""
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(UNIT_TEMPLATE, repo / "deploy" / "kbot.service")
    shutil.copy(INSTALLER, repo / "deploy" / "install-service.sh")
    (repo / "main.py").write_text("")

    result = subprocess.run(
        ["bash", str(repo / "deploy" / "install-service.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
        check=False,
    )

    assert result.returncode != 0
    assert ".venv" in result.stderr
