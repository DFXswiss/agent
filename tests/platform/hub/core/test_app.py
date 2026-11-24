"""The app factory (ADR-027 section 4): discovered routers only, every route declared, lifespan, WebSocket gate."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import APIRouter, WebSocket
from psycopg_pool import ConnectionPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_core.signing import b64url_encode, format_time, request_text, sha256_hex
from agent_hub.app import collect_routes, create_app
from agent_hub.config import HubConfig
from agent_hub.http.context import CurrentPrincipal
from agent_hub.http.routing import HubRouter
from agent_hub.jobs import Job
from agent_hub.registry import RegistryError
from tests.platform.hub.conftest import FixedClock, HubHarness, SigningKey, start_hub

ws_router = HubRouter()


@ws_router.hub_websocket("/v1/test/ws", auth="device", right="device")
async def device_socket(websocket: WebSocket, principal: CurrentPrincipal) -> None:
    await websocket.accept()
    await websocket.send_json({"kind": principal.kind})
    await websocket.close()


@ws_router.hub_websocket("/v1/test/ws-anon", auth="anon", right="anon")
async def anon_socket(websocket: WebSocket, principal: CurrentPrincipal) -> None:
    await websocket.accept()
    await websocket.send_json({"kind": principal.kind})
    await websocket.close()


@pytest.fixture
def sockets(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", ws_router)])


def test_routers_must_be_hub_routers(hub_config: HubConfig) -> None:
    with pytest.raises(RegistryError, match="must be a HubRouter"):
        create_app(hub_config, routers=[("bad", APIRouter())])


def test_undeclared_routes_are_refused() -> None:
    router = HubRouter()

    @router.api_route("/v1/undeclared", methods=["GET"])
    async def undeclared() -> None: ...

    with pytest.raises(RegistryError, match="has no hub declaration"):
        collect_routes([("bad", router)])


def test_duplicate_routes_are_refused() -> None:
    first, second = HubRouter(), HubRouter()
    for router in (first, second):

        @router.get("/v1/same", auth="anon", right="anon")
        async def same() -> None: ...

    with pytest.raises(RegistryError, match="GET /v1/same is registered twice"):
        collect_routes([("a", first), ("b", second)])
    third, fourth = HubRouter(), HubRouter()

    @third.get("/v1/items/{a}", auth="anon", right="anon")
    async def by_a(a: str) -> None: ...

    @fourth.get("/v1/items/{b}", auth="anon", right="anon")
    async def by_b(b: str) -> None: ...

    with pytest.raises(RegistryError, match="registered twice"):
        collect_routes([("c", third), ("d", fourth)])


def test_static_routes_come_last(hub_config: HubConfig) -> None:
    app = create_app(hub_config, run_jobs=False)
    paths = [getattr(route, "path", "") for route in app.router.routes]
    assert paths[-1] == "/{rest:path}"
    assert paths.index("/v1/health") < paths.index("/assets/{name:path}")
    assert app.openapi_url is None


def test_lifespan_opens_and_closes_its_own_pool(hub_config: HubConfig, clock: FixedClock) -> None:
    ran: list[str] = []
    job = Job("test.mark", 3600, lambda _context: ran.append("ran"))
    app = create_app(hub_config, clock=clock, jobs=[job])
    with TestClient(app, base_url=hub_config.public.origin) as client:
        assert client.get("/v1/health").status_code == 200
        pool = app.state.hub.pool
        deadline = time.monotonic() + 30
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
    assert ran == ["ran"]
    assert pool.closed


def test_a_failing_at_start_job_stops_the_start_and_closes_the_own_pool(
    hub_config: HubConfig, clock: FixedClock, hub_pool: ConnectionPool[psycopg.Connection[Any]]
) -> None:
    def broken(context: Any) -> None:
        opened.append(context.pool)
        raise RuntimeError("broken")

    opened: list[Any] = []
    job = Job("test.broken", 3600, broken, at_start=True)
    app = create_app(hub_config, clock=clock, jobs=[job], run_jobs=False)
    with pytest.raises(RuntimeError, match="failed at start"), TestClient(app, base_url=hub_config.public.origin):
        pass
    assert opened[0].closed
    app = create_app(hub_config, pool=hub_pool, clock=clock, jobs=[job], run_jobs=False)
    with pytest.raises(RuntimeError, match="failed at start"), TestClient(app, base_url=hub_config.public.origin):
        pass
    assert opened[1] is hub_pool
    assert not hub_pool.closed


def test_websocket_gate(sockets: HubHarness) -> None:
    key = sockets.keys.device()
    with sockets.base.websocket_connect("/v1/test/ws", headers=signed_upgrade(sockets, key, "/v1/test/ws")) as socket:
        assert socket.receive_json() == {"kind": "device"}


def signed_upgrade(harness: HubHarness, key: SigningKey, target: str) -> dict[str, str]:
    agent_time = format_time(int(harness.clock().timestamp()))
    nonce = b64url_encode(b"\x07" * 16)
    text = request_text(key.key_id, "GET", "localhost:8788", target, agent_time, nonce, sha256_hex(b""))
    return {
        "Agent-Key": key.key_id,
        "Agent-Time": agent_time,
        "Agent-Nonce": nonce,
        "Agent-Signature": b64url_encode(key.sign(text)),
    }


def test_websocket_nonce_replay(sockets: HubHarness) -> None:
    key = sockets.keys.device()
    headers = signed_upgrade(sockets, key, "/v1/test/ws")
    with sockets.base.websocket_connect("/v1/test/ws", headers=headers) as socket:
        socket.receive_json()
    with pytest.raises(WebSocketDisconnect) as closed, sockets.base.websocket_connect("/v1/test/ws", headers=headers):
        pass
    assert closed.value.code == 1008


@pytest.mark.parametrize("case", ["unsigned", "browser", "unknown_route"])
def test_websocket_refusals(sockets: HubHarness, case: str) -> None:
    path = "/v1/test/none" if case == "unknown_route" else "/v1/test/ws"
    headers = {} if case == "unsigned" else signed_upgrade(sockets, sockets.keys.session(), path)
    with pytest.raises(WebSocketDisconnect) as closed, sockets.base.websocket_connect(path, headers=headers):
        pass
    assert closed.value.code == 1008


def test_anonymous_websocket(sockets: HubHarness) -> None:
    with sockets.base.websocket_connect("/v1/test/ws-anon") as socket:
        assert socket.receive_json() == {"kind": "anonymous"}


def test_lifespan_scope_passes_through(hub_config: HubConfig) -> None:
    app = create_app(hub_config, run_jobs=False)

    async def lifespan() -> list[str]:
        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent: list[str] = []

        async def receive() -> dict[str, str]:
            return messages.pop(0)

        async def send(message: Any) -> None:
            sent.append(message["type"])

        await app({"type": "lifespan", "state": {}}, receive, send)
        return sent

    assert asyncio.run(lifespan()) == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
