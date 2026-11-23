"""Fixtures of the live-channel suites (U-HUB-stream): the scene of the events suites and a live hub served by
uvicorn on the same database (`support.live_hub`)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from agent_hub import signals
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import World
from tests.platform.hub.stream.support import LiveHub, free_port, live_hub

LIVE_PORT_OFFSET = 14


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def scene(world: World) -> Scene:
    """An org with an admin, a writer, a reader, a stranger and two devices assigned to its workgroup."""
    return Scene.build(world)


@pytest.fixture
def live(hub: HubHarness) -> Iterator[LiveHub]:
    """A live hub (uvicorn) next to the harness, sharing its database, keys and clock."""
    with live_hub(hub, free_port(LIVE_PORT_OFFSET)) as served:
        yield served


@pytest.fixture
def emitted() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Every `device.presence` and `device.capacity` signal (the discovered subscribers are restored afterwards)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def record(name: str, ids: Mapping[str, Any]) -> None:
        seen.append((name, dict(ids)))

    signals.configure({"device.presence": [record], "device.capacity": [record]})
    yield seen
    signals.configure()
