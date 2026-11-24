"""U-HUB-audit (ADR-063 section 1): the audit guard of write requests (`agent_hub.http.writelog`) and its place in the
request pipeline - every write that reached its route leaves a committed row, every refusal a row that is not `ok`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.responses import StreamingResponse
from psycopg_pool import ConnectionPool

from agent_hub.audit.chain import AuditRow, append_audit, read_audit
from agent_hub.audit.recorder import Recording, deferred, recording, unchanged
from agent_hub.auth.keys import ANONYMOUS, Principal
from agent_hub.config import HubConfig
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http import pipeline as pipeline_module
from agent_hub.http.context import Context
from agent_hub.http.readlog import ReadLog
from agent_hub.http.routing import HubRouter
from agent_hub.http.writelog import UNAUDITED, error_code, guard_row, settle, write_result
from tests.platform.hub.conftest import FIXED_NOW, FixedClock, HubHarness, start_hub, uuid7

router = HubRouter()


def _task_row(context: Any, result: str = "ok") -> None:
    with transaction(context.pool) as connection:
        append_audit(
            connection,
            AuditRow(at=FIXED_NOW, actor_kind="hub", action="agent.pairing", result=result, target_kind="test"),
        )


@router.post("/v1/test/w/audited", auth="browser", right="user")
def audited(context: Context) -> dict[str, Any]:
    _task_row(context)
    return {}


@router.post("/v1/test/w/silent", auth="browser", right="user")
def silent() -> dict[str, Any]:
    return {}


@router.post("/v1/test/w/unchanged", auth="browser", right="user")
def repeated() -> dict[str, Any]:
    unchanged()
    return {}


@router.post("/v1/test/w/deferred", auth="browser", right="user")
def later() -> dict[str, Any]:
    deferred()
    return {}


@router.post("/v1/test/w/orgs/{org}/rolled-back", auth="browser", right="user")
def rolled_back(org: str, context: Context) -> None:
    with transaction(context.pool) as connection:
        append_audit(connection, AuditRow(at=FIXED_NOW, actor_kind="hub", action="agent.pairing", result="ok"))
        raise HubError("forbidden")


@router.post("/v1/test/w/own-refusal", auth="browser", right="user")
def own_refusal(context: Context) -> None:
    _task_row(context, "denied")
    raise HubError("params_invalid")


@router.post("/v1/test/w/crash", auth="browser", right="user")
def crash() -> None:
    raise RuntimeError("boom")


@router.post("/v1/test/w/anon", auth="anon", right="anon")
def anon_write() -> None:
    raise HubError("request_invalid")


@router.get("/v1/test/w/stream", auth="browser", right="user", read_resource="me", streaming=True)
async def stream() -> StreamingResponse:
    async def parts() -> Any:
        yield b"x"

    return StreamingResponse(parts(), media_type="text/plain")


@router.get("/v1/test/w/stream-gone", auth="browser", right="user", read_resource="me", streaming=True)
async def stream_gone() -> StreamingResponse:
    raise HubError("not_found")


@pytest.fixture
def writes(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", router)])
    UNAUDITED.clear()


def actions(harness: HubHarness) -> list[tuple[str, str, dict[str, Any]]]:
    with harness.pool.connection() as connection:
        return [(item.row.action, item.row.result, dict(item.row.detail)) for item in read_audit(connection)]


def refusal(route: str, status: int, code: str) -> dict[str, Any]:
    return {"method": "POST", "route": route, "status": status, "code": code}


def test_results_and_codes() -> None:
    assert [write_result(status) for status in (200, 302, 400, 401, 403, 404, 409, 500, 503)] == [
        "ok",
        "ok",
        "rejected",
        "denied",
        "denied",
        "denied",
        "rejected",
        "failed",
        "failed",
    ]
    assert error_code(200, b"anything") == "ok"
    assert error_code(403, b'{"error":"forbidden"}') == "forbidden"
    assert error_code(400, b'{"error":"no_such_code"}') == "unknown"
    assert error_code(400, b"[1]") == "unknown"
    assert error_code(400, b"not json") == "unknown"


def test_guard_rows_name_the_request_never_its_values() -> None:
    org, agent = uuid7(), uuid7()
    device = Principal(kind="device", key_id="device." + "1" * 32, principal_id=uuid7(), actor_kind="device")
    found = guard_row(
        device,
        method="PUT",
        route="/v1/orgs/{org}/things/{id}",
        status=403,
        code="forbidden",
        path_params={"org": str(org), "id": "x"},
        query={"task": "not-an-id"},
        at=FIXED_NOW,
    )
    assert (found.action, found.result, found.org_id, found.task_id) == ("request.refused", "denied", org, None)
    assert (found.device_id, found.target_kind, found.target_id) == (device.principal_id, "id", "x")
    assert dict(found.detail) == {
        "method": "PUT",
        "route": "/v1/orgs/{org}/things/{id}",
        "status": 403,
        "code": "forbidden",
    }
    acting = Principal(
        kind="agent",
        key_id="agent." + "2" * 32,
        principal_id=uuid7(),
        actor_kind="user",
        attributes={"agent_id": agent},
    )
    ok = guard_row(acting, method="POST", route="/v1/x", status=200, code="ok", path_params={}, query={}, at=FIXED_NOW)
    assert (ok.action, ok.agent_id, ok.device_id, ok.target_kind) == ("request.write", agent, None, None)
    nameless = guard_row(
        ANONYMOUS, method="POST", route="/v1/x", status=200, code="ok", path_params={}, query={}, at=FIXED_NOW
    )
    assert (nameless.actor_kind, nameless.agent_id) == ("anonymous", None)


def test_settle(hub_pool: ConnectionPool[psycopg.Connection[Any]], caplog: pytest.LogCaptureFixture) -> None:
    log = ReadLog(hub_pool)
    user = Principal(kind="session", key_id="session." + "3" * 32, principal_id=uuid7(), actor_kind="user")

    def row(route: str, status: int) -> AuditRow:
        code = "ok" if status < 400 else "forbidden"
        return guard_row(
            user, method="POST", route=route, status=status, code=code, path_params={}, query={}, at=FIXED_NOW
        )

    with hub_pool.connection() as connection, recording() as own:
        append_audit(connection, AuditRow(at=FIXED_NOW, actor_kind="hub", action="agent.pairing", result="ok"))
    UNAUDITED.clear()
    # a success with its own row, or with a deferred one, needs nothing
    assert settle(hub_pool, log, own, row("/v1/a", 200), status=200, query="") is False
    assert settle(hub_pool, log, Recording(deferred=True), row("/v1/a", 200), status=200, query="") is False
    # a refusal whose own row is `ok` gets the refusal row
    assert settle(hub_pool, log, own, row("/v1/a", 403), status=403, query="") is True
    # an unchanged repeat and a generic route get the generic row without a defect
    assert settle(hub_pool, log, Recording(unchanged=True), row("/v1/b", 200), status=200, query="") is True
    assert settle(hub_pool, log, Recording(), row("/v1/passkeys/login/begin", 200), status=200, query="") is True
    assert not UNAUDITED
    # anything else is a defect: logged, counted, still recorded
    assert settle(hub_pool, log, Recording(), row("/v1/c", 200), status=200, query="") is True
    assert UNAUDITED == {"POST /v1/c": 1}
    assert "left no audit row of its own" in caplog.text
    UNAUDITED.clear()
    with hub_pool.connection() as connection:
        written = [(item.row.action, item.row.detail["route"]) for item in read_audit(connection) if item.seq > 1]
    assert written == [
        ("request.refused", "/v1/a"),
        ("request.write", "/v1/b"),
        ("request.write", "/v1/passkeys/login/begin"),
        ("request.write", "/v1/c"),
    ]


def test_routes_with_their_own_rows_get_none(writes: HubHarness) -> None:
    client = writes.browser()
    assert client.post("/v1/test/w/audited").status_code == 200
    assert client.post("/v1/test/w/deferred").status_code == 200
    assert [action for action, _result, _detail in actions(writes)] == ["agent.pairing"]


def test_a_silent_success_is_recorded_and_counted(writes: HubHarness) -> None:
    client = writes.browser()
    assert client.post("/v1/test/w/silent").status_code == 200
    assert client.post("/v1/test/w/unchanged").status_code == 200
    assert UNAUDITED == {"POST /v1/test/w/silent": 1}
    assert actions(writes) == [
        ("request.write", "ok", {"method": "POST", "route": "/v1/test/w/silent", "status": 200, "code": "ok"}),
        ("request.write", "ok", {"method": "POST", "route": "/v1/test/w/unchanged", "status": 200, "code": "ok"}),
    ]


def test_a_refusal_survives_the_rollback_of_its_route(writes: HubHarness) -> None:
    org = uuid7()
    client = writes.browser()
    assert client.post(f"/v1/test/w/orgs/{org}/rolled-back").json() == {"error": "forbidden"}
    [(action, result, detail)] = actions(writes)
    assert (action, result, detail) == (
        "request.refused",
        "denied",
        refusal("/v1/test/w/orgs/{org}/rolled-back", 403, "forbidden"),
    )
    with writes.pool.connection() as connection:
        [stored] = list(read_audit(connection))
    # the actor is no member of the organization it named: the row names none (placement of refusals)
    assert (stored.row.org_id, stored.row.target_kind, stored.row.target_id) == (None, "org", str(org))


def test_a_route_that_records_its_own_refusal_gets_no_second(writes: HubHarness) -> None:
    assert writes.browser().post("/v1/test/w/own-refusal").status_code == 400
    assert [(action, result) for action, result, _detail in actions(writes)] == [("agent.pairing", "denied")]


def test_a_crashing_write_is_recorded_as_failed(writes: HubHarness, caplog: pytest.LogCaptureFixture) -> None:
    assert writes.browser().post("/v1/test/w/crash").json() == {"error": "unavailable"}
    assert actions(writes) == [("request.refused", "failed", refusal("/v1/test/w/crash", 503, "unavailable"))]
    assert "the route failed" in caplog.text


def test_refusals_after_the_principal_is_known_are_settled(writes: HubHarness) -> None:
    assert writes.device().post("/v1/test/w/silent").json() == {"error": "forbidden"}
    assert writes.browser().post("/v1/test/w/audited", params={"x": "1"}).json() == {"error": "request_invalid"}
    assert writes.anon.post("/v1/test/w/anon").status_code == 400
    found = actions(writes)
    assert found == [
        ("request.refused", "denied", refusal("/v1/test/w/silent", 403, "forbidden")),
        ("request.refused", "rejected", refusal("/v1/test/w/audited", 400, "request_invalid")),
        ("request.refused", "rejected", refusal("/v1/test/w/anon", 400, "request_invalid")),
    ]


def test_refusals_before_a_principal_and_rate_limits_are_not_settled(writes: HubHarness) -> None:
    assert writes.anon.post("/v1/test/w/silent").json() == {"error": "signature_missing"}
    statuses = [writes.anon.post("/v1/test/w/anon").status_code for _ in range(31)]
    assert statuses[-1] == 429
    assert [row for row in actions(writes) if row[2]["status"] in (401, 429)] == []


def test_a_settle_failure_is_logged_and_the_answer_goes_out(
    writes: HubHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("database gone")

    monkeypatch.setattr(pipeline_module, "settle", broken)
    assert writes.browser().post("/v1/test/w/silent").status_code == 200
    assert "could not be settled" in caplog.text


def test_a_streaming_read_that_cannot_be_recorded_is_not_answered(
    writes: HubHarness, caplog: pytest.LogCaptureFixture
) -> None:
    key = writes.keys.session(UUID(int=1))  # no UUIDv7: its read cannot go into a row
    # an accepted stream records its own reads (the live channel does, review M3): the pipeline writes nothing
    ok = writes.client(key).get("/v1/test/w/stream", extensions={"streaming": True})
    assert (ok.status_code, ok.text) == (200, "x")
    # a refused stream is recorded by the pipeline; a refusal that cannot be recorded is not answered
    refused = writes.client(key).get("/v1/test/w/stream-gone", extensions={"streaming": True})
    assert (refused.status_code, refused.json()) == (503, {"error": "unavailable"})
    assert "could not be recorded" in caplog.text
