from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_cli.allow import CHECKLIST_KEYS
from agent_cli.coordinator_common import (
    CoordinatorError,
    comment_target,
    coord,
    is_order,
    order_id,
    save_task,
    source_kind,
    source_label,
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
from agent_cli.coordinator_runtime import (
    _apply_implementer_outcome,
    _implementer_instruction,
    _source_still_assigned,
    _spec_context,
    _verify_source_assigned,
    execution_binding,
    implement_commit_message,
    phase_implement,
    refresh_execution_binding,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    order_source,
    patch_account_runners,
    seed_order_task,
    write_accounts,
)


OID = "11111111-1111-4111-8111-111111111111"
OID2 = "33333333-3333-4333-8333-333333333333"
TID = "22222222-2222-4222-8222-222222222222"


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    return store, worker, fake


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
