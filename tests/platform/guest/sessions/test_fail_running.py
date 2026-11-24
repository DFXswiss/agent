"""U-GST-sessions: the guest ends a live session itself (``SessionManager.fail_running``, ADR-064 section 2) and the
api.sock ops of the fail-closed hook path (``control_routes/turns.py``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.control_routes import turns
from agent_guest.sandbox import unit
from agent_guest.state import StateError
from tests.platform.guest.sessions.conftest import SESSION, FakeSystem, TmuxRunner, request
from tests.platform.guest.sessions.test_sessions import failure, finish, records, states, wait_for


def started(manager: sessions.SessionManager) -> None:
    manager.create(request())
    manager.start(SESSION)


def test_a_live_session_ends_failed_with_the_reason(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem, tmux_runner: TmuxRunner
) -> None:
    started(manager)
    assert manager.fail_running(SESSION, "hook_unavailable") is True
    [failed] = [record for record in records(context) if record["state"] == "failed"]
    assert failed == {"state": "failed", "reason": "hook_unavailable", "at": failed["at"]}
    assert unit.UNIT_PREFIX + SESSION in fake_system.stopped
    assert "kill-session" in tmux_runner.subcommands()
    # the pane ends afterwards: the watcher reports nothing more (no agent_exited on top)
    finish(manager, 0)
    wait_for(lambda: SESSION not in manager._running)
    assert states(context).count("failed") == 1


def test_an_ended_unknown_or_stopping_session_is_left_alone(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    assert manager.fail_running(SESSION, "hook_unavailable") is False  # unknown
    started(manager)
    manager._stopping.add(SESSION)
    assert manager.fail_running(SESSION, "hook_unavailable") is False
    manager._stopping.discard(SESSION)
    assert manager.fail_running(SESSION, "hook_unavailable") is True
    assert manager.fail_running(SESSION, "hook_unavailable") is False  # ended already
    assert fake_system.stopped.count(unit.UNIT_PREFIX + SESSION) == 1


def test_a_session_that_is_not_running_ends_without_a_watcher(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    manager.create(request())  # sandbox_ready: no agent started yet
    assert manager.fail_running(SESSION, "hook_unavailable") is True
    assert "failed" in states(context)


# ---------------------------------------------------------------- the api.sock ops


def golden(tmp_path: Path) -> str:
    path = tmp_path / "install.json"
    path.write_text('{"test_mode": true}', encoding="utf-8")
    return str(path)


def test_a_turn_is_confirmed_while_the_session_works(manager: sessions.SessionManager, context: ControlContext) -> None:
    assert turns.check(context, {"session_id": SESSION}) == {"confirmed": False}  # unknown
    started(manager)
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(replace(session, state="idle"))
    assert turns.check(context, {"session_id": SESSION}) == {"confirmed": False}
    context.state.save_session(replace(session, state="working"))
    assert turns.check(context, {"session_id": SESSION}) == {"confirmed": True}


def test_a_failed_decision_hook_ends_the_session(manager: sessions.SessionManager, context: ControlContext) -> None:
    started(manager)
    assert turns.hook_failed(context, {"session_id": SESSION, "event": "Stop"}) == {"ended": True}
    assert failure(context) == "hook_unavailable"
    assert turns.hook_failed(context, {"session_id": SESSION, "event": "PreToolUse"}) == {"ended": False}


def test_the_switch_open_suppresses_the_ops(
    manager: sessions.SessionManager, context: ControlContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(manager)
    open_config = replace(context.config, fault_hooks="open", golden_install_file=golden(tmp_path))
    monkeypatch.setattr(context, "config", open_config)
    assert turns.check(context, {"session_id": "unknown"}) == {"confirmed": True}
    assert turns.hook_failed(context, {"session_id": SESSION, "event": "UserPromptSubmit"}) == {"ended": False}
    assert "failed" not in states(context)


@pytest.mark.parametrize(
    ("op", "args"),
    [
        (turns.check, {}),
        (turns.check, {"session_id": 1}),
        (turns.check, {"session_id": SESSION, "extra": True}),
        (turns.hook_failed, {"session_id": SESSION}),
        (turns.hook_failed, {"session_id": SESSION, "event": "Notification"}),
    ],
)
def test_malformed_arguments_are_refused(context: ControlContext, op: Any, args: dict[str, Any]) -> None:
    with pytest.raises(StateError) as error:
        op(context, args)
    assert error.value.code == "request_invalid"


def test_the_ops_are_registered() -> None:
    assert set(turns.API_OPS) == {"turn.check", "session.hook_failed"}
    assert frozenset({"UserPromptSubmit", "PreToolUse", "Stop", "StopFailure"}) == turns.DECISION_EVENTS
