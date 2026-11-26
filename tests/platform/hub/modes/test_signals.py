"""U-HUB-modes: a mode whose chooser lost write access is switched off (`chooser_left`, ADR-098 section 3), from the
`log.appended` signal of the membership log."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.device import signals as device_signals
from agent_hub.modes import signals
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.tasks.support import audit_rows, frames


@pytest.fixture
def bound(ops: Ops) -> Iterator[None]:
    previous = device_signals.bound()
    device_signals.bind(ops.hub.app.state.hub)
    yield
    signals.wait()
    device_signals._context.pop("hub", None)
    if previous is not None:
        device_signals.bind(previous)


def _on(ops: Ops) -> str:
    task, _session = ops.session()
    response = ops.scene.writer.browser.put(f"/v1/tasks/{task}/modes/test", json={})
    assert response.status_code == 200, response.text
    return task


def _narrow(ops: Ops) -> None:
    """The writer keeps only read access (as a `grant` with `read` projects it)."""
    ops.desk.execute(
        "UPDATE hub.wg_access SET access = 'read' WHERE wg_id = %s AND user_id = %s", ops.desk.wg, ops.scene.writer.id
    )


def test_a_mode_stays_while_its_chooser_may_write(ops: Ops, bound: None) -> None:
    task = _on(ops)
    signals.on_log_appended("log.appended", {"org_id": ops.desk.org, "seq": 1})
    signals.wait()
    assert audit_rows(ops.desk, "task.mode_off") == []
    assert ops.desk.rows("SELECT state FROM hub.task_mode WHERE task_id = %s", task) == [("on",)]


def test_the_mode_goes_off_when_the_chooser_loses_write_access(
    ops: Ops, bound: None, published: MemoryPublisher
) -> None:
    task = _on(ops)
    _narrow(ops)
    published.clear()
    signals.on_log_appended("log.appended", {"org_id": ops.desk.org, "seq": 1})
    signals.wait()
    [row] = audit_rows(ops.desk, "task.mode_off")
    assert (row[0], row[1]) == ("hub", None)
    assert row[3] == {"mode": "test", "reason": "chooser_left", "chosen_by": ops.scene.writer.id, "operation_id": None}
    assert ops.desk.rows("SELECT state, reason FROM hub.task_mode WHERE task_id = %s", task) == [
        ("off", "chooser_left")
    ]
    assert [frame.get("modes") for frame in frames(published, "task")] == [None]


def test_nothing_happens_without_a_bound_context(ops: Ops) -> None:
    previous = device_signals._context.pop("hub", None)
    try:
        _on(ops)
        _narrow(ops)
        signals.on_log_appended("log.appended", {"org_id": ops.desk.org, "seq": 1})
        signals.wait()
        assert audit_rows(ops.desk, "task.mode_off") == []
    finally:
        if previous is not None:
            device_signals.bind(previous)


def test_a_closed_pool_and_a_failure_are_harmless(caplog: pytest.LogCaptureFixture) -> None:
    signals.run(SimpleNamespace(pool=SimpleNamespace(closed=True)), uuid.uuid4())

    class Broken:
        closed = False

        def connection(self) -> Any:
            raise RuntimeError("down")

    signals.run(SimpleNamespace(pool=Broken(), clock=lambda: None), uuid.uuid4())
    assert "could not be switched off" in caplog.text
