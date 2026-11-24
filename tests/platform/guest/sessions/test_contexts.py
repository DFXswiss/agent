"""U-GST-contexts: context export, staging and restore of a session (ADR-072 sections 2 and 7, 03-api.md section 7.1;
``contexts.py``, ``control_routes/context.py``, the ``restore`` of ``sessions.py`` and the transcript baseline; review
KOR-33 nos. 5, 9, 11, 13, 14 and 19).

The session user is the test user here (files it owns are exported); real users, ownership and a real Claude Code
resume run in the Linux suite (``guest_linux/test_contexts_linux.py``).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from agent_guest import contexts, files, sessions, transcript
from agent_guest.control import ControlContext
from agent_guest.control_routes import context as context_route
from agent_guest.files import FilesError
from agent_guest.sandbox import unit
from agent_guest.scanner import WorkspaceScanner
from agent_guest.state import Session
from agent_guest.transcript import TranscriptFollower, cwd_key
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import SESSION, TASK, VERSION, FakeSystem, request

OLD = "01a1ee56-5e00-7003-8f1b-340691d99fa0"
CID = "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b"
OBJECT = "01a1ee56-5e00-7009-8f1b-340691d99f09"
OLD_KEY = cwd_key(f"/srv/sessions/{OLD}/work")
LIMITS = {"workspace_max_bytes": 1 << 30, "transcript_max_bytes": 1 << 30}


@pytest.fixture(autouse=True)
def quiet_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcripts here are written just before the export: no settle time (``test_settle.py`` covers it)."""
    monkeypatch.setattr(contexts, "SETTLE_SECONDS", 0.0)


def line(value: dict[str, Any]) -> bytes:
    return json.dumps(value).encode() + b"\n"


def assistant(message_id: str, text: str, cwd: str) -> dict[str, Any]:
    message = {"id": message_id, "role": "assistant", "content": [{"type": "text", "text": text}]}
    return {"type": "assistant", "message": message, "cwd": cwd, "sessionId": CID, "uuid": message_id}


def prompt(text: str, cwd: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": cwd, "uuid": f"u-{text}"}


def build_session(
    manager: sessions.SessionManager,
    context: ControlContext,
    session_id: str = OLD,
    *,
    state: str = "idle",
    claude: str | None = CID,
    uid: int | None = None,
    partial: bytes = b'{"type":"assist',
) -> Path:
    """A session of the test user with a transcript, companion files and a workspace; returns its base."""
    base = Path(manager.paths.sessions_root, session_id)
    work = base / "work"
    work.mkdir(parents=True)
    key = cwd_key(str(work))
    claude_dir = base / "home" / ".claude"
    project = claude_dir / "projects" / key
    (project / CID / "tool-results").mkdir(parents=True)
    lines = line(prompt("hello", str(work))) + line(assistant("msg_1", f"I read {work}/a.txt", str(work)))
    (project / f"{CID}.jsonl").write_bytes(lines + partial)
    (project / CID / "tool-results" / "r1.txt").write_text(f"result in {work}/a.txt")
    (project / CID / "subagents").mkdir()
    (project / CID / "subagents" / "agent-1.jsonl").write_bytes(b'{"type":"user"}\n{"type":"assi')
    (project / "other.jsonl").write_text("{}\n")
    (claude_dir / "todos").mkdir()
    (claude_dir / "todos" / f"{CID}-agent-{CID}.json").write_text("[]")
    (claude_dir / "todos" / "foreign.json").write_text("[]")
    (claude_dir / "file-history" / CID).mkdir(parents=True)
    (claude_dir / "file-history" / CID / "abc@v1").write_text("old")
    (claude_dir / "settings.json").write_text("{}")
    (claude_dir / ".claude.json").write_text("{}")
    (work / "a.txt").write_text("alpha\n")
    (work / "src").mkdir()
    (work / "src" / "b.py").write_text("x = 1\n")
    (work / "AGENTS.md").write_text("protected")
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref")
    os.symlink("/etc/passwd", work / "link")
    (work / "run.sh").write_text("#!/bin/sh\n")
    (work / "run.sh").chmod(0o755)
    (work / "empty").mkdir()
    os.symlink("a.txt", work / "inside")
    os.symlink(f"{work}/src/b.py", work / "abs")
    os.symlink("src", work / "srclink")
    os.mkfifo(work / "pipe")
    session = Session(
        id=session_id,
        uid=os.getuid() if uid is None else uid,
        token_sha256="0" * 64,
        state=state,
        workspace=str(work),
        claude_session_id=claude,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context.state.save_session(session)
    manager._write_meta(session_id, {"identity": {"task_id": TASK, "parent_session_id": None, "depth": 0}})
    return base


def export(client: ControlClient, session_id: str = OLD, **limits: int) -> Any:
    return client.request("POST", f"/v1/sessions/{session_id}/context/export", {**LIMITS, **limits})


def members(raw: bytes) -> dict[str, bytes]:
    """The regular files of an archive (owner 0, time 0, ``0644`` or - with an execute bit - ``0755``)."""
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            assert (member.uid, member.mtime) == (0, 0)
            if not member.isreg():
                continue
            assert member.mode in (0o644, 0o755)
            reader = archive.extractfile(member)
            assert reader is not None
            found[member.name] = reader.read()
    return found


def others(raw: bytes) -> dict[str, tuple[str, int]]:
    """Directories (``"dir"``) and links (their target) of an archive, with their modes."""
    found: dict[str, tuple[str, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if member.isdir():
                found[member.name] = ("dir", member.mode)
            elif member.issym():
                found[member.name] = (member.linkname, member.mode)
    return found


def stage_raw(client: ControlClient, raw: bytes, session_id: str = SESSION) -> Any:
    return client.request(
        "POST", f"/v1/contexts/{session_id}/stage", raw=raw, headers={"Content-Type": "application/x-tar"}
    )


def tar_of(*items: tuple[str, bytes | None], symlink: str | None = None) -> bytes:
    """A tar of ``items`` (content None: a directory) and a link ``symlink`` to ``/etc/passwd``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.type = tarfile.REGTYPE if data is not None else tarfile.DIRTYPE
            info.size = len(data or b"")
            out.addfile(info, io.BytesIO(data) if data is not None else None)
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            out.addfile(info)
    return buffer.getvalue()


def context_json(**changes: Any) -> bytes:
    value: dict[str, Any] = {
        "v": 1,
        "agent_kind": "claude-code",
        "agent_version": VERSION,
        "claude_session_id": CID,
        "session_id": OLD,
        "task_id": TASK,
        "cwd": f"/srv/sessions/{OLD}/work",
        "created_at": "2026-11-23T10:00:00.000Z",
        "transcript_files": [],
        "workspace": "empty",
        "workspace_files": 0,
        "workspace_bytes": 0,
    }
    value.update(changes)
    return json.dumps(value).encode()


# -- export --------------------------------------------------------------------------------------------------------


def test_export_holds_context_transcript_and_workspace(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    base = build_session(manager, context)
    reply = export(control_client)
    assert reply.status == 200
    assert reply.headers["Content-Type"] == "application/x-tar"
    found = members(reply.body)
    names = list(found)
    key = cwd_key(str(base / "work"))
    assert names[0] == "context.json"
    transcript_files = [
        f"file-history/{CID}/abc@v1",
        f"projects/{key}/{CID}.jsonl",
        f"projects/{key}/{CID}/subagents/agent-1.jsonl",
        f"projects/{key}/{CID}/tool-results/r1.txt",
        f"todos/{CID}-agent-{CID}.json",
    ]
    assert names[1:] == [
        f"transcript/projects/{key}/{CID}.jsonl",
        f"transcript/projects/{key}/{CID}/subagents/agent-1.jsonl",
        f"transcript/projects/{key}/{CID}/tool-results/r1.txt",
        f"transcript/todos/{CID}-agent-{CID}.json",
        f"transcript/file-history/{CID}/abc@v1",
        "work/a.txt",
        "work/run.sh",
        "work/src/b.py",
    ]
    assert json.loads(found["context.json"]) == {
        "v": 1,
        "agent_kind": "claude-code",
        "agent_version": context.info.claude_version,
        "claude_session_id": CID,
        "session_id": OLD,
        "task_id": TASK,
        "cwd": str(base / "work"),
        "created_at": json.loads(found["context.json"])["created_at"],
        "transcript_files": transcript_files,
        "workspace": "included",
        "workspace_files": 3,
        "workspace_bytes": 22,
        "workspace_omitted": 4,  # AGENTS.md, .git, the link to /etc/passwd, the FIFO
    }
    # review no. 5: execute bits, empty directories and links inside the workspace are kept
    with tarfile.open(fileobj=io.BytesIO(reply.body)) as archive:
        modes = {member.name: member.mode for member in archive.getmembers() if member.isreg()}
    assert (modes["work/run.sh"], modes["work/a.txt"]) == (0o755, 0o644)
    assert others(reply.body) == {
        "work/abs": ("src/b.py", 0o777),
        "work/empty": ("dir", 0o755),
        "work/inside": ("a.txt", 0o777),
        "work/src": ("dir", 0o755),
        "work/srclink": ("src", 0o777),
    }
    # review no. 13: the incomplete last line of every transcript file is cut, everything else is verbatim
    conversation = found[f"transcript/projects/{key}/{CID}.jsonl"]
    assert conversation.endswith(b"\n")
    assert b"assist" not in conversation.split(b"\n")[-1]
    assert len(conversation.splitlines()) == 2
    assert found[f"transcript/projects/{key}/{CID}/subagents/agent-1.jsonl"] == b'{"type":"user"}\n'
    assert not [p for p in base.iterdir() if p.name.startswith(".files-")]


def test_two_exports_of_an_unchanged_session_have_the_same_content_hash(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review no. 11 with the real export: only `created_at` differs, and the runner's hash ignores it."""
    from agent_runner.contexts import archive as runner_archive  # noqa: PLC0415

    build_session(manager, context)
    limits = runner_archive.limits_for(1 << 30, 1 << 30)
    first = export(control_client).body
    monkeypatch.setattr(context.journal.clock, "now", lambda: 1_900_000_000.0)
    second = export(control_client).body
    assert (
        json.loads(members(first)["context.json"])["created_at"]
        != json.loads(members(second)["context.json"])["created_at"]
    )
    found = [runner_archive.inspect(io.BytesIO(raw), limits) for raw in (first, second)]
    assert found[0].content_sha256 == found[1].content_sha256
    assert (found[0].workspace_state, found[0].workspace_omitted) == ("partial", 4)
    base = Path(manager.paths.sessions_root, OLD)
    (base / "work" / "run.sh").chmod(0o644)
    third = runner_archive.inspect(io.BytesIO(export(control_client).body), limits)
    assert third.content_sha256 != found[0].content_sha256


def test_an_unreadable_link_and_a_too_deep_directory_are_counted(
    manager: sessions.SessionManager, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = build_session(manager, context)
    deep = base / "work" / "deep"
    (deep / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(files, "MAX_DEPTH", 2)
    real = os.readlink

    def readlink(path: Any, *args: Any, **kwargs: Any) -> str:
        if str(path) == "inside":
            raise PermissionError(path)
        return str(real(path, *args, **kwargs))

    monkeypatch.setattr(os, "readlink", readlink)
    session = context.state.session(OLD)
    assert session is not None
    stream = contexts.export_context(
        session, task_id=TASK, agent_version=VERSION, created_at="x", workspace_max=1 << 20, transcript_max=1 << 20
    )
    raw = b"".join(stream)
    head = json.loads(members(raw)["context.json"])
    assert head["workspace_omitted"] == 4 + 1 + 1  # the unreadable link and `deep/a/b` (too deep)
    assert "work/inside" not in others(raw)
    assert others(raw)["work/deep/a"] == ("dir", 0o755)
    assert "work/deep/a/b" not in others(raw)


def test_entries_that_change_while_they_are_read_are_counted(
    manager: sessions.SessionManager, context: ControlContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = build_session(manager, context)
    session = context.state.session(OLD)
    assert session is not None
    stage = tmp_path / "stage"
    stage.mkdir()
    with tarfile.open(tmp_path / "t.tar", mode="w", format=tarfile.PAX_FORMAT) as archive:
        part = contexts._ContextExport(session, str(stage), archive, workspace=session.workspace)
        directory = os.open(base / "work" / "src", os.O_RDONLY)
        try:
            part.add_file(directory, "src")  # a directory where a file was seen
        finally:
            os.close(directory)
        real = os.read

        def broken(fd: int, size: int) -> bytes:
            raise OSError("gone")

        handle = os.open(base / "work" / "a.txt", os.O_RDONLY)
        try:
            monkeypatch.setattr(os, "read", broken)
            part.add_file(handle, "a.txt")
            monkeypatch.setattr(os, "read", real)
        finally:
            os.close(handle)
        root = os.open(base / "work", os.O_RDONLY)
        try:
            part.entry(root, "a.txt", "a.txt", 0)  # already handled: not again
        finally:
            os.close(root)
    assert part.omitted == 2
    assert part.files == []


def test_link_targets_inside_the_workspace() -> None:
    workspace = "/srv/sessions/x/work"
    assert contexts.inside_target(workspace, "a/link", "../b") == "../b"
    assert contexts.inside_target(workspace, "a/link", "../../b") is None
    assert contexts.inside_target(workspace, "a/link", f"{workspace}/c") == "../c"
    assert contexts.inside_target(workspace, "link", "/srv/sessions/x/other") is None
    assert contexts.inside_target(workspace, "link", "") is None
    assert contexts.inside_target(workspace, "link", "x" * 5000) is None
    assert contexts.staged_link_ok("work/a/link", "../b")
    assert not contexts.staged_link_ok("work/link", "/etc")
    assert not contexts.staged_link_ok("transcript/link", "a")
    assert not contexts.staged_link_ok("work/link", "../x")


def test_export_leaves_out_a_workspace_over_the_limit(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    build_session(manager, context)
    found = members(export(control_client, workspace_max_bytes=5).body)
    assert not [name for name in found if name.startswith("work/")]
    head = json.loads(found["context.json"])
    assert (head["workspace"], head["workspace_files"], head["workspace_bytes"]) == ("omitted_too_large", 0, 0)
    assert head["transcript_files"]


def test_export_of_an_empty_or_missing_workspace(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    base = build_session(manager, context, state="stopped")
    shutil.rmtree(base / "home" / ".claude" / "todos")
    for path in sorted((base / "work").rglob("*"), reverse=True):
        path.unlink() if not path.is_dir() or path.is_symlink() else path.rmdir()
    assert json.loads(members(export(control_client).body)["context.json"])["workspace"] == "empty"
    (base / "work").rmdir()
    assert json.loads(members(export(control_client).body)["context.json"])["workspace"] == "empty"


@pytest.mark.parametrize("extra", [-1, 1])
def test_a_transcript_over_the_limit_is_refused(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient, extra: int
) -> None:
    base = build_session(manager, context)
    conversation = base / "home" / ".claude" / "projects" / cwd_key(str(base / "work")) / f"{CID}.jsonl"
    complete = conversation.read_bytes().rsplit(b"\n", 1)[0] + b"\n"
    reply = export(control_client, transcript_max_bytes=len(complete) + extra)
    assert (reply.status, reply.json()) == (413, {"error": "size_exceeded"})
    assert not [p for p in base.iterdir() if p.name.startswith(".files-")]


def test_export_refusals(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    assert export(control_client).json() == {"error": "not_found"}
    base = build_session(manager, context, claude=None)
    assert (export(control_client).status, export(control_client).json()) == (409, {"error": "state_conflict"})
    other = "01a1ee56-5e00-7003-8f1b-340691d99fa1"
    build_session(manager, context, other, uid=-1)
    assert export(control_client, other).json() == {"error": "state_conflict"}
    unknown = "01a1ee56-5e00-7003-8f1b-340691d99fa2"
    build_session(manager, context, unknown)
    Path(manager.paths.meta_dir, f"{unknown}.json").unlink()
    assert export(control_client, unknown).json() == {"error": "state_conflict"}
    shutil.rmtree(base)
    context.state.save_session(
        Session(OLD, os.getuid(), "0" * 64, "stopped", str(base / "work"), CID, "2026-11-21T10:00:00.000Z")
    )
    assert export(control_client).json() == {"error": "not_found"}


@pytest.mark.parametrize("damage", ["no-claude", "no-project", "no-file", "a-directory", "foreign"])
def test_a_session_without_its_conversation_is_a_state_conflict(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient, damage: str
) -> None:
    base = build_session(manager, context)
    claude = base / "home" / ".claude"
    project = claude / "projects" / cwd_key(str(base / "work"))
    if damage == "no-claude":
        shutil.rmtree(claude)
    elif damage == "no-project":
        shutil.rmtree(project)
    elif damage == "no-file":
        (project / f"{CID}.jsonl").unlink()
    elif damage == "a-directory":
        (project / f"{CID}.jsonl").unlink()
        (project / f"{CID}.jsonl").mkdir()
    else:
        session = context.state.session(OLD)
        assert session is not None
        context.state.save_session(Session(OLD, os.getuid() + 1, "0" * 64, "idle", session.workspace, CID, "x"))
    reply = export(control_client)
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})


def test_complete_length_reads_backwards_over_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "CHUNK", 4)
    path = tmp_path / "t"
    path.write_bytes(b"ab\ncdefghijk")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert contexts.complete_length(fd, 12) == 3
        assert contexts.complete_length(fd, 2) == 0
        assert contexts.complete_length(fd, 0) == 0
    finally:
        os.close(fd)


def test_a_file_that_shrinks_while_it_is_read_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_bytes(b"abc")
    fd = os.open(path, os.O_RDONLY)
    try:
        reader = contexts._Reader(fd, 5)
        assert reader.read(2) == b"ab"
        with pytest.raises(FilesError) as error:
            reader.read()
        assert error.value.code == "unavailable"
        assert contexts._Reader(fd, 0).read(3) == b""
    finally:
        os.close(fd)


def test_a_failing_workspace_walk_fails_the_export(
    manager: sessions.SessionManager, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = build_session(manager, context)

    original = files._Export.children

    def broken(self: files._Export, fd: int, prefix: str, depth: int) -> None:
        if self.prefix == "work/":
            raise FilesError("unavailable")
        original(self, fd, prefix, depth)

    monkeypatch.setattr(files._Export, "children", broken)
    session = context.state.session(OLD)
    assert session is not None
    with pytest.raises(FilesError):
        contexts.export_context(
            session, task_id=TASK, agent_version=VERSION, created_at="x", workspace_max=1, transcript_max=1 << 20
        )
    assert not [p for p in base.iterdir() if p.name.startswith(".files-")]


def test_a_stream_part_shorter_than_its_length_ends_early(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "part").write_bytes(b"abc")
    stream = files.Stream(b"H", [(str(stage / "part"), 10)], str(stage), b"T")
    assert b"".join(stream) == b"HabcT"
    assert not stage.exists()


# -- staging ---------------------------------------------------------------------------------------------------------


def exported_archive(manager: sessions.SessionManager, context: ControlContext, client: ControlClient) -> bytes:
    build_session(manager, context)
    reply = export(client)
    assert reply.status == 200
    body: bytes = reply.body
    return body


def test_stage_takes_an_exported_archive(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    raw = exported_archive(manager, context, control_client)
    reply = stage_raw(control_client, raw)
    assert reply.status == 200
    assert reply.json() == {"files": 9, "bytes": reply.json()["bytes"]}
    root = Path(contexts.stage_root(manager.paths.sessions_root))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    found = contexts.staged(manager.paths.sessions_root, SESSION)
    assert found is not None
    assert found.context["claude_session_id"] == CID
    # a second staging replaces the first; a refused one leaves it
    assert stage_raw(control_client, raw).status == 200
    assert stage_raw(control_client, b"garbage" * 100).json() == {"error": "request_invalid"}
    assert contexts.staged(manager.paths.sessions_root, SESSION) is not None
    assert sorted(p.name for p in root.iterdir()) == [SESSION]


@pytest.mark.parametrize(
    ("items", "symlink"),
    [
        ((("context.json", context_json()), ("other.txt", b"x")), None),
        ((("context.json", context_json()), ("transcript/settings.json", b"{}")), None),
        ((("context.json", context_json()), ("transcript/commands", None)), None),
        ((("context.json", context_json()), ("work", b"file")), None),
        ((("context.json", None),), None),
        ((("work", None),), None),
        ((("context.json", context_json()),), "work/link"),
        ((("context.json", context_json()),), "transcript/link"),
        ((("context.json", b"[" * 10),), None),
        ((("context.json", b"[" * 100_000),), None),
        # review no. 19: only the files of the conversation of `claude_session_id`
        ((("context.json", context_json()), (f"transcript/projects/{OLD_KEY}/other.jsonl", b"{}\n")), None),
        ((("context.json", context_json()), (f"transcript/projects/{OLD_KEY}/{CID}.jsonl/x", b"{}\n")), None),
        ((("context.json", context_json()), (f"transcript/projects/{OLD_KEY}/{CID}", b"file")), None),
        ((("context.json", context_json()), (f"transcript/projects/{OLD_KEY}", b"file")), None),
        ((("context.json", context_json()), ("transcript/projects/-elsewhere/a.jsonl", b"{}\n")), None),
        ((("context.json", context_json()), ("transcript/todos/foreign.json", b"[]")), None),
        ((("context.json", context_json()), (f"transcript/todos/{CID}-x", None)), None),
        ((("context.json", context_json()), ("transcript/file-history/other/a", b"x")), None),
        ((("context.json", context_json()), (f"transcript/file-history/{CID}", b"x")), None),
        ((("context.json", context_json()), ("transcript/projects", b"x")), None),
    ],
)
def test_stage_refuses_what_is_no_context_archive(
    manager: sessions.SessionManager,
    control_client: ControlClient,
    items: tuple[tuple[str, bytes | None], ...],
    symlink: str | None,
) -> None:
    reply = stage_raw(control_client, tar_of(*items, symlink=symlink))
    assert (reply.status, reply.json()) == (400, {"error": "request_invalid"})
    root = Path(contexts.stage_root(manager.paths.sessions_root))
    assert list(root.iterdir()) == []


def test_stage_accepts_damaged_transcript_lines_and_links_inside_the_workspace(
    manager: sessions.SessionManager, control_client: ControlClient
) -> None:
    """Review no. 13: integrity is the runner's AEAD, digest and manifest; Claude Code tolerates its own damaged
    lines. Review no. 5: links inside the workspace are staged as links."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name, data in (
            ("context.json", context_json()),
            (f"transcript/projects/{OLD_KEY}/{CID}.jsonl", b'{"a":1}\n{"b"\nnot json\n'),
            (f"transcript/todos/{CID}-agent.json", b"[]"),
            ("work/run.sh", b"#!/bin/sh\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            out.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("work/inside")
        link.type = tarfile.SYMTYPE
        link.linkname = "run.sh"
        out.addfile(link)
    assert stage_raw(control_client, raw.getvalue()).status == 200
    staged = Path(contexts.stage_root(manager.paths.sessions_root), SESSION)
    assert os.readlink(staged / "work" / "inside") == "run.sh"
    assert stat.S_IMODE((staged / "work" / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((staged / "transcript" / "todos" / f"{CID}-agent.json").stat().st_mode) == 0o600
    new_key = cwd_key(f"{manager.paths.sessions_root}/{SESSION}/work")
    assert (staged / "transcript" / "projects" / new_key / f"{CID}.jsonl").read_bytes().endswith(b"not json\n")


def test_stage_refuses_a_protected_name_and_a_big_context_file(
    manager: sessions.SessionManager, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = stage_raw(control_client, tar_of(("context.json", context_json()), ("work/AGENTS.md", b"x")))
    assert (reply.status, reply.json()) == (400, {"error": "path_invalid"})
    monkeypatch.setattr(contexts, "MAX_CONTEXT_JSON", 10)
    reply = stage_raw(control_client, tar_of(("context.json", context_json())))
    assert reply.json() == {"error": "request_invalid"}


@pytest.mark.parametrize(
    "changes",
    [
        {"v": 2},
        {"v": True},
        {"claude_session_id": "x"},
        {"session_id": None},
        {"task_id": "01A1EE56-5E00-7005-8F1B-340691D99F05"},
        {"cwd": "relative/work"},
        {"cwd": "/srv/../etc"},
        {"cwd": 3},
    ],
)
def test_context_file_rules(changes: dict[str, Any]) -> None:
    with pytest.raises(FilesError) as error:
        contexts.check_context(context_json(**changes))
    assert error.value.code == "request_invalid"
    for bad in (b"not json", b"[1]"):
        with pytest.raises(FilesError):
            contexts.check_context(bad)
    assert contexts.check_context(context_json())["v"] == 1


def test_stage_size_limits(
    manager: sessions.SessionManager, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = control_client.request(
        "POST",
        f"/v1/contexts/{SESSION}/stage",
        raw=b"x",
        headers={"Content-Type": "application/x-tar", "Content-Length": str(contexts.MAX_STAGE_BYTES + 1)},
    )
    assert (reply.status, reply.json()) == (413, {"error": "size_exceeded"})
    monkeypatch.setattr(contexts, "MAX_STAGE_BYTES", 3)
    reply = stage_raw(control_client, tar_of(("context.json", context_json())))
    assert (reply.status, reply.json()) == (413, {"error": "size_exceeded"})


def test_stage_of_an_existing_session(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    raw = exported_archive(manager, context, control_client)
    assert stage_raw(control_client, raw, OLD).json() == {"error": "state_conflict"}
    session = context.state.session(OLD)
    assert session is not None
    context.state.save_session(Session(OLD, session.uid, "0" * 64, "suspended", session.workspace, CID, "x"))
    assert stage_raw(control_client, raw, OLD).status == 200


def test_prune_removes_old_stagings(
    manager: sessions.SessionManager, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = manager.paths.sessions_root
    assert contexts.prune(root, time.time()) == []
    stage = Path(contexts.stage_root(root))
    stage.mkdir(parents=True)
    for name in ("old", "young", "gone"):
        (stage / name).mkdir()
    past = time.time() - contexts.STAGE_TTL_SECONDS - 10
    os.utime(stage / "old", (past, past))
    real = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path).endswith("/gone"):
            raise FileNotFoundError(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    assert contexts.prune(root, time.time()) == ["old"]
    monkeypatch.setattr(os, "lstat", real)
    os.utime(stage / "young", (past, past))
    manager.cleanup()
    assert sorted(p.name for p in stage.iterdir()) == ["gone"]


def test_a_staging_root_that_is_no_directory_is_unavailable(
    manager: sessions.SessionManager, control_client: ControlClient
) -> None:
    root = Path(contexts.stage_root(manager.paths.sessions_root))
    root.parent.mkdir(parents=True)
    root.write_text("x")
    reply = stage_raw(control_client, tar_of(("context.json", context_json())))
    assert (reply.status, reply.json()) == (503, {"error": "unavailable"})


def test_staged_of_a_missing_or_damaged_staging(manager: sessions.SessionManager) -> None:
    root = manager.paths.sessions_root
    assert contexts.staged(root, SESSION) is None
    path = Path(contexts.stage_root(root), SESSION)
    path.mkdir(parents=True)
    (path / "context.json").write_text("{}")
    assert contexts.staged(root, SESSION) is None


# -- restore ---------------------------------------------------------------------------------------------------------


def restore_request(**changes: Any) -> dict[str, Any]:
    body = request(restore={"claude_session_id": CID, "object_id": OBJECT}, initial_prompt=None, **changes)
    body.pop("initial_prompt")
    return body


def staged_from_export(
    manager: sessions.SessionManager, context: ControlContext, client: ControlClient
) -> tuple[Path, Path]:
    old = build_session(manager, context)
    reply = stage_raw(client, export(client).body)
    assert reply.status == 200, reply.body
    return old, Path(manager.paths.sessions_root, SESSION)


def test_restore_moves_transcript_and_workspace_into_the_new_sandbox(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    fake_system: FakeSystem,
    session_paths: sessions.Paths,
) -> None:
    old, base = staged_from_export(manager, context, control_client)
    reply = control_client.request("POST", "/v1/sessions", restore_request())
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body
    work = base / "work"
    key, old_key = cwd_key(str(work)), cwd_key(str(old / "work"))
    claude = base / "home" / ".claude"
    conversation = claude / "projects" / key / f"{CID}.jsonl"
    text = conversation.read_text()
    assert str(work) in text
    assert str(old) not in text
    assert json.loads(text.splitlines()[0])["cwd"] == str(work)
    assert stat.S_IMODE(conversation.stat().st_mode) == 0o600
    assert (claude / "projects" / key / CID / "tool-results" / "r1.txt").read_text() == f"result in {work}/a.txt"
    assert not (claude / "projects" / old_key).exists()
    assert (claude / "todos" / f"{CID}-agent-{CID}.json").exists()
    assert (claude / "file-history" / CID / "abc@v1").read_text() == "old"
    assert not (claude / "settings.json").exists()
    assert (work / "a.txt").read_text() == "alpha\n"
    assert (work / "src" / "b.py").exists()
    assert not (work / "AGENTS.md").exists()
    assert not (work / ".git").exists()
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    # review no. 5: execute bits, empty directories and links inside the workspace come back
    assert stat.S_IMODE((work / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((work / "a.txt").stat().st_mode) == 0o644
    assert (work / "empty").is_dir()
    assert not list((work / "empty").iterdir())
    assert (os.readlink(work / "inside"), os.readlink(work / "abs"), os.readlink(work / "srclink")) == (
        "a.txt",
        "src/b.py",
        "src",
    )
    assert not (work / "link").exists()
    assert not (work / "pipe").exists()
    assert (claude / "projects" / key / CID / "subagents" / "agent-1.jsonl").read_bytes() == b'{"type":"user"}\n'
    uid = fake_system.users[unit.user_name(SESSION)]
    chowned = {path for path, owner in fake_system.chowned if owner == uid}
    assert {
        str(conversation),
        str(work / "src" / "b.py"),
        str(work / "src"),
        str(claude),
        str(work / "inside"),
    } <= chowned
    assert json.loads((base / transcript.BASELINE_FILE).read_text()) == {
        "keys": ["session.output:msg_1"],
    }
    session = context.state.session(SESSION)
    assert session is not None
    assert session.claude_session_id == CID
    assert contexts.staged(manager.paths.sessions_root, SESSION) is None
    assert sessions.credential_provisioned(context, SESSION)
    argv = json.loads(Path(session_paths.launch_dir, f"{SESSION}.json").read_text())["argv"]
    assert argv[argv.index("--resume") + 1] == CID
    manager.stop(SESSION)


def test_restored_files_are_the_scanner_baseline_and_no_chat_flood(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture's scanner and follower as ``sandbox_ready`` starts them, here with the test user as session user
    (the fake system's session UID owns no file; the Linux suite runs the real capture)."""
    _, base = staged_from_export(manager, context, control_client)
    monkeypatch.setattr(sessions, "session_events", dict)
    manager.create(restore_request())
    scanner = WorkspaceScanner(context.journal, SESSION, str(base), os.getuid())
    scanner.start()
    scanner.scan()
    follower = TranscriptFollower(context.journal, context.state, SESSION, str(base), os.getuid())
    follower.finish()
    found, _ = context.journal.read(after=0, limit=1000)
    kinds = [record["kind"] for record in found if record["session_id"] == SESSION]
    assert "session.file" not in kinds
    assert "session.output" not in kinds
    # what the resumed session does next is reported
    (base / "work" / "a.txt").write_text("beta\n")
    scanner.scan()
    conversation = base / "home" / ".claude" / "projects" / cwd_key(str(base / "work")) / f"{CID}.jsonl"
    with conversation.open("ab") as handle:
        handle.write(line(assistant("msg_2", "resumed", str(base / "work"))))
    follower.finish()
    found, _ = context.journal.read(after=0, limit=1000)
    bodies = [(r["kind"], r["body"]) for r in found if r["session_id"] == SESSION and r["kind"] != "session.state"]
    assert [kind for kind, _ in bodies] == ["session.file", "session.output"]
    assert bodies[1][1]["message_id"] == "msg_2"
    manager.stop(SESSION)  # no capture thread outlives the test


def test_restore_needs_a_matching_staging(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient
) -> None:
    reply = control_client.request("POST", "/v1/sessions", restore_request())
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})
    assert context.state.session(SESSION) is None
    path = Path(contexts.stage_root(manager.paths.sessions_root), SESSION)
    path.mkdir(parents=True)
    (path / "context.json").write_bytes(context_json(claude_session_id="0" * 8 + CID[8:]))
    assert control_client.request("POST", "/v1/sessions", restore_request()).json() == {"error": "state_conflict"}
    found, _ = context.journal.read(after=0, limit=10)
    assert found == []


def test_restore_and_resume_together_are_invalid(manager: sessions.SessionManager) -> None:
    with pytest.raises(sessions.SessionError) as error:
        manager.create(restore_request(resume_claude_session_id=CID))
    assert error.value.code == "request_invalid"


def test_a_failed_restore_fails_the_sandbox_and_drops_the_staging(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient, fake_system: FakeSystem
) -> None:
    staged_from_export(manager, context, control_client)
    fake_system.fail_add = True
    with pytest.raises(sessions.SessionError) as error:
        manager.create(restore_request())
    assert error.value.code == "sandbox_failed"
    assert contexts.staged(manager.paths.sessions_root, SESSION) is None


def test_a_restore_with_an_incomplete_image_drops_the_staging(
    manager: sessions.SessionManager, context: ControlContext, control_client: ControlClient, session_paths: Any
) -> None:
    staged_from_export(manager, context, control_client)
    Path(session_paths.rules_file).unlink()
    with pytest.raises(sessions.SessionError) as error:
        manager.create(restore_request())
    assert error.value.code == "sandbox_failed"
    assert contexts.staged(manager.paths.sessions_root, SESSION) is None


def test_restore_without_transcript_or_workspace(
    manager: sessions.SessionManager, control_client: ControlClient
) -> None:
    assert stage_raw(control_client, tar_of(("context.json", context_json()))).status == 200
    manager.create(restore_request())
    base = Path(manager.paths.sessions_root, SESSION)
    assert list((base / "work").iterdir()) == []
    assert json.loads((base / transcript.BASELINE_FILE).read_text()) == {"keys": []}
    manager.stop(SESSION)  # no capture thread outlives the test


def test_the_stage_rewrites_paths_and_names_and_the_restore_only_moves(tmp_path: Path) -> None:
    """Review no. 9: the byte rewrite and the baseline happen at the stage; the restore renames and replaces."""
    old_cwd = f"/srv/sessions/{OLD}/work"
    old_key = cwd_key(old_cwd)
    staging = tmp_path / "staging"
    project = staging / "transcript" / "projects" / old_key
    (project / CID / f"x-{old_key}").mkdir(parents=True)
    (project / f"{CID}.jsonl").write_text(json.dumps({"cwd": old_cwd}) + "\n")
    (project / "dir.jsonl").mkdir()
    base = tmp_path / "srv" / SESSION
    contexts._rewrite(str(staging), {"cwd": old_cwd}, str(base))
    new_key = cwd_key(str(base / "work"))
    staged_project = staging / "transcript" / "projects" / new_key
    assert json.loads((staged_project / f"{CID}.jsonl").read_text()) == {"cwd": str(base / "work")}
    assert (staged_project / CID / f"x-{new_key}").is_dir()
    assert json.loads((staging / transcript.BASELINE_FILE).read_text()) == {"keys": []}
    # the sandbox: an existing conversation file and a foreign project stay apart from what is replaced
    claude = base / "home" / ".claude"
    (base / "work").mkdir(parents=True)
    (claude / "projects" / "-elsewhere").mkdir(parents=True)
    (claude / "projects" / new_key).mkdir()
    (claude / "projects" / new_key / f"{CID}.jsonl").write_text("stale\n")
    (claude / "projects" / new_key / "unrelated.jsonl").write_text("kept\n")
    seen: list[str] = []
    contexts.restore(contexts.Staged(str(staging), {"cwd": old_cwd}), str(base), seen.append)
    assert json.loads((claude / "projects" / new_key / f"{CID}.jsonl").read_text()) == {"cwd": str(base / "work")}
    assert (claude / "projects" / new_key / "unrelated.jsonl").read_text() == "kept\n"
    assert (claude / "projects" / "-elsewhere").is_dir()
    assert (base / transcript.BASELINE_FILE).exists()
    assert str(claude / "projects" / new_key / f"{CID}.jsonl") in seen


def test_create_with_restore_does_no_heavy_work(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review no. 9: no rewrite and no transcript parsing inside `POST /v1/sessions` (manager lock, create timeout)."""
    staged_from_export(manager, context, control_client)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("heavy work inside the create")

    monkeypatch.setattr(contexts, "replace_bytes", forbidden)
    monkeypatch.setattr(transcript, "baseline_keys", forbidden)
    reply = control_client.request("POST", "/v1/sessions", restore_request())
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body
    manager.stop(SESSION)


def test_a_suspended_session_restored_under_its_own_id_gets_the_staged_state(
    manager: sessions.SessionManager,
    context: ControlContext,
    control_client: ControlClient,
    fake_system: FakeSystem,
) -> None:
    """Review no. 14 (A -> B -> A): the session ran here, moved away and comes back: its directories are replaced."""
    staged_from_export(manager, context, control_client)
    assert control_client.request("POST", "/v1/sessions", restore_request()).status == 200
    manager.stop(SESSION)
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(Session(SESSION, session.uid, "0" * 64, "suspended", session.workspace, CID, "x"))
    base = Path(manager.paths.sessions_root, SESSION)
    work = base / "work"
    (work / "a.txt").write_text("stale here\n")
    (work / "left-over.txt").write_text("from before\n")
    conversation = base / "home" / ".claude" / "projects" / cwd_key(str(work)) / f"{CID}.jsonl"
    conversation.write_text("stale\n")
    # what the session did elsewhere comes back as a context of its own ID
    raw = tar_of(
        ("context.json", context_json(session_id=SESSION, cwd=str(work))),
        (f"transcript/projects/{cwd_key(str(work))}/{CID}.jsonl", line(prompt("elsewhere", str(work)))),
        ("work/a.txt", b"from elsewhere\n"),
    )
    assert stage_raw(control_client, raw).status == 200
    reply = control_client.request("POST", "/v1/sessions", restore_request())
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body
    assert (work / "a.txt").read_text() == "from elsewhere\n"
    assert not (work / "left-over.txt").exists()
    assert json.loads(conversation.read_text())["message"]["content"] == "elsewhere"
    assert contexts.staged(manager.paths.sessions_root, SESSION) is None
    resumed = context.state.session(SESSION)
    assert resumed is not None
    assert resumed.state == "sandbox_ready"
    # without a staging the restore of the suspended session is refused and nothing changes
    manager.stop(SESSION)
    context.state.save_session(Session(SESSION, session.uid, "0" * 64, "suspended", session.workspace, CID, "x"))
    assert control_client.request("POST", "/v1/sessions", restore_request()).json() == {"error": "state_conflict"}
    assert fake_system.users[unit.user_name(SESSION)] == session.uid


def test_replace_bytes_across_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "CHUNK", 3)
    path = tmp_path / "f"
    path.write_bytes(b"xx/old/yy/old/z-key-")
    contexts.replace_bytes(str(path), [(b"/old/", b"/new/"), (b"-key-", b"-KEY-"), (b"same", b"same")])
    assert path.read_bytes() == b"xx/new/yy/new/z-KEY-"
    contexts.replace_bytes(str(path), [(b"a", b"a")])
    assert path.read_bytes() == b"xx/new/yy/new/z-KEY-"


# -- transcript baseline and task lookup -----------------------------------------------------------------------------


def test_transcript_baseline_files(tmp_path: Path) -> None:
    assert transcript.load_baseline(str(tmp_path)) == set()
    transcript.write_baseline(str(tmp_path), {"b", "a"})
    assert stat.S_IMODE((tmp_path / transcript.BASELINE_FILE).stat().st_mode) == 0o600
    assert transcript.load_baseline(str(tmp_path)) == {"a", "b"}
    for content in ("not json", "[]", '{"keys": 3}', '{"keys": ["x", 1]}'):
        (tmp_path / transcript.BASELINE_FILE).write_text(content)
        assert transcript.load_baseline(str(tmp_path)) == ({"x"} if "x" in content else set())


def test_baseline_keys_include_unfinished_tool_calls(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    use = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/a"}}
    message = {"id": "msg_1", "role": "assistant", "content": [use]}
    path.write_bytes(b"not json\n" + line({"type": "assistant", "message": message, "uuid": "1"}))
    assert transcript.baseline_keys([str(path)]) == {"session.tool:toolu_1"}


def test_task_of_a_session(manager: sessions.SessionManager) -> None:
    assert manager.task_id(SESSION) is None
    manager._write_meta(SESSION, {"identity": "x"})
    assert manager.task_id(SESSION) is None
    manager._write_meta(SESSION, {"identity": {"task_id": TASK}})
    assert manager.task_id(SESSION) == TASK


def test_route_module_lists_both_endpoints() -> None:
    assert [(route.method, route.path) for route in context_route.ROUTES] == [
        ("POST", "/v1/sessions/{id}/context/export"),
        ("POST", "/v1/contexts/{id}/stage"),
    ]
