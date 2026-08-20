from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_cli.main import main
from agent_cli.store import Store, utcnow


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
