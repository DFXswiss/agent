"""Evaluator coverage for the declarative coordinator precondition registry."""

from __future__ import annotations

import copy
import hashlib
import types
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli import coordinator_checks
from agent_cli.allow import SECOND_REVIEW_NOT_POSTED
from agent_cli.coordinator import tick
from agent_cli.coordinator_checks import (
    READERS,
    RESOLVERS,
    CheckContext,
    CheckValue,
    TaskFacts,
    action_checks,
    applies,
    apply_outcome,
    evaluate_action,
    needs_resolution,
    one_line,
    raise_check_question,
    read_fact,
    record_check,
    registry_row_id,
    resolve_pr_mergeable,
    resolve_spec_available,
    route_phase,
)
from agent_cli.coordinator_common import CoordinatorError, coord, save_task, task_text_path
from agent_cli.coordinator_lanes import record_gate
from agent_cli.coordinator_runtime import _ensure_checklist_rows, _ensure_round
from agent_cli.preconditions import CHECKS, REGISTRY_KEYS
from agent_cli.store import Store, StoreError
from test_coordinator import patch_execute_github, seed_task
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_order_worker,
    make_session,
    make_worker,
    patch_account_runners,
    patch_command_runner,
    seed_order_task,
    write_accounts,
)


H = "a" * 40
B = "b" * 40
ORDER_ID = "11111111-1111-1111-1111-111111111111"
ISSUE_ID = "22222222-2222-2222-2222-222222222222"


def _task(phase: str = "accept", **checkpoint) -> dict:
    c = {"phase": phase, "source": {"repo": "example/project", "number": 7}}
    c.update(checkpoint)
    return {
        "id": ISSUE_ID,
        "session_id": "worker-session",
        "workflow": "implement",
        "payload": {"coordinator": c},
        "state": "open",
        "models": {"first": "grok", "second": "codex"},
    }


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, orders: bool = False):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_order_worker(tmp_path) if orders else make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    return store, worker, fake


def _order(
    store: Store,
    worker,
    *,
    phase: str = "accept",
    order_id: str = ORDER_ID,
    checklist: bool = False,
    **checkpoint,
) -> dict:
    task = seed_order_task(store, worker, order_id, phase=phase, **checkpoint)
    if checklist:
        session = store.row("session", worker.session_id)
        assert session is not None
        _ensure_checklist_rows(store, task, session)
        task = store.row("task", order_id)
    assert task is not None
    return task


def _issue(store: Store, worker, *, phase: str, state: str = "open", **checkpoint) -> dict:
    c = {
        "phase": phase,
        "source": {
            "repo": "example/project",
            "number": 7,
            "assigned_id": "assigned-1",
            "publication_repo": "example/project",
            "base": "develop",
            "title": "Fix widget",
        },
        "branch": "task-22222222",
    }
    c.update(checkpoint)
    seed_task(
        store,
        worker,
        ISSUE_ID,
        {
            "id": ISSUE_ID,
            "session_id": worker.session_id,
            "workflow": "implement",
            "title": "Fix widget",
            "repo": "example/project",
            "ref": str(c.get("pr_number")) if c.get("pr_number") else None,
            "payload": {"coordinator": c},
            "state": state,
            "current_round": 0,
            "models": {"first": "grok", "second": "codex"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    task = store.row("task", ISSUE_ID)
    assert task is not None
    return task


def _update(store: Store, table: str, row: dict, **changes) -> dict:
    changed = {key: value for key, value in row.items() if not key.startswith("_")}
    changed.update(changes)
    store.write(table, "update", str(row["id"]), changed)
    updated = store.row(table, str(row["id"]))
    assert updated is not None
    return updated


def _checklist_row(store: Store, task_id: str, key: str) -> dict:
    return next(
        row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id and row.get("key") == key
    )


@pytest.mark.no_pg
def test_applies_predicates():
    plain = _task("accept")
    a38 = _task("accept", a38={"enabled": True})
    model = _task(
        "accept", a38={"enabled": True, "second_review_mode": "model"}
    )
    assert applies("always", plain)
    assert not applies("a38", plain)
    assert applies("not_a38", plain)
    assert applies("a38", a38)
    assert applies("external_second", a38)
    assert not applies("model_second", a38)
    assert applies("model_second", model)
    assert not applies("external_second", model)
    assert applies("phase:accept", plain)
    assert not applies("phase:tests", plain)
    assert not applies("unknown", plain)


@pytest.mark.no_pg
def test_route_phase_alias():
    assert route_phase("second_review", _task()) == "pr_gates_codex"
    historical = _task()
    historical.pop("models")
    assert route_phase("second_review", historical) == "pr_gates_codex"
    external = _task()
    external["models"] = {"first": "grok", "second": None}
    assert route_phase("second_review", external) == "await_second_review"
    assert route_phase("tests", external) == "tests"


@pytest.mark.no_pg
def test_one_line():
    assert one_line("a\n b\t c", 100) == "a b c"
    bounded = one_line("x" * 2000, 50)
    assert len(bounded) == 50
    assert bounded.endswith(" …")


@pytest.mark.no_pg
def test_registered_resolvers_are_declared():
    declared = {check.resolver for check in CHECKS.values()}
    assert set(RESOLVERS) <= declared
    assert set(RESOLVERS) == {
        "duplicates_checked",
        "no_live_lane",
        "one_review_permitted",
        "round_budget",
        "pr_mergeable",
        "review_2",
        "session_registered",
        "spec_written",
        "order_authorized",
        "spec_available",
        "checkout_owned",
        "head_signed_clean",
        "conflict_mandate",
    }


@pytest.mark.no_pg
def test_readers_cover_every_existing_fact():
    assert set(READERS) == {
        key for key, check in CHECKS.items() if check.fact != "registry"
    }


@pytest.mark.no_pg
def test_a_waived_fact_is_not_resolved_again():
    check = CHECKS["spec_available"]
    assert not needs_resolution(check, CheckValue("ja", "x", source="human"))
    assert needs_resolution(check, CheckValue("ja", "x", source="script"))


@pytest.mark.no_pg
def test_unknown_outcome_is_refused(tmp_path):
    check = replace(CHECKS["order_active"], on_no="remedy:x")
    with pytest.raises(CoordinatorError, match="declares an unknown outcome remedy:x"):
        apply_outcome(
            None,
            make_worker(tmp_path),
            _task(),
            None,
            check,
            CheckValue("nein", "x"),
            action="accept",
        )


def test_existing_fact_readers(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(
        store,
        worker,
        phase="pr_gates_grok",
        checklist=True,
        head_sha=H,
        pr_number=42,
        published_head=H,
        evidence={"tests_pass": True, "tests_head": H, "ci_green": True, "ci_head": H},
    )

    facts = TaskFacts(store, task)
    assert READERS["tests_green"](facts, task).status == "ja"
    assert READERS["ci_green"](facts, task).status == "ja"
    assert READERS["draft_open"](facts, task).evidence == "pull request #42"
    assert READERS["head_published"](facts, task).status == "ja"

    coord(task)["evidence"] = {
        "tests_pass": False,
        "tests_head": H,
        "ci_green": False,
        "ci_head": H,
    }
    assert READERS["tests_green"](TaskFacts(store, task), task).status == "nein"
    assert READERS["ci_green"](TaskFacts(store, task), task).status == "nein"
    coord(task)["evidence"]["tests_head"] = B
    coord(task)["evidence"]["ci_head"] = B
    assert READERS["tests_green"](TaskFacts(store, task), task).status == "pending"
    assert READERS["ci_green"](TaskFacts(store, task), task).status == "pending"

    coord(task)["a38"] = {
        "enabled": True,
        "base_sha": B,
        "report_waiver": {"head": H, "reason": "markdown-only change set"},
    }
    coord(task)["evidence"] = {
        "tests_head": H,
        "tests_waiver": "markdown-only change set",
    }
    waived = READERS["tests_green"](TaskFacts(store, task), task)
    assert waived == CheckValue(
        "ja", f"tests waived on {H}: markdown-only change set"
    )

    coord(task)["a38"] = {
        "enabled": True,
        "base_sha": B,
        "report": {"head": H, "base_sha": B, "policy_revision": B},
    }
    assert READERS["report_posted"](TaskFacts(store, task), task).status == "ja"
    coord(task)["a38"]["report"] = {"head": H}
    assert READERS["report_posted"](TaskFacts(store, task), task).status == "pending"
    coord(task)["a38"]["report"] = {
        "head": H,
        "base_sha": B,
        "policy_revision": B,
    }
    coord(task)["a38"]["pending_base_sha"] = B
    assert READERS["report_posted"](TaskFacts(store, task), task).status == "pending"
    coord(task)["a38"] = {
        "enabled": True,
        "base_sha": B,
        "report_waiver": {
            "head": H,
            "base_sha": B,
            "policy_revision": B,
            "reason": "markdown-only change set",
        },
    }
    assert READERS["report_posted"](
        TaskFacts(store, task), task
    ).evidence.startswith("A38 report waived on")

    coord(task)["a38"]["second_comment"] = {"head": H, "blocks_merge": False}
    assert READERS["second_comment_posted"](TaskFacts(store, task), task).status == "ja"
    coord(task)["a38"]["second_comment"]["blocks_merge"] = True
    assert READERS["second_comment_posted"](TaskFacts(store, task), task).status == "nein"

    task["current_round"] = 1
    coord(task)["lane_outcome"] = {
        "role": "implementer",
        "round": 1,
        "agent_id": "agent-1",
        "applied": False,
    }
    store.write(
        "agent",
        "insert",
        "agent-1",
        {
            "id": "agent-1",
            "session_id": worker.session_id,
            "task_id": task["id"],
            "role": "implementer",
            "status": "done",
            "result": "done",
        },
    )
    assert READERS["implementer_outcome"](TaskFacts(store, task), task).status == "ja"
    task["current_round"] = 2
    assert READERS["implementer_outcome"](TaskFacts(store, task), task).status == "pending"
    task["current_round"] = 1
    coord(task)["lane_outcome"]["applied"] = True
    assert READERS["implementer_outcome"](TaskFacts(store, task), task).status == "pending"

    activity_id = "33333333-3333-3333-3333-333333333333"
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": worker.session_id,
            "type": "comment.post",
            "payload": {},
            "execution_status": "pending",
        },
    )
    coord(task)["a38"]["first_comment"] = {"head": H, "activity_id": activity_id}
    assert READERS["first_comment_posted"](TaskFacts(store, task), task).status == "pending"
    _update(store, "activity", store.row("activity", activity_id), execution_status="done")
    assert READERS["first_comment_posted"](TaskFacts(store, task), task).status == "ja"

    approved = record_gate(
        store,
        task,
        stage="review-1",
        dimension="quality",
        vendor="grok",
        verdict="approved",
        head=H,
        agent_id="agent-1",
        evidence=None,
    )
    assert approved
    assert READERS["review_1_quality"](TaskFacts(store, task), task).status == "ja"
    rejected = record_gate(
        store,
        task,
        stage="review-1",
        dimension="quality",
        vendor="grok",
        verdict="rejected",
        head=H,
        agent_id="agent-1",
        evidence=None,
    )
    assert READERS["review_1_quality"](TaskFacts(store, task), task).status == "nein"
    coord(task)["handled_rejected_gate_ids"] = [rejected]
    assert READERS["review_1_quality"](TaskFacts(store, task), task).status == "pending"
    record_gate(
        store,
        task,
        stage="review-1",
        dimension="quality",
        vendor="grok",
        verdict="approved",
        head=B,
        agent_id="agent-1",
        evidence=None,
    )
    assert READERS["review_1_quality"](TaskFacts(store, task), task).status == "pending"

    for key in ("review_2_quality", "review_2_logic"):
        row = _checklist_row(store, str(task["id"]), key)
        _update(
            store,
            "checklist_item",
            row,
            status="n_a",
            evidence=SECOND_REVIEW_NOT_POSTED,
        )
    assert READERS["review_2_quality"](TaskFacts(store, task), task).status == "n_a"
    task["state"] = "pr-review"
    assert READERS["ready_allowed"](TaskFacts(store, task), task).status == "ja"
    task["state"] = "implementing"
    assert READERS["ready_allowed"](TaskFacts(store, task), task).status == "nein"


def test_action_checks_is_read_only(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, checklist=True)
    before = store.conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"]
    values = action_checks(TaskFacts(store, task), task, "accept")
    after = store.conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"]
    assert [value.status for _check, value in values] == [
        "ja",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert after == before


def test_accept_gate_resolves_and_records_facts(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, checklist=True)
    assert evaluate_action(store, worker, task, fake, None, action="accept") is None
    session = _checklist_row(store, ORDER_ID, "session_registered")
    spec = _checklist_row(store, ORDER_ID, "spec_written")
    assert session["status"] == "ja"
    assert spec["status"] == "ja"
    assert spec["source"] == "human"
    for key in ("order_authorized", "spec_available"):
        row = store.row("checklist_item", registry_row_id(ORDER_ID, key))
        assert row is not None
        assert row["status"] == "ja"
        assert row["registry"] is True
        assert row["source"] == "script"
        assert row["override"] is None
        assert row["skip"] is None


def test_order_authorized_is_no_without_the_worker_option(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _order(store, worker, checklist=True)
    lines = evaluate_action(store, worker, task, fake, None, action="accept")
    c = coord(task)
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "accept"
    assert c["failed_check"] == "order_authorized"
    assert c["blocker"] == "Check order_authorized failed: worker does not accept orders"
    row = store.row("checklist_item", registry_row_id(ORDER_ID, "order_authorized"))
    assert row is not None and row["status"] == "nein"
    assert lines is not None and lines[-1] == f"blocker recorded for order {ORDER_ID}"


def test_a_stored_no_is_resolved_again_after_a_resume(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _order(store, worker, checklist=True)
    evaluate_action(store, worker, task, fake, None, action="accept")
    coord(task)["phase"] = "accept"
    accepting = replace(worker, orders=True)
    assert evaluate_action(store, accepting, task, fake, None, action="accept") is None
    row = store.row("checklist_item", registry_row_id(ORDER_ID, "order_authorized"))
    assert row is not None and row["status"] == "ja"


def test_spec_available_is_resolved_again_on_every_evaluation(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, checklist=True)
    assert evaluate_action(store, worker, task, fake, None, action="accept") is None
    task_text_path(worker, ORDER_ID).write_bytes(b"changed")
    lines = evaluate_action(store, worker, task, fake, None, action="accept")
    assert lines is not None
    assert coord(task)["phase"] == "blocked"
    assert coord(task)["blocker"] == (
        "Check spec_available failed: task text does not match the submitted text"
    )


def test_spec_available_hashes_the_file_bytes(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker)
    data = b"line one\r\nline two\r\n"
    digest = hashlib.sha256(data).hexdigest()
    task_text_path(worker, ORDER_ID).write_bytes(data)
    coord(task)["source"]["body_sha256"] = digest
    save_task(store, task)
    value = resolve_spec_available(
        CheckContext(
            store,
            worker,
            task,
            fake,
            None,
            CHECKS["spec_available"],
            "accept",
        )
    )
    assert value == CheckValue(
        "ja", f"task text sha256 {digest[:12]}, 20 characters"
    )


def test_fact_is_written_only_on_change(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, checklist=True)
    assert evaluate_action(store, worker, task, fake, None, action="accept") is None
    before = store.conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"]
    assert evaluate_action(store, worker, task, fake, None, action="accept") is None
    after = store.conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"]
    assert after == before


def test_head_bound_fact_is_pending_on_a_new_head(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, phase="tests", head_sha=H)
    record_check(
        store,
        task,
        CHECKS["head_signed_clean"],
        CheckValue("ja", f"signed clean head {H}", head=H),
        action="tests",
    )
    coord(task)["head_sha"] = B
    value = read_fact(TaskFacts(store, task), task, CHECKS["head_signed_clean"])
    assert value == CheckValue("pending", "recorded on another head")


def test_a_waived_head_bound_fact_holds_on_a_new_head(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, phase="tests", head_sha=B)
    row_id = registry_row_id(ORDER_ID, "head_signed_clean")
    base = {
        "id": row_id,
        "task_id": ORDER_ID,
        "key": "head_signed_clean",
        "status": "ja",
        "evidence": f"signed clean head {H}",
        "source": "human",
        "head_sha": H,
    }
    store.write("checklist_item", "insert", row_id, base)
    assert read_fact(TaskFacts(store, task), task, CHECKS["head_signed_clean"]).status == "ja"
    _update(store, "checklist_item", store.row("checklist_item", row_id), source="script")
    assert read_fact(TaskFacts(store, task), task, CHECKS["head_signed_clean"]).status == "pending"


def test_record_check_writes_a_skip_with_who_and_why(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, phase="start_draft")
    skip = {
        "by": "human-owner",
        "reason": "none exists",
        "at": "2026-01-01T00:00:00Z",
    }
    value = CheckValue(
        "ja", "skipped at submit by human-owner: none exists", source="human"
    )
    assert record_check(
        store,
        task,
        CHECKS["duplicates_checked"],
        value,
        action="open_draft",
        skip=skip,
    )
    row = store.row(
        "checklist_item", registry_row_id(ORDER_ID, "duplicates_checked")
    )
    assert row is not None
    assert row["status"] == "ja"
    assert row["source"] == "human"
    assert row["granted_by"] == "human-owner"
    assert row["skip"] == skip
    assert row["override"] is None
    assert not record_check(
        store,
        task,
        CHECKS["duplicates_checked"],
        value,
        action="open_draft",
        skip=skip,
    )


def test_record_check_refuses_a_fact_outside_its_action(tmp_path, monkeypatch):
    store, worker, _fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, phase="tests")
    with pytest.raises(CoordinatorError, match="is not a check of action tests"):
        record_check(
            store,
            task,
            CHECKS["duplicates_checked"],
            CheckValue("ja", "x"),
            action="tests",
        )
    with pytest.raises(CoordinatorError, match="is not a registry fact"):
        record_check(
            store,
            task,
            CHECKS["draft_open"],
            CheckValue("ja", "x"),
            action="tests",
        )


def test_pending_routes_to_its_own_target(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(store, worker, phase="tests")
    _ensure_round(store, task)
    task = store.row("task", ORDER_ID)
    assert task is not None
    lines = evaluate_action(store, worker, task, fake, None, action="tests")
    assert lines == ["check reviewer_approved is pending; routing to inner_review"]
    assert coord(task)["phase"] == "inner_review"

    round_row = next(row for row in store.rows("task_round") if row["task_id"] == ORDER_ID)
    _update(store, "task_round", round_row, reviewer_verdict="rejected")
    coord(task)["phase"] = "tests"
    lines = evaluate_action(store, worker, task, fake, None, action="tests")
    assert lines == ["check reviewer_approved=nein; routing to implement"]
    assert coord(task)["phase"] == "implement"
    assert task["state"] == "implementing"
    assert coord(task)["findings"] == "round 1 rejected"


def test_route_to_publish_draft_sets_resume_phase(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(
        store,
        worker,
        phase="pr_gates_grok",
        head_sha=H,
        published_head=B,
        evidence={"tests_pass": True, "tests_head": H},
    )
    lines = evaluate_action(store, worker, task, fake, None, action="first_review")
    assert lines == ["check head_published is pending; routing to publish_draft"]
    assert coord(task)["phase"] == "publish_draft"
    assert coord(task)["resume_phase"] == "pr_gates_grok"
    assert coord(task)["gate_route"]["count"] == 1


def test_route_loop_blocks(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(
        store,
        worker,
        phase="pr_gates_grok",
        head_sha=H,
        published_head=B,
        evidence={"tests_pass": True, "tests_head": H},
    )
    for _ in range(3):
        coord(task)["phase"] = "pr_gates_grok"
        lines = evaluate_action(store, worker, task, fake, None, action="first_review")
    assert lines is not None
    assert coord(task)["phase"] == "blocked"
    assert coord(task)["resume_phase"] == "pr_gates_grok"
    assert coord(task)["blocker"] == (
        "Check head_published stays pending after its resolver phase publish_draft ran."
    )
    assert "gate_route" not in coord(task)


def test_gate_route_is_cleared_when_its_action_passes(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    task = _order(
        store,
        worker,
        phase="a38_first_comment",
        head_sha=H,
        a38={"enabled": True, "base_sha": B},
    )
    assert evaluate_action(
        store, worker, task, fake, None, action="first_review_comment"
    ) == ["check review_1_quality is pending; routing to pr_gates_grok"]
    for dimension in ("quality", "logic"):
        record_gate(
            store,
            task,
            stage="review-1",
            dimension=dimension,
            vendor="grok",
            verdict="approved",
            head=H,
            agent_id="agent-1",
            evidence=None,
        )
    coord(task)["a38"]["report"] = {
        "head": H,
        "base_sha": B,
        "policy_revision": B,
    }
    coord(task)["phase"] = "a38_first_comment"
    assert evaluate_action(
        store, worker, task, fake, None, action="first_review_comment"
    ) is None
    assert "gate_route" not in coord(task)


def test_block_outcome_publishes_a_resumable_blocker(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    worktree = str(worker.workspace_root / ORDER_ID)
    task = _order(store, worker, phase="start_draft", worktree=worktree)
    evaluate_action(store, worker, task, fake, None, action="open_draft")
    c = coord(task)
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "start_draft"
    assert c["failed_check"] == "checkout_owned"
    assert c["blocker"].startswith("Check checkout_owned failed: ")


def test_fail_outcome_ends_the_task(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _issue(store, worker, phase="accept", resume_phase="implement")
    lines = apply_outcome(
        store,
        worker,
        task,
        fake,
        CHECKS["order_active"],
        CheckValue("nein", "task state failed"),
        action="accept",
    )
    assert lines == ["check order_active=nein; order failed"]
    assert task["state"] == "failed"
    assert coord(task)["phase"] == "blocked"
    assert coord(task)["failed_kind"] == "order_active"
    assert "resume_phase" not in coord(task)


def test_retry_outcome_uses_the_lane_retry_bound(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    worker = replace(worker, lane_retries=1)
    task = _issue(store, worker, phase="implement")
    value = CheckValue("nein", "lane ended")
    first = apply_outcome(
        store,
        worker,
        task,
        fake,
        CHECKS["no_live_lane"],
        value,
        action="implement",
    )
    assert first[0].startswith("retry 1/1 of implement")
    second = apply_outcome(
        store,
        worker,
        task,
        fake,
        CHECKS["no_live_lane"],
        value,
        action="implement",
    )
    assert coord(task)["failed_check"] == "no_live_lane"
    assert "Model lane interrupted in implement; not a code rejection." in coord(task)["blocker"]
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 1
    assert "<!-- agent-coordinator:status:v1:lane-failed:" in comments[0]["payload"]["body"]
    assert second


def test_question_is_posted_on_the_issue_before_the_save(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinator_checks, "QUESTION_TEXTS", {})
    task = _issue(store, worker, phase="implement")
    raise_check_question(
        store,
        worker,
        task,
        fake,
        CHECKS["round_budget"],
        CheckValue("nein", "round 3 exceeds the limit"),
    )
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 1
    assert comments[0]["payload"]["body"].startswith(
        "Question:\nCheck round_budget failed for example/project#7: "
        "round 3 exceeds the limit"
    )
    c = coord(task)
    assert c["phase"] == "ask"
    assert task["state"] == "open"
    assert c["resume_phase"] == "implement"
    assert c["failed_check"] == "round_budget"
    assert c["question_activity_id"] == comments[0]["id"]
    assert c["replies_consumed_through"] is None
    assert c["question_text"].startswith("Check round_budget failed for")


def test_question_survives_a_crash_before_the_save(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinator_checks, "QUESTION_TEXTS", {})
    task = _issue(store, worker, phase="implement")
    original = copy.deepcopy(task)
    real_save = coordinator_checks.save_task
    calls = 0

    def crash_once(target_store, changed):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StoreError("crash")
        return real_save(target_store, changed)

    monkeypatch.setattr(coordinator_checks, "save_task", crash_once)
    with pytest.raises(StoreError, match="crash"):
        raise_check_question(
            store,
            worker,
            task,
            fake,
            CHECKS["round_budget"],
            CheckValue("nein", "round 3 exceeds the limit"),
        )
    assert task == original
    assert coord(store.row("task", ISSUE_ID))["phase"] == "implement"
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 1

    raise_check_question(
        store,
        worker,
        task,
        fake,
        CHECKS["round_budget"],
        CheckValue("nein", "round 3 exceeds the limit"),
    )
    assert coord(task)["question_activity_id"] == comments[0]["id"]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1


def test_question_for_an_order_without_draft_is_local(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    monkeypatch.setattr(coordinator_checks, "QUESTION_TEXTS", {})
    task = _order(store, worker, phase="implement", title="Add a widget")
    raise_check_question(
        store,
        worker,
        task,
        fake,
        CHECKS["round_budget"],
        CheckValue("nein", "round 3 exceeds the limit"),
    )
    assert not [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert coord(task)["phase"] == "ask"
    assert coord(task)["question_text"].startswith(
        'Check round_budget failed for order "'
    )
    assert "question_activity_id" not in coord(task)


@pytest.mark.no_pg
def test_gate_does_not_touch_the_store_for_an_issue_task_in_ci(tmp_path):
    task = {
        "id": "task-1",
        "state": "open",
        "payload": {
            "coordinator": {
                "phase": "ci",
                "source": {"repo": "example/project", "number": 7},
            }
        },
    }
    assert evaluate_action(
        types.SimpleNamespace(home=tmp_path),
        make_worker(tmp_path),
        task,
        object(),
        None,
        action="ci",
    ) is None


def test_issue_task_evaluates_only_the_shared_checks(tmp_path, monkeypatch):
    from test_coordinator_flow import _drive_through_ci_to

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, "leave_draft")
    tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert coord(task)["phase"] == "await_merge"

    rows = [
        row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task["id"]
        and row.get("key") in REGISTRY_KEYS
    ]
    assert {row["key"] for row in rows} == {
        "no_live_lane",
        "round_budget",
        "pr_mergeable",
    }
    assert all(row["status"] == "ja" and row["registry"] is True for row in rows)
    assert all(
        row["id"] == registry_row_id(str(task["id"]), str(row["key"]))
        for row in rows
    )


def test_commit_push_gate_blocks_an_order_without_checkout(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch, orders=True)
    worktree = tmp_path / "plain-checkout"
    worktree.mkdir()
    agent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    task = _order(
        store,
        worker,
        phase="implement",
        worktree=str(worktree),
        lane_outcome={
            "role": "implementer",
            "round": 0,
            "agent_id": agent_id,
            "status": "complete",
            "result": "done",
            "applied": False,
        },
    )
    store.write(
        "agent",
        "insert",
        agent_id,
        {
            "id": agent_id,
            "session_id": worker.session_id,
            "task_id": ORDER_ID,
            "round": 0,
            "role": "implementer",
            "vendor": "grok",
            "status": "done",
        },
    )
    lines = evaluate_action(
        store, worker, task, fake, None, action="commit_push"
    )
    assert lines is not None
    c = coord(task)
    assert c["failed_check"] == "checkout_owned"
    assert c["resume_phase"] == "implement"
    assert isinstance(c["lane_outcome"], dict)


def test_pr_mergeable_without_a_pull_request_raises_the_phase_error(
    tmp_path, monkeypatch
):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _issue(store, worker, phase="readiness")
    task["ref"] = None
    with pytest.raises(CoordinatorError, match="readiness requires PR number"):
        resolve_pr_mergeable(
            CheckContext(
                store,
                worker,
                task,
                fake,
                None,
                CHECKS["pr_mergeable"],
                "readiness",
            )
        )
    assert store.row("checklist_item", registry_row_id(ISSUE_ID, "pr_mergeable")) is None


@pytest.mark.parametrize(
    "mergeable,status,evidence",
    [
        ("MERGEABLE", "ja", f"mergeable on {H}"),
        (
            "CONFLICTING",
            "nein",
            "pull request has merge conflicts with develop; resolve them on the "
            "branch or cancel, then reply",
        ),
    ],
)
def test_pr_mergeable_classifies_computed_values(
    tmp_path, monkeypatch, mergeable, status, evidence
):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _issue(
        store,
        worker,
        phase="readiness",
        pr_number=42,
        head_sha=H,
    )
    monkeypatch.setattr(
        coordinator_checks,
        "_fetch_pr_json",
        lambda *args, **kwargs: {"mergeable": mergeable},
    )
    value = resolve_pr_mergeable(
        CheckContext(
            store,
            worker,
            task,
            fake,
            None,
            CHECKS["pr_mergeable"],
            "readiness",
        )
    )
    assert value.status == status
    assert value.evidence == evidence
    assert value.head == H


def test_unknown_mergeability_is_a_same_phase_retry(tmp_path, monkeypatch):
    store, worker, fake = _setup(tmp_path, monkeypatch)
    task = _issue(
        store,
        worker,
        phase="leave_draft",
        pr_number=42,
        head_sha=H,
    )
    monkeypatch.setattr(
        coordinator_checks,
        "_fetch_pr_json",
        lambda *args, **kwargs: {"mergeable": "UNKNOWN"},
    )
    value = resolve_pr_mergeable(
        CheckContext(
            store,
            worker,
            task,
            fake,
            None,
            CHECKS["pr_mergeable"],
            "ready",
        )
    )
    assert value.lines == [
        f"PR mergeability not yet computed on {H[:7]}; retrying leave_draft"
    ]
