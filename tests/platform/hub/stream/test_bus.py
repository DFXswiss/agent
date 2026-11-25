"""U-HUB-stream (bus): the `hub_stream` message format, oversized frames, own and invalid messages, the wake-up on
stop, a failing sender and the reconnect of a broken listening connection with its gap report (03-api.md section 5,
00-overview.md section 4)."""

from __future__ import annotations

import json
import queue
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from agent_core.record.connection import parse_connection_string
from agent_hub.db.pool import open_pool
from agent_hub.events.notify import Appended
from agent_hub.stream import bus as bus_module
from agent_hub.stream.bus import NOTIFY_LIMIT, STREAM_CHANNEL, Bus, BusMessage, decode, encode
from agent_hub.stream.publish import Audience, Published
from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.stream.support import wait_for

ORG = uuid.UUID("01a20000-0000-7000-8000-000000000001")


def test_encode_and_decode() -> None:
    item = Published(Audience("org", ORG), "log", {"org_id": str(ORG)}, wg_id=ORG)
    text = encode("me", item)
    assert json.loads(text) == {"i": "me", "a": ["org", str(ORG)], "f": "log", "d": {"org_id": str(ORG)}, "w": str(ORG)}
    assert decode(text) == BusMessage("me", item)
    plain = Published(Audience("device", ORG), "reconcile", {})
    assert decode(encode("me", plain)).item == plain


def test_an_oversized_frame_travels_as_a_resync() -> None:
    item = Published(Audience("user", ORG), "notification", {"pad": "x" * NOTIFY_LIMIT})
    text = encode("me", item)
    assert json.loads(text) == {"i": "me", "a": ["user", str(ORG)], "f": "notification", "w": None, "r": True}
    assert decode(text) == BusMessage("me", Published(Audience("user", ORG), "notification", {}), oversized=True)
    scoped = Published(Audience("org", ORG), "metrics", {"samples": ["x" * NOTIFY_LIMIT]}, wg_id=ORG)
    assert decode(encode("me", scoped)) == BusMessage("me", Published(Audience("org", ORG), "metrics", {}, ORG), True)


@pytest.mark.parametrize(
    "message",
    [
        [],
        {"a": ["org", str(ORG)], "r": True},
        {"i": "x", "a": ["team", str(ORG)], "r": True},
        {"i": "x", "a": ["org"], "r": True},
        {"i": "x", "a": "org", "r": True},
        {"i": "x", "a": ["org", 7], "r": True},
        {"i": "x", "a": ["org", str(ORG).upper()], "r": True},
        {"i": "x", "a": ["org", str(ORG)], "f": "task", "w": None, "r": False},
        {"i": "x", "a": ["org", str(ORG)], "f": "hello", "w": None, "r": True},
        {"i": "x", "a": ["org", str(ORG)], "f": 3, "w": None, "r": True},
        {"i": "x", "a": ["org", str(ORG)], "f": "task", "d": [], "w": None},
        {"i": "x", "a": ["org", str(ORG)], "f": 1, "d": {}, "w": None},
        {"i": "x", "a": ["org", str(ORG)], "f": "task", "d": {}},
        {"i": "x", "a": ["org", str(ORG)], "f": "hello", "d": {}, "w": None},
        {"i": "x", "a": ["org", str(ORG)], "f": "task", "d": {}, "w": "nope"},
    ],
)
def test_decode_refuses_other_messages(message: Any) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        decode(json.dumps(message))


class Recorder:
    def __init__(self) -> None:
        self.items: queue.Queue[tuple[str, Any]] = queue.Queue()

    def event(self, appended: Appended) -> None:
        self.items.put(("event", appended))

    def message(self, message: BusMessage) -> None:
        self.items.put(("message", message))

    def gap(self) -> None:
        self.items.put(("gap", None))

    def next(self, timeout: float = 5.0) -> tuple[str, Any]:
        return self.items.get(timeout=timeout)


@contextmanager
def running_bus(db: HubDatabase, recorder: Recorder, instance: str = "me", **options: Any) -> Iterator[Bus]:
    dsn = parse_connection_string(db.writer_url)
    pool = open_pool(dsn, min_size=1, max_size=2)
    bus = Bus(
        dsn.connect_kwargs(),
        pool,
        instance,
        on_event=recorder.event,
        on_message=recorder.message,
        on_gap=recorder.gap,
        **options,
    )
    bus.start()
    assert bus.wait_ready(10)
    try:
        yield bus
    finally:
        bus.stop()
        pool.close()


def _notify(db: HubDatabase, channel: str, payload: str) -> None:
    with psycopg.connect(db.writer_url, autocommit=True) as connection:
        connection.execute("SELECT pg_notify(%s, %s)", (channel, payload))


def test_messages_of_other_instances_events_and_invalid_payloads(migrated_db: HubDatabase) -> None:
    recorder = Recorder()
    with running_bus(migrated_db, recorder) as bus:
        item = Published(Audience("org", ORG), "log", {"org_id": str(ORG)})
        _notify(migrated_db, STREAM_CHANNEL, "not json")
        _notify(migrated_db, STREAM_CHANNEL, encode("me", item))  # own message: skipped
        _notify(migrated_db, STREAM_CHANNEL, "")  # wake-up: ignored
        event = {"org_id": str(ORG), "scope": "org", "scope_id": str(ORG), "scope_seq": 3, "kind": "org.profile"}
        event["session"] = None
        _notify(migrated_db, "hub_events", json.dumps(event))
        _notify(migrated_db, "hub_events", "{}")
        bus.send(encode("other", item))
        assert recorder.next() == ("event", Appended(ORG, "org", ORG, 3, "org.profile"))
        assert recorder.next() == ("message", BusMessage("other", item))
        with pytest.raises(queue.Empty):
            recorder.next(0.3)


def test_a_failing_sender_is_logged(migrated_db: HubDatabase, caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    with running_bus(migrated_db, recorder) as bus:

        class Broken:
            def connection(self) -> Any:
                raise RuntimeError("pool closed")

        bus._pool = Broken()  # type: ignore[assignment]
        bus.send("x")
        assert wait_for(lambda: "could not be sent" in caplog.text)


def test_a_broken_listener_reconnects_and_reports_the_gap(
    migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bus_module, "RECONNECT_MIN_SECONDS", 0.05)
    recorder = Recorder()
    failures = {"left": 1}

    def connect(**kwargs: Any) -> psycopg.Connection[Any]:
        if failures["left"]:
            failures["left"] -= 1
            raise psycopg.OperationalError("refused")
        return psycopg.connect(**kwargs)

    with running_bus(migrated_db, recorder, connect=connect) as bus:
        first = bus.listener_pid
        with migrated_db.admin() as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (first,))
        assert recorder.next() == ("gap", None)
        assert wait_for(lambda: bus.listener_pid not in (None, first))
        item = Published(Audience("org", ORG), "log", {})
        bus.send(encode("other", item))
        assert recorder.next() == ("message", BusMessage("other", item))
