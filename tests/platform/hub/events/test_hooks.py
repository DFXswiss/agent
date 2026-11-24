"""U-HUB-events (post-processors): hooks registered as `<area>/event_hooks.py:HOOKS` run inside the storing
transaction with the newly stored events, and a failing hook refuses the whole call (`agent_hub.events.hooks`)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from agent_hub.events import hooks
from agent_hub.events.store import StoredEvent
from agent_hub.registry import RegistryError
from agent_hub.whiteboards.event_hooks import project
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id


@pytest.fixture
def restore_hooks() -> Iterator[None]:
    yield
    hooks.configure()


def test_hooks_are_discovered_and_checked(restore_hooks: None) -> None:
    assert project in hooks.configure()  # the whiteboard projection (HUB-15)
    with pytest.raises(RegistryError, match="callables"):
        hooks.configure([1])  # type: ignore[list-item]


def test_hooks_see_new_events_in_the_transaction(scene: Scene, restore_hooks: None) -> None:
    seen: list[tuple[list[str], list[bool], int]] = []

    def hook(connection: Any, stored: list[StoredEvent]) -> None:
        ids = [item.id for item in stored]
        visible = connection.execute("SELECT count(*) FROM hub.event WHERE id = ANY(%s::uuid[])", (ids,)).fetchone()
        seen.append((ids, [item.notify for item in stored], visible[0]))

    hooks.configure([hook])
    entry = scene.wg_event(scene.writer, "whiteboard.entry", ref=new_id())
    settings = scene.wg_event(scene.writer)
    assert scene.post(scene.writer, entry, settings, notify=[True, False]).status_code == 200
    assert seen == [([entry["header"]["id"], settings["header"]["id"]], [True, False], 2)]
    assert scene.post(scene.writer, entry).json()["results"][0]["replayed"]
    assert len(seen) == 1  # replays only: no call


def test_a_failing_hook_refuses_the_call(scene: Scene, restore_hooks: None) -> None:
    def broken(_connection: Any, _stored: list[StoredEvent]) -> None:
        raise RuntimeError("projection failed")

    hooks.configure([broken])
    event = scene.wg_event(scene.writer)
    with pytest.raises(RuntimeError, match="projection"):
        scene.append(scene.writer, event)
    assert scene.rows("SELECT count(*) FROM hub.event WHERE id = %s", event["header"]["id"]) == [(0,)]
