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
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    gates = [g for g in store.rows("review_gate") if g.get("task_id") == tid]
    assert not any(g.get("verdict") == "rejected" for g in gates)
    assert any("unavailable" in line or "blocked" in line.lower() for line in lines)
