"""U-GST-recorder/scanner/transcript on Linux (marker ``linux``): a real root tmux server with ``pipe-pane``.

Runs as root in the ``guest-ci`` VM (``make platform-ci-linux``): the recorder pipes a real pane into its FIFO, the
scanner and the transcript follower read files of a real unprivileged owner and refuse links planted by it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import agent_guest
from agent_guest import tmux, transcript
from agent_guest.clock import SystemClock
from agent_guest.journal import Journal
from agent_guest.recorder import CAST_NAME, TerminalRecorder
from agent_guest.scanner import WorkspaceScanner
from agent_guest.schema import SchemaSet
from agent_guest.state import Session, State

pytestmark = pytest.mark.linux

REPO = Path(agent_guest.__file__).resolve().parents[2]
SID = "01a1ee56-5e00-7003-8f1b-340691d99fa5"
OWNER = 29999  # an unused session UID: no user needed for ownership checks
CLAUDE = "8e9d794a-70a5-4d82-bbd5-531bd6272078"


@pytest.fixture
def area() -> Iterator[Path]:
    # Short path: the tmux socket is an AF_UNIX path.
    path = Path(tempfile.mkdtemp(prefix="gst5", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def journal(area: Path) -> Iterator[Journal]:
    opened = Journal.open(str(area / "journal.db"), SystemClock(), SchemaSet(REPO / "contracts"))
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def terminal(area: Path) -> Iterator[tmux.Tmux]:
    client = tmux.Tmux(socket=str(area / "tmux.sock"), scope=False)
    try:
        yield client
    finally:
        subprocess.run(client.argv("kill-server"), capture_output=True, check=False, env=tmux.CLIENT_ENV)


def records(journal: Journal, kind: str) -> list[dict[str, Any]]:
    found, _ = journal.read(0, 1000)
    bodies = [record["body"] for record in found if record["kind"] == kind]
    return bodies


def until(condition: Callable[[], object], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.05)


def pane_text(journal: Journal) -> str:
    return "".join(frame[2] for body in records(journal, "session.terminal") for frame in body["frames"])


def test_a_real_pane_is_recorded_through_pipe_pane(area: Path, journal: Journal, terminal: tmux.Tmux) -> None:
    base = area / "base"
    base.mkdir()
    gate = area / "go"
    script = (
        f"while [ ! -e {gate} ]; do sleep 0.05; done; "
        "printf 'h\\303\\251llo \\342\\202\\254\\n'; printf '\\033[1mbold\\033[0m\\n'; "
        "head -c 6000 /dev/zero | tr '\\0' 'x' | sed 's/x/\\xe2\\x82\\xac/g'; printf '\\nend\\n'; sleep 60"
    )
    terminal.new_session(tmux.session_name(SID), ["/bin/sh", "-c", script])
    seen: list[int] = []
    recorder = TerminalRecorder(
        journal, SID, str(base), terminal, fifo_dir=str(area / "capture"), on_output=lambda: seen.append(1)
    )
    recorder.start()
    gate.touch()
    until(lambda: "end" in pane_text(journal))
    recorder.stop()
    text = pane_text(journal)
    assert "héllo €" in text
    assert "\x1b[1mbold\x1b[0m" in text
    assert "€" * 6000 in text
    assert "�" not in text
    assert seen == [1]
    bodies = records(journal, "session.terminal")
    offsets = [frame[0] for body in bodies for frame in body["frames"]]
    assert offsets == sorted(offsets)
    assert all(sum(len(frame[2].encode()) for frame in body["frames"]) <= 16384 for body in bodies)
    assert all((body["cols"], body["rows"]) == (160, 48) for body in bodies)
    cast = [json.loads(line) for line in (base / CAST_NAME).read_text(encoding="utf-8").splitlines()]
    assert cast[0]["version"] == 2
    assert [[round(event[0] * 1000), event[1], event[2]] for event in cast[1:]] == [
        frame for body in bodies for frame in body["frames"]
    ]
    # A second recorder (restart) continues the offsets after a marker.
    again = TerminalRecorder(journal, SID, str(base), terminal, fifo_dir=str(area / "capture"))
    again.start()
    subprocess.run(
        terminal.argv("send-keys", "-t", f"={tmux.session_name(SID)}:", "-l", "--", "typed"),
        check=True,
        env=tmux.CLIENT_ENV,
    )
    time.sleep(0.5)
    again.stop()
    frames = [frame for body in records(journal, "session.terminal") for frame in body["frames"]]
    marker = [frame for frame in frames if frame[1] == "m"]
    assert marker == [[offsets[-1], "m", "recorder restarted"]]
    assert [frame[0] for frame in frames] == sorted(frame[0] for frame in frames)


def test_root_reads_only_the_session_owners_files(area: Path, journal: Journal) -> None:
    base = area / "srv" / SID
    work = base / "work"
    work.mkdir(parents=True)
    os.chown(work, OWNER, OWNER)
    scanner = WorkspaceScanner(journal, SID, str(base), OWNER)
    scanner.start()
    (work / "own.txt").write_text("mine\n")
    os.chown(work / "own.txt", OWNER, OWNER)
    (work / "root.txt").write_text("not the session's\n")
    bad = os.path.join(os.fsencode(work), b"bad\xff.txt")  # no UTF-8: could not be journaled
    with open(bad, "wb") as handle:
        handle.write(b"x")
    os.chown(bad, OWNER, OWNER)
    os.symlink("/etc/shadow", work / "shadow.txt")
    os.lchown(work / "shadow.txt", OWNER, OWNER)
    assert scanner.scan() == 1
    assert [body["path"] for body in records(journal, "session.file")] == ["own.txt"]
    assert scanner.check("shadow.txt", "write") is False
    assert scanner.check("root.txt", "write") is False


def test_the_transcript_of_the_owner_is_followed_and_planted_links_are_not(area: Path, journal: Journal) -> None:
    base = area / "srv" / SID
    (base / "work").mkdir(parents=True)
    state = State(journal)
    state.save_session(
        Session(SID, OWNER, "0" * 64, "agent_starting", f"{base}/work", None, "2026-11-21T10:00:00.000Z")
    )
    project = base / "home" / ".claude" / "projects" / transcript.cwd_key(f"{base}/work")
    project.mkdir(parents=True)
    for path in (base / "home", base / "home" / ".claude", base / "home" / ".claude" / "projects", project):
        os.chown(path, OWNER, OWNER)
    entries = [
        {"type": "user", "sessionId": CLAUDE, "message": {"role": "user", "content": "Hi"}},
        {"type": "assistant", "sessionId": CLAUDE, "message": {"id": "msg_1", "content": [{"type": "text",
                                                                                           "text": "Hello"}]}},
    ]  # fmt: skip
    planted = project / "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b.jsonl"
    os.symlink("/etc/shadow", planted)
    os.lchown(planted, OWNER, OWNER)
    own = project / f"{CLAUDE}.jsonl"
    own.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    os.chown(own, OWNER, OWNER)
    follower = transcript.TranscriptFollower(journal, state, SID, str(base), OWNER)
    assert follower.finish() == 1
    assert [body["text"] for body in records(journal, "session.output")] == ["Hello"]
    # HOT-09: the transcript names the Claude session; moving a starting session on is the delivery watcher's
    assert records(journal, "session.state") == []
    session = state.session(SID)
    assert session is not None
    assert (session.state, session.claude_session_id) == ("agent_starting", CLAUDE)
