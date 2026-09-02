from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli.lane import LaneResult
from agent_cli.run_core import (
    EmptyReviewDiffError,
    ReviewDiffUnavailableError,
    _collect_review_diff,
    build_review_spec_file,
)
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
    assert any(a and a[0] == "pytest" for a in seen)
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
    assert ["true"] in seen


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
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        return LaneResult(
            role="implementer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="implemented the thing, distinctive-marker-run456\nSTATUS: complete\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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

    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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


def test_build_review_spec_oserror_does_not_leave_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError from build_review_spec_file must release the working agent, then re-raise."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # close implementer_done → reviewer next
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr("agent_cli.run_core.build_review_spec_file", boom)
    with pytest.raises(OSError):
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
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_empty_review_diff_short_circuits_before_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty collected review diff must fail the step without launching the lane."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # close implementer_done → reviewer next
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    monkeypatch.setattr(
        "agent_cli.run_core._collect_review_diff",
        lambda *_a, **_k: ("", [], True),
    )

    def boom_launch(**_kwargs: object) -> object:
        raise AssertionError("launch must not be called")

    monkeypatch.setattr("agent_cli.run_core.launch", boom_launch)
    with pytest.raises(SystemExit):
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
    assert _checklist(tmp_path, tid).get("reviewer_approved") != "ja"
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_empty_diff_text_with_nonempty_changed_paths_still_short_circuits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty diff_text must fail even when changed_paths is non-empty; launch must not run."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # close implementer_done → reviewer next
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    monkeypatch.setattr(
        "agent_cli.run_core._collect_review_diff",
        lambda *_a, **_k: ("", ["some/file.py"], True),
    )

    def boom_launch(**_kwargs: object) -> object:
        raise AssertionError("launch must not be called")

    monkeypatch.setattr("agent_cli.run_core.launch", boom_launch)
    with pytest.raises(SystemExit):
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
    assert _checklist(tmp_path, tid).get("reviewer_approved") != "ja"
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_review_diff_probe_failure_leaves_task_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed git probes with empty diff must not fail the task (retryable)."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # close implementer_done → reviewer next
    capsys.readouterr()
    before_state = _task_state(tmp_path, tid)
    before_reviewer = _checklist(tmp_path, tid).get("reviewer_approved")
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    monkeypatch.setattr(
        "agent_cli.run_core._collect_review_diff",
        lambda *_a, **_k: ("", [], False),
    )

    def boom_launch(**_kwargs: object) -> object:
        raise AssertionError("launch must not be called")

    monkeypatch.setattr("agent_cli.run_core.launch", boom_launch)
    with pytest.raises(SystemExit):
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
    assert _task_state(tmp_path, tid) == before_state
    assert _task_state(tmp_path, tid) != "failed"
    assert _checklist(tmp_path, tid).get("reviewer_approved") == before_reviewer
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_collect_review_diff_no_base_candidate_resolves_marks_probes_not_ok(
    tmp_path: Path,
) -> None:
    """When every base candidate fails rev-parse, probes_ok must be False."""

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return Completed(1, "", "")
        # Supplemental HEAD / --cached probes succeed but empty.
        return Completed(0, "", "")

    _diff, _paths, probes_ok = _collect_review_diff(str(tmp_path), fake_exec)
    assert probes_ok is False


def test_collect_review_diff_empty_merge_base_stdout_marks_probes_not_ok(
    tmp_path: Path,
) -> None:
    """merge-base exit 0 with empty/whitespace stdout must set probes_ok False."""

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            if argv[3] == "origin/develop":
                return Completed(0, "abc123\n", "")
            return Completed(1, "", "")
        if argv[:2] == ["git", "merge-base"]:
            return Completed(0, "   \n", "")
        # Supplemental HEAD / --cached probes succeed but empty.
        return Completed(0, "", "")

    _diff, _paths, probes_ok = _collect_review_diff(str(tmp_path), fake_exec)
    assert probes_ok is False


def test_build_review_spec_file_raises_unavailable_when_no_base_resolves(
    tmp_path: Path,
) -> None:
    """No resolving base candidate → ReviewDiffUnavailableError, not EmptyReviewDiffError."""

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return Completed(1, "", "")
        return Completed(0, "", "")

    store = _store(tmp_path)
    try:
        with pytest.raises(ReviewDiffUnavailableError) as ei:
            build_review_spec_file(
                store,
                "some-tid",
                role="reviewer",
                round_num=1,
                implement_spec_file=None,
                cwd=str(tmp_path),
                exec_argv=fake_exec,
            )
        assert not isinstance(ei.value, EmptyReviewDiffError)
    finally:
        store.close()


def test_build_review_spec_file_raises_unavailable_when_merge_base_stdout_empty(
    tmp_path: Path,
) -> None:
    """Empty merge-base stdout → ReviewDiffUnavailableError, not EmptyReviewDiffError."""

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            if argv[3] == "origin/develop":
                return Completed(0, "abc123\n", "")
            return Completed(1, "", "")
        if argv[:2] == ["git", "merge-base"]:
            return Completed(0, "", "")
        return Completed(0, "", "")

    store = _store(tmp_path)
    try:
        with pytest.raises(ReviewDiffUnavailableError) as ei:
            build_review_spec_file(
                store,
                "some-tid",
                role="reviewer",
                round_num=1,
                implement_spec_file=None,
                cwd=str(tmp_path),
                exec_argv=fake_exec,
            )
        assert not isinstance(ei.value, EmptyReviewDiffError)
    finally:
        store.close()


def test_build_review_spec_file_raises_unavailable_despite_dirty_worktree_diff(
    tmp_path: Path,
) -> None:
    """Failed range-diff probe must raise even when supplemental dirty-worktree diff is non-empty."""

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            if argv[3] == "origin/develop":
                return Completed(0, "abc123\n", "")
            return Completed(1, "", "")
        if argv[:2] == ["git", "merge-base"]:
            return Completed(1, "", "")
        if argv == ["git", "diff", "HEAD"]:
            return Completed(0, "diff --git a/x b/x\n+dirty\n", "")
        if argv == ["git", "diff", "--name-only", "HEAD"]:
            return Completed(0, "x\n", "")
        return Completed(0, "", "")

    store = _store(tmp_path)
    try:
        with pytest.raises(ReviewDiffUnavailableError):
            build_review_spec_file(
                store,
                "some-tid",
                role="reviewer",
                round_num=1,
                implement_spec_file=None,
                cwd=str(tmp_path),
                exec_argv=fake_exec,
            )
    finally:
        store.close()


def test_launch_oserror_does_not_leave_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError from launch must release the working agent, then re-raise."""
    tid = _bootstrap_implement(tmp_path, capsys)
    spec = tmp_path / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")

    def boom(**_kwargs: object) -> object:
        raise OSError("missing vendor CLI binary")

    monkeypatch.setattr("agent_cli.run_core.launch", boom)
    with pytest.raises(OSError):
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
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_launch_oserror_on_retry_does_not_leave_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError from launch on the RETRY attempt must release the working agent, then re-raise."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return LaneResult(
                role="reviewer",
                vendor="grok",
                status="complete",
                argv=["grok"],
                returncode=0,
                stdout="STATUS: complete\n",  # no FINDINGS: header -> unparseable -> retry
                stderr="",
            )
        raise OSError("missing vendor CLI binary")

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    with pytest.raises(OSError):
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
    assert calls["n"] == 2
    assert not any(a.get("status") == "working" for a in _agents(tmp_path, tid))


def test_run_spec_file_reviewer_complete_auto_approves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """STATUS: complete + FINDINGS: none → auto-approve reviewer_approved."""
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
            # Explicit FINDINGS header with zero items (not header-absent).
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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
    assert _checklist(tmp_path, tid)["reviewer_approved"] == "ja"
    assert any(
        a.get("role") == "reviewer" and a.get("status") == "done"
        for a in _agents(tmp_path, tid)
    )


def test_run_spec_file_reviewer_complete_without_findings_header_retries_then_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """STATUS: complete with no FINDINGS: header is unparseable → retry then fail."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\n",
            stderr="",
        )

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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
    assert exc.value.code != 0
    assert calls["n"] == 2  # initial + one retry
    assert _checklist(tmp_path, tid)["reviewer_approved"] != "ja"
    assert _task_state(tmp_path, tid) == "failed"


def test_run_spec_file_vendor_unavailable_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LaneResult(status=unavailable) on both attempts → exit 2, task left open."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="unavailable",
            argv=["grok"],
            returncode=127,
            stdout="",
            stderr="command not found",
        )

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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
    assert calls["n"] == 2  # initial + one retry
    assert _checklist(tmp_path, tid)["reviewer_approved"] != "ja"
    assert _task_state(tmp_path, tid) != "failed"


def test_run_spec_file_reviewer_retry_exhaustion_finishes_working_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry-exhaustion fails the task and finishes the still-working reviewer agent."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])  # implementer_done
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\n",
            stderr="",
        )

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
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
    assert exc.value.code != 0
    assert calls["n"] == 2
    assert _task_state(tmp_path, tid) == "failed"
    agents = _agents(tmp_path, tid)
    assert not any(a.get("status") == "working" for a in agents)
    reviewer = next(a for a in agents if a.get("role") == "reviewer")
    assert reviewer.get("status") == "done"
    assert "retry" in str(reviewer.get("note") or "").lower()


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

    called = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
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


def test_pushed_passes_expected_branch_none_for_ordinary_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary implement tasks have no payload.error_id → expected_branch=None."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)

    captured: dict[str, object] = {}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        captured["expected_branch"] = expected_branch
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert "expected_branch" in captured
    assert captured["expected_branch"] is None
    assert _checklist(tmp_path, tid)["pushed"] == "ja"


def test_pushed_fails_loudly_on_stale_whitespace_only_error_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 25 regression: run_core's expected_branch derivation used to
    treat "error_id key absent" and "error_id present but whitespace-only"
    identically (both -> expected_branch=None, silently skipping the push
    identity check). Round 24 now rejects whitespace-only error_id at
    creation time for NEW tasks, so this is only reachable via stale
    pre-existing store data — simulated here by writing the task payload
    directly, bypassing create-time validation."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["payload"] = {"error_id": " "}
        store.write("task", "update", tid, task)
    finally:
        store.close()

    called = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert "whitespace-only" in str(exc.value.code)
    assert called["n"] == 0
    assert _checklist(tmp_path, tid)["pushed"] != "ja"


def test_pushed_fails_loudly_on_error_id_without_error_fix_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """payload.error_id without a confirmed error.fix activity must not get
    the unattended auto-push shortcut (non-None expected_branch). Writing the
    payload directly bypasses error-fix bootstrap, so error_fix_confirmed
    stays False — same gate as chain.is_error_fix_originated."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task["payload"] = {"error_id": "abc123def456"}
        store.write("task", "update", tid, task)
    finally:
        store.close()

    called = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert "error_fix_confirmed" in str(exc.value.code)
    assert called["n"] == 0
    assert _checklist(tmp_path, tid)["pushed"] != "ja"


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

    push_called = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        push_called["n"] += 1
        return "abc1234"

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    run(tmp_path, ["run", "--task", tid, "--head", "abc1234"])
    capsys.readouterr()
    assert push_called["n"] == 1
    assert _checklist(tmp_path, tid)["pushed"] == "ja"

    for stage, vendor in (("grok-pr", "grok"), ("codex-pr", "codex")):
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
            key = f"{vendor}_pr_{dimension}"
            assert _checklist(tmp_path, tid)[key] == "ja"

    monkeypatch.setattr(
        "agent_cli.git_act.measure_mergeable",
        lambda *, cwd, runner, expected_head=None: "ok",
    )
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["mergeable"] == "ja"


def test_interpret_lane_rejects_multiple_report_blocks() -> None:
    """Early example STATUS/FINDINGS + real FINDINGS with a bug must not pass."""
    from agent_cli.lane import LaneResult
    from agent_cli.run_core import _interpret_lane

    stdout = (
        "Example format:\n"
        "STATUS: complete\n"
        "FINDINGS: none\n"
        "\n"
        "FINDINGS:\n"
        "- real bug in foo.py:1\n"
        "STATUS: complete\n"
    )
    result = LaneResult(
        role="reviewer",
        vendor="grok",
        status="complete",
        argv=["grok"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    decision, _findings = _interpret_lane("reviewer", result)
    assert decision != "pass"
    assert decision == "retry"


def test_interpret_lane_accepts_preamble_before_clean_report() -> None:
    """Reasoning narration before a clean STATUS/FINDINGS block still passes.

    Covers the gap left by reverting the preamble-emptiness check in
    has_single_terminal_report: legitimate reasoning narration ahead of an
    otherwise clean terminal report must still resolve to "pass".
    """
    from agent_cli.lane import LaneResult
    from agent_cli.run_core import _interpret_lane

    stdout = (
        "Let me walk through the diff section by section and check each "
        "changed file against the review dimension before concluding.\n"
        "\n"
        "STATUS: complete\n"
        "FINDINGS: none\n"
    )
    result = LaneResult(
        role="reviewer",
        vendor="grok",
        status="complete",
        argv=["grok"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    decision, findings = _interpret_lane("reviewer", result)
    assert decision == "pass"
    assert findings is None


def test_reviewer_gets_distinct_review_spec_with_diff_and_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer launch must receive a review prompt, not the implementer .spec.md."""
    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    impl_spec = tmp_path / "implement-spec.md"
    impl_spec.write_text("# Task\n\nImplement the feature.\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        path = str(kwargs.get("spec_file") or "")
        captured["spec_file"] = path
        body = Path(path).read_text(encoding="utf-8")
        captured["body"] = body
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS: none\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)
    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    run(
        tmp_path,
        [
            "run",
            "--task",
            tid,
            "--spec-file",
            str(impl_spec),
            "--no-tmux",
            "--cwd",
            str(tmp_path),
        ],
    )
    capsys.readouterr()
    assert captured.get("spec_file")
    assert Path(captured["spec_file"]).resolve() != impl_spec.resolve()
    body = captured["body"]
    assert "Implement the feature" in body  # context includes implementer spec
    assert "diff --git a/src/foo.py" in body
    assert "STATUS: complete | partial | timeout | unavailable" in body
    assert "FINDINGS:" in body
    assert "FINDINGS: 0" in body or "`FINDINGS: 0`" in body
    assert _checklist(tmp_path, tid)["reviewer_approved"] == "ja"


def test_execute_spine_step_unbounded_round_cap_without_kwarg(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_run path (no round_cap kwarg) must not fail at 5 rejection rounds."""
    from agent_cli.run_core import execute_spine_step

    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    spec = tmp_path / "review-spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return LaneResult(
            role="reviewer",
            vendor="grok",
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="STATUS: complete\nFINDINGS:\n- still broken\n",
            stderr="",
        )

    def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
        if "diff" in argv:
            return Completed(0, "diff --git a/x b/x\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.run_core.launch", fake_launch)
    monkeypatch.setattr("agent_cli.main._exec_argv", fake_exec)

    store = _store(tmp_path)
    try:
        # Drive more than 5 rejection rounds the way cmd_run calls execute_spine_step
        # (no round_cap kwarg → unbounded).
        for _ in range(6):
            # Re-open reviewer step after each rejection by ensuring implementer is done.
            task = store.row("task", tid)
            assert task is not None
            # After rejection, implementer_done is nein — finish implementer again.
            cl = _checklist(tmp_path, tid)
            if cl.get("implementer_done") != "ja":
                # Manually set implementer_done via a quick pass launch path is heavy;
                # instead close via checklist after starting a fresh implementer finish.
                round_n = int(task.get("current_round") or 1)
                if _task_state(tmp_path, tid) == "implementing":
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
                            str(round_n),
                        ],
                    )
                    impl_id = _last_agent_id(capsys.readouterr().out)
                    run(tmp_path, ["agent", "finish", "--id", impl_id, "--verdict", "done"])
                    capsys.readouterr()
                    run(tmp_path, ["run", "--task", tid])  # close implementer_done
                    capsys.readouterr()
            outcome = execute_spine_step(
                store,
                tid,
                head=None,
                dry_run=False,
                spec_file=str(spec),
                cwd=str(tmp_path),
                tmux=False,
                exec_argv=fake_exec,
            )
            assert outcome.kind != "failed" or "round cap" not in (
                outcome.message or outcome.reason or ""
            )
            if outcome.kind == "failed":
                assert "round cap" not in (outcome.message or "")
                assert "round cap" not in (outcome.reason or "")
            assert outcome.kind == "rejected_new_round"
        final_round = int((store.row("task", tid) or {}).get("current_round") or 0)
        assert final_round > 5
        assert "round cap" not in str(outcome.message or "")
    finally:
        store.close()


def test_check_record_rejects_invalid_head(tmp_path: Path) -> None:
    """--head must be a lowercase hex git SHA; ref names are refused before store access."""
    with pytest.raises(SystemExit, match="--head must be a git SHA"):
        run(
            tmp_path,
            [
                "check",
                "record",
                "--task",
                "does-not-matter",
                "--name",
                "local",
                "--command",
                "pytest -q",
                "--result",
                "pass",
                "--head",
                "origin/develop",
            ],
        )


def test_local_check_reruns_after_same_head_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior fail for the current head must not suppress a re-run; later pass satisfies."""
    from agent_cli.run_core import execute_spine_step

    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["local_check_pass"] != "ja"

    same_sha = "cccccccccccccccccccccccccccccccccccccccc"
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "local",
            "--command",
            "pytest -q",
            "--result",
            "fail",
            "--output",
            "boom",
            "--head",
            same_sha,
        ],
    )
    capsys.readouterr()
    assert any(
        c.get("name") == "local"
        and c.get("result") == "fail"
        and str(c.get("head_sha") or "").lower() == same_sha
        for c in _local_checks(tmp_path, tid)
    )

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task = dict(task)
        task["state"] = "local-check"
        store.write(
            "task",
            "update",
            tid,
            {k: v for k, v in task.items() if not str(k).startswith("_")},
        )

        check_calls = {"n": 0}

        def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
            if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
                return Completed(0, same_sha + "\n", "")
            if argv and argv[0] == "pytest":
                check_calls["n"] += 1
                return Completed(0, "ok\n", "")
            return Completed(0, "", "")

        outcome = execute_spine_step(
            store,
            tid,
            head=same_sha,
            cwd=str(tmp_path),
            tmux=False,
            exec_argv=fake_exec,
        )
        assert check_calls["n"] == 1, "must re-run check after same-head fail"
        assert outcome.kind in ("closed", "agent_closed") or outcome.key == "local_check_pass"
        checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
        assert any(
            c.get("name") == "local"
            and c.get("result") == "pass"
            and str(c.get("head_sha") or "").lower() == same_sha
            for c in checks
        )
    finally:
        store.close()
    assert _checklist(tmp_path, tid)["local_check_pass"] == "ja"


def test_local_check_reruns_after_same_head_pass_then_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last-wins: a later same-head fail must re-run even when an earlier pass exists."""
    from agent_cli.run_core import execute_spine_step

    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()
    assert _checklist(tmp_path, tid)["local_check_pass"] != "ja"

    same_sha = "dddddddddddddddddddddddddddddddddddddddd"
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "local",
            "--command",
            "pytest -q",
            "--result",
            "pass",
            "--output",
            "ok",
            "--head",
            same_sha,
        ],
    )
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "local",
            "--command",
            "pytest -q",
            "--result",
            "fail",
            "--output",
            "regression",
            "--head",
            same_sha,
        ],
    )
    capsys.readouterr()

    store = _store(tmp_path)
    try:
        task = store.row("task", tid)
        assert task is not None
        task = dict(task)
        task["state"] = "local-check"
        store.write(
            "task",
            "update",
            tid,
            {k: v for k, v in task.items() if not str(k).startswith("_")},
        )

        check_calls = {"n": 0}

        def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
            if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
                return Completed(0, same_sha + "\n", "")
            if argv and argv[0] == "pytest":
                check_calls["n"] += 1
                return Completed(0, "ok\n", "")
            return Completed(0, "", "")

        outcome = execute_spine_step(
            store,
            tid,
            head=same_sha,
            cwd=str(tmp_path),
            tmux=False,
            exec_argv=fake_exec,
        )
        assert check_calls["n"] == 1, "must re-run check after same-head pass→fail"
        assert outcome.kind in ("closed", "agent_closed") or outcome.key == "local_check_pass"
        checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
        assert any(
            c.get("name") == "local"
            and c.get("result") == "pass"
            and str(c.get("head_sha") or "").lower() == same_sha
            for c in checks
        )
    finally:
        store.close()
    assert _checklist(tmp_path, tid)["local_check_pass"] == "ja"


def test_local_check_reruns_after_pr_rejection_with_new_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale local_check pass for an old head must not satisfy a reopened step."""
    from agent_cli.run_core import execute_spine_step

    tid = _bootstrap_implement(tmp_path, capsys)
    _advance_to_pushed(tmp_path, tid, capsys, monkeypatch)
    old_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    new_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # Record a passing check bound to the old head, then reopen local_check_pass.
    run(
        tmp_path,
        [
            "check",
            "record",
            "--task",
            tid,
            "--name",
            "local",
            "--command",
            "pytest -q",
            "--result",
            "pass",
            "--output",
            "ok",
            "--head",
            old_sha,
        ],
    )
    capsys.readouterr()
    store = _store(tmp_path)
    try:
        for row in store.rows("checklist_item"):
            if row.get("task_id") == tid and row.get("key") == "local_check_pass":
                row = dict(row)
                row["status"] = "nein"
                row["evidence"] = "pr rejection reset"
                store.write(
                    "checklist_item",
                    "update",
                    row["id"],
                    {k: v for k, v in row.items() if not str(k).startswith("_")},
                )
        # Also reopen pushed so spine lands on local_check_pass first... actually
        # after local_check_pass=nein with prior steps ja, next is local_check_pass.
        check_calls = {"n": 0}

        def fake_exec(argv: list[str], *, cwd: str | None = None) -> Completed:
            if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
                return Completed(0, new_sha + "\n", "")
            if argv and argv[0] == "pytest":
                check_calls["n"] += 1
                return Completed(0, "ok\n", "")
            return Completed(0, "", "")

        outcome = execute_spine_step(
            store,
            tid,
            head=new_sha,
            cwd=str(tmp_path),
            tmux=False,
            exec_argv=fake_exec,
        )
        assert check_calls["n"] == 1, "must re-run check for the new head"
        assert outcome.kind in ("closed", "agent_closed") or outcome.key == "local_check_pass"
        checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
        assert any(
            c.get("name") == "local"
            and c.get("result") == "pass"
            and str(c.get("head_sha") or "").lower() == new_sha
            for c in checks
        )
    finally:
        store.close()


def test_chain_snapshot_does_not_resolve_stale_head_across_fresh_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a PR-gate rejection + re-push, a snapshot built the way a fresh
    process would (no in-memory head threaded through) must not resolve to
    the stale pre-rejection head via an old, superseded gate row."""
    from agent_cli import main as main_mod
    from agent_cli.chain import close_allowed
    from agent_cli.run_core import execute_spine_step

    tid = _bootstrap_implement(tmp_path, capsys)
    _finish_implementer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    _finish_reviewer(tmp_path, tid, capsys)
    run(tmp_path, ["run", "--task", tid])
    capsys.readouterr()

    old_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    new_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    shas = [old_sha, new_sha]
    push_calls = {"n": 0}

    def fake_push(*, cwd: str, runner, expected_branch=None):  # type: ignore[no-untyped-def]
        i = push_calls["n"]
        push_calls["n"] += 1
        return shas[min(i, len(shas) - 1)]

    def fake_exec(argv, *, cwd=None):  # type: ignore[no-untyped-def]
        if argv[:2] == ["git", "rev-parse"] and "HEAD" in argv:
            return Completed(0, shas[min(push_calls["n"], len(shas) - 1)] + "\n", "")
        if "diff" in argv:
            if "--name-only" in argv:
                return Completed(0, "src/foo.py\n", "")
            return Completed(0, "diff --git a/src/foo.py b/src/foo.py\n+fixed\n", "")
        if "rev-parse" in argv or "merge-base" in argv:
            return Completed(0, "abcdef1\n", "")
        if argv and argv[0] == "pytest":
            return Completed(0, "ok\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr("agent_cli.git_act.push_branch", fake_push)
    spec = tmp_path / "spec.md"
    spec.write_text("do work\n", encoding="utf-8")

    store = _store(tmp_path)
    try:
        # 1) local_check_pass, then close "pushed" @ old_sha.
        outcome = None
        for expected_key in ("local_check_pass", "pushed"):
            outcome = execute_spine_step(
                store,
                tid,
                head=None,
                spec_file=str(spec),
                cwd=str(tmp_path),
                tmux=False,
                exec_argv=fake_exec,
            )
            assert outcome.kind == "closed" and outcome.key == expected_key
        head = outcome.head_sha
        assert head == old_sha

        # 2) grok_pr_quality approves @ old_sha.
        def approve_launch(**kwargs):  # type: ignore[no-untyped-def]
            return LaneResult(
                role=kwargs["role"],
                vendor=kwargs["vendor"],
                status="complete",
                argv=[kwargs["vendor"]],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS: none\n",
                stderr="",
            )

        monkeypatch.setattr("agent_cli.run_core.launch", approve_launch)
        outcome = execute_spine_step(
            store,
            tid,
            head=head,
            spec_file=str(spec),
            cwd=str(tmp_path),
            tmux=False,
            exec_argv=fake_exec,
        )
        assert outcome.key == "grok_pr_quality"
        assert _checklist(tmp_path, tid)["grok_pr_quality"] == "ja"

        # 3) grok_pr_logic rejects @ old_sha -> resets the spine, new round.
        def reject_launch(**kwargs):  # type: ignore[no-untyped-def]
            return LaneResult(
                role=kwargs["role"],
                vendor=kwargs["vendor"],
                status="complete",
                argv=[kwargs["vendor"]],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS:\n- fix the retry loop\n",
                stderr="",
            )

        monkeypatch.setattr("agent_cli.run_core.launch", reject_launch)
        outcome = execute_spine_step(
            store,
            tid,
            head=head,
            spec_file=str(spec),
            cwd=str(tmp_path),
            tmux=False,
            exec_argv=fake_exec,
        )
        assert outcome.kind == "rejected_new_round"
        assert _checklist(tmp_path, tid)["grok_pr_quality"] != "ja"
        assert _checklist(tmp_path, tid)["pushed"] != "ja"

        # 4) Re-drive implementer_done -> reviewer_approved -> local_check_pass
        #    -> pushed @ new_sha. Deliberately stop here -- grok_pr_quality /
        #    grok_pr_logic for the new round have NOT run yet, so the only
        #    gate rows in the ledger are the stale old_sha ones from step 2/3.
        def pass_launch(**kwargs):  # type: ignore[no-untyped-def]
            return LaneResult(
                role=kwargs["role"],
                vendor=kwargs["vendor"],
                status="complete",
                argv=[kwargs["vendor"]],
                returncode=0,
                stdout="STATUS: complete\nFINDINGS: none\n",
                stderr="",
            )

        monkeypatch.setattr("agent_cli.run_core.launch", pass_launch)
        outcome = None
        for expected_key in (
            "implementer_done",
            "reviewer_approved",
            "local_check_pass",
            "pushed",
        ):
            outcome = execute_spine_step(
                store,
                tid,
                head=None,
                spec_file=str(spec),
                cwd=str(tmp_path),
                tmux=False,
                exec_argv=fake_exec,
            )
            assert outcome.key == expected_key, (
                outcome.key,
                outcome.kind,
                outcome.reason,
            )
        assert _checklist(tmp_path, tid)["pushed"] == "ja"
        assert outcome is not None
        assert outcome.head_sha == new_sha

        # 5) The scenario under test: a snapshot built the way a brand-new
        #    process would build it -- no extra_head, nothing threaded in memory.
        fresh_snap = main_mod._chain_snapshot(store, tid)
        assert fresh_snap["head_sha"] == new_sha, (
            f"fresh snapshot resolved head={fresh_snap['head_sha']!r}, expected "
            f"the current pushed sha {new_sha!r} (stale-head cross-scan bug)"
        )
        verdict = close_allowed(
            "implement",
            "grok_pr_quality",
            checklist=fresh_snap["checklist"],
            source="script",
            evidence="run auto",
            snapshot=fresh_snap,
        )
        assert not verdict.allowed, (
            "grok_pr_quality must not auto-close from the stale pre-rejection "
            "approval recorded at the old head"
        )
    finally:
        store.close()
