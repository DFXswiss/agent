"""Fixtures of the events suites (U-HUB-events): actors and an org with a workgroup, recorded frames and signals."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from agent_hub import signals
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import World


@pytest.fixture
def world(hub: HubHarness) -> World:
    """Actors and orgs on the running hub."""
    return World(hub)


@pytest.fixture
def scene(world: World) -> Scene:
    """An org with an admin, a writer, a reader, a stranger, an assigned device and a second workgroup."""
    return Scene.build(world)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


@pytest.fixture
def emitted() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Every `event.appended` signal (the discovered subscribers are restored afterwards)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def record(name: str, ids: Mapping[str, Any]) -> None:
        seen.append((name, dict(ids)))

    signals.configure({"event.appended": [record]})
    yield seen
    signals.configure()
