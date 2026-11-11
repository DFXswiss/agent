from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.lane import LaneResult, LaneUsage
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from test_cli import _last_agent_id, _last_task_id, run
from github_support import configure_accounts

OPERATOR_GROK_MODEL = "operator-run-grok-model"
OPERATOR_GROK_HOME = "/operator/path/grok-run-a"
OPERATOR_CODEX_HOME = "/operator/path/codex-run-a"
DEFAULT_SESSION = "sess-1"


def write_operator_ai_accounts(home: Path, *, sessions: dict | None = None) -> None:
    """Write an explicit operator ai-accounts.json for positive cmd_run lane starts.

    Installation defaults stay empty. Call this only for configured success
    paths; negative tests must leave the home unconfigured or deliberately
    malformed/mismatched.
    """
    data = {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "grok", "second": "codex"},
        "accounts": {
            "grok-a": {"provider": "grok", "config_dir": OPERATOR_GROK_HOME},
            "codex-a": {"provider": "codex", "config_dir": OPERATOR_CODEX_HOME},
        },
        "roles": {
            "run-builder": {
                "account": "grok-a",
                "model": OPERATOR_GROK_MODEL,
                "access": "workspace-write",
            },
            "run-reader": {
                "account": "grok-a",
                "model": OPERATOR_GROK_MODEL,
                "access": "read-only",
            },
            "codex-builder": {
                "account": "codex-a",
                "model": "operator-run-codex-model",
                "access": "workspace-write",
            },
        },
        "sessions": sessions
        or {
            DEFAULT_SESSION: {
                "interactive": "run-builder",
                "lanes": {
                    "grok:implementer": "run-builder",
                    "grok:reviewer": "run-reader",
                },
            },
        },
    }
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


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


def test_run_local_check_missing_command_records_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    monkeypatch.setenv("AGENT_CHECK_COMMAND", "/no/such/agent-check-command")
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid, "--cwd", str(tmp_path)])
    assert exc.value.code == 2
    assert _task_state(tmp_path, tid) == "failed"
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


def test_run_prints_vendor_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}
    argv_model = "model-passed-in-run-argv"
    lane_argv = ["grok", "-m", argv_model]

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return LaneResult(
            role="implementer",
            vendor="grok",
            model=argv_model,
            status="complete",
            argv=lane_argv,
            returncode=0,
            stdout=(
                "implemented the thing, distinctive-marker-run456\n"
                "STATUS: complete\nRESULT: done\n"
            ),
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
    out = capsys.readouterr().out
    marker_at = out.index("distinctive-marker-run456")
    summary_at = out.index("STATUS=complete")
    assert marker_at < summary_at
    assert seen.get("session_id") == "sess-1"
    assert seen.get("config_home") == tmp_path
    assert seen.get("role") == "implementer"
    assert seen.get("vendor") == "grok"
    assert seen.get("tmux") is False
    store = _store(tmp_path)
    try:
        rows = [r for r in store.rows("activity") if r.get("type") == "lane.run"]
    finally:
        store.close()
    assert len(rows) == 1
    assert rows[0]["payload"]["task_id"] == tid
    assert rows[0]["payload"]["model"] == lane_argv[lane_argv.index("-m") + 1]


def test_cli_lane_run_records_usage_and_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", DEFAULT_SESSION, "--kind", "human"])
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    argv_model = "model-passed-in-lane-argv"
    lane_argv = ["grok", "-m", argv_model]

    def fake_launch(**_kwargs):  # type: ignore[no-untyped-def]
        return LaneResult(
            role="implementer",
            vendor="grok",
            model=argv_model,
            status="complete",
            argv=lane_argv,
            returncode=0,
            stdout="secret=hidden\nSTATUS: complete\n",
            stderr="",
            usage=LaneUsage("reported/model", 10, 4, 2, 1, 3, 42),
            tools=({"tool": "Read", "target": "/work/a.py", "error": False},),
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    run(
        tmp_path,
        [
            "lane", "run", "--role", "implementer", "--vendor", "grok",
            "--session", DEFAULT_SESSION, "--spec-file", str(spec),
            "--cwd", str(tmp_path), "--no-tmux",
        ],
    )
    capsys.readouterr()
    store = _store(tmp_path)
    try:
        rows = [r for r in store.rows("activity") if r.get("type") == "lane.run"]
    finally:
        store.close()
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["model"] == lane_argv[lane_argv.index("-m") + 1]
    assert payload["reported_model"] == "reported/model"
    assert payload["usage"]["input_tokens"] == 10
    assert payload["tools"] == [
        {"tool": "Read", "target": "/work/a.py", "error": False}
    ]
    assert payload["final_text"] != "secret=hidden\nSTATUS: complete\n"


@pytest.mark.parametrize(
    ("failure_point", "error_type"),
    [("open_store", RuntimeError), ("record_lane_run", StoreError)],
)
def test_cli_lane_run_reports_activity_write_failure_without_changing_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    error_type: type[BaseException],
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", DEFAULT_SESSION, "--kind", "human"])
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            "implementer", "grok", OPERATOR_GROK_MODEL, "complete", ["grok"], 0,
            "STATUS: complete\n", "",
        ),
    )
    config_dir = tmp_path / "private-config"
    split_credential = "github_\x1b[31mpat_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    audit_error = error_type(
        f"store unavailable at {config_dir}: credential={split_credential}"
    )

    def fail_audit(*_args, **_kwargs):  # noqa: ANN001
        raise audit_error

    monkeypatch.setattr(f"agent_cli.main.{failure_point}", fail_audit)
    run(
        tmp_path,
        [
            "lane", "run", "--role", "implementer", "--vendor", "grok",
            "--session", DEFAULT_SESSION, "--spec-file", str(spec),
            "--cwd", str(tmp_path), "--no-tmux",
        ],
    )
    captured = capsys.readouterr()
    assert "STATUS=complete" in captured.out
    assert "lane.run audit write failed" in captured.err.splitlines()
    assert str(config_dir) not in captured.err
    assert "github_" not in captured.err
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in captured.err
    assert "\x1b" not in captured.err


def test_run_store_error_on_lane_audit_keeps_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nRESULT: done\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "agent_cli.main.record_lane_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StoreError("store unavailable")),
    )
    run(
        tmp_path,
        ["run", "--task", tid, "--spec-file", str(spec), "--no-tmux", "--cwd", str(tmp_path)],
    )
    captured = capsys.readouterr()
    assert "lane.run write failed: store unavailable" in captured.err
    assert _checklist(tmp_path, tid)["implementer_done"] == "ja"


def test_run_spec_file_implementer_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nRESULT: done\n",
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
    assert seen.get("session_id") == "sess-1"
    assert seen.get("config_home") == tmp_path
    assert seen.get("spec_file") == str(spec)


def test_run_exposes_only_the_shared_model_result_parser() -> None:
    from agent_cli import main
    from agent_cli.coordinator_common import parse_model_result

    assert main.parse_model_result is parse_model_result
    assert "_parsed_lane_result" not in vars(main)
    assert main.parse_model_result(
        "STATUS: complete\nRESULT: no-change\n", 0
    ) == ("complete", "no-change")


@pytest.mark.parametrize(
    "stdout",
    [
        "STATUS: complete\n",
        "STATUS: complete\nRESULT: done\nRESULT: done\n",
        "STATUS: partial\nSTATUS: complete\nRESULT: done\n",
        "STATUS: complete\nRESULT: unknown\nRESULT: done\n",
        "STATUS: unknown\nSTATUS: complete\nRESULT: done\n",
        "STATUS: complete\nRESULT: ask\nWhich error text?\n",
        "STATUS: complete\nRESULT: approved\n",
        "STATUS: complete\nRESULT: rejected\n",
    ],
)
def test_run_implementer_complete_without_one_role_valid_result_is_retryable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout=stdout,
            stderr="",
        ),
    )

    for expected_count in (1, 2):
        with pytest.raises(SystemExit) as exc:
            run(
                tmp_path,
                [
                    "run", "--task", tid, "--spec-file", str(spec),
                    "--no-tmux", "--cwd", str(tmp_path),
                ],
            )
        assert exc.value.code == 2
        assert _checklist(tmp_path, tid).get("implementer_done") != "ja"
        assert _task_state(tmp_path, tid) == "implementing"
        implementers = [
            agent for agent in _agents(tmp_path, tid)
            if agent.get("role") == "implementer"
        ]
        assert len(implementers) == expected_count
        assert all(agent["status"] == "done" for agent in implementers)
        assert all("aborted incomplete lane" in str(agent["note"]) for agent in implementers)
        assert all("invalid implementer STATUS/RESULT" in str(agent["note"]) for agent in implementers)


def test_run_implementer_no_change_counts_as_implemented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nRESULT: no-change\n",
            stderr="",
        ),
    )
    run(
        tmp_path,
        [
            "run", "--task", tid, "--spec-file", str(spec),
            "--no-tmux", "--cwd", str(tmp_path),
        ],
    )
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["implementer_done"] == "ja"


def test_run_failed_lane_diagnostic_is_redacted_in_note_and_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"
    marker = "\x1b[31m"
    split_secret = secret.replace("sk-or-", f"sk-or-{marker}")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="openrouter",
            model=OPERATOR_GROK_MODEL,
            status="unavailable",
            argv=["claude"],
            returncode=2,
            stdout="",
            stderr=f"provider output {split_secret}\n",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        run(
            tmp_path,
            [
                "run", "--task", tid, "--spec-file", str(spec),
                "--no-tmux", "--cwd", str(tmp_path),
            ],
        )

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert secret not in captured.out + captured.err
    assert marker not in captured.out + captured.err
    assert "[31m" not in captured.out + captured.err
    assert "[redacted]" in captured.err
    implementers = [
        agent for agent in _agents(tmp_path, tid)
        if agent.get("role") == "implementer"
    ]
    assert implementers
    note = str(implementers[0]["note"])
    assert secret not in note
    assert marker not in note
    assert "[31m" not in note
    assert "[redacted]" in note


def test_run_failed_lane_hides_control_interleaved_config_path_in_note_and_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    config_dir = "/opt/profiles/router"
    split_path = "/opt/profi\x1b[31mles/router/openrouter-api-key"
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="openrouter",
            model=OPERATOR_GROK_MODEL,
            status="unavailable",
            argv=["claude"],
            returncode=2,
            stdout="",
            stderr=f"provider output {split_path}\n",
            config_dir=config_dir,
        ),
    )

    with pytest.raises(SystemExit) as exc:
        run(
            tmp_path,
            [
                "run", "--task", tid, "--spec-file", str(spec),
                "--no-tmux", "--cwd", str(tmp_path),
            ],
        )

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "/opt/profiles/router" not in captured.out + captured.err
    assert "\x1b" not in captured.out + captured.err
    assert "<config_dir>/openrouter-api-key" in captured.err
    implementers = [
        agent for agent in _agents(tmp_path, tid)
        if agent.get("role") == "implementer"
    ]
    assert implementers
    assert implementers[0]["note"] == (
        "aborted incomplete lane: provider output <config_dir>/openrouter-api-key"
    )


def test_run_only_parsed_implementer_blocked_result_fails_the_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nRESULT: blocked\n",
            stderr="",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        run(
            tmp_path,
            [
                "run", "--task", tid, "--spec-file", str(spec),
                "--no-tmux", "--cwd", str(tmp_path),
            ],
        )

    assert exc.value.code == 2
    assert _task_state(tmp_path, tid) == "failed"
    implementers = [
        agent for agent in _agents(tmp_path, tid)
        if agent.get("role") == "implementer"
    ]
    assert len(implementers) == 1
    assert implementers[0]["status"] == "done"
    assert implementers[0]["note"] == "lane STATUS=complete RESULT=blocked"


def test_run_spec_file_invalid_openrouter_output_ends_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    router_config = tmp_path / "router-config"
    router_config.mkdir()
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps(
            {
                "lane_execution": {"mode": "this-device-is-lane-host"},
                "models": {"first": "openrouter", "second": "codex"},
                "accounts": {
                    "router": {
                        "provider": "openrouter",
                        "config_dir": str(router_config),
                    }
                },
                "roles": {
                    "router-builder": {
                        "account": "router",
                        "model": "vendor/model",
                        "access": "workspace-write",
                    }
                },
                "sessions": {
                    DEFAULT_SESSION: {
                        "interactive": "router-builder",
                        "lanes": {"openrouter:implementer": "router-builder"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    tid = _bootstrap_implement(tmp_path, capsys)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    output_paths: list[Path] = []

    def invalid_output(
        argv: list[str], stdin_text: str | None, cwd: str | None = None
    ) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        output_file = Path(argv[wrapper_index + 2])
        output_paths.append(output_file)
        output_file.write_bytes(b"\xff")
        assert stdin_text == "implement this\n"
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.lane._default_runner", invalid_output)
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
                str(worktree),
            ],
        )

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "lane output file is not valid UTF-8" in captured.err
    assert output_paths and not output_paths[0].exists()
    agents = _agents(tmp_path, tid)
    assert agents
    assert all(agent.get("status") != "working" for agent in agents)
    implementers = [agent for agent in agents if agent.get("role") == "implementer"]
    assert implementers and implementers[0].get("status") == "done"
    assert "lane output file is not valid UTF-8" in str(implementers[0].get("note"))
    assert _task_state(tmp_path, tid) == "implementing"

    with pytest.raises(SystemExit) as retry:
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
                str(worktree),
            ],
        )
    assert retry.value.code == 2
    retried = [
        agent for agent in _agents(tmp_path, tid)
        if agent.get("role") == "implementer"
    ]
    assert len(retried) == 2
    assert all(agent.get("status") == "done" for agent in retried)
    assert _task_state(tmp_path, tid) == "implementing"


@pytest.mark.parametrize("status", ["partial", "timeout", "unavailable"])
def test_run_incomplete_implementer_is_aborted_without_blocking_the_round(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="implementer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status=status,
            argv=["grok"],
            returncode={"partial": 0, "timeout": 124, "unavailable": 2}[status],
            stdout=f"STATUS: {status}\n",
            stderr="provider did not complete",
        ),
    )

    for expected_count in (1, 2):
        with pytest.raises(SystemExit) as exc:
            run(
                tmp_path,
                [
                    "run", "--task", tid, "--spec-file", str(spec),
                    "--no-tmux", "--cwd", str(tmp_path),
                ],
            )
        assert exc.value.code == 2
        implementers = [
            agent for agent in _agents(tmp_path, tid)
            if agent.get("role") == "implementer"
        ]
        assert len(implementers) == expected_count
        assert all(agent.get("status") == "done" for agent in implementers)
        assert all("aborted incomplete lane" in str(agent.get("note")) for agent in implementers)
        assert _task_state(tmp_path, tid) == "implementing"


def test_run_missing_spec_file_does_not_leave_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    with pytest.raises(SystemExit) as exc:
        run(
            tmp_path,
            [
                "run",
                "--task",
                tid,
                "--spec-file",
                str(tmp_path / "missing-spec.md"),
                "--no-tmux",
                "--cwd",
                str(tmp_path),
            ],
        )
    assert exc.value.code != 0
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def _assert_run_config_failure_leaves_no_working_agent(
    home: Path,
    tid: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: Path,
    match: str,
) -> None:
    called = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        called["n"] += 1
        raise AssertionError("model must not launch when AI lane binding fails")

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    with pytest.raises(SystemExit, match=match):
        run(
            home,
            [
                "run",
                "--task",
                tid,
                "--spec-file",
                str(spec),
                "--no-tmux",
                "--cwd",
                str(home),
            ],
        )
    capsys.readouterr()
    assert called["n"] == 0
    assert not any(a.get("status") == "working" for a in _agents(home, tid))


def test_run_unconfigured_ai_leaves_no_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty installation default: cmd_run must not persist a working lane."""
    tid = _bootstrap_implement(tmp_path, capsys)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    _assert_run_config_failure_leaves_no_working_agent(
        tmp_path,
        tid,
        capsys,
        monkeypatch,
        spec=spec,
        match="No AI session configured",
    )


def test_run_reports_missing_lane_before_missing_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("models")
    raw["sessions"][DEFAULT_SESSION]["lanes"] = {}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    _assert_run_config_failure_leaves_no_working_agent(
        tmp_path,
        tid,
        capsys,
        monkeypatch,
        spec=spec,
        match="No AI lane configured",
    )


def test_run_malformed_ai_accounts_leaves_no_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    (tmp_path / "ai-accounts.json").write_text("{", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    _assert_run_config_failure_leaves_no_working_agent(
        tmp_path,
        tid,
        capsys,
        monkeypatch,
        spec=spec,
        match="Cannot read ai-accounts.json",
    )


def test_run_provider_mismatch_leaves_no_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"] = {
        DEFAULT_SESSION: {"lanes": {"grok:implementer": "codex-builder"}},
    }
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    _assert_run_config_failure_leaves_no_working_agent(
        tmp_path,
        tid,
        capsys,
        monkeypatch,
        spec=spec,
        match="provider does not match",
    )


def test_run_writable_reviewer_binding_leaves_no_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"][DEFAULT_SESSION]["lanes"]["grok:reviewer"] = "run-builder"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    _assert_run_config_failure_leaves_no_working_agent(
        tmp_path,
        tid,
        capsys,
        monkeypatch,
        spec=spec,
        match="requires read-only access",
    )


def test_run_spec_file_reviewer_complete_no_auto_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    seen: dict = {}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return LaneResult(
            role="reviewer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
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
    assert seen.get("session_id") == "sess-1"
    assert seen.get("config_home") == tmp_path
    assert seen.get("role") == "reviewer"
    assert seen.get("vendor") == "grok"


def test_run_unavailable_inner_reviewer_is_retryable_in_the_same_round(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="reviewer",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="unavailable",
            argv=["grok"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        ),
    )

    for expected_count in (1, 2):
        with pytest.raises(SystemExit) as exc:
            run(
                tmp_path,
                [
                    "run", "--task", tid, "--spec-file", str(spec),
                    "--no-tmux", "--cwd", str(tmp_path),
                ],
            )
        assert exc.value.code == 2
        reviewers = [
            agent for agent in _agents(tmp_path, tid)
            if agent.get("role") == "reviewer"
        ]
        assert len(reviewers) == expected_count
        assert all(agent.get("status") == "done" for agent in reviewers)
        assert _task_state(tmp_path, tid) == "reviewing"


def _advance_to_pushed(
    home: Path, tid: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _finish_implementer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    _finish_reviewer(home, tid, capsys)
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    monkeypatch.setattr(
        "agent_cli.main._exec_argv",
        lambda argv, *, cwd=None: Completed(0, "ok", ""),
    )
    run(home, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(home, tid)["local_check_pass"] == "ja"


def test_run_pushed_calls_push_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, ["sess-1"], login="ok")

    called = {"n": 0}

    def fake_push(*, cwd: str, runner):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid, "--dry-run"])
    capsys.readouterr()
    assert called["n"] == 0
    assert _checklist(tmp_path, tid)["pushed"] != "ja"

    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert called["n"] == 1
    assert _checklist(tmp_path, tid)["pushed"] == "ja"


def _bootstrap_resolve(home: Path, capsys: pytest.CaptureFixture[str]) -> str:
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
    run(
        home,
        [
            "task",
            "create",
            "--session",
            "sess-1",
            "--workflow",
            "resolve-conflicts",
            "--title",
            "Unstick",
        ],
    )
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
    run(home, ["round", "start", "--task", tid])
    capsys.readouterr()
    return tid


def _record_pr_gate(
    home: Path,
    tid: str,
    capsys: pytest.CaptureFixture[str],
    *,
    stage: str,
    dimension: str,
    vendor: str,
    head: str = "abc1234",
) -> None:
    role = f"pr-reviewer-{dimension}"
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
            role,
            "--vendor",
            vendor,
        ],
    )
    agent_id = _last_agent_id(capsys.readouterr().out)
    run(home, ["agent", "finish", "--id", agent_id, "--verdict", "approved"])
    capsys.readouterr()
    run(
        home,
        [
            "gate",
            "record",
            "--task",
            tid,
            "--stage",
            stage,
            "--dimension",
            dimension,
            "--vendor",
            vendor,
            "--verdict",
            "approved",
            "--head",
            head,
            "--agent",
            agent_id,
        ],
    )
    capsys.readouterr()


def test_run_mergeable_after_gates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _bootstrap_resolve(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, ["sess-1"], login="ok")

    push_called = {"n": 0}

    def fake_push(*, cwd: str, runner):  # type: ignore[no-untyped-def]
        push_called["n"] += 1
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid, "--head", "abc1234"])
    capsys.readouterr()
    assert push_called["n"] == 1
    assert _checklist(tmp_path, tid)["pushed"] == "ja"

    for stage, vendor in (("review-1", "grok"), ("review-2", "codex")):
        for dimension in ("quality", "logic"):
            _record_pr_gate(
                tmp_path,
                tid,
                capsys,
                stage=stage,
                dimension=dimension,
                vendor=vendor,
            )
            run(tmp_path, ["run", "--task", tid])
            capsys.readouterr()
            key = f"{stage.replace('-', '_')}_{dimension}"
            assert _checklist(tmp_path, tid)[key] == "ja"

    monkeypatch.setattr(
        "agent_cli.git_act.measure_mergeable",
        lambda *, cwd, runner, expected_head=None, repo=None, number=None: "ok",
    )
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["mergeable"] == "ja"


def test_run_unavailable_pr_reviewer_records_gate_and_can_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(
        tmp_path,
        sessions={
            DEFAULT_SESSION: {
                "interactive": "run-builder",
                "lanes": {
                    "grok:implementer": "run-builder",
                    "grok:reviewer": "run-reader",
                    "grok:pr-reviewer-quality": "run-reader",
                },
            }
        },
    )
    tid = _bootstrap_resolve(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, [DEFAULT_SESSION], login="ok")
    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner: "abc1234",
    )
    run(tmp_path, ["run", "--task", tid, "--head", "abc1234"])
    capsys.readouterr()
    spec = tmp_path / "pr-quality.md"
    spec.write_text("review quality\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.main.launch",
        lambda **_kwargs: LaneResult(
            role="pr-reviewer-quality",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="unavailable",
            argv=["grok"],
            returncode=2,
            stdout="STATUS: unavailable\n",
            stderr="provider unavailable",
        ),
    )

    for expected_count in (1, 2):
        with pytest.raises(SystemExit) as exc:
            run(
                tmp_path,
                [
                    "run", "--task", tid, "--head", "abc1234",
                    "--spec-file", str(spec), "--no-tmux", "--cwd", str(tmp_path),
                ],
            )
        assert exc.value.code == 2
        reviewers = [
            agent for agent in _agents(tmp_path, tid)
            if agent.get("role") == "pr-reviewer-quality"
        ]
        assert len(reviewers) == expected_count
        assert all(agent.get("status") == "done" for agent in reviewers)
        assert _checklist(tmp_path, tid)["review_1_quality"] == "unavailable"
        store = _store(tmp_path)
        try:
            gates = [
                gate for gate in store.rows("review_gate")
                if gate.get("task_id") == tid
                and gate.get("stage") == "review-1"
                and gate.get("dimension") == "quality"
            ]
        finally:
            store.close()
        assert len(gates) == expected_count
        assert all(gate.get("verdict") == "unavailable" for gate in gates)
        assert all("provider unavailable" in str(gate.get("evidence")) for gate in gates)



def test_run_launches_the_provider_pinned_on_the_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    from test_model_slots import write_claude_first

    write_claude_first(tmp_path)
    tid = _bootstrap_implement(tmp_path, capsys)
    # The device switches to grok first after the task was created.
    path = tmp_path / "ai-accounts.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["models"] = {"first": "grok", "second": "codex"}
    path.write_text(_json.dumps(data), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    seen: dict = {}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return LaneResult(
            role="implementer", vendor=kwargs["vendor"], model="claude-model",
            status="complete", argv=["claude"],
            returncode=0, stdout="done\nSTATUS: complete\nRESULT: done\n", stderr="",
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    run(tmp_path, ["run", "--task", tid, "--spec-file", str(spec), "--no-tmux", "--cwd", str(tmp_path)])
    assert seen.get("vendor") == "claude"


def test_invalid_pinned_models_end_in_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_cli.main import open_store

    tid = _bootstrap_implement(tmp_path, capsys)
    store = open_store()
    try:
        row = {k: v for k, v in store.row("task", tid).items() if not k.startswith("_")}
        row["models"] = {"first": "gemini", "second": "codex"}
        store.write("task", "update", tid, row)
    finally:
        store.close()
    with pytest.raises(SystemExit, match="invalid models pinned on the task"):
        run(tmp_path, ["next", "--task", tid])


def test_run_refuses_external_second_slot_without_launching_lane(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = _bootstrap_resolve(tmp_path, capsys)
    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        updated = {key: value for key, value in task.items() if not key.startswith("_")}
        updated["models"] = {"first": "grok", "second": None}
        store.write("task", "update", tid, updated)
    finally:
        store.close()

    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    configure_accounts(tmp_path, ["sess-1"], login="ok")
    monkeypatch.setattr(
        "agent_cli.git_act.push_branch",
        lambda *, cwd, runner: "abc1234",
    )
    run(tmp_path, ["run", "--task", tid, "--head", "abc1234"])
    capsys.readouterr()
    for dimension in ("quality", "logic"):
        _record_pr_gate(
            tmp_path,
            tid,
            capsys,
            stage="review-1",
            dimension=dimension,
            vendor="grok",
        )
        run(tmp_path, ["run", "--task", tid])
        capsys.readouterr()

    spec = tmp_path / "second-review.md"
    spec.write_text("review this\n", encoding="utf-8")
    launches = {"count": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        launches["count"] += 1
        raise AssertionError(f"unexpected lane launch: {kwargs}")

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    agents_before = len(_agents(tmp_path, tid))
    with pytest.raises(
        SystemExit,
        match="model slot second is external; no agent lane can be started",
    ):
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
    assert launches["count"] == 0
    assert len(_agents(tmp_path, tid)) == agents_before
