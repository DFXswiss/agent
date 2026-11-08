from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli import error_fix_act as error_fix_act_mod
from agent_cli.error_fix_act import find_or_create_implement_task, scan_error_fix
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError, utcnow


def _runner_session(store: Store) -> None:
    # The local harness configures device model slots before creating tasks.
    if not (store.home / "ai-accounts.json").exists():
        (store.home / "ai-accounts.json").write_text(
            json.dumps({"models": {"first": "grok", "second": "codex"}}),
            encoding="utf-8",
        )
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


def _legacy_task(store: Store) -> None:
    now = utcnow()
    store.write(
        "task",
        "insert",
        "legacy-error-fix-task",
        {
            "id": "legacy-error-fix-task",
            "session_id": "runner-1",
            "workflow": "implement",
            "title": "Legacy error fix",
            "repo": "org/app",
            "ref": None,
            "payload": {"error_id": "error-seen-12345678", "repo": "org/app"},
            "state": "open",
            "current_round": 0,
            "created_at": now,
            "updated_at": now,
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


def test_scan_error_fix_pre_clone_session_race_stays_pending_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    staging = tmp_path / "error-fix-work" / "pending-fix-1"
    staging.mkdir(parents=True)
    active_checks = 0
    require_active_session = error_fix_act_mod._require_active_session

    def close_after_first_check(current_store: Store, session_id: str) -> dict:
        nonlocal active_checks
        active_checks += 1
        session = require_active_session(current_store, session_id)
        if active_checks == 1:
            updated = {
                key: value for key, value in session.items() if not key.startswith("_")
            }
            updated["status"] = "closed"
            current_store.write("session", "update", session_id, updated)
        return session

    monkeypatch.setattr(
        error_fix_act_mod,
        "_require_active_session",
        close_after_first_check,
    )
    calls: list[list[str]] = []

    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "pending"
    assert row.get("execution_error") is None
    assert row["result"] == {}
    assert active_checks == 2
    assert calls == []
    assert store.rows("task") == []
    assert not staging.exists()


def test_scan_error_fix_waits_for_model_configuration_before_git(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    (tmp_path / "ai-accounts.json").unlink()
    _seen(store)
    _fix(store)
    calls: list[list[str]] = []

    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["execution_error"].startswith("models is not configured:")
    assert row["result"]["kind"] == "model-configuration"
    assert calls == []
    assert not (tmp_path / "error-fix-work").exists()

    assert scan_error_fix(store, _clone_runner(calls)) == []
    assert calls == []

    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "grok", "second": "codex"}}),
        encoding="utf-8",
    )
    lines = scan_error_fix(store, _clone_runner(calls))
    tasks = store.rows("task")
    assert len(tasks) == 1
    assert lines == [
        f"error.fix fix-1 task={tasks[0]['id']} "
        f"worktree={tmp_path / 'error-fix-work' / tasks[0]['id']}"
    ]
    assert calls[0][:3] == ["git", "clone", "--"]


def test_scan_error_fix_requires_explicit_legacy_supersede_before_git(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    _legacy_task(store)
    calls: list[list[str]] = []

    lines = scan_error_fix(store, _clone_runner(calls))
    legacy = store.row("task", "legacy-error-fix-task")
    assert legacy is not None
    assert legacy["state"] == "open"
    assert lines == ["error.fix fix-1 error"]
    assert calls == []
    assert not (tmp_path / "error-fix-work").exists()
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert (
        "agent task state legacy-error-fix-task superseded"
        in row["execution_error"]
    )
    first_fingerprint = row["result"]["configuration_fingerprint"]

    assert scan_error_fix(store, _clone_runner(calls)) == []
    assert calls == []

    updated_legacy = {
        key: value for key, value in legacy.items() if not key.startswith("_")
    }
    updated_legacy["state"] = "superseded"
    store.write("task", "update", legacy["id"], updated_legacy)

    lines = scan_error_fix(store, _clone_runner(calls))
    tasks = store.rows("task")
    replacements = [task for task in tasks if task["id"] != legacy["id"]]
    assert len(replacements) == 1
    replacement = replacements[0]
    assert replacement["models"] == {"first": "grok", "second": "codex"}
    assert lines == [
        f"error.fix fix-1 task={replacement['id']} "
        f"worktree={tmp_path / 'error-fix-work' / replacement['id']}"
    ]
    assert calls[0][:3] == ["git", "clone", "--"]
    done = store.row("activity", "fix-1")
    assert done is not None
    assert done["execution_status"] == "done"
    assert done["result"]["task_id"] == replacement["id"]
    assert done["result"].get("configuration_fingerprint") is None
    assert first_fingerprint


@pytest.mark.parametrize("state", ["done", "failed", "superseded"])
def test_lookup_implement_task_ignores_terminal_tasks(
    tmp_path: Path, state: str
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _legacy_task(store)
    task = store.row("task", "legacy-error-fix-task")
    assert task is not None
    updated = {key: value for key, value in task.items() if not key.startswith("_")}
    updated["state"] = state
    store.write("task", "update", task["id"], updated)

    assert (
        error_fix_act_mod._lookup_implement_task(
            store, "runner-1", "error-seen-12345678"
        )
        is None
    )


def test_scan_error_fix_legacy_fingerprint_changes_with_model_configuration(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    (tmp_path / "ai-accounts.json").unlink()
    _seen(store)
    _fix(store)
    _legacy_task(store)
    calls: list[list[str]] = []

    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["result"]["kind"] == "model-configuration"
    first_fingerprint = row["result"]["configuration_fingerprint"]
    assert calls == []
    assert len(store.rows("task")) == 1
    legacy = store.row("task", "legacy-error-fix-task")
    assert legacy is not None
    assert legacy["state"] == "open"
    assert not (tmp_path / "error-fix-work").exists()

    assert scan_error_fix(store, _clone_runner(calls)) == []
    assert calls == []

    (tmp_path / "ai-accounts.json").write_text(
        json.dumps({"models": {"first": "grok", "second": "codex"}}),
        encoding="utf-8",
    )
    lines = scan_error_fix(store, _clone_runner(calls))
    legacy = store.row("task", "legacy-error-fix-task")
    assert legacy is not None
    assert legacy["state"] == "open"
    assert lines == ["error.fix fix-1 error"]
    retried = store.row("activity", "fix-1")
    assert retried is not None
    assert retried["result"]["configuration_fingerprint"] != first_fingerprint
    assert calls == []


def test_generic_error_clears_stale_configuration_fingerprint(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    row = store.row("activity", "fix-1")
    assert row is not None
    updated = {key: value for key, value in row.items() if not key.startswith("_")}
    updated["result"] = {
        "kind": "model-configuration",
        "configuration_fingerprint": "stale",
    }
    store.write("activity", "update", row["id"], updated)
    stale = store.row("activity", "fix-1")
    assert stale is not None

    error_fix_act_mod._mark(store, stale, status="error", error="ordinary failure")

    marked = store.row("activity", "fix-1")
    assert marked is not None
    assert marked["result"] == {}
    assert not error_fix_act_mod.is_retryable_configuration_error(marked)


def test_scan_error_fix_rechecks_active_session_before_each_git_operation(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["git", "clone", "--"]:
            Path(argv[-1]).joinpath(".git").mkdir(parents=True)
            session = store.row("session", "runner-1")
            assert session is not None
            updated = {
                key: value for key, value in session.items() if not key.startswith("_")
            }
            updated["status"] = "closed"
            store.write("session", "update", session["id"], updated)
        return Completed(0, "", "")

    assert scan_error_fix(store, runner) == ["error.fix fix-1 error"]
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "clone", "--"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert row["execution_error"] == "session is not active"
    assert store.rows("task") == []


def test_scan_error_fix_rejects_existing_task_with_invalid_implementer_before_git(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    _fix(store)
    now = utcnow()
    store.write(
        "task",
        "insert",
        "invalid-implementer-task",
        {
            "id": "invalid-implementer-task",
            "session_id": "runner-1",
            "workflow": "implement",
            "title": "Invalid implementer error fix",
            "repo": "org/app",
            "ref": None,
            "payload": {"error_id": "error-seen-12345678", "repo": "org/app"},
            "state": "open",
            "current_round": 0,
            "models": {"first": "grok", "second": "codex"},
            "implementer": "not-a-provider",
            "created_at": now,
            "updated_at": now,
        },
    )
    calls: list[list[str]] = []

    assert scan_error_fix(store, _clone_runner(calls)) == ["error.fix fix-1 error"]
    row = store.row("activity", "fix-1")
    assert row is not None
    assert row["execution_status"] == "error"
    assert "implementer must be one of" in row["execution_error"]
    assert calls == []
    assert not (tmp_path / "error-fix-work").exists()


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



def test_find_or_create_pins_the_device_models(tmp_path: Path) -> None:
    import json as _json

    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    (store.home / "ai-accounts.json").write_text(
        _json.dumps(
            {
                "models": {"first": "claude", "second": "codex"},
                "implementer": "deepseek",
            }
        ),
        encoding="utf-8",
    )
    find_or_create_implement_task(store, "runner-1", "error-seen-12345678", "Fix observed error")
    tasks = store.rows("task")
    assert len(tasks) == 1
    assert tasks[0]["models"] == {"first": "claude", "second": "codex"}
    assert tasks[0]["implementer"] == "deepseek"


def test_find_or_create_omits_an_implementer_equal_to_models_first(
    tmp_path: Path,
) -> None:
    import json as _json

    store = Store(tmp_path)
    _runner_session(store)
    _seen(store)
    (store.home / "ai-accounts.json").write_text(
        _json.dumps(
            {
                "models": {"first": "claude", "second": "codex"},
                "implementer": "claude",
            }
        ),
        encoding="utf-8",
    )
    find_or_create_implement_task(store, "runner-1", "error-seen-12345678", "Fix observed error")
    tasks = store.rows("task")
    assert len(tasks) == 1
    assert "implementer" not in tasks[0]
