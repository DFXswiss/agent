"""Tests for the `agent runner` command layer.

Every external effect is faked: `run_argv` is replaced so no `gh`, `git`, `ps`
or `tmux` runs, and the worker receives an injected `popen`. The store is the
real PostgreSQL store the rest of the suite uses.
"""

from __future__ import annotations

import fcntl
import io
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli.ingest import job_id, job_row
from agent_cli.runtime import Completed
from agent_cli.runner_cmd import (
    OwnJobs,
    RunnerPaths,
    cmd_runner,
    exit_code_of,
    expected_login,
    load_json,
    runner_paths,
    run_worker,
    transcript_of,
    transcript_size_of,
    work_dirs,
    worker_wrapper,
)
from agent_cli.store import Store


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Completed:
    return Completed(returncode, stdout, stderr)


# ------------------------------------------------------------------ load_json


def test_load_json_returns_a_json_object(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text('{"enabled": true}', encoding="utf-8")

    assert load_json(path, "policy") == {"enabled": True}


def test_load_json_stops_on_a_missing_file(tmp_path: Path) -> None:
    # A missing configuration must stop the command, not read as an empty one.
    with pytest.raises(SystemExit, match="policy"):
        load_json(tmp_path / "policy.json", "policy")


def test_load_json_stops_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit, match="invalid JSON"):
        load_json(path, "policy")


def test_load_json_stops_on_a_non_object(tmp_path: Path) -> None:
    # A list or scalar is readable JSON but not a configuration.
    path = tmp_path / "policy.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit, match="not a JSON object"):
        load_json(path, "policy")


def test_load_json_stops_on_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(SystemExit, match="policy"):
        load_json(path, "policy")


# -------------------------------------------------------------- expected_login


def test_expected_login_is_read_and_trimmed() -> None:
    assert expected_login({"expected_gh_user": " example-bot "}) == "example-bot"


@pytest.mark.parametrize("policy", [{}, {"expected_gh_user": ""}, {"expected_gh_user": 7}])
def test_expected_login_stops_when_the_policy_does_not_name_one(
    policy: dict[str, Any],
) -> None:
    # Acting under whatever identity `gh` happens to carry is not this runner.
    with pytest.raises(SystemExit, match="expected_gh_user"):
        expected_login(policy)


# -------------------------------------------------------------------- ingest


def _ingest_runner(
    *,
    login: str,
    calls: list[list[str]],
    notifications: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> Callable[[list[str]], Completed]:
    def run(argv: list[str]) -> Completed:
        calls.append(list(argv))
        joined = " ".join(argv)
        if "api user" in joined:
            return _completed(0, f"{login}\n")
        if joined.endswith("--slurp notifications"):
            return _completed(0, json.dumps([notifications or []]))
        if "/comments" in joined:
            return _completed(0, json.dumps([comments or []]))
        if "--jq .private" in joined:
            return _completed(0, "false\n")
        raise AssertionError(argv)

    return run


def _write_policy(home: Path, **overrides: Any) -> None:
    policy: dict[str, Any] = {
        "expected_gh_user": "Example-Bot",
        "enabled": True,
        "actors_allow": ["alice"],
        "repos_allow": ["owner/name"],
        "job_types_allow": ["pr-review"],
        "default_skill_on_mention": "pr-review",
    }
    policy.update(overrides)
    runner_dir = home / "runner"
    runner_dir.mkdir(parents=True, exist_ok=True)
    (runner_dir / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


def test_ingest_creates_a_job_for_an_admitted_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_policy(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        _ingest_runner(
            # The check is case-insensitive; the policy says Example-Bot.
            login="example-bot",
            calls=calls,
            notifications=[
                {
                    "repository": {"full_name": "owner/name"},
                    "subject": {"url": "https://api.github.com/repos/owner/name/issues/7"},
                }
            ],
            comments=[
                {"body": "@example-bot pr-review please", "user": {"login": "alice"}}
            ],
        ),
    )

    cmd_runner(["ingest", "--session", "session-1"])

    expected = job_id("owner/name", "7", "pr-review")
    store = Store(home)
    try:
        row = store.row("job", expected)
        assert row is not None
        assert row["state"] == "queued"
        assert row["actor"] == "alice"
    finally:
        store.close()
    assert f"job.queued {expected}" in capsys.readouterr().out


def test_ingest_fails_closed_when_gh_is_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_policy(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        _ingest_runner(login="someone-else", calls=calls),
    )

    with pytest.raises(SystemExit, match="gh is authenticated as someone-else"):
        cmd_runner(["ingest", "--session", "session-1"])

    # The notifications call must not happen under the wrong identity.
    assert not any("notifications" in " ".join(argv) for argv in calls)


def test_ingest_passes_the_policy_to_scan_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The policy answers "who may ask"; handing the runner config over instead
    # would enforce the wrong rules without failing.
    home = tmp_path / "home"
    _write_policy(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    seen: dict[str, Any] = {}

    def scan_mentions(
        _store: Any,
        _runner: Callable[[list[str]], Completed],
        *,
        session_id: str,
        login: str,
        policy: Any,
    ) -> tuple[list[str], int]:
        seen.update(session_id=session_id, login=login, policy=policy)
        return [], 0

    monkeypatch.setattr("agent_cli.ingest.scan_mentions", scan_mentions)
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda _argv: _completed(0, "example-bot\n"),
    )

    cmd_runner(["ingest", "--session", "session-1"])

    expected = json.loads(
        (home / "runner" / "policy.json").read_text(encoding="utf-8")
    )
    assert seen["policy"] == expected
    assert seen["login"] == "Example-Bot"
    assert seen["session_id"] == "session-1"
    assert capsys.readouterr().out.strip() == "job.queued none"


# ---------------------------------------------------------------------- lock


def test_a_held_lock_makes_a_mutating_command_report_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    state = home / "runner" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))

    with (state / "runner.lock").open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        cmd_runner(["prune"])

    assert "runner busy" in capsys.readouterr().out


# ------------------------------------------------------------------- OwnJobs


def _job_row(job_type: str = "pr-review") -> dict[str, Any]:
    return job_row(
        session_id="s",
        repo="owner/name",
        ref="7",
        job_type=job_type,
        actor="alice",
    )


def test_own_jobs_hides_and_refuses_foreign_rows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = Store(home)
    try:
        own_row = _job_row()
        store.write("job", "insert", own_row["id"], own_row)
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "job",
                "op": "insert",
                "row_id": "foreign-job",
                "payload": {"id": "foreign-job", "state": "queued"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        own = OwnJobs(store)

        assert [row["id"] for row in own.rows("job")] == [own_row["id"]]
        assert own.row("job", "foreign-job") is None
        assert own.row("job", own_row["id"]) is not None

        extra = _job_row("merge-conflict")
        own.write("job", "insert", extra["id"], extra)
        assert store.row("job", extra["id"]) is not None
    finally:
        store.close()


def _apply_foreign_job(store: Store, payload: dict[str, Any], *, seq: int = 1) -> None:
    """Materialize a job row owned by another device."""
    store.apply_remote(
        {
            "origin_device_id": "other-device",
            "origin_seq": seq,
            "table": "job",
            "op": "insert",
            "row_id": payload["id"],
            "payload": payload,
            "occurred_at": "2026-08-13T12:00:00Z",
        }
    )


# ------------------------------------------------------------------- dispatch


_BASELINE = json.dumps(
    {
        "data": {
            "repository": {
                "issueOrPullRequest": {
                    "__typename": "PullRequest",
                    "comments": {"nodes": [{"id": "C_1"}]},
                    "reviews": {"nodes": []},
                }
            }
        }
    }
)


def _dispatch_runner(calls: list[list[str]]) -> Callable[[list[str]], Completed]:
    def run(argv: list[str]) -> Completed:
        calls.append(list(argv))
        joined = " ".join(argv)
        if "rev-parse --is-bare-repository" in joined:
            return _completed(1, "false")
        if "clone --bare" in joined:
            return _completed()
        if "config remote.origin.fetch" in joined:
            return _completed()
        if "fetch --prune" in joined:
            return _completed()
        if "worktree add" in joined:
            return _completed()
        if "graphql" in joined:
            return _completed(0, _BASELINE)
        if "new-session" in joined:
            return _completed()
        raise AssertionError(argv)

    return run


def test_dispatch_removes_stale_logs_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    runner_dir = home / "runner"
    (runner_dir / "state" / "logs").mkdir(parents=True)
    (runner_dir / "runner.json").write_text(
        json.dumps({"max_concurrent": 1}), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_HOME", str(home))
    row = _job_row()
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()

    # A stale exit code would be read by finalize as this attempt's result.
    stale_exit = runner_dir / "state" / "logs" / f"{row['id']}.exit"
    stale_transcript = runner_dir / "state" / "logs" / f"{row['id']}.transcript"
    stale_exit.write_text("0\n", encoding="utf-8")
    stale_transcript.write_text("old attempt\n", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr("agent_cli.runner_cmd.run_argv", _dispatch_runner(calls))
    monkeypatch.setattr(
        "agent_cli.runner_cmd.shutil.which", lambda _name: "/usr/bin/agent"
    )

    cmd_runner(["dispatch"])

    assert not stale_exit.exists()
    assert not stale_transcript.exists()
    store = Store(home)
    try:
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "running"
        assert saved["session"] == dispatch.session_name(row["id"])
    finally:
        store.close()
    assert f"job.started {row['id']}" in capsys.readouterr().out


def test_the_worker_wrapper_runs_this_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another program named `agent` may be earlier on PATH, so the worker must
    # start the interpreter that dispatched it, not a looked-up binary.
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_HOME", str(home))

    assert shlex.split(worker_wrapper()) == [
        "env",
        f"AGENT_HOME={home}",
        sys.executable,
        "-m",
        "agent_cli.main",
        "runner",
        "work",
    ]


def test_dispatch_only_starts_this_devices_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A queued row replicated from another device must never reach the runner.
    home = tmp_path / "home"
    _write_runner_config(home, {"max_concurrent": 2})
    monkeypatch.setenv("AGENT_HOME", str(home))
    own = _job_row()
    foreign = _job_row("pr-review")
    # Give the foreign row a distinct identity while keeping the same shape.
    foreign["id"] = "foreign-queued"
    foreign.update({"state": "queued", "attempts": 0})
    store = Store(home)
    try:
        store.write("job", "insert", own["id"], own)
        _apply_foreign_job(store, foreign)
    finally:
        store.close()

    calls: list[list[str]] = []
    monkeypatch.setattr("agent_cli.runner_cmd.run_argv", _dispatch_runner(calls))

    cmd_runner(["dispatch"])

    store = Store(home)
    try:
        saved_own = store.row("job", own["id"])
        assert saved_own is not None
        assert saved_own["state"] == "running"
        saved_foreign = store.row("job", foreign["id"])
        assert saved_foreign is not None
        assert saved_foreign["state"] == "queued"
    finally:
        store.close()
    assert not any(foreign["id"] in " ".join(argv) for argv in calls)
    assert f"job.started {own['id']}" in capsys.readouterr().out


# ---------------------------------------------------------------------- prune


def _write_runner_config(home: Path, config: dict[str, Any]) -> None:
    runner_dir = home / "runner"
    runner_dir.mkdir(parents=True, exist_ok=True)
    (runner_dir / "runner.json").write_text(json.dumps(config), encoding="utf-8")


def test_prune_removes_old_finished_jobs_and_their_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {"retention_days": {"done": 0}})
    logs = home / "runner" / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    row = _job_row()
    row.update({"state": "done", "finished": "2020-01-01T00:00:00Z"})
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()
    for suffix in (".exit", ".transcript", ".prompt"):
        (logs / f"{row['id']}{suffix}").write_text("x", encoding="utf-8")

    cmd_runner(["prune"])

    store = Store(home)
    try:
        assert store.row("job", row["id"]) is None
    finally:
        store.close()
    assert not any(logs.iterdir())
    out = capsys.readouterr().out
    assert f"job.pruned {row['id']}" in out
    # An absent retention setting must not be read as zero.
    assert "prune failed not configured" in out


def test_prune_does_not_delete_when_retention_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {})
    monkeypatch.setenv("AGENT_HOME", str(home))
    row = _job_row()
    row.update({"state": "done", "finished": "2020-01-01T00:00:00Z"})
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()

    cmd_runner(["prune"])

    store = Store(home)
    try:
        assert store.row("job", row["id"]) is not None
    finally:
        store.close()
    out = capsys.readouterr().out
    assert "prune done not configured" in out
    assert "prune failed not configured" in out


def test_prune_only_deletes_this_devices_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {"retention_days": {"done": 0}})
    monkeypatch.setenv("AGENT_HOME", str(home))
    own = _job_row()
    own.update({"state": "done", "finished": "2020-01-01T00:00:00Z"})
    foreign = _job_row()
    foreign["id"] = "foreign-done"
    foreign.update({"state": "done", "finished": "2020-01-01T00:00:00Z"})
    store = Store(home)
    try:
        store.write("job", "insert", own["id"], own)
        _apply_foreign_job(store, foreign)
    finally:
        store.close()

    cmd_runner(["prune"])

    store = Store(home)
    try:
        assert store.row("job", own["id"]) is None
        saved_foreign = store.row("job", foreign["id"])
        assert saved_foreign is not None
        assert saved_foreign["state"] == "done"
    finally:
        store.close()
    assert f"job.pruned {own['id']}" in capsys.readouterr().out


def test_prune_honours_the_retention_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 30 days means strictly older than 30 days is deleted; 29 days is kept.
    home = tmp_path / "home"
    _write_runner_config(home, {"retention_days": {"done": 30}})
    logs = home / "runner" / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    now = datetime.now(timezone.utc)

    def _finished(days: int) -> str:
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    old = _job_row()
    old["id"] = "old-job"
    old.update({"state": "done", "finished": _finished(31)})
    recent = _job_row()
    recent["id"] = "recent-job"
    recent.update({"state": "done", "finished": _finished(29)})
    store = Store(home)
    try:
        store.write("job", "insert", old["id"], old)
        store.write("job", "insert", recent["id"], recent)
    finally:
        store.close()
    for row in (old, recent):
        for suffix in (".exit", ".transcript", ".prompt"):
            (logs / f"{row['id']}{suffix}").write_text("x", encoding="utf-8")

    cmd_runner(["prune"])

    store = Store(home)
    try:
        assert store.row("job", old["id"]) is None
        assert store.row("job", recent["id"]) is not None
    finally:
        store.close()
    for suffix in (".exit", ".transcript", ".prompt"):
        assert not (logs / f"{old['id']}{suffix}").exists()
        assert (logs / f"{recent['id']}{suffix}").exists()
    assert f"job.pruned {old['id']}" in capsys.readouterr().out


# ---------------------------------------------------------------------- retry


def test_retry_requeues_and_removes_the_previous_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {"max_concurrent": 1})
    runner_dir = home / "runner"
    logs = runner_dir / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    row = _job_row()
    row.update({"state": "failed", "finished": "2020-01-01T00:00:00Z", "attempts": 0})
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()
    for suffix in (".exit", ".transcript", ".prompt"):
        (logs / f"{row['id']}{suffix}").write_text("x", encoding="utf-8")
    worktree = runner_dir / "state" / "work" / row["id"]
    worktree.mkdir(parents=True)
    (worktree / "file.txt").write_text("stale", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda argv: (calls.append(list(argv)), _completed())[1],
    )

    cmd_runner(["retry", row["id"]])

    store = Store(home)
    try:
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "queued"
        assert saved["attempts"] == 1
        assert saved["started"] is None
        assert saved["exit_code"] is None
    finally:
        store.close()
    assert not any(logs.iterdir())
    assert not worktree.exists()
    joined = [" ".join(argv) for argv in calls]
    assert any("worktree remove" in command for command in joined)
    assert any("worktree prune" in command for command in joined)
    assert f"job.retried {row['id']}" in capsys.readouterr().out


def test_retry_refuses_a_job_owned_by_another_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {"max_concurrent": 1})
    monkeypatch.setenv("AGENT_HOME", str(home))
    store = Store(home)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "job",
                "op": "insert",
                "row_id": "foreign-job",
                "payload": {"id": "foreign-job", "state": "failed"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="belongs to another device"):
        cmd_runner(["retry", "foreign-job"])


def test_retry_refuses_a_running_job_and_leaves_it_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `running` cannot transition to `queued`; rewriting the row would make the
    # supervisor's current attempt silently become a queue entry.
    home = tmp_path / "home"
    runner_dir = home / "runner"
    logs = runner_dir / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    row = _job_row()
    row.update({"state": "running", "session": "job-running", "attempts": 1})
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()
    for suffix in (".exit", ".transcript", ".prompt"):
        (logs / f"{row['id']}{suffix}").write_text("x", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda argv: (calls.append(list(argv)), _completed())[1],
    )

    with pytest.raises(SystemExit, match="cannot be retried from state running"):
        cmd_runner(["retry", row["id"]])

    store = Store(home)
    try:
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "running"
        assert saved["attempts"] == 1
        assert saved["session"] == "job-running"
    finally:
        store.close()
    assert calls == []
    for suffix in (".exit", ".transcript", ".prompt"):
        assert (logs / f"{row['id']}{suffix}").exists()


def test_retry_refuses_a_symlink_that_points_outside_the_work_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `shutil.rmtree` must never follow a symlink: the target is data the
    # runner does not own, and the row must stay exactly as it was.
    home = tmp_path / "home"
    runner_dir = home / "runner"
    work = runner_dir / "state" / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    row = _job_row()
    row.update({"state": "failed", "attempts": 0})
    link = work / row["id"]
    link.symlink_to(outside, target_is_directory=True)
    store = Store(home)
    try:
        store.write("job", "insert", row["id"], row)
    finally:
        store.close()
    # The fake git leaves the path in place, so the fallback delete is reached.
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv", lambda _argv: _completed()
    )

    with pytest.raises(SystemExit, match="symlink"):
        cmd_runner(["retry", row["id"]])

    store = Store(home)
    try:
        saved = store.row("job", row["id"])
        assert saved is not None
        assert saved["state"] == "failed"
        assert saved["attempts"] == 0
    finally:
        store.close()
    assert link.is_symlink()
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"


# --------------------------------------------------------------- exit_code_of


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("0\n", 0),
        (" 7 \n", 7),
        ("-1\n", -1),
        ("", None),
        ("abc", None),
        ("1.0", None),
        ("0x10", None),
    ],
)
def test_exit_code_of_accepts_only_a_complete_integer(
    tmp_path: Path, content: str, expected: int | None
) -> None:
    paths = runner_paths(tmp_path / "runner")
    paths.logs.mkdir(parents=True)
    (paths.logs / "j.exit").write_text(content, encoding="utf-8")

    assert exit_code_of(paths, "j") is expected


def test_exit_code_of_returns_none_when_the_file_is_missing(tmp_path: Path) -> None:
    assert exit_code_of(runner_paths(tmp_path / "runner"), "j") is None


# ------------------------------------------------------------- log helpers


def test_transcript_helpers_tolerate_missing_and_undecodable_files(
    tmp_path: Path,
) -> None:
    paths = runner_paths(tmp_path / "runner")
    paths.logs.mkdir(parents=True)

    assert transcript_of(paths, "j") == ""
    assert transcript_size_of(paths, "j") is None

    (paths.logs / "j.transcript").write_bytes(b"ok \xff")
    assert transcript_of(paths, "j").startswith("ok ")
    assert transcript_size_of(paths, "j") == 4


def test_work_dirs_lists_sorted_immediate_directories(tmp_path: Path) -> None:
    paths = runner_paths(tmp_path / "runner")
    paths.work.mkdir(parents=True)
    (paths.work / "b").mkdir()
    (paths.work / "a").mkdir()
    (paths.work / "file").write_text("x", encoding="utf-8")

    assert work_dirs(paths) == ["a", "b"]


def test_work_dirs_is_empty_when_the_work_root_is_missing(tmp_path: Path) -> None:
    # `iterdir()` raises lazily; a missing directory must still answer [].
    paths = runner_paths(tmp_path / "runner")

    assert work_dirs(paths) == []


def test_work_dirs_does_not_list_a_symlinked_directory(tmp_path: Path) -> None:
    # A symlink is not a worktree the runner owns; reaping it would name a job
    # whose real directory is somewhere else.
    paths = runner_paths(tmp_path / "runner")
    paths.work.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (paths.work / "real").mkdir()
    (paths.work / "linked").symlink_to(target, target_is_directory=True)

    assert work_dirs(paths) == ["real"]


# --------------------------------------------------------------------- worker


class _FakeStore:
    """The two reads the worker makes, without a database."""

    def __init__(
        self,
        row: dict[str, Any] | None,
        device: str = "this-device",
        origin: str | None = None,
    ) -> None:
        self._row = row
        self._device = device
        # By default the row belongs to this device; a test can claim it for
        # another one to exercise the ownership check.
        self._origin = device if origin is None else origin

    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        if table == "job" and self._row is not None and row_id == self._row["id"]:
            return {**self._row, "_origin_device_id": self._origin}
        return None

    def device_id(self) -> str:
        return self._device


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        raise AssertionError("read() buffers; the worker must use read1()")

    def read1(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _Process:
    def __init__(self, code: int, chunks: list[bytes]) -> None:
        self.stdout = _Stream(chunks)
        self._code = code

    def wait(self) -> int:
        return self._code


def _worker_home(tmp_path: Path, *, deny: Any = None) -> tuple[RunnerPaths, str]:
    paths = runner_paths(tmp_path / "home" / "runner")
    paths.prompts.mkdir(parents=True)
    (paths.prompts / "pr-review.md").write_text(
        "repo {{REPO}} ref {{REF}} job {{JOB_ID}}", encoding="utf-8"
    )
    skills: dict[str, Any] = {}
    if deny is not None:
        skills["pr-review"] = {"deny": deny}
    config = {
        "agent": {"command": "agent-cli", "args": ["run"], "prompt_flag": "--prompt"},
        "defaults": {"deny": ["Bash"]},
        "skills": skills,
    }
    paths.runner_config.write_text(json.dumps(config), encoding="utf-8")
    job_id_value = job_id("owner/name", "7", "pr-review")
    (paths.work / job_id_value).mkdir(parents=True)
    return paths, job_id_value


def _worker_store(job_id_value: str) -> _FakeStore:
    row = {
        "id": job_id_value,
        "repo": "owner/name",
        "ref": "7",
        "job_type": "pr-review",
    }
    return _FakeStore(row)


def test_worker_runs_the_agent_and_records_its_output(tmp_path: Path) -> None:
    paths, job_id_value = _worker_home(tmp_path, deny=["Bash", "Edit"])
    out = io.BytesIO()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    chunks = [b"hello ", b"world\n"]

    def popen(argv: list[str], **kwargs: Any) -> _Process:
        calls.append((argv, kwargs))
        return _Process(0, chunks)

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=popen,
        out=out,
    )

    assert code == 0
    prompt = paths.logs / f"{job_id_value}.prompt"
    assert calls[0][0] == [
        "agent-cli",
        "run",
        "--deny",
        "Bash",
        "--deny",
        "Edit",
        "--prompt",
        str(prompt),
    ]
    # The agent's streams are merged so one transcript holds everything.
    assert calls[0][1] == {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    # The substituted prompt must name this job, its repository and its ref.
    assert prompt.read_text(encoding="utf-8") == (
        f"repo owner/name ref 7 job {job_id_value}"
    )
    assert out.getvalue() == b"hello world\n"
    assert (paths.logs / f"{job_id_value}.transcript").read_bytes() == b"hello world\n"
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "0\n"


def test_worker_inherits_the_default_deny_list(tmp_path: Path) -> None:
    # A job type without its own deny list uses the runner's defaults.
    paths, job_id_value = _worker_home(tmp_path, deny=None)
    calls: list[list[str]] = []

    def popen(argv: list[str], **_kwargs: Any) -> _Process:
        calls.append(argv)
        return _Process(0, [])

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=popen,
    )

    assert code == 0
    assert calls[0][:4] == ["agent-cli", "run", "--deny", "Bash"]


def test_worker_uses_read1_to_stream_output(tmp_path: Path) -> None:
    # `read(65536)` would block until the buffer fills or EOF, so a quiet
    # agent's progress would not reach the transcript and the stall watchdog
    # could kill live work. Only `read1` returns what is available.
    paths, job_id_value = _worker_home(tmp_path)
    seen: list[str] = []

    class Stream:
        def __init__(self) -> None:
            self._chunks = [b"live\n"]

        def read(self, _size: int) -> bytes:
            raise AssertionError("read() must not be called")

        def read1(self, _size: int) -> bytes:
            seen.append("read1")
            return self._chunks.pop(0) if self._chunks else b""

    class Process:
        stdout = Stream()

        def wait(self) -> int:
            return 0

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: Process(),
    )

    assert code == 0
    assert seen
    assert (paths.logs / f"{job_id_value}.transcript").read_bytes() == b"live\n"


def test_worker_preserves_a_nonzero_agent_result(tmp_path: Path) -> None:
    paths, job_id_value = _worker_home(tmp_path)

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: _Process(3, [b"partial\n"]),
    )

    assert code == 3
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "3\n"


def test_worker_writes_exit_two_when_the_job_cannot_be_identified(
    tmp_path: Path,
) -> None:
    paths, job_id_value = _worker_home(tmp_path)

    code = run_worker(
        paths,
        _FakeStore(None),
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: pytest.fail("must not start the agent"),
    )

    assert code == 2
    assert "not in the store" in (
        paths.logs / f"{job_id_value}.transcript"
    ).read_text(encoding="utf-8")
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "2\n"


def test_worker_writes_exit_127_when_the_command_cannot_start(
    tmp_path: Path,
) -> None:
    paths, job_id_value = _worker_home(tmp_path)

    def popen(_argv: list[str], **_kwargs: Any) -> Any:
        raise FileNotFoundError("agent-cli")

    code = run_worker(
        paths, _worker_store(job_id_value), str(paths.work / job_id_value), popen=popen
    )

    assert code == 127
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "127\n"


def test_worker_refuses_a_directory_that_is_not_directly_under_the_work_root(
    tmp_path: Path,
) -> None:
    paths, job_id_value = _worker_home(tmp_path)
    nested = paths.work / job_id_value / "nested"
    nested.mkdir()

    for cwd in (str(paths.work), str(nested), str(tmp_path / "elsewhere")):
        code = run_worker(paths, _worker_store(job_id_value), cwd)
        assert code == 2

    # Nothing may be written when the worker cannot name its job.
    assert not paths.logs.exists()


def test_worker_writes_exit_two_when_the_prompt_template_is_missing(
    tmp_path: Path,
) -> None:
    paths, job_id_value = _worker_home(tmp_path)
    (paths.prompts / "pr-review.md").unlink()

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: pytest.fail("must not start the agent"),
    )

    assert code == 2
    assert "prompt template" in (
        paths.logs / f"{job_id_value}.transcript"
    ).read_text(encoding="utf-8")


def test_worker_writes_exit_two_when_the_prompt_template_is_not_utf8(
    tmp_path: Path,
) -> None:
    # A template that cannot be decoded must not start an agent with no prompt.
    paths, job_id_value = _worker_home(tmp_path)
    (paths.prompts / "pr-review.md").write_bytes(b"\xff\xfe\x00")

    code = run_worker(
        paths,
        _worker_store(job_id_value),
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: pytest.fail("must not start the agent"),
    )

    assert code == 2
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "2\n"


def test_work_records_a_failure_when_the_store_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure outside run_worker's own checks still has to end the attempt,
    # and the exception itself must keep propagating.
    paths, job_id_value = _worker_home(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(paths.work / job_id_value)

    def failing_open_store(**_kwargs: Any) -> Any:
        raise SystemExit("database unavailable")

    monkeypatch.setattr("agent_cli.main.open_store", failing_open_store)

    with pytest.raises(SystemExit, match="database unavailable"):
        cmd_runner(["work"])

    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "2\n"


def test_worker_refuses_a_row_owned_by_another_device(tmp_path: Path) -> None:
    # Another device's row is not this worker's job; starting the agent would
    # run work the store cannot attribute to this instance.
    paths, job_id_value = _worker_home(tmp_path)
    row = {
        "id": job_id_value,
        "repo": "owner/name",
        "ref": "7",
        "job_type": "pr-review",
    }
    store = _FakeStore(row, origin="other-device")

    code = run_worker(
        paths,
        store,
        str(paths.work / job_id_value),
        popen=lambda *_args, **_kwargs: pytest.fail("must not start the agent"),
    )

    assert code == 2
    assert "belongs to another device" in (
        paths.logs / f"{job_id_value}.transcript"
    ).read_text(encoding="utf-8")
    assert (paths.logs / f"{job_id_value}.exit").read_text(encoding="utf-8") == "2\n"


# ---------------------------------------------------------------- supervise


def test_supervise_exits_one_when_a_reap_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {})
    (home / "runner" / "policy.json").write_text(
        json.dumps({"expected_gh_user": "example-bot"}), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_HOME", str(home))

    def run(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if "api user" in joined:
            return _completed(0, "example-bot\n")
        if "list-sessions" in joined:
            return _completed(0, "job-orphan\n")
        if "kill-session" in joined:
            return _completed(1, "", "no such session")
        raise AssertionError(argv)

    monkeypatch.setattr("agent_cli.runner_cmd.run_argv", run)

    with pytest.raises(SystemExit) as raised:
        cmd_runner(["supervise"])

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert "job.finalized none" in captured.out
    # Reap failures are reported on standard error, like the other failures.
    assert "reap failed job-orphan" in captured.err
    assert "reap failed job-orphan" not in captured.out


def test_supervise_only_finalizes_this_devices_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_runner_config(home, {})
    (home / "runner" / "policy.json").write_text(
        json.dumps({"expected_gh_user": "example-bot"}), encoding="utf-8"
    )
    logs = home / "runner" / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    own = _job_row()
    own.update(
        {
            "state": "running",
            "session": dispatch.session_name(own["id"]),
            "baseline_output_ids": [],
            "started": "2026-08-13T12:00:00Z",
        }
    )
    foreign = _job_row()
    foreign["id"] = "foreign-running"
    foreign.update(
        {
            "state": "running",
            "session": dispatch.session_name(foreign["id"]),
            "baseline_output_ids": [],
            "started": "2026-08-13T12:00:00Z",
        }
    )
    store = Store(home)
    try:
        store.write("job", "insert", own["id"], own)
        _apply_foreign_job(store, foreign)
    finally:
        store.close()
    # Both have an exit file; only the own one may be looked at.
    (logs / f"{own['id']}.exit").write_text("0\n", encoding="utf-8")
    (logs / f"{foreign['id']}.exit").write_text("0\n", encoding="utf-8")

    def run(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if "api user" in joined:
            return _completed(0, "example-bot\n")
        if "list-sessions" in joined:
            return _completed(0, "")
        if "has-session" in joined:
            return _completed(1)
        if "graphql" in joined:
            return _completed(0, _BASELINE)
        if "kill-session" in joined:
            return _completed()
        if "worktree" in joined:
            return _completed()
        raise AssertionError(argv)

    monkeypatch.setattr("agent_cli.runner_cmd.run_argv", run)

    cmd_runner(["supervise"])

    store = Store(home)
    try:
        saved_own = store.row("job", own["id"])
        assert saved_own is not None
        assert saved_own["state"] == "failed"
        saved_foreign = store.row("job", foreign["id"])
        assert saved_foreign is not None
        assert saved_foreign["state"] == "running"
    finally:
        store.close()
    assert f"job.finalized {own['id']}" in capsys.readouterr().out


# ------------------------------------------------------------------- doctor


def _healthy_runner_files(home: Path) -> RunnerPaths:
    paths = runner_paths(home / "runner")
    paths.prompts.mkdir(parents=True)
    (paths.prompts / "pr-review.md").write_text("prompt", encoding="utf-8")
    paths.policy.write_text(
        json.dumps(
            {
                "expected_gh_user": "example-bot",
                "job_types_allow": ["pr-review"],
            }
        ),
        encoding="utf-8",
    )
    paths.runner_config.write_text(
        json.dumps(
            {
                "max_concurrent": 1,
                "clone_stall_minutes": 5,
                "defaults": {"timeout_minutes": 30, "stall_minutes": 5, "deny": ["Bash"]},
                "skills": {"pr-review": {"done_kind": "pr-reviewed"}},
                "agent": {
                    "command": "agent-cli",
                    "args": ["run"],
                    "prompt_flag": "--prompt",
                },
            }
        ),
        encoding="utf-8",
    )
    paths.skills_catalog.write_text(
        json.dumps({"skills": [{"id": "pr-review"}]}), encoding="utf-8"
    )
    paths.agent_config.write_text(
        json.dumps(
            {
                "cli": "agent",
                "session_kind": "runner",
                "skills": ["spine", "review-loop", "pr-review"],
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_doctor_reports_a_healthy_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _healthy_runner_files(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(
        "agent_cli.runner_cmd.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda _argv: _completed(0, "example-bot\n"),
    )

    cmd_runner(["doctor"])

    assert capsys.readouterr().out.strip() == "doctor ok"


def test_doctor_reports_every_problem_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    paths = _healthy_runner_files(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(
        "agent_cli.runner_cmd.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda _argv: _completed(0, "example-bot\n"),
    )
    # Two independent problems must both be reported, not just the first.
    config = json.loads(paths.runner_config.read_text(encoding="utf-8"))
    config["max_concurrent"] = 0
    paths.runner_config.write_text(json.dumps(config), encoding="utf-8")
    agent_config = json.loads(paths.agent_config.read_text(encoding="utf-8"))
    agent_config["cli"] = "other"
    paths.agent_config.write_text(json.dumps(agent_config), encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        cmd_runner(["doctor"])

    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert "doctor problem:" in out
    assert "max_concurrent is not a positive integer" in out
    assert "cli is not 'agent'" in out
    assert "doctor 2 problems" in out


def test_doctor_reports_a_missing_prompt_template_and_a_broken_agent_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The three health functions are clean here; these two rules are what the
    # doctor has to catch on its own.
    home = tmp_path / "home"
    paths = _healthy_runner_files(home)
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(
        "agent_cli.runner_cmd.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        "agent_cli.runner_cmd.run_argv",
        lambda _argv: _completed(0, "example-bot\n"),
    )
    (paths.prompts / "pr-review.md").unlink()
    config = json.loads(paths.runner_config.read_text(encoding="utf-8"))
    config["agent"]["args"] = "not a list"
    paths.runner_config.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        cmd_runner(["doctor"])

    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert "agent.args is not a list of strings" in out
    assert f"prompt template {paths.prompts / 'pr-review.md'} is missing" in out
    assert "doctor 2 problems" in out


def test_doctor_reports_missing_configuration_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    (home / "runner").mkdir(parents=True)
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(
        "agent_cli.runner_cmd.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    with pytest.raises(SystemExit) as raised:
        cmd_runner(["doctor"])

    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert "doctor problem: policy" in out
    assert "doctor problem: runner config" in out
    assert "not writable" not in out
