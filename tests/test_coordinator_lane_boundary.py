"""Focused regressions for coordinator lane boundary corrections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.coordinator_common import CoordinatorError, coord
from agent_cli.coordinator_lanes import _prepare_pr_review_agent, launch_lane, phase_pr_gates
from agent_cli.coordinator_runtime import phase_inner_review
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    make_session,
    make_worker,
    patch_account_runners,
    write_accounts,
)

def _seed_pr_task(store: Store, worker, tid: str, wt: Path, fake: FakeGh, *, phase: str) -> None:
    from agent_cli.coordinator_runtime import execution_binding

    source = {
        "repo": "example/project",
        "number": 7,
        "assigned_id": "a",
        "publication_repo": "example/project",
        "base": "develop",
        "title": "Fix",
    }
    store.write(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": "42",
            "payload": {
                "coordinator": {
                    "phase": phase,
                    "source": source,
                    "worktree": str(wt),
                    "branch": f"task-{tid[:8]}",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                    "pr_number": 42,
                    "evidence": {"tests_pass": True, "tests_head": fake.head},
                    "execution_binding": execution_binding(store, worker, source["repo"]),
                }
            },
            "state": "pr-review" if phase.startswith("pr_") else "reviewing",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    store.write(
        "task_round",
        "insert",
        f"round-{tid}",
        {
            "id": f"round-{tid}",
            "task_id": tid,
            "round": 1,
            "implementer_verdict": "done",
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )


def test_prepare_pr_review_rejects_unconfigured_runtime_without_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper validates selected.account.lane_runtime before inserting a working agent."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    path = store.home / "ai-accounts.json"
    configuration = json.loads(path.read_text(encoding="utf-8"))
    configuration["accounts"]["grok-w"]["lane_runtime"] = None
    path.write_text(json.dumps(configuration), encoding="utf-8")

    tid = "11111111-1111-1111-1111-111111111111"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    _seed_pr_task(store, worker, tid, wt, fake, phase="pr_gates_grok")
    task = store.row("task", tid)
    assert task is not None

    before = [a for a in store.rows("agent") if a.get("task_id") == tid]
    with pytest.raises(CoordinatorError, match="lane_runtime is unconfigured"):
        _prepare_pr_review_agent(
            store,
            worker,
            task,
            role="pr-reviewer-quality",
            vendor="grok",
            head=fake.head,
            spec_body="review body",
        )
    after = [a for a in store.rows("agent") if a.get("task_id") == tid]
    assert after == before
    assert not any(a.get("status") == "working" for a in after)


def test_pr_and_inner_review_prompts_receive_complete_diff_without_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long diff tail beyond the former 12000-char excerpt reaches both review prompts."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    marker = "UNIQUE_DIFF_TAIL_MARKER_BEYOND_EXCERPT"
    long_diff = ("x" * 13000) + marker + "\n"

    def _git_with_long_diff(argv: list[str]) -> Completed:
        if argv and argv[0] == "env" and "git" in argv:
            argv = argv[argv.index("git") :]
        if argv[:1] == ["git"]:
            args = argv[1:]
            if args and args[0] == "-C":
                args = args[2:]
            if args and args[0] == "diff" and ".." in " ".join(args):
                return Completed(0, long_diff, "")
            if args and args[0] == "ls-files":
                return Completed(0, "", "")
            if args and args[0] == "rev-parse" and args[-1] == "HEAD":
                return Completed(0, fake.head + "\n", "")
            if args and args[0] == "verify-commit":
                return Completed(0, "", "")
            if args and args[0] == "status":
                return Completed(0, "", "")
            if args and args[0] == "cat-file":
                return Completed(0, "gpgsig -----BEGIN\n", "")
        return fake(argv)

    captured: list[str] = []

    def capturing_lane(selected, *, cwd, manifest, spec, timeout) -> Completed:
        assert selected.account.lane_runtime is not None and timeout > 0
        captured.append(spec)
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    # Avoid checklist bookkeeping in this prompt-shape regression.
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.set_checklist", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.set_checklist", lambda *a, **k: None
    )
    # Checkout ownership/signatures are separate tested prerequisites. This
    # regression isolates actual prompt construction and source-lane delivery.
    monkeypatch.setattr("agent_cli.coordinator_lanes.verify_checkout_identity", lambda *a: {})
    monkeypatch.setattr("agent_cli.coordinator_lanes.verify_signed_clean_head", lambda *a: fake.head)

    tid = "22222222-2222-2222-2222-222222222222"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    _seed_pr_task(store, worker, tid, wt, fake, phase="pr_gates_grok")
    task = store.row("task", tid)
    assert task is not None

    phase_pr_gates(
        store,
        worker,
        task,
        runner=_git_with_long_diff,
        lane_runner=capturing_lane,
        vendor="grok",
        stage="grok-pr",
    )
    assert len(captured) == 2, "both PR review dimensions must receive the complete diff"
    for spec in captured:
        assert marker in spec
        assert "---- complete diff ----" in spec
        assert "---- end diff ----" in spec
        assert "---- diff excerpt ----" not in spec
        assert "Script-generated diff artifact" not in spec
        # Host control artifact paths must not be instructed into the model prompt.
        assert "review-diff-" not in spec

    captured.clear()
    tid2 = "33333333-3333-3333-3333-333333333333"
    wt2 = worker.workspace_root / tid2
    wt2.mkdir(parents=True)
    (wt2 / ".git").mkdir()
    _seed_pr_task(store, worker, tid2, wt2, fake, phase="inner_review")
    task2 = store.row("task", tid2)
    assert task2 is not None

    phase_inner_review(
        store,
        worker,
        task2,
        runner=_git_with_long_diff,
        lane_runner=capturing_lane,
    )
    assert len(captured) == 1
    inner = captured[0]
    assert marker in inner
    assert "---- complete diff ----" in inner
    assert "---- end diff ----" in inner
    assert "---- diff excerpt ----" not in inner
    assert "Script-generated diff artifact" not in inner
    assert "review-diff-" not in inner


def test_launch_lane_inventory_failure_creates_no_working_agent_or_uncertain_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing static git inventory before executor creates neither agent nor uncertain_lane."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    # Checkout identity is unrelated; inventory failure is the regression under test.
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity", lambda *a: {}
    )

    launched: list[bool] = []

    def refusing_executor(selected, *, cwd, manifest, spec, timeout):
        launched.append(True)
        raise AssertionError("executor must not run after inventory failure")

    def failing_inventory(argv: list[str]) -> Completed:
        if argv and argv[0] == "env" and "git" in argv:
            argv = argv[argv.index("git") :]
        if argv[:1] == ["git"]:
            args = argv[1:]
            if args and args[0] == "-C":
                args = args[2:]
            if args and args[0] == "ls-files":
                return Completed(1, "", "source inventory unavailable")
        return fake(argv)

    tid = "44444444-4444-4444-4444-444444444444"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    _seed_pr_task(store, worker, tid, wt, fake, phase="implement")
    task = store.row("task", tid)
    assert task is not None

    before = [a for a in store.rows("agent") if a.get("task_id") == tid]
    with pytest.raises(CoordinatorError, match="inventory"):
        launch_lane(
            store,
            worker,
            task,
            role="implementer",
            vendor="grok",
            round_num=1,
            spec_body="implement body",
            runner=failing_inventory,
            lane_runner=refusing_executor,
        )
    after = [a for a in store.rows("agent") if a.get("task_id") == tid]
    assert after == before
    assert not any(a.get("status") == "working" for a in after)
    refreshed = store.row("task", tid)
    assert refreshed is not None
    c = coord(refreshed)
    assert c.get("uncertain_lane") is not True
    assert launched == []
