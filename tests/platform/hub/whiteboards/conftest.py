"""Fixtures of the whiteboard suites (U-HUB-whiteboards): the scene with its board and recorded frames."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import World
from tests.platform.hub.whiteboards.support import Board


@pytest.fixture
def scene(hub: HubHarness) -> Scene:
    return Scene.build(World(hub))


@pytest.fixture
def board(scene: Scene) -> Board:
    return Board.build(scene)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)
