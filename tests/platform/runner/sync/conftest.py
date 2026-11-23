"""Fixtures of the sync suites: a migrated store, a device identity, the fake hub and a daemon-like context.

`harness.env()` is an async context manager (tests run their coroutine with `asyncio.run`): it opens a pool on the
test database, builds the identity and the fake hub (HTTP through `httpx.ASGITransport`, WebSocket on a loopback port
of the package's range), and a context with the attributes the daemon passes to tasks (`DaemonContext`).
"""

from __future__ import annotations

import asyncio
import io
import itertools
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.storage import scope_key_context, seal_local
from agent_runner.encoding import b64u_encode
from agent_runner.host import Host
from agent_runner.identity import Device, DeviceKeys, Identity, generate_keys
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.sync.runtime import SyncOptions, SyncRuntime, runtime

from .fakehub import FakeHub

_PORTS = itertools.cycle(range(10, 20))


def next_port() -> int:
    """A loopback port of the package range (`WP_PORT_BASE + 10 .. + 19`), or 0 (ephemeral) outside the workspace."""
    base = os.environ.get("WP_PORT_BASE")
    return int(base) + next(_PORTS) if base else 0


@dataclass
class Wall:
    """Settable wall clock (Unix seconds)."""

    now: float = 1_795_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Clock:
    """Monotonic test clock; `sleep` advances it and yields."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)
    # duration -> real seconds actually waited (e.g. the 15 s heartbeat waits a little so frames do not flood)
    real: dict[float, float] = field(default_factory=dict)
    on_sleep: Callable[[float], None] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        await asyncio.sleep(self.real.get(seconds, 0))


@dataclass
class Context:
    """The attributes of `lifecycle.DaemonContext` the sync area uses, plus `sync_options`."""

    identity: Identity | None
    host: Host
    dsn: str
    pool: Any
    log: io.StringIO
    sync_options: SyncOptions
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    config: Any = None


@dataclass
class Env:
    context: Context
    hub: FakeHub
    keys: DeviceKeys
    wall: Wall
    clock: Clock
    port: int

    @property
    def sync(self) -> SyncRuntime:
        built = runtime(self.context)
        assert built is not None
        return built

    @property
    def pool(self) -> Any:
        return self.context.pool

    def log(self) -> str:
        return self.context.log.getvalue()

    async def add_scope_key(self, scope_id: str, epoch: int, key: bytes, scope: str = "wg") -> None:
        blob = seal_local(self.sync.storage_key, "scope_key", scope_key_context(scope, scope_id, epoch), key)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source) VALUES (%s, %s, %s, %s, 'wrap')",
                (scope, scope_id, epoch, blob),
            )

    async def set_log_head(self, org_id: str, seq: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.log_head (org_id, seq, hash) VALUES (%s, %s, 'h') "
                "ON CONFLICT (org_id) DO UPDATE SET seq = EXCLUDED.seq",
                (org_id, seq),
            )

    async def enqueue(self, kind: str, body: dict[str, Any]) -> int:
        async with self.pool.connection() as conn, conn.transaction():
            return await self.sync.enqueue(conn, kind, body)


@dataclass
class Harness:
    dsn: str

    @asynccontextmanager
    async def env(
        self,
        *,
        package: str | None = None,
        enrolled: bool = True,
        options: dict[str, Any] | None = None,
        hub_url: str | None = None,
    ) -> AsyncIterator[Env]:
        wall, clock = Wall(), Clock()
        keys = generate_keys()
        device_id = uuid7()
        port = next_port()
        hub = FakeHub(device_id=device_id, device_public=keys.signing_key().public_key().public_bytes_raw(), host="")
        hub.wall = wall
        port = await hub.start_ws(port)
        url = hub_url or f"http://127.0.0.1:{port}"
        hub.host = url.split("://", 1)[1]
        device = Device(
            device_id=device_id,
            kind="platform",
            hub_url=url,
            hub_key=b64u_encode(hub.public),
            enrolled_at="2026-11-21T00:00:00.000Z",
        )
        transport = httpx.ASGITransport(app=hub)  # type: ignore[arg-type]
        # pushes are not hedged unless a test asks for it (a real-time timer would race the in-process hub)
        settings: dict[str, Any] = {
            "transport": transport,
            "wall": wall,
            "random": lambda: 0.5,
            "pusher_settings": {"hedge_min": None},
        }
        settings.update(options or {})
        host = Host(env={}, clock=clock, sleep=clock.sleep)
        if package is not None:
            host.package = package
        pool = await open_pool(self.dsn, max_size=4)
        context = Context(
            identity=Identity(device=device, keys=keys) if enrolled else None,
            host=host,
            dsn=self.dsn,
            pool=pool,
            log=io.StringIO(),
            sync_options=SyncOptions(**settings),
        )
        try:
            yield Env(context=context, hub=hub, keys=keys, wall=wall, clock=clock, port=port)
        finally:
            built = getattr(context, "sync_runtime", None)
            if built is not None:
                await built.close()
            await pool.close()
            await hub.stop_ws()
        assert hub.violations == []


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    """A test database with the runner schema."""
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture
def harness(store_dsn: str) -> Harness:
    return Harness(store_dsn)


def run[T](coro: Callable[[], Any]) -> Any:
    return asyncio.run(coro())


def uuid() -> str:
    return uuid7()
