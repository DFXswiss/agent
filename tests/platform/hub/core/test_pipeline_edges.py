"""Edge cases of the request pipeline at the ASGI level: disconnects, chunked bodies, streamed responses to signed
requests, malformed targets and failures after a response started."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg_pool import ConnectionPool
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from agent_hub.auth.gate import request_target
from agent_hub.config import HubConfig
from agent_hub.errors import HubError
from agent_hub.http.headers import SecurityHeaders
from agent_hub.http.routing import HubRouter
from tests.platform.hub.conftest import FixedClock, HubHarness, start_hub

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
