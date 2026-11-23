"""U-GST-journal: pending MCP calls expire (`guest.error pending_expired`) instead of growing until the VM ends
(02-data-model.md section 5 `pending`, contracts `guest/journal/record.json`; AUD-01 B-15, decisions/KOR-02.md)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_guest import control as control_module
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext, ControlService
from agent_guest.journal import PENDING_SECONDS
from agent_guest.state import Session, StateError
from tests.platform.guest.conftest import SESSION_ID, FakeClock, make_session


def errors(context: ControlContext) -> list[tuple[str | None, dict[str, Any]]]:
    found, _ = context.journal.read(after=0, limit=1000)
    return [(record["session_id"], record["body"]) for record in found if record["kind"] == "guest.error"]


def rows(context: ControlContext) -> list[tuple[str, str]]:
    with context.journal.transaction() as db:
        return [(row[0], row[1]) for row in db.execute("SELECT id, state FROM pending ORDER BY id")]


def query(context: ControlContext) -> str:
    return context.pending.create(SESSION_ID, "query", {"tool": "list_tasks", "arguments": {}})


def test_unanswered_calls_expire_and_answered_ones_are_removed(
    context: ControlContext, session: Session, clock: FakeClock
) -> None:
    waiting, answered = query(context), query(context)
    context.pending.answer(answered, {"status": "ok", "result": {}})
    assert context.journal.prune_pending() == []  # young calls stay
    clock.advance(PENDING_SECONDS - 1)
    young = query(context)
    clock.advance(1)
    assert context.journal.prune_pending() == [waiting]
    assert rows(context) == [(young, "waiting")]
    assert errors(context) == [(SESSION_ID, {"code": "pending_expired"})]
    with pytest.raises(StateError, match="not_found"):  # the runner's late answer: refused, counted as delivered
        context.pending.answer(waiting, {"status": "ok", "result": {}})
    clock.advance(PENDING_SECONDS)
    assert context.journal.prune_pending() == [young]
    assert rows(context) == []


def test_the_control_process_expires_at_start_and_in_its_prune_loop(
    guest_config: GuestConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ControlService(guest_config, clock, startup=())
    context = first.start()
    try:
        context.state.save_session(make_session())
        old = query(context)
    finally:
        first.stop()
    clock.advance(PENDING_SECONDS)
    monkeypatch.setattr(control_module, "PRUNE_SECONDS", 0.05)
    second = ControlService(guest_config, clock, startup=())
    context = second.start()
    try:
        assert errors(context) == [(SESSION_ID, {"code": "pending_expired"})]  # at start
        assert old not in [pending_id for pending_id, _ in rows(context)]
        query(context)
        clock.advance(PENDING_SECONDS)
        deadline = time.monotonic() + 30
        while rows(context) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rows(context) == []  # in the loop
        assert errors(context) == [(SESSION_ID, {"code": "pending_expired"})] * 2
    finally:
        second.stop()
