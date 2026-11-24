"""U-HUB-notifications (plumbing): the subscribers, the job that binds their context, the device-state section, and
the worker's rules - nothing before a context is bound, nothing on a closed pool, failures logged."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.device.state import sections
from agent_hub.jobs import check_jobs
from agent_hub.notifications import device_state, jobs, signals
from agent_hub.registry import JOBS, SIGNAL_SUBSCRIBERS, concat_sequences, discover
from tests.platform.hub.conftest import HubHarness


def test_the_extensions_are_registered() -> None:
    found = dict(discover(SIGNAL_SUBSCRIBERS))["notifications"]
    assert set(found) == {
        "event.appended",
        "task.state",
        "task.attention",
        "invite.accepted",
        "log.appended",
        "device.decertified",
    }
    assert "notifications.bind_context" in [job.name for job in check_jobs(concat_sequences(JOBS))]
    assert device_state.SECTION in sections()


def test_the_job_binds_the_context(hub: HubHarness) -> None:
    try:
        jobs.bind_context(hub.app.state.hub)
        assert signals._context["hub"] is hub.app.state.hub
    finally:
        signals.bind(None)


def test_nothing_is_queued_without_a_context() -> None:
    signals.bind(None)
    signals.on_task_state("task.state", {"task_id": "x", "state": "failed"})
    signals.on_event(
        "event.appended", {"kind": "whiteboard.entry", "notify": 1, "ref": str(uuid.uuid4()), "event_id": uuid.uuid4()}
    )
    assert signals._pending == []


def test_other_events_are_ignored(hub: HubHarness) -> None:
    signals.bind(hub.app.state.hub)
    try:
        signals.on_event("event.appended", {"kind": "whiteboard.entry", "notify": 0})
        signals.on_event("event.appended", {"kind": "prompt", "notify": 0})
        assert signals._pending == []
    finally:
        signals.bind(None)


def test_work_is_skipped_on_a_closed_pool_and_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    ran: list[Any] = []
    signals.bind(SimpleNamespace(pool=SimpleNamespace(closed=True)))
    signals.run(ran.append)
    assert ran == []

    def broken(_context: Any) -> None:
        raise RuntimeError("database gone")

    signals.bind(SimpleNamespace(pool=SimpleNamespace(closed=False)))
    try:
        with caplog.at_level(logging.ERROR, logger="agent_hub.notifications"):
            signals._queue(broken)
            signals.wait()
        assert "notification work failed" in caplog.text
    finally:
        signals.bind(None)
