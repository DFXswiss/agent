"""U-GST-05 (HOT-06): a platform import (``files/import``, e.g. ``github.checkout``) is the baseline of the session's
workspace scanner - through the real control socket, for a running session and for one without capture
(``Capture.import_guard``; ADR-057)."""

from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_guest import tmux
from agent_guest.apisock import token_sha256
from agent_guest.control import ControlContext
from agent_guest.control_routes import capture as capture_routes
from agent_guest.control_routes.capture import Capture
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_TOKEN, ControlClient

SID = "01a1ee56-5e00-7003-8f1b-340691d99f05"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def capture(context: ControlContext, short_tmp: Path) -> Iterator[Capture]:
    created = Capture(
        context,
        tmux.Tmux(socket="/run/agent-guest/tmux.sock", scope=False),
        fifo_dir=str(short_tmp / "capture"),
        scan_seconds=3600,
    )
    capture_routes.install(context, created)
    yield created
    for session_id in list(created.sessions):
        created.agent_ended(session_id)


def save(context: ControlContext, base: Path, state: str) -> Session:
    session = Session(
        id=SID,
        uid=os.getuid(),
        token_sha256=token_sha256(SESSION_TOKEN),
        state=state,
        workspace=f"{base}/work",
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context.state.save_session(session)
    return session


def repository(count: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for index in range(count):
            data = f"line {index}\n".encode()
            info = tarfile.TarInfo(f"src/m{index:04}.py")
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def do_import(client: ControlClient, data: bytes) -> Any:
    target = f"/v1/sessions/{SID}/files/import?dir=repos/o/r&replace=true"
    return client.request("POST", target, raw=data, headers={"Content-Type": "application/x-tar"})


def files(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 100_000)
    return [record["body"] for record in records if record["kind"] == "session.file"]


def test_a_checkout_into_a_running_session_is_not_reported(
    context: ControlContext, capture: Capture, control_client: ControlClient, base: Path
) -> None:
    save(context, base, "working")
    (base / "work" / "draft.md").write_text("before the checkout\n")
    capture.ensure_scanner(SID)
    (base / "work" / "draft.md").write_text("changed just before the checkout\n")
    reply = do_import(control_client, repository(3000))
    assert (reply.status, reply.json()["files"]) == (200, 3000)
    assert [(body["path"], body["op"]) for body in files(context)] == [("draft.md", "edit")]
    scanner = capture.sessions[SID].scanner
    assert scanner.scan() == 0
    (base / "work" / "repos" / "o" / "r" / "src" / "m0007.py").write_text("line 7\nline 8\n")
    assert capture.file_written(SID, "repos/o/r/src/m0007.py", "edit") is True
    edit = files(context)[-1]
    assert edit["path"] == "repos/o/r/src/m0007.py"
    assert edit["patch"].endswith(" line 7\n+line 8\n")


def test_a_checkout_without_capture_updates_the_index_for_the_next_start(
    context: ControlContext, capture: Capture, control_client: ControlClient, base: Path
) -> None:
    save(context, base, "working")
    capture.ensure_scanner(SID)
    capture.agent_ended(SID)
    save(context, base, "suspended")
    assert do_import(control_client, repository(50)).status == 200
    assert SID not in capture.sessions
    save(context, base, "working")
    scanner = capture.ensure_scanner(SID).scanner
    assert scanner.scan() == 0
    assert files(context) == []


def test_a_capture_attached_while_the_import_waits_adopts_it(
    context: ControlContext, capture: Capture, base: Path
) -> None:
    session = save(context, base, "suspended")
    stage = base / ".files-test" / "tree"
    (stage / "src").mkdir(parents=True)
    (stage / "src" / "a.py").write_text("a = 1\n")
    fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
    try:
        moving = capture.import_guard(session)("repos/o/r", fd)
    finally:
        os.close(fd)
    save(context, base, "working")
    running = capture.ensure_scanner(SID).scanner
    with moving:
        (base / "work" / "repos" / "o").mkdir(parents=True)
        os.rename(stage, base / "work" / "repos" / "o" / "r")
    assert "repos/o/r/src/a.py" in running.known
    assert running.scan() == 0
    assert files(context) == []
