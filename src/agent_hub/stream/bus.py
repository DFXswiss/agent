"""The live bus between hub processes: PostgreSQL `LISTEN`/`NOTIFY` (00-overview.md section 4, 03-api.md section 5).

Every hub process runs one `Bus` with one dedicated autocommit connection that listens to two channels:

- `hub_events` (`agent_hub.events.notify.CHANNEL`): sent inside the storing transaction of every new event and
  delivered at its commit, in commit order; the payload names the event (`org_id`, `scope`, `scope_id`, `scope_seq`),
  the process reads it from `hub.event`. Every process, including the storing one, learns of every committed event
  exactly once this way.
- `hub_stream` (`STREAM_CHANNEL`): every other published frame and hint, as compact ASCII JSON
  `{"i": instance, "a": [audience kind, audience id], "f": frame, "d": data, "w": workgroup or null}`. A process
  delivers its own frames at once and skips its own messages on the bus (`i`). A frame whose text exceeds
  `NOTIFY_LIMIT` bytes (PostgreSQL allows 8000) travels as `{"i", "a", "f", "w", "r": true}` instead (without the
  data): the other processes send `resync {buffer_overflow}` to the browsers of that audience that would have
  received the frame (same filter: workgroup, monitoring flag) or `reconcile` to the device, and the receivers pull.

Messages carry identifiers, metadata and items that are signed or encrypted end to end; never plaintext content.
Sending is asynchronous: `send` queues the text, a sender thread issues `pg_notify` in order over the hub's pool.
When the listening connection breaks, the bus reconnects (backoff 0.2 s doubling to 5 s) and reports the gap
(`on_gap`): notifications sent meanwhile are lost, so the process resyncs every local receiver.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.events.notify import CHANNEL as EVENT_CHANNEL
from agent_hub.events.notify import Appended, parse
from agent_hub.stream.publish import Audience, Published, check

LOG = logging.getLogger("agent_hub.stream")
STREAM_CHANNEL = "hub_stream"
NOTIFY_LIMIT = 7900
POLL_SECONDS = 0.25
RECONNECT_MIN_SECONDS = 0.2
RECONNECT_MAX_SECONDS = 5.0
READY_TIMEOUT_SECONDS = 30.0
WAKE = ""  # the empty message: wakes listeners (stop), ignored by every process
_AUDIENCE_KINDS = frozenset({"org", "user", "device", "all"})


@dataclass(frozen=True)
class BusMessage:
    """One decoded message of `STREAM_CHANNEL`: a frame, or (`oversized`) the frame type and workgroup of a frame that
    was too large for a notification (its data is empty)."""

    instance: str
    item: Published
    oversized: bool = False


def _json(value: object) -> str:
    """Compact ASCII JSON (frames may carry numbers such as `cpu_ratio`, so not the canonical form of the crypto)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def encode(instance: str, item: Published) -> str:
    """The bus text of a published frame (the resync form when the frame is too large for a notification)."""
    audience = [item.audience.kind, str(item.audience.id)]
    wg = None if item.wg_id is None else str(item.wg_id)
    text = _json({"i": instance, "a": audience, "f": item.frame, "d": dict(item.data), "w": wg})
    if len(text) <= NOTIFY_LIMIT:
        return text
    return _json({"i": instance, "a": audience, "f": item.frame, "w": wg, "r": True})


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("not a UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("not a canonical UUID")
    return parsed


def decode(text: str) -> BusMessage:
    """A message of `STREAM_CHANNEL` (`ValueError` for anything else)."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("i"), str):
        raise ValueError("not a bus message")
    pair = data.get("a")
    if not isinstance(pair, list) or len(pair) != 2 or pair[0] not in _AUDIENCE_KINDS:
        raise ValueError("not a bus audience")
    audience = Audience(pair[0], _uuid(pair[1]))
    oversized = set(data) == {"i", "a", "f", "w", "r"} and data["r"] is True
    if not oversized and (set(data) != {"i", "a", "f", "d", "w"} or not isinstance(data["d"], dict)):
        raise ValueError("not a bus frame")
    if not isinstance(data["f"], str):
        raise ValueError("not a bus frame")
    wg = None if data["w"] is None else _uuid(data["w"])
    body = {} if oversized else cast(dict[str, Any], data["d"])
    item = Published(audience=audience, frame=data["f"], data=body, wg_id=wg)
    check(item)
    return BusMessage(data["i"], item, oversized)


Connect = Callable[..., psycopg.Connection[Any]]


class Bus:
    """The process's listener and sender threads (module docstring)."""

    def __init__(
        self,
        connect_kwargs: Mapping[str, Any],
        pool: ConnectionPool[psycopg.Connection[Any]],
        instance: str,
        *,
        on_event: Callable[[Appended], object],
        on_message: Callable[[BusMessage], object],
        on_gap: Callable[[], object],
        connect: Connect = psycopg.connect,
    ) -> None:
        self._connect_kwargs = dict(connect_kwargs)
        self._pool = pool
        self.instance = instance
        self._on_event = on_event
        self._on_message = on_message
        self._on_gap = on_gap
        self._connect = connect
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self.listener_pid: int | None = None

    def start(self) -> None:
        """Start the listener and the sender."""
        self._threads = [
            threading.Thread(target=self._listen, name="agent-hub-bus-listen", daemon=True),
            threading.Thread(target=self._send_loop, name="agent-hub-bus-send", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
        """Block until the listener listens (True) or the timeout passed (False)."""
        return self._ready.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop both threads (queued messages are sent first); an empty notification wakes the listener."""
        self._stop.set()
        self._queue.put(WAKE)
        self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout)
        self._threads = []

    def send(self, text: str) -> None:
        """Queue one message for `STREAM_CHANNEL`."""
        self._queue.put(text)

    def _send_loop(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            with contextlib.suppress(queue.Empty):
                while len(batch) < 100:
                    following = self._queue.get_nowait()
                    if following is None:
                        self._queue.put(None)
                        break
                    batch.append(following)
            try:
                with self._pool.connection() as connection:
                    for text in batch:
                        connection.execute("SELECT pg_notify(%s, %s)", (STREAM_CHANNEL, text))
            except Exception:
                LOG.error("live bus: %d message(s) could not be sent", len(batch), exc_info=True)

    def _dispatch(self, channel: str, payload: str) -> None:
        if payload == WAKE:
            return
        try:
            if channel == EVENT_CHANNEL:
                self._on_event(parse(payload))
                return
            message = decode(payload)
            if message.instance != self.instance:
                self._on_message(message)
        except Exception:
            LOG.error("live bus: a %s notification was not handled", channel, exc_info=True)

    def _listen(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        listened = False
        while not self._stop.is_set():
            try:
                with self._connect(**self._connect_kwargs, autocommit=True) as connection:
                    connection.execute(f"LISTEN {EVENT_CHANNEL}")
                    connection.execute(f"LISTEN {STREAM_CHANNEL}")
                    self.listener_pid = connection.info.backend_pid
                    if listened:
                        self._on_gap()
                    listened = True
                    delay = RECONNECT_MIN_SECONDS
                    self._ready.set()
                    while not self._stop.is_set():
                        for notification in connection.notifies(timeout=POLL_SECONDS):
                            self._dispatch(notification.channel, notification.payload)
            except Exception:
                LOG.error("live bus: the listening connection failed; reconnecting", exc_info=True)
                self._stop.wait(delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
