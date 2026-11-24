"""U-GST-hook-summary: routine hook records become one summary per session (ADR-086 section 3; KOR-35).

The journal of the guest fixtures (real SQLite, contract schemas, fake clock) counts ``PreToolUse`` allow and
``PostToolUse`` none and writes ``session.hook {event: "summary", ...}``; the ticker of the control process writes the
due summaries.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from agent_guest import hook_summary
from agent_guest.clock import iso_ms
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext, ControlService
from agent_guest.control_routes import hook_summary as ticker
from agent_guest.hook_summary import HookSummaries, counted
from agent_guest.journal import Journal, JournalError
from agent_guest.limits import HOOKS, HookLimits
from agent_guest.schema import SchemaSet
from tests.platform.guest.conftest import SESSION_ID, START, FakeClock

OTHER_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
PRE = {"event": "PreToolUse", "tool": "Read", "decision": "allow", "rule": 7, "path": "/srv/w/a"}
POST = {"event": "PostToolUse", "tool": "Read", "decision": "none"}
STOP = {"event": "Stop", "decision": "none"}
DENY = {"event": "PreToolUse", "tool": "Bash", "decision": "deny", "rule": 2, "reason": "no"}
STATE = {"state": "idle", "at": "2026-11-24T10:00:00.000Z"}


def records(journal: Journal) -> list[tuple[str, str | None, dict[str, Any]]]:
    found, _ = journal.read(0, 1000)
    return [(record["kind"], record["session_id"], record["body"]) for record in found]


def summary(pre: int, post: int, tools: dict[str, int], first: float, last: float) -> dict[str, Any]:
    counts = {name: n for name, n in (("PreToolUse", pre), ("PostToolUse", post)) if n}
    return {
        "event": "summary",
        "decision": "allow",
        "counts": counts,
        "tools": tools,
        "from": iso_ms(first),
        "to": iso_ms(last),
    }


@pytest.mark.parametrize(
    ("kind", "body", "expected"),
    [
        ("session.hook", PRE, True),
        ("session.hook", POST, True),
        ("session.hook", {"event": "PreToolUse", "decision": "allow"}, True),
        ("session.tool", PRE, False),
        ("session.hook", DENY, False),
        ("session.hook", {"event": "PreToolUse", "decision": "none", "reason": "fault injected"}, False),
        ("session.hook", {"event": "PostToolUse", "decision": "none", "reason": "hook body invalid"}, False),
        ("session.hook", {"event": "PostToolUse", "decision": "allow"}, False),
        ("session.hook", {"event": "UserPromptSubmit", "decision": "allow"}, False),
        ("session.hook", STOP, False),
        ("session.hook", {"event": 5, "decision": "allow"}, False),
    ],
)
def test_only_routine_calls_are_counted(kind: str, body: dict[str, Any], *, expected: bool) -> None:
    assert counted(kind, body) is expected


def test_routine_calls_become_one_summary_before_the_next_single_record(journal: Journal, clock: FakeClock) -> None:
    assert journal.append("session.hook", DENY, SESSION_ID) == 1
    assert journal.append("session.hook", PRE, SESSION_ID) == 1  # counted: the head is answered
    clock.advance(1.5)
    journal.append("session.hook", POST, SESSION_ID)
    journal.append("session.hook", {"event": "PreToolUse", "tool": "Grep", "decision": "allow"}, SESSION_ID)
    journal.append("session.hook", {"event": "PreToolUse", "decision": "allow"}, SESSION_ID)  # no tool name
    journal.append("session.hook", PRE, OTHER_ID)
    journal.append("session.terminal", {"offset_ms_start": 0, "frames": [[0, "o", "x"]], "cols": 1, "rows": 1},
                   SESSION_ID)  # fmt: skip
    assert [kind for kind, _, _ in records(journal)] == ["session.hook", "session.terminal"]
    clock.advance(0.25)
    journal.append("session.hook", STOP, SESSION_ID)
    found = records(journal)
    assert found[2:] == [
        ("session.hook", SESSION_ID, summary(3, 1, {"Read": 1, "Grep": 1}, START, START + 1.5)),
        ("session.hook", SESSION_ID, STOP),
    ]
    journal.append("session.state", STATE, OTHER_ID)  # the end of a session (any state record) writes its summary
    assert records(journal)[4:] == [
        ("session.hook", OTHER_ID, summary(1, 0, {"Read": 1}, START + 1.5, START + 1.5)),
        ("session.state", OTHER_ID, STATE),
    ]
    journal.append("session.state", STATE, SESSION_ID)  # nothing counted: no summary
    assert len(records(journal)) == 7


def test_a_summary_is_written_at_most_every_10_seconds(journal: Journal, clock: FakeClock) -> None:
    for _ in range(9):
        journal.append("session.hook", PRE, SESSION_ID)
        clock.advance(1.0)
    assert records(journal) == []
    clock.advance(1.0)
    journal.append("session.hook", POST, SESSION_ID)  # 10 s after the first: counted and written
    assert records(journal) == [("session.hook", SESSION_ID, summary(9, 1, {"Read": 9}, START, START + 10))]
    journal.append("session.hook", POST, SESSION_ID)
    assert len(records(journal)) == 1


def test_summaries_match_the_contract(journal: Journal, clock: FakeClock, schemas: SchemaSet) -> None:
    for index in range(hook_summary.MAX_TOOLS):
        journal.append("session.hook", {**PRE, "tool": f"mcp__x__tool_{index}"}, SESSION_ID)
    ((_, _, body),) = records(journal)  # the tools map is full: written at once
    assert len(body["tools"]) == hook_summary.MAX_TOOLS
    assert schemas.errors("guest/journal/session.hook.json", body) == []
    assert schemas.errors("events/session.hook.json", body) == []
    assert schemas.errors("guest/journal/session.hook.json", {**body, "decision": "deny"}) != []
    assert schemas.errors("guest/journal/session.hook.json", {**body, "counts": {"Stop": 1}}) != []


def test_due_summaries_are_written_by_the_flush(journal: Journal, clock: FakeClock) -> None:
    assert journal.flush_summaries() == 0
    assert journal.flush_summaries(every=True) == 0
    journal.append("session.hook", PRE, SESSION_ID)
    clock.advance(5)
    journal.append("session.hook", PRE, OTHER_ID)
    clock.advance(4.5)
    assert journal.flush_summaries() == 0
    clock.advance(0.5)
    assert journal.flush_summaries() == 1
    assert records(journal) == [("session.hook", SESSION_ID, summary(1, 0, {"Read": 1}, START, START))]
    assert journal.flush_summaries(every=True) == 1
    assert [session for _, session, _ in records(journal)] == [SESSION_ID, OTHER_ID]
    assert journal.summaries.sessions == []


def rolled_back(journal: Journal, body: dict[str, Any]) -> None:
    with journal.transaction() as db:
        journal.insert(db, "session.hook", body, SESSION_ID)
        raise RuntimeError("rolled back")


def test_counts_follow_the_transaction(journal: Journal, clock: FakeClock) -> None:
    with pytest.raises(RuntimeError):
        rolled_back(journal, PRE)
    assert journal.summaries.sessions == []
    journal.append("session.hook", PRE, SESSION_ID)
    journal.append("session.hook", POST, SESSION_ID)
    with pytest.raises(RuntimeError):
        rolled_back(journal, STOP)  # writes the summary, then rolls back
    assert records(journal) == []
    clock.advance(2)
    journal.append("session.hook", STOP, SESSION_ID)
    assert records(journal) == [
        ("session.hook", SESSION_ID, summary(1, 1, {"Read": 1}, START, START)),
        ("session.hook", SESSION_ID, STOP),
    ]
    summaries = HookSummaries()
    summaries.rollback()  # nothing changed: nothing restored
    assert summaries.sessions == []


def test_an_invalid_record_is_refused_before_it_is_counted(journal: Journal) -> None:
    with pytest.raises(JournalError) as error:
        journal.append("session.hook", {**PRE, "tool": "bad tool"}, SESSION_ID)
    assert error.value.code == "body_invalid"
    assert journal.summaries.sessions == []


def test_zero_seconds_turn_the_summaries_off(journal: Journal) -> None:
    journal.summaries = HookSummaries(HookLimits(summary_seconds=0))
    assert not journal.summaries.enabled
    assert journal.append("session.hook", PRE, SESSION_ID) == 1
    assert records(journal) == [("session.hook", SESSION_ID, PRE)]
    assert HOOKS.summary_seconds == 10.0
    assert HookSummaries().enabled


# -- the ticker of the control process ------------------------------------------------------------------------------


def wait_until(condition: Any) -> None:
    done = threading.Event()
    for _ in range(3000):  # up to 30 s (a loaded device), returns as soon as reached
        if condition():
            return
        done.wait(0.01)
    raise AssertionError("condition not reached")


def test_the_ticker_writes_due_summaries_until_the_journal_closes(
    tmp_path: Path, clock: FakeClock, schemas: SchemaSet
) -> None:
    journal = Journal.open(str(tmp_path / "journal.db"), clock, schemas)
    thread = threading.Thread(target=ticker.tick, args=(journal, 0.01), daemon=True)
    thread.start()
    journal.append("session.hook", PRE, SESSION_ID)
    clock.advance(10)
    wait_until(lambda: records(journal))
    journal.close()
    thread.join(30)
    assert not thread.is_alive()
    assert journal.wait_closed(30) is True  # at once


def test_a_failing_round_is_logged_and_the_next_one_runs(
    journal: Journal, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failures: list[Exception] = [JournalError("body_invalid"), RuntimeError("boom"), JournalError("unavailable")]

    def failing(every: bool = False) -> int:
        raise failures.pop(0)

    monkeypatch.setattr(journal, "flush_summaries", failing)
    ticker.tick(journal, 0)
    assert failures == []
    err = capsys.readouterr().err
    assert "hook summaries failed: body_invalid" in err
    assert "RuntimeError: boom" in err


def test_the_startup_hook_starts_the_ticker(context: ControlContext, clock: FakeClock) -> None:
    assert ticker.STARTUP is ticker.startup
    started = [thread for thread in threading.enumerate() if thread.name == "agent-guest-hook-summary"]
    assert started  # the control service ran the discovered start-up hooks
    context.journal.append("session.hook", PRE, SESSION_ID)
    clock.advance(11)
    wait_until(lambda: records(context.journal))
    assert records(context.journal)[0][2]["event"] == "summary"


def test_a_clean_stop_writes_the_counted_hook_records(guest_config: GuestConfig, clock: FakeClock) -> None:
    """Review (KOR-35): stopping the control process flushes every pending summary before the journal closes."""
    service = ControlService(guest_config, clock)
    service.start()
    assert service.context is not None
    service.context.journal.append("session.hook", PRE, SESSION_ID)
    service.stop()
    reopened = Journal.open(guest_config.journal, clock, None)
    try:
        hooks = [(session, body) for kind, session, body in records(reopened) if kind == "session.hook"]
    finally:
        reopened.close()
    assert hooks == [(SESSION_ID, summary(1, 0, {"Read": 1}, START, START))]


def test_a_failing_flush_at_stop_is_logged_and_the_stop_completes(
    guest_config: GuestConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = ControlService(guest_config, clock)
    service.start()
    assert service.context is not None
    journal = service.context.journal

    def failing(every: bool = False) -> int:
        assert every
        raise JournalError("unavailable")

    monkeypatch.setattr(journal, "flush_summaries", failing)
    service.stop()
    assert journal.wait_closed(0) is True
    assert "hook summaries at stop failed: unavailable" in capsys.readouterr().err


def test_a_hook_counted_after_the_parts_stopped_is_written_too(guest_config: GuestConfig, clock: FakeClock) -> None:
    """Review (KOR-35): the API threads still count hooks while the service stops; the flush happens under the journal
    lock as it closes, so a hook counted right before the close is in the written summary."""
    service = ControlService(guest_config, clock)
    service.start()
    assert service.context is not None
    context = service.context
    context.journal.append("session.hook", PRE, SESSION_ID)
    close_pending = context.pending.close

    def late_hook() -> None:
        context.journal.append("session.hook", PRE, SESSION_ID)  # counted after every part stopped
        close_pending()

    context.pending.close = late_hook  # type: ignore[method-assign]
    service.stop()
    reopened = Journal.open(guest_config.journal, clock, None)
    try:
        hooks = [(session, body) for kind, session, body in records(reopened) if kind == "session.hook"]
    finally:
        reopened.close()
    assert hooks == [(SESSION_ID, summary(2, 0, {"Read": 2}, START, START))]
