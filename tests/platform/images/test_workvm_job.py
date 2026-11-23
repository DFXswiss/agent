"""U-RUN-workvm helper: the mirror subcommands `apply` and `pack` of the work VM's `workvm-job`
(docs/platform/spec/13-work-vm.md sections 3 and 5). The helper is plain python3 stdlib; it is imported here from its
path and run against a temporary directory standing in for `/workspace` (no systemd, no VM). The formats are those of
`agent_runner.workvm.sync`, so a round trip runs both sides."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_guest import policy
from agent_runner.workvm import sync

HELPER = Path(__file__).resolve().parents[3] / "images" / "workvm" / "files" / "usr" / "local" / "sbin" / "workvm-job"
MTIME = 1_700_000_000


def load_helper() -> Any:
    loader = importlib.machinery.SourceFileLoader("workvm_job", str(HELPER))
    spec = importlib.util.spec_from_loader("workvm_job", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = load_helper()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(module, "WORKSPACE", str(workspace))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return module


def ws(job: Any) -> Path:
    return Path(job.WORKSPACE)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def push_tar(
    deletions: object = (),
    files: Mapping[str, bytes] | None = None,
    *,
    extra: Callable[[tarfile.TarFile], None] | None = None,
    first: str = ".workvm-deletions.json",
) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        data = json.dumps(list(deletions) if isinstance(deletions, tuple) else deletions).encode()
        info = tarfile.TarInfo(first)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for name, content in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), 0o644, MTIME
            archive.addfile(info, io.BytesIO(content))
        if extra is not None:
            extra(archive)
    buffer.seek(0)
    return buffer


def apply(job: Any, root: str, source: io.BytesIO) -> dict[str, int]:
    out = io.StringIO()
    result: dict[str, int] = job.apply(root, source, out)
    assert json.loads(out.getvalue()) == result
    return result


def pack(job: Any, root: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    out = io.BytesIO()
    listing = job.pack(root, out)
    out.seek(0)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=out, mode="r|") as archive:
        first = archive.next()
        assert first is not None
        assert first.name == ".workvm-pack.json"
        handle = archive.extractfile(first)
        assert handle is not None
        assert json.loads(handle.read()) == listing
        while (member := archive.next()) is not None:
            assert member.isreg()
            assert member.mode == 0o644
            reader = archive.extractfile(member)
            assert reader is not None
            files[member.name] = reader.read()
    assert sorted(files) == sorted(listing["files"])
    return listing, files


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def tree(root: Path) -> dict[str, str]:
    """Every entry below `root`: file content, `<dir>` or `-> target` for links."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = "-> " + os.readlink(path)
        elif path.is_dir():
            result[name] = "<dir>"
        else:
            result[name] = path.read_text()
    return result


# -- rules ---------------------------------------------------------------------------------------------------------


def test_protected_names_match_the_guest_policy(job: Any) -> None:
    assert (policy.PROTECTED_DIRS | policy.PROTECTED_FILES) - {".git"} == job.PROTECTED
    assert job.MAX_ENTRIES == sync.MAX_ENTRIES
    assert job.MAX_BYTES == sync.MAX_BYTES
    assert job.DELETIONS == sync.DELETIONS
    assert job.PACK_MANIFEST == sync.PACK_MANIFEST


@pytest.mark.parametrize(
    ("path", "valid"),
    [
        ("a/b", True),
        ("", False),
        ("a//b", False),
        ("../a", False),
        ("a/.GIT/x", False),
        ("a\\b", False),
        ("a\nb", False),
        ("x\udcff", False),
        ("x" * 4097, False),
    ],
)
def test_path_rules(job: Any, path: str, valid: bool) -> None:
    assert job.path_ok(path) is valid


def test_root_must_be_dot_or_a_valid_relative_path(job: Any) -> None:
    assert job.root_parts(".") == []
    assert job.root_parts("repos/x") == ["repos", "x"]
    for root in ("/abs", "a/../b", "", ".git"):
        with pytest.raises(job.SyncError):
            job.root_parts(root)


# -- apply -----------------------------------------------------------------------------------------------------------


def test_apply_writes_files_and_directories_below_the_root(job: Any) -> None:
    def directory(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("./empty/")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)

    result = apply(job, "repos/x", push_tar((), {"a.txt": b"A", "src/m.c": b"int m;"}, extra=directory))
    assert result == {"files": 2, "bytes": 7, "deleted": 0}
    base = ws(job) / "repos" / "x"
    assert tree(base) == {"a.txt": "A", "empty": "<dir>", "src": "<dir>", "src/m.c": "int m;"}
    info = (base / "src" / "m.c").stat()
    assert int(info.st_mtime) == MTIME
    assert info.st_mode & 0o777 == 0o644
    assert not [name for name in os.listdir(base) if name.startswith(".workvm-")]


def test_apply_deletes_files_and_prunes_emptied_directories(job: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("keep me")
    base = ws(job)
    (base / "deep" / "er").mkdir(parents=True)
    (base / "deep" / "er" / "gone.txt").write_text("x")
    (base / "keep").mkdir()
    (base / "keep" / "gone.txt").write_text("x")
    (base / "keep" / "stays.txt").write_text("s")
    (base / "dir-now").mkdir()
    (base / "link").symlink_to(outside / "victim")
    (base / "via").symlink_to(outside)
    deletions = ["deep/er/gone.txt", "keep/gone.txt", "dir-now", "link", "via/victim", "missing", "no/such/dir"]
    assert apply(job, ".", push_tar(deletions)) == {"files": 0, "bytes": 0, "deleted": 3}
    assert tree(base) == {
        "dir-now": "<dir>",
        "keep": "<dir>",
        "keep/stays.txt": "s",
        "via": "-> " + str(outside),
    }
    assert (outside / "victim").read_text() == "keep me"


def test_apply_replaces_what_is_in_the_way(job: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("outside")
    base = ws(job)
    (base / "root-link").symlink_to(outside)
    (base / "root-link-dir").mkdir()
    (base / "file-in-way").write_text("f")
    (base / "dir-in-way" / "sub").mkdir(parents=True)
    (base / "dir-in-way" / "sub" / "x").write_text("x")
    (base / "link-file").symlink_to(outside / "target")
    files = {"file-in-way/now.txt": b"n", "dir-in-way": b"now a file", "link-file": b"replaced"}
    assert apply(job, ".", push_tar((), files))["files"] == 3
    assert tree(base)["file-in-way/now.txt"] == "n"
    assert tree(base)["dir-in-way"] == "now a file"
    assert tree(base)["link-file"] == "replaced"
    assert apply(job, "root-link/sub", push_tar((), {"a": b"1"}))["files"] == 1
    assert (base / "root-link").is_dir()
    assert not (base / "root-link").is_symlink()
    assert (outside / "target").read_text() == "outside"
    assert sorted(os.listdir(outside)) == ["target"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (lambda: push_tar((), first="other.json"), "deletions"),
        (lambda: io.BytesIO(b"\0" * 1024), "deletions"),
        (lambda: push_tar({"a": 1}), "invalid deletions"),
        (lambda: push_tar(["../a"]), "invalid deletions"),
        (lambda: push_tar([1]), "invalid deletions"),
        (lambda: push_tar((), {"../escape": b"1"}), "invalid member name"),
        (lambda: push_tar((), {"x/.git/config": b"1"}), "invalid member name"),
        (lambda: io.BytesIO(b"no tar" * 300), "invalid push tar"),
    ],
)
def test_apply_refuses_a_bad_push_tar(job: Any, source: Callable[[], io.BytesIO], message: str) -> None:
    with pytest.raises(job.SyncError, match=message):
        job.apply(".", source(), io.StringIO())


def test_apply_refuses_invalid_deletions_json(job: Any) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(job.DELETIONS)
        info.size = 3
        archive.addfile(info, io.BytesIO(b"[[["))
    buffer.seek(0)
    with pytest.raises(job.SyncError, match="invalid deletions"):
        job.apply(".", buffer, io.StringIO())


def test_apply_refuses_links_and_specials(job: Any) -> None:
    def link(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("evil")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc"
        archive.addfile(info)

    with pytest.raises(job.SyncError, match="only files and directories"):
        job.apply(".", push_tar((), extra=link), io.StringIO())
    assert not os.path.lexists(ws(job) / "evil")


def test_apply_bounds_members_and_headers(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job, "MAX_ENTRIES", 1)
    with pytest.raises(job.SyncError, match="too many members"):
        job.apply(".", push_tar((), {"a": b"1", "b": b"2"}), io.StringIO())

    def big(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("c")
        info.pax_headers = {"comment": "x" * (job.MAX_HEADER + 1)}
        archive.addfile(info, io.BytesIO(b""))

    monkeypatch.setattr(job, "MAX_ENTRIES", 10)
    with pytest.raises(job.SyncError, match="extended header"):
        job.apply(".", push_tar((), extra=big), io.StringIO())


def test_apply_removes_the_temporary_file_of_a_short_member(job: Any) -> None:
    source = push_tar((), {"big": b"x" * 5000})
    raw = source.getvalue()
    truncated = io.BytesIO(raw[: raw.index(b"x" * 100) + 100])
    with pytest.raises(job.SyncError):
        job.apply(".", truncated, io.StringIO())
    assert os.listdir(ws(job)) == []


def test_write_file_cleans_up_when_the_reader_fails(job: Any) -> None:
    info = tarfile.TarInfo("a")
    info.size = 10
    descriptor = os.open(ws(job), os.O_RDONLY)
    try:
        with pytest.raises(job.SyncError, match="ends early"):
            job.write_file(descriptor, "d/a", info, io.BytesIO(b"short"))
    finally:
        os.close(descriptor)
    assert tree(ws(job)) == {"d": "<dir>"}


# -- pack ------------------------------------------------------------------------------------------------------------


def populate(base: Path) -> None:
    (base / "src").mkdir(parents=True)
    (base / "src" / "main.c").write_text("main")
    (base / "README").write_text("readme")
    (base / "build").mkdir()
    (base / "build" / "out.o").write_text("obj")
    (base / "debug.log").write_text("log")
    (base / ".gitignore").write_text("build/\n*.log\n")
    (base / "CLAUDE.md").write_text("injected")
    (base / ".Claude").mkdir()
    (base / ".Claude" / "settings.json").write_text("{}")
    (base / "link").symlink_to("/etc/passwd")
    (base / "dirlink").symlink_to("/etc")
    os.mkfifo(base / "fifo")
    (base / "bad\nname").write_text("x")


def test_pack_without_a_repository_applies_gitignore_rules(job: Any) -> None:
    base = ws(job) / "proj"
    populate(base)
    listing, files = pack(job, "proj")
    assert files == {".gitignore": b"build/\n*.log\n", "README": b"readme", "src/main.c": b"main"}
    assert sorted(listing["excluded"]) == [".Claude/", "CLAUDE.md", "build/", "debug.log", "dirlink", "fifo", "link"]
    assert not list(ws(job).rglob(".git"))


def test_pack_inside_a_repository_and_nested_repositories(job: Any) -> None:
    base = ws(job)
    populate(base / "repo")
    git("init", "-q", cwd=base / "repo")
    (base / "repo" / "vendor").mkdir()
    (base / "repo" / "vendor" / "tracked.log").write_text("tracked")
    git("add", "-f", "vendor/tracked.log", cwd=base / "repo")
    nested = base / "repo" / "sub" / "nested"
    nested.mkdir(parents=True)
    git("init", "-q", cwd=nested)
    (nested / ".gitignore").write_text("dist/\n")
    (nested / "dist").mkdir()
    (nested / "dist" / "bundle.js").write_text("b")
    (nested / "lib.py").write_text("lib")
    listing, files = pack(job, "repo")
    assert sorted(files) == [
        ".gitignore",
        "README",
        "src/main.c",
        "sub/nested/.gitignore",
        "sub/nested/lib.py",
        "vendor/tracked.log",
    ]
    assert sorted(listing["excluded"]) == [
        ".Claude/",
        ".git/",
        "CLAUDE.md",
        "build/",
        "debug.log",
        "dirlink",
        "fifo",
        "link",
        "sub/nested/.git/",
        "sub/nested/dist/",
    ]
    _listing, below = pack(job, "repo/src")
    assert below == {"main.c": b"main"}


def test_pack_without_git_has_no_ignore_rules(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    populate(ws(job))
    monkeypatch.setattr(job, "GIT", "/nonexistent/git")
    listing, files = pack(job, ".")
    assert sorted(files) == [".gitignore", "README", "build/out.o", "debug.log", "src/main.c"]
    assert sorted(listing["excluded"]) == [".Claude/", "CLAUDE.md", "dirlink", "fifo", "link"]


def test_pack_when_git_init_fails(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    populate(ws(job))
    calls: list[list[str]] = []

    def failing(args: list[str]) -> bytes | None:
        calls.append(args)
        return None

    monkeypatch.setattr(job, "run_git", failing)
    _listing, files = pack(job, ".")
    assert "build/out.o" in files
    assert calls[-1][-2:] == ["init", "-q"]


def test_run_git_answers_none_on_failure_and_timeout(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert job.run_git(["-C", job.WORKSPACE, "rev-parse", "--is-inside-work-tree"]) is None

    def slow(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(job.subprocess, "run", slow)
    assert job.run_git(["status"]) is None


def test_pack_excludes_unreadable_and_too_deep_entries(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    base = ws(job)
    (base / "a" / "b").mkdir(parents=True)
    (base / "a" / "b" / "deep.txt").write_text("d")
    (base / "a" / "top.txt").write_text("t")
    (base / "secret").write_text("s")
    (base / "secret").chmod(0)
    (base / "closed").mkdir()
    (base / "closed").chmod(0)
    monkeypatch.setattr(job, "MAX_DEPTH", 1)
    try:
        listing, files = pack(job, ".")
    finally:
        (base / "secret").chmod(0o600)
        (base / "closed").chmod(0o700)
    if os.geteuid() == 0:  # root reads everything
        return
    assert files == {"a/top.txt": b"t"}
    assert sorted(listing["excluded"]) == ["a/b/", "closed/", "secret"]


def test_pack_add_excludes_what_is_not_a_regular_file(job: Any) -> None:
    os.mkfifo(ws(job) / "fifo")
    descriptor = os.open(ws(job), os.O_RDONLY)
    try:
        with tarfile.open(fileobj=io.BytesIO(), mode="w") as archive:
            packing = job.Packing(archive)
            packing.add(descriptor, "fifo", "fifo")
            packing.walk(descriptor, "", job.WORKSPACE, set(), 0)
    finally:
        os.close(descriptor)
    assert packing.excluded == ["fifo", "fifo"]


def test_pack_skips_entries_that_vanish(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (ws(job) / "present").write_text("p")
    real = job.os.listdir
    monkeypatch.setattr(job.os, "listdir", lambda descriptor: [*real(descriptor), "vanished"])
    _listing, files = pack(job, ".")
    assert files == {"present": b"p"}


def test_pack_is_bounded(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        (ws(job) / f"f{index}").write_text("12345")
    monkeypatch.setattr(job, "MAX_ENTRIES", 2)
    with pytest.raises(job.SyncError, match="too many entries"):
        job.pack(".", io.BytesIO())
    (ws(job) / "link").symlink_to("f0")
    monkeypatch.setattr(job, "MAX_ENTRIES", 0)
    with pytest.raises(job.SyncError, match="too many entries"):
        job.pack(".", io.BytesIO())
    monkeypatch.setattr(job, "MAX_ENTRIES", 10)
    monkeypatch.setattr(job, "MAX_BYTES", 12)
    with pytest.raises(job.SyncError, match="too large"):
        job.pack(".", io.BytesIO())


def test_pack_needs_a_real_directory_as_root(job: Any) -> None:
    (ws(job) / "file").write_text("f")
    (ws(job) / "link").symlink_to(ws(job))
    for root, message in (("missing", "not found"), ("file", "not a directory"), ("link", "not a directory")):
        with pytest.raises(job.SyncError, match=message):
            job.pack(root, io.BytesIO())


# -- command line ----------------------------------------------------------------------------------------------------


class Stdio:
    def __init__(self, data: bytes = b"") -> None:
        self.buffer = io.BytesIO(data)
        self.text = io.StringIO()

    def write(self, text: str) -> int:
        return self.text.write(text)


def test_main_runs_apply_and_pack(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = Stdio(push_tar((), {"a": b"1"}).getvalue())
    stdout = Stdio()
    monkeypatch.setattr(job.sys, "stdin", stdin)
    monkeypatch.setattr(job.sys, "stdout", stdout)
    job.main(["apply", "."])
    assert json.loads(stdout.text.getvalue()) == {"files": 1, "bytes": 1, "deleted": 0}
    job.main(["pack", "."])
    assert b".workvm-pack.json" in stdout.buffer.getvalue()[:2048]


def test_main_reports_refusals_with_exit_code_2(job: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job.sys, "stdin", Stdio(b"garbage" * 200))
    monkeypatch.setattr(job.sys, "stdout", Stdio())
    monkeypatch.setattr(job.sys, "stderr", io.StringIO())
    for argv in (["apply", "."], ["pack", "missing"], ["pack"], ["apply", "/abs"]):
        with pytest.raises(SystemExit) as caught:
            job.main(argv)
        assert caught.value.code == 2


# -- round trip ------------------------------------------------------------------------------------------------------


def write_export(path: Path, files: Mapping[str, bytes]) -> Path:
    manifest = {
        "files": [{"path": name, "size": len(data), "sha256": sha(data)} for name, data in sorted(files.items())],
        "skipped": [],
    }
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo(sync.EXPORT_MANIFEST)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def test_round_trip_to_vm_and_back(job: Any, tmp_path: Path) -> None:
    workspace = {"src/main.c": b"v1", "README": b"r", ".gitignore": b"*.o\n.env\n", ".env": b"KEY=1"}
    export = write_export(tmp_path / "e1.tar", workspace)
    plan = sync.plan_push(None, sync.read_export(export))
    sync.write_push_tar(export, plan, tmp_path / "push.tar", "")
    with (tmp_path / "push.tar").open("rb") as source:
        assert apply(job, "proj", io.BytesIO(source.read()))["files"] == 4
    vm = ws(job) / "proj"
    (vm / "src" / "main.c").write_text("v2")
    (vm / "src" / "main.o").write_text("obj")
    (vm / "README").unlink()
    (vm / "NEW.md").write_text("new")
    out = io.BytesIO()
    job.pack("proj", out)
    (tmp_path / "pack.tar").write_bytes(out.getvalue())
    result = sync.merge_from_vm(export, tmp_path / "pack.tar", plan.manifest, tmp_path / "merged.tar")
    assert result == sync.MergeResult(files=2, bytes=5, deleted=1, skipped=2)
    with tarfile.open(tmp_path / "merged.tar") as archive:
        merged = {
            member.name: types.SimpleNamespace(data=handle.read() if (handle := archive.extractfile(member)) else None)
            for member in archive
        }
    assert {name: value.data for name, value in merged.items()} == {
        ".env": b"KEY=1",
        ".gitignore": b"*.o\n.env\n",
        "NEW.md": b"new",
        "src": None,
        "src/main.c": b"v2",
    }
