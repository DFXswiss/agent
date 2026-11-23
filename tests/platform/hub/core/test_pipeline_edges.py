"""Edge cases of the request pipeline at the ASGI level: disconnects, chunked bodies, streamed responses to signed
requests, malformed targets and failures after a response started."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from agent_hub.auth.gate import request_target
from agent_hub.auth.keys import Principal
from agent_hub.config import HubConfig
from agent_hub.errors import HubError
from agent_hub.http.headers import SecurityHeaders
from agent_hub.http.readlog import ReadLog, read_row
from agent_hub.http.routing import HubRouter
from tests.platform.hub.conftest import FIXED_NOW, FixedClock, HubHarness, start_hub, uuid7

router = HubRouter()


@router.post("/v1/test/echo", auth="anon", right="anon")
async def echo(body: dict[str, Any]) -> dict[str, Any]:
    return body


@router.get("/v1/test/chunks", auth="anon", right="anon")
async def chunks() -> StreamingResponse:
    async def parts() -> Any:
        for part in (b"one,", b"two,", b"three"):
            yield part

    return StreamingResponse(parts(), media_type="text/plain")


@pytest.fixture
def edges(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", router)])


def call(app: Any, scope: Scope, messages: list[Message]) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def http_scope(path: str, method: str = "POST", headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", b"localhost:8788")],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8788),
        "state": {},
    }


def test_client_gone_while_the_body_is_read(edges: HubHarness) -> None:
    sent = call(edges.app, http_scope("/v1/test/echo"), [{"type": "http.request", "body": b"{", "more_body": True}])
    assert sent == []


def test_body_in_several_messages(edges: HubHarness) -> None:
    messages: list[Message] = [
        {"type": "http.request", "body": b'{"a":', "more_body": True},
        {"type": "http.request", "body": b"1}", "more_body": False},
    ]
    headers = [(b"host", b"localhost:8788"), (b"content-type", b"application/json")]
    sent = call(edges.app, http_scope("/v1/test/echo", headers=headers), messages)
    assert sent[0]["status"] == 200
    assert b"".join(message.get("body", b"") for message in sent[1:]) == b'{"a":1}'


def test_a_streamed_response_to_a_signed_request_is_buffered_and_signed(edges: HubHarness) -> None:
    response = edges.root.get("/v1/test/chunks")
    assert response.text == "one,two,three"
    assert response.extensions["hub_signature"] is True


def test_a_failed_signature_outside_the_api_does_not_count(edges: HubHarness) -> None:
    impostor = edges.keys.session()
    headers = {
        "Agent-Key": impostor.key_id,
        "Agent-Time": "2026-11-21T12:00:00Z",
        "Agent-Nonce": "AAAAAAAAAAAAAAAAAAAAAA",
        "Agent-Signature": "A" * 86,
    }
    for _ in range(31):
        assert edges.base.get("/assets/app.js", headers=headers).status_code == 401
    assert edges.anon.get("/v1/test/chunks").status_code == 200


@pytest.mark.parametrize("raw_path", [b"/v1/\xff", b"v1/x", b"/v1/a b"])
def test_malformed_targets(raw_path: bytes) -> None:
    with pytest.raises(HubError, match="signature_missing"):
        request_target({"raw_path": raw_path, "path": "/", "query_string": b""})
    with pytest.raises(HubError, match="signature_missing"):
        request_target({"raw_path": b"/v1/x", "path": "/", "query_string": b"a=\xff"})
    assert request_target({"path": "/v1/x", "query_string": b"a=1"}) == "/v1/x?a=1"


def test_security_headers_after_a_late_failure() -> None:
    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late")

    sent = call(SecurityHeaders(app), http_scope("/v1/x", "GET"), [])
    assert [message["type"] for message in sent] == ["http.response.start"]
    assert (b"x-content-type-options", b"nosniff") in sent[0]["headers"]


def test_security_headers_pass_other_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    call(SecurityHeaders(app), {"type": "lifespan"}, [])
    assert seen == ["lifespan"]


def test_writer_writes_rows_older_than_the_delay_at_once(
    hub_pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    principal = edges_principal()
    row = read_row(principal, resource="me", status=200, path_params={}, query={}, at=FIXED_NOW)

    # the writer's clock is injected: the row is older than the delay of one minute, so it is written at once; with a
    # wait for the delay the row would still be pending after the 30-second bound (a real-time bound of milliseconds
    # would depend on the load of the device)
    now = [0.0]

    async def scenario() -> float:
        log = ReadLog(hub_pool, max_delay=60.0, monotonic=lambda: now[0])
        log.record(row)
        now[0] = 61.0
        started = time.monotonic()
        await log.start()
        while log.pending() and time.monotonic() - started < 30:
            await asyncio.sleep(0.005)
        elapsed = time.monotonic() - started
        await log.stop()
        return elapsed

    assert asyncio.run(scenario()) < 30


def test_writer_waits_the_rest_of_the_delay_for_a_new_row(
    hub_pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    # the wait is the delay minus the age of the oldest queued row (injected clock); a written batch resets it
    now = [100.0]
    log = ReadLog(hub_pool, max_delay=1.0, monotonic=lambda: now[0])
    assert log._delay() == 0.0
    log.record(read_row(edges_principal(), resource="me", status=200, path_params={}, query={}, at=FIXED_NOW))
    assert log._delay() == 1.0
    now[0] = 100.25
    log.record(read_row(edges_principal(), resource="me", status=200, path_params={}, query={}, at=FIXED_NOW))
    assert log._delay() == 0.75
    assert log.flush() == 2
    assert log._delay() == 0.0


def edges_principal() -> Principal:
    return Principal(kind="session", key_id="session." + "1" * 32, principal_id=uuid7(), actor_kind="user")
