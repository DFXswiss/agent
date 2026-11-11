"""Git helper tests against real temporary repositories."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli.coordinator_common import CoordinatorError
from agent_cli.conflict_mandate_git import (
    base_is_ancestor,
    branch_ref_valid,
    changed_paths,
    conflict_markers,
    git_file_snapshot,
    require_ok,
    range_commits,
    rebase,
    rebase_abort,
    rebase_complete,
    rebase_continue,
    rev_parse,
    stage_paths,
    unmerged_paths,
    verify_git_file,
    verify_signed_range,
)
from agent_cli.runtime import Completed


pytestmark = pytest.mark.no_pg

if shutil.which("git") is None:
    pytest.skip("git is required", allow_module_level=True)


def _run(argv: list[str], *, cwd: Path | None = None) -> Completed:
    env = dict(os.environ)
    for key in ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "VISUAL", "EDITOR"):
        env.pop(key, None)
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return Completed(completed.returncode, completed.stdout or "", completed.stderr or "")


def _runner(argv: list[str]) -> Completed:
    return _run(argv)


def _git(cwd: Path, *parts: str) -> str:
    completed = _run(["git", "-C", str(cwd), *parts])
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _commit(cwd: Path, message: str, *, sign: bool = False) -> str:
    flags = ["-c", "commit.gpgsign=true"] if sign else ["-c", "commit.gpgsign=false"]
    completed = _run(
        [
            "git",
            "-C",
            str(cwd),
            *flags,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            message,
        ]
    )
    assert completed.returncode == 0, completed.stderr
    return _git(cwd, "rev-parse", "HEAD").lower()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    assert _run(["git", "init", "-b", "main"], cwd=path).returncode == 0
    _git(path, "config", "user.name", "Conflict Test")
    _git(path, "config", "user.email", "conflict@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.hooksPath", "/dev/null")


def _repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    worktree = tmp_path / "worktree"
    _init_repo(upstream)
    (upstream / "file.txt").write_text("base\n", encoding="utf-8")
    (upstream / "space ä.txt").write_text("base non-ascii\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base")
    assert _run(["git", "clone", str(upstream), str(worktree)]).returncode == 0
    _git(worktree, "config", "user.name", "Conflict Test")
    _git(worktree, "config", "user.email", "conflict@example.com")
    _git(worktree, "config", "commit.gpgsign", "false")
    _git(worktree, "config", "core.hooksPath", "/dev/null")
    return upstream, worktree


def _feature_with_two_commits(worktree: Path, *, path: str = "file.txt") -> None:
    _git(worktree, "checkout", "-b", "feature")
    (worktree / path).write_text("feature one\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _commit(worktree, "feature one")
    (worktree / "second.txt").write_text("feature two\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _commit(worktree, "feature two")


def _move_base(upstream: Path, worktree: Path, *, path: str = "file.txt") -> None:
    (upstream / path).write_text("base change\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base change")
    _git(worktree, "fetch", "origin")


def _start_conflicted_rebase(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream, worktree = _repo_pair(tmp_path)
    _feature_with_two_commits(worktree)
    original = _git(worktree, "rev-parse", "HEAD").lower()
    _move_base(upstream, worktree)
    completed = rebase(_runner, str(worktree), base="main")
    assert completed.returncode != 0
    return upstream, worktree, original


def test_rebase_conflict_reports_real_uu_and_incomplete(tmp_path: Path) -> None:
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    paths = unmerged_paths(_runner, str(worktree))
    assert [(item.path, item.state) for item in paths] == [("file.txt", "UU")]
    assert paths[0].supported is True
    assert rebase_complete(_runner, str(worktree), base="main") is False


def test_rebase_continue_without_editor_finishes_signed_range(tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required for signing assertions")
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    key = tmp_path / "signing-key"
    completed = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)])
    assert completed.returncode == 0, completed.stderr
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(
        f"conflict@example.com {key.with_suffix('.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    _git(worktree, "config", "gpg.format", "ssh")
    _git(worktree, "config", "user.signingkey", str(key))
    _git(worktree, "config", "commit.gpgsign", "true")
    _git(worktree, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    (worktree / "file.txt").write_text("resolved\n", encoding="utf-8")
    assert conflict_markers(str(worktree), ["file.txt"]) == []
    stage_paths(_runner, str(worktree), ["file.txt"])
    continued = rebase_continue(_runner, str(worktree))
    assert continued.returncode == 0, continued.stderr
    assert rebase_complete(_runner, str(worktree), base="main") is True
    assert base_is_ancestor(_runner, str(worktree), base="main") is True
    commits = range_commits(_runner, str(worktree), base="main")
    assert len(commits) == 2
    verify_signed_range(_runner, str(worktree), commits)


def test_conflict_markers_report_leftover_markers(tmp_path: Path) -> None:
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    assert conflict_markers(str(worktree), ["file.txt"])


def test_non_ascii_path_with_space_is_returned_exactly(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    path = "space ä.txt"
    _git(worktree, "checkout", "-b", "feature")
    (worktree / path).write_text("feature non-ascii\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _commit(worktree, "feature")
    (upstream / path).write_text("base non-ascii changed\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base")
    _git(worktree, "fetch", "origin")
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    paths = unmerged_paths(_runner, str(worktree))
    assert [(item.path, item.state) for item in paths] == [(path, "UU")]


def test_delete_modify_conflict_keeps_real_state(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    _git(worktree, "checkout", "-b", "feature")
    (worktree / "file.txt").unlink()
    _git(worktree, "add", "-A")
    _commit(worktree, "delete")
    (upstream / "file.txt").write_text("base change\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base")
    _git(worktree, "fetch", "origin")
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    paths = unmerged_paths(_runner, str(worktree))
    assert len(paths) == 1
    assert paths[0].path == "file.txt"
    assert paths[0].state in {"DU", "UD"}
    assert paths[0].state != "UU"


def test_both_added_conflict_is_aa(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    _git(worktree, "checkout", "-b", "feature")
    (worktree / "added.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _commit(worktree, "feature add")
    (upstream / "added.txt").write_text("base\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base add")
    _git(worktree, "fetch", "origin")
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    paths = unmerged_paths(_runner, str(worktree))
    assert [(item.path, item.state) for item in paths] == [("added.txt", "AA")]


def test_binary_conflict_without_nul_bytes_is_unsupported(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    (upstream / ".gitattributes").write_text("data.bin binary\n", encoding="utf-8")
    (upstream / "data.bin").write_bytes(b"base bytes\n")
    _git(upstream, "add", "-A")
    _commit(upstream, "binary base")
    _git(worktree, "fetch", "origin")
    _git(worktree, "reset", "--hard", "origin/main")
    _git(worktree, "checkout", "-b", "feature")
    (worktree / "data.bin").write_bytes(b"feature bytes\n")
    _git(worktree, "add", "-A")
    _commit(worktree, "binary feature")
    (upstream / "data.bin").write_bytes(b"base change bytes\n")
    _git(upstream, "add", "-A")
    _commit(upstream, "binary base change")
    _git(worktree, "fetch", "origin")
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    (worktree / "data.bin").write_bytes(b"resolution bytes without NUL\n")
    paths = unmerged_paths(_runner, str(worktree))
    assert [(item.path, item.state) for item in paths] == [("data.bin", "UU")]
    assert paths[0].supported is False


def test_rebase_abort_restores_head_and_clears_markers(tmp_path: Path) -> None:
    _upstream, worktree, original = _start_conflicted_rebase(tmp_path)
    rebase_abort(_runner, str(worktree))
    assert rev_parse(_runner, str(worktree), "HEAD") == original
    assert rebase_complete(_runner, str(worktree), base="main") is True


def test_changed_paths_reports_edit_outside_conflict_set(tmp_path: Path) -> None:
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    (worktree / "outside.txt").write_text("outside\n", encoding="utf-8")
    assert "outside.txt" in changed_paths(_runner, str(worktree))


def test_changed_paths_returns_special_characters_exactly(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    path = "space ä.txt"
    _feature_with_two_commits(worktree, path=path)
    _move_base(upstream, worktree, path=path)
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    (worktree / path).write_text("resolved\n", encoding="utf-8")
    (worktree / "other file.txt").write_text("outside\n", encoding="utf-8")
    assert changed_paths(_runner, str(worktree)) == {
        path,
        "other file.txt",
    }


def _separate_git_worktree(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    worktree = tmp_path / "worktree"
    git_dir = tmp_path / "metadata" / "git"
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    _init_repo(upstream)
    (upstream / "file.txt").write_text("base\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "base")
    assert _run(
        [
            "git",
            "clone",
            "--no-checkout",
            "--separate-git-dir",
            str(git_dir),
            str(upstream),
            str(worktree),
        ]
    ).returncode == 0
    return worktree, git_dir


def test_snapshot_detects_rewritten_git_file(tmp_path: Path) -> None:
    worktree, _git_dir = _separate_git_worktree(tmp_path)
    snapshot = git_file_snapshot(worktree)
    verify_git_file(worktree, snapshot)
    (worktree / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    with pytest.raises(CoordinatorError, match="repository metadata"):
        verify_git_file(worktree, snapshot)


def test_snapshot_rejects_git_directory_as_metadata(tmp_path: Path) -> None:
    worktree, _git_dir = _separate_git_worktree(tmp_path)
    (worktree / ".git").unlink()
    (worktree / ".git").mkdir()
    with pytest.raises(CoordinatorError, match="repository metadata"):
        git_file_snapshot(worktree)


def test_separate_git_dir_accepts_local_hook_configuration(tmp_path: Path) -> None:
    worktree, git_dir = _separate_git_worktree(tmp_path)
    require_ok(
        _runner,
        str(worktree),
        "disable hooks",
        "config",
        "core.hooksPath",
        "/dev/null",
    )
    assert _git(worktree, "config", "--local", "core.hooksPath") == "/dev/null"
    config_path = git_dir / "config"
    assert config_path.is_file()
    assert "hooksPath = /dev/null" in config_path.read_text(encoding="utf-8")


def test_changed_paths_ignores_cleanly_staged_replay_files(tmp_path: Path) -> None:
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    (worktree / "src").mkdir(exist_ok=True)
    (worktree / "src" / "clean.py").write_text("staged cleanly\n", encoding="utf-8")
    _git(worktree, "add", "src/clean.py")
    assert changed_paths(_runner, str(worktree)) == {"file.txt"}
    (worktree / "file.txt").write_text("resolved\n", encoding="utf-8")
    touched = changed_paths(_runner, str(worktree))
    assert touched == {"file.txt"}


def test_changed_paths_reports_edit_to_cleanly_staged_file(tmp_path: Path) -> None:
    _upstream, worktree, _original = _start_conflicted_rebase(tmp_path)
    clean = worktree / "src" / "clean.py"
    clean.parent.mkdir(exist_ok=True)
    clean.write_text("staged cleanly\n", encoding="utf-8")
    _git(worktree, "add", "src/clean.py")
    clean.write_text("lane edit\n", encoding="utf-8")
    assert "src/clean.py" in changed_paths(_runner, str(worktree))


def test_changed_paths_reports_new_and_ignored_files(tmp_path: Path) -> None:
    upstream, worktree = _repo_pair(tmp_path)
    (upstream / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _commit(upstream, "ignore generated file")
    _git(worktree, "fetch", "origin")
    _git(worktree, "reset", "--hard", "origin/main")
    _feature_with_two_commits(worktree)
    _move_base(upstream, worktree)
    assert rebase(_runner, str(worktree), base="main").returncode != 0
    (worktree / "new file.txt").write_text("new\n", encoding="utf-8")
    assert "new file.txt" in changed_paths(_runner, str(worktree))
    (worktree / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    assert "ignored.txt" in changed_paths(_runner, str(worktree))


@pytest.mark.parametrize(
    "name",
    ["feature@2", "späti", "_topic", "release/2026.11", "a&b"],
)
def test_git_valid_branch_names_are_accepted(name: str) -> None:
    assert branch_ref_valid(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "@",
        ".hidden",
        "-topic",
        "/topic",
        "topic.",
        "topic/",
        "a..b",
        "a@{1}",
        "a//b",
        "a b",
        "space ä",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "topic.lock",
        "topic/.hidden",
        "topic/component.lock",
        "topic\nnext",
    ],
)
def test_git_invalid_branch_names_are_rejected(name: str) -> None:
    assert branch_ref_valid(name) is False
