"""Tests for the error-fix fixer driver (fixer_act) and related run_core wiring."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from agent_cli.fixer_act import (
    _contributing_ok_evidence,
    _drive_one,
    _error_fix_brief,
    _first_sentence,
    _open_error_fix_tasks,
    _pr_open_number,
    _pr_open_row_exists,
    _runner_to_completed,
    drive_error_fix_tasks,
    insert_pr_open_and_scan,
    template_pr_open_payload,
    write_error_fix_spec,
)
from agent_cli.git_act import GitActError, push_branch
from agent_cli.lane import LaneResult, findings_header_present
from agent_cli.run_core import ReviewDiffUnavailableError, build_review_spec_file
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_cli import _last_task_id, run
from test_run import (
    _agents,
    _checklist,
    _finish_implementer,
    _finish_reviewer,
    _local_checks,
    _task_state,
)


ERROR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _store(home: Path) -> Store:
    os.environ["AGENT_HOME"] = str(home)
    return Store(home)


def _gates(home: Path, tid: str) -> list[dict]:
    store = _store(home)
    try:
        return [r for r in store.rows("review_gate") if r.get("task_id") == tid]
    finally:
        store.close()


def _bootstrap_error_fix_task(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> str:
    """Create an error-fix-originated implement task; leave at implementer_done open."""
    run(home, ["init"])
    run(
        home,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
            "--skill",
            "error-fix",
        ],
    )
    store = _store(home)
    try:
        store.write(
            "activity",
            "insert",
            ERROR_ID,
            {
                "id": ERROR_ID,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "repo": "org/app",
                    "service": "api",
                    "class": "TimeoutError",
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": ERROR_ID,
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Timeout in handler; add retry.",
                },
                "execution_status": "pending",
            },
        )
    finally:
        store.close()

    run(
        home,
        [
            "task",
            "create",
            "--session",
            "sess-1",
            "--workflow",
            "implement",
            "--error-id",
            ERROR_ID,
            "--title",
            "Fix timeout",
        ],
    )
    tid = _last_task_id(capsys.readouterr().out)
    run(
        home,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    run(
        home,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "script",
            "--evidence",
            "auto spec from error.fix brief",
        ],
    )
    run(home, ["round", "start", "--task", tid])
    worktree = home / "error-fix-work" / tid
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").mkdir(exist_ok=True)
    # Spec lives under error-fix-specs (sibling of the git worktree), never
    # inside the pushed clone.
    specs = home / "error-fix-specs" / tid
    specs.mkdir(parents=True, exist_ok=True)
    (specs / ".spec.md").write_text("# Task\n\nfix it\n", encoding="utf-8")
    capsys.readouterr()
    return tid


def _advance_error_fix_to_pushed(
    home: Path,
    tid: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _finish_implementer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    _finish_reviewer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    monkeypatch.setattr(
        "agent_cli.main._exec_argv",
        lambda argv, *, cwd=None: Completed(0, "ok", ""),
    )
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(home, tid)["local_check_pass"] == "ja"


def test_findings_header_present_distinguishes_absent() -> None:
    assert findings_header_present("STATUS: complete\n") is False
    assert findings_header_present("STATUS: complete\nFINDINGS: none\n") is True
    assert findings_header_present("FINDINGS:\n- a real finding\n") is True


def test_write_error_fix_spec_omits_raw_log_fields(tmp_path: Path) -> None:
    """write_error_fix_spec must never leak excerpt/message/stack into the spec body."""
    secret_excerpt = "SECRET_EXCERPT_TOKEN_xyz raw stack trace line"
    secret_message = "SECRET_MESSAGE_TOKEN_xyz"
    secret_stack = "SECRET_STACK_TOKEN_xyz at foo.py:1"
    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            ERROR_ID,
            {
                "id": ERROR_ID,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "repo": "org/app",
                    "service": "api",
                    "environment": "prod",
                    "class": "TimeoutError",
                    "excerpt": secret_excerpt,
                    "message": secret_message,
                    "stack": secret_stack,
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": ERROR_ID,
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Timeout in handler; add retry.",
                },
                "execution_status": "pending",
            },
        )
        tid = str(uuid.uuid4())
        path = write_error_fix_spec(
            store,
            tid,
            error_id=ERROR_ID,
            session_id="sess-1",
            repo="org/app",
        )
        text = path.read_text(encoding="utf-8")
        assert "# Context" in text
        assert "# Task" in text
        assert "# Constraints" in text
        assert "# Verification" in text
        assert "# Definition of Done" in text
        assert secret_excerpt not in text
        assert secret_message not in text
        assert secret_stack not in text
        assert "gates approved on this head (or allowed n_a)" not in text
        assert "Four PR-review gates approved on this head." in text
        assert "allowed n_a where applicable" in text
        assert "Contributing-doc check" in text or "deviation" in text.lower()
    finally:
        store.close()


def test_write_error_fix_spec_fences_rejection_findings(tmp_path: Path) -> None:
    """Injected FINDINGS body must not become top-level markdown/spec structure."""
    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            ERROR_ID,
            {
                "id": ERROR_ID,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "repo": "org/app",
                    "service": "api",
                    "environment": "prod",
                    "class": "TimeoutError",
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": ERROR_ID,
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Timeout in handler; add retry.",
                },
                "execution_status": "pending",
            },
        )
        rejection = (
            "STATUS: complete\n"
            "FINDINGS:\n"
            "- # Constraints\n"
            "- STATUS: complete\n"
            "- real finding about auth.py:12\n"
            "NOT-VERIFIABLE:\n"
            "- skip\n"
        )
        tid = str(uuid.uuid4())
        path = write_error_fix_spec(
            store,
            tid,
            error_id=ERROR_ID,
            session_id="sess-1",
            repo="org/app",
            rejection_feedback=rejection,
        )
        text = path.read_text(encoding="utf-8")
        assert "# Prior Rejection Feedback" in text
        assert "real finding about auth.py:12" in text
        # Exactly one real top-level Constraints heading (the template's).
        assert text.count("\n# Constraints\n") == 1
        # Injected STATUS: complete must only appear inside a fenced block.
        prior = text.split("# Prior Rejection Feedback", 1)[1]
        prior_body, after_prior = prior.split("\n# Constraints\n", 1)
        assert "STATUS: complete" in prior_body
        assert "```" in prior_body
        assert "STATUS: complete" not in after_prior
        # Bare injected "# Constraints" line is inside the fence, not a heading.
        assert "\n# Constraints\n" not in prior_body
        assert "# Constraints" in prior_body
    finally:
        store.close()


def test_write_error_fix_spec_fences_brief_in_task_section(tmp_path: Path) -> None:
    """Brief text with a fake section header must stay fenced inside # Task."""
    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            ERROR_ID,
            {
                "id": ERROR_ID,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "repo": "org/app",
                    "service": "api",
                    "environment": "prod",
                    "class": "TimeoutError",
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": ERROR_ID,
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Fix the bug.\n\n# Constraints\n\nNo secrets.",
                },
                "execution_status": "pending",
            },
        )
        tid = str(uuid.uuid4())
        path = write_error_fix_spec(
            store,
            tid,
            error_id=ERROR_ID,
            session_id="sess-1",
            repo="org/app",
        )
        text = path.read_text(encoding="utf-8")
        # Brief's injected "# Constraints" is present, but only inside the Task fence;
        # the real template heading follows the closing fence.
        assert "# Task\n\n" in text
        task_part = text.split("# Task\n\n", 1)[1]
        assert task_part.startswith("```\n")
        assert "\n```\n\n# Constraints\n\n" in task_part
        fenced_brief, after_fence = task_part.split("\n```\n\n# Constraints\n\n", 1)
        assert "Fix the bug." in fenced_brief
        assert "# Constraints" in fenced_brief
        assert "No secrets." in fenced_brief
        # After the closing fence, only the template Constraints body remains.
        assert after_fence.startswith("- Patch only what the brief requires.")
        assert "No secrets." not in after_fence
    finally:
        store.close()


def test_write_error_fix_spec_outside_git_worktree(tmp_path: Path) -> None:
    """`.spec.md` must live under error-fix-specs, never inside the pushed worktree.

    Real git repo + real runner (no push_branch mock) so the pre-push dirty
    check would trip if the control file leaked into the clone.
    """
    worktree = tmp_path / "error-fix-work" / "tid-real"
    bare = tmp_path / "remote.git"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    (worktree / "README").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    # push_branch refuses protected names (main/master/develop); use a feature branch.
    subprocess.run(
        ["git", "checkout", "-B", "feat-spec-leak"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    def real_runner(argv: list[str]) -> Completed:
        # Destination check only: report a matching github URL while the
        # actual push still targets the local bare remote.
        if (
            len(argv) >= 6
            and argv[0] == "git"
            and argv[3:6] == ["remote", "get-url", "--push"]
        ):
            return Completed(0, "git@github.com:org/app.git\n", "")
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")

    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            ERROR_ID,
            {
                "id": ERROR_ID,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "repo": "org/app",
                    "service": "api",
                    "environment": "prod",
                    "class": "TimeoutError",
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": ERROR_ID,
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Timeout in handler; add retry.",
                },
                "execution_status": "pending",
            },
        )
        tid = "tid-real"
        path = write_error_fix_spec(
            store,
            tid,
            error_id=ERROR_ID,
            session_id="sess-1",
            repo="org/app",
        )
        assert path == tmp_path / "error-fix-specs" / tid / ".spec.md"
        assert path.is_file()
        # Spec must not be a descendant of the git worktree.
        assert worktree.resolve() not in path.resolve().parents
        assert not str(path.resolve()).startswith(str(worktree.resolve()) + os.sep)

        status = real_runner(
            [
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ]
        )
        assert status.returncode == 0
        assert status.stdout.strip() == ""
        assert ".spec.md" not in status.stdout

        # Real push_branch against the bare remote must succeed (dirty-check clean).
        sha = push_branch(
            cwd=str(worktree),
            runner=real_runner,
            expected_branch="feat-spec-leak",
            expected_repo="org/app",
        )
        assert sha
    finally:
        store.close()


def test_insert_pr_open_and_scan_writes_pending_and_calls_scan_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """insert_pr_open_and_scan inserts pending pr.open then returns scan_github."""
    calls: list[tuple[object, object]] = []

    def fake_scan_github(store: Store, runner: object) -> list[str]:
        calls.append((store, runner))
        return ["scanned"]

    monkeypatch.setattr("agent_cli.github_act.scan_github", fake_scan_github)

    def fake_runner(_argv: list[str]) -> Completed:
        raise AssertionError("runner must not be invoked directly")

    payload = {
        "repo": "org/app",
        "title": "t",
        "body": "b",
        "head": "h",
        "base": "main",
    }
    session_id = "sess-insert-pr-open"
    store = _store(tmp_path)
    try:
        result = insert_pr_open_and_scan(
            store,
            session_id=session_id,
            payload=payload,
            runner=fake_runner,
        )
        assert result == ["scanned"]
        assert len(calls) == 1
        assert calls[0][0] is store
        assert calls[0][1] is fake_runner
        rows = [r for r in store.rows("activity") if r.get("type") == "pr.open"]
        assert len(rows) == 1
        assert rows[0].get("execution_status") == "pending"
        assert rows[0].get("payload") == payload
        assert rows[0].get("session_id") == session_id
    finally:
        store.close()


def test_pushed_passes_expected_branch_from_error_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """error-fix tasks derive expected_branch=error-fix-<id8> for push_branch."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    captured: dict[str, object] = {}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        captured["expected_branch"] = expected_branch
        captured["expected_repo"] = expected_repo
        return "abcdef1234567890abcdef1234567890abcdef12"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert captured.get("expected_branch") == f"error-fix-{ERROR_ID[:8]}"
    assert captured.get("expected_repo") == "org/app"
    assert _checklist(tmp_path, tid)["pushed"] == "ja"


def test_pushed_fails_loudly_on_unresolvable_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """error-fix tasks must fail closed when repo cannot be resolved
    for the push-destination check — expected_repo=None would silently skip
    it while still pushing under the expected_branch-only identity check.

    Resolution is payload-first (matching _drive_one), so both fields must
    be unresolvable for the guard to fire.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["repo"] = "not-a-repo"
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        task["payload"] = {**payload, "repo": "also-not-a-repo"}
        store.write("task", "update", tid, task)
    finally:
        store.close()

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "push_branch must not run when task.repo is unresolvable"
        )

    monkeypatch.setattr("agent_cli.git_act.push_branch", boom)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid])
    assert (
        "task repo could not be resolved for the push-destination check"
        in str(exc.value.code)
    )
    assert _checklist(tmp_path, tid)["pushed"] != "ja"


def test_pushed_expected_repo_prefers_payload_over_task_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When task.repo and payload.repo both resolve but differ, payload wins
    (same precedence as _drive_one, which creates the PR)."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["repo"] = "org/task-repo"
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        task["payload"] = {**payload, "repo": "org/payload-repo"}
        store.write("task", "update", tid, task)
    finally:
        store.close()

    captured: dict[str, object] = {}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        captured["expected_repo"] = expected_repo
        return "abcdef1234567890abcdef1234567890abcdef12"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert captured.get("expected_repo") == "org/payload-repo"
    assert _checklist(tmp_path, tid)["pushed"] == "ja"


def test_drive_one_fails_loudly_on_stale_whitespace_only_error_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 26 regression: _drive_one used to derive error_id via
    str(x or "") and would build a garbage "error-fix- " branch head from a
    stale whitespace-only payload.error_id (simulated by writing the task
    row directly, bypassing create-time validation). Must fail loudly
    instead of building the garbage head or opening a PR."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: "abcdef1234567890abcdef1234567890abcdef12",
    )
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["pushed"] == "ja"

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["payload"] = {"error_id": " ", "repo": "org/app"}
        store.write("task", "update", tid, task)
    finally:
        store.close()

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "insert_pr_open_and_scan must not run for whitespace-only error_id"
        )

    monkeypatch.setattr("agent_cli.fixer_act.insert_pr_open_and_scan", boom)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        assert "whitespace-only" in result
        assert "failed" in result
        assert not _pr_open_row_exists(store, head="error-fix- ", repo="org/app")
    finally:
        store.close()


def test_drive_one_skips_github_when_session_inactive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed session + pushed=ja must not call scan_github / insert_pr_open_and_scan.

    Round 51: _drive_one gated only on is_error_fix_originated and never checked
    session_active, so a task whose session was closed after pushed=ja could still
    open/scan GitHub PRs — bypassing the session-active invariant every other
    write path enforces via _require_task_session_active.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: (
            "abcdef1234567890abcdef1234567890abcdef12"
        ),
    )
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["pushed"] == "ja"

    store = _store(tmp_path)
    try:
        session = store.row("session", "sess-1")
        assert session is not None
        session["status"] = "closed"
        store.write(
            "session",
            "update",
            "sess-1",
            {k: v for k, v in session.items() if not str(k).startswith("_")},
        )
    finally:
        store.close()

    github_calls: list[str] = []

    def boom_insert(*args, **kwargs):  # type: ignore[no-untyped-def]
        github_calls.append("insert_pr_open_and_scan")
        raise AssertionError("insert_pr_open_and_scan must not run for inactive session")

    def boom_scan(*args, **kwargs):  # type: ignore[no-untyped-def]
        github_calls.append("scan_github")
        raise AssertionError("scan_github must not run for inactive session")

    monkeypatch.setattr("agent_cli.fixer_act.insert_pr_open_and_scan", boom_insert)
    monkeypatch.setattr("agent_cli.github_act.scan_github", boom_scan)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert "skip" in result
    assert "session inactive" in result
    assert github_calls == []
    store = _store(tmp_path)
    try:
        assert not _pr_open_row_exists(
            store, head=f"error-fix-{ERROR_ID[:8]}", repo="org/app"
        )
    finally:
        store.close()


def test_fixer_threads_pushed_head_into_pr_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After pushed, the fixer must record PR gates with a real non-empty head_sha."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        return pushed_sha

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "pr-reviewer-quality")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    gates = _gates(tmp_path, tid)
    assert gates, "expected at least one PR-review gate after pushed"
    for g in gates:
        assert g.get("head_sha"), f"gate missing head_sha: {g}"
        assert str(g["head_sha"]).lower() == pushed_sha
    assert _checklist(tmp_path, tid)["pushed"] == "ja"


def test_fixer_strips_origin_prefix_from_pr_open_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh pr.open payload base must be bare; task.ref may stay origin/-prefixed."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        return pushed_sha

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "pr-reviewer-quality")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["ref"] = "origin/main"
        from agent_cli import main as main_mod

        store.write("task", "update", tid, main_mod._strip(task))

        task = store.row("task", tid)
        assert task is not None
        assert task.get("ref") == "origin/main"
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )

        pr_opens = [
            row
            for row in store.rows("activity")
            if isinstance(row, dict) and row.get("type") == "pr.open"
        ]
        assert pr_opens, "expected a pr.open activity row"
        payload = pr_opens[0].get("payload")
        assert isinstance(payload, dict)
        assert payload.get("base") == "main"
        updated = store.row("task", tid)
        assert updated is not None
        assert updated.get("ref") == "origin/main"
    finally:
        store.close()


def test_fixer_defers_when_worktree_not_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task row without a materialized worktree .git must defer without running steps."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    worktree = tmp_path / "error-fix-work" / tid
    git_dir = worktree / ".git"
    assert git_dir.is_dir()
    shutil.rmtree(git_dir)
    assert not git_dir.exists()

    before_state = _task_state(tmp_path, tid)
    called = {"n": 0}

    def spy_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", spy_rtc)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
        )
    finally:
        store.close()

    assert "worktree-not-ready" in result
    assert called["n"] == 0
    assert _task_state(tmp_path, tid) == before_state


def test_fixer_local_check_exec_uses_worktree_cwd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_check_pass via the fixer must invoke exec with the task worktree as cwd."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["local_check_pass"] != "ja"

    worktree = tmp_path / "error-fix-work" / tid
    assert worktree.is_dir()
    captured: dict[str, object] = {}

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        captured["cwd"] = cwd
        captured["argv"] = list(argv)
        return Completed(0, "ok\n", "")

    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    # Stop after local_check: pushed fails so the driver does not continue.
    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda **kwargs: (_ for _ in ()).throw(
            GitActError("stop-after-local-check")
        ),
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
        )
    finally:
        store.close()

    assert _checklist(tmp_path, tid)["local_check_pass"] == "ja"
    assert any(c.get("result") == "pass" for c in _local_checks(tmp_path, tid))
    assert captured.get("cwd") == str(worktree)
    assert "failed" in result
    assert _checklist(tmp_path, tid).get("pushed") != "ja"


def test_fixer_vendor_unavailable_leaves_task_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LaneResult(status=unavailable) on both attempts must not fail the task."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    before_state = _task_state(tmp_path, tid)
    before_checklist = _checklist(tmp_path, tid)
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        role = str(kwargs.get("role") or "implementer")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="unavailable",
            argv=[vendor],
            returncode=127,
            stdout="",
            stderr="command not found",
        )

    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert calls["n"] == 2
    assert "vendor-cli-unavailable" in result
    assert _task_state(tmp_path, tid) != "failed"
    assert _task_state(tmp_path, tid) == before_state
    assert _checklist(tmp_path, tid).get("implementer_done") == before_checklist.get(
        "implementer_done"
    )
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_fixer_retries_pr_open_across_scans_after_insert_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed pr.open insert must retry on the next scan once pushed is already ja."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"
    insert_calls = {"n": 0}
    head = f"error-fix-{ERROR_ID[:8]}"

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        return pushed_sha

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "pr-reviewer-quality")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def flaky_insert(store, *, session_id, payload, runner):  # type: ignore[no-untyped-def]
        insert_calls["n"] += 1
        if insert_calls["n"] == 1:
            raise OSError("github temporarily unavailable")
        # Mirror real insert_pr_open_and_scan enough for the ledger-derived retry check.
        # Real insert_pr_open_and_scan leaves execution_status=done after scan_github
        # succeeds; only done counts as present under the stricter exists check.
        activity_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": session_id,
                "type": "pr.open",
                "payload": payload,
                "execution_status": "done",
            },
        )
        return []

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act.insert_pr_open_and_scan", flaky_insert)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        first = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        assert "pr.open-error" in first
        assert _checklist(tmp_path, tid)["pushed"] == "ja"
        origin = store.device_id()
        pr_rows = [
            r
            for r in store.rows("activity")
            if r.get("_origin_device_id") == origin
            and r.get("type") == "pr.open"
            and isinstance(r.get("payload"), dict)
            and r["payload"].get("head") == head
        ]
        assert pr_rows == []

        task = store.row("task", tid)
        assert task is not None
        second = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        assert "pr.open-error" not in second
        pr_rows_after = [
            r
            for r in store.rows("activity")
            if r.get("_origin_device_id") == origin
            and r.get("type") == "pr.open"
            and isinstance(r.get("payload"), dict)
            and r["payload"].get("head") == head
        ]
        assert pr_rows_after, "second scan must create the pr.open row"
    finally:
        store.close()

    assert insert_calls["n"] == 2


def test_fixer_stops_on_persistent_gh_pr_create_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh pr create failure must not advance the spine or reach done/failed."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"
    create_calls = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        return pushed_sha

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "pr-reviewer-quality")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def failing_gh(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(
                1, "", 'no pull requests found for branch "error-fix-aaaaaaaa"'
            )
        if argv[:3] == ["gh", "pr", "create"]:
            create_calls["n"] += 1
            return Completed(1, "", "gh: persistent auth failure")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        first = _drive_one(
            store,
            task,
            runner=failing_gh,
            round_cap=5,
            lane_runner=None,
        )
        assert "pr.open-error" in first
        assert "done" not in first.split()[-1]
        assert _task_state(tmp_path, tid) not in ("done", "failed")
        cl = _checklist(tmp_path, tid)
        assert cl.get("contributing_ok") != "ja"
        assert cl.get("grok_pr_quality") != "ja"
        assert create_calls["n"] >= 1
        first_creates = create_calls["n"]

        task = store.row("task", tid)
        assert task is not None
        second = _drive_one(
            store,
            task,
            runner=failing_gh,
            round_cap=5,
            lane_runner=None,
        )
        assert "pr.open-error" in second
        assert _task_state(tmp_path, tid) not in ("done", "failed")
        assert create_calls["n"] > first_creates
        cl2 = _checklist(tmp_path, tid)
        assert cl2.get("contributing_ok") != "ja"
    finally:
        store.close()


def test_template_pr_open_payload_title_and_body() -> None:
    """template_pr_open_payload follows CONTRIBUTING.md PR title/body conventions."""
    session_id = "sess-12345678"
    error_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    payload = template_pr_open_payload(
        session_id=session_id,
        repo="org/app",
        error_id=error_id,
        brief="brief text",
        fingerprint="fp-1",
        title_suffix="Fix the thing",
    )
    assert payload["title"] == f"{session_id[:8]} - Fix the thing"
    assert payload["head"] == f"error-fix-{error_id[:8]}"
    body = str(payload["body"])
    assert "EN:\n" in body
    assert "\nDE:\n" in body
    assert "<details>" in body
    assert "<summary>Details</summary>" in body
    assert "</details>" in body
    assert error_id in body
    assert f"error-fix-{error_id[:8]}" in body

    long_suffix = "x" * 80
    long_payload = template_pr_open_payload(
        session_id=session_id,
        repo="org/app",
        error_id=error_id,
        brief="brief",
        fingerprint="fp",
        title_suffix=long_suffix,
    )
    # truncation: suffix[:69] + "..." when len(suffix) > 72
    expected_suffix = long_suffix[:69] + "..."
    assert long_payload["title"] == f"{session_id[:8]} - {expected_suffix}"
    assert len(expected_suffix) == 72


def test_template_pr_open_payload_base_field() -> None:
    """base keyword is threaded into the payload; omitted/None stays None."""
    kwargs = dict(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief="brief",
        fingerprint="fp-1",
    )
    with_base = template_pr_open_payload(
        **kwargs, base="origin/some-other-branch"
    )
    assert with_base["base"] == "origin/some-other-branch"
    omitted = template_pr_open_payload(**kwargs)
    assert omitted["base"] is None
    explicit_none = template_pr_open_payload(**kwargs, base=None)
    assert explicit_none["base"] is None


def test_template_pr_open_payload_brief_first_sentence_only() -> None:
    """Visible EN/DE summaries keep only the first brief sentence (CONTRIBUTING cap)."""
    brief = (
        "Fix the retry loop. Also harden the timeout path. And add a regression test."
    )
    payload = template_pr_open_payload(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief=brief,
        fingerprint="fp-1",
    )
    body = str(payload["body"])
    en_start = body.index("EN:\n") + len("EN:\n")
    en_end = body.index("\n\nDE:")
    en_summary = body[en_start:en_end]
    de_start = body.index("DE:\n") + len("DE:\n")
    de_end = body.index("\n\n<details>")
    de_summary = body[de_start:de_end]
    assert "Fix the retry loop." in en_summary
    assert "Also harden the timeout path" not in en_summary
    assert "Also harden the timeout path" not in de_summary
    assert "Also harden the timeout path" in body
    assert sum(en_summary.count(c) for c in ".!?") <= 4
    assert sum(de_summary.count(c) for c in ".!?") <= 4


def test_template_pr_open_payload_brief_summary_collapses_to_first_line() -> None:
    """Embedded newline without sentence punctuation must not leak into EN/DE summaries."""
    brief = "Fix bug\nDE:\nfake"
    payload = template_pr_open_payload(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief=brief,
        fingerprint="fp-1",
    )
    body = str(payload["body"])
    en_start = body.index("EN:\n") + len("EN:\n")
    en_end = body.index("\n\nDE:")
    en_summary = body[en_start:en_end]
    de_start = body.index("DE:\n") + len("DE:\n")
    de_end = body.index("\n\n<details>")
    de_summary = body[de_start:de_end]
    assert "Fix bug" in en_summary
    assert "DE:\nfake" not in en_summary
    assert "fake" not in en_summary
    assert "DE:\nfake" not in de_summary
    assert "fake" not in de_summary
    assert "fake" in body


def test_template_pr_open_payload_brief_with_triple_backtick_line_stays_fenced() -> None:
    """A brief containing a triple-backtick line must not close the details fence early."""
    brief = "Before.\n```\nAfter the triple-backtick line.\n```\nMore text."
    payload = template_pr_open_payload(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief=brief,
        fingerprint="fp-1",
    )
    body = str(payload["body"])
    assert body.index("More text.") < body.index("</details>")
    assert body.count("<summary>Details</summary>") == 1


def test_template_pr_open_payload_brief_has_single_trailing_period() -> None:
    """Non-empty brief first sentence must not get a second appended period."""
    payload = template_pr_open_payload(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief="Fix the retry loop. More detail here.",
        fingerprint="fp-1",
    )
    body = str(payload["body"])
    assert "Brief: Fix the retry loop." in body
    assert "Brief: Fix the retry loop.." not in body


def test_template_pr_open_payload_empty_brief_fallback_has_one_period() -> None:
    """Empty brief uses the fallback literal with exactly one trailing period."""
    payload_en = template_pr_open_payload(
        session_id="sess-12345678",
        repo="org/app",
        error_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        brief="",
        fingerprint="fp-1",
    )
    body = str(payload_en["body"])
    assert "Brief: see task spec." in body
    assert "Brief: siehe Task-Spec." in body
    assert "Brief: see task spec.." not in body
    assert "Brief: siehe Task-Spec.." not in body


def test_first_sentence_skips_common_abbreviations() -> None:
    """Period after e.g./Dr./etc. must not truncate the first sentence."""
    brief = "e.g. this is broken and needs fixing. Second sentence here."
    assert _first_sentence(brief) == "e.g. this is broken and needs fixing."
    assert _first_sentence(brief) != "e.g."
    assert (
        _first_sentence("Dr. Smith found a bug. More detail follows.")
        == "Dr. Smith found a bug."
    )


def test_contributing_ok_evidence_rejects_stale_head() -> None:
    """Approved gate on a different head_sha must not count as present."""
    current = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stale = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    snap = {
        "head_sha": current,
        "gates": [
            {
                "vendor": "grok",
                "dimension": "quality",
                "verdict": "approved",
                "head_sha": stale,
            },
            {
                "vendor": "grok",
                "dimension": "logic",
                "verdict": "approved",
                "head_sha": current,
            },
            {
                "vendor": "codex",
                "dimension": "quality",
                "verdict": "approved",
                "head_sha": current,
            },
            {
                "vendor": "codex",
                "dimension": "logic",
                "verdict": "approved",
                "head_sha": current,
            },
        ],
    }
    with pytest.raises(StoreError, match="grok/quality"):
        _contributing_ok_evidence(snap)


def test_open_error_fix_tasks_skips_whitespace_only_error_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whitespace-only payload.error_id must not be returned by _open_error_fix_tasks."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    store = _store(tmp_path)
    try:
        before = _open_error_fix_tasks(store)
        assert any(str(t.get("id")) == tid for t in before)
        task = store.row("task", tid)
        assert task is not None
        task["payload"] = {"error_id": "   ", "repo": "org/app"}
        store.write("task", "update", tid, task)
        after = _open_error_fix_tasks(store)
        assert all(str(t.get("id")) != tid for t in after)
    finally:
        store.close()


def test_runner_to_completed_honors_cwd(tmp_path: Path) -> None:
    completed = _runner_to_completed(
        lambda _argv: Completed(1, "", "runner-should-not-run"),
        ["pwd"],
        cwd=str(tmp_path),
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == str(tmp_path)


def test_runner_to_completed_without_cwd_uses_runner() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        seen.append(list(argv))
        return Completed(0, "from-runner", "")

    completed = _runner_to_completed(runner, ["echo", "hi"], cwd=None)
    assert completed.stdout == "from-runner"
    assert seen == [["echo", "hi"]]


def test_runner_to_completed_timeout_returns_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.TimeoutExpired in the cwd branch becomes Completed(124)."""

    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=["sleep", "999"], timeout=120)

    monkeypatch.setattr(subprocess, "run", boom)
    completed = _runner_to_completed(
        lambda _argv: Completed(1, "", "runner-should-not-run"),
        ["sleep", "999"],
        cwd=str(tmp_path),
    )
    assert completed.returncode == 124
    assert completed.stderr


def _fake_insert_pr_open_and_scan(store, *, session_id, payload, runner):  # type: ignore[no-untyped-def]
    """Simulate a successful insert_pr_open_and_scan: writes a real pr.open row
    so _pr_open_row_exists finds it (matches flaky_insert's success branch).

    Real insert_pr_open_and_scan leaves execution_status=done after scan_github
    succeeds; only done counts as present under the stricter exists check.
    Includes result.number so the fixer can persist payload.pr_number.
    """
    activity_id = str(uuid.uuid4())
    repo = payload.get("repo") if isinstance(payload, dict) else None
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "pr.open",
            "payload": payload,
            "execution_status": "done",
            "result": {
                "repo": repo,
                "number": 42,
                "url": f"https://github.com/{repo}/pull/42",
                "draft": True,
            },
        },
    )
    return []


def _pass_lane(**kwargs):  # type: ignore[no-untyped-def]
    role = str(kwargs.get("role") or "pr-reviewer-quality")
    vendor = str(kwargs.get("vendor") or "grok")
    return LaneResult(
        role=role,
        vendor=vendor,
        status="complete",
        argv=[vendor],
        returncode=0,
        stdout="STATUS: complete\nFINDINGS: none\n",
        stderr="",
    )


def test_fixer_drives_error_fix_task_to_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: error-fix task reaches done with deviation_* closed n_a."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert result.endswith("done") or " done" in result
    assert _task_state(tmp_path, tid) == "done"
    cl = _checklist(tmp_path, tid)
    assert cl["contributing_ok"] == "ja"
    assert cl["deviation_declared"] == "n_a"
    assert cl["deviation_granted"] == "n_a"


def test_ensure_done_readiness_summary_fallback_uses_distinct_german(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback change_summary_de must be German and differ from EN / raw brief."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        assert result.endswith("done") or " done" in result
        done = store.row("task", tid)
        assert done is not None
        en = (done.get("change_summary_en") or "").strip()
        de = (done.get("change_summary_de") or "").strip()
        brief = "Timeout in handler; add retry."
        assert en
        assert de
        assert de != en
        assert de != brief
        brief_marker = "Brief: "
        assert brief_marker in de
        german_sentence, _, rest = de.partition(brief_marker)
        german_sentence = german_sentence.strip()
        assert german_sentence.endswith(".")
        assert "Automatischer" in german_sentence
        assert rest == brief

    finally:
        store.close()


def test_drive_one_reports_contributing_ok_blocked_instead_of_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """StoreError from _contributing_ok_evidence must become contributing_ok-blocked."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    def boom_evidence(snap):  # type: ignore[no-untyped-def]
        raise StoreError("boom - stale gate")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )
    monkeypatch.setattr("agent_cli.fixer_act._contributing_ok_evidence", boom_evidence)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert "contributing_ok-blocked" in result
    assert _task_state(tmp_path, tid) != "done"


def test_fixer_pr_gate_rejection_clears_head_for_new_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-gate rejection drops stale head so a genuinely new push sha is accepted."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    shas = [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    push_calls = {"n": 0}
    rejects = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        i = push_calls["n"]
        push_calls["n"] += 1
        return shas[min(i, len(shas) - 1)]

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if (
            role == "pr-reviewer-quality"
            and vendor == "grok"
            and rejects["n"] == 0
        ):
            rejects["n"] += 1
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS:\n- fix the retry loop\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, shas[min(push_calls["n"], len(shas) - 1)] + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert "does not match pushed sha" not in result
    assert _checklist(tmp_path, tid)["pushed"] == "ja"
    assert _task_state(tmp_path, tid) == "done"
    gates = _gates(tmp_path, tid)
    approved_gq = [
        g
        for g in gates
        if g.get("vendor") == "grok"
        and g.get("dimension") == "quality"
        and g.get("verdict") == "approved"
    ]
    assert approved_gq, "expected a final approved grok/quality gate"
    approved_gq.sort(key=lambda g: str(g.get("recorded_at") or ""))
    assert str(approved_gq[-1].get("head_sha") or "").lower() == shas[1]


def test_fixer_backfills_pr_number_when_pr_open_already_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash window: done pr.open exists but payload.pr_number unset — backfill on next scan."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pr_head = f"error-fix-{ERROR_ID[:8]}"
    activity_id = str(uuid.uuid4())

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task_payload = (
            task.get("payload") if isinstance(task.get("payload"), dict) else {}
        )
        assert "pr_number" not in task_payload

        # Simulate earlier scan that created a done pr.open then crashed before
        # backfilling payload.pr_number — row already exists coming into this scan.
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "sess-1",
                "type": "pr.open",
                "payload": {
                    "repo": "org/app",
                    "title": "sess-1 - Fix timeout",
                    "head": pr_head,
                    "body": "EN:\nDraft\n",
                },
                "execution_status": "done",
                "result": {
                    "repo": "org/app",
                    "number": 42,
                    "url": "https://github.com/org/app/pull/42",
                    "draft": True,
                },
            },
        )
        assert _pr_open_row_exists(store, head=pr_head, repo="org/app")

        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        updated = store.row("task", tid)
        assert updated is not None
        payload = updated.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("pr_number") == 42
    finally:
        store.close()


def test_fixer_backfills_task_ref_from_pr_open_real_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When done pr.open result.base differs from task.ref, next scan heals task.ref."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pr_head = f"error-fix-{ERROR_ID[:8]}"
    activity_id = str(uuid.uuid4())

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        # Stale local resolution — GitHub's real base will disagree.
        task["ref"] = "origin/develop"
        from agent_cli import main as main_mod

        store.write("task", "update", tid, main_mod._strip(task))

        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "sess-1",
                "type": "pr.open",
                "payload": {
                    "repo": "org/app",
                    "title": "sess-1 - Fix timeout",
                    "head": pr_head,
                    "body": "EN:\nDraft\n",
                    "base": "origin/develop",
                },
                "execution_status": "done",
                "result": {
                    "repo": "org/app",
                    "number": 42,
                    "url": "https://github.com/org/app/pull/42",
                    "draft": True,
                    "base": "main",
                },
            },
        )
        assert _pr_open_row_exists(store, head=pr_head, repo="org/app")

        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        updated = store.row("task", tid)
        assert updated is not None
        assert updated.get("ref") == "origin/main"

        from agent_cli.run_core import _collect_review_diff

        def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
            if argv[:3] == ["git", "rev-parse", "--verify"]:
                if argv[3] == "origin/main":
                    return Completed(0, "abc123\n", "")
                return Completed(1, "", "")
            if argv[:2] == ["git", "merge-base"]:
                return Completed(0, "deadbeef\n", "")
            if argv[:2] == ["git", "diff"]:
                if "--name-only" in argv:
                    return Completed(0, "src/foo.py\n", "")
                return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
            return Completed(0, "", "")

        _diff, _paths, probes_ok = _collect_review_diff(
            str(tmp_path), fake_exec, base_ref=updated.get("ref")
        )
        assert probes_ok is True
    finally:
        store.close()


def test_fixer_persists_pr_number_and_queues_gate_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After pr.open, payload.pr_number is set so PR-gate rejection queues review.post."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    shas = [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    push_calls = {"n": 0}
    rejects = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        i = push_calls["n"]
        push_calls["n"] += 1
        return shas[min(i, len(shas) - 1)]

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == "pr-reviewer-quality" and vendor == "grok" and rejects["n"] == 0:
            rejects["n"] += 1
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS:\n- fix the retry loop\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, shas[min(push_calls["n"], len(shas) - 1)] + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        # error-fix tasks keep ref as a checkout ref, not a PR number.
        assert not (
            isinstance(task.get("ref"), str)
            and task["ref"].isdigit()
            and int(task["ref"]) > 0
        )
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        updated = store.row("task", tid)
        assert updated is not None
        payload = updated.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("pr_number") == 42
        review_posts = [
            r
            for r in store.rows("activity")
            if r.get("type") == "review.post"
        ]
        assert review_posts, "PR-gate rejection must queue a review.post via pr_number"
        assert any(
            isinstance(r.get("payload"), dict) and r["payload"].get("number") == 42
            for r in review_posts
        )
    finally:
        store.close()


def test_rejection_feedback_rewritten_into_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected PR-gate round must rewrite .spec.md with Prior Rejection Feedback."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    shas = [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    push_calls = {"n": 0}
    rejects = {"n": 0}
    findings_marker = "fix the retry loop specifically"

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        i = push_calls["n"]
        push_calls["n"] += 1
        return shas[min(i, len(shas) - 1)]

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if (
            role == "pr-reviewer-quality"
            and vendor == "grok"
            and rejects["n"] == 0
        ):
            rejects["n"] += 1
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, shas[min(push_calls["n"], len(shas) - 1)] + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
        encoding="utf-8"
    )
    assert "# Prior Rejection Feedback" in spec_text
    assert findings_marker in spec_text


def test_fixer_pr_gate_rejection_clears_head_before_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Head must be None on the first execute_spine_step call after a PR-gate
    rejection -- asserted directly on that call's head kwarg, not inferred from
    a later step succeeding (which could pass via local_check_pass's incidental
    git-rev-parse correction instead of the real fix)."""
    import agent_cli.fixer_act as fixer_mod

    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    shas = [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    push_calls = {"n": 0}
    rejects = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        i = push_calls["n"]
        push_calls["n"] += 1
        return shas[min(i, len(shas) - 1)]

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == "pr-reviewer-quality" and vendor == "grok" and rejects["n"] == 0:
            rejects["n"] += 1
            return LaneResult(
                role=role, vendor=vendor, status="complete", argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS:\n- fix the retry loop\n",
                stderr="",
            )
        return LaneResult(
            role=role, vendor=vendor, status="complete", argv=[vendor],
            returncode=0, stdout="STATUS: complete\nFINDINGS: none\n", stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, shas[min(push_calls["n"], len(shas) - 1)] + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    # Spy on complete_spine_agent_step (not execute_spine_step): the
    # parallel PR-dimension path calls it directly, bypassing
    # execute_spine_step entirely, so a spy at that higher level would
    # silently miss every outcome routed through the parallel pair.
    # complete_spine_agent_step is imported into both run_core's own
    # module globals (used by execute_spine_step's bare-name call) and
    # fixer_act's (used by _drive_parallel_pr_pair's bare-name call) --
    # each is a separate binding, so both must be patched.
    calls: list[tuple[str | None, str, str | None]] = []
    real_complete = fixer_mod.complete_spine_agent_step

    def spy_complete(store, tid, plan, result, **kwargs):  # type: ignore[no-untyped-def]
        outcome = real_complete(store, tid, plan, result, **kwargs)
        calls.append((plan.head, outcome.kind, outcome.key))
        return outcome

    monkeypatch.setattr("agent_cli.fixer_act.complete_spine_agent_step", spy_complete)
    monkeypatch.setattr("agent_cli.run_core.complete_spine_agent_step", spy_complete)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store, task, runner=lambda argv: Completed(0, "", ""),
            round_cap=5, lane_runner=None,
        )
    finally:
        store.close()

    reject_idx = next(
        i for i, (_, kind, key) in enumerate(calls)
        if kind == "rejected_new_round" and key != "reviewer_approved"
    )
    # Round 46: the sibling PR-gate dimension is always fully recorded too
    # (no more abandon/discard), so it shows up immediately after the
    # rejection in the SAME batch, with the SAME pre-attempt (stale) head
    # both dimensions were prepared with -- that's expected, not a bug.
    # Skip past any adjacent same-batch PR-gate entries to find the first
    # call that belongs to a genuinely new round.
    pr_gate_keys = {
        "grok_pr_quality", "grok_pr_logic", "codex_pr_quality", "codex_pr_logic",
    }
    next_idx = reject_idx + 1
    while next_idx < len(calls) and calls[next_idx][2] in pr_gate_keys:
        next_idx += 1
    assert next_idx < len(calls), (
        "expected a further execute_spine_step call after the PR-gate rejection"
    )
    next_head, _next_kind, _next_key = calls[next_idx]
    assert next_head is None, (
        "head must be None on the first execute_spine_step call after a "
        f"PR-gate rejection; got {next_head!r} (stale rehydration bug)"
    )


def test_fixer_inner_reviewer_rejection_keeps_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inner reviewer rejection must not clear the threaded pushed head."""
    import agent_cli.fixer_act as fixer_mod

    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)

    pushed_sha = "cccccccccccccccccccccccccccccccccccccccc"
    real_ensure = fixer_mod._ensure_done_readiness
    real_execute = fixer_mod.execute_spine_step

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )
    # Stop short of done so gates (and thus recoverable head_sha) remain.
    monkeypatch.setattr(
        "agent_cli.fixer_act._ensure_done_readiness", lambda *a, **k: None
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        first = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        assert "done-blocked" in first or _checklist(tmp_path, tid)["pushed"] == "ja"
        assert _checklist(tmp_path, tid)["pushed"] == "ja"

        for row in store.rows("checklist_item"):
            if row.get("task_id") == tid and row.get("key") in (
                "implementer_done",
                "reviewer_approved",
            ):
                row = dict(row)
                row["status"] = "pending"
                row["evidence"] = "reopen for inner-reviewer head test"
                store.write(
                    "checklist_item",
                    "update",
                    row["id"],
                    {k: v for k, v in row.items() if not str(k).startswith("_")},
                )
    finally:
        store.close()

    seen_heads: list[str | None] = []

    def spy(*a, **kw):  # type: ignore[no-untyped-def]
        seen_heads.append(kw.get("head"))
        return real_execute(*a, **kw)

    rejects = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == "reviewer" and rejects["n"] == 0:
            rejects["n"] += 1
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS:\n- fix the retry loop\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.fixer_act.execute_spine_step", spy)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._ensure_done_readiness", real_ensure)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert _checklist(tmp_path, tid)["pushed"] == "ja"
    assert seen_heads, "expected execute_spine_step calls"
    assert all(h == pushed_sha for h in seen_heads), seen_heads
    assert None not in seen_heads


def test_pr_open_row_exists_excludes_error_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing pr.open with execution_status=error must not count as present."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    head = f"error-fix-{ERROR_ID[:8]}"
    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        activity_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": task["session_id"],
                "type": "pr.open",
                "payload": {"head": head, "repo": "org/app", "title": "x", "body": "y"},
                "execution_status": "error",
            },
        )
        assert _pr_open_row_exists(store, head=head, repo="org/app") is False
    finally:
        store.close()


def test_pr_open_row_exists_excludes_pending_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing pr.open with execution_status=pending must not count as present."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    head = f"error-fix-{ERROR_ID[:8]}"
    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        activity_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": task["session_id"],
                "type": "pr.open",
                "payload": {"head": head, "repo": "org/app", "title": "x", "body": "y"},
                "execution_status": "pending",
            },
        )
        assert _pr_open_row_exists(store, head=head, repo="org/app") is False
    finally:
        store.close()


def test_pr_open_helpers_scope_by_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same head across different repos must not collide for exists/number."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    head = f"error-fix-{ERROR_ID[:8]}"
    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        session_id = task["session_id"]
        id_app = str(uuid.uuid4())
        id_other = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            id_app,
            {
                "id": id_app,
                "session_id": session_id,
                "type": "pr.open",
                "payload": {
                    "head": head,
                    "repo": "org/app",
                    "title": "x",
                    "body": "y",
                },
                "execution_status": "done",
                "result": {"number": 11},
            },
        )
        store.write(
            "activity",
            "insert",
            id_other,
            {
                "id": id_other,
                "session_id": session_id,
                "type": "pr.open",
                "payload": {
                    "head": head,
                    "repo": "org/other",
                    "title": "x",
                    "body": "y",
                },
                "execution_status": "done",
                "result": {"number": 22},
            },
        )
        assert _pr_open_row_exists(store, head=head, repo="org/app") is True
        assert _pr_open_row_exists(store, head=head, repo="org/other") is True
        assert _pr_open_row_exists(store, head=head, repo="org/third") is False
        assert _pr_open_number(store, head=head, repo="org/app") == 11
        assert _pr_open_number(store, head=head, repo="org/other") == 22
        assert _pr_open_number(store, head=head, repo="org/third") is None
    finally:
        store.close()


def test_pr_open_number_skips_malformed_newer_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer done row with a malformed result must not block an older valid number."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    head = f"error-fix-{ERROR_ID[:8]}"
    repo = "org/app"
    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        session_id = task["session_id"]
        older_id = str(uuid.uuid4())
        newer_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            older_id,
            {
                "id": older_id,
                "session_id": session_id,
                "type": "pr.open",
                "payload": {
                    "head": head,
                    "repo": repo,
                    "title": "x",
                    "body": "y",
                },
                "execution_status": "done",
                "result": {"number": 42},
            },
        )
        # utcnow() is second-precision; sleep so the malformed row sorts first.
        time.sleep(1.1)
        store.write(
            "activity",
            "insert",
            newer_id,
            {
                "id": newer_id,
                "session_id": session_id,
                "type": "pr.open",
                "payload": {
                    "head": head,
                    "repo": repo,
                    "title": "x",
                    "body": "y",
                },
                "execution_status": "done",
                "result": {"number": "not-a-number"},
            },
        )
        assert _pr_open_number(store, head=head, repo=repo) == 42
    finally:
        store.close()


def test_error_fix_brief_matches_whitespace_padded_persisted_error_id(
    tmp_path: Path,
) -> None:
    """A persisted error.fix payload.error_id with incidental whitespace
    (simulated by writing the activity row directly, bypassing
    validate_conclusion's normalization) must still match the caller's
    already-normalized error_id, same fix as has_error_fix_activity."""
    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "fix-1",
            {
                "id": "fix-1",
                "session_id": "runner-1",
                "type": "error.fix",
                "payload": {
                    "error_id": f"{ERROR_ID} ",
                    "fingerprint": "api|TimeoutError|abc|prod",
                    "brief": "Timeout in handler; add retry.",
                },
                "execution_status": "pending",
            },
        )
        assert _error_fix_brief(store, "runner-1", ERROR_ID) == (
            "Timeout in handler; add retry."
        )
    finally:
        store.close()


def test_fixer_resumes_pending_pr_open_via_scan_github(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-flight pending pr.open must resume via scan_github, not a duplicate insert."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"
    head = f"error-fix-{ERROR_ID[:8]}"
    activity_id = str(uuid.uuid4())
    scan_calls: list[tuple] = []
    insert_calls: list[tuple] = []

    def fake_push(*, cwd: str, runner, expected_branch=None, expected_repo=None):  # type: ignore[no-untyped-def]
        return pushed_sha

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "pr-reviewer-quality")
        vendor = str(kwargs.get("vendor") or "grok")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_scan_github(store, runner):  # type: ignore[no-untyped-def]
        scan_calls.append((store, runner))
        row = store.row("activity", activity_id)
        assert row is not None
        updated = {k: v for k, v in row.items() if not str(k).startswith("_")}
        updated["execution_status"] = "done"
        store.write("activity", "update", activity_id, updated)
        return []

    def fake_insert(store, *, session_id, payload, runner):  # type: ignore[no-untyped-def]
        insert_calls.append((store, session_id, payload, runner))
        return []

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.github_act.scan_github", fake_scan_github)
    monkeypatch.setattr("agent_cli.fixer_act.insert_pr_open_and_scan", fake_insert)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": task["session_id"],
                "type": "pr.open",
                "payload": {"head": head, "repo": "org/app", "title": "x", "body": "y"},
                "execution_status": "pending",
            },
        )
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert len(scan_calls) == 1
    assert insert_calls == []
    assert "pr.open-error" not in result


def test_drive_error_fix_tasks_isolates_per_task_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One task's SystemExit must not abort the scan for other open tasks."""
    tid1 = _bootstrap_error_fix_task(tmp_path, capsys)
    error_id_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    store = _store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            error_id_2,
            {
                "id": error_id_2,
                "session_id": "sess-1",
                "type": "error.seen",
                "payload": {
                    "fingerprint": "api|ValueError|def|prod",
                    "repo": "org/app",
                    "service": "api",
                    "class": "ValueError",
                },
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "fix-2",
            {
                "id": "fix-2",
                "session_id": "sess-1",
                "type": "error.fix",
                "payload": {
                    "error_id": error_id_2,
                    "fingerprint": "api|ValueError|def|prod",
                    "brief": "ValueError in handler; harden input.",
                },
                "execution_status": "pending",
            },
        )
    finally:
        store.close()
    run(
        tmp_path,
        [
            "task",
            "create",
            "--session",
            "sess-1",
            "--workflow",
            "implement",
            "--error-id",
            error_id_2,
            "--title",
            "Fix value error",
        ],
    )
    tid2 = _last_task_id(capsys.readouterr().out)
    first_tid, second_tid = sorted([tid1, tid2])

    def fake_drive_one(store, task, runner, *, round_cap, lane_runner=None):  # type: ignore[no-untyped-def]
        tid = str(task["id"])
        if tid == first_tid:
            raise SystemExit("round still has a working agent")
        return f"error-fix-work {tid} done"

    monkeypatch.setattr("agent_cli.fixer_act._drive_one", fake_drive_one)

    store = _store(tmp_path)
    try:
        lines = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert len(lines) == 2
    assert first_tid in lines[0]
    assert "scan-error" in lines[0]
    assert "SystemExit" in lines[0]
    assert lines[1] == f"error-fix-work {second_tid} done"


def test_empty_review_diff_fails_task_and_stops_reselection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """EmptyReviewDiffError must fail the task so the next scan does not re-select it."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        # Base ref resolves and merge-base returns a real sha (probes_ok stays
        # True) but every diff call comes back genuinely empty -- a confirmed
        # empty diff, not a probe failure.
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)

    store = _store(tmp_path)
    try:
        lines1 = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert _task_state(tmp_path, tid) == "failed"
    assert any(tid in line for line in lines1)
    agents_after_first = len(_agents(tmp_path, tid))

    store = _store(tmp_path)
    try:
        lines2 = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert all(tid not in line for line in lines2)
    assert len(_agents(tmp_path, tid)) == agents_after_first


def test_review_diff_probe_failure_leaves_task_retryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed git probes must not fail the task; next scan may re-select it."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    before_state = _task_state(tmp_path, tid)

    monkeypatch.setattr(
        "agent_cli.run_core._collect_review_diff",
        lambda *_a, **_k: ("", [], False),
    )

    def boom_launch(**_kwargs: object) -> object:
        raise AssertionError("launch must not be called")

    monkeypatch.setattr("agent_cli.run_core.launch", boom_launch)

    store = _store(tmp_path)
    try:
        lines1 = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert _task_state(tmp_path, tid) != "failed"
    assert _task_state(tmp_path, tid) == before_state
    assert any(tid in line for line in lines1)
    assert any("vendor-cli-unavailable" in line for line in lines1)
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))

    store = _store(tmp_path)
    try:
        lines2 = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert any(tid in line for line in lines2)
    assert _task_state(tmp_path, tid) != "failed"


def test_drive_error_fix_tasks_runs_pr_dimensions_concurrently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-vendor PR dimensions must overlap in wall-clock time under exclusive().

    Uses threading.Barrier(2): sequential launch would hang until timeout. Goes
    through drive_error_fix_tasks (not bare _drive_one) so a Store RLock
    deadlock under exclusive() would also hang this test.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"
    barriers = {
        "grok": threading.Barrier(2),
        "codex": threading.Barrier(2),
    }
    events: list[tuple[str, str, str, float]] = []
    lock = threading.Lock()

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        with lock:
            events.append(("enter", vendor, role, time.monotonic()))
        barriers[vendor].wait(timeout=5)
        with lock:
            events.append(("exit", vendor, role, time.monotonic()))
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, pushed_sha + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        lines = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert any(tid in line for line in lines)
    assert _task_state(tmp_path, tid) == "done"

    for vendor in ("grok", "codex"):
        vendor_events = [e for e in events if e[1] == vendor]
        enters = [e for e in vendor_events if e[0] == "enter"]
        exits = [e for e in vendor_events if e[0] == "exit"]
        assert len(enters) == 2, f"{vendor}: expected 2 parallel enters, got {enters}"
        assert len(exits) == 2, f"{vendor}: expected 2 exits, got {exits}"
        # Second enter before first exit → genuine overlap (Barrier already
        # enforced rendezvous; this asserts the recorded timestamps too).
        assert max(e[3] for e in enters) < min(e[3] for e in exits), (
            f"{vendor}: launches did not overlap: {vendor_events}"
        )


def _patch_pr_pair_order(
    monkeypatch: pytest.MonkeyPatch, *, reverse: bool
) -> None:
    """Optionally reverse ready pair order (quality↔logic) for order-independence."""
    if not reverse:
        return
    import agent_cli.fixer_act as fixer_mod

    real = fixer_mod._ready_pr_dimension_pair

    def reversed_pair(ready):  # type: ignore[no-untyped-def]
        pair = real(ready)
        return None if pair is None else list(reversed(pair))

    monkeypatch.setattr("agent_cli.fixer_act._ready_pr_dimension_pair", reversed_pair)


def _pr_pair_rtc(pushed_sha: str):  # type: ignore[no-untyped-def]
    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, pushed_sha + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    return fake_rtc


@pytest.mark.parametrize(
    "reverse_pair,reject_role,unavailable_role",
    [
        (False, "pr-reviewer-quality", "pr-reviewer-logic"),
        (True, "pr-reviewer-quality", "pr-reviewer-logic"),
        (False, "pr-reviewer-logic", "pr-reviewer-quality"),
        (True, "pr-reviewer-logic", "pr-reviewer-quality"),
    ],
    ids=[
        "quality-first-quality-rejects",
        "logic-first-quality-rejects",
        "quality-first-logic-rejects",
        "logic-first-logic-rejects",
    ],
)
def test_parallel_pr_pair_rejection_feedback_survives_sibling_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse_pair: bool,
    reject_role: str,
    unavailable_role: str,
) -> None:
    """Reject + vendor_unavailable must keep rejection findings in .spec.md
    regardless of pair order and which dimension rejected.

    cont=True from the rejection wins over the sibling's message-bearing
    unavailable outcome, so the driver continues rather than returning the
    unavailable message; findings are written once from the aggregated batch.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    _patch_pr_pair_order(monkeypatch, reverse=reverse_pair)

    pushed_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    findings_marker = "fix the retry loop specifically"
    # First PR-pair attempt only: reject once / unavailable once, then pass.
    reject_done = {"n": 0}
    unavailable_done = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if (
            role == reject_role
            and vendor == "grok"
            and reject_done["n"] == 0
        ):
            reject_done["n"] += 1
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_build(store, tid_, *, role, round_num, implement_spec_file, cwd, exec_argv):  # type: ignore[no-untyped-def]
        if role == unavailable_role and unavailable_done["n"] == 0:
            unavailable_done["n"] += 1
            raise ReviewDiffUnavailableError(
                f"simulated {unavailable_role} unavailable"
            )
        return build_review_spec_file(
            store,
            tid_,
            role=role,
            round_num=round_num,
            implement_spec_file=implement_spec_file,
            cwd=cwd,
            exec_argv=exec_argv,
        )

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.run_core.build_review_spec_file", fake_build)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert reject_done["n"] == 1
    assert unavailable_done["n"] == 1
    spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
        encoding="utf-8"
    )
    assert "# Prior Rejection Feedback" in spec_text
    assert findings_marker in spec_text
    reject_key = (
        "grok_pr_quality"
        if reject_role == "pr-reviewer-quality"
        else "grok_pr_logic"
    )
    assert f"## {reject_key}" in spec_text


@pytest.mark.parametrize(
    "reverse_pair",
    [False, True],
    ids=["quality-first", "logic-first"],
)
def test_parallel_pr_pair_both_reject_combines_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse_pair: bool,
) -> None:
    """Both dimensions rejecting must write both findings into one .spec.md."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    _patch_pr_pair_order(monkeypatch, reverse=reverse_pair)

    pushed_sha = "dddddddddddddddddddddddddddddddddddddddd"
    quality_marker = "QUALITY_FINDING_marker_alpha"
    logic_marker = "LOGIC_FINDING_marker_beta"
    pair_attempts = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if vendor == "grok" and role in (
            "pr-reviewer-quality",
            "pr-reviewer-logic",
        ):
            # Reject only on the first grok PR-pair wave; later waves pass.
            if pair_attempts["n"] < 2:
                pair_attempts["n"] += 1
                marker = (
                    quality_marker
                    if role == "pr-reviewer-quality"
                    else logic_marker
                )
                return LaneResult(
                    role=role,
                    vendor=vendor,
                    status="complete",
                    argv=[vendor],
                    returncode=0,
                    stdout=f"STATUS: complete\nFINDINGS:\n- {marker}\n",
                    stderr="",
                )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
        encoding="utf-8"
    )
    assert "# Prior Rejection Feedback" in spec_text
    assert quality_marker in spec_text
    assert logic_marker in spec_text
    assert "## grok_pr_quality" in spec_text
    assert "## grok_pr_logic" in spec_text


@pytest.mark.parametrize(
    "reverse_pair,reject_role",
    [
        (False, "pr-reviewer-quality"),
        (True, "pr-reviewer-quality"),
        (False, "pr-reviewer-logic"),
        (True, "pr-reviewer-logic"),
    ],
    ids=[
        "quality-first-quality-rejects",
        "logic-first-quality-rejects",
        "quality-first-logic-rejects",
        "logic-first-logic-rejects",
    ],
)
def test_parallel_pr_pair_reject_plus_pass_records_both(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse_pair: bool,
    reject_role: str,
) -> None:
    """Reject + clean pass: findings reach .spec.md and the pass gate row exists.

    Also asserts the rejection forces head invalidation (pushed reset) even
    when the pass is processed after the reject in pair order.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    _patch_pr_pair_order(monkeypatch, reverse=reverse_pair)

    pushed_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    findings_marker = "REJECT_PLUS_PASS_marker"
    pass_role = (
        "pr-reviewer-logic"
        if reject_role == "pr-reviewer-quality"
        else "pr-reviewer-quality"
    )
    pass_dim = "logic" if pass_role.endswith("logic") else "quality"
    reject_once = {"done": False}
    saw_reject_batch = {"done": False}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if (
            role == reject_role
            and vendor == "grok"
            and not reject_once["done"]
        ):
            reject_once["done"] = True
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    import agent_cli.fixer_act as fixer_mod

    real_aggregate = fixer_mod._aggregate_drive_outcomes

    def wrapping_aggregate(store, tid_, outcomes, **kwargs):  # type: ignore[no-untyped-def]
        msg, head, cont = real_aggregate(store, tid_, outcomes, **kwargs)
        kinds = {o.kind for o in outcomes}
        # The passing sibling is processed sequentially after the rejecting
        # one within the same batch, so by the time its own close_allowed
        # check runs, the reject has already reset the checklist -- its
        # close is correctly refused (not_closable), not agent_closed. The
        # gate row is still recorded regardless (checked separately below).
        if "rejected_new_round" in kinds and (
            "agent_closed" in kinds or "not_closable" in kinds
        ):
            saw_reject_batch["done"] = True
            # Rejection must win on head regardless of pass order.
            assert head is None
            assert cont is True
            pushed = next(
                (
                    str(r.get("status") or "")
                    for r in store.rows("checklist_item")
                    if r.get("task_id") == tid_ and r.get("key") == "pushed"
                ),
                "",
            )
            assert pushed != "ja"
        return msg, head, cont

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act._aggregate_drive_outcomes", wrapping_aggregate
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert saw_reject_batch["done"]
    spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
        encoding="utf-8"
    )
    assert "# Prior Rejection Feedback" in spec_text
    assert findings_marker in spec_text
    # Exactly one rejection section heading for the rejecting dimension.
    reject_key = (
        "grok_pr_quality"
        if reject_role == "pr-reviewer-quality"
        else "grok_pr_logic"
    )
    assert spec_text.count(f"## {reject_key}") == 1
    pass_key = (
        "grok_pr_logic" if pass_role == "pr-reviewer-logic" else "grok_pr_quality"
    )
    # Pass dimension contributes no rejection section.
    assert f"## {pass_key}" not in spec_text.split("# Prior Rejection Feedback", 1)[
        1
    ].split("\n# Constraints\n", 1)[0]

    gates = _gates(tmp_path, tid)
    approved_pass = [
        g
        for g in gates
        if g.get("stage") == "grok-pr"
        and g.get("dimension") == pass_dim
        and g.get("verdict") == "approved"
    ]
    assert approved_pass, f"expected approved gate for {pass_dim}, got {gates}"


def test_parallel_pr_pair_launch_oserror_releases_sibling_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError on one dimension's launch must not leave the sibling agent working.

    Quality (earlier in pair order) raises OSError from launch while logic's
    launch already completed successfully. Without the pair-wide cleanup sweep,
    finish would release only the OSError dimension and orphan logic's working
    row; the sweep must terminalize both.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    launched: list[str] = []

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        launched.append(f"{vendor}:{role}")
        if role == "pr-reviewer-quality" and vendor == "grok":
            raise OSError("No such file or directory: 'grok'")
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, pushed_sha + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert "vendor-cli-unavailable" in result
    assert "grok:pr-reviewer-quality" in launched
    assert "grok:pr-reviewer-logic" in launched
    agents = _agents(tmp_path, tid)
    pr_agents = [
        a
        for a in agents
        if a.get("role") in ("pr-reviewer-quality", "pr-reviewer-logic")
        and a.get("vendor") == "grok"
    ]
    assert len(pr_agents) >= 2, f"expected both grok PR agent rows, got {pr_agents}"
    assert not any(a.get("status") == "working" for a in pr_agents), pr_agents
    gates = _gates(tmp_path, tid)
    approved_logic = [
        g
        for g in gates
        if g.get("stage") == "grok-pr"
        and g.get("dimension") == "logic"
        and g.get("verdict") == "approved"
    ]
    assert approved_logic, f"expected approved gate for logic, got {gates}"
    assert len(approved_logic) == 1, f"sibling gate must be recorded exactly once: {gates}"


@pytest.mark.parametrize(
    "reverse_pair,retry_fail_role,sibling_role,sibling_verdict",
    [
        (False, "pr-reviewer-quality", "pr-reviewer-logic", "approved"),
        (True, "pr-reviewer-quality", "pr-reviewer-logic", "approved"),
        (False, "pr-reviewer-logic", "pr-reviewer-quality", "approved"),
        (True, "pr-reviewer-logic", "pr-reviewer-quality", "approved"),
        (False, "pr-reviewer-quality", "pr-reviewer-logic", "rejected"),
        (True, "pr-reviewer-quality", "pr-reviewer-logic", "rejected"),
        (False, "pr-reviewer-logic", "pr-reviewer-quality", "rejected"),
        (True, "pr-reviewer-logic", "pr-reviewer-quality", "rejected"),
    ],
    ids=[
        "quality-first-quality-retry-oserror-logic-approved",
        "logic-first-quality-retry-oserror-logic-approved",
        "quality-first-logic-retry-oserror-quality-approved",
        "logic-first-logic-retry-oserror-quality-approved",
        "quality-first-quality-retry-oserror-logic-rejected",
        "logic-first-quality-retry-oserror-logic-rejected",
        "quality-first-logic-retry-oserror-quality-rejected",
        "logic-first-logic-retry-oserror-quality-rejected",
    ],
)
def test_parallel_pr_pair_retry_oserror_preserves_sibling_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse_pair: bool,
    retry_fail_role: str,
    sibling_role: str,
    sibling_verdict: str,
) -> None:
    """Retry-phase OSError must not discard a sibling's already-available result.

    Round 48 guarded launch-phase exceptions sitting in launch_results; it did
    not guard OSError raised from complete_spine_agent_step → _lane_retry_then_fail
    mid-loop. Both pair orderings × approved/rejected sibling must persist the
    sibling gate (and rejection feedback) exactly once.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    _patch_pr_pair_order(monkeypatch, reverse=reverse_pair)

    pushed_sha = "d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0"
    findings_marker = "RETRY_OSERROR_SIBLING_REJECT_marker"
    retry_launches = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == retry_fail_role and vendor == "grok":
            retry_launches["n"] += 1
            if retry_launches["n"] == 1:
                # Ambiguous: STATUS complete, no FINDINGS → retry path.
                return LaneResult(
                    role=role,
                    vendor=vendor,
                    status="complete",
                    argv=[vendor],
                    returncode=0,
                    stdout="STATUS: complete\n",
                    stderr="",
                )
            raise OSError("No such file or directory: 'grok'")
        if role == sibling_role and vendor == "grok":
            if sibling_verdict == "rejected":
                return LaneResult(
                    role=role,
                    vendor=vendor,
                    status="complete",
                    argv=[vendor],
                    returncode=0,
                    stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                    stderr="",
                )
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS: none\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert "vendor-cli-unavailable" in result
    assert "OSError" in result
    assert retry_launches["n"] == 2  # initial ambiguous + retry that raises
    agents = _agents(tmp_path, tid)
    assert not any(a.get("status") == "working" for a in agents), agents

    sibling_dim = "logic" if sibling_role.endswith("logic") else "quality"
    gates = _gates(tmp_path, tid)
    sibling_gates = [
        g
        for g in gates
        if g.get("stage") == "grok-pr"
        and g.get("dimension") == sibling_dim
        and g.get("verdict") == sibling_verdict
    ]
    assert sibling_gates, (
        f"expected {sibling_verdict} gate for {sibling_dim}, got {gates}"
    )
    assert len(sibling_gates) == 1, (
        f"sibling gate must be recorded exactly once: {sibling_gates}"
    )

    if sibling_verdict == "rejected":
        spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
            encoding="utf-8"
        )
        assert "# Prior Rejection Feedback" in spec_text
        assert findings_marker in spec_text
        reject_key = (
            "grok_pr_logic" if sibling_role == "pr-reviewer-logic" else "grok_pr_quality"
        )
        assert spec_text.count(f"## {reject_key}") == 1


def test_parallel_pr_pair_prepare_exception_releases_first_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception on the second prepare must not leave the first dimension working."""
    import agent_cli.fixer_act as fixer_mod

    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "cccccccccccccccccccccccccccccccccccccccc"
    real_prepare = fixer_mod.prepare_spine_agent_step
    prepare_calls = {"n": 0}

    def fake_prepare(store, tid_, step, **kwargs):  # type: ignore[no-untyped-def]
        prepare_calls["n"] += 1
        # Only the parallel-pair path binds prepare via fixer_act; raise on
        # the second dimension of that pair (quality then logic).
        if prepare_calls["n"] >= 2:
            raise RuntimeError("boom during second prepare")
        return real_prepare(store, tid_, step, **kwargs)

    def fake_rtc(runner, argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, pushed_sha + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", _pass_lane)
    monkeypatch.setattr("agent_cli.fixer_act._runner_to_completed", fake_rtc)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )
    monkeypatch.setattr("agent_cli.fixer_act.prepare_spine_agent_step", fake_prepare)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        with pytest.raises(RuntimeError, match="boom during second prepare"):
            _drive_one(
                store,
                task,
                runner=lambda argv: Completed(0, "", ""),
                round_cap=5,
                lane_runner=None,
            )
    finally:
        store.close()

    assert prepare_calls["n"] >= 2
    agents = _agents(tmp_path, tid)
    assert not any(a.get("status") == "working" for a in agents), agents


@pytest.mark.parametrize(
    "reverse_pair,fail_role,reject_role",
    [
        (False, "pr-reviewer-quality", "pr-reviewer-logic"),
        (True, "pr-reviewer-quality", "pr-reviewer-logic"),
        (False, "pr-reviewer-logic", "pr-reviewer-quality"),
        (True, "pr-reviewer-logic", "pr-reviewer-quality"),
    ],
    ids=[
        "quality-first-quality-fails",
        "logic-first-quality-fails",
        "quality-first-logic-fails",
        "logic-first-logic-fails",
    ],
)
def test_parallel_pr_pair_failed_plus_reject_keeps_failed_and_skips_round_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse_pair: bool,
    fail_role: str,
    reject_role: str,
) -> None:
    """Lane-retry fail + sibling reject must keep state=failed and not round-start.

    A failed dimension (two unparseable STATUS: complete bodies, no FINDINGS:)
    paired with a parseable rejection must not be resurrected by the sibling's
    deferred ``_round_start``, and the aggregator's cont=True from the reject
    must not hide the failed message. A second scan must not re-select the task.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)
    _patch_pr_pair_order(monkeypatch, reverse=reverse_pair)

    pushed_sha = "ffffffffffffffffffffffffffffffffffffffff"
    findings_marker = "FAILED_PLUS_REJECT_marker"
    fail_launches = {"n": 0}

    store = _store(tmp_path)
    try:
        task_before = store.row("task", tid)
        assert task_before is not None
        round_before = int(task_before.get("current_round") or 0)
        rounds_before = [
            r for r in store.rows("task_round") if r.get("task_id") == tid
        ]
    finally:
        store.close()

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == fail_role and vendor == "grok":
            fail_launches["n"] += 1
            # No FINDINGS: header → unparseable → retry; second attempt fails task.
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout="STATUS: complete\n",
                stderr="",
            )
        if role == reject_role and vendor == "grok":
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        task_after = store.row("task", tid)
        assert task_after is not None
        rounds_after = [
            r for r in store.rows("task_round") if r.get("task_id") == tid
        ]
    finally:
        store.close()

    assert fail_launches["n"] == 2  # initial + one retry
    assert _task_state(tmp_path, tid) == "failed"
    assert int(task_after.get("current_round") or 0) == round_before
    assert len(rounds_after) == len(rounds_before)
    assert "failed" in result
    assert "lane retry exhausted" in result
    assert "scan-error" not in result
    assert "SystemExit" not in result

    agents_after_first = len(_agents(tmp_path, tid))
    store = _store(tmp_path)
    try:
        lines2 = drive_error_fix_tasks(
            store,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
    finally:
        store.close()

    assert _task_state(tmp_path, tid) == "failed"
    assert all(tid not in line for line in lines2)
    assert len(_agents(tmp_path, tid)) == agents_after_first


def test_parallel_pr_pair_reject_then_sibling_oserror_still_round_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject that commits a checklist reset must still round-start if the
    sibling dimension then raises OSError mid-batch.

    Quality (earlier in pair order) rejects with real findings so
    ``_apply_rejection_resets`` writes the checklist reset with
    ``defer_round_start=True``. Logic's launch raises OSError afterward.
    The except path must release working agents, then best-effort call
    ``_round_start`` so the reset is paired with a new ``task_round`` row,
    while still letting the original OSError surface as vendor-cli-unavailable.
    """
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "1212121212121212121212121212121212121212"
    findings_marker = "REJECT_THEN_OSERROR_marker"

    store = _store(tmp_path)
    try:
        task_before = store.row("task", tid)
        assert task_before is not None
        round_before = int(task_before.get("current_round") or 0)
        rounds_before = [
            r for r in store.rows("task_round") if r.get("task_id") == tid
        ]
    finally:
        store.close()

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        role = str(kwargs.get("role") or "")
        vendor = str(kwargs.get("vendor") or "grok")
        if role == "pr-reviewer-logic" and vendor == "grok":
            raise OSError("No such file or directory: 'grok'")
        if role == "pr-reviewer-quality" and vendor == "grok":
            return LaneResult(
                role=role,
                vendor=vendor,
                status="complete",
                argv=[vendor],
                returncode=0,
                stdout=f"STATUS: complete\nFINDINGS:\n- {findings_marker}\n",
                stderr="",
            )
        return LaneResult(
            role=role,
            vendor=vendor,
            status="complete",
            argv=[vendor],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner, expected_branch=None, expected_repo=None: pushed_sha,
    )
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act._runner_to_completed", _pr_pair_rtc(pushed_sha)
    )
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        _fake_insert_pr_open_and_scan,
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        result = _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
        task_after = store.row("task", tid)
        assert task_after is not None
        rounds_after = [
            r for r in store.rows("task_round") if r.get("task_id") == tid
        ]
    finally:
        store.close()

    assert "vendor-cli-unavailable" in result
    assert "OSError" in result
    # Original OSError must win; round_start recovery must not mask it.
    assert "scan-error" not in result
    assert int(task_after.get("current_round") or 0) == round_before + 1
    assert len(rounds_after) == len(rounds_before) + 1
    assert _task_state(tmp_path, tid) == "implementing"
    agents = _agents(tmp_path, tid)
    assert not any(a.get("status") == "working" for a in agents), agents
    spec_text = (tmp_path / "error-fix-specs" / tid / ".spec.md").read_text(
        encoding="utf-8"
    )
    assert "# Prior Rejection Feedback" in spec_text
    assert findings_marker in spec_text
    assert spec_text.count("## grok_pr_quality") == 1

    gates = _gates(tmp_path, tid)
    rejected_quality = [
        g
        for g in gates
        if g.get("stage") == "grok-pr"
        and g.get("dimension") == "quality"
        and g.get("verdict") == "rejected"
    ]
    assert len(rejected_quality) == 1, (
        f"rejection gate must be recorded exactly once: {gates}"
    )
