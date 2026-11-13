from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli.coordinator import tick
from agent_cli.coordinator_checks import registry_row_id
from agent_cli.coordinator_common import LaneFailedError, coord, infra_failure
from agent_cli.coordinator_config import WorkerConfig
from agent_cli.coordinator_lanes import (
    launch_lane,
    lane_retry,
    retry_remaining,
    schedule_retry,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_coordinator import (
    patch_account_runners,
    patch_execute_github,
    patch_run_bounded,
    seed_task,
)
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    patch_command_runner,
    scan_done,
    write_accounts,
)


def _expire_retry(store: Store, task_id: str) -> None:
    task = store.row("task", task_id)
    task["payload"]["coordinator"]["retry"]["at"] = "2020-01-01T00:00:00Z"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )


def _tick_to_implement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane_retries: int,
) -> tuple[Store, WorkerConfig, FakeGh, str]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = replace(make_worker(tmp_path), lane_retries=lane_retries)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    for _ in range(5):
        tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
        tasks = store.rows("task")
        if tasks and coord(tasks[0]).get("phase") == "implement":
            break
    task = store.rows("task")[0]
    assert coord(task)["phase"] == "implement"
    return store, worker, fake, str(task["id"])


def _drive_to_ready_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> tuple[Store, WorkerConfig, FakeGh, Any]:
    from agent_cli import github_act
    from test_coordinator_flow import _drive_through_ci_to
    from test_coordinator_support import patch_account_runners as patch_accounts

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_accounts(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, phase)
    return store, worker, fake, lane


def _tests_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane_retries: int,
    returncode: int,
) -> tuple[Store, WorkerConfig, FakeGh, str, list[str]]:
    from agent_cli import coordinator_runtime

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = replace(make_worker(tmp_path), lane_retries=lane_retries)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    worktree = worker.workspace_root / "check-task"
    worktree.mkdir()
    tid = "d1000001-0000-4000-8000-000000000001"
    seed_task(
        store,
        worker,
        tid,
        {
            "id": tid,
            "session_id": worker.session_id,
            "workflow": "implement",
            "state": "local-check",
            "title": "Check task",
            "repo": "example/project",
            "ref": "42",
            "models": {"first": "grok", "second": "codex"},
            "payload": {
                "coordinator": {
                    "phase": "tests",
                    "source": {"repo": "example/project", "number": 7},
                    "worktree": str(worktree),
                    "head_sha": fake.head,
                }
            },
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        coordinator_runtime, "verify_signed_clean_head", lambda *a, **k: fake.head
    )
    monkeypatch.setattr(
        coordinator_runtime,
        "run_bounded",
        lambda argv, **kwargs: Completed(returncode, "out", ""),
    )
    lines = coordinator_runtime.phase_tests(
        store,
        worker,
        store.row("task", tid),
        lambda argv: Completed(0, "", ""),
    )
    return store, worker, fake, tid, lines


@pytest.mark.no_pg
def test_infra_failure() -> None:
    assert all(infra_failure(code) for code in (124, 127, -9, -15))
    assert not any(infra_failure(code) for code in (0, 1, 2, 126))


@pytest.mark.no_pg
def test_retry_remaining() -> None:
    worker = SimpleNamespace(poll_seconds=30, lane_retries=2)
    assert retry_remaining({}, object()) == 0
    checkpoint: dict[str, Any] = {}
    schedule_retry(checkpoint, worker, phase="implement", reason="partial")
    assert 1 <= retry_remaining(checkpoint, worker) <= 30
    schedule_retry(checkpoint, worker, phase="implement", reason="partial")
    assert 31 <= retry_remaining(checkpoint, worker) <= 60
    checkpoint["retry"]["at"] = "2020-01-01T00:00:00Z"
    assert retry_remaining(checkpoint, worker) == 0
    checkpoint["retry"]["at"] = "not-a-time"
    assert retry_remaining(checkpoint, worker) == 0
    checkpoint["retry"] = {
        "phase": "implement",
        "attempt": 1,
        "at": "2999-01-01T00:00:00Z",
    }
    assert retry_remaining(checkpoint, worker) == 30


@pytest.mark.no_pg
def test_schedule_retry_counts_per_phase_and_stops_at_the_bound() -> None:
    worker = SimpleNamespace(poll_seconds=30, lane_retries=2)
    checkpoint: dict[str, Any] = {}
    first = schedule_retry(
        checkpoint, worker, phase="implement", reason="implementer status=partial"
    )
    second = schedule_retry(
        checkpoint, worker, phase="implement", reason="implementer status=partial"
    )
    third = schedule_retry(
        checkpoint, worker, phase="implement", reason="implementer status=partial"
    )
    assert first == [
        "retry 1/2 of implement after implementer status=partial; next attempt in 30s"
    ]
    assert second[0].endswith("next attempt in 60s")
    assert third is None and "retry" not in checkpoint
    line = schedule_retry(
        checkpoint, worker, phase="inner_review", reason="broken\noutput"
    )
    assert checkpoint["retry"]["attempt"] == 1
    assert checkpoint["phase"] == "inner_review"
    assert checkpoint["retry"]["reason"] == "broken output"
    assert "broken output" in line[0]
    disabled = SimpleNamespace(poll_seconds=30, lane_retries=0)
    untouched = {"phase": "implement"}
    assert schedule_retry(
        untouched, disabled, phase="implement", reason="partial"
    ) is None
    assert untouched == {"phase": "implement"}


def test_lane_retry_saves_a_copy_and_replaces_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes

    store = Store(tmp_path)
    worker = replace(make_worker(tmp_path), lane_retries=1)
    make_session(store, worker.session_id, ["spine", "review-loop", "pr-review"])
    tid = "d1000001-0000-4000-8000-000000000002"
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "implementing",
        "payload": {
            "coordinator": {
                "phase": "implement",
                "lane_outcome": {"role": "implementer"},
            }
        },
    }
    store.write("task", "insert", tid, task)
    real_save = coordinator_lanes.save_task

    def fail_save(*args: Any, **kwargs: Any) -> None:
        raise StoreError("save failed")

    monkeypatch.setattr(coordinator_lanes, "save_task", fail_save)
    with pytest.raises(StoreError, match="save failed"):
        lane_retry(
            store,
            worker,
            task,
            phase="implement",
            reason="partial",
            before_save=lambda cp: cp.pop("lane_outcome", None),
        )
    assert "lane_outcome" in coord(task) and "retry" not in coord(task)

    monkeypatch.setattr(coordinator_lanes, "save_task", real_save)
    lines = lane_retry(
        store,
        worker,
        task,
        phase="implement",
        reason="partial",
        before_save=lambda cp: cp.pop("lane_outcome", None),
    )
    stored = store.row("task", tid)
    assert len(lines) == 1
    assert coord(stored)["retry"]["attempt"] == 1
    assert coord(stored)["phase"] == "implement"
    assert "lane_outcome" not in coord(stored)
    assert coord(task) == coord(stored)

    saves = 0

    def count_save(*args: Any, **kwargs: Any) -> None:
        nonlocal saves
        saves += 1

    monkeypatch.setattr(coordinator_lanes, "save_task", count_save)
    assert lane_retry(
        store, worker, task, phase="implement", reason="partial"
    ) is None
    assert saves == 0 and "retry" not in coord(task)


@pytest.mark.no_pg
def test_backoff_parks_the_task() -> None:
    from agent_cli.coordinator_runtime import parked

    worker = SimpleNamespace(poll_seconds=30, lane_retries=1)
    task = {"payload": {"coordinator": {"phase": "implement"}}}
    schedule_retry(coord(task), worker, phase="implement", reason="partial")
    assert parked(task, worker) is True
    coord(task)["retry"]["at"] = "2020-01-01T00:00:00Z"
    assert parked(task, worker) is False


def test_incomplete_implementer_is_retried_then_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    fake.model_outputs["implementer"] = "STATUS: partial\n"
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert (
        "retry 1/1 of implement after implementer status=partial; next attempt in 30s"
    ) in lines
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "implement"
    assert checkpoint["retry"]["attempt"] == 1
    assert "lane_outcome" not in checkpoint
    rounds = [row for row in store.rows("task_round") if row.get("task_id") == tid]
    assert len(rounds) == 1 and rounds[0]["implementer_verdict"] is None

    _expire_retry(store, tid)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "implement"
    assert checkpoint["blocker"] == "implementer status=partial"
    assert "retry" not in checkpoint
    rounds = [row for row in store.rows("task_round") if row.get("task_id") == tid]
    assert len(rounds) == 1 and rounds[0]["implementer_verdict"] == "blocked"
    assert fake.launched.count("implementer") == 2


def test_retry_waits_for_its_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    fake.model_outputs["implementer"] = "STATUS: partial\n"
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    retry = coord(store.row("task", tid))["retry"].copy()
    launched = list(fake.launched)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert fake.launched == launched
    assert coord(store.row("task", tid))["retry"] == retry


def test_lane_retries_zero_keeps_the_immediate_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=0
    )
    fake.model_outputs["implementer"] = "STATUS: partial\n"
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["blocker"] == "implementer status=partial"
    assert "retry" not in checkpoint


def test_invalid_implementer_result_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    fake.model_outputs["implementer"] = "STATUS: complete\nRESULT: approved\n"
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert (
        "retry 1/1 of implement after implementer RESULT=approved; next attempt in 30s"
    ) in lines
    assert "lane_outcome" not in coord(store.row("task", tid))


def test_retry_budget_resets_after_a_human_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    fake.model_outputs["implementer"] = "STATUS: partial\n"
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    _expire_retry(store, tid)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    activity = store.row("activity", checkpoint["question_activity_id"])
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {"id": 2, "body": "retry now", "user": {"login": "human-owner"}},
    ]
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "implement" and "retry" not in checkpoint
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["retry"]["attempt"] == 1
    rounds = [row for row in store.rows("task_round") if row.get("task_id") == tid]
    assert len(rounds) == 2


def test_retry_is_removed_when_the_phase_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    default_output = fake.model_outputs["implementer"]
    fake.model_outputs["implementer"] = "STATUS: partial\n"
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    fake.model_outputs["implementer"] = default_output
    _expire_retry(store, tid)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] != "implement"
    assert "retry" not in checkpoint


def test_lane_exception_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )

    def broken(*args: Any, **kwargs: Any) -> Completed:
        raise RuntimeError("transport")

    lines = tick(store, worker, runner=fake, lane_runner=broken)
    assert lines == [
        "retry 1/1 of implement after lane interrupted: transport; next attempt in 30s"
    ]
    agents = [row for row in store.rows("agent") if row.get("task_id") == tid]
    assert len(agents) == 1
    assert agents[0]["status"] == "done"
    assert agents[0]["note"] == "interrupted: transport"
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["lane"] == {"agent_id": agents[0]["id"], "state": "failed"}
    assert checkpoint.get("uncertain_lane") is not True
    assert checkpoint["phase"] == "implement"
    rounds = [row for row in store.rows("task_round") if row.get("task_id") == tid]
    assert len(rounds) == 1 and rounds[0]["implementer_verdict"] is None
    assert not any(
        row.get("type") == "comment.post"
        and "Blocked:" in str((row.get("payload") or {}).get("body") or "")
        for row in store.rows("activity")
    )
    _expire_retry(store, tid)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert fake.launched == ["implementer"]


def test_lane_exception_blocks_as_lane_failed_and_a_reply_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=0
    )

    def broken(*args: Any, **kwargs: Any) -> Completed:
        raise RuntimeError("transport")

    lines = tick(store, worker, runner=fake, lane_runner=broken)
    blocker = "Model lane interrupted in implement; not a code rejection."
    assert lines[-2:] == [
        f"blocked: {blocker}",
        "blocker published on example/project#7",
    ]
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "implement"
    assert checkpoint["blocker"] == blocker
    assert checkpoint.get("uncertain_lane") is not True
    assert isinstance(checkpoint.get("question_activity_id"), str)
    assert not any(
        "transport" in str((row.get("payload") or {}).get("body") or "")
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    )
    activity = store.row("activity", checkpoint["question_activity_id"])
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {"id": 2, "body": "resume", "user": {"login": "human-owner"}},
    ]
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert coord(store.row("task", tid))["phase"] == "implement"
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert fake.launched == ["implementer"]


def test_incomplete_inner_reviewer_is_retried_then_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), lane_retries=1)
    patch_execute_github(monkeypatch)
    worktree = worker.workspace_root / "inner"
    worktree.mkdir()
    tid = "d1000001-0000-4000-8000-000000000003"
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "reviewing",
        "current_round": 1,
        "models": {"first": "grok", "second": "codex"},
        "payload": {
            "coordinator": {
                "phase": "inner_review",
                "worktree": str(worktree),
                "pr_number": 5,
                "head_sha": "a" * 40,
                "source": {"repo": "example/project", "number": 7},
            }
        },
    }
    store.write("task", "insert", tid, task)
    store.write(
        "task_round",
        "insert",
        "healing-round-1",
        {
            "id": "healing-round-1",
            "task_id": tid,
            "round": 1,
            "implementer_verdict": "done",
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *a, **k: {},
    )
    calls = 0

    def partial(*args: Any, **kwargs: Any) -> Completed:
        nonlocal calls
        calls += 1
        return Completed(0, "STATUS: partial\n", "")

    lines = coordinator_runtime.phase_inner_review(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", ""), partial
    )
    assert lines == [
        "retry 1/1 of inner_review after inner reviewer status=partial "
        "result=empty; next attempt in 30s"
    ]
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "inner_review"
    assert "lane_outcome" not in checkpoint
    agents = [row for row in store.rows("agent") if row.get("task_id") == tid]
    lane_runs = [
        row
        for row in store.rows("activity")
        if row.get("type") == "lane.run"
        and (row.get("payload") or {}).get("task_id") == tid
    ]
    assert len(agents) == 1 and agents[0]["status"] == "done"
    assert len(lane_runs) == 1 and (lane_runs[0].get("payload") or {})["status"] == "partial"
    _expire_retry(store, tid)
    coordinator_runtime.phase_inner_review(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", ""), partial
    )
    assert calls == 2
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "inner_review"
    assert checkpoint["blocker"] == (
        "inner reviewer incomplete (status=partial result=empty)"
    )
    assert "retry" not in checkpoint


@pytest.mark.parametrize("code", [124, 127, -9])
def test_check_exit_is_not_routed_to_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    store, _worker, fake, tid, lines = _tests_task(
        tmp_path, monkeypatch, lane_retries=0, returncode=code
    )
    blocker = (
        f"Check command could not run on {fake.head[:7]} (exit {code}); "
        "not a code failure."
    )
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "tests"
    assert checkpoint["blocker"] == blocker
    assert isinstance(checkpoint.get("question_activity_id"), str)
    assert not [row for row in store.rows("local_check") if row.get("task_id") == tid]
    assert not checkpoint.get("findings")
    assert "tests_pass" not in checkpoint.get("evidence", {})
    assert lines[-2:] == [f"blocked: {blocker}", "blocker published on example/project#7"]


def test_check_exit_is_retried_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid, lines = _tests_task(
        tmp_path, monkeypatch, lane_retries=1, returncode=124
    )
    assert lines == ["retry 1/1 of tests after check command exit 124; next attempt in 30s"]
    assert coord(store.row("task", tid))["phase"] == "tests"
    assert not [row for row in store.rows("local_check") if row.get("task_id") == tid]
    from agent_cli import coordinator_runtime

    _expire_retry(store, tid)
    lines = coordinator_runtime.phase_tests(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", "")
    )
    blocker = (
        f"Check command could not run on {fake.head[:7]} (exit 124); "
        "not a code failure."
    )
    assert lines[-2:] == [f"blocked: {blocker}", "blocker published on example/project#7"]


def test_failing_tests_still_route_to_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _fake, tid, _lines = _tests_task(
        tmp_path, monkeypatch, lane_retries=0, returncode=1
    )
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "implement"
    checks = [row for row in store.rows("local_check") if row.get("task_id") == tid]
    assert len(checks) == 1 and checks[0]["result"] == "fail"
    assert checkpoint["findings"].startswith("Tests failed on")


def _a38_tests_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    precheck_code: int,
    suite_code: int,
) -> tuple[Store, WorkerConfig, FakeGh, str, list[str], list[str]]:
    from agent_cli.coordinator_a38 import phase_tests

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
        lane_retries=0,
    )
    patch_execute_github(monkeypatch)
    fake = FakeGh()
    head = "a" * 40
    base = "b" * 40
    tid = "d1000001-0000-4000-8000-000000000004"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "local-check",
        "title": "A38 tests",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {
            "coordinator": {
                "phase": "tests",
                "head_sha": head,
                "worktree": str(worktree),
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7},
                "a38": {"enabled": True},
            }
        },
    }
    store.write("task", "insert", tid, task)
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: (base, ["src/app.py"], False, False),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped",
        lambda *a, **k: lambda argv: Completed(0, "false", ""),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.gh_json", lambda *a, **k: False)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.run_bounded",
        lambda *a, **k: Completed(precheck_code, "precheck", ""),
    )

    def fake_git(_store, _worker, _runner, _worktree, *parts):  # noqa: ANN001
        if parts[0] in ("fetch", "cat-file"):
            return Completed(0, "", "")
        if parts[0] == "show":
            return Completed(0, '{"schema":"a38-policy/v1","jobs":[]}', "")
        raise AssertionError(parts)

    monkeypatch.setattr("agent_cli.coordinator_a38.git", fake_git)
    calls: list[str] = []

    def fake_cli(argv, *, worker, cwd):  # noqa: ANN001
        calls.append(argv[0])
        if argv[0] == "run":
            return Completed(suite_code, "", "timeout" if suite_code else "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.coordinator_a38._run_cli", fake_cli)
    lines = phase_tests(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", "")
    )
    return store, worker, fake, tid, lines, calls


def test_a38_suite_timeout_is_not_routed_to_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _fake, tid, lines, calls = _a38_tests_task(
        tmp_path, monkeypatch, precheck_code=0, suite_code=124
    )
    assert lines == [
        "blocked: Check command could not run on aaaaaaa (exit 124); not a code failure.",
        "blocker published on example/project#7",
    ]
    checkpoint = coord(store.row("task", tid))
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "tests"
    assert not [
        row
        for row in store.rows("local_check")
        if row.get("task_id") == tid and row.get("name") == "a38"
    ]
    assert not checkpoint.get("findings")
    assert "verify" not in calls


def test_a38_precheck_that_could_not_run_blocks_before_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _worker, _fake, tid, lines, calls = _a38_tests_task(
        tmp_path, monkeypatch, precheck_code=127, suite_code=0
    )
    assert lines == [
        "blocked: Check command could not run on aaaaaaa (exit 127); not a code failure.",
        "blocker published on example/project#7",
    ]
    assert calls == []
    assert not [row for row in store.rows("local_check") if row.get("task_id") == tid]


def _working_implementer(store: Store, worker: WorkerConfig, task_id: str, agent_id: str):
    store.write(
        "agent",
        "insert",
        agent_id,
        {
            "id": agent_id,
            "session_id": worker.session_id,
            "task_id": task_id,
            "round": 1,
            "role": "implementer",
            "vendor": "grok",
            "status": "working",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "note": None,
        },
    )


def test_working_row_without_process_is_recovered_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    agent_id = "aaaaaaaa-0000-4000-8000-000000000001"
    _working_implementer(store, worker, tid, agent_id)

    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert any(
        line.startswith(
            "retry 1/1 of implement after a lane left running by an earlier "
            "tick ended without a result"
        )
        for line in lines
    )
    agent = store.row("agent", agent_id)
    assert agent["status"] == "done"
    assert agent["note"] == "recovered: no live process"
    row = store.row("checklist_item", registry_row_id(tid, "no_live_lane"))
    assert row["status"] == "nein"
    assert "implementer" not in fake.launched

    _expire_retry(store, tid)
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    row = store.row("checklist_item", registry_row_id(tid, "no_live_lane"))
    assert row["status"] == "ja"
    assert "implementer" in fake.launched


def test_working_row_blocks_as_lane_failed_without_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=0
    )
    _working_implementer(
        store, worker, tid, "aaaaaaaa-0000-4000-8000-000000000002"
    )

    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    c = coord(store.row("task", tid))
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "implement"
    assert c["blocker"] == (
        "Model lane interrupted in implement; not a code rejection."
    )
    assert c["failed_check"] == "no_live_lane"
    assert "uncertain_lane" not in c
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and "<!-- agent-coordinator:status:v1:lane-failed:"
        in row["payload"]["body"]
    ]
    assert len(comments) == 1


def test_live_orphan_lane_parks_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import lane_start_recorder
    from agent_cli.coordinator_runtime import parked
    from test_lane_process import sleeping_process

    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    agent_id = "aaaaaaaa-0000-4000-8000-000000000005"
    _working_implementer(store, worker, tid, agent_id)
    with sleeping_process() as proc:
        lane_start_recorder(worker, tid, agent_id)(proc.pid)
        lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
        assert "a lane of an earlier tick is still running; waiting" in lines
        task = store.row("task", tid)
        assert coord(task)["lane_wait"] is True
        assert parked(task, worker) is True
        assert "implementer" not in fake.launched

    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    agent = store.row("agent", agent_id)
    assert agent["status"] == "done"
    assert agent["note"] == "recovered: no live process"


def test_launch_refuses_an_open_lane_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes

    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=0
    )
    agent_id = "aaaaaaaa-0000-4000-8000-000000000003"
    _working_implementer(store, worker, tid, agent_id)
    task = store.row("task", tid)
    from copy import deepcopy

    before = deepcopy(store.row("task", tid))
    comments_before = {
        row["id"]
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    }
    monkeypatch.setattr(
        coordinator_lanes, "verify_checkout_identity", lambda *args, **kwargs: {}
    )

    with pytest.raises(
        LaneFailedError,
        match="a lane record of an earlier tick is still open",
    ):
        launch_lane(
            store,
            worker,
            task,
            role="implementer",
            vendor="grok",
            round_num=1,
            spec_body="Implement.",
            runner=fake,
            lane_runner=lambda *_args: pytest.fail("lane was started"),
        )
    assert store.row("task", tid) == before
    assert {
        row["id"]
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    } == comments_before


def test_stored_uncertain_flag_resumes_the_phase_of_the_newest_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, tid = _tick_to_implement(
        tmp_path, monkeypatch, lane_retries=1
    )
    task = store.row("task", tid)
    c = coord(task)
    c.update(
        phase="blocked",
        uncertain_lane=True,
        resume_phase="implement",
        blocker="uncertain prior lane outcome",
    )
    store.write(
        "task",
        "update",
        tid,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    store.write(
        "agent",
        "insert",
        "aaaaaaaa-0000-4000-8000-000000000004",
        {
            "id": "aaaaaaaa-0000-4000-8000-000000000004",
            "session_id": worker.session_id,
            "task_id": tid,
            "round": None,
            "role": "pr-reviewer-logic",
            "vendor": "codex",
            "stage": "review-2",
            "status": "done",
            "started_at": "2026-02-01T00:00:00Z",
        },
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)
    assert (
        "retry 1/1 of pr_gates_codex after lane outcome was uncertain; "
        "next attempt in 30s"
    ) in lines
    c = coord(store.row("task", tid))
    assert c["phase"] == "pr_gates_codex"
    assert "uncertain_lane" not in c
    assert "resume_phase" not in c
    assert "blocker" not in c


def test_saved_review_result_is_replayed_after_the_dead_sibling_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes
    from agent_cli.coordinator_runtime import advance_one
    from agent_cli.lane import LaneResult
    from test_coordinator import _codex_prelaunch_fixture

    tid = "aa000001-0000-4000-8000-000000000301"
    store, base_worker, task = _codex_prelaunch_fixture(tmp_path, monkeypatch, tid)
    worker = replace(base_worker, lane_retries=1)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    Path(coord(task)["worktree"]).mkdir(parents=True, exist_ok=True)
    head = str(coord(task)["head_sha"])
    quality_id = "aa000001-0000-4000-8000-000000000302"
    logic_id = "aa000001-0000-4000-8000-000000000303"
    common = {
        "session_id": worker.session_id,
        "task_id": tid,
        "round": None,
        "vendor": "codex",
        "provider": "codex",
        "model": "codex-model",
        "head_sha": head,
        "stage": "review-2",
        "status": "working",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "note": None,
    }
    store.write(
        "agent",
        "insert",
        quality_id,
        {
            **common,
            "id": quality_id,
            "role": "pr-reviewer-quality",
            "dimension": "quality",
        },
    )
    spool = coordinator_lanes._write_reviewer_spool(
        store,
        worker,
        task,
        {"id": quality_id},
        LaneResult(
            role="pr-reviewer-quality",
            vendor="codex",
            model="codex-model",
            status="complete",
            argv=[],
            returncode=0,
            stdout="STATUS: complete\nRESULT: approved\n",
            stderr="",
            config_dir=None,
            protocol_result="approved",
        ),
        round_num=None,
        head=head,
        dimension="quality",
        stage="review-2",
        output_file=None,
    )
    store.write(
        "agent",
        "insert",
        logic_id,
        {
            **common,
            "id": logic_id,
            "role": "pr-reviewer-logic",
            "dimension": "logic",
        },
    )

    lines = advance_one(
        store,
        worker,
        runner=fake,
        lane_runner=lambda *_args: pytest.fail("reviewer was started before retry"),
    )
    assert any(
        line.startswith(
            "retry 1/1 of pr_gates_codex after a lane left running by an "
            "earlier tick ended without a result"
        )
        for line in lines
    )
    assert store.row("agent", logic_id)["status"] == "done"
    assert store.row("agent", logic_id)["note"] == "recovered: no live process"
    assert store.row("agent", quality_id)["status"] == "working"
    assert spool.exists()

    _expire_retry(store, tid)
    starts: list[str] = []

    def reviewer(argv: list[str], stdin: str | None = None) -> Completed:
        material = " ".join(argv) + "\n" + (stdin or "")
        role = (
            "pr-reviewer-logic"
            if "pr-reviewer-logic" in material
            else "pr-reviewer-quality"
        )
        starts.append(role)
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            "STATUS: complete\nRESULT: approved\n", encoding="utf-8"
        )
        return Completed(0, "", "")

    advance_one(store, worker, runner=fake, lane_runner=reviewer)
    assert starts == ["pr-reviewer-logic"]
    assert store.row("agent", quality_id)["status"] == "done"
    saved = coord(store.row("task", tid))
    assert "uncertain_lane" not in saved
    assert saved["phase"] != "blocked"


def test_unknown_mergeability_retries_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _drive_to_ready_phase(
        tmp_path, monkeypatch, "readiness"
    )
    task = store.rows("task")[0]
    comments_before = len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    )
    fake.pr["mergeable"] = "UNKNOWN"
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.row("task", str(task["id"]))
    assert any(line.startswith("PR mergeability not yet computed on") for line in lines)
    assert coord(task)["phase"] == "readiness"
    assert "resume_phase" not in coord(task)
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == comments_before
    assert store.row(
        "checklist_item", registry_row_id(str(task["id"]), "pr_mergeable")
    ) is None

    fake.pr["mergeable"] = "MERGEABLE"
    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.row("task", str(task["id"]))
    assert coord(task)["phase"] == "formal_approve"
    assert store.row(
        "checklist_item", registry_row_id(str(task["id"]), "pr_mergeable")
    )["status"] == "ja"


def test_unknown_mergeability_retries_leave_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _drive_to_ready_phase(
        tmp_path, monkeypatch, "leave_draft"
    )
    fake.pr["mergeable"] = "UNKNOWN"
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert any(line.startswith("PR mergeability not yet computed on") for line in lines)
    assert coord(task)["phase"] == "leave_draft"
    assert "resume_phase" not in coord(task)
    assert fake.pr["isDraft"] is True


def test_pr_view_transport_error_retries_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _drive_to_ready_phase(
        tmp_path, monkeypatch, "readiness"
    )
    fake.pr_view_rc = 1
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert lines[0] == (
        f"PR metadata temporarily unavailable on {fake.head[:7]}; "
        "retrying readiness"
    )
    assert coord(task)["phase"] == "readiness"
    assert "resume_phase" not in coord(task)


def test_conflict_publishes_a_resumable_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _drive_to_ready_phase(
        tmp_path, monkeypatch, "readiness"
    )
    fake.pr["mergeable"] = "CONFLICTING"
    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    c = coord(task)
    evidence = (
        "pull request has merge conflicts with develop; resolve them on the "
        "branch or cancel, then reply"
    )
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "readiness"
    assert c["failed_check"] == "pr_mergeable"
    assert c["blocker"] == f"Check pr_mergeable failed: {evidence}"
    assert c.get("question_activity_id")
    assert any(
        "<!-- agent-coordinator:status:v1:conflict:" in str(comment.get("body"))
        for comment in fake.comments
    )
    row = store.row(
        "checklist_item", registry_row_id(str(task["id"]), "pr_mergeable")
    )
    assert row["status"] == "nein"


def test_conflict_resolved_after_a_reply_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _drive_to_ready_phase(
        tmp_path, monkeypatch, "readiness"
    )
    fake.pr["mergeable"] = "CONFLICTING"
    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert coord(task)["phase"] == "blocked"
    fake.pr["mergeable"] = "MERGEABLE"
    fake.comments.append(
        {
            "id": max(int(comment["id"]) for comment in fake.comments) + 1,
            "body": "The branch conflict is resolved.",
            "user": {"login": "human-owner"},
        }
    )

    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.row("task", str(task["id"]))
    assert coord(task)["phase"] == "readiness"
    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.row("task", str(task["id"]))
    assert coord(task)["phase"] == "formal_approve"
    assert "failed_check" not in coord(task)
    row = store.row(
        "checklist_item", registry_row_id(str(task["id"]), "pr_mergeable")
    )
    assert row["status"] == "ja"
