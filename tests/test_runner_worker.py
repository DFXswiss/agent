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
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import agent_cli.runner_worker as runner_worker
from agent_cli.finalize import transcript_has_marker
from agent_cli.ingest import job_row
from agent_cli.lane import LaneResult
from agent_cli.runner_cmd import runner_paths
from agent_cli.runtime import Completed
from agent_cli.store import Store
from driver_fixtures import write_drivers

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
REPO = "example-owner/example-repository"
DIFF = "diff --git a/x b/x\n+new\n"
REVIEW_URL = (
    "https://github.com/example-owner/example-repository/pull/7"
    "#pullrequestreview-77"
)
TEMPLATE = "Review {{REPO}} {{NUMBER}} {{BASE}} {{HEAD}} {{JOB_ID}}.\n"
COMMENT_ID = 555
TRIGGER = f"comment:{REPO}:7:{COMMENT_ID}"
ISSUE_URL = f"https://github.com/{REPO}/issues/7"
COMMENT_URL = f"{ISSUE_URL}#issuecomment-{COMMENT_ID}"
ANALYSIS_TEMPLATE = "Explain {{REPO}} {{NUMBER}} {{HEAD}} {{JOB_ID}}.\n"
ANALYSIS_STDOUT = "Likely cause.\nSTATUS: complete\nRESULT: done\n"
# A sentinel so a test can ask for a raw string answer (including `"null"`)
# without looking like "use the default shape".
_DEFAULT = object()


class _FakeAccount:
    def __init__(
        self,
        login: str = "example-bot",
        name: str = "test",
        runner_error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.login = login
        self.runner_error = runner_error
        # The executor reads these when it redacts a failed row's error.
        self.gh_config_dir = "/test/gh"
        self.git_identity: dict[str, str] | None = None

    def runner(self, runner: Any) -> Any:
        if self.runner_error is not None:
            raise self.runner_error
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
    analysis_template: str = ANALYSIS_TEMPLATE,
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
            "models": {"first": "driver-a", "second": "driver-b"},
            "lane_execution": {"mode": "this-device-is-lane-host"},
        }
        write_drivers(tmp_path)
        (tmp_path / "ai-accounts.json").write_text(json.dumps(ai), encoding="utf-8")
    (paths.prompts / "pr-review.md").write_text(template, encoding="utf-8")
    (paths.prompts / "log-error.md").write_text(analysis_template, encoding="utf-8")
    return paths


def _install_account(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login: str = "example-bot",
    present: bool = True,
    runner_error: BaseException | None = None,
) -> None:
    accounts = (
        {"s": _FakeAccount(login, runner_error=runner_error)} if present else {}
    )
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
    comment: Any = _DEFAULT,
    comment_rc: int = 0,
    comment_error: BaseException | None = None,
    issue: Any = _DEFAULT,
    issue_rc: int = 0,
    issue_error: BaseException | None = None,
    stderr: str = "",
) -> Any:
    rows = calls if calls is not None else []

    def _answer(value: Any, default: dict[str, Any]) -> str:
        if value is _DEFAULT:
            value = default
        if isinstance(value, dict) and value is not default:
            # A test changes one field; the rest keeps a readable shape.
            merged = dict(default)
            merged.update(value)
            value = merged
        return value if isinstance(value, str) else json.dumps(value)

    default_comment = {
        "body": "Reported failure.",
        "user": {"login": "alice"},
        "html_url": COMMENT_URL,
        "issue_url": ISSUE_URL,
    }
    default_issue = {"title": "Broken thing", "body": "Steps to reproduce."}

    def run(argv: list[str]) -> Completed:
        rows.append(list(argv))
        if argv[:2] == ["git", "-C"] and argv[-1] == "HEAD^{commit}":
            return Completed(head_rc, f"{head}\n", stderr)
        if argv[:2] == ["gh", "api"] and argv[-1] == ".base.ref":
            return Completed(base_rc, f"{base}\n", stderr)
        if argv[:2] == ["gh", "api"] and "/issues/comments/" in argv[2]:
            if comment_error is not None:
                raise comment_error
            return Completed(comment_rc, _answer(comment, default_comment), stderr)
        if argv[:2] == ["gh", "api"] and "/issues/" in argv[2]:
            if issue_error is not None:
                raise issue_error
            return Completed(issue_rc, _answer(issue, default_issue), stderr)
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


def _mark_done(
    store: Store, activity_id: str, *, activity_type: str = "review.post"
) -> str:
    row = store.row("activity", activity_id)
    assert isinstance(row, dict)
    saved = {key: value for key, value in row.items() if not key.startswith("_")}
    saved["execution_status"] = "done"
    saved["result"] = {"url": REVIEW_URL}
    store.write("activity", "update", activity_id, saved)
    return f"{activity_type} {activity_id} done"


def _done_executor(
    posted: list[str] | None = None, *, seen_exit: list[bool] | None = None,
    paths: Any = None, job_id: str = "job-1",
    activity_type: str = "review.post",
) -> Any:
    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        if posted is not None:
            posted.append(activity_id)
        if seen_exit is not None and paths is not None:
            seen_exit.append((paths.logs / f"{job_id}.exit").exists())
        return _mark_done(store, activity_id, activity_type=activity_type)

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


def _insert_analysis_job(
    tmp_path: Path,
    paths: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_id: str = "job-1",
    trigger: Any = TRIGGER,
    **overrides: Any,
) -> dict[str, Any]:
    """Insert a running log-error row with a usable trigger by default."""
    overrides.setdefault("job_type", "log-error")
    overrides.setdefault("trigger", trigger)
    return _insert_job(
        tmp_path, paths, monkeypatch, job_id=job_id, **overrides
    )


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


def _recording_reader(runner: Any) -> Any:
    """A bounded reader that delegates to the fake runner and records its argv.

    The diff argv therefore stays in the same `calls` list the other fakes use,
    and the fake's stdout becomes the bytes the reader returns.
    """

    def read(argv: list[str], limit: int) -> tuple[int, bytes, bool]:
        completed = runner(argv)
        data = (completed.stdout or "").encode("utf-8", "replace")
        if len(data) > limit:
            return completed.returncode, data[:limit], True
        return completed.returncode, data, False

    return read


def _run(
    paths: Any,
    tmp_path: Path,
    *,
    job_id: str = "job-1",
    cwd: str | None = None,
    runner: Any = None,
    launch: Any = None,
    execute: Any = None,
    read_diff: Any = None,
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
    fake_runner = runner or _fake_runner()
    try:
        code = runner_worker.run_worker(
            paths,
            store,
            cwd if cwd is not None else str(paths.work / job_id),
            home=tmp_path,
            runner=fake_runner,
            launch=launch or _fake_launch([]),
            execute_activity=execute or _done_executor(),
            read_diff=read_diff or _recording_reader(fake_runner),
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The inherited PATH may hold relative and empty entries; the wrapper keeps
    # only the absolute ones and names `env` by its absolute path.
    real_env_dir = os.path.dirname(shutil.which("env") or "/usr/bin/env")
    monkeypatch.setenv(
        "PATH",
        f".:{tmp_path}:{real_env_dir}:{os.path.dirname(sys.executable)}::{tmp_path}",
    )
    wrapper = runner_worker.worker_wrapper(tmp_path)

    path = runner_worker._absolute_path_entries(os.environ["PATH"])
    env = shutil.which("env", path=path)
    assert env is not None and os.path.isabs(env)
    trusted = Path(runner_worker.__file__).resolve().parent.parent
    assert shlex.split(wrapper) == [
        env,
        "-u",
        "PYTHONHOME",
        "-u",
        "PYTHONUSERBASE",
        f"PATH={path}",
        f"AGENT_HOME={tmp_path}",
        f"PYTHONPATH={trusted}",
        sys.executable,
        "-P",
        "-m",
        "agent_cli.main",
        "runner",
        "work",
    ]


def test_absolute_path_entries_keeps_only_absolute_entries_once() -> None:
    # An empty, `.` or relative entry names the checkout the worker runs in;
    # a duplicate absolute entry adds nothing.
    assert (
        runner_worker._absolute_path_entries(".:/usr/bin::bin:/bin:/usr/bin")
        == "/usr/bin:/bin"
    )


def test_worker_wrapper_refuses_an_agent_home_that_is_relative() -> None:
    # The dispatching command and the job run in different directories; a
    # relative home would name two places.
    with pytest.raises(ValueError, match="absolute"):
        runner_worker.worker_wrapper("home")


def test_worker_wrapper_refuses_a_path_with_no_absolute_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With nothing absolute, every name would resolve against the checkout.
    monkeypatch.setenv("PATH", ".:bin::")
    with pytest.raises(ValueError, match="absolute PATH"):
        runner_worker.worker_wrapper(tmp_path)


def test_worker_wrapper_resolves_nothing_against_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real subprocess, run the way tmux runs the wrapper: the shell resolves
    # the wrapper's first word, then `env` applies the absolute PATH and drops
    # the inherited PYTHONHOME/PYTHONUSERBASE. Executables committed at the
    # checkout root must never be the ones that run.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = tmp_path / "shadow-ran"
    for name in ("env", "git"):
        script = checkout / name
        script.write_text(
            f"#!/bin/sh\nprintf x >> {shlex.quote(str(marker))}\n", encoding="utf-8"
        )
        script.chmod(0o755)
    real_env = shutil.which("env")
    assert real_env is not None
    directories = f".:{os.path.dirname(real_env)}:{os.path.dirname(sys.executable)}"
    monkeypatch.setenv("PATH", directories)

    home = tmp_path / "home"
    home.mkdir()
    argv = shlex.split(runner_worker.worker_wrapper(home))
    index = argv.index("-m")
    probe = argv[:index] + [
        "-c",
        "import os, subprocess; "
        "subprocess.run(['git', '--version'], check=False); "
        "print(os.environ.get('PYTHONHOME'), os.environ.get('PYTHONUSERBASE'))",
    ]

    completed = subprocess.run(
        ["/bin/sh", "-c", shlex.join(probe)],
        cwd=str(checkout),
        env={
            **os.environ,
            "PATH": directories,
            "PYTHONHOME": "relative",
            "PYTHONUSERBASE": "relative",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    # `git --version` inherits the probe's stdout; the printed values are last.
    assert completed.stdout.strip().splitlines()[-1] == "None None"
    assert not marker.exists()


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
    job = _insert_job(tmp_path, paths, monkeypatch)
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
    assert launch["vendor"] == "driver-a"
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
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"runner-review:job-1:{job['created_at']}:{job['attempts']}:{HEAD}",
        )
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
        f"Review at `{HEAD[:12]}` (driver-a, m): approved\n\n"
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


def test_worker_writes_to_the_standard_output_buffer_without_an_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tmux pane is the worker's default mirror: when no `out` is passed,
    # every progress line goes to `sys.stdout.buffer`.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    buffer = io.BytesIO()

    class _Stdout:
        def __init__(self, buffer: io.BytesIO) -> None:
            self.buffer = buffer

    monkeypatch.setattr(runner_worker.sys, "stdout", _Stdout(buffer))
    fake_runner = _fake_runner()
    store = Store(tmp_path)
    try:
        code = runner_worker.run_worker(
            paths,
            store,
            str(paths.work / "job-1"),
            home=tmp_path,
            runner=fake_runner,
            launch=_fake_launch([]),
            execute_activity=_done_executor(),
            read_diff=_recording_reader(fake_runner),
        )
    finally:
        store.close()

    assert code == 0
    assert _exit_code(paths) == "0"
    assert "worker: review posted" in buffer.getvalue().decode("utf-8")


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
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    second = runner_worker._review_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    retry = runner_worker._review_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=1,
        head=HEAD,
    )
    other_generation = runner_worker._review_activity_id(
        job_id="job-1",
        created_at="2026-01-02T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    other_head = runner_worker._review_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=OTHER_HEAD,
    )
    assert first == second
    assert first != retry
    assert first != other_generation
    assert first != other_head


def test_analysis_activity_id_separates_attempts_generations_and_heads() -> None:
    first = runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    assert first == runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    assert first == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"runner-log-error:job-1:2026-01-01T00:00:00Z:0:{HEAD}",
        )
    )
    assert first != runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=1,
        head=HEAD,
    )
    assert first != runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at="2026-01-02T00:00:00Z",
        attempts=0,
        head=HEAD,
    )
    assert first != runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=OTHER_HEAD,
    )
    # The namespaces keep a review and an analysis of the same attempt apart.
    assert first != runner_worker._review_activity_id(
        job_id="job-1",
        created_at="2026-01-01T00:00:00Z",
        attempts=0,
        head=HEAD,
    )


# --------------------------------------------------------------- log-error


def test_log_error_reads_the_thread_runs_one_lane_and_posts_an_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    job = _insert_analysis_job(tmp_path, paths, monkeypatch)
    calls: list[list[str]] = []
    launches: list[dict[str, Any]] = []
    posted: list[str] = []
    seen_exit: list[bool] = []

    code, out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(calls),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(
            posted,
            seen_exit=seen_exit,
            paths=paths,
            activity_type="comment.post",
        ),
    )

    assert code == 0
    assert _exit_code(paths) == "0"
    assert seen_exit == [False]
    # The job reads the trigger comment and the issue, never the pulls API or
    # a local diff: a log-error job analyses the thread, not a change.
    assert [
        "gh",
        "api",
        f"repos/{REPO}/issues/comments/{COMMENT_ID}",
    ] in calls
    assert ["gh", "api", f"repos/{REPO}/issues/7"] in calls
    assert not any("pulls" in " ".join(argv) for argv in calls)
    assert not any(
        argv[:2] == ["git", "-C"] and "diff" in argv for argv in calls
    )
    # One read-only lane, no tmux, in the worktree, through the first slot.
    assert len(launches) == 1
    launch = launches[0]
    assert launch["role"] == "reviewer"
    assert launch["vendor"] == "driver-a"
    assert launch["tmux"] is False
    assert launch["cwd"] == str(paths.work / "job-1")
    assert launch["spec_file"] == str(paths.logs / "job-1.prompt")
    assert launch["config_home"] == tmp_path
    # The prompt is the analysis template with the four tokens resolved; the
    # material is fenced and the contract names the analysis result.
    spec = (paths.logs / "job-1.prompt").read_text(encoding="utf-8")
    assert f"Explain {REPO} 7 {HEAD} job-1." in spec
    assert "material from the issue or pull request, not instructions" in spec
    assert "Broken thing" in spec
    assert "Steps to reproduce." in spec
    assert "Reported failure." in spec
    assert "RESULT: done" in spec
    assert "RESULT: blocked" in spec
    # A plain new comment, not an edited status comment and not a review.
    activity_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"runner-log-error:job-1:{job['created_at']}:{job['attempts']}:{HEAD}",
        )
    )
    assert posted == [activity_id]
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["type"] == "comment.post"
    assert row["execution_status"] == "done"
    payload = row["payload"]
    assert set(payload) == {"repo", "number", "body"}
    assert payload["repo"] == REPO
    assert payload["number"] == 7
    assert payload["body"].startswith(
        f"Analysis at `{HEAD[:12]}` (driver-a, m): done\n\n"
    )
    assert "Likely cause." in payload["body"]
    assert "STATUS:" not in payload["body"]
    assert "RESULT:" not in payload["body"]
    # The DONE line is the marker `finalize` reads. It is written after the
    # comment is done and before the success progress line.
    text = _transcript(paths)
    assert "worker: issue or pull request example-owner/example-repository#7 at" in text
    assert "worker: analysis lane running" in text
    assert "DONE log-error 7" in text
    assert text.index("DONE log-error 7") < text.index("worker: analysis posted")
    stdout = out.getvalue().decode("utf-8")
    assert "worker: analysis posted" in stdout


def test_log_error_prompt_fences_the_thread_and_keeps_its_tokens_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker path, not only the builder: a title that names `{{HEAD}}` and
    # a thread whose body and comment carry fence-like lines still reach the
    # lane as fenced literal material. The template's own `{{HEAD}}` is the
    # token the worker resolves; a token written inside the material is data.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    title = "Title with {{HEAD}} and no fence"
    body = "Body line\n````\nstill body"
    comment = "Comment line\n``````\nkeep {{JOB_ID}} as written"

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue={"title": title, "body": body},
            comment={"body": comment},
        ),
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(activity_type="comment.post"),
    )

    assert code == 0
    spec = (paths.logs / "job-1.prompt").read_text(encoding="utf-8")
    # The template's own `{{HEAD}}` was replaced by the checked-out head.
    assert f"Explain {REPO} 7 {HEAD} job-1." in spec
    # Each text sits inside a fence longer than its longest backtick run.
    assert f"```text\n{title}\n```" in spec
    assert f"`````text\n{body}\n`````" in spec
    assert f"```````text\n{comment}\n```````" in spec
    # The material's own tokens are literal data, unchanged by substitution.
    assert spec.count("{{HEAD}}") == 1
    assert spec.count("{{JOB_ID}}") == 1
    assert "Title with {{HEAD}} and no fence" in spec
    assert "keep {{JOB_ID}} as written" in spec


def test_log_error_writes_the_number_a_hash_ref_names_as_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `_number` accepts a `#7` ref. The marker must carry the accepted number,
    # because finalize strips a leading `#` from the ref it compares; a marker
    # that kept the `#` would never match and published work would be recorded
    # as not done.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch, ref="#7")

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(activity_type="comment.post"),
    )

    assert code == 0
    text = _transcript(paths)
    assert "DONE log-error 7" in text
    assert "DONE log-error #7" not in text


def test_log_error_marker_for_a_hash_ref_matches_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The line the worker wrote is handed to finalize's marker reader, which
    # compares `#7` by stripping the `#`. This is the join that makes a
    # successful log-error record work_performed="yes".
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch, ref="#7")

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(activity_type="comment.post"),
    )

    assert code == 0
    line = next(
        line
        for line in _transcript(paths).splitlines()
        if line.startswith("DONE log-error")
    )
    assert line == "DONE log-error 7"
    assert transcript_has_marker(line, "#7") is True


def test_log_error_posts_an_analysis_on_a_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A trigger comment on a pull request is served by the issues endpoint,
    # whose answer carries the `pull_request` key GitHub sends for one. The
    # analysis is posted on that pull-request number exactly like on an issue.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(
        tmp_path, paths, monkeypatch, pull_request=True, ref="7"
    )
    posted: list[str] = []

    code, out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue={
                "title": "Broken thing",
                "body": "Steps to reproduce.",
                "pull_request": {
                    "url": f"https://api.github.com/repos/{REPO}/pulls/7",
                    "html_url": f"https://github.com/{REPO}/pull/7",
                },
            }
        ),
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 0
    assert _exit_code(paths) == "0"
    assert len(posted) == 1
    store = Store(tmp_path)
    try:
        row = store.row("activity", posted[0])
    finally:
        store.close()
    assert row is not None
    assert row["type"] == "comment.post"
    assert row["execution_status"] == "done"
    assert row["payload"]["number"] == 7
    text = _transcript(paths)
    assert "DONE log-error 7" in text
    assert text.index("DONE log-error 7") < text.index("worker: analysis posted")
    assert "worker: analysis posted" in out.getvalue().decode("utf-8")


def test_log_error_accepts_the_job_type_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch, job_type="Log-Error")
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 0
    assert len(launches) == 1


def test_log_error_posts_a_blocked_analysis_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `RESULT: blocked` is a complete analysis that names what is missing, so
    # it is posted exactly like `RESULT: done`.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [],
            protocol_result="blocked",
            stdout="Need the exact version.\nSTATUS: complete\nRESULT: blocked\n",
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 0
    store = Store(tmp_path)
    try:
        row = store.row("activity", posted[0])
    finally:
        store.close()
    assert row is not None
    assert row["payload"]["body"].startswith(
        f"Analysis at `{HEAD[:12]}` (driver-a, m): blocked\n\n"
    )
    assert row["payload"]["body"].endswith("Need the exact version.")


@pytest.mark.parametrize("protocol_result", ["approved", "rejected", "", "ask"])
def test_log_error_requires_done_or_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol_result: str
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result=protocol_result, stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert posted == []
    assert "analysis lane ended complete" in _transcript(paths)
    assert "DONE log-error" not in _transcript(paths)


@pytest.mark.parametrize("status", ["partial", "timeout", "", "unavailable"])
def test_log_error_requires_a_complete_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], status=status, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert posted == []
    expected = status or "unavailable"
    assert f"analysis lane ended {expected}" in _transcript(paths)
    assert "DONE log-error" not in _transcript(paths)


def test_log_error_reports_a_blank_lane_start_detail_without_a_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `SystemExit` whose message is only whitespace carries no safe
    # configuration detail, so the failure line is the plain message.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches, error=SystemExit("   ")),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert _exit_code(paths) == "2"
    assert len(launches) == 1
    assert posted == []
    text = _transcript(paths)
    assert "analysis lane could not start" in text.splitlines()
    assert "analysis lane could not start: " not in text
    assert "DONE log-error" not in text


def test_log_error_fails_closed_without_a_usable_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch, trigger=None)
    launches: list[dict[str, Any]] = []
    calls: list[list[str]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(calls),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 2
    assert runner_worker.FAILED_TRIGGER in _transcript(paths)
    assert launches == []
    assert calls == []
    assert not (paths.logs / "job-1.prompt").exists()


@pytest.mark.parametrize(
    "trigger",
    [None, "", "comment:example-owner/example-repository:7:abc", 7],
)
def test_log_error_rejects_every_unusable_trigger_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: Any,
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch, trigger=trigger)
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert runner_worker.FAILED_TRIGGER in _transcript(paths)
    assert launches == []


@pytest.mark.parametrize(
    ("comment", "comment_rc", "comment_error"),
    [
        ({"body": 5}, 0, None),
        ({"user": "alice"}, 0, None),
        ({"user": {"login": 5}}, 0, None),
        ({"user": {"login": ""}}, 0, None),
        ({"body": None}, 0, None),
        ({"html_url": 5}, 0, None),
        ({"html_url": ""}, 0, None),
        ({"issue_url": 5}, 0, None),
        ("not json", 0, None),
        ([], 0, None),
        (_DEFAULT, 1, None),
        (_DEFAULT, 0, OSError("gh is gone")),
    ],
)
def test_log_error_rejects_an_unreadable_triggering_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comment: Any,
    comment_rc: int,
    comment_error: BaseException | None,
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            comment=comment,
            comment_rc=comment_rc,
            comment_error=comment_error,
            stderr="SECRET-COMMENT-STDERR",
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert launches == []
    assert posted == []
    text = _transcript(paths)
    assert runner_worker.FAILED_COMMENT in text
    assert "SECRET-COMMENT-STDERR" not in text
    assert "DONE log-error" not in text


def test_log_error_rejects_a_comment_from_another_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The row's trigger names a comment id, but the comment itself belongs to
    # another issue. Its text is not this job's request.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            comment={"issue_url": f"https://github.com/{REPO}/issues/8"}
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_FOREIGN_COMMENT in _transcript(paths)


def test_log_error_rejects_a_triggering_comment_over_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            comment={"body": "x" * (runner_worker.MAX_COMMENT_CHARS + 1)}
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_COMMENT_TOO_LONG in _transcript(paths)
    assert not (paths.logs / "job-1.prompt").exists()


@pytest.mark.parametrize(
    ("issue", "issue_rc", "issue_error"),
    [
        ({"title": 5}, 0, None),
        ({"body": 5}, 0, None),
        ("not json", 0, None),
        ([], 0, None),
        (_DEFAULT, 1, None),
        (_DEFAULT, 0, OSError("gh is gone")),
    ],
)
def test_log_error_rejects_an_unreadable_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Any,
    issue_rc: int,
    issue_error: BaseException | None,
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue=issue,
            issue_rc=issue_rc,
            issue_error=issue_error,
            stderr="SECRET-ISSUE-STDERR",
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert launches == []
    assert posted == []
    text = _transcript(paths)
    assert runner_worker.FAILED_ISSUE in text
    assert "SECRET-ISSUE-STDERR" not in text


def test_log_error_rejects_an_issue_title_over_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue={"title": "t" * (runner_worker.MAX_TITLE_CHARS + 1)}
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_TITLE_TOO_LONG in _transcript(paths)


def test_log_error_rejects_an_issue_body_over_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue={"body": "b" * (runner_worker.MAX_BODY_CHARS + 1)}
        ),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_BODY_TOO_LONG in _transcript(paths)
    assert not (paths.logs / "job-1.prompt").exists()


def test_log_error_treats_a_null_issue_body_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub sends null for a description that was never written. That is an
    # empty body, not an unreadable answer.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(
            issue={"title": "Broken thing", "body": None},
            comment={"body": "Still broken."},
        ),
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 0
    spec = (paths.logs / "job-1.prompt").read_text(encoding="utf-8")
    assert "```text\n\n```" in spec
    assert "Still broken." in spec


def test_log_error_rejects_an_issue_answer_without_a_body_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dict fixture would merge the default body in, so the raw answer is
    # built as JSON here. A missing body key is not GitHub's explicit null and
    # must not be accepted as an empty description.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(issue=json.dumps({"title": "Broken thing"})),
        launch=_fake_launch(
            launches, protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert runner_worker.FAILED_ISSUE in _transcript(paths)
    assert launches == []
    assert posted == []


def test_log_error_fails_closed_without_its_prompt_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    (paths.prompts / "log-error.md").unlink()
    launches: list[dict[str, Any]] = []

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert "prompt template" in _transcript(paths)
    assert "log-error.md" in _transcript(paths)
    assert "is not usable" in _transcript(paths)


def test_log_error_fails_closed_when_the_analysis_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [],
            protocol_result="done",
            stdout="STATUS: complete\nRESULT: done\n",
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert posted == []
    assert runner_worker.FAILED_ANALYSIS_EMPTY in _transcript(paths)
    assert "DONE log-error" not in _transcript(paths)


def test_log_error_rejects_an_oversized_analysis_instead_of_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [],
            protocol_result="done",
            stdout="x" * (runner_worker.MAX_REVIEW_CHARS + 1)
            + "\nSTATUS: complete\nRESULT: done\n",
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert posted == []
    assert runner_worker.FAILED_ANALYSIS_TOO_LONG in _transcript(paths)
    assert "DONE log-error" not in _transcript(paths)


def test_log_error_fails_when_the_comment_was_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=lambda _store, _runner, activity_id: (
            f"comment.post {activity_id} error"
        ),
    )

    assert code == 2
    assert runner_worker.FAILED_ANALYSIS_POST in _transcript(paths)
    # The marker is written only for published work: a failed post must never
    # make finalize record `work_performed=yes`.
    assert "DONE log-error" not in _transcript(paths)


def test_log_error_posts_without_an_executor_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not every executor reports a text line. The analysis post is verified
    # from the activity row, so a missing line adds nothing to the transcript
    # and does not turn a done comment into a failure.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)

    def execute(store: Store, _runner: Any, activity_id: str) -> None:
        _mark_done(store, activity_id, activity_type="comment.post")
        return None

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=execute,
    )

    assert code == 0
    assert _exit_code(paths) == "0"
    text = _transcript(paths)
    assert "worker: analysis posted" in text
    assert "DONE log-error 7" in text
    assert "comment.post job-1" not in text


def test_log_error_fails_when_the_activity_row_is_gone_at_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A done row is the proof the comment was posted. An executor that reports
    # a line but leaves no row behind has published nothing this worker can
    # verify, so the job fails and no marker is written.
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)

    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        store.write("activity", "delete", activity_id, {})
        return f"comment.post {activity_id} done"

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=execute,
    )

    assert code == 2
    assert _exit_code(paths) == "2"
    text = _transcript(paths)
    assert runner_worker.FAILED_ANALYSIS_POST in text
    assert "DONE log-error" not in text


def test_log_error_fails_closed_when_the_analysis_cannot_be_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_analysis_job(tmp_path, paths, monkeypatch)
    posted: list[str] = []

    def fail_queue(*_args: Any, **_kwargs: Any) -> bool:
        raise runner_worker.StoreError("store unavailable")

    monkeypatch.setattr(
        runner_worker.runner_status, "queue_activity", fail_queue
    )

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert posted == []
    assert runner_worker.FAILED_ANALYSIS_ACTIVITY in _transcript(paths)
    assert "DONE log-error" not in _transcript(paths)
    store = Store(tmp_path)
    try:
        assert store.rows("activity") == []
    finally:
        store.close()


def test_log_error_fails_closed_for_an_analysis_row_from_another_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One attempt derives one activity id. A row another device already wrote
    # under that id is not this worker's to re-open: the real store refuses it,
    # so the job fails without posting and leaves the foreign row untouched.
    paths = _write_worker_files(tmp_path)
    job = _insert_analysis_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._analysis_activity_id(
        job_id="job-1",
        created_at=job["created_at"],
        attempts=job["attempts"],
        head=HEAD,
    )
    foreign = {
        "id": activity_id,
        "session_id": "s",
        "type": "comment.post",
        "payload": {
            "repo": REPO,
            "number": 7,
            "body": "posted by another device",
        },
        "execution_status": "pending",
    }
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "activity",
                "op": "insert",
                "row_id": activity_id,
                "payload": foreign,
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        before = store.row("activity", activity_id)
    finally:
        store.close()
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 2
    assert _exit_code(paths) == "2"
    assert runner_worker.FAILED_ANALYSIS_ACTIVITY in _transcript(paths)
    assert posted == []
    assert "DONE log-error" not in _transcript(paths)
    store = Store(tmp_path)
    try:
        after = store.row("activity", activity_id)
    finally:
        store.close()
    assert after == before
    assert after is not None
    assert after["_origin_device_id"] == "other-device"
    assert after["payload"]["body"] == "posted by another device"


def test_log_error_does_not_post_an_analysis_that_is_already_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-running one attempt derives the same activity id. The comment stays
    # delivered, the executor is not asked again, and the marker is still
    # recorded for finalize.
    paths = _write_worker_files(tmp_path)
    job = _insert_analysis_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._analysis_activity_id(
        job_id="job-1", created_at=job["created_at"], attempts=0, head=HEAD
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
                "type": "comment.post",
                "payload": {
                    "repo": REPO,
                    "number": 7,
                    "body": "already posted",
                },
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    assert code == 0
    assert posted == []
    store = Store(tmp_path)
    try:
        row = store.row("activity", activity_id)
    finally:
        store.close()
    assert row is not None
    assert row["payload"]["body"] == "already posted"
    assert "DONE log-error 7" in _transcript(paths)


def test_log_error_posts_again_for_a_retry_with_a_new_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    job = _insert_analysis_job(tmp_path, paths, monkeypatch, attempts=1)
    first_id = runner_worker._analysis_activity_id(
        job_id="job-1", created_at=job["created_at"], attempts=0, head=HEAD
    )
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            first_id,
            {
                "id": first_id,
                "session_id": "s",
                "type": "comment.post",
                "payload": {
                    "repo": REPO,
                    "number": 7,
                    "body": "first attempt",
                },
                "execution_status": "done",
            },
        )
    finally:
        store.close()
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(
            [], protocol_result="done", stdout=ANALYSIS_STDOUT
        ),
        execute=_done_executor(posted, activity_type="comment.post"),
    )

    retry_id = runner_worker._analysis_activity_id(
        job_id="job-1", created_at=job["created_at"], attempts=1, head=HEAD
    )
    assert code == 0
    assert posted == [retry_id]
    assert retry_id != first_id
    store = Store(tmp_path)
    try:
        first = store.row("activity", first_id)
        retry = store.row("activity", retry_id)
    finally:
        store.close()
    assert first is not None and first["payload"]["body"] == "first attempt"
    assert retry is not None and retry["execution_status"] == "done"


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
        ({"created_at": ""}, runner_worker.FAILED_CREATED_AT),
        ({"created_at": None}, runner_worker.FAILED_CREATED_AT),
        ({"session_id": "bad id"}, runner_worker.FAILED_SESSION),
        ({"worktree": None}, runner_worker.FAILED_WORKTREE),
        ({"worktree": "/somewhere/else"}, runner_worker.FAILED_WORKTREE),
        ({"repo": ""}, runner_worker.FAILED_REPO),
        ({"ref": None}, runner_worker.FAILED_REF),
        ({"ref": ""}, runner_worker.FAILED_REF),
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


def test_worker_rejects_a_repository_that_is_not_owner_slash_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-empty value still has to name a repository. The slug is what the
    # worktree and cleanup use, so the worker refuses it before any git or gh
    # call rather than deriving a name that names something else.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch, repo="not-a-slug")
    calls: list[list[str]] = []
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        runner=_fake_runner(calls),
        launch=_fake_launch(launches),
    )

    assert code == 2
    assert launches == []
    assert calls == []
    assert runner_worker.FAILED_REPO in _transcript(paths)


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


def test_worker_fails_closed_when_the_prompt_template_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A template that is missing or not decodable is not a template the lane
    # may be started from. It fails closed instead of falling back to text.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    (paths.prompts / "pr-review.md").unlink()
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


def test_worker_fails_closed_when_the_account_cannot_build_a_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `Account.runner` reads the account's isolated git and gh setup. A store
    # failure there means the worker cannot act as the validated login, so it
    # stops before the lane instead of falling back to the ambient one.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    _install_account(
        monkeypatch, runner_error=runner_worker.StoreError("no account config")
    )
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


def test_worker_fails_closed_when_the_prompt_cannot_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prompt is replaced into place atomically. If that final step fails,
    # no lane is started from a half-written or missing file.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    real_replace = os.replace

    def fail_prompt_replace(src: Any, dst: Any) -> None:
        if str(dst).endswith(".prompt"):
            raise OSError("prompt directory is not writable")
        real_replace(src, dst)

    monkeypatch.setattr(runner_worker.os, "replace", fail_prompt_replace)

    code, _out = _run(paths, tmp_path, launch=_fake_launch(launches))

    assert code == 2
    assert launches == []
    assert not (paths.logs / "job-1.prompt").exists()
    assert runner_worker.FAILED_PROMPT_WRITE in _transcript(paths)


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


def test_worker_reports_a_blank_lane_start_detail_without_a_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `SystemExit` whose message is only whitespace carries no safe
    # configuration detail, so the failure line is the plain message.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches, error=SystemExit("   ")),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert _exit_code(paths) == "2"
    assert len(launches) == 1
    assert posted == []
    text = _transcript(paths)
    assert "review lane could not start" in text.splitlines()
    assert "review lane could not start: " not in text


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


def test_worker_rejects_a_diff_the_bounded_reader_had_to_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reader stops the producer at the byte cap; a truncated read is never
    # decoded, checked against the character cap and treated as a diff.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        read_diff=lambda _argv, _limit: (0, b"", True),
    )

    assert code == 2
    assert launches == []
    assert runner_worker.FAILED_DIFF_TOO_LARGE in _transcript(paths)
    assert not (paths.logs / "job-1.prompt").exists()


def test_worker_reports_a_timed_out_diff_as_a_failed_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []

    def timed_out(_argv: list[str], _limit: int) -> tuple[int, bytes, bool]:
        raise TimeoutError("diff timed out")

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        read_diff=timed_out,
    )

    assert code == 2
    assert launches == []
    assert "diff against develop failed" in _transcript(paths)
    assert not (paths.logs / "job-1.prompt").exists()


def test_read_bounded_stops_and_truncates_at_the_limit() -> None:
    returncode, data, truncated = runner_worker._read_bounded(
        [sys.executable, "-c", "print('x'*100)"], 10
    )

    # The child may have exited on its own before the kill; what matters is
    # that its whole output was never read.
    assert returncode is not None
    assert len(data) == 10
    assert truncated is True


def test_read_bounded_returns_the_whole_output_within_the_limit() -> None:
    returncode, data, truncated = runner_worker._read_bounded(
        [sys.executable, "-c", "print('x'*100)"], 1000
    )

    assert returncode == 0
    assert data == b"x" * 100 + b"\n"
    assert truncated is False


def test_read_bounded_gives_up_on_a_producer_that_stops_writing() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        runner_worker._read_bounded(
            [
                sys.executable,
                "-c",
                "import time; print('x', flush=True); time.sleep(30)",
            ],
            1000,
            timeout=0.5,
        )

    assert time.monotonic() - started < 10


def test_read_bounded_gives_up_when_stdout_closes_before_the_process_exits() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        runner_worker._read_bounded(
            [sys.executable, "-c", "import os, time; os.close(1); time.sleep(30)"],
            1000,
            timeout=0.5,
        )

    assert time.monotonic() - started < 10


def test_read_bounded_kills_the_child_when_a_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_read = os.read

    def recording_popen(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        stdout_fd = process.stdout.fileno()

        # Only the child's stdout is unreadable; Popen itself reads its own
        # exec-error pipe through the same module function.
        def failing_read(fd: int, size: int) -> bytes:
            if fd == stdout_fd:
                raise OSError("read failed")
            return real_read(fd, size)

        monkeypatch.setattr(runner_worker.os, "read", failing_read)
        return process

    monkeypatch.setattr(runner_worker.subprocess, "Popen", recording_popen)

    with pytest.raises(OSError):
        runner_worker._read_bounded(
            [
                sys.executable,
                "-c",
                "import time; print('x', flush=True); time.sleep(30)",
            ],
            1000,
            timeout=30,
        )

    assert len(processes) == 1
    assert processes[0].poll() is not None


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


def test_worker_fails_when_the_review_activity_row_is_gone_at_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A done row is the proof the review was posted. An executor that reports
    # a line but leaves no row behind has published nothing this worker can
    # verify, so the job fails and the success line is never written.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)

    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        store.write("activity", "delete", activity_id, {})
        return f"review.post {activity_id} done"

    code, out = _run(
        paths,
        tmp_path,
        launch=_fake_launch([]),
        execute=execute,
    )

    assert code == 2
    assert _exit_code(paths) == "2"
    assert runner_worker.FAILED_REVIEW_POST in _transcript(paths)
    assert "worker: review posted" not in out.getvalue().decode("utf-8")


def test_worker_records_an_unexpected_failure_without_copying_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unexpected exception from the body is a worker failure, not a crash.
    # Its text is never copied: it may hold provider diagnostics or a path.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    diagnostic = "SECRET-RUNTIME-DIAGNOSTIC"

    code, out = _run(
        paths,
        tmp_path,
        launch=_fake_launch([], error=RuntimeError(diagnostic)),
    )

    assert code == 2
    assert runner_worker.FAILED_WORKER == "worker failed"
    assert _exit_code(paths) == "2"
    text = _transcript(paths)
    assert runner_worker.FAILED_WORKER in text
    assert diagnostic not in text
    assert diagnostic not in out.getvalue().decode("utf-8")
    assert "DONE log-error" not in text


def test_worker_returns_two_when_it_cannot_write_the_success_exit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exit file is the worker's last effect. A success it cannot record is
    # reported by a non-zero return instead of a silent clean exit.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)

    def fail_write(_paths: Any, _job_id: str, _code: int) -> None:
        raise OSError("logs are not writable")

    monkeypatch.setattr(runner_worker, "_write_exit", fail_write)

    code, _out = _run(paths, tmp_path)

    assert code == 2
    assert not (paths.logs / "job-1.exit").exists()
    assert "worker: review posted" in _transcript(paths)


def test_worker_fails_closed_when_the_review_activity_cannot_be_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store failure while inserting or re-opening the activity is not a
    # failed post yet: it is reported as a queueing failure, and no executor
    # is asked to run an activity that does not exist.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    launches: list[dict[str, Any]] = []
    posted: list[str] = []

    def fail_queue(*_args: Any, **_kwargs: Any) -> bool:
        raise runner_worker.StoreError("store unavailable")

    monkeypatch.setattr(
        runner_worker.runner_status, "queue_activity", fail_queue
    )

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch(launches),
        execute=_done_executor(posted),
    )

    assert code == 2
    assert len(launches) == 1
    assert posted == []
    assert runner_worker.FAILED_ACTIVITY in _transcript(paths)
    store = Store(tmp_path)
    try:
        assert store.rows("activity") == []
    finally:
        store.close()


def test_worker_posts_without_an_executor_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not every executor reports a text line. The post is verified from the
    # activity row, so a missing line adds nothing to the transcript and does
    # not turn a done post into a failure.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)

    def execute(store: Store, _runner: Any, activity_id: str) -> None:
        _mark_done(store, activity_id)
        return None

    code, _out = _run(paths, tmp_path, execute=execute)

    assert code == 0
    assert _exit_code(paths) == "0"
    assert "review posted" in _transcript(paths)
    assert "review.post job-1" not in _transcript(paths)


def test_worker_pins_the_account_validated_before_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The account is checked against `expected_gh_user` before the lane. If the
    # session binding changes during the lane, the queued row must still carry
    # the validated account so the executor refuses the switch: the review is
    # not posted through the new login.
    from agent_cli import github_act

    paths = _write_worker_files(tmp_path)
    job = _insert_job(tmp_path, paths, monkeypatch)
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
        job_id="job-1", created_at=job["created_at"], attempts=0, head=HEAD
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
    job = _insert_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._review_activity_id(
        job_id="job-1", created_at=job["created_at"], attempts=0, head=HEAD
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
    job = _insert_job(tmp_path, paths, monkeypatch)
    activity_id = runner_worker._review_activity_id(
        job_id="job-1", created_at=job["created_at"], attempts=0, head=HEAD
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
    heartbeat = "worker: worker running"
    text = _transcript(paths)
    stdout = out.getvalue().decode("utf-8")
    # The beat is a fixed text of its own, so every occurrence is a heartbeat.
    # Every beat must reach both surfaces, not just the transcript.
    assert text.count(heartbeat) >= 2
    assert stdout.count(heartbeat) >= 2


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


class _CountingSink:
    """An output mirror that counts the heartbeat beats it receives."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.beats = 0
        self._buffer = io.BytesIO()

    def write(self, data: bytes | str) -> int:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        if text.strip() == "worker: worker running":
            with self._guard:
                self.beats += 1
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        return self._buffer.write(raw)

    def flush(self) -> None:
        self._buffer.flush()


def test_heartbeat_keeps_beating_while_the_review_is_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The review POST is a network wait that grows no transcript. The fake
    # executor holds until a beat written after it began, with a bounded wait
    # so a heartbeat that stopped after the lane fails the test, not hangs it.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    sink = _CountingSink()
    beats_at_post: list[int] = []

    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        start = sink.beats
        deadline = time.monotonic() + 5
        while sink.beats == start and time.monotonic() < deadline:
            time.sleep(0.005)
        beats_at_post.append(sink.beats - start)
        return _mark_done(store, activity_id)

    code, _out = _run(
        paths,
        tmp_path,
        execute=execute,
        heartbeat_seconds=0.01,
        out=sink,
    )

    assert code == 0
    assert beats_at_post and beats_at_post[0] >= 1


class _FailingSink:
    """A mirror whose every write raises, like a closed tmux pane."""

    def __init__(self) -> None:
        self.beats = 0

    def write(self, data: bytes | str) -> int:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        if text.strip() == "worker: worker running":
            self.beats += 1
        raise OSError("mirror is gone")

    def flush(self) -> None:
        raise OSError("mirror is gone")


def test_a_broken_output_mirror_does_not_stop_the_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the transcript feeds the watchdog, so a failing standard-output
    # mirror must not end the heartbeat thread and let the job look stalled.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    sink = _FailingSink()

    code, _out = _run(
        paths,
        tmp_path,
        launch=_fake_launch([], sleep=0.2),
        heartbeat_seconds=0.01,
        out=sink,
    )

    assert code == 0
    assert sink.beats >= 2


class _BlockingHeartbeatSink:
    """An output stream that blocks one heartbeat write until the test releases it.

    The block is armed only once the lane is running, so the heartbeat is not
    stopped in an earlier step while the worker still has to call `progress`.
    The first matching write after arming holds the shared progress lock.
    """

    def __init__(self, heartbeat_text: str) -> None:
        self._heartbeat_text = heartbeat_text
        self._seen = 0
        self._guard = threading.Lock()
        self._buffer = io.BytesIO()
        self.armed = threading.Event()
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes | str) -> int:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        block = False
        if text.strip() == self._heartbeat_text and self.armed.is_set():
            with self._guard:
                self._seen += 1
                block = self._seen == 1
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
    # The join before the exit file must be unconditional: a heartbeat write
    # still in progress may not leave the exit file behind it while the worker
    # can still write another transcript line.
    paths = _write_worker_files(tmp_path)
    _insert_job(tmp_path, paths, monkeypatch)
    sink = _BlockingHeartbeatSink("worker: worker running")
    exit_path = paths.logs / "job-1.exit"
    lane_running = threading.Event()
    finish_lane = threading.Event()
    outcome: list[int] = []
    failures: list[BaseException] = []

    def launch(**kwargs: Any) -> LaneResult:
        # Hold the lane open while a beat blocks, so the write belongs to a
        # worker that still has work to do rather than one already stopping.
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
        sink.armed.set()
        assert sink.blocked.wait(5), "the heartbeat never reached the output sink"
        finish_lane.set()
        # The lane is over, but the beat still holds the progress lock, so the
        # worker can only be waiting to finish.
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
        runner_worker._lane_start_message(SystemExit("vendor must be driver-a"))
        == "review lane could not start: vendor must be driver-a"
    )
    assert runner_worker._lane_start_message(
        SystemExit("vendor must be driver-a"), label="analysis"
    ) == "analysis lane could not start: vendor must be driver-a"
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


def test_build_analysis_spec_replaces_only_the_four_known_placeholders() -> None:
    # A log-error job has no base ref. `{{BASE}}` is not a token, so an
    # operator who writes it gets it back literally rather than a value the
    # job does not have.
    spec = runner_worker.build_analysis_spec(
        "{{REPO}}#{{NUMBER}} {{HEAD}} {{JOB_ID}} {{BASE}} {{UNKNOWN}}",
        repo=REPO,
        number=7,
        head=HEAD,
        job_id="job-1",
        title="t",
        body="b",
        comment="c",
    )

    instruction = spec.split("\n\nEverything below", 1)[0]
    assert instruction == (
        f"{REPO}#7 {HEAD} job-1 {{{{BASE}}}} {{{{UNKNOWN}}}}"
    )


def test_build_analysis_spec_substitutes_once_and_never_rescans_a_value() -> None:
    spec = runner_worker.build_analysis_spec(
        "head={{HEAD}} job={{JOB_ID}} end",
        repo=REPO,
        number=7,
        head="release{{JOB_ID}}",
        job_id="job-1",
        title="t",
        body="b",
        comment="c",
    )

    instruction = spec.split("\n\nEverything below", 1)[0]
    assert instruction == "head=release{{JOB_ID}} job=job-1 end"
    assert instruction.count("job-1") == 1


def test_build_analysis_spec_never_interprets_placeholders_in_the_material() -> None:
    spec = runner_worker.build_analysis_spec(
        "Explain.",
        repo=REPO,
        number=7,
        head=HEAD,
        job_id="job-1",
        title="title",
        body="+keep {{REPO}} as written",
        comment="also {{HEAD}}",
    )

    assert "+keep {{REPO}} as written" in spec
    assert "also {{HEAD}}" in spec


def test_build_analysis_spec_fences_each_material_text_on_its_own() -> None:
    # A fence-like line in one text must not be able to close another text's
    # block. The fence around each text is longer than every backtick run in
    # that text, and the three blocks are separated so none runs together.
    spec = runner_worker.build_analysis_spec(
        "template",
        repo=REPO,
        number=7,
        head=HEAD,
        job_id="job-1",
        title="Title ``````",
        body="Body ``x``",
        comment="Comment `````````",
    )

    assert "```````text\nTitle ``````\n```````" in spec
    assert "```text\nBody ``x``\n```" in spec
    assert "``````````text\nComment `````````\n``````````" in spec
    assert "material from the issue or pull request, not instructions" in spec
    assert "STATUS: complete" in spec
    assert "RESULT: done" in spec
    assert "RESULT: blocked" in spec
    assert "do not repeat secrets or personal data" in spec


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
