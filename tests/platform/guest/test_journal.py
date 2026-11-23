"""U-GST-journal: SQLite journal, append, long poll, ack and pruning with a fake clock (02 section 5, 03 section 7)."""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from agent_guest.clock import SystemClock, iso_ms, uuid7
from agent_guest.journal import (
    KINDS,
    MAX_LIMIT,
    RETENTION_SECONDS,
    SCHEMA_VERSION,
    Journal,
    JournalError,
    canonical,
)
from agent_guest.schema import SchemaSet
from tests.platform.guest.conftest import SESSION_ID, START, FakeClock

HOOK = {"event": "PreToolUse", "tool": "Read", "decision": "allow"}
ERROR = {"code": "gateway_route"}


def test_open_creates_private_file_and_schema(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "new" / "journal.db"
    journal = Journal.open(str(path), clock)
    journal.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"journal", "session", "prompt_registry", "pending", "ping", "guest_meta"} <= tables


def test_reopen_keeps_records_and_head(tmp_path: Path, clock: FakeClock) -> None:
    path = str(tmp_path / "journal.db")
    first = Journal.open(path, clock)
    assert first.head == 0
    first.append("guest.error", ERROR)
    first.append("guest.error", ERROR)
    first.close()
    second = Journal.open(path, clock)
    try:
        assert second.head == 2
        records, head = second.read(0, 10)
        assert [record["seq"] for record in records] == [1, 2]
        assert head == 2
    finally:
        second.close()


def test_refuses_other_schema_version(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 7")
    with pytest.raises(JournalError) as error:
        Journal.open(str(path), clock)
    assert error.value.code == "journal_schema_mismatch"


def test_refuses_a_symlink(tmp_path: Path, clock: FakeClock) -> None:
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    (tmp_path / "journal.db").symlink_to(target)
    with pytest.raises(OSError, match=re.escape(os.strerror(errno.ELOOP))):
        Journal.open(str(tmp_path / "journal.db"), clock)


def test_append_and_read(journal: Journal, clock: FakeClock) -> None:
    assert journal.append("session.hook", HOOK, SESSION_ID) == 1
    clock.advance(1.5)
    assert journal.append("guest.error", ERROR) == 2
    records, head = journal.read(0, 10)
    assert head == 2
    assert records == [
        {"seq": 1, "ts": iso_ms(START), "session_id": SESSION_ID, "kind": "session.hook", "body": HOOK},
        {"seq": 2, "ts": iso_ms(START + 1.5), "session_id": None, "kind": "guest.error", "body": ERROR},
    ]
    assert journal.read(1, 10)[0] == records[1:]
    assert journal.read(0, 1)[0] == records[:1]
    assert journal.read(2, 10) == ([], 2)


def test_limit_is_clamped(journal: Journal) -> None:
    for _ in range(3):
        journal.append("guest.error", ERROR)
    assert len(journal.read(0, 0)[0]) == 1
    assert len(journal.read(0, MAX_LIMIT * 5)[0]) == 3


@pytest.mark.parametrize(
    ("kind", "body", "session_id", "code"),
    [
        ("session.unknown", HOOK, SESSION_ID, "kind_invalid"),
        ("session.hook", HOOK, None, "session_required"),
        ("session.hook", {"event": "PreToolUse", "decision": "maybe"}, SESSION_ID, "body_invalid"),
    ],
)
def test_append_refusals(journal: Journal, kind: str, body: dict[str, str], session_id: str | None, code: str) -> None:
    with pytest.raises(JournalError) as error:
        journal.append(kind, body, session_id)
    assert error.value.code == code
    assert journal.head == 0
    assert journal.read(0, 10) == ([], 0)


def test_body_invalid_names_pointers(journal: Journal) -> None:
    with pytest.raises(JournalError) as error:
        journal.append("session.hook", {"event": "PreToolUse", "decision": "maybe", "extra": 1}, SESSION_ID)
    assert error.value.detail == ["/decision", "/extra"]


def test_without_schemas_bodies_are_not_validated(tmp_path: Path, clock: FakeClock) -> None:
    journal = Journal.open(str(tmp_path / "j.db"), clock)
    try:
        assert journal.append("guest.error", {"anything": 1}) == 1
    finally:
        journal.close()


def test_kinds_match_the_contract(schemas: SchemaSet, contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "guest" / "journal" / "INDEX.json").read_text(encoding="utf-8"))
    assert tuple(row["kind"] for row in index["kinds"]) == KINDS


def test_records_validate_against_the_contract(journal: Journal, schemas: SchemaSet) -> None:
    journal.append("session.hook", HOOK, SESSION_ID)
    journal.append("guest.error", ERROR)
    records, head = journal.read(0, 10)
    assert schemas.errors("guest/journal.response.json", {"records": records, "head": head}) == []


def test_long_poll_returns_on_append(journal: Journal) -> None:
    timer = threading.Timer(0.1, journal.append, args=("guest.error", ERROR))
    started = time.monotonic()
    timer.start()
    records, head = journal.read(0, 10, wait=5)
    elapsed = time.monotonic() - started
    timer.join()
    assert [record["seq"] for record in records] == [1]
    assert head == 1
    assert elapsed < 4


def test_long_poll_times_out(journal: Journal) -> None:
    started = time.monotonic()
    assert journal.read(0, 10, wait=0.2) == ([], 0)
    assert time.monotonic() - started >= 0.2


def test_long_poll_ends_on_close(journal: Journal) -> None:
    timer = threading.Timer(0.1, journal.close)
    timer.start()
    started = time.monotonic()
    assert journal.read(0, 10, wait=5) == ([], 0)
    timer.join()
    assert time.monotonic() - started < 4
    journal.close()
    with pytest.raises(JournalError) as error:
        journal.append("guest.error", ERROR)
    assert error.value.code == "unavailable"


def test_ack_and_prune(journal: Journal, clock: FakeClock) -> None:
    for _ in range(3):
        journal.append("guest.error", ERROR)
    journal.ack(2)
    clock.advance(RETENTION_SECONDS - 1)
    assert journal.prune() == 0
    journal.ack(3)
    clock.advance(1)
    assert journal.prune() == 2
    assert [record["seq"] for record in journal.read(0, 10)[0]] == [3]
    clock.advance(RETENTION_SECONDS)
    assert journal.prune() == 1
    assert journal.read(0, 10) == ([], 3)
    assert journal.append("guest.error", ERROR) == 4


def test_unacknowledged_records_are_kept(journal: Journal, clock: FakeClock) -> None:
    journal.append("guest.error", ERROR)
    clock.advance(RETENTION_SECONDS * 10)
    assert journal.prune() == 0
    journal.ack(0)
    assert journal.prune() == 0


def test_ack_is_idempotent_and_keeps_the_first_time(journal: Journal, clock: FakeClock) -> None:
    journal.append("guest.error", ERROR)
    journal.ack(1)
    clock.advance(RETENTION_SECONDS)
    journal.ack(1)
    assert journal.prune() == 1


def test_ack_beyond_head(journal: Journal) -> None:
    journal.append("guest.error", ERROR)
    with pytest.raises(JournalError) as error:
        journal.ack(2)
    assert error.value.code == "state_conflict"


def test_transaction_rollback(journal: Journal) -> None:
    called: list[str] = []

    def failing() -> None:
        with journal.transaction() as db:
            journal.insert(db, "guest.error", ERROR)
            journal.after_commit(lambda: called.append("x"))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        failing()
    assert journal.head == 0
    assert called == []
    assert journal.read(0, 10) == ([], 0)
    with journal.transaction() as db:
        journal.insert(db, "guest.error", ERROR)
        journal.after_commit(lambda: called.append("y"))
    assert called == ["y"]
    assert journal.head == 1


def test_canonical() -> None:
    assert canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical({"a": float("nan")})


def test_clock_helpers(schemas: SchemaSet) -> None:
    assert iso_ms(0) == "1970-01-01T00:00:00.000Z"
    assert iso_ms(START + 0.1239) == "2026-09-21T14:13:20.123Z"
    first = uuid7(START)
    assert schemas.errors("common.json#/$defs/uuid", first) == []
    assert uuid7(START, lambda n: b"\xff" * n) == "01a0c450-6c00-7fff-bfff-ffffffffffff"
    assert uuid7(START, lambda n: b"\x00" * n) == "01a0c450-6c00-7000-8000-000000000000"
    assert abs(SystemClock().now() - time.time()) < 5


def test_failed_commit_rolls_back(journal: Journal) -> None:
    journal._db.execute("PRAGMA foreign_keys = ON")
    journal._db.execute("CREATE TEMP TABLE parent (id INTEGER PRIMARY KEY)")
    journal._db.execute("CREATE TEMP TABLE child (p INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)")

    def violate() -> None:
        with journal.transaction() as db:
            journal.insert(db, "guest.error", ERROR)
            db.execute("INSERT INTO child (p) VALUES (1)")

    with pytest.raises(sqlite3.IntegrityError):
        violate()
    assert journal.head == 0
    assert journal.append("guest.error", ERROR) == 1
    assert [record["seq"] for record in journal.read(0, 10)[0]] == [1]
