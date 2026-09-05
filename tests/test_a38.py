from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli.a38 import (
    A38Error,
    TERMINATION_GRACE_S,
    load_policy,
    main,
    parse_github_origin,
    run_policy,
    verify_report,
)
from agent_cli.local_ci import BEGIN_MARK, END_MARK, parse_comment

HEAD_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _policy_dict(**overrides: object) -> dict:
    base: dict = {
        "schema": "a38/v1",
        "standard": "A38",
        "documentation": "docs/a38.md",
        "mode": "enforce",
        "jobs": [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            }
        ],
        "exclusions": [],
    }
    base.update(overrides)
    return base


def _policy_text(**overrides: object) -> str:
    return json.dumps(_policy_dict(**overrides))


def _run_payload(
    *,
    ident: str = "unit",
    name: str = "Unit",
    command: str = "true",
    result: str = "pass",
    exit_code: int = 0,
    duration_s: float = 0.1,
    timeout_s: float = 30,
) -> dict:
    return {
        "id": ident,
        "name": name,
        "command": command,
        "result": result,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "timeout_s": timeout_s,
    }


def _report_comment(
    *,
    repo: str = "example/app",
    head: str = HEAD_A,
    private: bool = True,
    recorded_at: str | None = None,
    required: list[str] | None = None,
    runs: list[dict] | None = None,
) -> str:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if required is None:
        required = ["unit"]
    if runs is None:
        runs = [_run_payload()]
    payload = {
        "schema": "dfx-local-ci/v1",
        "repo": repo,
        "head": head,
        "private": private,
        "recorded_at": recorded_at,
        "required": required,
        "runs": runs,
    }
    return f"{BEGIN_MARK}\n```json\n{json.dumps(payload)}\n```\n{END_MARK}\n"


def _git(cwd: Path, *parts: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *parts],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path, *, origin: str = "https://github.com/example/app.git") -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "a38@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "A38 Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", origin],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return _git(path, "rev-parse", "HEAD").lower()


class PolicyTests(unittest.TestCase):
    def test_load_policy_ok(self) -> None:
        policy = load_policy(_policy_text())
        self.assertEqual(policy["schema"], "a38/v1")
        self.assertEqual(policy["jobs"][0]["id"], "unit")
        self.assertEqual(policy["exclusions"], [])

    def test_rejects_unknown_key(self) -> None:
        raw = _policy_dict()
        raw["extra"] = 1
        with self.assertRaisesRegex(A38Error, "unknown keys"):
            load_policy(json.dumps(raw))

    def test_rejects_empty_jobs(self) -> None:
        with self.assertRaisesRegex(A38Error, "non-empty"):
            load_policy(_policy_text(jobs=[]))

    def test_rejects_duplicate_job_ids(self) -> None:
        jobs = [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            },
            {
                "id": "unit",
                "name": "Other",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "other",
            },
        ]
        with self.assertRaisesRegex(A38Error, "duplicate job id"):
            load_policy(_policy_text(jobs=jobs))

    def test_rejects_duplicate_workflow_job_tuple(self) -> None:
        jobs = [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            }
        ]
        exclusions = [
            {
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
                "reason": "covered elsewhere",
            }
        ]
        with self.assertRaisesRegex(A38Error, "duplicate workflow/job"):
            load_policy(_policy_text(jobs=jobs, exclusions=exclusions))

    def test_rejects_command_newline(self) -> None:
        jobs = [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true\nfalse",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            }
        ]
        with self.assertRaisesRegex(A38Error, "newlines or NUL"):
            load_policy(_policy_text(jobs=jobs))

    def test_rejects_bad_workflow_path(self) -> None:
        jobs = [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/../secrets.yml",
                "job": "unit",
            }
        ]
        with self.assertRaisesRegex(A38Error, "workflows"):
            load_policy(_policy_text(jobs=jobs))

    def test_rejects_nonfinite_timeout(self) -> None:
        text = (
            '{"schema":"a38/v1","standard":"A38","documentation":"docs/a38.md",'
            '"mode":"enforce","jobs":[{"id":"unit","name":"Unit","command":"true",'
            '"timeout_s":NaN,"workflow":".github/workflows/ci.yml","job":"unit"}],'
            '"exclusions":[]}'
        )
        with self.assertRaisesRegex(A38Error, "non-finite"):
            load_policy(text)

    def test_rejects_duplicate_json_keys(self) -> None:
        text = (
            '{"schema":"a38/v1","standard":"A38","documentation":"docs/a38.md",'
            '"mode":"enforce","mode":"observe","jobs":[{"id":"unit","name":"Unit",'
            '"command":"true","timeout_s":30,"workflow":".github/workflows/ci.yml",'
            '"job":"unit"}],"exclusions":[]}'
        )
        with self.assertRaisesRegex(A38Error, "duplicate key"):
            load_policy(text)

    def test_rejects_timeout_out_of_range(self) -> None:
        jobs = [
            {
                "id": "unit",
                "name": "Unit",
                "command": "true",
                "timeout_s": 86401,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            }
        ]
        with self.assertRaisesRegex(A38Error, "86400"):
            load_policy(_policy_text(jobs=jobs))

    def test_markdownish_id_rejected(self) -> None:
        jobs = [
            {
                "id": "unit`x",
                "name": "Unit",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            }
        ]
        with self.assertRaisesRegex(A38Error, "kebab-case"):
            load_policy(_policy_text(jobs=jobs))


class VerifyTests(unittest.TestCase):
    def test_pass_private_and_public(self) -> None:
        policy = load_policy(_policy_text())
        for private in (True, False):
            comment = _report_comment(private=private)
            verdict = verify_report(
                comment, policy, repo="example/app", head=HEAD_A, private=private
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            self.assertEqual(verdict["status"], "pass")

    def test_public_does_not_bypass_failed_run(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(
            private=False,
            runs=[_run_payload(result="fail", exit_code=1)],
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=False
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("result is fail" in r for r in verdict["reasons"]))

    def test_repo_case_insensitive_head_exact(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(repo="Example/App", head=HEAD_A)
        ok = verify_report(comment, policy, repo="example/app", head=HEAD_A, private=True)
        self.assertTrue(ok["ok"])
        bad = verify_report(comment, policy, repo="example/app", head=HEAD_B, private=True)
        self.assertFalse(bad["ok"])
        self.assertTrue(any("head" in r for r in bad["reasons"]))

    def test_private_mismatch(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(private=True)
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=False
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("private" in r for r in verdict["reasons"]))

    def test_required_redefinition_rejected(self) -> None:
        policy = load_policy(
            _policy_text(
                jobs=[
                    {
                        "id": "unit",
                        "name": "Unit",
                        "command": "true",
                        "timeout_s": 30,
                        "workflow": ".github/workflows/ci.yml",
                        "job": "unit",
                    },
                    {
                        "id": "lint",
                        "name": "Lint",
                        "command": "true",
                        "timeout_s": 30,
                        "workflow": ".github/workflows/ci.yml",
                        "job": "lint",
                    },
                ]
            )
        )
        comment = _report_comment(required=["unit"], runs=[_run_payload()])
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("required ids" in r for r in verdict["reasons"]))

    def test_command_name_timeout_must_match_policy(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(runs=[_run_payload(command="false", name="Nope", timeout_s=9)])
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        joined = " ".join(verdict["reasons"])
        self.assertIn("command", joined)
        self.assertIn("name", joined)
        self.assertIn("timeout_s", joined)

    def test_future_timestamp_rejected(self) -> None:
        policy = load_policy(_policy_text())
        future = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        comment = _report_comment(recorded_at=future)
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("5 minutes" in r for r in verdict["reasons"]))

    def test_parse_error_does_not_echo_body(self) -> None:
        policy = load_policy(_policy_text())
        secret = "UNIQUE_SECRET_BODY_TOKEN_SHOULD_NOT_LEAK"
        verdict = verify_report(
            secret, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(all(secret not in r for r in verdict["reasons"]))

    def test_duration_over_policy_timeout(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(runs=[_run_payload(duration_s=31, timeout_s=30)])
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("duration_s exceeds" in r for r in verdict["reasons"]))


class OriginTests(unittest.TestCase):
    def test_https_and_ssh(self) -> None:
        self.assertEqual(
            parse_github_origin("https://github.com/Acme/App.git"),
            "Acme/App",
        )
        self.assertEqual(
            parse_github_origin("git@github.com:Acme/App.git"),
            "Acme/App",
        )
        self.assertEqual(
            parse_github_origin("ssh://git@github.com/Acme/App.git"),
            "Acme/App",
        )

    def test_rejects_non_github(self) -> None:
        with self.assertRaisesRegex(A38Error, "GitHub URL"):
            parse_github_origin("https://gitlab.com/Acme/App.git")


class RunnerTests(unittest.TestCase):
    def test_fork_report_targets_base_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo, origin="https://github.com/contributor/fork.git")
            calls = []

            def lookup(argv, cwd, env):
                if argv[0] == "gh":
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, '{"isPrivate":true}', "")
                return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)

            output = root / "report.md"
            verdict = run_policy(
                repo, _policy_dict(), output=output, logs_dir=root / "logs",
                base_sha=head, repository="example/app", run=lookup,
            )
            self.assertTrue(verdict["ok"])
            self.assertEqual(calls, [["gh", "repo", "view", "example/app", "--json", "isPrivate"]])
            report = parse_comment(output.read_text())
            self.assertEqual(report.repo, "example/app")
            self.assertTrue(report.private)

    def test_preflight_failures_invalidate_stale_success(self) -> None:
        for failure in ("dirty", "base", "visibility", "policy"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                head = _init_repo(repo)
                output = root / "report.md"
                output.write_text("stale-success")
                policy = _policy_dict()
                if failure == "dirty":
                    (repo / "dirty").write_text("changed")
                if failure == "policy":
                    policy["jobs"][0]["id"] = "../escape"

                def lookup(argv, cwd, env):
                    if argv[0] == "gh":
                        return subprocess.CompletedProcess(argv, 1, "", "unavailable")
                    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)

                with self.assertRaises(A38Error):
                    run_policy(
                        repo, policy, output=output, logs_dir=root / "logs",
                        base_sha=None if failure == "base" else head,
                        private=None if failure == "visibility" else True, run=lookup,
                    )
                self.assertFalse(output.exists())

    def test_preserves_policy_and_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            source = root / "policy.json"
            source.write_text(_policy_text())
            linked = root / "linked.md"
            linked.symlink_to(source)
            directory = root / "directory"
            directory.mkdir()
            for output in (source, linked, directory):
                with self.subTest(output=output), self.assertRaises(A38Error):
                    run_policy(
                        repo, _policy_dict(), output=output, logs_dir=root / "logs",
                        base_sha=head, private=True, policy_path=source,
                    )
                self.assertEqual(source.read_text(), _policy_text())
            with self.assertRaises(A38Error):
                run_policy(
                    repo, _policy_dict(), output=root / "report.md", logs_dir=root,
                    base_sha=head, private=True, policy_path=source,
                )
            self.assertEqual(source.read_text(), _policy_text())

    def test_launch_failure_writes_complete_failed_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            output = root / "report.md"
            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                if kwargs.get("args", [None])[0] == "bash":
                    time.sleep(0.01)
                    raise OSError("launch unavailable")
                return real_popen(*args, **kwargs)

            with mock.patch("agent_cli.a38.subprocess.Popen", side_effect=launch):
                verdict = run_policy(
                    repo, _policy_dict(), output=output, logs_dir=root / "logs",
                    base_sha=head, private=True,
                )
            self.assertFalse(verdict["ok"])
            report = parse_comment(output.read_text())
            self.assertEqual(report.runs[0].result, "error")
            self.assertGreater(report.runs[0].duration_s, 0)

    def test_sigterm_writes_failed_report_and_restores_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            output = root / "report.md"
            handler = signal.getsignal(signal.SIGTERM)

            def interrupted(**kwargs):
                time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)
                raise AssertionError("SIGTERM should interrupt the run")

            with mock.patch("agent_cli.a38._run_one_job", side_effect=interrupted):
                verdict = run_policy(
                    repo, _policy_dict(), output=output, logs_dir=root / "logs",
                    base_sha=head, private=True,
                )
            self.assertFalse(verdict["ok"])
            report = parse_comment(output.read_text())
            self.assertEqual(report.runs[0].result, "error")
            self.assertGreater(report.runs[0].duration_s, 0)
            self.assertEqual(signal.getsignal(signal.SIGTERM), handler)

    def test_report_cannot_pass_with_elapsed_time_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            output = root / "report.md"
            with mock.patch("agent_cli.a38._run_one_job", return_value=("pass", 0, 31.0)):
                verdict = run_policy(
                    repo, _policy_dict(), output=output, logs_dir=root / "logs",
                    base_sha=head, private=True,
                )
            self.assertFalse(verdict["ok"])
            self.assertEqual(parse_comment(output.read_text()).runs[0].result, "timeout")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_term_resistant_descendants_cleaned_after_timeout_and_success(self) -> None:
        for timeout in (True, False):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                head = _init_repo(repo)
                marker = root / "child.pid"
                child = (
                    "import os,signal,time,pathlib; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                )
                command = (
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} & "
                    f"while [ ! -s {shlex.quote(str(marker))} ]; do sleep 0.01; done; "
                    + ("wait" if timeout else "exit 0")
                )
                policy = _policy_dict()
                policy["jobs"][0].update(command=command, timeout_s=1 if timeout else 5)
                try:
                    started = time.monotonic()
                    verdict = run_policy(
                        repo, policy, output=root / "report.md", logs_dir=root / "logs",
                        base_sha=head, private=True,
                    )
                    self.assertLess(time.monotonic() - started, TERMINATION_GRACE_S / 2)
                    self.assertEqual(verdict["ok"], not timeout)
                    self.assertTrue(marker.exists())
                    pid = int(marker.read_text())
                    deadline = time.monotonic() + 3
                    alive = True
                    while time.monotonic() < deadline:
                        status = subprocess.run(
                            ["ps", "-o", "stat=", "-p", str(pid)],
                            text=True, capture_output=True,
                        )
                        # Orphan zombies on Linux have terminated; init owns reaping.
                        if not status.stdout.strip() or status.stdout.strip().startswith("Z"):
                            alive = False
                            break
                        time.sleep(0.05)
                    self.assertFalse(alive, "TERM-resistant descendant survived cleanup")
                finally:
                    if marker.exists():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_run_pass_fail_timeout_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            base = head
            policy = load_policy(
                _policy_text(
                    jobs=[
                        {
                            "id": "ok-job",
                            "name": "Ok",
                            "command": "true",
                            "timeout_s": 5,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "ok",
                        },
                        {
                            "id": "fail-job",
                            "name": "Fail",
                            "command": "false",
                            "timeout_s": 5,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "fail",
                        },
                        {
                            "id": "slow-job",
                            "name": "Slow",
                            "command": "sleep 5",
                            "timeout_s": 1,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "slow",
                        },
                    ]
                )
            )
            output = out_dir / "report.md"
            verdict = run_policy(
                repo,
                policy,
                output=output,
                logs_dir=logs,
                base_sha=base,
                private=True,
            )
            self.assertFalse(verdict["ok"])
            self.assertTrue(output.is_file())
            self.assertEqual(stat_mode(output) & 0o777, 0o600)
            report = parse_comment(output.read_text(encoding="utf-8"))
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["ok-job"].result, "pass")
            self.assertEqual(by_id["fail-job"].result, "fail")
            self.assertEqual(by_id["slow-job"].result, "timeout")
            for ident in ("ok-job", "fail-job", "slow-job"):
                log_path = logs / f"{ident}.log"
                self.assertTrue(log_path.is_file())
                self.assertEqual(stat_mode(log_path) & 0o777, 0o600)

    def test_process_group_cleanup_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            marker = root / "child.pid"
            # Start a grandchild sleep, write its pid, then sleep long.
            command = (
                f"sleep 30 & echo $! > '{marker}'; wait"
            )
            policy = load_policy(
                _policy_text(
                    jobs=[
                        {
                            "id": "tree",
                            "name": "Tree",
                            "command": command,
                            "timeout_s": 1,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "tree",
                        }
                    ]
                )
            )
            verdict = run_policy(
                repo,
                policy,
                output=out_dir / "report.md",
                logs_dir=logs,
                base_sha=head,
                private=False,
            )
            self.assertFalse(verdict["ok"])
            self.assertTrue(marker.is_file())
            child_pid = int(marker.read_text(encoding="utf-8").strip())
            # Give the killer a moment, then the child must be gone.
            deadline = time.time() + 3
            alive = True
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                except OSError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive, "child process should be reaped after timeout")

    def test_dirty_tree_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
            policy = load_policy(_policy_text())
            with self.assertRaisesRegex(A38Error, "not clean"):
                run_policy(
                    repo,
                    policy,
                    output=out_dir / "report.md",
                    logs_dir=logs,
                    base_sha=head,
                    private=True,
                )

    def test_output_inside_repo_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            head = _init_repo(repo)
            policy = load_policy(_policy_text())
            with self.assertRaisesRegex(A38Error, "outside"):
                run_policy(
                    repo,
                    policy,
                    output=repo / "report.md",
                    logs_dir=logs,
                    base_sha=head,
                    private=True,
                )

    def test_head_drift_forces_fail_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            # Job amends the tree and commits, drifting HEAD.
            command = (
                "echo drift >> README && git add README && "
                "git -c user.email=a38@example.com -c user.name=A38 "
                "-c commit.gpgsign=false -c core.hooksPath=/dev/null "
                "commit -m drift >/dev/null"
            )
            policy = load_policy(
                _policy_text(
                    jobs=[
                        {
                            "id": "drift",
                            "name": "Drift",
                            "command": command,
                            "timeout_s": 30,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "drift",
                        },
                        {
                            "id": "later",
                            "name": "Later",
                            "command": "true",
                            "timeout_s": 30,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "later",
                        },
                    ]
                )
            )
            output = out_dir / "report.md"
            verdict = run_policy(
                repo,
                policy,
                output=output,
                logs_dir=logs,
                base_sha=head,
                private=True,
            )
            self.assertFalse(verdict["ok"])
            report = parse_comment(output.read_text(encoding="utf-8"))
            self.assertTrue(all(run.result == "fail" for run in report.runs))
            self.assertEqual(len(report.runs), 2)

    def test_removes_stale_output_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            output = out_dir / "report.md"
            output.write_text("stale-success\n", encoding="utf-8")
            policy = load_policy(
                _policy_text(
                    jobs=[
                        {
                            "id": "boom",
                            "name": "Boom",
                            "command": "false",
                            "timeout_s": 5,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "boom",
                        }
                    ]
                )
            )
            run_policy(
                repo,
                policy,
                output=output,
                logs_dir=logs,
                base_sha=head,
                private=True,
            )
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("stale-success", text)
            self.assertIn(BEGIN_MARK, text)

    def test_strips_github_tokens_from_job_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            policy = load_policy(
                _policy_text(
                    jobs=[
                        {
                            "id": "env",
                            "name": "Env",
                            "command": (
                                'test -z "$GITHUB_TOKEN" && test -z "$GH_TOKEN" && '
                                'test -n "$A38_HEAD_SHA" && test -n "$A38_BASE_SHA"'
                            ),
                            "timeout_s": 5,
                            "workflow": ".github/workflows/ci.yml",
                            "job": "env",
                        }
                    ]
                )
            )
            with mock.patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret"},
                clear=False,
            ):
                verdict = run_policy(
                    repo,
                    policy,
                    output=out_dir / "report.md",
                    logs_dir=logs,
                    base_sha=head,
                    private=True,
                )
            self.assertTrue(verdict["ok"], msg=verdict)

    def test_explicit_private_skips_gh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            head = _init_repo(repo)
            policy = load_policy(_policy_text())

            def boom_gh(
                argv: list[str],
                cwd: Path | None,
                env: object,
            ) -> subprocess.CompletedProcess[str]:
                if argv and argv[0] == "gh":
                    raise AssertionError("gh should not be called when private is explicit")
                return subprocess.run(
                    argv,
                    cwd=str(cwd) if cwd is not None else None,
                    env=dict(env) if env is not None else None,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            verdict = run_policy(
                repo,
                policy,
                output=out_dir / "report.md",
                logs_dir=logs,
                base_sha=head,
                private=False,
                run=boom_gh,
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((out_dir / "report.md").read_text(encoding="utf-8"))
            self.assertFalse(report.private)

    def test_base_sha_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out_dir = root / "out"
            logs = root / "logs"
            out_dir.mkdir()
            logs.mkdir()
            _init_repo(repo)
            policy = load_policy(_policy_text())
            with self.assertRaisesRegex(A38Error, "base-sha"):
                run_policy(
                    repo,
                    policy,
                    output=out_dir / "report.md",
                    logs_dir=logs,
                    base_sha=None,
                    private=True,
                )


class CliTests(unittest.TestCase):
    def test_policy_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a38.json"
            path.write_text(_policy_text(), encoding="utf-8")
            code = main(["policy", "--file", str(path)])
            self.assertEqual(code, 0)

    def test_verify_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_path = root / "a38.json"
            report_path = root / "report.md"
            policy_path.write_text(_policy_text(), encoding="utf-8")
            report_path.write_text(_report_comment(), encoding="utf-8")
            ok = main(
                [
                    "verify",
                    "--policy",
                    str(policy_path),
                    "--file",
                    str(report_path),
                    "--repo",
                    "example/app",
                    "--head",
                    HEAD_A,
                    "--private",
                ]
            )
            self.assertEqual(ok, 0)
            bad = main(
                [
                    "verify",
                    "--policy",
                    str(policy_path),
                    "--file",
                    str(report_path),
                    "--repo",
                    "example/app",
                    "--head",
                    HEAD_B,
                    "--private",
                ]
            )
            self.assertEqual(bad, 1)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
