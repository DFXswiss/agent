"""The CLI of the database area: `register(subparsers)` (ADR-027 section 4) and `python -m agent_hub.db migrate`."""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys

import pytest

from agent_hub.db import cli
from agent_hub.db.migrate import load_migrations
from tests.platform.hub.conftest import HubDatabase


def test_register_adds_migrate_with_a_handler() -> None:
    parser = argparse.ArgumentParser()
    cli.register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["migrate"])
    assert args.handler is cli.migrate_command


def test_main_prints_one_line_on_stdout(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in hub_db.migrate_env().items():
        monkeypatch.setenv(name, value)
    assert cli.main(["migrate"]) == 0
    assert cli.main(["migrate"]) == 0
    captured = capsys.readouterr()
    count = len(load_migrations())
    assert captured.out == f"applied {count}\nunchanged {count}\n"
    assert captured.err == ""


def test_main_prints_a_failure_on_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("AGENT_HUB_OWNER_DATABASE", raising=False)
    assert cli.main(["migrate"]) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "failed config\n")


def test_main_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2
    capsys.readouterr()


def test_module_entry_point(hub_db: HubDatabase) -> None:
    environ = {**os.environ, **hub_db.migrate_env()}
    result = subprocess.run(
        [sys.executable, "-m", "agent_hub.db", "migrate"], env=environ, capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, f"applied {len(load_migrations())}\n", "")


def test_module_main_is_the_cli_main() -> None:
    module = importlib.import_module("agent_hub.db.__main__")
    assert module.main is cli.main
