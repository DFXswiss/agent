"""U-GST-orchestrator: the main orchestrator in a session, guest side (05-runner.md section 17; ADR-082 sections 6 and
9.7): keys and handover for one running main session, the claim in the background, the relay as a pending call, the
second MCP server only for the claimed orchestrator, and the end with the session."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_guest import orchestrator, sessions
from agent_guest.control import ControlContext
from agent_guest.journal import JournalError
from agent_guest.orchestrator import LinuxTool, Orchestrator, OrchestratorError, Record, ToolFailed
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.orchestrator.conftest import (
    HANDOVER,
    KEYS,
    KEYS_INPUT,
    OTHER_ID,
    PAIRING_ID,
    FakeTool,
    Reloads,
    Ticks,
    add_session,
    set_state,
)

RELAY = {
    "method": "POST",
    "target": "/v1/agent/claim",
    "headers": {
        "Agent-Key": "agent.0193000a00007000800000000000000b",
        "Agent-Time": "2026-11-25T10:00:00Z",
        "Agent-Nonce": "AAECAwQFBgcICQoLDA0ODw",
        "Agent-Signature": "A" * 86,
        "Content-Type": "application/json",
    },
    "body": "e30",
}
ANSWER = {"status": 200, "headers": {"Content-Type": "application/json"}, "body": "e30"}


def code(error: pytest.ExceptionInfo[OrchestratorError]) -> str:
    return error.value.code


def claimed(orch: Orchestrator, context: ControlContext, session: Session) -> None:
    set_state(context, "idle")
    orch.keys(SESSION_ID, KEYS_INPUT)
    orch.handover(SESSION_ID, HANDOVER)


# -- keys ------------------------------------------------------------------------------------------------------


def test_keys_are_drawn_by_the_tool_and_recorded(orch: Orchestrator, tool: FakeTool, session: Session) -> None:
    assert orch.keys(SESSION_ID, KEYS_INPUT) == KEYS
    assert tool.calls == [("pair", "--session")]
    assert tool.inputs == [{"session_id": SESSION_ID, **KEYS_INPUT}]
    assert orch.record() == Record(SESSION_ID, "keys", KEYS)
    # a repeat before the handover draws fresh keys (the hub had no live pairing any more, review KOR-57 b no. 2)
    tool.keys = {**tool.keys, "pairing_id": OTHER_ID}
    assert orch.keys(SESSION_ID, KEYS_INPUT)["pairing_id"] == OTHER_ID
    assert len(tool.calls) == 2
    record = orch.record()
    assert record is not None
    assert record.pairing_id == OTHER_ID


def test_keys_only_for_an_active_running_main_session(
    orch: Orchestrator, context: ControlContext, session: Session, depths: dict[str, int | None]
) -> None:
    with pytest.raises(OrchestratorError) as error:
        orch.keys(OTHER_ID, KEYS_INPUT)
    assert code(error) == "not_found"
    set_state(context, "stopped")
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "not_found"
    set_state(context, "agent_starting")
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "state_conflict"
    set_state(context, "working")
    depths[SESSION_ID] = 1  # a subagent
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "state_conflict"
    assert orch.record() is None


def test_one_orchestrator_per_vm(orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session) -> None:
    add_session(context)
    orch.keys(SESSION_ID, KEYS_INPUT)
    with pytest.raises(OrchestratorError) as error:
        orch.keys(OTHER_ID, KEYS_INPUT)
    assert code(error) == "state_conflict"
    set_state(context, "stopped")  # the first one ended: its keys go, the other session may draw
    assert orch.keys(OTHER_ID, KEYS_INPUT) == KEYS
    assert tool.stops == 1
    record = orch.record()
    assert record is not None
    assert record.session_id == OTHER_ID


def test_keys_after_the_handover_only_once_the_claim_failed(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    claimed(orch, context, session)
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "state_conflict"
    orch._save(Record(SESSION_ID, "failed", KEYS, "x"))
    assert orch.keys(SESSION_ID, KEYS_INPUT) == KEYS
    assert orch.record() == Record(SESSION_ID, "keys", KEYS)


@pytest.mark.parametrize(
    "answer",
    [{"ok": True, "pairing_id": PAIRING_ID}, {**KEYS, "pop": "short"}, {**KEYS, "pub": {"kem": "x", "sig": "y"}}],
)
def test_keys_the_tool_does_not_deliver_are_unavailable(
    orch: Orchestrator, tool: FakeTool, session: Session, answer: dict[str, Any]
) -> None:
    tool.keys = answer
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "unavailable"
    assert orch.record() is None


def test_a_failing_tool_is_unavailable(orch: Orchestrator, tool: FakeTool, session: Session) -> None:
    tool.fail["pair --session"] = "internal"
    with pytest.raises(OrchestratorError) as error:
        orch.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "unavailable"


# -- handover and claim -----------------------------------------------------------------------------------------


def test_the_handover_claims_serves_and_reloads_the_idle_agent(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, reloads: Reloads, session: Session
) -> None:
    claimed(orch, context, session)
    assert tool.calls[-1] == ("pair", "--session", "--handover")
    assert tool.inputs[-1] == HANDOVER
    assert tool.serving
    record = orch.record()
    assert record is not None
    assert (record.phase, record.pairing_id) == ("claimed", PAIRING_ID)
    assert reloads.calls == [SESSION_ID]
    orch.handover(SESSION_ID, HANDOVER)  # a repeat of the same handover: accepted, nothing runs again
    assert len(tool.calls) == 2


def test_a_handover_must_be_the_recorded_pairing_once(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    with pytest.raises(OrchestratorError) as error:
        orch.handover(SESSION_ID, HANDOVER)  # no keys
    assert code(error) == "state_conflict"
    set_state(context, "idle")
    orch.keys(SESSION_ID, KEYS_INPUT)
    with pytest.raises(OrchestratorError) as error:
        orch.handover(SESSION_ID, {**HANDOVER, "pairing_id": "0193000a-0000-7000-8000-0000000000ff"})
    assert code(error) == "state_conflict"
    add_session(context)
    with pytest.raises(OrchestratorError) as error:
        orch.handover(OTHER_ID, HANDOVER)
    assert code(error) == "state_conflict"
    orch.handover(SESSION_ID, HANDOVER)
    with pytest.raises(OrchestratorError) as error:
        orch.handover(SESSION_ID, {**HANDOVER, "ct": "AAECAwQG"})  # another handover after it
    assert code(error) == "state_conflict"


def test_a_handover_needs_a_running_session_and_the_keys_phase(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    orch.keys(SESSION_ID, KEYS_INPUT)
    set_state(context, "suspended")
    with pytest.raises(OrchestratorError) as error:
        orch.handover(SESSION_ID, HANDOVER)
    assert code(error) == "state_conflict"
    set_state(context, "idle")
    orch._save(Record(SESSION_ID, "failed", KEYS))
    with pytest.raises(OrchestratorError) as error:
        orch.handover(SESSION_ID, HANDOVER)
    assert code(error) == "state_conflict"


def test_a_failed_claim_deletes_the_keys(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, reloads: Reloads, session: Session
) -> None:
    tool.fail["pair --session --handover"] = "pairing_failed"
    claimed(orch, context, session)
    record = orch.record()
    assert record is not None
    assert record.phase == "failed"
    assert tool.stops == 1
    assert not tool.serving
    assert reloads.calls == []


def test_a_server_that_does_not_start_fails_the_claim(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    tool.fail["serve"] = "serve_failed"
    claimed(orch, context, session)
    record = orch.record()
    assert record is not None
    assert record.phase == "failed"


def test_a_session_that_ended_during_the_claim_stays_ended(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, reloads: Reloads, session: Session
) -> None:
    def stopped_meanwhile() -> None:
        set_state(context, "stopping")
        orch.ended(SESSION_ID)
        set_state(context, "idle")

    tool.on_claim = stopped_meanwhile
    claimed(orch, context, session)
    assert orch.record() is None
    assert not tool.serving
    assert reloads.calls == []
    tool.fail["pair --session --handover"] = "pairing_failed"
    claimed(orch, context, session)  # and a failure after the end changes nothing either
    assert orch.record() is None


def test_the_reload_waits_for_idle_and_repeats(
    orch: Orchestrator, context: ControlContext, reloads: Reloads, ticks: Ticks, session: Session
) -> None:
    orch.keys(SESSION_ID, KEYS_INPUT)
    reloads.answers = [False, True]
    ticks.on_sleep = lambda: set_state(context, "idle")  # the turn ends
    orch.handover(SESSION_ID, HANDOVER)  # working: wait; idle: the first reload fails, the second runs
    assert reloads.calls == [SESSION_ID, SESSION_ID]
    assert ticks.slept == [orchestrator.RELOAD_POLL_SECONDS] * 2
    assert orch._reloading == set()


@pytest.mark.usefixtures("session")
def test_the_reload_ends_with_the_session_the_record_or_the_deadline(
    orch: Orchestrator, context: ControlContext, reloads: Reloads, ticks: Ticks, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch.keys(SESSION_ID, KEYS_INPUT)
    ticks.on_sleep = lambda: set_state(context, "stopped")
    orch.handover(SESSION_ID, HANDOVER)
    assert reloads.calls == []
    record = orch.record()
    assert record is not None
    set_state(context, "working")
    ticks.on_sleep = lambda: orch._save(None)
    orch._reload_when_idle(record)
    monkeypatch.setattr(orchestrator, "RELOAD_MAX_SECONDS", 3.0)
    orch._save(record)
    ticks.on_sleep = None
    orch._reload_when_idle(record)
    assert reloads.calls == []


def test_the_default_reload_resumes_the_session_with_the_second_server(
    context: ControlContext, tool: FakeTool, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def reload_session(*args: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(sessions, "reload_session", reload_session)
    created = Orchestrator(context, tool, depth=lambda sid: 0)
    assert created._reload(SESSION_ID)
    assert calls == [(context, SESSION_ID, (orchestrator.MCP_CONFIG,), orchestrator.SENTENCE)]


def test_the_default_spawn_runs_in_a_thread(context: ControlContext, tool: FakeTool) -> None:
    done = threading.Event()
    created = Orchestrator(context, tool)
    created.spawn(done.set)
    assert done.wait(5)


def test_the_default_depth_comes_from_the_session_manager(
    context: ControlContext, tool: FakeTool, session: Session
) -> None:
    created = Orchestrator(context, tool)  # no manager meta for this session: not a main session
    with pytest.raises(OrchestratorError) as error:
        created.keys(SESSION_ID, KEYS_INPUT)
    assert code(error) == "state_conflict"


# -- relay ---------------------------------------------------------------------------------------------------------


def answer_next(context: ControlContext, answer: dict[str, Any]) -> threading.Thread:
    """Answer the next relay call as the runner does (in a thread, while the tool waits)."""

    def run() -> None:
        for _ in range(500):
            with context.journal.transaction() as db:
                row = db.execute("SELECT id FROM pending WHERE kind = 'relay' AND state = 'waiting'").fetchone()
            if row is not None:
                context.pending.answer(row[0], answer)
                return
            threading.Event().wait(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_relay_is_journaled_for_the_orchestrator_session_and_answered(
    orch: Orchestrator, context: ControlContext, session: Session
) -> None:
    claimed(orch, context, session)
    thread = answer_next(context, ANSWER)
    assert orch.relay(RELAY) == ANSWER
    thread.join(5)
    records, _ = context.journal.read(after=0, limit=1000)
    relayed = [record for record in records if record["kind"] == "hub.relay"]
    assert len(relayed) == 1
    assert relayed[0]["session_id"] == SESSION_ID
    assert {key: relayed[0]["body"][key] for key in RELAY} == RELAY


@pytest.mark.parametrize(
    ("refusal", "expected"), [("refused", "refused"), ("too_large", "too_large"), ("odd", "unavailable")]
)
def test_a_runner_refusal_is_the_relay_error(
    orch: Orchestrator, context: ControlContext, session: Session, refusal: str, expected: str
) -> None:
    claimed(orch, context, session)
    thread = answer_next(context, {"error": refusal})
    with pytest.raises(OrchestratorError) as error:
        orch.relay(RELAY)
    thread.join(5)
    assert code(error) == expected


def test_no_relay_without_a_handover_or_for_an_ended_session(
    orch: Orchestrator, context: ControlContext, session: Session
) -> None:
    for prepare in (lambda: None, lambda: orch.keys(SESSION_ID, KEYS_INPUT)):
        prepare()
        with pytest.raises(OrchestratorError) as error:
            orch.relay(RELAY)
        assert code(error) == "refused"
    orch._save(Record(SESSION_ID, "claimed", KEYS, "x"))
    set_state(context, "failed")
    with pytest.raises(OrchestratorError) as error:
        orch.relay(RELAY)
    assert code(error) == "refused"
    orch._save(Record(OTHER_ID, "claimed", KEYS, "x"))  # a record of a session the VM does not know
    with pytest.raises(OrchestratorError) as error:
        orch.relay(RELAY)
    assert code(error) == "refused"


@pytest.mark.parametrize(
    "args",
    [
        {k: v for k, v in RELAY.items() if k != "body"},
        {**RELAY, "session_id": OTHER_ID},
        {**RELAY, "target": "https://other.example/v1/me"},
        {**RELAY, "headers": {**RELAY["headers"], "Cookie": "a=b"}},  # type: ignore[dict-item]
    ],
)
def test_a_malformed_relay_is_refused_before_it_is_journaled(
    orch: Orchestrator, context: ControlContext, session: Session, args: dict[str, Any]
) -> None:
    claimed(orch, context, session)
    with pytest.raises(OrchestratorError) as error:
        orch.relay(args)
    assert code(error) == "request_invalid"
    records, _ = context.journal.read(after=0, limit=1000)
    assert not [record for record in records if record["kind"] == "hub.relay"]


def test_a_relay_without_an_answer_times_out(
    orch: Orchestrator, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed(orch, context, session)
    monkeypatch.setattr(orchestrator, "RELAY_WAIT_SECONDS", 0.05)
    with pytest.raises(OrchestratorError) as error:
        orch.relay(RELAY)
    assert code(error) == "timeout"


def test_a_journal_that_cannot_write_is_unavailable(
    orch: Orchestrator, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed(orch, context, session)

    def broken(*args: Any) -> str:
        raise JournalError("unavailable")

    monkeypatch.setattr(context.pending, "create", broken)
    with pytest.raises(OrchestratorError) as error:
        orch.relay(RELAY)
    assert code(error) == "unavailable"


# -- second MCP server and end ---------------------------------------------------------------------------------------


def test_the_mcp_socket_only_for_the_claimed_running_orchestrator(
    orch: Orchestrator, context: ControlContext, session: Session
) -> None:
    set_state(context, "idle")
    add_session(context)
    for prepare in (lambda: None, lambda: orch.keys(SESSION_ID, KEYS_INPUT)):
        prepare()
        with pytest.raises(OrchestratorError):
            orch.mcp_socket(SESSION_ID)
    orch.handover(SESSION_ID, HANDOVER)
    assert orch.mcp_socket(SESSION_ID) == context.config.orch_mcp_socket
    with pytest.raises(OrchestratorError) as error:
        orch.mcp_socket(OTHER_ID)  # every other session: not found
    assert code(error) == "not_found"
    set_state(context, "suspended")
    with pytest.raises(OrchestratorError):
        orch.mcp_socket(SESSION_ID)


def test_the_orchestrator_ends_with_a_stopped_or_failed_session_only(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    """Review KOR-57 b no. 1: a suspend (the session keeps its state while its run ends) and the orchestrator's own
    reload keep it; the hub revokes only at a final state."""
    claimed(orch, context, session)
    orch.ended(OTHER_ID)
    orch.ended(SESSION_ID)  # suspended: the session is still idle when its run ends
    assert orch.record() is not None
    set_state(context, "stopping")
    orch._reloading.add(SESSION_ID)
    orch.ended(SESSION_ID)
    assert orch.record() is not None
    orch._reloading.clear()
    orch.ended(SESSION_ID)
    assert orch.record() is None
    assert tool.stops == 1
    for final in ("failed", "stopped"):
        set_state(context, "idle")
        claimed(orch, context, session)
        set_state(context, final)
        orch.ended(SESSION_ID)
        assert orch.record() is None


def test_the_runner_ends_a_revoked_orchestrator(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    claimed(orch, context, session)
    orch.end_pairing(OTHER_ID, PAIRING_ID)
    orch.end_pairing(SESSION_ID, OTHER_ID)
    assert orch.record() is not None
    orch.end_pairing(SESSION_ID, PAIRING_ID)
    assert orch.record() is None
    assert tool.stops == 1
    orch.end_pairing(SESSION_ID, PAIRING_ID)  # nothing left: nothing happens
    assert tool.stops == 1


def test_a_started_control_process_ends_an_orchestrator_whose_session_stopped(
    orch: Orchestrator, context: ControlContext, tool: FakeTool, session: Session
) -> None:
    orch.check()
    orch.keys(SESSION_ID, KEYS_INPUT)
    orch.check()
    assert orch.record() is not None
    set_state(context, "stopped")
    orch.check()
    assert orch.record() is None
    assert tool.stops == 1


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"session_id": "x"}',
        '{"session_id": "x", "phase": "odd", "keys": {"pairing_id": "p"}, "handover_sha256": null}',
        '{"session_id": "x", "phase": "keys", "keys": [], "handover_sha256": null}',
        '{"session_id": "x", "phase": "keys", "keys": {}, "handover_sha256": null}',
    ],
)
def test_a_damaged_record_is_no_orchestrator(text: str) -> None:
    assert orchestrator.parse_record(text) is None


# -- the tool as the account agent-orch --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, '{"ok": true, "a": 1}\n', {"ok": True, "a": 1}),
        (0, 'agent-platform: progress\n{"ok": true}\n', {"ok": True}),
        (1, '{"ok": false, "error": "pairing_failed"}\n', "pairing_failed"),
        (1, '{"ok": false, "error": 3}\n', "tool_failed"),
        (1, '{"ok": true}\n', "tool_failed"),
        (0, "", "tool_failed"),
        (0, "not json\n", "tool_failed"),
        (0, "[1]\n", "tool_failed"),
    ],
)
def test_tool_output(returncode: int, stdout: str, expected: object) -> None:
    if isinstance(expected, str):
        with pytest.raises(ToolFailed) as error:
            orchestrator.tool_output(returncode, stdout)
        assert error.value.code == expected
    else:
        assert orchestrator.tool_output(returncode, stdout) == expected


class Commands:
    """``subprocess.run`` of ``LinuxTool``: records argv and keywords, answers from ``codes`` by the first word."""

    def __init__(self, stdout: str = '{"ok": true}\n', codes: dict[str, int] | None = None) -> None:
        self.stdout = stdout
        self.codes = codes or {}
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.error: BaseException | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.codes.get(argv[0], 0), self.stdout, "")


def test_linux_tool_runs_the_command_as_the_account_with_nothing_inherited(tmp_path: Path) -> None:
    commands = Commands()
    tool = LinuxTool(
        "/run/agent-guest/orch.sock", "/run/agent-orch/mcp.sock", state_dir=str(tmp_path), run_command=commands
    )
    assert tool.run(["pair", "--session"], {"session_id": SESSION_ID}) == {"ok": True}
    argv, kwargs = commands.calls[0]
    assert argv[:6] == ["runuser", "-u", "agent-orch", "--", "env", "-i"]
    assert f"AGENT_PLATFORM_HOME={tmp_path}" in argv
    assert "AGENT_PLATFORM_RELAY=/run/agent-guest/orch.sock" in argv
    assert argv[-3:] == [orchestrator.TOOL, "pair", "--session"]
    assert kwargs["input"] == '{"session_id": "' + SESSION_ID + '"}'
    assert kwargs["env"] == orchestrator.SYSTEM_ENV
    assert kwargs["timeout"] == orchestrator.TOOL_TIMEOUT_SECONDS
    commands.error = subprocess.TimeoutExpired(argv, 1)
    with pytest.raises(ToolFailed):
        tool.run(["pair", "--session"], {})


def test_linux_tool_serves_as_a_unit_of_the_account(tmp_path: Path) -> None:
    commands = Commands()
    tool = LinuxTool(
        "/run/agent-guest/orch.sock", "/run/agent-orch/mcp.sock", state_dir=str(tmp_path), run_command=commands
    )
    tool.serve()
    assert commands.calls[0][0] == ["systemctl", "stop", "agent-orch-mcp.service"]
    argv = commands.calls[1][0]
    assert argv[0] == "systemd-run"
    assert {"--unit=agent-orch-mcp", "--uid=agent-orch", "--gid=agent-orch"} <= set(argv)
    assert f"--setenv=AGENT_PLATFORM_HOME={tmp_path}" in argv
    assert argv[argv.index("--") + 1 :] == [orchestrator.TOOL, "mcp", "--socket", "/run/agent-orch/mcp.sock"]
    commands.codes["systemd-run"] = 1
    with pytest.raises(ToolFailed) as error:
        tool.serve()
    assert error.value.code == "serve_failed"
    commands.error = OSError("no systemd-run")
    with pytest.raises(ToolFailed):
        tool.serve()


def test_linux_tool_stop_deletes_the_state_with_the_keys(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "keys").mkdir(parents=True)
    (state / "keys" / "pending.key").write_text("secret")
    (state / "pending.json").write_text("{}")
    (state / "link").symlink_to(tmp_path)
    commands = Commands()
    LinuxTool("/a", "/b", state_dir=str(state), run_command=commands).stop()
    assert commands.calls[0][0] == ["systemctl", "stop", "agent-orch-mcp.service"]
    assert list(state.iterdir()) == []
    assert tmp_path.exists()  # a link is removed, never followed
    LinuxTool("/a", "/b", state_dir=str(tmp_path / "missing"), run_command=commands).stop()


def test_the_orchestrator_of_a_control_process_uses_the_linux_tool(context: ControlContext) -> None:
    vars(context).pop("_orchestrator", None)
    created = orchestrator.orchestrator(context)
    assert orchestrator.orchestrator(context) is created
    assert isinstance(created.tool, LinuxTool)
    assert (created.tool.relay_socket, created.tool.mcp_socket) == (
        context.config.orch_socket,
        context.config.orch_mcp_socket,
    )
