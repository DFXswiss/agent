"""The commands `export`, `verify` and `restore` (src/agent_core/record/commands.py).

RECORD-PROTOCOL.md section 12.3 and the tests of section 14 that concern the three commands: every
printed line and exit code, the file handling, `refused value_unwritable`, every refusal of
`restore`, the file format with its positions, the connect timeout, the identifier test and no
secret in any printed line. `verify` never connects; every test of a database runs against the
real PostgreSQL of AGENT_CORE_TEST_POSTGRES.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import psycopg
import pytest
from psycopg import sql

from agent_core.keys import KeyRing
from agent_core.record import commands, connection
from agent_core.record.checkpoint import Checkpoint
from agent_core.record.event import parse_event
from agent_core.record.export import write_export
from agent_core.record.storage import checkpoint_attempt, read_snapshot, verify_stored
from agent_core.signing import HubSigner, b64url_encode, parse_time
from tests.conftest import (
    DATABASES_LOCK,
    IDENTIFIER_LOCK,
    RecordDatabase,
    admin_params,
    advisory_lock,
    create_roles_and_database,
    drop_roles_and_database,
    recreate_database,
    record_database_for,
    schema_exists,
)
from tests.record_helpers import (
    ALICE,
    BOB,
    HUB,
    HUB_SIGNER,
    OTHER_HUB,
    OTHER_HUB_PUBLIC,
    OTHER_HUB_SIGNER,
    SEEDS,
    T0,
    Parallel,
    PureRecord,
    append_lock_held,
    closed_port,
    counts,
    hub_key_argument,
    insert_as_superuser,
    insert_checkpoint,
    load_ring,
    migrated,
    new_id,
    process,
    sign_event,
    signed_checkpoint,
    silent_listener,
    stored_event,
    tamper,
    unreachable_url,
    wait_for_append_lock,
    write_key_files,
    writer,
)
from tests.test_record_migrate import run_cli
from tests.test_record_reducer import private

EXPORTED_AT = parse_time("2026-11-18T02:00:00Z")
assert EXPORTED_AT is not None
WRITER_VARIABLE = "AGENT_CORE_RECORD_DATABASE"
KEYS_VARIABLE = "AGENT_CORE_KEYS"
TEAMS_VARIABLE = "AGENT_CORE_TEAMS"
H1 = T0 + 3600
CONFIG = ("refused config", 1)
DATABASE = ("refused database", 1)
# A connection string of the right form for the tests that never reach the database.
NO_DATABASE = f"postgresql://t_writer:{'0' * 64}@127.0.0.1:5432/t_record?sslmode=disable"


@pytest.fixture
def key_files(tmp_path: Path) -> tuple[Path, Path]:
    return write_key_files(tmp_path)


@pytest.fixture
def ring(key_files: tuple[Path, Path]) -> KeyRing:
    return load_ring(*key_files)


@pytest.fixture
def story(ring: KeyRing) -> PureRecord:
    record = PureRecord(ring)
    record.story()
    return record


@pytest.fixture
def no_connection(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every connection attempt is recorded and refused, so that a test sees whether one was made."""
    attempts: list[dict[str, Any]] = []

    def refuse(**kwargs: Any) -> None:
        attempts.append(kwargs)
        raise psycopg.OperationalError("no connection in this test")

    monkeypatch.setattr(psycopg, "connect", refuse)
    return attempts


def export(args: list[str], env: dict[str, str], clock: Callable[[], int] = lambda: EXPORTED_AT) -> tuple[str, int]:
    return commands.run_export(args, env, clock)


def verify(key_files: tuple[Path, Path], path: Path, *extra: str) -> tuple[str, int]:
    keys, teams = key_files
    return commands.run_verify(["--keys", str(keys), "--teams", str(teams), "--in", str(path), *extra], {})


def restore(env: dict[str, str], path: Path, *extra: str) -> tuple[str, int]:
    return commands.run_restore(["--in", str(path), *extra], env, lambda: T0)


def restore_env(url: str, key_files: tuple[Path, Path]) -> dict[str, str]:
    keys, teams = key_files
    return {WRITER_VARIABLE: url, KEYS_VARIABLE: str(keys), TEAMS_VARIABLE: str(teams)}


def written(tmp_path: Path, data: bytes, name: str = "export.jsonl") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def hub_key(signer: HubSigner = HUB_SIGNER) -> list[str]:
    return ["--hub-key", hub_key_argument(signer)]


def head_line(word: str, record: PureRecord) -> tuple[str, int]:
    return f"{word} {record.events[-1].position} {record.events[-1].record_hash}", 0


def stored_story(db: RecordDatabase, ring: KeyRing) -> str:
    """The story of three events in the database and the checkpoint of hub-1 at its head."""
    migrated(db)
    with process(db, ring) as hub:
        hub.story()
        hub.clock.now = H1
        assert checkpoint_attempt(hub.connection, hub.memory, ring, HUB_SIGNER, H1, hub.clock) is not None
        head = hub.memory.head()
    assert head is not None
    return head.record_hash


# --- export --------------------------------------------------------------------------------------


def test_export_of_an_empty_record_writes_the_header_0_and_null(record_db: RecordDatabase, tmp_path: Path) -> None:
    migrated(record_db)
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == ("exported 0 -", 0)
    assert out.read_bytes() == (
        b'{"format":"agent-record-export-v1","position":0,"record_hash":null,"exported_at":"2026-11-18T02:00:00Z"}\n'
    )


def test_export_writes_the_whole_record_with_mode_0600_and_prints_its_head(
    record_db: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], tmp_path: Path
) -> None:
    head = stored_story(record_db, ring)
    out = tmp_path / "export.jsonl"
    umask = os.umask(0o277)
    try:
        line = export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url})
    finally:
        os.umask(umask)
    assert line == (f"exported 3 {head}", 0)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    with writer(record_db) as reader:
        snapshot = read_snapshot(reader)
    assert out.read_bytes() == write_export(snapshot.events, snapshot.checkpoints, EXPORTED_AT)
    assert verify(key_files, out, *hub_key()) == (f"verified 3 {head}", 0)


EXPORT_ARGUMENTS = {
    "--out missing": [],
    "--out given twice": ["--out", "a.jsonl", "--out", "b.jsonl"],
    "an unknown argument": ["--out", "a.jsonl", "--in", "b.jsonl"],
    "--out without its value": ["--out"],
    "a value without its option": ["a.jsonl"],
    "--hub-key, which export does not take": ["--out", "a.jsonl", "--hub-key", hub_key_argument(HUB_SIGNER)],
}


@pytest.mark.parametrize("case", sorted(EXPORT_ARGUMENTS))
def test_export_refuses_its_arguments_with_refused_config(
    record_db: RecordDatabase, no_connection: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert export(EXPORT_ARGUMENTS[case], {WRITER_VARIABLE: record_db.writer_url}) == CONFIG
    assert no_connection == []
    assert list(tmp_path.iterdir()) == []


def names_refused(db: RecordDatabase) -> dict[str, str]:
    """Connection strings that break the form or the name rules of section 15."""
    out = {
        "empty": "",
        "without sslmode": db.writer_url.split("?")[0],
        "with another query parameter": db.writer_url + "&sslrootcert=system",
        "disable with a host name": db.url(db.writer_role, db.writer_password, host="postgres", sslmode="disable"),
    }
    for name in ("public", "pg_monitor", "pg_", "current_user", "current_role", "session_user", "user", "none", "PUBLIC", "Pg_x"):
        out[f"role {name}"] = db.url(name, db.writer_password)
        out[f"database {name}"] = db.url(db.writer_role, db.writer_password, database=name)
    return out


def test_export_refuses_a_missing_or_malformed_variable_without_connecting(
    record_db: RecordDatabase, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {}) == CONFIG
    for case, url in names_refused(record_db).items():
        assert export(["--out", str(out)], {WRITER_VARIABLE: url}) == CONFIG, case
    assert no_connection == []
    assert not out.exists()


@pytest.mark.parametrize("name", ["public_x", "x_pg_", "users"])
def test_export_accepts_a_name_that_only_contains_a_reserved_word(
    record_db: RecordDatabase, no_connection: list[dict[str, Any]], tmp_path: Path, name: str
) -> None:
    out = tmp_path / "export.jsonl"
    for url in (record_db.url(name, record_db.writer_password), record_db.url(record_db.writer_role, record_db.writer_password, database=name)):
        assert export(["--out", str(out)], {WRITER_VARIABLE: url}) == DATABASE
    assert len(no_connection) == 2
    assert not out.exists()


@pytest.mark.parametrize("version", [150007, 159999])
def test_export_and_restore_refuse_a_libpq_below_16(
    record_db: RecordDatabase,
    key_files: tuple[Path, Path],
    story: PureRecord,
    no_connection: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: int,
) -> None:
    monkeypatch.setattr(connection, "libpq_version", lambda: version)
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == CONFIG
    assert not out.exists()
    path = written(tmp_path, story.export(), "restore.jsonl")
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == CONFIG
    assert no_connection == []


def test_export_refuses_an_existing_file_and_leaves_it_unchanged(
    record_db: RecordDatabase, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    existing = written(tmp_path, b"keep\n", "existing.jsonl")
    assert export(["--out", str(existing)], {WRITER_VARIABLE: record_db.writer_url}) == ("refused file_exists", 1)
    assert existing.read_bytes() == b"keep\n"
    # A symbolic link counts as an existing file, also when it points nowhere.
    link = tmp_path / "link.jsonl"
    link.symlink_to(tmp_path / "missing.jsonl")
    assert export(["--out", str(link)], {WRITER_VARIABLE: record_db.writer_url}) == ("refused file_exists", 1)
    assert link.is_symlink() and not (tmp_path / "missing.jsonl").exists()
    assert no_connection == []


def test_export_refuses_a_file_that_cannot_be_created(
    record_db: RecordDatabase, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    not_a_directory = written(tmp_path, b"", "plain")
    for out in (not_a_directory / "export.jsonl", tmp_path / "missing" / "export.jsonl"):
        assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == ("refused file_write", 1)
    assert no_connection == []


def test_export_refuses_a_file_that_cannot_be_written_and_removes_it(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    migrated(record_db)

    def full(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == ("refused file_write", 1)
    assert not out.exists()


def test_export_refuses_an_unreachable_database_and_a_database_without_the_schema(
    record_db: RecordDatabase, tmp_path: Path
) -> None:
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: unreachable_url(record_db, closed_port())}) == DATABASE
    assert not out.exists()
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == DATABASE
    assert not out.exists()


def received_at_of_the_last_event(value: str) -> Callable[[RecordDatabase], None]:
    def change(db: RecordDatabase) -> None:
        tamper(db, "record.event", "UPDATE record.event SET received_at = %s::timestamptz WHERE position = 3", (value,))

    return change


def published_at_of_the_checkpoint(value: str) -> Callable[[RecordDatabase], None]:
    def change(db: RecordDatabase) -> None:
        tamper(db, "record.checkpoint", "UPDATE record.checkpoint SET published_at = %s::timestamptz", (value,))

    return change


def hub_key_id_outside_the_id_rule(db: RecordDatabase) -> None:
    insert_checkpoint(db, Checkpoint('hub"2', 1, "c" * 64, T0, bytes(64)))


UNWRITABLE = {
    "received_at in the year 10000": received_at_of_the_last_event("10000-01-01 00:00:00+00"),
    "received_at infinity": received_at_of_the_last_event("infinity"),
    "published_at in the year 10000": published_at_of_the_checkpoint("10000-01-01 00:00:00+00"),
    "published_at -infinity": published_at_of_the_checkpoint("-infinity"),
    'hub_key_id hub"2': hub_key_id_outside_the_id_rule,
}


@pytest.mark.parametrize("case", sorted(UNWRITABLE))
def test_export_refuses_a_stored_value_without_the_form_of_its_member(
    record_db: RecordDatabase, ring: KeyRing, tmp_path: Path, case: str
) -> None:
    stored_story(record_db, ring)
    UNWRITABLE[case](record_db)
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == ("refused value_unwritable", 1)
    assert not out.exists()


def test_export_and_restore_pass_connect_timeout_10_to_the_driver(
    record_db: RecordDatabase,
    key_files: tuple[Path, Path],
    story: PureRecord,
    no_connection: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == DATABASE
    assert not out.exists()
    path = written(tmp_path, story.export(), "restore.jsonl")
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == DATABASE
    assert [attempt["connect_timeout"] for attempt in no_connection] == [10, 10]


def test_export_and_restore_give_up_on_a_server_that_never_answers_within_15_seconds(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    out = tmp_path / "export.jsonl"
    path = written(tmp_path, story.export(), "restore.jsonl")
    with silent_listener() as port:
        url = unreachable_url(record_db, port)
        started = time.monotonic()
        assert export(["--out", str(out)], {WRITER_VARIABLE: url}) == DATABASE
        assert time.monotonic() - started < 15
        assert not out.exists()
        started = time.monotonic()
        assert restore(restore_env(url, key_files), path, *hub_key()) == DATABASE
        assert time.monotonic() - started < 15


def test_export_reads_one_snapshot_and_its_file_verifies_and_restores(
    record_db: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        head = first.memory.head()
        assert head is not None
        second.clock.now = first.clock.now
        original = read_snapshot

        def concurrent() -> None:
            second.append(BOB, "comment", new_id(), [("task", None, work), ("body", None, "Late")])
            second.clock.now = H1
            assert checkpoint_attempt(second.connection, second.memory, ring, HUB_SIGNER, H1, second.clock) is not None

        monkeypatch.setattr(commands, "read_snapshot", lambda reader: original(reader, concurrent))
        out = tmp_path / "export.jsonl"
        assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}) == (f"exported 3 {head.record_hash}", 0)
    lines = [json.loads(line) for line in out.read_bytes().splitlines()]
    assert (lines[0]["position"], lines[0]["record_hash"]) == (lines[-1]["position"], lines[-1]["record_hash"]) == (3, head.record_hash)
    assert verify(key_files, out, *hub_key()) == (f"verified 3 {head.record_hash}", 0)
    recreate_database(record_db)
    migrated(record_db)
    assert restore(restore_env(record_db.writer_url, key_files), out, *hub_key()) == (f"restored 3 {head.record_hash}", 0)


def test_checkpoints_of_two_hub_key_ids_are_exported_in_byte_order_whatever_the_collation(
    record_db_en_us: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], tmp_path: Path
) -> None:
    db = record_db_en_us
    migrated(db)
    with process(db, ring) as hub:
        hub.story()
        events = read_snapshot(hub.connection).events
    signers = [HubSigner.from_seed_hex("hub-2", "63" * 32), HubSigner.from_seed_hex("hub1", "64" * 32)]
    for signer in reversed(signers):
        for stored in (events[2], events[0]):
            insert_checkpoint(db, signed_checkpoint(signer, stored.position, stored.record_hash, H1))
    out = tmp_path / "export.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: db.writer_url}) == (f"exported 3 {events[2].record_hash}", 0)
    lines = [json.loads(line) for line in out.read_bytes().splitlines()]
    assert [(line["signed_by"], line["position"]) for line in lines if line.get("type") == "checkpoint"] == [
        ("hub-2", 1),
        ("hub-2", 3),
        ("hub1", 1),
        ("hub1", 3),
    ]
    keys = [argument for signer in signers for argument in hub_key(signer)]
    assert verify(key_files, out, *keys) == (f"verified 3 {events[2].record_hash}", 0)
    recreate_database(db)
    migrated(db)
    assert restore(restore_env(db.writer_url, key_files), out, *keys) == (f"restored 3 {events[2].record_hash}", 0)
    assert counts(db) == (3, 4)


# --- verify --------------------------------------------------------------------------------------


def test_verify_accepts_an_export_and_prints_its_head(
    key_files: tuple[Path, Path], story: PureRecord, ring: KeyRing, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    path = written(tmp_path, story.export())
    assert verify(key_files, path) == head_line("verified", story)
    assert verify(key_files, path, *hub_key()) == head_line("verified", story)
    empty = written(tmp_path, PureRecord(ring).export(), "empty.jsonl")
    assert verify(key_files, empty) == ("verified 0 -", 0)
    assert no_connection == []


def verify_arguments(keys: Path, teams: Path, path: Path) -> dict[str, list[str]]:
    base = ["--keys", str(keys), "--teams", str(teams), "--in", str(path)]
    return {
        "--keys missing": base[2:],
        "--teams missing": base[:2] + base[4:],
        "--in missing": base[:4],
        "--keys twice": base + ["--keys", str(keys)],
        "--teams twice": base + ["--teams", str(teams)],
        "--in twice": base + ["--in", str(path)],
        "an unknown argument": base + ["--out", str(path)],
        "an option without its value": base + ["--hub-key"],
        "a --hub-key without =": base + ["--hub-key", HUB],
        "a --hub-key with a malformed id": base + ["--hub-key", "Hub-1=" + HUB_SIGNER.public_key_text],
        "a --hub-key with a key of 31 bytes": base + ["--hub-key", f"{HUB}={b64url_encode(bytes(31))}"],
        "a --hub-key with a padded key": base + ["--hub-key", f"{HUB}={HUB_SIGNER.public_key_text}="],
        "a --hub-key with a small-order key": base + ["--hub-key", f"{HUB}={b64url_encode(bytes([1]) + bytes(31))}"],
        "a repeated --hub-key id": base + hub_key() + ["--hub-key", f"{HUB}={OTHER_HUB_SIGNER.public_key_text}"],
        "an unreadable file": base[:5] + [str(path.parent / "missing.jsonl")],
        "a directory as the file": base[:5] + [str(path.parent)],
        "a keys file that does not exist": ["--keys", str(keys.parent / "missing.yaml")] + base[2:],
        "a teams file that does not exist": base[:2] + ["--teams", str(teams.parent / "missing.yaml")] + base[4:],
    }


def test_verify_refuses_its_arguments_and_inputs_with_refused_config(
    key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    keys, teams = key_files
    path = written(tmp_path, story.export())
    for case, args in verify_arguments(keys, teams, path).items():
        assert commands.run_verify(args, {}) == CONFIG, case


@pytest.mark.parametrize(
    ("which", "content"),
    [
        ("keys", "keys: [\n"),
        ("keys", "keys:\n- id: x\n"),
        ("keys", None),
        ("teams", "teams: [\n"),
        ("teams", "nothing: {}\n"),
        ("teams", "teams:\n  ? [a]\n  : x\n"),
        ("teams", b"\xff\xfe"),
    ],
    ids=["keys not YAML", "keys entry broken", "keys team not in the teams file", "teams not YAML", "teams without teams",
         "teams with a list as key", "teams not UTF-8"],
)
def test_verify_refuses_a_keys_or_teams_file_that_does_not_load(
    key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path, which: str, content: str | bytes | None
) -> None:
    keys, teams = key_files
    path = written(tmp_path, story.export())
    if content is None:
        # The keys file names the team `example`; a teams file without it does not load the keys.
        teams.write_text("teams:\n  other:\n    members: [alice, bob, cara]\n", encoding="utf-8")
    else:
        target = keys if which == "keys" else teams
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    assert verify((keys, teams), path) == CONFIG


def with_event(record: PureRecord, key_id: str, object_type: str, object_id: str, changes: Any, **options: Any) -> bytes:
    """The export of `record` with one more validly signed and chained event of `key_id`."""
    text, signature = sign_event(record.state, key_id, object_type, object_id, changes, record.now, **options)
    extra = stored_event(len(record.events) + 1, record.now, text, signature, record.events[-1].record_hash)
    return write_export([*record.events, extra], record.checkpoints, record.now)


def work_of(record: PureRecord) -> str:
    return next(object_id for object_id, item in record.state.objects.items() if item.object_type == "task")


def changed(record: PureRecord, index: int, **fields: Any) -> bytes:
    events = list(record.events)
    events[index] = replace(events[index], **fields)
    return write_export(events, record.checkpoints, record.now)


FAILED: dict[str, tuple[Callable[[PureRecord], bytes], tuple[str, int]]] = {
    "a changed signature": (
        lambda record: changed(record, 1, signature=private(SEEDS[ALICE]).sign(record.events[1].text)),
        ("event_signature_invalid", 2),
    ),
    "a broken chain": (
        lambda record: with_event(record, ALICE, "comment", new_id(), [("task", None, work_of(record)), ("body", None, "x")], prev_hash="1" * 64),
        ("chain_broken", 4),
    ),
    "a changed record hash": (lambda record: changed(record, 1, record_hash="d" * 64), ("record_hash_mismatch", 2)),
    "a gap in the positions": (
        lambda record: write_export(
            [*record.events[:2], replace(record.events[2], position=4)], [], record.now
        ),
        ("position_gap", 3),
    ),
    "an event the reducer refuses": (
        lambda record: with_event(record, BOB, "task", work_of(record), [("title", "Fix login", "Stale")]),
        ("event_refused", 4),
    ),
    "two events of two keys with the same event_id": (
        lambda record: with_event(
            record, BOB, "comment", new_id(), [("task", None, work_of(record)), ("body", None, "x")],
            event_id=parse_event(record.events[0].text).event_id,
        ),
        ("event_id_taken", 4),
    ),
    "a key certificate": (
        lambda record: with_event(
            record, ALICE, "comment", new_id(), [("task", None, work_of(record)), ("body", None, "x")], certificate=b"cert"
        ),
        ("certificate_not_supported", 4),
    ),
}


@pytest.mark.parametrize("case", sorted(FAILED))
def test_verify_of_an_export_that_fails_prints_the_failure_and_its_position(
    key_files: tuple[Path, Path], story: PureRecord, no_connection: list[dict[str, Any]], tmp_path: Path, case: str
) -> None:
    make, (failure, position) = FAILED[case]
    path = written(tmp_path, make(story))
    assert verify(key_files, path) == (f"failed {position} {failure}", 1)
    # `restore` fails the same way at step 1 (c) and connects to no database, so it inserts nothing.
    assert restore(restore_env(NO_DATABASE, key_files), path, *hub_key()) == (f"failed {position} {failure}", 1)
    assert no_connection == []


def test_verify_of_an_export_without_a_key_of_the_keys_file_prints_key_unknown(
    story: PureRecord, tmp_path: Path
) -> None:
    directory = tmp_path / "without-bob"
    directory.mkdir()
    path = written(tmp_path, story.export())
    assert verify(write_key_files(directory, (ALICE,)), path) == ("failed 2 key_unknown", 1)


def test_verify_checks_a_checkpoint_only_with_its_hub_key(key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path) -> None:
    checkpoints = [replace(story.checkpoints[0], record_hash="c" * 64)]
    path = written(tmp_path, write_export(story.events, checkpoints, story.now))
    assert verify(key_files, path, *hub_key()) == ("failed 3 checkpoint_invalid", 1)
    assert verify(key_files, path) == head_line("verified", story)


# --- the file format (section 12.3) --------------------------------------------------------------


def lines_of(data: bytes) -> list[bytes]:
    return data[:-1].split(b"\n")


def joined(lines: list[bytes]) -> bytes:
    return b"\n".join(lines) + b"\n"


def replaced(index: int, make: Callable[[bytes], bytes]) -> Callable[[bytes], bytes]:
    def change(data: bytes) -> bytes:
        lines = lines_of(data)
        lines[index] = make(lines[index])
        return joined(lines)

    return change


def reordered(order: list[int]) -> Callable[[bytes], bytes]:
    def change(data: bytes) -> bytes:
        lines = lines_of(data)
        return joined([lines[index] for index in order])

    return change


def member(line: bytes, name: str, value: bytes) -> bytes:
    """The line with the JSON text of member `name` replaced by `value` as written."""
    start = line.index(b'"' + name.encode() + b'":') + len(name) + 3
    end = start
    depth_string = line[start:start + 1] == b'"'
    if depth_string:
        end = line.index(b'"', start + 1) + 1
    else:
        while line[end:end + 1] not in (b",", b"}"):
            end += 1
    return line[:start] + value + line[end:]


def with_space_in_event_hash(line: bytes) -> bytes:
    start = line.index(b'"event_hash":"') + len(b'"event_hash":"')
    return line[:start] + b"a " + line[start + 2:]


def with_escape_in_event_hash(line: bytes) -> bytes:
    start = line.index(b'"event_hash":"') + len(b'"event_hash":"')
    return line[:start] + b"\\u%04x" % line[start] + line[start + 1:]


def in_another_order(line: bytes) -> bytes:
    value = json.loads(line)
    return json.dumps(dict(reversed(list(value.items()))), separators=(",", ":")).encode()


def header_only(position: bytes, record_hash: bytes) -> Callable[[bytes], bytes]:
    def change(data: bytes) -> bytes:
        header = member(member(lines_of(data)[0], "position", position), "record_hash", record_hash)
        return joined([header])

    return change


def checkpoint_after_an_empty_header(data: bytes) -> bytes:
    lines = lines_of(data)
    return joined([member(member(lines[0], "position", b"0"), "record_hash", b"null"), lines[-1]])


# The story file: line 1 the header, lines 2 to 4 the events at positions 1 to 3, line 5 the
# checkpoint of hub-1 at position 3 (index 0 to 4 below).
FILE_INVALID: dict[str, tuple[Callable[[bytes], bytes], int]] = {
    "a file that is not UTF-8": (lambda data: b"\xff" + data, 0),
    "an empty file": (lambda data: b"", 0),
    "a missing final line feed": (lambda data: data[:-1], 0),
    "an empty line": (lambda data: joined(lines_of(data)[:2] + [b""] + lines_of(data)[2:]), 2),
    "a line that is not JSON": (replaced(2, lambda line: b"{"), 2),
    "a member twice": (replaced(1, lambda line: line[:-1] + b',"type":"event"}'), 1),
    "a header with an unknown format": (replaced(0, lambda line: member(line, "format", b'"agent-record-export-v2"')), 0),
    "an unknown type": (replaced(1, lambda line: member(line, "type", b'"note"')), 1),
    "a missing member": (replaced(1, lambda line: line[: line.index(b',"signature"')] + b"}"), 1),
    "an unknown member": (replaced(1, lambda line: line[:-1] + b',"extra":"x"}'), 1),
    "an event line after a checkpoint line": (reordered([0, 1, 2, 4, 3]), 3),
    "the same checkpoint twice": (lambda data: data + lines_of(data)[4] + b"\n", 4),
    **{
        f"an event position written as {text.decode()}": (replaced(1, lambda line, text=text: member(line, "position", text)), 1)
        for text in (b"1.0", b"1e0", b"-0", b"01", b'"1"')
    },
    **{
        f"a header position written as {text.decode()}": (replaced(0, lambda line, text=text: member(line, "position", text)), 0)
        for text in (b"3.0", b"3e0", b"-0", b"03", b'"3"')
    },
    "a header whose position differs from the last event line": (replaced(0, lambda line: member(line, "position", b"2")), 0),
    "a header whose record_hash differs from the last event line": (
        replaced(0, lambda line: member(line, "record_hash", b'"' + b"d" * 64 + b'"')),
        0,
    ),
    "a header that is not 0 and null without event lines": (header_only(b"3", b'"' + b"d" * 64 + b'"'), 0),
    "a checkpoint line after a header 0 and null without event lines": (checkpoint_after_an_empty_header, 0),
    "an empty first line": (lambda data: b"\n" + data, 0),
    "a first line that is not JSON": (replaced(0, lambda line: b"header"), 0),
    "a space between two tokens of the header": (replaced(0, lambda line: line.replace(b'"position":', b'"position": ')), 0),
    "a space between two tokens of an event line": (replaced(2, lambda line: line.replace(b'"position":', b'"position": ')), 2),
    "a space before an event line": (replaced(2, lambda line: b" " + line), 2),
    "a space after an event line": (replaced(2, lambda line: line + b" "), 2),
    "an event line with its members in another order": (replaced(2, in_another_order), 2),
    "an event_hash written with an escape": (replaced(2, with_escape_in_event_hash), 2),
    "a space inside the event_hash of an event line": (replaced(2, with_space_in_event_hash), 2),
    "an unknown format and a later space inside a string": (
        lambda data: replaced(2, with_space_in_event_hash)(
            replaced(0, lambda line: member(line, "format", b'"agent-record-export-v2"'))(data)
        ),
        0,
    ),
    "a space inside a string of line 2 and line 5 not JSON": (
        lambda data: replaced(4, lambda line: b"x")(replaced(1, with_space_in_event_hash)(data)),
        4,
    ),
    "line 2 not JSON, line 3 an event line, line 4 empty": (
        lambda data: replaced(3, lambda line: b"")(replaced(1, lambda line: b"x")(data)),
        2,
    ),
    "line 2 an event line, line 3 of type x, line 4 empty": (
        lambda data: replaced(3, lambda line: b"")(replaced(2, lambda line: member(line, "type", b'"x"'))(data)),
        2,
    ),
}


def two_checkpoints(story: PureRecord) -> bytes:
    """The story with a second checkpoint of hub-1 at position 2, in the order of the writer."""
    second = signed_checkpoint(HUB_SIGNER, 2, story.events[1].record_hash, story.now)
    return write_export(story.events, [*story.checkpoints, second], story.now)


@pytest.mark.parametrize("case", sorted(FILE_INVALID))
def test_verify_and_restore_refuse_a_file_outside_the_format_with_its_position(
    key_files: tuple[Path, Path], story: PureRecord, no_connection: list[dict[str, Any]], tmp_path: Path, case: str
) -> None:
    make, position = FILE_INVALID[case]
    path = written(tmp_path, make(story.export()))
    expected = (f"failed {position} file_invalid", 1)
    assert verify(key_files, path) == expected
    assert verify(key_files, path, *hub_key()) == expected
    assert restore(restore_env(NO_DATABASE, key_files), path, *hub_key()) == expected
    assert restore(restore_env(NO_DATABASE, key_files), path) == expected
    assert no_connection == []


def test_verify_and_restore_refuse_checkpoint_lines_out_of_order(
    key_files: tuple[Path, Path], story: PureRecord, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    data = two_checkpoints(story)
    path = written(tmp_path, reordered([0, 1, 2, 3, 5, 4])(data))
    assert verify(key_files, path) == ("failed 4 file_invalid", 1)
    assert restore(restore_env(NO_DATABASE, key_files), path, *hub_key()) == ("failed 4 file_invalid", 1)
    # In the order of the writer the same file verifies.
    assert verify(key_files, written(tmp_path, data, "ordered.jsonl"), *hub_key()) == head_line("verified", story)
    assert no_connection == []


# --- restore -------------------------------------------------------------------------------------


def test_restore_into_a_migrated_empty_database_inserts_the_record_as_exported(
    record_db: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    migrated(record_db)
    data = story.export()
    path = written(tmp_path, data)
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == head_line("restored", story)
    assert counts(record_db) == (3, 1)
    out = tmp_path / "again.jsonl"
    assert export(["--out", str(out)], {WRITER_VARIABLE: record_db.writer_url}, lambda: story.now) == head_line("exported", story)
    assert out.read_bytes() == data
    # A hub with its own hub key id verifies the restored record, the copied columns included,
    # and checks of the checkpoint of hub-1 only the form.
    with writer(record_db) as reader:
        assert verify_stored(reader, ring, {OTHER_HUB: OTHER_HUB_PUBLIC}, None).failure is None


def test_restore_of_an_empty_export_prints_0(record_db: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], tmp_path: Path) -> None:
    migrated(record_db)
    path = written(tmp_path, PureRecord(ring).export())
    assert restore(restore_env(record_db.writer_url, key_files), path) == ("restored 0 -", 0)
    assert counts(record_db) == (0, 0)


def test_restore_refuses_a_database_without_the_schema_and_creates_none(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    path = written(tmp_path, story.export())
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == (
        "refused schema_missing (run python -m agent_core.record migrate first)",
        1,
    )
    assert not schema_exists(record_db)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())",
        "DELETE FROM record.schema_version",
    ],
    ids=["an unknown version", "no version row"],
)
def test_restore_refuses_a_schema_of_another_version(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path, statement: str
) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute(statement)
    path = written(tmp_path, story.export())
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == ("refused schema_version", 1)
    assert counts(record_db) == (0, 0)


WRITER_RIGHTS = {
    "a writer with UPDATE on record.event": "GRANT UPDATE ON record.event TO {writer}",
    "a SECURITY DEFINER function executable through PUBLIC": (
        "CREATE FUNCTION public.t_definer() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
    ),
}


@pytest.mark.parametrize("case", sorted(WRITER_RIGHTS))
def test_restore_refuses_a_writer_with_a_right_beyond_section_11_2(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path, case: str
) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute(sql.SQL(WRITER_RIGHTS[case]).format(writer=sql.Identifier(record_db.writer_role)))
    path = written(tmp_path, story.export())
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == ("refused writer_rights", 1)
    assert counts(record_db) == (0, 0)


def test_restore_refuses_a_server_with_a_second_user_database(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    migrated(record_db)
    other = f"t_second_{new_id().replace('-', '')[:12]}"
    with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(other)))
    try:
        path = written(tmp_path, story.export())
        assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == ("refused writer_rights", 1)
    finally:
        with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(other)))
    assert counts(record_db) == (0, 0)


def test_restore_refuses_a_database_that_holds_a_row_and_leaves_its_rows_unchanged(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, ring: KeyRing, tmp_path: Path
) -> None:
    migrated(record_db)
    path = written(tmp_path, story.export())
    env = restore_env(record_db.writer_url, key_files)
    assert restore(env, path, *hub_key()) == head_line("restored", story)
    with writer(record_db) as reader:
        before = read_snapshot(reader)
    assert restore(env, path, *hub_key()) == ("refused not_empty", 1)
    with writer(record_db) as reader:
        assert read_snapshot(reader) == before
    # Also a database with only a checkpoint is not empty.
    recreate_database(record_db)
    migrated(record_db)
    insert_checkpoint(record_db, story.checkpoints[0])
    assert restore(env, path, *hub_key()) == ("refused not_empty", 1)
    assert counts(record_db) == (0, 1)


@pytest.mark.parametrize("isolation", [None, "repeatable read"])
def test_restore_that_waited_for_the_lock_sees_a_row_committed_meanwhile(
    record_db: RecordDatabase, ring: KeyRing, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path, isolation: str | None
) -> None:
    migrated(record_db)
    if isolation is not None:
        with record_db.admin() as admin:
            admin.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_isolation = {}").format(
                    sql.Identifier(record_db.writer_role), sql.Literal(isolation)
                )
            )
    path = written(tmp_path, story.export())
    env = restore_env(record_db.writer_url, key_files)
    with append_lock_held(record_db):
        started = Parallel(lambda: restore(env, path, *hub_key()))
        wait_for_append_lock(record_db, 1)
        insert_as_superuser(record_db, story.events[0])
    assert started.results() == [("refused not_empty", 1)]
    assert counts(record_db) == (1, 0)


def test_restore_refuses_an_unreachable_database(key_files: tuple[Path, Path], story: PureRecord, record_db: RecordDatabase, tmp_path: Path) -> None:
    path = written(tmp_path, story.export())
    assert restore(restore_env(unreachable_url(record_db, closed_port()), key_files), path, *hub_key()) == DATABASE


def test_restore_of_a_changed_file_prints_the_failure_and_inserts_nothing(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    migrated(record_db)
    path = written(tmp_path, changed(story, 1, record_hash="d" * 64))
    assert restore(restore_env(record_db.writer_url, key_files), path, *hub_key()) == ("failed 2 record_hash_mismatch", 1)
    assert counts(record_db) == (0, 0)


def test_restore_refuses_a_checkpoint_whose_hub_key_is_not_given_before_the_rest_of_the_verification(
    key_files: tuple[Path, Path], story: PureRecord, no_connection: list[dict[str, Any]], tmp_path: Path
) -> None:
    env = restore_env(NO_DATABASE, key_files)
    sound = written(tmp_path, story.export())
    assert restore(env, sound) == ("refused hub_key_missing", 1)
    assert restore(env, sound, *hub_key(OTHER_HUB_SIGNER)) == ("refused hub_key_missing", 1)
    broken = written(tmp_path, changed(story, 1, record_hash="d" * 64), "broken.jsonl")
    assert restore(env, broken) == ("refused hub_key_missing", 1)
    assert no_connection == []


def restore_arguments(path: Path) -> dict[str, list[str]]:
    base = ["--in", str(path), *hub_key()]
    return {
        "--in missing": hub_key(),
        "--in twice": base + ["--in", str(path)],
        "an unknown argument": base + ["--keys", str(path)],
        "an option without its value": base + ["--in"],
        "a malformed --hub-key": ["--in", str(path), "--hub-key", "hub-1"],
        "a repeated --hub-key id": base + hub_key(),
        "an unreadable file": ["--in", str(path.parent / "missing.jsonl"), *hub_key()],
    }


def test_restore_refuses_its_arguments_and_variables_with_refused_config(
    record_db: RecordDatabase,
    key_files: tuple[Path, Path],
    story: PureRecord,
    no_connection: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    path = written(tmp_path, story.export())
    env = restore_env(record_db.writer_url, key_files)
    for case, args in restore_arguments(path).items():
        assert commands.run_restore(args, env, lambda: T0) == CONFIG, case
    keys, teams = key_files
    variables = {
        "AGENT_CORE_KEYS missing": {k: v for k, v in env.items() if k != KEYS_VARIABLE},
        "AGENT_CORE_TEAMS missing": {k: v for k, v in env.items() if k != TEAMS_VARIABLE},
        "AGENT_CORE_RECORD_DATABASE missing": {k: v for k, v in env.items() if k != WRITER_VARIABLE},
        "AGENT_CORE_KEYS empty": {**env, KEYS_VARIABLE: ""},
        "AGENT_CORE_TEAMS empty": {**env, TEAMS_VARIABLE: ""},
        "AGENT_CORE_KEYS invalid": {**env, KEYS_VARIABLE: str(tmp_path / "missing.yaml")},
        "AGENT_CORE_TEAMS invalid": {**env, TEAMS_VARIABLE: str(tmp_path)},
        **{f"AGENT_CORE_RECORD_DATABASE {case}": {**env, WRITER_VARIABLE: url} for case, url in names_refused(record_db).items()},
    }
    for case, variables_of_case in variables.items():
        assert restore(variables_of_case, path, *hub_key()) == CONFIG, case
    teams.write_text("teams: [\n", encoding="utf-8")
    assert restore(env, path, *hub_key()) == CONFIG
    teams.write_text("teams:\n  example:\n    members: [alice, bob, cara]\n", encoding="utf-8")
    keys.write_text("keys: {}\n", encoding="utf-8")
    assert restore(env, path, *hub_key()) == CONFIG
    assert no_connection == []


# --- the identifier test and the printed lines ---------------------------------------------------


def test_export_and_restore_with_role_and_database_names_that_are_sql_keywords(
    key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    params = admin_params()
    with advisory_lock(params, IDENTIFIER_LOCK), advisory_lock(params, DATABASES_LOCK):
        db = record_database_for(params, "order", "grant", "select")
        with psycopg.connect(**params, autocommit=True) as admin:
            drop_roles_and_database(admin, db.name, (db.writer_role, db.owner_role))
            create_roles_and_database(admin, db.name, (db.owner_role, db.owner_password), (db.writer_role, db.writer_password))
        try:
            migrated(db)
            empty = tmp_path / "empty.jsonl"
            assert export(["--out", str(empty)], {WRITER_VARIABLE: db.writer_url}) == ("exported 0 -", 0)
            path = written(tmp_path, story.export())
            assert restore(restore_env(db.writer_url, key_files), path, *hub_key()) == head_line("restored", story)
            out = tmp_path / "again.jsonl"
            assert export(["--out", str(out)], {WRITER_VARIABLE: db.writer_url}, lambda: story.now) == head_line("exported", story)
            assert out.read_bytes() == path.read_bytes()
        finally:
            with psycopg.connect(**params, autocommit=True) as admin:
                drop_roles_and_database(admin, db.name, (db.writer_role, db.owner_role))


def test_the_commands_print_their_one_line_and_no_secret(
    record_db: RecordDatabase, key_files: tuple[Path, Path], story: PureRecord, tmp_path: Path
) -> None:
    migrated(record_db)
    keys, teams = key_files
    path = written(tmp_path, story.export())
    env = restore_env(record_db.writer_url, key_files)
    restored = run_cli(["restore", "--in", str(path), *hub_key()], env)
    assert (restored.stdout, restored.stderr, restored.returncode) == (head_line("restored", story)[0] + "\n", "", 0)
    out = tmp_path / "export-cli.jsonl"
    exported = run_cli(["export", "--out", str(out)], {WRITER_VARIABLE: record_db.writer_url})
    assert (exported.stdout, exported.stderr, exported.returncode) == (head_line("exported", story)[0] + "\n", "", 0)
    verified = run_cli(["verify", "--keys", str(keys), "--teams", str(teams), "--in", str(out), *hub_key()], {})
    assert (verified.stdout, verified.stderr, verified.returncode) == (head_line("verified", story)[0] + "\n", "", 0)
    again = run_cli(["export", "--out", str(out)], {WRITER_VARIABLE: record_db.writer_url})
    assert (again.stdout, again.stderr, again.returncode) == ("refused file_exists\n", "", 1)
    refused = run_cli(
        ["export", "--out", str(tmp_path / "missing-role.jsonl")],
        {WRITER_VARIABLE: record_db.url(record_db.writer_role + "_missing", record_db.writer_password)},
    )
    assert (refused.stdout, refused.stderr, refused.returncode) == ("refused database\n", "", 1)
    outputs = [result.stdout + result.stderr for result in (restored, exported, verified, again, refused)]
    for output in outputs:
        for secret in record_db.secret_values():
            assert secret not in output
        assert record_db.writer_role not in output and record_db.name not in output
