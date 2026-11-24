"""Fixtures of the tasks, sessions, scheduler and workgroups suites: the desk, recorded frames and signals, and fresh
module state (no bound scheduler context, discovered device-state sections and signal subscribers)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from agent_hub import signals
from agent_hub.device import signals as device_signals
from agent_hub.device import state
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World
from tests.platform.hub.tasks.support import Desk


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    for cache in (logs.ORG_LOGS, logs.PLATFORM_LOGS, platform_log.CACHE):
        cache.clear()
    scheduler_signals._context.clear()
    device_signals._context.clear()
    state.configure()
    signals.configure()
    yield
    scheduler_signals.wait()
    device_signals.wait()
    scheduler_signals._context.clear()
    device_signals._context.clear()
    signals.configure()


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def desk(world: World) -> Desk:
    """The scene with both devices online and with capacity."""
    found = Desk.build(world)
    found.online(found.scene.device)
    found.online(found.scene.other_device, {**_low()})
    return found


def _low() -> dict[str, int]:
    return {
        "vm_memory_budget_mib": 65536,
        "vm_memory_used_mib": 0,
        "vm_slots_total": 8,
        "vm_slots_free": 1,
        "pool_ready": 0,
    }


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


@pytest.fixture
def task_signals() -> Iterator[list[dict[str, Any]]]:
    """Every `task.state` signal (the discovered subscribers are kept and restored afterwards)."""
    seen: list[dict[str, Any]] = []

    def record(_name: str, ids: Mapping[str, Any]) -> None:
        seen.append(dict(ids))

    found = {name: list(items) for name, items in signals.configure().items()}
    found.setdefault("task.state", []).append(record)
    signals.configure(found)
    yield seen
    signals.configure()


@pytest.fixture
def fleet(world: World) -> Fleet:
    """Devices with real keys (platform enrollment and certification)."""
    return Fleet(world)
