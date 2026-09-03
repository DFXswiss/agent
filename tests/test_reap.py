"""Tests for reap.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

import pytest

from agent_cli.reap import (
    list_sessions_argv,
    orphan_sessions,
    orphan_worktrees,
    preparing_active,
    within_root,
)


# ---------------------------------------------------------------- argv builders


def test_list_sessions_argv_returns_exact_6_element_list() -> None:
    # The argv is fixed; the caller only supplies the socket path.
    assert list_sessions_argv("/tmp/sock") == [
        "tmux",
        "-S",
        "/tmp/sock",
        "list-sessions",
        "-F",
        "#{session_name}",
    ]


# ---------------------------------------------------------------- preparing_active


def test_preparing_active_returns_false_when_marker_absent() -> None:
    # Absent marker must not block reaping.
    assert preparing_active(False, 1000, 2000) is False


def test_preparing_active_returns_true_when_mtime_unreadable() -> None:
    # Marker present but mtime None (unreadable) must protect the worktree.
    assert preparing_active(True, None, 2000) is True


def test_preparing_active_returns_true_for_fresh_marker() -> None:
    assert preparing_active(True, 1900, 2000) is True


def test_preparing_active_returns_false_exactly_at_max_age() -> None:
    # Boundary is >= max_age.
    assert preparing_active(True, 1700, 2000, max_age=300) is False


def test_preparing_active_returns_true_one_second_younger_than_max_age() -> None:
    assert preparing_active(True, 1701, 2000, max_age=300) is True


def test_preparing_active_returns_true_for_future_mtime() -> None:
    # Clock skew must not turn the marker negative-age and drop the guard.
    assert preparing_active(True, 3000, 2000) is True


# ---------------------------------------------------------------- within_root


def test_within_root_returns_true_for_direct_child() -> None:
    assert within_root("/work", "/work/job-1") is True


def test_within_root_returns_true_for_nested_path() -> None:
    assert within_root("/work", "/work/a/b/c") is True


def test_within_root_returns_false_for_root_itself() -> None:
    # The root must never be deletable.
    assert within_root("/work", "/work") is False


def test_within_root_returns_false_for_sibling_with_shared_prefix() -> None:
    # "/work" vs "/workshop" must not be confused.
    assert within_root("/work", "/workshop") is False


def test_within_root_returns_false_for_path_outside_root() -> None:
    assert within_root("/work", "/other") is False


def test_within_root_returns_false_for_traversal_escape() -> None:
    # Normalisation must reject "/work/../etc".
    assert within_root("/work", "/work/../etc") is False


def test_within_root_returns_false_for_relative_root() -> None:
    assert within_root("work", "/work/job-1") is False


def test_within_root_returns_false_for_relative_candidate() -> None:
    assert within_root("/work", "job-1") is False


def test_within_root_ignores_trailing_slash() -> None:
    assert within_root("/work/", "/work/job-1/") is True


# ---------------------------------------------------------------- orphan_worktrees


def test_orphan_worktrees_keeps_running_id() -> None:
    assert orphan_worktrees(["job-1"], {"job-1"}, set()) == []


def test_orphan_worktrees_keeps_preparing_id() -> None:
    assert orphan_worktrees(["job-1"], set(), {"job-1"}) == []


def test_orphan_worktrees_flags_neither() -> None:
    assert orphan_worktrees(["job-1"], set(), set()) == ["job-1"]


def test_orphan_worktrees_preserves_input_order() -> None:
    assert orphan_worktrees(["b", "a"], set(), set()) == ["b", "a"]


# ---------------------------------------------------------------- orphan_sessions


def test_orphan_sessions_flags_session_with_no_match() -> None:
    assert orphan_sessions(["job-1"], set(), set(), prefix="job-") == ["job-1"]


def test_orphan_sessions_keeps_running_session() -> None:
    assert orphan_sessions(["job-1"], {"job-1"}, set(), prefix="job-") == []


def test_orphan_sessions_keeps_preparing_session() -> None:
    assert orphan_sessions(["job-1"], set(), {"1"}, prefix="job-") == []


def test_orphan_sessions_ignores_name_without_prefix() -> None:
    # A session belonging to something else must never be killed, even though it
    # matches no row and no marker. Only names carrying the prefix are ours.
    assert orphan_sessions(["editor"], set(), set(), prefix="job-") == []
    # And it is skipped even alongside a genuine orphan.
    assert orphan_sessions(["editor", "job-1"], set(), set(), prefix="job-") == ["job-1"]


def test_orphan_sessions_preserves_input_order() -> None:
    assert orphan_sessions(["job-2", "job-1"], set(), set(), prefix="job-") == [
        "job-2",
        "job-1",
    ]
