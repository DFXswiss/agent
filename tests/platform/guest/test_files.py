"""U-GST-control: file import and export of a session workspace (03-api.md section 7.1, 05-runner.md section 12.1;
ATK-34 and ATK-40 at unit level) through the real control socket, and the race and edge cases of ``files.py`` called
directly. The session user is the test user here; ownership by another session user is covered by the Linux suite
(``guest_linux/test_files_linux.py``)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_guest import files, tmux
from agent_guest.control import ControlContext
from agent_guest.control_routes import capture as capture_routes
from agent_guest.control_routes.capture import Capture
from agent_guest.state import Session

from .conftest import ControlClient, make_session

SID = "01a1ee56-5e00-7003-8f1b-340691d99f10"


def tar_bytes(*members: tuple[str, bytes, bytes | None], fmt: int = tarfile.PAX_FORMAT, **extra: Any) -> bytes:
    """Members `(name, type, data)`; `data` None for non-files; `extra` sets attributes of every member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as out:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o777
            info.size = len(data) if data is not None else 0
            for key, value in extra.items():
                setattr(info, key, value)
            out.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def f(name: str, data: bytes = b"x") -> tuple[str, bytes, bytes]:
    return (name, tarfile.REGTYPE, data)


def d(name: str) -> tuple[str, bytes, None]:
    return (name, tarfile.DIRTYPE, None)


@pytest.fixture(autouse=True)
def quiet_capture(context: ControlContext, short_tmp: Path) -> Iterator[Capture]:
    """The capture an import attaches (``Capture.import_guard``) without a scan loop of its own; ended afterwards."""
    created = capture_routes.install(
        context, Capture(context, tmux.Tmux(scope=False), fifo_dir=str(short_tmp), scan_seconds=3600)
    )
    yield created
    for session_id in list(created.sessions):
        created.agent_ended(session_id)


@pytest.fixture
def ws(tmp_path: Path, context: ControlContext) -> Iterator[Session]:
    """A working session of the test user with an empty workspace."""
    base = tmp_path / "sessions" / SID
    (base / "work").mkdir(parents=True)
    session = Session(
        id=SID,
        uid=os.getuid(),
        token_sha256="0" * 64,
        state="working",
        workspace=str(base / "work"),
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context.state.save_session(session)
    yield session
    assert [p.name for p in base.iterdir() if p.name.startswith(".files-")] == []


def work(session: Session) -> Path:
    return Path(session.workspace)


def do_import(client: ControlClient, data: bytes, directory: str = "repos/o/r", replace: str = "true") -> Any:
    target = f"/v1/sessions/{SID}/files/import?dir={directory}&replace={replace}"
    return client.request("POST", target, raw=data, headers={"Content-Type": "application/x-tar"})


def do_export(client: ControlClient, body: dict[str, Any]) -> Any:
    return client.request("POST", f"/v1/sessions/{SID}/files/export", body)


def unpack(raw: bytes) -> tuple[dict[str, Any], dict[str, bytes], list[str]]:
    """Manifest, file contents and member names of an export."""
    contents: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        names = archive.getnames()
        for member in archive.getmembers():
            reader = archive.extractfile(member)
            assert reader is not None
            contents[member.name] = reader.read()
            assert (member.mode, member.uid, member.mtime) == (0o600, 0, 0)
    manifest = json.loads(contents.pop(files.MANIFEST))
    return manifest, contents, names


# -- import --------------------------------------------------------------------------------------------------------


def test_import_creates_files_and_directories_with_session_owner_and_modes(
    ws: Session, control_client: ControlClient
) -> None:
    data = tar_bytes(
        d("./"), d("./src"), f("./src/app.py", b"x = 1\n"), f("README.md", b"hi\n"), d("empty"), f("x/y", b""), d("x")
    )
    reply = do_import(control_client, data)
    assert (reply.status, reply.json()) == (200, {"files": 3, "bytes": 9})
    root = work(ws) / "repos" / "o" / "r"
    assert (root / "src" / "app.py").read_bytes() == b"x = 1\n"
    assert (root / "README.md").read_bytes() == b"hi\n"
    assert (root / "empty").is_dir()
    for path in (root / "src" / "app.py", root / "README.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_uid == ws.uid
    for path in (work(ws) / "repos", work(ws) / "repos" / "o", root, root / "src", root / "empty"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_import_replace_swaps_the_whole_directory_even_a_link(
    ws: Session, control_client: ControlClient, tmp_path: Path
) -> None:
    assert do_import(control_client, tar_bytes(f("old.txt"), f("keep/a"))).status == 200
    assert do_import(control_client, tar_bytes(f("new.txt", b"n"))).status == 200
    root = work(ws) / "repos" / "o" / "r"
    assert sorted(p.name for p in root.iterdir()) == ["new.txt"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("stay")
    os.rename(root, work(ws) / "moved")
    root.symlink_to(outside)
    assert do_import(control_client, tar_bytes(f("victim", b"evil"))).status == 200
    assert (outside / "victim").read_text() == "stay"
    assert not root.is_symlink()
    assert (root / "victim").read_bytes() == b"evil"


def test_import_without_replace_needs_a_missing_or_empty_directory(ws: Session, control_client: ControlClient) -> None:
    assert do_import(control_client, tar_bytes(f("a")), "one", "false").status == 200
    reply = do_import(control_client, tar_bytes(f("b")), "one", "false")
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})
    assert sorted(p.name for p in (work(ws) / "one").iterdir()) == ["a"]
    (work(ws) / "two").mkdir()
    assert do_import(control_client, tar_bytes(f("b")), "two", "false").status == 200
    (work(ws) / "three").write_text("file")
    assert do_import(control_client, tar_bytes(f("b")), "three", "false").json() == {"error": "state_conflict"}


@pytest.mark.parametrize(
    ("members", "code"),
    [
        ([f("a"), ("link", tarfile.SYMTYPE, None)], "request_invalid"),
        ([f("a"), ("hard", tarfile.LNKTYPE, None)], "request_invalid"),
        ([("dev", tarfile.CHRTYPE, None)], "request_invalid"),
        ([("blk", tarfile.BLKTYPE, None)], "request_invalid"),
        ([("fifo", tarfile.FIFOTYPE, None)], "request_invalid"),
        ([f("/etc/passwd")], "path_invalid"),
        ([f("../escape")], "path_invalid"),
        ([f("a/../../b")], "path_invalid"),
        ([f(".git/hooks/post-checkout")], "path_invalid"),
        # names Claude Code reads as instructions or configuration (05 section 5.3, ATS-03 B-09)
        ([f("repo/.claude/settings.json")], "path_invalid"),
        ([f("repo/.CLAUDE/agents/x.md")], "path_invalid"),
        ([f("repo/.mcp.json")], "path_invalid"),
        ([f("repo/CLAUDE.md")], "path_invalid"),
        ([f("repo/sub/claude.md")], "path_invalid"),
        ([f("repo/CLAUDE.local.md")], "path_invalid"),
        ([f("repo/AGENTS.md")], "path_invalid"),
        ([f("repo/.claude.json")], "path_invalid"),
        ([f("repo/.gitconfig")], "path_invalid"),
        ([f("a\x01b")], "path_invalid"),
        ([f("a"), f("a")], "request_invalid"),
        ([f("a"), f("a/b")], "request_invalid"),
        ([f("a/b"), f("a")], "request_invalid"),
    ],
)
def test_import_refuses_malicious_archives_and_leaves_the_workspace_alone(
    ws: Session, control_client: ControlClient, members: list[tuple[str, bytes, bytes | None]], code: str
) -> None:
    reply = do_import(control_client, tar_bytes(*members, linkname="/etc/passwd"))
    assert reply.json() == {"error": code}
    assert list(work(ws).iterdir()) == []


def test_import_refuses_sparse_and_broken_archives(ws: Session, control_client: ControlClient) -> None:
    sparse = tar_bytes(("s", tarfile.GNUTYPE_SPARSE, None), fmt=tarfile.GNU_FORMAT)
    assert do_import(control_client, sparse).json() == {"error": "request_invalid"}
    assert do_import(control_client, b"").json() == {"error": "request_invalid"}
    assert do_import(control_client, b"no tar" * 200).json() == {"error": "request_invalid"}
    whole = tar_bytes(f("a", b"x" * 5000), f("b", b"y" * 5000))
    assert do_import(control_client, whole[:3000]).json() == {"error": "request_invalid"}
    broken = bytearray(tar_bytes(f("a"), f("b" * 120)))
    start = bytes(broken).index(b"path=" + b"b" * 120) // 512 * 512 + 512
    broken[start + 148 : start + 156] = b"garbage!"
    assert do_import(control_client, bytes(broken)).json() == {"error": "request_invalid"}
    assert list(work(ws).iterdir()) == []


def test_import_limits(ws: Session, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "MAX_ENTRIES", 2)
    assert do_import(control_client, tar_bytes(f("a"), f("b"), f("c"))).json() == {"error": "size_exceeded"}
    monkeypatch.setattr(files, "MAX_ENTRIES", 100)
    monkeypatch.setattr(files, "MAX_IMPORT_BYTES", 10)
    assert do_import(control_client, tar_bytes(f("a", b"x" * 6), f("b", b"y" * 6))).json() == {"error": "size_exceeded"}
    target = f"/v1/sessions/{SID}/files/import?dir=x&replace=true"
    huge = {"Content-Type": "application/x-tar", "Content-Length": str(512 * 1024 * 1024 + 1)}
    reply = control_client.request("POST", target, raw=b"", headers=huge)
    assert (reply.status, reply.json()) == (413, {"error": "size_exceeded"})
    assert list(work(ws).iterdir()) == []


def test_import_target_paths(ws: Session, control_client: ControlClient, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (work(ws) / "repos").symlink_to(outside)
    assert do_import(control_client, tar_bytes(f("a")), "repos/o/r").json() == {"error": "path_invalid"}
    assert list(outside.iterdir()) == []
    (work(ws) / "file").write_text("x")
    assert do_import(control_client, tar_bytes(f("a")), "file/r").json() == {"error": "path_invalid"}
    # refused before the body is read: a body the server never reads is kept small
    for bad in ("../x", "a/.git", "a/./b", "a/", "a/.claude", "CLAUDE.md"):
        assert do_import(control_client, b"", bad).json() == {"error": "path_invalid"}
    assert do_import(control_client, b"", "/abs").json()["error"] == "request_invalid"
    assert do_import(control_client, b"", "x", "maybe").json()["error"] == "request_invalid"


def test_sessions_that_are_unknown_or_ended(context: ControlContext, control_client: ControlClient) -> None:
    assert do_import(control_client, b"").json() == {"error": "not_found"}
    assert do_export(control_client, {"dir": "x"}).json() == {"error": "not_found"}
    context.state.save_session(make_session(SID, state="stopped"))
    assert do_import(control_client, b"").json() == {"error": "state_conflict"}
    assert do_export(control_client, {"dir": "x"}).json() == {"error": "state_conflict"}


def test_a_missing_workspace_is_not_found(ws: Session, control_client: ControlClient) -> None:
    os.rmdir(ws.workspace)
    assert do_import(control_client, tar_bytes(f("a"))).json() == {"error": "not_found"}
    assert do_export(control_client, {"dir": "x"}).json() == {"error": "not_found"}
    os.mkdir(ws.workspace)


# -- export --------------------------------------------------------------------------------------------------------


def populate(root: Path, outside: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_bytes(b"x = 1\n")
    (root / "README.md").write_bytes(b"hi\n")
    (root / "empty").write_bytes(b"")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")
    (root / "CLAUDE.md").write_text("protected: never exported (ATS-03)")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    (root / "link").symlink_to(outside / "secret")
    (root / "dirlink").symlink_to(outside)
    os.mkfifo(root / "fifo")
    (root / "bad\x01name").write_text("x")


def test_export_streams_manifest_first_then_the_files(
    ws: Session, control_client: ControlClient, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("never")
    root = work(ws) / "repos" / "o" / "r"
    populate(root, outside)
    reply = do_export(control_client, {"dir": "repos/o/r"})
    assert reply.status == 200
    assert reply.headers["Content-Type"] == "application/x-tar"
    manifest, contents, names = unpack(reply.body)
    assert names[0] == files.MANIFEST
    raw_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert reply.headers["X-Manifest-Sha256"] == hashlib.sha256(raw_manifest).hexdigest()
    assert manifest == {
        "files": [
            {"path": "README.md", "size": 3, "sha256": hashlib.sha256(b"hi\n").hexdigest()},
            {"path": "empty", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
            {"path": "src/app.py", "size": 6, "sha256": hashlib.sha256(b"x = 1\n").hexdigest()},
        ],
        "skipped": [
            {"path": "dirlink", "reason": "symlink"},
            {"path": "fifo", "reason": "special"},
            {"path": "link", "reason": "symlink"},
        ],
    }
    assert contents == {"README.md": b"hi\n", "empty": b"", "src/app.py": b"x = 1\n"}


def test_export_of_listed_paths(ws: Session, control_client: ControlClient, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("never")
    root = work(ws) / "repo"
    populate(root, outside)
    paths = ["src", "src/app.py", "README.md", "gone.txt", "missing/dir/x", "dirlink/secret", "link", "README.md/x"]
    reply = do_export(control_client, {"dir": "repo", "paths": paths})
    manifest, contents, _ = unpack(reply.body)
    assert [item["path"] for item in manifest["files"]] == ["README.md", "src/app.py"]
    assert manifest["skipped"] == [
        {"path": "dirlink/secret", "reason": "symlink"},
        {"path": "link", "reason": "symlink"},
    ]
    assert set(contents) == {"README.md", "src/app.py"}
    for bad in (["../x"], [".git/config"]):
        assert do_export(control_client, {"dir": "repo", "paths": bad}).json() == {"error": "path_invalid"}


def test_export_directory_errors(ws: Session, control_client: ControlClient, tmp_path: Path) -> None:
    assert do_export(control_client, {"dir": "nothing"}).json() == {"error": "not_found"}
    (work(ws) / "file").write_text("x")
    assert do_export(control_client, {"dir": "file"}).json() == {"error": "path_invalid"}
    (work(ws) / "ln").symlink_to(tmp_path)
    assert do_export(control_client, {"dir": "ln"}).json() == {"error": "path_invalid"}
    assert do_export(control_client, {"dir": "a/../b"}).json() == {"error": "path_invalid"}
    assert do_export(control_client, {"dir": "x", "extra": 1}).json()["error"] == "request_invalid"


def test_export_skips_foreign_unreadable_and_too_deep_entries(
    ws: Session, context: ControlContext, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = work(ws) / "r"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "b" / "c" / "deep.txt").write_text("deep")
    (root / "locked.txt").write_text("x")
    (root / "locked.txt").chmod(0)
    monkeypatch.setattr(files, "MAX_DEPTH", 2)
    manifest, _, _ = unpack(do_export(control_client, {"dir": "r"}).body)
    assert manifest == {
        "files": [],
        "skipped": [{"path": "a/b/c", "reason": "unreadable"}, {"path": "locked.txt", "reason": "unreadable"}],
    }
    (root / "locked.txt").chmod(0o600)
    context.state.save_session(Session(**{**vars(ws), "uid": ws.uid + 1}))
    manifest, _, _ = unpack(do_export(control_client, {"dir": "r", "paths": ["locked.txt"]}).body)
    assert manifest == {"files": [], "skipped": [{"path": "locked.txt", "reason": "unreadable"}]}


def test_export_limits_remove_the_staging_directory(
    ws: Session, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = work(ws) / "r"
    root.mkdir()
    (root / "a").write_bytes(b"x" * 10)
    (root / "b").write_bytes(b"y" * 10)
    monkeypatch.setattr(files, "MAX_EXPORT_BYTES", 15)
    assert do_export(control_client, {"dir": "r"}).json() == {"error": "size_exceeded"}
    monkeypatch.setattr(files, "MAX_EXPORT_BYTES", 100)
    monkeypatch.setattr(files, "MAX_ENTRIES", 1)
    assert do_export(control_client, {"dir": "r"}).json() == {"error": "size_exceeded"}


def test_a_large_export_is_spooled_and_streamed(ws: Session, control_client: ControlClient) -> None:
    root = work(ws) / "r"
    root.mkdir()
    big = os.urandom(3 * 1024 * 1024 + 17)
    (root / "big.bin").write_bytes(big)
    _, contents, _ = unpack(do_export(control_client, {"dir": "r"}).body)
    assert contents == {"big.bin": big}


def test_import_then_export_round_trip(ws: Session, control_client: ControlClient) -> None:
    data = tar_bytes(f("a.txt", b"A"), d("dir"), f("dir/b.bin", bytes(range(256))))
    assert do_import(control_client, data, "repos/o/r").status == 200
    manifest, contents, _ = unpack(do_export(control_client, {"dir": "repos/o/r"}).body)
    assert contents == {"a.txt": b"A", "dir/b.bin": bytes(range(256))}
    assert manifest["skipped"] == []


# -- races and helpers, called directly ----------------------------------------------------------------------------


def test_paths_must_be_valid_utf8() -> None:
    assert files.path_ok("a/b")
    assert files.path_ok("docs/CLAUDE.md.txt")
    assert files.path_ok("claude/x")
    assert not files.path_ok("a\udcff")
    assert not files.path_ok("x" * 4097)


def test_owner_of_an_unknown_user_is_unavailable(ws: Session) -> None:
    with pytest.raises(files.FilesError) as caught:
        files.owner_of(Session(**{**vars(ws), "uid": 2**31 - 3}))
    assert caught.value.code == "unavailable"


def test_walk_tolerates_a_directory_created_concurrently(ws: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    real = files.open_dir
    calls: list[str] = []

    def racing(parent: int, name: str) -> int:
        calls.append(name)
        if len(calls) == 1:
            os.mkdir(name, dir_fd=parent)  # the session creates it after our first look
            raise files.FilesError("not_found")
        return real(parent, name)

    monkeypatch.setattr(files, "open_dir", racing)
    root = os.open(ws.workspace, files.DIR_FLAGS)
    try:
        fd = files.walk(root, ["new"], create=files.owner_of(ws))
        os.close(fd)
    finally:
        os.close(root)
    assert (work(ws) / "new").is_dir()


def test_open_dir_passes_other_errors_on(ws: Session) -> None:
    root = os.open(ws.workspace, files.DIR_FLAGS)
    try:
        with pytest.raises(OSError, match="too long"):
            files.open_dir(root, "x" * 1000)
    finally:
        os.close(root)


def test_install_gives_up_when_the_target_keeps_coming_back(ws: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    stage = files.make_stage(ws)
    stage_fd = os.open(stage, files.DIR_FLAGS)
    os.mkdir("tree", dir_fd=stage_fd)
    real = os.rename

    def refuse(src: str, dst: str, **kw: Any) -> None:
        if src == "tree":
            raise OSError("exists again")
        real(src, dst, **kw)

    monkeypatch.setattr(os, "rename", refuse)
    try:
        with pytest.raises(files.FilesError) as caught:
            files._install(ws, ["t"], stage_fd, files.owner_of(ws), replace=True)
        assert caught.value.code == "state_conflict"
    finally:
        os.close(stage_fd)
        files.remove_stage(stage)


def test_copy_refuses_a_member_that_ends_early(tmp_path: Path) -> None:
    fd = os.open(tmp_path / "out", os.O_WRONLY | os.O_CREAT)
    try:
        with pytest.raises(files.FilesError):
            files._copy(io.BytesIO(b"ab"), 5, fd)
    finally:
        os.close(fd)


def test_export_races(ws: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = work(ws)
    (root / "f").write_text("x")
    (root / "ln").symlink_to(tmp_path)
    os.mkfifo(root / "fifo")
    stage = files.make_stage(ws)
    fd = os.open(root, files.DIR_FLAGS)
    try:
        with tarfile.open(os.path.join(stage, "t.tar"), "w") as archive:
            export = files._Export(ws, stage, archive)
            export.regular(fd, "ln", "ln")  # a file replaced by a link after the stat
            export.regular(fd, "gone", "gone")
            export.regular(fd, "fifo", "fifo")  # a file replaced by a FIFO
            export.entry(fd, "vanished", "vanished", 0)
            export.directory(fd, "vanished", "vanished", 0)
            export.directory(fd, "ln", "ln2", 0)
            export.directory(fd, "f", "f2", 0)  # a directory replaced by a file
            assert not files._is_link(fd, "vanished")
            real_read = os.read

            def failing(handle: int, size: int) -> bytes:
                raise OSError("io error")

            monkeypatch.setattr(os, "read", failing)
            export.regular(fd, "f", "f")
            monkeypatch.setattr(os, "read", real_read)
        assert export.skipped == [
            {"path": "ln", "reason": "symlink"},
            {"path": "fifo", "reason": "special"},
            {"path": "ln2", "reason": "symlink"},
            {"path": "f", "reason": "unreadable"},
        ]
        assert export.files == []
    finally:
        os.close(fd)
        files.remove_stage(stage)


def test_an_export_stream_closed_early_removes_the_staging_directory(ws: Session) -> None:
    (work(ws) / "r").mkdir()
    (work(ws) / "r" / "a").write_text("a")
    exported = files.export_tar(ws, "r")
    assert exported.manifest["files"][0]["path"] == "a"
    stream = iter(exported.stream)
    assert next(stream)
    stream.close()  # type: ignore[attr-defined]
    stage_dirs = [p for p in work(ws).parent.iterdir() if p.name.startswith(".files-")]
    assert stage_dirs == []
    unread = files.export_tar(ws, "r")
    del unread  # the client went away before the body was sent
    assert [p for p in work(ws).parent.iterdir() if p.name.startswith(".files-")] == []


def test_extended_header_bombs_and_backslashes(ws: Session, control_client: ControlClient) -> None:
    huge = tar_bytes(f("d/" + "a" * (files.MAX_HEADER + 10)))
    with pytest.raises(files.FilesError) as caught:  # directly: the server refuses before reading the large body
        files.import_tar(ws, "x", io.BytesIO(huge), replace=True)
    assert caught.value.code == "size_exceeded"
    assert do_import(control_client, tar_bytes(f("a\\b"))).json() == {"error": "path_invalid"}
    assert not files.path_ok("x\\y")
    assert list(work(ws).iterdir()) == []


def test_export_counts_every_entry(ws: Session, control_client: ControlClient, monkeypatch: pytest.MonkeyPatch) -> None:
    root = work(ws) / "r"
    root.mkdir()
    for index in range(3):
        (root / f"ln{index}").symlink_to("/etc")
    monkeypatch.setattr(files, "MAX_ENTRIES", 2)
    assert do_export(control_client, {"dir": "r"}).json() == {"error": "size_exceeded"}
