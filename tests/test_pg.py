from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli.pg import HARDCODED_PG_BIN_DIRS, PgError, _bin, extra_pg_bin_dirs

pytestmark = pytest.mark.no_pg


def _exe(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_extra_pg_bin_dirs_includes_postgres_app_latest() -> None:
    dirs = extra_pg_bin_dirs(versions_root=Path("/no/such/postgres-app-versions"))
    assert dirs[:4] == list(HARDCODED_PG_BIN_DIRS)
    assert dirs[4] == Path("/no/such/postgres-app-versions/latest/bin")
    assert len(dirs) == 5


def test_extra_pg_bin_dirs_numbered_newest_first(tmp_path: Path) -> None:
    (tmp_path / "latest" / "bin").mkdir(parents=True)
    (tmp_path / "9.6" / "bin").mkdir(parents=True)
    (tmp_path / "10" / "bin").mkdir(parents=True)
    (tmp_path / "16" / "bin").mkdir(parents=True)
    (tmp_path / "preview" / "bin").mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("skip", encoding="utf-8")
    dirs = extra_pg_bin_dirs(versions_root=tmp_path)
    assert dirs[4] == tmp_path / "latest" / "bin"
    assert dirs[5:] == [
        tmp_path / "16" / "bin",
        tmp_path / "10" / "bin",
        tmp_path / "9.6" / "bin",
    ]


def test_bin_prefers_path_over_extra_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path_dir = tmp_path / "path"
    extra_dir = tmp_path / "extra"
    path_dir.mkdir()
    extra_dir.mkdir()
    _exe(path_dir / "pg_ctl")
    _exe(extra_dir / "pg_ctl")
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr("agent_cli.pg.extra_pg_bin_dirs", lambda **_: [extra_dir])
    assert _bin("pg_ctl") == str(path_dir / "pg_ctl")


def test_bin_falls_back_to_extra_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exe(tmp_path / "pg_ctl")
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("agent_cli.pg.extra_pg_bin_dirs", lambda **_: [tmp_path])
    assert _bin("pg_ctl") == str(tmp_path / "pg_ctl")


def test_bin_uses_agent_pg_bin_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exe(tmp_path / "pg_ctl")
    monkeypatch.setenv("AGENT_PG_BIN", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("agent_cli.pg.extra_pg_bin_dirs", lambda **_: [])
    assert _bin("pg_ctl") == str(tmp_path / "pg_ctl")


def test_bin_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        "agent_cli.pg.extra_pg_bin_dirs",
        lambda **_: [Path("/no/such/pg/bin")],
    )
    with pytest.raises(PgError, match="pg_ctl is not installed"):
        _bin("pg_ctl")


def test_pg_process_env_overrides_broken_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli.pg import _pg_process_env

    monkeypatch.setenv("LC_ALL", "not_a_real_locale")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    monkeypatch.setenv("AGENT_KEEP", "yes")
    env = _pg_process_env()
    assert env["LC_ALL"] == "C"
    assert env["LC_CTYPE"] == "UTF-8"  # LC_ALL wins; the broken value stays inert
    assert env["AGENT_KEEP"] == "yes"


def test_running_passes_forced_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_cli.pg as pg

    seen: list[dict | None] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 3, "", "")

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    monkeypatch.setenv("LC_ALL", "not_a_real_locale")
    assert pg._running(tmp_path) is False
    assert seen and seen[0] is not None and seen[0]["LC_ALL"] == "C"


def test_start_cluster_existing_passes_forced_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_cli.pg as pg

    pgdata = tmp_path / "data"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("17\n", encoding="utf-8")
    (tmp_path / "port").write_text("5433\n", encoding="utf-8")
    start_envs: list[dict | None] = []

    def fake_run(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 3, "", "")
        if "start" in argv:
            start_envs.append(kwargs.get("env"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected argv {argv}")

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    dsn = pg.start_cluster(tmp_path)
    assert dsn == "host=127.0.0.1 port=5433 user=agent dbname=postgres"
    assert start_envs and start_envs[0] is not None and start_envs[0]["LC_ALL"] == "C"

