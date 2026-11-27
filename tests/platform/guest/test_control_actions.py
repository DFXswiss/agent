"""U-GST-control: ``interrupt``/``suspend`` over the control socket, the end of a turn (Stop hook -> ``idle``) and the
subagent-stop extension point (GST-06).

Real control process, API server and prompt registry; the session manager has a fake operating system and records
tmux calls; the pane is recorded by a small fake.
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import delivery, registry, sessions, tmux
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.control_routes import control
from agent_guest.driver import ClaudeCodeDriver
from agent_guest.hook_addons import idle
from agent_guest.hooks import HookCall
from agent_guest.server import ApiContext
from agent_guest.state import StateError
from tests.platform.guest.conftest import SESSION_ID, ControlClient, FakeClock, make_session
from tests.platform.guest.hooks.conftest import HookClient, hook_body
from tests.platform.guest.sessions.conftest import FakeSystem, TmuxRunner

CONTROL = f"/v1/sessions/{SESSION_ID}/control"
CLAUDE_ID = "5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69"
OTHER_CLAUDE_ID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


@dataclass
class KeyPane:
    keys: list[tuple[str, ...]] = field(default_factory=list)
    error: BaseException | None = None

    def send_keys(self, name: str, *keys: str, literal: bool = False) -> None:
        if self.error is not None:
            raise self.error
        self.keys.append(keys)

    def capture(self, name: str) -> str:
        raise AssertionError("not used")

    def paste(self, name: str, text: str) -> None:
        raise AssertionError("not used")


@pytest.fixture
def pane(context: ControlContext) -> KeyPane:
    created = KeyPane()
    delivery.install(context, delivery.Delivery(context, created, sleep=lambda seconds: None))
    return created


@pytest.fixture
def system() -> FakeSystem:
    return FakeSystem()


@pytest.fixture
def tmux_calls() -> TmuxRunner:
    return TmuxRunner()


@pytest.fixture
def manager(
    context: ControlContext, tmp_path: Path, system: FakeSystem, tmux_calls: TmuxRunner
) -> Iterator[sessions.SessionManager]:
    created = sessions.SessionManager(
        context=context,
        system=system,
        tmux=tmux.Tmux(run=tmux_calls, scope=False),
        driver=ClaudeCodeDriver("2.1.330"),
        paths=sessions.Paths(meta_dir=str(tmp_path / "meta")),
    )
    sessions.install(context, created)
    try:
        yield created
    finally:
        created.close()


def put(context: ControlContext, state: str, claude_session_id: str | None = None) -> None:
    context.state.save_session(replace(make_session(state=state), claude_session_id=claude_session_id))


def current(context: ControlContext) -> str:
    session = context.state.session(SESSION_ID)
    assert session is not None
    return session.state


def states(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(after=0, limit=1000)
    return [
        {key: value for key, value in record["body"].items() if key != "at"}
        for record in records
        if record["kind"] == "session.state"
    ]


def post(client: ControlClient, action: str) -> tuple[int, Any]:
    reply = client.request("POST", CONTROL, {"action": action})
    return reply.status, reply.json()


# ------------------------------------------------------------------------------------------------ interrupt


def test_interrupt_sends_escape_and_ends_a_working_turn(
    context: ControlContext, control_client: ControlClient, pane: KeyPane, clock: FakeClock
) -> None:
    put(context, "working", CLAUDE_ID)
    assert post(control_client, "interrupt") == (200, {})
    assert pane.keys == [("Escape",)]
    assert current(context) == "idle"
    assert states(context) == [{"state": "idle", "claude_session_id": CLAUDE_ID}]
    # A second interrupt right away would open the rewind dialog: answered without a key.
    clock.advance(1.9)
    assert post(control_client, "interrupt") == (200, {})
    assert pane.keys == [("Escape",)]
    clock.advance(0.1)
    assert post(control_client, "interrupt") == (200, {})
    assert pane.keys == [("Escape",), ("Escape",)]
    # Already idle: no second state record.
    assert len(states(context)) == 1


@pytest.mark.parametrize("state", ["agent_starting", "prompt_delivering", "idle"])
def test_interrupt_in_other_live_states_changes_no_state(
    context: ControlContext, control_client: ControlClient, pane: KeyPane, state: str
) -> None:
    put(context, state)
    assert post(control_client, "interrupt") == (200, {})
    assert pane.keys == [("Escape",)]
    assert current(context) == state
    assert states(context) == []


@pytest.mark.parametrize("state", ["sandbox_ready", "suspended", "stopping", "stopped", "failed"])
def test_interrupt_needs_a_running_agent(
    context: ControlContext, control_client: ControlClient, pane: KeyPane, state: str
) -> None:
    put(context, state)
    assert post(control_client, "interrupt") == (409, {"error": "state_conflict"})
    assert pane.keys == []


def test_interrupt_without_a_pane_is_a_state_conflict(
    context: ControlContext, control_client: ControlClient, pane: KeyPane
) -> None:
    put(context, "working")
    pane.error = tmux.TmuxError("no such session")
    assert post(control_client, "interrupt") == (409, {"error": "state_conflict"})
    assert current(context) == "working"


def test_interrupt_of_an_unknown_session(context: ControlContext, control_client: ControlClient) -> None:
    assert post(control_client, "interrupt") == (404, {"error": "not_found"})
    with pytest.raises(StateError, match="not_found"):
        control.interrupt(context, SESSION_ID)


# ------------------------------------------------------------------------------------------------ suspend


@pytest.mark.usefixtures("manager")
def test_suspend_stops_the_unit_and_keeps_the_session(
    context: ControlContext,
    control_client: ControlClient,
    pane: KeyPane,
    system: FakeSystem,
    tmux_calls: TmuxRunner,
) -> None:
    put(context, "idle", CLAUDE_ID)
    assert post(control_client, "suspend") == (200, {})
    assert current(context) == "suspended"
    assert system.stopped == [f"agent-session-{SESSION_ID}"] * 2
    assert tmux_calls.subcommands() == ["kill-session"]
    assert states(context) == [{"state": "suspended"}]
    assert pane.keys == []
    # Repeating it answers {} and changes nothing.
    assert post(control_client, "suspend") == (200, {})
    assert len(states(context)) == 1


@pytest.mark.parametrize("state", ["sandbox_ready", "agent_starting", "prompt_delivering", "working", "stopped"])
def test_suspend_only_from_idle(
    context: ControlContext, control_client: ControlClient, pane: KeyPane, manager: sessions.SessionManager, state: str
) -> None:
    put(context, state)
    assert post(control_client, "suspend") == (409, {"error": "state_conflict"})
    assert current(context) == state


def test_suspend_rechecks_the_state_under_the_pane_lock(
    context: ControlContext, pane: KeyPane, manager: sessions.SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(context, "idle")
    engine = delivery.manager(context)
    lock = engine.pane_lock(SESSION_ID)

    class Racing:
        def __enter__(self) -> None:
            lock.acquire()
            put(context, "working")  # a delivery took the session first

        def __exit__(self, *exc: object) -> None:
            lock.release()

    monkeypatch.setattr(engine, "pane_lock", lambda session_id: Racing())
    with pytest.raises(StateError, match="state_conflict"):
        control.suspend(context, SESSION_ID)
    assert current(context) == "working"


# ------------------------------------------------------------------------------------------------ turn end


def test_turn_ended_makes_a_working_session_idle(
    context: ControlContext, api_client: ApiClient, manager: sessions.SessionManager
) -> None:
    put(context, "working")
    answer = api_client.call(
        "session.turn_ended", {"session_id": SESSION_ID, "claude_session_id": CLAUDE_ID, "failed": False}
    )
    assert answer == {"state": "idle"}
    assert states(context) == [{"state": "idle", "claude_session_id": CLAUDE_ID}]
    session = context.state.session(SESSION_ID)
    assert session is not None
    assert session.claude_session_id == CLAUDE_ID
    # A second Stop (or one after an interrupt) changes nothing.
    answer = api_client.call(
        "session.turn_ended", {"session_id": SESSION_ID, "claude_session_id": OTHER_CLAUDE_ID, "failed": False}
    )
    assert answer == {"state": "idle"}
    assert len(states(context)) == 1


def test_turn_ended_without_a_claude_id_keeps_the_known_one(
    context: ControlContext, api_client: ApiClient, manager: sessions.SessionManager
) -> None:
    put(context, "working", CLAUDE_ID)
    assert api_client.call(
        "session.turn_ended", {"session_id": SESSION_ID, "claude_session_id": None, "failed": False}
    ) == {"state": "idle"}
    assert states(context) == [{"state": "idle", "claude_session_id": CLAUDE_ID}]
    put(context, "working")
    control.mark_idle(context, SESSION_ID, None)
    assert states(context)[-1] == {"state": "idle"}


@pytest.mark.parametrize(
    "args",
    [
        {"session_id": SESSION_ID},
        {"session_id": SESSION_ID, "claude_session_id": None},
        {"session_id": 1, "claude_session_id": None, "failed": False},
        {"session_id": SESSION_ID, "claude_session_id": "not-a-uuid", "failed": False},
        {"session_id": SESSION_ID, "claude_session_id": 7, "failed": False},
        {"session_id": SESSION_ID, "claude_session_id": None, "failed": "no"},
        {"session_id": SESSION_ID, "claude_session_id": None, "failed": False, "extra": 1},
        # FIX-02: an error class only with a failed turn, and only one of the list
        {"session_id": SESSION_ID, "claude_session_id": None, "failed": False, "stop_error": "rate_limit"},
        {"session_id": SESSION_ID, "claude_session_id": None, "failed": True, "stop_error": "weird"},
        {"session_id": SESSION_ID, "claude_session_id": None, "failed": True, "stop_error": 7},
    ],
)
def test_turn_ended_refuses_invalid_arguments(context: ControlContext, api_client: ApiClient, args: Any) -> None:
    put(context, "working")
    with pytest.raises(ApiSockError, match="request_invalid"):
        api_client.call("session.turn_ended", args)
    assert current(context) == "working"


def test_turn_ended_of_an_unknown_session(api_client: ApiClient) -> None:
    with pytest.raises(ApiSockError, match="not_found"):
        api_client.call("session.turn_ended", {"session_id": SESSION_ID, "claude_session_id": None, "failed": False})


def write_meta(manager: sessions.SessionManager, depth: object) -> None:
    identity = {"task_id": "t", "parent_session_id": None, "depth": depth}
    manager._write_meta(SESSION_ID, {"identity": identity, "stopped_at": None})


def fake_module(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    module = types.ModuleType("agent_guest.control_routes.zz_subagents")
    module.SUBAGENT_STOP = value  # type: ignore[attr-defined]
    original = registry.modules
    monkeypatch.setattr(registry, "modules", lambda package: [*original(package), module])


def test_a_subagent_stop_runs_the_completion_hooks(
    context: ControlContext,
    manager: sessions.SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def broken(ctx: ControlContext, session_id: str) -> None:
        raise RuntimeError("bug")

    def record(ctx: ControlContext, session_id: str) -> None:
        seen.append(session_id)

    fake_module(monkeypatch, [broken, record])
    put(context, "working")
    write_meta(manager, 1)
    assert control.turn_ended(context, {"session_id": SESSION_ID, "claude_session_id": None, "failed": False}) == {
        "state": "idle"
    }
    assert seen == [SESSION_ID]
    assert "subagent stop handler failed" in capsys.readouterr().err
    # Only the transition runs them (a repeated Stop does not).
    control.turn_ended(context, {"session_id": SESSION_ID, "claude_session_id": None, "failed": False})
    assert seen == [SESSION_ID]


def test_a_failed_subagent_turn_is_no_completion(
    context: ControlContext, manager: sessions.SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    fake_module(monkeypatch, [lambda ctx, session_id: seen.append(session_id)])
    put(context, "working")
    write_meta(manager, 1)
    answer = control.turn_ended(context, {"session_id": SESSION_ID, "claude_session_id": None, "failed": True})
    assert answer == {"state": "idle"}
    assert seen == []


@pytest.mark.parametrize("depth", [0, None, "1", True])
def test_main_sessions_run_no_completion_hooks(
    context: ControlContext, manager: sessions.SessionManager, monkeypatch: pytest.MonkeyPatch, depth: object
) -> None:
    seen: list[str] = []
    fake_module(monkeypatch, (lambda ctx, session_id: seen.append(session_id),))
    put(context, "working")
    write_meta(manager, depth)
    control.turn_ended(context, {"session_id": SESSION_ID, "claude_session_id": None, "failed": False})
    assert seen == []
    # Without metadata the session counts as a main session as well.
    put(context, "working")
    Path(manager._meta_file(SESSION_ID)).unlink()
    control.turn_ended(context, {"session_id": SESSION_ID, "claude_session_id": None, "failed": False})
    assert seen == []


@pytest.mark.parametrize("value", [{"x": print}, ["not callable"], "text"])
def test_invalid_subagent_stop_registrations_are_refused(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    fake_module(monkeypatch, value)
    with pytest.raises(registry.RegistryError, match="SUBAGENT_STOP"):
        control.subagent_stop_handlers()


def test_no_module_registers_a_subagent_stop_yet() -> None:
    assert control.subagent_stop_handlers() == []


# ------------------------------------------------------------------------------------------------ Stop hook addon


@pytest.fixture
def hooks(api_port: int) -> HookClient:
    return HookClient(api_port)


def test_the_stop_hook_ends_the_turn(
    context: ControlContext, hooks: HookClient, manager: sessions.SessionManager, schemas: Any
) -> None:
    put(context, "working")
    assert hooks.post("Stop", hook_body("Stop", session_id=CLAUDE_ID)).json() == {}
    assert current(context) == "idle"
    records, _ = context.journal.read(after=0, limit=100)
    assert [record["kind"] for record in records] == ["session.hook", "session.state"]
    for record in records:
        assert not schemas.errors(f"guest/journal/{record['kind']}.json", record["body"])
    assert records[1]["body"]["claude_session_id"] == CLAUDE_ID


def test_stop_failure_ends_the_turn_too(
    context: ControlContext, hooks: HookClient, manager: sessions.SessionManager, schemas: Any
) -> None:
    put(context, "working")
    body = hook_body("StopFailure", session_id="not-a-uuid", error="rate_limit")
    assert hooks.post("StopFailure", body).status == 200
    assert current(context) == "idle"
    # FIX-02: the error class goes with the idle state, so the runner can continue the turn
    assert states(context) == [{"state": "idle", "stop_error": "rate_limit"}]
    records, _ = context.journal.read(after=0, limit=100)
    state = [record for record in records if record["kind"] == "session.state"]
    assert not schemas.errors("guest/journal/session.state.json", state[0]["body"])


def test_an_unreadable_stop_body_still_ends_the_turn(
    context: ControlContext, hooks: HookClient, manager: sessions.SessionManager
) -> None:
    put(context, "working")
    assert hooks.post("Stop", raw=b"{not json").json() == {}
    assert states(context) == [{"state": "idle"}]


def test_the_addon_calls_the_control_process(context: ControlContext) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Control:
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            calls.append((op, args))
            return {"state": "idle"}

    api = ApiContext(config=context.config, schemas=context.schemas, control=Control())  # type: ignore[arg-type]
    cases: list[tuple[str, dict[str, Any] | None]] = [
        ("Stop", {"session_id": CLAUDE_ID}),
        ("Stop", {"session_id": 5, "error": "rate_limit"}),
        ("StopFailure", None),
        ("StopFailure", {"session_id": CLAUDE_ID, "error": "overloaded"}),
        ("StopFailure", {"error": "something_new"}),
        ("StopFailure", {"error": 429}),
    ]
    for event, body in cases:
        call = HookCall(event=event, session_id=SESSION_ID, body=body, decision="none", decorate=False)
        assert idle.handle(api, call) is None
    base = {"session_id": SESSION_ID}
    assert calls == [
        ("session.turn_ended", {**base, "claude_session_id": CLAUDE_ID, "failed": False}),
        ("session.turn_ended", {**base, "claude_session_id": None, "failed": False}),
        ("session.turn_ended", {**base, "claude_session_id": None, "failed": True, "stop_error": "unknown"}),
        ("session.turn_ended", {**base, "claude_session_id": CLAUDE_ID, "failed": True, "stop_error": "overloaded"}),
        ("session.turn_ended", {**base, "claude_session_id": None, "failed": True, "stop_error": "unknown"}),
        ("session.turn_ended", {**base, "claude_session_id": None, "failed": True, "stop_error": "unknown"}),
    ]
    assert idle.ADDON.events == frozenset({"Stop", "StopFailure"})
    assert json.dumps(sorted(idle.ADDON.events)) == '["Stop", "StopFailure"]'
