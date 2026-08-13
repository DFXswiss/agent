from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.main import main
from agent_cli.store import Store


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
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
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
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    with pytest.raises(SystemExit, match="open tasks"):
        run(tmp_path, ["session", "close", "--id", "s"])


def test_happy_path_round_agent_check_work(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
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
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
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
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
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
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
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
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    tid = _last_task_id(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="round start requires workflow"):
        run(tmp_path, ["round", "start", "--task", tid])


def test_done_requires_gates_after_summaries_and_checklist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
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
            "Adds complete ledger CLI.",
            "--de",
            "Ergaenzt die vollstaendige Ledger-CLI.",
        ],
    )
    capsys.readouterr()

    store = Store(tmp_path / "ledger.sqlite")
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
