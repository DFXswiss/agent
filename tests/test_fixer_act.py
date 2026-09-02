"""Tests for the error-fix fixer driver (fixer_act) and related run_core wiring."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from agent_cli.fixer_act import _drive_one, _runner_to_completed
from agent_cli.git_act import GitActError
from agent_cli.lane import LaneResult, findings_header_present
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_cli import _last_task_id, run
from test_run import (
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


def test_fixer_threads_pushed_head_into_pr_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After pushed, the fixer must record PR gates with a real non-empty head_sha."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"

    def fake_push(*, cwd: str, runner):  # type: ignore[no-untyped-def]
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

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr(
        "agent_cli.fixer_act.insert_pr_open_and_scan",
        lambda *a, **k: [],
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


def test_fixer_retries_pr_open_across_scans_after_insert_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed pr.open insert must retry on the next scan once pushed is already ja."""
    tid = _bootstrap_error_fix_task(tmp_path, capsys)
    _advance_error_fix_to_pushed(tmp_path, tid, capsys, monkeypatch)

    pushed_sha = "abcdef1234567890abcdef1234567890abcdef12"
    insert_calls = {"n": 0}
    head = f"error-fix-{ERROR_ID[:8]}"

    def fake_push(*, cwd: str, runner):  # type: ignore[no-untyped-def]
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
                "execution_status": "pending",
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
        _drive_one(
            store,
            task,
            runner=lambda argv: Completed(0, "", ""),
            round_cap=5,
            lane_runner=None,
        )
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
