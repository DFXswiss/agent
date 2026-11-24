"""The read log of 03-api.md section 1, 02-data-model.md section 2.9 and ADR-063 section 3: every authenticated `GET`
appends an audit row `read.<resource>` before its answer leaves, repeats of a key are counted in `audit.repeat`; every
authenticated GET route declares its resource."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolClosed

from agent_hub.app import create_app, discovered_routers
from agent_hub.audit.chain import AuditRow, AuditRowInvalid, StoredAudit, append_audit, read_audit, verify_chain
from agent_hub.audit.placement import place
from agent_hub.auth.keys import ANONYMOUS, Principal
from agent_hub.config import HubConfig
from agent_hub.errors import HubError
from agent_hub.http import readlog
from agent_hub.http.readlog import ReadLog, ReadNotStored, _Window, query_key, read_key, read_row
from agent_hub.http.routing import HubRouter, route_spec
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import FIXED_NOW, FixedClock, HubHarness, start_hub, uuid7

router = HubRouter()


@router.get("/v1/test/orgs/{org}", auth="browser", right="org:member", read_resource="org")
async def get_org(org: str) -> dict[str, Any]:
    if org == "missing":
        raise HubError("not_found")
    return {}


@router.get("/v1/test/monitoring/overview", auth="browser", right="org:member", read_resource="monitoring.overview")
async def overview(org: str | None = None) -> dict[str, Any]:
    return {}


@router.get("/v1/test/operations/{id}", auth="browser", right="task:read", read_resource="operation")
async def operation(id: str) -> dict[str, Any]:
    return {}


@router.get("/v1/test/tasks/{task}/operations", auth="browser", right="org:member", read_resource="task.operations")
async def task_operations(task: str) -> dict[str, Any]:
    return {}


@router.get("/v1/test/device/state", auth="device", right="device:any", read_resource="device.state")
async def device_state() -> dict[str, Any]:
    return {}


@pytest.fixture
def reads(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", router)])


def audit_rows(harness: HubHarness) -> list[AuditRow]:
    with harness.pool.connection() as connection:
        return [stored.row for stored in read_audit(connection)]


def test_authenticated_read_writes_a_row_before_the_answer(reads: HubHarness) -> None:
    org = uuid7()
    client = reads.browser()
    assert client.get(f"/v1/test/orgs/{org}").status_code == 200
    [row] = audit_rows(reads)  # no flush: the first read of a key is written before its answer leaves
    key = client.key
    assert key is not None
    assert row.action == "read.org"
    assert row.result == "ok"
    assert row.actor_kind == "user"
    assert row.actor_id == key.principal.principal_id
    assert row.org_id == org
    assert (row.target_kind, row.target_id) == ("org", str(org))
    assert row.at == FIXED_NOW
    assert dict(row.detail) == {}


def test_denied_reads_are_logged(reads: HubHarness) -> None:
    reads.browser().get("/v1/test/orgs/missing")
    [row] = audit_rows(reads)
    assert (row.result, row.org_id, row.target_id) == ("denied", None, "missing")


def test_device_reads_and_query_ids(reads: HubHarness) -> None:
    org = uuid7()
    reads.device().get("/v1/test/device/state")
    reads.browser().get("/v1/test/monitoring/overview", params={"org": str(org)})
    reads.browser().get(f"/v1/test/operations/{uuid7()}")
    device, monitoring, operation_row = audit_rows(reads)
    assert (device.action, device.actor_kind, device.device_id) == ("read.device.state", "device", device.actor_id)
    assert (monitoring.org_id, monitoring.target_kind) == (org, None)
    assert operation_row.target_kind == "operation"


def test_anonymous_and_refused_reads_write_nothing(reads: HubHarness) -> None:
    reads.anon.get(f"/v1/test/orgs/{uuid7()}")
    reads.anon.get("/v1/config")
    reads.flush_read_log()
    assert audit_rows(reads) == []


def test_repeats_in_one_window_are_counted(reads: HubHarness) -> None:
    client = reads.browser()
    org = uuid7()
    for _ in range(4):
        assert client.get(f"/v1/test/orgs/{org}").status_code == 200
    # another query is another read (paging, filters): its own row
    assert client.get("/v1/test/monitoring/overview", params={"org": str(org)}).status_code == 200
    assert client.get("/v1/test/monitoring/overview", params={"org": str(uuid7())}).status_code == 200
    assert [row.action for row in audit_rows(reads)] == [
        "read.org",
        "read.monitoring.overview",
        "read.monitoring.overview",
    ]
    assert reads.flush_read_log() == 1
    first, *_rest, repeat = list_stored(reads)
    assert repeat.row.action == "audit.repeat"
    assert dict(repeat.row.detail) == {"action": "read.org", "repeats": 3, "first_seq": first.seq, "window_s": 300}
    assert (repeat.row.actor_id, repeat.row.org_id, repeat.row.target_id) == (
        first.row.actor_id,
        first.row.org_id,
        first.row.target_id,
    )
    with reads.pool.connection() as connection:
        assert verify_chain(connection).ok


def test_a_read_that_cannot_be_recorded_is_not_answered(reads: HubHarness, caplog: pytest.LogCaptureFixture) -> None:
    # A user ID that is no UUIDv7 cannot go into an audit row; the session resolver reads it from the database.
    key = reads.keys.session(UUID(int=1))
    response = reads.client(key).get(f"/v1/test/orgs/{uuid7()}")
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})
    assert "could not be recorded" in caplog.text
    assert audit_rows(reads) == []


def list_stored(harness: HubHarness) -> list[StoredAudit]:
    with harness.pool.connection() as connection:
        return list(read_audit(connection))


def principal() -> Principal:
    return Principal(kind="session", key_id="session." + "0" * 32, principal_id=uuid7(), actor_kind="user")


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (200, "ok"),
        (304, "rejected"),
        (401, "denied"),
        (403, "denied"),
        (404, "denied"),
        (409, "rejected"),
        (503, "failed"),
    ],
)
def test_read_row_results(status: int, result: str) -> None:
    row = read_row(principal(), resource="org", status=status, path_params={}, query={}, at=FIXED_NOW)
    assert row.result == result


def test_read_row_ids_and_targets() -> None:
    org, wg, task, session = uuid7(), uuid7(), uuid7(), uuid7()
    row = read_row(
        principal(),
        resource="session",
        status=200,
        path_params={"session": str(session)},
        query={"org": str(org), "wg": str(wg), "task": str(task).upper()},
        at=FIXED_NOW,
    )
    assert (row.org_id, row.wg_id, row.task_id, row.session_id) == (org, wg, None, session)
    assert (row.target_kind, row.target_id) == ("session", str(session))
    v4 = "8a6f3d5e-0d1f-4c7e-9a1b-2b3c4d5e6f70"
    weird = read_row(
        ANONYMOUS, resource="me.credential", status=200, path_params={"kind": "aä"}, query={"org": v4}, at=FIXED_NOW
    )
    assert (weird.org_id, weird.target_kind, weird.target_id) == (None, None, None)
    by_id = read_row(
        principal(), resource="device_request", status=200, path_params={"id": "x"}, query={}, at=FIXED_NOW
    )
    assert by_id.target_kind == "device_request"
    nested = read_row(
        principal(), resource="device.request", status=200, path_params={"id": "x"}, query={}, at=FIXED_NOW
    )
    assert nested.target_kind == "device_request"


# Placement of successful reads that name only a task, session, operation, workgroup or scope.


def seed_scope(connection: psycopg.Connection[Any]) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """An org with a workgroup, a task, a session and an operation (only the columns placement reads matter)."""
    org, wg, task, session, operation = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    connection.execute(
        "INSERT INTO hub.org (id, private, default_wg, created_by, created_at, log_head_seq, log_head_hash)"
        " VALUES (%s, true, %s, gen_random_uuid(), now(), 0, 'h')",
        (org, wg),
    )
    connection.execute("INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, true, 1)", (wg, org))
    connection.execute(
        "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
        " VALUES (%s, %s, %s, gen_random_uuid(), 'working', now(), now(), now())",
        (task, org, wg),
    )
    connection.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, depth, state, created_at)"
        " VALUES (%s, %s, %s, %s, gen_random_uuid(), 0, 'requested', now())",
        (session, task, org, wg),
    )
    connection.execute(
        "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, gen_random_uuid(), gen_random_uuid(), 'task.set_title',"
        " 'succeeded', now())",
        (operation, org, wg, task, session),
    )
    return org, wg, task, session, operation


def placed(
    connection: psycopg.Connection[Any],
    path_params: dict[str, str],
    query: dict[str, str],
    *,
    resource: str = "x",
    status: int = 200,
) -> AuditRow:
    row = read_row(principal(), resource=resource, status=status, path_params=path_params, query=query, at=FIXED_NOW)
    return place(connection, row)


def scope_of(row: AuditRow) -> tuple[UUID | None, UUID | None, UUID | None, UUID | None]:
    return row.org_id, row.wg_id, row.task_id, row.session_id


def test_place_takes_the_scope_from_the_hub_rows(writer: psycopg.Connection[Any]) -> None:
    org, wg, task, session, operation = seed_scope(writer)
    assert scope_of(placed(writer, {"session": str(session)}, {})) == (org, wg, task, session)
    assert scope_of(placed(writer, {"task": str(task)}, {})) == (org, wg, task, None)
    assert scope_of(placed(writer, {}, {"wg": str(wg)})) == (org, wg, None, None)
    # `{id}` routes of sessions and operations, and event scopes (a workgroup or the organization itself)
    assert scope_of(placed(writer, {"id": str(session)}, {}, resource="session")) == (org, wg, task, session)
    assert scope_of(placed(writer, {"id": str(operation)}, {}, resource="operation")) == (org, wg, task, session)
    assert scope_of(placed(writer, {"scope": str(wg)}, {"task": str(task)})) == (org, wg, task, None)
    assert scope_of(placed(writer, {"scope": str(wg)}, {})) == (org, wg, None, None)
    assert scope_of(placed(writer, {"scope": str(org)}, {})) == (org, None, None, None)
    # a value the request named that agrees with the hub's row is kept
    assert scope_of(placed(writer, {"org": str(org)}, {"task": str(task)})) == (org, wg, task, None)


def test_place_leaves_refused_contradicting_and_unknown_reads(writer: psycopg.Connection[Any]) -> None:
    org, wg, task, session, _ = seed_scope(writer)
    other_org, *_ = seed_scope(writer)
    # a refused read of a stranger keeps none of the IDs it named (ADR-063 section 1)
    assert scope_of(placed(writer, {"task": str(task)}, {}, status=404)) == (None, None, None, None)
    # an audit filter of one organization naming another organization's session stays as named
    contradiction = placed(writer, {"org": str(other_org)}, {"session": str(session)})
    assert scope_of(contradiction) == (other_org, None, None, session)
    # the most specific ID decides: an unknown session leaves the row as named, whatever the scope
    stray = uuid7()
    assert scope_of(placed(writer, {"scope": str(wg)}, {"session": str(stray)})) == (None, None, None, stray)
    unknown = uuid7()
    for path_params in ({"task": str(unknown)}, {"scope": str(unknown)}, {"scope": "not-a-uuid"}, {"kind": "x"}, {}):
        row = placed(writer, path_params, {})
        assert (row.org_id, row.wg_id) == (None, None)
    assert scope_of(placed(writer, {"id": str(unknown)}, {}, resource="device_request"))[0] is None
    assert placed(writer, {"task": str(task)}, {}).org_id == org


def test_a_task_read_lands_in_the_org_audit(reads: HubHarness) -> None:
    with reads.pool.connection() as connection:
        org, wg, task, _, _ = seed_scope(connection)
    assert reads.browser().get(f"/v1/test/tasks/{task}/operations").status_code == 200
    reads.flush_read_log()
    [row] = audit_rows(reads)
    assert (row.action, row.org_id, row.wg_id, row.task_id) == ("read.task.operations", org, wg, task)


# The windowed writer (ADR-063 section 3).


def make_row(actor: Principal | None = None, resource: str = "me") -> AuditRow:
    return read_row(actor or principal(), resource=resource, status=200, path_params={}, query={}, at=FIXED_NOW)


def stored(pool: ConnectionPool[psycopg.Connection[Any]]) -> list[StoredAudit]:
    with pool.connection() as connection:
        return list(read_audit(connection))


def test_admit_validates(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool)
    with pytest.raises(AuditRowInvalid):
        log.admit(AuditRow(at=FIXED_NOW, actor_kind="user", action="read.nothing", result="ok"))
    assert log.pending() == 0
    assert log.flush() == 0


def test_query_key() -> None:
    assert query_key(b"") == ""
    assert query_key(b"a=1&b=2") == query_key(b"b=2&a=1") != query_key(b"a=1&b=3")
    assert len(query_key(b"limit=10")) == 32


def test_first_row_repeats_and_windows(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    now = [0.0]
    log = ReadLog(hub_pool, window=10.0, monotonic=lambda: now[0], clock=lambda: FIXED_NOW)
    actor = principal()
    row = make_row(actor)
    log.admit(row)
    log.admit(row)
    log.admit(row, "q")  # another query: another key
    assert [item.row.action for item in stored(hub_pool)] == ["read.me", "read.me"]
    assert log.pending() == 1
    # nothing expired yet: a sweep writes nothing
    assert log.flush(all_windows=False) == 0
    now[0] = 10.0
    log.admit(row)  # the window ended: this read opens the next one, the ended window waits for its row
    assert log.pending() == 1
    assert log.flush(all_windows=False) == 1
    rows = stored(hub_pool)
    assert [item.row.action for item in rows] == ["read.me", "read.me", "read.me", "audit.repeat"]
    assert dict(rows[3].row.detail) == {"action": "read.me", "repeats": 1, "first_seq": rows[0].seq, "window_s": 10}
    # windows without repeats are ended silently
    now[0] = 30.0
    assert log.flush(all_windows=False) == 0
    assert log.pending() == 0


def test_the_oldest_windows_are_ended_beyond_max_keys(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    now = [0.0]
    log = ReadLog(hub_pool, max_keys=2, monotonic=lambda: now[0])
    first = make_row()
    log.admit(first)
    log.admit(first)
    for resource in ("me.devices", "me.agents"):
        now[0] += 1
        log.admit(make_row(resource=resource))
    # the oldest window (with its repeat) was ended early; nothing is dropped
    assert log.flush(all_windows=False) == 1
    assert [item.row.action for item in stored(hub_pool)][-1] == "audit.repeat"


def test_a_failed_first_write_records_nothing(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool)
    hub_pool.close()
    with pytest.raises(PoolClosed):
        log.admit(make_row())
    assert log.pending() == 0


def test_a_repeat_waits_for_the_first_row(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool, first_wait=0.01)
    row = make_row()
    key = read_key(row)
    pending = _Window(opened=time.monotonic(), first=row)
    log._open[key] = pending
    # the first row of the key is not stored (yet): the repeat is not recorded either
    with pytest.raises(ReadNotStored):
        log.admit(row)
    pending.failed = True
    pending.stored.set()
    with pytest.raises(ReadNotStored):
        log.admit(row)
    assert pending.repeats == 0


def test_a_window_ended_during_its_first_write(
    hub_pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ReadLog(hub_pool)
    row = make_row()
    key = read_key(row)

    def ended_then_failed(_connection: Any, _row: AuditRow) -> None:
        # more keys than `max_keys` end the window while its first row is written; a repeat is counted meanwhile
        window = log._open[key]
        window.repeats = 1
        with log._lock:
            log._end(key, window)
        # not settled yet: the ended window waits
        assert log.flush() == 0
        assert log.pending() == 1
        raise RuntimeError("database gone")

    monkeypatch.setattr(readlog, "append_audit", ended_then_failed)
    with pytest.raises(RuntimeError):
        log.admit(row)
    # the first row failed: nothing to repeat, nothing written, nothing left
    assert log.flush() == 0
    assert log.pending() == 0


def test_failed_repeat_rows_wait_for_the_next_flush(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool)
    row = make_row()
    log.admit(row)
    log.admit(row)
    hub_pool.close()
    with pytest.raises(PoolClosed):
        log.flush()
    assert log.pending() == 1


def test_sweeper_writes_expired_windows(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    now = [0.0]

    async def scenario() -> int:
        log = ReadLog(hub_pool, window=10.0, sweep=0.01, monotonic=lambda: now[0])
        row = make_row()
        await asyncio.to_thread(log.admit, row)
        await asyncio.to_thread(log.admit, row)
        await log.start()
        await asyncio.sleep(0.05)
        assert log.pending() == 1  # the window is still open
        now[0] = 11.0
        started = time.monotonic()
        while log.pending():
            assert time.monotonic() - started < 30, "the sweeper never wrote"
            await asyncio.sleep(0.01)
        await log.stop()
        await log.stop()
        return len(stored(hub_pool))

    assert asyncio.run(scenario()) == 2


def test_stop_waits_for_the_write_of_the_sweeper(
    hub_pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweeper has taken the expired window and is still writing it when `stop` comes: `stop` returns only after
    that write (before HOT-15 it returned at once, and a write failing afterwards lost the repeats)."""
    now = [0.0]
    writing = threading.Event()
    release = threading.Event()

    def slow_repeat(connection: psycopg.Connection[Any], row: AuditRow) -> StoredAudit:
        if row.action == readlog.REPEAT_ACTION:
            writing.set()
            assert release.wait(30)
        return append_audit(connection, row)

    monkeypatch.setattr(readlog, "append_audit", slow_repeat)

    async def scenario() -> int:
        log = ReadLog(hub_pool, window=10.0, sweep=0.01, monotonic=lambda: now[0])
        row = make_row()
        await asyncio.to_thread(log.admit, row)
        await asyncio.to_thread(log.admit, row)
        await log.start()
        now[0] = 11.0
        assert await asyncio.to_thread(writing.wait, 30)
        assert log.pending() == 0  # taken by the sweeper, not yet written
        stopping = asyncio.ensure_future(log.stop())
        try:
            await asyncio.sleep(0.2)
            assert not stopping.done()  # stop waits for the write
        finally:
            release.set()
            await stopping
        return len(stored(hub_pool))

    assert asyncio.run(scenario()) == 2


def test_sweeper_and_stop_survive_failures(
    hub_pool: ConnectionPool[psycopg.Connection[Any]], caplog: pytest.LogCaptureFixture
) -> None:
    now = [0.0]

    async def scenario() -> int:
        log = ReadLog(hub_pool, window=10.0, sweep=0.01, monotonic=lambda: now[0])
        row = make_row()
        await asyncio.to_thread(log.admit, row)
        await asyncio.to_thread(log.admit, row)
        hub_pool.close()
        now[0] = 11.0
        await log.start()
        started = time.monotonic()
        while "writing the repeats failed" not in caplog.text:
            assert time.monotonic() - started < 30, "the sweeper never tried"
            await asyncio.sleep(0.01)
        await log.stop()
        return log.pending()

    assert asyncio.run(scenario()) == 1
    assert "final write failed" in caplog.text


# Completeness: every authenticated GET route declares its read resource (ADR-027 section 4).


def test_every_registered_get_route_declares_its_resource(hub_config: HubConfig) -> None:
    app = create_app(hub_config, run_jobs=False)
    checked = 0
    for route in app.router.routes:
        spec = route_spec(route)
        assert spec is not None
        methods: set[str] = getattr(route, "methods", None) or set()
        if "GET" in methods and str(getattr(route, "path", "")).startswith("/v1/") and spec.auth != "anon":
            assert spec.streaming or spec.read_resource is not None, getattr(route, "path", "")
            checked += 1
    assert checked >= 0


def test_registered_api_routes_match_the_index(hub_config: HubConfig) -> None:
    """Every discovered `/v1/` route has the authentication class, rights, read resource and streaming flag of its
    row in contracts/api/INDEX.json (later packages are checked by this test as they add routes)."""
    index = json.loads((CONTRACTS / "api" / "INDEX.json").read_text(encoding="utf-8"))
    rows = {(row["method"], row["path"]): row for row in index["endpoints"]}
    found = 0
    for _area, area_router in discovered_routers():
        for route in area_router.routes:
            spec = route_spec(route)
            path = str(getattr(route, "path", ""))
            for method in getattr(route, "methods", None) or {"GET"}:
                row = rows.get((method, path))
                assert row is not None, f"{method} {path} is not in contracts/api/INDEX.json"
                assert spec is not None
                assert spec.auth == row["auth"], path
                assert list(spec.rights) == row["rights"], path
                assert spec.read_resource == row["read_resource"], path
                assert spec.streaming == row["streaming"], path
                found += 1
    assert found >= 2
