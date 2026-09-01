from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli.dispatch import dispatch_queued, running_slots, session_name, worktree_path
from agent_cli.ingest import job_row
from agent_cli.runtime import Completed
from agent_cli.store import Store


def _runner(
    *,
    calls: list[list[str]],
    is_bare_rc: int = 1,
    is_bare_stdout: str = "false",
    clone_rc: int = 0,
    refspec_rc: int = 0,
    fetch_rc: int = 0,
    add_rc: int = 0,
    remove_rc: int = 0,
    prune_rc: int = 0,
    start_rc: int = 0,
    has_rc: int = 0,
    kill_rc: int = 0,
) -> Callable[[list[str]], Completed]:
    def run(argv: list[str]) -> Completed:
        calls.append(list(argv))
        joined = " ".join(argv)
        if "rev-parse --is-bare-repository" in joined:
            return Completed(is_bare_rc, is_bare_stdout, "")
        if "clone --bare" in joined:
            return Completed(clone_rc, "", "")
        if "config remote.origin.fetch" in joined:
            return Completed(refspec_rc, "", "")
        if "fetch --prune" in joined:
            return Completed(fetch_rc, "", "")
        if "worktree add" in joined:
            return Completed(add_rc, "", "")
        if "worktree remove" in joined:
            return Completed(remove_rc, "", "")
        if "worktree prune" in joined:
            return Completed(prune_rc, "", "")
        if "new-session" in joined:
            return Completed(start_rc, "", "")
        if "has-session" in joined:
            return Completed(has_rc, "", "")
        if "kill-session" in joined:
            return Completed(kill_rc, "", "")
        raise AssertionError(argv)

    return run


def _dispatch(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    max_concurrent: int = 1,
) -> tuple[list[str], int]:
    return dispatch_queued(
        store,
        runner,
        socket="/tmp/agent.sock",
        repos_root="/tmp/repos",
        work_root="/tmp/work",
        wrapper="/bin/agent-wrapper",
        max_concurrent=max_concurrent,
    )


# --------------------------------------------------------------- naming


def test_the_worktree_path_places_the_job_under_the_work_root() -> None:
    # A worker must receive an isolated path derived from its own job id.
    assert worktree_path("/tmp/work", "job-7") == "/tmp/work/job-7"


def test_the_session_name_is_safe_for_tmux() -> None:
    # Job punctuation must not leak into the tmux session name.
    assert session_name("job/7") == "agent-job-7"


# --------------------------------------------------------------- slots


def test_a_live_running_session_occupies_one_slot(tmp_path: Path) -> None:
    # Capacity accounting must ask tmux whether a stored running worker is live.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        row.update({"state": "running", "session": "agent-job-7"})
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        slots = running_slots(
            store,
            _runner(calls=calls, has_rc=0),
            socket="/tmp/agent.sock",
        )

        assert slots == 1
        assert len(calls) == 1
        assert "has-session" in " ".join(calls[0])
        assert store.row("job", row["id"])["state"] == "running"
    finally:
        store.close()


# ------------------------------------------------------------ successful dispatch


def test_all_job_fields_survive_the_start(tmp_path: Path) -> None:
    # Replacement updates must not erase the fields required by the next phase.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls))

        assert started == [row["id"]]
        assert skipped == 0
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["repo"] == "owner/name"
        assert saved["ref"] == "7"
        assert saved["job_type"] == "pr-review"
        assert saved["actor"] == "davidleomay"
        assert saved["attempts"] == 0
        assert saved["state"] == "running"
        assert saved["session"] == session_name(row["id"])
        assert saved["worktree"] == worktree_path("/tmp/work", row["id"])
        assert saved["started"]
    finally:
        store.close()


def test_fetch_runs_when_the_bare_repository_already_exists(tmp_path: Path) -> None:
    # An existing bare clone still needs fresh remote refs before worktree creation.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(
            store,
            _runner(calls=calls, is_bare_rc=0, is_bare_stdout="true"),
        )

        joined = [" ".join(argv) for argv in calls]
        assert started == [row["id"]]
        assert skipped == 0
        assert not any("clone --bare" in command for command in joined)
        assert any("fetch --prune" in command for command in joined)
        assert store.row("job", row["id"])["state"] == "running"
    finally:
        store.close()


# ------------------------------------------------------------- capacity


def test_capacity_is_rechecked_after_each_job_starts(tmp_path: Path) -> None:
    # A newly started worker must consume capacity before the next queued row.
    store = Store(tmp_path)
    try:
        first = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        second = job_row(
            session_id="s",
            repo="owner/name",
            ref="8",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", first["id"], first)
        store.write("job", "insert", second["id"], second)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls), max_concurrent=1)

        assert len(started) == 1
        assert skipped == 0
        states = {row["id"]: row["state"] for row in store.rows("job")}
        assert states[started[0]] == "running"
        waiting = ({first["id"], second["id"]} - set(started)).pop()
        assert states[waiting] == "queued"
        assert len([argv for argv in calls if "new-session" in " ".join(argv)]) == 1
    finally:
        store.close()


def test_a_running_row_without_a_session_occupies_no_slot(tmp_path: Path) -> None:
    # An orphaned running row must not prevent valid queued work from starting.
    store = Store(tmp_path)
    try:
        orphan = job_row(
            session_id="s",
            repo="owner/name",
            ref="6",
            job_type="pr-review",
            actor="davidleomay",
        )
        orphan["state"] = "running"
        queued = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", orphan["id"], orphan)
        store.write("job", "insert", queued["id"], queued)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls), max_concurrent=1)

        assert started == [queued["id"]]
        assert skipped == 0
        assert store.row("job", queued["id"])["state"] == "running"
        saved_orphan = store.row("job", orphan["id"])
        assert saved_orphan["state"] == "running"
        assert "session" not in saved_orphan
        assert not any("has-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


# -------------------------------------------------- repository preparation


def test_a_clone_failure_leaves_the_job_queued_and_counts_it_skipped(tmp_path: Path) -> None:
    # A transient clone failure must leave the row available for a later retry.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls, clone_rc=1))

        assert started == []
        assert skipped == 1
        assert store.row("job", row["id"])["state"] == "queued"
        assert not any("fetch --prune" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_fetch_failure_leaves_the_job_queued_and_counts_it_skipped(tmp_path: Path) -> None:
    # A transient fetch failure must not turn a retryable row into a failed job.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls, fetch_rc=1))

        assert started == []
        assert skipped == 1
        assert store.row("job", row["id"])["state"] == "queued"
        assert not any("worktree add" in " ".join(argv) for argv in calls)
    finally:
        store.close()


# --------------------------------------------------------- worker failures


def test_a_worktree_add_failure_marks_the_workspace_failed(tmp_path: Path) -> None:
    # A workspace that cannot be created is a terminal preparation failure.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls, add_rc=1))

        assert started == []
        assert skipped == 1
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "failed"
        assert saved["outcome"] == "workspace_failed"
        assert saved["finished"]
        assert not any("new-session" in " ".join(argv) for argv in calls)
    finally:
        store.close()


def test_a_worker_start_failure_cleans_the_worktree_and_marks_the_job_crashed(
    tmp_path: Path,
) -> None:
    # A failed tmux start must remove and prune its otherwise stranded worktree.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls, start_rc=1))

        assert started == []
        assert skipped == 1
        joined = [" ".join(argv) for argv in calls]
        remove_at = next(i for i, command in enumerate(joined) if "worktree remove" in command)
        prune_at = next(i for i, command in enumerate(joined) if "worktree prune" in command)
        assert remove_at < prune_at
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "failed"
        assert saved["outcome"] == "crashed"
        assert saved["finished"]
    finally:
        store.close()


# -------------------------------------------------------------- validation


def test_a_malformed_row_is_skipped_without_raising(tmp_path: Path) -> None:
    # One malformed queue entry must not abort the dispatcher process.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        del row["repo"]
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        started, skipped = _dispatch(store, _runner(calls=calls))

        assert started == []
        assert skipped == 1
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "queued"
        assert "repo" not in saved
        assert calls == []
    finally:
        store.close()


def test_zero_capacity_is_rejected(tmp_path: Path) -> None:
    # A disabled dispatcher must be configured outside this positive limit.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        with pytest.raises(ValueError, match="max_concurrent must be a positive int"):
            _dispatch(store, _runner(calls=calls), max_concurrent=0)

        assert store.row("job", row["id"])["state"] == "queued"
        assert calls == []
    finally:
        store.close()



# ----------------------------------------------------- exception paths


def test_write_update_failure_triggers_worktree_and_session_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed state transition must still clean the prepared workspace.
    store = Store(tmp_path)
    try:
        row = job_row(
            session_id="s",
            repo="owner/name",
            ref="7",
            job_type="pr-review",
            actor="davidleomay",
        )
        store.write("job", "insert", row["id"], row)
        calls: list[list[str]] = []

        original_write = store.write

        def raising_write(op: str, action: str, key: str, value: object) -> None:
            if action == "update":
                raise RuntimeError("db unavailable")
            original_write(op, action, key, value)

        monkeypatch.setattr(store, "write", raising_write)

        started, skipped = _dispatch(store, _runner(calls=calls))

        assert started == []
        assert skipped == 1
        joined = [" ".join(argv) for argv in calls]
        remove_at = next(i for i, c in enumerate(joined) if "worktree remove" in c)
        prune_at = next(i for i, c in enumerate(joined) if "worktree prune" in c)
        kill_at = next(i for i, c in enumerate(joined) if "kill-session" in c)
        assert kill_at < remove_at < prune_at
    finally:
        store.close()
