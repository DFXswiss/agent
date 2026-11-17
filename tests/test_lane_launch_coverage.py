"""Remaining paths of ``lane.launch`` and ``agent run`` (``cmd_run``).

The model provider, the GitHub push and the merge probe are mocked; the store
is the real PostgreSQL store of the test session.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_cli.ai_accounts import load_ai_accounts
from agent_cli.chain import next_steps
from agent_cli.git_act import GitActError
from agent_cli.lane import LANE_ROLES, LaneResult, launch, tmux_wrap_argv
from agent_cli.runtime import Completed
from driver_fixtures import write_accounts, write_drivers
from github_support import configure_accounts
from test_cli import _last_agent_id, _last_task_id, run
from test_run import (
    DEFAULT_SESSION,
    OPERATOR_MODEL_A,
    _advance_to_pushed,
    _agents,
    _bootstrap_implement,
    _bootstrap_resolve,
    _checklist,
    _finish_implementer,
    _finish_reviewer,
    _local_checks,
    _record_pr_gate,
    _store,
    write_operator_ai_accounts,
)

PUSHED_SHA = "abc1234"


# --- lane.launch -------------------------------------------------------------


def _lane_home(tmp_path: Path) -> tuple[Path, Path]:
    """Placeholder drivers and accounts, a work directory and a spec file."""
    write_drivers(tmp_path)
    write_accounts(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    return work, spec


@pytest.mark.no_pg
def test_launch_refuses_an_unknown_role(tmp_path: Path) -> None:
    work, spec = _lane_home(tmp_path)
    with pytest.raises(SystemExit) as refused:
        launch(
            role="architect",
            vendor="driver-c",
            spec_file=str(spec),
            cwd=str(work),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id="session-a",
        )
    assert str(refused.value) == f"role must be {'|'.join(LANE_ROLES)}"
    assert str(refused.value) == (
        "role must be implementer|reviewer|pr-reviewer-quality|pr-reviewer-logic"
    )


@pytest.mark.no_pg
def test_launch_refuses_a_binding_of_another_provider(tmp_path: Path) -> None:
    work, spec = _lane_home(tmp_path)
    ai = load_ai_accounts(tmp_path)
    with pytest.raises(SystemExit) as refused:
        launch(
            role="implementer",
            vendor="driver-c",
            spec_file=str(spec),
            cwd=str(work),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id="session-a",
            binding=ai.roles["driver-a-writer"],
        )
    assert str(refused.value) == "lane binding provider does not match requested vendor"


@pytest.mark.no_pg
def test_launch_refuses_an_empty_spec_file(tmp_path: Path) -> None:
    work, _spec = _lane_home(tmp_path)
    empty = tmp_path / "empty-spec.md"
    empty.write_text("  \n\t\n", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        launch(
            role="implementer",
            vendor="driver-c",
            spec_file=str(empty),
            cwd=str(work),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id="session-a",
        )
    assert str(refused.value) == f"spec-file is empty: {empty}"


@pytest.mark.no_pg
def test_launch_interrupted_while_wrapping_for_tmux_removes_the_empty_github_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, spec = _lane_home(tmp_path)
    profiles: list[Path] = []

    def interrupted_redirect(_io: Any, argv: list[str], **_kwargs: Any) -> list[str]:
        # An interrupt arrives while the tmux argv is built. The empty GitHub
        # profile of this launch exists at that moment.
        assignment = next(value for value in argv if value.startswith("GH_CONFIG_DIR="))
        profile = Path(assignment.split("=", 1)[1])
        assert profile.is_dir()
        profiles.append(profile)
        raise KeyboardInterrupt("interrupted while wrapping")

    monkeypatch.setattr("agent_cli.lane.redirect_argv", interrupted_redirect)
    with pytest.raises(KeyboardInterrupt) as interrupted:
        launch(
            role="implementer",
            vendor="driver-c",
            spec_file=str(spec),
            cwd=str(work),
            dry_run=True,
            tmux=True,
            config_home=tmp_path,
            session_id="session-a",
        )
    assert str(interrupted.value) == "interrupted while wrapping"
    assert len(profiles) == 1
    assert not profiles[0].exists()


def _github_profile(argv: list[str]) -> Path:
    assignment = next(value for value in argv if value.startswith("GH_CONFIG_DIR="))
    return Path(assignment.split("=", 1)[1])


def _no_tmux_session(*_args: Any, **_kwargs: Any) -> Completed:
    raise AssertionError("no tmux session may start")


def _launch_in_tmux(work: Path, spec: Path, home: Path) -> LaneResult:
    return launch(
        role="implementer",
        vendor="driver-c",
        spec_file=str(spec),
        cwd=str(work),
        tmux=True,
        config_home=home,
        session_id="session-a",
    )


@pytest.mark.no_pg
def test_launch_refuses_a_tmux_argv_without_the_command_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmux_wrap_argv always puts "--" before the lane command today. A wrapped
    # argv without it cannot be split into the inner command and is refused.
    work, spec = _lane_home(tmp_path)
    profiles: list[Path] = []

    def without_separator(inner: list[str], *, name: str, cwd: str) -> list[str]:
        profiles.append(_github_profile(inner))
        return [
            "tmux", "new-session", "-d", "-s", name, "-c", cwd,
            *(value for value in inner if value != "--"),
        ]

    monkeypatch.setattr("agent_cli.lane.tmux_wrap_argv", without_separator)
    monkeypatch.setattr("agent_cli.lane._run_in_tmux", _no_tmux_session)
    with pytest.raises(SystemExit) as refused:
        _launch_in_tmux(work, spec, tmp_path)
    assert type(refused.value) is SystemExit
    assert str(refused.value) == "tmux argv missing command separator"
    # The empty GitHub profile of this launch is removed on the refusal.
    (profile,) = profiles
    assert not profile.exists()


@pytest.mark.no_pg
def test_launch_refuses_a_tmux_lane_without_a_session_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lane_tmux_name never returns an empty name today; a lane that still has
    # no tmux session name cannot be run or read back and is refused.
    work, spec = _lane_home(tmp_path)
    profiles: list[Path] = []

    def recording_wrap(inner: list[str], *, name: Any, cwd: str) -> list[str]:
        assert name is None
        profiles.append(_github_profile(inner))
        return tmux_wrap_argv(inner, name=name, cwd=cwd)

    monkeypatch.setattr("agent_cli.lane.lane_tmux_name", lambda **_kwargs: None)
    monkeypatch.setattr("agent_cli.lane.tmux_wrap_argv", recording_wrap)
    monkeypatch.setattr("agent_cli.lane._run_in_tmux", _no_tmux_session)
    with pytest.raises(SystemExit) as refused:
        _launch_in_tmux(work, spec, tmp_path)
    assert type(refused.value) is SystemExit
    assert str(refused.value) == "tmux session name missing"
    (profile,) = profiles
    assert not profile.exists()


# --- cmd_run -----------------------------------------------------------------


def _complete_implementer(**_kwargs: Any) -> LaneResult:
    return LaneResult(
        role="implementer",
        vendor="driver-a",
        model=OPERATOR_MODEL_A,
        status="complete",
        argv=["tool-a"],
        returncode=0,
        stdout="STATUS: complete\nRESULT: done\n",
        stderr="",
        protocol_result="done",
    )


def _spec(home: Path, text: str = "implement this\n") -> Path:
    spec = home / "spec.md"
    spec.write_text(text, encoding="utf-8")
    return spec


def _run_spec_args(tid: str, spec: Path, home: Path) -> list[str]:
    return ["run", "--task", tid, "--spec-file", str(spec), "--no-tmux", "--cwd", str(home)]


def _close_working_rows(home: Path, tid: str, role: str, note: str) -> list[str]:
    """End the task's working rows of ``role`` as another process would."""
    store = _store(home)
    closed_ids: list[str] = []
    try:
        for row in store.rows("agent"):
            if (
                row.get("task_id") == tid
                and row.get("role") == role
                and row.get("status") == "working"
            ):
                closed = {k: v for k, v in row.items() if not k.startswith("_")}
                closed["status"] = "done"
                closed["note"] = note
                store.write("agent", "update", str(row["id"]), closed)
                closed_ids.append(str(row["id"]))
    finally:
        store.close()
    return closed_ids


def _lane_run_rows(home: Path, tid: str) -> list[dict[str, Any]]:
    store = _store(home)
    try:
        return [
            row
            for row in store.rows("activity")
            if row.get("type") == "lane.run"
            and (row.get("payload") or {}).get("task_id") == tid
        ]
    finally:
        store.close()


def _set_task_fields(home: Path, tid: str, **fields: Any) -> None:
    store = _store(home)
    try:
        row = store.row("task", tid)
        assert row is not None
        updated = {k: v for k, v in row.items() if not k.startswith("_")}
        updated.update(fields)
        store.write("task", "update", tid, updated)
    finally:
        store.close()


def _push_and_pass_gates(
    home: Path,
    tid: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From an open ``local_check_pass`` step to approved review gates."""
    _advance_to_pushed(home, tid, capsys, monkeypatch)
    configure_accounts(home, [DEFAULT_SESSION], login="ok")
    monkeypatch.setattr("agent_cli.git_act.push_branch", lambda *, cwd, runner: PUSHED_SHA)
    run(home, ["run", "--task", tid, "--head", PUSHED_SHA])
    capsys.readouterr()
    assert _checklist(home, tid)["pushed"] == "ja"
    for stage, vendor in (("review-1", "driver-a"), ("review-2", "driver-b")):
        for dimension in ("quality", "logic"):
            _record_pr_gate(
                home, tid, capsys, stage=stage, dimension=dimension, vendor=vendor
            )
            run(home, ["run", "--task", tid])
            capsys.readouterr()
    checklist = _checklist(home, tid)
    for key in ("review_1_quality", "review_1_logic", "review_2_quality", "review_2_logic"):
        assert checklist[key] == "ja"


def _to_reviewer_step(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    tid = _bootstrap_implement(home, capsys)
    _finish_implementer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(home, tid)["implementer_done"] == "ja"
    return tid


def _to_local_check_step(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    tid = _to_reviewer_step(home, capsys)
    _finish_reviewer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(home, tid)["reviewer_approved"] == "ja"
    return tid


def test_run_without_a_task_prints_the_usage(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    with pytest.raises(SystemExit) as usage:
        run(tmp_path, ["run"])
    assert str(usage.value) == (
        "agent: Usage: agent run --task ID [--dry-run] [--head SHA] "
        "[--cwd PATH] [--spec-file PATH] [--no-tmux]"
    )


def test_run_stops_at_a_human_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, ["init"])
    run(
        tmp_path,
        [
            "session", "register", "--id", DEFAULT_SESSION, "--kind", "human",
            "--skill", "spine", "--skill", "review-loop", "--skill", "pr-review",
        ],
    )
    run(
        tmp_path,
        ["task", "create", "--session", DEFAULT_SESSION, "--workflow", "implement", "--title", "Ship"],
    )
    tid = _last_task_id(capsys.readouterr().out)
    run(
        tmp_path,
        [
            "close-step", "--task", tid, "--key", "session_registered",
            "--source", "script", "--evidence", "session register",
        ],
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as stopped:
        run(tmp_path, ["run", "--task", tid])
    captured = capsys.readouterr()
    assert stopped.value.code == 2
    assert f"run task={tid} spec_written kind=human source=human" in captured.out
    assert (
        "agent: human must close spec_written (close-step --source human)"
        in captured.err.splitlines()
    )
    assert _checklist(tmp_path, tid).get("spec_written") != "ja"


def test_run_refuses_an_empty_check_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _to_local_check_step(tmp_path, capsys)

    def no_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        raise AssertionError(f"no check command may run: {argv}")

    monkeypatch.setattr("agent_cli.main._exec_argv", no_exec)
    monkeypatch.setenv("AGENT_CHECK_COMMAND", "")
    with pytest.raises(SystemExit) as empty:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    assert str(empty.value) == "agent: AGENT_CHECK_COMMAND is set but empty"

    # A command of only blanks splits into no argv at all.
    monkeypatch.setenv("AGENT_CHECK_COMMAND", "   ")
    with pytest.raises(SystemExit) as blank:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    assert str(blank.value) == "agent: check command is empty"
    assert _local_checks(tmp_path, tid) == []
    assert _checklist(tmp_path, tid).get("local_check_pass") != "ja"


def test_run_reports_a_failed_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, [DEFAULT_SESSION], login="ok")

    def rejected(*, cwd: str, runner: Any) -> str:
        raise GitActError("push rejected by the remote")

    monkeypatch.setattr("agent_cli.git_act.push_branch", rejected)
    with pytest.raises(SystemExit) as failed:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    assert str(failed.value) == "agent: push rejected by the remote"
    assert _checklist(tmp_path, tid).get("pushed") != "ja"


def test_run_refuses_a_head_that_differs_from_the_pushed_sha(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, [DEFAULT_SESSION], login="ok")
    monkeypatch.setattr("agent_cli.git_act.push_branch", lambda *, cwd, runner: PUSHED_SHA)
    with pytest.raises(SystemExit) as mismatch:
        run(tmp_path, ["run", "--task", tid, "--head", "fff9999", "--cwd", str(tmp_path)])
    assert str(mismatch.value) == (
        f"agent: --head fff9999 does not match pushed sha {PUSHED_SHA}"
    )
    assert _checklist(tmp_path, tid).get("pushed") != "ja"


def test_run_refuses_to_auto_close_a_real_check_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _push_and_pass_gates(tmp_path, tid, capsys, monkeypatch)
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, ["run", "--task", tid])
    captured = capsys.readouterr()
    assert refused.value.code == 2
    assert f"run task={tid} contributing_ok kind=script source=script" in captured.out
    assert (
        "agent: contributing_ok is not auto-closable — "
        "close-step --source script --evidence …"
    ) in captured.err.splitlines()
    assert _checklist(tmp_path, tid).get("contributing_ok") != "ja"


def test_run_measures_the_task_pull_request_and_then_is_idle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_resolve(tmp_path, capsys)
    _push_and_pass_gates(tmp_path, tid, capsys, monkeypatch)
    _set_task_fields(tmp_path, tid, repo="example/project", ref="42")
    seen: list[dict[str, Any]] = []

    def measure(
        *, cwd: str, runner: Any, expected_head: str | None = None,
        repo: str | None = None, number: int | None = None,
    ) -> str:
        seen.append({"cwd": cwd, "repo": repo, "number": number})
        return f"mergeable at {PUSHED_SHA}"

    monkeypatch.setattr("agent_cli.git_act.measure_mergeable", measure)
    run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    capsys.readouterr()
    assert seen == [
        {"cwd": str(tmp_path.resolve()), "repo": "example/project", "number": 42}
    ]
    assert _checklist(tmp_path, tid)["mergeable"] == "ja"

    # Every spine step is closed: nothing is ready any more.
    run(tmp_path, ["run", "--task", tid])
    out = capsys.readouterr().out
    assert f"run task={tid} idle" in out.splitlines()
    assert len(seen) == 1


def test_run_reports_a_failed_merge_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_resolve(tmp_path, capsys)
    _push_and_pass_gates(tmp_path, tid, capsys, monkeypatch)

    def conflicting(**_kwargs: Any) -> str:
        raise GitActError("branch has conflicts with its base")

    monkeypatch.setattr("agent_cli.git_act.measure_mergeable", conflicting)
    with pytest.raises(SystemExit) as failed:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    assert str(failed.value) == "agent: branch has conflicts with its base"
    assert _checklist(tmp_path, tid).get("mergeable") != "ja"


def test_run_refuses_a_script_step_whose_evidence_is_not_closable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_resolve(tmp_path, capsys)
    _push_and_pass_gates(tmp_path, tid, capsys, monkeypatch)
    # The merge probe answers without evidence.
    monkeypatch.setattr("agent_cli.git_act.measure_mergeable", lambda **_kwargs: "  ")
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    captured = capsys.readouterr()
    assert refused.value.code == 2
    assert (
        "agent: script step mergeable not closable: mergeable requires evidence"
        in captured.err.splitlines()
    )
    assert _checklist(tmp_path, tid).get("mergeable") != "ja"


def test_run_refuses_an_empty_spec_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = _spec(tmp_path, " \n\n")

    def no_launch(**kwargs: Any) -> LaneResult:
        raise AssertionError(f"an empty spec started a lane: {kwargs}")

    monkeypatch.setattr("agent_cli.main.launch", no_launch)
    with pytest.raises(SystemExit) as empty:
        run(tmp_path, _run_spec_args(tid, spec, tmp_path))
    assert str(empty.value) == f"agent: spec-file is empty: {spec}"
    assert not any(agent.get("role") == "implementer" for agent in _agents(tmp_path, tid))


def test_run_reuses_the_working_implementer_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    run(
        tmp_path,
        [
            "agent", "start", "--session", DEFAULT_SESSION, "--task", tid,
            "--role", "implementer", "--vendor", "driver-a", "--round", "1",
        ],
    )
    working_id = _last_agent_id(capsys.readouterr().out)
    launches: list[dict[str, Any]] = []

    def fake_launch(**kwargs: Any) -> LaneResult:
        launches.append(kwargs)
        return _complete_implementer()

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    run(tmp_path, _run_spec_args(tid, _spec(tmp_path), tmp_path))
    capsys.readouterr()
    assert len(launches) == 1
    implementers = [
        agent for agent in _agents(tmp_path, tid) if agent.get("role") == "implementer"
    ]
    # No second row was started: the lane ran as the existing working row.
    assert [agent["id"] for agent in implementers] == [working_id]
    assert implementers[0]["status"] == "done"
    assert _checklist(tmp_path, tid)["implementer_done"] == "ja"
    runs = _lane_run_rows(tmp_path, tid)
    assert len(runs) == 1
    assert runs[0]["payload"]["agent_id"] == working_id


@pytest.mark.parametrize(
    ("status", "protocol_result"),
    [("complete", "done"), ("partial", "")],
)
def test_run_refuses_an_implementer_lane_whose_row_ended_elsewhere(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    protocol_result: str,
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    closed: list[str] = []

    def closing_launch(**_kwargs: Any) -> LaneResult:
        closed.extend(
            _close_working_rows(tmp_path, tid, "implementer", "ended by an operator")
        )
        return LaneResult(
            role="implementer",
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status=status,
            argv=["tool-a"],
            returncode=0,
            stdout=f"STATUS: {status}\n",
            stderr="",
            protocol_result=protocol_result,
        )

    monkeypatch.setattr("agent_cli.main.launch", closing_launch)
    with pytest.raises(SystemExit) as missing:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path), tmp_path))
    assert str(missing.value) == "agent: implementer working agent not found after lane"
    assert len(closed) == 1
    implementers = [
        agent for agent in _agents(tmp_path, tid) if agent.get("role") == "implementer"
    ]
    assert [(agent["id"], agent["status"], agent["note"]) for agent in implementers] == [
        (closed[0], "done", "ended by an operator")
    ]
    assert _checklist(tmp_path, tid).get("implementer_done") != "ja"
    assert len(_lane_run_rows(tmp_path, tid)) == 1


def test_run_refuses_an_incomplete_reviewer_lane_whose_row_ended_elsewhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _to_reviewer_step(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    closed: list[str] = []

    def closing_launch(**_kwargs: Any) -> LaneResult:
        closed.extend(
            _close_working_rows(tmp_path, tid, "reviewer", "ended by an operator")
        )
        return LaneResult(
            role="reviewer",
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="unavailable",
            argv=["tool-a"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        )

    monkeypatch.setattr("agent_cli.main.launch", closing_launch)
    with pytest.raises(SystemExit) as missing:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path, "review this\n"), tmp_path))
    assert str(missing.value) == "agent: reviewer working agent not found after lane"
    assert len(closed) == 1
    reviewers = [
        agent for agent in _agents(tmp_path, tid) if agent.get("role") == "reviewer"
    ]
    assert [(agent["status"], agent["note"]) for agent in reviewers] == [
        ("done", "ended by an operator")
    ]
    assert _checklist(tmp_path, tid).get("reviewer_approved") != "ja"


def test_run_unavailable_pr_reviewer_without_head_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(
        tmp_path,
        sessions={
            DEFAULT_SESSION: {
                "interactive": "run-builder",
                "lanes": {
                    "driver-a:implementer": "run-builder",
                    "driver-a:reviewer": "run-reader",
                    "driver-a:pr-reviewer-quality": "run-reader",
                },
            }
        },
    )
    tid = _bootstrap_resolve(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, [DEFAULT_SESSION], login="ok")
    monkeypatch.setattr("agent_cli.git_act.push_branch", lambda *, cwd, runner: PUSHED_SHA)
    run(tmp_path, ["run", "--task", tid, "--head", PUSHED_SHA])
    capsys.readouterr()
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="pr-reviewer-quality",
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="unavailable",
            argv=["tool-a"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        ),
    )
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path, "review quality\n"), tmp_path))
    assert str(refused.value) == (
        "agent: pr-reviewer-quality unavailable result requires --head"
    )
    assert _checklist(tmp_path, tid).get("review_1_quality") != "unavailable"
    store = _store(tmp_path)
    try:
        gates = [gate for gate in store.rows("review_gate") if gate.get("task_id") == tid]
    finally:
        store.close()
    assert gates == []


# --- cmd_run: fail-closed guards the current chain never reaches -------------


def _no_launch(**kwargs: Any) -> LaneResult:
    raise AssertionError(f"no lane may start: {kwargs}")


def _review_gates(home: Path, tid: str) -> list[dict[str, Any]]:
    store = _store(home)
    try:
        return [gate for gate in store.rows("review_gate") if gate.get("task_id") == tid]
    finally:
        store.close()


def _to_pr_review_step(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> str:
    """A pushed task whose next step is the first-slot quality PR review."""
    write_operator_ai_accounts(
        home,
        sessions={
            DEFAULT_SESSION: {
                "interactive": "run-builder",
                "lanes": {
                    "driver-a:implementer": "run-builder",
                    "driver-a:reviewer": "run-reader",
                    "driver-a:pr-reviewer-quality": "run-reader",
                },
            }
        },
    )
    tid = _bootstrap_resolve(home, capsys)
    _advance_to_pushed(home, tid, capsys, monkeypatch)
    configure_accounts(home, [DEFAULT_SESSION], login="ok")
    monkeypatch.setattr("agent_cli.git_act.push_branch", lambda *, cwd, runner: PUSHED_SHA)
    run(home, ["run", "--task", tid, "--head", PUSHED_SHA])
    capsys.readouterr()
    assert _checklist(home, tid)["pushed"] == "ja"
    return tid


def test_run_refuses_an_agent_step_without_a_model_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every agent step of the chain has a slot, so step_vendor always names a
    # provider today. A step it resolves to no provider is refused before any
    # agent row is written.
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    resolved: list[Any] = []

    def no_provider(step: Any, _models: Any, *, snapshot: Any = None) -> str | None:
        resolved.append(step)
        return None

    monkeypatch.setattr("agent_cli.main.step_vendor", no_provider)
    monkeypatch.setattr("agent_cli.main.launch", _no_launch)
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path), tmp_path))
    assert str(refused.value) == "agent: implementer_done has no model provider"
    (step,) = resolved
    # A first-slot step: the external-second refusal does not apply.
    assert (step.key, step.slot) == ("implementer_done", "first")
    assert not any(agent.get("role") == "implementer" for agent in _agents(tmp_path, tid))
    assert _checklist(tmp_path, tid).get("implementer_done") != "ja"


def test_run_refuses_to_start_a_pr_reviewer_of_a_slot_without_a_review_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every review slot maps to a stage today. A slot missing from the table
    # is refused before the PR reviewer row is started.
    tid = _to_pr_review_step(tmp_path, capsys, monkeypatch)
    monkeypatch.setattr("agent_cli.main.SLOT_STAGES", {})
    monkeypatch.setattr("agent_cli.main.launch", _no_launch)
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path, "review quality\n"), tmp_path))
    assert str(refused.value) == "agent: review_1_quality has no review stage"
    assert not any(
        agent.get("role") == "pr-reviewer-quality" for agent in _agents(tmp_path, tid)
    )
    assert _checklist(tmp_path, tid).get("review_1_quality") != "ja"
    assert _review_gates(tmp_path, tid) == []


def test_run_refuses_an_unavailable_pr_reviewer_of_a_slot_without_a_review_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The working PR reviewer row already exists, so the lane runs without a
    # start. Its unavailable result cannot be recorded without a review stage.
    tid = _to_pr_review_step(tmp_path, capsys, monkeypatch)
    run(
        tmp_path,
        [
            "agent", "start", "--session", DEFAULT_SESSION, "--task", tid,
            "--role", "pr-reviewer-quality", "--vendor", "driver-a",
        ],
    )
    working_id = _last_agent_id(capsys.readouterr().out)
    monkeypatch.setattr("agent_cli.main.SLOT_STAGES", {})
    launches: list[dict[str, Any]] = []

    def unavailable(**kwargs: Any) -> LaneResult:
        launches.append(kwargs)
        return LaneResult(
            role="pr-reviewer-quality",
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="unavailable",
            argv=["tool-a"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        )

    monkeypatch.setattr("agent_cli.main.launch", unavailable)
    with pytest.raises(SystemExit) as refused:
        run(
            tmp_path,
            [
                *_run_spec_args(tid, _spec(tmp_path, "review quality\n"), tmp_path),
                "--head", PUSHED_SHA,
            ],
        )
    assert str(refused.value) == "agent: review_1_quality has no review stage"
    assert len(launches) == 1
    reviewers = [
        agent
        for agent in _agents(tmp_path, tid)
        if agent.get("role") == "pr-reviewer-quality"
    ]
    # No second row was started, and the working row is not finished.
    assert [(agent["id"], agent["status"]) for agent in reviewers] == [
        (working_id, "working")
    ]
    assert _checklist(tmp_path, tid).get("review_1_quality") != "unavailable"
    assert _review_gates(tmp_path, tid) == []


def test_run_refuses_an_incomplete_lane_of_an_unknown_role(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every agent step of the chain is an implementer, reviewer or PR reviewer
    # today. An incomplete lane of any other role is refused instead of being
    # recorded with a guessed verdict.
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(
        tmp_path,
        sessions={
            DEFAULT_SESSION: {
                "interactive": "run-builder",
                "lanes": {
                    "driver-a:implementer": "run-builder",
                    "driver-a:reviewer": "run-reader",
                    "driver-a:check": "run-reader",
                },
            }
        },
    )

    def check_step(
        workflow: str, checklist: dict[str, str], *, spine_only: bool = True
    ) -> list[Any]:
        return [
            replace(step, role="check") if step.key == "implementer_done" else step
            for step in next_steps(workflow, checklist, spine_only=spine_only)
        ]

    monkeypatch.setattr("agent_cli.main.next_steps", check_step)
    # The working row of that role exists, as an earlier attempt left it.
    store = _store(tmp_path)
    try:
        store.write(
            "agent",
            "insert",
            "agent-check",
            {
                "id": "agent-check",
                "task_id": tid,
                "session_id": DEFAULT_SESSION,
                "round": None,
                "role": "check",
                "vendor": "driver-a",
                "provider": "driver-a",
                "model": OPERATOR_MODEL_A,
                "status": "working",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
                "note": None,
            },
        )
    finally:
        store.close()
    launches: list[dict[str, Any]] = []

    def unavailable(**kwargs: Any) -> LaneResult:
        launches.append(kwargs)
        return LaneResult(
            role="check",
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="unavailable",
            argv=["tool-a"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        )

    monkeypatch.setattr("agent_cli.main.launch", unavailable)
    with pytest.raises(SystemExit) as refused:
        run(tmp_path, _run_spec_args(tid, _spec(tmp_path, "check this\n"), tmp_path))
    assert str(refused.value) == "agent: unknown incomplete lane role: check"
    assert len(launches) == 1
    assert launches[0]["role"] == "check"
    checks = [agent for agent in _agents(tmp_path, tid) if agent.get("role") == "check"]
    assert [(agent["id"], agent["status"]) for agent in checks] == [
        ("agent-check", "working")
    ]
    assert _checklist(tmp_path, tid).get("implementer_done") != "ja"
