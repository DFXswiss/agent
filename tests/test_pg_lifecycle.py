from __future__ import annotations

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


def test_init_writes_pg_dsn_into_service_unit(
    tmp_path: Path, pg_dsn: str
) -> None:
    import os

    run(tmp_path, ["init"])
    text = (tmp_path / "daemon.service").read_text(encoding="utf-8")
    assert "AGENT_PG_DSN" in text
    assert os.environ["AGENT_PG_DSN"] in text


def test_init_without_pg_dsn_leaves_unit_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_dsn: str
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN")

    def fake(data_dir, *, create=True):
        return pg_dsn

    monkeypatch.setattr("agent_cli.main.ensure_cluster", fake)
    run(tmp_path, ["init"])
    assert "AGENT_PG_DSN" not in (tmp_path / "daemon.service").read_text(encoding="utf-8")


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
    assert "agent daemon --uninstall" in err
    assert "agent pg stop" not in err
    assert "device.json stays" in err


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


@pytest.mark.no_pg
def test_pg_status_reports_missing_pg_ctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.pg import PgError

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")

    def boom(data_dir: Path) -> bool:
        raise PgError("pg_ctl is not installed")

    monkeypatch.setattr("agent_cli.main.cluster_running", boom)
    with pytest.raises(SystemExit, match=r"^agent: pg_ctl is not installed$"):
        run(tmp_path, ["pg", "status"])


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
def test_pg_stop_calls_stop_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: True)
    run(tmp_path, ["pg", "stop"])
    assert stopped == [tmp_path / "pg"]
    assert "stopped" in capsys.readouterr().out


@pytest.mark.no_pg
def test_pg_stop_refuses_while_daemon_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    (tmp_path / "daemon.service").write_text("unit", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    with pytest.raises(SystemExit, match="daemon --uninstall"):
        run(tmp_path, ["pg", "stop"])
    assert stopped == []


@pytest.mark.no_pg
def test_pg_stop_allowed_when_daemon_belongs_to_other_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from agent_cli.daemon import service_unit_text

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/elsewhere"),
        platform=sys.platform,
        extra_env={},
    )
    (tmp_path / "daemon.service").write_text(text, encoding="utf-8")
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: True)
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    run(tmp_path, ["pg", "stop"])
    assert stopped == [tmp_path / "pg"]
    assert "stopped" in capsys.readouterr().out


@pytest.mark.no_pg
def test_pg_stop_reports_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: False)
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    run(tmp_path, ["pg", "stop"])
    out = capsys.readouterr().out
    assert "not running" in out
    assert stopped == []


@pytest.mark.no_pg
def test_pg_stop_surfaces_pg_ctl_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.pg import PgError

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: True)

    def boom(data_dir: Path) -> None:
        raise PgError("pg_ctl: boom")

    monkeypatch.setattr("agent_cli.main.stop_cluster", boom)
    with pytest.raises(SystemExit, match="boom"):
        run(tmp_path, ["pg", "stop"])


@pytest.mark.no_pg
def test_stop_cluster_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from agent_cli.pg import PgError, stop_cluster

    (tmp_path / "pg" / "data").mkdir(parents=True)
    monkeypatch.setattr("agent_cli.pg._bin", lambda name: "/bin/false")

    def fake_run(args, **kwargs):
        rc = 0 if "status" in args else 1
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="pg_ctl: could not stop")

    monkeypatch.setattr("agent_cli.pg.subprocess.run", fake_run)
    with pytest.raises(PgError, match="could not stop"):
        stop_cluster(tmp_path / "pg")


@pytest.mark.no_pg
def test_stop_cluster_is_noop_when_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from agent_cli.pg import stop_cluster

    (tmp_path / "pg" / "data").mkdir(parents=True)
    monkeypatch.setattr("agent_cli.pg._bin", lambda name: "/bin/false")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        rc = 3 if "status" in args else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    monkeypatch.setattr("agent_cli.pg.subprocess.run", fake_run)
    stop_cluster(tmp_path / "pg")
    assert len(calls) == 1
    assert "status" in calls[0]
    assert not any("stop" in call for call in calls)


@pytest.mark.no_pg
def test_daemon_uninstall_calls_stop_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: True)

    def boom(_argv: list[str]) -> None:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    run(tmp_path, ["daemon", "--uninstall"])
    assert stopped == [tmp_path / "pg"]


@pytest.mark.no_pg
def test_daemon_uninstall_reports_missing_pg_ctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.pg import PgError

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    (tmp_path / "daemon.service").write_text("unit", encoding="utf-8")
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))

    def boom(_argv: list[str]) -> None:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)

    def boom_running(data_dir: Path) -> bool:
        raise PgError("pg_ctl is not installed")

    monkeypatch.setattr("agent_cli.main.cluster_running", boom_running)
    with pytest.raises(SystemExit, match=r"^agent: pg_ctl is not installed$"):
        run(tmp_path, ["daemon", "--uninstall"])
    assert stopped == []


@pytest.mark.no_pg
def test_daemon_uninstall_surfaces_pg_ctl_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.pg import PgError

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    (tmp_path / "daemon.service").write_text("unit", encoding="utf-8")

    def boom(_argv: list[str]) -> None:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    monkeypatch.setattr("agent_cli.main.cluster_running", lambda data_dir: True)

    def boom_stop(data_dir: Path) -> None:
        raise PgError("pg_ctl: boom")

    monkeypatch.setattr("agent_cli.main.stop_cluster", boom_stop)
    with pytest.raises(SystemExit, match=r"^agent: pg_ctl: boom$"):
        run(tmp_path, ["daemon", "--uninstall"])
    assert not (tmp_path / "daemon.service").exists()


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
def test_running_raises_on_unexpected_pg_ctl_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from agent_cli.pg import PgError, cluster_running

    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    monkeypatch.setattr("agent_cli.pg._bin", lambda name: "/bin/false")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 4, stdout="", stderr="pg_ctl: directory is not a database cluster directory"
        )

    monkeypatch.setattr("agent_cli.pg.subprocess.run", fake_run)
    with pytest.raises(PgError, match="not a database cluster directory"):
        cluster_running(tmp_path / "pg")


@pytest.mark.no_pg
def test_pg_status_surfaces_pg_ctl_error_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")
    monkeypatch.setattr("agent_cli.pg._bin", lambda name: "/bin/false")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 4, stdout="", stderr="pg_ctl: directory is not a database cluster directory"
        )

    monkeypatch.setattr("agent_cli.pg.subprocess.run", fake_run)
    with pytest.raises(
        SystemExit, match=r"^agent: pg_ctl: directory is not a database cluster directory$"
    ):
        run(tmp_path, ["pg", "status"])


@pytest.mark.no_pg
def test_service_home_roundtrip(tmp_path: Path) -> None:
    from agent_cli.daemon import service_home, service_unit_text

    for platform in ("darwin", "linux"):
        text = service_unit_text(
            program=["/usr/bin/agent", "daemon"],
            home=Path("/srv/agent home"),
            platform=platform,
            extra_env={"PATH": "/usr/bin"},
        )
        unit_path = tmp_path / f"unit-{platform}"
        unit_path.write_text(text, encoding="utf-8")
        assert service_home(unit_path, platform) == Path("/srv/agent home")
    assert service_home(tmp_path / "missing", "darwin") is None


@pytest.mark.no_pg
def test_daemon_uninstall_refuses_foreign_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from agent_cli.daemon import service_unit_text

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/elsewhere"),
        platform=sys.platform,
        extra_env={},
    )
    (tmp_path / "daemon.service").write_text(text, encoding="utf-8")
    pg_version = tmp_path / "pg" / "data" / "PG_VERSION"
    pg_version.parent.mkdir(parents=True)
    pg_version.write_text("17", encoding="utf-8")

    def boom(_argv: list[str]) -> None:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    stopped: list[Path] = []
    monkeypatch.setattr("agent_cli.main.stop_cluster", lambda data_dir: stopped.append(data_dir))
    with pytest.raises(SystemExit, match="installed for AGENT_HOME=/elsewhere"):
        run(tmp_path, ["daemon", "--uninstall"])
    assert (tmp_path / "daemon.service").is_file()
    assert stopped == []


def test_real_cluster_stop_roundtrip(tmp_path: Path) -> None:
    from agent_cli.pg import cluster_running, ensure_cluster, stop_cluster

    data = tmp_path / "pg"
    ensure_cluster(data)
    try:
        assert cluster_running(data) is True
        stop_cluster(data)
        assert cluster_running(data) is False
        stop_cluster(data)
    finally:
        stop_cluster(data)


@pytest.mark.no_pg
def test_usage_lists_pg() -> None:
    with pytest.raises(SystemExit, match=r"daemon\|pg\|knock"):
        main(["--help"])
