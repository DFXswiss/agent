"""`agent-hub audit verify`: the operator's check of the global audit chain and the newest checkpoint (03-api.md
section 4.11 verification, 07-testing.md section 2.1 "hub audit chain verifies" at the end of every E2E run)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from agent_hub import cli as hub_cli
from agent_hub.audit import cli
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.checkpoint import write_checkpoint
from agent_hub.config import load_config
from tests.platform.hub.conftest import HubDatabase, hub_environ

ROW = AuditRow(at=datetime(2026, 11, 22, 7, 0, tzinfo=UTC), actor_kind="hub", action="read.me", result="ok")


def _set_env(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for name in [name for name in os.environ if name.startswith("AGENT_HUB_") and "TEST" not in name]:
        monkeypatch.delenv(name)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def _verify(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str]:
    code = hub_cli.main(["audit", "verify"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out else {}, captured.err


def test_register_adds_audit_verify_with_a_handler() -> None:
    parser = argparse.ArgumentParser()
    cli.register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["audit", "verify"])
    assert args.handler is cli.verify_command


def test_audit_without_a_subcommand_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        hub_cli.main(["audit"])
    assert caught.value.code == 2
    capsys.readouterr()


def test_an_empty_audit_verifies(
    migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch, hub_environ(migrated_db))
    assert _verify(capsys) == (0, {"ok": True, "head": {"seq": 0, "hash": "0" * 64}, "checkpoint": None}, "")


def test_rows_and_a_checkpoint_verify(
    migrated_db: HubDatabase,
    writer: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = hub_environ(migrated_db)
    stored = [append_audit(writer, ROW) for _ in range(3)]
    checkpoint = write_checkpoint(writer, load_config(environ).signer(), datetime(2026, 11, 22, 8, 0, tzinfo=UTC))
    assert checkpoint is not None
    _set_env(monkeypatch, environ)
    code, result, err = _verify(capsys)
    assert (code, err, result["ok"]) == (0, "", True)
    assert result["head"] == {"seq": 3, "hash": stored[-1].hash}
    assert result["checkpoint"]["seq"] == 3


def test_a_tampered_row_fails(
    migrated_db: HubDatabase,
    writer: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(2):
        append_audit(writer, ROW)
    with migrated_db.admin() as admin:
        admin.execute("ALTER TABLE hub.audit DISABLE TRIGGER audit_no_change")
        admin.execute("UPDATE hub.audit SET result = 'denied' WHERE seq = 1")
    _set_env(monkeypatch, hub_environ(migrated_db))
    code, result, err = _verify(capsys)
    assert (code, result["ok"], result["head"]["seq"]) == (cli.EXIT_BROKEN, False, 2)
    assert err == "agent-hub: the audit chain does not verify\n"


def test_a_configuration_error_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch, hub_environ(AGENT_HUB_SIGNING_KEY="secret-value"))
    code, result, err = _verify(capsys)
    assert (code, result) == (hub_cli.EXIT_CONFIG, {})
    assert "AGENT_HUB_SIGNING_KEY" in err
    assert "secret-value" not in err


def test_the_main_cli_discovers_the_command(migrated_db: HubDatabase) -> None:
    environ = {
        **{name: value for name, value in os.environ.items() if not name.startswith("AGENT_HUB_")},
        **hub_environ(migrated_db),
    }
    done = subprocess.run(
        [sys.executable, "-m", "agent_hub", "audit", "verify"], env=environ, capture_output=True, text=True, check=False
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert json.loads(done.stdout)["ok"] is True


def test_an_unreachable_database_is_not_reported_as_a_broken_chain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unreachable = "postgresql://writer:" + "0" * 64 + "@127.0.0.1:9/hub?sslmode=disable"
    _set_env(monkeypatch, hub_environ(AGENT_HUB_DATABASE=unreachable))
    monkeypatch.setattr(cli, "CONNECT_TIMEOUT_SECONDS", 0.5)
    code, result, err = _verify(capsys)
    assert (code, result) == (cli.EXIT_UNAVAILABLE, {})
    assert err == "agent-hub: the hub database is not reachable\n"
    assert "0" * 64 not in err
