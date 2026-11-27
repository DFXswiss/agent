"""U-RUN-vnc (handler): `vnc.open` (ADR-103) with the VNC double behind the screen and the hub's real relay in
memory - an agent is refused, a malformed body, no screen and a server that does not start fail, and the connection
ends with every reason the final response names: `ended`, `idle`, `closed`, `unpaired`, `timeout`, `broken`,
`unreachable`; the VNC server is stopped on every path and the password never reaches the log."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed

from agent_runner.crypto.encoding import b64u_decode
from agent_runner.vnc import requests as vnc_requests
from agent_runner.vnc.requests import VncOptions, WsSocket, target, vnc_open
from agent_runner.vnc.screens import screens_for
from agent_runner.vnc.tunnel import CLIENT, SocketClosed, Tunnel
from agent_runner.workvm.jobs import ShellResult

from .double import DoubleScreen, VncServer, view_and_click
from .kit import FakeFlow, Wire

LOGIN = "01930060-0000-7000-8000-000000000301"
SESSION = "01930060-0000-7000-8000-000000000302"


def run(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(main())


def opts(wire: Wire | None = None, **values: Any) -> VncOptions:
    connect = (wire or Wire()).connector("device")
    return VncOptions(**{"idle": 5.0, "pair_wait": 5.0, "check": 0.05, "connect": connect, **values})


def final(flow: FakeFlow) -> dict[str, Any]:
    body, done = flow.responses[-1]
    assert done
    return body


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"kind": "login"},
        {"kind": "desktop", "id": LOGIN},
        {"kind": "login", "id": "nope"},
        {"kind": "login", "id": LOGIN, "x": 1},
    ],
)
def test_a_malformed_body_is_upstream_error(body: dict[str, Any]) -> None:
    flow = FakeFlow(body)
    run(lambda: vnc_open(flow))  # type: ignore[arg-type]
    assert flow.responses == [({"step": "failed", "code": "upstream_error"}, True)]
    assert target({"kind": "session", "id": SESSION}) == ("session", SESSION)


def test_an_agent_never_takes_a_screen_over() -> None:
    async def main() -> None:
        flow = FakeFlow({"kind": "login", "id": LOGIN}, requester="alice-agent")
        screen = DoubleScreen(VncServer())
        screens_for(flow.context).offer("login", LOGIN, "alice", screen)
        await vnc_open(flow)  # type: ignore[arg-type]
        assert flow.responses == [({"step": "failed", "code": "forbidden"}, True)]
        assert screen.starts == 0
        assert "runner.vnc refused reason=agent" in flow.log()

    run(main)


def test_no_screen_of_the_owner_is_no_screen_and_a_server_that_does_not_start_fails() -> None:
    async def main() -> None:
        flow = FakeFlow({"kind": "login", "id": LOGIN}, user="bob", requester="bob")
        screens_for(flow.context).offer("login", LOGIN, "alice", DoubleScreen(VncServer()))
        await vnc_open(flow)  # type: ignore[arg-type]
        assert flow.responses == [({"step": "failed", "code": "no_screen"}, True)]
        broken = FakeFlow({"kind": "login", "id": LOGIN})
        screens_for(broken.context).offer("login", LOGIN, "alice", DoubleScreen(VncServer(), fail=True))
        await vnc_open(broken)  # type: ignore[arg-type]
        assert broken.responses == [({"step": "failed", "code": "upstream_error"}, True)]
        assert "runner.vnc start_failed reason=vnc_start_failed" in broken.log()

    run(main)


async def _offered(
    flow: FakeFlow, server: VncServer, client: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
) -> tuple[DoubleScreen, Any]:
    """Offer the double's screen for the flow's login, run the handler and, once it offered the connection, `client`
    with the `vnc` body; the screen and what `client` returned."""
    screen = DoubleScreen(server)
    offer = screens_for(flow.context).offer("login", LOGIN, flow.user, screen)
    handler = asyncio.ensure_future(vnc_open(flow))  # type: ignore[arg-type]
    for _ in range(500):
        if flow.responses:
            break
        await asyncio.sleep(0.01)
    body = flow.responses[0][0]
    assert body["step"] == "vnc"
    assert body["idle_seconds"] >= 1
    result = None if client is None else await client(body)
    offer.ended.set() if client is None else None
    await handler
    return screen, result


def _client(wire: Wire, flow: FakeFlow, act: Callable[[Any, Tunnel], Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    async def client(body: dict[str, Any]) -> Any:
        tunnel = Tunnel(b64u_decode(body["key"], 32), flow.id, CLIENT)
        async with wire.connector(CLIENT)(f"ws://hub.test/v1/vnc/{flow.id}", {}) as socket:
            assert await socket.recv() == '{"type":"paired"}'
            return await act(socket, tunnel)

    return client


async def nobody(body: dict[str, Any]) -> None:
    """A client that never joins the tunnel."""


def _reason(flow: FakeFlow, server: VncServer, client: Any = None) -> str:
    async def main() -> None:
        await server.start()
        try:
            await _offered(flow, server, client)
        finally:
            await server.stop()

    run(main)
    return str(final(flow)["reason"])


def test_the_connection_ends_when_the_check_passes() -> None:
    server, wire = VncServer(), Wire()
    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(wire)

    async def act(socket: Any, tunnel: Tunnel) -> Any:
        version = tunnel.open(await socket.recv())  # the VNC server speaks first
        assert version == b"RFB 003.008\n"
        offer = screens_for(flow.context).find("login", LOGIN, "alice")
        assert offer is not None
        assert offer.viewers == 1
        screens_for(flow.context).withdraw(offer)  # the check passed: the process goes on
        with contextlib.suppress(Exception):
            while True:
                await socket.recv()
        return "left"

    assert _reason(flow, server, _client(wire, flow, act)) == "ended"
    assert [action for action, _ in wire.audits] == ["vnc.connect", "vnc.close"]
    assert flow.responses[0][0]["password"] not in flow.log()
    assert "runner.vnc vnc_connected" in flow.log()
    assert "runner.vnc closed reason=ended" in flow.log()


def test_a_connection_without_input_ends_idle_and_a_viewer_that_leaves_closes_it() -> None:
    server, wire = VncServer(), Wire()
    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(wire, idle=0.2)

    async def silent(socket: Any, tunnel: Tunnel) -> Any:
        with contextlib.suppress(Exception):
            while True:
                await socket.recv()

    assert _reason(flow, server, _client(wire, flow, silent)) == "idle"
    server, wire = VncServer(), Wire()
    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(wire)

    async def leaves(socket: Any, tunnel: Tunnel) -> Any:
        await socket.recv()
        await socket.send(tunnel.seal(b""))  # the viewer closed

    assert _reason(flow, server, _client(wire, flow, leaves)) == "closed"


def test_a_forged_record_breaks_and_a_wrong_pairing_closes() -> None:
    server, wire = VncServer(), Wire()
    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(wire)

    async def forged(socket: Any, tunnel: Tunnel) -> Any:
        await socket.send(b"x" * 40)

    assert _reason(flow, server, _client(wire, flow, forged)) == "broken"

    @contextlib.asynccontextmanager
    async def odd(url: str, headers: Mapping[str, str]) -> AsyncIterator[Any]:
        class Socket:
            async def recv(self) -> str:
                return '{"type":"other"}'

        yield Socket()

    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(connect=odd)
    assert _reason(flow, VncServer(), nobody) == "closed"


def test_without_a_client_the_offer_ends_or_the_pairing_times_out() -> None:
    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts()
    assert _reason(flow, VncServer()) == "ended"  # `_offered` withdraws the offer while nobody joined
    flow = FakeFlow({"kind": "login", "id": LOGIN}, seconds=5.2)
    flow.context.vnc_options = opts()
    server = VncServer()

    async def main() -> None:
        await server.start()
        screen = DoubleScreen(server)
        screens_for(flow.context).offer("login", LOGIN, "alice", screen)
        await vnc_open(flow)  # type: ignore[arg-type]
        await server.stop()
        assert screen.stops == 1

    run(main)
    assert final(flow)["reason"] == "unpaired"


def test_the_requests_time_and_an_unreachable_hub_end_the_connection() -> None:
    server, wire = VncServer(), Wire()
    flow = FakeFlow({"kind": "login", "id": LOGIN}, seconds=6.0)
    flow.context.vnc_options = opts(wire)

    async def waits(socket: Any, tunnel: Tunnel) -> Any:
        with contextlib.suppress(Exception):
            while True:
                await socket.recv()

    assert _reason(flow, server, _client(wire, flow, waits)) == "timeout"

    class Refused:
        async def __aenter__(self) -> Any:
            raise OSError("refused")

        async def __aexit__(self, *error: object) -> None:
            return None

    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(connect=lambda url, headers: Refused())
    assert _reason(flow, VncServer(), nobody) == "unreachable"


def test_the_websockets_closed_signal_becomes_the_tunnels() -> None:
    class Closed:
        async def send(self, message: bytes) -> None:
            raise ConnectionClosed(None, None)

        async def recv(self) -> str:
            raise ConnectionClosed(None, None)

    class Open:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        async def send(self, message: bytes) -> None:
            self.sent.append(message)

        async def recv(self) -> str:
            return "message"

    async def main() -> None:
        with pytest.raises(SocketClosed):
            await WsSocket(Closed()).send(b"x")
        with pytest.raises(SocketClosed):
            await WsSocket(Closed()).recv()
        opened = Open()
        await WsSocket(opened).send(b"x")
        assert opened.sent == [b"x"]
        assert await WsSocket(opened).recv() == "message"

    run(main)

    @contextlib.asynccontextmanager
    async def closing(url: str, headers: Mapping[str, str]) -> AsyncIterator[Any]:
        yield Closed()

    flow = FakeFlow({"kind": "login", "id": LOGIN})
    flow.context.vnc_options = opts(connect=closing)
    assert _reason(flow, VncServer(), nobody) == "closed"  # the hub closed before the pairing


class Browsers:
    """The platform browser of `SESSION` (`running` answers the given VMs in turn, the last one repeated)."""

    def __init__(self, owner: str | None, *found: Any) -> None:
        self.found = list(found)
        self.owner = owner
        self.shell_calls: list[list[str]] = []

        async def shell(argv: Any, **kw: Any) -> ShellResult:
            self.shell_calls.append(list(argv))
            return ShellResult(0, b'{"ok":true}', False)

        self.workvms = SimpleNamespace(deps=SimpleNamespace(shell_for=lambda vm: shell))
        self.db = self

    async def running(self, session_id: str) -> Any:
        assert session_id == SESSION
        return self.found.pop(0) if len(self.found) > 1 else self.found[0]

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        yield None


def _session(monkeypatch: pytest.MonkeyPatch, browsers: Browsers | None, **flow_options: Any) -> FakeFlow:
    async def get(conn: Any, session_id: str) -> Any:
        if browsers is None or browsers.owner is None:
            return None
        return SimpleNamespace(owner_id=browsers.owner)

    monkeypatch.setattr(vnc_requests, "browsers_for", lambda context: browsers)
    monkeypatch.setattr("agent_runner.sessions.records.get", get)
    flow = FakeFlow({"kind": "session", "id": SESSION}, **flow_options)
    flow.context.vnc_options = opts()
    run(lambda: vnc_open(flow))  # type: ignore[arg-type]
    return flow


def test_the_platform_browser_of_a_session_is_offered_to_its_task_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    vm = SimpleNamespace(id="wvm-1")
    assert final(_session(monkeypatch, None))["code"] == "no_screen"  # a device without work VMs
    assert final(_session(monkeypatch, Browsers("alice", None)))["code"] == "no_screen"  # no open browser
    assert final(_session(monkeypatch, Browsers(None, (None, vm))))["code"] == "no_screen"  # an unknown session
    assert final(_session(monkeypatch, Browsers("bob", (None, vm))))["code"] == "no_screen"  # another task owner
    other = SimpleNamespace(id="wvm-2")
    browsers = Browsers("alice", (None, vm), (None, vm), (None, other))
    flow = _session(monkeypatch, browsers)
    assert flow.responses[0][0]["step"] == "vnc"
    assert final(flow) == {"step": "done", "reason": "ended"}  # the browser session lost its VM
    assert browsers.shell_calls == [
        ["sudo", "-n", "/usr/local/bin/workvm-vnc", "start"],
        ["sudo", "-n", "/usr/local/bin/workvm-vnc", "stop"],
    ]
    gone = Browsers("alice", (None, vm), None)
    assert final(_session(monkeypatch, gone)) == {"step": "done", "reason": "ended"}  # the browser session closed


def test_the_viewer_double_authenticates_and_clicks() -> None:
    async def main() -> None:
        clicked: list[tuple[int, int]] = []

        async def on_click(x: int, y: int) -> None:
            clicked.append((x, y))

        server = VncServer(on_click=on_click)
        port = await server.start()
        server.password = "Ab3dEf7h"
        assert await view_and_click(port, "wrong123", 1, 2) == {"refused": True}
        seen = await view_and_click(port, "Ab3dEf7h", 112, 112, key=0x61)
        assert seen == {"width": 1280, "height": 800, "name": "agent-vnc-double"}
        for _ in range(200):
            if clicked:
                break
            await asyncio.sleep(0.01)
        assert clicked == [(112, 112)]
        assert server.keys == [0x61]
        assert server.refused == 1
        await server.stop()

    run(main)
