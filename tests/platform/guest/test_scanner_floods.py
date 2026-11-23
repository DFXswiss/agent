"""U-GST-scanner (HOT-06): platform imports are the scanner's baseline, excluded directories, bulk summaries and the
per-session window of ``session.file`` records (``scanner.py``; ADR-057).

Same set-up as ``test_scanner.py``: a temporary session base owned by the test user and the journal of the guest
fixtures. The staged tree of an import lives next to ``work`` like the one of ``files.import_tar``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_guest import scanner
from agent_guest.control import ControlContext
from agent_guest.scanner import Limits, WorkspaceScanner

SID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
REPO = "repos/o/r"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / SID
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def work(base: Path) -> Path:
    return base / "work"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make(
    context: ControlContext, base: Path, limits: Limits = scanner.DEFAULT_LIMITS, clock: Clock | None = None
) -> WorkspaceScanner:
    return WorkspaceScanner(context.journal, SID, str(base), os.getuid(), limits=limits, monotonic=clock or Clock())


def files(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 100_000)
    bodies = [record["body"] for record in records if record["kind"] == "session.file"]
    for body in bodies:
        assert context.schemas.errors("guest/journal/session.file.json", body) == []
    return bodies


def stage(base: Path, tree: dict[str, str]) -> Path:
    """A staged tree as ``files.import_tar`` leaves it before the move."""
    root = base / ".files-test" / "tree"
    root.mkdir(parents=True)
    for rel, text in tree.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


@pytest.fixture
def opened() -> Iterator[list[int]]:
    fds: list[int] = []
    yield fds
    for fd in fds:
        os.close(fd)


def survey(found: WorkspaceScanner, root: Path, opened: list[int], directory: str = REPO) -> scanner.Survey:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    opened.append(fd)
    return found.survey(fd, directory)


def install(root: Path, work: Path, directory: str = REPO) -> None:
    target = work / directory
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.rename(target, root.parent / "old")
    os.rename(root, target)


# -- imports ---------------------------------------------------------------------------------------------------------


def test_an_import_becomes_the_baseline_and_pending_changes_are_reported_first(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    (work / "notes.md").write_text("one\n")
    found = make(context, base)
    found.start()
    (work / "notes.md").write_text("one\ntwo\n")  # a session change not scanned yet
    tree = {f"src/f{index:04}.py": f"value = {index}\n" for index in range(3000)} | {"README.md": "hello\n"}
    root = stage(base, tree)
    staged = survey(found, root, opened)
    with found.materializing(REPO, staged):
        assert [record["path"] for record in files(context)] == ["notes.md"]
        install(root, work)
    assert [record["path"] for record in files(context)] == ["notes.md"]
    assert found.scan() == 0
    index = json.loads((base / scanner.INDEX_FILE).read_text())
    assert len([rel for rel in index if rel.startswith(f"{REPO}/")]) == 3001
    # A restart of the control process does not report the import either.
    again = make(context, base)
    again.start()
    assert again.scan() == 0
    # The agent's next edit of an imported file comes with its patch.
    (work / REPO / "README.md").write_text("hello\nworld\n")
    assert found.check(f"{REPO}/README.md", "edit") is True
    edit = files(context)[-1]
    assert edit["op"] == "edit"
    assert edit["patch"] == f"--- a/{REPO}/README.md\n+++ b/{REPO}/README.md\n@@ -1 +1,2 @@\n hello\n+world\n"


def test_a_new_import_replaces_the_known_tree_without_deletions(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    (work / "repos" / "o").mkdir(parents=True)
    (work / REPO).write_text("a file where the directory goes\n")
    found = make(context, base)
    found.start()
    first = stage(base, {"old.txt": "old\n", "keep.txt": "keep\n"})
    with found.materializing(REPO, survey(found, first, opened)):
        os.unlink(work / REPO)
        install(first, work)
    (work / REPO / "session.txt").write_text("written by the session\n")
    assert found.scan() == 1
    os.rename(base / ".files-test", base / ".files-first")
    second = stage(base, {"keep.txt": "keep\n", "new.txt": "new\n"})
    with found.materializing(REPO, survey(found, second, opened)):
        install(second, work)
    assert sorted(found.known) == [f"{REPO}/keep.txt", f"{REPO}/new.txt"]
    assert found.scan() == 0
    assert [record["op"] for record in files(context)] == ["write"]


def test_a_change_after_the_move_is_still_reported(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    found = make(context, base)
    found.start()
    root = stage(base, {"a.txt": "imported\n", "b.txt": "imported\n"})
    with found.materializing(REPO, survey(found, root, opened)):
        install(root, work)
        # The session writes between the move and the adoption: not lost.
        (work / REPO / "a.txt").write_text("changed by the session\n")
        os.utime(work / REPO / "a.txt", ns=(7, 7))
        (work / REPO / "c.txt").write_text("new\n")
    assert found.scan() == 2
    edit, new = files(context)
    assert (edit["path"], edit["op"]) == (f"{REPO}/a.txt", "edit")
    assert "+changed by the session\n" in edit["patch"]
    assert (new["path"], new["op"]) == (f"{REPO}/c.txt", "write")


def test_a_failed_move_keeps_the_known_state(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    (work / "x.txt").write_text("x\n")
    found = make(context, base)
    found.start()
    before = dict(found.known)
    root = stage(base, {"a.txt": "a\n"})
    with pytest.raises(OSError, match="no move"), found.materializing(REPO, survey(found, root, opened)):
        raise OSError("no move")
    assert found.known == before
    assert found.scan() == 0


def test_the_survey_keeps_texts_only_up_to_the_cache(
    context: ControlContext, base: Path, work: Path, opened: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner, "TEXT_CACHE_TOTAL", 6)
    monkeypatch.setattr(scanner, "HASH_LIMIT", 10)
    found = make(context, base)
    tree = {"a.txt": "1234\n", "b.txt": "5678\n", "big.txt": "x" * 11, "node_modules/x.js": "skip\n"}
    root = stage(base, tree)
    staged = survey(found, root, opened)
    assert sorted(staged.files) == ["a.txt", "b.txt"]
    assert staged.files["a.txt"][1] == b"1234\n"
    assert staged.files["b.txt"][1] is None
    digest = hashlib.sha256(b"5678\n").hexdigest()
    assert staged.files["b.txt"][0].sha256 == digest


# -- excluded directories --------------------------------------------------------------------------------------------


def test_dependency_and_build_directories_are_not_reported(context: ControlContext, base: Path, work: Path) -> None:
    found = make(context, base)
    found.start()
    for rel in ("node_modules/pkg/index.js", "app/.venv/lib/site.py", "target/debug/out", "src/__pycache__/m.pyc"):
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text("generated\n")
    (work / "dist").write_text("a file named like an excluded directory\n")
    assert found.scan() == 1
    assert [record["path"] for record in files(context)] == ["dist"]
    assert found.check("node_modules/pkg/index.js", "write") is False
    assert scanner.excluded("a/node_modules/b") is True
    assert scanner.excluded("node_modules") is False


def test_an_index_of_an_earlier_version_drops_excluded_entries(context: ControlContext, base: Path, work: Path) -> None:
    (work / "kept.txt").write_text("k")
    found = make(context, base)
    found.start()
    index = json.loads((base / scanner.INDEX_FILE).read_text())
    index["node_modules/old.js"] = [1, 1, "0" * 64]
    (base / scanner.INDEX_FILE).write_text(json.dumps(index))
    again = make(context, base)
    again.start()
    assert sorted(again.known) == ["kept.txt"]
    assert again.scan() == 0
    assert files(context) == []


# -- bulk and window -------------------------------------------------------------------------------------------------


def test_a_bulk_pass_becomes_one_summary(context: ControlContext, base: Path, work: Path) -> None:
    (work / "gen").mkdir()
    (work / "gone.txt").write_text("bye")
    found = make(context, base, Limits(bulk_files=100, summary_paths=20))
    found.start()
    (work / "gone.txt").unlink()
    for index in range(2000):
        (work / "gen" / f"f{index:04}.ts").write_text("x" * 10)
    assert found.scan() == 1
    (summary,) = files(context)
    assert summary == {
        "path": "gen/f0000.ts",
        "op": "summary",
        "files": 2001,
        "deleted": 1,
        "bytes": 20_000,
        "paths": [f"gen/f{index:04}.ts" for index in range(20)],
        "reason": "bulk",
    }
    # Known afterwards: the next pass reports nothing, a later edit diffs against the bulk content.
    assert found.scan() == 0
    (work / "gen" / "f0001.ts").write_text("y" * 10)
    assert found.check("gen/f0001.ts", "edit") is True
    assert "+yyyyyyyyyy" in files(context)[-1]["patch"]


def test_the_summary_path_is_the_common_directory(context: ControlContext, base: Path, work: Path) -> None:
    limits = Limits(summary_paths=2)

    def record(path: str, op: str = "write", size: int | None = 1) -> dict[str, Any]:
        return {"path": path, "op": op, "sha256": None, "size": size}

    assert scanner.summary([record("a/b/x"), record("a/b/y"), record("a/c")], "bulk", limits) == {
        "path": "a",
        "op": "summary",
        "files": 3,
        "deleted": 0,
        "bytes": 3,
        "paths": ["a/b/x", "a/b/y"],
        "reason": "bulk",
    }
    assert scanner.summary([record("a/b/x")], "limit", limits)["path"] == "a/b"
    assert scanner.summary([record("x", "delete", None), record("y")], "bulk", limits)["path"] == "x"
    assert scanner.summary([record("x", "delete", None)], "bulk", limits)["bytes"] == 0


def test_the_window_bounds_single_records_per_session(context: ControlContext, base: Path, work: Path) -> None:
    clock = Clock()
    found = make(context, base, Limits(window_seconds=60, window_files=3, window_bytes=10_000), clock)
    found.start()
    for index in range(5):
        (work / f"f{index}.txt").write_text(f"{index}\n")
    assert found.scan() == 4
    bodies = files(context)
    assert [body["op"] for body in bodies] == ["write", "write", "write", "summary"]
    assert (bodies[3]["files"], bodies[3]["paths"], bodies[3]["reason"]) == (2, ["f3.txt", "f4.txt"], "limit")
    # The agent's own edit is reported singly at once even beyond the window, without its patch.
    (work / "f0.txt").write_text("edited\n")
    assert found.check("f0.txt", "edit") is True
    beyond = files(context)[-1]
    assert (beyond["path"], beyond["op"], beyond["size"]) == ("f0.txt", "edit", 7)
    assert "patch" not in beyond
    (work / "f1.txt").write_text("again\n")
    os.utime(work / "f1.txt", ns=(9, 9))
    assert found.scan() == 1
    assert files(context)[-1]["op"] == "summary"
    clock.now += 60
    (work / "f2.txt").write_text("next window\n")
    os.utime(work / "f2.txt", ns=(11, 11))
    assert found.scan() == 1
    assert files(context)[-1]["op"] == "edit"


def test_the_window_bounds_patch_bytes(context: ControlContext, base: Path, work: Path) -> None:
    found = make(context, base, Limits(window_files=100, window_bytes=120))
    found.start()
    (work / "a.txt").write_text("a" * 40 + "\n")
    (work / "b.txt").write_text("b" * 40 + "\n")
    assert found.scan() == 2
    single, rest = files(context)
    assert single["path"] == "a.txt"
    assert "patch" in single
    assert (rest["op"], rest["paths"]) == ("summary", ["b.txt"])


def test_a_failed_journal_write_restores_the_window(
    context: ControlContext, base: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = make(context, base, Limits(window_files=1))
    found.start()
    (work / "a.txt").write_text("a\n")
    real_insert = context.journal.insert

    def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("journal down")

    monkeypatch.setattr(context.journal, "insert", failing)
    with pytest.raises(RuntimeError, match="journal down"):
        found.scan()
    monkeypatch.setattr(context.journal, "insert", real_insert)
    assert found.scan() == 1
    assert files(context)[0]["op"] == "write"


def test_a_check_within_the_window_keeps_its_patch(context: ControlContext, base: Path, work: Path) -> None:
    found = make(context, base, Limits(window_files=1))
    found.start()
    (work / "a.txt").write_text("a\n")
    assert found.check("a.txt", "write") is True
    assert files(context)[-1]["patch"].endswith("+a\n")


def test_a_repository_named_like_an_excluded_directory_is_scanned(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    found = make(context, base)
    found.start()
    root = stage(base, {"index.js": "one\n", "dist/bundle.js": "built\n"})
    with found.materializing("repos/dist/target", survey(found, root, opened, "repos/dist/target")):
        install(root, work, "repos/dist/target")
    assert sorted(found.known) == ["repos/dist/target/index.js"]
    assert found.scan() == 0
    (work / "repos/dist/target/index.js").write_text("two\n")
    assert found.check("repos/dist/target/index.js", "edit") is True
    assert files(context)[-1]["op"] == "edit"
    assert scanner.excluded("repos/dist/target/dist/bundle.js") is True
    assert scanner.excluded("repos/dist/x") is False


def test_an_import_into_an_excluded_directory_surveys_nothing(
    context: ControlContext, base: Path, work: Path, opened: list[int]
) -> None:
    found = make(context, base)
    found.start()
    root = stage(base, {"a.js": "a\n"})
    with found.materializing("node_modules/pkg", survey(found, root, opened, "node_modules/pkg")):
        install(root, work, "node_modules/pkg")
    assert found.known == {}
    assert found.scan() == 0
    assert files(context) == []


def test_an_index_write_failure_after_the_move_keeps_the_baseline(
    context: ControlContext, base: Path, opened: list[int], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    work = base / "work"
    found = make(context, base)
    found.start()
    root = stage(base, {"a.txt": "a\n"})
    staged = survey(found, root, opened)

    def failing() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(found, "_save_index", failing)
    with found.materializing(REPO, staged):
        install(root, work)
    assert sorted(found.known) == [f"{REPO}/a.txt"]
    assert "capture index not written" in capsys.readouterr().err
    monkeypatch.undo()
    assert found.scan() == 0
