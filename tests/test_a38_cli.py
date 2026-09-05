"""Exercise the installed CLI dispatcher without a database or GitHub writes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.no_pg
ROOT = Path(__file__).resolve().parents[1]


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-m", "agent_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("command", ["a38", "pr-guard"])
def test_subcommand_help_is_dispatched(command: str) -> None:
    result = invoke(command, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_a38_policy_dispatch_accepts_example() -> None:
    result = invoke("a38", "policy", "--file", str(ROOT / "examples/a38.json"))
    assert result.returncode == 0, result.stderr


def test_a38_policy_dispatch_preserves_failure(tmp_path: Path) -> None:
    policy = tmp_path / "invalid.json"
    policy.write_text('{"schema":"invalid"}', encoding="utf-8")
    result = invoke("a38", "policy", "--file", str(policy))
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_pr_guard_dispatch_rejects_invalid_arguments() -> None:
    result = invoke("pr-guard", "--not-an-option")
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
