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
from agent_cli.coordinator_github import (
    phase_formal_approve,
    phase_leave_draft,
    phase_readiness,
)
from agent_cli.coordinator_runtime import (
    CoordinatorError,
    harden_grok_write_argv,
    parse_model_result,
    preflight_worker,
    review_is_approved,
)
from agent_cli.model_slots import MODELS_NOT_CONFIGURED
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
        "agent_cli.coordinator_a38",
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
    monkeypatch.setattr("agent_cli.coordinator_a38.run_bounded", fake_bounded)


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


def test_missing_models_is_a_preflight_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("models")
    path.write_text(json.dumps(data), encoding="utf-8")
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    assert tick(store, worker, runner=fake) == [
        f"preflight blocked: {MODELS_NOT_CONFIGURED}"
    ]


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
        ("review-1", "quality", "grok"),
        ("review-1", "logic", "grok"),
        ("review-2", "quality", "codex"),
        ("review-2", "logic", "codex"),
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
    merged = [row for row in store.rows("activity") if row.get("type") == "pr.merged"]
    assert merged and merged[0]["payload"]["merged_by"] == "human-owner"
    assert any(r.get("type") == "pr.merged" for r in store.rows("activity"))
    assert any(r.get("type") == "issue.assigned.ack" for r in store.rows("activity"))


def _seed_one_review_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tid: str,
    stored_login: str,
) -> tuple[Any, Any, FakeGh]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    seed_task(
        store,
        worker,
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "t",
            "repo": "example/project",
            "ref": "42",
            "payload": {
                "one_review_author": {
                    "pull_opener": stored_login,
                    "trusted_pr_guard": {"one_review_authors": [stored_login]},
                },
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
                    "evidence": {"ready_head": fake.head},
                },
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
        if key in ("review_2_quality", "review_2_logic"):
            status, evidence = "n_a", "second review not posted"
        elif key in ("deviation_declared", "deviation_granted"):
            status, evidence = "n_a", "seed"
        else:
            status, evidence = "ja", "seed"
        store.write(
            "checklist_item",
            "insert",
            f"{tid}-{key}",
            {
                "id": f"{tid}-{key}",
                "task_id": tid,
                "key": key,
                "status": status,
                "evidence": evidence,
                "source": "human" if key in ("spec_written", "deviation_declared", "deviation_granted") else "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    for dim in ("quality", "logic"):
        store.write(
            "review_gate",
            "insert",
            f"{tid}-grok-{dim}",
            {
                "id": f"{tid}-grok-{dim}",
                "task_id": tid,
                "stage": "review-1",
                "dimension": dim,
                "vendor": "grok",
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
        f"{tid}-lc",
        {
            "id": f"{tid}-lc",
            "task_id": tid,
            "name": "coordinator-check",
            "command": "/operator/checks",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": fake.head,
        },
    )
    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    return store, worker, fake


def test_human_merge_done_reads_the_live_one_review_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _seed_one_review_merge(
        tmp_path,
        monkeypatch,
        tid="55555555-5555-4555-8555-555555555551",
        stored_login="alice",
    )
    _list_opener(fake, "worker-bot")
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", "55555555-5555-4555-8555-555555555551")
    assert task is not None and task["state"] == "done"
    merged = [row for row in store.rows("activity") if row.get("type") == "pr.merged"]
    assert merged and merged[0]["payload"]["merged_by"] == "human-owner"


def test_human_merge_stays_blocked_when_the_trusted_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _seed_one_review_merge(
        tmp_path,
        monkeypatch,
        tid="55555555-5555-4555-8555-555555555552",
        stored_login="worker-bot",
    )
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", "55555555-5555-4555-8555-555555555552")
    assert task is not None and task["state"] != "done"
    assert any("task-done blocked" in line for line in lines)
    assert not any(row.get("type") == "pr.merged" for row in store.rows("activity"))


def test_human_merge_retries_when_the_trusted_list_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _seed_one_review_merge(
        tmp_path,
        monkeypatch,
        tid="55555555-5555-4555-8555-555555555553",
        stored_login="alice",
    )
    _list_opener(fake, "worker-bot")

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        from agent_cli.pr_guard_config import PrGuardLookupError

        raise PrGuardLookupError("down")

    monkeypatch.setattr("agent_cli.coordinator_github.fetch_trusted_pr_guard", boom)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", "55555555-5555-4555-8555-555555555553")
    assert task is not None and task["state"] != "done"
    assert any("trusted review list could not be read" in line for line in lines)
    merged = [row for row in store.rows("activity") if row.get("type") == "pr.merged"]
    assert merged and merged[0]["payload"]["merged_by"] == "human-owner"
    assert not any(row.get("type") == "issue.assigned.ack" for row in store.rows("activity"))


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


def test_ci_absent_rollup_inventory_action_required_is_blocker_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent statusCheckRollup must still surface inventory action_required."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-55555555555a"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
    fake.pr["statusCheckRollup"] = None
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
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "ci"
    assert any("action_required" in line for line in lines)
    assert coord.get("evidence", {}).get("ci_green") is not True
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched


def test_ci_absent_rollup_inventory_failure_routes_to_implement_with_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent statusCheckRollup must still surface inventory failure via logs path."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-55555555555b"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
    fake.pr["statusCheckRollup"] = None
    fake.workflow_runs = [
        {
            "id": 12,
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
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "implement", lines
    assert any("CI failed" in line for line in lines)
    findings = str(coord.get("findings") or "")
    assert "failing log line" in findings or "AssertionError" in findings
    assert coord.get("evidence", {}).get("ci_green") is False


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
    qid = task["payload"]["coordinator"].get("question_activity_id")
    assert isinstance(qid, str) and qid
    assert "implementer" not in fake.launched
    assert any("action_required" in line for line in lines)


def test_a38_ci_action_required_stays_retryable_without_reply_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_github import phase_ci

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    tid = "55555555-5555-4555-8555-55555555555c"
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "ci",
                "head_sha": fake.head,
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7, "base": "develop"},
            }
        },
    }
    store.write("task", "insert", tid, task)
    fake.pr["statusCheckRollup"] = [
        {"name": "tests", "conclusion": "action_required", "status": "completed"}
    ]
    fake.workflow_runs = [
        {
            "id": 12,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
        }
    ]

    lines = phase_ci(store, worker, store.row("task", tid), fake)

    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "ci"
    assert "question_activity_id" not in saved["payload"]["coordinator"]
    assert store.rows("activity") == []
    assert lines == [
        f"CI authorization pending on {fake.head[:7]} (guard-owned): tests, .github/workflows/ci.yml"
    ]


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_a38_ci_accepts_report_covered_skipped_and_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conclusion: str,
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_github import phase_ci
    from agent_cli.local_ci import LocalCiReport, LocalCiRun, SCHEMA_ID, render_block

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    tid = "55555555-5555-4555-8555-55555555555d"
    base = "b" * 40
    activity_id = "55555555-5555-4555-8555-55555555555e"
    report = LocalCiReport(
        schema=SCHEMA_ID,
        repo="example/project",
        head=fake.head,
        private=True,
        recorded_at="2026-01-01T00:00:00Z",
        required=("tests",),
        runs=(
            LocalCiRun(
                id="tests",
                name="tests",
                command="pytest",
                result="pass",
                exit_code=0,
                duration_s=1.0,
                timeout_s=60.0,
            ),
        ),
    )
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": worker.session_id,
            "type": "comment.post",
            "payload": {
                "repo": "example/project",
                "number": 42,
                "body": render_block(report),
                "target": "pr",
                "exact_body": True,
            },
            "execution_status": "done",
        },
    )
    a38_evidence: dict[str, object] = {
        "report": {
            "head": fake.head,
            "base_sha": base,
            "policy_revision": base,
            "activity_id": activity_id,
        }
    }
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "ci",
                "head_sha": fake.head,
                "base_sha": base,
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7, "base": "develop"},
                "a38": a38_evidence,
            }
        },
    }
    store.write("task", "insert", tid, task)
    fake.pr["statusCheckRollup"] = [
        {
            "name": "tests",
            "workflowName": "CI",
            "conclusion": conclusion,
            "status": "completed",
        }
    ]
    fake.workflow_runs = [
        {
            "id": 13,
            "path": ".github/workflows/ci.yml",
            "name": "CI",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": 1,
        }
    ]

    lines = phase_ci(store, worker, store.row("task", tid), fake)

    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "readiness"
    assert lines == [f"CI green on {fake.head[:7]}"]


def test_a38_report_waiver_does_not_cover_missing_rollup_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_github import phase_ci

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    base = "b" * 40
    tid = "55555555-5555-4555-8555-55555555555f"
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "ci",
                "head_sha": fake.head,
                "base_sha": base,
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7, "base": "develop"},
                "a38": {
                    "report_waiver": {
                        "head": fake.head,
                        "base_sha": base,
                        "policy_revision": base,
                        "reason": "guard-docs change set",
                    }
                },
            }
        },
    }
    store.write("task", "insert", tid, task)
    fake.pr["statusCheckRollup"] = []
    fake.workflow_runs = [
        {
            "id": 14,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "head_sha": fake.head,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
    ]

    lines = phase_ci(store, worker, store.row("task", tid), fake)

    assert lines == [
        f"CI pending on {fake.head[:7]} (incomplete rollup/inventory success evidence)"
    ]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "ci"


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "failure", "failed"),
        ("completed", "cancelled", "failed"),
        ("in_progress", "", "pending"),
    ],
)
def test_a38_report_keeps_failed_cancelled_and_pending_strict(
    status: str, conclusion: str, expected: str
) -> None:
    from agent_cli.coordinator_github import _classify_rollup_and_inventory

    rollup = [{"name": "tests", "status": status, "conclusion": conclusion}]
    latest = {
        ".github/workflows/ci.yml|pull_request": {
            "path": ".github/workflows/ci.yml",
            "status": status,
            "conclusion": conclusion,
        }
    }

    observation = _classify_rollup_and_inventory(
        rollup, latest, passed_a38=frozenset({"tests"})
    )

    assert observation.kind == expected


def test_a38_report_covered_rollup_does_not_cover_cancelled_workflow() -> None:
    from agent_cli.coordinator_github import _classify_rollup_and_inventory

    rollup = [
        {
            "name": "tests",
            "workflowName": "CI",
            "status": "completed",
            "conclusion": "skipped",
        }
    ]
    latest = {
        ".github/workflows/ci.yml|pull_request": {
            "path": ".github/workflows/ci.yml",
            "name": "CI",
            "status": "completed",
            "conclusion": "cancelled",
        }
    }

    observation = _classify_rollup_and_inventory(
        rollup, latest, passed_a38=frozenset({"tests"})
    )

    assert observation.kind == "failed"


def test_ci_action_required_authorized_reply_resumes_ci_not_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External CI authorization blocker: reply resumes ci observation, never implement."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-555555555556"
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
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert task["payload"]["coordinator"].get("resume_phase") == "ci"
    qid = task["payload"]["coordinator"]["question_activity_id"]
    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    assert qid in body
    # Multi-page slurped comments: checkpoint + authorized reply on page 2.
    fake.comment_pages = [
        [{"id": 1, "body": "earlier noise", "user": {"login": "other-user"}}],
        [
            {"id": 10, "body": body, "user": {"login": "worker-bot"}},
            {
                "id": 11,
                "body": "workflow authorized; continue observation",
                "user": {"login": "human-owner"},
            },
        ],
    ]
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "ci", lines
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched
    # Next tick observes CI again; still action_required → block, still no implementer.
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked", lines
    assert task["payload"]["coordinator"].get("resume_phase") == "ci"
    assert "implementer" not in fake.launched
    # The same authorization reply cannot re-trigger another attempt.
    consumed = task["payload"]["coordinator"]["replies_consumed_through"]
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked", lines
    assert task["payload"]["coordinator"]["replies_consumed_through"] == consumed
    assert fake.launched == launched_before


def test_ci_inventory_protocol_fault_blocks_with_ci_resume_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard inventory shape fault → blocked+resume_phase=ci; reply recovers once valid."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-555555555557"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    # Protocol fault: object without workflow_runs (not transport).
    fake.workflow_inventory_body = {"total_count": 1}
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked", lines
    assert task["payload"]["coordinator"].get("resume_phase") == "ci"
    qid = task["payload"]["coordinator"].get("question_activity_id")
    assert isinstance(qid, str) and qid
    assert fake.launched == launched_before
    assert "implementer" not in fake.launched

    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [
        {"id": 1, "body": body, "user": {"login": "worker-bot"}},
        {
            "id": 2,
            "body": "inventory restored; continue CI observation",
            "user": {"login": "human-owner"},
        },
    ]
    # Restore valid inventory before the reply resumes ci.
    fake.workflow_inventory_body = None
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
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "ci", lines
    assert fake.launched == launched_before
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "readiness", lines
    assert "implementer" not in fake.launched


def test_ci_inventory_transport_failure_retries_same_ci_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient inventory transport stays on phase=ci; no blocked reply gate."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-555555555558"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
        {"name": "tests", "conclusion": "success", "status": "completed"}
    ]
    fake.workflow_inventory_rc = 1
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "ci", lines
    assert task["payload"]["coordinator"].get("resume_phase") in (None, "")
    assert task["payload"]["coordinator"].get("question_activity_id") in (None, "")
    assert any("temporarily unavailable" in line for line in lines)
    assert fake.launched == launched_before
    # Same phase retries after transport recovers.
    fake.workflow_inventory_rc = 0
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
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "readiness", lines
    assert "implementer" not in fake.launched


def test_ci_pr_view_transport_failure_retries_same_ci_phase_then_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient PR-view transport stays on phase=ci; later tick observes green."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-555555555559"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
    fake.pr_view_rc = 1
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "ci", lines
    assert task["payload"]["coordinator"].get("resume_phase") in (None, "")
    assert task["payload"]["coordinator"].get("question_activity_id") in (None, "")
    assert any("PR view temporarily unavailable" in line for line in lines)
    assert fake.launched == launched_before
    # Same static observation recovers to green without a human reply gate.
    fake.pr_view_rc = 0
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "readiness", lines
    assert "implementer" not in fake.launched


def test_ci_rollup_protocol_fault_blocks_with_ci_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard malformed rollup still fail-closed blocked + resume_phase=ci."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    tid = "55555555-5555-5555-5555-55555555555a"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    seed_task(
        store,
        worker,
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
    fake.pr["statusCheckRollup"] = {"not": "a-list"}
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked", lines
    assert task["payload"]["coordinator"].get("resume_phase") == "ci"
    assert isinstance(task["payload"]["coordinator"].get("question_activity_id"), str)
    assert fake.launched == launched_before


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


def test_assignment_discovery_flattens_slurped_issue_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    issue = dict(fake.issues[0])
    fake.issues = []
    fake.issue_pages = [
        [
            {
                "number": 3,
                "id": 300,
                "title": "PR-shaped",
                "body": "",
                "html_url": "https://github.com/example/project/pull/3",
                "state": "open",
                "updated_at": "2026-09-01T00:00:00Z",
                "assignees": [{"login": "worker-bot"}],
                "pull_request": {"url": "https://api.github.com/repos/example/project/pulls/3"},
            }
        ],
        [issue],
    ]
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tasks = store.rows("task")
    assert tasks, lines
    assert tasks[0]["payload"]["coordinator"]["source"]["number"] == 7


def test_implementer_result_blocked_sets_task_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review-loop: RESULT blocked → task failed and stop (not reply-open)."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.model_outputs["implementer"] = (
        "STATUS: complete\nRESULT: blocked\nNeed a human decision before coding.\n"
    )
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    # Drive real admission, acceptance and checkout ownership before the model.
    for _ in range(5):
        tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
        tasks = store.rows("task")
        if tasks and tasks[0]["payload"]["coordinator"]["phase"] == "implement":
            break
    task = store.rows("task")[0]
    tid = task["id"]
    assert task["payload"]["coordinator"]["phase"] == "implement"
    assert fake.launched == []
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["state"] == "failed", lines
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert task["payload"]["coordinator"].get("resume_phase") in (None, "")
    assert "implementer" in fake.launched
    launched = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["state"] == "failed"
    assert fake.launched == launched
    assert any("failed task remains" in line for line in lines)


def test_uncertain_lane_authorized_reply_does_not_resume_model(
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
    tid = "88888888-8888-8888-8888-888888888888"
    qid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    marker = f"<!-- agent-coordinator:question:v1:{qid} -->"
    fake.comments = [
        {"id": 1, "body": f"Blocked uncertain\n{marker}", "user": {"login": "worker-bot"}},
        {"id": 2, "body": "please retry the lane", "user": {"login": "human-owner"}},
    ]
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
                    "resume_phase": "implement",
                    "uncertain_lane": True,
                    "blocker": "uncertain prior lane outcome",
                    "question_activity_id": qid,
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix",
                    },
                    "worktree": str(wt),
                    "branch": "task-88888888",
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
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert task["payload"]["coordinator"].get("uncertain_lane") is True
    assert task["payload"]["coordinator"].get("phase") != "implement"
    assert "implementer" not in fake.launched
    assert any("uncertain" in line.lower() or "blocked" in line.lower() for line in lines)


@pytest.mark.parametrize(
    "model_result,stdout",
    [
        (
            "done",
            "STATUS: complete\nRESULT: done\npatched without summaries\n",
        ),
        (
            "no-change",
            "STATUS: complete\nRESULT: no-change\nNo further code change.\n",
        ),
    ],
)
def test_missing_change_summaries_reply_checkpoint_resumes_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_result: str, stdout: str
) -> None:
    """done/no-change without SUMMARY_* pins a reply checkpoint and resumes implement safely."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.model_outputs["implementer"] = stdout
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    for _ in range(5):
        tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
        tasks = store.rows("task")
        if tasks and tasks[0]["payload"]["coordinator"]["phase"] == "implement":
            break
    task = store.rows("task")[0]
    tid = task["id"]
    assert task["payload"]["coordinator"]["phase"] == "implement"
    if model_result == "no-change":
        # no-change without PR/sha takes the dedicated no-change blocker; give a PR so
        # the missing-summary path is the one under test.
        task["payload"]["coordinator"]["pr_number"] = 42
        store.write("task", "update", tid, {k: v for k, v in task.items() if not k.startswith("_")})

    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    coord = task["payload"]["coordinator"]
    assert task["state"] == "open", lines
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "implement"
    qid = coord.get("question_activity_id")
    assert isinstance(qid, str) and qid
    assert "implementer" in fake.launched
    assert not task.get("change_summary_en")
    assert not task.get("change_summary_de")
    assert not any(
        r.get("task_id") == tid and r.get("key") == "implementer_done" and r.get("status") == "ja"
        for r in store.rows("checklist_item")
    )
    evidence = coord.get("evidence") if isinstance(coord.get("evidence"), dict) else {}
    assert evidence.get("tests_pass") is not True

    # Waiting/status re-publish must keep the same checkpoint and not reset consumed replies.
    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [{"id": 10, "body": body, "user": {"login": "worker-bot"}}]
    coord["replies_consumed_through"] = 10
    store.write("task", "update", tid, {k: v for k, v in task.items() if not k.startswith("_")})
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["question_activity_id"] == qid
    assert task["payload"]["coordinator"]["replies_consumed_through"] == 10
    assert task["payload"]["coordinator"]["phase"] == "blocked"

    fake.comments.append(
        {"id": 11, "body": "summaries will be supplied on the next implement pass", "user": {"login": "human-owner"}}
    )
    launched_before = list(fake.launched)
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "implement", lines
    assert fake.launched == launched_before
    # Resume must not forge checklist/test evidence from the failed summary pass.
    assert not any(
        r.get("task_id") == tid and r.get("key") == "implementer_done" and r.get("status") == "ja"
        for r in store.rows("checklist_item")
    )
    evidence = task["payload"]["coordinator"].get("evidence")
    if isinstance(evidence, dict):
        assert evidence.get("tests_pass") is not True
    assert not task.get("change_summary_en")
    assert not task.get("change_summary_de")
    # A later implementer start is allowed; still no forged evidence before it applies.
    fake.model_outputs["implementer"] = (
        "STATUS: complete\nRESULT: ask\nWhich summary wording is required?\n"
    )
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert "implementer" in fake.launched[len(launched_before) :]
    assert not any(
        r.get("task_id") == tid and r.get("key") == "implementer_done" and r.get("status") == "ja"
        for r in store.rows("checklist_item")
    )
    evidence = task["payload"]["coordinator"].get("evidence")
    if isinstance(evidence, dict):
        assert evidence.get("tests_pass") is not True


def test_accept_unassigned_race_reply_resumes_accept_before_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assignment removed between advance_one precheck and accept stays reply-recoverable."""
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)

    tid = "77777777-7777-7777-7777-777777777777"
    seed_task(
        store,
        worker,
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "title": "Fix widget",
            "repo": "example/project",
            "ref": None,
            "payload": {
                "coordinator": {
                    "phase": "accept",
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a-seed",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Fix widget",
                    },
                    "worker_login": "worker-bot",
                }
            },
            "state": "open",
            "current_round": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )

    from agent_cli import coordinator_github, coordinator_runtime

    calls = {"n": 0}
    real_verify = coordinator_github.verify_issue_assigned

    def race_verify(runner, repo, number, login):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return real_verify(runner, repo, number, login)
        raise CoordinatorError(
            f"issue {repo}#{number} is no longer assigned to configured login"
        )

    monkeypatch.setattr(coordinator_github, "verify_issue_assigned", race_verify)
    monkeypatch.setattr(coordinator_runtime, "verify_issue_assigned", race_verify)

    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    coord = task["payload"]["coordinator"]
    assert coord["phase"] == "blocked", lines
    assert coord.get("resume_phase") == "accept"
    qid = coord.get("question_activity_id")
    assert isinstance(qid, str) and qid
    assert "implementer" not in fake.launched
    assert not any(
        r.get("type") == "comment.post"
        and "Accepted for implementation" in str((r.get("payload") or {}).get("body") or "")
        for r in store.rows("activity")
    )

    activity = store.row("activity", qid)
    assert activity is not None
    body = str((activity.get("payload") or {}).get("body") or "")
    fake.comments = [{"id": 20, "body": body, "user": {"login": "worker-bot"}}]
    # Status re-publish while still unassigned must keep the checkpoint.
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["question_activity_id"] == qid
    assert task["payload"]["coordinator"]["phase"] == "blocked"
    assert "implementer" not in fake.launched

    # Reassignment restored; authorized reply required before accept resumes.
    monkeypatch.setattr(coordinator_github, "verify_issue_assigned", real_verify)
    monkeypatch.setattr(coordinator_runtime, "verify_issue_assigned", real_verify)
    fake.comments.append(
        {"id": 21, "body": "reassigned; continue acceptance", "user": {"login": "human-owner"}}
    )
    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "accept", lines
    assert "implementer" not in fake.launched

    lines = tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["phase"] == "checkout", lines
    assert any(
        r.get("type") == "comment.post"
        and "Accepted for implementation" in str((r.get("payload") or {}).get("body") or "")
        for r in store.rows("activity")
    )
    assert "implementer" not in fake.launched
    assert any(
        r.get("task_id") == tid and r.get("key") == "session_registered" and r.get("status") == "ja"
        for r in store.rows("checklist_item")
    )
    assert any(
        r.get("task_id") == tid and r.get("key") == "spec_written" and r.get("status") == "ja"
        for r in store.rows("checklist_item")
    )


def test_publish_blocker_auto_pins_from_resume_phase_without_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Central eligibility: resume_phase alone pins a checkpoint; status does not reset it."""
    from agent_cli.coordinator_github import publish_blocker, reply_checkpoint_eligible

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)

    tid = "66666666-6666-6666-6666-666666666666"
    seed_task(
        store,
        worker,
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
                    "resume_phase": "implement",
                    "blocker": "synthetic recoverable blocker",
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
            "state": "open",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    task = store.row("task", tid)
    assert reply_checkpoint_eligible(task) is True
    # Deliberately omit reply_checkpoint=True — eligibility must still pin.
    publish_blocker(
        store,
        worker,
        task,
        fake,
        "synthetic recoverable blocker",
        kind="synthetic-recoverable",
    )
    task = store.row("task", tid)
    qid = task["payload"]["coordinator"].get("question_activity_id")
    assert isinstance(qid, str) and qid
    task["payload"]["coordinator"]["replies_consumed_through"] = 99
    store.write("task", "update", tid, {k: v for k, v in task.items() if not k.startswith("_")})
    # Status re-publish (default reply_checkpoint=False) must keep checkpoint + cursor.
    publish_blocker(
        store,
        worker,
        store.row("task", tid),
        fake,
        "synthetic recoverable blocker",
        kind="status",
    )
    task = store.row("task", tid)
    assert task["payload"]["coordinator"]["question_activity_id"] == qid
    assert task["payload"]["coordinator"]["replies_consumed_through"] == 99

    failed = dict(task)
    failed["state"] = "failed"
    failed["payload"] = {
        "coordinator": {
            **task["payload"]["coordinator"],
            "resume_phase": "implement",
            "question_activity_id": None,
        }
    }
    assert reply_checkpoint_eligible(failed) is False
    done = dict(task)
    done["state"] = "done"
    done["payload"] = {
        "coordinator": {
            **task["payload"]["coordinator"],
            "resume_phase": "implement",
            "question_activity_id": None,
        }
    }
    assert reply_checkpoint_eligible(done) is False
    uncertain = dict(task)
    uncertain["payload"] = {
        "coordinator": {
            **task["payload"]["coordinator"],
            "uncertain_lane": True,
            "resume_phase": "implement",
            "question_activity_id": None,
        }
    }
    assert reply_checkpoint_eligible(uncertain) is False


def _ready_side_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_github: bool = False,
) -> tuple[Store, Any, FakeGh]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_run_bounded(monkeypatch, fake)
    if patch_github:
        patch_execute_github(monkeypatch)
    fake.pr["isDraft"] = True
    fake.pr["state"] = "OPEN"
    fake.pr["mergeable"] = "MERGEABLE"
    fake.pr["author"] = {"login": "worker-bot"}
    fake.pr["baseRefName"] = "develop"
    fake.pr["headRefOid"] = fake.head
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
    return store, worker, fake


def _seed_ready_side_task(
    store: Store,
    worker: Any,
    fake: FakeGh,
    *,
    tid: str,
    phase: str,
    checklist_codex: dict[str, str],
    grok_gates: tuple[str, ...] = ("quality", "logic"),
    codex_gates: tuple[str, ...] = (),
    seed_formal_review: bool = False,
    extra_evidence: dict[str, Any] | None = None,
    models: dict[str, str | None] | None = None,
    first_vendor: str = "grok",
    second_vendor: str = "codex",
) -> dict[str, Any]:
    from agent_cli.allow import CHECKLIST_KEYS

    wt = worker.workspace_root / tid
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()
    evidence = {
        "tests_pass": True,
        "tests_head": fake.head,
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    seed_task(
        store,
        worker,
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
                    "phase": phase,
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
                    "worktree": str(wt),
                    "branch": f"task-{tid[:8]}",
                    "base_sha": fake.base,
                    "head_sha": fake.head,
                    "evidence": evidence,
                    **({"a38": {"enabled": True}} if worker.a38 else {}),
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": "Implemented example/project#7.",
            "change_summary_de": "Umsetzung von example/project#7.",
            **({"models": models} if models is not None else {}),
        },
    )
    for key in CHECKLIST_KEYS["implement"]:
        if key in ("review_2_quality", "review_2_logic") and key not in checklist_codex:
            continue
        status = checklist_codex.get(
            key,
            "n_a" if key in ("deviation_declared", "deviation_granted") else "ja",
        )
        cid = f"{tid}-c-{key}"
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "key": key,
                "status": status,
                "evidence": (
                    "second review not posted"
                    if status == "n_a" and key in ("review_2_quality", "review_2_logic")
                    else "seed"
                ),
                "source": "human" if key in ("spec_written", "deviation_declared", "deviation_granted") else "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    gates: list[tuple[str, str, str]] = []
    for dim in grok_gates:
        gates.append(("review-1", dim, first_vendor))
    for dim in codex_gates:
        gates.append(("review-2", dim, second_vendor))
    for stage, dim, vendor in gates:
        gid = f"{tid}-g-{stage}-{dim}"
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
        f"{tid}-lc1",
        {
            "id": f"{tid}-lc1",
            "task_id": tid,
            "name": "coordinator-check",
            "command": "/operator/checks",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": fake.head,
        },
    )
    if seed_formal_review:
        fake.reviews = [
            {
                "id": 1,
                "body": f"Formal approval for head `{fake.head[:7]}`",
                "html_url": "https://x/r",
                "state": "APPROVED",
                "commit_id": fake.head,
                "user": {"login": "review-bot"},
            }
        ]
    task = store.row("task", tid)
    assert task is not None
    return task


def _list_opener(fake: FakeGh, login: str = "worker-bot") -> None:
    fake.pr_guard_body = json.dumps(
        {
            "schema": "pr-guard/v1",
            "a38": {"enforce": [], "exclude": [], "default": "enforce"},
            "one_review_authors": [login],
        }
    )


def test_readiness_skips_review_2_when_opener_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    _list_opener(fake)
    tid = "aa000001-0000-4000-8000-000000000001"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    lines = phase_readiness(store, worker, task, fake)
    assert any(f"readiness ok on {fake.head[:7]}" in line for line in lines)
    assert not any("missing approved gate review-2/" in line for line in lines)


def test_readiness_requires_review_2_when_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    tid = "aa000001-0000-4000-8000-000000000001"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    with pytest.raises(CoordinatorError, match="missing approved gate review-2/"):
        phase_readiness(store, worker, task, fake)


def test_readiness_still_requires_review_1_when_codex_checklist_na(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    tid = "aa000001-0000-4000-8000-000000000002"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
        grok_gates=("logic",),
    )
    with pytest.raises(CoordinatorError, match="missing approved gate review-1/"):
        phase_readiness(store, worker, task, fake)


def test_readiness_requires_review_2_when_only_one_key_na(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    cases = (
        (
            "aa000001-0000-4000-8000-000000000011",
            {"review_2_quality": "n_a", "review_2_logic": "ja"},
        ),
        (
            "aa000001-0000-4000-8000-000000000012",
            {"review_2_quality": "ja", "review_2_logic": "n_a"},
        ),
        (
            "aa000001-0000-4000-8000-000000000013",
            {"review_2_quality": "n_a"},
        ),
        (
            "aa000001-0000-4000-8000-000000000014",
            {"review_2_logic": "n_a"},
        ),
    )
    for tid, checklist_codex in cases:
        task = _seed_ready_side_task(
            store,
            worker,
            fake,
            tid=tid,
            phase="readiness",
            checklist_codex=checklist_codex,
        )
        with pytest.raises(CoordinatorError, match="missing approved gate review-2/"):
            phase_readiness(store, worker, task, fake)


def test_readiness_requires_review_2_when_both_keys_ja_and_gates_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    tid = "aa000001-0000-4000-8000-000000000021"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "ja", "review_2_logic": "ja"},
    )
    with pytest.raises(CoordinatorError, match="missing approved gate review-2/"):
        phase_readiness(store, worker, task, fake)


def test_readiness_requires_review_2_when_unavailable_with_na(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    tid = "aa000001-0000-4000-8000-000000000031"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "unavailable", "review_2_logic": "n_a"},
    )
    with pytest.raises(CoordinatorError, match="missing approved gate review-2/"):
        phase_readiness(store, worker, task, fake)


def test_formal_approve_skips_review_2_when_opener_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch, patch_github=True)
    _list_opener(fake)
    tid = "aa000001-0000-4000-8000-000000000041"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="formal_approve",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
        extra_evidence={"readiness_head": fake.head},
    )
    lines = phase_formal_approve(store, worker, task, fake)
    assert not any("missing approved gate review-2/" in line for line in lines)
    assert any(
        "formal APPROVE" in line or "formal approval not yet verified" in line
        for line in lines
    )


@pytest.mark.no_pg
def test_leave_draft_comment_names_one_run_only_for_the_exception() -> None:
    from agent_cli.coordinator_github import _leave_draft_comment

    head = "abcdef0" + ("1" * 33)
    one = _leave_draft_comment(head, one_review=True)
    two = _leave_draft_comment(head, one_review=False)
    assert "one review run is approved" in one
    assert "two review runs are required and approved" not in one
    assert "One review is enough only when the pull request opener is listed in one_review_authors" in one
    assert "A missing file, a missing key, or an empty list keeps two reviews required." in one
    assert "An entry that exists only on the pull request head does not count." in one
    assert "two review runs are required and approved" in two
    assert "and CI green." in two
    assert "one review run is approved" not in two
    assert "Still not merge; a human merges." in one
    assert "Still not merge; a human merges." in two


def test_leave_draft_skips_review_2_when_opener_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch, patch_github=True)
    _list_opener(fake)
    tid = "aa000001-0000-4000-8000-000000000051"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="leave_draft",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
        seed_formal_review=True,
        extra_evidence={
            "formal_head": fake.head,
            "readiness_head": fake.head,
        },
    )
    lines = phase_leave_draft(store, worker, task, fake)
    assert not any("missing approved gate review-2/" in line for line in lines)
    assert any("left draft" in line for line in lines)
    assert fake.ready_calls == 1


def test_leave_draft_does_not_ready_when_the_exception_is_revoked_after_the_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch, patch_github=True)
    _list_opener(fake)
    fake.revoke_one_review_on_pr_comment = True
    from agent_cli import coordinator_github

    marked = coordinator_github.execute_github

    def deliver_then_mark(store, runner, *, activity_ids):  # noqa: ANN001
        for aid in activity_ids:
            row = store.row("activity", aid) or {}
            if row.get("type") != "comment.post" or row.get("execution_status") != "pending":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            fake(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(payload.get("number") or 42),
                    "--repo",
                    str(payload.get("repo") or "example/project"),
                    "--body",
                    str(payload.get("body") or ""),
                ]
            )
        return marked(store, runner, activity_ids=activity_ids)

    monkeypatch.setattr(coordinator_github, "execute_github", deliver_then_mark)
    tid = "aa000001-0000-4000-8000-000000000052"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="leave_draft",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
        seed_formal_review=True,
        extra_evidence={
            "formal_head": fake.head,
            "readiness_head": fake.head,
        },
    )
    with pytest.raises(
        CoordinatorError,
        match="one-review exception revoked before leave-draft",
    ):
        phase_leave_draft(store, worker, task, fake)
    assert fake.ready_calls == 0
    assert fake.pr["isDraft"] is True
    bodies = "\n".join(str(comment.get("body") or "") for comment in fake.pr_comments)
    assert "one review run is approved" in bodies
    assert "The one-review exception was no longer present" in bodies
    assert "Two review runs are required." in bodies


def test_leave_draft_posts_the_revoked_comment_on_a_later_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch, patch_github=True)
    _list_opener(fake)
    fake.fail_pr_guard_after_comment = True
    from agent_cli import coordinator_github

    marked = coordinator_github.execute_github

    def deliver_then_mark(store, runner, *, activity_ids):  # noqa: ANN001
        for aid in activity_ids:
            row = store.row("activity", aid) or {}
            if row.get("type") != "comment.post" or row.get("execution_status") != "pending":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            fake(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(payload.get("number") or 42),
                    "--repo",
                    str(payload.get("repo") or "example/project"),
                    "--body",
                    str(payload.get("body") or ""),
                ]
            )
        return marked(store, runner, activity_ids=activity_ids)

    monkeypatch.setattr(coordinator_github, "execute_github", deliver_then_mark)
    tid = "aa000001-0000-4000-8000-000000000053"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="leave_draft",
        checklist_codex={"review_2_quality": "n_a", "review_2_logic": "n_a"},
        seed_formal_review=True,
        extra_evidence={
            "formal_head": fake.head,
            "readiness_head": fake.head,
        },
    )
    lines = phase_leave_draft(store, worker, task, fake)
    assert fake.ready_calls == 0
    assert any("temporarily unavailable" in line for line in lines)
    bodies = "\n".join(str(comment.get("body") or "") for comment in fake.pr_comments)
    assert "one review run is approved" in bodies
    assert "The one-review exception was no longer present" not in bodies

    fake.pr_guard_body = json.dumps(
        {
            "schema": "pr-guard/v1",
            "a38": {"enforce": [], "exclude": [], "default": "enforce"},
            "one_review_authors": [],
        }
    )
    task = store.row("task", tid)
    assert task is not None
    with pytest.raises(
        CoordinatorError,
        match="one-review exception revoked before leave-draft",
    ):
        phase_leave_draft(store, worker, task, fake)
    assert fake.ready_calls == 0
    assert fake.pr["isDraft"] is True
    bodies = "\n".join(str(comment.get("body") or "") for comment in fake.pr_comments)
    assert "one review run is approved" in bodies
    assert "The one-review exception was no longer present" in bodies
    assert "Two review runs are required." in bodies


def test_readiness_accepts_approved_review_2_when_checklist_not_both_na(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    tid = "aa000001-0000-4000-8000-000000000061"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="readiness",
        checklist_codex={"review_2_quality": "ja", "review_2_logic": "ja"},
        codex_gates=("quality", "logic"),
    )
    lines = phase_readiness(store, worker, task, fake)
    assert any(f"readiness ok on {fake.head[:7]}" in line for line in lines)


_POSTED_HEAD = "c" * 40


def _seed_posted_codex_task(store, worker, tid: str, *, codex_dims: tuple[str, ...], codex_status: dict[str, str]) -> dict:
    """Review task whose Codex checklist is n_a unless a test overrides it."""
    from agent_cli.allow import CHECKLIST_KEYS

    seed_task(
        store,
        worker,
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "review",
            "title": "Review",
            "repo": "example/project",
            "ref": None,
            "payload": {
                "coordinator": {
                    "phase": "pr_gates_codex",
                    "worktree": str(worker.workspace_root / tid),
                    "base_sha": "b" * 40,
                    "head_sha": _POSTED_HEAD,
                    "source": {
                        "repo": "example/project",
                        "number": 7,
                        "assigned_id": "a-posted",
                        "publication_repo": "example/project",
                        "base": "develop",
                        "title": "Review",
                    },
                }
            },
            "state": "pr-review",
            "current_round": 1,
            "models": {"first": "grok", "second": "codex"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "change_summary_en": "Reviewed.",
            "change_summary_de": "Geprüft.",
        },
    )
    before = {
        "session_registered",
        "contributing_read",
        "review_1_quality",
        "review_1_logic",
    }
    for key in CHECKLIST_KEYS["review"]:
        if key in codex_status:
            status, evidence = codex_status[key], "second review not posted"
        elif key in before:
            status, evidence = "ja", "seed"
        else:
            status, evidence = "pending", None
        cid = f"{tid}-{key}"
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "key": key,
                "status": status,
                "evidence": evidence,
                "source": "script",
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    for stage, dim, vendor in (
        ("review-1", "quality", "grok"),
        ("review-1", "logic", "grok"),
    ):
        gid = f"{tid}-{stage}-{dim}"
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
                "head_sha": _POSTED_HEAD,
                "agent_id": f"{tid}-agent-{dim}",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
    for dim in codex_dims:
        gid = f"{tid}-review-2-{dim}"
        store.write(
            "review_gate",
            "insert",
            gid,
            {
                "id": gid,
                "task_id": tid,
                "stage": "review-2",
                "dimension": dim,
                "vendor": "codex",
                "verdict": "approved",
                "evidence": None,
                "head_sha": _POSTED_HEAD,
                "agent_id": f"{tid}-codex-{dim}",
                "recorded_at": "2026-01-01T00:00:01Z",
            },
        )
    task = store.row("task", tid)
    assert task is not None
    return task


def _checklist_status(store, tid: str) -> dict[str, str]:
    return {
        str(row.get("key")): str(row.get("status"))
        for row in store.rows("checklist_item")
        if row.get("task_id") == tid
    }


def _patch_pr_gate_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_diff(store, worker, task, runner, *, head):  # noqa: ANN001
        directory = worker.workspace_root / ".coordinator-control" / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"review-{head[:12]}.patch"
        path.write_text("diff\n", encoding="utf-8")
        path.with_suffix(".excerpt.txt").write_text("excerpt\n", encoding="utf-8")
        return path

    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_signed_clean_head",
        lambda *args, **kwargs: _POSTED_HEAD,
    )
    monkeypatch.setattr("agent_cli.coordinator_lanes.write_review_diff", fake_diff)
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.write_review_context",
        lambda *args, **kwargs: "",
    )


def _codex_prelaunch_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tid: str
) -> tuple[Store, Any, dict[str, Any]]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    return store, worker, task


def _record_blocker_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    published: list[str] = []

    def execute(store, _runner, *, activity_ids):  # noqa: ANN001
        for activity_id in activity_ids:
            row = store.row("activity", activity_id)
            assert row is not None
            assert row.get("type") == "comment.post"
            published.append(activity_id)
            updated = {
                key: value for key, value in row.items() if not key.startswith("_")
            }
            updated["execution_status"] = "done"
            updated["result"] = {
                "repo": "example/project",
                "number": 7,
                "url": "https://x/comment",
                "id": len(published),
            }
            store.write("activity", "update", activity_id, updated)
        return []

    monkeypatch.setattr("agent_cli.coordinator_github.execute_github", execute)
    return published


def test_codex_preparation_oserror_closes_prepared_agents_before_one_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes
    from agent_cli.coordinator_runtime import advance_one

    tid = "aa000001-0000-4000-8000-000000000062"
    store, worker, _task = _codex_prelaunch_fixture(tmp_path, monkeypatch, tid)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    published = _record_blocker_publications(monkeypatch)
    real_mkstemp = coordinator_lanes.tempfile.mkstemp
    mkstemp_calls = 0
    output_paths: list[Path] = []

    def fail_second_mkstemp(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        nonlocal mkstemp_calls
        mkstemp_calls += 1
        if mkstemp_calls == 2:
            raise OSError("cannot prepare output")
        fd, path = real_mkstemp(dir=tmp_path)
        output_paths.append(Path(path))
        return fd, path

    monkeypatch.setattr(coordinator_lanes.tempfile, "mkstemp", fail_second_mkstemp)
    lines = advance_one(
        store,
        worker,
        runner=fake,
        lane_runner=lambda _argv, _stdin=None: Completed(
            0, "STATUS: complete\nRESULT: approved\n", ""
        ),
    )

    agents = [row for row in store.rows("agent") if row.get("task_id") == tid]
    assert len(agents) == 1
    assert agents[0]["status"] == "done"
    assert agents[0]["note"] == "prelaunch aborted"
    assert output_paths and all(not path.exists() for path in output_paths)
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    assert published == [comments[0]["id"]]
    assert "cannot prepare output" in str(comments[0].get("payload"))
    assert "phase-error" in str(comments[0].get("payload"))
    assert "gate-prelaunch" not in str(comments[0].get("payload"))
    saved = store.row("task", tid)
    assert saved is not None
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "pr_gates_codex"
    assert checkpoint["question_activity_id"] == comments[0]["id"]
    assert lines == [
        "blocked: cannot prepare output",
        "blocker published on example/project#7",
    ]


def test_preparation_failure_after_mkstemp_removes_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes

    tid = "aa000001-0000-4000-8000-000000000064"
    store, worker, task = _codex_prelaunch_fixture(tmp_path, monkeypatch, tid)
    real_mkstemp = coordinator_lanes.tempfile.mkstemp
    output_paths: list[Path] = []

    def tracked_mkstemp(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        fd, path = real_mkstemp(dir=tmp_path)
        output_paths.append(Path(path))
        return fd, path

    monkeypatch.setattr(coordinator_lanes.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(
        coordinator_lanes,
        "provider_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider argv failed")
        ),
    )

    with pytest.raises(RuntimeError, match="provider argv failed"):
        coordinator_lanes._prepare_pr_review_agent(
            store,
            worker,
            task,
            role="pr-reviewer-quality",
            vendor="codex",
            stage="review-2",
            head=_POSTED_HEAD,
            spec_body="Review.",
        )

    assert output_paths and all(not path.exists() for path in output_paths)
    assert [row for row in store.rows("agent") if row.get("task_id") == tid] == []


@pytest.mark.parametrize("failure", ["decode", "oserror"])
def test_run_prepared_returns_unavailable_and_removes_owned_output(
    tmp_path: Path, failure: str
) -> None:
    from agent_cli import coordinator_lanes
    from agent_cli.lane import LaneResult

    output = tmp_path / "provider-output.txt"
    if failure == "decode":
        output.write_bytes(b"\xff")

    prepared = {
        "agent_id": "agent-1",
        "role": "pr-reviewer-quality",
        "vendor": "codex",
        "argv": ["codex"],
        "stdin_text": None,
        "output_file": str(output) if failure == "decode" else str(tmp_path / "gone.txt"),
        "model": "codex-model",
        "worktree": str(tmp_path),
    }

    agent_id, outcome = coordinator_lanes._run_prepared(
        prepared,
        timeout=1,
        lane_runner=lambda _argv, _stdin=None: Completed(0, "", ""),
    )

    assert agent_id == "agent-1"
    assert isinstance(outcome, LaneResult)
    assert outcome.status == "unavailable"
    assert outcome.stdout == ""
    if failure == "decode":
        assert "not valid UTF-8" in outcome.stderr
        assert not output.exists()
    else:
        assert "lane output file is unavailable" in outcome.stderr
    assert len(outcome.stderr) <= 4000


def test_thread_start_failure_closes_all_prepared_agents_before_one_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes
    from agent_cli.coordinator_runtime import advance_one

    tid = "aa000001-0000-4000-8000-000000000063"
    store, worker, _task = _codex_prelaunch_fixture(tmp_path, monkeypatch, tid)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    published = _record_blocker_publications(monkeypatch)
    start_calls = 0
    provider_calls: list[list[str]] = []
    joined: list[threading.Thread] = []
    real_start = threading.Thread.start
    real_join = threading.Thread.join

    def fail_second_start(thread: threading.Thread) -> None:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 2:
            raise RuntimeError("thread start failed")
        real_start(thread)

    def track_join(thread: threading.Thread, *args: Any, **kwargs: Any) -> None:
        real_join(thread, *args, **kwargs)
        joined.append(thread)

    monkeypatch.setattr(
        coordinator_lanes.threading.Thread, "start", fail_second_start
    )
    monkeypatch.setattr(coordinator_lanes.threading.Thread, "join", track_join)

    def lane_runner(argv: list[str], _stdin: str | None = None) -> Completed:
        provider_calls.append(argv)
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    lines = advance_one(store, worker, runner=fake, lane_runner=lane_runner)

    agents = [row for row in store.rows("agent") if row.get("task_id") == tid]
    assert len(agents) == 2
    assert all(row.get("status") == "done" for row in agents)
    assert all(row.get("note") == "prelaunch aborted" for row in agents)
    assert provider_calls == []
    assert len(joined) == 1
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    assert published == [comments[0]["id"]]
    assert "thread start failed" in str(comments[0].get("payload"))
    assert "phase-error" in str(comments[0].get("payload"))
    assert "gate-prelaunch" not in str(comments[0].get("payload"))
    saved = store.row("task", tid)
    assert saved is not None
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "pr_gates_codex"
    assert checkpoint["question_activity_id"] == comments[0]["id"]
    assert lines == [
        "blocked: thread start failed",
        "blocker published on example/project#7",
    ]


def test_uncertain_prior_pr_lane_clears_stale_resume_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import phase_pr_gates

    tid = "aa000001-0000-4000-8000-000000000065"
    store, worker, task = _codex_prelaunch_fixture(tmp_path, monkeypatch, tid)
    task["payload"]["coordinator"]["resume_phase"] = "implement"
    store.write(
        "task", "update", tid,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    store.write(
        "agent",
        "insert",
        "aa000001-0000-4000-8000-000000000066",
        {
            "id": "aa000001-0000-4000-8000-000000000066",
            "session_id": "worker-session",
            "task_id": tid,
            "round": None,
            "role": "pr-reviewer-quality",
            "vendor": "codex",
            "status": "working",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "note": None,
        },
    )
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)

    with pytest.raises(CoordinatorError, match="uncertain prior lane outcome"):
        phase_pr_gates(
            store,
            worker,
            store.row("task", tid),
            fake,
            None,
            vendor="codex",
            stage="review-2",
        )

    checkpoint = store.row("task", tid)["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["uncertain_lane"] is True
    assert checkpoint.get("resume_phase") is None


def test_approved_codex_stage_reconciles_pending_checklist_on_a_later_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached gates repair pending keys when close_allowed permits both closes."""
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-000000000054"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=("quality", "logic"),
        codex_status={"review_2_quality": "pending", "review_2_logic": "pending"},
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched: list[str] = []

    def run(argv, stdin=None):  # noqa: ANN001
        launched.append("lane")
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    lines = phase_pr_gates(
        store, worker, task, lambda argv: Completed(0, "", ""), run, vendor="codex", stage="review-2"
    )
    assert launched == []
    assert any("already complete" in line for line in lines)
    status = _checklist_status(store, tid)
    assert status["review_2_quality"] == "ja"
    assert status["review_2_logic"] == "ja"
    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "ci"
    assert saved["payload"]["coordinator"].get("phase") != "blocked"


def test_persisted_rejected_gate_routes_to_implementer_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash recovery treats a current-head rejection as the durable verdict."""
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-0000000000f9"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "pending", "review_2_logic": "pending"},
    )
    head = task["payload"]["coordinator"]["head_sha"]
    store.write(
        "review_gate",
        "insert",
        "aa000001-0000-4000-8000-0000000000f2",
        {
            "id": "aa000001-0000-4000-8000-0000000000f2",
            "task_id": tid,
            "stage": "review-2",
            "dimension": "logic",
            "vendor": "codex",
            "verdict": "rejected",
            "evidence": "Logic found a durable defect.",
            "head_sha": head,
            "agent_id": "aa000001-0000-4000-8000-0000000000f3",
            "recorded_at": "2026-01-01T00:00:00.000000Z",
        },
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched: list[str] = []

    def run(argv, stdin=None):  # noqa: ANN001
        launched.append("lane")
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    lines = phase_pr_gates(
        store,
        worker,
        task,
        lambda argv: Completed(0, "", ""),
        run,
        vendor="codex",
        stage="review-2",
    )

    assert launched == []
    assert any("rejection recovered" in line for line in lines)
    saved = store.row("task", tid)
    assert saved["state"] == "implementing"
    assert saved["payload"]["coordinator"]["phase"] == "implement"
    assert saved["payload"]["coordinator"]["findings"] == "Logic found a durable defect."


def test_cached_codex_dimension_replaces_n_a_while_the_other_lane_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved dimension cached on retry still moves n_a to ja. The other lane starts."""
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-000000000055"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=("quality",),
        codex_status={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched: list[str] = []

    def run(argv, stdin=None):  # noqa: ANN001
        text = " ".join(str(part) for part in argv) + "\n" + (stdin or "")
        for name in ("pr-reviewer-quality", "pr-reviewer-logic"):
            if name in text:
                launched.append(name)
                break
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    lines = phase_pr_gates(
        store, worker, task, lambda argv: Completed(0, "", ""), run, vendor="codex", stage="review-2"
    )
    assert launched == ["pr-reviewer-logic"]
    assert any("cached" in line for line in lines)
    assert any("logic=approved" in line for line in lines)
    status = _checklist_status(store, tid)
    assert status["review_2_quality"] == "ja"
    assert status["review_2_logic"] == "ja"
    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "ci"


def test_codex_lanes_still_start_when_the_checklist_is_n_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-review checklist does not skip the Codex lane."""
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-000000000056"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched: list[str] = []

    def run(argv, stdin=None):  # noqa: ANN001
        text = " ".join(str(part) for part in argv) + "\n" + (stdin or "")
        for name in ("pr-reviewer-quality", "pr-reviewer-logic"):
            if name in text:
                launched.append(name)
                break
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    phase_pr_gates(
        store, worker, task, lambda argv: Completed(0, "", ""), run, vendor="codex", stage="review-2"
    )
    assert set(launched) == {"pr-reviewer-quality", "pr-reviewer-logic"}
    status = _checklist_status(store, tid)
    assert status["review_2_quality"] == "ja"
    assert status["review_2_logic"] == "ja"
    lane_runs = [
        row for row in store.rows("activity") if row.get("type") == "lane.run"
    ]
    assert len(lane_runs) == 2
    assert {row["payload"]["role"] for row in lane_runs} == {
        "pr-reviewer-quality", "pr-reviewer-logic"
    }


def test_parallel_openrouter_lanes_record_usage_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    config_path = store.home / "ai-accounts.json"
    data = json.loads(config_path.read_text())
    data["accounts"]["router"] = {
        "provider": "openrouter", "config_dir": "/test/openrouter"
    }
    data["roles"]["router-reader"] = {
        "account": "router", "model": "vendor/model", "access": "read-only"
    }
    data["sessions"]["worker-session"]["lanes"].update(
        {
            "openrouter:pr-reviewer-quality": "router-reader",
            "openrouter:pr-reviewer-logic": "router-reader",
        }
    )
    config_path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-0000000000e2"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    task["models"] = {"first": "grok", "second": "openrouter"}
    store.write(
        "task", "update", tid, {key: value for key, value in task.items() if not key.startswith("_")}
    )
    Path(task["payload"]["coordinator"]["worktree"]).mkdir(parents=True)
    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched_prompts: dict[str, str] = {}
    attached_dirs: dict[str, str] = {}

    def run(argv, stdin=None):  # noqa: ANN001
        wrapper_index = next(i for i, value in enumerate(argv) if "openrouter-api-key" in value)
        output_file = Path(argv[wrapper_index + 2])
        role = "pr-reviewer-quality" if "pr-reviewer-quality" in (stdin or "") else "pr-reviewer-logic"
        launched_prompts[role] = stdin or ""
        attached_dirs[role] = str(argv[argv.index("--add-dir") + 1])
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": role, "name": "Grep", "input": {"pattern": role}}
            ]}},
            {"type": "result", "result": "STATUS: complete\nRESULT: approved",
             "usage": {"input_tokens": 20, "output_tokens": 6},
             "modelUsage": {"vendor/model": {}}, "num_turns": 2, "duration_ms": 40},
        ]
        output_file.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )
        return Completed(0, "", "")

    phase_pr_gates(
        store,
        worker,
        task,
        lambda argv: Completed(0, "", ""),
        run,
        vendor="openrouter",
        stage="review-2",
    )
    rows = [row for row in store.rows("activity") if row.get("type") == "lane.run"]
    assert len(rows) == 2
    assert all(row["payload"]["usage"]["input_tokens"] == 20 for row in rows)
    assert all(row["payload"]["tools"][0]["tool"] == "Grep" for row in rows)
    agents = [
        row
        for row in store.rows("agent")
        if row.get("task_id") == tid and row.get("vendor") == "openrouter"
    ]
    assert len(agents) == 2
    for agent in agents:
        launched = launched_prompts[str(agent["role"])]
        assert agent["prompt"] == launched
        assert "Script-generated diff artifact (full, read-only):" in launched
        assert "diff excerpt" not in launched
        assert attached_dirs[str(agent["role"])] in launched
        assert len(launched) <= 20_000


def test_parallel_openrouter_missing_key_records_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    config_path = store.home / "ai-accounts.json"
    data = json.loads(config_path.read_text())
    data["accounts"]["router"] = {
        "provider": "openrouter",
        "config_dir": "/test/openrouter",
    }
    data["roles"]["router-reader"] = {
        "account": "router",
        "model": "vendor/model",
        "access": "read-only",
    }
    data["sessions"]["worker-session"]["lanes"].update(
        {
            "openrouter:pr-reviewer-quality": "router-reader",
            "openrouter:pr-reviewer-logic": "router-reader",
        }
    )
    config_path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-0000000000e3"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "n_a", "review_2_logic": "n_a"},
    )
    task["models"] = {"first": "grok", "second": "openrouter"}
    store.write(
        "task",
        "update",
        tid,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    Path(task["payload"]["coordinator"]["worktree"]).mkdir(parents=True)
    _patch_pr_gate_git(monkeypatch, tmp_path)
    patch_execute_github(monkeypatch)
    reason = "STATUS: unavailable\nopenrouter key missing at <config_dir>/openrouter-api-key\n"

    def missing_key(argv, stdin=None):  # noqa: ANN001
        assert any("openrouter-api-key" in str(value) for value in argv)
        return Completed(
            2, "", "STATUS: unavailable\nopenrouter key missing at /test/openrouter/openrouter-api-key\n"
        )

    lines = phase_pr_gates(
        store,
        worker,
        task,
        lambda argv: Completed(0, "", ""),
        missing_key,
        vendor="openrouter",
        stage="review-2",
    )
    assert any("unavailable; blocked" in line for line in lines)
    agents = [
        row
        for row in store.rows("agent")
        if row.get("task_id") == tid and row.get("vendor") == "openrouter"
    ]
    assert len(agents) == 2
    assert all(row["note"] == reason for row in agents)
    lane_runs = [
        row
        for row in store.rows("activity")
        if row.get("type") == "lane.run" and row["payload"].get("task_id") == tid
    ]
    assert len(lane_runs) == 2
    assert all(row["payload"]["final_text"] == reason for row in lane_runs)
    assert not any("/test/openrouter" in row["note"] for row in agents)


def test_approved_retry_does_not_advance_when_pending_codex_key_cannot_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached approval stays recoverable when an earlier checklist hole denies close."""
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-000000000057"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=("quality", "logic"),
        codex_status={"review_2_quality": "pending", "review_2_logic": "n_a"},
    )
    review_1_quality = store.row("checklist_item", f"{tid}-review_1_quality")
    assert review_1_quality is not None
    review_1_quality["status"] = "pending"
    review_1_quality["evidence"] = None
    store.write(
        "checklist_item",
        "update",
        review_1_quality["id"],
        {
            key: value
            for key, value in review_1_quality.items()
            if not key.startswith("_")
        },
    )
    _patch_pr_gate_git(monkeypatch, tmp_path)
    lines = phase_pr_gates(
        store,
        worker,
        task,
        lambda argv: Completed(0, "", ""),
        None,
        vendor="codex",
        stage="review-2",
    )
    assert any(
        "recoverable checklist blocker for review_2_quality" in line
        for line in lines
    )
    status = _checklist_status(store, tid)
    assert status["review_2_quality"] == "pending"
    assert status["review_2_logic"] == "ja"
    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "pr_gates_codex"


def test_pr_review_scope_keeps_the_compliance_reminder_and_states_a_missing_issue_is_not_a_defect() -> None:
    from agent_cli.a38_review import COMPLIANCE_PROMPT
    from agent_cli.coordinator_lanes import _pr_review_scope

    quality = _pr_review_scope("quality")
    logic = _pr_review_scope("logic")
    for scope in (quality, logic):
        assert scope.startswith(COMPLIANCE_PROMPT + " ")
        assert "A missing issue is not a defect." in scope
    assert "Also judge it against the linked issue." in quality
    assert "complete for the linked issue" in logic
    assert "and the linked issue" in logic
    assert "Quality:" in quality
    assert "Logic:" in logic


def test_execution_binding_includes_explicit_models_block(tmp_path: Path) -> None:
    """The coordinator hashes the required device model assignment."""
    from agent_cli.coordinator_runtime import execution_binding

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    repo = next(iter(worker.repositories))
    before = execution_binding(store, worker, repo)
    import hashlib
    from dataclasses import asdict

    from agent_cli.ai_accounts import load_ai_accounts
    from agent_cli.coordinator_runtime import repo_cfg
    from agent_cli.github_accounts import load_accounts

    configured_slots = (
        "grok:implementer",
        "grok:reviewer",
        "grok:pr-reviewer-quality",
        "grok:pr-reviewer-logic",
        "codex:pr-reviewer-quality",
        "codex:pr-reviewer-logic",
    )
    ai = load_ai_accounts(store.home)
    github = load_accounts(store.home)
    selected = {
        "repository": asdict(repo_cfg(worker, repo)), "workspace": str(worker.workspace_root),
        "worker_session": worker.session_id, "review_session": worker.review_session,
        "reply_logins": worker.reply_logins,
        "github": [asdict(github.for_session(sid)) for sid in (worker.session_id, worker.review_session)],
        "models": {"first": "grok", "second": "codex"},
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "lanes": [asdict(ai.for_lane(worker.session_id, s.split(":", 1)[1], s.split(":", 1)[0])) for s in configured_slots],
    }
    assert before == hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": "claude"}
    data["accounts"]["claude-w"] = {"provider": "claude", "config_dir": "/test/claude"}
    data["roles"]["claude-reader"] = {"account": "claude-w", "model": "m", "access": "read-only"}
    data["lanes"] = {"claude:pr-reviewer-quality": "claude-reader", "claude:pr-reviewer-logic": "claude-reader"}
    path.write_text(json.dumps(data))
    assert execution_binding(store, worker, repo) != before
    data.pop("models")
    path.write_text(json.dumps(data))
    from agent_cli.ai_accounts import AccountError

    with pytest.raises(AccountError, match="models is not configured"):
        execution_binding(store, worker, repo)


@pytest.mark.parametrize("nondefault_models", [False, True])
def test_accepted_legacy_execution_binding_is_repinned_once(
    tmp_path: Path, nondefault_models: bool
) -> None:
    """Both old hash forms migrate; subsequent execution changes still stop."""
    from agent_cli.coordinator_runtime import (
        _legacy_execution_binding,
        execution_binding,
        refresh_execution_binding,
    )

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    repo = next(iter(worker.repositories))
    path = store.home / "ai-accounts.json"
    configuration = json.loads(path.read_text())
    if nondefault_models:
        configuration["models"] = {"first": "claude", "second": "codex"}
        configuration["accounts"]["claude-w"] = {
            "provider": "claude",
            "config_dir": "/test/claude",
        }
        configuration["roles"]["claude-impl"] = {
            "account": "claude-w",
            "model": "claude-model",
            "access": "workspace-write",
        }
        configuration["roles"]["claude-rev"] = {
            "account": "claude-w",
            "model": "claude-model",
            "access": "read-only",
        }
        lanes = configuration["sessions"]["worker-session"]["lanes"]
        configuration["sessions"]["worker-session"]["lanes"] = {
            key.replace("grok:", "claude:"): (
                "claude-impl" if value == "impl" else "claude-rev"
            )
            for key, value in lanes.items()
            if key.startswith("grok:")
        } | {
            key: value for key, value in lanes.items() if key.startswith("codex:")
        }
        path.write_text(json.dumps(configuration))

    from agent_cli.ai_accounts import load_ai_accounts

    ai = load_ai_accounts(store.home)
    legacy = _legacy_execution_binding(store, worker, repo, ai)
    tid = (
        "aa000001-0000-4000-8000-0000000000e1"
        if nondefault_models
        else "aa000001-0000-4000-8000-0000000000e0"
    )
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "open",
        "payload": {
            "coordinator": {
                "source": {"repo": repo},
                "execution_binding": legacy,
            }
        },
    }
    store.write("task", "insert", tid, task)
    stored = store.row("task", tid)
    assert stored is not None

    refresh_execution_binding(store, worker, stored)

    migrated = store.row("task", tid)
    assert migrated is not None
    assert migrated["payload"]["coordinator"]["execution_binding"] == execution_binding(
        store, worker, repo
    )

    configuration = json.loads(path.read_text())
    selected_implementer = "claude-impl" if nondefault_models else "impl"
    configuration["roles"][selected_implementer]["model"] = "changed-model"
    path.write_text(json.dumps(configuration))
    with pytest.raises(CoordinatorError, match="task execution configuration changed"):
        refresh_execution_binding(store, worker, migrated)


def test_preflight_refuses_codex_as_first_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "codex", "second": "grok"}
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    with pytest.raises(CoordinatorError, match="models.first = codex"):
        preflight_worker(store, worker, fake)


def test_preflight_refuses_missing_lane_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data.pop("lane_execution")
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    with pytest.raises(CoordinatorError, match="lane_execution is not configured"):
        preflight_worker(store, worker, fake)


def test_preflight_refuses_remote_lane_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["lane_execution"] = {"mode": "remote", "host": "lane.example"}
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)

    with pytest.raises(CoordinatorError, match="remote.*not supported"):
        preflight_worker(store, worker, fake)
    assert fake.launched == []


def test_preflight_refuses_a_separate_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["implementer"] = "deepseek"
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    with pytest.raises(CoordinatorError, match="separate implementer is not supported"):
        preflight_worker(store, worker, fake)


def test_create_implement_task_refuses_a_separate_implementer(tmp_path: Path) -> None:
    from agent_cli.coordinator_runtime import _create_implement_task

    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["implementer"] = "deepseek"
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)

    with pytest.raises(CoordinatorError, match="separate implementer is not supported"):
        _create_implement_task(
            store,
            worker,
            repo="example/project",
            number=7,
            title="Fix widget",
            assigned_id="assigned-7",
            repository_config=worker.repositories["example/project"],
            body="Please fix",
        )
    assert store.rows("task") == []


def test_create_implement_task_refuses_external_second_for_non_a38_worker(
    tmp_path: Path,
) -> None:
    from agent_cli.coordinator_runtime import _create_implement_task

    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)

    with pytest.raises(CoordinatorError, match="only for A38 workers"):
        _create_implement_task(
            store,
            worker,
            repo="example/project",
            number=7,
            title="Fix widget",
            assigned_id="assigned-7",
            repository_config=worker.repositories["example/project"],
            body="Please fix",
        )
    assert store.rows("task") == []


def test_create_implement_task_accepts_null_second_for_a38_worker(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_runtime import _create_implement_task

    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )

    task = _create_implement_task(
        store,
        worker,
        repo="example/project",
        number=7,
        title="Fix widget",
        assigned_id="assigned-7",
        repository_config=worker.repositories["example/project"],
        body="Please fix",
    )
    assert task is not None
    assert task["models"] == {"first": "grok", "second": None}
    assert task["payload"]["coordinator"]["a38"] == {"enabled": True}
    assert len(store.rows("task")) == 1


def test_preflight_refuses_external_second_for_non_a38_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    with pytest.raises(CoordinatorError, match="only for A38 workers"):
        preflight_worker(store, worker, fake)


def test_a38_preflight_requires_null_second_and_excludes_worker_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    with pytest.raises(CoordinatorError, match="models.second = null"):
        preflight_worker(store, worker, fake)

    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": None}
    path.write_text(json.dumps(data))
    preflight_worker(store, worker, fake)

    from agent_cli.coordinator_runtime import execution_binding

    binding = execution_binding(store, worker, "example/project")
    assert execution_binding(
        store, replace(worker, review_session="ignored-session"), "example/project"
    ) == binding
    assert execution_binding(
        store, replace(worker, second_reviewers=("another-reviewer",)), "example/project"
    ) != binding

    own_reviewer = replace(worker, second_reviewers=("worker-bot",))
    with pytest.raises(CoordinatorError, match="worker GitHub login"):
        preflight_worker(store, own_reviewer, fake)


def test_a38_first_review_pass_count_survives_crash_after_gate_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.allow import CHECKLIST_KEYS
    from agent_cli.coordinator_a38 import phase_first_comment
    from agent_cli.coordinator_github import phase_blocked
    from agent_cli.coordinator_lanes import (
        _reconcile_approved_gate_checklist as real_reconcile_approved_gate_checklist,
        phase_pr_gates,
    )

    store = Store(tmp_path)
    write_accounts(store.home)
    manifest = store.home / "ai-accounts.json"
    ai = json.loads(manifest.read_text())
    ai["models"] = {"first": "grok", "second": None}
    manifest.write_text(json.dumps(ai))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    store.set_meta("github_login", "worker-bot")
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    _patch_pr_gate_git(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.verify_signed_clean_head",
        lambda *args, **kwargs: _POSTED_HEAD,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.review_rules_bound",
        lambda *args, **kwargs: True,
    )
    tid = "aa000001-0000-4000-8000-000000000062"
    worktree = worker.workspace_root / tid
    worktree.mkdir(parents=True)
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "review",
        "state": "pr-review",
        "title": "A38 review retry",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "change_summary_en": "The unavailable launch is excluded from the pass count.",
        "change_summary_de": "Der nicht verfügbare Start zählt nicht als Review-Durchlauf.",
        "payload": {
            "coordinator": {
                "phase": "pr_gates_grok",
                "worktree": str(worktree),
                "base_sha": "b" * 40,
                "head_sha": _POSTED_HEAD,
                "pr_number": 42,
                "pr_url": "https://github.com/example/project/pull/42",
                "source": {"repo": "example/project", "number": 7},
                "a38": {"enabled": True},
            }
        },
    }
    store.write("task", "insert", tid, task)
    for key in CHECKLIST_KEYS["review"]:
        status = "ja" if key in {"session_registered", "contributing_read"} else "pending"
        store.write(
            "checklist_item",
            "insert",
            f"{tid}-{key}",
            {
                "id": f"{tid}-{key}",
                "task_id": tid,
                "key": key,
                "status": status,
                "evidence": "seed" if status == "ja" else None,
                "source": "script",
            },
        )

    fake.model_outputs["pr-reviewer-quality"] = "STATUS: unavailable\n"
    fake.model_outputs["pr-reviewer-logic"] = "STATUS: complete\nRESULT: approved\n"
    first_lines = phase_pr_gates(
        store,
        worker,
        store.row("task", tid),
        fake,
        lane_runner(fake),
        vendor="grok",
        stage="review-1",
    )
    blocked = store.row("task", tid)
    assert any("unavailable; blocked" in line for line in first_lines)
    assert "first_review_passes" not in blocked["payload"]["coordinator"]["a38"]

    question_id = blocked["payload"]["coordinator"]["question_activity_id"]
    question = store.row("activity", question_id)
    fake.comments = [
        {"id": 1, "body": question["payload"]["body"], "user": {"login": "worker-bot"}},
        {"id": 2, "body": "Provider restored; resume.", "user": {"login": "human-owner"}},
    ]
    resume_lines = phase_blocked(store, worker, blocked, fake)
    resumed = store.row("task", tid)
    assert any("resuming pr_gates_grok" in line for line in resume_lines)
    assert resumed["payload"]["coordinator"]["phase"] == "pr_gates_grok"

    fake.model_outputs["pr-reviewer-quality"] = "STATUS: complete\nRESULT: approved\n"

    def crash_after_gate_rows(*args: Any, **kwargs: Any) -> tuple[str, str] | None:
        approved_dimensions = {
            str(gate.get("dimension"))
            for gate in store.rows("review_gate")
            if gate.get("task_id") == tid
            and gate.get("stage") == "review-1"
            and gate.get("verdict") == "approved"
        }
        if approved_dimensions == {"quality", "logic"}:
            raise RuntimeError("simulated crash after gate rows")
        return real_reconcile_approved_gate_checklist(*args, **kwargs)

    monkeypatch.setattr(
        "agent_cli.coordinator_lanes._reconcile_approved_gate_checklist",
        crash_after_gate_rows,
    )
    with pytest.raises(RuntimeError, match="simulated crash after gate rows"):
        phase_pr_gates(
            store,
            worker,
            resumed,
            fake,
            lambda argv, stdin=None: Completed(
                0, "STATUS: complete\nRESULT: approved\n", ""
            ),
            vendor="grok",
            stage="review-1",
        )
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes._reconcile_approved_gate_checklist",
        real_reconcile_approved_gate_checklist,
    )

    approved = store.row("task", tid)
    assert approved["payload"]["coordinator"]["a38"]["first_review_passes"] == 1
    assert approved["payload"]["coordinator"]["phase"] == "pr_gates_grok"
    approved_gates = [
        gate
        for gate in store.rows("review_gate")
        if gate.get("task_id") == tid and gate.get("verdict") == "approved"
    ]
    assert {gate.get("dimension") for gate in approved_gates} == {"quality", "logic"}
    checklist_before_recovery = {
        key: store.row("checklist_item", f"{tid}-{key}")["status"]
        for key in ("review_1_quality", "review_1_logic")
    }
    # The pre-outage lane already closed logic; only quality remains unreconciled.
    assert checklist_before_recovery == {
        "review_1_quality": "unavailable",
        "review_1_logic": "ja",
    }

    recovery_lines = phase_pr_gates(
        store,
        worker,
        approved,
        fake,
        lane_runner(fake),
        vendor="grok",
        stage="review-1",
    )
    assert any("review-1 already complete" in line for line in recovery_lines)
    approved = store.row("task", tid)
    assert approved["payload"]["coordinator"]["a38"]["first_review_passes"] == 1
    checklist_after_recovery = {
        key: store.row("checklist_item", f"{tid}-{key}")["status"]
        for key in ("review_1_quality", "review_1_logic")
    }
    assert checklist_after_recovery == {
        "review_1_quality": "ja",
        "review_1_logic": "ja",
    }

    phase_first_comment(store, worker, approved, fake)
    comments = [
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and row.get("payload", {}).get("target") == "pr"
    ]
    assert len(comments) == 1
    body = comments[0]["payload"]["body"]
    assert "Ready after 1 review passes." in body
    assert "Bereit nach 1 Review-Durchläufen." in body


def test_a38_ci_and_readiness_route_to_guard_ready_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_github import phase_ci

    store, ordinary_worker, fake = _ready_side_env(tmp_path, monkeypatch)
    manifest = store.home / "ai-accounts.json"
    ai = json.loads(manifest.read_text())
    ai["models"] = {"first": "grok", "second": None}
    manifest.write_text(json.dumps(ai))
    worker = replace(
        ordinary_worker,
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    tid = "44444444-4444-4444-8444-444444444444"
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=tid,
        phase="ci",
        checklist_codex={"review_2_quality": "ja", "review_2_logic": "ja"},
        models={"first": "grok", "second": None},  # type: ignore[dict-item]
    )
    evidence = f"external second review by outside-reviewer at {fake.head}: https://x/comment"
    for row in store.rows("checklist_item"):
        if row.get("task_id") == tid and row.get("key") in {
            "review_2_quality",
            "review_2_logic",
        }:
            updated = {key: value for key, value in row.items() if not key.startswith("_")}
            updated["evidence"] = evidence
            updated["source"] = "script"
            store.write("checklist_item", "update", row["id"], updated)
    current = store.row("task", tid)
    current["payload"]["coordinator"]["a38"] = {
        "enabled": True,
        "base_sha": fake.base,
        "policy_revision": fake.base,
        "report": {
            "head": fake.head,
            "base_sha": fake.base,
            "policy_revision": fake.base,
        },
        "first_comment": {"head": fake.head},
        "second_review": {"head": fake.head, "status": "posted",
            "reviews": [{"reviewer": "outside-reviewer", "blocks_merge": False, "at": "2026-01-01T00:00:00Z"}],
        },
    }
    store.write(
        "task",
        "update",
        tid,
        {key: value for key, value in current.items() if not key.startswith("_")},
    )

    assert "CI green" in phase_ci(store, worker, store.row("task", tid), fake)[0]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "readiness"
    lines = phase_readiness(store, worker, store.row("task", tid), fake)
    assert "readiness ok" in lines[0]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "await_ready"


def _a38_second_review_payload(
    task_id: str, head: str, *, blocks_merge: bool = False
) -> dict[str, Any]:
    from agent_cli.a38_review import COMPLIANCE_PROMPT

    defects = 1 if blocks_merge else 0
    return {
        "kind": "a38.second-review/v1",
        "task_id": task_id,
        "head": head,
        "defects": defects,
        "blocks_merge": blocks_merge,
        "findings": "A blocking defect." if defects else "",
        "lanes": [
            {
                "id": lane_id,
                "provider": "OpenAI",
                "model": "Reviewer Model 2",
                "model_number": "reviewer-model-2",
                "prompt": COMPLIANCE_PROMPT,
                "status": "complete",
                "result": "pass",
            }
            for lane_id in ("conformity-b", "logic-b")
        ],
    }


def _a38_review_mail(
    store: Store,
    worker: Any,
    *,
    message_id: str,
    login: str,
    payload: dict[str, Any],
    second: int,
) -> None:
    store.apply_replica_row(
        {
            "table": "activity",
            "row_id": message_id,
            "origin_device_id": f"{message_id}-device",
            "github_login": login,
            "payload": {
                "id": message_id,
                "session_id": f"{login}-session",
                "type": "message",
                "payload": {"to_session": worker.session_id, "payload": payload},
                "execution_status": "pending",
            },
            "updated_at": f"2026-01-01T00:00:{second:02d}Z",
        }
    )


def _a38_external_review_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str,
) -> tuple[Store, Any, FakeGh, dict[str, Any]]:
    from dataclasses import replace

    store, ordinary_worker, fake = _ready_side_env(tmp_path, monkeypatch)
    manifest = store.home / "ai-accounts.json"
    ai = json.loads(manifest.read_text())
    ai["models"] = {"first": "grok", "second": None}
    manifest.write_text(json.dumps(ai))
    worker = replace(
        ordinary_worker,
        review_session=None,
        a38=True,
        second_reviewers=("reviewer-one", "reviewer-two"),
    )
    task = _seed_ready_side_task(
        store,
        worker,
        fake,
        tid=task_id,
        phase="await_second_review",
        checklist_codex={"review_2_quality": "pending", "review_2_logic": "pending"},
        models={"first": "grok", "second": None},
    )
    checkpoint = task["payload"]["coordinator"]
    checkpoint["a38"]["second_review"] = {
        "head": fake.head,
        "pinged": list(worker.second_reviewers),
        "status": "requested",
        "last_requested_at": "2026-01-01T00:00:00Z",
    }
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    patch_execute_github(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_a38.review_rules_bound", lambda *a: True)
    saved = store.row("task", task_id)
    assert saved is not None
    return store, worker, fake, saved


@pytest.mark.parametrize(
    ("blocking", "expected_comments", "expected_phase"),
    [
        ((True,), 1, "implement"),
        ((False, True), 2, "implement"),
        ((False, False), 2, "ci"),
    ],
)
def test_a38_intake_processes_all_reviews_for_one_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocking: tuple[bool, ...],
    expected_comments: int,
    expected_phase: str,
) -> None:
    from agent_cli import coordinator_a38

    task_id = "55555555-5555-4555-8555-555555555555"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    for index, blocks_merge in enumerate(blocking, start=1):
        message_id = f"review-mail-{index}"
        _a38_review_mail(
            store,
            worker,
            message_id=message_id,
            login=f"reviewer-{'one' if index == 1 else 'two'}",
            payload=_a38_second_review_payload(
                task_id, fake.head, blocks_merge=blocks_merge
            ),
            second=index,
        )
    if blocking == (False, False):
        store.apply_replica_row(
            {
                "table": "activity",
                "row_id": "foreign-read",
                "origin_device_id": "foreign-device",
                "payload": {
                    "id": "foreign-read",
                    "session_id": worker.session_id,
                    "type": "message.read",
                    "payload": {"message_id": "review-mail-1"},
                    "execution_status": "done",
                },
                "updated_at": "2026-01-01T00:00:03Z",
            }
        )
        store.write(
            "activity",
            "insert",
            "pending-local-read",
            {
                "id": "pending-local-read",
                "session_id": worker.session_id,
                "type": "message.read",
                "payload": {"message_id": "review-mail-2"},
                "execution_status": "pending",
            },
        )
    checklist_calls: list[str] = []
    real_set_checklist = coordinator_a38.set_checklist

    def counted_set_checklist(*args, **kwargs):  # noqa: ANN002, ANN003
        checklist_calls.append(str(args[2]))
        return real_set_checklist(*args, **kwargs)

    monkeypatch.setattr(coordinator_a38, "set_checklist", counted_set_checklist)

    lines = coordinator_a38.intake_second_reviews(store, worker, task, fake, fake.head)

    assert lines is not None
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == expected_comments
    saved = store.row("task", task_id)
    assert saved is not None
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == expected_phase
    passing = [flag for flag in blocking if not flag]
    if blocking[0]:
        assert checklist_calls == []
    else:
        # Every passing review records the evidence.
        assert checklist_calls == ["review_2_quality", "review_2_logic"] * len(passing)
    if expected_phase == "ci":
        second_review = checkpoint["a38"]["second_review"]
        assert second_review["reviewer"] == "reviewer-one"
        assert [review["reviewer"] for review in second_review["reviews"]] == [
            "reviewer-one",
            "reviewer-two",
        ]
        assert all(
            set(review) == {"reviewer", "activity_id", "id", "url", "blocks_merge", "at", "mail_id"}
            for review in second_review["reviews"]
        )
        assert all(review["blocks_merge"] is False for review in second_review["reviews"])
        local_reads = [
            row
            for row in store.rows("activity")
            if row.get("type") == "message.read"
            and row.get("execution_status") == "done"
            and row.get("_origin_device_id") == store.device_id()
        ]
        assert {
            row["payload"]["message_id"] for row in local_reads
        } >= {"review-mail-1", "review-mail-2"}
        assert coordinator_a38.intake_second_reviews(
            store, worker, saved, fake, fake.head
        ) is None
        assert len(
            [row for row in store.rows("activity") if row.get("type") == "comment.post"]
        ) == expected_comments
        stale = _a38_second_review_payload(task_id, "b" * 40)
        _a38_review_mail(
            store,
            worker,
            message_id="stale-later-mail",
            login="reviewer-two",
            payload=stale,
            second=4,
        )
        assert coordinator_a38.intake_second_reviews(
            store, worker, store.row("task", task_id), fake, fake.head
        ) is None
        stale_task = store.row("task", task_id)
        assert stale_task is not None
        observation = stale_task["payload"]["coordinator"]["observations"][-1]["text"]
        assert "stale head" in observation


def test_a38_new_head_second_review_updates_legacy_checklist_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews, require_ready_evidence

    task_id = "abababab-0000-4000-8000-000000000020"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    old_head = "b" * 40
    old_evidence = (
        f"external second review by reviewer-one at {old_head}: https://x/old"
    )
    legacy_keys = {
        "review_2_quality": "codex_pr_quality",
        "review_2_logic": "codex_pr_logic",
    }
    for row in store.rows("checklist_item"):
        legacy = legacy_keys.get(str(row.get("key")))
        if row.get("task_id") != task_id or legacy is None:
            continue
        updated = {key: value for key, value in row.items() if not key.startswith("_")}
        updated["key"] = legacy
        updated["status"] = "ja"
        updated["evidence"] = old_evidence
        updated["source"] = "script"
        store.write("checklist_item", "update", row["id"], updated)

    _a38_review_mail(
        store,
        worker,
        message_id="new-head-review",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )

    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    rows = {
        str(row.get("key")): row
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    new_evidence = f"external second review by reviewer-one at {fake.head}: https://x"
    for legacy in legacy_keys.values():
        assert rows[legacy]["status"] == "ja"
        assert rows[legacy]["evidence"] == new_evidence

    saved = store.row("task", task_id)
    checkpoint = saved["payload"]["coordinator"]
    # This test exercises legacy checklist keys, not legacy report identity.
    # Supply current base-bound report evidence so the readiness check reaches
    # the checklist compatibility assertion below.
    checkpoint["base_sha"] = fake.base
    checkpoint["a38"]["base_sha"] = fake.base
    checkpoint["a38"]["policy_revision"] = fake.base
    checkpoint["a38"]["report"] = {
        "head": fake.head,
        "base_sha": fake.base,
        "policy_revision": fake.base,
    }
    checkpoint["a38"]["first_comment"] = {"head": fake.head}
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in saved.items() if not key.startswith("_")},
    )

    require_ready_evidence(store, store.row("task", task_id), fake.head)


def test_a38_legacy_unbound_report_routes_readiness_back_to_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import require_ready_evidence

    task_id = "abababab-0000-4000-8000-000000000021"
    store, _worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    checkpoint = task["payload"]["coordinator"]
    checkpoint["phase"] = "readiness"
    checkpoint["resume_phase"] = "readiness"
    checkpoint["question_activity_id"] = "stale-readiness-question"
    checkpoint["base_sha"] = fake.base
    checkpoint["evidence"] = {
        "tests_pass": True,
        "tests_head": fake.head,
    }
    checkpoint["a38"]["report"] = {"head": fake.head}
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    assert require_ready_evidence(
        store, store.row("task", task_id), fake.head
    ) is False
    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["phase"] == "tests"
    assert saved.get("resume_phase") in (None, "")
    assert saved.get("question_activity_id") in (None, "")
    assert "report" not in saved["a38"]
    assert "tests_pass" not in saved["evidence"]
    assert "tests_head" not in saved["evidence"]


def test_a38_later_phase_intake_routes_a_later_blocking_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_runtime import advance_one

    task_id = "66666666-6666-4666-8666-666666666666"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="first-review-mail",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )
    intake_second_reviews(store, worker, task, fake, fake.head)
    assert store.row("task", task_id)["payload"]["coordinator"]["phase"] == "ci"
    _a38_review_mail(
        store,
        worker,
        message_id="later-blocking-mail",
        login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=2,
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert any("routing to implementer" in line for line in lines)
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["state"] == "implementing"
    assert saved["payload"]["coordinator"]["phase"] == "implement"
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 2


@pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
def test_a38_await_merge_skips_second_review_intake_once_the_pr_is_not_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_runtime import advance_one

    task_id = "77777777-7777-4777-8777-777777777777"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="first-review-mail",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )
    intake_second_reviews(store, worker, task, fake, fake.head)
    saved = store.row("task", task_id)
    assert saved is not None
    saved["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in saved.items() if not key.startswith("_")},
    )
    fake.pr["state"] = pr_state
    _a38_review_mail(
        store,
        worker,
        message_id="late-blocking-mail",
        login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=2,
    )
    monkeypatch.setattr(
        coordinator_runtime, "phase_await_merge", lambda *args, **kwargs: ["merge observed"]
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    after = store.row("task", task_id)
    assert after is not None
    assert after["payload"]["coordinator"]["phase"] == "await_merge"
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    if pr_state == "MERGED":
        # The merged head's review is still taken in, but never reopens work.
        assert any("left the open state" in line for line in lines)
        assert len(comments) == 2
    else:
        assert lines == ["merge observed"]
        assert len(comments) == 1


@pytest.mark.parametrize("phase", ["await_second_review", "ci", "readiness", "await_ready"])
@pytest.mark.parametrize(
    ("pr_state", "expected_phase"),
    [("OPEN", "blocked"), ("MERGED", "await_merge"), ("CLOSED", "await_merge")],
)
def test_a38_revoked_assignment_in_await_ready_blocks_unless_the_pr_left_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    pr_state: str,
    expected_phase: str,
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000001"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = phase
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    fake.issues[0]["assignees"] = []
    fake.pr["state"] = pr_state
    for name in ("phase_a38_await_ready", "phase_await_second_review", "phase_ci", "phase_readiness"):
        monkeypatch.setattr(coordinator_runtime, name, lambda *args, **kwargs: ["ready handler"])

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert "ready handler" not in lines
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == expected_phase
    if expected_phase == "await_merge":
        verb = "merged" if pr_state == "MERGED" else "closed"
        assert lines == [f"PR {verb} during {phase}; verifying the merge"]


@pytest.mark.parametrize("phase", ["await_second_review", "ci", "readiness", "await_ready"])
def test_a38_unassigned_source_with_unreadable_pr_state_retries_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-00000000000b"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = phase
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    fake.issues[0]["assignees"] = []
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *args, **kwargs: None)

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert lines == [f"PR state temporarily unavailable during {phase}; retrying"]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == phase
    assert "blocker" not in saved["payload"]["coordinator"]


@pytest.mark.parametrize("unreadable", ["head", "state"])
def test_a38_unreadable_pr_before_second_review_intake_retries_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-00000000000e"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    if unreadable == "head":
        monkeypatch.setattr(coordinator_runtime, "fresh_pr_head", lambda *a, **k: None)
    else:
        monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: None)
    monkeypatch.setattr(
        coordinator_runtime, "phase_await_merge", lambda *args, **kwargs: ["merge handler"]
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert lines == [
        "PR state temporarily unavailable before second-review intake in await_merge; retrying"
    ]
    assert store.row("task", task_id)["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_await_merge_posts_no_second_review_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000002"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="first-review-mail",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )
    intake_second_reviews(store, worker, task, fake, fake.head)
    saved = store.row("task", task_id)
    assert saved is not None
    saved["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in saved.items() if not key.startswith("_")},
    )
    fake.pr["state"] = "OPEN"
    fake.issues[0]["assignees"] = []
    _a38_review_mail(
        store,
        worker,
        message_id="late-blocking-mail",
        login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=2,
    )
    monkeypatch.setattr(
        coordinator_runtime, "phase_await_merge", lambda *args, **kwargs: ["merge observed"]
    )

    assert advance_one(store, worker, runner=fake, lane_runner=None) == ["merge observed"]
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 1


def test_a38_later_phase_ignores_mail_for_a_head_replaced_by_a_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000003"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="first-review-mail",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )
    intake_second_reviews(store, worker, task, fake, fake.head)
    _a38_review_mail(
        store,
        worker,
        message_id="old-head-blocking-mail",
        login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=2,
    )
    fake.pr["headRefOid"] = "c" * 40
    monkeypatch.setattr(coordinator_runtime, "phase_ci", lambda *args, **kwargs: ["ci handler"])

    assert advance_one(store, worker, runner=fake, lane_runner=None) == ["ci handler"]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "ci"
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(comments) == 1


def test_a38_await_second_review_stops_on_a_closed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import phase_await_second_review

    task_id = "abababab-0000-4000-8000-000000000004"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    fake.pr["state"] = "CLOSED"
    _a38_review_mail(
        store,
        worker,
        message_id="mail-after-close",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )

    with pytest.raises(CoordinatorError, match="not open while awaiting second review"):
        phase_await_second_review(store, worker, task, fake)
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert comments == []


@pytest.mark.parametrize(("pr_state", "verb"), [("MERGED", "merged"), ("CLOSED", "closed")])
@pytest.mark.parametrize("phase", ["await_second_review", "ci", "readiness"])
def test_a38_human_merge_during_a_wait_phase_goes_to_the_merge_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, pr_state: str, verb: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000005"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = phase
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    fake.pr["state"] = pr_state
    for name in ("phase_await_second_review", "phase_ci", "phase_readiness"):
        monkeypatch.setattr(coordinator_runtime, name, lambda *args, **kwargs: ["handler ran"])

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert lines == [f"PR {verb} during {phase}; verifying the merge"]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_await_second_review_hands_a_merged_pr_to_the_merge_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import phase_await_second_review

    task_id = "abababab-0000-4000-8000-000000000006"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    fake.pr["state"] = "MERGED"

    assert phase_await_second_review(store, worker, task, fake) == [
        "PR merged while awaiting second review; verifying the merge"
    ]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_blocking_review_after_a_merge_does_not_reopen_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000007"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="blocking-after-merge",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    fake.pr["state"] = "MERGED"

    lines = intake_second_reviews(store, worker, task, fake, fake.head)

    assert lines is not None and "left the open state" in lines[-1]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"
    assert saved["state"] != "implementing"


def test_a38_pass_posted_after_a_tied_block_decides_like_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-00000000000d"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="a-blocking",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=9,
    )
    _a38_review_mail(
        store,
        worker,
        message_id="b-passing",
        login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=9,
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")

    lines = intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    saved = store.row("task", task_id)
    assert saved["payload"]["coordinator"]["phase"] == "ci"
    assert lines is not None and "posted" in lines[-1]
    assert _checklist_status(store, task_id)["review_2_quality"] == "ja"


def test_a38_older_review_delivered_after_a_newer_post_is_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000010"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store, worker, message_id="newer-pass", login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head), second=20,
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")
    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)
    posted = len([row for row in store.rows("activity") if row.get("type") == "comment.post"])
    _a38_review_mail(
        store, worker, message_id="older-block", login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True), second=5,
    )

    lines = intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    c = store.row("task", task_id)["payload"]["coordinator"]
    assert len([row for row in store.rows("activity") if row.get("type") == "comment.post"]) == posted
    assert c["phase"] == "ci"
    assert any("superseded" in o["text"] for o in c["observations"])
    assert lines is None or not any("routing to implementer" in line for line in lines)


def test_a38_same_time_review_arriving_later_is_superseded_by_mail_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000011"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")
    _a38_review_mail(
        store, worker, message_id="b-pass", login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head), second=7,
    )
    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)
    _a38_review_mail(
        store, worker, message_id="a-block", login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True), second=7,
    )

    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    c = store.row("task", task_id)["payload"]["coordinator"]
    assert [r["mail_id"] for r in c["a38"]["second_review"]["reviews"]] == ["b-pass"]
    assert c["phase"] == "ci"


@pytest.mark.parametrize(("pr_state", "phase"), [("MERGED", "await_merge"), ("OPEN", "implement")])
def test_a38_implement_after_a_block_checks_for_a_merge_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str, phase: str
) -> None:  # the later rerun phases are covered below
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000012"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    c = task["payload"]["coordinator"]
    c["phase"] = "implement"
    c["a38"]["second_review"] = {
        "head": fake.head, "status": "posted", "reviews": [{"blocks_merge": True, "at": "t"}]
    }
    store.write(
        "task", "update", task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: pr_state)
    monkeypatch.setattr(
        coordinator_runtime, "phase_implement", lambda *a, **k: ["implementer started"]
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert store.row("task", task_id)["payload"]["coordinator"]["phase"] == phase
    assert ("implementer started" in lines) is (pr_state == "OPEN")


@pytest.mark.parametrize(
    "phase", ["inner_review", "tests", "publish_draft", "pr_gates_grok", "a38_first_comment"]
)
@pytest.mark.parametrize(("pr_state", "expected"), [("MERGED", "await_merge"), (None, None)])
def test_a38_published_rerun_phases_check_for_a_merge_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, pr_state, expected
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000013"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = phase
    store.write(
        "task", "update", task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: pr_state)

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    saved_phase = store.row("task", task_id)["payload"]["coordinator"]["phase"]
    if expected is None:
        assert lines == [f"PR state temporarily unavailable before {phase}; retrying"]
        assert saved_phase == phase
    else:
        assert saved_phase == expected


@pytest.mark.parametrize("phase", ["blocked", "ask", "await_second_review"])
def test_a38_merge_is_checked_first_in_any_phase_with_a_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000014"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = phase
    if phase == "blocked":
        task["payload"]["coordinator"]["resume_phase"] = "ci"
    store.write(
        "task", "update", task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: "MERGED")

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert lines == [f"PR merged during {phase}; verifying the merge"]
    assert store.row("task", task_id)["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_uncertain_lane_with_stale_resume_is_final_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000019"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    checkpoint = task["payload"]["coordinator"]
    checkpoint.update(
        phase="blocked",
        blocker="uncertain prior lane outcome",
        uncertain_lane=True,
        resume_phase="implement",
    )
    store.write(
        "task", "update", task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    states: list[str] = []
    monkeypatch.setattr(
        coordinator_runtime,
        "pr_state",
        lambda *args, **kwargs: states.append("read") or "MERGED",
    )

    advance_one(store, worker, runner=fake, lane_runner=None)

    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["phase"] == "blocked"
    assert saved["uncertain_lane"] is True
    assert states == []


@pytest.mark.parametrize(
    "kind", ["merged-with-blocking-second-review", "merged-without-second-review", "closed-unmerged"]
)
def test_a38_final_merge_blocker_stays_blocked_on_later_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-000000000015"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    c = task["payload"]["coordinator"]
    c.update(phase="blocked", blocker=kind)
    c.pop("resume_phase", None)
    store.write(
        "task", "update", task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    states: list[str] = []
    monkeypatch.setattr(
        coordinator_runtime, "pr_state", lambda *a, **k: states.append("read") or "MERGED"
    )
    _a38_review_mail(
        store, worker, message_id="late-pass", login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head), second=50,
    )

    for _ in range(2):
        advance_one(store, worker, runner=fake, lane_runner=None)

    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert saved["phase"] == "blocked"
    assert not [
        row for row in store.rows("activity")
        if row.get("type") == "comment.post" and row["payload"].get("target") == "pr"
    ]
    assert states == []


def test_check_label_hides_owner_commands() -> None:
    from agent_cli.coordinator_common import check_label
    from agent_cli.coordinator_config import RepositoryConfig

    cfg = RepositoryConfig("Example/repo", "main", "Example/repo", ("/opt/secret/check", "--token=x"), ())
    assert check_label({"source": {"owner_scoped": True}}, cfg) == "owner_check_argv"
    assert check_label({"source": {}}, cfg) == "/opt/secret/check --token=x"


def test_a38_crash_after_a_post_keeps_an_older_late_review_unposted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000016"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")
    _a38_review_mail(
        store, worker, message_id="newer-pass", login="reviewer-two",
        payload=_a38_second_review_payload(task_id, fake.head), second=20,
    )
    real_post = coordinator_a38._post_comment

    def post_then_crash(*args, **kwargs):
        real_post(*args, **kwargs)
        raise RuntimeError("crash after the post")

    monkeypatch.setattr(coordinator_a38, "_post_comment", post_then_crash)
    with pytest.raises(RuntimeError):
        intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)
    monkeypatch.setattr(coordinator_a38, "_post_comment", real_post)
    _a38_review_mail(
        store, worker, message_id="older-block", login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True), second=5,
    )

    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    state = store.row("task", task_id)["payload"]["coordinator"]["a38"]["second_review"]
    assert [review["mail_id"] for review in state["reviews"]] == ["newer-pass"]
    assert "pending_post" not in state
    posts = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert len(posts) == 1


def test_a38_unchanged_head_after_a_block_asks_the_reviewers_again() -> None:
    from agent_cli.coordinator_a38 import head_review_blocks

    head = "a" * 40
    blocked = {"a38": {"second_review": {"head": head, "reviews": [{"blocks_merge": True}]}}}
    passed = {"a38": {"second_review": {"head": head, "reviews": [{"blocks_merge": False}]}}}
    other_head = {"a38": {"second_review": {"head": "b" * 40, "reviews": [{"blocks_merge": True}]}}}

    assert head_review_blocks(blocked, head) is True
    assert head_review_blocks(passed, head) is False
    assert head_review_blocks(other_head, head) is False
    assert head_review_blocks({"a38": {}}, head) is False


def test_a38_routing_a_block_keeps_the_review_history_of_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_github import _a38_second_review_outcome

    task_id = "abababab-0000-4000-8000-00000000000f"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="blocking-kept",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=3,
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")

    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    c = store.row("task", task_id)["payload"]["coordinator"]
    assert c["phase"] == "implement"
    assert _a38_second_review_outcome(c, fake.head) == "blocking"


def test_a38_late_older_blocking_review_keeps_the_newer_block_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-00000000000c"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    newer = _a38_second_review_payload(task_id, fake.head, blocks_merge=True)
    newer["findings"] = "Newer blocking finding."
    _a38_review_mail(
        store, worker, message_id="newer-block", login="reviewer-one", payload=newer, second=20
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *a, **k: "OPEN")
    monkeypatch.setattr(coordinator_a38, "_route_newest_blocking_review", lambda *a, **k: None)
    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)
    older = _a38_second_review_payload(task_id, fake.head, blocks_merge=True)
    older["findings"] = "Older blocking finding."
    _a38_review_mail(
        store, worker, message_id="older-block", login="reviewer-two", payload=older, second=5
    )

    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    block = store.row("task", task_id)["payload"]["coordinator"]["second_review_block"]
    assert "Newer blocking finding." in block["findings"]
    assert block["reviewer"] == "reviewer-one"


def test_a38_blocking_review_in_await_merge_on_an_open_pr_reopens_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_runtime import advance_one

    task_id = "abababab-0000-4000-8000-00000000000a"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    task["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    _a38_review_mail(
        store,
        worker,
        message_id="blocking-on-a-ready-pr",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    fake.pr["state"] = "OPEN"
    monkeypatch.setattr(
        coordinator_runtime, "phase_await_merge", lambda *args, **kwargs: ["merge handler"]
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert "merge handler" not in lines
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "implement"
    assert saved["state"] == "implementing"


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        (None, "missing"),
        ([{"blocks_merge": False}], "passed"),
        ([{"blocks_merge": False}, {"blocks_merge": True}], "blocking"),
        ([{"blocks_merge": True}, {"blocks_merge": False}], "passed"),
    ],
)
def test_a38_second_review_outcome_for_a_merged_head(reviews, expected) -> None:
    from agent_cli.coordinator_github import _a38_second_review_outcome

    head = "a" * 40
    c: dict = {"a38": {}}
    if reviews is not None:
        c["a38"]["second_review"] = {"head": head, "status": "posted", "reviews": reviews}
    assert _a38_second_review_outcome(c, head) == expected
    assert _a38_second_review_outcome(c, "b" * 40) == "missing"


def test_a38_merge_without_second_review_waits_then_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_github

    task_id = "abababab-0000-4000-8000-000000000008"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    worker = replace(worker, second_review_timeout_seconds=600)
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_github,
        "publish_blocker",
        lambda _store, _worker, _task, _runner, message, *, kind="blocker", **_kw: (
            published.append((kind, message)) or [f"blocker {kind}"]
        ),
    )
    clock = iter(["2026-11-05T10:00:00Z", "2026-11-05T10:05:00Z", "2026-11-05T10:11:00Z"])
    monkeypatch.setattr(coordinator_github, "utcnow", lambda: next(clock))

    first = coordinator_github._await_second_review_after_merge(
        store, worker, task, fake, fake.head, blocking=False
    )
    second = coordinator_github._await_second_review_after_merge(
        store, worker, task, fake, fake.head, blocking=False
    )
    third = coordinator_github._await_second_review_after_merge(
        store, worker, task, fake, fake.head, blocking=False
    )

    assert "awaiting the external second review" in first[0]
    assert "awaiting the external second review" in second[0]
    assert third == ["blocker merged-without-second-review"]
    assert task["payload"]["coordinator"]["phase"] == "blocked"


def test_a38_merge_with_blocking_second_review_blocks_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_github

    task_id = "abababab-0000-4000-8000-000000000009"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    monkeypatch.setattr(
        coordinator_github,
        "publish_blocker",
        lambda _store, _worker, _task, _runner, message, *, kind="blocker", **_kw: [f"blocker {kind}"],
    )

    assert coordinator_github._await_second_review_after_merge(
        store, worker, task, fake, fake.head, blocking=True
    ) == ["blocker merged-with-blocking-second-review"]
    assert task["payload"]["coordinator"]["phase"] == "blocked"


def test_a38_blocking_review_with_unknown_pr_state_retries_without_rerouting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import _read_message_ids, intake_second_reviews

    task_id = "abababab-0000-4000-8000-00000000000a"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="blocking-unknown-state",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *_args: None)

    lines = intake_second_reviews(store, worker, task, fake, fake.head)

    assert lines is not None and "retrying the blocking second review" in lines[-1]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] != "implement"
    assert "blocking-unknown-state" not in _read_message_ids(store, worker)


def test_a38_await_second_review_with_unknown_pr_state_takes_no_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_await_second_review

    task_id = "abababab-0000-4000-8000-00000000000b"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="mail-unknown-state",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=1,
    )
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *_args: None)

    assert phase_await_second_review(store, worker, task, fake) == [
        "PR state temporarily unavailable; retrying"
    ]
    assert [row for row in store.rows("activity") if row.get("type") == "comment.post"] == []


@pytest.mark.parametrize("variant", ["no_login", "unauthorized", "malformed", "stale"])
def test_a38_rejected_review_mail_stays_unread_when_the_observation_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import _read_message_ids, intake_second_reviews

    task_id = "abababab-0000-4000-8000-00000000000c"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    payload = _a38_second_review_payload(task_id, fake.head)
    login = "reviewer-one"
    if variant == "no_login":
        login = ""
    elif variant == "unauthorized":
        login = "someone-else"
    elif variant == "malformed":
        payload = {**payload, "defects": -1}
    else:
        payload = _a38_second_review_payload(task_id, "b" * 40)
    _a38_review_mail(
        store, worker, message_id="rejected-mail", login=login, payload=payload, second=1
    )

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("save failed")

    monkeypatch.setattr(coordinator_a38, "save_task", fail_save)
    with pytest.raises(RuntimeError, match="save failed"):
        intake_second_reviews(store, worker, task, fake, fake.head)
    assert "rejected-mail" not in _read_message_ids(store, worker)


def test_a38_public_prompt_hides_local_paths(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import public_prompt
    from agent_cli.coordinator_common import control_dir

    worker = replace(make_worker(tmp_path), a38=True)
    task = {
        "id": "abababab-0000-4000-8000-00000000000d",
        "payload": {"coordinator": {"worktree": str(worker.workspace_root / "task-tree")}},
    }
    ctrl = control_dir(worker, task["id"])
    prompt = (
        f"Diff artifact: {ctrl}/pr-diff.patch\n"
        f"Worktree: {worker.workspace_root / 'task-tree'}\n"
        f"Other: {worker.workspace_root}/cache and {Path.home()}/notes\n"
    )

    published = public_prompt(prompt, worker, task)

    assert "<control-dir>/pr-diff.patch" in published
    assert "<worktree>" in published
    assert "<workspace>/cache" in published
    assert "~/notes" in published
    assert str(worker.workspace_root) not in published
    assert str(Path.home()) not in published


def test_a38_public_prompt_replaces_resolved_symlinked_paths_whole(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import public_prompt

    real = tmp_path / "real-workspace"
    real.mkdir()
    link = tmp_path / "ws"
    link.symlink_to(real)
    worker = replace(make_worker(tmp_path), a38=True, workspace_root=link)
    task = {"id": "abababab-0000-4000-8000-00000000000e", "payload": {"coordinator": {}}}

    published = public_prompt(f"Cache: {real.resolve()}/cache and {link}/other", worker, task)

    assert published == "Cache: <workspace>/cache and <workspace>/other"


def test_a38_passing_review_after_a_blocking_one_on_a_merged_head_closes_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews
    from agent_cli.coordinator_github import _a38_second_review_outcome

    task_id = "abababab-0000-4000-8000-00000000000f"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    fake.pr["state"] = "MERGED"
    _a38_review_mail(
        store,
        worker,
        message_id="first-blocking",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    intake_second_reviews(store, worker, task, fake, fake.head)
    _a38_review_mail(
        store,
        worker,
        message_id="later-pass",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=2,
    )
    intake_second_reviews(store, worker, store.row("task", task_id), fake, fake.head)

    saved = store.row("task", task_id)
    assert saved is not None
    assert _a38_second_review_outcome(saved["payload"]["coordinator"], fake.head) == "passed"
    statuses = {
        row["key"]: row["status"]
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    assert statuses["review_2_quality"] == "ja"
    assert statuses["review_2_logic"] == "ja"


def test_a38_after_a_merge_every_waiting_review_is_taken_in_and_the_newest_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import _read_message_ids, intake_second_reviews
    from agent_cli.coordinator_github import _a38_second_review_outcome

    task_id = "abababab-0000-4000-8000-000000000010"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    fake.pr["state"] = "MERGED"
    _a38_review_mail(
        store,
        worker,
        message_id="merged-block",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    _a38_review_mail(
        store,
        worker,
        message_id="merged-pass",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=2,
    )

    intake_second_reviews(store, worker, task, fake, fake.head)

    read = _read_message_ids(store, worker)
    assert {"merged-block", "merged-pass"} <= read
    saved = store.row("task", task_id)
    assert saved is not None
    assert _a38_second_review_outcome(saved["payload"]["coordinator"], fake.head) == "passed"


def test_a38_open_pr_newer_pass_supersedes_an_older_block_in_the_same_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000011"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    _a38_review_mail(
        store,
        worker,
        message_id="older-block",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
        second=1,
    )
    _a38_review_mail(
        store,
        worker,
        message_id="newer-pass",
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=2,
    )

    intake_second_reviews(store, worker, task, fake, fake.head)

    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "ci"
    assert saved["state"] != "implementing"


def test_a38_tests_refuses_a_foreign_row_on_the_report_activity_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38

    task_id = "abababab-0000-4000-8000-000000000012"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    monkeypatch.setattr(coordinator_a38, "verify_signed_clean_head", lambda *_a: fake.head)
    monkeypatch.setattr(
        coordinator_a38,
        "_pull_a38_context",
        lambda *_a, **_k: (fake.base, ["src/app.py"]),
    )
    activity_id = coordinator_a38._report_activity_id(
        task_id,
        head=fake.head,
        base_sha=fake.base,
        policy_revision=fake.base,
    )
    store.apply_replica_row(
        {
            "table": "activity",
            "row_id": activity_id,
            "origin_device_id": "foreign-device",
            "payload": {
                "id": activity_id,
                "session_id": worker.session_id,
                "type": "comment.post",
                "payload": {"repo": "example/project", "number": 42, "body": "x", "exact_body": True},
                "execution_status": "done",
                "result": {"url": "https://github.com/example/project/pull/42#issuecomment-1"},
            },
            "updated_at": "2026-01-01T00:00:03Z",
        }
    )

    with pytest.raises(CoordinatorError, match="did not write"):
        coordinator_a38.phase_tests(store, worker, task, fake)


def test_a38_first_review_comment_refuses_a_foreign_deterministic_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import NAMESPACE_URL, uuid5

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_first_comment

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    captured: dict[str, str] = {}

    def capture_body(*_args, body: str, **_kwargs):
        captured["body"] = body
        raise RuntimeError("captured first review body")

    with monkeypatch.context() as capture_patch:
        capture_patch.setattr(coordinator_a38, "_post_comment", capture_body)
        with pytest.raises(RuntimeError, match="captured first review body"):
            phase_first_comment(store, worker, store.row("task", task_id), fake)

    activity_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task_id}:{fake.head}")
    )
    store.apply_replica_row(
        {
            "table": "activity",
            "row_id": activity_id,
            "origin_device_id": "foreign-device",
            "payload": {
                "id": activity_id,
                "session_id": worker.session_id,
                "type": "comment.post",
                "payload": {
                    "repo": "example/project",
                    "number": 42,
                    "body": captured["body"],
                    "target": "pr",
                },
                "execution_status": "done",
                "result": {"url": "https://x/foreign-first"},
            },
            "updated_at": "2026-01-01T00:00:03Z",
        }
    )

    with pytest.raises(CoordinatorError, match="did not write"):
        phase_first_comment(store, worker, store.row("task", task_id), fake)

    saved = store.row("task", task_id)
    assert saved["payload"]["coordinator"]["phase"] == "a38_first_comment"
    assert store.rows("ping") == []
    assert store.row("activity", activity_id)["_origin_device_id"] == "foreign-device"


def test_a38_second_review_comment_refuses_a_foreign_deterministic_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import NAMESPACE_URL, uuid5

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import _read_message_ids, intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000015"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    message_id = "foreign-collision-mail"
    _a38_review_mail(
        store,
        worker,
        message_id=message_id,
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=4,
    )
    captured: dict[str, str] = {}

    def capture_body(*_args, body: str, **_kwargs):
        captured["body"] = body
        raise RuntimeError("captured second review body")

    with monkeypatch.context() as capture_patch:
        capture_patch.setattr(coordinator_a38, "_post_comment", capture_body)
        with pytest.raises(RuntimeError, match="captured second review body"):
            intake_second_reviews(store, worker, task, fake, fake.head)

    activity_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-a38-second:{task_id}:{fake.head}:{message_id}",
        )
    )
    store.apply_replica_row(
        {
            "table": "activity",
            "row_id": activity_id,
            "origin_device_id": "foreign-device",
            "payload": {
                "id": activity_id,
                "session_id": worker.session_id,
                "type": "comment.post",
                "payload": {
                    "repo": "example/project",
                    "number": 42,
                    "body": captured["body"],
                    "target": "pr",
                },
                "execution_status": "done",
                "result": {"url": "https://x/foreign-second"},
            },
            "updated_at": "2026-01-01T00:00:05Z",
        }
    )

    with pytest.raises(CoordinatorError, match="did not write"):
        intake_second_reviews(
            store, worker, store.row("task", task_id), fake, fake.head
        )

    saved = store.row("task", task_id)
    state = saved["payload"]["coordinator"]["a38"]["second_review"]
    assert saved["payload"]["coordinator"]["phase"] == "await_second_review"
    assert state.get("status") != "posted"
    assert state.get("reviews") in (None, [])
    assert message_id not in _read_message_ids(store, worker)


def test_a38_second_review_comment_reuses_its_own_row_after_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import _read_message_ids, intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000016"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    message_id = "own-row-retry-mail"
    _a38_review_mail(
        store,
        worker,
        message_id=message_id,
        login="reviewer-one",
        payload=_a38_second_review_payload(task_id, fake.head),
        second=6,
    )

    def crash_after_comment(*_args, **_kwargs):
        raise RuntimeError("crash after second review comment")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            "agent_cli.coordinator_a38.set_checklist",
            crash_after_comment,
        )
        with pytest.raises(RuntimeError, match="crash after second review comment"):
            intake_second_reviews(store, worker, task, fake, fake.head)

    comments = [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    assert comments[0]["_origin_device_id"] == store.device_id()
    assert message_id not in _read_message_ids(store, worker)

    lines = intake_second_reviews(
        store, worker, store.row("task", task_id), fake, fake.head
    )

    assert lines == [f"external second review by @reviewer-one posted on {fake.head[:7]}"]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1
    assert message_id in _read_message_ids(store, worker)


def test_a38_block_on_open_pr_then_block_after_merge_does_not_reopen_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000013"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    for index, message_id in enumerate(("block-open", "block-merged"), start=1):
        _a38_review_mail(
            store,
            worker,
            message_id=message_id,
            login="reviewer-one",
            payload=_a38_second_review_payload(task_id, fake.head, blocks_merge=True),
            second=index,
        )
    states = iter(["OPEN", "MERGED", "MERGED", "MERGED"])
    monkeypatch.setattr(coordinator_a38, "pr_state", lambda *_args: next(states))

    intake_second_reviews(store, worker, task, fake, fake.head)

    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"
    assert saved["state"] != "implementing"


def test_a38_saved_newest_block_routes_on_a_later_tick_after_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import intake_second_reviews

    task_id = "abababab-0000-4000-8000-000000000014"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    c = task["payload"]["coordinator"]
    c.setdefault("a38", {})["second_review"] = {
        "head": fake.head,
        "status": "posted",
        "reviews": [{"reviewer": "reviewer-one", "activity_id": "a", "id": 1, "url": "u", "blocks_merge": True}],
    }
    c["second_review_block"] = {"head": fake.head, "findings": "Fix it.", "reviewer": "reviewer-one"}
    fake.pr["state"] = "OPEN"

    lines = intake_second_reviews(store, worker, task, fake, fake.head)

    assert lines is not None and "routing to implementer" in lines[-1]
    saved = store.row("task", task_id)
    assert saved is not None
    assert saved["payload"]["coordinator"]["phase"] == "implement"
    assert saved["payload"]["coordinator"]["findings"] == "Fix it."


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ([{"blocks_merge": True, "at": "t1"}, {"blocks_merge": False, "at": "t2"}], False),
        ([{"blocks_merge": False, "at": "t1"}, {"blocks_merge": True, "at": "t2"}], True),
        # Same second: the last posted review decides, as the guard reads the comments.
        ([{"blocks_merge": True, "at": "t2"}, {"blocks_merge": False, "at": "t2"}], False),
        ([{"blocks_merge": False, "at": "t2"}, {"blocks_merge": True, "at": "t2"}], True),
        ([{"blocks_merge": False}], False),
        ([], False),
    ],
)
def test_a38_newest_reviews_block_follows_the_posting_order(reviews, expected) -> None:
    from agent_cli.coordinator_a38 import newest_reviews_block

    assert newest_reviews_block(reviews) is expected


def test_a38_read_receipt_survives_a_foreign_row_on_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import NAMESPACE_URL, uuid5

    from agent_cli import coordinator_a38

    store, worker, _fake, _task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id="88888888-8888-4888-8888-888888888888"
    )
    ack_id = str(
        uuid5(
            NAMESPACE_URL,
            "coordinator-a38-message-read:"
            f"{store.device_id()}:{worker.session_id}:occupied-mail",
        )
    )
    store.apply_replica_row(
        {
            "table": "activity",
            "row_id": ack_id,
            "origin_device_id": "foreign-device",
            "payload": {
                "id": ack_id,
                "session_id": worker.session_id,
                "type": "message.read",
                "payload": {"message_id": "other-mail"},
                "execution_status": "done",
            },
            "updated_at": "2026-01-01T00:00:03Z",
        }
    )

    coordinator_a38._mark_message_read(store, worker, "occupied-mail")
    coordinator_a38._mark_message_read(store, worker, "occupied-mail")

    assert "occupied-mail" in coordinator_a38._read_message_ids(store, worker)
    assert "other-mail" not in coordinator_a38._read_message_ids(store, worker)
    receipts = [
        row
        for row in store.rows("activity")
        if row.get("type") == "message.read"
        and row.get("payload", {}).get("message_id") == "occupied-mail"
    ]
    assert len(receipts) == 1


def _a38_first_comment_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    second_reviewers: tuple[str, ...] = ("outside-reviewer",),
) -> tuple[Store, Any, FakeGh, str]:
    from dataclasses import replace

    from agent_cli.a38_review import COMPLIANCE_PROMPT
    from agent_cli.coordinator_runtime import execution_binding

    store = Store(tmp_path)
    write_accounts(store.home)
    manifest = store.home / "ai-accounts.json"
    ai = json.loads(manifest.read_text())
    ai["models"] = {"first": "grok", "second": None}
    manifest.write_text(json.dumps(ai))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    store.set_meta("github_login", "worker-bot")
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=second_reviewers,
    )
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: fake.head)
    monkeypatch.setattr("agent_cli.coordinator_a38.review_rules_bound", lambda *a: True)
    tid = "11111111-1111-4111-8111-111111111111"
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "pr-review",
        "title": "A38 task",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "change_summary_en": "The coordinator supports an external second review.",
        "change_summary_de": "Der Koordinator unterstützt einen externen zweiten Review.",
        "payload": {
            "coordinator": {
                "phase": "a38_first_comment",
                "head_sha": fake.head,
                "base_sha": fake.base,
                "worktree": str(worker.workspace_root / tid),
                "pr_number": 42,
                "pr_url": "https://github.com/example/project/pull/42",
                "source": {
                    "repo": "example/project",
                    "number": 7,
                    "assigned_id": "assigned-a38",
                    "base": "develop",
                },
                "execution_binding": execution_binding(
                    store, worker, "example/project"
                ),
                "a38": {
                    "enabled": True,
                    "base_sha": fake.base,
                    "policy_revision": fake.base,
                    "report": {
                        "head": fake.head,
                        "base_sha": fake.base,
                        "policy_revision": fake.base,
                        "activity_id": "report",
                    },
                    "first_review_passes": 1,
                },
            }
        },
    }
    store.write("task", "insert", tid, task)
    for dimension in ("quality", "logic"):
        gid = f"grok-{dimension}"
        aid = f"grok-{dimension}-agent"
        store.write(
            "agent",
            "insert",
            aid,
            {
                "id": aid,
                "session_id": worker.session_id,
                "task_id": tid,
                "role": f"pr-reviewer-{dimension}",
                "vendor": "grok",
                "provider": "grok",
                "model": "grok-model",
                "prompt": COMPLIANCE_PROMPT + f" Check {dimension}.",
                "head_sha": fake.head,
                "dimension": dimension,
                "status": "done",
            },
        )
        store.write(
            "review_gate",
            "insert",
            gid,
            {
                "id": gid,
                "task_id": tid,
                "stage": "review-1",
                "dimension": dimension,
                "vendor": "grok",
                "verdict": "approved",
                "head_sha": fake.head,
                "agent_id": aid,
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
    checklist = {
        "session_registered": "ja", "spec_written": "ja", "implementer_done": "ja",
        "reviewer_approved": "ja", "local_check_pass": "ja", "pushed": "ja",
        "review_1_quality": "ja", "review_1_logic": "ja",
        "review_2_quality": "pending", "review_2_logic": "pending",
        "contributing_ok": "ja", "deviation_declared": "n_a", "deviation_granted": "n_a",
    }
    for key, status in checklist.items():
        cid = f"check-{key}"
        store.write(
            "checklist_item",
            "insert",
            cid,
            {"id": cid, "task_id": tid, "key": key, "status": status},
        )
    store.write(
        "local_check",
        "insert",
        "a38-local-check",
        {
            "id": "a38-local-check",
            "task_id": tid,
            "name": "a38",
            "command": "/operator/a38",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": fake.head,
        },
    )
    return store, worker, fake, tid


def test_a38_publication_reconcile_ignores_request_state_without_saved_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import reconcile_a38_publication_intents

    task_id = "11111111-1111-4111-8111-111111111119"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )

    assert reconcile_a38_publication_intents(store, worker, task, fake) == []
    assert store.rows("ping") == []
    assert [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ] == []


def test_a38_closed_pr_does_not_reconcile_saved_publication_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_await_second_review, phase_first_comment

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_post_comment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("crash before comment")
            ),
        )
        with pytest.raises(RuntimeError, match="crash before comment"):
            phase_first_comment(store, worker, store.row("task", task_id), fake)

    fake.pr["state"] = "CLOSED"
    with pytest.raises(CoordinatorError, match="not open while awaiting second review"):
        phase_await_second_review(store, worker, store.row("task", task_id), fake)

    assert store.rows("ping") == []
    assert [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ] == []


def test_a38_revoked_await_merge_does_not_reconcile_saved_publication_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38, coordinator_runtime
    from agent_cli.coordinator_a38 import phase_first_comment
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_post_comment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("crash before comment")
            ),
        )
        with pytest.raises(RuntimeError, match="crash before comment"):
            phase_first_comment(store, worker, store.row("task", task_id), fake)

    task = store.row("task", task_id)
    task["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    fake.pr["state"] = "OPEN"
    fake.issues[0]["assignees"] = []
    monkeypatch.setattr(
        coordinator_runtime,
        "phase_await_merge",
        lambda *args, **kwargs: ["merge observed"],
    )

    assert advance_one(store, worker, runner=fake, lane_runner=None) == ["merge observed"]
    assert store.rows("ping") == []
    assert [
        row for row in store.rows("activity") if row.get("type") == "comment.post"
    ] == []


def test_a38_first_comment_crash_before_post_resumes_comment_and_all_pings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_first_comment

    store, worker, fake, tid = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )

    def crash_before_post(*_args, **_kwargs):
        raise RuntimeError("crash immediately before first comment")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(coordinator_a38, "_post_comment", crash_before_post)
        with pytest.raises(RuntimeError, match="immediately before first comment"):
            phase_first_comment(store, worker, store.row("task", tid), fake)

    crashed = store.row("task", tid)
    checkpoint = crashed["payload"]["coordinator"]
    assert checkpoint["phase"] == "a38_first_comment"
    assert checkpoint["a38"]["first_comment"]["head"] == fake.head
    assert checkpoint["a38"]["second_review"]["head"] == fake.head
    assert checkpoint["a38"]["second_review"]["pinged"] == list(
        worker.second_reviewers
    )
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 0
    assert store.rows("ping") == []

    assert phase_first_comment(store, worker, crashed, fake) == [
        f"first A38 review comment posted on {fake.head[:7]}"
    ]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1
    pings = store.rows("ping")
    assert len(pings) == len(worker.second_reviewers)
    assert sorted(row["to_login"] for row in pings) == sorted(worker.second_reviewers)
    assert len({row["id"] for row in pings}) == len(worker.second_reviewers)


def test_a38_legacy_first_comment_without_activity_is_upgraded_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import NAMESPACE_URL, uuid5

    from agent_cli.coordinator_a38 import phase_first_comment

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    activity_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task_id}:{fake.head}")
    )
    task["payload"]["coordinator"]["a38"]["first_comment"] = {
        "head": fake.head,
        "activity_id": activity_id,
    }
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    lines = phase_first_comment(store, worker, store.row("task", task_id), fake)

    assert lines == [f"first A38 review comment posted on {fake.head[:7]}"]
    saved = store.row("task", task_id)["payload"]["coordinator"]["a38"][
        "first_comment"
    ]
    assert saved["activity_id"] == activity_id
    assert isinstance(saved["body"], str) and saved["body"]
    assert saved["passes"] == 1
    assert store.row("activity", activity_id)["payload"]["body"] == saved["body"]


def test_a38_reconciliation_upgrades_legacy_first_comment_from_owned_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        phase_first_comment,
        reconcile_a38_publication_intents,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    phase_first_comment(store, worker, store.row("task", task_id), fake)
    task = store.row("task", task_id)
    first = task["payload"]["coordinator"]["a38"]["first_comment"]
    exact_body = store.row("activity", first["activity_id"])["payload"]["body"]
    first.pop("body")
    first.pop("passes")
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )

    assert reconcile_a38_publication_intents(
        store, worker, store.row("task", task_id), fake
    ) == []
    upgraded = store.row("task", task_id)["payload"]["coordinator"]["a38"][
        "first_comment"
    ]
    assert upgraded["body"] == exact_body
    assert upgraded["passes"] == 1


def test_a38_first_comment_partial_ping_crash_adds_only_the_missing_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_first_comment

    store, worker, fake, tid = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )
    real_write_pings = coordinator_a38._write_second_review_request_pings

    def write_first_ping_then_crash(store_arg, task_arg, plan):  # noqa: ANN001
        partial = dict(plan)
        partial["recipients"] = list(plan["recipients"][:1])
        real_write_pings(store_arg, task_arg, partial)
        raise RuntimeError("crash after first review ping")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_write_second_review_request_pings",
            write_first_ping_then_crash,
        )
        with pytest.raises(RuntimeError, match="crash after first review ping"):
            phase_first_comment(store, worker, store.row("task", tid), fake)

    crashed = store.row("task", tid)
    assert crashed["payload"]["coordinator"]["phase"] == "a38_first_comment"
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1
    first_ping = store.rows("ping")
    assert len(first_ping) == 1
    assert first_ping[0]["to_login"] == worker.second_reviewers[0]

    assert phase_first_comment(store, worker, crashed, fake) == [
        f"first A38 review comment already posted on {fake.head[:7]}"
    ]

    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1
    pings = store.rows("ping")
    assert len(pings) == len(worker.second_reviewers)
    assert sorted(row["to_login"] for row in pings) == sorted(worker.second_reviewers)
    assert len({row["id"] for row in pings}) == len(worker.second_reviewers)


def test_a38_merge_after_saved_first_comment_intent_reconciles_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_first_comment
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, tid = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_post_comment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("crash before comment")
            ),
        )
        with pytest.raises(RuntimeError, match="crash before comment"):
            phase_first_comment(store, worker, store.row("task", tid), fake)

    fake.pr["state"] = "MERGED"
    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    assert lines == ["PR merged during a38_first_comment; verifying the merge"]
    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"
    first = saved["payload"]["coordinator"]["a38"]["first_comment"]
    assert first["url"] == "https://x"
    comments = [
        row for row in store.rows("activity")
        if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    assert len(store.rows("ping")) == len(worker.second_reviewers)
    assert saved["state"] != "done"


def test_a38_merge_after_first_initial_ping_fills_only_missing_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import phase_first_comment
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, tid = _a38_first_comment_env(
        tmp_path,
        monkeypatch,
        second_reviewers=("outside-reviewer", "reviewer-two"),
    )
    real_write_pings = coordinator_a38._write_second_review_request_pings

    def write_first_ping_then_crash(store_arg, task_arg, plan):  # noqa: ANN001
        partial = dict(plan)
        partial["recipients"] = list(plan["recipients"][:1])
        real_write_pings(store_arg, task_arg, partial)
        raise RuntimeError("crash after initial ping")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_write_second_review_request_pings",
            write_first_ping_then_crash,
        )
        with pytest.raises(RuntimeError, match="crash after initial ping"):
            phase_first_comment(store, worker, store.row("task", tid), fake)

    # Simulate a crash after the merge observer persisted await_merge but before
    # that phase could reconcile its saved publication occurrence.
    awaiting = store.row("task", tid)
    awaiting["payload"]["coordinator"]["phase"] = "await_merge"
    store.write(
        "task",
        "update",
        tid,
        {key: value for key, value in awaiting.items() if not key.startswith("_")},
    )
    fake.pr["state"] = "MERGED"
    advance_one(store, worker, runner=fake, lane_runner=None)

    checkpoint = awaiting["payload"]["coordinator"]
    expected_comment_id = checkpoint["a38"]["first_comment"]["activity_id"]
    # Advancing may also publish a legitimate source-issue status. Count only
    # the first-review publication on this pull request.
    first_review_comments = []
    for row in store.rows("activity"):
        payload = row.get("payload")
        if (
            row.get("type") == "comment.post"
            and isinstance(payload, dict)
            and payload.get("target") == "pr"
            and payload.get("number") == checkpoint["pr_number"]
            and str(payload.get("body") or "").startswith("EN:\nFirst review with ")
        ):
            first_review_comments.append(row)
    assert len(first_review_comments) == 1
    assert first_review_comments[0]["id"] == expected_comment_id
    pings = store.rows("ping")
    assert len(pings) == len(worker.second_reviewers)
    assert sorted(row["to_login"] for row in pings) == sorted(
        worker.second_reviewers
    )
    assert len({row["id"] for row in pings}) == len(worker.second_reviewers)
    saved = store.row("task", tid)
    assert saved["state"] != "done"


def test_a38_second_review_ping_refuses_a_foreign_deterministic_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        _ping_id,
        _prepare_second_review_request,
        _write_second_review_request_pings,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    assert task is not None
    _requested, plan = _prepare_second_review_request(store, worker, task)
    login = worker.second_reviewers[0]
    ping_id = _ping_id(task_id, fake.head, login, int(plan["occurrence"]))
    store.apply_replica_row(
        {
            "table": "ping",
            "row_id": ping_id,
            "origin_device_id": "foreign-device",
            "payload": {
                "id": ping_id,
                "from_login": plan["sender"],
                "to_login": login,
                "kind": "review-request",
                "task_id": task_id,
                "body": plan["note"],
                "created_at": plan["created_at"],
                "acked_at": None,
            },
            "updated_at": "2026-01-01T00:00:03Z",
        }
    )

    with pytest.raises(CoordinatorError, match="foreign or mismatched"):
        _write_second_review_request_pings(store, task, plan)

    assert store.row("ping", ping_id)["_origin_device_id"] == "foreign-device"


def test_a38_second_review_ping_refuses_own_row_with_mismatched_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        _ping_id,
        _prepare_second_review_request,
        _write_second_review_request_pings,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    assert task is not None
    _requested, plan = _prepare_second_review_request(store, worker, task)
    login = worker.second_reviewers[0]
    ping_id = _ping_id(task_id, fake.head, login, int(plan["occurrence"]))
    store.write(
        "ping",
        "insert",
        ping_id,
        {
            "id": ping_id,
            "from_login": plan["sender"],
            "to_login": login,
            "kind": "review-request",
            "task_id": task_id,
            "body": "different review request",
            "created_at": plan["created_at"],
            "acked_at": None,
        },
    )

    with pytest.raises(CoordinatorError, match="foreign or mismatched"):
        _write_second_review_request_pings(store, task, plan)

    assert store.row("ping", ping_id)["body"] == "different review request"


def test_a38_second_review_ping_reuses_own_matching_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        _ping_id,
        _prepare_second_review_request,
        _write_second_review_request_pings,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    assert task is not None
    _requested, plan = _prepare_second_review_request(store, worker, task)
    login = worker.second_reviewers[0]
    ping_id = _ping_id(task_id, fake.head, login, int(plan["occurrence"]))
    store.write(
        "ping",
        "insert",
        ping_id,
        {
            "id": ping_id,
            "from_login": plan["sender"],
            "to_login": login,
            "kind": "review-request",
            "task_id": task_id,
            "body": plan["note"],
            "created_at": plan["created_at"],
            "acked_at": "2026-01-01T00:00:05Z",
        },
    )

    _write_second_review_request_pings(store, task, plan)

    assert len(store.rows("ping")) == 1
    assert store.row("ping", ping_id)["acked_at"] == "2026-01-01T00:00:05Z"


def test_a38_timeout_reping_crash_resumes_only_the_missing_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import (
        _ping_id,
        phase_await_second_review,
        phase_first_comment,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )
    monkeypatch.setattr(
        coordinator_a38, "utcnow", lambda: "2026-01-01T00:00:00Z"
    )
    phase_first_comment(store, worker, store.row("task", task_id), fake)
    assert len(store.rows("ping")) == 2
    real_write_pings = coordinator_a38._write_second_review_request_pings

    def crash_after_first_reping(store_arg, task_arg, plan):  # noqa: ANN001
        if int(plan["occurrence"]) == 0:
            real_write_pings(store_arg, task_arg, plan)
            return
        partial = dict(plan)
        partial["recipients"] = list(plan["recipients"][:1])
        real_write_pings(store_arg, task_arg, partial)
        raise RuntimeError("crash after first timeout re-ping")

    monkeypatch.setattr(
        coordinator_a38, "utcnow", lambda: "2026-01-02T00:00:00Z"
    )
    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_write_second_review_request_pings",
            crash_after_first_reping,
        )
        with pytest.raises(RuntimeError, match="first timeout re-ping"):
            phase_await_second_review(
                store, worker, store.row("task", task_id), fake
            )

    crashed = store.row("task", task_id)
    assert crashed is not None
    state = crashed["payload"]["coordinator"]["a38"]["second_review"]
    assert state["request_count"] == 1
    assert len(store.rows("ping")) == 3
    assert store.row(
        "ping",
        _ping_id(task_id, fake.head, worker.second_reviewers[0], 1),
    ) is not None
    assert store.row(
        "ping",
        _ping_id(task_id, fake.head, worker.second_reviewers[1], 1),
    ) is None

    monkeypatch.setattr(
        coordinator_a38, "utcnow", lambda: "2026-01-02T00:01:00Z"
    )
    assert phase_await_second_review(
        store, worker, store.row("task", task_id), fake
    ) == ["awaiting external second review"]

    pings = store.rows("ping")
    assert len(pings) == 4
    resumed = store.row("task", task_id)
    assert resumed is not None
    assert resumed["payload"]["coordinator"]["a38"]["second_review"][
        "request_count"
    ] == 1
    for login in worker.second_reviewers:
        assert sum(row["to_login"] == login for row in pings) == 2
        assert store.row("ping", _ping_id(task_id, fake.head, login, 1)) is not None
        assert store.row("ping", _ping_id(task_id, fake.head, login, 2)) is None


def test_a38_merge_after_first_timeout_reping_fills_saved_occurrence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_a38
    from agent_cli.coordinator_a38 import (
        _ping_id,
        phase_await_second_review,
        phase_first_comment,
    )
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )
    monkeypatch.setattr(
        coordinator_a38, "utcnow", lambda: "2026-01-01T00:00:00Z"
    )
    phase_first_comment(store, worker, store.row("task", task_id), fake)
    real_write_pings = coordinator_a38._write_second_review_request_pings

    def crash_after_first_reping(store_arg, task_arg, plan):  # noqa: ANN001
        if int(plan["occurrence"]) == 0:
            real_write_pings(store_arg, task_arg, plan)
            return
        partial = dict(plan)
        partial["recipients"] = list(plan["recipients"][:1])
        real_write_pings(store_arg, task_arg, partial)
        raise RuntimeError("crash after first re-ping")

    monkeypatch.setattr(
        coordinator_a38, "utcnow", lambda: "2026-01-02T00:00:00Z"
    )
    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            coordinator_a38,
            "_write_second_review_request_pings",
            crash_after_first_reping,
        )
        with pytest.raises(RuntimeError, match="crash after first re-ping"):
            phase_await_second_review(
                store, worker, store.row("task", task_id), fake
            )

    fake.pr["state"] = "MERGED"
    advance_one(store, worker, runner=fake, lane_runner=None)

    comments = [
        row for row in store.rows("activity")
        if row.get("type") == "comment.post"
    ]
    assert len(comments) == 1
    pings = store.rows("ping")
    assert len(pings) == 2 * len(worker.second_reviewers)
    for login in worker.second_reviewers:
        assert store.row("ping", _ping_id(task_id, fake.head, login, 0)) is not None
        assert store.row("ping", _ping_id(task_id, fake.head, login, 1)) is not None
        assert store.row("ping", _ping_id(task_id, fake.head, login, 2)) is None
    saved = store.row("task", task_id)
    assert saved["payload"]["coordinator"]["a38"]["second_review"][
        "request_count"
    ] == 1
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"
    assert saved["state"] != "done"


def test_a38_first_comment_ping_and_external_mail_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.a38_guard import status_context_enforce, status_context_observe
    from agent_cli.a38_review import COMPLIANCE_PROMPT, parse_review_block, validate_visible
    from agent_cli.coordinator_a38 import phase_await_ready, phase_await_second_review, phase_first_comment
    from agent_cli.coordinator_github import phase_await_merge
    from agent_cli.coordinator_lanes import invalidate_head_evidence

    store, worker, fake, tid = _a38_first_comment_env(tmp_path, monkeypatch)

    saved = store.row("task", tid)
    invalidate_head_evidence(saved["payload"]["coordinator"], fake.head)
    assert saved["payload"]["coordinator"]["a38"]["report"]["head"] == fake.head
    store.write(
        "task",
        "update",
        tid,
        {key: value for key, value in saved.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.utcnow", lambda: "2026-01-01T00:00:00Z"
    )
    saved = store.row("task", tid)
    assert "first A38 review comment posted" in phase_first_comment(store, worker, saved, fake)[0]
    comments = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    first_body = comments[0]["payload"]["body"]
    first_declaration = parse_review_block(first_body)
    assert {lane["id"] for lane in first_declaration["lanes"]} == {"conformity-a", "logic-a"}
    assert len(first_body) < 65_536
    prompt_by_dimension = {
        str(row["dimension"]): row["prompt"]
        for row in store.rows("agent")
        if row.get("task_id") == tid
    }
    assert next(
        lane["prompt"] for lane in first_declaration["lanes"] if lane["id"] == "conformity-a"
    ) == prompt_by_dimension["quality"]
    assert next(
        lane["prompt"] for lane in first_declaration["lanes"] if lane["id"] == "logic-a"
    ) == prompt_by_dimension["logic"]
    assert validate_visible(first_body, 1) == []
    assert len(store.rows("ping")) == 1
    ping_body = store.rows("ping")[0]["body"]
    assert len(ping_body) <= 1000
    assert "worker-session" in ping_body
    assert "docs/issue-coordinator.md" in ping_body
    assert "https://github.com/example/project/pull/42" in ping_body
    assert '"to_session"' not in ping_body
    phase_first_comment(store, worker, store.row("task", tid), fake)
    assert len(store.rows("ping")) == 1

    second = {
        "kind": "a38.second-review/v1", "task_id": tid, "head": fake.head,
        "defects": 0, "blocks_merge": False, "findings": "",
        "lanes": [
            {"id": "conformity-b", "provider": "OpenAI", "model": "Reviewer Model 2", "model_number": "reviewer-model-2", "prompt": COMPLIANCE_PROMPT, "status": "complete", "result": "pass"},
            {"id": "logic-b", "provider": "OpenAI", "model": "Reviewer Model 2", "model_number": "reviewer-model-2", "prompt": COMPLIANCE_PROMPT, "status": "complete", "result": "pass"},
        ],
    }

    def mail(row_id: str, login: str, payload: object) -> None:
        store.apply_replica_row(
            {"table": "activity", "row_id": row_id, "origin_device_id": f"{row_id}-device", "github_login": login, "payload": {"id": row_id, "session_id": "reviewer-session", "type": "message", "payload": {"to_session": worker.session_id, "payload": payload}, "execution_status": "pending"}, "updated_at": "2026-01-01T00:00:01Z"}
        )

    mail("unauthorized-mail", "not-allowed", second)
    assert phase_await_second_review(store, worker, store.row("task", tid), fake) == ["awaiting external second review"]
    assert "unauthorized" in store.row("task", tid)["payload"]["coordinator"]["observations"][-1]["text"]

    stale = {**second, "head": "b" * 40}
    mail("stale-mail", "outside-reviewer", stale)
    phase_await_second_review(store, worker, store.row("task", tid), fake)
    assert "stale head" in store.row("task", tid)["payload"]["coordinator"]["observations"][-1]["text"]

    mail("malformed-mail", "outside-reviewer", {"kind": "a38.second-review/v1", "task_id": tid})
    phase_await_second_review(store, worker, store.row("task", tid), fake)
    assert "malformed" in store.row("task", tid)["payload"]["coordinator"]["observations"][-1]["text"]

    invalid_blocking = {**second, "blocks_merge": True}
    mail("invalid-blocking-mail", "outside-reviewer", invalid_blocking)
    phase_await_second_review(store, worker, store.row("task", tid), fake)
    assert "blocking defects" in store.row("task", tid)["payload"]["coordinator"]["observations"][-1]["text"]

    waiting = store.row("task", tid)
    waiting["payload"]["coordinator"]["a38"]["second_review"]["last_requested_at"] = "2026-01-01T00:00:00Z"
    store.write("task", "update", tid, {key: value for key, value in waiting.items() if not key.startswith("_")})
    monkeypatch.setattr("agent_cli.coordinator_a38.utcnow", lambda: "2026-01-02T00:00:00Z")
    assert "re-sent" in phase_await_second_review(store, worker, store.row("task", tid), fake)[0]
    assert len(store.rows("ping")) == 2

    mail("second-mail", "outside-reviewer", second)
    comments_before = len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    )

    def fail_checklist(*_args, **_kwargs):
        raise RuntimeError("simulated checklist write failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            "agent_cli.coordinator_a38.set_checklist", fail_checklist
        )
        with pytest.raises(RuntimeError, match="checklist write failure"):
            phase_await_second_review(store, worker, store.row("task", tid), fake)
    assert not any(
        row.get("type") == "message.read"
        and row.get("payload", {}).get("message_id") == "second-mail"
        for row in store.rows("activity")
    )
    comments_after_failure = len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    )
    assert comments_after_failure == comments_before + 1
    lines = phase_await_second_review(store, worker, store.row("task", tid), fake)
    assert "external second review" in lines[0]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == comments_after_failure
    final = store.row("task", tid)
    assert final["payload"]["coordinator"]["phase"] == "ci"
    statuses = {row["key"]: row for row in store.rows("checklist_item") if row.get("task_id") == tid}
    assert statuses["review_2_quality"]["status"] == "ja"
    assert statuses["review_2_logic"]["evidence"].startswith("external second review by outside-reviewer")

    awaiting = store.row("task", tid)
    awaiting["payload"]["coordinator"]["phase"] = "await_ready"
    # The test skips phase_readiness; record that readiness ran on this head.
    awaiting["payload"]["coordinator"].setdefault("evidence", {})["readiness_head"] = fake.head
    store.write("task", "update", tid, {key: value for key, value in awaiting.items() if not key.startswith("_")})
    enforce_context = status_context_enforce("develop")
    observe_context = status_context_observe("develop")
    fake.statuses = [
        {
            "context": enforce_context,
            "state": "pending",
            "description": "Waiting for guard policy",
        }
    ]
    writes_before = len(store.rows("activity"))
    assert "Waiting for guard policy" in phase_await_ready(store, worker, store.row("task", tid), fake)[0]
    waiting = store.row("task", tid)
    assert waiting["payload"]["coordinator"]["a38"]["guard_status"]["description"] == "Waiting for guard policy"
    assert len(store.rows("activity")) == writes_before
    fake.pr["isDraft"] = False
    assert phase_await_ready(store, worker, store.row("task", tid), fake) == [
        f"PR left draft without a successful A38 status on {fake.head[:7]}"
    ]
    not_ready = store.row("task", tid)["payload"]["coordinator"]
    assert not_ready["phase"] == "await_ready"
    assert not_ready["observations"][-1]["text"] == (
        f"PR left draft without a successful A38 status on {fake.head[:7]}"
    )
    fake.statuses = [
        {
            "context": observe_context,
            "state": "success",
            "description": "Observe passed",
        }
    ]
    assert phase_await_ready(store, worker, store.row("task", tid), fake) == [
        f"PR left draft without a successful A38 status on {fake.head[:7]}"
    ]
    observed_only = store.row("task", tid)["payload"]["coordinator"]
    assert observed_only["phase"] == "await_ready"
    assert observed_only["a38"]["guard_status"]["context"] is None
    fake.statuses = [
        {
            "context": observe_context,
            "state": "success",
            "description": "Observe passed",
        },
        {
            "context": enforce_context,
            "state": "failure",
            "description": "Enforce failed",
        },
    ]
    assert phase_await_ready(store, worker, store.row("task", tid), fake) == [
        f"PR left draft without a successful A38 status on {fake.head[:7]}"
    ]
    enforce_red = store.row("task", tid)["payload"]["coordinator"]
    assert enforce_red["phase"] == "await_ready"
    assert enforce_red["a38"]["guard_status"]["context"] == enforce_context
    assert enforce_red["a38"]["guard_status"]["state"] == "failure"
    fake.statuses = [
        {
            "context": enforce_context,
            "state": "success",
            "description": "A38 passed",
        }
    ]
    assert "awaiting human merge" in phase_await_ready(store, worker, store.row("task", tid), fake)[0]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "await_merge"
    fake.pr["isDraft"] = True
    assert "returned to draft" in phase_await_merge(
        store, worker, store.row("task", tid), fake
    )[0]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "await_ready"
    fake.pr["isDraft"] = False
    assert "awaiting human merge" in phase_await_ready(
        store, worker, store.row("task", tid), fake
    )[0]
    assert store.row("task", tid)["payload"]["coordinator"]["phase"] == "await_merge"
    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "dddddddddddddddddddddddddddddddddddddddd"}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    # A later blocking review of the merged head wins over the earlier passing one,
    # even though task-done would already be allowed.
    import copy

    from agent_cli import coordinator_github

    blocked_variant = copy.deepcopy(store.row("task", tid))
    variant_reviews = blocked_variant["payload"]["coordinator"]["a38"]["second_review"].setdefault(
        "reviews", []
    )
    variant_reviews.append(
        {"reviewer": "outside-reviewer", "blocks_merge": True, "at": "2999-01-01 00:00:00+00:00"}
    )
    with monkeypatch.context() as variant_patch:
        variant_patch.setattr(coordinator_github, "save_task", lambda *_args: None)
        variant_patch.setattr(
            coordinator_github,
            "publish_blocker",
            lambda *_args, kind="blocker", **_kwargs: [f"blocker {kind}"],
        )
        assert phase_await_merge(store, worker, blocked_variant, fake) == [
            "blocker merged-with-blocking-second-review"
        ]
    assert blocked_variant["payload"]["coordinator"]["phase"] == "blocked"
    lines = phase_await_merge(store, worker, store.row("task", tid), fake)
    assert lines == ["human merge verified example/project#42; task done"]
    assert store.row("task", tid)["state"] == "done"
    acknowledgements = [
        row for row in store.rows("activity") if row.get("type") == "issue.assigned.ack"
    ]
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["payload"]["assigned_id"] == "assigned-a38"

    blocking_tid = "22222222-2222-4222-8222-222222222222"
    base_task = {
        key: value
        for key, value in copy.deepcopy(store.row("task", tid)).items()
        if not key.startswith("_")
    }
    blocking_task = {
        **base_task,
        "id": blocking_tid,
        "state": "pr-review",
        "payload": {
            "coordinator": {
                "phase": "await_second_review",
                "head_sha": fake.head,
                "worktree": str(worker.workspace_root / blocking_tid),
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7},
                "a38": {
                    "enabled": True,
                    "second_review": {
                        "head": fake.head,
                        "status": "requested",
                        "last_requested_at": "2026-01-01T00:00:00Z",
                    }
                },
            }
        },
    }
    store.write("task", "insert", blocking_tid, blocking_task)
    # The second scenario uses a fresh, still open draft pull request.
    fake.pr["state"] = "OPEN"
    blocking_result = {
        **second,
        "task_id": blocking_tid,
        "defects": 1,
        "blocks_merge": True,
        "findings": "The result contains a defect that must be fixed.",
    }
    mail("blocking-mail", "outside-reviewer", blocking_result)
    comments_before = len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    )

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("simulated task save failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr("agent_cli.coordinator_a38.save_task", fail_save)
        with pytest.raises(RuntimeError, match="task save failure"):
            phase_await_second_review(
                store, worker, store.row("task", blocking_tid), fake
            )
    assert not any(
        row.get("type") == "message.read"
        and row.get("payload", {}).get("message_id") == "blocking-mail"
        for row in store.rows("activity")
    )
    comments_after_failure = len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    )
    # The ordering watermark is saved before the post, so a failed save posts nothing.
    assert comments_after_failure == comments_before
    blocking_lines = phase_await_second_review(
        store, worker, store.row("task", blocking_tid), fake
    )
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == comments_before + 1
    assert "routing to implementer" in blocking_lines[-1]
    blocked = store.row("task", blocking_tid)
    assert blocked["state"] == "implementing"
    assert blocked["payload"]["coordinator"]["phase"] == "implement"
    assert blocked["payload"]["coordinator"]["findings"] == blocking_result["findings"]
    kept = blocked["payload"]["coordinator"]["a38"]["second_review"]
    assert kept["head"] == fake.head
    assert kept["reviews"][-1]["blocks_merge"] is True


def test_a38_await_second_review_retries_transient_pr_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_await_second_review

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = replace(make_worker(tmp_path), a38=True)
    head = "a" * 40
    task = {
        "id": "77777777-7777-4777-8777-777777777777",
        "session_id": worker.session_id,
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "await_second_review",
                "head_sha": head,
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7},
            }
        },
    }

    def unavailable(_argv: list[str]) -> Completed:
        return Completed(1, "", "connection reset")

    def malformed_head(_argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"headRefOid": "not-a-sha"}), "")

    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped", lambda _store, _session, runner: runner
    )
    lines = phase_await_second_review(store, worker, task, unavailable)

    assert lines == ["PR state temporarily unavailable; retrying"]
    assert task["payload"]["coordinator"]["phase"] == "await_second_review"
    with pytest.raises(CoordinatorError, match="cannot read current pull request head"):
        phase_await_second_review(store, worker, task, malformed_head)


def test_a38_await_ready_retries_transient_pr_and_status_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_await_ready

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = replace(make_worker(tmp_path), a38=True)
    head = "a" * 40
    task = {
        "id": "88888888-8888-4888-8888-888888888888",
        "session_id": worker.session_id,
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "await_ready",
                "head_sha": head,
                "pr_number": 42,
                "source": {
                    "repo": "example/project",
                    "number": 7,
                    "base": "develop",
                },
            }
        },
    }

    def unavailable(_argv: list[str]) -> Completed:
        return Completed(1, "", "connection reset")

    def statuses_unavailable(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(
                0,
                json.dumps({"headRefOid": head, "isDraft": True, "state": "OPEN"}),
                "",
            )
        return Completed(1, "", "connection reset")

    def malformed_draft(_argv: list[str]) -> Completed:
        return Completed(
            0,
            json.dumps({"headRefOid": head, "isDraft": "unknown", "state": "OPEN"}),
            "",
        )

    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped", lambda _store, _session, runner: runner
    )
    assert phase_await_ready(store, worker, task, unavailable) == [
        "PR state temporarily unavailable; retrying"
    ]
    assert phase_await_ready(store, worker, task, statuses_unavailable) == [
        "PR state temporarily unavailable; retrying"
    ]
    assert task["payload"]["coordinator"]["phase"] == "await_ready"
    with pytest.raises(CoordinatorError, match="draft state is unavailable"):
        phase_await_ready(store, worker, task, malformed_draft)


def test_a38_await_ready_base_move_invalidates_report_and_reruns_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import phase_await_ready

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    task["payload"]["coordinator"]["phase"] = "await_ready"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    moved_base = "c" * 40
    fake.pr["baseRefOid"] = moved_base

    lines = phase_await_ready(store, worker, store.row("task", task_id), fake)

    saved = store.row("task", task_id)["payload"]["coordinator"]
    assert lines == [
        "PR base changed while awaiting guard Ready; invalidating A38 report evidence"
    ]
    assert saved["phase"] == "tests"
    assert saved["base_sha"] == moved_base
    assert "report" not in saved["a38"]
    assert "policy_revision" not in saved["a38"]


@pytest.mark.parametrize(("pr_state", "verb"), [("MERGED", "merged"), ("CLOSED", "closed")])
def test_a38_await_ready_hands_a_merged_pr_to_await_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str, verb: str
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_await_ready

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = replace(make_worker(tmp_path), a38=True)
    head = "a" * 40
    task = {
        "id": "99999999-9999-4999-8999-999999999999",
        "session_id": worker.session_id,
        "repo": "example/project",
        "ref": 42,
        "payload": {
            "coordinator": {
                "phase": "await_ready",
                "head_sha": head,
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7, "base": "develop"},
            }
        },
    }

    def merged(_argv: list[str]) -> Completed:
        return Completed(
            0, json.dumps({"headRefOid": head, "isDraft": False, "state": pr_state}), ""
        )

    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped", lambda _store, _session, runner: runner
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.save_task", lambda *_args: None)

    assert phase_await_ready(store, worker, task, merged) == [
        f"PR {verb} while awaiting guard Ready; verifying the merge"
    ]
    assert task["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_second_review_pings_get_fresh_ids_after_a_blocking_review_on_the_same_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import _ping_id, send_second_review_requests
    from agent_cli.coordinator_common import coord
    from agent_cli.coordinator_lanes import invalidate_head_evidence

    task_id = "aaaaaaaa-0000-4000-8000-000000000001"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    store.set_meta("github_login", "worker-bot")
    c = coord(task)
    c.setdefault("a38", {})["second_review"] = {
        "head": fake.head,
        "request_count": 2,
        "status": "posted",
    }
    invalidate_head_evidence(c, "")
    assert "second_review" not in c["a38"]
    assert c["a38"]["second_review_next_occurrence"] == 3
    c["head_sha"] = fake.head

    assert send_second_review_requests(store, worker, task) is True

    for login in worker.second_reviewers:
        assert store.row("ping", _ping_id(task_id, fake.head, login, 3)) is not None


def test_a38_same_head_no_change_after_block_sends_one_new_ping_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli import coordinator_runtime
    from agent_cli.coordinator_a38 import phase_await_second_review, phase_first_comment
    from agent_cli.coordinator_runtime import phase_implement

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    worker = replace(
        worker, second_reviewers=("outside-reviewer", "reviewer-two")
    )

    phase_first_comment(store, worker, store.row("task", task_id), fake)
    first_intent = store.row("task", task_id)["payload"]["coordinator"]["a38"][
        "first_comment"
    ]
    first_activity_id = first_intent["activity_id"]
    original_first_body = store.row("activity", first_activity_id)["payload"]["body"]
    assert len(store.rows("ping")) == len(worker.second_reviewers)

    message_id = "same-head-blocking-review"
    _a38_review_mail(
        store,
        worker,
        message_id=message_id,
        login="outside-reviewer",
        payload=_a38_second_review_payload(
            task_id, fake.head, blocks_merge=True
        ),
        second=8,
    )
    lines = phase_await_second_review(
        store, worker, store.row("task", task_id), fake
    )
    assert "routing to implementer" in lines[-1]

    # Apply the implementer's actual no-change protocol result. The intervening
    # inner review, A38 suite and first gates are already represented by this
    # fixture; they keep the same head and first-review pass count.
    no_change = store.row("task", task_id)
    no_change["payload"]["coordinator"]["lane_outcome"] = {
        "role": "implementer",
        "round": 1,
        "status": "complete",
        "result": "no-change",
        "stdout": (
            "STATUS: complete\n"
            "RESULT: no-change\n"
            "SUMMARY_EN: The unchanged head now has a revised English summary.\n"
            "SUMMARY_DE: Der unveränderte Head hat jetzt eine überarbeitete deutsche Zusammenfassung.\n"
            "REUSED: `src/agent_cli/coordinator_a38.py:1` existing coordinator.\n"
            "ADDED: `tests/test_coordinator.py:1` retry coverage.\n"
        ),
        "applied": False,
    }
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in no_change.items() if not key.startswith("_")},
    )
    with monkeypatch.context() as no_change_patch:
        no_change_patch.setattr(
            coordinator_runtime, "stage_sign_commit_if_changes", lambda *_a: None
        )
        no_change_patch.setattr(
            coordinator_runtime, "ensure_draft", lambda *_a: []
        )
        no_change_patch.setattr(
            coordinator_runtime, "refresh_draft_body", lambda *_a: []
        )
        no_change_patch.setattr(
            coordinator_runtime, "review_rules_bound", lambda *_a: True
        )
        assert phase_implement(
            store,
            worker,
            store.row("task", task_id),
            fake,
            lane_runner=None,
        ) == ["implementer done round=1"]

    no_change = store.row("task", task_id)
    assert no_change["payload"]["coordinator"]["head_sha"] == fake.head
    assert no_change["payload"]["coordinator"]["a38"]["first_review_passes"] == 1
    no_change["state"] = "pr-review"
    no_change["payload"]["coordinator"]["phase"] = "a38_first_comment"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in no_change.items() if not key.startswith("_")},
    )

    assert phase_first_comment(
        store, worker, store.row("task", task_id), fake
    ) == [f"first A38 review comment already posted on {fake.head[:7]}"]
    assert store.row("activity", first_activity_id)["payload"]["body"] == original_first_body
    pings_after_retry = store.rows("ping")
    assert len(pings_after_retry) == 2 * len(worker.second_reviewers)
    for login in worker.second_reviewers:
        assert sum(row["to_login"] == login for row in pings_after_retry) == 2
    state = store.row("task", task_id)["payload"]["coordinator"]["a38"][
        "second_review"
    ]
    assert state["request_count"] == 1
    assert message_id in state["request_key"]

    # Simulate a restart from the last durable first-comment checkpoint. The
    # blocking-review identity keeps this retry on the same deterministic ids.
    restarted = store.row("task", task_id)
    restarted["payload"]["coordinator"]["phase"] = "a38_first_comment"
    phase_first_comment(store, worker, restarted, fake)
    assert len(store.rows("ping")) == len(pings_after_retry)
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 2


def test_a38_first_comment_reconcile_rejects_mismatched_completed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        phase_first_comment,
        reconcile_a38_publication_intents,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    phase_first_comment(store, worker, store.row("task", task_id), fake)
    task = store.row("task", task_id)
    first = task["payload"]["coordinator"]["a38"]["first_comment"]
    row = store.row("activity", first["activity_id"])
    changed = {key: value for key, value in row.items() if not key.startswith("_")}
    changed["payload"] = dict(changed["payload"], number=999)
    store.write("activity", "update", changed["id"], changed)

    with pytest.raises(CoordinatorError, match="foreign or mismatched"):
        reconcile_a38_publication_intents(store, worker, task, fake)


def test_a38_ping_reconcile_rejects_wrong_saved_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import (
        phase_first_comment,
        reconcile_a38_publication_intents,
    )

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    phase_first_comment(store, worker, store.row("task", task_id), fake)
    ping = store.rows("ping")[0]
    changed = {key: value for key, value in ping.items() if not key.startswith("_")}
    changed["body"] = "incorrect review request"
    store.write("ping", "update", changed["id"], changed)

    with pytest.raises(CoordinatorError, match="foreign or mismatched"):
        reconcile_a38_publication_intents(
            store, worker, store.row("task", task_id), fake
        )


def test_a38_forced_second_review_reping_keeps_posted_reviews_of_the_same_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_a38 import send_second_review_requests
    from agent_cli.coordinator_common import coord

    task_id = "aaaaaaaa-0000-4000-8000-000000000002"
    store, worker, fake, task = _a38_external_review_env(
        tmp_path, monkeypatch, task_id=task_id
    )
    store.set_meta("github_login", "worker-bot")
    c = coord(task)
    c["head_sha"] = fake.head
    reviews = [{"reviewer": "reviewer-one", "blocks_merge": True, "at": "2026-01-01T00:00:00Z"}]
    c["a38"]["second_review"] = {
        "head": fake.head,
        "request_count": 1,
        "status": "posted",
        "reviews": reviews,
        "requested_at": "2026-01-01T00:00:00Z",
    }

    assert send_second_review_requests(store, worker, task, force=True) is True

    state = c["a38"]["second_review"]
    assert state["status"] == "posted"
    assert state["reviews"] == reviews
    assert state["request_count"] == 2
    assert state["requested_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("missing_evidence", ["agent_id", "prompt"])
def test_a38_first_comment_reruns_approved_gate_without_prompt_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_evidence: str,
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_first_comment
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    suffix = "1" if missing_evidence == "prompt" else "0"
    tid = f"44444444-4444-4444-8444-44444444444{suffix}"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "review",
        "state": "pr-review",
        "title": "A38 prompt provenance",
        "repo": "example/project",
        "models": {"first": "grok", "second": None},
        "payload": {
            "coordinator": {
                "phase": "a38_first_comment",
                "head_sha": _POSTED_HEAD,
                "worktree": str(worktree),
                "source": {"repo": "example/project", "number": 7},
                "a38": {"enabled": True, "first_review_passes": 1},
            }
        },
    }
    store.write("task", "insert", tid, task)
    for dimension in ("quality", "logic"):
        cid = f"{tid}-check-{dimension}"
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "key": f"review_1_{dimension}",
                "status": "ja",
                "evidence": "seed",
                "source": "script",
            },
        )
        aid = f"{tid}-agent-{dimension}"
        if dimension == "logic" or missing_evidence == "prompt":
            agent = {
                "id": aid,
                "session_id": worker.session_id,
                "task_id": tid,
                "role": f"pr-reviewer-{dimension}",
                "vendor": "grok",
                "provider": "grok",
                "model": "grok-model",
                "head_sha": _POSTED_HEAD,
                "dimension": dimension,
                "status": "done",
            }
            if dimension == "logic":
                agent["prompt"] = "Review the logic."
            store.write("agent", "insert", aid, agent)
        gid = f"{tid}-gate-{dimension}"
        gate = {
            "id": gid,
            "task_id": tid,
            "stage": "review-1",
            "dimension": dimension,
            "vendor": "grok",
            "verdict": "approved",
            "head_sha": _POSTED_HEAD,
            "recorded_at": "2026-01-01T00:00:00Z",
        }
        if dimension == "logic" or missing_evidence == "prompt":
            gate["agent_id"] = aid
        store.write("review_gate", "insert", gid, gate)

    monkeypatch.setattr(
        "agent_cli.coordinator_a38.verify_signed_clean_head",
        lambda *args: _POSTED_HEAD,
    )
    lines = phase_first_comment(
        store,
        worker,
        store.row("task", tid),
        lambda argv: Completed(0, "", ""),
    )
    assert lines == ["first review quality prompt evidence missing; re-running gate"]
    saved = store.row("task", tid)
    assert saved["payload"]["coordinator"]["phase"] == "pr_gates_grok"

    _patch_pr_gate_git(monkeypatch, tmp_path)
    launched: list[str] = []

    def run(argv, stdin=None):  # noqa: ANN001
        text = " ".join(str(part) for part in argv) + "\n" + (stdin or "")
        if "pr-reviewer-quality" in text or "Quality:" in text:
            launched.append("quality")
        elif "pr-reviewer-logic" in text or "Logic:" in text:
            launched.append("logic")
        return Completed(0, "STATUS: complete\nRESULT: approved\n", "")

    phase_pr_gates(
        store,
        worker,
        saved,
        lambda argv: Completed(0, "", ""),
        run,
        vendor="grok",
        stage="review-1",
    )
    assert launched == ["quality"]


@pytest.mark.parametrize("failing", ["precheck", "policy", "run", "verify"])
def test_a38_tests_failure_routes_to_implementer_without_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_tests as phase_a38_tests

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    patch_execute_github(monkeypatch)
    head = "a" * 40
    tid = "33333333-3333-4333-8333-33333333333f"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid, "session_id": worker.session_id, "workflow": "implement",
        "state": "local-check", "title": "A38 tests", "repo": "example/project", "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {"coordinator": {"phase": "tests", "head_sha": head, "worktree": str(worktree), "pr_number": 42, "source": {"repo": "example/project", "number": 7}, "a38": {"enabled": True}}},
    }
    store.write("task", "insert", tid, task)
    store.write("checklist_item", "insert", "a38-fail-local", {"id": "a38-fail-local", "task_id": tid, "key": "local_check_pass", "status": "pending"})
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: ("b" * 40, ["src/app.py"]),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.scoped", lambda *a, **k: lambda argv: Completed(0, "false", ""))
    monkeypatch.setattr("agent_cli.coordinator_a38.gh_json", lambda *a, **k: False)
    monkeypatch.setattr("agent_cli.coordinator_a38.ensure_draft", lambda *a, **k: [])
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.run_bounded",
        lambda argv, **_k: Completed(1 if failing == "precheck" else 0, "precheck output", ""),
    )

    def fake_git(_store, _worker, _runner, _worktree, *parts):  # noqa: ANN001
        if parts[0] == "fetch":
            return Completed(0, "", "")
        if parts[0] == "rev-parse":
            return Completed(0, "b" * 40 + "\n", "")
        if parts[0] == "show":
            return Completed(0, '{"schema":"a38-policy/v1","jobs":[]}', "")
        raise AssertionError(parts)

    monkeypatch.setattr("agent_cli.coordinator_a38.git", fake_git)
    calls: list[str] = []

    def fake_cli(argv, *, worker, cwd):  # noqa: ANN001
        calls.append(argv[0])
        if argv[0] == "run":
            Path(argv[argv.index("--output") + 1]).write_text("report", encoding="utf-8")
        return Completed(1 if argv[0] == failing else 0, f"{argv[0]} output", "")

    monkeypatch.setattr("agent_cli.coordinator_a38._run_cli", fake_cli)

    lines = phase_a38_tests(store, worker, store.row("task", tid), lambda argv: Completed(0, "", ""))

    saved = store.row("task", tid)
    c = saved["payload"]["coordinator"]
    prefix = "Trusted precheck failed" if failing == "precheck" else "A38 failed"
    assert lines == [f"{prefix.lower() if failing == 'precheck' else prefix} on {head[:7]}; routing to implementer"]
    assert c["phase"] == "implement"
    assert saved["state"] == "implementing"
    assert c["findings"].startswith(f"{prefix} on {head[:7]}")
    if failing != "precheck":
        assert c["evidence"]["tests_pass"] is False
    assert "report" not in c.get("a38", {})
    assert not [
        row for row in store.rows("activity")
        if row.get("type") == "comment.post" and row["payload"].get("exact_body")
    ]
    if failing == "precheck":
        assert calls == []


@pytest.mark.parametrize("failing", ["none", "run"])
def test_a38_tests_after_a_merge_during_the_suite_go_to_the_merge_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_tests as phase_a38_tests

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    patch_execute_github(monkeypatch)
    head = "a" * 40
    tid = "33333333-3333-4333-8333-33333333334f"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid, "session_id": worker.session_id, "workflow": "implement",
        "state": "local-check", "title": "A38 tests", "repo": "example/project", "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {"coordinator": {"phase": "tests", "head_sha": head, "worktree": str(worktree), "pr_number": 42, "source": {"repo": "example/project", "number": 7}, "a38": {"enabled": True}}},
    }
    store.write("task", "insert", tid, task)
    store.write("checklist_item", "insert", "a38-fail-local", {"id": "a38-fail-local", "task_id": tid, "key": "local_check_pass", "status": "pending"})
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: ("b" * 40, ["src/app.py"]),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.scoped", lambda *a, **k: lambda argv: Completed(0, "false", ""))
    monkeypatch.setattr("agent_cli.coordinator_a38.gh_json", lambda *a, **k: False)
    monkeypatch.setattr("agent_cli.coordinator_a38.ensure_draft", lambda *a, **k: [])
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.run_bounded",
        lambda argv, **_k: Completed(1 if failing == "precheck" else 0, "precheck output", ""),
    )

    def fake_git(_store, _worker, _runner, _worktree, *parts):  # noqa: ANN001
        if parts[0] == "fetch":
            return Completed(0, "", "")
        if parts[0] == "rev-parse":
            return Completed(0, "b" * 40 + "\n", "")
        if parts[0] == "show":
            return Completed(0, '{"schema":"a38-policy/v1","jobs":[]}', "")
        raise AssertionError(parts)

    monkeypatch.setattr("agent_cli.coordinator_a38.git", fake_git)
    calls: list[str] = []

    def fake_cli(argv, *, worker, cwd):  # noqa: ANN001
        calls.append(argv[0])
        if argv[0] == "run":
            Path(argv[argv.index("--output") + 1]).write_text("report", encoding="utf-8")
        return Completed(1 if argv[0] == failing else 0, f"{argv[0]} output", "")

    monkeypatch.setattr("agent_cli.coordinator_a38._run_cli", fake_cli)

    monkeypatch.setattr("agent_cli.coordinator_a38.pr_state", lambda *a, **k: "MERGED")
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.ensure_draft",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push a merged PR")),
    )

    lines = phase_a38_tests(store, worker, store.row("task", tid), lambda argv: Completed(0, "", ""))

    saved = store.row("task", tid)
    assert lines == ["PR merged during the A38 suite; verifying the merge"]
    assert saved["payload"]["coordinator"]["phase"] == "await_merge"


def test_a38_pr_left_open_routes_only_a_definite_merge_or_close(monkeypatch) -> None:
    from agent_cli import coordinator_runtime

    saved: list[str] = []
    monkeypatch.setattr(coordinator_runtime, "save_task", lambda _store, task: saved.append(task["payload"]["coordinator"]["phase"]))
    for state, expected in (("MERGED", "await_merge"), ("CLOSED", "await_merge"), ("OPEN", None), (None, None)):
        task = {"payload": {"coordinator": {"phase": "implement"}}}
        monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, _s=state, **k: _s)
        lines = coordinator_runtime._a38_pr_left_open(object(), object(), task, object(), "implement")
        assert (lines is None) is (expected is None)
        if expected:
            assert task["payload"]["coordinator"]["phase"] == expected


@pytest.mark.parametrize(
    ("paths", "reason"),
    [
        (["docs/guide.md", "README.md"], "markdown-only change set"),
        (
            ["docs/guide.md", ".github/workflows/a38-guard.yml"],
            "guard-docs change set",
        ),
    ],
)
def test_a38_inventory_waiver_skips_suite_and_report_but_continues_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
    reason: str,
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import (
        local_test_gate_satisfied,
        phase_tests as phase_a38_tests,
    )

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    head = "a" * 40
    base = "b" * 40
    tid = "33333333-3333-4333-8333-33333333335f"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "local-check",
        "title": "A38 waiver",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {
            "coordinator": {
                "phase": "tests",
                "head_sha": head,
                "worktree": str(worktree),
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7, "base": "develop"},
                "a38": {"enabled": True},
            }
        },
    }
    store.write("task", "insert", tid, task)
    statuses = {
        "session_registered": "ja",
        "spec_written": "ja",
        "implementer_done": "ja",
        "reviewer_approved": "ja",
        "local_check_pass": "pending",
        "pushed": "pending",
        "review_1_quality": "pending",
        "review_1_logic": "pending",
        "review_2_quality": "pending",
        "review_2_logic": "pending",
        "contributing_ok": "pending",
        "deviation_declared": "pending",
        "deviation_granted": "pending",
    }
    for key, status in statuses.items():
        ident = f"waiver-{key}"
        store.write(
            "checklist_item",
            "insert",
            ident,
            {"id": ident, "task_id": tid, "key": key, "status": status},
        )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: (base, paths),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped",
        lambda _store, _session, runner: runner,
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._run_cli",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("suite must be waived")),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.ensure_draft", lambda *a, **k: [])

    lines = phase_a38_tests(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", "")
    )

    saved = store.row("task", tid)
    checkpoint = saved["payload"]["coordinator"]
    assert lines == [f"A38 report waived on {head[:7]} ({reason})"]
    assert checkpoint["phase"] == "pr_gates_grok"
    assert checkpoint["a38"]["report_waiver"] == {
        "head": head,
        "base_sha": base,
        "policy_revision": base,
        "reason": reason,
        "observed_at": checkpoint["a38"]["report_waiver"]["observed_at"],
    }
    assert "report" not in checkpoint["a38"]
    assert checkpoint["evidence"]["tests_waiver"] == reason
    assert local_test_gate_satisfied(saved, head) is True
    assert not [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    checks = [row for row in store.rows("local_check") if row.get("task_id") == tid]
    assert len(checks) == 1 and checks[0]["result"] == "skip"


def test_a38_policy_change_fails_before_old_base_evidence_is_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_tests as phase_a38_tests

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True)
    head = "a" * 40
    tid = "33333333-3333-4333-8333-33333333336f"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "local-check",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {"coordinator": {"phase": "tests", "head_sha": head, "worktree": str(worktree), "pr_number": 42, "source": {"repo": "example/project", "number": 7, "base": "develop"}, "a38": {"enabled": True}}},
    }
    store.write("task", "insert", tid, task)
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: ("b" * 40, [".github/a38.json", "src/app.py"]),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped",
        lambda _store, _session, runner: runner,
    )

    with pytest.raises(CoordinatorError, match="policy migrations are not supported"):
        phase_a38_tests(
            store, worker, store.row("task", tid), lambda argv: Completed(0, "", "")
        )

    assert store.rows("local_check") == []
    assert store.rows("activity") == []


def test_a38_policy_change_is_a_final_blocker_without_reply_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_runtime import advance_one

    store, worker, fake, task_id = _a38_first_comment_env(tmp_path, monkeypatch)
    task = store.row("task", task_id)
    checkpoint = task["payload"]["coordinator"]
    checkpoint["phase"] = "tests"
    checkpoint["resume_phase"] = "readiness"
    checkpoint["question_activity_id"] = "stale-question"
    store.write(
        "task",
        "update",
        task_id,
        {key: value for key, value in task.items() if not key.startswith("_")},
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: (fake.base, [".github/a38.json", "src/app.py"]),
    )

    lines = advance_one(store, worker, runner=fake, lane_runner=None)

    saved = store.row("task", task_id)
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked", lines
    assert "policy migrations are not supported" in checkpoint["blocker"]
    assert checkpoint.get("resume_phase") in (None, "")
    assert checkpoint.get("question_activity_id") in (None, "")
    assert any("blocker published" in line for line in lines)

    fake.comments.append(
        {"id": 999, "body": "please continue", "user": {"login": "human-owner"}}
    )
    advance_one(store, worker, runner=fake, lane_runner=None)
    checkpoint = store.row("task", task_id)["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint.get("resume_phase") in (None, "")


def test_a38_report_intent_identity_includes_base_and_policy_revision() -> None:
    from agent_cli.coordinator_a38 import _report_activity_id

    task_id = "33333333-3333-4333-8333-33333333337f"
    head = "a" * 40
    base_one = "b" * 40
    base_two = "c" * 40
    first = _report_activity_id(
        task_id, head=head, base_sha=base_one, policy_revision=base_one
    )
    moved = _report_activity_id(
        task_id, head=head, base_sha=base_two, policy_revision=base_two
    )
    policy_changed = _report_activity_id(
        task_id, head=head, base_sha=base_one, policy_revision=head
    )

    assert len({first, moved, policy_changed}) == 3


def test_a38_context_reads_authoritative_base_and_one_pr_inventory() -> None:
    from agent_cli.coordinator_a38 import _pull_a38_context

    head = "a" * 40
    base = "b" * 40
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(
                0,
                json.dumps(
                    {
                        "headRefOid": head,
                        "baseRefOid": base,
                        "baseRefName": "develop",
                    }
                ),
                "",
            )
        if argv[:2] == ["gh", "api"]:
            return Completed(
                0,
                json.dumps(
                    [[
                        {"status": "modified", "filename": "docs/guide.md"},
                        {
                            "status": "modified",
                            "filename": ".github/workflows/a38-guard.yml",
                        },
                    ]]
                ),
                "",
            )
        raise AssertionError(argv)

    observed_base, paths = _pull_a38_context(
        runner,
        repo="example/project",
        number=42,
        expected_head=head,
        expected_base="develop",
    )

    assert observed_base == base
    assert paths == ["docs/guide.md", ".github/workflows/a38-guard.yml"]
    assert len(calls) == 2


def test_a38_tests_run_policy_run_verify_and_post_exact_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_a38 import phase_tests as phase_a38_tests

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(
        make_worker(tmp_path),
        review_session=None,
        a38=True,
        second_reviewers=("outside-reviewer",),
    )
    patch_execute_github(monkeypatch)
    head = "a" * 40
    base = "b" * 40
    tid = "33333333-3333-4333-8333-333333333333"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid, "session_id": worker.session_id, "workflow": "implement",
        "state": "local-check", "title": "A38 tests", "repo": "example/project", "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {"coordinator": {"phase": "tests", "head_sha": head, "worktree": str(worktree), "pr_number": 42, "source": {"repo": "example/project", "number": 7}, "a38": {"enabled": True}}},
    }
    store.write("task", "insert", tid, task)
    checklist = {
        "session_registered": "ja", "spec_written": "ja", "implementer_done": "ja",
        "reviewer_approved": "ja", "local_check_pass": "pending", "pushed": "pending",
        "review_1_quality": "pending", "review_1_logic": "pending",
        "review_2_quality": "pending", "review_2_logic": "pending",
        "contributing_ok": "pending", "deviation_declared": "pending", "deviation_granted": "pending",
    }
    for key, status in checklist.items():
        cid = f"a38-check-{key}"
        store.write("checklist_item", "insert", cid, {"id": cid, "task_id": tid, "key": key, "status": status})

    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a: head)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *a, **k: (base, ["src/app.py"]),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.scoped", lambda *a, **k: lambda argv: Completed(0, "false", ""))
    monkeypatch.setattr("agent_cli.coordinator_a38.gh_json", lambda *a, **k: False)
    monkeypatch.setattr("agent_cli.coordinator_a38.ensure_draft", lambda *a, **k: [])
    prechecks: list[list[str]] = []

    def fake_bounded(argv, **_kwargs):  # noqa: ANN001
        prechecks.append(argv)
        return Completed(0, "trusted check passed", "")

    monkeypatch.setattr("agent_cli.coordinator_a38.run_bounded", fake_bounded)

    def fake_git(_store, _worker, _runner, _worktree, *parts):  # noqa: ANN001
        if parts[0] == "fetch":
            return Completed(0, "", "")
        if parts[0] == "rev-parse":
            return Completed(0, base + "\n", "")
        if parts[0] == "show":
            return Completed(0, '{"schema":"a38-policy/v1","jobs":[]}', "")
        raise AssertionError(parts)

    monkeypatch.setattr("agent_cli.coordinator_a38.git", fake_git)
    calls: list[str] = []
    report_body = "<!-- DFX-LOCAL-CI:v1 -->\nverified report\n<!-- /DFX-LOCAL-CI:v1 -->"

    def fake_cli(argv, *, worker, cwd):  # noqa: ANN001
        calls.append(argv[0])
        if argv[0] == "run":
            Path(argv[argv.index("--output") + 1]).write_text(report_body, encoding="utf-8")
        return Completed(0, "ok", "")

    monkeypatch.setattr("agent_cli.coordinator_a38._run_cli", fake_cli)

    def crash_before_task_save(*_args, **_kwargs):
        raise RuntimeError("simulated task save failure")

    def runner(_argv: list[str]) -> Completed:
        return Completed(0, "", "")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr("agent_cli.coordinator_a38.save_task", crash_before_task_save)
        with pytest.raises(RuntimeError, match="task save failure"):
            phase_a38_tests(store, worker, store.row("task", tid), runner)
    assert calls == ["policy", "run", "verify"]
    assert len(prechecks) == 1
    report_rows = [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    assert report_rows[0]["payload"]["body"] == report_body
    assert report_rows[0]["payload"]["exact_body"] is True
    checks = [row for row in store.rows("local_check") if row.get("task_id") == tid]
    assert any(row.get("name") == "a38" and row.get("result") == "pass" for row in checks)

    errored = store.row("activity", report_rows[0]["id"])
    assert errored is not None
    errored = {key: value for key, value in errored.items() if not key.startswith("_")}
    errored["execution_status"] = "error"
    errored["execution_error"] = "temporary GitHub failure"
    errored.pop("result", None)
    store.write("activity", "update", errored["id"], errored)
    retried_bodies: list[str] = []

    def retry_github(store_arg, runner_arg, *, activity_ids):  # noqa: ANN001
        assert callable(runner_arg)
        assert activity_ids == (errored["id"],)
        row = store_arg.row("activity", errored["id"])
        assert row is not None and row["execution_status"] == "pending"
        retried_bodies.append(row["payload"]["body"])
        updated = {key: value for key, value in row.items() if not key.startswith("_")}
        updated["execution_status"] = "done"
        updated.pop("execution_error", None)
        updated["result"] = {"id": 99, "url": "https://x"}
        store_arg.write("activity", "update", errored["id"], updated)
        return []

    monkeypatch.setattr("agent_cli.coordinator_a38.execute_github", retry_github)

    lines = phase_a38_tests(store, worker, store.row("task", tid), runner)

    assert calls == ["policy", "run", "verify"]
    assert len(prechecks) == 1
    assert retried_bodies == [report_body]
    assert len(
        [row for row in store.rows("activity") if row.get("type") == "comment.post"]
    ) == 1
    assert "already posted" in lines[0]
    saved = store.row("task", tid)
    recovered_report = saved["payload"]["coordinator"]["a38"]["report"]
    assert recovered_report["head"] == head
    assert recovered_report["base_sha"] == base
    assert recovered_report["policy_revision"] == base
    assert recovered_report["activity_id"] == report_rows[0]["id"]
    assert recovered_report["url"] == "https://x"
    assert saved["payload"]["coordinator"]["evidence"]["tests_pass"] is True
    assert saved["payload"]["coordinator"]["evidence"]["tests_head"] == head
    assert saved["payload"]["coordinator"]["phase"] == "pr_gates_grok"
    local_check = next(
        row
        for row in store.rows("checklist_item")
        if row.get("task_id") == tid and row.get("key") == "local_check_pass"
    )
    assert local_check["status"] == "ja"
    assert local_check["evidence"] == f"A38 pass on {head}"


def _claude_first_manifest(home: Path) -> None:
    path = home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "claude", "second": "codex"}
    data["accounts"]["claude-w"] = {"provider": "claude", "config_dir": "/test/claude"}
    data["roles"]["claude-builder"] = {"account": "claude-w", "model": "claude-model", "access": "workspace-write"}
    data["roles"]["claude-reader"] = {"account": "claude-w", "model": "claude-model", "access": "read-only"}
    data["lanes"] = {
        "claude:implementer": "claude-builder",
        "claude:reviewer": "claude-reader",
        "claude:pr-reviewer-quality": "claude-reader",
        "claude:pr-reviewer-logic": "claude-reader",
    }
    path.write_text(json.dumps(data))


def test_preflight_accepts_claude_first_with_device_lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    preflight_worker(store, worker, fake)


def test_preflight_accepts_openrouter_first_with_device_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "openrouter", "second": "codex"}
    data["accounts"]["router-w"] = {
        "provider": "openrouter", "config_dir": "/test/openrouter"
    }
    data["roles"]["router-builder"] = {
        "account": "router-w", "model": "vendor/model", "access": "workspace-write"
    }
    data["roles"]["router-reader"] = {
        "account": "router-w", "model": "vendor/model", "access": "read-only"
    }
    data["sessions"]["worker-session"]["lanes"].update({
        "openrouter:implementer": "router-builder",
        "openrouter:reviewer": "router-reader",
        "openrouter:pr-reviewer-quality": "router-reader",
        "openrouter:pr-reviewer-logic": "router-reader",
    })
    path.write_text(json.dumps(data))
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    preflight_worker(store, worker, fake)


def test_coordinator_refuses_openrouter_config_overlapping_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import launch_lane

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = worker.workspace_root / "overlap"
    worktree.mkdir()
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["accounts"]["router"] = {
        "provider": "openrouter", "config_dir": str(worktree / "config")
    }
    data["roles"]["router-builder"] = {
        "account": "router", "model": "vendor/model", "access": "workspace-write"
    }
    data["sessions"]["worker-session"]["lanes"]["openrouter:implementer"] = "router-builder"
    path.write_text(json.dumps(data))
    task = {
        "id": "aa000001-0000-4000-8000-0000000000e0",
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "implementing",
        "models": {"first": "openrouter", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }
    store.write("task", "insert", task["id"], task)
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )
    with pytest.raises(CoordinatorError, match="config_dir overlaps worktree"):
        launch_lane(
            store,
            worker,
            task,
            role="implementer",
            vendor="openrouter",
            round_num=1,
            spec_body="Implement.",
            runner=lambda argv: Completed(0, "", ""),
            lane_runner=lambda argv, stdin: Completed(0, "", ""),
        )
    assert not [row for row in store.rows("agent") if row.get("task_id") == task["id"]]


def test_sequential_openrouter_lane_hides_its_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import launch_lane

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = worker.workspace_root / "hidden"
    worktree.mkdir()
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["accounts"]["router"] = {"provider": "openrouter", "config_dir": "/opt/profiles/router"}
    data["roles"]["router-builder"] = {
        "account": "router", "model": "vendor/model", "access": "workspace-write"
    }
    data["sessions"]["worker-session"]["lanes"]["openrouter:implementer"] = "router-builder"
    path.write_text(json.dumps(data))
    task = {
        "id": "aa000001-0000-4000-8000-0000000000e7",
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "implementing",
        "models": {"first": "openrouter", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }
    store.write("task", "insert", task["id"], task)
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )

    agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="openrouter",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lambda argv, stdin: Completed(
            2, "", "cannot read /opt/profiles/router/openrouter-api-key"
        ),
    )

    assert "/opt/profiles/router" not in result.stderr
    assert "<config_dir>/openrouter-api-key" in result.stderr
    runs = [row for row in store.rows("activity") if row.get("type") == "lane.run"]
    assert runs and "/opt/profiles/router" not in runs[0]["payload"]["final_text"]


@pytest.mark.parametrize("failure", ["decode", "oserror"])
def test_sequential_openrouter_output_failure_ends_agent_and_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from agent_cli.coordinator_lanes import launch_lane

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    patch_execute_github(monkeypatch)
    worktree = worker.workspace_root / "decode"
    worktree.mkdir()
    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["accounts"]["router"] = {"provider": "openrouter", "config_dir": "/opt/profiles/router"}
    data["roles"]["router-builder"] = {
        "account": "router", "model": "vendor/model", "access": "workspace-write"
    }
    data["sessions"]["worker-session"]["lanes"]["openrouter:implementer"] = "router-builder"
    path.write_text(json.dumps(data))
    task = {
        "id": "aa000001-0000-4000-8000-0000000000e8",
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "implementing",
        "models": {"first": "openrouter", "second": "codex"},
        "payload": {
            "coordinator": {
                "worktree": str(worktree),
                "source": {"repo": "example/project", "number": 7},
            }
        },
    }
    store.write("task", "insert", task["id"], task)
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )
    output_paths: list[Path] = []
    launches = {"count": 0}

    def decode_failure(argv: list[str], _stdin: str | None) -> Completed:
        launches["count"] += 1
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        output = Path(argv[wrapper_index + 2])
        output_paths.append(output)
        if failure == "decode":
            output.write_bytes(b"\xff")
        else:
            output.unlink()
        return Completed(0, "", "")

    _agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="openrouter",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=decode_failure,
    )

    expected = (
        "not valid UTF-8" if failure == "decode" else "lane output file is unavailable"
    )
    assert launches["count"] == 1
    assert result.status == "unavailable"
    assert expected in result.stderr
    assert output_paths and not output_paths[0].exists()
    agents = [row for row in store.rows("agent") if row.get("task_id") == task["id"]]
    assert agents and all(row.get("status") != "working" for row in agents)
    assert expected in str(agents[0]["note"])
    saved = store.row("task", task["id"])
    assert saved is not None
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["lane"]["state"] == "finished"
    assert checkpoint["lane"]["status"] == "unavailable"
    assert checkpoint["lane"]["result"] == ""
    assert checkpoint["lane_outcome"]["status"] == "unavailable"
    assert checkpoint["lane_outcome"]["result"] == ""
    # Recoverable incomplete output: never an uncertain interruption.
    assert checkpoint.get("uncertain_lane") is not True
    assert saved["state"] != "failed"
    runs = [
        row
        for row in store.rows("activity")
        if row.get("type") == "lane.run" and row["payload"].get("task_id") == task["id"]
    ]
    assert len(runs) == 1
    assert runs[0]["payload"]["status"] == "unavailable"

    # The finished agent does not block a retry of the same safe step.
    def valid_output(argv: list[str], _stdin: str | None) -> Completed:
        launches["count"] += 1
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        stream = json.dumps(
            {"type": "result", "result": "STATUS: complete\nRESULT: done", "usage": {}}
        ) + "\n"
        Path(argv[wrapper_index + 2]).write_text(stream, encoding="utf-8")
        return Completed(0, "", "")

    _agent, retried = launch_lane(
        store,
        worker,
        store.row("task", task["id"]),
        role="implementer",
        vendor="openrouter",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=valid_output,
    )
    assert launches["count"] == 2
    assert retried.status == "complete"


def test_sequential_inner_reviewer_output_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import launch_lane
    from agent_cli.coordinator_runtime import phase_inner_review

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    patch_execute_github(monkeypatch)
    worktree = worker.workspace_root / "inner"
    worktree.mkdir()
    tid = "aa000001-0000-4000-8000-0000000000e9"
    task = {
        "id": tid,
        "session_id": "worker-session",
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
        "round-1",
        {
            "id": "round-1",
            "task_id": tid,
            "round": 1,
            "implementer_verdict": "done",
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )
    data = json.loads((store.home / "ai-accounts.json").read_text())
    data["accounts"]["router"] = {
        "provider": "openrouter", "config_dir": "/test/openrouter"
    }
    data["roles"]["router-reader"] = {
        "account": "router", "model": "vendor/model", "access": "read-only"
    }
    data["sessions"]["worker-session"]["lanes"]["openrouter:reviewer"] = "router-reader"
    (store.home / "ai-accounts.json").write_text(json.dumps(data))
    task["models"] = {"first": "openrouter", "second": "codex"}
    store.write("task", "update", tid, {k: v for k, v in task.items() if not k.startswith("_")})
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )

    def broken(argv, stdin=None):  # noqa: ANN001
        wrapper_index = next(i for i, value in enumerate(argv) if "openrouter-api-key" in value)
        Path(argv[wrapper_index + 2]).write_bytes(b"\xff")
        return Completed(0, "", "")

    saved = store.row("task", tid)
    assert saved is not None
    lines = phase_inner_review(store, worker, saved, lambda argv: Completed(0, "", ""), broken)

    assert any("incomplete" in line for line in lines)
    checkpoint = store.row("task", tid)["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint["resume_phase"] == "inner_review"
    assert checkpoint.get("uncertain_lane") is not True
    reviewer = [
        row for row in store.rows("agent")
        if row.get("task_id") == tid and row.get("role") == "reviewer"
    ]
    assert len(reviewer) == 1
    assert reviewer[0]["status"] == "done"
    assert "not valid UTF-8" in str(reviewer[0]["note"])
    runs = [
        row for row in store.rows("activity")
        if row.get("type") == "lane.run" and row["payload"].get("task_id") == tid
    ]
    assert len(runs) == 1
    assert runs[0]["payload"]["status"] == "unavailable"


@pytest.mark.parametrize("failure", ["decode", "oserror"])
def test_parallel_pr_review_output_failure_is_retryable_provider_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    tid = "aa000001-0000-4000-8000-0000000000f1"
    task = _seed_posted_codex_task(
        store,
        worker,
        tid,
        codex_dims=(),
        codex_status={"review_2_quality": "pending", "review_2_logic": "pending"},
    )
    Path(task["payload"]["coordinator"]["worktree"]).mkdir(parents=True)
    _patch_pr_gate_git(monkeypatch, tmp_path)
    patch_execute_github(monkeypatch)

    def run(argv, stdin=None):  # noqa: ANN001
        output = Path(argv[argv.index("--output-last-message") + 1])
        if failure == "decode":
            output.write_bytes(b"\xff")
        else:
            output.unlink()
        return Completed(0, "", "")

    lines = phase_pr_gates(
        store,
        worker,
        task,
        lambda argv: Completed(0, "", ""),
        run,
        vendor="codex",
        stage="review-2",
    )

    assert any("unavailable; blocked" in line for line in lines)
    saved = store.row("task", tid)
    assert saved is not None
    checkpoint = saved["payload"]["coordinator"]
    assert checkpoint["phase"] == "blocked"
    assert checkpoint.get("uncertain_lane") is not True
    assert checkpoint["resume_phase"] == "pr_gates_codex"
    agents = [
        row for row in store.rows("agent")
        if row.get("task_id") == tid and row.get("vendor") == "codex"
    ]
    expected = (
        "not valid UTF-8" if failure == "decode" else "lane output file is unavailable"
    )
    assert len(agents) == 2
    assert all(row["status"] == "done" for row in agents)
    assert all(expected in str(row["note"]) for row in agents)
    runs = [
        row for row in store.rows("activity")
        if row.get("type") == "lane.run" and row["payload"].get("task_id") == tid
    ]
    assert len(runs) == 2
    assert all(row["payload"]["status"] == "unavailable" for row in runs)
    gates = [row for row in store.rows("review_gate") if row.get("task_id") == tid]
    unavailable = [
        gate
        for gate in gates
        if gate.get("stage") == "review-2" and gate.get("verdict") == "unavailable"
    ]
    assert {gate["dimension"] for gate in unavailable} == {"quality", "logic"}
    assert all(gate.get("evidence") for gate in unavailable)
    checklist = _checklist_status(store, tid)
    assert checklist["review_2_quality"] == "unavailable"
    assert checklist["review_2_logic"] == "unavailable"

    # The safe read-only review is retryable: no working agent blocks a new stage.
    from agent_cli.coordinator_lanes import vendor_stage_has_working_agents

    assert vendor_stage_has_working_agents(store, tid, vendor="codex") is None


def test_pr_review_agent_for_claude_feeds_spec_on_stdin(tmp_path: Path) -> None:
    from agent_cli.coordinator_lanes import _prepare_pr_review_agent

    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = {
        "id": "t-claude",
        "session_id": "worker-session",
        "models": {"first": "claude", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }
    prep = _prepare_pr_review_agent(
        store, worker, task, role="pr-reviewer-quality", vendor="claude",
        stage="review-1", head="a" * 40,
        spec_body="Review this.",
    )
    argv = prep["argv"]
    assert "claude" in argv and "--print" in argv
    assert "--restricted" in argv and "--setting-sources" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--add-dir" in argv
    assert "Review this." in (prep.get("stdin_text") or "")
    agent = store.row("agent", prep["agent_id"])
    assert agent is not None
    assert agent["prompt"] == prep["stdin_text"]
    assert agent["prompt"].startswith("# Role: pr-reviewer-quality")
    assert len(agent["prompt"]) <= 20_000


def test_pr_review_agent_rejects_prompt_over_limit(tmp_path: Path) -> None:
    from agent_cli.coordinator_lanes import _prepare_pr_review_agent

    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = tmp_path / "wt-long-prompt"
    worktree.mkdir()
    task = {
        "id": "t-claude-long-prompt",
        "session_id": "worker-session",
        "models": {"first": "claude", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }

    with pytest.raises(CoordinatorError, match="exceeds 20000 characters"):
        _prepare_pr_review_agent(
            store,
            worker,
            task,
            role="pr-reviewer-quality",
            vendor="claude",
            stage="review-1",
            head="a" * 40,
            spec_body="x" * 20_000,
        )

    assert not store.rows("agent")


@pytest.mark.parametrize(("a38", "refused"), [(True, True), (False, False)])
def test_a38_pr_review_prompt_is_bounded_so_the_first_comment_fits(
    tmp_path: Path, a38: bool, refused: bool
) -> None:
    from dataclasses import replace

    from agent_cli.coordinator_lanes import _prepare_pr_review_agent

    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), a38=a38)
    worktree = tmp_path / "wt-a38-prompt"
    worktree.mkdir()
    task = {
        "id": "t-claude-a38-prompt",
        "session_id": "worker-session",
        "models": {"first": "claude", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }

    def prepare():
        return _prepare_pr_review_agent(
            store,
            worker,
            task,
            role="pr-reviewer-quality",
            vendor="claude",
            stage="review-1",
            head="a" * 40,
            spec_body="x" * 15_000,
        )

    if refused:
        with pytest.raises(CoordinatorError, match="exceeds 14000 JSON characters"):
            prepare()
        assert not store.rows("agent")
    else:
        prepare()
        assert store.rows("agent")


def test_sequential_claude_lane_records_usage_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import launch_lane

    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = worker.workspace_root / "seq-claude"
    worktree.mkdir()
    task = {
        "id": "aa000001-0000-4000-8000-0000000000e1",
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "implementing",
        "models": {"first": "claude", "second": "codex"},
        "payload": {"coordinator": {"worktree": str(worktree)}},
    }
    store.write("task", "insert", task["id"], task)
    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.verify_checkout_identity",
        lambda *args, **kwargs: {},
    )
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "a.py"}}
        ]}},
        {"type": "result", "result": "STATUS: complete\nRESULT: done",
         "usage": {"input_tokens": 12, "output_tokens": 5},
         "modelUsage": {"claude-model": {}}, "num_turns": 2, "duration_ms": 30},
    ]
    stream = "\n".join(json.dumps(event) for event in events) + "\n"
    _agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="claude",
        round_num=1,
        spec_body="Implement.",
        runner=lambda argv: Completed(0, "", ""),
        lane_runner=lambda argv, stdin: Completed(0, stream, ""),
    )
    assert result.usage is not None and result.usage.input_tokens == 12
    rows = [row for row in store.rows("activity") if row.get("type") == "lane.run"]
    assert len(rows) == 1
    assert rows[0]["payload"]["usage"]["output_tokens"] == 5
    assert rows[0]["payload"]["tools"] == [
        {"tool": "Read", "target": "a.py", "error": False}
    ]


def test_accepted_task_pins_models_and_phases_use_the_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    _claude_first_manifest(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    patch_run_bounded(monkeypatch, fake)
    tick(store, worker, runner=fake, lane_runner=lane_runner(fake))
    tasks = store.rows("task")
    assert tasks and all(t.get("models") == {"first": "claude", "second": "codex"} for t in tasks)

    # A later device change does not move what the accepted task runs on.
    from agent_cli import coordinator_lanes
    from agent_cli.coordinator_runtime import _first_slot_vendor

    path = store.home / "ai-accounts.json"
    data = json.loads(path.read_text())
    data["models"] = {"first": "grok", "second": "codex"}
    path.write_text(json.dumps(data))
    task = dict(tasks[0])
    assert _first_slot_vendor(task) == "claude"
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_lanes,
        "phase_pr_gates",
        lambda *a, vendor, stage, **k: seen.append((vendor, stage)) or [],
    )
    coordinator_lanes.phase_pr_gates_grok(store, worker, task, fake, None)
    coordinator_lanes.phase_pr_gates_codex(store, worker, task, fake, None)
    assert seen == [("claude", "review-1"), ("codex", "review-2")]



def test_readiness_uses_the_providers_pinned_on_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phase_readiness compares gate vendors with the task pin, not the device or fixed names."""
    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    pinned = {"first": "claude", "second": "codex"}
    task = _seed_ready_side_task(
        store, worker, fake, tid="aa000001-0000-4000-8000-0000000000c1", phase="readiness",
        checklist_codex={}, codex_gates=("quality", "logic"), models=pinned, first_vendor="claude",
    )
    lines = phase_readiness(store, worker, task, fake)
    assert any(f"readiness ok on {fake.head[:7]}" in line for line in lines)

    task = _seed_ready_side_task(
        store, worker, fake, tid="aa000001-0000-4000-8000-0000000000c2", phase="readiness",
        checklist_codex={}, codex_gates=("quality", "logic"), models=pinned, first_vendor="grok",
    )
    with pytest.raises(CoordinatorError, match="missing approved gate review-1/"):
        phase_readiness(store, worker, task, fake)




def test_first_slot_vendor_refuses_a_codex_pin() -> None:
    from agent_cli.coordinator_runtime import _first_slot_vendor

    with pytest.raises(CoordinatorError, match="models.first = codex"):
        _first_slot_vendor({"models": {"first": "codex", "second": "grok"}})
    with pytest.raises(CoordinatorError, match="separate implementer is not supported"):
        _first_slot_vendor(
            {
                "models": {"first": "grok", "second": "codex"},
                "implementer": "deepseek",
            }
        )
    with pytest.raises(CoordinatorError, match="task models is not configured"):
        _first_slot_vendor({})



def test_pr_gates_ignore_gates_from_another_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong-provider gate neither unlocks slot second nor counts as cached."""
    from agent_cli import coordinator_lanes

    store, worker, fake = _ready_side_env(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinator_lanes, "verify_checkout_identity", lambda *a, **k: None)
    monkeypatch.setattr(coordinator_lanes, "verify_signed_clean_head", lambda *a, **k: fake.head)
    pinned = {"first": "claude", "second": "codex"}
    task = _seed_ready_side_task(
        store, worker, fake, tid="aa000001-0000-4000-8000-0000000000d1", phase="pr_gates_codex",
        checklist_codex={}, models=pinned, first_vendor="grok",
    )
    with pytest.raises(CoordinatorError, match="review-2 requires approved review-1/"):
        coordinator_lanes.phase_pr_gates(store, worker, task, fake, None, vendor="codex", stage="review-2")

    task = _seed_ready_side_task(
        store, worker, fake, tid="aa000001-0000-4000-8000-0000000000d2", phase="pr_gates_grok",
        checklist_codex={}, models=pinned, first_vendor="grok",
    )
    launched: list[str] = []
    diff = tmp_path / "review.diff"
    diff.write_text("diff\n", encoding="utf-8")
    monkeypatch.setattr(coordinator_lanes, "write_review_diff", lambda *a, **k: diff)
    monkeypatch.setattr(coordinator_lanes, "write_review_context", lambda *a, **k: "")
    monkeypatch.setattr(
        coordinator_lanes, "_prepare_pr_review_agent",
        lambda *a, **k: launched.append(k["role"]) or (_ for _ in ()).throw(CoordinatorError("stop")),
    )
    with pytest.raises(CoordinatorError, match="stop"):
        coordinator_lanes.phase_pr_gates(store, worker, task, fake, None, vendor="claude", stage="review-1")
    assert launched  # grok gates on a claude-pinned task are not taken as cached
