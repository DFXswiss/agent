from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_cli.main import main
from agent_cli.store import Store, StoreError, utcnow


def run(home: Path, argv: list[str]) -> None:
    import os

    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def _last_task_id(out: str) -> str:
    task_line = [ln for ln in out.splitlines() if ln.startswith("task ")][-1]
    return task_line.split()[1]


def _last_agent_id(out: str) -> str:
    agent_line = [ln for ln in out.splitlines() if ln.startswith("agent ")][-1]
    return agent_line.split()[1]


def test_session_task_checklist_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship sync"])
    out = capsys.readouterr().out
    tid = _last_task_id(out)
    run(tmp_path, ["checklist", "set", "--task", tid, "--key", "session_registered", "--status", "ja", "--source", "script"])
    run(tmp_path, ["status"])
    status = capsys.readouterr().out
    assert "tasks_open=1" in status
    assert "agents_working=" in status
    assert "work_open=" in status
    assert "Ship sync" in status
    with pytest.raises(SystemExit, match="summaries missing"):
        run(tmp_path, ["task", "state", tid, "done"])


def test_cannot_close_with_pending_assigned(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "assigned", "--kind", "runner"])
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "asg-1",
            {
                "id": "asg-1",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-01-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="pending assigned"):
        run(tmp_path, ["session", "close", "--id", "assigned"])


def test_cannot_close_with_open_task(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    with pytest.raises(SystemExit, match="open tasks"):
        run(tmp_path, ["session", "close", "--id", "s"])


def test_happy_path_round_agent_check_work(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    run(tmp_path, ["round", "start", "--task", tid])
    assert f"task {tid} round 1" in capsys.readouterr().out

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "sess-1",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "reviewing"

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "sess-1",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    rev_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", rev_id, "--verdict", "approved"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "local-check"

    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "pytest",
            "--result",
            "pass",
        ],
    )
    assert "check lint=pass" in capsys.readouterr().out

    run(
        tmp_path,
        [
            "work",
            "add",
            "--session",
            "sess-1",
            "--key",
            "standing",
            "--closable-by",
            "agent",
        ],
    )
    assert "work standing open closable_by=agent" in capsys.readouterr().out

    run(tmp_path, ["work", "list"])
    listing = capsys.readouterr().out
    assert "sess-1  standing  open  agent" in listing


def test_n_a_without_evidence_dies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "R"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="n_a requires --evidence"):
        run(
            tmp_path,
            [
                "checklist",
                "set",
                "--task",
                tid,
                "--key",
                "coverage_ok",
                "--status",
                "n_a",
                "--source",
                "script",
            ],
        )


def test_check_fail_sets_task_failed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "T"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "false",
            "--result",
            "fail",
        ],
    )
    assert "check lint=fail" in capsys.readouterr().out
    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "failed"


def test_work_set_human_closable_rejects_runner_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(
        tmp_path,
        ["work", "add", "--session", "s", "--key", "mandate", "--closable-by", "human"],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="closable_by=human requires --source human"):
        run(
            tmp_path,
            [
                "work",
                "set",
                "--session",
                "s",
                "--key",
                "mandate",
                "--status",
                "done",
                "--source",
                "runner",
            ],
        )


def test_round_start_on_review_workflow_dies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="round start requires workflow"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_done_requires_gates_after_summaries_and_checklist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    with pytest.raises(SystemExit, match="summaries missing"):
        run(tmp_path, ["task", "state", tid, "done"])

    run(
        tmp_path,
        [
            "task",
            "summary",
            "--id",
            tid,
            "--en",
            "Adds complete session-store CLI.",
            "--de",
            "Ergaenzt die vollstaendige Session-Store-CLI.",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        keys = [
            r["key"]
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid
        ]
    finally:
        store.close()
    for key in keys:
        argv = [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            key,
            "--status",
            "ja",
            "--source",
            "human",
        ]
        if key == "deviation_granted":
            argv.extend(
                [
                    "--deviation-granted",
                    "true",
                    "--granted-by",
                    "reviewer",
                    "--actor-session",
                    "s",
                ]
            )
        run(tmp_path, argv)
    capsys.readouterr()

    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "lint",
            "--command",
            "true",
            "--result",
            "pass",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="missing gate|gate"):
        run(tmp_path, ["task", "state", tid, "done"])


def test_gate_record_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
        ],
    )
    agent_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", agent_id, "--verdict", "approved"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            "grok-pr",
            "--dimension",
            "quality",
            "--vendor",
            "grok",
            "--verdict",
            "approved",
            "--head",
            "abcdef0",
            "--agent",
            agent_id,
        ],
    )
    assert "gate grok-pr/quality=approved" in capsys.readouterr().out


def test_gate_record_codex_before_grok_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "codex",
        ],
    )
    agent_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", agent_id, "--verdict", "approved"])
    capsys.readouterr()

    with pytest.raises(SystemExit, match="codex-pr requires approved grok-pr"):
        run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "codex-pr",
                "--dimension",
                "quality",
                "--vendor",
                "codex",
                "--verdict",
                "approved",
                "--head",
                "abcdef0",
                "--agent",
                agent_id,
            ],
        )


def test_reviewer_start_before_implementer_done_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()
    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="reviewing|implementer_verdict"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "reviewer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )


def test_implementer_finish_blocked_sets_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "blocked"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "failed"


def _seed_foreign_implement_task(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 2,
                "table": "task",
                "op": "insert",
                "row_id": "t-foreign",
                "payload": {
                    "id": "t-foreign",
                    "session_id": "sess-f",
                    "workflow": "implement",
                    "state": "implementing",
                    "current_round": 1,
                },
                "occurred_at": "2026-08-13T12:00:01Z",
            }
        )
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 3,
                "table": "task_round",
                "op": "insert",
                "row_id": "r-foreign",
                "payload": {
                    "id": "r-foreign",
                    "task_id": "t-foreign",
                    "round": 1,
                    "implementer_verdict": None,
                },
                "occurred_at": "2026-08-13T12:00:02Z",
            }
        )
    finally:
        store.close()


def test_agent_start_on_foreign_task_dies(tmp_path: Path) -> None:
    _seed_foreign_implement_task(tmp_path)
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "sess-f",
                "--task",
                "t-foreign",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )


def test_check_record_on_foreign_task_dies(tmp_path: Path) -> None:
    _seed_foreign_implement_task(tmp_path)
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "check",
                "record",
                "--task",
                "t-foreign",
                "--name",
                "lint",
                "--command",
                "true",
                "--result",
                "pass",
            ],
        )


def test_work_add_on_foreign_session_dies(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "work",
                "add",
                "--session",
                "sess-f",
                "--key",
                "standing",
                "--closable-by",
                "agent",
            ],
        )


def test_implementer_blocked_sets_round_finished_at(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "blocked"])
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        rounds = [
            r
            for r in store.rows("task_round")
            if r.get("task_id") == tid and r.get("round") == 1
        ]
        assert len(rounds) == 1
        assert rounds[0].get("finished_at") is not None
    finally:
        store.close()


def test_reviewer_finish_rejected_sets_implementing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "reviewer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    rev_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", rev_id, "--verdict", "rejected"])
    capsys.readouterr()

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "implementing"


def test_implementer_finish_on_pr_review_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["task", "state", tid, "pr-review"])
    capsys.readouterr()

    with pytest.raises(SystemExit, match="task state must be implementing"):
        run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])


def test_second_implementer_finish_same_round_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    first_id = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", first_id, "--verdict", "done"])
    capsys.readouterr()

    # First finish left state reviewing + implementer_verdict set. Force a second
    # working implementer on the same round so finish hits the double-finish guard.
    second_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["state"] = "implementing"
        store.write(
            "task",
            "update",
            tid,
            {k: v for k, v in task.items() if not k.startswith("_")},
        )
        store.write(
            "agent",
            "insert",
            second_id,
            {
                "id": second_id,
                "session_id": "s",
                "task_id": tid,
                "round": 1,
                "role": "implementer",
                "vendor": "grok",
                "status": "working",
                "started_at": utcnow(),
                "finished_at": None,
                "note": None,
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="implementer already finished this round"):
        run(tmp_path, ["agent", "finish", "--id", second_id, "--verdict", "done"])


def test_round_start_with_working_agent_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="round still has a working agent"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_round_start_with_working_pr_reviewer_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
        ],
    )
    capsys.readouterr()

    with pytest.raises(SystemExit, match="round still has a working agent"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_implementer_finish_after_session_closed_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    run(
        tmp_path,
        [
            "agent",
            "start",
            "--session",
            "s",
            "--task",
            tid,
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--round",
            "1",
        ],
    )
    impl_id = _last_agent_id(capsys.readouterr().out)

    store = Store(tmp_path)
    try:
        session = store.row("session", "s")
        assert session is not None
        session["status"] = "closed"
        store.write(
            "session",
            "update",
            "s",
            {k: v for k, v in session.items() if not k.startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="session is not active"):
        run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])


def test_agent_finish_on_foreign_agent_dies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["round", "start", "--task", tid])
    capsys.readouterr()

    foreign_agent_id = str(uuid.uuid4())
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "agent",
                "op": "insert",
                "row_id": foreign_agent_id,
                "payload": {
                    "id": foreign_agent_id,
                    "session_id": "s",
                    "task_id": tid,
                    "round": 1,
                    "role": "implementer",
                    "vendor": "grok",
                    "status": "working",
                    "started_at": utcnow(),
                    "finished_at": None,
                    "note": None,
                },
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="another device"):
        run(tmp_path, ["agent", "finish", "--id", foreign_agent_id, "--verdict", "done"])

    run(tmp_path, ["task", "show", tid])
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "implementing"


def test_watch_grok_usage_prints_snapshot_or_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    monkeypatch.setattr(
        "agent_cli.main.scan_usage",
        lambda store: "11111111-1111-1111-1111-111111111111",
    )
    run(tmp_path, ["watch", "grok-usage"])
    assert "usage.snapshot 11111111-1111-1111-1111-111111111111" in capsys.readouterr().out

    monkeypatch.setattr("agent_cli.main.scan_usage", lambda store: None)
    run(tmp_path, ["watch", "grok-usage"])
    assert "usage.snapshot none" in capsys.readouterr().out


def test_watch_errors_prints_none_and_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    monkeypatch.setattr("agent_cli.errors.scan_errors", lambda store, fetch: ([], []))
    run(tmp_path, ["watch", "errors"])
    assert "error.seen none" in capsys.readouterr().out
    monkeypatch.setattr(
        "agent_cli.errors.scan_errors",
        lambda store, fetch: (["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"], ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]),
    )
    run(tmp_path, ["watch", "errors"])
    out = capsys.readouterr().out
    assert "error.seen aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in out
    assert "error.seen enrich bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in out


def test_watch_error_fix_prints_none_and_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", lambda store, runner: [])
    run(tmp_path, ["watch", "error-fix"])
    assert "error.fix none" in capsys.readouterr().out
    monkeypatch.setattr(
        "agent_cli.error_fix_act.scan_error_fix",
        lambda store, runner: ["error.fix x task=t worktree=/tmp/w"],
    )
    run(tmp_path, ["watch", "error-fix"])
    assert "error.fix x task=t worktree=/tmp/w" in capsys.readouterr().out


def test_knock_once_does_not_poll_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []

    def fake_scan(store: object) -> None:
        calls.append(store)

    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_daemon_polls_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(_store: object) -> str:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("boom")
        return "11111111-1111-1111-1111-111111111111"

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "usage.snapshot error: boom" in captured.err
    assert "usage.snapshot 11111111-1111-1111-1111-111111111111" in captured.out


def test_knock_once_does_not_poll_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    (tmp_path / "error-fix.json").write_text('{"session_id":"s"}', encoding="utf-8")
    calls: list[object] = []

    def fake_scan(store: object, fetch: object) -> tuple[list[str], list[str]]:
        calls.append(store)
        return ([], [])

    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_once_does_not_call_scan_error_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []

    def fake_scan(*_args: object, **_kwargs: object) -> list[str]:
        calls.append(_args)
        return []

    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", fake_scan)
    run(tmp_path, ["knock", "--once"])
    assert calls == []


def test_knock_daemon_polls_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    (tmp_path / "error-fix.json").write_text('{"session_id":"s"}', encoding="utf-8")
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(_store: object, _fetch: object) -> tuple[list[str], list[str]]:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("nope")
        return (
            ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
        )

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "error.seen error: nope" in captured.err
    assert "error.seen aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in captured.out
    assert "error.seen enrich bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in captured.out


def test_knock_daemon_calls_scan_error_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    scan_calls = {"n": 0}
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan(*_args: object, **_kwargs: object) -> list[str]:
        scan_calls["n"] += 1
        if scan_calls["n"] == 1:
            raise StoreError("nope")
        return ["error.fix x task=t worktree=/tmp/w"]

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        if listen_calls["n"] == 2:
            raise SystemExit("stop")
        return None

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "error.fix error: nope" in captured.err
    assert "error.fix x task=t worktree=/tmp/w" in captured.out


def test_knock_daemon_skips_errors_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    calls: list[object] = []
    listen_calls = {"n": 0}

    def fake_scan(store: object, fetch: object) -> tuple[list[str], list[str]]:
        calls.append(store)
        return ([], [])

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", lambda *_a, **_k: True)
    monkeypatch.setattr("agent_cli.main.scan_usage", lambda _store: None)
    monkeypatch.setattr("agent_cli.errors.scan_errors", fake_scan)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    assert calls == []


def test_work_set_done_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(
        tmp_path,
        ["work", "add", "--session", "s", "--key", "standing", "--closable-by", "agent"],
    )
    capsys.readouterr()
    run(
        tmp_path,
        [
            "work",
            "set",
            "--session",
            "s",
            "--key",
            "standing",
            "--status",
            "done",
            "--source",
            "script",
        ],
    )
    assert "work standing=done" in capsys.readouterr().out
    run(tmp_path, ["work", "list"])
    listing = capsys.readouterr().out
    assert "s  standing  done  agent" in listing


def test_checklist_deviation_flags_persist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "checklist",
            "set",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--status",
            "ja",
            "--source",
            "human",
            "--deviation-declared",
            "true",
            "--deviation-granted",
            "true",
            "--granted-by",
            "reviewer",
            "--actor-session",
            "s",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path)
    try:
        items = [
            r
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid and r.get("key") == "session_registered"
        ]
        assert len(items) == 1
        item = items[0]
        assert item["status"] == "ja"
        assert item["deviation_declared"] is True
        assert item["deviation_granted"] is True
        assert item["granted_by"] == "reviewer"
    finally:
        store.close()


def test_activity_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "sess-2", "body": "hello"}', encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "sess-1",
            "--type",
            "message",
            "--payload-file",
            str(payload),
        ],
    )
    out = capsys.readouterr().out
    assert "activity " in out
    assert "type=message" in out
    store = Store(tmp_path)
    try:
        rows = store.rows("activity")
        assert len(rows) == 1
        assert rows[0]["type"] == "message"
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["payload"]["to_session"] == "sess-2"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def _seed_cli_error_seen_for_conclusion(
    tmp_path: Path,
    *,
    error_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    fingerprint: str = "traceback-fingerprint",
    repo: str | None = "org/app",
    with_error_fix_skill: bool = True,
) -> None:
    run(tmp_path, ["init"])
    register = [
        "session",
        "register",
        "--id",
        "error-session",
        "--kind",
        "human",
        "--skill",
        "spine",
    ]
    if with_error_fix_skill:
        register.extend(["--skill", "error-fix"])
    run(tmp_path, register)
    payload: dict[str, object] = {"fingerprint": fingerprint}
    if repo is not None:
        payload["repo"] = repo
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            error_id,
            {
                "id": error_id,
                "session_id": "error-session",
                "type": "error.seen",
                "payload": payload,
                "execution_status": "done",
            },
        )
    finally:
        store.close()


def _add_cli_error_conclusion(
    tmp_path: Path,
    *,
    typ: str,
    payload: dict[str, object],
) -> None:
    payload_file = tmp_path / f"{typ}.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "error-session",
            "--type",
            typ,
            "--payload-file",
            str(payload_file),
        ],
    )


def test_activity_add_error_fix_happy_path(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": error_id, "fingerprint": fingerprint},
    )

    store = Store(tmp_path)
    try:
        rows = [row for row in store.rows("activity") if row["type"] == "error.fix"]
        assert len(rows) == 1
        assert rows[0]["type"] == "error.fix"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def test_activity_add_error_skip_happy_path(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.skip",
        payload={
            "error_id": error_id,
            "fingerprint": fingerprint,
            "reason": "Known external failure",
        },
    )

    store = Store(tmp_path)
    try:
        rows = [row for row in store.rows("activity") if row["type"] == "error.skip"]
        assert len(rows) == 1
        assert rows[0]["type"] == "error.skip"
        assert rows[0]["execution_status"] == "pending"
    finally:
        store.close()


def test_activity_add_error_fix_requires_mapped_repo(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint=fingerprint,
        repo=None,
    )

    with pytest.raises(SystemExit, match="unmapped-repo"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_fix_requires_skill(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint=fingerprint,
        with_error_fix_skill=False,
    )

    with pytest.raises(SystemExit, match="error-fix"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_refuses_second_error_conclusion(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    _add_cli_error_conclusion(
        tmp_path,
        typ="error.skip",
        payload={
            "error_id": error_id,
            "fingerprint": fingerprint,
            "reason": "Known external failure",
        },
    )

    with pytest.raises(SystemExit, match="conclusion already exists"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_skip_requires_reason(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)

    with pytest.raises(SystemExit, match="reason is required"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.skip",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_conclusion_rejects_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_cli_error_seen_for_conclusion(
        tmp_path,
        error_id=error_id,
        fingerprint="traceback-fingerprint",
    )

    with pytest.raises(SystemExit, match="fingerprint mismatch"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": "different-fingerprint"},
        )


def test_activity_add_error_fix_rejects_already_open_draft(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "pending",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_activity_add_error_fix_rejects_draft_by_head(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"repo": "org/app", "head": "error-fix-aaaaaaaa", "title": "Fix"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        _add_cli_error_conclusion(
            tmp_path,
            typ="error.fix",
            payload={"error_id": error_id, "fingerprint": fingerprint},
        )


def test_task_create_error_id_rejects_already_open_draft(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "pending",
            },
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="already-open-draft"):
        run(
            tmp_path,
            [
                "task",
                "create",
                "--session",
                "error-session",
                "--workflow",
                "implement",
                "--error-id",
                error_id,
                "--title",
                "Fix observed error",
            ],
        )


def test_activity_add_error_fix_after_merged_draft_is_allowed(tmp_path: Path) -> None:
    first_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    second_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=first_id, fingerprint=fingerprint)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "session_id": "error-session",
                "type": "pr.open",
                "payload": {"fingerprint": fingerprint},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
            {
                "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "session_id": "error-session",
                "type": "pr.merged",
                "payload": {"pr_open_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            second_id,
            {
                "id": second_id,
                "session_id": "error-session",
                "type": "error.seen",
                "payload": {"fingerprint": fingerprint, "repo": "org/app"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()

    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": second_id, "fingerprint": fingerprint},
    )


def test_task_create_error_id_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id)
    capsys.readouterr()

    create = [
        "task",
        "create",
        "--session",
        "error-session",
        "--workflow",
        "implement",
        "--error-id",
        error_id,
        "--title",
        "Fix observed error",
    ]
    run(tmp_path, create)
    first_id = _last_task_id(capsys.readouterr().out)
    run(tmp_path, create)
    second_id = _last_task_id(capsys.readouterr().out)

    assert first_id == second_id
    store = Store(tmp_path)
    try:
        assert len(store.rows("task")) == 1
    finally:
        store.close()


def test_activity_add_assigned_ack_requires_queue_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "assigned", "--kind", "runner"])
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "asg-1",
            {
                "id": "asg-1",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-01-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
        store.write(
            "activity",
            "insert",
            "asg-2",
            {
                "id": "asg-2",
                "session_id": "assigned",
                "type": "issue.assigned",
                "payload": {"assigned_at": "2026-02-01T00:00:00Z"},
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    bad = tmp_path / "ack-bad.json"
    bad.write_text('{"assigned_id": "asg-2"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="queue head"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "assigned",
                "--type",
                "issue.assigned.ack",
                "--payload-file",
                str(bad),
            ],
        )
    good = tmp_path / "ack-good.json"
    good.write_text('{"assigned_id": "asg-1"}', encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "assigned",
            "--type",
            "issue.assigned.ack",
            "--payload-file",
            str(good),
        ],
    )
    assert "type=issue.assigned.ack" in capsys.readouterr().out


@pytest.mark.parametrize(
    "typ",
    [
        "pr.merged",
        "mail.ingest",
        "mail.seen",
        "query.result",
        "session.register",
        "usage.snapshot",
        "issue.assigned",
    ],
)
def test_activity_add_rejects_script_only(tmp_path: Path, typ: str) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    payload = tmp_path / "script.json"
    payload.write_text('{"repo": "o/r", "number": 1}', encoding="utf-8")
    with pytest.raises(SystemExit, match="written by a script"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "sess-1",
                "--type",
                typ,
                "--payload-file",
                str(payload),
            ],
        )


def test_open_store_dies_on_legacy_sqlite(tmp_path: Path) -> None:
    (tmp_path / "ledger.sqlite").write_bytes(b"")
    with pytest.raises(SystemExit, match="move it aside"):
        run(tmp_path, ["init"])


def test_activity_add_unknown_type_is_error(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    payload = tmp_path / "x.json"
    payload.write_text('{"k": "v"}', encoding="utf-8")
    run(
        tmp_path,
        [
            "activity",
            "add",
            "--session",
            "sess-1",
            "--type",
            "not-a-catalog-type",
            "--payload-file",
            str(payload),
        ],
    )
    store = Store(tmp_path)
    try:
        rows = store.rows("activity")
        assert len(rows) == 1
        assert rows[0]["execution_status"] == "error"
    finally:
        store.close()


def test_activity_add_rejects_foreign_session(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_replica_row(
            {
                "table": "session",
                "row_id": "foreign",
                "origin_device_id": "other-device",
                "payload": {"id": "foreign", "kind": "human", "status": "active"},
                "updated_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "x", "body": "hello"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="another device"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "foreign",
                "--type",
                "message",
                "--payload-file",
                str(payload),
            ],
        )


def test_activity_add_rejects_closed_session(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human", "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review"])
    run(tmp_path, ["session", "close", "--id", "sess-1"])
    payload = tmp_path / "mail.json"
    payload.write_text('{"to_session": "x", "body": "hello"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="not active"):
        run(
            tmp_path,
            [
                "activity",
                "add",
                "--session",
                "sess-1",
                "--type",
                "message",
                "--payload-file",
                str(payload),
            ],
        )


def test_spine_skill_required_for_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "bare", "--kind", "human"])
    with pytest.raises(SystemExit, match="does not have skill spine"):
        run(tmp_path, ["task", "create", "--session", "bare", "--workflow", "implement", "--title", "Nope"])
    run(tmp_path, ["session", "skill", "list", "--id", "bare"])
    listed = capsys.readouterr().out
    assert "(none)" in listed
    run(tmp_path, ["session", "skill", "attach", "--id", "bare", "--skill", "spine"])
    run(tmp_path, ["session", "skill", "list", "--id", "bare"])
    assert "spine" in capsys.readouterr().out
    run(tmp_path, ["task", "create", "--session", "bare", "--workflow", "implement", "--title", "Yes"])


def test_review_loop_and_pr_review_skills_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "implement", "--title", "T"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="does not have skill review-loop"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--round",
                "1",
            ],
        )
    with pytest.raises(SystemExit, match="does not have skill pr-review"):
        run(
            tmp_path,
            [
                "gate",
                "record",
                "--task",
                tid,
                "--stage",
                "grok-pr",
                "--dimension",
                "quality",
                "--vendor",
                "grok",
                "--verdict",
                "approved",
                "--head",
                "abc1234",
                "--agent",
                str(uuid.uuid4()),
            ],
        )
    with pytest.raises(SystemExit, match="does not have skill pr-review"):
        run(
            tmp_path,
            [
                "agent",
                "start",
                "--session",
                "s",
                "--task",
                tid,
                "--role",
                "pr-reviewer-quality",
                "--vendor",
                "grok",
            ],
        )
    run(tmp_path, ["session", "skill", "attach", "--id", "s", "--skill", "review-loop"])
    run(tmp_path, ["session", "skill", "attach", "--id", "s", "--skill", "pr-review"])


def test_knock_and_watch_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    run(tmp_path, ["knock", "--once"])
    run(tmp_path, ["watch", "pr-merged"])
    out = capsys.readouterr().out
    assert "pr.merged none" in out


def test_watch_usage_mentions_grok_usage() -> None:
    with pytest.raises(SystemExit, match="grok-usage"):
        main(["watch"])
    with pytest.raises(SystemExit, match="grok-usage"):
        main(["watch", "nope"])


def test_watch_assigned_rejects_unknown_flag(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]"):
        run(tmp_path, ["watch", "assigned", "--folow"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]"):
        run(tmp_path, ["watch", "assigned", "--follow", "--follow"])


def test_watch_assigned_requires_watch_json(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match="assigned_repos|watch.json"):
        run(tmp_path, ["watch", "assigned"])


def test_watch_usage_mentions_assigned(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit, match=r"assigned \[--follow\]\|grok-usage"):
        run(tmp_path, ["watch"])


def test_watch_usage_mentions_errors() -> None:
    with pytest.raises(SystemExit, match="errors"):
        main(["watch"])


def test_watch_usage_mentions_error_fix() -> None:
    with pytest.raises(SystemExit, match="error-fix"):
        main(["watch"])


def test_session_close_refuses_pending_error_fix(tmp_path: Path) -> None:
    error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fingerprint = "traceback-fingerprint"
    _seed_cli_error_seen_for_conclusion(tmp_path, error_id=error_id, fingerprint=fingerprint)
    _add_cli_error_conclusion(
        tmp_path,
        typ="error.fix",
        payload={"error_id": error_id, "fingerprint": fingerprint},
    )
    with pytest.raises(SystemExit, match="pending error.fix"):
        run(tmp_path, ["session", "close", "--id", "error-session"])


def test_allow_next_close_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
        ],
    )
    capsys.readouterr()
    run(tmp_path, ["allow", "--action", "claim-done", "--session", "sess-1"])
    assert "allow action=claim-done" in capsys.readouterr().out
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["next", "--task", tid])
    assert "session_registered" in capsys.readouterr().out
    run(
        tmp_path,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "session_registered",
            "--source",
            "script",
            "--evidence",
            "session register",
        ],
    )
    with pytest.raises(SystemExit):
        run(tmp_path, ["allow", "--action", "pr-ready", "--session", "sess-1"])
    run(tmp_path, ["allow", "--action", "pr-create", "--draft", "true"])
    assert "allow action=pr-create" in capsys.readouterr().out
    capsys.readouterr()
    run(tmp_path, ["run", "--task", tid, "--dry-run"])
    dry = capsys.readouterr().out
    assert f"run task={tid}" in dry
    assert "spec_written" in dry


_GATE_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _seed_pr_review_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    repo: str | None = "owner/name",
    ref: str | None = "7",
    verdict: str = "rejected",
) -> tuple[str, str]:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human", "--skill", "spine", "--skill", "pr-review"])
    create = ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"]
    if repo is not None:
        create += ["--repo", repo]
    if ref is not None:
        create += ["--ref", ref]
    run(tmp_path, create)
    tid = _last_task_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "start", "--session", "s", "--task", tid, "--role", "pr-reviewer-quality", "--vendor", "grok"])
    aid = _last_agent_id(capsys.readouterr().out)
    run(tmp_path, ["agent", "finish", "--id", aid, "--verdict", verdict])
    capsys.readouterr()
    return tid, aid


def _gate_argv(tid: str, aid: str, verdict: str, *extra: str, head: str = _GATE_HEAD) -> list[str]:
    return [
        "gate", "record", "--task", tid, "--stage", "grok-pr", "--dimension", "quality",
        "--vendor", "grok", "--verdict", verdict, "--head", head, "--agent", aid, *extra,
    ]


def _comment_activities(tmp_path: Path) -> list[dict]:
    store = Store(tmp_path)
    try:
        return [row for row in store.rows("activity") if row["type"] == "comment.post"]
    finally:
        store.close()


def test_rejected_gate_without_evidence_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    with pytest.raises(SystemExit, match="--evidence is required"):
        run(tmp_path, _gate_argv(tid, aid, "rejected"))
    assert _comment_activities(tmp_path) == []


def test_rejected_gate_queues_its_findings_for_the_pull_request(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()"))
    out = capsys.readouterr().out
    assert "gate grok-pr/quality=rejected" in out
    assert "type=comment.post" in out
    rows = _comment_activities(tmp_path)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["repo"] == "owner/name"
    assert payload["number"] == 7
    assert payload["target"] == "pr"
    assert "dto.ts:91 keeps @IsOptional()" in payload["body"]
    assert _GATE_HEAD in payload["body"]
    assert rows[0]["execution_status"] == "pending"


def test_approved_gate_queues_no_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys, verdict="approved")
    run(tmp_path, _gate_argv(tid, aid, "approved"))
    out = capsys.readouterr().out
    assert "gate grok-pr/quality=approved" in out
    assert "type=comment.post" not in out
    assert _comment_activities(tmp_path) == []


def test_rejected_gate_without_a_pull_request_queues_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys, repo=None, ref=None)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "no pull request yet"))
    out = capsys.readouterr().out
    assert "gate grok-pr/quality=rejected" in out
    assert "type=comment.post" not in out
    assert "no pull request on this task: findings not queued" in out
    assert _comment_activities(tmp_path) == []


def test_rejected_gate_with_blank_evidence_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    with pytest.raises(SystemExit, match="--evidence is required"):
        run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "   \t "))
    assert _comment_activities(tmp_path) == []


def test_rejected_gate_recorded_twice_queues_one_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    first = capsys.readouterr().out
    run(tmp_path, argv)
    second = capsys.readouterr().out
    assert "type=comment.post" in first
    assert "type=comment.post" not in second
    assert len(_comment_activities(tmp_path)) == 1


def test_rejected_gate_requeues_a_comment_the_executor_gave_up_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    rows = _comment_activities(tmp_path)
    assert len(rows) == 1
    activity_id = rows[0]["id"]

    # github_act marks a transient GitHub failure as `error`; scan_github only
    # picks up pending work, so nothing would retry it on its own.
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
        assert row is not None
        store.write(
            "activity",
            "update",
            activity_id,
            {
                "id": activity_id,
                "session_id": row["session_id"],
                "type": row["type"],
                "payload": row["payload"],
                "execution_status": "error",
            },
        )
    finally:
        store.close()

    run(tmp_path, argv)
    out = capsys.readouterr().out
    assert "type=comment.post" in out
    rows = _comment_activities(tmp_path)
    assert len(rows) == 1
    assert rows[0]["id"] == activity_id
    assert rows[0]["execution_status"] == "pending"


def test_rejected_gate_survives_a_stale_existence_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two callers can record the same rejection. One inserts the deterministic
    # activity and the executor marks it `error` before the other takes the lock,
    # so the second caller's insert/update choice is already stale when it writes.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    activity_id = _comment_activities(tmp_path)[0]["id"]

    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
        assert row is not None
        store.write(
            "activity",
            "update",
            activity_id,
            {
                "id": activity_id,
                "session_id": row["session_id"],
                "type": row["type"],
                "payload": row["payload"],
                "execution_status": "error",
            },
        )
    finally:
        store.close()

    real_row = Store.row
    seen: list[int] = []

    def _stale_op_read(self: Store, table: str, row_id: str):
        # Call 1 is `_settled()`, which must see the errored row so the retry
        # proceeds at all. Call 2 is the insert/update choice — that is the read a
        # concurrent insert makes stale, so it alone reports the row as absent.
        if table == "activity" and row_id == activity_id:
            seen.append(1)
            if len(seen) == 2:
                return None
        return real_row(self, table, row_id)

    monkeypatch.setattr(Store, "row", _stale_op_read)

    run(tmp_path, argv)
    out = capsys.readouterr().out
    assert "type=comment.post" in out
    rows = _comment_activities(tmp_path)
    assert len(rows) == 1
    assert rows[0]["execution_status"] == "pending"


def test_rejected_gate_does_not_retry_a_failed_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recovery exists only for a stale `insert`. An `update` that fails is a real
    # failure: retrying it repeats the same write and buries the original error.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    argv = _gate_argv(tid, aid, "rejected", "--evidence", "dto.ts:91 keeps @IsOptional()")
    run(tmp_path, argv)
    capsys.readouterr()
    activity_id = _comment_activities(tmp_path)[0]["id"]

    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
        assert row is not None
        store.write(
            "activity",
            "update",
            activity_id,
            {
                "id": activity_id,
                "session_id": row["session_id"],
                "type": row["type"],
                "payload": row["payload"],
                "execution_status": "error",
            },
        )
    finally:
        store.close()

    ops: list[str] = []

    def _refuse(self: Store, table: str, op: str, row_id: str, payload: dict, **kwargs: object):
        ops.append(op)
        raise StoreError("write refused")

    monkeypatch.setattr(Store, "write_with_advisory", _refuse)

    with pytest.raises((SystemExit, StoreError)):
        run(tmp_path, argv)
    assert ops == ["update"]


def test_a_second_rejection_with_new_findings_queues_its_own_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same task, lane and head, different findings. Keying only on the lane would
    # treat the second rejection as a repeat and drop its evidence.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "first finding"))
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", "corrected finding"))
    capsys.readouterr()
    bodies = sorted(row["payload"]["body"] for row in _comment_activities(tmp_path))
    assert len(bodies) == 2
    assert any("first finding" in b for b in bodies)
    assert any("corrected finding" in b for b in bodies)


def test_the_same_finding_at_a_new_head_queues_its_own_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The head is part of the comment's identity: the same defect surviving a new
    # push is a new rejection to report, not a repeat of the one already posted.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    evidence = "dto.ts:91 keeps @IsOptional()"
    other_head = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence, head=other_head))
    capsys.readouterr()
    rows = _comment_activities(tmp_path)
    assert len(rows) == 2
    bodies = sorted(row["payload"]["body"] for row in rows)
    assert any(_GATE_HEAD in b for b in bodies)
    assert any(other_head in b for b in bodies)


def test_a_retargeted_task_queues_a_comment_on_the_new_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same lane, head and evidence, different destination. Keying only on the
    # content would treat the retargeted comment as one already delivered.
    tid, aid = _seed_pr_review_gate(tmp_path, capsys)
    evidence = "dto.ts:91 keeps @IsOptional()"
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    capsys.readouterr()

    def _retarget(repo: str, ref: str) -> None:
        store = Store(tmp_path)
        try:
            task = store.row("task", tid)
            assert task is not None
            task["repo"] = repo
            task["ref"] = ref
            task["updated_at"] = utcnow()
            store.write("task", "update", tid, task)
        finally:
            store.close()

    # Move the number alone, then the repository alone. Changing both at once would
    # leave either one missing from the key undetected: the other still separates them.
    _retarget("owner/name", "99")
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    _retarget("owner/other", "99")
    run(tmp_path, _gate_argv(tid, aid, "rejected", "--evidence", evidence))
    capsys.readouterr()
    rows = _comment_activities(tmp_path)
    targets = sorted((row["payload"]["repo"], row["payload"]["number"]) for row in rows)
    assert targets == [("owner/name", 7), ("owner/name", 99), ("owner/other", 99)]
