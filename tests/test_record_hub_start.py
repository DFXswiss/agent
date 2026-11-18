"""The start of the hub's record part (RECORD-PROTOCOL.md section 11.4) and the entry of the hub.

Every case starts the real application through `create_app` with `start_record` against the real
PostgreSQL of AGENT_CORE_TEST_POSTGRES and checks the lines of the record part (section 13): each
start failure logs `record start failed <step>` as its only line of the record part, except a failed
verification, which logs `record verify failed <position> <failure>` first; the hub does not start.
"""

from __future__ import annotations

import logging.config
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

import agent_core.__main__ as hub_main
from agent_core.config import Config
from agent_core.record import connection as connection_module
from agent_core.record.runtime import RecordStartFailed
from tests.conftest import RecordDatabase
from tests.record_helpers import closed_port, migrated, silent_listener, tamper, unreachable_url
from tests.record_hub_helpers import ALICE, Points, new_id, start_hub, task_create
from tests.test_record_storage import WIDER_RIGHTS, as_superuser
from tests.test_signed_requests import log_lines  # noqa: F401 - a fixture, used by name

RECORD_PREFIX = "uvicorn.error "


def record_lines(lines: list[str]) -> list[str]:
    """The lines of the record part (section 13), without logger name, as `LEVEL text`."""
    out = []
    for line in lines:
        if not line.startswith(RECORD_PREFIX):
            continue
        level, _, text = line[len(RECORD_PREFIX):].partition(" ")
        if text.startswith("record "):
            out.append(f"{level} {text}")
    return out


def failed_start(db: RecordDatabase, cfg: Config, tmp_path: Path, **options: Any) -> str:
    """Start a hub that must not start; the step of its failure."""
    with pytest.raises(RecordStartFailed) as failed:
        start_hub(db, cfg, tmp_path, **options)
    assert failed.value.code == 1
    return failed.value.step


@pytest.fixture
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every attempt of the hub to connect, recorded and refused."""
    attempts: list[dict[str, Any]] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(kwargs)
        raise psycopg.OperationalError("no connection in this test")

    monkeypatch.setattr(psycopg, "connect", refuse)
    return attempts


# --- success ----------------------------------------------------------------------------------------


def test_a_start_on_an_empty_record_logs_record_verified_0_dash(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    migrated(record_db)
    hub = start_hub(record_db, cfg, tmp_path)
    try:
        assert record_lines(log_lines) == ["INFO record verified 0 -"]
        assert hub.runtime.memory.head() is None
        assert not hub.runtime.unverified
    finally:
        hub.close()


def test_a_start_builds_the_state_from_the_stored_record(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    migrated(record_db)
    first = start_hub(record_db, cfg, tmp_path, name="first")
    try:
        work = new_id()
        first.write(ALICE, "task", work, task_create())
        appended = first.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    finally:
        first.close()
    log_lines.clear()
    second = start_hub(record_db, cfg, tmp_path, name="second")
    try:
        assert record_lines(log_lines) == [f"INFO record verified 2 {appended['record_hash']}"]
        head = second.runtime.memory.head()
        assert head is not None and (head.position, head.record_hash) == (2, appended["record_hash"])
        assert second.runtime.memory.state.task(work) is not None
    finally:
        second.close()


# --- config -----------------------------------------------------------------------------------------


MALFORMED = {
    "missing": None,
    "empty": "",
    "no sslmode": "postgresql://{role}:{password}@127.0.0.1:5432/{database}",
    "another query parameter": "postgresql://{role}:{password}@127.0.0.1:5432/{database}?sslmode=disable&x=1",
    "disable with a host name": "postgresql://{role}:{password}@postgres:5432/{database}?sslmode=disable",
    "a short password": "postgresql://{role}:abc@127.0.0.1:5432/{database}?sslmode=disable",
    "role public": "postgresql://public:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role pg_monitor": "postgresql://pg_monitor:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role pg_": "postgresql://pg_:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role current_user": "postgresql://current_user:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role current_role": "postgresql://current_role:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role session_user": "postgresql://session_user:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role user": "postgresql://user:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role none": "postgresql://none:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "role PUBLIC in uppercase": "postgresql://PUBLIC:{password}@127.0.0.1:5432/{database}?sslmode=disable",
    "database public": "postgresql://{role}:{password}@127.0.0.1:5432/public?sslmode=disable",
    "database Pg_x": "postgresql://{role}:{password}@127.0.0.1:5432/Pg_x?sslmode=disable",
    "database pg_x": "postgresql://{role}:{password}@127.0.0.1:5432/pg_x?sslmode=disable",
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_missing_empty_or_malformed_writer_string_fails_step_config_without_connecting(
    record_db: RecordDatabase,
    cfg: Config,
    tmp_path: Path,
    log_lines: list[str],  # noqa: F811
    connect_attempts: list[dict[str, Any]],
    case: str,
) -> None:
    template = MALFORMED[case]
    environ = {} if template is None else {
        "AGENT_CORE_RECORD_DATABASE": template.format(
            role=record_db.writer_role, password=record_db.writer_password, database=record_db.name
        )
    }
    assert failed_start(record_db, cfg, tmp_path, environ=environ) == "config"
    assert record_lines(log_lines) == ["ERROR record start failed config"]
    assert connect_attempts == []
    for line in log_lines:
        for secret in record_db.secret_values():
            assert secret not in line


@pytest.mark.parametrize("value", ["", "postgresql://x"], ids=["empty", "set"])
def test_the_owner_variable_in_the_hub_environment_fails_step_config(
    record_db: RecordDatabase,
    cfg: Config,
    tmp_path: Path,
    log_lines: list[str],  # noqa: F811
    connect_attempts: list[dict[str, Any]],
    value: str,
) -> None:
    environ = {"AGENT_CORE_RECORD_DATABASE": record_db.writer_url, "AGENT_CORE_RECORD_OWNER_DATABASE": value}
    assert failed_start(record_db, cfg, tmp_path, environ=environ) == "config"
    assert record_lines(log_lines) == ["ERROR record start failed config"]
    assert connect_attempts == []


def test_a_libpq_below_16_fails_step_config(
    record_db: RecordDatabase,
    cfg: Config,
    tmp_path: Path,
    log_lines: list[str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(connection_module, "libpq_version", lambda: 150007)
    assert failed_start(record_db, cfg, tmp_path) == "config"
    assert record_lines(log_lines) == ["ERROR record start failed config"]
    assert connect_attempts == []


def test_names_that_only_contain_a_reserved_word_pass_step_config(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, connect_attempts: list[dict[str, Any]]
) -> None:
    # `public_x`, `x_pg_` and `users` pass the name rules; the start goes on to step `schema`, whose
    # connection this test refuses.
    for role, database in (("public_x", "x_pg_"), ("users", "public_x")):
        url = record_db.url(role, record_db.writer_password, database=database)
        assert failed_start(record_db, cfg, tmp_path, environ={"AGENT_CORE_RECORD_DATABASE": url}) == "schema"
    assert [attempt["user"] for attempt in connect_attempts] == ["public_x", "users"]


def test_the_hub_passes_connect_timeout_10_and_the_writer_role_to_the_driver(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, connect_attempts: list[dict[str, Any]]
) -> None:
    assert failed_start(record_db, cfg, tmp_path) == "schema"
    assert len(connect_attempts) == 1
    assert connect_attempts[0]["connect_timeout"] == 10
    assert connect_attempts[0]["user"] == record_db.writer_role


# --- schema -----------------------------------------------------------------------------------------


def test_an_unreachable_database_fails_step_schema(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    environ = {"AGENT_CORE_RECORD_DATABASE": unreachable_url(record_db, closed_port())}
    assert failed_start(record_db, cfg, tmp_path, environ=environ) == "schema"
    assert record_lines(log_lines) == ["ERROR record start failed schema"]


def test_a_listener_that_never_answers_fails_step_schema_by_the_connect_timeout(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    with silent_listener() as port:
        started = time.monotonic()
        environ = {"AGENT_CORE_RECORD_DATABASE": unreachable_url(record_db, port)}
        assert failed_start(record_db, cfg, tmp_path, environ=environ) == "schema"
        assert time.monotonic() - started < 15
    assert record_lines(log_lines) == ["ERROR record start failed schema"]


SCHEMA_CASES = {
    "a database on which migrate never ran": None,
    "an unknown version": "INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())",
    "no version row": "DELETE FROM record.schema_version",
}


@pytest.mark.parametrize("case", sorted(SCHEMA_CASES))
def test_a_schema_other_than_version_1_fails_step_schema(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], case: str  # noqa: F811
) -> None:
    statement = SCHEMA_CASES[case]
    if statement is not None:
        migrated(record_db)
        with record_db.admin() as admin:
            admin.execute(statement)
    assert failed_start(record_db, cfg, tmp_path) == "schema"
    assert record_lines(log_lines) == ["ERROR record start failed schema"]


# --- writer -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(WIDER_RIGHTS))
def test_a_writer_with_a_right_beyond_section_11_2_fails_step_writer(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], case: str  # noqa: F811
) -> None:
    migrated(record_db)
    as_superuser(record_db, WIDER_RIGHTS[case])
    assert failed_start(record_db, cfg, tmp_path) == "writer"
    assert record_lines(log_lines) == ["ERROR record start failed writer"]


def test_a_second_user_database_on_the_server_fails_step_writer(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    migrated(record_db)
    other = "t_second_" + new_id().replace("-", "")[:12]
    with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(other)))
    try:
        assert failed_start(record_db, cfg, tmp_path) == "writer"
    finally:
        with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(other)))
    assert record_lines(log_lines) == ["ERROR record start failed writer"]


# --- verify -----------------------------------------------------------------------------------------


def test_a_stored_record_that_fails_verification_logs_exactly_two_lines_and_does_not_start(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    migrated(record_db)
    hub = start_hub(record_db, cfg, tmp_path, name="first")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
    finally:
        hub.close()
    tamper(record_db, "record.event", "UPDATE record.event SET record_hash = %s WHERE position = 1", ("d" * 64,))
    log_lines.clear()
    assert failed_start(record_db, cfg, tmp_path, name="second") == "verify"
    assert record_lines(log_lines) == [
        "ERROR record verify failed 1 record_hash_mismatch",
        "ERROR record start failed verify",
    ]


def end_writer_sessions(db: RecordDatabase) -> None:
    """The server ends every session of the writer role, as a lost connection does."""
    with db.admin() as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (db.writer_role,))


def test_a_read_of_step_verify_that_fails_logs_only_verify_read(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    migrated(record_db)
    hub = start_hub(record_db, cfg, tmp_path, name="first")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
    finally:
        hub.close()
    log_lines.clear()
    # The test point of the snapshot ends the start's connection between the read of the events and
    # the read of the checkpoints of a sound record.
    points = Points()
    points.snapshot_between_reads = lambda: end_writer_sessions(record_db)
    assert failed_start(record_db, cfg, tmp_path, name="second", points=points) == "verify_read"
    assert record_lines(log_lines) == ["ERROR record start failed verify_read"]


# --- the entry of the hub -----------------------------------------------------------------------------


def test_main_starts_the_record_before_uvicorn_runs(
    record_db: RecordDatabase,
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: list[str],  # noqa: F811
) -> None:
    migrated(record_db)
    environment = {
        "AGENT_CORE_PUBLIC_URL": "http://127.0.0.1:8787",
        "AGENT_CORE_SESSION_SECRET": "x" * 32,
        "AGENT_CORE_GITHUB_CLIENT_ID": "id",
        "AGENT_CORE_GITHUB_CLIENT_SECRET": "secret",
        "AGENT_CORE_GITHUB_AUTHORIZE_URL": "https://github.com/login/oauth/authorize",
        "AGENT_CORE_GITHUB_TOKEN_URL": "https://github.com/login/oauth/access_token",
        "AGENT_CORE_GITHUB_USER_URL": "https://api.github.com/user",
        "AGENT_CORE_DATABASE": str(tmp_path / "main.sqlite"),
        "AGENT_CORE_TEAMS": str(cfg.teams_path),
        "AGENT_CORE_KEYS": str(cfg.keys_path),
        "AGENT_CORE_SIGNING_KEY": "ab" * 32,
        "AGENT_CORE_HUB_KEY_ID": "hub-test-1",
        "AGENT_CORE_HOST": "127.0.0.1",
        "AGENT_CORE_PORT": "8787",
        "AGENT_CORE_RECORD_DATABASE": record_db.writer_url,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_CORE_RECORD_OWNER_DATABASE", raising=False)
    configured: list[dict[str, Any]] = []
    ran: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(logging.config, "dictConfig", configured.append)
    monkeypatch.setattr(hub_main.uvicorn, "run", lambda app, **options: ran.append((app, options)))
    hub_main.main()
    assert len(configured) == 1
    ((app, options),) = ran
    assert options["host"] == "127.0.0.1" and options["port"] == 8787
    runtime = app.state.record
    try:
        assert runtime.pool.taken == 0
        assert record_lines(log_lines) == ["INFO record verified 0 -"]
    finally:
        runtime.stop()
        app.state.hub.store.close()


def test_main_does_not_run_the_hub_when_the_record_start_fails(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_lines: list[str]  # noqa: F811
) -> None:
    for name, value in {
        "AGENT_CORE_PUBLIC_URL": "http://127.0.0.1:8787",
        "AGENT_CORE_SESSION_SECRET": "x" * 32,
        "AGENT_CORE_GITHUB_CLIENT_ID": "id",
        "AGENT_CORE_GITHUB_CLIENT_SECRET": "secret",
        "AGENT_CORE_GITHUB_AUTHORIZE_URL": "https://github.com/login/oauth/authorize",
        "AGENT_CORE_GITHUB_TOKEN_URL": "https://github.com/login/oauth/access_token",
        "AGENT_CORE_GITHUB_USER_URL": "https://api.github.com/user",
        "AGENT_CORE_DATABASE": str(tmp_path / "main.sqlite"),
        "AGENT_CORE_TEAMS": str(cfg.teams_path),
        "AGENT_CORE_KEYS": str(cfg.keys_path),
        "AGENT_CORE_SIGNING_KEY": "ab" * 32,
        "AGENT_CORE_HUB_KEY_ID": "hub-test-1",
        "AGENT_CORE_HOST": "127.0.0.1",
        "AGENT_CORE_PORT": "8787",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_CORE_RECORD_DATABASE", raising=False)
    monkeypatch.setattr(logging.config, "dictConfig", lambda config: None)
    ran: list[Any] = []
    monkeypatch.setattr(hub_main.uvicorn, "run", lambda app, **options: ran.append(app))
    with pytest.raises(SystemExit) as exited:
        hub_main.main()
    assert exited.value.code == 1
    assert ran == []
    assert record_lines(log_lines) == ["ERROR record start failed config"]
