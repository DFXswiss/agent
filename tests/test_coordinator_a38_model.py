from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli import preconditions
from agent_cli.a38_guard import status_context_enforce
from agent_cli.a38_review import (
    COMPLIANCE_PROMPT,
    REVIEW_BEGIN,
    mixes_report_and_review,
    parse_review_block,
    validate_declaration,
    validate_review_record,
    validate_visible,
)
from agent_cli.coordinator_a38 import (
    _checkpoint_report_context,
    _quoted_findings,
    _report_checkpoint_state,
    _render_review_comment,
    phase_await_ready,
    phase_first_comment,
    phase_second_comment,
    reconcile_a38_publication_intents,
    require_ready_evidence,
)
from agent_cli.coordinator_common import CoordinatorError
from agent_cli.coordinator_github import (
    _a38_second_review_outcome,
    _await_second_review_after_merge,
    phase_readiness,
)
from agent_cli.coordinator_lanes import invalidate_head_evidence, phase_pr_gates
from agent_cli.coordinator_runtime import (
    _create_implement_task,
    advance_one,
    execution_binding,
    phase_handlers,
    preflight_worker,
)
from agent_cli.local_ci import BEGIN_MARK, LocalCiReport, SCHEMA_ID, render_block
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator import (
    _a38_first_comment_env,
    _ready_side_env,
    _seed_ready_side_task,
    patch_account_runners,
    patch_execute_github,
    patch_run_bounded,
    seed_task,
)
from test_coordinator_support import (
    FakeGh,
    lane_runner,
    make_order_worker,
    make_session,
    make_worker,
    submit_order,
    write_accounts,
    write_check_lane,
)


HEAD = "a" * 40
FENCE = chr(96) * 3


def test_fresh_task_binds_the_measured_base_as_pending(tmp_path: Path) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    tid = "aa000001-0000-4000-8000-000000000301"
    base = "b" * 40
    store.write("task", "insert", tid, {
        "id": tid,
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "local-check",
        "payload": {"coordinator": {
            "head_sha": HEAD,
            "base_sha": base,
            "a38": {"enabled": True},
        }},
    })
    task = store.row("task", tid)
    assert task is not None

    _checkpoint_report_context(
        store,
        task,
        expected_state=_report_checkpoint_state(task),
        head=HEAD,
        base_sha=base,
    )

    saved = store.row("task", tid)
    assert saved is not None
    assert saved["payload"]["coordinator"]["a38"]["pending_base_sha"] == base


def test_promoted_base_is_not_bound_again(tmp_path: Path) -> None:
    store = Store(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    tid = "aa000001-0000-4000-8000-000000000302"
    base = "b" * 40
    store.write("task", "insert", tid, {
        "id": tid,
        "session_id": "worker-session",
        "workflow": "implement",
        "state": "local-check",
        "payload": {"coordinator": {
            "head_sha": HEAD,
            "base_sha": base,
            "a38": {"enabled": True, "base_sha": base},
        }},
    })
    task = store.row("task", tid)
    assert task is not None

    _checkpoint_report_context(
        store,
        task,
        expected_state=_report_checkpoint_state(task),
        head=HEAD,
        base_sha=base,
    )

    saved = store.row("task", tid)
    assert saved is not None
    assert "pending_base_sha" not in saved["payload"]["coordinator"]["a38"]


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _update(store: Store, table: str, row: dict[str, Any]) -> None:
    store.write(table, "update", str(row["id"]), _strip(row))


def _patch_gate_git(monkeypatch: pytest.MonkeyPatch, worker: Any, fake: FakeGh) -> None:
    def diff(_store, _worker, task, _runner, *, head):  # noqa: ANN001
        directory = worker.workspace_root / ".coordinator-control" / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"review-{head[:12]}.patch"
        path.write_text("diff\n", encoding="utf-8")
        path.with_suffix(".excerpt.txt").write_text("excerpt\n", encoding="utf-8")
        return path

    monkeypatch.setattr("agent_cli.coordinator_lanes.verify_checkout_identity", lambda *a, **k: {})
    monkeypatch.setattr("agent_cli.coordinator_lanes.verify_signed_clean_head", lambda *a, **k: fake.head)
    monkeypatch.setattr("agent_cli.coordinator_lanes.write_review_diff", diff)
    monkeypatch.setattr("agent_cli.coordinator_lanes.write_review_context", lambda *a, **k: "")


def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Store, Any, FakeGh]:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True, a38_second_review="model")
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    patch_run_bounded(monkeypatch, fake)
    patch_execute_github(monkeypatch)
    fake.pr["statusCheckRollup"] = [{"name": "tests", "conclusion": "success", "status": "completed"}]
    fake.workflow_runs = [{
        "id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request",
        "head_sha": fake.head, "status": "completed", "conclusion": "success", "run_attempt": 1,
    }]
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a, **k: fake.head)
    monkeypatch.setattr("agent_cli.coordinator_a38.review_rules_bound", lambda *a, **k: True)
    _patch_gate_git(monkeypatch, worker, fake)
    return store, worker, fake


def seed_model_task(
    store: Store,
    worker: Any,
    fake: FakeGh,
    tid: str,
    *,
    phase: str,
    second: dict[str, tuple[str, str | None]] | None = None,
    second_prompt: str | None = None,
    **a38_extra: Any,
) -> dict[str, Any]:
    from agent_cli.allow import CHECKLIST_KEYS

    worktree = worker.workspace_root / tid
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").mkdir(exist_ok=True)
    report_body = render_block(
        LocalCiReport(
            schema=SCHEMA_ID,
            repo="example/project",
            head=fake.head,
            private=False,
            recorded_at="2026-01-01T00:00:00Z",
            required=(),
            runs=(),
        )
    )
    task = {
        "id": tid, "session_id": worker.session_id, "workflow": "implement",
        "state": "pr-review", "title": "A38 model task", "repo": "example/project", "ref": "42",
        "models": {"first": "grok", "second": "codex"},
        "change_summary_en": "The coordinator runs the model second review.",
        "change_summary_de": "The coordinator runs the model second review.",
        "payload": {"coordinator": {
            "phase": phase,
            "source": {"repo": "example/project", "number": 7, "assigned_id": f"assigned-{tid}",
                       "publication_repo": "example/project", "base": "develop", "title": "Fix widget"},
            "publication_repo": "example/project", "worktree": str(worktree),
            "branch": f"task-{tid[:8]}", "base_sha": fake.base, "head_sha": fake.head,
            "pr_number": 42, "pr_url": "https://github.com/example/project/pull/42",
            "evidence": {"tests_pass": True, "tests_head": fake.head},
            "a38": {"enabled": True, "second_review_mode": "model", "base_sha": fake.base,
                    "policy_revision": fake.base,
                    "report": {"head": fake.head, "base_sha": fake.base,
                               "policy_revision": fake.base, "activity_id": "report",
                               "body": report_body},
                    "first_review_passes": 1, **a38_extra},
        }},
        "current_round": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    }
    seed_task(store, worker, tid, task)
    for stage, vendor, model in (("review-1", "grok", "grok-model"),):
        for dimension in ("quality", "logic"):
            aid = f"{tid}-{stage}-{dimension}-agent"
            gid = f"{tid}-{stage}-{dimension}"
            store.write("agent", "insert", aid, {
                "id": aid, "session_id": worker.session_id, "task_id": tid,
                "role": f"pr-reviewer-{dimension}", "vendor": vendor, "provider": vendor,
                "model": model, "prompt": COMPLIANCE_PROMPT + f" Check {dimension}.",
                "stage": stage, "head_sha": fake.head, "dimension": dimension, "status": "done",
            })
            store.write("review_gate", "insert", gid, {
                "id": gid, "task_id": tid, "stage": stage, "dimension": dimension,
                "vendor": vendor, "verdict": "approved", "evidence": None,
                "head_sha": fake.head, "agent_id": aid, "recorded_at": "2026-01-01T00:00:00Z",
            })
    for key in CHECKLIST_KEYS["implement"]:
        status = "n_a" if key in ("deviation_declared", "deviation_granted") else (
            "pending" if key in ("review_2_quality", "review_2_logic") else "ja"
        )
        cid = f"{tid}-check-{key}"
        store.write("checklist_item", "insert", cid, {
            "id": cid, "task_id": tid, "key": key, "status": status,
            "evidence": "seed" if status != "pending" else None,
            "source": "human" if key in ("spec_written", "deviation_declared", "deviation_granted") else "script",
            "deviation_declared": False, "deviation_granted": False, "granted_by": None,
            "updated_at": "2026-01-01T00:00:00Z",
        })
    store.write("local_check", "insert", f"{tid}-a38-check", {
        "id": f"{tid}-a38-check", "task_id": tid, "name": "a38", "command": "/operator/a38",
        "result": "pass", "output": "ok", "ran_at": "2026-01-01T00:00:00Z", "head_sha": fake.head,
    })
    for dimension in ("quality", "logic"):
        if not second or dimension not in second:
            continue
        verdict, evidence = second[dimension]
        aid = f"{tid}-review-2-{dimension}-agent"
        gid = f"{tid}-review-2-{dimension}"
        store.write("agent", "insert", aid, {
            "id": aid, "session_id": worker.session_id, "task_id": tid,
            "role": f"pr-reviewer-{dimension}", "vendor": "codex", "provider": "codex",
            "model": "codex-model", "prompt": second_prompt or COMPLIANCE_PROMPT + f" Check {dimension}.",
            "stage": "review-2", "head_sha": fake.head, "dimension": dimension, "status": "done",
        })
        store.write("review_gate", "insert", gid, {
            "id": gid, "task_id": tid, "stage": "review-2", "dimension": dimension,
            "vendor": "codex", "verdict": verdict, "evidence": evidence,
            "head_sha": fake.head, "agent_id": aid, "recorded_at": "2026-01-01T00:00:01Z",
        })
    saved = store.row("task", tid)
    assert saved is not None
    return saved


def one_lane(fake: FakeGh, text: str):
    def run(argv: list[str], stdin: str | None = None) -> Completed:
        joined = " ".join(argv) + "\n" + (stdin or "")
        role = next(name for name in ("pr-reviewer-quality", "pr-reviewer-logic") if name in joined)
        fake.launched.append(role)
        Path(argv[argv.index("--output-last-message") + 1]).write_text(text, encoding="utf-8")
        return Completed(0, "", "")
    return run


def comments(store: Store) -> list[dict[str, Any]]:
    return [row for row in store.rows("activity") if row.get("type") == "comment.post"]


def checkpoint(store: Store, tid: str) -> dict[str, Any]:
    task = store.row("task", tid)
    assert task is not None
    return task["payload"]["coordinator"]


def _gates(store: Store, tid: str) -> list[dict[str, Any]]:
    return [row for row in store.rows("review_gate") if row.get("task_id") == tid and row.get("stage") == "review-2"]


def _validate(body: str, head: str) -> dict[str, Any]:
    declaration = parse_review_block(body)
    assert validate_declaration(declaration, head=head, markdown_only=False, rules_bound=True) == []
    assert validate_visible(body, int(declaration["passes"])) == []
    assert validate_review_record(body, declaration, rules_bound=True) == []
    return declaration


def test_preflight_requires_models_second_for_model_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    manifest = store.home / "ai-accounts.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["models"]["second"] = None
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CoordinatorError, match="A38 workers with a38_second_review = model require models.second"):
        preflight_worker(store, worker, fake)
    write_accounts(store.home)
    preflight_worker(store, worker, fake)
    external = replace(worker, a38_second_review="external", second_reviewers=("outside-reviewer",))
    with pytest.raises(CoordinatorError, match="A38 workers require models.second = null"):
        preflight_worker(store, external, fake)


def test_created_task_pins_the_model_mode(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = replace(make_worker(tmp_path), review_session=None, a38=True, a38_second_review="model")
    task = _create_implement_task(
        store, worker, repo="example/project", number=7, title="Fix widget",
        assigned_id="assigned-7", repository_config=worker.repositories["example/project"], body="Please fix",
    )
    assert task is not None
    assert task["payload"]["coordinator"]["a38"] == {"enabled": True, "second_review_mode": "model"}
    assert task["models"] == {"first": "grok", "second": "codex"}


def test_order_task_pins_the_model_mode(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    write_check_lane(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    manifest = store.home / "ai-accounts.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["models"]["second"] = None
    manifest.write_text(json.dumps(data), encoding="utf-8")
    worker = make_order_worker(
        tmp_path, review_session=None, a38=True, a38_second_review="model"
    )
    with pytest.raises(CoordinatorError, match="require models.second"):
        submit_order(store, worker)
    assert store.rows("task") == []

    data["models"]["second"] = "codex"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    task = submit_order(store, worker)
    assert task["payload"]["coordinator"]["a38"] == {
        "enabled": True,
        "second_review_mode": "model",
    }
    assert task["models"] == {"first": "grok", "second": "codex"}


def test_binding_hash_unchanged_without_new_options(tmp_path: Path) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    altered = replace(worker, park_poll_seconds=5, lane_retries=7, max_rounds=3,
                      max_rounds_by_model={"grok-model": 4}, orders=True, draft_on_accept=True)
    assert execution_binding(store, worker, "example/project") == execution_binding(store, altered, "example/project")
    model = replace(worker, review_session=None, a38=True, a38_second_review="model")
    assert execution_binding(store, model, "example/project") != execution_binding(
        store, replace(model, a38_second_review="external"), "example/project"
    )


def test_first_comment_of_a_model_task_goes_to_the_second_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "10000000-0000-4000-8000-000000000001"
    task = seed_model_task(store, worker, fake, tid, phase="a38_first_comment")
    assert phase_first_comment(store, worker, task, fake) == [f"first A38 review comment posted on {fake.head[:7]}"]
    c = checkpoint(store, tid)
    assert c["phase"] == "pr_gates_codex" and len(comments(store)) == 1 and store.rows("ping") == []
    assert "second_review" not in c["a38"]
    first = c["a38"]["first_comment"]
    assert first["body"] and first["passes"] == 1 and first["url"] == "https://x" and "request_key" not in first
    c["phase"] = "a38_first_comment"
    assert phase_first_comment(store, worker, store.row("task", tid), fake) == [
        f"first A38 review comment already posted on {fake.head[:7]}"
    ]
    assert len(comments(store)) == 1


def test_reconcile_posts_a_saved_first_comment_of_a_model_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "10000000-0000-4000-8000-000000000002"
    task = seed_model_task(store, worker, fake, tid, phase="a38_first_comment")
    monkeypatch.setattr("agent_cli.coordinator_a38.execute_github", lambda *a, **k: [])
    with pytest.raises(CoordinatorError, match="A38 review comment was not verified published"):
        phase_first_comment(store, worker, task, fake)
    first = checkpoint(store, tid)["a38"]["first_comment"]
    assert store.row("activity", first["activity_id"])["execution_status"] == "pending"
    patch_execute_github(monkeypatch)
    assert reconcile_a38_publication_intents(store, worker, store.row("task", tid), fake) == [
        f"first A38 review comment reconciled on {fake.head[:7]}"
    ]
    assert store.row("activity", first["activity_id"])["execution_status"] == "done"
    assert store.rows("ping") == []
    assert reconcile_a38_publication_intents(store, worker, store.row("task", tid), fake) == []


def test_approved_second_pair_goes_to_the_second_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "20000000-0000-4000-8000-000000000001"
    task = seed_model_task(store, worker, fake, tid, phase="pr_gates_codex")
    phase_pr_gates(store, worker, task, fake, lane_runner(fake), vendor="codex", stage="review-2")
    gates = _gates(store, tid)
    assert {g["dimension"] for g in gates if g["verdict"] == "approved"} == {"quality", "logic"}
    checklist = {r["key"]: r["status"] for r in store.rows("checklist_item") if r.get("task_id") == tid}
    assert checklist["review_2_quality"] == checklist["review_2_logic"] == "ja"
    c = checkpoint(store, tid)
    assert c["a38"]["second_review_passes"] == 1 and c["phase"] == "a38_second_comment"
    assert not [r for r in store.rows("activity") if r.get("type") == "review.post"]


def test_rejected_second_pair_posts_no_plain_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "20000000-0000-4000-8000-000000000002"
    task = seed_model_task(store, worker, fake, tid, phase="pr_gates_codex")
    fake.model_outputs["pr-reviewer-quality"] = "STATUS: complete\nRESULT: rejected\nQ-FINDING in the widget\n"
    lines = phase_pr_gates(store, worker, task, fake, lane_runner(fake), vendor="codex", stage="review-2")
    assert {g["verdict"] for g in _gates(store, tid)} == {"approved", "rejected"}
    assert not [r for r in store.rows("activity") if r.get("type") == "review.post"]
    c = checkpoint(store, tid)
    assert "rejection_review_activity_ids" not in c and not c.get("handled_rejected_gate_ids")
    assert c["phase"] == "a38_second_comment" and c["a38"]["second_review_passes"] == 1
    assert f"review-2/quality rejected on {fake.head[:7]}" in lines


def test_rejection_waits_for_an_incomplete_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "20000000-0000-4000-8000-000000000003"
    task = seed_model_task(store, worker, fake, tid, phase="pr_gates_codex")
    fake.model_outputs["pr-reviewer-quality"] = "STATUS: complete\nRESULT: rejected\nQ-FINDING\n"
    fake.model_outputs["pr-reviewer-logic"] = "STATUS: unavailable\n"
    phase_pr_gates(store, worker, task, fake, lane_runner(fake), vendor="codex", stage="review-2")
    c = checkpoint(store, tid)
    assert c["phase"] == "blocked" and c["resume_phase"] == "pr_gates_codex"
    assert "review unavailable/incomplete" in c["blocker"] and "second_review_passes" not in c["a38"]
    gates = _gates(store, tid)
    assert {g["dimension"]: g["verdict"] for g in gates} == {"quality": "rejected", "logic": "unavailable"}
    quality_id = next(g["id"] for g in gates if g["dimension"] == "quality")
    assert not [r for r in store.rows("activity") if r.get("type") == "review.post"]
    assert not [r for r in comments(store) if REVIEW_BEGIN in str(r.get("payload", {}).get("body") or "")]
    saved = store.row("task", tid)
    saved["payload"]["coordinator"]["phase"] = "pr_gates_codex"
    saved["payload"]["coordinator"].pop("resume_phase", None)
    _update(store, "task", saved)
    fake.launched.clear()
    lines = phase_pr_gates(store, worker, store.row("task", tid), fake,
                           one_lane(fake, "STATUS: complete\nRESULT: approved\n"), vendor="codex", stage="review-2")
    assert fake.launched == ["pr-reviewer-logic"] and "review-2/quality=rejected (kept)" in lines
    assert [g["id"] for g in _gates(store, tid) if g["dimension"] == "quality"] == [quality_id]
    c = checkpoint(store, tid)
    assert c["a38"]["second_review_passes"] == 1 and c["phase"] == "a38_second_comment"


def test_recorded_pair_in_the_gate_phase_goes_to_the_second_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "20000000-0000-4000-8000-000000000004"
    task = seed_model_task(store, worker, fake, tid, phase="pr_gates_codex",
                           second={"quality": ("approved", None), "logic": ("approved", None)})
    def no_lane(*_a, **_k):  # noqa: ANN202
        pytest.fail("a recorded pair must not launch a lane")
    assert phase_pr_gates(store, worker, task, fake, no_lane, vendor="codex", stage="review-2") == [
        f"review-2 already complete on {fake.head[:7]}"
    ]
    assert checkpoint(store, tid)["phase"] == "a38_second_comment"
    tid = "20000000-0000-4000-8000-000000000005"
    task = seed_model_task(store, worker, fake, tid, phase="pr_gates_codex",
                           second={"quality": ("rejected", "Q-FINDING"), "logic": ("approved", None)})
    assert phase_pr_gates(store, worker, task, fake, no_lane, vendor="codex", stage="review-2") == [
        "review-2/quality=rejected (kept)", "review-2/logic=approved (cached)"
    ]
    assert checkpoint(store, tid)["phase"] == "a38_second_comment"
    assert not [r for r in store.rows("activity") if r.get("type") == "review.post"]


def test_second_comment_is_rendered_by_the_guard_renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000001"
    task = seed_model_task(
        store, worker, fake, tid, phase="a38_second_comment",
        second={"quality": ("approved", None), "logic": ("approved", None)}, second_review_passes=1,
    )
    assert phase_second_comment(store, worker, task, fake) == [f"second A38 review comment posted on {fake.head[:7]}"]
    rows = comments(store)
    assert len(rows) == 1 and rows[0]["payload"]["target"] == "pr"
    body = rows[0]["payload"]["body"]
    assert body.startswith("EN:\nSecond review with codex-model\nNo defects.")
    assert "conformity-b" in body and "logic-b" in body
    assert '"blocks_merge":false' in body and '"defects":0' in body and "Second review by @" not in body
    declaration = _validate(body, fake.head)
    assert {lane["id"] for lane in declaration["lanes"]} == {"conformity-b", "logic-b"}
    c = checkpoint(store, tid)
    assert c["a38"]["second_comment"] == {
        "head": fake.head, "activity_id": rows[0]["id"], "id": 99, "url": "https://x", "blocks_merge": False,
    }
    assert c["phase"] == "ci"


def test_rejected_second_pair_posts_a_blocking_comment_and_routes_to_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000002"
    task = seed_model_task(
        store, worker, fake, tid, phase="a38_second_comment",
        second={"quality": ("rejected", "Q-FINDING in the widget"), "logic": ("approved", None)},
        first_comment={"head": fake.head},
    )
    assert phase_second_comment(store, worker, task, fake) == [
        f"blocking second A38 review comment posted on {fake.head[:7]}; routing to implementer"
    ]
    body = comments(store)[0]["payload"]["body"]
    assert "There are defects that must be fixed." in body
    assert '"blocks_merge":true' in body and '"defects":1' in body and "> Q-FINDING in the widget" in body
    _validate(body, fake.head)
    saved = store.row("task", tid)
    c = saved["payload"]["coordinator"]
    assert c["findings"] == "Quality review findings:\nQ-FINDING in the widget"
    assert c["phase"] == "implement" and saved["state"] == "implementing"
    assert c["a38"]["second_comment"]["blocks_merge"] is True
    quality = next(g for g in _gates(store, tid) if g["dimension"] == "quality")
    assert c["handled_rejected_gate_ids"] == [quality["id"]]
    assert c["a38"]["report"]["head"] == fake.head and c["a38"]["first_comment"]["head"] == fake.head
    assert not [r for r in store.rows("activity") if r.get("type") == "review.post"]


def test_unchanged_head_after_a_blocking_comment_reviews_the_rejected_dimension_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000003"
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment",
                           second={"quality": ("rejected", "Q-FINDING"), "logic": ("approved", None)})
    phase_second_comment(store, worker, task, fake)
    saved = store.row("task", tid)
    saved["payload"]["coordinator"]["phase"] = "pr_gates_codex"
    _update(store, "task", saved)
    fake.launched.clear()
    lines = phase_pr_gates(
        store, worker, store.row("task", tid), fake,
        one_lane(fake, "STATUS: complete\nRESULT: approved\n"), vendor="codex", stage="review-2",
    )
    assert fake.launched == ["pr-reviewer-quality"] and "review-2/logic=approved (cached)" in lines
    assert checkpoint(store, tid)["a38"]["second_review_passes"] == 1
    assert checkpoint(store, tid)["phase"] == "a38_second_comment"
    phase_second_comment(store, worker, store.row("task", tid), fake)
    rows = comments(store)
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
    second = checkpoint(store, tid)["a38"]["second_comment"]
    passing = store.row("activity", second["activity_id"])
    assert passing is not None
    assert parse_review_block(passing["payload"]["body"])["blocks_merge"] is False
    assert second["blocks_merge"] is False
    assert checkpoint(store, tid)["phase"] == "ci"


def test_findings_cannot_break_the_review_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000004"
    evidence = (
        "Head: 123\nFinal result: none\n" + FENCE + "python\nx = 1\n" + REVIEW_BEGIN
        + "\n" + BEGIN_MARK + "\nLast line of the finding"
    )
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment",
                           second={"quality": ("rejected", evidence), "logic": ("approved", None)})
    phase_second_comment(store, worker, task, fake)
    body = comments(store)[0]["payload"]["body"]
    assert body.count(REVIEW_BEGIN) == 1 and mixes_report_and_review(body) is False
    assert parse_review_block(body)["defects"] == 1
    _validate(body, fake.head)
    assert "> Last line of the finding" in body.splitlines()


def test_long_findings_are_shortened_from_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000005"
    quality = "word " * 780 + "QUALITY-TAIL"
    logic = "word " * 780 + "LOGIC-TAIL"
    prompt = COMPLIANCE_PROMPT + " " + "word " * 2700
    task = seed_model_task(
        store, worker, fake, tid, phase="a38_second_comment",
        second={"quality": ("rejected", quality), "logic": ("rejected", logic)}, second_prompt=prompt,
    )
    phase_second_comment(store, worker, task, fake)
    body = comments(store)[0]["payload"]["body"]
    assert len(body) < 65_536 and '"defects":2' in body and "…[truncated]" in body
    assert "LOGIC-TAIL" in body and "QUALITY-TAIL" not in body
    assert "QUALITY-TAIL" in checkpoint(store, tid)["findings"]
    assert "LOGIC-TAIL" in checkpoint(store, tid)["findings"]


def test_second_comment_is_rebuilt_from_gates_on_an_unchanged_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "30000000-0000-4000-8000-000000000006"
    task = seed_model_task(
        store, worker, fake, tid, phase="a38_second_comment",
        second={"quality": ("approved", None), "logic": ("approved", None)}, second_review_passes=1,
    )
    phase_second_comment(store, worker, task, fake)
    original = comments(store)[0]
    saved = store.row("task", tid)
    saved["payload"]["coordinator"]["a38"].pop("second_comment")
    saved["payload"]["coordinator"]["phase"] = "a38_second_comment"
    saved["change_summary_en"] = "A later implementer rewrote this summary."
    _update(store, "task", saved)
    phase_second_comment(store, worker, store.row("task", tid), fake)
    rows = comments(store)
    assert len(rows) == 1 and rows[0]["payload"]["body"] == original["payload"]["body"]
    assert checkpoint(store, tid)["a38"]["second_comment"]["activity_id"] == original["id"]
    assert checkpoint(store, tid)["phase"] == "ci"


@pytest.mark.parametrize("case", ("missing", "other-head", "other-vendor", "unavailable", "missing-prompt"))
def test_second_comment_returns_to_the_gates_when_a_gate_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = f"40000000-0000-4000-8000-{len(case):012d}"
    second = {"quality": ("approved", None)}
    if case != "missing":
        second["logic"] = ("approved", None)
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment", second=second)
    if case != "missing":
        gate = next(g for g in _gates(store, tid) if g["dimension"] == "logic")
        if case == "other-head":
            gate["head_sha"] = "b" * 40
            _update(store, "review_gate", gate)
        elif case == "other-vendor":
            gate["vendor"] = "grok"
            _update(store, "review_gate", gate)
        elif case == "unavailable":
            gate["verdict"] = "unavailable"
            _update(store, "review_gate", gate)
        else:
            agent = store.row("agent", gate["agent_id"])
            agent["prompt"] = None
            _update(store, "agent", agent)
    assert phase_second_comment(store, worker, task, fake) == [
        f"second review logic gate missing on {fake.head[:7]}; re-running gate"
    ]
    assert checkpoint(store, tid)["phase"] == "pr_gates_codex" and comments(store) == []


def test_second_comment_follows_a_changed_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "40000000-0000-4000-8000-000000000006"
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment",
                           second={"quality": ("approved", None), "logic": ("approved", None)})
    changed = "e" * 40
    monkeypatch.setattr("agent_cli.coordinator_a38.verify_signed_clean_head", lambda *a, **k: changed)
    assert phase_second_comment(store, worker, task, fake) == ["head changed before second A38 review comment"]
    c = checkpoint(store, tid)
    assert c["phase"] == "tests" and c["head_sha"] == changed and "report" not in c["a38"]


def test_second_comment_requires_the_model_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "40000000-0000-4000-8000-000000000007"
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment")
    task["payload"]["coordinator"]["a38"] = {"enabled": True}
    with pytest.raises(CoordinatorError, match="A38 second-comment phase requires the model second review"):
        phase_second_comment(store, worker, task, fake)
    tid = "40000000-0000-4000-8000-000000000008"
    task = seed_model_task(store, worker, fake, tid, phase="a38_second_comment",
                           second={"quality": ("approved", None), "logic": ("approved", None)})
    gate = next(g for g in _gates(store, tid) if g["dimension"] == "logic")
    agent = store.row("agent", gate["agent_id"])
    agent["model"] = "another-model"
    _update(store, "agent", agent)
    with pytest.raises(CoordinatorError, match="second review lanes do not share one model"):
        phase_second_comment(store, worker, task, fake)


def test_readiness_requires_the_second_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "50000000-0000-4000-8000-000000000001"
    task = seed_model_task(store, worker, fake, tid, phase="readiness", first_comment={"head": fake.head})
    with pytest.raises(CoordinatorError, match=f"A38 second_comment evidence is missing on {fake.head[:7]}"):
        require_ready_evidence(store, task, fake.head, model_second=True)
    a38 = task["payload"]["coordinator"]["a38"]
    a38["second_comment"] = {"head": fake.head, "blocks_merge": True}
    with pytest.raises(CoordinatorError, match="model second review blocks the merge"):
        require_ready_evidence(store, task, fake.head, model_second=True)
    a38["second_comment"]["blocks_merge"] = False
    assert require_ready_evidence(store, task, fake.head, model_second=True) is True
    with pytest.raises(CoordinatorError, match=f"A38 second_review evidence is missing on {fake.head[:7]}"):
        require_ready_evidence(store, task, fake.head)


def test_readiness_of_a_model_task_requires_all_four_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ordinary, fake = _ready_side_env(tmp_path, monkeypatch)
    worker = replace(ordinary, review_session=None, a38=True, a38_second_review="model")

    def ready_task(tid: str, codex: tuple[str, ...]) -> dict[str, Any]:
        task = _seed_ready_side_task(
            store, worker, fake, tid=tid, phase="readiness",
            checklist_codex={"review_2_quality": "ja", "review_2_logic": "ja"},
            codex_gates=codex, models={"first": "grok", "second": "codex"},
        )
        task["payload"]["coordinator"]["a38"] = {
            "enabled": True, "second_review_mode": "model", "base_sha": fake.base,
            "policy_revision": fake.base,
            "report": {"head": fake.head, "base_sha": fake.base, "policy_revision": fake.base},
            "first_comment": {"head": fake.head},
            "second_comment": {"head": fake.head, "blocks_merge": False},
        }
        _update(store, "task", task)
        return store.row("task", tid)

    passing = ready_task("50000000-0000-4000-8000-000000000002", ("quality", "logic"))
    assert "readiness ok" in phase_readiness(store, worker, passing, fake)[0]
    assert checkpoint(store, passing["id"])["phase"] == "await_ready"
    missing = ready_task("50000000-0000-4000-8000-000000000003", ())
    with pytest.raises(CoordinatorError, match="missing approved gate review-2/quality"):
        phase_readiness(store, worker, missing, fake)


def test_guard_ready_is_observed_for_a_model_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "50000000-0000-4000-8000-000000000004"
    task = seed_model_task(
        store, worker, fake, tid, phase="await_ready",
        first_comment={"head": fake.head}, second_comment={"head": fake.head, "blocks_merge": False},
    )
    fake.pr["isDraft"] = False
    fake.statuses = [{"context": status_context_enforce("develop"), "state": "success", "description": "A38 passed"}]
    assert "awaiting human merge" in phase_await_ready(store, worker, task, fake)[0]
    c = checkpoint(store, tid)
    assert c["evidence"]["ready_head"] == fake.head and c["phase"] == "await_merge"
    blocked = seed_model_task(
        store, worker, fake, "50000000-0000-4000-8000-000000000005", phase="await_ready",
        first_comment={"head": fake.head}, second_comment={"head": fake.head, "blocks_merge": True},
    )
    with pytest.raises(CoordinatorError, match="model second review blocks the merge"):
        phase_await_ready(store, worker, blocked, fake)


@pytest.mark.no_pg
def test_second_review_outcome_of_a_model_task() -> None:
    c = {"a38": {"enabled": True, "second_review_mode": "model"}}
    assert _a38_second_review_outcome(c, HEAD) == "missing"
    c["a38"]["second_comment"] = {"head": "b" * 40, "blocks_merge": False}
    assert _a38_second_review_outcome(c, HEAD) == "missing"
    c["a38"]["second_comment"] = {"head": HEAD, "blocks_merge": True}
    assert _a38_second_review_outcome(c, HEAD) == "blocking"
    c["a38"]["second_comment"] = {"head": HEAD, "blocks_merge": False}
    assert _a38_second_review_outcome(c, HEAD) == "passed"


def test_model_task_merged_without_second_comment_blocks_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_github

    store, worker, fake = env(tmp_path, monkeypatch)
    first = seed_model_task(store, worker, fake, "60000000-0000-4000-8000-000000000001", phase="await_merge")
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_github, "publish_blocker",
        lambda _s, _w, _t, _r, message, *, kind="blocker", **_kw: (
            published.append((kind, message)) or [f"blocker {kind}"]
        ),
    )
    assert _await_second_review_after_merge(store, worker, first, fake, fake.head, outcome="missing") == [
        "blocker merged-without-second-review"
    ]
    assert published[-1] == (
        "merged-without-second-review",
        f"Pull request was merged on {fake.head[:7]} before the model second review was posted.",
    )
    c = checkpoint(store, first["id"])
    assert c["phase"] == "blocked" and "resume_phase" not in c and "merged_waiting_since" not in c["a38"]
    assert c["blocker"] == f"merged before the model second review was posted on {fake.head}"
    second = seed_model_task(store, worker, fake, "60000000-0000-4000-8000-000000000002", phase="await_merge")
    assert _await_second_review_after_merge(store, worker, second, fake, fake.head, outcome="blocking") == [
        "blocker merged-with-blocking-second-review"
    ]
    assert published[-1] == (
        "merged-with-blocking-second-review",
        f"Pull request was merged on {fake.head[:7]} although the model second review blocks the merge.",
    )
    assert checkpoint(store, second["id"])["blocker"] == (
        f"merged although the model second review blocks the merge on {fake.head}"
    )


def test_merge_reconcile_restores_a_posted_second_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "60000000-0000-4000-8000-000000000004"
    task = seed_model_task(
        store, worker, fake, tid, phase="a38_second_comment",
        second={"quality": ("approved", None), "logic": ("approved", None)}, second_review_passes=1,
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.execute_github", lambda *a, **k: [])
    with pytest.raises(CoordinatorError, match="A38 review comment was not verified published"):
        phase_second_comment(store, worker, task, fake)
    rows = comments(store)
    assert len(rows) == 1 and rows[0]["execution_status"] == "pending"
    assert "second_comment" not in checkpoint(store, tid)["a38"]
    patch_execute_github(monkeypatch)
    assert reconcile_a38_publication_intents(store, worker, store.row("task", tid), fake) == [
        f"second A38 review comment reconciled on {fake.head[:7]}"
    ]
    c = checkpoint(store, tid)
    assert c["a38"]["second_comment"] == {
        "head": fake.head, "activity_id": rows[0]["id"], "id": 99, "url": "https://x", "blocks_merge": False,
    }
    assert store.row("activity", rows[0]["id"])["execution_status"] == "done"
    assert _a38_second_review_outcome(c, fake.head) == "passed"
    assert reconcile_a38_publication_intents(store, worker, store.row("task", tid), fake) == []
    assert len(comments(store)) == 1


def test_unreadable_pull_request_state_retries_the_second_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime

    store, worker, fake = env(tmp_path, monkeypatch)
    tid = "60000000-0000-4000-8000-000000000003"
    seed_model_task(store, worker, fake, tid, phase="a38_second_comment",
                    second={"quality": ("approved", None), "logic": ("approved", None)})
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: None)
    assert advance_one(store, worker, runner=fake, lane_runner=None) == [
        "PR state temporarily unavailable before a38_second_comment; retrying"
    ]
    assert checkpoint(store, tid)["phase"] == "a38_second_comment"
    monkeypatch.setattr(coordinator_runtime, "pr_state", lambda *a, **k: "MERGED")
    advance_one(store, worker, runner=fake, lane_runner=None)
    assert checkpoint(store, tid)["phase"] == "await_merge"


def test_external_route_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, worker, fake, tid = _a38_first_comment_env(tmp_path, monkeypatch)
    assert "first A38 review comment posted" in phase_first_comment(store, worker, store.row("task", tid), fake)[0]
    c = checkpoint(store, tid)
    assert len(store.rows("ping")) == 1 and c["phase"] == "await_second_review"
    assert "request_key" in c["a38"]["first_comment"]


@pytest.mark.no_pg
def test_invalidate_head_evidence_drops_a_second_comment_of_another_head() -> None:
    c = {"a38": {"enabled": True, "second_comment": {"head": HEAD}}}
    invalidate_head_evidence(c, HEAD)
    assert c["a38"]["second_comment"]["head"] == HEAD
    invalidate_head_evidence(c, "b" * 40)
    assert "second_comment" not in c["a38"]


def _declaration() -> dict[str, Any]:
    lanes = [{
        "id": lane_id, "provider": "OpenAI", "model": "Reviewer Model 2",
        "model_number": "reviewer-model-2", "prompt": COMPLIANCE_PROMPT,
        "status": "complete", "result": "pass",
    } for lane_id in ("conformity-b", "logic-b")]
    return {
        "schema": "a38-review/v2", "head": HEAD, "passes": 1, "defects": 0,
        "set_aside": "none", "blocks_merge": False, "lanes": lanes,
    }


@pytest.mark.no_pg
def test_render_without_a_reviewer_omits_the_reviewer_line() -> None:
    task = {
        "change_summary_en": "The model second review is complete.",
        "change_summary_de": "The model second review is complete.",
    }
    declaration = _declaration()
    body = _render_review_comment(task, declaration, rules_bound=True, reviewer=None)
    assert "Second review by @" not in body
    assert validate_declaration(declaration, head=HEAD, markdown_only=False, rules_bound=True) == []
    assert validate_visible(body, 1) == []
    assert validate_review_record(body, declaration, rules_bound=True) == []
    with_reviewer = _render_review_comment(task, declaration, rules_bound=True, reviewer="reviewer")
    assert "Second review by @reviewer." in with_reviewer


@pytest.mark.no_pg
def test_quoted_findings() -> None:
    assert _quoted_findings("first\n\n" + FENCE + "\n<!-- x -->\nHead: y", limit=400) == (
        "> first\n>\n> " + FENCE + "\n> <!- - x -->\n> Head: y"
    )
    shortened = _quoted_findings("word " * 2000 + "END", limit=400)
    assert len(shortened) <= 400 and "…[truncated]" in shortened and shortened.endswith("END")
    assert all(line.startswith(">") for line in shortened.splitlines())


@pytest.mark.no_pg
def test_handler_map_covers_every_phase() -> None:
    assert set(phase_handlers(None, None, {}, None, None)) | {"queued"} == set(preconditions.PHASES)
