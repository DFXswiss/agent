"""U-GST-scanner: workspace scanner (05-runner.md sections 5.5 and 6.4, 03-api.md section 7.2 ``session.file``; GST-05).

The scanner runs against a temporary session base (``<tmp>/srv/<id>/work``) owned by the test user and the journal of
the guest fixtures (fake clock); the Linux suite repeats the essentials as root with a real session user.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_guest import scanner
from agent_guest.control import ControlContext
from agent_guest.scanner import WorkspaceScanner

SID = "01a1ee56-5e00-7003-8f1b-340691d99f03"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def work(base: Path) -> Path:
    return base / "work"


def make(context: ControlContext, base: Path, uid: int | None = None) -> WorkspaceScanner:
    return WorkspaceScanner(context.journal, SID, str(base), os.getuid() if uid is None else uid)


def files(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 1000)
    bodies = [record["body"] for record in records if record["kind"] == "session.file"]
    for body in bodies:
        assert context.schemas.errors("guest/journal/session.file.json", body) == []
    return bodies


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_baseline_is_not_reported_and_later_changes_are(context: ControlContext, base: Path, work: Path) -> None:
    (work / "imported.txt").write_text("from the runner\n")
    found = make(context, base)
    found.start()
    assert found.scan() == 0
    assert files(context) == []
    (work / "docs").mkdir()
    (work / "docs" / "new.md").write_text("alpha\nbeta\n")
    assert found.scan() == 1
    (new,) = files(context)
    assert new == {
        "path": "docs/new.md",
        "op": "write",
        "sha256": sha(b"alpha\nbeta\n"),
        "size": 11,
        "patch": "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1,2 @@\n+alpha\n+beta\n",
    }
    (work / "docs" / "new.md").write_text("alpha\ngamma\n")
    os.utime(work / "docs" / "new.md", ns=(1, 1))
    (work / "imported.txt").unlink()
    assert found.scan() == 2
    edit, delete = files(context)[1:]
    assert edit["op"] == "edit"
    assert edit["patch"] == ("--- a/docs/new.md\n+++ b/docs/new.md\n@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n")
    assert delete == {"path": "imported.txt", "op": "delete", "sha256": None, "size": None}
    assert found.scan() == 0


def test_a_touched_file_with_the_same_content_is_not_reported(context: ControlContext, base: Path, work: Path) -> None:
    (work / "same.txt").write_text("x")
    (work / "unreadable.txt").write_text("x")
    os.chmod(work / "unreadable.txt", 0)
    found = make(context, base)
    found.start()
    assert set(found.known) == {"same.txt"}
    os.chmod(work / "unreadable.txt", 0o600)
    assert found.scan() == 1  # readable now: a file the baseline did not hold
    os.utime(work / "same.txt", ns=(5, 5))
    assert found.scan() == 0
    assert found.known["same.txt"].mtime_ns == 5


def test_patches_only_for_known_small_text(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner, "TEXT_CACHE_FILE", 8)
    (work / "big.txt").write_text("0123456789\n")
    found = make(context, base)
    found.start()
    (work / "binary.bin").write_bytes(b"\xff\xfe\x00")
    (work / "big.txt").write_text("0123456789abc\n")
    found.scan()
    binary, big = sorted(files(context), key=lambda body: body["path"])[::-1]
    assert "patch" not in binary
    assert "patch" not in big
    monkeypatch.setattr(scanner, "PATCH_LIMIT", 10)
    (work / "small.txt").write_text("a\n")
    found.scan()
    assert "patch" not in files(context)[-1]


def test_the_text_cache_is_bounded(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner, "TEXT_CACHE_TOTAL", 4)
    (work / "a.txt").write_text("aaa")
    (work / "b.txt").write_text("bbb")
    found = make(context, base)
    found.start()
    assert set(found._texts) == {"a.txt"}
    (work / "a.txt").write_text("aaaa")
    (work / "b.txt").write_text("bbbb")
    found.scan()
    a, b = files(context)
    assert a["patch"].endswith("-aaa\n\\ No newline at end of file\n+aaaa\n\\ No newline at end of file\n")
    assert "patch" not in b
    assert found._text_bytes == 4


def test_files_above_10_mib_are_not_hashed_or_reported(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner, "HASH_LIMIT", 4)
    found = make(context, base)
    found.start()
    (work / "big.log").write_bytes(b"12345")
    assert found.scan() == 0
    assert found.check("big.log", "write") is False
    assert "big.log" not in found.known


def test_a_failed_journal_write_reports_the_changes_on_the_next_pass(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (work / "old.txt").write_text("o")
    found = make(context, base)
    found.start()
    (work / "new.txt").write_text("n")
    (work / "old.txt").unlink()
    real_insert = context.journal.insert

    def failing(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("journal down")

    monkeypatch.setattr(context.journal, "insert", failing)
    with pytest.raises(RuntimeError, match="journal down"):
        found.scan()
    with pytest.raises(RuntimeError, match="journal down"):
        found.check("new.txt", "write")
    assert set(found.known) == {"old.txt"}
    monkeypatch.setattr(context.journal, "insert", real_insert)
    assert found.scan() == 2
    assert [(body["path"], body["op"]) for body in files(context)] == [("new.txt", "write"), ("old.txt", "delete")]


def test_git_links_foreign_and_special_files_are_skipped(
    context: ControlContext, base: Path, work: Path, tmp_path: Path
) -> None:
    found = make(context, base)
    found.start()
    (work / ".git").mkdir()
    (work / ".git" / "config").write_text("x")
    (work / "sub" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (work / "link.txt").symlink_to(outside)
    (work / "linkdir").symlink_to(tmp_path)
    os.mkfifo(work / "pipe")
    (work / "bad\nname").write_text("x")
    assert found.scan() == 0
    foreign = make(context, base, uid=os.getuid() + 1)
    foreign.start()
    (work / "plain.txt").write_text("x")
    assert foreign.scan() == 0


def test_a_symlinked_workspace_is_not_walked_and_reports_no_deletions(
    context: ControlContext, base: Path, work: Path, tmp_path: Path
) -> None:
    (work / "kept.txt").write_text("x")
    found = make(context, base)
    found.start()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    work.rename(tmp_path / "moved")
    work.symlink_to(elsewhere)
    assert found.scan() == 0
    assert "kept.txt" in found.known


def test_an_incomplete_walk_reports_no_deletions(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        (work / name).write_text(name)
    found = make(context, base)
    found.start()
    (work / "c").unlink()
    monkeypatch.setattr(scanner, "MAX_ENTRIES", 1)
    assert found.scan() == 0
    monkeypatch.setattr(scanner, "MAX_ENTRIES", 100)
    (work / "deep").mkdir()
    (work / "deep" / "x").mkdir()
    monkeypatch.setattr(scanner, "MAX_DEPTH", 2)
    assert found.scan() == 0
    monkeypatch.setattr(scanner, "MAX_DEPTH", 64)
    assert found.scan() == 1
    assert files(context)[0]["op"] == "delete"


def test_the_entry_limit_also_stops_inside_a_directory(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (work / "a").mkdir()
    for name in ("x", "y", "z"):
        (work / "a" / name).write_text(name)
    (work / "b").write_text("b")
    found = make(context, base)
    monkeypatch.setattr(scanner, "MAX_ENTRIES", 3)
    found.start()
    assert set(found.known) == {"a/x", "a/y"}


def test_unreadable_entries_make_the_walk_incomplete(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (work / "gone.txt").write_text("x")
    (work / "closed").mkdir()
    (work / "locked.txt").write_text("x")
    found = make(context, base)
    found.start()
    (work / "gone.txt").unlink()
    os.chmod(work / "closed", 0)
    os.chmod(work / "locked.txt", 0)
    os.utime(work / "locked.txt", ns=(9, 9))
    real_stat = os.stat

    def stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if name == "vanishing":
            raise FileNotFoundError(name)
        return real_stat(name, *args, **kwargs)

    real_listdir = os.listdir
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "listdir", lambda fd: [*real_listdir(fd), "vanishing"])
    try:
        assert found.scan() == 0
    finally:
        os.chmod(work / "closed", 0o700)
        os.chmod(work / "locked.txt", 0o600)


def test_the_index_survives_a_restart_and_reports_changes_since(
    context: ControlContext, base: Path, work: Path
) -> None:
    (work / "a.txt").write_text("a")
    make(context, base).start()
    index = json.loads((base / scanner.INDEX_FILE).read_text())
    assert list(index) == ["a.txt"]
    assert oct((base / scanner.INDEX_FILE).stat().st_mode & 0o777) == "0o600"
    (work / "a.txt").write_text("changed")
    again = make(context, base)
    again.start()
    assert again.scan() == 1
    assert files(context)[0] | {"sha256": ""} == {"path": "a.txt", "op": "edit", "sha256": "", "size": 7}


def test_an_unreadable_index_starts_a_new_baseline(context: ControlContext, base: Path, work: Path) -> None:
    (base / scanner.INDEX_FILE).write_text('{"a.txt": [1]}')
    (work / "a.txt").write_text("a")
    found = make(context, base)
    found.start()
    assert found.scan() == 0
    assert set(found.known) == {"a.txt"}


def test_a_failing_index_write_leaves_no_temporary_file(
    context: ControlContext, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = make(context, base)
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError, match="full"):
        found.start()
    assert sorted(path.name for path in base.iterdir()) == ["work"]


def test_check_reports_one_path_with_the_hook_op(context: ControlContext, base: Path, work: Path) -> None:
    found = make(context, base)
    found.start()
    (work / "dir").mkdir()
    (work / "dir" / "f.txt").write_text("v1")
    assert found.check("dir/f.txt", "edit") is True
    assert files(context)[0]["op"] == "edit"
    assert found.check("dir/f.txt", "edit") is False  # nothing new
    assert found.scan() == 0  # the scan does not report it again
    (work / "top.txt").write_text("t")
    assert found.check("top.txt") is True
    assert files(context)[1]["op"] == "write"
    (work / "dir" / "f.txt").unlink()
    assert found.check("dir/f.txt") is True
    assert files(context)[2]["op"] == "delete"
    assert found.check("dir/f.txt") is False
    assert found.check("missing/f.txt") is False
    assert found.check("../escape", "write") is False
    assert found.check("top.txt", "delete") is False
    os.mkfifo(work / "pipe")
    assert found.check("pipe") is False
    (work / "gone").mkdir()
    (work / "gone" / "f.txt").write_text("g")
    assert found.check("gone/f.txt") is True
    (work / "gone" / "f.txt").unlink()
    (work / "gone").rmdir()
    assert found.check("gone/f.txt") is True
    assert files(context)[-1] == {"path": "gone/f.txt", "op": "delete", "sha256": None, "size": None}


def test_check_finds_a_change_that_kept_mtime_and_size(context: ControlContext, base: Path, work: Path) -> None:
    (work / "f.txt").write_text("aa")
    found = make(context, base)
    found.start()
    before = (work / "f.txt").stat()
    (work / "f.txt").write_text("bb")
    os.utime(work / "f.txt", ns=(before.st_atime_ns, before.st_mtime_ns))
    assert found.scan() == 0
    assert found.check("f.txt") is True


def test_helpers() -> None:
    assert scanner.valid_rel("a/b.txt")
    for rel in ("", "a/../b", "./a", "a//b", "a/.git/x", "a\x7fb", "x" * 4097):
        assert not scanner.valid_rel(rel)
    assert scanner.make_patch("f", None, b"x") is None
    assert scanner.make_patch("f", b"x", None) is None
    assert scanner.make_patch("f", b"\xff", b"x") is None
    assert (scanner.make_patch("f", b"", b"x\n") or "").startswith("--- a/f\n")
    assert not scanner.valid_rel("bad\udcffname")
    with pytest.raises(OSError, match="invalid path segment"):
        scanner.open_dir("/", [".."])
