"""The job runner, the nonce purge job, internal signals and the live-publishing interface (ADR-027 section 4)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_hub import signals
from agent_hub.http.jobs import JOBS as HTTP_JOBS
from agent_hub.http.jobs import purge_request_nonces
from agent_hub.http.nonces import insert_nonce
from agent_hub.jobs import Job, JobRunner, check_jobs
from agent_hub.registry import SIGNAL_SUBSCRIBERS, RegistryError, discover
from agent_hub.stream.publish import (
    Audience,
    MemoryPublisher,
    Published,
    current_publisher,
    publish,
    set_publisher,
)
from tests.platform.hub.conftest import FIXED_NOW, FixedClock, uuid7

# Jobs.


def test_job_rules() -> None:
    for name, interval, delay in (("Bad Name", 1, 0), ("ok", 0, 0), ("ok", 1, -1)):
        with pytest.raises(ValueError, match="job"):
            Job(name, interval, lambda _context: None, delay)
    job = Job("a.b", 1, lambda _context: None)
    assert check_jobs([job]) == [job]
    with pytest.raises(TypeError, match="Job values"):
        check_jobs([object()])
    with pytest.raises(ValueError, match="registered twice"):
        check_jobs([job, Job("a.b", 2, lambda _context: None)])


def test_runner_runs_periodically_and_survives_failures(caplog: pytest.LogCaptureFixture) -> None:
    runs: list[str] = []

    def flaky(context: Any) -> None:
        runs.append(context)
        if len(runs) == 1:
            raise RuntimeError("first pass fails")

    async def scenario() -> None:
        runner = JobRunner([Job("test.flaky", 0.01, flaky)], "context")
        runner.start()
        started = time.monotonic()
        while len(runs) < 3:  # three passes, each a thread hop: by condition, not by a fixed 0.2 s
            assert time.monotonic() - started < 30, "the job did not run three times"
            await asyncio.sleep(0.01)
        await runner.stop()
        await runner.stop()

    asyncio.run(scenario())
    assert len(runs) >= 3
    assert set(runs) == {"context"}
    assert "job test.flaky failed" in caplog.text


def test_initial_delay_and_stop_before_the_first_run() -> None:
    runs: list[int] = []

    async def scenario() -> None:
        runner = JobRunner([Job("test.late", 1, lambda _context: runs.append(1), initial_delay_seconds=60)], None)
        runner.start()
        await asyncio.sleep(0.05)
        await runner.stop()

    asyncio.run(scenario())
    assert runs == []


def test_stuck_job_is_cancelled_after_the_grace_period(caplog: pytest.LogCaptureFixture) -> None:
    release = threading.Event()

    async def scenario() -> bool:
        runner = JobRunner([Job("test.stuck", 1, lambda _context: release.wait(5))], None)
        runner.start()
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await runner.stop(grace_seconds=0.1)
        release.set()
        return time.monotonic() - started < 2

    assert asyncio.run(scenario())
    assert "did not stop in time" in caplog.text


def test_run_once() -> None:
    async def scenario() -> list[bool]:
        runner = JobRunner([], None)
        ok = await runner.run_once(Job("test.ok", 1, lambda _context: None))
        failed = await runner.run_once(Job("test.fail", 1, lambda _context: 1 / 0))
        return [ok, failed]

    assert asyncio.run(scenario()) == [True, False]


def test_prepare_runs_the_at_start_jobs_once() -> None:
    ran: list[str] = []
    early = Job("test.early", 3600, lambda _context: ran.append("early"), at_start=True)
    late = Job("test.late", 3600, lambda _context: ran.append("late"))
    asyncio.run(JobRunner([early, late], None).prepare())
    assert ran == ["early"]


def test_a_failing_at_start_job_stops_the_start(caplog: pytest.LogCaptureFixture) -> None:
    broken = Job("test.broken", 3600, lambda _context: 1 / 0, at_start=True)
    with pytest.raises(RuntimeError, match=r"job test\.broken failed at start"):
        asyncio.run(JobRunner([broken], None).prepare())
    assert "job test.broken failed" in caplog.text


class PurgeContext:
    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock) -> None:
        self.pool = pool
        self.clock = clock


def test_nonce_purge_job(hub_pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock) -> None:
    assert [job.name for job in HTTP_JOBS] == ["request_nonce.purge"]
    assert insert_nonce(hub_pool, "platform-root", "n1", FIXED_NOW)
    assert insert_nonce(hub_pool, "platform-root", "n2", FIXED_NOW + timedelta(seconds=100))
    assert not insert_nonce(hub_pool, "platform-root", "n1", FIXED_NOW + timedelta(seconds=10))
    clock.advance(650)
    assert purge_request_nonces(PurgeContext(hub_pool, clock)) == 1
    assert purge_request_nonces(PurgeContext(hub_pool, clock)) == 0
    with hub_pool.connection() as connection:
        assert connection.execute("SELECT nonce FROM hub.request_nonce").fetchall() == [("n2",)]


# Signals.


@pytest.fixture
def restore_signals() -> Iterator[None]:
    yield
    signals.configure()


def test_emit_delivers_ids_after_commit_to_every_subscriber(
    restore_signals: None, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def record(name: str, ids: Any) -> None:
        seen.append((name, dict(ids)))

    def broken(_name: str, _ids: Any) -> None:
        raise RuntimeError("subscriber bug")

    signals.configure({"task.state": [broken, record], "other.thing": [record]})
    task = uuid7()
    assert signals.emit("task.state", task_id=task, seq=3, state="working") == 1
    assert seen == [("task.state", {"task_id": task, "seq": 3, "state": "working"})]
    assert "subscriber" in caplog.text
    assert signals.emit("nobody.listens", x=1) == 0


@pytest.mark.parametrize(
    ("name", "ids"),
    [
        ("NoDots", {}),
        ("task.state", {"Bad": 1}),
        ("task.state", {"flag": True}),
        ("task.state", {"title": "a prompt with spaces"}),
        ("task.state", {"value": 1.5}),
    ],
)
def test_emit_refuses_content(name: str, ids: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="signal"):
        signals.emit(name, **ids)


def test_subscriber_collection_rules() -> None:
    def subscriber(_name: str, _ids: Any) -> None: ...

    merged = signals.collect([("a", {"x.y": [subscriber]}), ("b", {"x.y": (subscriber,), "z.w": []})])
    assert merged == {"x.y": (subscriber, subscriber), "z.w": ()}
    for found, message in (
        ([("a", [subscriber])], "must be a mapping"),
        ([("a", {"bad": [subscriber]})], "lower-case dotted"),
        ([("a", {"x.y": [1]})], "must be callable"),
    ):
        with pytest.raises(RegistryError, match=message):
            signals.collect(found)


def test_subscribers_are_discovered_on_first_use(restore_signals: None) -> None:
    signals._state.clear()
    found = signals.subscribers()
    discovered = [subscriber for _area, value in discover(SIGNAL_SUBSCRIBERS) for subscriber in value["log.appended"]]
    assert list(found["log.appended"]) == discovered != []
    assert signals.emit("signals.unused", event_id=UUID(int=1)) == 0


# Live publishing interface.


def test_memory_publisher() -> None:
    fake = MemoryPublisher()
    previous = set_publisher(fake)
    try:
        assert current_publisher() is fake
        org = uuid7()
        publish(Audience("org", org), "task", {"task_id": "t1"})
        assert fake.items() == [Published(Audience("org", org), "task", {"task_id": "t1"})]
        fake.clear()
        assert fake.items() == []
    finally:
        set_publisher(previous)
