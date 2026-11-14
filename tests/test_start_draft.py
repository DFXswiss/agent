from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_cli import coordinator_git, coordinator_runtime
from agent_cli.coordinator import tick
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_git import (
    START_COMMIT_MESSAGE,
    close_pull_request,
    phase_checkout,
    sign_empty_commit,
)
from agent_cli.coordinator_runtime import (
    NO_CHANGE_CLOSE_BODY,
    advance_one,
    close_empty_draft,
)
from agent_cli.store import Store, StoreError
from test_coordinator import (
    _a38_external_review_env,
    _ready_side_env,
    _seed_ready_side_task,
)
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    patch_account_runners,
    patch_command_runner,
    seed_order_task,
    write_accounts,
)


NO_CHANGE = "STATUS: complete\nRESULT: no-change\nNothing to change.\n"
TID = "11111111-1111-4111-8111-111111111111"
OID = "22222222-2222-4222-8222-222222222222"
AID = str(uuid5(NAMESPACE_URL, "coordinator-test-close"))


class SimulatedCrash(BaseException):
    pass


def _env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    draft: bool = True,
):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    if draft:
        worker = replace(worker, draft_on_accept=True)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    return store, worker, fake, lane_runner(fake)


def _task(store: Store) -> dict:
    tasks = store.rows("task")
    assert len(tasks) == 1
    return tasks[0]


def _c(store: Store) -> dict:
    return _task(store)["payload"]["coordinator"]


def _drive(store: Store, worker, fake: FakeGh, lane, phase: str) -> None:
    for _ in range(8):
        if store.rows("task") and _c(store).get("phase") == phase:
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store).get("phase") == phase


def _save(store: Store, task: dict) -> None:
    store.write(
        "task",
        "update",
        task["id"],
        {key: value for key, value in task.items() if not key.startswith("_")},
    )


def test_draft_on_accept_opens_the_draft_before_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "start_draft")

    tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    commits = [command for command in fake.git_commands if "commit" in command]
    assert fake.empty_commits == 1
    assert fake.commit_used_S
    assert len(commits) == 1
    assert commits[0][-5:] == [
        "commit",
        "-S",
        "--allow-empty",
        "-m",
        START_COMMIT_MESSAGE,
    ]
    assert checkpoint["pr_number"] == 42
    assert checkpoint["start_commit"] == checkpoint["head_sha"]
    assert checkpoint["head_sha"] == checkpoint["published_head"] == fake.head
    assert "start_commit_pending" not in checkpoint
    assert checkpoint["phase"] == "implement"
    assert fake.launched == []


def test_issue_task_without_the_option_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch, draft=False)
    phases: list[str] = []
    for _ in range(8):
        tick(store, worker, runner=fake, lane_runner=lane)
        if store.rows("task"):
            phases.append(str(_c(store).get("phase")))
            if phases[-1] == "implement":
                break

    assert phases[-1] == "implement"
    assert "start_draft" not in phases
    assert "start_commit" not in _c(store)
    assert fake.empty_commits == 0
    assert not any(row.get("type") == "pr.open" for row in store.rows("activity"))


def test_checkout_of_an_order_ends_in_start_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    task = seed_order_task(store, worker, OID, phase="checkout")

    phase_checkout(store, worker, task, fake)

    assert _c(store)["phase"] == "start_draft"


def test_start_draft_stays_until_the_pull_request_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "start_draft")
    original = coordinator_git.execute_github
    monkeypatch.setattr(coordinator_git, "execute_github", lambda *_args, **_kwargs: [])

    tick(store, worker, runner=fake, lane_runner=lane)
    checkpoint = _c(store)
    assert checkpoint["phase"] == "start_draft"
    assert fake.empty_commits == 1
    assert checkpoint["start_commit"] == fake.head
    assert "pr_number" not in checkpoint

    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "start_draft"
    assert fake.empty_commits == 1

    monkeypatch.setattr(coordinator_git, "execute_github", original)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "implement"
    assert _c(store)["pr_number"] == 42
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "pr.open"]
    ) == 1


def test_start_draft_resumes_after_a_crash_between_commit_and_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "start_draft")
    task = _task(store)
    task["payload"]["coordinator"]["start_commit_pending"] = True
    _save(store, task)
    fake.commits_ahead = True
    fake.head = "c" * 40

    tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert fake.empty_commits == 0
    assert checkpoint["start_commit"] == "c" * 40
    assert "start_commit_pending" not in checkpoint
    assert checkpoint["pr_number"] == 42
    assert checkpoint["phase"] == "implement"


def test_implementer_commit_is_pushed_to_the_start_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "implement")
    fake.dirty = True

    tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert fake.launched == ["implementer"]
    assert checkpoint["head_sha"] != checkpoint["start_commit"]
    assert checkpoint["published_head"] == checkpoint["head_sha"]
    assert checkpoint["head_sha"] == fake.pr["headRefOid"]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "pr.open"]
    ) == 1
    assert checkpoint["phase"] == "inner_review"


def test_a_pull_request_head_that_lags_the_push_is_retried_without_a_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    worker = replace(worker, lane_retries=1)
    _drive(store, worker, fake, lane, "implement")
    fake.dirty = True
    real_git = fake._git
    pushed: list[str] = []

    def lagging_git(argv: list[str]):
        shown = fake.pr["headRefOid"]
        result = real_git(argv)
        if "push" in argv and not pushed:
            pushed.append(fake.head)
            fake.pr["headRefOid"] = shown
        return result

    monkeypatch.setattr(fake, "_git", lagging_git)

    lines = tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert (
        "retry 1/1 of implement after updated Draft head is not yet verified; "
        "next attempt in 30s"
    ) in lines
    assert checkpoint["phase"] == "implement"
    assert checkpoint["retry"]["reason"] == "updated Draft head is not yet verified"
    assert checkpoint["head_sha"] == checkpoint["pending_published_head"] == pushed[0]
    assert checkpoint["published_head"] == checkpoint["start_commit"]
    assert checkpoint.get("blocker") is None

    fake.pr["headRefOid"] = pushed[0]
    task = _task(store)
    task["payload"]["coordinator"]["retry"]["at"] = "2020-01-01T00:00:00Z"
    _save(store, task)
    tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert fake.launched == ["implementer"]
    assert checkpoint["phase"] == "inner_review"
    assert checkpoint["published_head"] == checkpoint["head_sha"] == pushed[0]
    assert "pending_published_head" not in checkpoint
    assert "retry" not in checkpoint
    assert checkpoint.get("blocker") is None
    bodies = [
        str(row["payload"].get("body") or "")
        for row in store.rows("activity")
        if isinstance(row.get("payload"), dict)
    ]
    assert not any("not yet verified" in body for body in bodies)


def test_no_change_on_the_start_commit_closes_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "implement")
    fake.model_outputs["implementer"] = NO_CHANGE

    tick(store, worker, runner=fake, lane_runner=lane)

    task = _task(store)
    checkpoint = _c(store)
    pr_comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and (row.get("payload") or {}).get("target") == "pr"
    ]
    assert len(pr_comments) == 1
    assert pr_comments[0]["payload"]["body"] == NO_CHANGE_CLOSE_BODY
    assert fake.pr_comments[0]["body"].startswith(NO_CHANGE_CLOSE_BODY)
    assert fake.close_calls == 1
    assert fake.pr["state"] == "CLOSED"
    assert task["state"] == "failed"
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["failed_kind"] == "no-change"
    assert checkpoint["blocker"] == "implementation lane reported no change"
    for key in ("resume_phase", "question_activity_id", "closing_draft", "lane_outcome"):
        assert key not in checkpoint
    rounds = [
        row
        for row in store.rows("task_round")
        if row.get("task_id") == task["id"] and row.get("round") == 1
    ]
    assert len(rounds) == 1
    assert rounds[0]["implementer_verdict"] == "blocked"
    assert rounds[0]["finished_at"]
    assert any(
        "agent-coordinator:status:v1:no-change:" in row["body"]
        for row in fake.comments
    )
    launched = list(fake.launched)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.launched == launched


def test_no_change_close_resumes_after_a_crash_before_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "implement")
    fake.model_outputs["implementer"] = NO_CHANGE
    original = coordinator_runtime.close_pull_request

    def crash(*_args, **_kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(coordinator_runtime, "close_pull_request", crash)
    with pytest.raises(SimulatedCrash):
        tick(store, worker, runner=fake, lane_runner=lane)

    task = _task(store)
    checkpoint = _c(store)
    assert checkpoint["closing_draft"] == "no-change"
    assert checkpoint["phase"] == "implement"
    assert checkpoint["lane_outcome"]["applied"] is False
    assert task["state"] != "failed"

    monkeypatch.setattr(coordinator_runtime, "close_pull_request", original)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _task(store)["state"] == "failed"
    assert _c(store)["failed_kind"] == "no-change"
    assert fake.close_calls == 1
    assert fake.launched == ["implementer"]


def test_failed_close_is_repeated_on_the_next_visit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "implement")
    fake.close_rc = 1
    fake.model_outputs["implementer"] = NO_CHANGE

    lines = tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert "blocked: close refused" in lines
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "implement"
    assert checkpoint["closing_draft"] == "no-change"
    assert _task(store)["state"] != "failed"
    assert fake.pr["state"] == "OPEN"

    fake.close_rc = 0
    tick(store, worker, runner=fake, lane_runner=lane)
    checkpoint = _c(store)
    assert _task(store)["state"] == "failed"
    assert checkpoint["failed_kind"] == "no-change"
    assert fake.close_calls == 2
    assert len(fake.pr_comments) == 1
    assert "resume_phase" not in checkpoint
    assert "question_activity_id" not in checkpoint
    assert fake.launched == ["implementer"]


@pytest.mark.parametrize("worker_kind", ["plain", "a38"])
def test_no_change_close_resumes_after_the_pull_request_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_kind: str,
) -> None:
    if worker_kind == "plain":
        store, worker, fake = _ready_side_env(
            tmp_path, monkeypatch, patch_github=True
        )
        task = _seed_ready_side_task(
            store,
            worker,
            fake,
            tid=TID,
            phase="implement",
            checklist_codex={},
        )
    else:
        store, worker, fake, task = _a38_external_review_env(
            tmp_path, monkeypatch, task_id=TID
        )
        task["payload"]["coordinator"]["phase"] = "implement"
    checkpoint = task["payload"]["coordinator"]
    checkpoint["closing_draft"] = "no-change"
    checkpoint["start_commit"] = checkpoint["head_sha"]
    task["state"] = "implementing"
    _save(store, task)
    fake.pr["state"] = "CLOSED"

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    task = store.row("task", TID)
    assert task is not None
    checkpoint = task["payload"]["coordinator"]
    assert lines[0] == "no change on the start commit; draft closed"
    assert task["state"] == "failed"
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["failed_kind"] == "no-change"
    assert fake.close_calls == 0
    assert not any(
        row.get("type") == "comment.post"
        and (row.get("payload") or {}).get("target") == "pr"
        for row in store.rows("activity")
    )
    assert not any(
        "status:v1:closed-unmerged" in str((row.get("payload") or {}).get("body") or "")
        for row in store.rows("activity")
    )


def test_no_change_with_real_commits_is_not_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    _drive(store, worker, fake, lane, "implement")
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "inner_review"

    fake.model_outputs["reviewer"] = (
        "STATUS: complete\nRESULT: rejected\nfix the widget\n"
    )
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "implement"
    fake.model_outputs["implementer"] = (
        "STATUS: complete\nRESULT: no-change\n"
        "SUMMARY_EN: Nothing further to change.\n"
        "SUMMARY_DE: Keine weitere Änderung nötig.\n"
    )

    tick(store, worker, runner=fake, lane_runner=lane)

    checkpoint = _c(store)
    assert fake.close_calls == 0
    assert fake.pr["state"] == "OPEN"
    assert "closing_draft" not in checkpoint
    assert checkpoint["phase"] == "inner_review"


@pytest.mark.parametrize("review_md", [None, "# Review\n"])
def test_no_change_without_a_commit_keeps_the_summaries_and_closes_its_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_md: str | None
) -> None:
    store, worker, fake, lane = _env(tmp_path, monkeypatch)
    fake.review_md = review_md
    fake.model_outputs["implementer"] = (
        "STATUS: complete\nRESULT: done\n"
        "SUMMARY_EN: Correct widget initialization.\n"
        "SUMMARY_DE: Widget-Initialisierung korrigiert.\n"
        "REUSED: `src/pay.ts:40` existing gift invoice\n"
        "ADDED: `src/habit.ts:12` member habit route\n"
    )
    _drive(store, worker, fake, lane, "implement")
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "inner_review"
    head = _c(store)["head_sha"]
    reused = _task(store).get("reused_lines")
    edits = list(fake.pr_edits)

    fake.model_outputs["reviewer"] = (
        "STATUS: complete\nRESULT: rejected\nfix the widget\n"
    )
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _c(store)["phase"] == "implement"
    fake.model_outputs["implementer"] = NO_CHANGE

    lines = tick(store, worker, runner=fake, lane_runner=lane)

    task = _task(store)
    checkpoint = _c(store)
    assert "implementer done round=2" in lines
    assert checkpoint["phase"] == "inner_review"
    assert checkpoint.get("blocker") is None
    assert checkpoint["head_sha"] == head
    assert task["change_summary_en"] == "Correct widget initialization."
    assert task["change_summary_de"] == "Widget-Initialisierung korrigiert."
    assert task.get("reused_lines") == reused
    assert fake.pr_edits == edits
    rounds = {
        row["round"]: row
        for row in store.rows("task_round")
        if row["task_id"] == task["id"]
    }
    assert rounds[2]["implementer_verdict"] == "done"


def test_no_change_close_keeps_the_live_task_when_the_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    task = {
        "id": TID,
        "session_id": worker.session_id,
        "workflow": "implement",
        "title": "worker-s - Fix widget",
        "repo": "example/project",
        "ref": None,
        "payload": {
            "coordinator": {
                "phase": "implement",
                "source": {"repo": "example/project", "number": 7},
                "closing_draft": "no-change",
                "start_commit": "c" * 40,
                "head_sha": "c" * 40,
                "lane_outcome": {"role": "implementer", "applied": False},
            }
        },
        "state": "implementing",
        "current_round": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "change_summary_en": None,
        "change_summary_de": None,
    }
    store.write("task", "insert", TID, task)
    round_id = "33333333-3333-4333-8333-333333333333"
    store.write(
        "task_round",
        "insert",
        round_id,
        {
            "id": round_id,
            "task_id": TID,
            "round": 1,
            "implementer_verdict": None,
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )
    live_task = store.row("task", TID)
    assert live_task is not None
    before_live = copy.deepcopy(live_task)
    before_task = store.row("task", TID)
    before_round = store.row("task_round", round_id)

    def fail_save(*_args, **_kwargs):
        raise StoreError("save failed")

    def fail_runner(_argv):
        pytest.fail("runner must not be called")

    monkeypatch.setattr(coordinator_runtime, "save_task", fail_save)
    with pytest.raises(StoreError, match="save failed"):
        close_empty_draft(store, worker, live_task, fail_runner)

    assert live_task == before_live
    assert store.row("task", TID) == before_task
    assert store.row("task_round", round_id) == before_round


def test_no_change_close_of_an_order_posts_no_status_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    task = seed_order_task(
        store,
        worker,
        OID,
        phase="implement",
        pr_number=42,
        closing_draft="no-change",
        start_commit="c" * 40,
        head_sha="c" * 40,
    )

    lines = close_empty_draft(store, worker, task, fake)

    stored = store.row("task", OID)
    assert stored is not None
    assert lines == ["no change on the start commit; draft closed"]
    assert fake.close_calls == 1
    assert len(fake.pr_comments) == 1
    assert fake.pr_comments[0]["body"].startswith(NO_CHANGE_CLOSE_BODY)
    assert fake.comments == []
    assert stored["state"] == "failed"
    assert stored["payload"]["coordinator"]["failed_kind"] == "no-change"


def test_sign_empty_commit_requires_the_base_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    with pytest.raises(CoordinatorError, match="unchanged base head"):
        sign_empty_commit(
            store,
            worker,
            fake,
            str(tmp_path),
            START_COMMIT_MESSAGE,
            base_sha="b" * 40,
        )
    assert fake.empty_commits == 0

    fake.dirty = True
    with pytest.raises(CoordinatorError, match="worktree is not clean"):
        sign_empty_commit(
            store,
            worker,
            fake,
            str(tmp_path),
            START_COMMIT_MESSAGE,
            base_sha="a" * 40,
        )


def test_close_pull_request_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=TID,
        phase="implement",
        checklist_codex={},
    )

    assert close_pull_request(
        store, worker, task, fake, activity_id=AID, body="Closing."
    ) == "CLOSED"
    assert fake.close_calls == 1
    assert len(fake.pr_comments) == 1
    assert fake.pr_comments[0]["body"].startswith("Closing.")

    assert close_pull_request(
        store, worker, task, fake, activity_id=AID, body="Closing."
    ) == "CLOSED"
    assert fake.close_calls == 1
    assert len(fake.pr_comments) == 1


def test_close_pull_request_reports_a_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=TID,
        phase="implement",
        checklist_codex={},
    )
    fake.pr["state"] = "MERGED"

    assert close_pull_request(
        store, worker, task, fake, activity_id=AID, body="Closing."
    ) == "MERGED"
    assert store.row("activity", AID) is None
    assert fake.close_calls == 0


def test_close_pull_request_needs_the_published_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=TID,
        phase="implement",
        checklist_codex={},
    )
    monkeypatch.setattr(coordinator_git, "execute_github", lambda *_args, **_kwargs: [])

    with pytest.raises(CoordinatorError, match="close comment was not verified published"):
        close_pull_request(
            store, worker, task, fake, activity_id=AID, body="Closing."
        )
    assert fake.close_calls == 0
    assert fake.pr["state"] == "OPEN"


def test_close_pull_request_without_a_pull_request(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    task = {
        "id": TID,
        "payload": {"coordinator": {"source": {"repo": "example/project", "number": 7}}},
    }

    def fail_runner(_argv):
        pytest.fail("runner must not be called")

    with pytest.raises(CoordinatorError, match="no pull request to close"):
        close_pull_request(
            store,
            worker,
            task,
            fail_runner,
            activity_id=AID,
            body="Closing.",
        )
