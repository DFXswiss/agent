"""U-HUB-outbox (pacing, KOR-13, ADR-048): the push pacer is a token bucket per device with the rate of one push per
`MIN_INTERVAL_SECONDS` and a reserve of `BURST` pushes. A runner that paces start to start (agent
`agent_runner.sync.push`) never meets a `429`, however the way bunches its arrivals; a device that pushes faster for
good is held to the rate."""

from __future__ import annotations

import random
from uuid import UUID

from agent_hub.http.limits import DEVICE_PER_MINUTE
from agent_hub.outbox.pacing import BURST, MAX_DEVICES, MIN_INTERVAL_SECONDS, RETRY_AFTER_SECONDS, Pacer
from tests.platform.hub.conftest import FixedClock
from tests.platform.hub.orgs.support import new_id


def _arrivals(pacer: Pacer, clock: FixedClock, device: UUID, at: list[float]) -> list[int | None]:
    """Admit pushes of `device` that arrive at the offsets `at` (seconds, ascending) from now."""
    answers: list[int | None] = []
    elapsed = 0.0
    for offset in at:
        clock.advance(offset - elapsed)
        elapsed = offset
        answers.append(pacer.admit(device))
    return answers


def test_a_runner_paced_start_to_start_never_sees_429_despite_jitter() -> None:
    clock, device = FixedClock(), UUID(new_id())
    pacer = Pacer(clock)
    jitter = 0.99 * (BURST - 1) * MIN_INTERVAL_SECONDS  # about the largest spread of travel times the reserve absorbs
    rng = random.Random(13)
    starts = [k * MIN_INTERVAL_SECONDS for k in range(2000)]
    arrivals = sorted(start + rng.uniform(0.0, jitter) for start in starts)
    assert _arrivals(pacer, clock, device, arrivals) == [None] * len(arrivals)


def test_a_bunched_arrival_after_a_stalled_push_is_admitted() -> None:
    """One push is held up on the way (here 0.9 s at a 100 ms interval: a stall of the wireless link); the runner sends
    nothing while it is on the way, then the next pushes follow at once and arrive close together."""
    clock, device = FixedClock(), UUID(new_id())
    pacer = Pacer(clock)
    steady = [k * MIN_INTERVAL_SECONDS for k in range(50)]
    stalled = steady[-1] + MIN_INTERVAL_SECONDS + (BURST - 1) * MIN_INTERVAL_SECONDS
    bunch = [stalled + k * 0.001 for k in range(1, BURST)]
    assert _arrivals(pacer, clock, device, [*steady, stalled, *bunch]) == [None] * (len(steady) + 1 + len(bunch))


def test_a_device_pushing_faster_for_good_is_held_to_the_rate() -> None:
    clock, device = FixedClock(), UUID(new_id())
    pacer = Pacer(clock)
    seconds = 60.0
    step = MIN_INTERVAL_SECONDS / 4
    answers = _arrivals(pacer, clock, device, [k * step for k in range(int(seconds / step))])
    admitted = answers.count(None)
    assert admitted <= BURST + seconds / MIN_INTERVAL_SECONDS + 1
    assert admitted >= seconds / MIN_INTERVAL_SECONDS
    assert set(answers) == {None, RETRY_AFTER_SECONDS}


def test_the_reserve_does_not_grow_beyond_its_size_and_refused_pushes_spend_nothing() -> None:
    clock, device = FixedClock(), UUID(new_id())
    pacer = Pacer(clock)
    clock.advance(3600)
    assert [pacer.admit(device) for _ in range(BURST + 1)] == [None] * BURST + [RETRY_AFTER_SECONDS]
    assert [pacer.admit(device) for _ in range(5)] == [RETRY_AFTER_SECONDS] * 5
    clock.advance(1.01 * MIN_INTERVAL_SECONDS)  # one push's worth (timestamps are floats)
    assert pacer.admit(device) is None
    assert pacer.admit(device) == RETRY_AFTER_SECONDS


def test_a_clock_that_steps_back_grants_nothing() -> None:
    clock, device = FixedClock(), UUID(new_id())
    pacer = Pacer(clock)
    assert [pacer.admit(device) for _ in range(BURST)] == [None] * BURST
    clock.advance(-60)
    assert pacer.admit(device) == RETRY_AFTER_SECONDS
    clock.advance(1.01 * MIN_INTERVAL_SECONDS)  # one push's worth (timestamps are floats)
    assert pacer.admit(device) is None


def test_devices_are_paced_apart_and_the_number_kept_is_bounded() -> None:
    clock = FixedClock()
    pacer = Pacer(clock, max_devices=3)
    devices = [UUID(new_id()) for _ in range(4)]
    for device in devices[:3]:
        assert [pacer.admit(device) for _ in range(BURST)] == [None] * BURST
        assert pacer.admit(device) == RETRY_AFTER_SECONDS
    assert len(pacer) == 3
    assert pacer.admit(devices[3]) is None  # drops the least recently seen device (devices[0])
    assert len(pacer) == 3
    assert pacer.admit(devices[0]) is None  # forgotten: a full reserve again (drops devices[1])
    assert pacer.admit(devices[1]) is None  # kept, it would still be empty
    assert MAX_DEVICES == 100_000


def test_the_push_rate_stays_well_inside_the_general_device_limit() -> None:
    """03-api.md section 1: 6000 requests per minute and device key. Pushes at the full rate plus the reserve leave
    more than half of it to the other device requests (state, pulls, inbox, requests; heartbeats go over the socket)."""
    pushes_per_minute = 60 / MIN_INTERVAL_SECONDS + BURST
    assert pushes_per_minute <= DEVICE_PER_MINUTE / 2
