"""U-RUN-ws: signed upgrade, reconnect order reconcile -> push -> pull, heartbeat providers, hint dispatch, backoff
1 s -> 30 s with reset after 30 s stable, no redirects (03-api.md section 6.2), against the fake hub's real
loopback WebSocket server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from agent_runner.sync import outbox
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.push import SyncFatal
from agent_runner.sync.ws import EARLY_BEAT_S, HEARTBEAT_S, WsClient

from .conftest import Harness, run
from .test_outbox import fixture_body

PROBE_CONNECT = """
from agent_runner.sync.extensions import ConnectHook

async def reconcile(context):
    context.order.append("reconcile")

async def pull(context):
    pushed = [r for r in context.hub.requests if r.target == "/v1/device/outbox"]
    context.order.append(f"pull after {len(pushed)} pushes")

async def broken(context):
    raise RuntimeError("secret detail")

HOOKS = [ConnectHook("reconcile", "probe-reconcile", reconcile), ConnectHook("pull", "probe-pull", pull),
         ConnectHook("pull", "probe-broken", broken)]
"""

PROBE_HINTS = """
from agent_runner.sync.hints import hello

async def events(context, hint):
    context.order.append(("events", hint["scope_seq"]))

def wraps(context, hint):
    context.order.append(("wraps",))

async def reconcile(context, hint):
    raise RuntimeError("secret detail")

HANDLERS = {"events": events, "wraps": wraps, "reconcile": reconcile, "hello": hello}
"""

PROBE_HEARTBEAT = """
from agent_runner.sync.extensions import HeartbeatSection

async def vms(context):
    return [{"vm_id": "vm-0000abcd", "state": "running"}]

async def sessions(context):
    raise RuntimeError("secret detail")

SECTIONS = [HeartbeatSection("vms", vms), HeartbeatSection("sessions", sessions)]
"""

SCOPE = "0190f000-0000-7000-8000-000000000001"


async def until(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def probe_package(area_package: Callable[[dict[str, str]], str]) -> str:
    return area_package(
        {"probe/connect.py": PROBE_CONNECT, "probe/hints.py": PROBE_HINTS, "probe/heartbeat.py": PROBE_HEARTBEAT}
    )


def test_session_order_heartbeat_and_hint_dispatch(
    harness: Harness, area_package: Callable[[dict[str, str]], str]
) -> None:
    package = probe_package(area_package)

    async def body() -> None:
        async with harness.env(package=package) as env:
            context: Any = env.context
            context.order, context.hub = [], env.hub
            env.clock.real[HEARTBEAT_S] = 0.05
            await env.enqueue("metrics", fixture_body("metrics"))
            before = env.sync.ws.connected
            task = asyncio.ensure_future(env.sync.ws.run(context.stopping))
            await until(lambda: len(context.order) == 2 and len(env.hub.frames) >= 1)
            during = env.sync.ws.connected
            assert context.order == ["reconcile", "pull after 1 pushes"]
            assert sorted(env.hub.stored) == [1]
            frame = env.hub.frames[0]
            assert frame["vms"] == [{"vm_id": "vm-0000abcd", "state": "running"}]
            assert frame["sessions"] == []
            assert frame["capacity"]["vm_slots_free"] == 0
            for hint in (
                '{"type":"events","scope_id":"' + SCOPE + '","scope_seq":4}',
                "not json",
                '{"type":"bogus"}',
                '{"type":"reconcile"}',
                '{"type":"wraps"}',
                '{"type":"hello","hub_time":"2020-01-01T00:00:00.000Z"}',
                '{"type":"events","scope_id":"' + SCOPE + '","scope_seq":5}',
            ):
                env.hub.to_send.put_nowait(hint)
            await until(lambda: len(context.order) == 5)
            assert context.order[2:] == [("events", 4), ("wraps",), ("events", 5)]
            context.stopping.set()
            await asyncio.wait_for(task, 10)
            assert (before, during, env.sync.ws.connected) == (False, True, False)
            log = env.log()
            assert "runner.sync ws_connected count=1\n" in log
            assert env.hub.ws_features == ["log.admit,log.org_assign,vm.choice"]
            assert "runner.sync hint_invalid\n" in log
            assert "runner.sync hint_unknown type=bogus\n" in log
            assert "runner.sync hint_failed type=reconcile error=RuntimeError\n" in log
            assert "runner.sync clock_skew seconds=" in log
            assert "connect_hook_failed name=probe-broken error=RuntimeError" in log
            assert "heartbeat_section_failed field=sessions error=RuntimeError" in log
            assert "secret detail" not in log
            async with env.pool.connection() as conn:
                assert await outbox.get_state(conn, outbox.WS_LAST_CONNECT) is not None

    run(body)


def test_beat_soon_sends_the_next_heartbeat_at_once(harness: Harness) -> None:
    """A capacity change (VM area) does not wait for the 15 s period: the hub's estimate uses `pool_ready`."""

    async def body() -> None:
        async with harness.env() as env:
            env.clock.real[HEARTBEAT_S] = 60.0
            ws = env.sync.ws
            ws.beat_soon()  # without a socket: nothing to send, the first frame follows the connect
            task = asyncio.ensure_future(ws.run(env.context.stopping))
            await until(lambda: len(env.hub.frames) == 1)
            await asyncio.sleep(0.1)
            assert len(env.hub.frames) == 1
            ws.beat_soon()
            ws.beat_soon()
            await until(lambda: len(env.hub.frames) == 2)
            await asyncio.sleep(0.1)
            assert len(env.hub.frames) == 2  # coalesced into one frame
            assert env.clock.sleeps.count(EARLY_BEAT_S) == 1  # an early frame waits the minimum spacing first
            env.context.stopping.set()
            await asyncio.wait_for(task, 10)

    run(body)


def test_backoff_doubles_to_the_cap_with_jitter(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env(options={"random": lambda: 1.0}) as env:
            env.hub.refuse_ws = 7
            env.clock.on_sleep = lambda seconds: env.context.stopping.set() if seconds == HEARTBEAT_S else None
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            first_beat = env.clock.sleeps.index(HEARTBEAT_S)
            assert env.clock.sleeps[:first_beat] == pytest.approx([1.2, 2.4, 4.8, 9.6, 19.2, 36.0, 36.0])
            assert env.hub.connections == 1
            assert env.log().count("ws_failed error=InvalidStatus") == 7

    run(body)


def test_backoff_resets_after_30_s_stable(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env(options={"random": lambda: 0.0}) as env:
            env.hub.refuse_ws = 2
            beats = []

            def on_sleep(seconds: float) -> None:
                if seconds == HEARTBEAT_S:
                    beats.append(seconds)
                    if len(beats) == 2:
                        env.hub.to_send.put_nowait(None)  # the hub closes after 30 s
                elif beats:
                    env.context.stopping.set()

            env.clock.on_sleep = on_sleep
            env.clock.real[HEARTBEAT_S] = 0.05
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            failures = [s for s in env.clock.sleeps if s != HEARTBEAT_S]
            assert failures == pytest.approx([0.8, 1.6, 0.8])
            assert "runner.sync ws_closed\n" in env.log()

    run(body)


class ClosingSocket:
    """A socket the hub has closed normally just as the runner sends: `send` raises `ConnectionClosedOK`, the receive
    side has not ended yet (the order the race produces on a loaded device)."""

    def __aiter__(self) -> ClosingSocket:
        return self

    async def __anext__(self) -> str:
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        raise ConnectionClosedOK(Close(1000, "bye"), Close(1000, "bye"), True)


def test_a_normal_close_seen_while_sending_is_a_close(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:

            @asynccontextmanager
            async def connect(url: str, headers: Mapping[str, str]) -> AsyncIterator[ClosingSocket]:
                yield ClosingSocket()

            env.sync.ws.connect = connect
            env.clock.on_sleep = lambda seconds: env.context.stopping.set()
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            assert "runner.sync ws_closed\n" in env.log()
            assert "ws_failed" not in env.log()

    run(body)


def test_redirects_are_not_followed(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.hub.redirect_ws = 1
            env.clock.on_sleep = lambda seconds: env.context.stopping.set() if seconds == HEARTBEAT_S else None
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            assert env.clock.sleeps[:2] == [1.0, HEARTBEAT_S]
            assert "ws_failed error=InvalidStatus" in env.log()
            assert env.hub.connections == 1

    run(body)


def test_upgrade_with_a_signature_the_hub_refuses(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.hub.wall = lambda: env.wall.now + 3600
            env.clock.on_sleep = lambda _s: env.context.stopping.set()
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            assert env.hub.connections == 0
            assert "ws_failed error=InvalidStatus" in env.log()

    run(body)


def test_unreachable_hub_socket(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await env.hub.stop_ws()
            env.clock.on_sleep = lambda _s: env.context.stopping.set()
            await asyncio.wait_for(env.sync.ws.run(env.context.stopping), 20)
            assert "ws_failed error=ConnectionRefusedError" in env.log()
            # a stopped client does not connect at all
            await env.sync.ws.run(env.context.stopping)

    run(body)


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (SyncFatal("origin_seq_conflict"), "connect_push_stopped code=origin_seq_conflict"),
        (HubRefusal(403, "forbidden"), "connect_push_refused error=forbidden"),
        (HubTransportError("hub_unreachable"), "connect_push_failed reason=hub_unreachable"),
    ],
)
def test_push_failures_on_connect_do_not_end_the_session(harness: Harness, error: Exception, line: str) -> None:
    async def body() -> None:
        async with harness.env() as env:
            ws: WsClient = env.sync.ws

            async def failing() -> Any:
                raise error

            ws.pusher.drain = failing  # type: ignore[method-assign]
            await ws.on_connect()
            assert line in env.log()

    run(body)


def test_default_heartbeat_frame(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            frame = await env.sync.ws.heartbeat_frame()
            env.hub.check("ws/device/heartbeat.json", frame)
            assert frame["sessions"] == []
            assert frame["vms"] == []

    run(body)
