"""U-GST-capture: capture at the session lifecycle and the PostToolUse addon (05-runner.md 5.5, 6.4, 7; GST-05).

Builds on the guest fixtures (control process, journal with fake clock, API server). The session lives in a temporary
base owned by the test user; tmux is a recording fake, the pane a FIFO written by the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import hooks, sessions, tmux, transcript
from agent_guest.apisock import ApiClient, ApiSockError, token_sha256
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext, ControlService
from agent_guest.control_routes import capture as capture_routes
from agent_guest.control_routes.capture import Capture
from agent_guest.control_routes.hooks import FILE_HINTS
from agent_guest.driver import ClaudeCodeDriver
from agent_guest.hook_addons import file_changes
from agent_guest.state import Session, StateError
from tests.platform.guest.conftest import SESSION_TOKEN, FakeClock
from tests.platform.guest.hooks.conftest import HookClient, tool_body

SID = "01a1ee56-5e00-7003-8f1b-340691d99f03"
CLAUDE = "8e9d794a-70a5-4d82-bbd5-531bd6272078"


@dataclass
class FakeTmux:
    """``subprocess.run`` of tmux: records argv; ``codes[subcommand]`` answers (default 0)."""

    calls: list[list[str]] = field(default_factory=list)
    codes: dict[str, int] = field(default_factory=dict)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        sub = argv[argv.index("-S") + 2]
        return subprocess.CompletedProcess(argv, self.codes.get(sub, 0), "", "")

    def subcommands(self) -> list[str]:
        return [argv[argv.index("-S") + 2] for argv in self.calls]


@pytest.fixture
def fake_tmux() -> FakeTmux:
    return FakeTmux()


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    (path / "work").mkdir(parents=True)
    (path / "work" / "imported.txt").write_text("from the runner\n")
    return path


@pytest.fixture
def capture(context: ControlContext, fake_tmux: FakeTmux, short_tmp: Path) -> Iterator[Capture]:
    created = Capture(
        context,
        tmux.Tmux(socket="/run/agent-guest/tmux.sock", run=fake_tmux, scope=False),
        fake_tmux,
        fifo_dir=str(short_tmp / "capture"),
        scan_seconds=0.02,
        poll=0.01,
    )
    capture_routes.install(context, created)
    yield created
    for session_id in list(created.sessions):
        created.agent_ended(session_id)


def save(context: ControlContext, base: Path, state: str, session_id: str = SID, uid: int | None = None) -> Session:
    session = Session(
        id=session_id,
        uid=os.getuid() if uid is None else uid,
        token_sha256=token_sha256(SESSION_TOKEN),
        state=state,
        workspace=f"{base}/work",
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context.state.save_session(session)
    return session


def journal(context: ControlContext, kind: str) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 1000)
    return [record["body"] for record in records if record["kind"] == kind]


def until(condition: Callable[[], object], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def emit(context: ControlContext, event: str, session_id: str = SID) -> None:
    capture_routes.SESSION_EVENTS[event](context, session_id)


def pane(capture: Capture, data: bytes, session_id: str = SID) -> None:
    fd = os.open(f"{capture.fifo_dir}/{session_id}.fifo", os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def test_a_session_run_is_captured_from_start_to_end(
    context: ControlContext, capture: Capture, base: Path, fake_tmux: FakeTmux
) -> None:
    session = save(context, base, "sandbox_ready")
    emit(context, "sandbox_ready")
    assert (base / "capture-index.json").exists()
    context.state.save_session(replace(session, state="agent_starting"))
    emit(context, "agent_starting")
    assert fake_tmux.subcommands() == ["pipe-pane"]
    pane(capture, b"\x1b[2J welcome")
    until(lambda: journal(context, "session.terminal"))
    # HOT-09: the pane output moves nothing; prompt_delivering is the delivery watcher's (TUI and platform tools)
    assert journal(context, "session.state") == []
    project = base / "home" / ".claude" / "projects" / transcript.cwd_key(f"{base}/work")
    project.mkdir(parents=True)
    entries = [
        {"type": "user", "sessionId": CLAUDE, "message": {"role": "user", "content": "Hi"}},
        {"type": "assistant", "sessionId": CLAUDE, "message": {"id": "msg_1", "content": [{"type": "text",
                                                                                           "text": "Hello"}]}},
    ]  # fmt: skip
    (project / f"{CLAUDE}.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    until(lambda: getattr(context.state.session(SID), "claude_session_id", None) == CLAUDE)
    assert journal(context, "session.state") == []  # named, still starting (the watcher's record will carry it)
    (base / "work" / "answer.md").write_text("42\n")
    until(lambda: journal(context, "session.file"))
    assert journal(context, "session.file")[0]["path"] == "answer.md"
    emit(context, "agent_ended")
    assert capture.sessions == {}
    assert [body["text"] for body in journal(context, "session.output")] == ["Hello"]
    frames = [frame for body in journal(context, "session.terminal") for frame in body["frames"]]
    assert "".join(frame[2] for frame in frames) == "\x1b[2J welcome"
    assert fake_tmux.subcommands()[-1] == "pipe-pane"
    assert not os.path.exists(f"{capture.fifo_dir}/{SID}.fifo")


def test_a_resumed_run_replaces_the_previous_one(
    context: ControlContext, capture: Capture, base: Path, fake_tmux: FakeTmux
) -> None:
    save(context, base, "agent_starting")
    capture.agent_starting(SID)
    first = capture.sessions[SID].recorder
    capture.agent_starting(SID)
    assert capture.sessions[SID].recorder is not first
    assert fake_tmux.subcommands() == ["pipe-pane", "pipe-pane", "pipe-pane"]


def test_a_pane_that_cannot_be_piped_still_gets_the_transcript(
    context: ControlContext, capture: Capture, base: Path, fake_tmux: FakeTmux, capsys: pytest.CaptureFixture[str]
) -> None:
    save(context, base, "agent_starting")
    fake_tmux.codes["pipe-pane"] = 1
    emit(context, "agent_starting")
    entry = capture.sessions[SID]
    assert entry.recorder is None
    assert entry.follower is not None
    assert "terminal capture could not start" in capsys.readouterr().err


def test_events_of_unknown_or_ended_sessions_change_nothing(
    context: ControlContext, capture: Capture, base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.agent_ended(SID)
    save(context, base, "failed", uid=-1)
    with pytest.raises(Exception, match="not_found"):
        capture.ensure_scanner(SID)
    assert capture.sessions == {}


def test_running_sessions_without_capture_are_attached_again(
    context: ControlContext, capture: Capture, base: Path, tmp_path: Path, fake_tmux: FakeTmux
) -> None:
    other = "01a1ee56-5e00-7003-8f1b-340691d99f04"
    gone = "01a1ee56-5e00-7003-8f1b-340691d99f05"
    other_base = tmp_path / "srv" / other
    (other_base / "work").mkdir(parents=True)
    save(context, base, "working")
    save(context, other_base, "stopped", session_id=other)
    save(context, other_base, "idle", session_id=gone)

    def has_session(name: str) -> bool:
        return name == f"s-{SID}"

    capture.terminal.has_session = has_session  # type: ignore[method-assign]
    capture.attach_running()
    assert set(capture.sessions) == {SID}
    capture.attach_running()
    assert fake_tmux.subcommands() == ["pipe-pane"]


def test_scan_failures_are_logged_and_the_loop_goes_on(
    context: ControlContext, capture: Capture, base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save(context, base, "sandbox_ready")
    calls: list[int] = []
    original = capture.scan

    def failing(session: Session, scanner: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        original(session, scanner)

    capture.scan = failing  # type: ignore[method-assign]
    capture.ensure_scanner(SID)
    until(lambda: len(calls) >= 2)
    assert "workspace scan failed" in capsys.readouterr().err


def test_file_hints_of_the_hook_are_checked_by_the_scan(context: ControlContext, capture: Capture, base: Path) -> None:
    session = save(context, base, "working")
    entry = capture.ensure_scanner(SID)
    entry.stop.set()
    (base / "work" / "hinted.txt").write_text("h")
    FILE_HINTS.add(SID, f"{base}/work/hinted.txt")
    capture.scan(session, entry.scanner)
    assert [body["path"] for body in journal(context, "session.file")] == ["hinted.txt"]


# -- op and addon -------------------------------------------------------------------------------------------------


def test_the_op_reports_a_written_file_at_once(
    context: ControlContext, capture: Capture, base: Path, api_client: ApiClient
) -> None:
    save(context, base, "working")
    entry = capture.ensure_scanner(SID)
    # the periodic scan reports the same change on its own; stopped here, so the op is the one that reports it
    entry.stop.set()
    entry.thread.join(30)
    (base / "work" / "a.txt").write_text("a\n")
    reply = api_client.call("capture.file_written", {"session_id": SID, "path": f"{base}/work/a.txt", "op": "write"})
    assert reply == {"reported": True}
    (base / "work" / "a.txt").write_text("b\n")
    reply = api_client.call("capture.file_written", {"session_id": SID, "path": " a.txt ", "op": "edit"})
    assert reply == {"reported": True}
    assert [(body["path"], body["op"]) for body in journal(context, "session.file")] == [
        ("a.txt", "write"),
        ("a.txt", "edit"),
    ]
    outside = {"session_id": SID, "path": f"{base}/work/../escape.txt", "op": "write"}
    assert api_client.call("capture.file_written", outside) == {"reported": False}


@pytest.mark.parametrize(
    "args",
    [
        {"session_id": SID, "path": "a", "op": "delete"},
        {"session_id": SID, "path": "", "op": "write"},
        {"session_id": SID, "path": "a\x00b", "op": "write"},
        {"session_id": SID, "path": "x" * 4097, "op": "write"},
        {"session_id": SID, "path": 3, "op": "write"},
        {"session_id": 3, "path": "a", "op": "write"},
        {"session_id": SID, "path": "a", "op": "write", "extra": 1},
    ],
)
def test_the_op_refuses_malformed_arguments(
    context: ControlContext, capture: Capture, base: Path, api_client: ApiClient, args: dict[str, Any]
) -> None:
    save(context, base, "working")
    with pytest.raises(ApiSockError) as raised:
        api_client.call("capture.file_written", args)
    assert raised.value.code == "request_invalid"


def test_the_op_refuses_unknown_sessions(capture: Capture, api_client: ApiClient) -> None:
    with pytest.raises(ApiSockError) as raised:
        api_client.call("capture.file_written", {"session_id": SID, "path": "a", "op": "write"})
    assert raised.value.code == "not_found"


def test_the_capture_shares_the_tmux_client_of_the_session_manager(context: ControlContext) -> None:
    runner = FakeTmux()
    terminal = tmux.Tmux(run=runner, scope=False)
    manager = sessions.SessionManager(context, system=None, tmux=terminal, driver=ClaudeCodeDriver("2.1.330"))  # type: ignore[arg-type]
    sessions.install(context, manager)
    created = capture_routes.capture(context)
    assert created.terminal is terminal
    assert created.run is runner
    assert capture_routes.capture(context) is created


def test_the_addon_is_registered_for_post_tool_use() -> None:
    addons = dict(hooks.load_addons())
    assert addons["file_changes"] is file_changes.ADDON
    assert file_changes.ADDON.events == frozenset({"PostToolUse"})


@dataclass
class FakeControl:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, args))
        return {"reported": True}


@dataclass
class FakeApi:
    control: FakeControl = field(default_factory=FakeControl)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"tool_name": "Write", "tool_input": {"file_path": "/w/a"}}, ("/w/a", "write")),
        ({"tool_name": "Edit", "tool_input": {"file_path": "b"}}, ("b", "edit")),
        ({"tool_name": "Read", "tool_input": {"file_path": "b"}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": ""}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": 3}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": "a\x00"}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": "p" * 4097}}, None),
        (None, None),
    ],
)
def test_the_addon_hands_written_paths_to_the_control_process(
    body: dict[str, Any] | None, expected: tuple[str, str] | None
) -> None:
    api = FakeApi()
    call = hooks.HookCall(event="PostToolUse", session_id=SID, body=body, decision="none", decorate=True)
    file_changes.handle(api, call)  # type: ignore[arg-type]
    if expected is None:
        assert api.control.calls == []
    else:
        assert api.control.calls == [
            ("capture.file_written", {"session_id": SID, "path": expected[0], "op": expected[1]})
        ]


def test_a_post_tool_use_hook_journals_the_file_change(
    context: ControlContext, capture: Capture, base: Path, api_port: int
) -> None:
    save(context, base, "working")
    emit(context, "sandbox_ready")
    (base / "work" / "notes.md").write_text("written by the tool\n")
    body = tool_body(
        "PostToolUse",
        "Write",
        {"file_path": f"{base}/work/notes.md", "content": "written by the tool\n"},
        tool_use_id="toolu_1",
        tool_response={"type": "create"},
    )
    reply = HookClient(api_port).post("PostToolUse", body)
    assert reply.status == 200
    assert reply.json() == {}
    (record,) = journal(context, "session.file")
    assert record["path"] == "notes.md"
    assert record["op"] == "write"
    assert journal(context, "guest.error") == []


def test_a_session_that_ends_without_agent_ended_stops_its_capture(
    context: ControlContext, capture: Capture, base: Path
) -> None:
    session = save(context, base, "sandbox_ready")
    capture.ensure_scanner(SID)
    context.state.save_session(replace(session, state="stopped"))
    until(lambda: SID not in capture.sessions)


def test_a_file_written_while_the_session_ends_is_left_to_its_last_scan(
    context: ControlContext, capture: Capture, base: Path
) -> None:
    save(context, base, "stopping")
    assert capture.file_written(SID, "a.txt", "write") is False
    assert capture.sessions == {}


def test_the_capture_ends_with_the_control_process(
    guest_config: GuestConfig, clock: FakeClock, fake_tmux: FakeTmux, short_tmp: Path, base: Path
) -> None:
    service = ControlService(guest_config, clock, startup=())
    context = service.start()
    try:
        created = capture_routes.install(
            context,
            Capture(
                context,
                tmux.Tmux(socket="/run/agent-guest/tmux.sock", run=fake_tmux, scope=False),
                fake_tmux,
                fifo_dir=str(short_tmp / "capture"),
                scan_seconds=0.02,
                poll=0.01,
            ),
        )
        save(context, base, "agent_starting")
        emit(context, "agent_starting")
        running = created.sessions[SID]
        assert running.follower is not None
        assert running.recorder is not None
        threads = [running.thread, running.follower._thread, running.recorder._thread]
    finally:
        service.stop()
    assert created.sessions == {}
    assert not any(thread is not None and thread.is_alive() for thread in threads)
    assert fake_tmux.subcommands() == ["pipe-pane", "pipe-pane"]
    # a closed capture attaches nothing any more: not for a late session event, an op or a restart hook
    emit(context, "agent_starting")
    created.attach_running()
    with pytest.raises(StateError, match="not_found"):
        created.ensure_scanner(SID)
    assert created.sessions == {}
    assert fake_tmux.subcommands() == ["pipe-pane", "pipe-pane"]
