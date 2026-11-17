"""Tests for the one-job runner worker.

The provider and GitHub transport are faked; the store is the real PostgreSQL
store the rest of the runner tests use. No test starts a vendor, a lane, tmux,
`gh` or `git`.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import agent_cli.runner_worker as runner_worker
from agent_cli.ingest import job_row
from agent_cli.lane import LaneResult
from agent_cli.runner_cmd import runner_paths
from agent_cli.runtime import Completed
from agent_cli.store import Store

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
REPO = "example-owner/example-repository"
DIFF = "diff --git a/x b/x\n+new\n"
REVIEW_URL = (
    "https://github.com/example-owner/example-repository/pull/7"
    "#pullrequestreview-77"
)
TEMPLATE = "Review {{REPO}} {{NUMBER}} {{BASE}} {{HEAD}} {{JOB_ID}}.\n"


class _FakeAccount:
    def __init__(self, login: str = "example-bot", name: str = "test") -> None:
        self.name = name
        self.login = login
        # The executor reads these when it redacts a failed row's error.
        self.gh_config_dir = "/test/gh"
        self.git_identity: dict[str, str] | None = None

    def runner(self, runner: Any) -> Any:
        return runner


class _FakeAccounts:
    def __init__(self, accounts: dict[str, _FakeAccount]) -> None:
        self._accounts = accounts

    def for_session(self, session_id: str) -> _FakeAccount:
        account = self._accounts.get(session_id)
        if account is None:
            raise runner_worker.GitHubAccountError(
                f"No GitHub account configured for session {session_id}"
            )
        return account


def _write_worker_files(
    tmp_path: Path,
    *,
    template: str = TEMPLATE,
    expected_login: str = "example-bot",
    write_policy: bool = True,
    write_ai: bool = True,
) -> Any:
    """Write the policy, AI selection and template a worker run needs."""
    paths = runner_paths(tmp_path / "runner")
    paths.logs.mkdir(parents=True, exist_ok=True)
    paths.work.mkdir(parents=True, exist_ok=True)
    paths.prompts.mkdir(parents=True, exist_ok=True)
    if write_policy:
        paths.policy.write_text(
            json.dumps({"expected_gh_user": expected_login}), encoding="utf-8"
        )
    if write_ai:
        ai = {
            "models": {"first": "claude", "second": "codex"},
            "lane_execution": {"mode": "this-device-is-lane-host"},
        }
        (tmp_path / "ai-accounts.json").write_text(json.dumps(ai), encoding="utf-8")
    (paths.prompts / "pr-review.md").write_text(template, encoding="utf-8")
    return paths


def _install_account(
    monkeypatch: pytest.MonkeyPatch, *, login: str = "example-bot", present: bool = True
) -> None:
    accounts = {"s": _FakeAccount(login)} if present else {}
    monkeypatch.setattr(
        runner_worker.github_accounts,
        "load_accounts",
        lambda _home: _FakeAccounts(accounts),
    )


def _fake_runner(
    calls: list[list[str]] | None = None,
    *,
    head: str = HEAD,
    head_rc: int = 0,
    base: str = "develop",
    base_rc: int = 0,
    check_rc: int = 0,
    diff: str = DIFF,
    diff_rc: int = 0,
    stderr: str = "",
) -> Any:
    rows = calls if calls is not None else []

    def run(argv: list[str]) -> Completed:
        rows.append(list(argv))
        if argv[:2] == ["git", "-C"] and argv[-1] == "HEAD^{commit}":
            return Completed(head_rc, f"{head}\n", stderr)
        if argv[:2] == ["gh", "api"] and argv[-1] == ".base.ref":
            return Completed(base_rc, f"{base}\n", stderr)
        if argv[:2] == ["git", "check-ref-format"]:
            return Completed(check_rc, "", stderr)
        if argv[:2] == ["git", "-C"] and "diff" in argv:
            return Completed(diff_rc, diff, stderr)
        raise AssertionError(argv)

    return run


def _fake_launch(
    calls: list[dict[str, Any]],
    *,
    status: str = "complete",
    protocol_result: str = "approved",
    stdout: str = "Finding.\nSTATUS: complete\nRESULT: approved\n",
    model: str = "m",
    sleep: float = 0.0,
    error: BaseException | None = None,
) -> Any:
    def launch(**kwargs: Any) -> LaneResult:
        calls.append(kwargs)
        if sleep:
            time.sleep(sleep)
        if error is not None:
            raise error
        return LaneResult(
            role="reviewer",
            vendor=str(kwargs["vendor"]),
            model=model,
            status=status,
            argv=[],
            returncode=0,
            stdout=stdout,
            stderr="",
            protocol_result=protocol_result,
        )

    return launch


def _mark_done(store: Store, activity_id: str) -> str:
    row = store.row("activity", activity_id)
    assert isinstance(row, dict)
    saved = {key: value for key, value in row.items() if not key.startswith("_")}
    saved["execution_status"] = "done"
    saved["result"] = {"url": REVIEW_URL}
    store.write("activity", "update", activity_id, saved)
    return f"review.post {activity_id} done"


def _done_executor(
    posted: list[str] | None = None, *, seen_exit: list[bool] | None = None,
    paths: Any = None, job_id: str = "job-1",
) -> Any:
    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        if posted is not None:
            posted.append(activity_id)
        if seen_exit is not None and paths is not None:
            seen_exit.append((paths.logs / f"{job_id}.exit").exists())
        return _mark_done(store, activity_id)

    return execute


def _insert_job(
    tmp_path: Path,
    paths: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_id: str = "job-1",
    queued: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    # Build a valid row first: the invalid values some tests seed would make
    # `job_row` itself refuse, and the worker has to be the one that rejects
    # them. A queued row is exactly what ingest writes: no state override and
    # no `worktree` field at all.
    row = job_row(
        session_id="s",
        repo=REPO,
        ref="7",
        job_type="pr-review",
        actor="alice",
    )
    row.update({"id": job_id})
    if not queued:
        row.update(
            {
                "state": "running",
                "worktree": str(paths.work / job_id),
            }
        )
    row.update(overrides)
    (paths.work / job_id).mkdir(parents=True, exist_ok=True)
    _install_account(monkeypatch)
    store = Store(tmp_path)
    try:
        store.write("job", "insert", job_id, row)
    finally:
        store.close()
    return row


class _DispatchHandoff:
    """A store that publishes dispatch's running update after the first read.

    The wrapped store is the real test store. The worker's first job read sees
    the fresh queued row, exactly as it does in production while dispatch is
    still between starting the tmux session and writing the row. The second
    read applies dispatch's one update and returns the running row.
    """

    def __init__(self, inner: Store, job_id: str, worktree: str) -> None:
        self._inner = inner
        self._job_id = job_id
        self._worktree = worktree
        self._job_reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def row(self, table: str, row_id: str) -> Any:
        if table == "job" and row_id == self._job_id:
            self._job_reads += 1
            if self._job_reads == 2:
                row = self._inner.row(table, row_id)
                assert isinstance(row, dict)
                updated = {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("_")
                }
                updated.update(
                    {"state": "running", "worktree": self._worktree}
                )
                self._inner.write("job", "update", row_id, updated)
        return self._inner.row(table, row_id)


def _run(
    paths: Any,
    tmp_path: Path,
    *,
    job_id: str = "job-1",
    cwd: str | None = None,
    runner: Any = None,
    launch: Any = None,
    execute: Any = None,
    heartbeat_seconds: float = 60,
    handoff_seconds: float = runner_worker.HANDOFF_SECONDS,
    handoff_poll_seconds: float = runner_worker.HANDOFF_POLL_SECONDS,
    out: Any = None,
    store: Any = None,
) -> tuple[int, io.BytesIO]:
    stdout = out if out is not None else io.BytesIO()
    own_store = store is None
    if store is None:
        store = Store(tmp_path)
    try:
        code = runner_worker.run_worker(
            paths,
            store,
            cwd if cwd is not None else str(paths.work / job_id),
            home=tmp_path,
            runner=runner or _fake_runner(),
            launch=launch or _fake_launch([]),
            execute_activity=execute or _done_executor(),
            heartbeat_seconds=heartbeat_seconds,
            handoff_seconds=handoff_seconds,
            handoff_poll_seconds=handoff_poll_seconds,
            out=stdout,
        )
    finally:
        if own_store:
            store.close()
    return code, stdout


def _transcript(paths: Any, job_id: str = "job-1") -> str:
    return (paths.logs / f"{job_id}.transcript").read_text(
        encoding="utf-8", errors="replace"
    )


def _exit_code(paths: Any, job_id: str = "job-1") -> str:
    return (paths.logs / f"{job_id}.exit").read_text(encoding="utf-8").strip()


# ------------------------------------------------------------------ wrapper


def test_worker_wrapper_runs_the_work_subcommand_with_the_exact_argv(
    tmp_path: Path,
) -> None:
    wrapper = runner_worker.worker_wrapper(tmp_path)

    trusted = Path(runner_worker.__file__).resolve().parent.parent
    assert shlex.split(wrapper) == [
        "env",
        f"AGENT_HOME={tmp_path}",
        f"PYTHONPATH={trusted}",
        sys.executable,
        "-P",
        "-m",
        "agent_cli.main",
        "runner",
        "work",
    ]


def test_worker_wrapper_refuses_a_relative_agent_home() -> None:
    # The dispatching command and the job run in different directories; a
    # relative home would name two places.
    with pytest.raises(ValueError, match="absolute"):
        runner_worker.worker_wrapper("home")


@pytest.mark.parametrize("inherited_pythonpath", [".", "", ":", "agent_cli/.."])
def test_worker_wrapper_does_not_import_agent_cli_from_the_job_worktree(
    tmp_path: Path, inherited_pythonpath: str
) -> None:
    # Dispatch runs the wrapper with the pull-request checkout as its working
    # directory. This repository copy stands in for that checkout and ships an
    # `agent_cli` package that records its own import. The wrapper's own
    # `PYTHONPATH` assignment and its `-P` flag must both keep it off
    # `sys.path`, whatever the tmux server's `PYTHONPATH` happens to be.
    home = tmp_path / "home"
    home.mkdir()
    shadow_cwd = tmp_path / "checkout"
    package = shadow_cwd / "agent_cli"
    package.mkdir(parents=True)
    marker = tmp_path / "shadow-imported"
    write_marker = f"open({str(marker)!r}, 'a').write('imported')\n"
    (package / "__init__.py").write_text(write_marker, encoding="utf-8")
    (package / "main.py").write_text(write_marker, encoding="utf-8")

    # Keep the wrapper's own `env` assignments and interpreter flags exactly as
    # it builds them; only the `runner work` tail is replaced so no real worker
    # starts.
    argv = shlex.split(runner_worker.worker_wrapper(home))
    probe_argv = argv[: argv.index("-m")] + [
        "-c",
        "import agent_cli; print(agent_cli.__file__)",
    ]
    environment = dict(os.environ)
    # What the worker inherits from the tmux server: a relative or empty entry
    # resolves against the checkout. The wrapper's own assignment must replace
    # this value; without it the shadow package is imported.
    environment["PYTHONPATH"] = inherited_pythonpath

    completed = subprocess.run(
        probe_argv,
        cwd=str(shadow_cwd),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert not marker.exists()
    imported = Path(completed.stdout.strip()).resolve()
    assert imported == Path(runner_worker.__file__).resolve().parent / "__init__.py"


# ---------------------------------------------------------------- happy path


def test_worker_gathers_facts_runs_one_read_only_lane_and_posts_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    calls: list[list[str]] = []
    launches: list[dict[str, Any]] = []
    posted: list[str] = []
    seen_exit: list[bool] = []

    code, out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(calls),
        launch=_fake_launch(launches),
        execute=_done_executor(
            posted, seen_exit=seen_exit, paths=paths, job_id="job-1"
        ),
    )

    assert code == 0
    assert _exit_code(paths) == "0"
    # The activity was still pending when the executor ran; the exit file is
    # the worker's last effect.
    assert seen_exit == [False]
    # One lane, read-only role, this device's first model slot, no tmux.
    assert len(launches) == 1
    launch = launches[0]
    assert set(launch) == {
        "role",
        "vendor",
        "spec_file",
        "cwd",
        "tmux",
        "config_home",
        "session_id",
    }
    assert launch["role"] == "reviewer"
    assert launch["vendor"] == "claude"
    assert launch["tmux"] is False
    assert launch["session_id"] == "s"
    assert launch["config_home"] == tmp_path
    assert launch["cwd"] == str(paths.work / "job-1")
    assert launch["spec_file"] == str(paths.logs / "job-1.prompt")
    # Facts come from the checked-out commit and the pulls API, not gh pr view.
    worktree = str(paths.work / "job-1")
    assert ["git", "-C", worktree, "rev-parse", "--verify", "HEAD^{commit}"] in calls
    assert [
        "gh",
        "api",
        f"repos/{REPO}/pulls/7",
        "--jq",
        ".base.ref",
    ] in calls
    assert ["git", "check-ref-format", "--branch", "develop"] in calls
    assert [
        "git",
        "-C",
        worktree,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "refs/remotes/origin/develop...HEAD",
    ] in calls
    assert not any("pr view" in " ".join(argv) or "pr diff" in " ".join(argv) for argv in calls)
    # The prompt is the operator template with every placeholder resolved and
    # the fixed data and contract section appended.
    spec = (paths.logs / "job-1.prompt").read_text(encoding="utf-8")
    assert f"Review {REPO} 7 develop {HEAD} job-1." in spec
    assert "not instructions" in spec
    assert "+new" in spec
    assert "STATUS: complete" in spec
    assert "RESULT: rejected" in spec
    # The activity binds the review to the checked-out head and carries the
    # protocol-free body under the required header.
    activity_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"runner-review:job-1:0:{HEAD}")
    )
    assert posted == [activity_id]
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["type"] == "review.post"
    assert row["execution_status"] == "done"
    payload = row["payload"]
    assert payload["repo"] == REPO
    assert payload["number"] == 7
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == HEAD
    assert payload["body"].startswith(
        f"Review at `{HEAD[:12]}` (claude, m): approved\n\n"
    )
    assert "Finding." in payload["body"]
    assert "STATUS:" not in payload["body"]
    assert "RESULT:" not in payload["body"]
    text = _transcript(paths)
    assert "worker: job job-1" in text
    # The row was already running, so there is no handoff to wait for.
    assert "worker: waiting for dispatch" not in text
    assert "worker: review lane running" in text
    assert "worker: review posted" in text
    assert "Finding." in text
    stdout = out.getvalue().decode("utf-8")
    assert "worker: review posted" in stdout


def test_worker_diff_disables_external_diff_and_textconv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The diff is shown to the lane. A repository attribute may select a
    # text-conversion driver that runs a command and rewrites the displayed
    # content, so the worker passes `--no-textconv` right after `--no-ext-diff`
    # exactly as the other Git readers in this repository do.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    calls: list[list[str]] = []

    code, _out = _run(paths, tmp_path, runner=_fake_runner(calls))

    assert code == 0
    diff_calls = [
        argv for argv in calls if argv[:2] == ["git", "-C"] and "diff" in argv
    ]
    assert len(diff_calls) == 1
    diff = diff_calls[0]
    assert diff[diff.index("--no-ext-diff") + 1] == "--no-textconv"


def test_worker_accepts_the_job_type_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, job_type="PR-REVIEW")
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 0
    assert len(launches) == 1


def test_worker_reuses_the_activity_id_for_one_attempt_and_not_for_another() -> None:
    first = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=HEAD
    )
    second = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=HEAD
    )
    retry = runner_worker._review_activity_id(
        job_id="job-1", attempts=1, head=HEAD
    )
    other_head = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=OTHER_HEAD
    )
    assert first == second
    assert first != retry
    assert first != other_head


# ------------------------------------------------------------------- handoff


def test_worker_waits_for_dispatch_to_publish_the_running_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fresh queued row has no worktree. Dispatch starts tmux before it writes
    # the running row, so the worker's first read sees this row; the second read
    # sees the update and the worker proceeds.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, queued=True)
    worktree = str(paths.work / "job-1")
    store = Store(tmp_path)
    try:
        fresh = store.row("job", "job-1")
        assert isinstance(fresh, dict)
        assert "worktree" not in fresh
        launches: list[dict[str, Any]] = []
        code, _out = _run(
            paths,
            tmp_path,
            launch=_fake_launch(launches),
            handoff_seconds=5,
            handoff_poll_seconds=0,
            store=_DispatchHandoff(store, "job-1", worktree),
        )
        row = store.row("job", "job-1")
    finally:
        store.close()

    assert code == 0
    assert _exit_code(paths) == "0"
    assert len(launches) == 1
    assert launches[0]["cwd"] == worktree
    assert isinstance(row, dict)
    assert row["state"] == "running"
    assert row["worktree"] == worktree
    assert "worker: waiting for dispatch" in _transcript(paths)


def test_worker_fails_when_dispatch_never_hands_the_job_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is the injected clock, not the 120-second default.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, queued=True)
    launches: list[dict[str, Any]] = []
    started = time.monotonic()

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        handoff_seconds=0.0,
        handoff_poll_seconds=0.5,
    )
    elapsed = time.monotonic() - started

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_HANDOFF in _transcript(paths)
    assert "job was not handed over by dispatch" in _transcript(paths)
    assert _exit_code(paths) == "2"
    assert elapsed < 5
    assert "worker: waiting for dispatch" in _transcript(paths)


def test_worker_rejects_a_handed_over_row_for_another_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, worktree="/somewhere/else")
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_WORKTREE in _transcript(paths)
    assert "worker: waiting for dispatch" not in _transcript(paths)


# --------------------------------------------------------------- identification


def test_worker_outside_the_work_root_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _install_account(monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths, tmp_path, cwd=str(outside), launch=_fake_launch(launches)
    )

    assert code == 2
    assert launches == []
    assert not paths.logs.exists() or not any(paths.logs.iterdir())


def test_worker_in_a_nested_directory_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    nested = paths.work / "job-1" / "nested"
    nested.mkdir()
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths, tmp_path, cwd=str(nested), launch=_fake_launch(launches)
    )

    assert code == 2
    assert launches == []
    assert not (paths.logs / "job-1.exit").exists()


def test_worker_with_an_unsafe_worktree_name_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _install_account(monkeypatch)
    unsafe = paths.work / "bad.id"
    unsafe.mkdir()
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths, tmp_path, cwd=str(unsafe), launch=_fake_launch(launches)
    )

    assert code == 2
    assert launches == []
    assert not any(paths.logs.iterdir())


# ------------------------------------------------------------------- failures


def test_worker_fails_closed_for_a_missing_job_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _install_account(monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_JOB_MISSING in _transcript(paths)
    assert _exit_code(paths) == "2"


def test_worker_fails_closed_for_a_foreign_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _install_account(monkeypatch)
    row = job_row(
        session_id="s", repo=REPO, ref="7", job_type="pr-review", actor="alice"
    )
    row.update(
        {
            "id": "job-1",
            "state": "running",
            "worktree": str(paths.work / "job-1"),
        }
    )
    (paths.work / "job-1").mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "job",
                "op": "insert",
                "row_id": row["id"],
                "payload": row,
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_FOREIGN_JOB in _transcript(paths)


def test_worker_fails_closed_for_a_job_that_is_not_queued_or_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, state="done")
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_NOT_RUNNING in _transcript(paths)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"attempts": "0"}, runner_worker.FAILED_ATTEMPTS),
        ({"attempts": True}, runner_worker.FAILED_ATTEMPTS),
        ({"session_id": "bad id"}, runner_worker.FAILED_SESSION),
        ({"worktree": None}, runner_worker.FAILED_WORKTREE),
        ({"worktree": "/somewhere/else"}, runner_worker.FAILED_WORKTREE),
        ({"repo": ""}, runner_worker.FAILED_REPO),
        ({"ref": "seven"}, runner_worker.FAILED_REF),
        ({"job_type": ""}, runner_worker.FAILED_JOB_TYPE_MISSING),
    ],
)
def test_worker_requires_every_mandatory_row_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    message: str,
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, **overrides)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert message in _transcript(paths)


def test_worker_fails_closed_for_a_job_type_it_does_not_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, job_type="merge-conflict")
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert "job type merge-conflict has no worker" in _transcript(paths)


def test_worker_fails_closed_without_a_prompt_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    (paths.prompts / "pr-review.md").write_text("  \n", encoding="utf-8")
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert "prompt template" in _transcript(paths)
    assert "is not usable" in _transcript(paths)


def test_worker_fails_closed_without_an_expected_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path, write_policy=False)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_EXPECTED_LOGIN in _transcript(paths)


def test_worker_fails_closed_without_a_configured_github_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    _install_account(monkeypatch, present=False)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert "no GitHub account for session s" in _transcript(paths)


def test_worker_fails_before_launch_when_the_account_is_not_the_runner_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    _install_account(monkeypatch, login="other-bot")
    launches: list[dict[str, Any]] = []
    calls: list[list[str]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(calls),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert calls == []
    assert "GitHub account other-bot is not the runner login" in _transcript(paths)


def test_worker_compares_the_expected_login_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path, expected_login="Example-Bot")
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 0
    assert len(launches) == 1


def test_worker_fails_closed_without_an_ai_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path, write_ai=False)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_LANE_START in _transcript(paths)


def test_worker_reports_a_lane_configuration_failure_with_safe_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `lane.launch` refuses a device that does not declare lane execution. That
    # message is written by this repository, so it is safe to keep.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            launches,
            sleep=0.02,
            error=SystemExit("lane_execution is not configured"),
        ),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert len(launches) == 1
    assert posted == []
    text = _transcript(paths)
    assert "review lane could not start: lane_execution is not configured" in text
    assert not any(
        thread.name.startswith("runner-worker-heartbeat")
        and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_worker_fails_closed_when_the_head_is_not_a_lowercase_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(head=HEAD.upper()),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_HEAD in _transcript(paths)


def test_worker_fails_closed_when_the_base_ref_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(check_rc=1),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert f"pull request {REPO}#7 is not readable" in _transcript(paths)


def test_worker_never_copies_gh_stderr_into_the_transcript_or_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(base_rc=1, stderr="SECRET-STDERR"),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    text = _transcript(paths)
    assert "SECRET-STDERR" not in text
    assert "SECRET-STDERR" not in out.getvalue().decode("utf-8")
    assert f"pull request {REPO}#7 is not readable" in text


def test_worker_fails_closed_when_the_diff_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(diff_rc=1, stderr="fatal: no diff"),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert "diff against develop failed" in _transcript(paths)
    assert "fatal: no diff" not in _transcript(paths)


def test_worker_rejects_an_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(diff=""),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert "pull request has no diff against develop" in _transcript(paths)


def test_worker_rejects_an_oversized_diff_instead_of_truncating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(diff="x" * (runner_worker.MAX_DIFF_CHARS + 1)),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_DIFF_TOO_LARGE in _transcript(paths)
    assert not (paths.logs / "job-1.prompt").exists()


@pytest.mark.parametrize("status", ["partial", "timeout", "", "unavailable"])
def test_worker_fails_closed_when_the_lane_is_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches, status=status),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert len(launches) == 1
    assert posted == []
    expected = status or "unavailable"
    assert f"review lane ended {expected}" in _transcript(paths)
    store = Store(tmp_path)
    try:
        assert store.rows("activity") == []
    finally:
        store.close()


@pytest.mark.parametrize("protocol_result", ["done", "blocked", "", "ask"])
def test_worker_requires_an_approved_or_rejected_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol_result: str
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches, protocol_result=protocol_result),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert posted == []
    assert "review lane ended complete" in _transcript(paths)


def test_worker_fails_closed_when_the_review_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            launches, stdout="STATUS: complete\nRESULT: approved\n"
        ),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert posted == []
    assert runner_worker.FAILED_REVIEW_EMPTY in _transcript(paths)


def test_worker_removes_protocol_lines_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [],
            stdout="Finding.\nstatus: complete\nresult: approved\n",
            protocol_result="approved",
        ),
        execute=_done_executor(posted),
    )

    assert code == 0
    store = Store(tmp_path)
    try:
        row = store.row("activity", posted[0])
    finally:
        store.close()
    assert row is not None
    assert row["payload"]["body"].endswith("Finding.")


def test_worker_rejects_an_oversized_final_review_instead_of_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            launches,
            stdout="r" * (runner_worker.MAX_REVIEW_CHARS + 1)
            + "\nSTATUS: complete\nRESULT: approved\n",
        ),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert posted == []
    assert runner_worker.FAILED_REVIEW_TOO_LONG in _transcript(paths)


def test_worker_fails_when_the_review_was_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        execute=lambda _store, _runner, _id: "review.post job-1 error",
    )

    assert code == 2
    assert runner_worker.FAILED_REVIEW_POST in _transcript(paths)


def test_worker_pins_the_account_validated_before_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The account is checked against `expected_gh_user` before the lane. If the
    # session binding changes during the lane, the queued row must still carry
    # the validated account so the executor refuses the switch: the review is
    # not posted through the new login.
    from agent_cli import github_act

    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    validated = _FakeAccount(login="example-bot", name="test")
    switched = _FakeAccount(login="someone-else", name="other")

    class _SwitchingAccounts:
        account = validated

        def for_session(self, _session_id: str) -> _FakeAccount:
            return self.account

    accounts = _SwitchingAccounts()
    monkeypatch.setattr(
        runner_worker.github_accounts,
        "load_accounts",
        lambda _home: accounts,
    )
    monkeypatch.setattr(github_act, "load_accounts", lambda _home: accounts)
    launches: list[dict[str, Any]] = []
    calls: list[list[str]] = []

    def launch(**kwargs: Any) -> LaneResult:
        launches.append(kwargs)
        # The session's binding changes while the lane runs.
        accounts.account = switched
        return LaneResult(
            role="reviewer",
            vendor=str(kwargs["vendor"]),
            model="m",
            status="complete",
            argv=[],
            returncode=0,
            stdout="Finding.\nSTATUS: complete\nRESULT: approved\n",
            stderr="",
            protocol_result="approved",
        )

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "[]", "")

    code, _out = _run(
        paths,
        tmp_path,
        launch=launch,
        execute=lambda store, _runner, activity_id: github_act.execute_activity(
            store, runner, activity_id
        ),
    )

    assert code == 2
    assert len(launches) == 1
    assert runner_worker.FAILED_REVIEW_POST in _transcript(paths)
    # The executor refused before any gh call, so nothing was posted.
    assert not any("-X" in argv and "POST" in argv for argv in calls)
    activity_id = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=HEAD
    )
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["execution_status"] == "error"
    assert "account binding changed" in row["execution_error"].lower()
    assert row["execution_account"] == {
        "account": "test",
        "login": "example-bot",
    }


def test_worker_does_not_execute_an_activity_that_is_already_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-running one attempt derives the previous attempt's activity id. A
    # review that was already delivered stays delivered, and the executor is
    # never asked to run it again.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=HEAD
    )
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "s",
                "type": "review.post",
                "payload": {
                    "repo": REPO,
                    "number": 7,
                    "body": "already posted",
                    "event": "COMMENT",
                    "commit_id": HEAD,
                },
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        execute=_done_executor(posted),
    )

    assert code == 0
    assert len(launches) == 1
    assert posted == []
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["payload"]["body"] == "already posted"


def test_worker_resets_a_failed_activity_to_pending_before_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._review_activity_id(
        job_id="job-1", attempts=0, head=HEAD
    )
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "s",
                "type": "review.post",
                "payload": {
                    "repo": REPO,
                    "number": 7,
                    "body": "failed attempt",
                    "event": "COMMENT",
                    "commit_id": HEAD,
                },
                "execution_status": "error",
            },
        )
    finally:
        store.close()
    seen: list[str] = []

    def execute(inner: Store, _runner: Any, inner_id: str) -> str:
        row = inner.row("activity", inner_id)
        assert row is not None
        seen.append(str(row["execution_status"]))
        return _mark_done(inner, inner_id)

    code, _out = _run(paths, tmp_path, execute=execute)

    assert code == 0
    assert seen == ["pending"]
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["execution_status"] == "done"


# ------------------------------------------------------------------ heartbeat


def test_heartbeat_interval_stays_inside_the_shortest_stall_window() -> None:
    # `doctor` accepts the smallest positive whole `stall_minutes`, and the
    # watchdog turns it into a window of `stall_minutes * 60` seconds against
    # a baseline stored as whole seconds. The beat must leave room for a beat
    # to be missed and a supervise pass to sample just after one, or a quiet
    # but healthy lane is judged stalled and killed at the minimum budget.
    from agent_cli import health

    shortest_minutes = next(
        minutes for minutes in range(1, 10) if health._positive_budget(minutes)
    )
    assert runner_worker.HEARTBEAT_SECONDS * 2 < shortest_minutes * 60


def test_heartbeat_writes_every_beat_to_the_transcript_and_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches, sleep=0.2),
        heartbeat_seconds=0.01,
    )

    assert code == 0
    heartbeat = "worker: review lane running"
    text = _transcript(paths)
    stdout = out.getvalue().decode("utf-8")
    # The worker writes one "review lane running" line before it starts the
    # lane; only the occurrences after it are heartbeat beats. Every beat must
    # reach both surfaces, not just the transcript.
    assert text.count(heartbeat) - 1 >= 2
    assert stdout.count(heartbeat) - 1 >= 2


def test_heartbeat_stops_when_the_lane_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            launches, sleep=0.03, error=OSError("provider diagnostic")
        ),
        heartbeat_seconds=0.01,
    )

    assert code == 2
    text = _transcript(paths)
    assert runner_worker.FAILED_LANE_START in text
    # OSError text is never copied anywhere.
    assert "provider diagnostic" not in text
    assert not any(
        thread.name.startswith("runner-worker-heartbeat")
        and thread.is_alive()
        for thread in threading.enumerate()
    )


class _BlockingHeartbeatSink:
    """An output stream that blocks one heartbeat write until the test releases it.

    The worker writes one "review lane running" progress line before the lane
    starts; the second occurrence of that text is the first heartbeat, and it is
    the write that blocks.
    """

    def __init__(self, heartbeat_text: str) -> None:
        self._heartbeat_text = heartbeat_text
        self._seen = 0
        self._guard = threading.Lock()
        self._buffer = io.BytesIO()
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes | str) -> int:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        block = False
        if text.strip() == self._heartbeat_text:
            with self._guard:
                self._seen += 1
                block = self._seen == 2
        if block:
            self.blocked.set()
            # The test always releases this in a finally; the timeout only
            # keeps a broken test from stranding the heartbeat thread.
            self.release.wait(5)
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        return self._buffer.write(raw)

    def flush(self) -> None:
        self._buffer.flush()


def test_exit_file_waits_for_a_blocked_heartbeat_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The join after the lane must be unconditional: a heartbeat write still in
    # progress may not leave the exit file behind it while the worker can still
    # write another transcript line.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    sink = _BlockingHeartbeatSink("worker: review lane running")
    exit_path = paths.logs / "job-1.exit"
    lane_running = threading.Event()
    finish_lane = threading.Event()
    outcome: list[int] = []
    failures: list[BaseException] = []

    def launch(**kwargs: Any) -> LaneResult:
        # Hold the lane open while the heartbeat beats, so the blocked write is
        # guaranteed to belong to a running lane rather than a stopped one.
        lane_running.set()
        finish_lane.wait(5)
        return LaneResult(
            role="reviewer",
            vendor=str(kwargs["vendor"]),
            model="m",
            status="complete",
            argv=[],
            returncode=0,
            stdout="Finding.\nSTATUS: complete\nRESULT: approved\n",
            stderr="",
            protocol_result="approved",
        )

    def run() -> None:
        try:
            code, _out = _run(
                paths,
                tmp_path,
                launch=launch,
                heartbeat_seconds=0.01,
                out=sink,
            )
            outcome.append(code)
        except BaseException as exc:  # surfaced on the test thread below
            failures.append(exc)

    worker = threading.Thread(target=run, name="test-runner-worker")
    worker.start()
    try:
        assert lane_running.wait(5), "the lane never started"
        assert sink.blocked.wait(5), "the heartbeat never reached the output sink"
        finish_lane.set()
        # The lane is over, so the worker can only be in the heartbeat join now.
        time.sleep(0.2)
        assert worker.is_alive()
        assert not exit_path.exists()
    finally:
        finish_lane.set()
        sink.release.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert failures == []
    assert outcome == [0]
    assert _exit_code(paths) == "0"
    assert "worker: review lane running" in sink._buffer.getvalue().decode("utf-8")


def test_lane_start_reports_safe_configuration_detail() -> None:
    assert (
        runner_worker._lane_start_message(SystemExit("vendor must be claude"))
        == "review lane could not start: vendor must be claude"
    )
    assert runner_worker._lane_start_message(OSError("secret")) == (
        "review lane could not start"
    )
    assert runner_worker._lane_start_message(
        runner_worker.ModelSlotsError("secret")
    ) == "review lane could not start"


# ------------------------------------------------------------ prompt builder


def test_build_review_spec_replaces_only_the_five_known_placeholders() -> None:
    spec = runner_worker.build_review_spec(
        "{{REPO}}#{{NUMBER}} {{BASE}} {{HEAD}} {{JOB_ID}} {{UNKNOWN}}",
        repo=REPO,
        number=7,
        base="develop",
        head=HEAD,
        job_id="job-1",
        diff=DIFF,
    )

    assert f"{REPO}#7 develop {HEAD} job-1 {{{{UNKNOWN}}}}" in spec


@pytest.mark.parametrize(
    "base",
    [
        "release{{HEAD}}",
        "release{{JOB_ID}}",
        "release\\1",
        "release\\g<0>",
    ],
)
def test_build_review_spec_substitutes_once_and_never_rescans_a_value(
    base: str,
) -> None:
    # A value filled in for one token must not be offered to the others. The
    # base ref is a branch name `git check-ref-format --branch` accepts, so it
    # can legitimately contain braces; as data it must stay literal. A value
    # with a backslash sequence must come out literally too, which is why the
    # replacement is callable and not a template string.
    spec = runner_worker.build_review_spec(
        "base={{BASE}} head={{HEAD}} job={{JOB_ID}} end",
        repo=REPO,
        number=7,
        base=base,
        head=HEAD,
        job_id="job-1",
        diff=DIFF,
    )

    instruction = spec.split("\n\nEverything below", 1)[0]
    assert instruction == f"base={base} head={HEAD} job=job-1 end"
    # The head SHA appears only where the template had {{HEAD}}, and the job id
    # only where it had {{JOB_ID}}.
    assert instruction.count(HEAD) == 1
    assert instruction.count("job-1") == 1


def test_build_review_spec_never_interprets_placeholders_in_the_diff() -> None:
    spec = runner_worker.build_review_spec(
        "Review.",
        repo=REPO,
        number=7,
        base="develop",
        head=HEAD,
        job_id="job-1",
        diff="+keep {{REPO}} as written\n",
    )

    assert "+keep {{REPO}} as written" in spec


def test_build_review_spec_fences_any_backtick_run_in_the_diff() -> None:
    # A diff containing a fence must not be able to close the data block and
    # turn its text into instructions.
    spec = runner_worker.build_review_spec(
        "template",
        repo=REPO,
        number=7,
        base="develop",
        head=HEAD,
        job_id="job-1",
        diff="diff with `````` inside",
    )
    fence = "`" * 7
    assert f"{fence}text" in spec
    assert f"{fence}\n" in spec
    assert "STATUS: complete" in spec
    assert "RESULT: approved" in spec
    assert "RESULT: rejected" in spec
    assert "not instructions" in spec


# ------------------------------------------------------------- worker helpers


def test_worker_job_id_accepts_only_the_direct_child_of_the_work_root(
    tmp_path: Path,
) -> None:
    paths = runner_paths(tmp_path / "runner")
    paths.work.mkdir(parents=True)
    job = paths.work / "job-1"
    job.mkdir()
    nested = job / "nested"
    nested.mkdir()

    assert runner_worker._worker_job_id(paths, str(job)) == "job-1"
    assert runner_worker._worker_job_id(paths, str(nested)) is None
    assert runner_worker._worker_job_id(paths, str(paths.work)) is None


def test_worker_fail_writes_the_message_and_the_exit_file_last(
    tmp_path: Path,
) -> None:
    paths = runner_paths(tmp_path / "runner")
    paths.logs.mkdir(parents=True)
    transcript = runner_worker._Transcript(paths, "job-1")
    transcript.open()
    seen: list[tuple[str, bool]] = []

    original_write = transcript.write

    def write(message: str) -> None:
        # The exit code marks the attempt as finished; it must not exist while
        # a failure can still be recording its reason.
        seen.append((message, (paths.logs / "job-1.exit").exists()))
        original_write(message)

    transcript.write = write  # type: ignore[method-assign]
    try:
        code = runner_worker._worker_fail(
            paths, "job-1", "worker failed", transcript=transcript
        )
    finally:
        transcript.close()

    assert code == 2
    assert seen == [("worker failed", False)]
    assert _exit_code(paths) == "2"
    assert not (paths.logs / "job-1.exit.tmp").exists()
