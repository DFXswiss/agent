"""U-HUB-audit (chain part, R-6.7): gapless `seq` under parallel writers, the hash formula of 02-data-model.md
section 2.9 against hand-computed vectors, tamper detection on a copy and in the database, row form."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from agent_hub.audit import chain
from agent_hub.audit.actions import AUDIT_ACTIONS
from agent_hub.audit.chain import (
    AuditIsolationError,
    AuditRow,
    AuditRowInvalid,
    ChainCheck,
    append_audit,
    audit_hash,
    hash_text,
    read_audit,
    row_json,
    validate_row,
    verify_chain,
    verify_rows,
)
from tests.platform.contracts.harness import Contracts
from tests.platform.hub.conftest import FixedClock, HubDatabase


def uid(n: int) -> UUID:
    return UUID(f"0190f0f0-0000-7000-8000-{n:012d}")


AT_1 = datetime(2026, 11, 21, 12, 0, 0, 123456, tzinfo=UTC)
REFUSED = {"code": "signature_invalid", "key_id": None}
ROW_1 = AuditRow(at=AT_1, actor_kind="hub", action="read.me", result="ok")
ROW_2 = AuditRow(
    at=datetime(2026, 11, 21, 13, 0, 1, tzinfo=timezone(timedelta(hours=1))),
    actor_kind="session",
    action="hook.denied",
    result="denied",
    actor_id=uid(1),
    org_id=uid(2),
    wg_id=uid(3),
    task_id=uid(4),
    session_id=uid(5),
    device_id=uid(6),
    target_kind="tool",
    target_id="Bash",
    detail={"tool": "Bash", "rule": 3},
    event_id=uid(7),
)

# Hand-written canonical rows and their SHA-256 (computed outside the code under test).
CANONICAL_1 = (
    '{"action":"read.me","actor_id":null,"actor_kind":"hub","at":"2026-11-21T12:00:00.123Z","detail":{},'
    '"device_id":null,"event_id":null,"org_id":null,"result":"ok","seq":1,"session_id":null,"target_id":null,'
    '"target_kind":null,"task_id":null,"wg_id":null}'
)
HASH_1 = "d204d9d6cdff5623521e13fc5a453761ab235d17073a7d65363e524736eca8ac"
CANONICAL_2 = (
    '{"action":"hook.denied","actor_id":"0190f0f0-0000-7000-8000-000000000001","actor_kind":"session",'
    '"at":"2026-11-21T12:00:01.000Z","detail":{"rule":3,"tool":"Bash"},'
    '"device_id":"0190f0f0-0000-7000-8000-000000000006","event_id":"0190f0f0-0000-7000-8000-000000000007",'
    '"org_id":"0190f0f0-0000-7000-8000-000000000002","result":"denied","seq":2,'
    '"session_id":"0190f0f0-0000-7000-8000-000000000005","target_id":"Bash","target_kind":"tool",'
    '"task_id":"0190f0f0-0000-7000-8000-000000000004","wg_id":"0190f0f0-0000-7000-8000-000000000003"}'
)
HASH_2 = "9cf2f5da10c84c6d759de781aec7d87c17c782eeb16cfd4c445fc3b975989958"


def test_hash_text_and_hash_match_the_hand_computed_vectors() -> None:
    assert hash_text(1, ROW_1, None) == f"agent-audit-v1\n1\n{CANONICAL_1}\n-".encode()
    assert audit_hash(1, ROW_1, None) == HASH_1
    assert hash_text(2, ROW_2, HASH_1) == f"agent-audit-v1\n2\n{CANONICAL_2}\n{HASH_1}".encode()
    assert audit_hash(2, ROW_2, HASH_1) == HASH_2


def test_constants() -> None:
    assert chain.AUDIT_LOCK == 742180101
    assert chain.HASH_LABEL == "agent-audit-v1"


def test_row_json_matches_the_audit_row_contract() -> None:
    contracts = Contracts.load()
    rows = [row_json(1, ROW_1), row_json(2, ROW_2)]
    contracts.validate("api/org-audit.response.json", {"rows": rows, "next_before": None})


def test_appended_rows_link_and_verify(writer: psycopg.Connection[Any]) -> None:
    first = append_audit(writer, ROW_1)
    second = append_audit(writer, ROW_2)
    assert (first.seq, first.prev_hash, first.hash) == (1, None, HASH_1)
    assert (second.seq, second.prev_hash, second.hash) == (2, HASH_1, HASH_2)
    assert first.row.at == datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)
    stored = list(read_audit(writer))
    assert stored == [first, second]
    assert verify_chain(writer) == ChainCheck(ok=True, head_seq=2, head_hash=HASH_2, broken_seq=None)


def test_stored_columns(writer: psycopg.Connection[Any]) -> None:
    append_audit(writer, ROW_2)
    row = writer.execute(
        "SELECT at, detail, prev_hash, hash, target_kind, event_id FROM hub.audit WHERE seq = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == datetime(2026, 11, 21, 12, 0, 1, tzinfo=UTC)
    assert row[1] == {"tool": "Bash", "rule": 3}
    assert row[2] is None
    assert row[4:] == ("tool", uid(7))


def test_clock_fixture_times_are_kept_to_the_millisecond(writer: psycopg.Connection[Any], clock: FixedClock) -> None:
    refused = AuditRow(at=clock(), actor_kind="anonymous", action="auth.refused", result="denied", detail=REFUSED)
    stored = append_audit(writer, refused)
    clock.advance(0.0005)
    later = append_audit(writer, replace(refused, at=clock()))
    assert stored.row.at == later.row.at == datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)
    assert verify_chain(writer).ok


def test_a_rolled_back_append_leaves_no_gap(writer: psycopg.Connection[Any]) -> None:
    append_audit(writer, ROW_1)

    def append_then_fail() -> None:
        with writer.transaction():
            append_audit(writer, ROW_2)
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        append_then_fail()
    again = append_audit(writer, ROW_2)
    assert (again.seq, again.hash) == (2, HASH_2)


def test_parallel_writers_get_gapless_seq(migrated_db: HubDatabase) -> None:
    writers, per_writer = 8, 25
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            with migrated_db.connect(migrated_db.writer_role) as connection:
                barrier.wait()
                for n in range(per_writer):
                    with connection.transaction():
                        append_audit(
                            connection,
                            AuditRow(
                                at=AT_1,
                                actor_kind="device",
                                action="event.append",
                                result="ok",
                                detail={"kind": "prompt", "scope": "wg", "scope_seq": n + 1, "ct_bytes": index},
                            ),
                        )
        except BaseException as error:  # collected and re-raised in the test thread
            errors.append(error)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with migrated_db.connect(migrated_db.writer_role) as connection:
        seqs = [row[0] for row in connection.execute("SELECT seq FROM hub.audit ORDER BY seq").fetchall()]
        assert seqs == list(range(1, writers * per_writer + 1))
        check = verify_chain(connection)
    assert check.ok
    assert check.head_seq == writers * per_writer


def _three(writer: psycopg.Connection[Any]) -> list[chain.StoredAudit]:
    append_audit(writer, ROW_1)
    append_audit(writer, ROW_2)
    append_audit(writer, replace(ROW_1, action="read.org"))
    return list(read_audit(writer))


def test_tampering_with_a_copy_is_detected(writer: psycopg.Connection[Any]) -> None:
    rows = _three(writer)
    assert verify_rows(rows, 1, None).ok
    tampered_detail = [rows[0], replace(rows[1], row=replace(rows[1].row, detail={"tool": "Read"})), rows[2]]
    assert verify_rows(tampered_detail, 1, None).broken_seq == 2
    tampered_result = [rows[0], replace(rows[1], row=replace(rows[1].row, result="ok")), rows[2]]
    assert verify_rows(tampered_result, 1, None).broken_seq == 2
    rehashed = replace(rows[1], row=replace(rows[1].row, result="ok"))
    rehashed = replace(rehashed, hash=audit_hash(2, rehashed.row, rehashed.prev_hash))
    assert verify_rows([rows[0], rehashed, rows[2]], 1, None) == ChainCheck(
        ok=False, head_seq=2, head_hash=rehashed.hash, broken_seq=3
    )
    assert verify_rows([rows[0], rows[2]], 1, None).broken_seq == 2
    assert verify_rows([rows[1], rows[0], rows[2]], 1, None).broken_seq == 1
    assert verify_rows([replace(rows[0], prev_hash="0" * 64), *rows[1:]], 1, None).broken_seq == 1
    assert verify_rows(rows[1:], 2, "f" * 64) == ChainCheck(ok=False, head_seq=1, head_hash="f" * 64, broken_seq=2)
    assert verify_rows(rows[1:], 2, rows[0].hash).ok
    assert verify_rows([], 5, "e" * 64) == ChainCheck(ok=True, head_seq=4, head_hash="e" * 64, broken_seq=None)


def test_tampering_in_the_database_is_detected(migrated_db: HubDatabase, writer: psycopg.Connection[Any]) -> None:
    _three(writer)
    with migrated_db.admin() as admin:
        admin.execute("ALTER TABLE hub.audit DISABLE TRIGGER audit_no_change")
        admin.execute('UPDATE hub.audit SET detail = \'{"tool": "Read"}\' WHERE seq = 2')
    assert verify_chain(writer) == ChainCheck(ok=False, head_seq=1, head_hash=HASH_1, broken_seq=2)
    assert verify_chain(writer, 3).ok
    with migrated_db.admin() as admin:
        admin.execute("DELETE FROM hub.audit WHERE seq = 2")
    assert verify_chain(writer, 3) == ChainCheck(ok=False, head_seq=2, head_hash=None, broken_seq=2)
    assert verify_chain(writer, 1, 1).ok
    assert verify_chain(writer).broken_seq == 2


def test_verify_ranges(writer: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    assert verify_chain(writer) == ChainCheck(ok=True, head_seq=0, head_hash=None, broken_seq=None)
    rows = _three(writer)
    monkeypatch.setattr(chain, "READ_BATCH", 2)
    assert [s.seq for s in read_audit(writer)] == [1, 2, 3]
    assert [s.seq for s in read_audit(writer, 2, 2)] == [2]
    assert [s.seq for s in read_audit(writer, 1, 2)] == [1, 2]
    assert verify_chain(writer, 2, 3) == ChainCheck(ok=True, head_seq=3, head_hash=rows[2].hash, broken_seq=None)
    assert verify_chain(writer, 4) == ChainCheck(ok=True, head_seq=3, head_hash=rows[2].hash, broken_seq=None)
    with pytest.raises(ValueError, match="from_seq"):
        verify_chain(writer, 0)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"actor_kind": "robot"}, "actor_kind"),
        ({"action": "Auth.Login"}, "action"),
        ({"action": "a" * 97}, "action"),
        ({"action": "auth.unknown"}, "action"),
        ({"actor_id": UUID("6f1c2c38-3a5e-4b53-9b3a-3d8f7d5f6b01")}, "actor_id"),
        ({"detail": {"title": "x"}}, "detail"),
        ({"detail": {"prompt_text": "x"}}, "detail"),
        ({"detail": {"repo": "x"}}, "detail"),
        ({"action": "auth..login"}, "action"),
        ({"result": "maybe"}, "result"),
        ({"org_id": "0190f0f0-0000-7000-8000-000000000002"}, "org_id"),
        ({"event_id": 7}, "event_id"),
        ({"target_kind": "Tool"}, "target_kind"),
        ({"target_id": ""}, "target_id"),
        ({"target_id": "café"}, "target_id"),
        ({"detail": [("a", 1)]}, "detail"),
        ({"detail": {f"k{n}": n for n in range(33)}}, "detail"),
        ({"detail": {"Key": 1}}, "detail"),
        ({"detail": {"k": 1.5}}, "detail"),
        ({"detail": {"k": 2**53}}, "detail"),
        ({"detail": {"k": "x" * 257}}, "detail"),
        ({"detail": {"k": "café"}}, "detail"),
        ({"detail": {"k": {"nested": 1}}}, "detail"),
        ({"detail": {1: 1}}, "detail"),
        ({"at": datetime(2026, 11, 21, 12, 0)}, "at"),
    ],
)
def test_invalid_rows_are_refused(writer: psycopg.Connection[Any], change: dict[str, Any], field: str) -> None:
    row = replace(ROW_2, **change)
    with pytest.raises(AuditRowInvalid, match=f"^{field}"):
        validate_row(row)
    with pytest.raises(AuditRowInvalid):
        append_audit(writer, row)
    assert writer.execute("SELECT count(*) FROM hub.audit").fetchone() == (0,)


def test_boundary_values_pass_the_generic_checks() -> None:
    """The generic form accepts 32 flat keys, safe integers and 256 printable characters; only the action's detail
    schema refuses them then (its message names the first offending property)."""
    boundary = replace(
        ROW_2,
        action="read.session.get_context",
        target_id="~" * 128,
        detail={**{f"k{n}": -(2**53 - 1) for n in range(31)}, "s": " " * 256},
    )
    with pytest.raises(AuditRowInvalid, match=r"^detail\.result_count of read\.session\.get_context$"):
        validate_row(boundary)
    validate_row(replace(boundary, detail={"result_count": 2**53 - 1}))


@pytest.mark.parametrize(
    ("action", "detail", "field"),
    [
        ("auth.login", {}, "passkey_id"),
        ("auth.login", {"passkey_id": "a:b"}, "passkey_id"),
        ("auth.login", {"passkey_id": "AAAA", "kind": "x"}, "kind"),
        ("hook.denied", {"tool": "Bash", "rule": 7}, "rule"),
        ("hook.denied", {"tool": "Bash", "rule": True}, "rule"),
        ("read.me", {"count": 1}, "count"),
        ("device.enroll", {"kind": "org", "state": "active"}, "device_kind"),
        ("device.enroll", {"device_kind": "org", "state": "decertified"}, "state"),
    ],
)
def test_details_outside_the_schema_of_their_action_are_refused(
    writer: psycopg.Connection[Any], action: str, detail: dict[str, Any], field: str
) -> None:
    row = AuditRow(at=AT_1, actor_kind="hub", action=action, result="ok", detail=detail)
    with pytest.raises(AuditRowInvalid, match=f"^detail\\.{field} of {re.escape(action)}$"):
        append_audit(writer, row)
    assert writer.execute("SELECT count(*) FROM hub.audit").fetchone() == (0,)


def test_actions_equal_the_contract(repo_root: Path) -> None:
    listed = json.loads((repo_root / "contracts/enums/audit-actions.json").read_text())["actions"]
    assert {entry["action"] for entry in listed} == AUDIT_ACTIONS
    assert len(listed) == len(AUDIT_ACTIONS)


def test_append_refuses_a_snapshot_isolation(writer: psycopg.Connection[Any]) -> None:
    def append_in_repeatable_read() -> None:
        with writer.transaction():
            writer.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            append_audit(writer, ROW_1)

    with pytest.raises(AuditIsolationError):
        append_in_repeatable_read()
    assert writer.execute("SELECT count(*) FROM hub.audit").fetchone() == (0,)


def test_rows_that_cannot_be_hashed_are_reported_not_raised(writer: psycopg.Connection[Any]) -> None:
    rows = _three(writer)
    for detail in ({"k": 1.5}, {"k": "café"}, ["not", "an", "object"]):
        broken = replace(rows[1], row=replace(rows[1].row, detail=detail))  # type: ignore[arg-type]
        assert verify_rows([rows[0], broken, rows[2]], 1, None).broken_seq == 2


def test_a_cut_off_tail_does_not_verify(writer: psycopg.Connection[Any]) -> None:
    rows = _three(writer)
    assert verify_chain(writer, 1, 5) == ChainCheck(ok=False, head_seq=3, head_hash=rows[2].hash, broken_seq=4)
    assert verify_chain(writer, 1, 3).ok
