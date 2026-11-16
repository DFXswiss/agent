"""Task-text, tail-redaction, and combined-review coordinator coverage."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli.coordinator import tick
from agent_cli.lane import LaneResult
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_coordinator_flow import seed_task
from test_coordinator_support import (
    FakeGh,
    grok_messages_stream,
    lane_runner,
    make_session,
    make_worker,
    patch_account_runners,
    patch_command_runner,
    scan_done,
    write_accounts,
)


def _implemented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.issues[0]["body"] = body
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = lane_runner(fake)

    tick(store, worker, runner=fake, lane_runner=lane)
    tick(store, worker, runner=fake, lane_runner=lane)
    for _ in range(3):
        tasks = store.rows("task")
        if tasks and tasks[0]["payload"]["coordinator"]["phase"] == "implement":
            break
        tick(store, worker, runner=fake, lane_runner=lane)
    task = store.rows("task")[0]
    assert task["payload"]["coordinator"]["phase"] == "implement"
    fake.dirty = True
    tick(store, worker, runner=fake, lane_runner=lane)
    return store, worker, store.rows("task")[0]


def _gate_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quality: str,
    logic: str,
    *,
    lane_results: dict[str, Completed] | None = None,
    lane_stderr: dict[str, str] | None = None,
    passes: int = 1,
):
    """Run review-1 gate steps and return the last step's lines.

    ``lane_results`` replaces a role's process result. ``lane_stderr`` keeps a
    role's ordinary process result and replaces only its stderr. ``passes``
    repeats the step, for example to recover a checkpoint that failed in the
    first one.
    """
    from agent_cli.coordinator_git import _write_marker
    from agent_cli.coordinator_lanes import phase_pr_gates

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_worker(tmp_path)
    fake = FakeGh()
    fake.model_outputs["pr-reviewer-quality"] = quality
    fake.model_outputs["pr-reviewer-logic"] = logic
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    tid = "99999999-9999-4999-8999-999999999997"
    worktree = worker.workspace_root / tid
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
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
            "models": {"first": "grok", "second": "codex"},
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
                    "worktree": str(worktree),
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
    branch = "task-99999999"
    fake.remotes = {
        "origin": "https://github.com/example/project.git",
        "publication": "https://github.com/example/project.git",
    }
    fake.branch = branch
    fake.branches.add(branch)
    _write_marker(
        worker,
        tid,
        {
            "phase": "ready",
            "task_id": tid,
            "session_id": worker.session_id,
            "target_repo": "example/project",
            "publication_repo": "example/project",
            "branch": branch,
            "base": "develop",
            "base_sha": fake.base,
            "worktree": str(worktree),
            "worker_login": "worker-bot",
        },
    )

    def complete_saved_intents(store_, _runner, *, activity_ids):  # noqa: ANN001
        for activity_id in activity_ids:
            row = store_.row("activity", activity_id)
            assert row is not None
            updated = {
                key: value for key, value in row.items() if not key.startswith("_")
            }
            updated["execution_status"] = "done"
            store_.write("activity", "update", activity_id, updated)
        return []

    monkeypatch.setattr(
        "agent_cli.coordinator_lanes.execute_github", complete_saved_intents
    )
    ordinary = lane_runner(fake)

    def lane(argv: list[str], stdin: str | None = None) -> Completed:
        # The fake runner still runs for every role so both parallel
        # reviewers meet at its barrier.
        completed = ordinary(argv, stdin)
        material = " ".join(argv) + "\n" + (stdin or "")
        for role, replacement in (lane_results or {}).items():
            if role in material:
                return replacement
        for role, stderr in (lane_stderr or {}).items():
            if role in material:
                return replace(completed, stderr=stderr)
        return completed

    lines: list[str] = []
    for _ in range(passes):
        lines = phase_pr_gates(
            store,
            worker,
            store.row("task", tid),
            fake,
            lane,
            vendor="grok",
            stage="review-1",
        )
    return store, tid, lines


@pytest.mark.no_pg
def test_redact_tail_keeps_the_end() -> None:
    from agent_cli.coordinator_common import redact_tail

    text = "head\n" + "x" * 5000 + "\nTAIL-MARKER"
    shortened = redact_tail(text, limit=200)
    assert shortened.startswith("…[truncated]\n")
    assert shortened.endswith("TAIL-MARKER")
    assert len(shortened) <= 200
    assert redact_tail("short text") == "short text"
    secret = redact_tail("see ghp_" + "a" * 40)
    assert "[redacted]" in secret
    assert "ghp_" not in secret
    assert redact_tail("x" * 100, limit=20) == "…[truncated]\n"


@pytest.mark.no_pg
def test_redact_tail_masks_before_it_cuts() -> None:
    from agent_cli.coordinator_common import redact_tail

    text = "x" * 100 + " sk-" + "A" * 60 + " " + "y" * 150
    shortened = redact_tail(text, limit=200)
    assert "[redacted]" in shortened
    assert "A" not in shortened


@pytest.mark.no_pg
def test_write_and_read_task_text(tmp_path: Path) -> None:
    from agent_cli.coordinator_common import read_task_text, write_task_text

    worker = make_worker(tmp_path)
    text = "Fix the widget.\nSecond line."
    digest, length = write_task_text(worker, "task-1", text)
    path = worker.workspace_root / ".coordinator-control" / "task-1" / "task-text.md"
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert length == 28
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".tmp").exists()
    task = {
        "id": "task-1",
        "payload": {"coordinator": {"source": {"body": "preview"}}},
    }
    assert read_task_text(worker, task) == text
    task["id"] = "task-2"
    assert read_task_text(worker, task) == "preview"
    task["id"] = "task-1"
    path.write_bytes(b"")
    assert read_task_text(worker, task) == "preview"


@pytest.mark.no_pg
def test_task_text_is_stored_masked_and_bounded(tmp_path: Path) -> None:
    from agent_cli.coordinator_common import read_task_text, write_task_text

    worker = make_worker(tmp_path)
    source = "see ghp_" + "a" * 40 + " here\n" + "word " * 14_000
    _digest, length = write_task_text(worker, "task-1", source)
    task = {"id": "task-1", "payload": {"coordinator": {"source": {}}}}
    stored = read_task_text(worker, task)
    assert "ghp_" not in stored
    assert len(stored) <= 60_000
    assert stored.endswith("…[truncated]")
    assert length == len(stored)


@pytest.mark.no_pg
def test_task_text_reference_names_hash_and_length() -> None:
    from agent_cli.coordinator_common import task_text_reference

    assert task_text_reference("abc") == (
        "[task text: sha256 " + hashlib.sha256(b"abc").hexdigest() + ", 3 characters]"
    )


@pytest.mark.no_pg
def test_spec_context_embeds_the_given_text() -> None:
    from agent_cli.coordinator_runtime import _spec_context

    spec = _spec_context(
        {"source": {"body": "preview"}},
        "",
        text="word " * 1000 + "END-MARKER",
    )
    assert "END-MARKER" in spec
    assert "preview" not in spec


@pytest.mark.no_pg
def test_spec_context_without_text_keeps_the_1500_bound() -> None:
    from agent_cli.coordinator_runtime import _spec_context

    spec = _spec_context({"source": {"body": "zz " * 1000}})
    assert 0 < spec.count("z") <= 1500
    assert "…[truncated]" in spec


@pytest.mark.no_pg
def test_findings_block_in_the_spec_is_not_cut_at_4000() -> None:
    from agent_cli.coordinator_runtime import _spec_context

    spec = _spec_context(
        {"source": {}, "findings": "a" * 3900 + "\n\n" + "b" * 3900 + "END"}
    )
    assert "END" in spec


@pytest.mark.no_pg
def test_merge_review_findings_orders_and_titles() -> None:
    from agent_cli.coordinator_lanes import merge_review_findings

    assert merge_review_findings([("logic", "L"), ("quality", "Q")]) == (
        "Quality review findings:\nQ\n\nLogic review findings:\nL"
    )
    assert merge_review_findings([("logic", "L")]) == "Logic review findings:\nL"
    assert merge_review_findings([]) == ""


@pytest.mark.no_pg
def test_render_spec_is_what_write_spec_writes(tmp_path: Path) -> None:
    from agent_cli.coordinator_common import prompt_prohibitions
    from agent_cli.coordinator_lanes import render_spec, write_spec

    path = tmp_path / "s.md"
    write_spec(path, "implementer", "body")
    expected = f"# Role: implementer\n\n{prompt_prohibitions()}\nbody\n"
    assert path.read_text(encoding="utf-8") == render_spec("implementer", "body")
    assert path.read_text(encoding="utf-8") == expected


@pytest.mark.no_pg
def test_prompt_prohibitions_allow_read_only_commands() -> None:
    from agent_cli.coordinator_common import prompt_prohibitions

    prohibitions = prompt_prohibitions()
    assert (
        "Reading files with read-only commands (cat, head, sed -n, grep, ls, git diff, git show, git log) is allowed."
        in prohibitions
    )
    assert "any shell/Bash commands" not in prohibitions


def test_prompts_that_forbid_git_do_not_allow_read_only_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_lanes
    from agent_cli.coordinator_common import prompt_prohibitions
    from agent_cli.coordinator_runtime import phase_inner_review

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    worktree = worker.workspace_root / "inner-review-prompt"
    worktree.mkdir()
    task_id = "aa000001-0000-4000-8000-000000000234"
    task = {
        "id": task_id,
        "session_id": worker.session_id,
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
    store.write("task", "insert", task_id, task)
    store.write(
        "task_round",
        "insert",
        f"{task_id}-round",
        {
            "id": f"{task_id}-round",
            "task_id": task_id,
            "round": 1,
            "implementer_verdict": "done",
            "reviewer_verdict": None,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    )
    monkeypatch.setattr(
        coordinator_lanes,
        "verify_checkout_identity",
        lambda *_args, **_kwargs: {},
    )

    saved = store.row("task", task_id)
    assert saved is not None
    phase_inner_review(
        store,
        worker,
        saved,
        lambda _argv: Completed(0, "", ""),
        lambda _argv, _stdin: Completed(
            0, grok_messages_stream("STATUS: complete\nRESULT: rejected\n"), ""
        ),
    )
    inner_reviewer = (
        worker.workspace_root
        / ".coordinator-control"
        / task_id
        / "reviewer-grok.md"
    ).read_text(encoding="utf-8")

    assert "Reading files with read-only commands" not in inner_reviewer
    assert (
        "- Do not run tests, builds, installs, or any shell/Bash commands."
        in inner_reviewer
    )
    assert "Reading files with read-only commands" in prompt_prohibitions()


@pytest.mark.no_pg
def test_ci_failure_findings_keep_the_checks_and_the_end() -> None:
    from agent_cli.coordinator_github import _ci_failure_findings

    findings = _ci_failure_findings(
        "a" * 40, ["tests"], "y" * 9000 + "\nLAST-LINE"
    )
    assert findings.startswith("CI failed on aaaaaaa:\ntests\n")
    assert findings.endswith("LAST-LINE")
    assert len(findings) <= 4000


@pytest.mark.no_pg
def test_ci_log_keeps_the_end_of_each_run() -> None:
    from agent_cli.coordinator_github import fetch_failure_logs

    latest = {
        ".github/workflows/check.yml|pull_request": {
            "path": ".github/workflows/check.yml",
            "status": "completed",
            "conclusion": "failure",
            "id": 99,
            "run_attempt": 1,
        }
    }
    logs = fetch_failure_logs(
        lambda _argv: Completed(0, "y" * 4000 + "\nLAST-LINE", ""),
        "example/project",
        latest,
        [],
    )
    assert "LAST-LINE" in logs
    assert len(logs) <= 1500


def test_issue_text_reaches_the_implementer_untruncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "task text line\n" * 400 + "END-MARKER"
    _store, worker, task = _implemented(tmp_path, monkeypatch, body)
    control = worker.workspace_root / ".coordinator-control" / task["id"]
    assert "END-MARKER" in (control / "implementer-grok.md").read_text()
    assert (control / "task-text.md").read_text() == body


def test_long_task_text_is_not_stored_in_the_agent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "task text line\n" * 400 + "END-MARKER"
    store, _worker, _task = _implemented(tmp_path, monkeypatch, body)
    row = next(row for row in store.rows("agent") if row.get("role") == "implementer")
    assert "[task text: sha256 " in row["prompt"]
    assert "END-MARKER" not in row["prompt"]


def test_short_task_text_keeps_the_launched_prompt_in_the_agent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, worker, task = _implemented(tmp_path, monkeypatch, "Please fix")
    row = next(row for row in store.rows("agent") if row.get("role") == "implementer")
    spec = (
        worker.workspace_root
        / ".coordinator-control"
        / task["id"]
        / "implementer-grok.md"
    ).read_text()
    assert row["prompt"] == spec


def test_task_text_file_is_removed_when_the_source_already_has_a_task(
    tmp_path: Path,
) -> None:
    from agent_cli.coordinator_runtime import _create_implement_task

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)
    arguments = {
        "repo": "example/project",
        "number": 7,
        "title": "Fix widget",
        "assigned_id": "assigned-7",
        "repository_config": worker.repositories["example/project"],
        "body": "Please fix",
    }
    first = _create_implement_task(store, worker, **arguments)
    assert first is not None
    assert _create_implement_task(store, worker, **arguments) is None
    root = worker.workspace_root / ".coordinator-control"
    directories = list(root.iterdir())
    assert [path.name for path in directories] == [first["id"]]
    assert (directories[0] / "task-text.md").is_file()


def test_task_text_file_is_removed_when_the_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_runtime import _create_implement_task

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)

    def refuse(*_args, **_kwargs):
        raise StoreError("insert refused")

    monkeypatch.setattr(store, "write_with_advisory", refuse)
    with pytest.raises(StoreError, match="insert refused"):
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
    root = worker.workspace_root / ".coordinator-control"
    assert not root.exists() or list(root.iterdir()) == []


def test_task_text_file_is_removed_when_the_checklist_rows_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime

    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    worker = make_worker(tmp_path)

    def refuse(*_args, **_kwargs):
        raise StoreError("checklist refused")

    monkeypatch.setattr(coordinator_runtime, "_ensure_checklist_rows", refuse)
    with pytest.raises(StoreError, match="checklist refused"):
        with store.advisory_transaction("coordinator-source:example/project:7"):
            coordinator_runtime._create_implement_task(
                store,
                worker,
                repo="example/project",
                number=7,
                title="Fix widget",
                assigned_id="assigned-7",
                repository_config=worker.repositories["example/project"],
                body="Please fix",
            )
    root = worker.workspace_root / ".coordinator-control"
    assert not root.exists() or list(root.iterdir()) == []
    assert store.rows("task") == []


def test_test_output_keeps_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import coordinator_runtime

    store = Store(tmp_path)
    worker = make_worker(tmp_path)
    tid = "11111111-1111-4111-8111-111111111111"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    store.write(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": "worker-session",
            "workflow": "implement",
            "state": "local-check",
            "current_round": 1,
            "payload": {
                "coordinator": {
                    "phase": "tests",
                    "source": {"repo": "example/project", "number": 7},
                    "worktree": str(worktree),
                    "head_sha": "a" * 40,
                    "pr_number": 42,
                }
            },
        },
    )
    monkeypatch.setattr(
        coordinator_runtime,
        "verify_signed_clean_head",
        lambda *args, **kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        coordinator_runtime,
        "run_bounded",
        lambda argv, **kwargs: Completed(
            1, "x" * 6000 + "\nFAILED test_tail", ""
        ),
    )
    coordinator_runtime.phase_tests(
        store,
        worker,
        store.row("task", tid),
        lambda argv: Completed(0, "", ""),
    )
    saved = store.row("task", tid)["payload"]["coordinator"]
    assert saved["findings"].startswith("Tests failed on aaaaaaa:")
    assert "FAILED test_tail" in saved["findings"]
    check = store.rows("local_check")[0]
    assert check["output"].startswith("…[truncated]")
    assert "FAILED test_tail" in check["output"]
    assert saved["phase"] == "implement"


def test_a38_local_check_output_keeps_the_end(tmp_path: Path) -> None:
    from agent_cli.coordinator_a38 import _record_local_check

    store = Store(tmp_path)
    _record_local_check(
        store,
        {"id": "task-1"},
        name="coordinator-check",
        command="check",
        passed=False,
        output="x" * 6000 + "\nFAILED tail",
        head="a" * 40,
    )
    output = store.rows("local_check")[0]["output"]
    assert output.startswith("…[truncated]")
    assert "FAILED tail" in output
    assert len(output) <= 4000


def test_a38_precheck_findings_keep_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    head = "a" * 40
    promoted_base = "c" * 40
    observed_base = "b" * 40
    tid = "33333333-3333-4333-8333-33333333333f"
    worktree = worker.workspace_root / tid
    worktree.mkdir()
    task = {
        "id": tid,
        "session_id": worker.session_id,
        "workflow": "implement",
        "state": "local-check",
        "title": "A38 tests",
        "repo": "example/project",
        "ref": 42,
        "models": {"first": "grok", "second": None},
        "payload": {
            "coordinator": {
                "phase": "tests",
                "head_sha": head,
                "worktree": str(worktree),
                "pr_number": 42,
                "source": {"repo": "example/project", "number": 7},
                "a38": {
                    "enabled": True,
                    "base_sha": promoted_base,
                    "policy_revision": promoted_base,
                },
            }
        },
    }
    store.write("task", "insert", tid, task)
    store.write(
        "checklist_item",
        "insert",
        "a38-fail-local",
        {
            "id": "a38-fail-local",
            "task_id": tid,
            "key": "local_check_pass",
            "status": "pending",
        },
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.verify_signed_clean_head", lambda *args: head
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38._pull_a38_context",
        lambda *args, **kwargs: (observed_base, ["src/app.py"], False, False),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.scoped",
        lambda *args, **kwargs: lambda argv: Completed(0, "false", ""),
    )
    monkeypatch.setattr("agent_cli.coordinator_a38.gh_json", lambda *a, **k: False)
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.ensure_draft", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.run_bounded",
        lambda argv, **kwargs: Completed(
            1, "x" * 6000 + "\nFAILED precheck_tail", ""
        ),
    )
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.git",
        lambda *_args, **_kwargs: Completed(0, "", ""),
    )
    phase_a38_tests(
        store, worker, store.row("task", tid), lambda argv: Completed(0, "", "")
    )
    findings = store.row("task", tid)["payload"]["coordinator"]["findings"]
    assert findings.startswith("Trusted precheck failed on aaaaaaa:")
    assert "FAILED precheck_tail" in findings


def test_findings_carry_both_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_runtime import _spec_context

    quality = (
        "STATUS: complete\nRESULT: rejected\n"
        + "quality note " * 250
        + "Q-FINDING"
    )
    logic = (
        "STATUS: complete\nRESULT: rejected\n"
        + "logic note " * 300
        + "L-FINDING"
    )
    store, tid, _lines = _gate_task(tmp_path, monkeypatch, quality, logic)
    task = store.row("task", tid)
    c = task["payload"]["coordinator"]
    positions = [
        c["findings"].index(value)
        for value in (
            "Quality review findings:",
            "Q-FINDING",
            "Logic review findings:",
            "L-FINDING",
        )
    ]
    assert positions == sorted(positions)
    rejected = [
        row
        for row in store.rows("review_gate")
        if row.get("task_id") == tid and row.get("verdict") == "rejected"
    ]
    assert len(rejected) == 2
    assert c["phase"] == "implement"
    spec = _spec_context(c, "")
    assert "Q-FINDING" in spec
    assert "L-FINDING" in spec


def test_approved_dimension_is_recorded_next_to_a_rejected_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: rejected\nQ-FINDING",
        "STATUS: complete\nRESULT: approved\n",
    )
    gates = {
        row["dimension"]: row["verdict"]
        for row in store.rows("review_gate")
        if row.get("task_id") == tid
    }
    assert gates == {"quality": "rejected", "logic": "approved"}
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["findings"].startswith("Quality review findings:")
    assert "Logic review findings:" not in c["findings"]
    assert c["phase"] == "implement"


_PROVIDER_402 = (
    "API error (status 402 Payment Required): "
    "Grok Build usage balance exhausted"
)
_TODAY_INCOMPLETE_BLOCKER = (
    "grok quality, logic review unavailable/incomplete "
    "(quality: status=unavailable, result=empty; "
    "logic: status=unavailable, result=empty). Not a code rejection."
)


def _lane_result(
    *,
    stdout: str = "",
    stderr: str = "",
    protocol_result: str = "",
    status: str = "unavailable",
    config_dir: str | None = None,
) -> LaneResult:
    return LaneResult(
        role="pr-reviewer-quality",
        vendor="grok",
        model="grok-model",
        status=status,
        argv=[],
        returncode=1,
        stdout=stdout,
        stderr=stderr,
        config_dir=config_dir,
        protocol_result=protocol_result,
    )


def _progress_lines() -> str:
    """Progress output longer than the 4000 characters a stored stream keeps."""
    progress = "".join(
        f"progress {index:03d}: reading repository files\n" for index in range(150)
    )
    assert len(progress) > 4000
    return progress


def _incomplete_blocker_comments(store: Store) -> list[str]:
    return [
        str((row.get("payload") or {}).get("body") or "")
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and "Blocked:" in str((row.get("payload") or {}).get("body") or "")
    ]


def _reviewer_agents(store: Store, tid: str) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("dimension")): row
        for row in store.rows("agent")
        if row.get("task_id") == tid
        and str(row.get("role") or "").startswith("pr-reviewer-")
    }


@pytest.mark.no_pg
def test_incomplete_output_excerpt_takes_the_last_visible_stderr_line() -> None:
    from agent_cli.coordinator_lanes import _incomplete_output_excerpt

    stdout = "model text\nlast stdout line\n"
    stderr = f"starting review\n\x1b[31m{_PROVIDER_402}\x1b[0m\n\n   \n"
    outcome = _lane_result(stdout=stdout, stderr=stderr)
    assert _incomplete_output_excerpt(outcome) == _PROVIDER_402
    valid = _lane_result(
        stdout=stdout, stderr=stderr, protocol_result="approved", status="complete"
    )
    assert _incomplete_output_excerpt(valid) == "last stdout line"
    # A complete lane without a valid result token reads stderr first.
    no_token = _lane_result(
        stdout=stdout, stderr=stderr, protocol_result="ask", status="complete"
    )
    assert _incomplete_output_excerpt(no_token) == _PROVIDER_402
    # A lane that did not complete reads stderr first even with a valid token.
    for status in ("unavailable", "timeout", "partial"):
        for token in ("approved", "rejected"):
            not_complete = _lane_result(
                stdout=stdout, stderr=stderr, protocol_result=token, status=status
            )
            assert _incomplete_output_excerpt(not_complete) == _PROVIDER_402, (
                status,
                token,
            )
    stdout_only = _lane_result(stdout=f"{_PROVIDER_402}\n", stderr=" \n")
    assert _incomplete_output_excerpt(stdout_only) == _PROVIDER_402
    assert _incomplete_output_excerpt(_lane_result()) == ""
    assert (
        _incomplete_output_excerpt(_lane_result(stdout="\n \n", stderr="\t\n"))
        == ""
    )
    long_output = _lane_result(stderr=f"{_progress_lines()}{_PROVIDER_402}\n")
    assert _incomplete_output_excerpt(long_output) == _PROVIDER_402


@pytest.mark.no_pg
def test_incomplete_output_excerpt_is_redacted_and_bounded() -> None:
    from agent_cli.coordinator_lanes import (
        INCOMPLETE_OUTPUT_EXCERPT_LIMIT,
        _incomplete_output_excerpt,
    )

    token = "ghp_" + "a" * 40
    secret = _incomplete_output_excerpt(
        _lane_result(stderr=f"request failed for credential {token}\n")
    )
    assert secret == "request failed for credential [redacted]"
    key_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
    pem = _incomplete_output_excerpt(
        _lane_result(stderr=f"boom\n-----BEGIN PRIVATE KEY-----\n{key_body}\n")
    )
    assert key_body not in pem
    assert "[redacted]" in pem
    config = _incomplete_output_excerpt(
        _lane_result(
            stderr="cannot read /test/account/settings.json\n",
            config_dir="/test/account",
        )
    )
    assert config == "cannot read <config_dir>/settings.json"
    long_line = "E" * 500
    cut = _incomplete_output_excerpt(_lane_result(stderr=f"first\n{long_line}\n"))
    assert INCOMPLETE_OUTPUT_EXCERPT_LIMIT == 200
    assert len(cut) <= INCOMPLETE_OUTPUT_EXCERPT_LIMIT
    assert cut.startswith("E" * 150)


def test_reviewer_spool_keeps_the_excerpt_of_the_complete_output(
    tmp_path: Path,
) -> None:
    import json

    from agent_cli import coordinator_lanes

    store = Store(tmp_path)
    write_accounts(store.home)
    worker = make_worker(tmp_path)
    task = {"id": "aa000001-0000-4000-8000-000000000301"}
    agent = {
        "id": "aa000001-0000-4000-8000-000000000302",
        "role": "pr-reviewer-quality",
        "vendor": "codex",
    }
    identity = {
        "round_num": None,
        "head": "a" * 40,
        "dimension": "quality",
        "stage": "review-1",
    }
    path = coordinator_lanes._write_reviewer_spool(
        store,
        worker,
        task,
        agent,
        replace(
            _lane_result(stderr=f"{_progress_lines()}{_PROVIDER_402}\n"),
            vendor="codex",
        ),
        output_file=None,
        **identity,
    )

    def read() -> Any:
        return coordinator_lanes._read_reviewer_spool(
            store, worker, task, agent, **identity
        )

    recovered = read()
    assert recovered is not None
    saved, _output_file, excerpt = recovered
    assert excerpt == _PROVIDER_402
    # The spooled stderr keeps only the start of the stream.
    assert _PROVIDER_402 not in saved.stderr
    assert saved.stderr.endswith("[truncated]")

    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["result"]["output_excerpt"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    older = read()
    assert older is not None
    assert older[2] is None
    payload["result"]["output_excerpt"] = 402
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read() is None


def test_incomplete_review_blocker_names_the_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tid, lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(
                1, "", f"starting review\n{_PROVIDER_402}\n"
            ),
            "pr-reviewer-logic": Completed(1, "", ""),
        },
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["resume_phase"] == "pr_gates_grok"
    assert c["blocker_publish_kind"] == "provider-incomplete"
    # The logic lane produced no output, so its details keep today's text.
    assert c["blocker"] == (
        f"{_TODAY_INCOMPLETE_BLOCKER} Lane output: quality: {_PROVIDER_402}"
    )
    comments = _incomplete_blocker_comments(store)
    assert len(comments) == 1
    assert f"Blocked: {c['blocker']}" in comments[0]
    assert "review-1/quality unavailable; blocked" in lines
    assert "review-1/logic unavailable; blocked" in lines


def test_incomplete_review_blocker_without_output_keeps_todays_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(1, "", ""),
            "pr-reviewer-logic": Completed(1, "", " \n"),
        },
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["blocker"] == _TODAY_INCOMPLETE_BLOCKER
    comments = _incomplete_blocker_comments(store)
    assert len(comments) == 1
    assert f"Blocked: {_TODAY_INCOMPLETE_BLOCKER}" in comments[0]


def test_incomplete_review_blocker_redacts_and_cuts_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_lanes import INCOMPLETE_OUTPUT_EXCERPT_LIMIT

    token = "ghp_" + "b" * 40
    long_error = "API error (status 500): " + "detail " * 80
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(
                1, "", f"API error (status 401): bad credential {token}\n"
            ),
            "pr-reviewer-logic": Completed(1, "", f"{long_error}\n"),
        },
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    blocker = c["blocker"]
    assert blocker.startswith(f"{_TODAY_INCOMPLETE_BLOCKER} Lane output: ")
    assert token not in blocker
    assert "ghp_" not in blocker
    output = blocker.split(" Lane output: ", 1)[1]
    quality_part, logic_excerpt = output.split("; logic: ")
    assert quality_part == "quality: API error (status 401): bad credential [redacted]"
    assert len(long_error.strip()) > INCOMPLETE_OUTPUT_EXCERPT_LIMIT
    assert logic_excerpt.startswith("API error (status 500): detail detail")
    assert logic_excerpt.endswith(" …")
    assert len(logic_excerpt) <= INCOMPLETE_OUTPUT_EXCERPT_LIMIT
    for body in _incomplete_blocker_comments(store):
        assert "ghp_" not in body


def test_incomplete_review_blocker_keeps_the_error_after_long_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(
                1, "", f"{_progress_lines()}{_PROVIDER_402}\n"
            ),
            "pr-reviewer-logic": Completed(1, "", ""),
        },
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["blocker"] == (
        f"{_TODAY_INCOMPLETE_BLOCKER} Lane output: quality: {_PROVIDER_402}"
    )
    assert "[truncated]" not in c["blocker"]
    comments = _incomplete_blocker_comments(store)
    assert len(comments) == 1
    assert f"Blocked: {c['blocker']}" in comments[0]
    assert "[truncated]" not in comments[0]
    # The stored stderr keeps only the start of the stream, so the excerpt
    # must come from the complete output.
    note = str(_reviewer_agents(store, tid)["quality"].get("note") or "")
    assert note.endswith("[truncated]")
    assert _PROVIDER_402 not in note


def _fail_first_reviewer_checkpoint(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Fail the first reviewer checkpoint once and return its call counter.

    The quality checkpoint runs first, so both reviewer results are then
    recovered from their spools in the next gate step.
    """
    from agent_cli import coordinator_lanes

    real_checkpoint = coordinator_lanes._checkpoint_reviewer_outcome
    calls = [0]

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise StoreError("checkpoint unavailable")
        return real_checkpoint(*args, **kwargs)

    monkeypatch.setattr(
        coordinator_lanes, "_checkpoint_reviewer_outcome", fail_once
    )
    return calls


def _drop_checkpoint_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write reviewer checkpoints as they were before the excerpt was stored."""
    from agent_cli import coordinator_lanes

    real_fields = coordinator_lanes._reviewer_replay_fields

    def older_fields(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entry = real_fields(*args, **kwargs)
        del entry["output_excerpt"]
        return entry

    monkeypatch.setattr(coordinator_lanes, "_reviewer_replay_fields", older_fields)


def test_incomplete_review_blocker_keeps_the_error_after_spool_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fail_first_reviewer_checkpoint(monkeypatch)
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(
                1, "", f"{_progress_lines()}{_PROVIDER_402}\n"
            ),
            "pr-reviewer-logic": Completed(1, "", ""),
        },
        passes=2,
    )
    # One failed checkpoint, then both results recovered from their spools.
    assert calls == [3]
    agents = _reviewer_agents(store, tid)
    assert sorted(agents) == ["logic", "quality"]
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["blocker"] == (
        f"{_TODAY_INCOMPLETE_BLOCKER} Lane output: quality: {_PROVIDER_402}"
    )
    note = str(agents["quality"].get("note") or "")
    assert note.endswith("[truncated]")
    assert _PROVIDER_402 not in note


def test_incomplete_review_blocker_reads_a_checkpoint_without_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drop_checkpoint_excerpt(monkeypatch)
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_results={
            "pr-reviewer-quality": Completed(
                1, "", f"starting review\n{_PROVIDER_402}\n"
            ),
            "pr-reviewer-logic": Completed(1, "", ""),
        },
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["blocker"] == (
        f"{_TODAY_INCOMPLETE_BLOCKER} Lane output: quality: {_PROVIDER_402}"
    )


@pytest.mark.parametrize(
    "recovery", ["checkpoint", "spool", "checkpoint without excerpt"]
)
def test_incomplete_review_blocker_reads_stderr_of_a_lane_that_did_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    passes = 1
    if recovery == "spool":
        _fail_first_reviewer_checkpoint(monkeypatch)
        passes = 2
    elif recovery == "checkpoint without excerpt":
        _drop_checkpoint_excerpt(monkeypatch)
    store, tid, _lines = _gate_task(
        tmp_path,
        monkeypatch,
        # A valid result token, but the lane did not complete.
        "STATUS: unavailable\nRESULT: rejected\nQ-FINDING\n",
        "STATUS: complete\nRESULT: approved\n",
        lane_stderr={
            "pr-reviewer-quality": f"starting review\n{_PROVIDER_402}\n"
        },
        passes=passes,
    )
    c = store.row("task", tid)["payload"]["coordinator"]
    assert c["phase"] == "blocked"
    assert c["blocker_publish_kind"] == "provider-incomplete"
    blocker = c["blocker"]
    assert (
        " quality review unavailable/incomplete "
        "(quality: status=unavailable, result=rejected). Not a code rejection."
    ) in blocker
    # The provider error on stderr, not the last stdout line.
    assert blocker.endswith(f" Lane output: quality: {_PROVIDER_402}")
    assert "Q-FINDING" not in blocker
    comments = _incomplete_blocker_comments(store)
    assert len(comments) == 1
    assert f"Blocked: {blocker}" in comments[0]
