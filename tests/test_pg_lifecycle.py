from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli.main import main


def run(home: Path, argv: list[str]) -> None:
    import os

    os.environ["AGENT_HOME"] = str(home)
    main(argv)


@pytest.mark.no_pg
def test_status_without_cluster_dies_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    with pytest.raises(SystemExit, match="run agent init"):
        run(tmp_path, ["status"])
    assert not (tmp_path / "pg").exists()


def test_only_init_creates_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_dsn: str
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN")
    calls: list[bool] = []

    def fake(data_dir, *, create=True):
        calls.append(create)
        return pg_dsn

    monkeypatch.setattr("agent_cli.main.ensure_cluster", fake)
    run(tmp_path, ["init"])
    run(tmp_path, ["status"])
    assert calls == [True, False]


def test_init_warns_on_non_default_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_dsn: str,
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN")

    def fake(data_dir, *, create=True):
        return pg_dsn

    monkeypatch.setattr("agent_cli.main.ensure_cluster", fake)
    run(tmp_path, ["init"])
    err = capsys.readouterr().err
    assert "is not the default" in err
    assert "agent pg stop" in err


def test_init_warning_with_external_dsn_mentions_no_cluster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    err = capsys.readouterr().err
    assert "is not the default" in err
    assert "bypassed by AGENT_PG_DSN" in err
    assert "agent pg stop" not in err


def test_pg_status_reports_external_dsn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["pg", "status"])
    out = capsys.readouterr().out
    assert "dsn=external" in out


@pytest.mark.no_pg
def test_pg_status_reports_missing_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    run(tmp_path, ["pg", "status"])
    out = capsys.readouterr().out
    assert "exists=no" in out
    assert "running=no" in out
    assert "port=-" in out
    assert not (tmp_path / "pg").exists()


def test_pg_stop_refuses_external_dsn(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="AGENT_PG_DSN"):
        run(tmp_path, ["pg", "stop"])


@pytest.mark.no_pg
def test_pg_stop_without_cluster_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    with pytest.raises(SystemExit, match="no local postgres cluster"):
        run(tmp_path, ["pg", "stop"])


@pytest.mark.no_pg
def test_pg_stop_stops_existing_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    run(tmp_path, ["pg", "stop"])
    assert stopped == [tmp_path / "pg"]
    assert "stopped" in capsys.readouterr().out


@pytest.mark.no_pg
def test_daemon_uninstall_stops_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))

    def boom(_argv: list[str]) -> None:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    run(tmp_path, ["daemon", "--uninstall"])
    assert stopped == [tmp_path / "pg"]


def test_daemon_uninstall_leaves_external_dsn_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    run(tmp_path, ["daemon", "--uninstall"])
    assert stopped == []


def test_legacy_dfx_ledger_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_legacy = tmp_path / "dfx-ledger"
    fake_legacy.mkdir()
    monkeypatch.setattr("agent_cli.main.LEGACY_DFX_LEDGER", fake_legacy)
    run(tmp_path, ["status"])
    err = capsys.readouterr().err
    assert "legacy dfx-ledger store" in err

    monkeypatch.setattr("agent_cli.main.LEGACY_DFX_LEDGER", tmp_path / "does-not-exist")
    run(tmp_path, ["status"])
    err2 = capsys.readouterr().err
    assert "legacy dfx-ledger store" not in err2


@pytest.mark.no_pg
def test_ensure_cluster_create_false_raises(tmp_path: Path) -> None:
    from agent_cli.pg import PgError, cluster_exists, cluster_running, ensure_cluster

    with pytest.raises(PgError, match="run agent init"):
        ensure_cluster(tmp_path / "pg", create=False)
    assert cluster_exists(tmp_path / "pg") is False
    assert cluster_running(tmp_path / "pg") is False
    assert not (tmp_path / "pg").exists()


@pytest.mark.no_pg
def test_empty_pg_dsn_is_an_error_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_PG_DSN", "")
    (tmp_path / "daemon.service").write_text("unit", encoding="utf-8")
    for argv in (["status"], ["pg", "status"], ["pg", "stop"], ["daemon", "--uninstall"]):
        with pytest.raises(SystemExit, match="set but empty"):
            run(tmp_path, argv)
    assert (tmp_path / "daemon.service").is_file()
    assert not (tmp_path / "pg").exists()


@pytest.mark.no_pg
def test_daemon_uninstall_checks_dsn_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_PG_DSN", "")
    (tmp_path / "daemon.service").write_text("unit", encoding="utf-8")
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    with pytest.raises(SystemExit, match="set but empty"):
        run(tmp_path, ["daemon", "--uninstall"])
    assert (tmp_path / "daemon.service").is_file()
    assert stopped == []


@pytest.mark.no_pg
def test_stopped_cluster_is_started_without_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    started: list[Path] = []
    monkeypatch.setattr(
        "agent_cli.pg.start_cluster",
        lambda data_dir: (started.append(data_dir), "host=127.0.0.1 port=1 user=agent dbname=postgres")[1],
    )
    monkeypatch.setattr("agent_cli.pg.wait_ready", lambda dsn, timeout=10.0: None)
    from agent_cli.pg import ensure_cluster

    assert (
        ensure_cluster(tmp_path / "pg", create=False)
        == "host=127.0.0.1 port=1 user=agent dbname=postgres"
    )
    assert started == [tmp_path / "pg"]


def test_status_starts_stopped_cluster_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pg_dsn: str
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN")
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    started: list[Path] = []
    monkeypatch.setattr(
        "agent_cli.pg.start_cluster",
        lambda data_dir: (started.append(data_dir), pg_dsn)[1],
    )
    monkeypatch.setattr("agent_cli.pg.wait_ready", lambda dsn, timeout=10.0: None)
    run(tmp_path, ["status"])
    out = capsys.readouterr().out
    assert "tasks_open=" in out
    assert started == [tmp_path / "pg"]


@pytest.mark.no_pg
def test_usage_lists_pg() -> None:
    with pytest.raises(SystemExit, match=r"daemon\|pg\|knock"):
        main(["--help"])
