"""`agent-hub ui-health [--days N]`: the operator's view of the UI health counters (ADR-086 section 5,
08-monitoring.md section 6.1)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from agent_hub import cli as hub_cli
from agent_hub.telemetry import cli, store
from tests.platform.hub.conftest import HubDatabase, hub_environ

NOW = datetime(2026, 11, 24, 7, 0, tzinfo=UTC)


def _set_env(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for name in [name for name in os.environ if name.startswith("AGENT_HUB_") and "TEST" not in name]:
        monkeypatch.delenv(name)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def _record(writer: psycopg.Connection[Any], day: date, view: str) -> None:
    with writer.transaction():
        store.record(writer, day, "0.1.0", {view: store.Counts(2, 900, 1)})


def test_register_adds_ui_health_with_a_handler() -> None:
    parser = argparse.ArgumentParser()
    cli.register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["ui-health", "--days", "3"])
    assert (args.handler, args.days) == (cli.ui_health_command, 3)
    assert parser.parse_args(["ui-health"]).days == 7


@pytest.mark.parametrize("days", ["0", "91", "x", "-1", "٣"])
def test_bad_days_are_usage_errors(days: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        hub_cli.main(["ui-health", "--days", days])
    assert caught.value.code == 2
    assert "must be a whole number from 1 to 90" in capsys.readouterr().err


def test_the_counters_are_printed(
    migrated_db: HubDatabase,
    writer: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _record(writer, date(2026, 11, 24), "task.chat")
    _record(writer, date(2026, 11, 20), "home")
    _set_env(monkeypatch, hub_environ(migrated_db))
    monkeypatch.setattr(cli, "CLOCK", [lambda: NOW])
    assert hub_cli.main(["ui-health", "--days", "2"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "from": "2026-11-23",
        "rows": [
            {
                "day": "2026-11-24",
                "version": "0.1.0",
                "view": "task.chat",
                "reports": 1,
                "count": 2,
                "blocked_ms": 900,
                "max_bucket": 1,
            }
        ],
    }
    assert hub_cli.main(["ui-health"]) == 0
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 2


def test_a_configuration_error_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch, hub_environ(AGENT_HUB_SIGNING_KEY="secret-value"))
    assert hub_cli.main(["ui-health"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "AGENT_HUB_SIGNING_KEY" in err
    assert "secret-value" not in err


def test_an_unreachable_database(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    unreachable = "postgresql://writer:" + "0" * 64 + "@127.0.0.1:9/hub?sslmode=disable"
    _set_env(monkeypatch, hub_environ(AGENT_HUB_DATABASE=unreachable))
    monkeypatch.setattr(cli, "CONNECT_TIMEOUT_SECONDS", 0.5)
    assert hub_cli.main(["ui-health"]) == cli.EXIT_UNAVAILABLE
    assert capsys.readouterr().err == "agent-hub: the hub database is not reachable\n"


def test_the_main_cli_discovers_the_command(migrated_db: HubDatabase) -> None:
    environ = {
        **{name: value for name, value in os.environ.items() if not name.startswith("AGENT_HUB_")},
        **hub_environ(migrated_db),
    }
    done = subprocess.run(
        [sys.executable, "-m", "agent_hub", "ui-health", "--days", "1"],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert json.loads(done.stdout)["rows"] == []
