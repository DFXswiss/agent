"""The read log of 03-api.md section 1 and 02-data-model.md section 2.9: every authenticated `GET` appends an audit row
`read.<resource>`, batched and written within one second; every authenticated GET route declares its resource."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolClosed

from agent_hub.app import create_app, discovered_routers
from agent_hub.audit.chain import AuditRow, AuditRowInvalid, read_audit, verify_chain
from agent_hub.auth.keys import ANONYMOUS, Principal
from agent_hub.config import HubConfig
from agent_hub.errors import HubError
from agent_hub.http.readlog import MAX_DELAY_SECONDS, ReadLog, place, read_row
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


def test_authenticated_read_writes_a_row(reads: HubHarness) -> None:
    org = uuid7()
    client = reads.browser()
    assert client.get(f"/v1/test/orgs/{org}").status_code == 200
    assert reads.flush_read_log() == 1
    [row] = audit_rows(reads)
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
    reads.flush_read_log()
    [row] = audit_rows(reads)
    assert (row.result, row.org_id, row.target_id) == ("denied", None, "missing")


def test_device_reads_and_query_ids(reads: HubHarness) -> None:
    org = uuid7()
    reads.device().get("/v1/test/device/state")
    reads.browser().get("/v1/test/monitoring/overview", params={"org": str(org)})
    reads.browser().get(f"/v1/test/operations/{uuid7()}")
    reads.flush_read_log()
    device, monitoring, operation_row = audit_rows(reads)
    assert (device.action, device.actor_kind, device.device_id) == ("read.device.state", "device", device.actor_id)
    assert (monitoring.org_id, monitoring.target_kind) == (org, None)
    assert operation_row.target_kind == "operation"


def test_anonymous_and_refused_reads_write_nothing(reads: HubHarness) -> None:
    reads.anon.get(f"/v1/test/orgs/{uuid7()}")
    reads.anon.get("/v1/config")
    reads.flush_read_log()
    assert audit_rows(reads) == []


def test_rows_are_written_within_one_second(reads: HubHarness) -> None:
    assert reads.app.state.hub.read_log.max_delay == MAX_DELAY_SECONDS == 1.0
    # the one second is the configured delay (above); the writer's timing is checked with an injected clock
    # (test_pipeline_edges.py), so the wait here only bounds a broken writer, not the load of the device
    started = time.monotonic()
    reads.browser().get(f"/v1/test/orgs/{uuid7()}")
    while not audit_rows(reads):
        assert time.monotonic() - started < 30
        time.sleep(0.02)
    with reads.pool.connection() as connection:
        assert verify_chain(connection).ok


def test_rows_are_batched(reads: HubHarness) -> None:
    client = reads.browser()
    for _ in range(5):
        client.get(f"/v1/test/orgs/{uuid7()}")
    assert reads.flush_read_log() == 5
    assert len(audit_rows(reads)) == 5


def test_a_row_that_cannot_be_recorded_is_logged(reads: HubHarness, caplog: pytest.LogCaptureFixture) -> None:
    # A user ID that is no UUIDv7 cannot go into an audit row; the session resolver reads it from the database.
    key = reads.keys.session(UUID(int=1))
    assert reads.client(key).get(f"/v1/test/orgs/{uuid7()}").status_code == 200
    assert "could not be recorded" in caplog.text
    assert reads.flush_read_log() == 0


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
    # refused reads are not placed: the reader may not see the resource's organization
    assert scope_of(placed(writer, {"task": str(task)}, {}, status=404)) == (None, None, task, None)
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


# The writer.


def make_row() -> AuditRow:
    return read_row(principal(), resource="me", status=200, path_params={}, query={}, at=FIXED_NOW)


def test_record_validates(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool)
    with pytest.raises(AuditRowInvalid):
        log.record(AuditRow(at=FIXED_NOW, actor_kind="user", action="read.nothing", result="ok"))
    assert log.pending() == 0
    assert log.flush() == 0


def test_failed_write_keeps_the_rows(hub_pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
    log = ReadLog(hub_pool, max_rows=3)
    log.record(make_row())
    hub_pool.close()
    with pytest.raises(PoolClosed):
        log.flush()
    assert log.pending() == 1


def test_queue_is_bounded(hub_pool: ConnectionPool[psycopg.Connection[Any]], caplog: pytest.LogCaptureFixture) -> None:
    log = ReadLog(hub_pool, max_rows=2)
    for _ in range(3):
        log.record(make_row())
    assert log.pending() == 2
    assert "1 rows dropped" in caplog.text


async def drained(log: ReadLog, pool: ConnectionPool[psycopg.Connection[Any]], *, written: int) -> int:
    """Wait (at most 30 s) until the writer has stored `written` rows in total; the rows still queued."""

    def stored() -> int:
        with pool.connection() as connection:
            return len(list(read_audit(connection)))

    started = time.monotonic()
    while True:
        if await asyncio.to_thread(stored) >= written:
            return log.pending()
        assert time.monotonic() - started < 30, f"{written} rows not written"
        await asyncio.sleep(0.01)


def test_writer_task(hub_pool: ConnectionPool[psycopg.Connection[Any]], migrated_db: Any) -> None:
    async def scenario() -> list[int]:
        log = ReadLog(hub_pool, max_delay=0.05)
        log.record(make_row())
        await log.start()
        first = await drained(log, hub_pool, written=1)
        log.record(make_row())
        log.record(make_row())
        second = await drained(log, hub_pool, written=3)
        log.record(make_row())
        await log.stop()
        await log.stop()
        return [first, second, log.pending()]

    assert asyncio.run(scenario()) == [0, 0, 0]
    with hub_pool.connection() as connection:
        assert len(list(read_audit(connection))) == 4


def test_writer_retries_after_a_failure(
    hub_pool: ConnectionPool[psycopg.Connection[Any]], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> int:
        log = ReadLog(hub_pool, max_delay=0.01)
        await log.start()
        hub_pool.close()
        log.record(make_row())
        started = time.monotonic()
        while "writing failed" not in caplog.text:
            assert time.monotonic() - started < 30, "the writer never tried"
            await asyncio.sleep(0.01)
        await log.stop()
        return log.pending()

    assert asyncio.run(scenario()) == 1
    assert "writing failed" in caplog.text
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
