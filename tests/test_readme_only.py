"""Unit and git-backed tests for fail-closed README-only detection."""

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

from agent_cli.readme_only import (
    github_file_paths,
    github_is_readme_only,
    git_changed_paths,
    git_is_readme_only,
    is_readme_path,
    parse_name_status_z,
    paths_are_readme_only,
)


def _git(cwd: Path, *parts: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *parts],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> str:
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
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return _git(path, "rev-parse", "HEAD").lower()


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


class PathHelperTests(unittest.TestCase):
    def test_is_readme_path(self) -> None:
        self.assertTrue(is_readme_path("README.md"))
        self.assertTrue(is_readme_path("foo/README.md"))
        self.assertTrue(is_readme_path("a/b/README.md"))
        self.assertFalse(is_readme_path("readme.md"))
        self.assertFalse(is_readme_path("README.MD"))
        self.assertFalse(is_readme_path("README"))
        self.assertFalse(is_readme_path("README.md.bak"))
        self.assertFalse(is_readme_path(""))

    def test_paths_are_readme_only_fail_closed(self) -> None:
        self.assertFalse(paths_are_readme_only([]))
        self.assertTrue(paths_are_readme_only(["README.md"]))
        self.assertTrue(paths_are_readme_only(["docs/README.md"]))
        self.assertFalse(paths_are_readme_only(["README.md", "src/main.py"]))
        self.assertFalse(paths_are_readme_only(["readme.md"]))
        self.assertFalse(paths_are_readme_only([123]))  # type: ignore[list-item]


class ParseNameStatusTests(unittest.TestCase):
    def test_empty_blob(self) -> None:
        self.assertEqual(parse_name_status_z(b""), [])

    def test_real_git_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            (repo / "README.md").write_text("doc\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            head = _commit(repo, "add readme")
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "diff",
                    "--name-status",
                    "-z",
                    "--find-renames=100%",
                    f"{base}...{head}",
                ],
                check=True,
                capture_output=True,
            )
            paths = parse_name_status_z(proc.stdout)
            self.assertEqual(paths, ["README.md"])

    def test_unknown_status_is_none(self) -> None:
        self.assertIsNone(parse_name_status_z(b"X\0path\0"))
        self.assertIsNone(parse_name_status_z(b"M100\0path\0"))


class GitDetectionTests(unittest.TestCase):
    def test_only_readme_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            (repo / "README.md").write_text("doc\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            head = _commit(repo, "readme only")
            self.assertTrue(git_is_readme_only(repo, base, head))
            self.assertEqual(git_changed_paths(repo, base, head), ["README.md"])

    def test_readme_plus_other_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            (repo / "README.md").write_text("doc\n", encoding="utf-8")
            (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head = _commit(repo, "mixed")
            self.assertFalse(git_is_readme_only(repo, base, head))

    def test_nested_readme_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            nested = repo / "docs"
            nested.mkdir()
            (nested / "README.md").write_text("nested\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "docs/README.md"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head = _commit(repo, "nested readme")
            self.assertTrue(git_is_readme_only(repo, base, head))

    def test_rename_readme_to_contributing_not_readme_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = _init_repo(repo)
            (repo / "README.md").write_text("doc\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
            )
            mid = _commit(repo, "add readme")
            subprocess.run(
                ["git", "mv", "README.md", "CONTRIBUTING.md"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head = _commit(repo, "rename")
            paths = git_changed_paths(repo, mid, head)
            self.assertIsNotNone(paths)
            assert paths is not None
            self.assertIn("README.md", paths)
            self.assertIn("CONTRIBUTING.md", paths)
            self.assertFalse(git_is_readme_only(repo, mid, head))

    def test_git_error_missing_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _init_repo(repo)
            self.assertIsNone(
                git_changed_paths(repo, "0" * 40, head)
            )
            self.assertFalse(git_is_readme_only(repo, "0" * 40, head))

    def test_empty_diff_not_readme_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _init_repo(repo)
            self.assertEqual(git_changed_paths(repo, head, head), [])
            self.assertFalse(git_is_readme_only(repo, head, head))


class GitHubHelperTests(unittest.TestCase):
    def test_added_readme(self) -> None:
        entries = [{"filename": "README.md", "status": "added"}]
        self.assertTrue(github_is_readme_only(entries, truncated=False))

    def test_empty_entries(self) -> None:
        self.assertFalse(github_is_readme_only([], truncated=False))

    def test_truncated(self) -> None:
        entries = [{"filename": "README.md", "status": "modified"}]
        self.assertFalse(github_is_readme_only(entries, truncated=True))

    def test_unknown_status(self) -> None:
        entries = [{"filename": "README.md", "status": "weird"}]
        self.assertIsNone(github_file_paths(entries))
        self.assertFalse(github_is_readme_only(entries, truncated=False))

    def test_renamed_missing_previous(self) -> None:
        entries = [{"filename": "README.md", "status": "renamed"}]
        self.assertIsNone(github_file_paths(entries))
        self.assertFalse(github_is_readme_only(entries, truncated=False))

    def test_renamed_includes_both(self) -> None:
        entries = [
            {
                "filename": "docs/README.md",
                "status": "renamed",
                "previous_filename": "README.md",
            }
        ]
        self.assertEqual(
            github_file_paths(entries), ["README.md", "docs/README.md"]
        )
        self.assertTrue(github_is_readme_only(entries, truncated=False))


if __name__ == "__main__":
    unittest.main()
