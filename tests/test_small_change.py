"""Unit and git-backed tests for the deterministic small-change verdict."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import pytest

    pytestmark = pytest.mark.no_pg
except ImportError:
    pass

from agent_cli.small_change import (
    EVIDENCE_PREFIX,
    FileStat,
    check_repo,
    evaluate,
    evidence_head,
    excluded_reason,
)


def _git(cwd: Path, *parts: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *parts],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _write(path: Path, name: str, content: str | bytes) -> None:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _commit(path: Path, message: str) -> str:
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


def _init_repo(path: Path, files: dict[str, str | bytes] | None = None) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "small@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Small Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    for name, content in (files or {"base.txt": "base\n"}).items():
        _write(path, name, content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    return _commit(path, "init")


def _default_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD")


def _checkout(path: Path, branch: str, *, create: bool = False) -> None:
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _branch_head(path: Path, branch: str) -> str:
    return _git(path, "rev-parse", branch).lower()


class ExcludedReasonTests(unittest.TestCase):
    def test_every_class(self) -> None:
        cases = [
            (".github/workflows/ci.yml", "workflow"),
            (".github/a38.json", "workflow"),
            (".github/pr-guard.json", "workflow"),
            ("package.json", "dependencies"),
            ("package-lock.json", "dependencies"),
            ("Dockerfile", "infrastructure"),
            ("docker-compose.yml", "infrastructure"),
            ("main.tf", "infrastructure"),
            (".env.production", "infrastructure"),
            ("infra/x.yml", "infrastructure"),
            ("migrations/001.ts", "migrations"),
            ("x.sql", "migrations"),
            ("src/auth/login.ts", "security or money"),
            ("src/allowGuard.ts", "security or money"),
            ("src/payout/service.ts", "security or money"),
        ]
        for path, label in cases:
            reason = excluded_reason(path, [])
            self.assertIsNotNone(reason, path)
            assert reason is not None
            self.assertIn(path, reason)
            self.assertIn(label, reason)

    def test_allowed_paths(self) -> None:
        for path in (
            "README.md",
            "src/agent_cli/readme_only.py",
            "docs/a38.md",
            "plugins/dfx/skills/orchestration/SKILL.md",
        ):
            self.assertIsNone(excluded_reason(path, []), path)

    def test_extra_globs(self) -> None:
        self.assertIsNotNone(excluded_reason("src/core/x.py", ["src/core/*"]))
        self.assertIsNotNone(excluded_reason("vendor/lib/x.py", ["vendor/"]))
        self.assertIsNone(excluded_reason("src/app.py", ["src/core/*", "vendor/"]))

    def test_tokenizer(self) -> None:
        for path in (
            "JWTAuth.ts",
            "parseJWTToken.ts",
            "apiKey.ts",
            "src/login.ts",
        ):
            self.assertIsNotNone(excluded_reason(path, []), path)
        for path in (
            "CONTRIBUTING.md",
            "REVIEW.md",
            ".envrc",
            ".github/dependabot.yml",
        ):
            self.assertIsNotNone(excluded_reason(path, []), path)


class EvaluateTests(unittest.TestCase):
    def test_empty_diff(self) -> None:
        self.assertEqual(evaluate([], [], []), ["empty diff"])

    def test_too_many_files(self) -> None:
        stats = [FileStat(f"f{i}.py", 1, 0) for i in range(4)]
        paths = [item.path for item in stats]
        self.assertIn("4 files > 3", evaluate(stats, paths, []))

    def test_binary_file(self) -> None:
        reasons = evaluate([FileStat("image.png", None, None)], ["image.png"], [])
        self.assertIn("binary file image.png", reasons)

    def test_too_many_lines(self) -> None:
        reasons = evaluate([FileStat("x.py", 16, 15)], ["x.py"], [])
        self.assertIn("31 changed lines > 30", reasons)

    def test_exactly_at_the_limits(self) -> None:
        stats = [
            FileStat("a.py", 10, 0),
            FileStat("b.py", 10, 0),
            FileStat("c.py", 10, 0),
        ]
        self.assertEqual(evaluate(stats, [item.path for item in stats], []), [])

    def test_several_reasons_are_all_reported(self) -> None:
        stats = [
            FileStat("a.py", 31, 0),
            FileStat("b.bin", None, None),
            FileStat("f0.py", 1, 1),
            FileStat("f1.py", 1, 1),
        ]
        paths = [item.path for item in stats]
        reasons = evaluate(stats, paths, [])
        self.assertIn("4 files > 3", reasons)
        self.assertIn("binary file b.bin", reasons)
        self.assertIn("35 changed lines > 30", reasons)

    def test_stats_without_paths_reports_files_and_binary(self) -> None:
        stats = [
            FileStat("a.py", 1, 0),
            FileStat("b.py", 1, 0),
            FileStat("c.py", 1, 0),
            FileStat("d.bin", None, None),
        ]
        reasons = evaluate(stats, None, [])
        self.assertIn("4 files > 3", reasons)
        self.assertIn("binary file d.bin", reasons)

    def test_paths_without_stats_reports_files_and_no_line_reason(self) -> None:
        reasons = evaluate(None, ["a.py", "b.py", "c.py", "d.py"], [])
        self.assertIn("4 files > 3", reasons)
        self.assertFalse(any("line" in reason for reason in reasons))

    def test_paths_without_stats_reports_exclusion(self) -> None:
        reasons = evaluate(None, [".github/workflows/ci.yml"], [])
        self.assertIn(
            "excluded path .github/workflows/ci.yml (workflow)", reasons
        )


class EvidenceHeadTests(unittest.TestCase):
    def test_valid(self) -> None:
        sha = "a" * 40
        self.assertEqual(evidence_head(EVIDENCE_PREFIX + sha), sha)
        self.assertEqual(evidence_head(EVIDENCE_PREFIX + sha + " and more"), sha)
        self.assertEqual(evidence_head(EVIDENCE_PREFIX + sha + "; note"), sha)

    def test_invalid(self) -> None:
        self.assertIsNone(evidence_head(None))
        self.assertIsNone(evidence_head("no prefix " + "a" * 40))
        self.assertIsNone(evidence_head(EVIDENCE_PREFIX + "A" * 40))
        self.assertIsNone(evidence_head(EVIDENCE_PREFIX + "a" * 39))
        self.assertIsNone(evidence_head(EVIDENCE_PREFIX + "a" * 40 + "x"))


def _add_commit(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    return _commit(path, message)


class CheckRepoTests(unittest.TestCase):
    def test_small_change_on_a_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "1\n2\n3\n")
            _write(repo, "b.py", "1\n2\n")
            head = _add_commit(repo, "small")
            verdict = check_repo(repo, base, head)
            self.assertEqual(verdict.verdict, "small")
            self.assertEqual(verdict.lines, 5)
            self.assertEqual(verdict.merge_base, base)
            self.assertEqual(verdict.reasons, ())

    def test_uses_merge_base_not_two_dot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            default = _default_branch(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "1\n2\n3\n4\n5\n")
            head = _add_commit(repo, "five lines")
            _checkout(repo, default)
            _write(repo, "base.txt", "".join(f"line {i}\n" for i in range(40)))
            advanced_base = _add_commit(repo, "advance base")
            _checkout(repo, "feature")
            verdict = check_repo(repo, advanced_base, head)
            self.assertEqual(verdict.verdict, "small")
            self.assertEqual(verdict.lines, 5)
            self.assertEqual(verdict.merge_base, base)

    def test_rename_counts_as_two_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo, {"a.py": "".join(f"line {i}\n" for i in range(20))})
            _checkout(repo, "feature", create=True)
            _git(repo, "mv", "a.py", "b.py")
            head = _add_commit(repo, "rename")
            verdict = check_repo(repo, base, head)
            self.assertEqual(verdict.verdict, "not-small")
            self.assertEqual(len(verdict.files), 2)
            self.assertEqual(verdict.lines, 40)
            self.assertTrue(any("changed lines > 30" in r for r in verdict.reasons))
            self.assertFalse(any("files > 3" in r for r in verdict.reasons))

    def test_rename_plus_two_files_is_too_many_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo, {"a.py": "one\n", "c.py": "c\n", "d.py": "d\n"})
            _checkout(repo, "feature", create=True)
            _git(repo, "mv", "a.py", "b.py")
            _write(repo, "c.py", "c\nmore\n")
            _write(repo, "d.py", "d\nmore\n")
            head = _add_commit(repo, "rename and edits")
            verdict = check_repo(repo, base, head)
            self.assertEqual(len(verdict.files), 4)
            self.assertIn("4 files > 3", verdict.reasons)

    def test_dirty_tracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "one\n")
            head = _add_commit(repo, "small")
            _write(repo, "base.txt", "dirty\n")
            verdict = check_repo(repo, base, head)
            self.assertIn("working tree is not clean; commit first", verdict.reasons)

    def test_untracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "one\n")
            head = _add_commit(repo, "small")
            _write(repo, "untracked.txt", "new\n")
            verdict = check_repo(repo, base, head)
            self.assertIn("working tree is not clean; commit first", verdict.reasons)

    def test_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "image.bin", b"\x00\x01\x02\n")
            head = _add_commit(repo, "binary")
            verdict = check_repo(repo, base, head)
            self.assertEqual(verdict.verdict, "not-small")
            self.assertIn("binary file image.bin", verdict.reasons)

    def test_base_sha_not_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "one\n")
            head = _add_commit(repo, "small")
            missing = "f" * 40
            verdict = check_repo(repo, missing, head)
            self.assertTrue(
                any("is not available locally; fetch the base branch" in r for r in verdict.reasons)
            )

    def test_local_head_must_equal_the_pull_request_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "one\n")
            head = _add_commit(repo, "small")
            expected = "f" * 40
            verdict = check_repo(repo, base, expected)
            self.assertTrue(
                any(f"local HEAD {head} is not the pull request head {expected}" in r for r in verdict.reasons)
            )

    def test_config_on_head_only_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            _checkout(repo, "feature", create=True)
            _write(repo, "src/core/x.py", "one\n")
            _write(
                repo,
                ".github/small-change.json",
                '{"excluded_paths": ["src/core/*"]}\n',
            )
            head = _add_commit(repo, "config on head")
            verdict = check_repo(repo, base, head)
            self.assertFalse(
                any("excluded by .github/small-change.json" in r for r in verdict.reasons)
            )
            self.assertFalse(
                any("excluded path src/core/x.py" in r for r in verdict.reasons)
            )
            self.assertTrue(
                any(
                    "excluded path .github/small-change.json (workflow)" in r
                    for r in verdict.reasons
                )
            )

    def test_config_on_base_adds_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(
                repo,
                {
                    "base.txt": "base\n",
                    ".github/small-change.json": '{"excluded_paths": ["src/core/*"]}\n',
                },
            )
            _checkout(repo, "feature", create=True)
            _write(repo, "src/core/x.py", "one\n")
            head = _add_commit(repo, "excluded path")
            verdict = check_repo(repo, base, head)
            self.assertIn(
                "excluded path src/core/x.py (excluded by .github/small-change.json)",
                verdict.reasons,
            )

    def test_submodule_pointer_is_counted_when_diff_ignores_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_repo(repo)
            source = root / "lib"
            _init_repo(source, {"lib.txt": "lib\n"})
            probe = subprocess.run(
                ["git", "-C", str(repo), "submodule", "status"],
                capture_output=True,
            )
            if probe.returncode != 0:
                pytest.skip("git submodule is unavailable")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(source),
                    "vendor/lib",
                ],
                check=True,
                capture_output=True,
            )
            base = _add_commit(repo, "add submodule")
            _checkout(repo, "feature", create=True)
            submodule = repo / "vendor" / "lib"
            _git(submodule, "config", "user.email", "small@example.com")
            _git(submodule, "config", "user.name", "Small Test")
            _write(submodule, "lib.txt", "lib\nmore\n")
            _add_commit(submodule, "update submodule")
            _write(repo, "a.py", "one\n")
            _write(repo, "b.py", "two\n")
            _write(repo, "c.py", "three\n")
            head = _add_commit(repo, "submodule pointer and three files")
            _git(repo, "config", "diff.ignoreSubmodules", "all")
            verdict = check_repo(repo, base, head)
            self.assertIn("4 files > 3", verdict.reasons)
            self.assertIn("vendor/lib", [item.path for item in verdict.files])

    def test_invalid_config_on_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(
                repo,
                {"base.txt": "base\n", ".github/small-change.json": "not json\n"},
            )
            _checkout(repo, "feature", create=True)
            _write(repo, "a.py", "one\n")
            head = _add_commit(repo, "small")
            verdict = check_repo(repo, base, head)
            self.assertTrue(
                any(
                    r == f"invalid .github/small-change.json at {base}"
                    for r in verdict.reasons
                )
            )
