from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.main import main


def run(home: Path, argv: list[str]) -> None:
    import os

    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def test_session_task_checklist_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    run(tmp_path, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship sync"])
    out = capsys.readouterr().out
    task_line = [ln for ln in out.splitlines() if ln.startswith("task ")][-1]
    tid = task_line.split()[1]
    run(tmp_path, ["checklist", "set", "--task", tid, "--key", "session_registered", "--status", "ja", "--source", "script"])
    run(tmp_path, ["status"])
    status = capsys.readouterr().out
    assert "tasks_open=1" in status
    assert "Ship sync" in status
    with pytest.raises(SystemExit, match="summaries missing"):
        run(tmp_path, ["task", "state", tid, "done"])


def test_cannot_close_with_open_task(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s", "--kind", "human"])
    run(tmp_path, ["task", "create", "--session", "s", "--workflow", "review", "--title", "Look"])
    with pytest.raises(SystemExit, match="open tasks"):
        run(tmp_path, ["session", "close", "--id", "s"])
