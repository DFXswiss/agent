"""U-GST-contexts on Linux (marker ``linux``): context export as root with real session users, restore into a new
sandbox owned by its own user, the capture's real scanner taking the restored tree as baseline, and Claude Code
resuming a restored conversation (ADR-072 sections 2 and 7).
"""

from __future__ import annotations

import io
import json
import os
import pwd
import shutil
import stat
import tarfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.model import ModelDouble
from agent_guest import contexts, sessions, tmux
from agent_guest.clock import uuid7
from agent_guest.sandbox import unit
from agent_guest.transcript import cwd_key
from tests.platform.guest.sessions.conftest import request
from tests.platform.guest_linux import test_sessions_linux as rigs
from tests.platform.guest_linux.conftest import Guest, ldd_paths
from tests.platform.guest_linux.test_sessions_linux import LIMITS, Rig, create, new_id, states, stop, wait_for

# fixtures of the session suite, used by name
make_rig = rigs.make_rig
sleeper = rigs.sleeper
claude_binary = rigs.claude_binary
model_gateway = rigs.model_gateway

pytestmark = [pytest.mark.linux, pytest.mark.timeout(900)]

CID = "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b"
OBJECT = "01a1ee56-5e00-7009-8f1b-340691d99f09"
EXPORT_LIMITS = {"workspace_max_bytes": 1 << 30, "transcript_max_bytes": 1 << 30}


@pytest.fixture(autouse=True)
def no_staging_root_left() -> Any:
    """The staging root a stage creates is removed again: later suites expect an empty ``/srv/sessions``."""
    yield
    shutil.rmtree(contexts.stage_root(unit.SESSIONS_ROOT), ignore_errors=True)


def records(rig: Rig, session_id: str, kind: str) -> list[dict[str, Any]]:
    found, _ = rig.context.journal.read(after=0, limit=10000)
    return [r["body"] for r in found if r["kind"] == kind and r["session_id"] == session_id]


def export(rig: Rig, session_id: str) -> bytes:
    reply = rig.client.request("POST", f"/v1/sessions/{session_id}/context/export", EXPORT_LIMITS)
    assert reply.status == 200, reply.body
    return reply.body


def stage_and_restore(rig: Rig, raw: bytes, target: str, claude_id: str, **changes: Any) -> None:
    reply = rig.client.request(
        "POST", f"/v1/contexts/{target}/stage", raw=raw, headers={"Content-Type": "application/x-tar"}
    )
    assert reply.status == 200, reply.body
    body = request(session_id=target, restore={"claude_session_id": claude_id, "object_id": OBJECT}, **changes)
    body.pop("initial_prompt")
    reply = rig.client.request("POST", "/v1/sessions", body)
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body


def as_user(path: Path, uid: int, data: bytes | None = None) -> None:
    if data is None:
        path.mkdir(mode=0o700, exist_ok=True)
    else:
        path.write_bytes(data)
    os.chown(path, uid, uid)


def test_export_as_root_skips_foreign_files_and_links_and_restore_belongs_to_the_new_user(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    rig = make_rig(sleeper, guest.exec_paths)
    old, new = new_id(), new_id()
    create(rig, old, limits=LIMITS)
    uid = pwd.getpwnam(unit.user_name(old)).pw_uid
    base = Path(unit.session_dir(old))
    claude = base / "home" / ".claude"
    as_user(claude / "projects", uid)
    project = claude / "projects" / cwd_key(str(base / "work"))
    as_user(project, uid)
    entry = {"type": "user", "message": {"role": "user", "content": "hi"}, "cwd": str(base / "work"), "uuid": "u1"}
    answer = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}
    lines = json.dumps(entry).encode() + b"\n" + json.dumps(answer).encode() + b"\n"
    as_user(project / f"{CID}.jsonl", uid, lines)
    os.symlink("/etc", claude / "todos")  # a link the session planted: never followed by root
    work = base / "work"
    as_user(work / "mine.txt", uid, b"mine\n")
    (work / "root.txt").write_bytes(b"foreign")  # owned by root: left out
    os.symlink("/etc/shadow", work / "shadow")
    # review KOR-33 no. 5: an executable, an empty directory and a link inside the workspace come back
    as_user(work / "run.sh", uid, b"#!/bin/sh\n")
    os.chmod(work / "run.sh", 0o755)
    as_user(work / "empty", uid)
    os.symlink("mine.txt", work / "inside")
    os.lchown(work / "inside", uid, uid)
    session = rig.context.state.session(old)
    assert session is not None
    rig.context.state.save_session(replace(session, claude_session_id=CID))

    raw = export(rig, old)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        names = archive.getnames()
        head = json.loads(archive.extractfile("context.json").read())  # type: ignore[union-attr]
    assert names == [
        "context.json",
        f"transcript/projects/{project.name}/{CID}.jsonl",
        "work/empty",
        "work/inside",
        "work/mine.txt",
        "work/run.sh",
    ]
    assert head["workspace_omitted"] == 2  # root.txt (another owner) and the link to /etc/shadow

    stage_and_restore(rig, raw, new, CID, limits=LIMITS)
    new_uid = pwd.getpwnam(unit.user_name(new)).pw_uid
    new_base = Path(unit.session_dir(new))
    restored = new_base / "home" / ".claude" / "projects" / cwd_key(str(new_base / "work")) / f"{CID}.jsonl"
    assert json.loads(restored.read_text().splitlines()[0])["cwd"] == str(new_base / "work")
    for path, mode in (
        (restored, 0o600),
        (restored.parent, 0o700),
        (new_base / "work", 0o700),
        (new_base / "work" / "mine.txt", 0o644),
        (new_base / "work" / "run.sh", 0o755),
        (new_base / "work" / "empty", 0o700),
    ):
        info = path.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (new_uid, new_uid, mode), path
    link = (new_base / "work" / "inside").lstat()
    assert stat.S_ISLNK(link.st_mode)
    assert (link.st_uid, os.readlink(new_base / "work" / "inside")) == (new_uid, "mine.txt")
    assert not Path(contexts.stage_root(unit.SESSIONS_ROOT), new).exists()
    # the capture's scanner started at sandbox_ready with the restored tree as its baseline
    time.sleep(6)
    assert records(rig, new, "session.file") == []
    (new_base / "work" / "mine.txt").write_bytes(b"changed\n")
    wait_for(lambda: records(rig, new, "session.file"), 20)
    assert [body["path"] for body in records(rig, new, "session.file")] == ["mine.txt"]


def test_claude_code_resumes_a_restored_conversation_in_another_sandbox(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, claude_binary: str, model_gateway: ModelDouble
) -> None:
    ctypes_so = next(Path(guest.lib_dynload).glob("_ctypes.cpython-*.so"))
    rig = make_rig(claude_binary, tuple(ldd_paths(claude_binary, guest.python, str(ctypes_so))))
    old, new = new_id(), new_id()
    model = {"access": "platform", "model_id": "double-model", "env": {"ANTHROPIC_MODEL": "double-model"}}
    create(rig, old, model=model, initial_prompt={"prompt_id": uuid7(time.time()), "text": "What is 2+2?"})
    assert sessions.credential_provisioned(rig.context, old)
    wait_for(lambda: "2+2 is 4." in rig.pane(old), 90, lambda: rig.pane(old))
    wait_for(lambda: records(rig, old, "session.output"), 30)
    stop(rig, old)
    wait_for(lambda: not rig.tmux.has_session(tmux.session_name(old)), 20)
    session = rig.context.state.session(old)
    assert session is not None
    assert session.claude_session_id is not None

    stage_and_restore(rig, export(rig, old), new, session.claude_session_id, model=model)
    # KOR-33 E2E: a resumed model never reads the new harness file; the notice of the restore waits for its first hook
    pending = rig.manager._meta(new)["notice"]
    assert pending["sentences"] == [sessions.CONTINUED_SENTENCE]
    assert f"- Session ID: `{new}`" in pending["facts"]
    assert sessions.credential_provisioned(rig.context, new)
    # the resumed TUI shows the earlier turn of the same conversation (not a summary, not a fresh session)
    wait_for(lambda: "2+2 is 4." in rig.pane(new), 90, lambda: rig.pane(new))
    resumed = rig.context.state.session(new)
    assert resumed is not None
    assert resumed.claude_session_id == session.claude_session_id
    time.sleep(3)
    # the earlier turns are not posted again as output of the new session
    assert [body for body in records(rig, new, "session.output") if "2+2 is 4." in body["text"]] == []
    stop(rig, new)
    assert states(rig.context, new)[-1] == "stopped"
