"""U-GST-sessions: the reload of an idle session's agent with the main orchestrator's second MCP server (05-runner.md
section 17.5): ``--resume`` of its conversation, the further ``--mcp-config``, a new token, the notice sentence once,
nothing journaled; and every case in which there is nothing to reload."""

from __future__ import annotations

import dataclasses
import json
import types
from pathlib import Path
from typing import Any

import pytest

from agent_guest import harness, registry, sessions
from agent_guest.control import ControlContext
from tests.platform.guest.sessions.conftest import RESUME, SESSION, FakeSystem, TmuxRunner, request

CONFIG = "/opt/agent-guest/mcp-orchestrator.json"
SENTENCE = "You are now the user's main orchestrator."


def set_session(context: ControlContext, **changes: Any) -> None:
    current = context.state.session(SESSION)
    assert current is not None
    context.state.save_session(dataclasses.replace(current, **changes))


def launched_argv(session_paths: sessions.Paths) -> list[str]:
    launch = json.loads(Path(session_paths.launch_dir, f"{SESSION}.json").read_text())
    argv: list[str] = launch["argv"]
    return argv


def meta(session_paths: sessions.Paths) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(Path(session_paths.meta_dir, f"{SESSION}.json").read_text())
    return value


def started_idle(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.create(request())
    assert manager.start(SESSION)
    set_session(context, state="idle", claude_session_id=RESUME)


def test_reload_resumes_the_conversation_with_the_second_server(
    manager: sessions.SessionManager,
    context: ControlContext,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
    fake_system: FakeSystem,
) -> None:
    events: list[str] = []
    module = types.ModuleType("agent_guest.control_routes.fake_reload_events")
    module.SESSION_EVENTS = {  # type: ignore[attr-defined]
        "agent_starting": lambda ctx, sid: events.append("agent_starting"),
        "agent_ended": lambda ctx, sid: events.append("agent_ended"),
    }
    started_idle(manager, context)
    first = context.state.session(SESSION)
    assert first is not None
    first_run = manager._running[SESSION]
    original = registry.modules
    try:
        registry.modules = lambda package: [*original(package), module]
        assert sessions.reload_session(context, SESSION, (CONFIG,), SENTENCE)
    finally:
        registry.modules = original
    assert events == ["agent_ended", "agent_starting"]
    second = context.state.session(SESSION)
    assert second is not None
    assert second.token_sha256 != first.token_sha256
    assert second.state == "idle"
    assert first_run.stopped
    assert f"agent-session-{SESSION}" in fake_system.stopped
    assert tmux_runner.subcommands().count("new-session") == 2
    run = manager._running[SESSION].start
    assert run is not None
    assert (run.resume, run.mcp_configs) == (RESUME, (CONFIG,))
    assert meta(session_paths)["notice"]["sentences"] == [SENTENCE]
    assert sessions.take_notice(context, SESSION) == harness.NOTICE_PREFIX + SENTENCE


def test_reload_adds_the_sentence_to_a_pending_notice(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    started_idle(manager, context)
    current = meta(session_paths)
    current["notice"] = {"sentences": ["The session moved."], "facts": []}
    Path(session_paths.meta_dir, f"{SESSION}.json").write_text(json.dumps(current))
    assert manager.reload(SESSION, (CONFIG,), SENTENCE)
    assert meta(session_paths)["notice"]["sentences"] == ["The session moved.", SENTENCE]


def test_reload_only_for_an_idle_session_with_a_conversation(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    assert not manager.reload(SESSION, (CONFIG,), SENTENCE)  # unknown
    manager.create(request())
    manager.start(SESSION)
    assert not manager.reload(SESSION, (CONFIG,), SENTENCE)  # starting, not idle
    set_session(context, state="idle")
    assert not manager.reload(SESSION, (CONFIG,), SENTENCE)  # no conversation yet
    set_session(context, claude_session_id=RESUME)
    manager._stopping.add(SESSION)
    assert not manager.reload(SESSION, (CONFIG,), SENTENCE)
    manager._stopping.discard(SESSION)
    assert manager.reload(SESSION, (CONFIG,), SENTENCE)


def test_reload_stops_when_the_session_turned_meanwhile(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_idle(manager, context)
    monkeypatch.setattr(fake_system, "stop_unit", lambda name: set_session(context, state="working"))
    assert not manager.reload(SESSION, (CONFIG,), SENTENCE)


def test_the_reloaded_command_line_carries_both_configurations(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    started_idle(manager, context)
    assert manager.reload(SESSION, (CONFIG,), SENTENCE)
    argv = launched_argv(session_paths)
    index = argv.index("--mcp-config")
    assert argv[index + 1 : index + 3] == ["/opt/agent-guest/mcp.json", CONFIG]
    assert argv[argv.index("--resume") + 1] == RESUME
