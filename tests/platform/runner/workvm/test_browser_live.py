"""The live picture of the platform browser, runner side (docs/platform/spec/13-work-vm.md section 13.6): the hint
starts one stream per session, frames are sealed under the workgroup key and paced by a byte budget (the newest frame
that did not fit is kept), an empty post keeps the lease, `409 not_watched`, a closed browser session, a broken helper
and the shutdown end it. Real PostgreSQL, fake Lima, a fake screencast process and a fake hub."""

from __future__ import annotations

import asyncio
import json
import stat
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.crypto.browserframe import open_frame
from agent_runner.crypto.errors import CryptoError
from agent_runner.github import snapshot
from agent_runner.operations.extensions import OperationContext
from agent_runner.store.migrate import migrate
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.workvm import browser_helper, browser_live, hints, service, shutdown
from agent_runner.workvm import browser_records as rows
from agent_runner.workvm.browser import Browsers
from agent_runner.workvm.browser_live import Link, LiveStreams, spawn_process
from agent_runner.workvm.manager import WorkVms
from agent_runner.workvm.shell import SlotCommand, VmShell

from .kit import SESSION, WG, World, session

KEY = bytes(range(32))
OP = "0193000a-0000-7000-8000-0000000000f1"


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


class FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """A screencast: frames are fed into `stdout`; `ended` when killed."""

    def __init__(self, *, stdin: bool = True, lingers: bool = False) -> None:
        self.stdout = asyncio.StreamReader(limit=browser_helper.HEADER_LIMIT + 2)
        self.stdin = FakeStdin() if stdin else None
        self.returncode: int | None = None
        self.killed = asyncio.Event()
        self.lingers = lingers

    def frame(self, jpeg: bytes, width: int = 1280) -> None:
        header = {"bytes": len(jpeg), "width": width, "height": 800}
        self.stdout.feed_data(json.dumps(header).encode() + b"\n" + jpeg)

    def kill(self) -> None:
        if self.returncode is not None:
            raise ProcessLookupError
        self.returncode = -9
        self.killed.set()

    async def wait(self) -> int:
        if self.lingers:
            await asyncio.Event().wait()
        await self.killed.wait()
        return -9


class Hub:
    def __init__(self) -> None:
        self.posts: list[tuple[float, str, dict[str, Any]]] = []
        self.answers: list[Any] = []
        self.kbps: Any = 1000

    async def post(self, target: str, body: Any) -> Any:
        self.posts.append((time.monotonic(), target, dict(body)))
        if self.answers:
            step = self.answers.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return {"until": "2026-11-23T10:00:30.000Z", "kbps": self.kbps}

    def frames(self) -> list[dict[str, Any]]:
        return [body["frame"] for _at, _target, body in self.posts if "frame" in body]


def link(hub: Hub, process: FakeProcess, *, key: tuple[int, bytes] | None = (3, KEY)) -> Link:
    async def get_key(wg_id: str) -> tuple[int, bytes] | None:
        assert wg_id == WG
        return key

    async def spawn(vm: Any, argv: Any) -> FakeProcess:
        assert list(argv) == ["sudo", "-n", "/usr/local/bin/workvm-browser", "stream", "5", "60", "1280"]
        return process

    return Link(post=hub.post, key=get_key, spawn=spawn)


def ctx() -> OperationContext:
    op: Any = SimpleNamespace(id=OP)
    return OperationContext(context=None, operation=op, session=session(), params={}, data={}, attempt=1, env="env")


async def until(check: Callable[[], bool], limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def live_for(
    world: World, body: Callable[[Browsers, LiveStreams, Hub, FakeProcess], Any], *, key: Any = (3, KEY), **options: Any
) -> Any:
    hub, shape = Hub(), options.pop("process", {})

    async def run(vms: WorkVms) -> Any:
        process = FakeProcess(**shape)  # its stream reader belongs to the running loop
        browsers = Browsers(workvms=vms)
        browsers.live = LiveStreams(browsers, link(hub, process, key=key), **{"max_kbps": 3000, **options})
        await browsers.open(ctx())
        return await body(browsers, browsers.live, hub, process)

    return world.run(run)


def test_one_stream_per_session_seals_frames_and_ends_with_409(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        assert await live.start(WG) is False  # no browser session of that session
        assert await live.start(SESSION) is True
        assert await live.start(SESSION) is False
        await until(lambda: len(hub.posts) == 1)
        process.frame(b"jpeg-1")
        await until(lambda: len(hub.frames()) == 1)
        hub.answers.append(HubRefusal(409, "not_watched"))
        await asyncio.sleep(0.25)
        process.frame(b"jpeg-2")
        await until(lambda: not live.tasks)
        row = await rows.open_of(browsers.db, SESSION)
        return hub, process, row

    hub, process, row = live_for(world, body)
    _first, target, empty = hub.posts[0]
    assert (target, empty) == (f"/v1/device/sessions/{SESSION}/browser/live", {})
    frame, _refused = hub.frames()
    assert (frame["browser"], frame["session"], frame["wg"], frame["epoch"], frame["seq"]) == (
        row.browser_id,
        SESSION,
        WG,
        3,
        1,
    )
    assert frame["ts"] == "2026-11-23T10:00:00.000Z"
    assert open_frame(KEY, frame, session=SESSION, wg=WG) == b"jpeg-1"
    assert row.frame_seq == 2  # the refused frame used its own seq
    assert process.stdin is not None
    assert process.stdin.closed
    assert process.returncode == -9
    assert any(
        line.startswith(f"runner.browser live browser_id={row.browser_id} frames=1 bytes=") for line in world.logs
    )


def test_a_first_409_starts_no_screencast(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        hub.answers.append(HubRefusal(409, "not_watched"))
        await live.start(SESSION)
        await until(lambda: not live.tasks)

    live_for(world, body)
    assert any(line.endswith("frames=0 bytes=0") for line in world.logs)


def test_frames_are_paced_by_the_byte_budget_keeping_the_newest(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        hub.kbps = 100  # 12500 bytes per second
        await live.start(SESSION)
        await until(lambda: len(hub.posts) == 1)
        process.frame(b"a" * 2000)
        await until(lambda: len(hub.frames()) == 1)
        process.frame(b"b" * 10)
        process.frame(b"c" * 10)  # replaces b before the budget allows
        await until(lambda: len(hub.frames()) == 2)
        live.stop(SESSION)
        await until(lambda: not live.tasks)
        return hub

    hub = live_for(world, body)
    posted = [(at, body["frame"]) for at, _t, body in hub.posts if "frame" in body]
    first, second = posted
    assert open_frame(KEY, second[1], session=SESSION, wg=WG) == b"c" * 10
    size = len(json.dumps(first[1]))
    assert second[0] - first[0] >= size * 8 / (100 * 1000) - 0.01


def test_the_cap_and_the_keepalive(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        hub.kbps = 9000
        hub.answers += [{"kbps": "fast"}]
        await live.start(SESSION)
        await until(lambda: len(hub.posts) >= 4)
        assert live.tasks
        await live.stop_all()
        return hub

    hub = live_for(world, body, max_kbps=500, keepalive=0.02)
    assert all(body == {} for _at, _t, body in hub.posts)


def test_kbps_is_capped(world: World) -> None:
    seen: list[int] = []

    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        hub.kbps = 9000
        totals = browser_live._Totals()
        row = await rows.open_of(browsers.db, SESSION)
        assert row is not None
        assert await live._post(row, {}, totals) is True
        seen.append(totals.kbps)

    live_for(world, body, max_kbps=500)
    assert seen == [500]


def test_a_hub_that_does_not_answer_ends_the_stream(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        hub.answers += [HubRefusal(503, "unavailable"), HubTransportError("connect_failed")] * 100
        await live.start(SESSION)
        await until(lambda: len(hub.posts) >= 1)
        process.frame(b"x")  # a frame the hub does not take: the stream goes on
        await until(lambda: len(hub.frames()) == 1)
        await until(lambda: not live.tasks)

    live_for(world, body, keepalive=0.01, give_up=0.5)
    assert any("live_post_failed" in line and "code=unavailable" in line for line in world.logs)
    assert any("live_post_failed" in line and "reason=connect_failed" in line for line in world.logs)


def test_without_a_key_or_a_seal_no_frame_leaves(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        await live.start(SESSION)
        await until(lambda: len(hub.posts) == 1)
        process.frame(b"x")
        await asyncio.sleep(0.3)
        process.frame(b"y")
        await asyncio.sleep(0.3)
        await live.stop_all()
        return hub

    hub = live_for(world, body, key=None)
    assert hub.frames() == []
    assert sum("live_key_missing" in line for line in world.logs) == 1

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise CryptoError("size_exceeded")

    monkeypatch.setattr(browser_live, "seal_frame", broken)
    hub = live_for(world, body)
    assert hub.frames() == []
    assert any("live_seal_failed" in line and "code=size_exceeded" in line for line in world.logs)


@pytest.mark.parametrize("ending", ["header", "overrun", "partial_line", "partial_frame"])
def test_a_broken_screencast_loses_the_vm(world: World, ending: str) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        await live.start(SESSION)
        await until(lambda: len(hub.posts) == 1)
        if ending == "header":
            process.stdout.feed_data(b'{"bytes": 1}\n')
        elif ending == "overrun":
            process.stdout.feed_data(b"x" * 5000)
        elif ending == "partial_line":
            process.stdout.feed_data(b'{"bytes"')
            process.stdout.feed_eof()
        else:
            process.stdout.feed_data(b'{"bytes": 9, "width": 1, "height": 1}\nabc')
            process.stdout.feed_eof()
        await until(lambda: not live.tasks)
        return await browsers.running(SESSION)

    assert live_for(world, body) is None
    assert any(line.startswith("runner.browser lost browser_id=") for line in world.logs)


def test_the_end_of_the_screencast_ends_the_stream(world: World) -> None:
    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        await live.start(SESSION)
        await until(lambda: len(hub.posts) == 1)
        process.stdout.feed_eof()
        await until(lambda: not live.tasks)
        return await browsers.running(SESSION)

    assert live_for(world, body) is not None


def test_closing_the_browser_ends_its_stream(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_live, "STOP_WAIT", 0.05)

    async def body(browsers: Browsers, live: LiveStreams, hub: Hub, process: FakeProcess) -> Any:
        await live.start(SESSION)
        await until(lambda: len(hub.posts) == 1)
        await browsers.close(ctx())
        await until(lambda: not live.tasks)
        live.stop(SESSION)  # nothing runs: nothing to stop
        return process

    process = live_for(world, body, process={"stdin": False, "lingers": True})
    assert process.returncode == -9


def test_spawn_process_runs_the_argv_in_the_vm(tmp_path: Path) -> None:
    limactl = tmp_path / "limactl"
    limactl.write_text("#!/bin/sh\nshift 4\nprintf '%s\\n' \"$*\"\ncat\n", encoding="utf-8")
    limactl.chmod(limactl.stat().st_mode | stat.S_IXUSR)
    shell = VmShell(SlotCommand((), limactl=str(limactl)), 0, "wvm-0a1b2c3d")
    assert shell.argv(["a"]) == [str(limactl), "shell", "--workdir", "/", "wvm-0a1b2c3d", "a"]

    async def run() -> tuple[bytes, bytes]:
        process = await spawn_process(shell, ["sudo", "x"])
        assert process.stdin is not None
        assert process.stdout is not None
        line = await process.stdout.readuntil(b"\n")
        process.stdin.write(b"echo")
        process.stdin.close()
        rest = await process.stdout.read()
        await process.wait()
        return line, rest

    assert asyncio.run(run()) == (b"sudo x\n", b"echo")


# -- hint and shutdown -----------------------------------------------------------------------------------------


def test_the_hint_starts_the_stream_of_a_valid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    class FakeLive:
        async def start(self, session_id: str) -> bool:
            started.append(session_id)
            return True

    found: dict[str, Any] = {"browsers": None}
    monkeypatch.setattr(hints, "browsers_for", lambda context: found["browsers"])
    assert {"browser": hints.watch} == hints.HANDLERS
    asyncio.run(hints.watch(None, {"type": "browser", "session_id": SESSION}))  # no work VMs
    found["browsers"] = SimpleNamespace(live=None)
    asyncio.run(hints.watch(None, {"type": "browser", "session_id": SESSION}))  # no live picture
    found["browsers"] = SimpleNamespace(live=FakeLive())
    for bad in (None, 7, "not-a-session"):
        asyncio.run(hints.watch(None, {"type": "browser", "session_id": bad}))
    asyncio.run(hints.watch(None, {"type": "browser", "session_id": SESSION}))
    assert started == [SESSION]


def test_the_shutdown_ends_every_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped: list[bool] = []

    class FakeLive:
        async def stop_all(self) -> None:
            stopped.append(True)

    assert [hook.name for hook in shutdown.HOOKS] == ["workvm-browser-live"]
    context: Any = SimpleNamespace()
    asyncio.run(shutdown.stop_live(context))
    monkeypatch.setattr(shutdown, "existing_browsers", lambda found: SimpleNamespace(live=None))
    asyncio.run(shutdown.stop_live(context))
    monkeypatch.setattr(shutdown, "existing_browsers", lambda found: SimpleNamespace(live=FakeLive()))
    asyncio.run(shutdown.stop_live(context))
    assert stopped == [True]
    assert service.existing_browsers(SimpleNamespace(_browsers="not browsers")) is None
