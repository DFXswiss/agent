from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli import coordinator_checks, coordinator_conflicts, coordinator_runtime
from agent_cli import preconditions
from agent_cli.conflict_mandate import MandateRun
from agent_cli.coordinator_checks import (
    CheckContext,
    TaskFacts,
    evaluate_action,
    read_fact,
    registry_row_id,
    remedy_running,
)
from agent_cli.coordinator_common import (
    CoordinatorError,
    coord,
    evidence_base_sha,
    save_task,
)
from agent_cli.coordinator_config import ConflictRemedyConfig
from agent_cli.coordinator_conflicts import (
    remedy_state,
    resolve_conflict_mandate,
)
from agent_cli.coordinator_git import _write_marker, adopt_published_head
from agent_cli.coordinator_orders import own_order_events, reconcile_order_events
from agent_cli.preconditions import CHECKS, Action, ISSUE, ORDER, Requirement
from agent_cli.store import utcnow
from test_coordinator import seed_task
from test_coordinator_support import (
    insert_order_intent,
    seed_order_task,
)
from test_orders_runtime import OID, checkpoint, prepare_orders


REMEDY = ConflictRemedyConfig(("/operator/conflict-checks",), 5)
H = "a" * 40
R = "d" * 40
B1 = "e" * 40


def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any):
    remedy = overrides.pop("conflict_remedy", REMEDY)
    store, worker, fake, lane = prepare_orders(
        tmp_path,
        monkeypatch,
        conflict_remedy=remedy,
        **overrides,
    )
    fake.issues = []
    return store, worker, fake, lane


def seed(store, worker, fake, *, phase: str = "readiness", **extra: Any):
    wt = worker.workspace_root / OID
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)
    fake.remotes = {
        "origin": "https://github.com/example/project.git",
        "publication": "https://github.com/example/project.git",
    }
    fake.branch = "task-11111111"
    fake.branches.add(fake.branch)
    _write_marker(
        worker,
        OID,
        {
            "phase": "ready",
            "task_id": OID,
            "session_id": worker.session_id,
            "target_repo": "example/project",
            "publication_repo": "example/project",
            "branch": fake.branch,
            "base": "develop",
            "base_sha": fake.base,
            "worktree": str(wt),
            "worker_login": "worker-bot",
        },
    )
    values = {
        "worktree": str(wt),
        "branch": fake.branch,
        "base_sha": fake.base,
        "head_sha": fake.head,
        "published_head": fake.head,
        "pr_number": 42,
    }
    values.update(extra)
    task = seed_order_task(store, worker, OID, phase=phase, **values)
    fake.pr["mergeable"] = "CONFLICTING"
    return task


def prefix(store) -> str:
    source = checkpoint(store, OID)["source"]
    return "pull request has merge conflicts with " + str(
        source.get("base") or "the base branch"
    )


def title(store) -> str:
    return checkpoint(store, OID)["source"]["title"]


def who(store) -> str:
    return checkpoint(store, OID)["source"]["submitted_by"]


def short_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        preconditions.ACTIONS,
        "readiness",
        Action(
            "readiness",
            ("readiness",),
            (
                Requirement("order_active"),
                Requirement("pr_mergeable", (ORDER, ISSUE)),
            ),
        ),
    )


def stub(monkeypatch: pytest.MonkeyPatch, *runs: Any):
    calls: list[dict[str, Any]] = []
    discards: list[tuple[Any, Any]] = []
    queue = list(runs)

    def run(
        store,
        worker,
        request,
        *,
        mandated_head,
        runner,
        lane_runner=None,
    ):
        del store, runner, lane_runner
        call = {
            "worker": worker,
            "request": request,
            "mandated_head": mandated_head,
        }
        calls.append(call)
        if not queue:
            raise AssertionError("unexpected conflict remedy run")
        selected = queue.pop(0) if len(queue) > 1 else queue[0]
        return selected(call) if callable(selected) else selected

    def discard(worker, request):
        discards.append((worker, request))

    monkeypatch.setattr(coordinator_conflicts, "run_script_mandate", run)
    monkeypatch.setattr(coordinator_conflicts, "discard_script_mandate", discard)
    return calls, discards


def pushed(fake):
    def finish(call):
        del call
        fake.published_head = R
        fake.pr["headRefOid"] = R
        fake.extra_ancestors.add(B1)
        return MandateRun(
            "finished",
            old_head=H,
            new_head=R,
            base_sha=B1,
            lines=("pushed",),
        )

    return finish


def gate(store, worker, fake, lane):
    return evaluate_action(
        store,
        worker,
        store.row("task", OID),
        fake,
        lane,
        action="readiness",
    )


def fact(store, key: str):
    return store.row("checklist_item", registry_row_id(OID, key))


@pytest.mark.no_pg
def test_registered_remedies_are_declared() -> None:
    assert set(coordinator_checks.REMEDIES) == {
        check.remedy for check in CHECKS.values() if check.remedy
    }


@pytest.mark.no_pg
def test_evidence_base_follows_a_rebase() -> None:
    assert evidence_base_sha(
        {"base_sha": "b" * 40, "rebase_base_sha": B1}, a38=False
    ) == B1
    assert evidence_base_sha({"base_sha": "b" * 40}, a38=False) == "b" * 40
    assert evidence_base_sha(
        {"base_sha": "b" * 40, "rebase_base_sha": "x"}, a38=False
    ) == "b" * 40
    assert evidence_base_sha(
        {
            "a38": {"base_sha": "c" * 40},
            "base_sha": "b" * 40,
            "rebase_base_sha": B1,
        },
        a38=True,
    ) == "c" * 40


def test_order_conflict_starts_the_remedy_without_a_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch, pushed(fake))
    lines = gate(store, worker, fake, lane)
    assert lines[0] == (
        "conflict resolution 1 started for example/project#42 on aaaaaaa"
    )
    assert lines[-1] == (
        "conflict resolution 1 rebased aaaaaaa; adopted head ddddddd; "
        "routing to tests"
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["mandated_head"] == H
    assert call["request"].comment_id == 1
    assert call["request"].created_at == remedy_state(
        store.row("task", OID)
    )["mandated_at"]
    cw = call["worker"]
    assert cw.workspace_root == (
        worker.workspace_root
        / ".coordinator-control"
        / OID
        / "conflict-remedy"
    )
    assert cw.repositories["example/project"].check_argv == (
        "/operator/conflict-checks",
    )
    assert cw.max_rebase_steps == 5
    assert cw.request_logins == ()
    assert cw.lane_timeout == worker.lane_timeout
    saved = checkpoint(store, OID)
    assert saved["phase"] == "tests"
    assert saved["head_sha"] == saved["published_head"] == R
    assert saved["rebase_base_sha"] == B1
    assert saved["base_sha"] == fake.base
    assert saved["remedies"]["pr_mergeable"] | {
        "state": "finished",
        "result": "resolved",
        "request": 1,
        "starts": 1,
        "new_head": R,
    } == saved["remedies"]["pr_mergeable"]
    assert fake.resets == [R]
    mandate = fact(store, "conflict_mandate")
    assert mandate["status"] == "ja"
    assert mandate["head_sha"] == H
    assert mandate["source"] == "script"
    assert mandate["evidence"] == (
        f"order {OID} submitted by {who(store)}: conflict resolution 1 "
        "of pull request #42 on aaaaaaa"
    )
    assert not [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ]
    reconcile_order_events(store, worker, store.row("task", OID))
    assert not [
        row
        for row in own_order_events(store)
        if row["payload"]["kind"] in ("question", "blocked")
    ]
    store.close()


def test_marker_and_fact_are_saved_before_the_request_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)

    def inspect(call):
        del call
        marker = remedy_state(store.row("task", OID))
        assert marker["state"] == "running"
        assert marker["request"] == 1
        assert marker["head"] == H
        assert fact(store, "pr_mergeable")["evidence"] == (
            prefix(store) + "; conflict resolution 1 is running"
        )
        return MandateRun("pending", reason="transient: x", decided=True)

    stub(monkeypatch, inspect)
    gate(store, worker, fake, lane)
    store.close()


def test_evidence_is_dropped_and_facts_read_pending_on_the_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    task = seed(
        store,
        worker,
        fake,
        evidence={
            "tests_pass": True,
            "tests_head": H,
            "ci_green": True,
            "ci_head": H,
            "formal_head": H,
            "ready_head": H,
        },
    )
    original_round = task["current_round"]
    review_id = registry_row_id(OID, "review_2")
    store.write(
        "checklist_item",
        "insert",
        review_id,
        {
            "id": review_id,
            "task_id": OID,
            "key": "review_2",
            "status": "ja",
            "evidence": "human review",
            "source": "human",
            "updated_at": utcnow(),
            "registry": True,
            "resolver": "review_2",
            "head_sha": H,
        },
    )
    short_readiness(monkeypatch)
    stub(monkeypatch, pushed(fake))
    gate(store, worker, fake, lane)
    saved = store.row("task", OID)
    evidence = coord(saved)["evidence"]
    for key in ("tests_pass", "ci_green", "formal_head", "ready_head"):
        assert key not in evidence
    facts = TaskFacts(store, saved)
    for key in (
        "tests_green",
        "ci_green",
        "pr_mergeable",
        "conflict_mandate",
        "head_signed_clean",
    ):
        assert read_fact(facts, saved, CHECKS[key]).status == "pending"
    assert read_fact(facts, saved, CHECKS["review_2"]).status == "ja"
    assert saved["current_round"] == original_round
    store.close()


def test_running_remedy_continues_before_mergeability_is_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(
        store,
        worker,
        fake,
        remedies={
            "pr_mergeable": {
                "name": "conflict_resolution",
                "state": "running",
                "request": 1,
                "starts": 1,
                "replies": 0,
                "blocks": 0,
                "head": H,
                "mandated_at": "2026-01-01T00:00:00Z",
            }
        },
    )
    short_readiness(monkeypatch)
    fake.pr_view_rc = 1
    calls, _ = stub(monkeypatch, pushed(fake))
    lines = gate(store, worker, fake, lane)
    assert lines[-1].endswith("routing to tests")
    assert checkpoint(store, OID)["phase"] == "tests"
    assert calls[0]["request"].comment_id == 1
    assert calls[0]["request"].created_at == "2026-01-01T00:00:00Z"
    assert not remedy_running(store.row("task", OID), CHECKS["pr_mergeable"])
    store.close()


def test_remedy_blocker_becomes_a_question_of_kind_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    stub(
        monkeypatch,
        MandateRun("blocked", reason="configured check failed:\nboom"),
    )
    gate(store, worker, fake, lane)
    saved = checkpoint(store, OID)
    evidence = (
        prefix(store)
        + "; conflict resolution 1 ended with a blocker: configured check failed: boom"
    )
    assert saved["phase"] == "blocked"
    assert saved["resume_phase"] == "readiness"
    assert saved["failed_check"] == "pr_mergeable"
    assert saved["blocker"] == "Check pr_mergeable failed: " + evidence
    assert saved["question_text"].splitlines() == [
        f'Check pr_mergeable failed for order "{title(store)}": {evidence}',
        f"Run it again with: agent order reply --id {OID} --body-file PATH",
        f"Otherwise: agent order cancel --id {OID} --reason TEXT",
    ]
    comments = [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    assert comments[0]["payload"]["target"] == "pr"
    assert comments[0]["payload"]["body"].startswith(
        "Blocked: Check pr_mergeable failed for order"
    )
    assert "<!-- agent-coordinator:status:v1:conflict:" in comments[0]["payload"]["body"]
    assert saved["question_activity_id"] == comments[0]["id"]
    assert saved["remedies"]["pr_mergeable"]["state"] == "blocked"
    assert saved["remedies"]["pr_mergeable"]["blocks"] == 1
    mergeable = fact(store, "pr_mergeable")
    assert mergeable["status"] == "nein"
    assert mergeable["evidence"] == evidence
    reconcile_order_events(store, worker, store.row("task", OID))
    questions = [
        row for row in own_order_events(store) if row["payload"]["kind"] == "question"
    ]
    assert len(questions) == 1
    payload = questions[0]["payload"]
    assert payload["wake"] is True
    assert payload["check"] == "pr_mergeable"
    assert payload["answers"] == ["reply", "cancel"]
    assert payload["digest"] == saved["question_text"]
    assert not [
        row for row in own_order_events(store) if row["payload"]["kind"] == "blocked"
    ]
    store.close()


def test_reply_starts_a_new_request_with_a_fresh_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    stub(monkeypatch, MandateRun("blocked", reason="configured check failed"))
    gate(store, worker, fake, lane)
    reconcile_order_events(store, worker, store.row("task", OID))
    question = [
        row for row in own_order_events(store) if row["payload"]["kind"] == "question"
    ][0]
    insert_order_intent(
        store,
        worker,
        OID,
        "reply",
        body="again",
        answers=question["id"],
    )
    coordinator_runtime.advance_one(
        store, worker, runner=fake, lane_runner=lane
    )
    assert checkpoint(store, OID)["phase"] == "readiness"
    calls, _ = stub(monkeypatch, pushed(fake))
    gate(store, worker, fake, lane)
    assert calls[0]["request"].comment_id == 2
    marker = checkpoint(store, OID)["remedies"]["pr_mergeable"]
    assert marker["request"] == 2
    assert marker["starts"] == 1
    assert marker["replies"] == 1
    assert checkpoint(store, OID)["phase"] == "tests"
    store.close()


def test_attempt_budget_ends_in_a_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(
        tmp_path,
        monkeypatch,
        conflict_remedy=replace(REMEDY, max_attempts=1),
    )
    seed(
        store,
        worker,
        fake,
        remedies={
            "pr_mergeable": {
                "name": "conflict_resolution",
                "state": "finished",
                "result": "resolved",
                "request": 1,
                "starts": 1,
                "replies": 0,
                "blocks": 0,
                "head": "c" * 40,
            }
        },
    )
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch)
    gate(store, worker, fake, lane)
    assert calls == []
    mandate = fact(store, "conflict_mandate")
    assert mandate["status"] == "nein"
    assert mandate["evidence"] == (
        "conflict resolution ran 1 times without a human decision"
    )
    assert checkpoint(store, OID)["blocker"] == (
        "Check pr_mergeable failed: "
        + prefix(store)
        + "; conflict resolution did not start: conflict resolution ran 1 "
        "times without a human decision"
    )
    store.close()


def test_worker_without_the_key_asks_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(
        tmp_path, monkeypatch, conflict_remedy=None
    )
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch)
    gate(store, worker, fake, lane)
    assert calls == []
    assert checkpoint(store, OID)["blocker"].endswith(
        "conflict resolution did not start: conflict resolution is not "
        "configured for this worker"
    )
    reconcile_order_events(store, worker, store.row("task", OID))
    question = [
        row for row in own_order_events(store) if row["payload"]["kind"] == "question"
    ][0]
    assert question["payload"]["answers"] == ["reply", "cancel"]
    store.close()


def test_unpublished_head_is_not_rebased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake, published_head="c" * 40)
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch)
    gate(store, worker, fake, lane)
    assert calls == []
    assert checkpoint(store, OID)["blocker"].endswith(
        "conflict resolution did not start: check head_published is pending"
    )
    store.close()


def test_open_outcome_retries_under_the_lane_retry_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch, lane_retries=1)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    pending = MandateRun(
        "pending", reason="transient: GitHub mergeable state is still unknown"
    )
    calls, discards = stub(monkeypatch, pending)
    first = gate(store, worker, fake, lane)
    assert any(line.startswith("retry 1/1 of readiness") for line in first)
    assert checkpoint(store, OID)["remedies"]["pr_mergeable"]["state"] == "running"
    assert coordinator_runtime.parked(store.row("task", OID), worker)
    task = store.row("task", OID)
    coord(task)["retry"]["at"] = "2000-01-01T00:00:00Z"
    save_task(store, task)
    second = gate(store, worker, fake, lane)
    assert any("did not complete after 2 attempts" in line for line in second)
    assert len(discards) == 1
    assert len(calls) == 2
    assert all(call["request"].comment_id == 1 for call in calls)
    store.close()


def test_decided_outcome_is_retried_without_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    calls, discards = stub(
        monkeypatch,
        MandateRun(
            "pending", reason="result comment not posted yet: x", decided=True
        ),
    )
    for _ in range(3):
        lines = gate(store, worker, fake, lane)
        assert lines[-1] == (
            "conflict resolution 1 is finishing on aaaaaaa; retrying readiness"
        )
    saved = checkpoint(store, OID)
    assert "retry" not in saved
    assert saved["phase"] == "readiness"
    assert saved["remedies"]["pr_mergeable"]["state"] == "running"
    assert len(calls) == 3
    assert discards == []
    store.close()


def test_finished_without_a_push_returns_to_the_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    stub(monkeypatch, MandateRun("finished"))
    fake.published_head = H
    lines = gate(store, worker, fake, lane)
    assert lines[-1] == (
        "conflict resolution 1 found no conflicts on aaaaaaa; retrying readiness"
    )
    saved = checkpoint(store, OID)
    assert saved["phase"] == "readiness"
    assert saved["head_sha"] == H
    assert saved["remedies"]["pr_mergeable"]["result"] == "no-conflicts"
    assert fake.resets == []
    store.close()


def test_failed_adoption_asks_and_is_tried_again_when_the_order_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch, pushed(fake))
    with monkeypatch.context() as patch:
        patch.setattr(
            coordinator_conflicts,
            "adopt_published_head",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                CoordinatorError("fetch published head failed")
            ),
        )
        gate(store, worker, fake, lane)
    saved = checkpoint(store, OID)
    assert saved["blocker"].endswith("fetch published head failed")
    assert saved["remedies"]["pr_mergeable"]["state"] == "running"
    assert saved["head_sha"] == H
    task = store.row("task", OID)
    cp = coord(task)
    cp["phase"] = "readiness"
    cp.pop("resume_phase", None)
    save_task(store, task)
    fake.pr["mergeable"] = "MERGEABLE"
    gate(store, worker, fake, lane)
    assert checkpoint(store, OID)["phase"] == "tests"
    assert checkpoint(store, OID)["head_sha"] == R
    assert len(calls) == 2
    assert all(call["request"].comment_id == 1 for call in calls)
    assert fact(store, "pr_mergeable")["status"] == "nein"
    store.close()


def test_foreign_published_head_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(store, worker, fake)
    short_readiness(monkeypatch)
    stub(
        monkeypatch,
        MandateRun("finished", old_head=H, new_head=R, base_sha=B1),
    )
    fake.published_head = "f" * 40
    gate(store, worker, fake, lane)
    assert "published head changed outside" in checkpoint(store, OID)["blocker"]
    assert fake.resets == []
    assert checkpoint(store, OID)["head_sha"] == H
    store.close()


def test_adopt_published_head_returns_none_when_nothing_was_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    task = seed(store, worker, fake)
    fake.published_head = H
    assert adopt_published_head(
        store,
        worker,
        task,
        fake,
        expected_old=H,
        expected_new=None,
        base_sha=None,
    ) is None
    assert fake.resets == []
    store.close()


def test_adopt_published_head_resets_to_the_pushed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    task = seed(store, worker, fake)
    stored = store.row("task", OID)
    fake.published_head = R
    fake.extra_ancestors.add(B1)
    assert adopt_published_head(
        store,
        worker,
        task,
        fake,
        expected_old=H,
        expected_new=R,
        base_sha=B1,
    ) == R
    assert fake.resets == [R]
    assert adopt_published_head(
        store,
        worker,
        task,
        fake,
        expected_old=H,
        expected_new=R,
        base_sha=B1,
    ) == R
    assert fake.resets == [R]
    assert store.row("task", OID) == stored
    store.close()


def test_adopt_published_head_refuses_a_head_off_the_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    task = seed(store, worker, fake)
    fake.published_head = R
    with pytest.raises(CoordinatorError):
        adopt_published_head(
            store,
            worker,
            task,
            fake,
            expected_old=H,
            expected_new=R,
            base_sha=B1,
        )
    assert fake.resets == []
    store.close()


def test_adopt_published_head_refuses_a_dirty_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    task = seed(store, worker, fake)
    fake.dirty = True
    with pytest.raises(CoordinatorError, match="worktree is not clean"):
        adopt_published_head(
            store,
            worker,
            task,
            fake,
            expected_old=H,
            expected_new=R,
            base_sha=None,
        )
    assert not any("fetch" in command for command in fake.git_commands)
    store.close()


def test_conflict_mandate_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    task = seed(store, worker, fake)

    def resolve(task_value, worker_value=worker):
        return resolve_conflict_mandate(
            CheckContext(
                store,
                worker_value,
                task_value,
                fake,
                None,
                CHECKS["conflict_mandate"],
                "resolve_conflicts",
            )
        )

    assert resolve(task).status == "ja"
    assert resolve(task, replace(worker, conflict_remedy=None)).evidence == (
        "conflict resolution is not configured for this worker"
    )
    blocked = store.row("task", OID)
    coord(blocked)["remedies"] = {
        "pr_mergeable": {
            "state": "blocked",
            "request": 2,
            "starts": 2,
            "replies": 0,
        }
    }
    assert resolve(blocked).evidence == (
        "conflict resolution ran 2 times without a human decision"
    )
    coord(blocked)["authorized_replies"] = ["go on"]
    assert "conflict resolution 3" in resolve(blocked).evidence
    coord(blocked)["remedies"]["pr_mergeable"].update(
        state="running", head=H
    )
    assert "conflict resolution 2" in resolve(blocked).evidence
    store.close()


def test_issue_task_with_a_conflict_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    issue_id = "22222222-2222-4222-8222-222222222222"
    task = {
        "id": issue_id,
        "session_id": worker.session_id,
        "workflow": "implement",
        "title": "Fix",
        "repo": "example/project",
        "state": "open",
        "current_round": 0,
        "models": {"first": "grok", "second": "codex"},
        "payload": {
            "coordinator": {
                "phase": "readiness",
                "source": {
                    "repo": "example/project",
                    "number": 7,
                    "base": "develop",
                },
                "pr_number": 42,
                "head_sha": H,
            }
        },
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    seed_task(store, worker, issue_id, task)
    short_readiness(monkeypatch)
    calls, _ = stub(monkeypatch)
    fake.pr["mergeable"] = "CONFLICTING"
    evaluate_action(
        store,
        worker,
        store.row("task", issue_id),
        fake,
        lane,
        action="readiness",
    )
    saved = coord(store.row("task", issue_id))
    assert saved["blocker"].startswith(
        "Check pr_mergeable failed: pull request has merge conflicts with "
    )
    assert saved["blocker"].endswith(
        "; resolve them on the branch or cancel, then reply"
    )
    assert calls == []
    assert "remedies" not in saved
    assert "question_text" not in saved
    assert store.row(
        "checklist_item", registry_row_id(issue_id, "conflict_mandate")
    ) is None
    store.close()


def test_cancel_ends_an_order_with_a_running_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = env(tmp_path, monkeypatch)
    seed(
        store,
        worker,
        fake,
        remedies={
            "pr_mergeable": {
                "name": "conflict_resolution",
                "state": "running",
                "request": 1,
                "starts": 1,
                "replies": 0,
                "blocks": 0,
                "head": H,
                "mandated_at": "2026-01-01T00:00:00Z",
            }
        },
    )
    calls, _ = stub(monkeypatch)
    insert_order_intent(
        store, worker, OID, "cancel", reason="not needed"
    )
    coordinator_runtime.advance_one(
        store, worker, runner=fake, lane_runner=lane
    )
    saved = store.row("task", OID)
    assert saved["state"] == "failed"
    assert coord(saved)["failed_kind"] == "cancelled"
    assert calls == []
    store.close()


def test_preflight_requires_the_conflicts_skill_for_a_remedy_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, _lane = env(tmp_path, monkeypatch)
    with pytest.raises(
        CoordinatorError, match="worker session missing skill conflicts"
    ):
        coordinator_runtime.preflight_worker(store, worker, fake)
    session = store.row("session", worker.session_id)
    session["skills"] = ["spine", "review-loop", "pr-review", "conflicts"]
    store.write(
        "session",
        "update",
        worker.session_id,
        {key: value for key, value in session.items() if not key.startswith("_")},
    )
    coordinator_runtime.preflight_worker(store, worker, fake)
    session["skills"] = ["spine", "review-loop", "pr-review"]
    store.write(
        "session",
        "update",
        worker.session_id,
        {key: value for key, value in session.items() if not key.startswith("_")},
    )
    coordinator_runtime.preflight_worker(
        store, replace(worker, conflict_remedy=None), fake
    )
    store.close()


def test_conflict_remedy_does_not_enter_the_execution_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = env(tmp_path, monkeypatch)
    assert coordinator_runtime.execution_binding(
        store, worker, "example/project"
    ) == coordinator_runtime.execution_binding(
        store, replace(worker, conflict_remedy=None), "example/project"
    )
    store.close()
