"""U-RUN-sync (daemon wiring): the `sync` and `ws` tasks, the NOTIFY listener, the shutdown flush, the extension
points and the code lists (05-runner.md sections 1.1 and 2.3, ADR-027 section 4)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.sync import codes, extensions, hints, shutdown, tasks
from agent_runner.sync.backoff import Backoff
from agent_runner.sync.push import IDLE_POLL_S
from agent_runner.sync.runtime import SyncOptions, runtime
from agent_runner.sync.ws import HEARTBEAT_S

from .conftest import Harness, run
from .fakehub import CONTRACTS
from .test_outbox import fixture_body
from .test_ws import until

METRICS = fixture_body("metrics")


def test_tasks_end_at_once_without_an_identity(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env(enrolled=False) as env:
            await tasks.run_sync(env.context)
            await tasks.run_ws(env.context)
            await shutdown.flush(env.context)
            assert runtime(env.context) is None
            assert env.log() == "runner.sync not_enrolled task=sync\nrunner.sync not_enrolled task=ws\n"

    run(body)


def test_sync_task_pushes_when_notified(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.clock.real[IDLE_POLL_S] = 30.0  # only NOTIFY can wake the loop in time
            task = asyncio.ensure_future(tasks.run_sync(env.context))
            await until(lambda: any(r.target == "/v1/device/state" for r in env.hub.requests))
            await asyncio.sleep(0.1)
            await env.enqueue("metrics", METRICS)
            await until(lambda: sorted(env.hub.stored) == [1])
            env.context.stopping.set()
            env.sync.pusher.notify()
            await asyncio.wait_for(task, 10)

    run(body)


def test_listener_reconnects_after_a_database_failure(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.context.dsn = "postgresql://nobody@127.0.0.1:1/none"
            failures: list[float] = []
            done = asyncio.Event()

            def on_sleep(seconds: float) -> None:
                failures.append(seconds)
                if len(failures) == 2:
                    done.set()

            env.clock.on_sleep = on_sleep
            listener = asyncio.ensure_future(tasks.listen(env.context, env.sync.pusher))
            await asyncio.wait_for(done.wait(), 10)
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            assert failures[:2] == [tasks.LISTEN_RETRY_S, tasks.LISTEN_RETRY_S]
            assert "runner.sync listen_failed error=OperationalError\n" in env.log()

    run(body)


def test_ws_task_connects_and_stops(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.clock.real[HEARTBEAT_S] = 0.05
            task = asyncio.ensure_future(tasks.run_ws(env.context))
            await until(lambda: len(env.hub.frames) >= 1)
            env.context.stopping.set()
            await asyncio.wait_for(task, 10)

    run(body)


def test_shutdown_flush(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await env.enqueue("metrics", METRICS)
            await shutdown.flush(env.context)
            assert sorted(env.hub.stored) == [1]
            assert "runner.sync flushed waiting=idle\n" in env.log()
            assert env.sync.client._client.is_closed

    run(body)


class _Broken(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)


def test_shutdown_flush_with_the_hub_unreachable(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env(options={"transport": _Broken()}) as env:
            await env.enqueue("metrics", METRICS)
            await shutdown.flush(env.context)
            assert "runner.sync flush_failed error=HubTransportError\n" in env.log()

    run(body)


def test_runtime_is_built_once_with_defaults(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            context: Any = env.context
            del context.sync_options
            built = runtime(context)
            assert built is not None
            assert runtime(context) is built
            assert built.client.origin.base_url == env.context.identity.device.hub_url  # type: ignore[union-attr]
            assert isinstance(SyncOptions().random(), float)

    run(body)


def test_real_package_registrations() -> None:
    # The sync area's own registrations; other areas add theirs (vm: tasks and heartbeat fields, VM-02; reconcile and
    # inbox: tasks, hints and connect hooks, RUN-03), so only the presence of the sync entries is checked.
    assert {"sync", "ws"} <= {spec.name for spec in registry.tasks()}
    assert "outbox-flush" in [hook.name for hook in registry.shutdown_hooks()]
    assert registry.hint_handlers()["hello"] is hints.hello
    assert set(extensions.connect_hooks()) == {"reconcile", "pull"}
    assert set(extensions.heartbeat_sections()) <= {"capacity", "sessions", "vms"}


C_ = "from agent_runner.sync.extensions import ConnectHook as C\n"
H_ = "from agent_runner.sync.extensions import HeartbeatSection as H\n"


def discover_all(package: str) -> None:
    extensions.connect_hooks(package)
    extensions.heartbeat_sections(package)


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"a/connect.py": "HOOKS = 'x'"}, "registry_invalid a.connect"),
        ({"a/connect.py": "HOOKS = [1]"}, "registry_invalid a.connect"),
        ({"a/connect.py": "HOOKS = []\nX = 1", "b/connect.py": "Y = 1"}, "registry_invalid b.connect"),
        (
            {"a/connect.py": C_ + "HOOKS = [C('push', 'n', None)]"},
            "registry_invalid a.connect",
        ),
        (
            {"a/connect.py": C_ + "HOOKS = [C('pull', 'N!', None)]"},
            "registry_invalid a.connect",
        ),
        (
            {
                "a/connect.py": C_ + "HOOKS = [C('pull', 'n', None)]",
                "b/connect.py": C_ + "HOOKS = [C('reconcile', 'n', None)]",
            },
            "registry_duplicate n",
        ),
        (
            {"a/heartbeat.py": H_ + "SECTIONS = [H('cpu', None)]"},
            "registry_invalid a.heartbeat",
        ),
        (
            {
                "a/heartbeat.py": H_ + "SECTIONS = [H('vms', None)]",
                "b/heartbeat.py": H_ + "SECTIONS = [H('vms', None)]",
            },
            "registry_duplicate vms",
        ),
        ({"a/heartbeat.py": "SECTIONS = b'x'"}, "registry_invalid a.heartbeat"),
    ],
)
def test_extension_discovery_refusals(
    area_package: Callable[[dict[str, str]], str], files: dict[str, str], error: str
) -> None:
    package = area_package(files)
    with pytest.raises(RunnerError) as caught:
        discover_all(package)
    assert str(caught.value) == error


def test_extension_discovery_order(area_package: Callable[[dict[str, str]], str]) -> None:
    hook = C_ + "HOOKS = [C('{phase}', '{name}', None)]"
    package = area_package(
        {
            "zeta/connect.py": hook.format(phase="pull", name="zeta-pull"),
            "alpha/connect.py": hook.format(phase="pull", name="alpha-pull"),
            "beta/connect.py": hook.format(phase="reconcile", name="beta-reconcile"),
        }
    )
    phases = extensions.connect_hooks(package)
    assert [h.name for h in phases["pull"]] == ["alpha-pull", "zeta-pull"]
    assert [h.name for h in phases["reconcile"]] == ["beta-reconcile"]


def test_codes_equal_the_contracts() -> None:
    errors = json.loads((CONTRACTS / "enums/error-codes.json").read_text())["codes"]
    assert {c["code"] for c in errors if c["origin"] == "hub"} == codes.HUB_ERROR_CODES
    kinds = json.loads((CONTRACTS / "enums/outbox-item-kinds.json").read_text())["kinds"]
    assert list(codes.ITEM_KINDS) == kinds


def test_backoff() -> None:
    values = iter([0.0, 1.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    backoff = Backoff(random=lambda: next(values))
    assert [backoff.next() for _ in range(7)] == pytest.approx([0.8, 2.4, 4.0, 8.0, 16.0, 30.0, 30.0])
    backoff.reset()
    assert backoff.next() == pytest.approx(1.0)
    capped = Backoff(random=lambda: 0.5)
    capped.failures = 5000
    assert capped.next() == 30.0
    assert capped.failures == 64


@pytest.mark.parametrize(
    "message",
    [b'{"type":"reconcile"}', "[]", '{"type":3}', '{"type":"Bad Type"}', '{"nope":1}', "{" * 5000],
)
def test_hint_parse_refusals(message: str | bytes) -> None:
    assert hints.parse(message) is None


def test_hello_hint(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            runtime(env.context)
            stamp = datetime.fromtimestamp(env.wall.now, UTC)
            await hints.hello(env.context, {"type": "hello", "hub_time": stamp.strftime("%Y-%m-%dT%H:%M:%S.123Z")})
            await hints.hello(env.context, {"type": "hello", "hub_time": "2026-02-30T00:00:00.000Z"})
            await hints.hello(env.context, {"type": "hello"})
            assert env.log() == "runner.sync hello_invalid\nrunner.sync hello_invalid\n"

    run(body)
