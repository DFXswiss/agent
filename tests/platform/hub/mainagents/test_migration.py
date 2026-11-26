"""MIG-01 for migration 0037 (ADR-095 sections 1 and 4; KOR-52): it applies on the previous schema with data. Existing
tasks are numbered per organization by creation (ties by ID) and the counter continues after the highest number;
existing children get their places below the main agent (depth 1 among the task's depth-1 sessions, also those of a
later main session; deeper ones below their parent), main sessions none; an audit row written before keeps its hash
and the chain verifies; the triggers number what comes after."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from agent_hub.audit.chain import AuditRow, audit_hash, verify_chain
from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from tests.platform.hub.conftest import HubDatabase, uuid7

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
NAME = "main_agents"
AT = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
FIRST_ORG, SECOND_ORG, THIRD_ORG = uuid7(), uuid7(), uuid7()
WG, OWNER = uuid7(), uuid7()


def _task(writer: psycopg.Connection[Any], org: object, seconds: int, task: object | None = None) -> object:
    found = task or uuid7()
    at = AT + timedelta(seconds=seconds)
    writer.execute(
        "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
        " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
        (found, org, WG, OWNER, at, at, at),
    )
    return found


def _session(writer: psycopg.Connection[Any], task: object, parent: object | None, depth: int, seconds: int) -> object:
    found = uuid7()
    writer.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, parent_session_id, depth, state, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 'idle', %s)",
        (found, task, FIRST_ORG, WG, OWNER, parent, depth, AT + timedelta(seconds=seconds)),
    )
    return found


def test_the_migration_numbers_existing_tasks_and_children(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    own = next(path.name for path in MIGRATIONS.glob("*.sql") if NAME in path.name)
    files = [path for path in sorted(MIGRATIONS.glob("*.sql")) if path.name < own]
    for path in files:
        shutil.copy(path, previous / path.name)
    assert run_migrate(hub_db.migrate_env(), directory=previous) == (f"applied {len(files)}", 0)
    low, high = sorted([uuid7(), uuid7()], key=str)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        third = _task(writer, FIRST_ORG, 30)
        tied_high = _task(writer, FIRST_ORG, 10, high)
        tied_low = _task(writer, FIRST_ORG, 10, low)  # the same creation time: the lower ID first
        other = _task(writer, SECOND_ORG, 0)
        main = _session(writer, tied_low, None, 0, 0)
        first = _session(writer, tied_low, main, 1, 1)
        second = _session(writer, tied_low, main, 1, 2)
        first_first = _session(writer, tied_low, first, 2, 3)
        first_second = _session(writer, tied_low, first, 2, 4)
        deepest = _session(writer, tied_low, first_first, 3, 5)
        later = _session(writer, tied_low, None, 0, 6)  # a follow-up main session
        third_child = _session(writer, tied_low, later, 1, 7)
        elsewhere = _session(writer, third, _session(writer, third, None, 0, 0), 1, 8)
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        numbers = dict(writer.execute("SELECT id, number FROM hub.task").fetchall())
        assert [numbers[task] for task in (tied_low, tied_high, third, other)] == [1, 2, 3, 1]
        counters = dict(writer.execute("SELECT org_id, last FROM hub.task_counter").fetchall())
        assert counters == {FIRST_ORG: 3, SECOND_ORG: 1}
        paths = dict(writer.execute("SELECT id, agent_path FROM hub.session").fetchall())
        assert [paths[session] for session in (main, later)] == [None, None]
        assert [paths[session] for session in (first, second, third_child)] == ["1", "2", "3"]
        assert [paths[session] for session in (first_first, first_second, deepest)] == ["1.1", "1.2", "1.1.1"]
        assert paths[elsewhere] == "1"
        # the triggers continue: the next task of each org, a first task of a new org, the next children
        fourth = _task(writer, FIRST_ORG, 40)
        fresh = _task(writer, THIRD_ORG, 40)
        found = dict(writer.execute("SELECT id, number FROM hub.task WHERE id IN (%s, %s)", (fourth, fresh)).fetchall())
        assert (found[fourth], found[fresh]) == (4, 1)
        children = [_session(writer, tied_low, second, 2, 9), _session(writer, tied_low, later, 1, 10)]
        placed = dict(
            writer.execute("SELECT id, agent_path FROM hub.session WHERE id = ANY(%s)", (children,)).fetchall()
        )
        assert [placed[child] for child in children] == ["2.1", "4"]
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)


def test_rows_written_before_verify_after_the_migration(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    own = next(path.name for path in MIGRATIONS.glob("*.sql") if NAME in path.name)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < own:
            shutil.copy(path, previous / path.name)
    run_migrate(hub_db.migrate_env(), directory=previous)
    row = AuditRow(at=AT, actor_kind="device", action="audit.repeat", result="ok", actor_id=uuid7())
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute(
            "INSERT INTO hub.audit (seq, at, actor_id, actor_kind, action, result, detail, prev_hash, hash)"
            " VALUES (1, %s, %s, 'device', 'audit.repeat', 'ok', '{}', NULL, %s)",
            (AT, row.actor_id, audit_hash(1, row, None)),
        )
    run_migrate(hub_db.migrate_env())
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        assert writer.execute("SELECT on_behalf_of FROM hub.audit WHERE seq = 1").fetchone() == (None,)
        assert verify_chain(writer, 1, 1).ok
        # the widened actor kinds hold for new rows
        writer.execute(
            "INSERT INTO hub.audit (seq, at, actor_id, actor_kind, action, result, detail, prev_hash, hash,"
            " on_behalf_of) VALUES (2, %s, %s, 'subagent', 'audit.repeat', 'ok', '{}', %s, %s, %s)",
            (AT, uuid7(), "0" * 64, "1" * 64, uuid7()),
        )
