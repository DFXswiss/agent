from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli import error_fix_act as error_fix_act_mod
from agent_cli.error_fix_act import (
    find_or_create_implement_task,
    has_error_fix_activity,
    scan_error_fix,
)
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


def _seen(
    store: Store,
    *,
    repo: str | None = "org/app",
    line_fingerprint: str | None = None,
) -> None:
    payload = {"fingerprint": "api|TimeoutError|abc|prod"}
    if repo is not None:
        payload["repo"] = repo
    if line_fingerprint is not None:
        payload["line_fingerprint"] = line_fingerprint
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


def test_scan_error_fix_persists_origin_default_base_as_ref(tmp_path: Path) -> None:
    """Fresh clone: task.ref must record origin/HEAD (e.g. origin/main), no extra runner call."""
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
            head_ref = destination / ".git" / "refs" / "remotes" / "origin" / "HEAD"
            head_ref.parent.mkdir(parents=True, exist_ok=True)
            head_ref.write_text("ref: refs/remotes/origin/main\n", encoding="utf-8")
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
    assert tasks[0]["ref"] == "origin/main"


def test_scan_error_fix_prints_valid_line_fingerprint(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    hex64 = "ab" * 32
    _seen(store, line_fingerprint=hex64)
    _fix(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["git", "clone", "--"]:
            destination = Path(argv[-1])
            (destination / ".git").mkdir(parents=True)
        return Completed(0, "", "")

    lines = scan_error_fix(store, runner)
    task_id = store.rows("task")[0]["id"]
    worktree = tmp_path / "error-fix-work" / task_id
    assert lines == [
        f"error.fix fix-1 task={task_id} worktree={worktree} line_fingerprint={hex64}"
    ]
    row = store.row("activity", "fix-1")
    updated = {k: v for k, v in row.items() if not k.startswith("_")}
    updated["execution_status"] = "pending"
    store.write("activity", "update", "fix-1", updated)
    assert scan_error_fix(store, runner) == [
        f"error.fix fix-1 task={task_id} worktree={worktree} line_fingerprint={hex64}"
    ]


def test_scan_error_fix_omits_invalid_line_fingerprint(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, line_fingerprint="NOTHEX")
    _fix(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["git", "clone", "--"]:
            destination = Path(argv[-1])
            (destination / ".git").mkdir(parents=True)
        return Completed(0, "", "")

    lines = scan_error_fix(store, runner)
    task_id = store.rows("task")[0]["id"]
    worktree = tmp_path / "error-fix-work" / task_id
    assert lines == [f"error.fix fix-1 task={task_id} worktree={worktree}"]


@pytest.mark.parametrize(
    "value",
    [
        "ab" * 31 + "a",  # 63
        "ab" * 32 + "a",  # 65
        "AB" * 32,  # uppercase
        "ag" * 32,  # non-hex
    ],
)
def test_scan_error_fix_omits_malformed_line_fingerprints(
    tmp_path: Path, value: str
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store, line_fingerprint=value)
    _fix(store)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["git", "clone", "--"]:
            destination = Path(argv[-1])
            (destination / ".git").mkdir(parents=True)
        return Completed(0, "", "")

    lines = scan_error_fix(store, runner)
    task_id = store.rows("task")[0]["id"]
    worktree = tmp_path / "error-fix-work" / task_id
    assert lines == [f"error.fix fix-1 task={task_id} worktree={worktree}"]


def test_scan_error_fix_prints_without_fingerprint_if_reload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    hex64 = "ab" * 32
    _seen(store, line_fingerprint=hex64)
    _fix(store)
    real = error_fix_act_mod._error_seen

    def after_mark(store_inner: Store, session_id: str, error_id: str) -> dict:
        row = store_inner.row("activity", "fix-1")
        if row is not None and row.get("execution_status") == "done":
            raise StoreError("error.seen not found")
        return real(store_inner, session_id, error_id)

    monkeypatch.setattr(error_fix_act_mod, "_error_seen", after_mark)
    lines = scan_error_fix(store, _clone_runner([]))
    task_id = store.rows("task")[0]["id"]
    worktree = tmp_path / "error-fix-work" / task_id
    assert lines == [f"error.fix fix-1 task={task_id} worktree={worktree}"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "done"
    updated = {k: v for k, v in row.items() if not k.startswith("_")}
    updated["execution_status"] = "pending"
    store.write("activity", "update", "fix-1", updated)
    assert scan_error_fix(store, _clone_runner([])) == [
        f"error.fix fix-1 task={task_id} worktree={worktree}"
    ]
    again = store.row("activity", "fix-1")
    assert again is not None
    assert again["execution_status"] == "done"


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


def test_has_error_fix_activity_true_for_matching_fix(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    assert has_error_fix_activity(store, "runner-1", "error-seen-12345678") is True


def test_has_error_fix_activity_false_for_seen_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    assert has_error_fix_activity(store, "runner-1", "error-seen-12345678") is False


def test_has_error_fix_activity_false_for_empty_error_id(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    assert has_error_fix_activity(store, "runner-1", "") is False


def test_nonempty_str_rejects_unicode_whitespace_only() -> None:
    """U+00A0-only must be absent so run_core cannot strip it to skip identity."""
    assert error_fix_act_mod._nonempty_str("\u00a0") is None
    assert error_fix_act_mod._nonempty_str("\u00a0\u00a0") is None
    assert error_fix_act_mod._nonempty_str("") is None
    assert error_fix_act_mod._nonempty_str("  ") is None
    assert error_fix_act_mod._nonempty_str("ok") == "ok"
    assert error_fix_act_mod._nonempty_str("  ok  ") == "ok"


def test_has_error_fix_activity_false_for_mismatched_ids(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    assert (
        has_error_fix_activity(store, "runner-1", "other-error-id-00000000") is False
    )
    assert (
        has_error_fix_activity(store, "other-session", "error-seen-12345678") is False
    )


def test_find_or_create_returns_existing_task_for_whitespace_padded_error_id(
    tmp_path: Path,
) -> None:
    """A prior implement task persisted with incidental whitespace in
    payload.error_id (simulated by writing the task row directly, bypassing
    the normal create path's normalization) must still be found by
    find_or_create_implement_task for a normalized error_id, not duplicated."""
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    store.write(
        "task",
        "insert",
        "task-1",
        {
            "id": "task-1",
            "session_id": "runner-1",
            "workflow": "implement",
            "title": "Fix observed error",
            "repo": "org/app",
            "ref": None,
            "payload": {"error_id": "error-seen-12345678 ", "repo": "org/app"},
            "state": "open",
            "current_round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    tid, created = find_or_create_implement_task(
        store,
        "runner-1",
        "error-seen-12345678",
        "Fix observed error",
    )
    assert created is False
    assert tid == "task-1"


def test_has_error_fix_activity_true_for_whitespace_padded_persisted_error_id(
    tmp_path: Path,
) -> None:
    """Round 26 regression: a persisted error.fix payload.error_id with
    incidental whitespace (simulated by writing the activity row directly,
    bypassing validate_conclusion's normalization) must still match the
    caller's already-normalized error_id."""
    store = Store(tmp_path)
    store.write(
        "activity",
        "insert",
        "fix-1",
        {
            "id": "fix-1",
            "session_id": "runner-1",
            "type": "error.fix",
            "payload": {
                "error_id": "error-seen-12345678\u00a0",
                "fingerprint": "api|TimeoutError|abc|prod",
            },
            "execution_status": "pending",
        },
    )
    assert has_error_fix_activity(store, "runner-1", "error-seen-12345678") is True


def test_validate_conclusion_matches_whitespace_padded_stored_fingerprint(
    tmp_path: Path,
) -> None:
    """Legacy error.seen rows may retain a whitespace-padded fingerprint; the
    stored side must be stripped before compare (site 1)."""
    store = Store(tmp_path)
    _runner_session(store)
    store.write(
        "activity",
        "insert",
        "error-seen-12345678",
        {
            "id": "error-seen-12345678",
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": {
                "fingerprint": "api|TimeoutError|abc|prod ",
                "repo": "org/app",
            },
            "execution_status": "done",
        },
    )
    normalized = error_fix_act_mod.validate_conclusion(
        store,
        "runner-1",
        "error.skip",
        {
            "error_id": "error-seen-12345678",
            "fingerprint": "api|TimeoutError|abc|prod",
            "reason": "noisy",
        },
    )
    assert normalized["fingerprint"] == "api|TimeoutError|abc|prod"
    assert normalized["error_id"] == "error-seen-12345678"


def test_already_open_draft_matches_whitespace_padded_fingerprints(
    tmp_path: Path,
) -> None:
    """_error_fix_heads (site 2) and _draft_matches (site 3) must strip the
    stored fingerprint before comparing to a bare computed value."""
    store = Store(tmp_path)
    _runner_session(store)
    store.write(
        "activity",
        "insert",
        "error-seen-12345678",
        {
            "id": "error-seen-12345678",
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": {
                "fingerprint": "api|TimeoutError|abc|prod ",
                "repo": "org/app",
            },
            "execution_status": "done",
        },
    )
    bare = "api|TimeoutError|abc|prod"
    assert error_fix_act_mod._error_fix_heads(store, bare) == {"error-fix-error-se"}
    store.write(
        "activity",
        "insert",
        "pr-open-1",
        {
            "id": "pr-open-1",
            "session_id": "runner-1",
            "type": "pr.open",
            "payload": {"fingerprint": "api|TimeoutError|abc|prod "},
            "execution_status": "pending",
        },
    )
    assert error_fix_act_mod._draft_matches(
        {"fingerprint": "api|TimeoutError|abc|prod "}, bare, set()
    )
    assert error_fix_act_mod._already_open_draft(store, bare) is True


def test_pending_fix_matches_whitespace_padded_stored_fingerprint(
    tmp_path: Path,
) -> None:
    """_pending_fix / scan_error_fix (site 4): legacy padded fingerprint on
    error.seen must still match the bare fingerprint on error.fix."""
    store = Store(tmp_path)
    _runner_session(store)
    store.write(
        "activity",
        "insert",
        "error-seen-12345678",
        {
            "id": "error-seen-12345678",
            "session_id": "runner-1",
            "type": "error.seen",
            "payload": {
                "fingerprint": "api|TimeoutError|abc|prod ",
                "repo": "org/app",
            },
            "execution_status": "done",
        },
    )
    _fix(store)
    calls: list[list[str]] = []
    lines = scan_error_fix(store, _clone_runner(calls))
    assert len(lines) == 1
    assert lines[0].startswith("error.fix fix-1 task=")
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "done"
