from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import sys
import subprocess
import tempfile
import threading
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
    _build_report_dict,
    _write_report,
    ensure_local_ci_cli_satisfies_pin,
    guard_action_pins,
    load_policy,
    local_ci_revision,
    main,
    parse_github_origin,
    report_fact_reasons,
    run_policy,
    verify_report,
)
from agent_cli.local_ci import BEGIN_MARK, END_MARK, parse_comment, render_block
from agent_cli.readme_only import GUARD_WORKFLOW_PATH

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


def _compose_executor_job(**config_overrides: object) -> dict:
    config = {
        "companion": {
            "directory_env": "EXAMPLE_SERVICES_DIR",
            "ref_env": "EXAMPLE_SERVICES_REF",
            "ref": "main",
            "repository": "example/services",
        },
        "files": ["compose.yml"],
        "test_service": "tests",
        "artifacts": [],
        **config_overrides,
    }
    return {
        "id": "compose",
        "name": "Compose",
        "executor": {"adapter": "compose", "config": config},
        "timeout_s": 30,
        "workflow": ".github/workflows/ci.yml",
        "job": "compose",
    }


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
    readme_only: bool = False,
    markdown_only: bool = False,
) -> str:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if required is None:
        required = ["unit"]
    if runs is None:
        runs = [_run_payload()]
    payload: dict = {
        "schema": "dfx-local-ci/v1",
        "repo": repo,
        "head": head,
        "private": private,
        "recorded_at": recorded_at,
        "required": required,
        "runs": runs,
    }
    if readme_only:
        payload["readme_only"] = True
    if markdown_only:
        payload["markdown_only"] = True
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


def _commit_all(path: Path, message: str) -> str:
    """Commit everything in ``path`` and return the new revision."""
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            message,
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return _git(path, "rev-parse", "HEAD").lower()


def _init_two_commit_checkout(path: Path) -> tuple[str, str]:
    """Create a linear two-commit repository and return ``(ancestor, descendant)``."""
    ancestor = _init_repo(path)
    (path / "README").write_text("two\n", encoding="utf-8")
    descendant = _commit_all(path, "second")
    return ancestor, descendant


def _commit_beside(path: Path, base: str) -> str:
    """A real commit on ``base`` that is no ancestor of HEAD, leaving HEAD alone."""
    tree = _git(path, "rev-parse", f"{base}^{{tree}}")
    return _git(path, "commit-tree", tree, "-p", base, "-m", "beside").lower()


def _installation_module(path: Path, *, git: bool) -> Path:
    """``path/src/agent_cli/a38.py``, with a committed checkout when ``git`` is set."""
    module = path / "src" / "agent_cli" / "a38.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# placeholder\n", encoding="utf-8")
    if not git:
        return module
    _init_repo(path)
    subprocess.run(
        ["git", "add", "src/agent_cli/a38.py"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "install",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return module


def _guard_workflow_text(steps: str) -> str:
    """A real workflow document, because the pin is read from parsed YAML."""
    return (
        "name: A38 guard\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "jobs:\n"
        "  guard:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
    ) + steps


def _write_guard_workflow(repo: Path, *refs: str) -> None:
    workflow = repo / ".github" / "workflows" / "a38-guard.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        _guard_workflow_text(
            "".join(
                f"      - uses: DFXswiss/agent/.github/actions/a38-guard@{ref}\n"
                for ref in refs
            )
        ),
        encoding="utf-8",
    )


def _forbid_run(
    argv: list[str],
    cwd: Path | None,
    env: object,
) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"git must not run: {argv}")


class PolicyTests(unittest.TestCase):
    def test_load_policy_ok(self) -> None:
        policy = load_policy(_policy_text())
        self.assertEqual(policy["schema"], "a38/v1")
        self.assertEqual(policy["jobs"][0]["id"], "unit")
        self.assertEqual(policy["exclusions"], [])
        self.assertEqual(policy["readme_only_omit"], [])

    def test_readme_only_omit_jobs(self) -> None:
        policy = load_policy(
            _policy_text(readme_only={"omit_jobs": ["unit"]})
        )
        self.assertEqual(policy["readme_only_omit"], ["unit"])
        self.assertEqual(load_policy(json.dumps(policy)), policy)

    def test_normalized_policy_without_omit_reloads(self) -> None:
        policy = load_policy(_policy_text())
        self.assertEqual(policy["readme_only_omit"], [])
        self.assertEqual(load_policy(json.dumps(policy)), policy)

    def test_readme_only_rejects_unknown_job_id(self) -> None:
        with self.assertRaisesRegex(A38Error, "unknown id"):
            load_policy(_policy_text(readme_only={"omit_jobs": ["missing"]}))

    def test_readme_only_rejects_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(A38Error, "duplicate id"):
            load_policy(_policy_text(readme_only={"omit_jobs": ["unit", "unit"]}))

    def test_readme_only_rejects_empty_omit_jobs(self) -> None:
        with self.assertRaisesRegex(A38Error, "non-empty"):
            load_policy(_policy_text(readme_only={"omit_jobs": []}))

    def test_readme_only_rejects_unknown_key(self) -> None:
        with self.assertRaisesRegex(A38Error, "unknown keys"):
            load_policy(
                _policy_text(readme_only={"omit_jobs": ["unit"], "extra": True})
            )

    def test_readme_only_rejects_normalized_and_wire_together(self) -> None:
        raw = _policy_dict(readme_only={"omit_jobs": ["unit"]})
        raw["readme_only_omit"] = ["unit"]
        with self.assertRaisesRegex(A38Error, "cannot both be set"):
            load_policy(json.dumps(raw))

    def test_readme_only_rejects_non_list_normalized_omit(self) -> None:
        raw = _policy_dict()
        raw["readme_only_omit"] = {"unit": True}
        with self.assertRaisesRegex(A38Error, "non-empty bounded array"):
            load_policy(json.dumps(raw))

    def test_rejects_unknown_key(self) -> None:
        raw = _policy_dict()
        raw["extra"] = 1
        with self.assertRaisesRegex(A38Error, "unknown keys"):
            load_policy(json.dumps(raw))

    def test_command_job_lock_round_trip(self) -> None:
        raw = _policy_dict()
        raw["jobs"][0]["lock"] = "jest-full"
        policy = load_policy(json.dumps(raw))
        self.assertEqual(policy["jobs"][0]["lock"], "jest-full")

    def test_command_job_rejects_empty_lock(self) -> None:
        raw = _policy_dict()
        raw["jobs"][0]["lock"] = ""
        with self.assertRaisesRegex(A38Error, "lock name"):
            load_policy(json.dumps(raw))

    def test_command_job_rejects_illegal_lock_name(self) -> None:
        raw = _policy_dict()
        raw["jobs"][0]["lock"] = "jest full"
        with self.assertRaisesRegex(A38Error, "lock name"):
            load_policy(json.dumps(raw))

    def test_executor_job_rejects_sibling_lock(self) -> None:
        raw = _policy_dict(
            jobs=[_commands_lock_job(ident="jest", gh_job="jest", lock="jest-full")]
        )
        raw["jobs"][0]["lock"] = "jest-full"
        with self.assertRaisesRegex(A38Error, "executor.config"):
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

    def test_not_applicable_without_flag_fails(self) -> None:
        policy = load_policy(_policy_text(readme_only={"omit_jobs": ["unit"]}))
        comment = _report_comment(
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("not_applicable is not authorized" in r for r in verdict["reasons"]))

    def test_not_applicable_flag_but_id_not_in_omit_fails(self) -> None:
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
                "id": "lint",
                "name": "Lint",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "lint",
            },
        ]
        policy = load_policy(
            _policy_text(jobs=jobs, readme_only={"omit_jobs": ["lint"]})
        )
        comment = _report_comment(
            required=["unit", "lint"],
            runs=[
                _run_payload(),
                _run_payload(
                    ident="lint",
                    name="Lint",
                    result="not_applicable",
                    exit_code=0,
                    duration_s=0,
                ),
            ],
            readme_only=True,
        )
        # unit is not_applicable but only lint is omitted → fail on unit
        bad = _report_comment(
            required=["unit", "lint"],
            runs=[
                _run_payload(
                    result="not_applicable", exit_code=0, duration_s=0
                ),
                _run_payload(ident="lint", name="Lint"),
            ],
            readme_only=True,
        )
        verdict = verify_report(
            bad, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("unit: not_applicable is not authorized" in r for r in verdict["reasons"]))
        ok = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertTrue(ok["ok"], msg=ok)

    def test_not_applicable_with_flag_omit_and_exit_zero_passes(self) -> None:
        policy = load_policy(_policy_text(readme_only={"omit_jobs": ["unit"]}))
        comment = _report_comment(
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
            readme_only=True,
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertTrue(verdict["ok"], msg=verdict)

    def test_not_applicable_with_flag_omit_and_exit_one_fails(self) -> None:
        policy = load_policy(_policy_text(readme_only={"omit_jobs": ["unit"]}))
        comment = _report_comment(
            runs=[_run_payload(result="not_applicable", exit_code=1, duration_s=0)],
            readme_only=True,
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("exit_code is 1" in r for r in verdict["reasons"]))

    def test_markdown_only_authorizes_all_jobs_not_applicable(self) -> None:
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
                "id": "lint",
                "name": "Lint",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "lint",
            },
        ]
        policy = load_policy(_policy_text(jobs=jobs))
        comment = _report_comment(
            required=["unit", "lint"],
            runs=[
                _run_payload(result="not_applicable", exit_code=0, duration_s=0),
                _run_payload(
                    ident="lint",
                    name="Lint",
                    result="not_applicable",
                    exit_code=0,
                    duration_s=0,
                ),
            ],
            markdown_only=True,
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertTrue(verdict["ok"], msg=verdict)

    def test_markdown_only_without_flag_rejects_not_applicable(self) -> None:
        policy = load_policy(_policy_text())
        comment = _report_comment(
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
        )
        verdict = verify_report(
            comment, policy, repo="example/app", head=HEAD_A, private=True
        )
        self.assertFalse(verdict["ok"])
        self.assertTrue(
            any("not_applicable is not authorized" in r for r in verdict["reasons"])
        )

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


class ReportPresentationTests(unittest.TestCase):
    def test_presentation_preserves_evidence_and_validation_for_all_outcomes(self) -> None:
        for result, code in (("pass", 0), ("fail", 1), ("error", 127), ("timeout", 124)):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                original = _report_comment(runs=[_run_payload(result=result, exit_code=code)])
                payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
                output = Path(tmp) / "report.md"
                _write_report(output, payload)
                comment = output.read_text()
                visible, details = comment.split("<details>\n<summary>Details</summary>\n\n")
                self.assertEqual(visible, (
                    "EN:\nLocal CI report\nThe A38 report below records the checks, results and durations.\n\n"
                    "DE:\nLokaler CI-Bericht\nDer A38-Bericht unten dokumentiert die Prüfungen, Ergebnisse und Laufzeiten.\n\n"
                ))
                # Only the presentation changes; the entire original evidence remains intact.
                table, original_details = details.split("<details>\n<summary>Original report / Originalbericht</summary>\n\n")
                self.assertIn(f"| unit: Unit | 1 s | 30 s | {result} | {code} |", table)
                self.assertEqual(original_details, render_block(parse_comment(original)) + "\n</details>\n\n</details>\n")
                self.assertNotIn("<details open", comment)
                self.assertEqual(parse_comment(comment), parse_comment(original))
                self.assertEqual(comment.count(BEGIN_MARK), 1)
                self.assertEqual(comment.count(END_MARK), 1)
                verdict = verify_report(comment, _policy_dict(), repo="example/app", head=HEAD_A, private=True)
                self.assertEqual(verdict["ok"], result == "pass")

    def test_table_lists_every_job_in_order_with_ceiling_seconds(self) -> None:
        runs = [
            _run_payload(ident="zero", name="Zero", duration_s=0),
            _run_payload(ident="whole", name="Whole", duration_s=84, result="fail", exit_code=1),
            _run_payload(
                ident="fraction", name="Fraction", duration_s=84.467,
                result="error", exit_code=127, timeout_s=30.2,
            ),
            _run_payload(ident="tiny", name="Tiny", duration_s=0.001, result="timeout", exit_code=124),
        ]
        original = _report_comment(required=[r["id"] for r in runs], runs=runs)
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(output, payload)
            comment = output.read_text()
        rows = [line for line in comment.splitlines() if line.startswith("| ")][2:]
        self.assertEqual(rows, [
            "| zero: Zero | 0 s | 30 s | pass | 0 |",
            "| whole: Whole | 84 s | 30 s | fail | 1 |",
            "| fraction: Fraction | 85 s | 31 s | error | 127 |",
            "| tiny: Tiny | 1 s | 30 s | timeout | 124 |",
        ])
        self.assertEqual(parse_comment(comment), parse_comment(original))
        self.assertIn(render_block(parse_comment(original)), comment)

    def test_policy_name_cannot_inject_table_rows_or_html(self) -> None:
        name = "Checks | `code`\n</details><script>x</script> [link](url) *bold* \\"
        original = _report_comment(runs=[_run_payload(name=name)])
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(output, payload)
            comment = output.read_text()
        body = comment.split("<summary>Details</summary>\n\n", 1)[1].split("<details>", 1)[0]
        rows = [line for line in body.splitlines() if line.startswith("| ")]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].count("|"), 6)
        table = "\n".join(rows)
        for fragment in ("</details>", "<script>", "`", "[link]", "*bold*", "\\"):
            self.assertNotIn(fragment, table)
        self.assertIn("&#124;", table)
        self.assertIn("&lt;/details&gt;", table)
        self.assertEqual(parse_comment(comment).runs[0].name, name)

    def test_details_state_the_outcome(self) -> None:
        recorded = "2026-10-01T07:28:08Z"
        original = _report_comment(
            recorded_at=recorded,
            markdown_only=True,
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
        )
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["docs/guide.md", "README.md"],
                omit_reason="markdown-only change set",
            )
            comment = output.read_text()
        facts = comment.split("<summary>Details</summary>\n\n", 1)[1].split("\n\n| ", 1)[0]
        self.assertEqual(
            facts,
            "\n".join([
                f"Head: `{HEAD_A}`",
                f"Recorded: `{recorded}`",
                f"Base: `{HEAD_B}`",
                f"Policy: `.github/a38.json` at `{HEAD_B}`",
                "Changed paths: 2. Every path ends in `.md`, so the local run is not required.",
                "Omitted: markdown-only change set.",
            ]),
        )
        self.assertNotIn("true", facts)
        required = _report_comment(recorded_at=recorded)
        required_payload = json.loads(required.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                required_payload,
                base_sha=HEAD_B,
                changed_paths=["docs/a38-local-run.json"],
                omit_reason=None,
            )
            facts = output.read_text().split("<summary>Details</summary>\n\n", 1)[1].split("\n\n| ", 1)[0]
        self.assertIn(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
            facts,
        )
        self.assertNotIn("Omitted:", facts)
        self.assertEqual(parse_comment(comment), parse_comment(original))

        unknown = _report_comment(recorded_at=recorded, runs=[_run_payload(result="pass")])
        unknown_payload = json.loads(unknown.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                unknown_payload,
                base_sha="not-a-sha",
                changed_paths=None,
                omit_reason="markdown-only change set",
            )
            unknown_text = output.read_text()
        unknown_facts = unknown_text.split("<summary>Details</summary>\n\n", 1)[1].split("\n\n| ", 1)[0]
        self.assertIn("Changed paths: unknown. The local run is required.", unknown_facts)
        self.assertNotIn("Base:", unknown_facts)
        self.assertNotIn("Omitted:", unknown_facts)
        self.assertEqual(parse_comment(unknown_text), parse_comment(unknown))

    def test_guard_requires_outcome_lines_to_match_the_inventory(self) -> None:
        recorded = "2026-10-01T09:04:19Z"
        original = _report_comment(
            recorded_at=recorded,
            runs=[_run_payload(duration_s=28.371, timeout_s=600)],
        )
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            )
            comment = output.read_text()
        report = parse_comment(comment)
        self.assertEqual(
            report_fact_reasons(
                comment,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        missing = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.\n",
            "",
            1,
        )
        self.assertIn(
            "report outcome does not match the pull request",
            report_fact_reasons(
                missing, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
        )
        self.assertEqual(
            report_fact_reasons(
                comment, report, base_sha=HEAD_B, changed_paths=None
            ),
            ["report changed paths unavailable"],
        )
        self.assertEqual(
            report_fact_reasons(
                comment, report, base_sha="not-a-sha", changed_paths=["docs/sample.json"]
            ),
            ["report base is unavailable"],
        )
        invented = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
            "Changed paths: unknown. The local run is required.",
            1,
        )
        self.assertIn(
            "report outcome does not match the pull request",
            report_fact_reasons(
                invented, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
        )
        later_unknown = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required."
            " Path names stay unknown in this comment.",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                later_unknown,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        with_omit = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.\n",
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.\n"
            "Omitted: markdown-only change set.\n",
            1,
        )
        self.assertIn(
            "report states an omission without not_applicable",
            report_fact_reasons(
                with_omit, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
        )

    def test_negated_outcome_lines_do_not_count(self) -> None:
        recorded = "2026-10-01T09:04:19Z"
        original = _report_comment(
            recorded_at=recorded,
            runs=[_run_payload(result="pass")],
        )
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            )
            comment = output.read_text()
        report = parse_comment(comment)
        self.assertEqual(
            report_fact_reasons(
                comment,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        negated_head = comment.replace(f"Head: `{HEAD_A}`", f"Head: not `{HEAD_A}`", 1)
        self.assertNotEqual(
            report_fact_reasons(
                negated_head,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        negated_paths = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
            "Changed paths: 1. It is not true that every path ends in .md.",
            1,
        )
        self.assertNotEqual(
            report_fact_reasons(
                negated_paths,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        self.assertEqual(
            report_fact_reasons(
                comment,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        markdown = _report_comment(
            recorded_at=recorded,
            markdown_only=True,
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
        )
        markdown_payload = json.loads(markdown.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                markdown_payload,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
                omit_reason="markdown-only change set",
            )
            markdown_comment = output.read_text()
        markdown_report = parse_comment(markdown_comment)
        self.assertEqual(
            report_fact_reasons(
                markdown_comment,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )
        not_used = markdown_comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: markdown-only change set was not used.",
            1,
        )
        self.assertNotEqual(
            report_fact_reasons(
                not_used,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )
        isnt = markdown_comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: markdown-only change set isn't the reason.",
            1,
        )
        self.assertNotEqual(
            report_fact_reasons(
                isnt,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )
        both = markdown_comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: markdown-only change set, not the README-only change set.",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                both,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )
        not_pass = comment.replace(" | pass | ", " | not pass | ", 1)
        self.assertNotEqual(
            report_fact_reasons(
                not_pass,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        pass_not_fail = comment.replace(" | pass | ", " | pass, not fail | ", 1)
        self.assertEqual(
            report_fact_reasons(
                pass_not_fail,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        for denied_head in (
            f"Head: isn't `{HEAD_A}`",
            f"Head: cannot be `{HEAD_A}`",
            f"Head: `{HEAD_A}` is not this commit.",
            f"Head: nicht `{HEAD_A}`",
        ):
            denied = comment.replace(f"Head: `{HEAD_A}`", denied_head, 1)
            self.assertNotEqual(
                report_fact_reasons(
                    denied,
                    report,
                    base_sha=HEAD_B,
                    changed_paths=["docs/sample.json"],
                ),
                [],
                denied_head,
            )
        affirmed_head = comment.replace(
            f"Head: `{HEAD_A}`",
            f"Head: `{HEAD_A}`, not a different repository.",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                affirmed_head,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        recorded_not = comment.replace(
            f"Recorded: `{recorded}`",
            f"Recorded: not `{recorded}`",
            1,
        )
        self.assertNotEqual(
            report_fact_reasons(
                recorded_not,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        for denied_paths in (
            "Changed paths: 1. It isn't true that every path ends in .md.",
            "Changed paths: 1. Every path doesn't end in .md.",
        ):
            denied = comment.replace(
                "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
                denied_paths,
                1,
            )
            self.assertNotEqual(
                report_fact_reasons(
                    denied,
                    report,
                    base_sha=HEAD_B,
                    changed_paths=["docs/sample.json"],
                ),
                [],
                denied_paths,
            )
        distant = markdown_comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: not something that a reviewer would ever treat as the stated "
            "markdown-only change set.",
            1,
        )
        self.assertNotEqual(
            report_fact_reasons(
                distant,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )
        pass_is_not = comment.replace(" | pass | ", " | pass is not | ", 1)
        self.assertNotEqual(
            report_fact_reasons(
                pass_is_not,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            ),
            [],
        )
        self.assertEqual(
            report_fact_reasons(
                markdown_comment,
                markdown_report,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
            ),
            [],
        )

    def test_omission_line_accepts_only_a_reason_the_inventory_makes_true(self) -> None:
        recorded = "2026-10-01T09:04:19Z"
        original = _report_comment(
            recorded_at=recorded,
            markdown_only=True,
            runs=[_run_payload(result="not_applicable", exit_code=0, duration_s=0)],
        )
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["README.md"],
                omit_reason="markdown-only change set",
            )
            comment = output.read_text()
        report = parse_comment(comment)
        self.assertEqual(
            report_fact_reasons(
                comment, report, base_sha=HEAD_B, changed_paths=["README.md"]
            ),
            [],
        )
        readme_line = comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: README-only change set.",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                readme_line, report, base_sha=HEAD_B, changed_paths=["README.md"]
            ),
            [],
        )
        self.assertEqual(
            report_fact_reasons(
                readme_line, report, base_sha=HEAD_B, changed_paths=["docs/guide.md"]
            ),
            ["report omission does not match the change set"],
        )
        bogus = comment.replace(
            "Omitted: markdown-only change set.",
            "Omitted: full suite skipped.",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                bogus, report, base_sha=HEAD_B, changed_paths=["README.md"]
            ),
            ["report omission does not match the change set"],
        )
        code_only = comment.replace("Changed paths: 1. Every path ends in `.md`, so the local run is not required.", "Changed paths: 1. Not every path ends in `.md`, so the local run is required.", 1)
        code_only = code_only.replace("Omitted: markdown-only change set.\n", "", 1)
        self.assertIn(
            "report omission does not match the change set",
            report_fact_reasons(
                code_only, report, base_sha=HEAD_B, changed_paths=["app.py"]
            ),
        )

    def test_outcome_wording_can_vary_when_the_facts_match(self) -> None:
        recorded = "2026-10-01T09:04:19Z"
        original = _report_comment(
            recorded_at=recorded,
            runs=[_run_payload(duration_s=28.371, timeout_s=600)],
        )
        payload = json.loads(original.split("```json\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["docs/sample.json"],
            )
            comment = output.read_text()
        report = parse_comment(comment)
        reworded = comment.replace(
            "Changed paths: 1. Not every path ends in `.md`, so the local run is required.",
            "Changed paths: 1 file. Not every path ends in .md.",
            1,
        ).replace("| 29 s |", "| 29s |", 1).replace(
            "```json\n",
            "```json\n| unit: Unit | 1 s | 1 s | fail | 7 |\n",
            1,
        )
        self.assertEqual(
            report_fact_reasons(
                reworded, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
            [],
        )
        short = reworded.replace("| 29s |", "| 28 s |", 1)
        self.assertIn(
            "report outcome does not match the pull request",
            report_fact_reasons(
                short, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
        )
        wrong_count = reworded.replace("Changed paths: 1 file.", "Changed paths: 2 files.", 1)
        self.assertIn(
            "report outcome does not match the pull request",
            report_fact_reasons(
                wrong_count, report, base_sha=HEAD_B, changed_paths=["docs/sample.json"]
            ),
        )

    def test_v2_original_must_match_the_inventory(self) -> None:
        recorded = "2026-10-01T10:51:24Z"
        run = _run_payload(ident="lint", name="Lint", duration_s=43.391, timeout_s=600)
        payload = _build_report_dict(
            repo="DFXswiss/app",
            head=HEAD_A,
            private=False,
            recorded_at=recorded,
            required=["lint"],
            runs=[run],
            base=HEAD_B,
            changed_paths=["docs/a38-fact-record.json"],
            omit_reason=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.md"
            _write_report(
                output,
                payload,
                base_sha=HEAD_B,
                changed_paths=["docs/a38-fact-record.json"],
            )
            comment = output.read_text()
        report = parse_comment(comment)
        self.assertEqual(report.schema, "dfx-local-ci/v2")
        self.assertEqual(report.changed_count, 1)
        self.assertFalse(report.all_markdown)
        self.assertIsNone(report.omitted)
        self.assertEqual(
            report_fact_reasons(
                comment,
                report,
                base_sha=HEAD_B,
                changed_paths=["docs/a38-fact-record.json"],
            ),
            [],
        )
        lied = parse_comment(comment.replace('"count": 1', '"count": 2', 1))
        self.assertIn(
            "report outcome does not match the pull request",
            report_fact_reasons(
                comment,
                lied,
                base_sha=HEAD_B,
                changed_paths=["docs/a38-fact-record.json"],
            ),
        )


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


class LocalCiCliPinTests(unittest.TestCase):
    def test_absent_workflow_skips_version_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                side_effect=AssertionError("revision must not be resolved"),
            ):
                self.assertEqual(guard_action_pins(repo), [])
                self.assertIsNone(
                    ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
                )

    def test_ancestor_pin_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            repo = root / "repo"
            _write_guard_workflow(repo, ancestor)
            self.assertEqual(guard_action_pins(repo), [ancestor])
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                note = ensure_local_ci_cli_satisfies_pin(repo)
            self.assertEqual(
                note,
                f"local CI CLI {descendant} satisfies the guard revision {ancestor} "
                "installed by this repository",
            )
            _write_guard_workflow(repo, descendant)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                same = ensure_local_ci_cli_satisfies_pin(repo)
            self.assertIn(descendant, same)

    def test_stale_cli_predates_the_guard_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            repo = root / "repo"
            _write_guard_workflow(repo, descendant)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(ancestor, checkout),
            ):
                with self.assertRaises(A38Error) as raised:
                    ensure_local_ci_cli_satisfies_pin(repo)
            message = str(raised.exception)
            self.assertIn("does not contain the guard revision", message)
            self.assertEqual(
                message,
                f"this agent installation ({ancestor}) does not contain the guard revision "
                f"{descendant} that this repository installs; a report written now would be "
                "rejected with 'report outcome does not match the pull request'. Update "
                f"the CLI to a revision that contains {descendant}.",
            )

    def test_unknown_cli_revision_is_refused(self) -> None:
        pin = "0123456789abcdef0123456789abcdef01234567"
        expected = (
            "cannot determine the revision of this agent installation; it is not a Git "
            "checkout, or Git could not report its HEAD, so the report would be written by an "
            "unknown CLI version. Install the "
            f"CLI from a Git checkout at or after {pin}, or set A38_LOCAL_CI_REVISION to its "
            "40-character commit SHA."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _write_guard_workflow(repo, pin)
            module = _installation_module(root / "installation", git=False)
            unknown = local_ci_revision(env={}, module_file=module)
            self.assertEqual(unknown, (None, None))
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=unknown,
            ):
                with self.assertRaises(A38Error) as raised:
                    ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
            self.assertIn("not a Git checkout", str(raised.exception))
            self.assertEqual(str(raised.exception), expected)

    def test_snapshot_without_git_dir_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "outer"
            _init_repo(outer)
            module = _installation_module(outer / "installation", git=False)
            self.assertEqual(
                local_ci_revision(env={}, module_file=module),
                (None, None),
            )

    def test_unknown_pin_cannot_be_proved(self) -> None:
        pin = "0000000000000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            _ancestor, descendant = _init_two_commit_checkout(checkout)
            repo = root / "repo"
            _write_guard_workflow(repo, pin)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                with self.assertRaises(A38Error) as raised:
                    ensure_local_ci_cli_satisfies_pin(repo)
            message = str(raised.exception)
            self.assertIn("cannot prove", message)
            self.assertEqual(
                message,
                f"cannot prove this agent installation ({descendant}) is at or after the "
                f"guard revision {pin}; that commit is not present in this checkout. "
                "Fetch it and run again.",
            )

    def test_malformed_revision_override_is_rejected(self) -> None:
        for sample in ("", "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 40 + "\n"):
            with self.subTest(sample=sample):
                with self.assertRaises(A38Error) as raised:
                    local_ci_revision(env={"A38_LOCAL_CI_REVISION": sample})
                self.assertIn("A38_LOCAL_CI_REVISION", str(raised.exception))
                self.assertEqual(
                    str(raised.exception),
                    "A38_LOCAL_CI_REVISION must be a 40-character lowercase hexadecimal "
                    "commit SHA",
                )

    def test_declared_revision_without_checkout_must_be_the_pin(self) -> None:
        """Without ancestry the declaration is only trusted where nothing needs proving."""
        declared = "0123456789abcdef0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _installation_module(root / "installation", git=False)
            self.assertEqual(
                local_ci_revision(
                    env={"A38_LOCAL_CI_REVISION": declared},
                    module_file=module,
                ),
                (declared, None),
            )
            repo = root / "repo"
            _write_guard_workflow(repo, declared)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(declared, None),
            ):
                note = ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
            self.assertEqual(
                note,
                f"local CI CLI revision {declared} was declared via the "
                "A38_LOCAL_CI_REVISION environment variable and is the guard revision "
                "this repository installs",
            )

    def test_an_unbound_checkout_does_not_point_at_the_env_var(self) -> None:
        # A provable checkout always decides over a declaration, so naming the variable in
        # this refusal would send the operator down a path this very case ignores.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _write_guard_workflow(repo, HEAD_A)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(None, root / "install"),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
            message = str(caught.exception)
            self.assertIn("is a Git checkout at", message)
            self.assertIn("src/agent_cli", message)
            self.assertNotIn("A38_LOCAL_CI_REVISION", message)

    def test_a_declared_revision_that_is_not_the_pin_is_refused(self) -> None:
        # The operator sets this variable, so accepting any value would make the gate
        # advisory: an older installation could declare itself and write the report.
        declared = "0123456789abcdef0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _write_guard_workflow(repo, HEAD_A)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(declared, None),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
            self.assertEqual(
                str(caught.exception),
                f"the declared revision of this agent installation ({declared}) is not the "
                f"guard revision {HEAD_A} that this repository installs, and without a Git "
                "checkout their relationship cannot be proved. Install the CLI from a Git "
                "checkout, or set A38_LOCAL_CI_REVISION to the installed guard revision.",
            )

    def test_non_sha_pin_warns_without_comparing(self) -> None:
        warning = (
            "could not read a guard revision pin from "
            ".github/workflows/a38-guard.yml; no version comparison was possible"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for ref in ("main", "v1.2.3", "refs/heads/release"):
                with self.subTest(ref=ref):
                    repo = root / ref.replace(".", "-")
                    _write_guard_workflow(repo, ref)
                    self.assertEqual(guard_action_pins(repo), [])
                    with mock.patch(
                        "agent_cli.a38.local_ci_revision",
                        side_effect=AssertionError("revision must not be resolved"),
                    ):
                        note = ensure_local_ci_cli_satisfies_pin(repo, run=_forbid_run)
                    self.assertEqual(note, warning)

    def test_run_refuses_stale_cli_before_touching_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            # A real installation has its own source committed, which is what binds HEAD
            # to the code that runs. The stale revision is therefore a commit that already
            # carries the module.
            installation = root / "install"
            module = _installation_module(installation, git=True)
            ancestor = _git(installation, "rev-parse", "HEAD").lower()
            (installation / "README").write_text("two\n", encoding="utf-8")
            descendant = _commit_all(installation, "second")
            _git(installation, "checkout", "--detach", ancestor)
            _write_guard_workflow(repo, descendant)
            policy = root / "a38.json"
            policy.write_text(_policy_text(), encoding="utf-8")
            output = root / "report.md"
            sentinel = "sentinel-must-survive"
            output.write_text(sentinel, encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("A38_LOCAL_CI_REVISION", None)
                with mock.patch(
                    "agent_cli.a38.__file__",
                    str(module),
                ), contextlib.redirect_stderr(stderr):
                    code = main([
                        "run",
                        "--repo", str(repo),
                        "--policy", str(policy),
                        "--output", str(output),
                        "--logs-dir", str(root / "logs"),
                        "--base-sha", head,
                        "--public",
                    ])
            self.assertNotEqual(code, 0)
            printed = stderr.getvalue()
            self.assertTrue(printed.startswith("a38: "))
            self.assertIn("does not contain the guard revision", printed)
            self.assertEqual(output.read_text(encoding="utf-8"), sentinel)

    def test_checkout_revision_is_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertEqual(revision, _git(path, "rev-parse", "HEAD").lower())
            self.assertEqual(root, module.resolve().parents[2])

    def test_declared_revision_without_checkout_skips_git(self) -> None:
        """No provable checkout: the declaration stands in, and Git is never asked."""
        declared = "fedcba9876543210fedcba9876543210fedcba98"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=False)
            with mock.patch(
                "agent_cli.a38.subprocess.run",
                side_effect=AssertionError("git must not run"),
            ):
                revision, root = local_ci_revision(
                    env={"A38_LOCAL_CI_REVISION": declared},
                    module_file=module,
                )
            self.assertEqual(revision, declared)
            self.assertIsNone(root)

    def test_declared_revision_cannot_override_head(self) -> None:
        """With a checkout, HEAD decides: exporting the pin must not satisfy the check."""
        declared = "fedcba9876543210fedcba9876543210fedcba98"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            with self.assertRaisesRegex(A38Error, "A38_LOCAL_CI_REVISION"):
                local_ci_revision(
                    env={"A38_LOCAL_CI_REVISION": declared},
                    module_file=module,
                )

    def test_declared_revision_matching_head_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            head = _git(path, "rev-parse", "HEAD").lower()
            revision, root = local_ci_revision(
                env={"A38_LOCAL_CI_REVISION": head},
                module_file=module,
            )
            self.assertEqual(revision, head)
            self.assertEqual(root, module.resolve().parents[2])

    def test_git_dir_in_the_environment_cannot_redirect_the_proof(self) -> None:
        """GIT_DIR replaces discovery even against an explicit -C, so it must be stripped."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            real_head = _git(path, "rev-parse", "HEAD").lower()
            decoy = Path(tmp) / "decoy"
            _init_repo(decoy)
            decoy_head = _git(decoy, "rev-parse", "HEAD").lower()
            self.assertNotEqual(real_head, decoy_head)
            with mock.patch.dict(
                os.environ, {"GIT_DIR": str(decoy / ".git")}, clear=False
            ):
                revision, root = local_ci_revision(env={}, module_file=module)
            self.assertEqual(revision, real_head)
            self.assertEqual(root, module.resolve().parents[2])

    def test_gitdir_pointer_file_counts_as_checkout(self) -> None:
        """A worktree or submodule carries .git as a pointer file, not a directory."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            head = _git(path, "rev-parse", "HEAD").lower()
            marker = path / ".git"
            moved = Path(tmp) / "gitdir"
            marker.rename(moved)
            marker.write_text(f"gitdir: {moved}\n", encoding="utf-8")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertEqual(revision, head)
            self.assertEqual(root, module.resolve().parents[2])

    def test_edited_source_proves_nothing(self) -> None:
        """HEAD describes committed code; edited code is not at any revision."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            module.write_text("# placeholder\n# local edit\n", encoding="utf-8")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_a_checkout_during_the_check_proves_nothing(self) -> None:
        """A revision is only returned when it still is HEAD after the source was measured."""
        module_under_test = importlib.import_module("agent_cli.a38")
        real_run = module_under_test._default_run
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            head_reads: list[str] = []

            def moving_head(
                argv: list[str],
                cwd: Path | None,
                env: object,
            ) -> subprocess.CompletedProcess[str]:
                completed = real_run(argv, cwd, env)  # type: ignore[arg-type]
                if argv[-2:] == ["rev-parse", "HEAD"]:
                    head_reads.append(completed.stdout.strip())
                    if len(head_reads) == 2:
                        # As if another process checked out a descendant in between.
                        return subprocess.CompletedProcess(argv, 0, HEAD_B + "\n", "")
                return completed

            with mock.patch.object(module_under_test, "_default_run", moving_head):
                revision, root = local_ci_revision(env={}, module_file=module)
            self.assertEqual(len(head_reads), 2)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_an_edit_elsewhere_in_the_package_proves_nothing(self) -> None:
        """The blob check covers this module; the package check covers its siblings."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            sibling = module.parent / "chain.py"
            sibling.write_text("# committed\n", encoding="utf-8")
            _commit_all(path, "add sibling")
            sibling.write_text("# committed\n# local edit\n", encoding="utf-8")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_a_skip_worktree_edit_proves_nothing(self) -> None:
        """The index can be told to call a modified file clean; its content cannot."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            sibling = module.parent / "chain.py"
            sibling.write_text("# committed\n", encoding="utf-8")
            _commit_all(path, "add sibling")
            subprocess.run(
                ["git", "update-index", "--skip-worktree", "src/agent_cli/chain.py"],
                cwd=path,
                check=True,
                capture_output=True,
            )
            sibling.write_text("# committed\n# hidden edit\n", encoding="utf-8")
            # The blind spot this guards against: porcelain reports nothing.
            self.assertEqual(_git(path, "status", "--porcelain"), "")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_an_untracked_package_module_proves_nothing(self) -> None:
        """A module the revision does not carry is still imported from this package."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            (module.parent / "extra.py").write_text("# not committed\n", encoding="utf-8")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_a_clean_filter_cannot_launder_an_edit(self) -> None:
        """``hash-object`` runs clean filters by default; this proof must not."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            (path / ".gitattributes").write_text(
                "src/agent_cli/a38.py filter=launder\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "config", "filter.launder.clean", "sed -e 's/ LOCAL//'"],
                cwd=path,
                check=True,
                capture_output=True,
            )
            module.write_text("# placeholder LOCAL\n", encoding="utf-8")
            # The blind spot this guards against: filtered hashing returns the committed blob
            # for an edited file, so a comparison that honours filters would accept it.
            self.assertEqual(
                _git(path, "hash-object", "--", "src/agent_cli/a38.py"),
                _git(path, "rev-parse", "HEAD:src/agent_cli/a38.py"),
            )
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_a_replacement_object_cannot_forge_the_tree(self) -> None:
        """A replace ref is repository-local and survives the GIT_* sanitisation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            head = _git(path, "rev-parse", "HEAD")
            parent = _git(path, "rev-parse", "HEAD^")
            module.write_text("# tampered\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "src/agent_cli/a38.py"],
                cwd=path,
                check=True,
                capture_output=True,
            )
            forged_tree = _git(path, "write-tree")
            subprocess.run(["git", "reset", "-q"], cwd=path, check=True, capture_output=True)
            forged = _git(path, "commit-tree", forged_tree, "-p", parent, "-m", "forged")
            subprocess.run(
                ["git", "replace", head, forged], cwd=path, check=True, capture_output=True
            )
            # The blind spot: with replacement honoured, the revision appears to carry the
            # tampered tree, so the content compare would succeed against the edited file.
            self.assertNotEqual(
                _git(path, "ls-tree", "-r", head, "--", "src/agent_cli"),
                _git(path, "--no-replace-objects", "ls-tree", "-r", head, "--", "src/agent_cli"),
            )
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_a_pointer_into_another_repository_proves_nothing(self) -> None:
        """A copied tree can point at a foreign Git directory, whose HEAD is not its own."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            own_head = _git(path, "rev-parse", "HEAD").lower()
            foreign = Path(tmp) / "foreign"
            _init_repo(foreign)
            foreign_head = _git(foreign, "rev-parse", "HEAD").lower()
            self.assertNotEqual(own_head, foreign_head)
            # Point this tree's .git at the other repository, as a stale copy would.
            marker = path / ".git"
            shutil.rmtree(marker)
            marker.write_text(f"gitdir: {foreign / '.git'}\n", encoding="utf-8")
            revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_git_error_yields_no_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installation"
            module = _installation_module(path, git=True)
            with mock.patch(
                "agent_cli.a38.subprocess.run",
                side_effect=subprocess.TimeoutExpired("git", 10),
            ):
                revision, root = local_ci_revision(env={}, module_file=module)
            self.assertIsNone(revision)
            self.assertEqual(root, module.resolve().parents[2])

    def test_every_uses_pin_is_collected_in_document_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            # HEAD_B comes first so that sorting the pins would disagree with document
            # order. A double-quoted scalar, a trailing comment and a repeat that has to
            # collapse to one pin are covered here too.
            workflow.write_text(
                _guard_workflow_text(
                    "      - uses: DFXswiss/agent/.github/actions/a38-guard@"
                    f"{HEAD_B}\n"
                    '      - uses: "DFXswiss/agent/.github/actions/a38-guard@'
                    f'{HEAD_A}"  # pinned\n'
                    "      - uses: DFXswiss/agent/.github/actions/a38-guard@"
                    f"{HEAD_B}\n"
                ),
                encoding="utf-8",
            )
            self.assertEqual(guard_action_pins(repo), [HEAD_B, HEAD_A])

    def test_a_uses_outside_a_step_is_not_a_pin(self) -> None:
        # Only `jobs.<id>.steps[*].uses` installs a composite action. A job-level `uses`
        # names a reusable workflow, and a `uses` key under `env` or `with` is just data;
        # treating either as installed would refuse over a pin Actions never runs.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: A38 guard\n"
                "on:\n"
                "  workflow_dispatch:\n"
                "jobs:\n"
                "  called:\n"
                "    uses: DFXswiss/agent/.github/workflows/reusable.yml@"
                f"{HEAD_A}\n"
                "  guard:\n"
                "    runs-on: ubuntu-latest\n"
                "    env:\n"
                "      uses: DFXswiss/agent/.github/actions/a38-guard@"
                f"{HEAD_A}\n"
                "    steps:\n"
                "      - uses: DFXswiss/agent/.github/actions/a38-guard@"
                f"{HEAD_B}\n"
                "        with:\n"
                "          uses: DFXswiss/agent/.github/actions/a38-guard@"
                f"{HEAD_A}\n",
                encoding="utf-8",
            )
            self.assertEqual(guard_action_pins(repo), [HEAD_B])

    def test_an_uppercase_revision_is_the_same_pin(self) -> None:
        # Git resolves a 40-hex SHA in either case to the same commit, so an upper-case pin
        # installs the same guard; missing it would wave an older CLI through. It is
        # normalised, so the comparison and dedup see one revision.
        with tempfile.TemporaryDirectory() as tmp:
            # Upper case alone, so that rejecting it cannot be masked by a lower-case pin
            # sitting in the same workflow.
            repo = Path(tmp) / "repo"
            _write_guard_workflow(repo, HEAD_A.upper())
            self.assertEqual(guard_action_pins(repo), [HEAD_A])
            # The same revision written both ways is still one pin.
            mixed = Path(tmp) / "mixed"
            _write_guard_workflow(mixed, HEAD_A.upper(), HEAD_A)
            self.assertEqual(guard_action_pins(mixed), [HEAD_A])

    def test_the_action_path_inside_the_repository_stays_case_sensitive(self) -> None:
        # Owner and repository are case-insensitive on GitHub, but the path to the action is
        # a file path. A wrong-case path installs nothing, so counting it as a pin would
        # refuse over a revision that is not installed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                _guard_workflow_text(
                    "      - uses: DFXswiss/agent/.github/actions/A38-GUARD@"
                    f"{HEAD_A}\n"
                ),
                encoding="utf-8",
            )
            self.assertEqual(guard_action_pins(repo), [])

    def test_the_repository_part_of_the_reference_is_case_insensitive(self) -> None:
        # GitHub resolves owner/repository case-insensitively, so a lower-case reference
        # installs the same action; missing it would wave an older CLI through.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                _guard_workflow_text(
                    "      - uses: dfxswiss/Agent/.github/actions/a38-guard@"
                    f"{HEAD_A}\n"
                ),
                encoding="utf-8",
            )
            self.assertEqual(guard_action_pins(repo), [HEAD_A])

    def test_an_unparseable_workflow_yields_no_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("jobs:\n  guard:\n   - [unbalanced\n", encoding="utf-8")
            self.assertEqual(guard_action_pins(repo), [])

    def test_a_revision_outside_a_uses_value_is_not_a_pin(self) -> None:
        # A bumped pin leaves the old SHA behind in a comment, and a run: block scalar can
        # quote one too. Neither is what Actions installs. A quoted "uses" key is still a
        # uses key, so the parsed document decides rather than the file text.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workflow = repo / ".github" / "workflows" / "a38-guard.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                _guard_workflow_text(
                    "      - name: note\n"
                    "        run: |\n"
                    "          echo 'uses: DFXswiss/agent/.github/actions/a38-guard@"
                    f"{HEAD_A}'\n"
                    '      - "uses": DFXswiss/agent/.github/actions/a38-guard@'
                    f"{HEAD_B}\n"
                ),
                encoding="utf-8",
            )
            self.assertEqual(guard_action_pins(repo), [HEAD_B])

    def test_a_graft_cannot_forge_the_ancestry(self) -> None:
        """`--no-replace-objects` covers refs/replace but not the legacy grafts file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            beside = _commit_beside(checkout, ancestor)
            repo = root / "repo"
            _write_guard_workflow(repo, beside)
            info = checkout / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "grafts").write_text(f"{descendant} {beside}\n", encoding="utf-8")
            # The blind spot this guards against: through the graft, Git itself reports the
            # divergent pin as an ancestor, and the flag that stops replace refs does not help.
            forged = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "--no-replace-objects",
                    "merge-base",
                    "--is-ancestor",
                    beside,
                    descendant,
                ],
                capture_output=True,
            )
            self.assertEqual(forged.returncode, 0)
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo)
            self.assertIn("rewrites Git history", str(caught.exception))

    def test_a_later_pin_not_contained_is_refused(self) -> None:
        # Passing the first pin says nothing about a second step on another revision. The
        # second pin is a real commit, so this exercises the exit code 1 branch rather
        # than the unknown-object branch, and the message is asserted in full to tell the
        # two refusals apart.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            beside = _commit_beside(checkout, ancestor)
            repo = root / "repo"
            _write_guard_workflow(repo, ancestor, beside)
            self.assertEqual(guard_action_pins(repo), [ancestor, beside])
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo)
            self.assertEqual(
                str(caught.exception),
                f"this agent installation ({descendant}) does not contain the guard "
                f"revision {beside} that this repository installs; a report written now "
                "would be rejected with 'report outcome does not match the pull "
                f"request'. Update the CLI to a revision that contains {beside}.",
            )

    def test_an_uncontained_pin_is_refused_even_when_it_is_not_last(self) -> None:
        # The predated pin comes first and a satisfied pin follows, so a loop that only
        # trusts exit code 1 on the last pin would fall through to the wrong refusal. The
        # message is asserted in full, because both refusals name the same pin.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            beside = _commit_beside(checkout, ancestor)
            repo = root / "repo"
            _write_guard_workflow(repo, beside, ancestor)
            self.assertEqual(guard_action_pins(repo), [beside, ancestor])
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo)
            self.assertEqual(
                str(caught.exception),
                f"this agent installation ({descendant}) does not contain the guard "
                f"revision {beside} that this repository installs; a report written now "
                "would be rejected with 'report outcome does not match the pull "
                f"request'. Update the CLI to a revision that contains {beside}.",
            )

    def test_a_failing_first_pin_is_not_rescued_by_a_later_one(self) -> None:
        # The failing pin is first and a passing one follows, so a loop that only checks
        # the last pin, or that lets a later success overwrite an earlier refusal, accepts.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            ancestor, descendant = _init_two_commit_checkout(checkout)
            repo = root / "repo"
            _write_guard_workflow(repo, HEAD_B, ancestor)
            self.assertEqual(guard_action_pins(repo), [HEAD_B, ancestor])
            with mock.patch(
                "agent_cli.a38.local_ci_revision",
                return_value=(descendant, checkout),
            ):
                with self.assertRaises(A38Error) as caught:
                    ensure_local_ci_cli_satisfies_pin(repo)
            self.assertEqual(
                str(caught.exception),
                f"cannot prove this agent installation ({descendant}) is at or after the guard "
                f"revision {HEAD_B}; that commit is not present in this checkout. Fetch it and "
                "run again.",
            )

    def test_shallow_module_path_is_unknown(self) -> None:
        self.assertEqual(
            local_ci_revision(env={}, module_file=Path("/a38.py")),
            (None, None),
        )


class RunnerTests(unittest.TestCase):
    def test_fork_report_targets_base_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo, origin="https://github.com/contributor/fork.git")
            (root / 'github-accounts.json').write_text(json.dumps({
                'accounts': {'configured': {'login': 'ExampleWorker', 'gh_config_dir': '/test/github'}},
                'sessions': {'visibility': 'configured'},
            }))
            calls = []

            def lookup(argv, cwd, env):
                if argv[0] == 'env':
                    self.assertIn('GH_CONFIG_DIR=/test/github', argv)
                    command = argv[argv.index('gh'):]
                    calls.append(command)
                    output = 'ExampleWorker' if command[:3] == ['gh', 'api', 'user'] else '{"isPrivate":true}'
                    return subprocess.CompletedProcess(argv, 0, output, "")
                return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)

            output = root / "report.md"
            verdict = run_policy(
                repo, _policy_dict(), output=output, logs_dir=root / "logs",
                base_sha=head, repository="example/app", run=lookup,
                github_session='visibility', config_home=root,
            )
            self.assertTrue(verdict["ok"])
            self.assertEqual(calls, [
                ['gh', 'api', 'user', '--jq', '.login'],
                ["gh", "repo", "view", "example/app", "--json", "isPrivate"],
            ])
            self.assertTrue(output.read_text().startswith("EN:\n"))
            self.assertIn("<details>\n<summary>Details</summary>\n\n", output.read_text())
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

    def test_compose_preflight_fails_before_any_job_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            head = _init_repo(repo)
            policy = _policy_dict(jobs=[_compose_executor_job()])
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
                "agent_cli.a38.shutil.which", return_value="/usr/bin/agent"
            ), mock.patch(
                "agent_cli.a38._run_one_job",
                side_effect=AssertionError("job must not run"),
            ):
                os.environ.pop("EXAMPLE_SERVICES_DIR", None)
                with self.assertRaises(A38Error) as raised:
                    run_policy(
                        repo,
                        policy,
                        output=root / "report.md",
                        logs_dir=logs,
                        base_sha=head,
                        private=True,
                    )
            self.assertIn("EXAMPLE_SERVICES_DIR", str(raised.exception))
            self.assertEqual(list(logs.iterdir()), [])

    def test_compose_preflight_accepts_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = _policy_dict(jobs=[_compose_executor_job()])
            with mock.patch.dict(
                os.environ, {"EXAMPLE_SERVICES_DIR": "/tmp/services"}, clear=False
            ), mock.patch(
                "agent_cli.a38.shutil.which", return_value="/usr/bin/agent"
            ), mock.patch(
                "agent_cli.a38._run_one_job", return_value=("pass", 0, 0.1)
            ) as run_job:
                verdict = run_policy(
                    repo,
                    policy,
                    output=root / "report.md",
                    logs_dir=root / "logs",
                    base_sha=head,
                    private=True,
                )
            self.assertTrue(verdict["ok"])
            run_job.assert_called_once()

    def test_compose_preflight_accepts_configured_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = _policy_dict(
                jobs=[
                    _compose_executor_job(
                        env={"EXAMPLE_SERVICES_DIR": "/configured/services"}
                    )
                ]
            )
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
                "agent_cli.a38.shutil.which", return_value="/usr/bin/agent"
            ), mock.patch(
                "agent_cli.a38._run_one_job", return_value=("pass", 0, 0.1)
            ) as run_job:
                os.environ.pop("EXAMPLE_SERVICES_DIR", None)
                verdict = run_policy(
                    repo,
                    policy,
                    output=root / "report.md",
                    logs_dir=root / "logs",
                    base_sha=head,
                    private=True,
                )
            self.assertTrue(verdict["ok"])
            run_job.assert_called_once()

    def test_compose_preflight_honors_configured_environment_removal(self) -> None:
        for removal in (
            {"unset": ["EXAMPLE_SERVICES_DIR"]},
            {"unset_prefixes": ["EXAMPLE_SERVICES_"]},
        ):
            with self.subTest(removal=removal), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                head = _init_repo(repo)
                policy = _policy_dict(jobs=[_compose_executor_job(**removal)])
                with mock.patch.dict(
                    os.environ, {"EXAMPLE_SERVICES_DIR": "/tmp/services"}, clear=False
                ), mock.patch(
                    "agent_cli.a38.shutil.which", return_value="/usr/bin/agent"
                ), mock.patch(
                    "agent_cli.a38._run_one_job",
                    side_effect=AssertionError("job must not run"),
                ):
                    with self.assertRaises(A38Error) as raised:
                        run_policy(
                            repo,
                            policy,
                            output=root / "report.md",
                            logs_dir=root / "logs",
                            base_sha=head,
                            private=True,
                        )
                self.assertIn("EXAMPLE_SERVICES_DIR", str(raised.exception))

    def test_agent_executable_is_required_only_for_executor_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            executor_policy = _policy_dict(
                jobs=[
                    {
                        "id": "commands",
                        "name": "Commands",
                        "executor": {
                            "adapter": "commands",
                            "config": {"steps": [["true"]]},
                        },
                        "timeout_s": 30,
                        "workflow": ".github/workflows/ci.yml",
                        "job": "commands",
                    }
                ]
            )
            with mock.patch("agent_cli.a38.shutil.which", return_value=None):
                with self.assertRaisesRegex(
                    A38Error,
                    re.escape(
                        'agent executable not found on PATH; executor jobs run '
                        '"agent a38 job ..."'
                    ),
                ):
                    run_policy(
                        repo,
                        executor_policy,
                        output=root / "executor-report.md",
                        logs_dir=root / "executor-logs",
                        base_sha=head,
                        private=True,
                    )

            with mock.patch("agent_cli.a38.shutil.which", return_value=None) as which, mock.patch(
                "agent_cli.a38._run_one_job", return_value=("pass", 0, 0.1)
            ):
                verdict = run_policy(
                    repo,
                    _policy_dict(),
                    output=root / "command-report.md",
                    logs_dir=root / "command-logs",
                    base_sha=head,
                    private=True,
                )
            self.assertTrue(verdict["ok"])
            which.assert_not_called()

    def test_readme_only_omitted_compose_skips_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = _init_repo(repo)
            (repo / "README.md").write_text("docs\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "readme",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            policy = _policy_dict(
                jobs=[_compose_executor_job()],
                readme_only={"omit_jobs": ["compose"]},
            )
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
                "agent_cli.a38.shutil.which", return_value=None
            ) as which, mock.patch(
                "agent_cli.a38._run_one_job",
                side_effect=AssertionError("omitted job must not run"),
            ):
                os.environ.pop("EXAMPLE_SERVICES_DIR", None)
                verdict = run_policy(
                    repo,
                    policy,
                    output=root / "report.md",
                    logs_dir=root / "logs",
                    base_sha=base,
                    private=True,
                )
            self.assertTrue(verdict["ok"])
            which.assert_not_called()

    def test_cli_preflight_invalidates_stale_success(self) -> None:
        for failure in ("missing", "malformed", "base", "repository", "visibility"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                head = _init_repo(repo)
                source = root / "policy.json"
                if failure != "missing":
                    source.write_text("{" if failure == "malformed" else _policy_text())
                output = root / "report.md"
                output.write_text("stale-success")
                args = ["run", "--repo", str(repo), "--policy", str(source),
                        "--output", str(output), "--logs-dir", str(root / "logs"),
                        "--base-sha", "missing" if failure == "base" else head, "--public"]
                if failure == "repository":
                    args += ["--repository", "invalid"]
                if failure == "visibility":
                    args += ["--private"]
                self.assertEqual(main(args), 1)
                self.assertFalse(output.exists())

    def test_cli_unsafe_destination_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            source = root / "policy.json"
            source.write_text("malformed policy, must survive")
            for output in (source, repo / "tracked.txt"):
                output.write_text("must survive")
                self.assertEqual(main([
                    "run", "--repo", str(repo), "--policy", str(source),
                    "--output", str(output), "--logs-dir", str(root / "logs"),
                    "--base-sha", head, "--public",
                ]), 1)
                self.assertEqual(output.read_text(), "must survive")

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
            self.assertTrue(output.read_text().startswith("EN:\n"))
            self.assertTrue(output.read_text().endswith("\n</details>\n"))
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

    def test_interrupt_after_last_pass_invalidates_report(self) -> None:
        # First clean-tree call is preflight, second follows the only job,
        # third is the final integrity check immediately before serialization.
        for interrupted_call in (2, 3):
            with self.subTest(call=interrupted_call), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                head = _init_repo(repo)
                output = root / "report.md"
                policy = _policy_dict()
                outcomes = [None] * (interrupted_call - 1) + [KeyboardInterrupt()]
                with mock.patch("agent_cli.a38._require_clean_tree", side_effect=outcomes):
                    verdict = run_policy(
                        repo, policy, output=output, logs_dir=root / "logs",
                        base_sha=head, private=True,
                    )
                self.assertFalse(verdict["ok"])
                report = parse_comment(output.read_text())
                self.assertEqual(len(report.runs), len(policy["jobs"]))
                self.assertNotEqual(report.runs[0].result, "pass")
                checked = verify_report(
                    output.read_text(), policy, repo=report.repo, head=head, private=True,
                )
                self.assertFalse(checked["ok"], "an interrupted run produced acceptable evidence")

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

    def test_readme_only_omits_configured_job(self) -> None:
        # README.md is also markdown-only, so the broader markdown rule omits
        # every required job. readme_only remains true when omit_jobs applied.
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            (repo / "README.md").write_text("docs\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "readme",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            policy = load_policy(
                _policy_text(jobs=two_jobs, readme_only={"omit_jobs": ["lint"]})
            )
            verdict = run_policy(
                repo,
                policy,
                output=root / "report.md",
                logs_dir=logs,
                base_sha=base,
                private=True,
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertTrue(report.readme_only)
            self.assertTrue(report.markdown_only)
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["unit"].result, "not_applicable")
            self.assertEqual(by_id["lint"].result, "not_applicable")
            self.assertEqual(by_id["lint"].exit_code, 0)
            self.assertEqual(by_id["lint"].duration_s, 0.0)
            for ident in ("unit", "lint"):
                text = (logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertEqual(text, "omitted: markdown-only change set\n")

    def test_interrupt_after_readme_only_omit_invalidates_report(self) -> None:
        # Preflight is the first _require_clean_tree call; omit path skips
        # per-job checks, so the second call is the final integrity check.
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            (repo / "README.md").write_text("docs\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "readme",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head = _git(repo, "rev-parse", "HEAD").lower()
            output = root / "report.md"
            policy = load_policy(
                _policy_text(
                    jobs=two_jobs,
                    readme_only={"omit_jobs": ["unit", "lint"]},
                )
            )
            with mock.patch(
                "agent_cli.a38._require_clean_tree",
                side_effect=[None, KeyboardInterrupt()],
            ):
                verdict = run_policy(
                    repo,
                    policy,
                    output=output,
                    logs_dir=logs,
                    base_sha=base,
                    private=True,
                )
            self.assertFalse(verdict["ok"])
            report = parse_comment(output.read_text(encoding="utf-8"))
            self.assertEqual(len(report.runs), 2)
            for run in report.runs:
                self.assertNotIn(run.result, {"pass", "not_applicable"})
            checked = verify_report(
                output.read_text(encoding="utf-8"),
                policy,
                repo=report.repo,
                head=head,
                private=True,
            )
            self.assertFalse(checked["ok"], "an interrupted omit run produced acceptable evidence")

    def test_readme_only_mixed_files_runs_all_jobs(self) -> None:
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            (repo / "README.md").write_text("docs\n", encoding="utf-8")
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "mixed",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            policy = load_policy(
                _policy_text(jobs=two_jobs, readme_only={"omit_jobs": ["lint"]})
            )
            verdict = run_policy(
                repo,
                policy,
                output=root / "report.md",
                logs_dir=logs,
                base_sha=base,
                private=True,
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertFalse(report.readme_only)
            self.assertFalse(report.markdown_only)
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["unit"].result, "pass")
            self.assertEqual(by_id["lint"].result, "pass")
            for ident in ("unit", "lint"):
                text = (logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertNotIn("omitted:", text)

    def test_markdown_only_omits_all_jobs(self) -> None:
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            docs = repo / "docs"
            docs.mkdir()
            (docs / "guide.md").write_text("guide\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "docs/guide.md"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "markdown",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            policy = load_policy(_policy_text(jobs=two_jobs))
            verdict = run_policy(
                repo,
                policy,
                output=root / "report.md",
                logs_dir=logs,
                base_sha=base,
                private=True,
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertTrue(report.markdown_only)
            self.assertFalse(report.readme_only)
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["unit"].result, "not_applicable")
            self.assertEqual(by_id["lint"].result, "not_applicable")
            for ident in ("unit", "lint"):
                text = (logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertEqual(text, "omitted: markdown-only change set\n")

    def test_guard_docs_only_omits_all_jobs(self) -> None:
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            wf = repo / GUARD_WORKFLOW_PATH
            wf.parent.mkdir(parents=True)
            wf.write_text("name: guard\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", GUARD_WORKFLOW_PATH],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "guard docs",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            policy = load_policy(_policy_text(jobs=two_jobs))
            verdict = run_policy(
                repo,
                policy,
                output=root / "report.md",
                logs_dir=logs,
                base_sha=base,
                private=True,
            )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertFalse(report.markdown_only)
            self.assertFalse(report.readme_only)
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["unit"].result, "not_applicable")
            self.assertEqual(by_id["lint"].result, "not_applicable")
            for ident in ("unit", "lint"):
                text = (logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertEqual(text, "omitted: guard-docs change set\n")

    def test_single_git_inventory_second_listing_cannot_flip_omit(self) -> None:
        two_jobs = [
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            base = _init_repo(repo)
            calls = {"n": 0}

            def fake_changed(
                _root: Path, _base: str, _head: str
            ) -> list[str] | None:
                calls["n"] += 1
                if calls["n"] == 1:
                    return ["src/app.py"]
                return [GUARD_WORKFLOW_PATH]

            policy = load_policy(_policy_text(jobs=two_jobs))
            with mock.patch(
                "agent_cli.a38.git_changed_paths", side_effect=fake_changed
            ), mock.patch(
                "agent_cli.readme_only.git_changed_paths",
                side_effect=fake_changed,
            ):
                verdict = run_policy(
                    repo,
                    policy,
                    output=root / "report.md",
                    logs_dir=logs,
                    base_sha=base,
                    private=True,
                )
            self.assertEqual(calls["n"], 1)
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertFalse(report.markdown_only)
            by_id = {run.id: run for run in report.runs}
            self.assertEqual(by_id["unit"].result, "pass")
            self.assertEqual(by_id["lint"].result, "pass")
            for ident in ("unit", "lint"):
                text = (logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertNotIn("omitted:", text)

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


def _commands_lock_job(
    *,
    ident: str,
    gh_job: str,
    lock: str | None,
    timeout_s: float = 30,
) -> dict:
    config: dict = {"steps": [["true", ident]]}
    if lock is not None:
        config["lock"] = lock
    return {
        "id": ident,
        "name": ident,
        "executor": {"adapter": "commands", "config": config},
        "timeout_s": timeout_s,
        "workflow": ".github/workflows/ci.yml",
        "job": gh_job,
    }


class SchedulerTests(unittest.TestCase):
    def _run_with_sleeper(
        self,
        jobs: list[dict],
        *,
        sleep_s: float = 0.25,
        env: dict[str, str] | None = None,
        outcomes: dict[str, tuple[str, int, float]] | None = None,
    ) -> tuple[dict, float, list[tuple[str, str, float]]]:
        events: list[tuple[str, str, float]] = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = load_policy(_policy_text(jobs=jobs))
            ident_by_command = {str(job["command"]): str(job["id"]) for job in policy["jobs"]}

            def fake_run_one_job_mapped(**kwargs: object) -> tuple[str, int, float]:
                command = str(kwargs["command"])
                ident = ident_by_command.get(command, "unknown")
                t0 = time.monotonic()
                with lock:
                    events.append(("start", ident, t0))
                time.sleep(sleep_s)
                t1 = time.monotonic()
                with lock:
                    events.append(("end", ident, t1))
                if outcomes and ident in outcomes:
                    return outcomes[ident]
                return ("pass", 0, t1 - t0)

            extra_env = env or {}
            with mock.patch.dict(os.environ, extra_env, clear=False):
                with mock.patch("agent_cli.a38._run_one_job", side_effect=fake_run_one_job_mapped):
                    started = time.monotonic()
                    verdict = run_policy(
                        repo,
                        policy,
                        output=root / "report.md",
                        logs_dir=root / "logs",
                        base_sha=head,
                        private=True,
                    )
                    wall = time.monotonic() - started
        return verdict, wall, events

    def test_disjoint_lock_domains_overlap(self) -> None:
        jobs = [
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
            _commands_lock_job(ident="docker", gh_job="compose", lock="docker-heavy"),
        ]
        verdict, wall, events = self._run_with_sleeper(jobs, sleep_s=0.3)
        self.assertTrue(verdict["ok"], msg=verdict)
        starts = [item for item in events if item[0] == "start"]
        ends = [item for item in events if item[0] == "end"]
        self.assertEqual({item[1] for item in starts}, {"jest", "docker"})
        first_end = min(item[2] for item in ends)
        second_start = max(item[2] for item in starts)
        self.assertLess(second_start, first_end)

    def test_same_lock_does_not_overlap(self) -> None:
        jobs = [
            _commands_lock_job(ident="a", gh_job="a", lock="jest-full"),
            _commands_lock_job(ident="b", gh_job="b", lock="jest-full"),
        ]
        verdict, wall, events = self._run_with_sleeper(jobs, sleep_s=0.25)
        self.assertTrue(verdict["ok"], msg=verdict)
        starts = sorted((item[2], item[1]) for item in events if item[0] == "start")
        ends = {item[1]: item[2] for item in events if item[0] == "end"}
        first_id = starts[0][1]
        self.assertGreaterEqual(starts[1][0], ends[first_id])
        self.assertGreaterEqual(wall, 0.45)

    def test_command_and_jest_share_cpu_domain(self) -> None:
        jobs = [
            {
                "id": "lint",
                "name": "lint",
                "command": "true",
                "timeout_s": 30,
                "workflow": ".github/workflows/ci.yml",
                "job": "lint",
            },
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
        ]
        verdict, wall, events = self._run_with_sleeper(jobs, sleep_s=0.25)
        self.assertTrue(verdict["ok"], msg=verdict)
        starts = sorted((item[2], item[1]) for item in events if item[0] == "start")
        ends = {item[1]: item[2] for item in events if item[0] == "end"}
        first_id = starts[0][1]
        self.assertGreaterEqual(starts[1][0], ends[first_id])
        self.assertGreaterEqual(wall, 0.45)

    def test_max_in_flight_cap_prevents_overlap(self) -> None:
        jobs = [
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
            _commands_lock_job(ident="docker", gh_job="compose", lock="docker-heavy"),
        ]
        verdict, wall, events = self._run_with_sleeper(
            jobs, sleep_s=0.25, env={"A38_MAX_IN_FLIGHT": "1"}
        )
        self.assertTrue(verdict["ok"], msg=verdict)
        starts = sorted((item[2], item[1]) for item in events if item[0] == "start")
        ends = {item[1]: item[2] for item in events if item[0] == "end"}
        first_id = starts[0][1]
        self.assertGreaterEqual(starts[1][0], ends[first_id])
        self.assertGreaterEqual(wall, 0.45)

    def test_failure_still_runs_independent_domain(self) -> None:
        jobs = [
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
            _commands_lock_job(ident="docker", gh_job="compose", lock="docker-heavy"),
        ]
        verdict, _wall, events = self._run_with_sleeper(
            jobs,
            sleep_s=0.05,
            outcomes={"jest": ("fail", 1, 0.05)},
        )
        self.assertFalse(verdict["ok"])
        ran = {item[1] for item in events if item[0] == "start"}
        self.assertEqual(ran, {"jest", "docker"})

    def test_report_runs_follow_policy_order(self) -> None:
        jobs = [
            _commands_lock_job(ident="docker", gh_job="compose", lock="docker-heavy"),
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
        ]
        hold_docker = threading.Event()

        def fake_run_one_job(**kwargs: object) -> tuple[str, int, float]:
            command = str(kwargs["command"])
            if "docker-heavy" in command:
                hold_docker.wait(timeout=2)
                time.sleep(0.05)
                return ("pass", 0, 0.05)
            time.sleep(0.02)
            hold_docker.set()
            return ("pass", 0, 0.02)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = load_policy(_policy_text(jobs=jobs))
            with mock.patch("agent_cli.a38._run_one_job", side_effect=fake_run_one_job):
                verdict = run_policy(
                    repo,
                    policy,
                    output=root / "report.md",
                    logs_dir=root / "logs",
                    base_sha=head,
                    private=True,
                )
            self.assertTrue(verdict["ok"], msg=verdict)
            report = parse_comment((root / "report.md").read_text(encoding="utf-8"))
            self.assertEqual([run.id for run in report.runs], ["docker", "jest"])

    def test_head_drift_terminates_in_flight_peer(self) -> None:
        jobs = [
            _commands_lock_job(ident="jest", gh_job="jest", lock="jest-full"),
            _commands_lock_job(ident="docker", gh_job="compose", lock="docker-heavy"),
        ]
        terminated = {"n": 0}
        tree_calls = {"n": 0}

        def fake_run_one_job(**kwargs: object) -> tuple[str, int, float]:
            time.sleep(0.15)
            return ("pass", 0, 0.15)

        def fake_clean_tree(*args: object, **kwargs: object) -> None:
            tree_calls["n"] += 1
            if tree_calls["n"] >= 2:
                raise A38Error("dirty")

        def fake_terminate() -> None:
            terminated["n"] += 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = load_policy(_policy_text(jobs=jobs))
            with mock.patch("agent_cli.a38._run_one_job", side_effect=fake_run_one_job):
                with mock.patch("agent_cli.a38._require_clean_tree", side_effect=fake_clean_tree):
                    with mock.patch(
                        "agent_cli.a38._terminate_active_job_procs",
                        side_effect=fake_terminate,
                    ):
                        verdict = run_policy(
                            repo,
                            policy,
                            output=root / "report.md",
                            logs_dir=root / "logs",
                            base_sha=head,
                            private=True,
                        )
        self.assertFalse(verdict["ok"])
        self.assertGreaterEqual(terminated["n"], 1)

    def test_invalid_max_in_flight_raises_before_jobs(self) -> None:
        jobs = [_commands_lock_job(ident="jest", gh_job="jest", lock="jest-full")]
        called = {"n": 0}

        def fake_run_one_job(**kwargs: object) -> tuple[str, int, float]:
            called["n"] += 1
            return ("pass", 0, 0.01)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            head = _init_repo(repo)
            policy = load_policy(_policy_text(jobs=jobs))
            with mock.patch.dict(os.environ, {"A38_MAX_IN_FLIGHT": "0"}):
                with mock.patch("agent_cli.a38._run_one_job", side_effect=fake_run_one_job):
                    with self.assertRaises(A38Error) as ctx:
                        run_policy(
                            repo,
                            policy,
                            output=root / "report.md",
                            logs_dir=root / "logs",
                            base_sha=head,
                            private=True,
                        )
            self.assertIn("A38_MAX_IN_FLIGHT", str(ctx.exception))
            self.assertEqual(called["n"], 0)


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


@pytest.mark.parametrize('session', [None, 'unbound'])
def test_visibility_lookup_refuses_missing_account_before_network(tmp_path, monkeypatch, session):
    from agent_cli.a38 import _resolve_private
    monkeypatch.setenv('GH_TOKEN', 'synthetic-ambient-token')
    def forbidden(*args):
        pytest.fail('Unconfigured visibility lookup must not access GitHub')
    with pytest.raises(A38Error):
        _resolve_private(tmp_path, None, run=forbidden, repository='example/app',
                         github_session=session, config_home=tmp_path)
    assert _resolve_private(tmp_path, False, run=forbidden, repository='example/app') is False


@pytest.mark.parametrize('auth_result', ['WrongWorker', None])
def test_visibility_lookup_rejects_wrong_or_unavailable_account(tmp_path, auth_result):
    from agent_cli.a38 import _resolve_private
    (tmp_path / 'github-accounts.json').write_text(json.dumps({
        'accounts': {'selected': {'login': 'ExpectedWorker', 'gh_config_dir': '/test/selected'}},
        'sessions': {'chosen': 'selected'},
    }))
    calls = []
    def runner(argv, cwd, env):
        command = argv[argv.index('gh'):]
        calls.append(command)
        assert command == ['gh', 'api', 'user', '--jq', '.login']
        return subprocess.CompletedProcess(argv, 1 if auth_result is None else 0,
                                           auth_result or '', 'synthetic-private-error')
    with pytest.raises(A38Error) as exc:
        _resolve_private(tmp_path, None, run=runner, repository='example/app',
                         github_session='chosen', config_home=tmp_path)
    assert len(calls) == 1
    assert 'synthetic-private-error' not in str(exc.value)
