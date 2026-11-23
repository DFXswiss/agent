"""U-RUN-operations: inbound data of the pipeline - the `operation.request` journal handler, the verified
`settings.workgroup` and `operation.result` events (05-runner.md sections 2.4 and 12 steps 2-3; 03-api.md sections
7.2, 9, 10; ADR-039)."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.operations import journal, records, results, settings
from agent_runner.operations.settings import DEFAULTS, UNREADABLE, SettingsInvalid, WorkgroupSettings
from agent_runner.operations.stored import open_stored
from agent_runner.sessions.journal import JournalRecordInvalid

from ..reconcile.rig import Rig, seal
from ..sessions.kit import ORG, TASK, WG, session_id
from .kit import REQUESTED_AT, OKit, pull, run, setup

REPO = "example-org/example-repo"


async def count(env: Any) -> int:
    rows = await env.rows("SELECT count(*) FROM runner.operation")
    return int(rows[0][0])


def body_of(**changes: Any) -> dict[str, Any]:
    found: dict[str, Any] = {
        "pending_id": uuid7(),
        "operation_id": uuid7(),
        "type": "task.set_title",
        "params": {"title": "x"},
        "requested_at": REQUESTED_AT,
    }
    found.update(changes)
    return {k: v for k, v in found.items() if v is not None}


# -- journal handler ---------------------------------------------------------------------------------------------


def test_a_request_is_recorded_once_and_notified_after_the_commit(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        async with await psycopg.AsyncConnection.connect(env.context.dsn, autocommit=True) as listener:
            await listener.execute(f"LISTEN {journal.CHANNEL}")
            request = body_of(idempotency_key="k1")
            await kit_.journal(sid, request)
            notes = [n async for n in listener.notifies(timeout=30.0, stop_after=1)]
            assert [n.channel for n in notes] == [journal.CHANNEL]
            # the same record again (a re-read page): nothing new, no notification
            kit_.seq -= 1
            await kit_.journal(sid, request)
            assert [n async for n in listener.notifies(timeout=0.2, stop_after=1)] == []
        assert await count(env) == 1
        op = await kit_.op(request["operation_id"])
        assert (op.state, op.session_id, op.task_id, op.wg_id, op.org_id) == ("received", sid, TASK, WG, ORG)
        assert (op.pending_id, op.idempotency_key, op.local_error) == (request["pending_id"], "k1", None)
        assert op.vm_id == (await kit_.vm()).id
        assert op.requested_at == REQUESTED_AT

    run(rig, body)


def test_requests_of_sessions_not_on_this_vm_are_ignored(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        await kit_.session()
        await kit_.journal(session_id(7), body_of())
        await kit_.journal(None, body_of())
        assert await count(env) == 0
        assert sum("foreign_session" in line for line in kit_.lines) == 2

    run(rig, body)


@pytest.mark.parametrize(
    "changes",
    [
        {"pending_id": "x"},
        {"operation_id": None},
        {"type": ""},
        {"type": "t" * 129},
        {"params": []},
        {"requested_at": 5},
        {"requested_at": "yesterday"},
        {"idempotency_key": ""},
        {"idempotency_key": 3},
        {"local_check": "maybe"},
        {"extra": 1},
    ],
)
def test_malformed_requests_are_skipped(rig: Rig, changes: dict[str, Any]) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        await kit_.journal(sid, body_of(**changes))
        assert await count(env) == 0
        assert any("record_skipped" in line and "JournalRecordInvalid" in line for line in kit_.lines)

    run(rig, body)


def test_local_rejections_keep_a_well_formed_error() -> None:
    assert journal.local_error({}) is None
    assert journal.local_error({"local_check": "passed"}) is None
    assert journal.local_error({"local_check": "rejected"}) == {"code": "params_invalid"}
    assert journal.local_error({"local_check": "rejected", "local_error": {"code": "Bad Code"}}) == {
        "code": "params_invalid"
    }
    good = {"code": "params_invalid", "pointers": ["/params/repo"], "message": "dropped"}
    assert journal.local_error({"local_check": "rejected", "local_error": good}) == {
        "code": "params_invalid",
        "pointers": ["/params/repo"],
    }
    for pointers in ([], ["no-slash"], [1], "x", ["/" + "a" * 300]):
        error = {"code": "unknown_type", "pointers": pointers}
        assert journal.local_error({"local_check": "rejected", "local_error": error}) == {"code": "unknown_type"}
    with pytest.raises(JournalRecordInvalid):
        journal.local_error({"local_check": True})


# -- settings ------------------------------------------------------------------------------------------------------


def test_settings_parse_only_the_contract_shape() -> None:
    found = settings.parse({"repositories": [REPO], "protected_branches": ["main", "hot/*"], "pr_draft_default": False})
    assert found == WorkgroupSettings(frozenset({REPO}), ("main", "hot/*"), False)
    assert found.allows("Example-Org/Example-Repo")
    assert not found.allows("other/repo")
    # KOR-30: without a list every repository is allowed; unreadable stored settings allow none (fail closed)
    assert DEFAULTS.allows(REPO)
    assert not DEFAULTS.restricted
    assert UNREADABLE.restricted
    assert not UNREADABLE.allows(REPO)
    for bad in (
        {},
        {"repositories": [REPO], "protected_branches": [], "pr_draft_default": "yes"},
        {"repositories": ["Upper/Case"], "protected_branches": [], "pr_draft_default": True},
        {"repositories": REPO, "protected_branches": [], "pr_draft_default": True},
        {"repositories": [REPO] * 501, "protected_branches": [], "pr_draft_default": True},
        {"repositories": [], "protected_branches": ["a b"], "pr_draft_default": True},
        {"repositories": [], "protected_branches": [], "pr_draft_default": True, "x": 1},
    ):
        with pytest.raises(SettingsInvalid):
            settings.parse(bad)


def test_the_latest_verified_settings_event_wins(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        async with env.pool.connection() as conn:
            assert await settings.load(conn, env.sync.storage_key, WG) == DEFAULTS
        await kit_.settings([REPO], ["main"], draft=False)
        await kit_.settings(["a/b", REPO], ["trunk"])
        async with env.pool.connection() as conn:
            found = await settings.load(conn, env.sync.storage_key, WG)
            assert found == WorkgroupSettings(frozenset({"a/b", REPO}), ("trunk",), True)
            # an older event never replaces a later one
            rows = await env.rows("SELECT event_id::text, author_sig, sealed FROM runner.workgroup_settings")
            event_id, sig, sealed = rows[0]
            stored = await settings.keep(
                conn, org_id=ORG, wg_id=WG, event_id=event_id, scope_seq=1, author_sig=sig, sealed=sealed
            )
            assert stored is False
        # the stored event is ciphertext, never the repository names
        assert REPO not in str(rows)

    run(rig, body)


def test_invalid_settings_events_are_refused_and_audited(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        org = env.world.orgs[ORG]
        bad = {"repositories": ["x"], "protected_branches": [], "pr_draft_default": True}
        env.world.add_event(WG, 1, seal(org, "alice", "settings.workgroup", label="bad", body=bad))
        await pull(env)
        assert await env.rows("SELECT * FROM runner.workgroup_settings") == []
        (audit,) = await env.audits()
        assert audit["detail"] == {"source": "setting", "code": "settings_invalid"}
        assert kit_.pipeline is not None

    run(rig, body)


def test_stored_settings_that_no_longer_open_or_parse_allow_no_repository(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        await kit_.settings([REPO])
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.workgroup_settings SET author_sig = %s", ("A" * 43,))
            assert await settings.load(conn, env.sync.storage_key, WG) == UNREADABLE
        # a stored body of the wrong shape (only possible by tampering with the store)
        org = env.world.orgs[ORG]
        sealed = seal(org, "alice", "settings.workgroup", label="odd", body={"repositories": "x"})
        from agent_runner.crypto.log import vectors as v  # noqa: PLC0415

        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.workgroup_settings")
            await settings.keep(
                conn, org_id=ORG, wg_id=WG, event_id=uuid7(), scope_seq=9, author_sig=v.sig_pk("alice"), sealed=sealed
            )
            assert await settings.load(conn, env.sync.storage_key, WG) == UNREADABLE

    run(rig, body)


async def _unopenable() -> None:
    # no header epoch: refused before any store access
    assert await open_stored(None, b"k", WG, {"header": {"epoch": "1"}}, "sig") is None  # type: ignore[arg-type]
    assert await open_stored(None, b"k", WG, "not an event", "sig") is None  # type: ignore[arg-type]


def test_open_stored_without_the_epoch_key(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await setup(env)
        org = env.world.orgs[ORG]
        sealed = seal(org, "alice", "settings.workgroup", label="e9", epoch=9, key=bytes(32), body={})
        async with env.pool.connection() as conn:
            assert await open_stored(conn, env.sync.storage_key, WG, sealed, "x") is None

    run(rig, body)


# -- operation results -----------------------------------------------------------------------------------------


def result_event(kit_: OKit, seq: int, label: str, body: dict[str, Any], ref: str) -> None:
    org = kit_.env.world.orgs[ORG]
    kit_.env.world.add_event(WG, seq, seal(org, "rdev", "operation.result", label=label, body=body, ref=ref))


def test_verified_result_events_are_kept_once_per_operation(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        op = uuid7()
        result_event(kit_, 1, "r1", {"operation_id": op, "status": "succeeded", "result": {"a": 1}}, op)
        result_event(kit_, 2, "r2", {"operation_id": op, "status": "succeeded", "result": {"a": 2}}, op)
        await pull(env)
        async with env.pool.connection() as conn:
            found = await results.original_outcome(conn, env.sync.storage_key, op, wg_id=WG, op_type="github.comment")
        assert found == results.Outcome("succeeded", result={"a": 1})
        assert len(await env.rows("SELECT * FROM runner.operation_result")) == 1

    run(rig, body)


@pytest.mark.parametrize(
    ("result_body", "ref_same"),
    [
        ({"status": "succeeded", "result": {}}, False),  # ref names another operation
        ({"status": "succeeded"}, True),
        ({"status": "succeeded", "result": [], "x": 1}, True),
        ({"status": "failed", "error": {"code": "Bad"}}, True),
        ({"status": "failed", "error": {"code": "timeout"}, "result": {}}, True),
        ({"status": "pending"}, True),
    ],
)
def test_invalid_result_events_are_refused_and_audited(rig: Rig, result_body: dict[str, Any], ref_same: bool) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        op = uuid7()
        result_event(kit_, 1, "bad", {"operation_id": op, **result_body}, op if ref_same else uuid7())
        await pull(env)
        assert await env.rows("SELECT * FROM runner.operation_result") == []
        (audit,) = await env.audits()
        assert audit["detail"] == {"source": "event", "code": "result_invalid"}

    run(rig, body)


def test_result_bodies_and_outcomes() -> None:
    op = "01930009-0000-7000-8000-000000000001"
    assert results.valid({"ref": op}, {"operation_id": op, "status": "expired", "error": {"code": "timeout"}})
    assert not results.valid({"ref": "x"}, {"operation_id": "x", "status": "failed", "error": {"code": "timeout"}})
    assert not results.valid({}, {"operation_id": op, "status": "failed", "error": {"code": "timeout"}})
    assert results.outcome_of({"operation_id": op, "status": "denied", "error": {"code": "rights_denied"}}) == (
        results.Outcome("denied", error={"code": "rights_denied"})
    )
    assert results.outcome_of({"operation_id": op, "status": "failed", "error": "timeout"}) is None


def test_original_outcomes_from_local_rows_and_stored_events(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        key = env.sync.storage_key
        scope: dict[str, Any] = {"wg_id": WG, "op_type": "task.set_title"}
        async with env.pool.connection() as conn:
            assert await results.original_outcome(conn, key, op_id, **scope) is None  # received, nothing stored
            # another type or another workgroup named by the hub: never used
            assert await results.original_outcome(conn, key, op_id, wg_id=WG, op_type="github.comment") is None
            await records.update(conn, op_id, state="failed", error_code="timeout", retryable=True)
            assert await results.original_outcome(conn, key, op_id, **scope) == results.Outcome(
                "failed", error={"code": "timeout", "retryable": True}
            )
            await records.update(conn, op_id, state="succeeded", result={"x": 1})
            assert await results.original_outcome(conn, key, op_id, **scope) == results.Outcome(
                "succeeded", result={"x": 1}
            )
            await records.update(conn, op_id, state="executing")
        # an executing local row falls back to a stored result event (another device finished it)
        result_event(kit_, 1, "r9", {"operation_id": op_id, "status": "succeeded", "result": {"y": 2}}, op_id)
        await pull(env)
        async with env.pool.connection() as conn:
            assert await results.original_outcome(conn, key, op_id, **scope) == results.Outcome(
                "succeeded", result={"y": 2}
            )
            await conn.execute("UPDATE runner.operation_result SET author_sig = %s", ("A" * 43,))
            assert await results.original_outcome(conn, key, op_id, **scope) is None

    run(rig, body)


def test_records_refuse_unknown_columns_and_states(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await setup(env)
        async with env.pool.connection() as conn:
            choices: list[dict[str, object]] = [{"params": {}}, {}, {"state": "lost"}]
            for fields in choices:
                with pytest.raises(ValueError, match=r"not updatable|unknown operation state"):
                    await records.update(conn, uuid7(), **fields)
            assert await records.get(conn, uuid7()) is None

    run(rig, body)


def test_pending_work_lists_open_and_unanswered_operations(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        received = await kit_.request(sid, "task.set_title", {"title": "a"})
        answered = await kit_.request(sid, "task.set_title", {"title": "b"})
        old = await kit_.request(sid, "task.set_title", {"title": "c"})
        duplicate = await kit_.request(sid, "task.set_title", {"title": "d"})
        async with env.pool.connection() as conn:
            await records.update(conn, answered, state="succeeded", answer={"x": 1}, answered_at=kit_.pipeline.now())
            await records.update(conn, old, state="succeeded", answer={"x": 1})
            await records.update(conn, duplicate, state="duplicate")
        await kit_.age(old, 3600)
        async with env.pool.connection() as conn:
            assert await records.pending_work(conn, 900) == [received, duplicate]

    run(rig, body)


def test_task_commits_and_the_github_connection(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "github.commit", {})
        async with env.pool.connection() as conn:
            assert await records.task_commits(conn, TASK) == set()
            await records.update(conn, op_id, state="succeeded", result={"commit_sha": "f" * 40})
            assert await records.task_commits(conn, TASK) == {"f" * 40}
            assert not await records.github_connected(conn, kit_.kit.session_env.device_id)
        await kit_.connect()
        async with env.pool.connection() as conn:
            from ..sessions.kit import OWNER  # noqa: PLC0415

            assert await records.github_connected(conn, OWNER)

    run(rig, body)


def test_open_stored_refuses_events_it_cannot_open() -> None:
    asyncio.run(_unopenable())
