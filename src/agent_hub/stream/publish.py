"""Live publishing of the hub (ADR-027 section 4: live publishing only through this module).

Areas call `publish(audience, frame, data)` after their transaction committed: `audience` names who receives the frame
(the browsers of an organization or of one user, or one device), `frame` the SSE frame type of 03-api.md section 5 or
the device hint type of section 6.2, `data` its JSON body (identifiers, metadata and signed or encrypted items only,
never plaintext content). `hint(device_id, type, body)` is the shorthand for a device hint. A browser frame that
belongs to one workgroup (task, session and operation rows, workgroup metrics) reaches only browsers that may read
that workgroup: the stream takes the workgroup from `wg_id=` or from the row in `data` (`agent_hub.stream.sse`).

The default fan-out is `LIVE`: it hands every frame to the live services attached to it - one per running hub app
(`agent_hub.stream.service`), which delivers it to its own browsers and devices and, over the PostgreSQL bus, to every
other hub process (`agent_hub.stream.bus`). Without an attached service a frame is dropped: nobody is connected to this
process, and the hub never queues frames or hints (03-api.md section 6.2). `event` and `terminal` frames are announced
by `agent_hub.events.notify` and reach every process through the event channel `hub_events` (which the bus listens
to), so the live services ignore them here. Tests install `MemoryPublisher` with `set_publisher`.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

AudienceKind = Literal["org", "user", "device"]
# 03-api.md section 5: every frame of GET /v1/stream; `hello` and `resync` are made by the stream itself.
BROWSER_FRAMES = frozenset(
    {
        "hello",
        "event",
        "terminal",
        "task",
        "session",
        "operation",
        "log",
        "wraps",
        "notification",
        "device_response",
        "metrics",
        "resync",
    }
)
# An internal frame that is never sent to a browser: it ends the open streams of the keys in `data["key_ids"]` of the
# user (a revoked agent, an ended browser session; KOR-24), in every hub process.
CLOSE_KEYS = "close_keys"
PUBLISHED_FRAMES = (BROWSER_FRAMES - {"hello", "resync"}) | {CLOSE_KEYS}
# 03-api.md section 6.2: every hint of GET /v1/device/ws; `hello` is sent by the socket itself.
DEVICE_HINTS = frozenset(
    {
        "hello",
        "reconcile",
        "session.start",
        "session.control",
        "prompt",
        "events",
        "log",
        "wraps",
        "credentials",
        "request",
        "ping",
        "decertified",
    }
)
PUBLISHED_HINTS = DEVICE_HINTS - {"hello"}


@dataclass(frozen=True)
class Audience:
    """The receivers of a frame: the browsers of an organization or of a user, or one device."""

    kind: AudienceKind
    id: UUID


@dataclass(frozen=True)
class Published:
    """One published frame; `wg_id` restricts a browser frame to readers of that workgroup."""

    audience: Audience
    frame: str
    data: Mapping[str, Any]
    wg_id: UUID | None = None


class Publisher(Protocol):
    """The fan-out behind `publish`."""

    def publish(self, item: Published) -> None: ...


def check(item: Published) -> None:
    """Refuse a frame type that the audience cannot receive (a programming error of the publishing area)."""
    if item.audience.kind not in ("org", "user", "device"):
        raise ValueError("unknown audience kind")
    allowed = PUBLISHED_HINTS if item.audience.kind == "device" else PUBLISHED_FRAMES
    if item.frame not in allowed:
        raise ValueError(f"{item.frame!r} is not a frame for a {item.audience.kind} audience")
    if item.audience.kind == "device" and (item.wg_id is not None or "type" in item.data):
        raise ValueError("a device hint has no workgroup restriction and no field `type` in its body")


class MemoryPublisher:
    """In-process fake: keeps every published frame in order (for tests)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Published] = []

    def publish(self, item: Published) -> None:
        with self._lock:
            self._items.append(item)

    def items(self) -> list[Published]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class LivePublisher:
    """The default fan-out: every attached live service receives every frame (module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sinks: tuple[Publisher, ...] = ()

    def attach(self, sink: Publisher) -> None:
        with self._lock:
            self._sinks = (*self._sinks, sink)

    def detach(self, sink: Publisher) -> None:
        with self._lock:
            self._sinks = tuple(item for item in self._sinks if item is not sink)

    def sinks(self) -> tuple[Publisher, ...]:
        return self._sinks

    def publish(self, item: Published) -> None:
        for sink in self._sinks:
            sink.publish(item)


LIVE = LivePublisher()
_state: dict[str, Publisher] = {"publisher": LIVE}


def set_publisher(publisher: Publisher) -> Publisher:
    """Install the fan-out; returns the previous one."""
    previous = _state["publisher"]
    _state["publisher"] = publisher
    return previous


def current_publisher() -> Publisher:
    """The installed fan-out."""
    return _state["publisher"]


def publish(audience: Audience, frame: str, data: Mapping[str, Any], *, wg_id: UUID | None = None) -> None:
    """Publish one frame to the audience (`ValueError` for a frame the audience cannot receive)."""
    item = Published(audience=audience, frame=frame, data=dict(data), wg_id=wg_id)
    check(item)
    _state["publisher"].publish(item)


def close_keys(user_id: UUID, key_ids: Sequence[str]) -> None:
    """End the open streams of `user_id` signed with one of `key_ids` in every hub process (after the commit)."""
    if key_ids:
        publish(Audience("user", user_id), CLOSE_KEYS, {"key_ids": list(key_ids)})


def hint(device_id: UUID, hint_type: str, body: Mapping[str, Any] | None = None) -> None:
    """Publish the content-free hint `hint_type` (03-api.md section 6.2) to one device."""
    publish(Audience("device", device_id), hint_type, body or {})
