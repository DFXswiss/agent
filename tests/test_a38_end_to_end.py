"""Real local execution through the dfx pr guard's pure assessment boundary.

Uses public example repository identities, temporary Git repositories, and small
shell commands. GitHub transport/publication is covered by guard API tests.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli.a38 import load_policy, run_policy  # noqa: E402
from agent_cli.a38_guard import (  # noqa: E402
    GUARD_MARKER,
    PullSnapshot,
    assess_from_parts,
)
from agent_cli.local_ci import (  # noqa: E402
    BEGIN_MARK,
    END_MARK,
    extract_json_text,
    parse_comment,
)

BASE_REPOSITORY = "example/public-app"
FORK_REPOSITORY = "contributor/public-app"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=A38 Test",
            "-c", "user.email=a38@example.com",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _policy(second_command: str = "test -f README", timeout_s: float = 5) -> dict:
    return load_policy(json.dumps({
        "schema": "a38/v1",
        "standard": "A38",
        "documentation": "docs/a38.md",
        "mode": "enforce",
        "jobs": [
            {
                "id": "environment",
                "name": "Environment",
                "command": 'test -n "$A38_HEAD_SHA" && test -n "$A38_BASE_SHA"',
                "timeout_s": 5,
                "workflow": ".github/workflows/ci.yml",
                "job": "environment",
            },
            {
                "id": "unit",
                "name": "Unit",
                "command": second_command,
                "timeout_s": timeout_s,
                "workflow": ".github/workflows/ci.yml",
                "job": "unit",
            },
        ],
        "exclusions": [],
    }))


def _prepare(root: Path, policy: dict) -> tuple[Path, PullSnapshot]:
    repo = root / "checkout"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", f"https://github.com/{FORK_REPOSITORY}.git")
    (repo / "README").write_text("public fixture\n", encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github" / "a38.json").write_text(json.dumps(policy), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial fixture")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, PullSnapshot(
        repo=BASE_REPOSITORY,
        number=1,
        state="open",
        head_sha=head,
        base_sha=head,
        base_ref="main",
        private=False,
        author_id=1001,
        author_login="contributor",
        head_repo=FORK_REPOSITORY,
    )


def _execute(root: Path, repo: Path, pull: PullSnapshot, policy: dict) -> tuple[dict, str]:
    output = root / "report.md"
    verdict = run_policy(
        repo,
        policy,
        output=output,
        logs_dir=root / "logs",
        base_sha=pull.base_sha,
        private=False,
        repository=pull.repo,
        policy_path=repo / ".github" / "a38.json",
    )
    return verdict, output.read_text(encoding="utf-8")


def _assess(pull: PullSnapshot, policy: dict, body: str | None):
    comment = None if body is None else {
        "id": 123,
        "body": body,
        "user": {"id": pull.author_id, "login": pull.author_login},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    return assess_from_parts(
        pull=pull,
        policy=policy,
        policy_error=None,
        workflow_problems=[],
        author_comment=comment,
    )


class RunnerGuardEndToEndTests(unittest.TestCase):
    def test_real_complete_report_passes_for_target_of_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = _policy()
            repo, pull = _prepare(root, policy)
            verdict, body = _execute(root, repo, pull, policy)
            self.assertTrue(verdict["ok"], verdict)
            report = parse_comment(body)
            self.assertEqual(report.repo, BASE_REPOSITORY)
            self.assertNotEqual(report.repo, FORK_REPOSITORY)
            self.assertEqual(report.head, pull.head_sha)
            self.assertEqual(len(report.runs), len(policy["jobs"]))
            for run in report.runs:
                self.assertEqual(run.result, "pass")
                self.assertGreater(run.duration_s, 0)
                self.assertLessEqual(run.duration_s, run.timeout_s)
                self.assertTrue((root / "logs" / f"{run.id}.log").is_file())
            assessment = _assess(pull, policy, body)
            self.assertTrue(assessment.ok, assessment.reasons)
            self.assertEqual(assessment.state_for_status, "success")
            self.assertIn(GUARD_MARKER, assessment.comment_body)
            self.assertEqual(_git(repo, "status", "--porcelain"), "")

    def test_missing_and_partial_reports_fail_after_real_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = _policy()
            repo, pull = _prepare(root, policy)
            verdict, body = _execute(root, repo, pull, policy)
            self.assertTrue(verdict["ok"])
            payload = json.loads(extract_json_text(body))
            payload["runs"].pop()
            partial = f"{BEGIN_MARK}\n```json\n{json.dumps(payload)}\n```\n{END_MARK}\n"
            for comment in (None, partial):
                with self.subTest(missing=comment is None):
                    assessment = _assess(pull, policy, comment)
                    self.assertFalse(assessment.ok)
                    self.assertEqual(assessment.state_for_status, "failure")
                    self.assertTrue(assessment.reasons)

    def test_real_failure_and_timeout_produce_rejected_complete_reports(self) -> None:
        for command, timeout, expected in (("exit 7", 5, "fail"), ("sleep 10", 0.2, "timeout")):
            with self.subTest(result=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                policy = _policy(command, timeout)
                repo, pull = _prepare(root, policy)
                verdict, body = _execute(root, repo, pull, policy)
                self.assertFalse(verdict["ok"])
                report = parse_comment(body)
                self.assertEqual(len(report.runs), 2)
                self.assertEqual(report.runs[0].result, "pass")
                self.assertEqual(report.runs[1].result, expected)
                self.assertGreater(report.runs[1].duration_s, 0)
                assessment = _assess(pull, policy, body)
                self.assertFalse(assessment.ok)
                self.assertEqual(assessment.state_for_status, "failure")
                self.assertTrue(assessment.reasons)

    def test_new_head_invalidates_old_report_and_new_execution_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = _policy()
            repo, pull = _prepare(root, policy)
            verdict, old_body = _execute(root, repo, pull, policy)
            self.assertTrue(verdict["ok"])
            self.assertTrue(_assess(pull, policy, old_body).ok)
            (repo / "README").write_text("updated public fixture\n", encoding="utf-8")
            _git(repo, "add", "README")
            _git(repo, "commit", "-m", "Update fixture")
            updated = replace(pull, head_sha=_git(repo, "rev-parse", "HEAD"))
            self.assertNotEqual(updated.head_sha, pull.head_sha)
            stale = _assess(updated, policy, old_body)
            self.assertFalse(stale.ok)
            self.assertEqual(stale.state_for_status, "failure")
            self.assertTrue(any("head" in reason.lower() for reason in stale.reasons))
            verdict, new_body = _execute(root, repo, updated, policy)
            self.assertTrue(verdict["ok"])
            self.assertEqual(parse_comment(new_body).head, updated.head_sha)
            self.assertTrue(_assess(updated, policy, new_body).ok)


if __name__ == "__main__":
    unittest.main()
