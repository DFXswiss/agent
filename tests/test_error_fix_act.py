from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli.error_fix_act import find_or_create_implement_task, scan_error_fix
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError, utcnow


def _runner_session(store: Store) -> None:
    store.write(
        "session",
        "insert",
        "runner-1",
        {
            "id": "runner-1",
            "kind": "runner",
            "status": "active",
            "skills": ["spine", "error-fix"],
        },
    )


def _seen(store: Store, *, repo: str | None = "org/app") -> None:
    payload = {"fingerprint": "api|TimeoutError|abc|prod"}
    if repo is not None:
        payload["repo"] = repo
    store.write(
        "activity",
        "insert",
        "error-seen-12345678",
        {
            "id": "error-seen-12345678",
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": payload,
            "execution_status": "done",
        },
    )


def _fix(store: Store, activity_id: str = "fix-1") -> None:
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "runner-1",
            "type": "error.fix",
            "payload": {
                "error_id": "error-seen-12345678",
                "fingerprint": "api|TimeoutError|abc|prod",
            },
            "execution_status": "pending",
        },
    )


def test_scan_error_fix_clones_then_reuses(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["git", "clone", "--"]:
            destination = Path(argv[-1])
            (destination / ".git").mkdir(parents=True)
        return Completed(0, "", "")

    lines = scan_error_fix(store, runner)
    tasks = store.rows("task")
    assert len(tasks) == 1
    task_id = tasks[0]["id"]
    staging = tmp_path / "error-fix-work" / "pending-fix-1"
    worktree = tmp_path / "error-fix-work" / task_id
    assert lines == [f"error.fix fix-1 task={task_id} worktree={worktree}"]
    assert calls == [
        ["git", "clone", "--", "https://github.com/org/app.git", str(staging)],
        [
            "git",
            "-C",
            str(staging),
            "checkout",
            "-B",
            "error-fix-error-se",
        ],
    ]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"] == {
        "task_id": task_id,
        "worktree": str(worktree),
        "head": "error-fix-error-se",
        "repo": "org/app",
    }
    assert tasks[0]["repo"] == "org/app"
    assert tasks[0]["payload"] == {
        "error_id": "error-seen-12345678",
        "repo": "org/app",
    }

    updated = {k: v for k, v in row.items() if not k.startswith("_")}
    updated["execution_status"] = "pending"
    store.write("activity", "update", "fix-1", updated)
    calls.clear()
    assert scan_error_fix(store, runner) == [
        f"error.fix fix-1 task={task_id} worktree={worktree}"
    ]
    assert calls == []
    assert len(store.rows("task")) == 1


def test_scan_error_fix_marks_ineligible_rows(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, repo=None)
    _fix(store)

    assert scan_error_fix(store, lambda _argv: Completed(0, "", "")) == [
        "error.fix fix-1 error"
    ]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["execution_error"] == "unmapped-repo"


def test_scan_error_fix_clone_failure_stays_pending_and_cleans_staging(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)

    assert scan_error_fix(
        store,
        lambda _argv: Completed(1, "", "clone failed"),
    ) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "pending"
    assert store.rows("task") == []
    assert not (tmp_path / "error-fix-work" / "pending-fix-1").exists()


def test_find_or_create_requires_active_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    session = store.row("session", "runner-1")
    assert session is not None
    updated = {key: value for key, value in session.items() if not key.startswith("_")}
    updated["status"] = "closed"
    store.write("session", "update", "runner-1", updated)
    with pytest.raises(StoreError, match="session is not active"):
        find_or_create_implement_task(
            store,
            "runner-1",
            "error-seen-12345678",
            "Fix observed error",
        )
    assert store.rows("task") == []


def _clone_runner(calls: list[list[str]]) -> Callable[[list[str]], Completed]:
    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["git", "clone", "--"]:
            Path(argv[-1]).joinpath(".git").mkdir(parents=True)
        return Completed(0, "", "")

    return runner


def test_scan_error_fix_honors_existing_task_ref(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    find_or_create_implement_task(
        store,
        "runner-1",
        "error-seen-12345678",
        "Fix observed error",
        ref="origin/develop",
    )
    _fix(store)
    calls: list[list[str]] = []
    lines = scan_error_fix(store, _clone_runner(calls))
    tasks = store.rows("task")
    assert len(tasks) == 1
    staging = tmp_path / "error-fix-work" / "pending-fix-1"
    worktree = tmp_path / "error-fix-work" / tasks[0]["id"]
    assert lines == [f"error.fix fix-1 task={tasks[0]['id']} worktree={worktree}"]
    assert [
        "git",
        "-C",
        str(staging),
        "checkout",
        "-B",
        "error-fix-error-se",
        "origin/develop",
    ] in calls
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "done"


def test_scan_error_fix_rename_oserror_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    real_rename = Path.rename

    def boom(self: Path, target: Path) -> Path:
        if self.name.startswith("pending-"):
            raise OSError("cross-device")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", boom)
    calls: list[list[str]] = []
    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "pending"
    assert len(store.rows("task")) == 1
    staging = tmp_path / "error-fix-work" / "pending-fix-1"
    assert (staging / ".git").exists()


def test_scan_error_fix_ignores_non_implement_task(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    now = utcnow()
    store.write(
        "task",
        "insert",
        "review-task",
        {
            "id": "review-task",
            "session_id": "runner-1",
            "workflow": "review",
            "title": "Review observed error",
            "repo": "org/app",
            "ref": None,
            "payload": {"error_id": "error-seen-12345678", "repo": "org/app"},
            "state": "open",
            "current_round": 0,
            "created_at": now,
            "updated_at": now,
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    _fix(store)
    calls: list[list[str]] = []
    scan_error_fix(store, _clone_runner(calls))
    implement = [row for row in store.rows("task") if row.get("workflow") == "implement"]
    assert len(implement) == 1
    assert implement[0]["id"] != "review-task"
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["task_id"] == implement[0]["id"]


def test_find_or_create_rejects_already_open_draft(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    store.write(
        "activity",
        "insert",
        "pr-open-1",
        {
            "id": "pr-open-1",
            "session_id": "runner-1",
            "type": "pr.open",
            "payload": {"head": "error-fix-error-se", "repo": "org/app"},
            "execution_status": "pending",
        },
    )
    with pytest.raises(StoreError, match="already-open-draft"):
        find_or_create_implement_task(
            store,
            "runner-1",
            "error-seen-12345678",
            "Fix observed error",
        )
    assert store.rows("task") == []


def test_find_or_create_returns_existing_implement_despite_draft(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    tid, created = find_or_create_implement_task(
        store,
        "runner-1",
        "error-seen-12345678",
        "Fix observed error",
    )
    assert created is True
    store.write(
        "activity",
        "insert",
        "pr-open-1",
        {
            "id": "pr-open-1",
            "session_id": "runner-1",
            "type": "pr.open",
            "payload": {"fingerprint": "api|TimeoutError|abc|prod"},
            "execution_status": "pending",
        },
    )
    again, created_again = find_or_create_implement_task(
        store,
        "runner-1",
        "error-seen-12345678",
        "Fix observed error",
    )
    assert again == tid
    assert created_again is False


def test_scan_inactive_session_after_clone_stays_pending(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    session = store.row("session", "runner-1")
    assert session is not None
    updated = {key: value for key, value in session.items() if not key.startswith("_")}
    updated["status"] = "closed"
    store.write("session", "update", "runner-1", updated)
    calls: list[list[str]] = []
    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "pending"
    assert store.rows("task") == []
    assert not (tmp_path / "error-fix-work" / "pending-fix-1").exists()
