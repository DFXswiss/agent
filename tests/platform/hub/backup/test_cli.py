"""The commands `agent-hub export`, `restore` and `rebuild-projections` (exit codes and output, ADR-027 section 4)."""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_hub import cli as main_cli
from agent_hub.backup import cli
from agent_hub.db.migrate import OWNER_VARIABLE
from tests.platform.hub.backup.support import Estate, lines
from tests.platform.hub.conftest import HubDatabase, hub_environ

CLOSED_PORT = 9


def use(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for name in list(os.environ):
        if name.startswith("AGENT_HUB_") and not name.startswith("AGENT_HUB_TEST_"):
            monkeypatch.delenv(name)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def fixed_clock(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "CLOCK", [estate.hub.clock])


@pytest.fixture
def source_env(migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, hub_environ(migrated_db))


def unreachable(database: HubDatabase) -> str:
    return database.url(database.writer_role, database.writer_password).replace(
        f":{database.port}/", f":{CLOSED_PORT}/"
    )


def test_commands_are_registered() -> None:
    parser = main_cli.build_parser()
    assert parser.parse_args(["export", "--org", "x"]).handler is cli.export_command
    assert parser.parse_args(["restore", "--in", "a", "--in", "b"]).inputs == ["a", "b"]
    assert parser.parse_args(["rebuild-projections", "--check"]).check is True


@pytest.mark.usefixtures("source_env")
def test_export_writes_a_private_file_and_restore_reads_it(
    estate: Estate,
    target: HubDatabase,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "org.jsonl"
    assert main_cli.main(["export", "--org", estate.org, "--out", str(out)]) == 0
    count = len(lines(out.read_bytes())) - 2
    assert capsys.readouterr().out == f"exported {count}\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_bytes() == estate.export()
    assert list(tmp_path.iterdir()) == [out]
    other = tmp_path / "other.jsonl"
    assert main_cli.main(["export", "--org", estate.other_org, "--out", str(other)]) == 0
    capsys.readouterr()
    use(monkeypatch, {**hub_environ(target), OWNER_VARIABLE: target.owner_url})
    assert main_cli.main(["restore", "--in", str(out), "--in", str(other)]) == 0
    assert capsys.readouterr().out.startswith("restored ")
    assert main_cli.main(["restore", "--in", str(out)]) == 1
    assert capsys.readouterr().err == "failed not_empty\n"


def test_export_to_stdout(estate: Estate, source_env: None, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert main_cli.main(["export", "--org", estate.org]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == estate.export()
    assert captured.err.startswith(b"exported ")


def test_export_refusals(
    estate: Estate,
    source_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    owner: psycopg.Connection[Any],
) -> None:
    existing = tmp_path / "exists.jsonl"
    existing.write_bytes(b"keep")
    assert main_cli.main(["export", "--org", estate.org, "--out", str(existing)]) == 1
    assert existing.read_bytes() == b"keep"
    assert main_cli.main(["export", "--org", "not-an-id", "--out", str(tmp_path / "a")]) == 1
    assert main_cli.main(["export", "--org", "01a20491-a8c7-7645-8beb-c2984e623d82", "--out", str(tmp_path / "b")]) == 1
    assert main_cli.main(["export", "--org", estate.org, "--out", str(tmp_path / "missing" / "c")]) == 1
    owner.execute("ALTER TABLE hub.task ADD COLUMN extra money DEFAULT 1")
    assert main_cli.main(["export", "--org", estate.org, "--out", str(tmp_path / "d")]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "failed out_exists",
        "failed org",
        "failed org",
        "failed write",
        "failed value_unwritable",
    ]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["exists.jsonl"]


def test_configuration_errors_exit_with_2(
    estate: Estate, migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, hub_environ(migrated_db, AGENT_HUB_SIGNING_KEY=""))
    for argv in (["export", "--org", estate.org], ["restore", "--in", "x"], ["rebuild-projections"]):
        assert main_cli.main(argv) == cli.EXIT_CONFIG
    assert "AGENT_HUB_SIGNING_KEY" in capsys.readouterr().err
    use(monkeypatch, hub_environ(migrated_db))
    assert main_cli.main(["restore", "--in", "x"]) == cli.EXIT_CONFIG
    assert capsys.readouterr().err == "failed config\n"


def test_an_unreachable_database_exits_with_3(
    estate: Estate, migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "CONNECT_TIMEOUT_SECONDS", 1.0)
    use(monkeypatch, hub_environ(migrated_db, AGENT_HUB_DATABASE=unreachable(migrated_db)))
    assert main_cli.main(["export", "--org", estate.org]) == cli.EXIT_UNAVAILABLE
    assert main_cli.main(["rebuild-projections"]) == cli.EXIT_UNAVAILABLE
    file = tmp_path / "f"
    file.write_bytes(b"x")
    owner = migrated_db.url(migrated_db.owner_role, migrated_db.owner_password)
    monkeypatch.setenv(OWNER_VARIABLE, owner.replace(f":{migrated_db.port}/", f":{CLOSED_PORT}/"))
    assert main_cli.main(["restore", "--in", str(file)]) == cli.EXIT_UNAVAILABLE
    assert main_cli.main(["restore", "--in", str(tmp_path / "missing")]) == 1


def test_rebuild_projections_prints_one_line(
    estate: Estate,
    source_env: None,
    capsys: pytest.CaptureFixture[str],
    migrated_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert main_cli.main(["rebuild-projections", "--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["check"] is True
    assert report["differences"] == []
    assert set(report["rebuilt"]) >= {"orgs", "platform", "whiteboards"}
    with estate.hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.platform_role")
    assert main_cli.main(["rebuild-projections", "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["differences"] == ["platform_role"]
    assert main_cli.main(["rebuild-projections"]) == 0
    assert json.loads(capsys.readouterr().out) | {"rebuilt": {}} == {
        "ok": True,
        "check": False,
        "rebuilt": {},
        "differences": ["platform_role"],
    }
    monkeypatch.setattr("agent_hub.backup.rebuild.LOCK_TIMEOUT_MS", 200)
    with migrated_db.connect(migrated_db.writer_role) as holder:
        holder.execute("LOCK TABLE hub.task IN ROW EXCLUSIVE MODE")
        assert main_cli.main(["rebuild-projections"]) == 1
    assert capsys.readouterr().err == "failed rebuild\n"


def test_the_clock_of_the_export_is_utc() -> None:
    assert cli._now().utcoffset() == timedelta(0)


@pytest.mark.usefixtures("source_env")
def test_a_target_created_during_the_export_is_kept(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "org.jsonl"
    real_link = os.link

    def racing_link(source: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"other")
        real_link(source, destination)

    monkeypatch.setattr(os, "link", racing_link)
    assert main_cli.main(["export", "--org", estate.org, "--out", str(target)]) == 1
    assert capsys.readouterr().err == "failed out_exists\n"
    assert target.read_bytes() == b"other"
    assert [path.name for path in tmp_path.iterdir()] == ["org.jsonl"]


@pytest.mark.usefixtures("source_env")
def test_any_rebuild_error_is_refused_without_its_message(
    estate: Estate, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret value")

    monkeypatch.setattr("agent_hub.backup.rebuild.rebuild", broken)
    assert main_cli.main(["rebuild-projections"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "failed rebuild\n"
    assert "secret" not in captured.out + captured.err
