"""U-HUB-stream (publish): the publishing interface - closed frame lists per audience, the default live fan-out with
attached services, the fake publisher and the `hint` shorthand (ADR-027 section 4)."""

from __future__ import annotations

import uuid

import pytest

from agent_hub.stream import publish as module
from agent_hub.stream.publish import (
    LIVE,
    Audience,
    LivePublisher,
    MemoryPublisher,
    Published,
    check,
    current_publisher,
    hint,
    publish,
    set_publisher,
)


def test_the_default_fan_out_is_live() -> None:
    assert isinstance(LIVE, LivePublisher)
    assert module._state["publisher"] is LIVE or isinstance(current_publisher(), MemoryPublisher)


def test_live_publisher_hands_frames_to_every_attached_sink() -> None:
    live = LivePublisher()
    first, second = MemoryPublisher(), MemoryPublisher()
    item = Published(Audience("org", uuid.uuid4()), "task", {})
    live.publish(item)  # nobody attached: dropped
    live.attach(first)
    live.attach(second)
    assert live.sinks() == (first, second)
    live.publish(item)
    live.detach(first)
    live.publish(item)
    assert first.items() == [item]
    assert second.items() == [item, item]


def test_publish_and_hint_go_through_the_installed_publisher() -> None:
    fake = MemoryPublisher()
    previous = set_publisher(fake)
    try:
        device, org = uuid.uuid4(), uuid.uuid4()
        hint(device, "reconcile")
        hint(device, "session.start", {"session_id": "s"})
        publish(Audience("org", org), "metrics", {"samples": []}, wg_id=org)
        assert fake.items() == [
            Published(Audience("device", device), "reconcile", {}),
            Published(Audience("device", device), "session.start", {"session_id": "s"}),
            Published(Audience("org", org), "metrics", {"samples": []}, wg_id=org),
        ]
    finally:
        set_publisher(previous)


@pytest.mark.parametrize(
    "item",
    [
        Published(Audience("org", uuid.UUID(int=1)), "hello", {}),
        Published(Audience("org", uuid.UUID(int=1)), "resync", {}),
        Published(Audience("user", uuid.UUID(int=1)), "events", {}),
        Published(Audience("device", uuid.UUID(int=1)), "hello", {}),
        Published(Audience("device", uuid.UUID(int=1)), "task", {}),
        Published(Audience("device", uuid.UUID(int=1)), "reconcile", {}, wg_id=uuid.UUID(int=2)),
        Published(Audience("device", uuid.UUID(int=1)), "reconcile", {"type": "decertified"}),
        Published(Audience("team", uuid.UUID(int=1)), "task", {}),  # type: ignore[arg-type]
    ],
)
def test_frames_outside_the_closed_lists_are_refused(item: Published) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message names the frame
        check(item)
    with pytest.raises(ValueError):  # noqa: PT011
        publish(item.audience, item.frame, item.data, wg_id=item.wg_id)
