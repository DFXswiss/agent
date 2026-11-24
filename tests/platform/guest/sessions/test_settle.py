"""U-GST-settle: the context export waits for a quiet transcript and never produces a checkpoint without a complete
turn (KOR-33 E2E; ``contexts.settle``, ``has_turn``, ``turn_ended``).

The E2E run of KOR-33 took the idle checkpoint when the ``Stop`` hook reported ``idle`` - before Claude Code had
written the turn to its transcript: the first checkpoints held 545-623 bytes and restoring one failed the agent's
start. These tests fail without the fix: an export of an ``idle`` session whose turn is still being written waits for
it, and a conversation without a complete turn is ``state_conflict`` (nothing to save).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_guest import contexts, files, sessions
from agent_guest.control import ControlContext
from agent_guest.control_routes import context as context_route
from agent_guest.files import FilesError
from agent_guest.state import Session
from agent_guest.transcript import cwd_key
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import TASK, VERSION
from tests.platform.guest.sessions.test_contexts import CID, LIMITS, OLD, assistant, build_session, line, prompt

TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.txt"}}


class Clock:
    """Wall and monotonic time that only move when the export sleeps; ``on_sleep`` runs at every sleep."""

    def __init__(self, state: str | None, mtime: float, on_sleep: Callable[[int], None] | None = None) -> None:
        self.state_value = state
        self.now = mtime
        self.sleeps = 0
        self.on_sleep = on_sleep

    def settle(self) -> contexts.Settle:
        return contexts.Settle(
            state=lambda: self.state_value, wall=lambda: self.now, monotonic=lambda: self.now, sleep=self.sleep
        )

    def sleep(self, seconds: float) -> None:
        assert seconds == contexts.SETTLE_POLL_SECONDS
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def conversation_path(base: Path) -> Path:
    return base / "home" / ".claude" / "projects" / cwd_key(str(base / "work")) / f"{CID}.jsonl"


def session_of(context: ControlContext, session_id: str = OLD) -> Session:
    found = context.state.session(session_id)
    assert found is not None
    return found


def export_with(session: Session, clock: contexts.Settle | None) -> bytes:
    stream = contexts.export_context(
        session,
        task_id=TASK,
        agent_version=VERSION,
        created_at="2026-11-24T10:00:00.000Z",
        workspace_max=1 << 20,
        transcript_max=1 << 20,
        clock=clock,
    )
    return b"".join(stream)


def exported_conversation(raw: bytes, base: Path) -> bytes:
    name = f"transcript/projects/{cwd_key(str(base / 'work'))}/{CID}.jsonl"
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        reader = archive.extractfile(name)
        assert reader is not None
        return reader.read()


# -- the failing case of the E2E run ---------------------------------------------------------------------------------


def test_an_idle_export_waits_until_the_turn_is_written(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    base = build_session(manager, context, partial=b"")
    path = conversation_path(base)
    work = str(base / "work")
    path.write_bytes(line(prompt("first", work)) + line(assistant("msg_1", "one", work)) + line(prompt("second", work)))
    mtime = path.stat().st_mtime

    def claude_writes(count: int) -> None:
        if count == 3:  # Claude Code writes the turn's answer a moment after the Stop hook reported idle
            with path.open("ab") as handle:
                handle.write(line(assistant("msg_2", "two", work)))

    clock = Clock("idle", mtime, claude_writes)
    raw = export_with(session_of(context), clock.settle())
    assert exported_conversation(raw, base).splitlines()[-1] == line(assistant("msg_2", "two", work)).strip()
    # it waited for the answer, then until the file was quiet for SETTLE_SECONDS
    assert clock.sleeps >= int(contexts.SETTLE_SECONDS / contexts.SETTLE_POLL_SECONDS)


def test_a_conversation_without_a_complete_turn_is_not_exported(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    base = build_session(manager, context, state="stopped", partial=b"")
    path = conversation_path(base)
    path.write_bytes(b'{"type":"summary","summary":"x"}\n' + line(prompt("hello", str(base / "work"))))
    clock = Clock("stopped", path.stat().st_mtime + 60)
    with pytest.raises(FilesError) as refused:
        export_with(session_of(context), clock.settle())
    assert refused.value.code == "state_conflict"
    assert clock.sleeps == 0
    assert not [p for p in base.iterdir() if p.name.startswith(".files-")]
    # through the route, too (the runner drops such a checkpoint: nothing to save yet)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    reply = control_client.request("POST", f"/v1/sessions/{OLD}/context/export", LIMITS)
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})


def test_an_idle_export_whose_turn_never_ends_is_bounded(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    base = build_session(manager, context, partial=b"")
    path = conversation_path(base)
    work = str(base / "work")
    tool = assistant("msg_2", "reading", work)
    tool["message"]["content"].append(TOOL_USE)
    path.write_bytes(line(prompt("hello", work)) + line(assistant("msg_1", "hi", work)) + line(tool))
    clock = Clock("idle", path.stat().st_mtime + 60)
    raw = export_with(session_of(context), clock.settle())
    assert abs(clock.sleeps - int(contexts.SETTLE_MAX_SECONDS / contexts.SETTLE_POLL_SECONDS)) <= 1
    assert exported_conversation(raw, base) == path.read_bytes()


def test_working_sessions_are_exported_at_once_and_others_wait_for_a_quiet_file(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    base = build_session(manager, context)
    mtime = conversation_path(base).stat().st_mtime
    working = Clock("working", mtime)
    export_with(session_of(context), working.settle())
    assert working.sleeps == 0
    export_with(session_of(context), None)  # without a clock the session counts as working
    stopped = Clock("stopped", mtime)
    export_with(session_of(context), stopped.settle())
    assert abs(stopped.sleeps - int(contexts.SETTLE_SECONDS / contexts.SETTLE_POLL_SECONDS)) <= 1
    gone = Clock(None, mtime + 2 * contexts.SETTLE_SECONDS)
    export_with(session_of(context), gone.settle())
    assert gone.sleeps == 0


def test_settle_without_a_conversation_file_leaves_the_refusal_to_the_export(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    base = build_session(manager, context)
    session = session_of(context)
    slug = cwd_key(session.workspace)
    root = os.open(base, os.O_RDONLY)
    try:
        clock = Clock("idle", 0.0)
        contexts.settle(root, session, "missing-project", clock.settle())
        path = conversation_path(base)
        path.unlink()
        path.mkdir()  # not a regular file
        contexts.settle(root, session, slug, clock.settle())
        path.rmdir()
        contexts.settle(root, session, slug, clock.settle())  # no file
        path.write_bytes(line(prompt("hello", "w")) + line(assistant("m", "x", "w")))
        foreign = Session(session.id, session.uid + 1, "0" * 64, "idle", session.workspace, CID, "x")
        contexts.settle(root, foreign, slug, clock.settle())  # another owner's file is never read
        assert clock.sleeps == 0
    finally:
        os.close(root)
    assert context_route._state(context, "01a1ee56-5e00-7003-8f1b-340691d99f77") is None
    assert context_route._state(context, OLD) == "idle"


# -- the record checks -----------------------------------------------------------------------------------------------


def write(tmp_path: Path, data: bytes) -> int:
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    return os.open(path, os.O_RDONLY)


def test_lines_forward_and_backward_across_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "CHUNK", 4)
    data = b"alpha\nbe\n\ngamma-delta\nz\n"
    fd = write(tmp_path, data)
    try:
        assert list(contexts._lines_forward(fd, len(data))) == [b"alpha", b"be", b"", b"gamma-delta", b"z"]
        assert list(contexts._lines_backward(fd, len(data), 1 << 20)) == [
            b"",
            b"z",
            b"gamma-delta",
            b"",
            b"be",
            b"alpha",
        ]
        # a budget that ends inside a line stops before it
        assert list(contexts._lines_backward(fd, len(data), 6)) == [b"", b"z"]
        assert list(contexts._lines_backward(fd, 0, 10)) == [b""]
        assert list(contexts._lines_forward(fd, 0)) == []
    finally:
        os.close(fd)


def test_has_turn_and_turn_ended() -> None:
    user = json.dumps({"type": "user", "message": {"content": "x"}}).encode()
    answer = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "y"}]}}).encode()
    tool = json.dumps({"type": "assistant", "message": {"content": [TOOL_USE]}}).encode()
    side = json.dumps({"type": "user", "isSidechain": True}).encode()
    odd = json.dumps({"type": "assistant", "message": "text"}).encode()
    assert contexts.has_turn([b"not json", b"[1]", b"[" * 100_000, user, b'{"type":["user"]}', answer, b"never read"])
    assert not contexts.has_turn([user, user, b'{"type":"summary"}'])
    assert not contexts.has_turn([])
    assert contexts.turn_ended([b"", b'{"type":"system"}', side, answer, user])
    assert not contexts.turn_ended([user, answer])
    assert not contexts.turn_ended([tool, user])
    assert contexts.turn_ended([odd])
    assert not contexts.turn_ended([b"x", b'{"type":"summary"}'])
    assert contexts._record(b"[" * 100_000) is None


def test_the_route_gives_the_export_the_session_state(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_session(manager, context, state="working")
    seen: list[str | None] = []
    real = contexts.settle

    def spy(base: int, session: Session, slug: str, clock: contexts.Settle) -> None:
        seen.append(clock.state())
        real(base, session, slug, clock)

    monkeypatch.setattr(contexts, "settle", spy)
    reply = control_client.request("POST", f"/v1/sessions/{OLD}/context/export", LIMITS)
    assert reply.status == 200
    assert seen == ["working"]
