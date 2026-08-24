from __future__ import annotations

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
