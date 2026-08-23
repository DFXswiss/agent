from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli.lane import LaneResult
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_cli import _last_agent_id, _last_task_id, run


def _store(home: Path) -> Store:
    os.environ["AGENT_HOME"] = str(home)
    return Store(home)


def _checklist(home: Path, tid: str) -> dict[str, str]:
    store = _store(home)
    try:
        return {
            str(r["key"]): str(r["status"])
            for r in store.rows("checklist_item")
            if r.get("task_id") == tid
        }
    finally:
        store.close()


def _local_checks(home: Path, tid: str) -> list[dict]:
    store = _store(home)
    try:
        return [r for r in store.rows("local_check") if r.get("task_id") == tid]
    finally:
        store.close()


def _agents(home: Path, tid: str) -> list[dict]:
    store = _store(home)
    try:
        return [r for r in store.rows("agent") if r.get("task_id") == tid]
    finally:
        store.close()


def _task_state(home: Path, tid: str) -> str:
    store = _store(home)
    try:
        row = store.row("task", tid)
        assert row is not None
        return str(row["state"])
    finally:
        store.close()


def _bootstrap_implement(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    run(home, ["init"])
    run(
        home,
        [
            "session",
            "register",
            "--id",
            "sess-1",
            "--kind",
            "human",
            "--skill",
            "spine",
            "--skill",
            "review-loop",
            "--skill",
            "pr-review",
        ],
    )
    run(home, ["task", "create", "--session", "sess-1", "--workflow", "implement", "--title", "Ship"])
    tid = _last_task_id(capsys.readouterr().out)
    run(
        home,
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
    run(
        home,
        [
            "close-step",
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "human",
            "--evidence",
            "spec",
        ],
    )
    run(home, ["round", "start", "--task", tid])
    capsys.readouterr()
    return tid


def _finish_implementer(home: Path, tid: str, capsys: pytest.CaptureFixture[str]) -> None:
    run(
        home,
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
    run(home, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
    capsys.readouterr()


def _finish_reviewer(home: Path, tid: str, capsys: pytest.CaptureFixture[str]) -> None:
    run(
        home,
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
    run(home, ["agent", "finish", "--id", rev_id, "--verdict", "approved"])
    capsys.readouterr()


def test_run_closes_implementer_done_when_artifact_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["implementer_done"] == "ja"


def test_run_agent_without_artifact_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid])
    assert exc.value.code == 2
    assert _checklist(tmp_path, tid)["implementer_done"] != "ja"


def test_run_local_check_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # closes implementer_done
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # closes reviewer_approved
    capsys.readouterr()

    seen: list[list[str]] = []

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        seen.append(list(argv))
        return Completed(0, "ok", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    run(tmp_path, ["run", "--task", tid])
    out = capsys.readouterr().out
    assert "local_check_pass" in out
    assert seen
    assert seen[0][0] == "pytest"
    assert _checklist(tmp_path, tid)["local_check_pass"] == "ja"
    assert any(
        c.get("name") == "local" and c.get("result") == "pass"
        for c in _local_checks(tmp_path, tid)
    )


def test_run_local_check_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()

    monkeypatch.setattr(
        "agent_cli.main._exec_argv",
        lambda argv, *, cwd=None: Completed(1, "", "boom"),
    )
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid])
    assert exc.value.code == 2
    assert _task_state(tmp_path, tid) == "failed"
    assert _checklist(tmp_path, tid)["local_check_pass"] != "ja"
    assert any(c.get("result") == "fail" for c in _local_checks(tmp_path, tid))


def test_run_agent_check_command_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()

    seen: list[list[str]] = []

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        seen.append(list(argv))
        return Completed(0, "", "")

    monkeypatch.setenv("AGENT_CHECK_COMMAND", "true")
    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    run(tmp_path, ["run", "--task", tid])
    assert seen == [["true"]]


def test_run_dry_run_skips_local_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()

    called = {"n": 0}

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        called["n"] += 1
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    run(tmp_path, ["run", "--task", tid, "--dry-run"])
    out = capsys.readouterr().out
    assert "local_check_pass" in out
    assert called["n"] == 0


def test_run_spec_file_implementer_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        return LaneResult(
            role="implementer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    run(
        tmp_path,
        [
            "run",
            "--task",
            tid,
            "--spec-file",
            str(spec),
            "--no-tmux",
            "--cwd",
            str(tmp_path),
        ],
    )
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["implementer_done"] == "ja"
    assert any(
        a.get("role") == "implementer" and a.get("status") == "done"
        for a in _agents(tmp_path, tid)
    )


def test_run_spec_file_reviewer_complete_no_auto_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    with pytest.raises(SystemExit) as exc:
        run(
            tmp_path,
            [
                "run",
                "--task",
                tid,
                "--spec-file",
                str(spec),
                "--no-tmux",
                "--cwd",
                str(tmp_path),
            ],
        )
    assert exc.value.code == 2
    assert _checklist(tmp_path, tid)["reviewer_approved"] != "ja"
    for agent in _agents(tmp_path, tid):
        if agent.get("role") != "reviewer":
            continue
        assert agent.get("status") == "working"
