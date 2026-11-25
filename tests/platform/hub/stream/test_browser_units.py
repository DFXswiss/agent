"""U-HUB-stream (platform browser units, KOR-44): the query parameters `browser` and `browser_kbps`, the live slot of
a stream (one per watched session, a newer frame replaces one not sent, no overflow) and its byte budget, the
`browser_action` filter, and the service's edges of live frames (13-work-vm.md section 13.6)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest

from agent_hub.errors import HubError
from agent_hub.events.notify import Appended
from agent_hub.stream import browser
from agent_hub.stream.publish import BROWSER_LIVE_REF, Audience, Published
from agent_hub.stream.service import StreamService
from agent_hub.stream.sse import BrowserStream, LiveEvent, Opening, StreamQuery, live_bytes, parse_query
from tests.platform.hub.conftest import HubHarness

ORG = uuid.UUID("01a20000-0000-7000-8000-000000000001")
WG = uuid.UUID("01a20000-0000-7000-8000-000000000002")
USER = uuid.UUID("01a20000-0000-7000-8000-000000000003")
SESSION = uuid.UUID("01a20000-0000-7000-8000-000000000004")
OTHER = uuid.UUID("01a20000-0000-7000-8000-000000000005")
EVENT = {"header": {}, "ct": "", "sig": ""}
HELLO = b"event: hello\ndata: {}\n\n"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _stream(
    clock: Clock, *, kbps: int = 100, buffer: int = 10, browsers: frozenset[uuid.UUID] = frozenset({SESSION})
) -> BrowserStream:
    query = StreamQuery(ORG, frozenset(), {}, False, browsers=browsers, browser_kbps=kbps)
    return BrowserStream(USER, query, frozenset({ORG, WG}), frozenset(), buffer=buffer, browsers=browsers, clock=clock)


LOOP = asyncio.new_event_loop()
"""One loop for every take of a test (a stream's wake-up event binds to the first loop that waits on it)."""


def _take(stream: BrowserStream, timeout: float = 0.01) -> list[bytes]:
    return LOOP.run_until_complete(stream.take(timeout))


def test_query_parameters() -> None:
    first = "01a20000-0000-7000-8000-000000000010"
    second = "01a20000-0000-7000-8000-000000000011"
    org = str(ORG)
    query = parse_query(org, [], None, None, browser=[first, second], browser_kbps="10000")
    assert query.browsers == {uuid.UUID(first), uuid.UUID(second)}
    assert query.browser_kbps == 10000
    assert query.sessions == query.browsers
    assert parse_query(org, [first], None, None).browser_kbps == 1000
    assert parse_query(org, [], None, None, browser_kbps="100").browser_kbps == 100
    for kbps in ("99", "10001", "0100", "1e3", "", "abc"):
        with pytest.raises(HubError):
            parse_query(org, [], None, None, browser=[first], browser_kbps=kbps)
    too_many = [f"01a20000-0000-7000-8000-00000000002{index}" for index in range(4)]
    for browsers in (too_many, [first, first], ["nope"]):
        with pytest.raises(HubError):
            parse_query(org, [], None, None, browser=browsers)


def test_the_live_slot_waits_for_the_opening_and_keeps_the_newest_frame() -> None:
    clock = Clock()
    stream = _stream(clock)
    stream.offer_live(SESSION, b"early")
    stream.offer_live(OTHER, b"not watched")
    assert _take(stream) == []  # nothing before the opening
    stored = {SESSION: b"stored", OTHER: b"other"}
    stream.prime(Opening([HELLO], {}, stored))  # the stored frame is older than the one that came meanwhile
    assert _take(stream) == [HELLO, b"early"]
    stream.offer_live(SESSION, b"one")
    stream.offer_live(SESSION, b"two")  # replaces the frame not sent yet
    assert _take(stream) == [b"two"]
    fresh = _stream(clock)
    fresh.prime(Opening([HELLO], {}, stored))
    assert _take(fresh) == [HELLO, b"stored"]


def test_the_byte_budget_paces_the_frames() -> None:
    clock = Clock()
    stream = _stream(clock, kbps=100)  # 12500 bytes per second
    stream.prime(Opening([], {}))
    stream.offer_live(SESSION, b"a" * 12500)
    assert _take(stream) == [b"a" * 12500]  # a new stream has one second of budget
    stream.offer_live(SESSION, b"b" * 6250)
    assert _take(stream) == []  # nothing left: 0.5 s to wait
    clock.now += 0.25
    assert _take(stream) == []
    clock.now += 0.25
    assert _take(stream) == [b"b" * 6250]
    stream.offer_live(SESSION, b"c" * 25000)  # larger than one second of budget: goes once the bucket is full
    clock.now += 0.75
    assert _take(stream) == []
    clock.now += 0.25
    assert _take(stream) == [b"c" * 25000]
    stream.offer_live(SESSION, b"d")
    clock.now += 1.0
    assert _take(stream) == []  # still in debt
    clock.now += 0.5
    assert _take(stream) == [b"d"]


def test_a_held_frame_wakes_the_reader_when_its_budget_allows() -> None:
    stream = _stream(Clock(), kbps=100)
    stream.clock = time.monotonic
    stream.prime(Opening([], {}))
    stream.offer_live(SESSION, b"a" * 12000)
    assert _take(stream) == [b"a" * 12000]
    stream.offer_live(SESSION, b"b" * 1000)  # 500 left: about 0.04 s
    started = time.monotonic()
    assert LOOP.run_until_complete(_take_until(stream)) == [b"b" * 1000]
    assert time.monotonic() - started < 2  # the wait ends with the budget, long before the heartbeat timeout


async def _take_until(stream: BrowserStream) -> list[bytes]:
    for _ in range(100):
        found = await stream.take(5)
        if found:
            return found
    return []


def test_live_frames_never_overflow_and_stop_with_the_stream() -> None:
    clock = Clock()
    stream = _stream(clock, buffer=1, kbps=10000)
    stream.prime(Opening([], {}))
    for index in range(10):
        stream.offer_live(SESSION, b"x%d" % index)
    stream.offer(Published(Audience("user", USER), "wraps", {}))
    taken = _take(stream)
    assert taken[-1] == b"x9"
    assert not any(b"resync" in chunk for chunk in taken)
    stream.offer_live(SESSION, b"late")
    stream.close()
    stream.offer_live(SESSION, b"after")
    assert _take(stream) == []


def test_lost_rights_drop_the_slot_and_browser_actions_follow_the_watch() -> None:
    clock = Clock()
    stream = _stream(clock, browsers=frozenset({SESSION, OTHER}))
    stream.prime(Opening([], {WG: 0}))
    stream.offer_live(SESSION, b"one")
    stream.offer_live(OTHER, b"two")
    stream.update_rights(frozenset({ORG, WG}), frozenset(), frozenset({OTHER}))
    assert stream.browsers == {OTHER}
    assert _take(stream) == [b"two"]
    stream.update_rights(frozenset({ORG, WG}), frozenset())  # browsers unchanged
    assert stream.browsers == {OTHER}
    stream.offer_event(LiveEvent(ORG, WG, 1, "browser.action", SESSION, EVENT))  # not watched any more
    stream.offer_event(LiveEvent(ORG, WG, 2, "browser.action", OTHER, EVENT))
    [chunk] = _take(stream)
    assert chunk.startswith(b"event: browser_action\n")
    assert stream.watching() == {"terminal": frozenset(), "browser": frozenset({OTHER})}
    assert stream.rate == 12500


def test_live_bytes() -> None:
    assert live_bytes(SESSION, {"v": 1}) == (
        b'event: browser_live\ndata: {"session_id":"' + str(SESSION).encode() + b'","frame":{"v":1}}\n\n'
    )


def test_service_edges_of_live_frames(hub: HubHarness) -> None:
    service = StreamService(hub.app.state.hub)
    reference = Published(Audience("org", ORG), BROWSER_LIVE_REF, {"session_id": str(SESSION), "seq": 1}, wg_id=WG)
    service.publish(reference)  # nobody here watches the session: not read
    service._watching = {"browser": frozenset({SESSION})}
    service._read_live(reference)  # watched, but no stored frame
    service._orgs = frozenset({ORG})
    service._on_event(Appended(ORG, "wg", WG, 1, "browser.action", SESSION))  # watched kind nobody watches here
    service._on_event(Appended(ORG, "wg", WG, 1, "browser.action", None))
    stored = browser.Stored("br-0123456789ab", 1, {"v": 1})
    service._deliver_live(SESSION, stored)  # no local watcher (any more)
    assert service._live_sent == {}
    stream = _stream(Clock())
    stream.prime(Opening([], {}))
    service.browser_watchers[SESSION] = {stream}
    service._deliver_live(SESSION, stored)
    service._deliver_live(SESSION, stored)  # the same frame again (a repeated reference): dropped
    assert _take(stream) == [live_bytes(SESSION, {"v": 1})]
    assert _take(stream) == []
    assert service._wishes({SESSION, OTHER}) == {SESSION: 100, OTHER: None}


def test_a_stopping_process_drops_its_leases_and_the_frames_they_kept(hub: HubHarness) -> None:
    service = StreamService(hub.app.state.hub)
    now = hub.clock()
    later = now + timedelta(seconds=browser.LEASE_SECONDS)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.browser_watch (session_id, instance, until, kbps) VALUES (%s, %s, %s, 1000)",
            (SESSION, service.instance, later),
        )
        connection.execute(
            "INSERT INTO hub.browser_live (session_id, browser_id, seq, frame, received_at)"
            " VALUES (%s, 'br-0123456789ab', 0, '{}', %s)",
            (SESSION, now),
        )
    asyncio.run(service.stop())  # never started: no renewal to end
    with hub.pool.connection() as connection:
        rows = [
            connection.execute(f"SELECT count(*) FROM hub.{table} WHERE session_id = %s", (SESSION,)).fetchone()
            for table in ("browser_watch", "browser_live")
        ]
    assert rows == [(0,), (0,)]
