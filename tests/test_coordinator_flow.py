"""Successive-tick integration flow for the issue coordinator.

Drives script-generated facts through acceptance → checkout → implement →
immediate Draft → inner review → checks → parallel Grok PR gates → Codex PR
gates → CI → readiness → formal approve → Ready → human merge.

Fake GitHub/model/check transports only. No real network/models/tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.coordinator import tick
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    patch_account_runners,
    patch_command_runner,
    scan_done,
    write_accounts,
)


def seed_task(store, worker, tid, data):
    """Seed an isolated checkpoint with its real configured binding; no check/gate changes."""
    from agent_cli.coordinator_runtime import execution_binding
    checkpoint = data.get("payload", {}).get("coordinator", {})
    source = checkpoint.get("source", {})
    checkpoint["execution_binding"] = execution_binding(store, worker, source["repo"])
    store.write("task", "insert", tid, data)


def _phase(store: Store) -> str | None:
    tasks = store.rows("task")
    if not tasks:
        return None
    return (tasks[0].get("payload") or {}).get("coordinator", {}).get("phase")


def test_task_binding_ignores_unrelated_profiles_but_pins_selected_model(tmp_path):
    from agent_cli.coordinator_runtime import execution_binding
    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    before = execution_binding(store, worker, 'example/project')
    path = store.home / 'ai-accounts.json'
    configuration = json.loads(path.read_text())
    configuration['accounts']['unrelated'] = {'provider': 'grok', 'config_dir': '/test/unrelated'}
    path.write_text(json.dumps(configuration))
    assert execution_binding(store, worker, 'example/project') == before
    configuration['roles']['impl']['model'] = 'explicitly-changed-model'
    path.write_text(json.dumps(configuration))
    assert execution_binding(store, worker, 'example/project') != before


def test_successive_ticks_to_human_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.launched == []
    tick(store, worker, runner=fake, lane_runner=lane)

    for _ in range(3):
        if _phase(store) == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    assert "implementer" in fake.launched
    assert fake.commit_used_S
    task = store.rows("task")[0]
    assert task["payload"]["coordinator"].get("head_sha")
    for _ in range(4):
        phase = _phase(store)
        if phase in ("inner_review", "tests", "pr_gates_grok"):
            break
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert task.get("ref") == "42" or task["payload"]["coordinator"].get("pr_number") == 42

    for _ in range(3):
        if _phase(store) in ("tests", "pr_gates_grok"):
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert "reviewer" in fake.launched

    for _ in range(2):
        if _phase(store) == "pr_gates_grok":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "pr_gates_grok", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    before = list(fake.launched)
    tick(store, worker, runner=fake, lane_runner=lane)
    launched = fake.launched[len(before) :]
    assert "pr-reviewer-quality" in launched
    assert "pr-reviewer-logic" in launched
    assert fake.parallel_launch_seen
    assert _phase(store) == "pr_gates_codex", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    before = list(fake.launched)
    tick(store, worker, runner=fake, lane_runner=lane)
    launched = fake.launched[len(before) :]
    assert any("pr-reviewer" in r for r in launched)
    assert _phase(store) == "ci", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "readiness", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "formal_approve", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    tick(store, worker, runner=fake, lane_runner=lane)
    assert any(r.get("state") == "APPROVED" for r in fake.reviews)
    assert all(
        r.get("commit_id") == fake.head for r in fake.reviews if r.get("state") == "APPROVED"
    )
    assert _phase(store) == "leave_draft", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.pr["isDraft"] is False
    assert _phase(store) == "await_merge", store.rows("task")[0]["payload"]["coordinator"].get("blocker")

    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert task["state"] == "done", lines
    assert task.get("change_summary_en") and task.get("change_summary_de")
    assert any(r.get("type") == "pr.merged" for r in store.rows("activity"))
    assert any(r.get("type") == "issue.assigned.ack" for r in store.rows("activity"))
    assert all(item["status"] in {"ja", "n_a"} for item in store.rows("checklist_item")
               if item.get("task_id") == task["id"])


def test_stale_head_invalidates_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    tid = "77777777-7777-7777-7777-777777777777"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    old = fake.head
    seed_task(store, worker,
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
                    "phase": "ci",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "worktree": str(wt),
                    "branch": "task-77777777",
                    "base_sha": fake.base,
                    "head_sha": old,
                    "pr_number": 42,
                    "evidence": {
                        "tests_pass": True,
                        "tests_head": old,
                        "gates_head": old,
                        "ci_green": True,
                        "ci_head": old,
                    },
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    fake.pr["headRefOid"] = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert any("invalidat" in line.lower() or "head changed" in line.lower() for line in lines)
    assert task["payload"]["coordinator"]["phase"] in ("tests", "implement", "ci")


def test_crash_window_publish_draft_no_second_implementer(tmp_path, monkeypatch):
    from agent_cli import coordinator_runtime as runtime
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    lane = lane_runner(fake)
    for _ in range(4):
        tick(store, worker, runner=fake, lane_runner=lane)
        if _phase(store) == "implement":
            break
    assert _phase(store) == "implement", store.rows("task")[0]["payload"]["coordinator"].get("blocker")
    original_publish = runtime.ensure_draft
    class SimulatedCrash(BaseException):
        pass
    def interrupt_after_commit(*args, **kwargs):
        assert fake.commit_used_S
        raise SimulatedCrash
    monkeypatch.setattr(runtime, "ensure_draft", interrupt_after_commit)
    fake.dirty = True
    with pytest.raises(SimulatedCrash):
        tick(store, worker, runner=fake, lane_runner=lane)
    before = list(fake.launched)
    assert before == ["implementer"]
    task = store.rows("task")[0]
    assert task["payload"]["coordinator"]["lane_outcome"]["result"] == "done"
    monkeypatch.setattr(runtime, "ensure_draft", original_publish)
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.launched == before
    task = store.row("task", task["id"])
    assert task["payload"]["coordinator"].get("pr_number") == 42, lines
    assert task["payload"]["coordinator"]["phase"] == "inner_review", lines


def test_incomplete_pr_review_blocks_not_rejected_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.model_outputs["pr-reviewer-quality"] = "STATUS: unavailable\n"
    fake.model_outputs["pr-reviewer-logic"] = "STATUS: complete\nRESULT: approved\n"
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    tid = "99999999-9999-9999-9999-999999999999"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(store, worker,
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
                    "phase": "pr_gates_grok",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "worktree": str(wt),
                    "branch": "task-99999999",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                    "pr_number": 42,
                    "evidence": {"tests_pass": True, "tests_head": fake.head},
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked"
    assert coord.get("resume_phase") == "pr_gates_grok"
    qid = coord.get("question_activity_id")
    assert isinstance(qid, str) and qid
    gates = [g for g in store.rows("review_gate") if g.get("task_id") == tid]
    assert not any(g.get("verdict") == "rejected" for g in gates)
    assert any("unavailable" in line or "blocked" in line.lower() for line in lines)
    # Authorized reply resumes the gate stage — not a blind implementer start.
    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {"id": 2, "body": "vendor restored; resume gate", "user": {"login": "human-owner"}},
    ]
    fake.model_outputs["pr-reviewer-quality"] = "STATUS: complete\nRESULT: approved\n"
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "pr_gates_grok", lines
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched[len(launched_before) :]


@pytest.mark.parametrize("dismissal_point", ["readiness", "evidence_comment"])
@pytest.mark.parametrize("repeat_dismissal", [False, True])
def test_formal_dismissal_recovery_requires_new_reply_then_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dismissal_point: str, repeat_dismissal: bool
) -> None:
    """Dismissal during leave-draft clears formal evidence and recovers only after a new reply.

    Covers readiness and evidence-comment dismissal windows, no premature Ready,
    no automatic re-APPROVE, no implementer start, crash-idempotent re-APPROVE,
    then actual Ready via the real activity executor.
    """
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(4):
        phase = _phase(store)
        if phase in ("inner_review", "tests", "pr_gates_grok"):
            break
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) in ("tests", "pr_gates_grok"):
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(2):
        if _phase(store) == "pr_gates_grok":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "pr_gates_codex"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ci"
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "readiness"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "formal_approve"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "leave_draft"
    first_approve = [r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"]
    assert len(first_approve) == 1
    first_approve_id = first_approve[0]["id"]
    first_activity = store.rows("task")[0]["payload"]["coordinator"].get("formal_approve_id")
    assert isinstance(first_activity, str) and first_activity
    assert fake.pr["isDraft"] is True

    dismissed_once = {"done": False}

    def dismissing_readiness(
        argv, *, timeout, cwd=None, stdin_text=None, env=None, clear_ambient_github=False
    ):
        assert cwd and timeout > 0
        env = env or {}
        assert env.get("AGENT_COORDINATOR_HEAD") == fake.head
        if argv[0] == "/operator/checks":
            return Completed(fake.check_rc, "configured full-check result", "")
        if argv[0] == "/operator/readiness":
            assert clear_ambient_github
            if (
                dismissal_point == "readiness"
                and not dismissed_once["done"]
            ):
                for review in fake.reviews:
                    if str(review.get("state") or "").upper() == "APPROVED":
                        review["state"] = "DISMISSED"
                dismissed_once["done"] = True
            return Completed(
                fake.readiness_rc,
                json.dumps(
                    {
                        "head": fake.head,
                        "base": fake.base,
                        "contributing_ok": True,
                        "deviation": {"declared": False},
                    }
                ),
                "",
            )
        raise AssertionError(f"unconfigured test command: {argv}")

    monkeypatch.setattr("agent_cli.coordinator_runtime.run_bounded", dismissing_readiness)
    monkeypatch.setattr("agent_cli.coordinator_github.run_bounded", dismissing_readiness)

    def transport(argv):
        if (
            dismissal_point == "evidence_comment"
            and not dismissed_once["done"]
            and argv[:3] == ["gh", "pr", "comment"]
        ):
            for review in fake.reviews:
                review["state"] = "DISMISSED"
            dismissed_once["done"] = True
        return fake(argv)

    lines = tick(store, worker, runner=transport, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert fake.pr["isDraft"] is True, lines
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "formal_approve", coord
    evidence = coord.get("evidence") if isinstance(coord.get("evidence"), dict) else {}
    assert evidence.get("formal_head") is None
    assert coord.get("formal_approve_id") is None
    assert coord.get("formal_approve_attempt") == 1
    assert any(
        "formal" in line.lower()
        or "approv" in line.lower()
        or "dismiss" in line.lower()
        or "blocked" in line.lower()
        for line in lines
    )
    assert not any(str(r.get("state") or "").upper() == "APPROVED" for r in fake.reviews)
    qid = coord.get("question_activity_id")
    assert isinstance(qid, str) and qid

    # Successive tick without a NEW authorized reply must not re-APPROVE or Ready.
    launched_before = list(fake.launched)
    review_count = len(fake.reviews)
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "formal_approve"
    assert fake.pr["isDraft"] is True
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched[len(launched_before) :]
    assert len(fake.reviews) == review_count
    assert not any(str(r.get("state") or "").upper() == "APPROVED" for r in fake.reviews)

    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {
            "id": 2,
            "body": "re-approve after dismissal; resume formal",
            "user": {"login": "human-owner"},
        },
    ]
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert _phase(store) == "formal_approve", lines
    assert "implementer" not in fake.launched[len(launched_before) :]

    # New formal APPROVE on the same head via a new durable activity occurrence.
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert _phase(store) == "leave_draft", lines
    second_activity = coord.get("formal_approve_id")
    assert isinstance(second_activity, str) and second_activity
    assert second_activity != first_activity
    approved = [r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"]
    assert len(approved) == 1
    assert approved[0]["id"] != first_approve_id
    assert approved[0].get("commit_id") == fake.head
    assert (coord.get("evidence") or {}).get("formal_head") == fake.head

    # Crash/retry idempotency: same occurrence rediscovers, does not POST again.
    from agent_cli.coordinator_common import save_task

    approved_count = len(approved)
    coord["phase"] = "formal_approve"
    (coord.get("evidence") or {}).pop("formal_head", None)
    save_task(store, task)
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert _phase(store) == "leave_draft", lines
    assert (
        len([r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"])
        == approved_count
    )
    assert store.rows("task")[0]["payload"]["coordinator"].get("formal_approve_id") == second_activity

    if repeat_dismissal:
        # This comment predates the next dismissal and is not new authorization.
        fake.comments.append({"id": len(fake.comments) + 1,
                              "body": "Message before the second dismissal.",
                              "user": {"login": "human-owner"}})
        for review in fake.reviews:
            if review.get("state") == "APPROVED":
                review["state"] = "DISMISSED"
        lines = tick(store, worker, runner=transport, lane_runner=lane)
        assert _phase(store) == "blocked", lines
        checkpoint = store.rows("task")[0]["payload"]["coordinator"]
        assert checkpoint["question_activity_id"] != qid
        count = len(fake.reviews)
        lines = tick(store, worker, runner=transport, lane_runner=lane)
        assert _phase(store) == "blocked", lines
        assert len(fake.reviews) == count
        assert fake.launched == launched_before
        fake.comments.append({"id": len(fake.comments) + 1,
                              "body": "Authorize a new approval after this second dismissal.",
                              "user": {"login": "human-owner"}})
        tick(store, worker, runner=transport, lane_runner=lane)
        assert _phase(store) == "formal_approve"
        tick(store, worker, runner=transport, lane_runner=lane)
        assert _phase(store) == "leave_draft"
        assert len(fake.reviews) == count + 1
        assert fake.launched == launched_before

    # Actual Ready with the real executor; dismissed review stays rejected.
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert fake.pr["isDraft"] is False, lines
    assert _phase(store) == "await_merge", lines
    dismissed = [r for r in fake.reviews if r.get("id") == first_approve_id]
    assert dismissed and str(dismissed[0].get("state") or "").upper() == "DISMISSED"


@pytest.mark.parametrize("outcome", ["closed-unmerged", "nonhuman-merge"])
def test_nonrecoverable_post_ready_ignores_authorized_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Prior ask checkpoint + Ready, then closed/non-human: replies must not resume implement."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    ask_body = (
        "STATUS: complete\nRESULT: ask\nWhich edge case should the widget cover?\n"
    )
    done_body = (
        "STATUS: complete\nRESULT: done\n"
        "SUMMARY_EN: Correct widget initialization.\n"
        "SUMMARY_DE: Widget-Initialisierung korrigiert.\npatched\n"
    )
    fake.model_outputs["implementer"] = ask_body
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ask"
    task = store.rows("task")[0]
    qid = task["payload"]["coordinator"].get("question_activity_id")
    assert isinstance(qid, str) and qid
    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {
            "id": 2,
            "body": "cover the empty-list edge; continue",
            "user": {"login": "human-owner"},
        },
    ]
    fake.model_outputs["implementer"] = done_body
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    # Stale question checkpoint remains after reply consume (pre-Ready).
    assert store.rows("task")[0]["payload"]["coordinator"].get("question_activity_id") == qid

    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(4):
        phase = _phase(store)
        if phase in ("inner_review", "tests", "pr_gates_grok"):
            break
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) in ("tests", "pr_gates_grok"):
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(2):
        if _phase(store) == "pr_gates_grok":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "pr_gates_codex"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ci"
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "readiness"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "formal_approve"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "leave_draft"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "await_merge"
    assert fake.pr["isDraft"] is False

    launched_before = list(fake.launched)
    if outcome == "closed-unmerged":
        fake.pr["state"] = "CLOSED"
    else:
        fake.pr["state"] = "MERGED"
        fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
        fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
        fake.pr["mergedBy"] = {"login": "dependabot[bot]", "type": "Bot"}

    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") in (None, "")
    assert coord.get("question_activity_id") in (None, "")
    assert task["state"] != "done"
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched[len(launched_before) :]
    consumed_before = coord.get("replies_consumed_through")
    replies_before = list(coord.get("authorized_replies") or [])

    # New authorized comments after the non-recoverable outcome must not resume.
    fake.comments = list(fake.comments) + [
        {
            "id": 90,
            "body": "please continue implementing anyway",
            "user": {"login": "human-owner"},
        },
        {
            "id": 91,
            "body": "authorized retry after close",
            "user": {"login": "human-owner"},
        },
    ]
    for _ in range(3):
        lines = tick(store, worker, runner=fake, lane_runner=lane)
        task = store.rows("task")[0]
        coord = task["payload"]["coordinator"]
        assert coord["phase"] == "blocked", lines
        assert coord.get("resume_phase") in (None, "")
        # No NEW lane starts after the non-recoverable outcome (pre-Ready
        # implementers remain in the captured baseline).
        assert fake.launched == launched_before
        assert "implementer" not in fake.launched[len(launched_before) :]
        # No reply consumption across subsequent ticks.
        assert coord.get("replies_consumed_through") == consumed_before
        assert list(coord.get("authorized_replies") or []) == replies_before
        assert task["state"] != "implementing"


@pytest.mark.parametrize("lost_response", [False, True])
def test_formal_approve_transport_preserves_same_attempt_until_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_response: bool
) -> None:
    """Lost/empty discovery after POST keeps attempt; later discover reconciles once."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(4):
        phase = _phase(store)
        if phase in ("inner_review", "tests", "pr_gates_grok"):
            break
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) in ("tests", "pr_gates_grok"):
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(2):
        if _phase(store) == "pr_gates_grok":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "pr_gates_codex"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ci"
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "readiness"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "formal_approve"

    hide_discovery = {"n": 0}
    post_seen = {"n": 0}

    def transport(argv):
        joined = " ".join(argv)
        if "pulls/42/reviews" in joined and "-X" in argv:
            result = fake(argv)
            post_seen["n"] += 1
            # After the real POST, hide the review on the immediate coordinator GET.
            hide_discovery["n"] = 2
            if lost_response:
                return Completed(1, "", "connection lost after server accepted review")
            return result
        if (
            hide_discovery["n"] > 0
            and "pulls/42/reviews" in joined
            and "-X" not in argv
        ):
            hide_discovery["n"] -= 1
            # Empty successful list once, then transport failure once.
            if hide_discovery["n"] == 1:
                return Completed(0, json.dumps([]), "")
            return Completed(1, "", "temporary reviews API unavailable")
        return fake(argv)

    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert _phase(store) == "formal_approve", lines
    assert post_seen["n"] == 1
    assert len([r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"]) == 1
    coord = store.rows("task")[0]["payload"]["coordinator"]
    assert coord.get("formal_approve_attempt") in (None, 0)
    assert coord.get("formal_approve_id") is None
    assert coord.get("resume_phase") in (None, "")
    assert fake.launched == launched_before
    activities = [
        row for row in store.rows("activity")
        if row.get("type") == "review.post" and row.get("payload", {}).get("event") == "APPROVE"
    ]
    assert len(activities) == 1
    approval_id = activities[0]["id"]
    assert activities[0]["execution_status"] == ("error" if lost_response else "done")

    # The failed POST is reconciled through the real executor once discovery
    # succeeds, even if its first retry GET also fails. No new activity or POST.
    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert _phase(store) == ("leave_draft" if lost_response else "formal_approve"), lines
    assert post_seen["n"] == 1
    assert fake.launched == launched_before

    # Discovery visible again: reconcile same activity, advance to leave_draft.
    if not lost_response:
        lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert _phase(store) == "leave_draft", lines
    assert post_seen["n"] == 1
    coord = store.rows("task")[0]["payload"]["coordinator"]
    assert coord.get("formal_approve_id") == approval_id
    assert store.row("activity", approval_id)["execution_status"] == "done"
    assert [
        row["id"] for row in store.rows("activity")
        if row.get("type") == "review.post" and row.get("payload", {}).get("event") == "APPROVE"
    ] == [approval_id]
    assert coord.get("formal_approve_attempt") in (None, 0)
    assert len([r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"]) == 1
    assert (coord.get("evidence") or {}).get("formal_head") == fake.head
    assert fake.launched == launched_before

    lines = tick(store, worker, runner=transport, lane_runner=lane)
    assert fake.pr["isDraft"] is False, lines
    assert _phase(store) == "await_merge", lines
    assert post_seen["n"] == 1


def _drive_through_ci_to(store, worker, fake, lane, target_phase: str) -> None:
    """Drive successive ticks from discover through green CI to target Ready phase."""
    tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(4):
        phase = _phase(store)
        if phase in ("inner_review", "tests", "pr_gates_grok"):
            break
        if phase == "publish_draft":
            fake.commits_ahead = True
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        if _phase(store) in ("tests", "pr_gates_grok"):
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(2):
        if _phase(store) == "pr_gates_grok":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "pr_gates_codex"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ci"
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "readiness"
    if target_phase == "readiness":
        return
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "formal_approve"
    if target_phase == "formal_approve":
        return
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "leave_draft"
    assert target_phase == "leave_draft"


def _green_ci(fake: FakeGh) -> None:
    fake.pr_view_rc = 0
    fake.workflow_inventory_rc = 0
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]


@pytest.mark.parametrize(
    "ready_phase,fault",
    [
        ("readiness", "rollup_pending"),
        ("readiness", "inventory_pending"),
        ("readiness", "pr_view_transport"),
        ("readiness", "inventory_transport"),
        ("formal_approve", "rollup_pending"),
        ("formal_approve", "inventory_pending"),
        ("formal_approve", "pr_view_transport"),
        ("formal_approve", "inventory_transport"),
        ("leave_draft", "rollup_pending"),
        ("leave_draft", "inventory_pending"),
        ("leave_draft", "pr_view_transport"),
        ("leave_draft", "inventory_transport"),
    ],
)
def test_ready_side_ci_pending_or_transport_retries_without_reply_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ready_phase: str,
    fault: str,
) -> None:
    """Ready-side fresh CI rechecks: pending/transport never invent a reply gate."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, ready_phase)

    launched_before = list(fake.launched)
    reviews_before = list(fake.reviews)
    coord_before = store.rows("task")[0]["payload"]["coordinator"]
    attempt_before = coord_before.get("formal_approve_attempt")
    activity_before = coord_before.get("formal_approve_id")
    question_before = coord_before.get("question_activity_id")

    if fault == "rollup_pending":
        fake.pr["statusCheckRollup"] = [
            {"name": "tests", "conclusion": "", "status": "in_progress"}
        ]
    elif fault == "inventory_pending":
        fake.workflow_runs = [
            {
                "id": 1,
                "path": ".github/workflows/ci.yml",
                "event": "pull_request",
                "head_sha": fake.head,
                "status": "in_progress",
                "conclusion": "",
                "run_attempt": 1,
            }
        ]
    elif fault == "pr_view_transport":
        fake.pr_view_rc = 1
    else:
        fake.workflow_inventory_rc = 1

    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] != "blocked", lines
    assert coord.get("resume_phase") in (None, "")
    assert coord.get("question_activity_id") == question_before
    assert fake.pr["isDraft"] is True
    assert fake.launched == launched_before
    assert fake.reviews == reviews_before
    assert coord.get("formal_approve_attempt") == attempt_before
    assert coord.get("formal_approve_id") == activity_before
    # Pending/transport must not count as green progress into Ready.
    assert _phase(store) != "await_merge"

    _green_ci(fake)
    # Restore success: Ready-side phases progress without a new authorized reply.
    for _ in range(4):
        phase = _phase(store)
        if phase == "await_merge" or fake.pr["isDraft"] is False:
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.pr["isDraft"] is False
    assert _phase(store) == "await_merge"
    assert fake.launched == launched_before
    coord = store.rows("task")[0]["payload"]["coordinator"]
    if ready_phase in ("formal_approve", "leave_draft"):
        # Same durable formal attempt/activity across the pending window.
        assert coord.get("formal_approve_attempt") == attempt_before
        if activity_before is not None:
            assert coord.get("formal_approve_id") == activity_before
        assert len([r for r in fake.reviews if str(r.get("state") or "").upper() == "APPROVED"]) == 1


def test_leave_draft_ci_pending_after_evidence_comment_retries_same_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Immediate CI recheck after Ready evidence comment stays leave_draft, no new gate."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, "leave_draft")

    launched_before = list(fake.launched)
    coord_before = store.rows("task")[0]["payload"]["coordinator"]
    attempt_before = coord_before.get("formal_approve_attempt")
    activity_before = coord_before.get("formal_approve_id")
    pending_after_comment = {"armed": False}

    def flap_after_evidence(argv):
        joined = " ".join(argv)
        # Evidence comment is a gh pr comment; arm pending for the post-comment CI recheck.
        if argv[:3] == ["gh", "pr", "comment"]:
            result = fake(argv)
            pending_after_comment["armed"] = True
            return result
        if pending_after_comment["armed"] and (
            argv[:3] == ["gh", "pr", "view"] or "actions/runs" in joined
        ):
            # First post-comment CI observation is pending rollup; then restore.
            if argv[:3] == ["gh", "pr", "view"] and "statusCheckRollup" in joined:
                pending_after_comment["armed"] = False
                view = dict(fake.pr)
                view["statusCheckRollup"] = [
                    {"name": "tests", "conclusion": "", "status": "in_progress"}
                ]
                return Completed(0, json.dumps(view), "")
        return fake(argv)

    lines = tick(store, worker, runner=flap_after_evidence, lane_runner=lane)
    assert fake.pr["isDraft"] is True, lines
    assert _phase(store) == "leave_draft", lines
    coord = store.rows("task")[0]["payload"]["coordinator"]
    assert coord.get("resume_phase") in (None, "")
    assert coord.get("question_activity_id") in (None, "")
    assert coord.get("formal_approve_attempt") == attempt_before
    assert coord.get("formal_approve_id") == activity_before
    assert fake.launched == launched_before
    # Evidence comment exists; Ready mutation did not run.
    assert any(
        row.get("type") == "comment.post"
        and "Ready for review" in str((row.get("payload") or {}).get("body") or "")
        for row in store.rows("activity")
    )

    lines = tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.pr["isDraft"] is False, lines
    assert _phase(store) == "await_merge", lines
    coord = store.rows("task")[0]["payload"]["coordinator"]
    assert coord.get("formal_approve_attempt") == attempt_before
    assert coord.get("formal_approve_id") == activity_before
    assert fake.launched == launched_before


def test_ready_side_ci_hard_fault_still_blocks_with_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard inventory protocol on Ready-side recheck remains fail-closed blocked."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, "readiness")
    launched_before = list(fake.launched)
    fake.workflow_inventory_body = {"total_count": 1}  # missing workflow_runs
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "readiness"
    assert isinstance(coord.get("question_activity_id"), str)
    assert fake.pr["isDraft"] is True
    assert fake.launched == launched_before


def _red_ci(fake: FakeGh, *, conclusion: str = "failure") -> None:
    fake.pr_view_rc = 0
    fake.workflow_inventory_rc = 0
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": conclusion, "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": 1,
        }
    ]


@pytest.mark.parametrize(
    "ready_phase,after_evidence_comment",
    [
        ("readiness", False),
        ("formal_approve", False),
        ("leave_draft", False),
        ("leave_draft", True),
    ],
)
def test_ready_side_actual_red_ci_routes_to_ci_then_implement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ready_phase: str,
    after_evidence_comment: bool,
) -> None:
    """Ready-side observed failed CI returns to phase_ci → logs → implement.

    No human reply gate, no premature APPROVE/Ready, and no implementer until
    actual plain-text failure logs exist on the next ci tick.
    """
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, ready_phase)

    launched_before = list(fake.launched)
    reviews_before = list(fake.reviews)
    question_before = store.rows("task")[0]["payload"]["coordinator"].get(
        "question_activity_id"
    )

    if after_evidence_comment:
        # Arm failure only for the immediate post–evidence-comment CI recheck.
        pending_after_comment = {"armed": False}

        def fail_after_evidence(argv):
            joined = " ".join(argv)
            if argv[:3] == ["gh", "pr", "comment"]:
                result = fake(argv)
                pending_after_comment["armed"] = True
                return result
            if pending_after_comment["armed"] and (
                argv[:3] == ["gh", "pr", "view"] or "actions/runs" in joined
            ):
                if argv[:3] == ["gh", "pr", "view"] and "statusCheckRollup" in joined:
                    view = dict(fake.pr)
                    view["statusCheckRollup"] = [
                        {"name": "tests", "conclusion": "failure", "status": "completed"}
                    ]
                    return Completed(0, json.dumps(view), "")
                if "actions/runs" in joined:
                    pending_after_comment["armed"] = False
                    return Completed(
                        0,
                        json.dumps(
                            {
                                "total_count": 1,
                                "workflow_runs": [
                                    {
                                        "id": 1,
                                        "path": ".github/workflows/ci.yml",
                                        "event": "pull_request",
                                        "head_sha": fake.head,
                                        "status": "completed",
                                        "conclusion": "failure",
                                        "run_attempt": 1,
                                    }
                                ],
                            }
                        ),
                        "",
                    )
            return fake(argv)

        lines = tick(store, worker, runner=fail_after_evidence, lane_runner=lane)
        # Persist the observed failure so the following phase_ci tick sees it.
        _red_ci(fake)
    else:
        _red_ci(fake)
        lines = tick(store, worker, runner=fake, lane_runner=lane)

    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "ci", lines
    assert coord.get("resume_phase") in (None, "")
    assert coord.get("blocker") in (None, "")
    assert coord.get("question_activity_id") == question_before
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    assert fake.reviews == reviews_before
    evidence = coord.get("evidence") if isinstance(coord.get("evidence"), dict) else {}
    assert evidence.get("ci_green") is False

    # Next tick: existing phase_ci fetches plain-text logs and routes implement.
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "implement", lines
    assert any("CI failed" in line or "routing to implementer" in line for line in lines)
    findings = str(coord.get("findings") or "")
    assert "failing log line" in findings or "AssertionError" in findings
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "await_merge"
    # Still no model until the script starts implement on a later tick.
    assert fake.launched == launched_before


def test_ready_side_actual_red_ci_does_not_become_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual failed CI on recheck never becomes green; cancelled/skipped neither."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, "readiness")
    launched_before = list(fake.launched)
    _red_ci(fake, conclusion="failure")
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    # Recovery path: Ready-side failure returns to script ci observation.
    assert coord["phase"] == "ci", lines
    assert coord.get("resume_phase") in (None, "")
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "formal_approve"
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    evidence = coord.get("evidence") if isinstance(coord.get("evidence"), dict) else {}
    assert evidence.get("ci_green") is False

    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "implement", lines
    assert fake.pr["isDraft"] is True
    assert fake.launched == launched_before

    # cancelled/skipped must also fail closed, not count as green or Ready progress.
    # Re-enter readiness with stale ci_green cleared so the fresh recheck sees red.
    from agent_cli.coordinator_common import save_task

    coord["phase"] = "readiness"
    coord["resume_phase"] = None
    coord["blocker"] = None
    if isinstance(coord.get("evidence"), dict):
        coord["evidence"]["ci_green"] = True  # stale flag must not win
        coord["evidence"]["ci_head"] = fake.head
    save_task(store, task)
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "cancelled", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 1,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "skipped",
            "run_attempt": 1,
        }
    ]
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "ci", lines
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "formal_approve"
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    evidence = coord.get("evidence") if isinstance(coord.get("evidence"), dict) else {}
    assert evidence.get("ci_green") is False


@pytest.mark.parametrize("log_failure", ["transport", "empty", "whitespace"])
def test_ready_side_red_ci_preserves_logs_inaccessible_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_failure: str
) -> None:
    """Ready-side failure still uses phase_ci logs-inaccessible external blocker."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, "readiness")
    launched_before = list(fake.launched)
    _red_ci(fake)
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ci", lines
    fake.failed_logs_inaccessible = log_failure == "transport"
    fake.failed_log_text = " \n\t" if log_failure == "whitespace" else ""
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "ci"
    assert isinstance(coord.get("question_activity_id"), str)
    assert any("inaccessible" in line for line in lines)
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched[len(launched_before) :]
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "blocked", lines
    assert fake.launched == launched_before


@pytest.mark.parametrize("ready_phase", ["readiness", "formal_approve", "leave_draft"])
def test_ready_side_absent_rollup_inventory_action_required_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready_phase: str
) -> None:
    """Absent rollup on Ready-side recheck still surfaces inventory action_required."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, ready_phase)
    launched_before = list(fake.launched)
    fake.pr["statusCheckRollup"] = None
    fake.workflow_runs = [
        {
            "id": 3,
            "path": ".github/workflows/deploy.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
        }
    ]
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == ready_phase
    assert isinstance(coord.get("question_activity_id"), str)
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched[len(launched_before) :]


@pytest.mark.parametrize("ready_phase", ["readiness", "formal_approve", "leave_draft"])
def test_ready_side_absent_rollup_inventory_failure_routes_to_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready_phase: str
) -> None:
    """Absent rollup on Ready-side recheck still surfaces inventory failure."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    _drive_through_ci_to(store, worker, fake, lane, ready_phase)
    launched_before = list(fake.launched)
    fake.pr["statusCheckRollup"] = None
    fake.workflow_runs = [
        {
            "id": 4,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
        }
    ]
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "ci", lines
    assert coord.get("resume_phase") in (None, "")
    assert fake.pr["isDraft"] is True
    assert _phase(store) != "await_merge"
    assert fake.launched == launched_before
    lines = tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement", lines
    assert fake.launched == launched_before


@pytest.mark.parametrize("crash_after_question", [False, True])
def test_repeated_question_needs_a_new_reply(tmp_path, monkeypatch, crash_after_question):
    """A later identical ask is a new occurrence, not reuse of the old reply."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.model_outputs["implementer"] = "STATUS: complete\nRESULT: ask\nWhich behavior is required?\n"
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)
    from agent_cli import coordinator_runtime
    original_post = coordinator_runtime.post_issue_comment
    class InterruptedPublication(BaseException):
        pass
    crashed = False
    def interrupted_post(*args, **kwargs):
        nonlocal crashed
        result = original_post(*args, **kwargs)
        if crash_after_question and not crashed and kwargs.get("kind") == "question":
            crashed = True
            raise InterruptedPublication()
        return result
    monkeypatch.setattr(coordinator_runtime, "post_issue_comment", interrupted_post)
    for _ in range(5):
        try:
            tick(store, worker, runner=fake, lane_runner=lane)
        except InterruptedPublication:
            assert fake.launched == ["implementer"]
            continue
        if _phase(store) == "ask":
            break
    assert crashed == crash_after_question
    assert fake.launched == ["implementer"]
    assert len([c for c in fake.comments if "Which behavior is required?" in c["body"]]) == 1
    assert _phase(store) == "ask"
    first = store.rows("task")[0]["payload"]["coordinator"]["question_activity_id"]
    fake.comments.append({"id": len(fake.comments) + 1, "body": "First answer.",
                          "user": {"login": "human-owner"}})
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ask"
    second = store.rows("task")[0]["payload"]["coordinator"]["question_activity_id"]
    assert first != second
    launched = list(fake.launched)
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "ask"
    assert fake.launched == launched
    fake.comments.append({"id": len(fake.comments) + 1, "body": "Second answer.",
                          "user": {"login": "human-owner"}})
    tick(store, worker, runner=fake, lane_runner=lane)
    assert _phase(store) == "implement"


def test_comments_with_same_prefix_do_not_collide(tmp_path, monkeypatch):
    from agent_cli.coordinator_github import post_issue_comment
    from agent_cli import github_act
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    ids = [post_issue_comment(store, worker, fake, repo="example/project", number=7,
                              body="Shared context " * 20 + suffix, kind="status")
           for suffix in ("First finding.", "Second finding.")]
    assert ids[0] != ids[1]
    assert len(fake.comments) == 2
    assert "First finding." in fake.comments[0]["body"]
    assert "Second finding." in fake.comments[1]["body"]
