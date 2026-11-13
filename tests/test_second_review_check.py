from __future__ import annotations

import json
import types
from dataclasses import replace

import pytest

from agent_cli.a38_guard import status_context_enforce
from agent_cli.coordinator_a38 import phase_await_ready, require_ready_evidence
from agent_cli.coordinator_checks import (
    CheckContext,
    CheckValue,
    Deferred,
    evaluate_action,
    record_check,
    registry_row_id,
    resolve_action,
    resolve_one_review_permitted,
    resolve_review_2,
    resume_after_second_review_waiver,
    second_review_waived,
    second_review_waiver_refusal,
)
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_github import (
    _trusted_done_review,
    _trusted_review_config,
    phase_await_merge,
    phase_leave_draft,
    phase_readiness,
)
from agent_cli.coordinator_lanes import record_gate, set_checklist
from agent_cli.coordinator_orders import consume_order_intents, create_order_task
from agent_cli.coordinator_runtime import preflight_worker
from agent_cli.model_slots import canonical_key
from agent_cli.order_ops import OrderError, op_override
from agent_cli.preconditions import CHECKS
from agent_cli.pr_guard_config import PrGuardLookupError
from test_coordinator import (
    _list_opener,
    _ready_side_env,
    _seed_ready_side_task,
)
from test_coordinator_a38_model import env as model_env
from test_coordinator_a38_model import seed_model_task
from test_coordinator_support import (
    insert_order_intent,
    make_worker,
    order_source,
)
from test_orders_runtime import prepare_orders, run_tick


OID = "5ec0d0de-0000-4000-8000-000000000001"


def H7(fake) -> str:  # noqa: N802, ANN001
    return fake.head[:7]


def review_env(tmp_path, monkeypatch):  # noqa: ANN001
    store, worker, fake = _ready_side_env(
        tmp_path, monkeypatch, patch_github=True
    )
    return store, replace(worker, orders=True), fake


def seed_review_order(
    store,
    worker,
    fake,
    oid,
    *,
    phase,
    formal=False,
    evidence=None,
    **checkpoint,
):  # noqa: ANN001
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=oid,
        phase=phase,
        checklist_codex={
            "review_2_quality": "pending",
            "review_2_logic": "pending",
        },
        seed_formal_review=formal,
        extra_evidence=evidence,
        models={"first": "grok", "second": "codex"},
    )
    c = task["payload"]["coordinator"]
    c["source"] = order_source(oid)
    c.update(checkpoint)
    store.write(
        "task",
        "update",
        oid,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    return store.row("task", oid)


def model_order(store, task, oid):  # noqa: ANN001
    task["payload"]["coordinator"]["source"] = order_source(oid)
    store.write(
        "task",
        "update",
        oid,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    return store.row("task", oid)


def second_gate(store, task, fake, dimension, verdict, head=None):  # noqa: ANN001
    return record_gate(
        store,
        task,
        stage="review-2",
        dimension=dimension,
        vendor="codex",
        verdict=verdict,
        head=head or fake.head,
        agent_id="agent-1",
        evidence="Q-FINDING" if verdict == "rejected" else None,
    )


def rows(store, oid):  # noqa: ANN001
    return {
        canonical_key(str(row.get("key"))): row
        for row in store.rows("checklist_item")
        if row.get("task_id") == oid
    }


def fact(store, oid, key):  # noqa: ANN001
    return store.row("checklist_item", registry_row_id(oid, key))


def set_rows_n_a(store, oid):  # noqa: ANN001
    for key in ("review_2_quality", "review_2_logic"):
        row = rows(store, oid)[key]
        row.update(
            status="n_a",
            evidence="second review not posted",
            source="script",
        )
        store.write(
            "checklist_item",
            "update",
            row["id"],
            {name: value for name, value in row.items() if not name.startswith("_")},
        )


def record_waiver(store, task):  # noqa: ANN001
    set_rows_n_a(store, task["id"])
    record_check(
        store,
        task,
        CHECKS["review_2"],
        CheckValue(
            "ja",
            "override by human-owner: one review is enough",
            source="human",
        ),
        action="ci",
        override={
            "by": "human-owner",
            "reason": "one review is enough",
            "at": "2026-01-01T00:00:00Z",
            "intent_id": "intent-1",
        },
    )


def override_intent(store, worker, oid):  # noqa: ANN001
    return insert_order_intent(
        store,
        worker,
        oid,
        "override",
        check="review_2",
        reason="one review is enough",
    )


def consume(store, worker, fake, oid, iid):  # noqa: ANN001
    return consume_order_intents(
        store,
        worker,
        store.row("task", oid),
        fake,
        [store.row("activity", iid)],
    )


def context(store, worker, task, fake, key, action):  # noqa: ANN001
    return CheckContext(store, worker, task, fake, None, CHECKS[key], action)


@pytest.mark.parametrize(
    ("case", "status", "evidence"),
    [
        ("approved", "ja", "second review free of findings on {head}"),
        ("rejected", "nein", "second review has findings on {head7}"),
        ("missing", "pending", "second review not complete on {head7}"),
        ("n_a", "pending", "second review not complete on {head7}"),
        ("other-head", "pending", "second review not complete on {head7}"),
    ],
)
def test_review_2_resolver(tmp_path, monkeypatch, case, status, evidence):  # noqa: ANN001
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    if case == "approved":
        second_gate(store, task, fake, "quality", "approved")
        second_gate(store, task, fake, "logic", "approved")
    elif case == "rejected":
        second_gate(store, task, fake, "quality", "rejected")
    elif case == "n_a":
        set_rows_n_a(store, OID)
    elif case == "other-head":
        second_gate(store, task, fake, "quality", "approved", "e" * 40)
        second_gate(store, task, fake, "logic", "approved", "e" * 40)
    value = resolve_review_2(context(store, worker, task, fake, "review_2", "ci"))
    assert value.status == status
    assert value.evidence == evidence.format(head=fake.head, head7=H7(fake))
    assert value.head == fake.head
    store.close()


def test_review_2_resolver_treats_a_handled_rejection_as_pending(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    gate_id = second_gate(store, task, fake, "quality", "rejected")
    task["payload"]["coordinator"]["handled_rejected_gate_ids"] = [gate_id]
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    value = resolve_review_2(
        context(store, worker, store.row("task", OID), fake, "review_2", "ci")
    )
    assert value.status == "pending"
    store.close()


def test_model_review_2_resolver_requires_the_second_comment(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = model_env(tmp_path, monkeypatch)
    task = seed_model_task(
        store,
        worker,
        fake,
        OID,
        phase="ci",
        second={
            "quality": ("approved", None),
            "logic": ("approved", None),
        },
    )
    value = resolve_review_2(context(store, worker, task, fake, "review_2", "ci"))
    assert value.status == "pending"
    task["payload"]["coordinator"]["a38"]["second_comment"] = {
        "head": fake.head,
        "blocks_merge": False,
    }
    assert resolve_review_2(
        context(store, worker, task, fake, "review_2", "ci")
    ).status == "ja"
    task["payload"]["coordinator"]["a38"]["second_comment"][
        "blocks_merge"
    ] = True
    assert resolve_review_2(
        context(store, worker, task, fake, "review_2", "ci")
    ).status == "nein"
    store.close()


def test_ci_gate_routes_to_the_second_review(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    assert evaluate_action(store, worker, task, fake, None, action="ci") == [
        "check review_2 is pending; routing to pr_gates_codex"
    ]
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "pr_gates_codex"
    saved = fact(store, OID, "review_2")
    assert (saved["status"], saved["source"], saved["head_sha"]) == (
        "pending",
        "script",
        fake.head,
    )
    store.close()


def test_ci_gate_passes_after_the_second_review(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    second_gate(store, task, fake, "quality", "approved")
    second_gate(store, task, fake, "logic", "approved")
    assert evaluate_action(store, worker, task, fake, None, action="ci") is None
    saved = fact(store, OID, "review_2")
    assert (saved["status"], saved["source"]) == ("ja", "script")
    assert fact(store, OID, "one_review_permitted") is None
    store.close()


def test_ci_gate_routes_to_the_implementer_on_findings(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    second_gate(store, task, fake, "quality", "rejected")
    assert evaluate_action(store, worker, task, fake, None, action="ci") == [
        "check review_2=nein; routing to implement"
    ]
    stored = store.row("task", OID)
    assert stored["payload"]["coordinator"]["phase"] == "implement"
    assert stored["state"] == "implementing"
    store.close()


def test_manual_n_a_does_not_drop_the_second_review(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    set_rows_n_a(store, OID)
    assert evaluate_action(store, worker, task, fake, None, action="ci") == [
        "check review_2 is pending; routing to pr_gates_codex"
    ]
    store.close()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("none", None),
        ("external", "the second review of this order comes from external reviewers"),
        ("first", "the first review is not complete"),
        ("approved", "a second review was already approved for this pull request"),
        ("a38-rejected", "a blocking second review is already posted on this pull request"),
        ("non-a38-rejected", None),
        ("no-pr", "no pull request"),
    ],
)
def test_waiver_refusal_rules(tmp_path, monkeypatch, mutation, reason):  # noqa: ANN001
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    if mutation == "external":
        task["models"] = {"first": "grok", "second": None}
    elif mutation == "first":
        row = rows(store, OID)["review_1_logic"]
        row["status"] = "pending"
        store.write(
            "checklist_item",
            "update",
            row["id"],
            {key: item for key, item in row.items() if not key.startswith("_")},
        )
    elif mutation == "approved":
        row = rows(store, OID)["review_2_quality"]
        row["status"] = "ja"
        store.write(
            "checklist_item",
            "update",
            row["id"],
            {key: item for key, item in row.items() if not key.startswith("_")},
        )
    elif mutation == "a38-rejected":
        task["payload"]["coordinator"]["a38"] = {"enabled": True}
        second_gate(store, task, fake, "quality", "rejected")
    elif mutation == "non-a38-rejected":
        second_gate(store, task, fake, "quality", "rejected")
    elif mutation == "no-pr":
        task["payload"]["coordinator"].pop("pr_number")
    if mutation in ("external", "a38-rejected", "no-pr"):
        store.write(
            "task",
            "update",
            OID,
            {key: value for key, value in task.items() if not key.startswith("_")},
        )
        task = store.row("task", OID)
    assert second_review_waiver_refusal(store, task) == reason
    store.close()


def test_second_review_waived(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="ci")
    assert second_review_waived(store, task) is False
    record_check(
        store,
        task,
        CHECKS["review_2"],
        CheckValue("ja", "second review free of findings on x", head=fake.head),
        action="ci",
    )
    assert second_review_waived(store, task) is False
    record_waiver(store, task)
    assert second_review_waived(store, task) is True
    store.close()


@pytest.mark.no_pg
def test_second_review_waived_reads_nothing_for_an_issue_task() -> None:
    task = {
        "id": "t",
        "payload": {
            "coordinator": {
                "source": {"repo": "example/project", "number": 7}
            }
        },
    }
    assert second_review_waived(object(), task) is False


def test_one_review_permitted_reads_the_trusted_list(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    _list_opener(fake)
    value = resolve_one_review_permitted(
        context(
            store,
            worker,
            task,
            fake,
            "one_review_permitted",
            "waive_second_review",
        )
    )
    assert (value.status, value.evidence) == (
        "ja",
        "one review is enough for worker-bot in example/project",
    )
    fake.pr_guard_body = None
    value = resolve_one_review_permitted(
        context(
            store,
            worker,
            task,
            fake,
            "one_review_permitted",
            "waive_second_review",
        )
    )
    assert value.status == "nein"
    assert value.evidence == (
        "example/project requires two reviews for pull requests opened by worker-bot"
    )
    row = rows(store, OID)["review_1_logic"]
    row["status"] = "pending"
    store.write(
        "checklist_item",
        "update",
        row["id"],
        {key: item for key, item in row.items() if not key.startswith("_")},
    )

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("runner called")

    value = resolve_one_review_permitted(
        context(
            store,
            worker,
            task,
            fail,
            "one_review_permitted",
            "waive_second_review",
        )
    )
    assert (value.status, value.evidence) == (
        "nein",
        "the first review is not complete",
    )
    store.close()


def test_one_review_permitted_defers_a_trusted_list_failure(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")

    def down(*args, **kwargs):  # noqa: ANN002, ANN003
        raise PrGuardLookupError("down")

    monkeypatch.setattr("agent_cli.coordinator_github.fetch_trusted_pr_guard", down)
    value = resolve_one_review_permitted(
        context(
            store,
            worker,
            task,
            fake,
            "one_review_permitted",
            "waive_second_review",
        )
    )
    assert isinstance(value, Deferred)
    assert value.lines == [
        "trusted review list temporarily unavailable; retrying the review_2 override"
    ]
    store.close()


def test_trusted_done_review_still_requires_both_keys_n_a(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("runner called")

    assert _trusted_done_review(store, worker, task, fail) == ("", None)
    _list_opener(fake)
    login, config = _trusted_review_config(store, worker, task, fake)
    assert login == "worker-bot"
    assert config["one_review_authors"] == ["worker-bot"]
    set_rows_n_a(store, OID)
    assert _trusted_done_review(store, worker, task, fake) == (login, config)
    store.close()


def test_set_checklist_is_unchanged_without_snapshot_extra(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    with pytest.raises(
        CoordinatorError,
        match="close_allowed denied for review_2_quality: review_2_quality n_a requires one_review_authors",
    ):
        set_checklist(
            store,
            task,
            "review_2_quality",
            "n_a",
            "second review not posted",
        )
    assert rows(store, OID)["review_2_quality"]["status"] == "pending"
    store.close()


def test_set_checklist_closes_n_a_with_the_trusted_list(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    extra = {
        "trusted_pr_guard": {"one_review_authors": ["worker-bot"]},
        "pull_opener": "worker-bot",
    }
    set_checklist(
        store,
        task,
        "review_2_quality",
        "n_a",
        "second review not posted",
        snapshot_extra=extra,
    )
    first = rows(store, OID)["review_2_quality"]
    assert (first["status"], first["evidence"]) == (
        "n_a",
        "second review not posted",
    )
    updated = first["updated_at"]
    set_checklist(
        store,
        task,
        "review_2_quality",
        "n_a",
        "second review not posted",
        snapshot_extra=extra,
    )
    assert rows(store, OID)["review_2_quality"]["updated_at"] == updated
    with pytest.raises(
        CoordinatorError,
        match="close_allowed denied for review_2_logic: review_2_logic n_a requires one_review_authors",
    ):
        set_checklist(
            store,
            task,
            "review_2_logic",
            "n_a",
            "second review not posted",
            snapshot_extra={**extra, "pull_opener": "someone-else"},
        )
    store.close()


def test_override_waives_the_second_review(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    iid = override_intent(store, worker, OID)
    assert consume(store, worker, fake, OID, iid) == [
        "check review_2 overridden by human-owner"
    ]
    assert second_review_waived(store, store.row("task", OID)) is True
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    saved_rows = rows(store, OID)
    for key in ("review_2_quality", "review_2_logic"):
        assert saved_rows[key]["status"] == "n_a"
        assert saved_rows[key]["evidence"] == "second review not posted"
    permitted = fact(store, OID, "one_review_permitted")
    assert permitted["status"] == "ja"
    assert permitted["evidence"] == (
        "one review is enough for worker-bot in example/project"
    )
    waiver = fact(store, OID, "review_2")
    assert waiver["status"] == "ja"
    assert waiver["source"] == "human"
    assert waiver["granted_by"] == "human-owner"
    assert waiver["evidence"] == "override by human-owner: one review is enough"
    assert waiver["override"]["by"] == "human-owner"
    assert waiver["override"]["intent_id"] == iid
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    intent = store.row("activity", iid)
    assert intent["execution_status"] == "done"
    assert intent["result"]["effect"] == "override"
    assert second_review_waived(store, store.row("task", OID)) is True
    assert fake.launched == []
    store.close()


def test_override_is_refused_when_the_repository_requires_two_reviews(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    iid = override_intent(store, worker, OID)
    assert consume(store, worker, fake, OID, iid) is None
    intent = store.row("activity", iid)
    assert intent["execution_status"] == "error"
    assert intent["execution_error"] == (
        "check review_2 cannot be waived: example/project requires two reviews "
        "for pull requests opened by worker-bot"
    )
    assert fact(store, OID, "review_2") is None
    assert fact(store, OID, "one_review_permitted")["status"] == "nein"
    assert all(
        rows(store, OID)[key]["status"] == "pending"
        for key in ("review_2_quality", "review_2_logic")
    )
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "pr_gates_codex"
    store.close()


def test_override_waits_for_the_trusted_list(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    iid = override_intent(store, worker, OID)

    def down(*args, **kwargs):  # noqa: ANN002, ANN003
        raise PrGuardLookupError("down")

    with monkeypatch.context() as patch:
        patch.setattr("agent_cli.coordinator_github.fetch_trusted_pr_guard", down)
        assert consume(store, worker, fake, OID, iid) == [
            "trusted review list temporarily unavailable; retrying the review_2 override"
        ]
    assert store.row("activity", iid)["execution_status"] == "pending"
    assert fact(store, OID, "review_2") is None
    assert fact(store, OID, "one_review_permitted") is None
    assert all(
        rows(store, OID)[key]["status"] == "pending"
        for key in ("review_2_quality", "review_2_logic")
    )
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "pr_gates_codex"
    assert consume(store, worker, fake, OID, iid) == [
        "check review_2 overridden by human-owner"
    ]
    assert second_review_waived(store, store.row("task", OID)) is True
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    store.close()


def test_override_is_resumed_after_a_crash_between_rows_and_fact(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    set_rows_n_a(store, OID)
    iid = override_intent(store, worker, OID)
    consume(store, worker, fake, OID, iid)
    waiver = fact(store, OID, "review_2")
    assert (waiver["status"], waiver["source"]) == ("ja", "human")
    assert store.row("activity", iid)["execution_status"] == "done"
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    store.close()


def test_override_resumes_an_order_blocked_in_the_second_review(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(
        store,
        worker,
        fake,
        OID,
        phase="blocked",
        resume_phase="pr_gates_codex",
        blocker="codex quality review unavailable/incomplete",
        blocker_publish_once=True,
        blocker_publish_kind="provider-incomplete",
        blocker_activity_id="activity-1",
        retry={"phase": "pr_gates_codex", "attempt": 1, "reason": "x"},
        lane_outcome={
            "role": "reviewer-results",
            "outcomes": [{"stage": "review-2", "dimension": "quality"}],
        },
    )
    iid = override_intent(store, worker, OID)
    consume(store, worker, fake, OID, iid)
    task = store.row("task", OID)
    c = task["payload"]["coordinator"]
    assert (c["phase"], task["state"]) == ("ci", "pr-review")
    for key in (
        "resume_phase",
        "blocker",
        "retry",
        "lane_outcome",
        "blocker_publish_once",
        "blocker_publish_kind",
        "blocker_activity_id",
    ):
        assert key not in c
    store.close()


def test_override_waits_for_the_blocker_comment(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    task = seed_review_order(
        store,
        worker,
        fake,
        OID,
        phase="blocked",
        resume_phase="pr_gates_codex",
        blocker="review incomplete",
        blocker_publish_once=True,
    )
    iid = override_intent(store, worker, OID)
    assert consume(store, worker, fake, OID, iid) is None
    assert store.row("activity", iid)["execution_status"] == "pending"
    assert fact(store, OID, "review_2") is None
    assert fact(store, OID, "one_review_permitted") is None
    assert all(
        rows(store, OID)[key]["status"] == "pending"
        for key in ("review_2_quality", "review_2_logic")
    )
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "blocked"
    task["payload"]["coordinator"]["blocker_activity_id"] = "activity-1"
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    assert consume(store, worker, fake, OID, iid) == [
        "check review_2 overridden by human-owner"
    ]
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    assert store.row("activity", iid)["execution_status"] == "done"
    assert all(
        rows(store, OID)[key]["status"] == "n_a"
        for key in ("review_2_quality", "review_2_logic")
    )
    store.close()


def test_override_keeps_another_phase(tmp_path, monkeypatch) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(
        store,
        worker,
        fake,
        OID,
        phase="blocked",
        resume_phase="implement",
        blocker="stop",
    )
    iid = override_intent(store, worker, OID)
    consume(store, worker, fake, OID, iid)
    c = store.row("task", OID)["payload"]["coordinator"]
    assert (c["phase"], c["resume_phase"], c["blocker"]) == (
        "blocked",
        "implement",
        "stop",
    )
    waiver = fact(store, OID, "review_2")
    assert (waiver["status"], waiver["source"]) == ("ja", "human")
    assert all(
        rows(store, OID)[key]["status"] == "n_a"
        for key in ("review_2_quality", "review_2_logic")
    )
    assert store.row("activity", iid)["execution_status"] == "done"
    store.close()


def test_override_with_a_written_fact_only_finishes(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    iid = override_intent(store, worker, OID)
    record_check(
        store,
        task,
        CHECKS["review_2"],
        CheckValue("ja", "override by human-owner: one review", source="human"),
        action="ci",
        override={
            "by": "human-owner",
            "reason": "one review",
            "at": "2026-01-01T00:00:00Z",
            "intent_id": iid,
        },
    )

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("trusted list read")

    monkeypatch.setattr("agent_cli.coordinator_github.fetch_trusted_pr_guard", fail)
    consume(store, worker, fake, OID, iid)
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "ci"
    assert store.row("activity", iid)["execution_status"] == "done"
    store.close()


def test_waived_order_skips_the_second_review_on_a_later_head(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    iid = override_intent(store, worker, OID)
    assert consume(store, worker, fake, OID, iid) == [
        "check review_2 overridden by human-owner"
    ]
    task = store.row("task", OID)
    c = task["payload"]["coordinator"]
    c["phase"] = "pr_gates_codex"
    c["head_sha"] = "e" * 40
    c["lane_outcome"] = {
        "role": "reviewer-results",
        "outcomes": [{"stage": "review-2", "dimension": "quality"}],
    }
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("lane runner called")

    task = store.row("task", OID)
    assert evaluate_action(
        store, worker, task, fake, fail, action="second_review"
    ) == ["action second_review is waived by review_2; continuing at ci"]
    stored = store.row("task", OID)
    assert stored["payload"]["coordinator"]["phase"] == "ci"
    assert "lane_outcome" not in stored["payload"]["coordinator"]
    waiver = fact(store, OID, "review_2")

    task = store.row("task", OID)
    c = task["payload"]["coordinator"]
    c["phase"] = "a38_second_comment"
    c["lane_outcome"] = {
        "role": "reviewer-results",
        "outcomes": [{"stage": "review-2", "dimension": "logic"}],
    }
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    assert evaluate_action(
        store,
        worker,
        store.row("task", OID),
        fake,
        fail,
        action="second_review_comment",
    ) == [
        "action second_review_comment is waived by review_2; continuing at ci"
    ]
    stored = store.row("task", OID)
    assert stored["payload"]["coordinator"]["phase"] == "ci"
    assert "lane_outcome" not in stored["payload"]["coordinator"]
    assert evaluate_action(store, worker, stored, fake, None, action="ci") is None
    assert fact(store, OID, "review_2") == waiver
    store.close()


@pytest.mark.no_pg
def test_waiver_is_not_tested_for_an_issue_task(tmp_path) -> None:
    task = {
        "id": "task-1",
        "state": "open",
        "payload": {
            "coordinator": {
                "phase": "a38_second_comment",
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
        action="second_review_comment",
    ) is None


def test_waived_order_reaches_ready_without_review_2(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    _list_opener(fake)
    seed_review_order(
        store,
        worker,
        fake,
        OID,
        phase="pr_gates_codex",
        formal=True,
        evidence={"ci_green": True, "ci_head": fake.head},
    )
    iid = override_intent(store, worker, OID)
    assert consume(store, worker, fake, OID, iid) == [
        "check review_2 overridden by human-owner"
    ]
    task = store.row("task", OID)
    assert evaluate_action(store, worker, task, fake, None, action="ci") is None

    task = store.row("task", OID)
    task["payload"]["coordinator"]["phase"] = "readiness"
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    lines = phase_readiness(store, worker, store.row("task", OID), fake)
    assert any(f"readiness ok on {H7(fake)}" in line for line in lines)
    assert not any("missing approved gate review-2/" in line for line in lines)
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "formal_approve"

    task = store.row("task", OID)
    c = task["payload"]["coordinator"]
    c["phase"] = "leave_draft"
    c["evidence"]["formal_head"] = fake.head
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    lines = phase_leave_draft(store, worker, store.row("task", OID), fake)
    assert any("left draft" in line for line in lines)
    assert fake.ready_calls == 1
    assert store.row("task", OID)["payload"]["coordinator"]["phase"] == "await_merge"
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    ]
    assert any("one review run is approved" in row["payload"]["body"] for row in comments)
    store.close()


@pytest.mark.no_pg
def test_resume_after_second_review_waiver() -> None:
    cleared = (
        "resume_phase",
        "blocker",
        "failed_check",
        "question_text",
        "question_activity_id",
        "replies_consumed_through",
        "retry",
        "blocker_publish_once",
        "blocker_publish_kind",
        "blocker_activity_id",
    )

    def task_for(phase, *, resume=None, **extra):  # noqa: ANN001
        checkpoint = {
            "phase": phase,
            "source": order_source(OID),
            **extra,
        }
        if resume is not None:
            checkpoint["resume_phase"] = resume
        return {
            "id": OID,
            "state": "open",
            "payload": {"coordinator": checkpoint},
        }

    for phase in ("pr_gates_codex", "a38_second_comment"):
        task = task_for(phase)
        resume_after_second_review_waiver(task)
        assert task["payload"]["coordinator"]["phase"] == "ci"
        assert task["state"] == "open"

    waiting = task_for(
        "ask",
        resume="await_second_review",
        blocker="b",
        failed_check="review_2",
        question_text="q",
        question_activity_id="a",
        replies_consumed_through="r",
        retry={"phase": "await_second_review"},
        blocker_publish_once=True,
        blocker_publish_kind="provider-incomplete",
        blocker_activity_id="activity-1",
    )
    resume_after_second_review_waiver(waiting)
    checkpoint = waiting["payload"]["coordinator"]
    assert checkpoint["phase"] == "ci"
    assert waiting["state"] == "pr-review"
    assert all(key not in checkpoint for key in cleared)

    implementing = task_for("implement", blocker="keep")
    resume_after_second_review_waiver(implementing)
    assert implementing["payload"]["coordinator"] == {
        "phase": "implement",
        "source": order_source(OID),
        "blocker": "keep",
    }

    blocked = task_for("blocked", resume="implement", blocker="stop")
    resume_after_second_review_waiver(blocked)
    assert blocked["payload"]["coordinator"]["phase"] == "blocked"
    assert blocked["payload"]["coordinator"]["resume_phase"] == "implement"
    assert blocked["payload"]["coordinator"]["blocker"] == "stop"

    reviewer = task_for(
        "pr_gates_codex",
        lane_outcome={
            "role": "reviewer-results",
            "outcomes": [
                {"stage": "review-1", "dimension": "quality"},
                {"stage": "review-2", "dimension": "logic"},
            ],
        },
    )
    resume_after_second_review_waiver(reviewer)
    assert reviewer["payload"]["coordinator"]["lane_outcome"]["outcomes"] == [
        {"stage": "review-1", "dimension": "quality"}
    ]

    implementer = task_for(
        "pr_gates_codex",
        lane_outcome={"role": "implementer", "round": 1},
    )
    resume_after_second_review_waiver(implementer)
    assert implementer["payload"]["coordinator"]["lane_outcome"] == {
        "role": "implementer",
        "round": 1,
    }


def test_resolve_action_records_facts_without_outcomes(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    task["payload"]["coordinator"].pop("pr_number")
    store.write(
        "task",
        "update",
        OID,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("runner called")

    before = store.row("task", OID)
    resolved = resolve_action(
        store,
        worker,
        before,
        fail,
        None,
        action="waive_second_review",
    )
    assert not isinstance(resolved, Deferred)
    assert [check.key for check, value in resolved] == [
        "order_active",
        "draft_open",
        "one_review_permitted",
    ]
    assert [value.status for check, value in resolved] == ["ja", "pending", "nein"]
    assert resolved[-1][1].evidence == "no pull request"
    assert fact(store, OID, "one_review_permitted")["status"] == "nein"
    assert store.row("task", OID) == before
    store.close()


@pytest.mark.parametrize("mutation", ("first", "written"))
def test_override_command_refuses_from_the_store(
    tmp_path, monkeypatch, mutation
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    if mutation == "first":
        row = rows(store, OID)["review_1_logic"]
        row["status"] = "pending"
        store.write(
            "checklist_item",
            "update",
            row["id"],
            {key: item for key, item in row.items() if not key.startswith("_")},
        )
        match = "check review_2 cannot be waived: the first review is not complete"
    else:
        record_check(
            store,
            task,
            CHECKS["review_2"],
            CheckValue("ja", "second review free of findings", head=fake.head),
            action="ci",
        )
        match = f"check review_2 is not failed on order {OID}"
    with pytest.raises(OrderError, match=match):
        op_override(
            store,
            store.home,
            by="human-owner",
            order_id=OID,
            check="review_2",
            reason="one review is enough",
        )
    assert store.activity_of_types(("order.reply",)) == []
    store.close()


def test_override_command_accepts_a_pending_review_2(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = review_env(tmp_path, monkeypatch)
    task = seed_review_order(store, worker, fake, OID, phase="pr_gates_codex")
    missing = op_override(
        store,
        store.home,
        by="human-owner",
        order_id=OID,
        check="review_2",
        reason="one review is enough",
    )
    assert set(missing) == {"intent", "order_id", "check"}
    record_check(
        store,
        task,
        CHECKS["review_2"],
        CheckValue("pending", "second review not complete", head=fake.head),
        action="ci",
    )
    pending = op_override(
        store,
        store.home,
        by="human-owner",
        order_id=OID,
        check="review_2",
        reason="one review is enough",
    )
    assert set(pending) == {"intent", "order_id", "check"}
    intents = [
        store.row("activity", result["intent"])
        for result in (missing, pending)
    ]
    assert len(store.activity_of_types(("order.reply",))) == 2
    for intent in intents:
        assert intent["execution_status"] == "pending"
        assert intent["payload"]["kind"] == "override"
        assert intent["payload"]["check"] == "review_2"
    store.close()


def test_require_ready_evidence_with_a_waiver(tmp_path, monkeypatch) -> None:
    store, worker, fake = model_env(tmp_path, monkeypatch)
    task = seed_model_task(store, worker, fake, OID, phase="readiness")
    with pytest.raises(
        CoordinatorError,
        match=f"A38 first_comment evidence is missing on {H7(fake)}",
    ):
        require_ready_evidence(store, task, fake.head, second_waived=True)
    task["payload"]["coordinator"]["a38"]["first_comment"] = {
        "head": fake.head
    }
    assert require_ready_evidence(
        store, task, fake.head, second_waived=True
    ) is True
    assert require_ready_evidence(
        store,
        task,
        fake.head,
        model_second=True,
        second_waived=True,
    ) is True
    task["payload"]["coordinator"]["a38"].pop("report")
    with pytest.raises(
        CoordinatorError,
        match=f"A38 report or waiver evidence is missing on {H7(fake)}",
    ):
        require_ready_evidence(store, task, fake.head, second_waived=True)
    store.close()


def test_waived_model_task_passes_readiness_and_merge(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = model_env(tmp_path, monkeypatch)
    _list_opener(fake)
    task = seed_model_task(
        store,
        worker,
        fake,
        OID,
        phase="readiness",
        first_comment={"head": fake.head},
    )
    task = model_order(store, task, OID)
    record_waiver(store, task)

    lines = phase_readiness(store, worker, store.row("task", OID), fake)
    assert any("readiness ok" in line for line in lines)
    saved = store.row("task", OID)
    assert saved["payload"]["coordinator"]["phase"] == "await_ready"
    assert "second_comment" not in saved["payload"]["coordinator"]["a38"]

    fake.pr["isDraft"] = False
    fake.statuses = [
        {
            "context": status_context_enforce("develop"),
            "state": "success",
            "description": "A38 passed",
        }
    ]
    phase_await_ready(store, worker, saved, fake)
    saved = store.row("task", OID)
    c = saved["payload"]["coordinator"]
    assert c["evidence"]["ready_head"] == fake.head
    assert c["phase"] == "await_merge"

    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "d" * 40}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    assert phase_await_merge(store, worker, saved, fake) == [
        "human merge verified example/project#42; task done"
    ]
    assert store.row("task", OID)["state"] == "done"
    store.close()


def test_unwaived_model_task_still_requires_the_second_comment(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = model_env(tmp_path, monkeypatch)
    task = seed_model_task(
        store,
        worker,
        fake,
        OID,
        phase="readiness",
        first_comment={"head": fake.head},
    )
    task = model_order(store, task, OID)
    with pytest.raises(
        CoordinatorError,
        match=f"A38 second_comment evidence is missing on {H7(fake)}",
    ):
        phase_readiness(store, worker, task, fake)
    store.close()


def test_waived_model_task_fails_the_done_gate_when_the_opener_is_no_longer_listed(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake = model_env(tmp_path, monkeypatch)
    task = seed_model_task(
        store,
        worker,
        fake,
        OID,
        phase="readiness",
        first_comment={"head": fake.head},
    )
    task = model_order(store, task, OID)
    record_waiver(store, task)

    lines = phase_readiness(store, worker, store.row("task", OID), fake)
    assert any("readiness ok" in line for line in lines)
    saved = store.row("task", OID)
    assert saved["payload"]["coordinator"]["phase"] == "await_ready"

    fake.pr["isDraft"] = False
    fake.statuses = [
        {
            "context": status_context_enforce("develop"),
            "state": "success",
            "description": "A38 passed",
        }
    ]
    phase_await_ready(store, worker, saved, fake)
    saved = store.row("task", OID)
    assert saved["payload"]["coordinator"]["evidence"]["ready_head"] == fake.head
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"

    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "d" * 40}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    lines = phase_await_merge(store, worker, saved, fake)
    assert len(lines) == 1
    assert lines[0].startswith("human merge observed; task-done blocked:")
    assert store.row("task", OID)["state"] != "done"
    store.close()


def test_preflight_requires_one_first_model_for_order_workers(
    tmp_path, monkeypatch
) -> None:
    store, worker, fake, lane = prepare_orders(tmp_path, monkeypatch)
    fake.issues = []
    preflight_worker(store, worker, fake)
    path = store.home / "ai-accounts.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["roles"]["rev-other"] = {
        "account": "grok-w",
        "model": "other-model",
        "access": "read-only",
    }
    manifest["sessions"]["worker-session"]["lanes"][
        "grok:pr-reviewer-logic"
    ] = "rev-other"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    message = (
        "order workers require one model for the implementer and the first review: "
        "grok:pr-reviewer-logic uses other-model, grok:implementer uses grok-model"
    )
    assert run_tick(store, worker, fake, lane) == [f"preflight blocked: {message}"]
    with pytest.raises(CoordinatorError, match=message):
        create_order_task(
            store,
            worker,
            repo="example/project",
            title="Add a widget",
            text="Do it",
            submitted_by="human-owner",
        )
    assert store.rows("task") == []
    assert run_tick(store, replace(worker, orders=False), fake, lane) == ["idle"]
    store.close()
