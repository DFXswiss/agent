"""Tests for the error-fix fixer driver (fixer_act) and related run_core wiring."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from agent_cli.fixer_act import (
    _contributing_ok_evidence,
    _drive_one,
    _error_fix_brief,
    _first_sentence,
    _open_error_fix_tasks,
    _pr_open_row_exists,
    _runner_to_completed,
    drive_error_fix_tasks,
    template_pr_open_payload,
    write_error_fix_spec,
)
from agent_cli.git_act import GitActError
from agent_cli.lane import LaneResult, findings_header_present
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
    (worktree / ".spec.md").write_text("# Task\n\nfix it\n", encoding="utf-8")
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


def test_pushed_passes_expected_branch_from_error_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """error-fix tasks derive expected_branch=error-fix-<id8> for push_branch."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    captured: dict[str, object] = {}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        captured["expected_branch"] = expected_branch
        return "abcdef1234567890abcdef1234567890abcdef12"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert captured.get("expected_branch") == f"error-fix-{ERROR_ID[:8]}"
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
        lambda *, cwd, runner, expected_branch=None: "abcdef1234567890abcdef1234567890abcdef12",
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
        assert not _pr_open_row_exists(store, head="error-fix- ")
    finally:
        store.close()


def test_fixer_threads_pushed_head_into_pr_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After pushed, the fixer must record PR gates with a real non-empty head_sha."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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


def _fake_insert_pr_open_and_scan(store, *, session_id, payload, runner):  # type: ignore[no-untyped-def]
    """Simulate a successful insert_pr_open_and_scan: writes a real pr.open row
    so _pr_open_row_exists finds it (matches flaky_insert's success branch).

    Real insert_pr_open_and_scan leaves execution_status=done after scan_github
    succeeds; only done counts as present under the stricter exists check.
    """
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
        lambda *, cwd, runner, expected_branch=None: pushed_sha,
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
        lambda *, cwd, runner, expected_branch=None: pushed_sha,
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
        lambda *, cwd, runner, expected_branch=None: pushed_sha,
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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

    spec_text = (tmp_path / "error-fix-work" / tid / ".spec.md").read_text(
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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

    calls: list[tuple[str | None, str, str | None]] = []
    real_execute = fixer_mod.execute_spine_step

    def spy_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        outcome = real_execute(*args, **kwargs)
        calls.append((kwargs.get("head"), outcome.kind, outcome.key))
        return outcome

    monkeypatch.setattr("agent_cli.fixer_act.execute_spine_step", spy_execute)

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
    assert reject_idx + 1 < len(calls), (
        "expected a further execute_spine_step call after the PR-gate rejection"
    )
    next_head, _next_kind, _next_key = calls[reject_idx + 1]
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
        lambda *, cwd, runner, expected_branch=None: pushed_sha,
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
        assert _pr_open_row_exists(store, head=head) is False
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
        assert _pr_open_row_exists(store, head=head) is False
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

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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
