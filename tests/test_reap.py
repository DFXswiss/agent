"""Tests for reap.py decision logic. Pure module: no Store, no runner, no filesystem."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli.ingest import job_row
from agent_cli.reap import (
    list_sessions_argv,
    orphan_sessions,
    orphan_worktrees,
    preparing_active,
    reap_orphans,
    within_root,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store


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


# ---------------------------------------------------------------- reap_orphans


def _runner(
    *,
    calls: list[list[str]],
    list_sessions_rc: int = 0,
    list_sessions_stdout: str = "",
):
    """Fake runner recording every argv, answering the commands reap_orphans issues."""

    def run(argv: list[str]) -> Completed:
        calls.append(argv)
        joined = " ".join(argv)
        if "list-sessions" in joined:
            return Completed(list_sessions_rc, list_sessions_stdout, "")
        if "worktree" in joined:
            return Completed(0, "", "")
        if "kill-session" in joined:
            return Completed(0, "", "")
        raise AssertionError(argv)

    return run


def test_a_work_dir_whose_job_is_running_is_not_reaped(tmp_path: Path) -> None:
    # A running job must keep its worktree; no worktree remove is issued.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "running",
                "session": "job-1",
                "worktree": "/tmp/work/job-1",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []
        jid = row["id"]

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[jid],
            marker_of=lambda j: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("worktree remove" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_work_dir_protected_by_a_fresh_preparing_marker_is_not_reaped(tmp_path: Path) -> None:
    # A fresh preparing marker must protect the worktree even if no running row exists.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=["job-1"],
            marker_of=lambda jid: (True, 1000000000),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("worktree remove" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_work_dir_whose_marker_is_older_than_the_max_age_is_reaped(tmp_path: Path) -> None:
    # An old preparing marker must not protect the worktree forever.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "running",
                "session": "job-1",
                "worktree": "/tmp/work/job-1",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []
        jid = row["id"]

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[jid],
            marker_of=lambda j: (True, 1000000000 - 400) if j == jid else (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
    finally:
        store.close()


def test_an_orphan_with_a_job_row_issues_worktree_remove_then_prune(tmp_path: Path) -> None:
    # A genuine orphan must issue remove then prune and be returned in removed_worktrees.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "done",
                "session": "job-1",
                "worktree": "/tmp/work/job-1",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []
        jid = row["id"]

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[jid],
            marker_of=lambda j: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == [jid]
        assert killed == []
        assert skipped == 0
        joined = [" ".join(argv) for argv in calls]
        remove_idx = next(i for i, c in enumerate(joined) if "worktree remove" in c)
        prune_idx = next(i for i, c in enumerate(joined) if "worktree prune" in c)
        assert remove_idx < prune_idx
    finally:
        store.close()


def test_an_orphan_with_no_job_row_is_skipped_and_issues_no_command(tmp_path: Path) -> None:
    # A directory with no job row must be skipped; raw delete is out of scope.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=["job-1"],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 1
        assert not any("worktree remove" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_job_id_that_escapes_the_work_root_is_refused(tmp_path: Path) -> None:
    # A traversal segment in the job id must be counted skipped and never reach a command.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "done",
                "session": "job-1",
                "worktree": "/tmp/work/../escape",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        store.write("job", "insert", "../escape", row)
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=["../escape"],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 1
        assert not any("worktree remove" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_row_whose_repo_is_missing_or_empty_is_skipped_without_raising(tmp_path: Path) -> None:
    # Missing or empty repo must be skipped, not raise.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "done",
                "session": "job-1",
                "worktree": "/tmp/work/job-1",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        del row["repo"]
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=["job-1"],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 1
    finally:
        store.close()


def test_an_orphaned_session_is_killed_and_returned_in_killed_sessions(tmp_path: Path) -> None:
    # An orphan session must be killed and returned.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls, list_sessions_stdout="job-1\n"),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == ["job-1"]
        assert skipped == 0
        assert any("kill-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_session_named_in_a_running_row_is_not_killed(tmp_path: Path) -> None:
    # A session belonging to a running job must not be killed.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="1",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update(
            {
                "state": "running",
                "session": "job-" + row["id"],
                "worktree": "/tmp/work/job-1",
                "started": "2026-01-01T00:00:00Z",
                "baseline_output_ids": [],
            }
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []
        jid = row["id"]

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls, list_sessions_stdout=f"job-{jid}\n"),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[],
            marker_of=lambda j: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("kill-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_session_whose_job_id_has_a_fresh_preparing_marker_is_not_killed(tmp_path: Path) -> None:
    # A session whose stripped job id has a fresh marker must be protected.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []
        jid = "jobid123"

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls, list_sessions_stdout=f"job-{jid}\n"),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[],
            marker_of=lambda j: (True, 1000000000) if j == jid else (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("kill-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_non_zero_return_from_list_sessions_kills_nothing_at_all(tmp_path: Path) -> None:
    # list-sessions failure must not produce any kill-session command.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls, list_sessions_rc=1, list_sessions_stdout="job-orphan\n"),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("kill-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_session_name_without_the_prefix_is_never_killed(tmp_path: Path) -> None:
    # A name without the prefix must be ignored even if nothing else matches.
    store = Store(tmp_path)
    try:
        calls: list[list[str]] = []

        removed, killed, skipped = reap_orphans(
            store,
            _runner(calls=calls, list_sessions_stdout="editor\n"),
            socket="/tmp/sock",
            repos_root="/tmp/repos",
            work_root="/tmp/work",
            work_dirs=[],
            marker_of=lambda jid: (False, None),
            now_epoch=1000000000,
            session_prefix="job-",
        )

        assert removed == []
        assert killed == []
        assert skipped == 0
        assert not any("kill-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()
