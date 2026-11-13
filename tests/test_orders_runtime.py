from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_cli import coordinator_runtime, github_act
from agent_cli.allow import CHECKLIST_KEYS
from agent_cli.coordinator import tick
from agent_cli.coordinator_checks import registry_row_id
from agent_cli.coordinator_common import (
    CoordinatorError,
    comment_target,
    coord,
    is_order,
    order_id,
    save_task,
    source_kind,
    source_label,
    task_text_path,
)
from agent_cli.coordinator_git import (
    _write_marker,
    control_dir,
    draft_pr_body,
    refresh_draft_body,
)
from agent_cli.coordinator_github import (
    _apply_readiness_checklist,
    _mandate_provenance,
    apply_authorized_replies,
    phase_read_replies,
    pin_owner_repository,
    publish_blocker,
)
from agent_cli.coordinator_lanes import _post_issue_status, phase_pr_gates, write_review_context
from agent_cli.coordinator_orders import (
    create_order_task,
    find_order,
    list_orders,
    order_state,
    phase_accept_order,
    serves_repo,
)
from agent_cli.coordinator_runtime import (
    _apply_implementer_outcome,
    _implementer_instruction,
    _source_still_assigned,
    _spec_context,
    _verify_source_assigned,
    discover_assignments,
    execution_binding,
    has_runnable_task,
    implement_commit_message,
    phase_implement,
    refresh_execution_binding,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_order_worker,
    make_session,
    make_worker,
    order_lane_runner,
    order_source,
    patch_account_runners,
    scan_done,
    seed_order_task,
    submit_order,
    write_accounts,
    write_check_lane,
)


OID = "11111111-1111-4111-8111-111111111111"
OID2 = "33333333-3333-4333-8333-333333333333"
TID = "22222222-2222-4222-8222-222222222222"
OWNERS = dict(
    repository_owners=("example",),
    owner_check_argv=("/operator/check",),
    owner_readiness_argv=("/operator/ready",),
)


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    return store, worker, fake


def prepare_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    check_lane: bool = True,
    fake=None,
    **overrides,
):
    store = Store(tmp_path)
    write_accounts(store.home)
    if check_lane:
        write_check_lane(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_order_worker(tmp_path, **overrides)
    fake = fake or FakeGh()
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = order_lane_runner(fake)
    return store, worker, fake, lane


def run_tick(store, worker, fake, lane) -> list[str]:
    return tick(store, worker, runner=fake, lane_runner=lane)


def checkpoint(store, tid) -> dict:
    return store.row("task", tid)["payload"]["coordinator"]


def text_files(worker) -> list:
    return list((worker.workspace_root / ".coordinator-control").glob("*/task-text.md"))


def seed_issue_task(store, worker, tid: str, *, phase: str, **checkpoint) -> dict:
    c = {
        "phase": phase,
        "source": {
            "repo": "example/project",
            "number": 7,
            "assigned_id": "a",
            "publication_repo": "example/project",
            "base": "develop",
            "title": "Fix",
        },
        "branch": f"task-{tid[:8]}",
        "execution_binding": execution_binding(store, worker, "example/project"),
        **checkpoint,
    }
    row = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "title": "t",
        "repo": "example/project",
        "ref": None,
        "payload": {"coordinator": c},
        "state": "open",
        "current_round": 0,
        "models": {"first": "grok", "second": "codex"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "change_summary_en": None,
        "change_summary_de": None,
    }
    store.write("task", "insert", tid, row)
    return store.row("task", tid)


def comment_posts(store) -> list[dict]:
    return [row for row in store.rows("activity") if row.get("type") == "comment.post"]


def round_row(store, tid: str) -> dict:
    row = {
        "id": "round-1",
        "task_id": tid,
        "round": 1,
        "implementer_verdict": None,
        "reviewer_verdict": None,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
    }
    store.write("task_round", "insert", "round-1", row)
    return store.row("task_round", "round-1")


@pytest.mark.no_pg
def test_source_helpers() -> None:
    assert source_kind({}) == "github-issue"
    assert source_kind({"source": {"kind": "order"}}) == "order"
    task = {"id": "t", "payload": {"coordinator": {"source": order_source(OID)}}}
    assert is_order(task) is True
    assert order_id(task) == OID
    assert source_label(task) == f"order {OID}"
    task["payload"]["coordinator"]["source"].pop("order_id")
    assert order_id(task) == "t"
    issue = {
        "id": "t",
        "payload": {"coordinator": {"source": {"repo": "example/project", "number": 7}}},
    }
    assert is_order(issue) is False
    assert source_label(issue) == "example/project#7"


@pytest.mark.no_pg
def test_comment_target() -> None:
    issue = {"payload": {"coordinator": {"source": {"repo": "example/project", "number": 7}}}}
    assert comment_target(issue) == ("example/project", 7, "issue")
    order = {"id": OID, "repo": "example/project", "payload": {"coordinator": {"source": order_source(OID)}}}
    assert comment_target(order) is None
    order["payload"]["coordinator"]["pr_number"] = 42
    assert comment_target(order) == ("example/project", 42, "pr")
    with pytest.raises(KeyError):
        comment_target({"payload": {"coordinator": {"source": {"repo": "example/project"}}}})


def test_issue_blocker_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_issue_task(store, worker, TID, phase="tests")
    assert publish_blocker(store, worker, task, fake, "stop") == [
        "blocked: stop",
        "blocker published on example/project#7",
    ]
    rows = comment_posts(store)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert (payload["repo"], payload["number"], payload["target"]) == (
        "example/project",
        7,
        "issue",
    )
    assert rows[0]["id"] == str(
        uuid5(
            NAMESPACE_URL,
            "coordinator-blocker:worker-session:example/project:7::Blocked: stop",
        )
    )
    assert len(fake.comments) == 1 and fake.comments[0]["body"].startswith("Blocked: stop")
    assert fake.pr_comments == []


def test_order_blocker_without_draft_is_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(
        store, worker, OID, phase="blocked", resume_phase="implement"
    )

    def fail(_argv):
        pytest.fail("runner must not be called")

    assert publish_blocker(
        store, worker, task, fail, "stop", reply_checkpoint=True
    ) == ["blocked: stop", f"blocker recorded for order {OID}"]
    assert comment_posts(store) == []
    assert "question_activity_id" not in coord(store.row("task", OID))


def test_order_blocker_is_posted_on_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(
        store, worker, OID, phase="blocked", resume_phase="ci", pr_number=42
    )
    lines = publish_blocker(store, worker, task, fake, "stop", reply_checkpoint=True)
    assert lines[-1] == "blocker published on example/project#42"
    rows = comment_posts(store)
    assert len(rows) == 1
    assert rows[0]["payload"]["target"] == "pr"
    assert rows[0]["payload"]["number"] == 42
    assert rows[0]["execution_status"] == "done"
    assert len(fake.pr_comments) == 1 and "Blocked: stop" in fake.pr_comments[0]["body"]
    assert fake.comments == []
    assert coord(store.row("task", OID))["question_activity_id"] == rows[0]["id"]


def test_pull_request_comment_reply_resumes_an_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(
        store, worker, OID, phase="blocked", resume_phase="ci", pr_number=42
    )
    publish_blocker(store, worker, task, fake, "stop", reply_checkpoint=True)
    fake.pr_comments.append(
        {"id": 101, "body": "Go on.", "user": {"login": "human-owner"}}
    )
    assert phase_read_replies(store, worker, store.row("task", OID), fake) == [
        "consumed 1 authorized reply(ies); resuming ci"
    ]
    c = coord(store.row("task", OID))
    assert c["phase"] == "ci"
    assert c["authorized_replies"] == ["Go on."]
    assert c["replies_consumed_through"] == 101
    assert "resume_phase" not in c


def test_order_without_draft_waits_for_an_order_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)

    def fail(_argv):
        pytest.fail("runner must not be called")

    task = seed_order_task(
        store, worker, OID, phase="blocked", resume_phase="implement"
    )
    assert phase_read_replies(store, worker, task, fail) == [
        f"waiting for order reply on {OID}"
    ]
    second = seed_order_task(store, worker, OID2, phase="blocked")
    assert phase_read_replies(store, worker, second, fail) == [
        f"blocked: no eligible resume_phase for reply recovery on order {OID2}"
    ]


def test_apply_authorized_replies_resumes_like_the_comment_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)
    seed = dict(
        phase="blocked",
        resume_phase="implement",
        blocker_publish_once=True,
        blocker_publish_kind="status",
        blocker_activity_id="a",
        replies_consumed_through=5,
    )
    task = seed_issue_task(store, worker, TID, **seed)
    assert apply_authorized_replies(store, task, ["Do it this way."]) == [
        "consumed 1 authorized reply(ies); resuming implement"
    ]
    saved = store.row("task", TID)
    c = coord(saved)
    assert saved["state"] == "implementing" and c["phase"] == "implement"
    assert c["authorized_replies"] == ["Do it this way."]
    assert c["replies_consumed_through"] == 5
    assert not {"blocker_publish_once", "blocker_publish_kind", "blocker_activity_id", "resume_phase"} & set(c)
    second = seed_issue_task(store, worker, OID2, **seed)
    apply_authorized_replies(store, second, ["Do it."], consumed_through=9)
    assert coord(store.row("task", OID2))["replies_consumed_through"] == 9


def test_order_source_is_never_verified_against_an_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="await_merge")

    def fail(_argv):
        pytest.fail("runner must not be called")

    assert _verify_source_assigned(store, worker, coord(task), fail) is None
    assert _source_still_assigned(store, worker, coord(task), fail) is True


@pytest.mark.no_pg
def test_draft_body_for_an_order() -> None:
    assert "Draft for order 11111111: Add a widget." in draft_pr_body(order_source(OID))
    assert "Entwurf für Auftrag 11111111: Add a widget." in draft_pr_body(order_source(OID))
    assert "Draft for order 11111111: Add a widget." in draft_pr_body(
        order_source(OID, title="Add a widget.")
    )
    assert "Draft for example/project#7." in draft_pr_body(
        {"repo": "example/project", "number": 7}
    )


def test_refresh_draft_body_updates_an_order_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="implement", pr_number=42)
    task["reused_lines"] = ["`src/pay.ts:40` existing gift invoice"]
    task["added_lines"] = ["`src/habit.ts:12` member habit route"]
    save_task(store, task)
    assert refresh_draft_body(store, worker, task, fake) == [
        "posted reused and added list on example/project#42"
    ]
    assert "Draft for order 11111111: Add a widget." in fake.pr["body"]
    assert "existing gift invoice" in fake.pr["body"]
    assert "member habit route" in fake.pr["body"]


def test_review_context_uses_the_order_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    wt = worker.workspace_root / OID
    wt.mkdir()
    task = seed_order_task(
        store,
        worker,
        OID,
        phase="pr_gates_grok",
        text="ORDER-TEXT-MARKER full order text",
        worktree=str(wt),
        base_sha=fake.base,
        pr_number=42,
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        return fake(argv)

    note = write_review_context(store, worker, task, runner)
    assert "The order text takes the place of the linked issue." in note
    assert "Linked issue body" in note
    issue = control_dir(worker, OID) / f"review-base-{fake.base[:12]}-issue.txt"
    assert "ORDER-TEXT-MARKER" in issue.read_text(encoding="utf-8")
    assert not any(argv[:3] == ["gh", "issue", "view"] for argv in calls)

    wt2 = worker.workspace_root / OID2
    wt2.mkdir()
    second = seed_order_task(
        store,
        worker,
        OID2,
        phase="pr_gates_grok",
        text=" ",
        worktree=str(wt2),
        base_sha=fake.base,
        pr_number=42,
    )
    write_review_context(store, worker, second, runner)
    issue2 = control_dir(worker, OID2) / f"review-base-{fake.base[:12]}-issue.txt"
    assert issue2.read_text(encoding="utf-8") == "The order text is empty.\n"


@pytest.mark.no_pg
def test_spec_context_names_the_order() -> None:
    assert _spec_context({"source": order_source(OID)}).startswith(
        f"Source order: {OID}\n"
    )
    assert _spec_context(
        {"source": {"repo": "example/project", "number": 7}}
    ).startswith("Source issue: example/project#7\n")


@pytest.mark.no_pg
def test_commit_message_and_instruction_for_an_order(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    order = {"id": OID, "payload": {"coordinator": {"source": order_source(OID)}}}
    issue = {
        "id": TID,
        "payload": {
            "coordinator": {
                "source": {"repo": "example/project", "number": 7}
            }
        },
    }
    assert implement_commit_message(order) == "Implement the order."
    assert implement_commit_message(issue) == "Implement assigned issue."
    assert _implementer_instruction(None, worker, order, None).startswith(
        "Implement the order. Edit files only.\n"
    )
    assert _implementer_instruction(None, worker, issue, None).startswith(
        "Implement the assigned issue. Edit files only.\n"
    )


def test_order_implement_prompt_and_commit_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    wt = worker.workspace_root / OID
    wt.mkdir()
    task = seed_order_task(
        store,
        worker,
        OID,
        phase="implement",
        worktree=str(wt),
        pr_number=42,
    )
    messages = []
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_checks.verify_checkout_identity",
        lambda *args, **kwargs: {"branch": "task-11111111", "base_sha": "b" * 40},
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.review_rules_bound", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.set_checklist", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_runtime.ensure_draft", lambda *args, **kwargs: []
    )

    def sign(*args, **kwargs):
        messages.append(args[-1])
        return None

    monkeypatch.setattr("agent_cli.coordinator_runtime.stage_sign_commit_if_changes", sign)
    phase_implement(
        store,
        worker,
        task,
        fake,
        lambda _argv, _stdin: Completed(0, fake.model_outputs["implementer"], ""),
    )
    assert messages == ["Implement the order."]
    prompt = control_dir(worker, OID) / "implementer-grok.md"
    body = prompt.read_text(encoding="utf-8")
    assert f"Source order: {OID}" in body
    assert "Implement the order. Edit files only." in body
    assert coord(store.row("task", OID))["phase"] == "inner_review"


def test_order_recovery_commit_uses_the_order_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    wt = worker.workspace_root / OID
    wt.mkdir()
    agent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    task = seed_order_task(
        store,
        worker,
        OID,
        phase="implement",
        worktree=str(wt),
        lane_outcome={
            "role": "implementer",
            "round": 1,
            "agent_id": agent_id,
            "status": "complete",
            "result": "done",
            "stdout": "done",
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
            "task_id": OID,
            "round": 1,
            "role": "implementer",
            "vendor": "grok",
            "status": "done",
        },
    )
    messages = []

    def sign(*args, **kwargs):
        messages.append(args[-1])
        return None

    def draft_pending(store, worker, task, runner):
        coord(task)["phase"] = "publish_draft"
        save_task(store, task)
        return ["draft pending after push"]

    monkeypatch.setattr("agent_cli.coordinator_runtime.stage_sign_commit_if_changes", sign)
    monkeypatch.setattr("agent_cli.coordinator_runtime.ensure_draft", draft_pending)
    monkeypatch.setattr(
        "agent_cli.coordinator_checks.verify_checkout_identity",
        lambda *args, **kwargs: {"branch": "task-11111111", "base_sha": "b" * 40},
    )
    phase_implement(store, worker, task, fake, None)
    assert messages == ["Implement the order."]


def test_order_question_is_posted_on_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="implement", pr_number=42)
    lines = _apply_implementer_outcome(
        store,
        worker,
        task,
        fake,
        round_num=1,
        tr=round_row(store, OID),
        status="complete",
        model_result="ask",
        stdout="Which behavior is required?",
        replay=None,
        sha=None,
        draft_lines=[],
    )
    assert lines == ["ask posted on example/project#42"]
    c = coord(store.row("task", OID))
    assert c["phase"] == "ask" and c["resume_phase"] == "implement"
    assert c["question_text"] == "Which behavior is required?"
    rows = comment_posts(store)
    assert c["question_activity_id"] == rows[0]["id"]
    assert rows[0]["payload"]["target"] == "pr"
    assert rows[0]["payload"]["number"] == 42
    assert "Question:\nWhich behavior is required?" in fake.pr_comments[0]["body"]
    assert store.row("task_round", "round-1")["implementer_verdict"] == "blocked"


def test_issue_question_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_issue_task(store, worker, TID, phase="implement")
    lines = _apply_implementer_outcome(
        store,
        worker,
        task,
        fake,
        round_num=1,
        tr=round_row(store, TID),
        status="complete",
        model_result="ask",
        stdout="Which behavior is required?",
        replay=None,
        sha=None,
        draft_lines=[],
    )
    assert lines == ["ask posted on example/project#7"]
    rows = comment_posts(store)
    assert rows[0]["payload"]["target"] == "issue"
    assert rows[0]["payload"]["number"] == 7
    assert "question_text" not in coord(store.row("task", TID))


def test_order_question_without_draft_is_kept_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="implement")

    def fail(_argv):
        pytest.fail("runner must not be called")

    lines = _apply_implementer_outcome(
        store,
        worker,
        task,
        fail,
        round_num=1,
        tr=round_row(store, OID),
        status="complete",
        model_result="ask",
        stdout="Which behavior is required?",
        replay=None,
        sha=None,
        draft_lines=[],
    )
    assert lines == [
        "blocked: Failed to publish implementer question: question has no comment target",
        f"blocker recorded for order {OID}",
    ]
    c = coord(store.row("task", OID))
    assert c["phase"] == "blocked" and c["resume_phase"] == "implement"
    assert c["pending_question"] == "Which behavior is required?"
    assert c["blocker"] == "ask publish failed: question has no comment target"
    assert comment_posts(store) == []


def test_order_status_goes_to_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="tests", pr_number=42)
    activity_id = _post_issue_status(
        store, worker, fake, task, "Blocked: lane interrupted.", "uncertain-lane"
    )
    row = store.row("activity", activity_id)
    assert row["payload"]["target"] == "pr" and row["payload"]["number"] == 42
    assert len(fake.pr_comments) == 1
    second = seed_order_task(store, worker, OID2, phase="tests")
    with pytest.raises(CoordinatorError, match="status has no comment target"):
        _post_issue_status(
            store,
            worker,
            fake,
            second,
            "Blocked: lane interrupted.",
            "uncertain-lane",
        )


@pytest.mark.no_pg
def test_mandate_provenance_of_an_order() -> None:
    assert _mandate_provenance(order_source(OID)) == {
        "repo": "example/project",
        "kind": "order",
        "order_id": OID,
        "submitted_by": "human-owner",
        "submitted_at": "2026-01-01T00:00:00Z",
    }


def owner_pin_fixture(tmp_path: Path):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        repository_owners=("example",),
        owner_check_argv=("/operator/check",),
        owner_readiness_argv=("/operator/ready",),
    )
    source = order_source(
        OID,
        repo="example/owned",
        owner_scoped=True,
        require_hub_assignment=False,
        publication_repo=None,
        base=None,
    )
    task = seed_order_task(store, worker, OID, phase="accept", source=source)

    def scoped_runner(argv):
        if argv[-2:] == ["--jq", ".default_branch"]:
            return Completed(0, "main\n", "")
        if argv == ["gh", "api", "repos/example/owned"]:
            return Completed(0, '{"permissions": {"push": true}}', "")
        pytest.fail(f"unexpected command: {argv}")

    return store, worker, task, scoped_runner


def test_pin_owner_repository_honours_the_requested_base(tmp_path: Path) -> None:
    store, worker, task, scoped_runner = owner_pin_fixture(tmp_path)
    assert pin_owner_repository(
        store,
        worker,
        task,
        lambda _argv: pytest.fail("unscoped runner must not be called"),
        scoped_runner,
        SimpleNamespace(login="worker-bot"),
        base="release",
    ) is None
    source = coord(task)["source"]
    assert source["base"] == "release"
    assert source["publication_repo"] == "example/owned"
    assert source["repository_config"] == {
        "repo": "example/owned",
        "base": "release",
        "publication_repo": "example/owned",
    }
    assert len(coord(task)["execution_binding"]) == 64
    int(coord(task)["execution_binding"], 16)
    refresh_execution_binding(store, worker, task)

    def fail(_argv):
        pytest.fail("configured source must not be looked up again")

    assert pin_owner_repository(
        store,
        worker,
        task,
        fail,
        fail,
        SimpleNamespace(login="worker-bot"),
        base="different",
    ) is None


def test_pin_owner_repository_without_base_matches_the_issue_path(tmp_path: Path) -> None:
    store, worker, task, scoped_runner = owner_pin_fixture(tmp_path)
    assert pin_owner_repository(
        store,
        worker,
        task,
        lambda _argv: pytest.fail("unscoped runner must not be called"),
        scoped_runner,
        SimpleNamespace(login="worker-bot"),
    ) is None
    source = coord(task)["source"]
    assert source["base"] == "main"
    assert source["repository_config"]["base"] == "main"


def test_pin_owner_repository_refuses_an_invalid_base(tmp_path: Path) -> None:
    store, worker, task, scoped_runner = owner_pin_fixture(tmp_path)
    with pytest.raises(StoreError, match="invalid base branch"):
        pin_owner_repository(
            store,
            worker,
            task,
            lambda _argv: pytest.fail("unscoped runner must not be called"),
            scoped_runner,
            SimpleNamespace(login="worker-bot"),
            base="bad..name",
        )
    assert "repository_config" not in coord(task)["source"]
    assert "execution_binding" not in coord(task)


def test_pin_owner_repository_leaves_a_configured_source_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake = prepare(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, OID, phase="accept")
    before = deepcopy(coord(task))

    def fail(_argv):
        pytest.fail("runner must not be called")

    assert pin_owner_repository(
        store,
        worker,
        task,
        fail,
        fail,
        SimpleNamespace(login="worker-bot"),
    ) is None
    assert coord(task) == before


class GateStore:
    def __init__(self, verdict: str = "approved") -> None:
        self.verdict = verdict

    def rows(self, table):
        assert table == "review_gate"
        return [
            {
                "id": "gate-1",
                "_origin_device_id": "test-device",
                "task_id": "task-1",
                "stage": "review-1",
                "dimension": "quality",
                "verdict": self.verdict,
                "head_sha": "a" * 40,
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        ]

    def row_insert_sequences(self, table):
        return {
            row["id"]: (row["_origin_device_id"], sequence)
            for sequence, row in enumerate(self.rows(table), start=1)
        }


def test_owner_a38_order_closes_deviation_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda _store, _task, key, status, evidence, **kwargs: calls.append(
            (key, status, evidence, kwargs)
        ),
    )
    task = {
        "id": "task-1",
        "state": "open",
        "payload": {
            "coordinator": {
                "phase": "readiness",
                "source": order_source(OID, owner_scoped=True),
            }
        },
    }
    _apply_readiness_checklist(
        GateStore(),
        replace(make_worker(tmp_path), a38=True, review_session=None),
        task,
        {"owner_a38_default": True, "deviation": {"declared": False}},
        head="a" * 40,
    )
    evidence = f"order {OID} submitted by human-owner: orders declare no deviation"
    assert calls == [
        (
            "contributing_ok",
            "ja",
            f"conformity-a approved at {'a' * 40}",
            {"source": "script"},
        ),
        ("deviation_declared", "n_a", evidence, {"source": "human"}),
        ("deviation_granted", "n_a", evidence, {"source": "human"}),
    ]


def test_owner_a38_order_still_requires_the_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "agent_cli.coordinator_github.set_checklist",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    task = {
        "id": "task-1",
        "state": "open",
        "payload": {
            "coordinator": {
                "phase": "readiness",
                "source": order_source(OID, owner_scoped=True),
            }
        },
    }
    with pytest.raises(CoordinatorError, match="approved review-1/quality"):
        _apply_readiness_checklist(
            GateStore("rejected"),
            replace(make_worker(tmp_path), a38=True, review_session=None),
            task,
            {"owner_a38_default": True, "deviation": {"declared": False}},
            head="a" * 40,
        )
    assert calls == []


def test_incomplete_review_blocker_of_an_order_goes_to_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = prepare(tmp_path, monkeypatch)
    fake.model_outputs["pr-reviewer-quality"] = "STATUS: unavailable\n"
    fake.model_outputs["pr-reviewer-logic"] = "STATUS: complete\nRESULT: approved\n"
    wt = worker.workspace_root / OID
    wt.mkdir()
    (wt / ".git").mkdir()
    seed_order_task(
        store,
        worker,
        OID,
        phase="pr_gates_grok",
        worktree=str(wt),
        base_sha=fake.base,
        head_sha=fake.head,
        pr_number=42,
        evidence={"tests_pass": True, "tests_head": fake.head},
    )
    completed = {
        "session_registered",
        "spec_written",
        "implementer_done",
        "reviewer_approved",
        "local_check_pass",
        "pushed",
    }
    for key in CHECKLIST_KEYS["implement"]:
        status = "ja" if key in completed else "pending"
        row_id = f"{OID}-{key}"
        store.write(
            "checklist_item",
            "insert",
            row_id,
            {
                "id": row_id,
                "task_id": OID,
                "key": key,
                "status": status,
                "evidence": "seed" if status == "ja" else None,
                "source": (
                    "human"
                    if key
                    in ("spec_written", "deviation_declared", "deviation_granted")
                    else "script"
                ),
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
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
            "branch": "task-11111111",
            "base": "develop",
            "base_sha": fake.base,
            "worktree": str(wt),
            "worker_login": "worker-bot",
        },
    )
    lines = phase_pr_gates(
        store,
        worker,
        store.row("task", OID),
        fake,
        lane_runner(fake),
        vendor="grok",
        stage="review-1",
    )
    c = coord(store.row("task", OID))
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "pr_gates_grok"
    assert c["blocker_publish_kind"] == "provider-incomplete"
    qid = c["question_activity_id"]
    row = store.row("activity", qid)
    assert row["payload"]["number"] == 42
    assert row["payload"]["target"] == "pr"
    assert row["execution_status"] == "done"
    assert c["blocker_activity_id"] == qid
    assert len(fake.pr_comments) == 1
    assert fake.comments == []
    assert "blocker published on example/project#42" in lines
    reviewers = [
        row
        for row in store.rows("agent")
        if row.get("task_id") == OID
        and str(row.get("role") or "").startswith("pr-reviewer-")
    ]
    assert len(reviewers) == 2
    for reviewer in reviewers:
        prompt = reviewer["prompt"]
        assert f"Order: {OID} Add a widget" in prompt
        assert "The order text takes the place of the linked issue." in prompt
        assert not any(line.startswith("Issue: ") for line in prompt.splitlines())


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ({"state": "done", "payload": {"coordinator": {}}}, "merged"),
        (
            {
                "state": "failed",
                "payload": {"coordinator": {"failed_kind": "cancelled"}},
            },
            "cancelled",
        ),
        (
            {
                "state": "failed",
                "payload": {"coordinator": {"failed_kind": "no-change"}},
            },
            "failed",
        ),
        (
            {"state": "open", "payload": {"coordinator": {"phase": "blocked"}}},
            "failed",
        ),
        (
            {"state": "open", "payload": {"coordinator": {"phase": "queued"}}},
            "queued",
        ),
        (
            {
                "state": "open",
                "payload": {
                    "coordinator": {
                        "phase": "blocked",
                        "resume_phase": "implement",
                    }
                },
            },
            "waiting",
        ),
        (
            {
                "state": "open",
                "payload": {
                    "coordinator": {"phase": "ask", "resume_phase": "implement"}
                },
            },
            "waiting",
        ),
        (
            {
                "state": "open",
                "payload": {
                    "coordinator": {
                        "phase": "implement",
                        "binding_blocker": "x",
                    }
                },
            },
            "waiting",
        ),
        (
            {
                "state": "open",
                "payload": {
                    "coordinator": {
                        "phase": "await_merge",
                        "evidence": {"ready_head": "a" * 40},
                    }
                },
            },
            "ready",
        ),
        (
            {
                "state": "open",
                "payload": {
                    "coordinator": {
                        "phase": "ask",
                        "resume_phase": "implement",
                        "evidence": {"ready_head": "a" * 40},
                    }
                },
            },
            "waiting",
        ),
        (
            {
                "state": "open",
                "payload": {"coordinator": {"phase": "implement"}},
            },
            "running",
        ),
        ({"state": "open", "payload": {"coordinator": {}}}, "running"),
    ],
)
def test_order_state(task: dict, expected: str) -> None:
    assert order_state(task) == expected


@pytest.mark.no_pg
def test_serves_repo(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    assert serves_repo(worker, "example/project") is True
    assert serves_repo(worker, "Example/Project") is True
    assert serves_repo(worker, "example/other") is False
    owner_worker = replace(worker, repository_owners=("example",))
    assert serves_repo(owner_worker, "example/other") is True
    assert serves_repo(owner_worker, "Example/other") is True
    assert serves_repo(owner_worker, "other/thing") is False


@pytest.mark.no_pg
def test_accept_handler_depends_on_the_source_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_runtime, "phase_accept_order", lambda *a: ["order"])
    monkeypatch.setattr(coordinator_runtime, "phase_accept", lambda *a: ["issue"])
    order = {
        "id": OID,
        "payload": {"coordinator": {"source": order_source(OID)}},
    }
    handlers = coordinator_runtime.phase_handlers(None, None, order, None, None)
    assert handlers["accept"]() == ["order"]
    issue = {
        "id": TID,
        "payload": {
            "coordinator": {"source": {"repo": "example/project", "number": 7}}
        },
    }
    handlers = coordinator_runtime.phase_handlers(None, None, issue, None, None)
    assert handlers["accept"]() == ["issue"]
    assert "queued" not in handlers


@pytest.mark.no_pg
def test_queued_order_is_not_dispatched(tmp_path: Path) -> None:
    task = {
        "id": OID,
        "state": "open",
        "payload": {
            "coordinator": {"phase": "queued", "source": order_source(OID)}
        },
    }
    assert coordinator_runtime._advance_task(
        SimpleNamespace(),
        make_worker(tmp_path),
        task,
        runner=None,
        lane_runner=None,
    ) == [f"order queued {OID}"]


def test_create_order_task_writes_one_queued_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    task = create_order_task(
        store,
        worker,
        repo="Example/Project",
        title="Add a widget",
        text="Do it",
        submitted_by="Human-Owner",
        notify_session="concierge-1",
    )
    assert len(store.rows("task")) == 1
    assert task["workflow"] == "implement"
    assert task["state"] == "open"
    assert task["current_round"] == 0
    assert task["ref"] is None
    assert task["repo"] == "example/project"
    assert task["title"] == "worker-s - Add a widget"
    assert task["models"] == {"first": "grok", "second": "codex"}
    assert "implementer" not in task
    assert task["created_at"] == task["updated_at"]
    assert "source_issue" not in task["payload"]
    c = coord(task)
    assert c["phase"] == "queued"
    assert c["branch"] == f"task-{task['id'][:8]}"
    assert c["worker_session"] == "worker-session"
    assert c["worker_login"] == "worker-bot"
    assert "a38" not in c
    assert c["execution_binding"] == execution_binding(
        store, worker, "example/project"
    )
    source = c["source"]
    assert source["kind"] == "order"
    assert source["order_id"] == task["id"]
    assert source["repo"] == "example/project"
    assert source["title"] == "Add a widget"
    assert source["body"] == "Do it"
    assert source["body_chars"] == 5
    assert source["submitted_by"] == "human-owner"
    assert source["submitted_at"] == task["created_at"]
    assert source["notify_session"] == "concierge-1"
    assert source["publication_repo"] == "example/project"
    assert source["base"] == "develop"
    assert not {"requested_base", "owner_scoped", "number", "assigned_id"} & set(
        source
    )
    text_path = task_text_path(worker, task["id"])
    assert source["body_sha256"] == hashlib.sha256(text_path.read_bytes()).hexdigest()
    assert store.rows("checklist_item") == []
    assert store.rows("activity") == []
    assert order_state(task) == "queued"
    store.close()


def test_create_order_task_refuses_a_worker_without_orders(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    with pytest.raises(
        CoordinatorError, match="worker worker-session does not accept orders"
    ):
        create_order_task(
            store,
            worker,
            repo="example/project",
            title="Add a widget",
            text="Do it",
            submitted_by="human-owner",
        )
    assert store.rows("task") == []
    assert text_files(worker) == []
    store.close()


def test_create_order_task_refuses_an_unserved_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    with pytest.raises(
        CoordinatorError, match="worker worker-session does not serve other/thing"
    ):
        submit_order(store, worker, repo="other/thing")
    assert store.rows("task") == []
    assert text_files(worker) == []
    store.close()


def test_create_order_task_requires_an_active_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    write_check_lane(store.home)
    worker = make_order_worker(tmp_path)
    with pytest.raises(CoordinatorError, match="session worker-session not found"):
        submit_order(store, worker)
    assert store.rows("task") == []
    assert text_files(worker) == []
    store.close()


def test_create_order_task_requires_the_check_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(
        tmp_path, monkeypatch, check_lane=False
    )
    with pytest.raises(
        CoordinatorError,
        match="No AI lane configured for session worker-session slot grok:check",
    ):
        submit_order(store, worker)
    assert store.rows("task") == []
    assert text_files(worker) == []
    store.close()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("implementer", "separate implementer is not supported"),
        ("second", "only for A38 workers"),
    ],
)
def test_create_order_task_applies_the_creation_refusals_of_issue_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if case == "implementer":
        data["implementer"] = "deepseek"
    else:
        data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CoordinatorError, match=message):
        submit_order(store, worker)
    assert store.rows("task") == []
    assert text_files(worker) == []
    store.close()


def test_a38_order_pins_the_a38_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(
        tmp_path,
        monkeypatch,
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data), encoding="utf-8")
    task = submit_order(store, worker)
    assert coord(task)["a38"] == {"enabled": True}
    assert task["models"] == {"first": "grok", "second": None}
    store.close()


def test_create_order_task_refuses_another_base_for_a_configured_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    with pytest.raises(
        CoordinatorError,
        match=(
            "base main differs from the configured base develop of example/project"
        ),
    ):
        submit_order(store, worker, requested_base="main")
    assert store.rows("task") == []
    assert text_files(worker) == []
    task = submit_order(store, worker, requested_base="develop")
    assert "requested_base" not in coord(task)["source"]
    store.close()


def test_owner_scoped_order_is_created_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(
        tmp_path, monkeypatch, **OWNERS
    )
    task = submit_order(
        store, worker, repo="example/owned", requested_base="release"
    )
    source = coord(task)["source"]
    assert source["owner_scoped"] is True
    assert source["require_hub_assignment"] is False
    assert source["requested_base"] == "release"
    assert "base" not in source
    assert "publication_repo" not in source
    assert "execution_binding" not in coord(task)
    assert task["repo"] == "example/owned"
    second = submit_order(store, worker, repo="example/owned")
    assert "requested_base" not in coord(second)["source"]
    store.close()


def test_order_pins_the_round_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    first = submit_order(store, worker)
    assert coord(first)["round_limit"] == {
        "max_rounds": 10,
        "origin": "default",
        "model": "grok-model",
        "limit": 10,
    }
    model_worker = replace(worker, max_rounds_by_model={"grok-model": 3})
    second = submit_order(store, model_worker)
    assert coord(second)["round_limit"] == {
        "max_rounds": 3,
        "origin": "model",
        "model": "grok-model",
        "limit": 3,
    }
    third = submit_order(store, model_worker, max_rounds=4)
    assert coord(third)["round_limit"] == {
        "max_rounds": 4,
        "origin": "submit",
        "model": "grok-model",
        "limit": 4,
    }
    store.close()


def test_create_order_task_removes_the_text_when_the_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)

    def raising(*_args, **_kwargs):
        raise StoreError("insert refused")

    monkeypatch.setattr(store, "write", raising)
    with pytest.raises(StoreError, match="insert refused"):
        submit_order(store, worker)
    assert text_files(worker) == []
    store.close()


def test_preflight_requires_the_check_lane_for_order_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(
        tmp_path, monkeypatch, check_lane=False
    )
    lines = run_tick(store, worker, fake, lane)
    assert len(lines) == 1
    assert lines[0].startswith("preflight blocked: ")
    assert "No AI lane configured" in lines[0]
    assert "grok:check" in lines[0]
    fake.issues = []
    assert run_tick(store, replace(worker, orders=False), fake, lane) == ["idle"]
    store.close()


def test_preflight_refuses_a_writable_check_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(
        tmp_path, monkeypatch, check_lane=False
    )
    write_check_lane(store.home, role="impl")
    lines = run_tick(store, worker, fake, lane)
    assert len(lines) == 1
    assert lines[0].startswith("preflight blocked: ")
    assert "requires read-only access" in lines[0]
    store.close()


def test_queued_order_is_promoted_and_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(tmp_path, monkeypatch)
    fake.issues = []
    order = submit_order(store, worker)
    lines = run_tick(store, worker, fake, lane)
    assert f"order accepted {order['id']}" in lines
    saved = store.row("task", order["id"])
    c = coord(saved)
    assert c["phase"] == "checkout"
    assert c["accepted_at"]
    rows = [
        row
        for row in store.rows("checklist_item")
        if row.get("task_id") == order["id"]
    ]
    for key in CHECKLIST_KEYS["implement"]:
        assert sum(row["key"] == key for row in rows) == 1
    registered = next(row for row in rows if row["key"] == "session_registered")
    assert registered["status"] == "ja"
    written = next(row for row in rows if row["key"] == "spec_written")
    assert written["status"] == "ja"
    assert written["source"] == "human"
    assert written["evidence"].startswith(
        f"order {order['id']} submitted by human-owner at "
    )
    for key in ("order_authorized", "spec_available"):
        assert store.row(
            "checklist_item", registry_row_id(order["id"], key)
        )["status"] == "ja"
    assert not any(row.get("type") == "comment.post" for row in store.rows("activity"))
    assert order_state(saved) == "running"
    store.close()


def test_order_waits_in_the_queue_while_the_worker_has_a_runnable_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(tmp_path, monkeypatch)
    seed_issue_task(store, worker, TID, phase="tests")
    monkeypatch.setattr(
        coordinator_runtime, "phase_tests", lambda *a, **k: ["tests handler"]
    )
    order = submit_order(store, worker)
    queued_updated_at = order["updated_at"]
    for _ in range(2):
        assert "tests handler" in run_tick(store, worker, fake, lane)
        saved_order = store.row("task", order["id"])
        assert coord(saved_order)["phase"] == "queued"
        assert saved_order["updated_at"] == queued_updated_at

    issue = store.row("task", TID)
    coord(issue)["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        TID,
        {key: value for key, value in issue.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(
        coordinator_runtime,
        "phase_await_merge",
        lambda *a, **k: ["merge handler"],
    )
    lines = run_tick(store, worker, fake, lane)
    assert "merge handler" in lines
    assert f"order accepted {order['id']}" in lines
    assert checkpoint(store, order["id"])["phase"] == "checkout"
    store.close()


def test_oldest_queued_order_is_promoted_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(tmp_path, monkeypatch)
    fake.issues = []
    with monkeypatch.context() as patch:
        patch.setattr(
            "agent_cli.coordinator_orders.utcnow",
            lambda: "2026-03-01T00:00:00Z",
        )
        first = submit_order(store, worker)
    with monkeypatch.context() as patch:
        patch.setattr(
            "agent_cli.coordinator_orders.utcnow",
            lambda: "2026-02-01T00:00:00Z",
        )
        older = submit_order(store, worker)
    lines = run_tick(store, worker, fake, lane)
    assert checkpoint(store, older["id"])["phase"] == "checkout"
    assert checkpoint(store, first["id"])["phase"] == "queued"
    assert sum(line.startswith("order accepted") for line in lines) == 1
    run_tick(store, worker, fake, lane)
    assert checkpoint(store, first["id"])["phase"] == "queued"
    store.close()


def test_discovery_is_skipped_while_an_order_is_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    submit_order(store, worker)
    assert has_runnable_task(store, worker) is True

    def fail(_argv):
        pytest.fail("runner must not be called")

    assert discover_assignments(store, worker, fail) == []
    store.close()


def test_phase_accept_order_makes_no_github_call_for_a_configured_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    seed_order_task(store, worker, OID, phase="accept")

    def fail(_argv):
        pytest.fail("runner must not be called")

    assert phase_accept_order(
        store, worker, store.row("task", OID), fail
    ) == [f"order accepted {OID}"]
    c = checkpoint(store, OID)
    assert c["phase"] == "checkout"
    assert c["accepted_at"]
    store.close()


def test_list_and_find_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, _fake, _lane = prepare_orders(tmp_path, monkeypatch)
    seed_issue_task(store, worker, TID, phase="tests")
    seed_order_task(store, worker, OID, phase="implement")
    with monkeypatch.context() as patch:
        patch.setattr(
            "agent_cli.coordinator_orders.utcnow",
            lambda: "2026-02-01T00:00:00Z",
        )
        submitted = submit_order(store, worker)
    assert [row["id"] for row in list_orders(store)] == [OID, submitted["id"]]
    assert find_order(store, OID)["id"] == OID
    assert find_order(store, TID) is None
    assert find_order(store, "44444444-4444-4444-8444-444444444444") is None
    store.close()


def test_order_runs_to_implement_without_issue_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingGh(FakeGh):
        def __init__(self) -> None:
            super().__init__()
            self.argvs: list[list[str]] = []

        def __call__(self, argv: list[str]) -> Completed:
            self.argvs.append(list(argv))
            return super().__call__(argv)

    fake = RecordingGh()
    store, worker, fake, lane = prepare_orders(
        tmp_path, monkeypatch, fake=fake
    )
    fake.issues = []
    order = submit_order(store, worker)
    phases = []
    for _ in range(6):
        run_tick(store, worker, fake, lane)
        phase = checkpoint(store, order["id"])["phase"]
        phases.append(phase)
        if phase == "implement":
            break
    assert "checkout" in phases
    assert "start_draft" in phases
    assert phases.index("checkout") < phases.index("start_draft")
    assert phases[-1] == "implement"
    assert store.row(
        "checklist_item", registry_row_id(order["id"], "duplicates_checked")
    )["status"] == "ja"
    assert fake.empty_commits == 1
    opened = [row for row in store.rows("activity") if row.get("type") == "pr.open"]
    assert len(opened) == 1
    assert "Draft for order" in opened[0]["payload"]["body"]
    assert checkpoint(store, order["id"])["pr_number"] == 42
    assert not any(
        any("/issues/7" in element or "issues?assignee=" in element for element in argv)
        for argv in fake.argvs
    )
    assert "check" not in fake.launched
    assert "implementer" not in fake.launched
    store.close()


def test_order_with_an_open_pull_request_runs_the_check_lane_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(tmp_path, monkeypatch)
    fake.issues = []
    fake.open_pulls = [
        {
            "number": 5,
            "title": "Other work",
            "body": "Something else",
            "head": {"ref": "feature"},
            "user": {"login": "someone"},
        }
    ]
    order = submit_order(store, worker)
    for _ in range(6):
        run_tick(store, worker, fake, lane)
        if checkpoint(store, order["id"])["phase"] == "implement":
            break
    assert checkpoint(store, order["id"])["phase"] == "implement"
    assert fake.launched.count("check") == 1
    store.close()


def test_owner_scoped_order_pins_the_requested_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OwnerGh(FakeGh):
        def __call__(self, argv: list[str]) -> Completed:
            if argv[-2:] == ["--jq", ".default_branch"]:
                return Completed(0, "main\n", "")
            if argv == ["gh", "api", "repos/example/owned"]:
                return Completed(
                    0, json.dumps({"permissions": {"push": True}}), ""
                )
            return super().__call__(argv)

    fake = OwnerGh()
    store, worker, fake, lane = prepare_orders(
        tmp_path, monkeypatch, fake=fake, **OWNERS
    )
    fake.issues = []
    order = submit_order(
        store, worker, repo="example/owned", requested_base="release"
    )
    run_tick(store, worker, fake, lane)
    c = checkpoint(store, order["id"])
    assert c["phase"] == "checkout"
    assert c["accepted_at"]
    source = c["source"]
    assert source["base"] == "release"
    assert source["publication_repo"] == "example/owned"
    assert source["repository_config"] == {
        "repo": "example/owned",
        "base": "release",
        "publication_repo": "example/owned",
    }
    assert len(c["execution_binding"]) == 64
    assert all(character in "0123456789abcdef" for character in c["execution_binding"])
    lines = run_tick(store, worker, fake, lane)
    c = checkpoint(store, order["id"])
    assert "binding_blocker" not in c
    assert not any(
        "task execution configuration changed or is unpinned" in line
        for line in lines
    )
    store.close()


def test_failed_acceptance_blocks_with_a_resumable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake, lane = prepare_orders(
        tmp_path, monkeypatch, **OWNERS
    )
    fake.issues = []
    order = submit_order(store, worker, repo="example/owned")
    lines = run_tick(store, worker, fake, lane)
    assert lines[-1] == f"blocker recorded for order {order['id']}"
    c = checkpoint(store, order["id"])
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "accept"
    assert c["blocker"]
    assert "accepted_at" not in c
    assert "repository_config" not in c["source"]
    assert order_state(store.row("task", order["id"])) == "waiting"
    assert not any(row.get("type") == "comment.post" for row in store.rows("activity"))
    store.close()
