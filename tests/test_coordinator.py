"""Coordinator runtime tests with fake transports and real Store fixtures.

These tests prove finding regressions against the corrected runtime. They do
not call real GitHub, models, or repository test suites.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_cli.coordinator import tick
from agent_cli.coordinator_git import verify_signed_clean_head
from agent_cli.coordinator_runtime import (
    REQUIRED_LANE_SLOTS,
    CoordinatorError,
    harden_grok_write_argv,
    parse_model_result,
    preflight_worker,
    review_is_approved,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_session,
    make_worker,
    write_accounts,
)


def seed_task(store, worker, tid, data):
    """Seed an isolated checkpoint with its real configured binding; no check/gate changes."""
    from agent_cli.coordinator_runtime import execution_binding
    checkpoint = data.get("payload", {}).get("coordinator", {})
    source = checkpoint.get("source", {})
    checkpoint["execution_binding"] = execution_binding(store, worker, source["repo"])
    store.write("task", "insert", tid, data)


def patch_account_runners(monkeypatch: Any, fake: FakeGh) -> None:
    """Match real Account.runner: accept bare gh/git, never require pre-wrapped env."""
    from agent_cli import github_accounts

    def runner(self, base, *, require_git=False):  # noqa: ANN001
        login = self.login

        def scoped(argv: list[str]) -> Completed:
            if not argv or argv[0] not in {"gh", "git"}:
                raise github_accounts.AccountError("GitHub account runner accepts only gh and git")
            if argv == ["gh", "api", "user", "--jq", ".login"]:
                return Completed(0, login, "")
            if argv[:3] == ["gh", "run", "view"] and "--log-failed" in argv:
                return Completed(0, "failing log line\n", "")
            # Present the same env-prefixed shape FakeGh already understands.
            env_argv = [
                "env",
                f"GH_CONFIG_DIR=/test/gh-{login}",
                "GH_HOST=github.com",
                *argv,
            ]
            return fake(env_argv)

        return scoped

    monkeypatch.setattr(github_accounts.Account, "runner", runner)


def patch_execute_github(monkeypatch: Any) -> None:
    """Scoped executor: only the exact activity_ids batch is marked done."""

    def fake_execute(store, runner, *, activity_ids):  # noqa: ANN001
        if not isinstance(activity_ids, tuple) or not activity_ids:
            raise TypeError("execute_github requires non-empty activity_ids tuple")
        for aid in activity_ids:
            row = store.row("activity", aid)
            if row is None or row.get("execution_status") != "pending":
                continue
            updated = {k: v for k, v in row.items() if not k.startswith("_")}
            updated["execution_status"] = "done"
            payload = updated.get("payload") if isinstance(updated.get("payload"), dict) else {}
            number = 42 if row.get("type") == "pr.open" else payload.get("number", 7)
            result: dict[str, Any] = {
                "repo": "example/project",
                "number": number,
                "url": "https://x",
                "draft": True,
                "id": 99,
            }
            if row.get("type") == "review.post":
                result.update(
                    {
                        "state": payload.get("event") == "APPROVE" and "APPROVED" or "COMMENTED",
                        "commit_id": payload.get("commit_id"),
                        "login": "review-bot",
                    }
                )
            updated["result"] = result
            store.write("activity", "update", aid, updated)
        return []

    for mod in (
        "agent_cli.coordinator_git",
        "agent_cli.coordinator_github",
        "agent_cli.coordinator_lanes",
    ):
        monkeypatch.setattr(f"{mod}.execute_github", fake_execute)


def patch_run_bounded(monkeypatch: Any, fake: FakeGh) -> None:
    def fake_bounded(argv, **kwargs):  # noqa: ANN001
        joined = " ".join(str(a) for a in argv)
        if "readiness" in joined:
            env = kwargs.get("env") or {}
            payload = {
                "head": env.get("AGENT_COORDINATOR_HEAD") or fake.head,
                "base": env.get("AGENT_COORDINATOR_BASE") or fake.base,
                "contributing_ok": True,
                "deviation": {"declared": False},
            }
            return Completed(0 if fake.readiness_rc == 0 else 1, json.dumps(payload), "")
        if "checks" in joined:
            return Completed(fake.check_rc, "ok" if fake.check_rc == 0 else "fail", "")
        return Completed(127, "", f"unhandled bounded argv: {argv}")

    monkeypatch.setattr("agent_cli.coordinator_runtime.run_bounded", fake_bounded)
    monkeypatch.setattr("agent_cli.coordinator_github.run_bounded", fake_bounded)


def test_harden_grok_write_adds_denies() -> None:
    argv = ["env", "-u", "X", "grok", "--permission-mode", "acceptEdits", "--allow", "Write"]
    out = harden_grok_write_argv(argv)
    assert "--deny" in out and "Bash" in out
    assert "--no-subagents" in out
    assert "--disable-web-search" in out


def test_invalid_review_never_approved() -> None:
    assert not review_is_approved("partial", "approved")
    assert not review_is_approved("complete", "rejected")
    assert not review_is_approved("timeout", "approved")
    assert not review_is_approved("complete", "")
    assert not review_is_approved("unavailable", "approved")
    assert review_is_approved("complete", "approved")
    status, result = parse_model_result("STATUS: complete\nRESULT: approved\n", 0)
    assert status == "complete" and result == "approved"


def test_signature_verification_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.signed = False
    patch_account_runners(monkeypatch, fake)
    wt = worker.workspace_root / "t"
    wt.mkdir()
    (wt / ".git").mkdir()
    with pytest.raises(CoordinatorError, match="cryptographic signature verification"):
        verify_signed_clean_head(store, worker, fake, str(wt))


def test_preflight_missing_config(tmp_path: Path) -> None:
    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    with pytest.raises(CoordinatorError):
        preflight_worker(store, worker, lambda argv: Completed(1, "", "no"))


def test_preflight_same_github_login_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    data = json.loads((store.home / "github-accounts.json").read_text())
    data["accounts"]["reviewer"]["login"] = "worker-bot"
    (store.home / "github-accounts.json").write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    with pytest.raises(CoordinatorError, match="different GitHub login"):
        preflight_worker(store, worker, fake)


def test_acceptance_before_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.launched == []
    tick(store, worker, runner=fake, lane_runner=lane)
    assert any(
        r.get("type") == "comment.post" and r.get("execution_status") == "done" for r in store.rows("activity")
    )
    assert fake.launched == []

    for _ in range(4):
        tasks = store.rows("task")
        phase = (tasks[0].get("payload") or {}).get("coordinator", {}).get("phase") if tasks else None
        if phase == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)

    before = list(fake.launched)
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    assert "implementer" in fake.launched[len(before) :]
    assert fake.commit_used_S


def test_forbidden_implicit_account_selection(tmp_path: Path) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    lines = tick(store, worker, runner=lambda a: Completed(0, "", ""))
    assert any("preflight blocked" in line for line in lines)


def test_concurrent_lock_excludes_second_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    held = threading.Event()
    release = threading.Event()
    original_exclusive = store.exclusive

    from contextlib import contextmanager

    @contextmanager
    def slow_exclusive(key: str):
        with original_exclusive(key):
            held.set()
            release.wait(timeout=2)
            yield

    store.exclusive = slow_exclusive  # type: ignore[method-assign]
    results: list[list[str]] = []

    def run_one() -> None:
        results.append(tick(store, worker, runner=fake, lane_runner=lane_runner(fake)))

    t1 = threading.Thread(target=run_one)
    t1.start()
    assert held.wait(timeout=2)
    assert not release.is_set()
    release.set()
    t1.join(timeout=2)
    assert results and isinstance(results[0], list)


def test_authorized_reply_filtering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "11111111-1111-1111-1111-111111111111"
    qid = "22222222-2222-2222-2222-222222222222"
    seed_task(store, worker,
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": None,
            "payload": {
                "coordinator": {
                    "phase": "ask",
                    "resume_phase": "implement",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a1",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "question_activity_id": qid,
                    "worktree": str(worker.workspace_root / tid),
                    "branch": "task-11111111",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                }
            },
            "state": "open",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    marker = f"<!-- agent-coordinator:question:v1:{qid} -->"
    fake.comments = [
        {"id": 1, "body": f"Question\n{marker}", "user": {"login": "worker-bot"}},
        {"id": 2, "body": "ignore me", "user": {"login": "random-user"}},
        {"id": 3, "body": f"copied marker {marker}", "user": {"login": "human-owner"}},
        {"id": 4, "body": "please also handle X", "user": {"login": "human-owner"}},
    ]
    (worker.workspace_root / tid).mkdir(parents=True)
    (worker.workspace_root / tid / ".git").mkdir()
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task is not None
    replies = task["payload"]["coordinator"].get("authorized_replies") or []
    assert "please also handle X" in replies
    assert task["payload"]["coordinator"]["phase"] == "implement"
    assert any("authorized reply" in line for line in lines)


def test_human_merge_only_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "33333333-3333-3333-3333-333333333333"
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
                    "phase": "await_merge",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "assigned-1",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "pr_number": 42,
                    "publication_repo": "example/project",
                    "head_sha": fake.head,
                    "worktree": str(worker.workspace_root / tid),
                    "evidence": {
                        "tests_pass": True,
                        "tests_head": fake.head,
                        "ci_green": True,
                        "ci_head": fake.head,
                        "formal_head": fake.head,
                        "ready_head": fake.head,
                        "readiness_head": fake.head,
                    },
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": "Implemented example/project#7.",
            "change_summary_de": "Umsetzung von example/project#7.",
        },
    )
    from agent_cli.allow import CHECKLIST_KEYS

    for key in CHECKLIST_KEYS["implement"]:
        cid = f"c-{key}"
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "key": key,
                "status": "ja" if key not in ("deviation_declared", "deviation_granted") else "n_a",
                "evidence": "seed",
                "source": "human" if key in ("spec_written", "deviation_declared", "deviation_granted") else "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    for stage, dim, vendor in (
        ("grok-pr", "quality", "grok"),
        ("grok-pr", "logic", "grok"),
        ("codex-pr", "quality", "codex"),
        ("codex-pr", "logic", "codex"),
    ):
        gid = f"g-{stage}-{dim}"
        store.write(
            "review_gate",
            "insert",
            gid,
            {
                "id": gid,
                "task_id": tid,
                "stage": stage,
                "dimension": dim,
                "vendor": vendor,
                "verdict": "approved",
                "evidence": None,
                "head_sha": fake.head,
                "agent_id": "a",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
    store.write(
        "local_check",
        "insert",
        "lc1",
        {
            "id": "lc1",
            "task_id": tid,
            "name": "coordinator-check",
            "command": "/operator/checks",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": fake.head,
        },
    )

    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert any("awaiting human merge" in line for line in lines)
    task = store.row("task", tid)
    assert task is not None and task["state"] != "done"

    fake.pr["state"] = "CLOSED"
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert any("closed" in line.lower() for line in lines)
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"

    task["payload"]["coordinator"]["phase"] = "await_merge"
    task["state"] = "pr-review"
    store.write("task", "update", tid, {k: v for k, v in task.items() if not k.startswith("_")})
    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["state"] == "done"
    assert any(r.get("type") == "pr.merged" for r in store.rows("activity"))
    assert any(r.get("type") == "issue.assigned.ack" for r in store.rows("activity"))


def test_merge_missing_type_is_not_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "33333333-3333-3333-3333-333333333334"
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
                    "phase": "await_merge",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "assigned-1",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "pr_number": 42,
                    "head_sha": fake.head,
                    "worktree": str(worker.workspace_root / tid),
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": "Implemented example/project#7.",
            "change_summary_de": "Umsetzung von example/project#7.",
        },
    )
    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
    fake.pr["mergedBy"] = {"login": "human-owner"}  # missing type
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert any("missing type" in line or "non-human" in line.lower() for line in lines)


def test_exact_head_invalidation_and_ci_failure_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "44444444-4444-4444-4444-444444444444"
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
                    "branch": "task-44444444",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                    "pr_number": 42,
                    "publication_repo": "example/project",
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
    fake.pr["statusCheckRollup"] = [{"name": "tests", "conclusion": "failure", "status": "completed"}]
    fake.workflow_runs = [
        {
            "id": 9,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
        }
    ]
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "implement"
    assert any("CI failed" in line for line in lines)


def test_ci_action_required_is_blocker_not_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-555555555555"
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
                    "branch": "task-55555555",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                    "pr_number": 42,
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
    fake.pr["statusCheckRollup"] = [
        {"name": "deploy", "conclusion": "action_required", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 11,
            "path": ".github/workflows/deploy.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
        }
    ]
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert task["payload"]["coordinator"].get("resume_phase") == "ci"
    assert "implementer" not in fake.launched
    assert any("action_required" in line for line in lines)


def test_no_duplicate_acceptance_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    accept_rows = [
        r
        for r in store.rows("activity")
        if r.get("type") == "comment.post"
        and isinstance(r.get("payload"), dict)
        and "Accepted for implementation" in str(r["payload"].get("body") or "")
    ]
    assert len(accept_rows) == 1


def test_failed_task_not_auto_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "66666666-6666-6666-6666-666666666666"
    seed_task(store, worker,
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": None,
            "payload": {
                "coordinator": {
                    "phase": "blocked",
                    "blocker": "earlier failure",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                }
            },
            "state": "failed",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert any("failed task remains" in line for line in lines)
    task = store.row("task", tid)
    assert task["state"] == "failed"


def test_spec_files_outside_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    for _ in range(3):
        tasks = store.rows("task")
        if not tasks:
            break
        phase = tasks[0]["payload"]["coordinator"]["phase"]
        if phase == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tasks = store.rows("task")
    assert tasks
    tid = tasks[0]["id"]
    worktree = worker.workspace_root / tid
    control = worker.workspace_root / ".coordinator-control" / tid
    assert control.exists()
    assert not (worktree / ".agent-coordinator").exists()


def test_execute_github_requires_activity_ids_and_ignores_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    # Foreign pending activity must remain untouched when we publish acceptance.
    foreign = "foreign-activity-id"
    store.write(
        "activity",
        "insert",
        foreign,
        {
            "id": foreign,
            "session_id": "other-session",
            "type": "comment.post",
            "payload": {"repo": "example/project", "number": 99, "body": "other", "target": "issue"},
            "execution_status": "pending",
        },
    )
    seen: list[tuple[str, ...]] = []

    def tracking_execute(store_, runner_, *, activity_ids):  # noqa: ANN001
        seen.append(tuple(activity_ids))
        for aid in activity_ids:
            row = store_.row("activity", aid)
            if row is None or row.get("execution_status") != "pending":
                continue
            updated = {k: v for k, v in row.items() if not k.startswith("_")}
            updated["execution_status"] = "done"
            updated["result"] = {"repo": "example/project", "number": 7, "url": "https://x"}
            store_.write("activity", "update", aid, updated)
        return []

    for mod in (
        "agent_cli.coordinator_git",
        "agent_cli.coordinator_github",
        "agent_cli.coordinator_lanes",
    ):
        monkeypatch.setattr(f"{mod}.execute_github", tracking_execute)

    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    assert seen
    assert all(foreign not in batch for batch in seen)
    foreign_row = store.row("activity", foreign)
    assert foreign_row is not None and foreign_row.get("execution_status") == "pending"


def test_revoked_assignment_stops_before_formal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.issues[0]["assignees"] = []
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
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
                    "phase": "formal_approve",
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
                    "evidence": {
                        "tests_pass": True,
                        "tests_head": fake.head,
                        "ci_green": True,
                        "ci_head": fake.head,
                        "readiness_head": fake.head,
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
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert any("no longer assigned" in line or "stopped" in line.lower() or "blocked" in line for line in lines)


def test_required_lane_slots_constant() -> None:
    assert "grok:implementer" in REQUIRED_LANE_SLOTS
    assert "codex:pr-reviewer-logic" in REQUIRED_LANE_SLOTS
