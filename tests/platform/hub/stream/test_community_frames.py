"""The audience `all` and the `community` frame (12-community.md section 5, ADR-061 decision 8): every open browser
stream receives it, whichever org it follows; a user frame reaches only that user; the bus carries both."""

from __future__ import annotations

import uuid

import pytest

from agent_hub.stream import bus
from agent_hub.stream.publish import ALL, Audience, Published, check, publish
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, browser_key


def test_every_stream_of_every_org_gets_a_frame_for_all(scene: Scene, live: LiveHub) -> None:
    other = scene.world.private_org()  # a user of another org
    mine = live.stream(browser_key(scene.reader), org=scene.org)
    theirs = live.stream(browser_key(other.founder), org=other.id)
    assert mine.frame().event == "hello"  # type: ignore[union-attr]
    assert theirs.frame().event == "hello"  # type: ignore[union-attr]
    publish(Audience("all", ALL), "community", {"change": "message", "seq": 3})
    for stream in (mine, theirs):
        frame = stream.frame()
        assert frame is not None
        assert (frame.event, frame.data) == ("community", {"change": "message", "seq": 3})
    publish(Audience("user", uuid.UUID(other.founder.id)), "community", {"change": "key", "seq": 3})
    frame = theirs.frame()
    assert frame is not None
    assert frame.data == {"change": "key", "seq": 3}
    assert mine.quiet() == []


@pytest.mark.parametrize(
    "item",
    [
        Published(Audience("all", uuid.uuid4()), "community", {}),
        Published(Audience("all", ALL), "community", {}, wg_id=uuid.uuid4()),
        Published(Audience("all", ALL), "reconcile", {}),
    ],
)
def test_wrong_frames_for_all_are_refused(item: Published) -> None:
    with pytest.raises(ValueError, match=r"audience all|not a frame"):
        check(item)


def test_the_bus_carries_the_audience_all() -> None:
    item = Published(Audience("all", ALL), "community", {"change": "delete", "seq": 7})
    decoded = bus.decode(bus.encode("instance-1", item))
    assert (decoded.instance, decoded.item, decoded.oversized) == ("instance-1", item, False)
