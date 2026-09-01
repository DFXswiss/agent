from __future__ import annotations

import pytest

from agent_cli.workspace import (
    bare_path,
    clone_argv,
    fetch_argv,
    fetch_refspec_argv,
    has_session_argv,
    kill_session_argv,
    new_session_argv,
    repo_slug,
    worktree_add_argv,
    worktree_prune_argv,
    worktree_remove_argv,
)


# ---------------------------------------------------------------- repo_slug


def test_repo_slug_lowercases_and_replaces_slash() -> None:
    # The filesystem name must be stable across case variants of the same repo.
    assert repo_slug("Owner/Name") == "owner__name"


def test_repo_slug_rejects_missing_slash() -> None:
    # Same rejection shape and message as jobs.py::job_id for malformed input.
    with pytest.raises(ValueError, match="repo_slug requires owner/name"):
        repo_slug("ownername")


def test_repo_slug_rejects_empty_owner() -> None:
    with pytest.raises(ValueError, match="repo_slug requires owner/name"):
        repo_slug("/name")


def test_repo_slug_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="repo_slug requires owner/name"):
        repo_slug("owner/")


def test_repo_slug_rejects_more_than_one_slash() -> None:
    with pytest.raises(ValueError, match="repo_slug requires owner/name"):
        repo_slug("a/b/c")


# ---------------------------------------------------------------- bare_path


def test_bare_path_appends_slug_and_git_suffix() -> None:
    assert bare_path("/tmp/repos", "Owner/Name") == "/tmp/repos/owner__name.git"


# ---------------------------------------------------------------- clone_argv


def test_clone_argv_builds_bare_clone_command() -> None:
    assert clone_argv("owner/name", "/tmp/dest") == [
        "git",
        "clone",
        "--bare",
        "https://github.com/owner/name.git",
        "/tmp/dest",
    ]


# ---------------------------------------------------------------- fetch_refspec_argv


def test_fetch_refspec_argv_sets_origin_fetch_config() -> None:
    assert fetch_refspec_argv("/tmp/bare.git") == [
        "git",
        "--git-dir=/tmp/bare.git",
        "config",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
    ]


# ---------------------------------------------------------------- fetch_argv


def test_fetch_argv_fetches_with_prune() -> None:
    assert fetch_argv("/tmp/bare.git") == [
        "git",
        "--git-dir=/tmp/bare.git",
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
    ]


# ---------------------------------------------------------------- worktree_add_argv


def test_worktree_add_argv_creates_detached_worktree() -> None:
    assert worktree_add_argv("/tmp/bare.git", "/tmp/wt", "main") == [
        "git",
        "--git-dir=/tmp/bare.git",
        "worktree",
        "add",
        "--detach",
        "/tmp/wt",
        "refs/remotes/origin/main",
    ]


# ---------------------------------------------------------------- worktree_remove_argv


def test_worktree_remove_argv_removes_with_force() -> None:
    assert worktree_remove_argv("/tmp/bare.git", "/tmp/wt") == [
        "git",
        "--git-dir=/tmp/bare.git",
        "worktree",
        "remove",
        "--force",
        "/tmp/wt",
    ]


# ---------------------------------------------------------------- worktree_prune_argv


def test_worktree_prune_argv_prunes_stale_entries() -> None:
    assert worktree_prune_argv("/tmp/bare.git") == [
        "git",
        "--git-dir=/tmp/bare.git",
        "worktree",
        "prune",
    ]


# ---------------------------------------------------------------- new_session_argv


def test_new_session_argv_starts_detached_tmux_session() -> None:
    assert new_session_argv("/tmp/sock", "job-1", "/tmp/wt", "/bin/wrapper") == [
        "tmux",
        "-S",
        "/tmp/sock",
        "new-session",
        "-d",
        "-s",
        "job-1",
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        "/tmp/wt",
        "/bin/wrapper",
    ]


# ---------------------------------------------------------------- has_session_argv


def test_has_session_argv_uses_exact_match_prefix() -> None:
    # The `=` prefix is load-bearing: without it tmux matches a prefix, so one
    # session would be reported alive under another's name.
    assert has_session_argv("/tmp/sock", "job-1") == [
        "tmux",
        "-S",
        "/tmp/sock",
        "has-session",
        "-t",
        "=job-1",
    ]


# ---------------------------------------------------------------- kill_session_argv


def test_kill_session_argv_uses_exact_match_prefix() -> None:
    # Same `=` prefix rule as has_session_argv for consistency.
    assert kill_session_argv("/tmp/sock", "job-1") == [
        "tmux",
        "-S",
        "/tmp/sock",
        "kill-session",
        "-t",
        "=job-1",
    ]
