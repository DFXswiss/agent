"""U-RUN-legacy-untouched (R-P.2): the platform work never changes legacy files (ADR-001).

Compares the working tree with the merge base of `origin/develop`: added, changed and removed files lie only in the
platform areas (since the platform was merged into `develop`, its files exist on the base and change there, too); of
the other files only `.gitignore` (lines added) and `pyproject.toml` (new sections and new keys in the extras, scripts
and package-data tables) may change.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_REF = "origin/develop"

PLATFORM_PREFIXES = (
    "src/agent_runner/",
    "src/agent_guest/",
    "src/agent_doubles/",
    "src/agent_platform/",
    "tests/platform/",
    "docs/platform/",
    "contracts/",
    "scripts/",
    "images/",
    "deploy/monitoring/",
    ".github/workflows/platform-",
)
PLATFORM_FILES = frozenset({"Makefile", ".coveragerc-platform"})
# Tables of pyproject.toml that may gain keys; every key that exists on develop keeps its value.
# Where whole new tables may appear (e.g. `[tool.mypy]`, `[tool.ruff]`).
NEW_TABLE_PARENTS = ((), ("tool",))
EXTENSIBLE_TABLES = (
    ("project", "optional-dependencies"),
    ("project", "scripts"),
    ("tool", "setuptools", "package-data"),
)
# Tables that configure the platform gates only (lint and types of the platform packages, ADR-027 section 6): a new
# platform package is added to their lists (KOR-24: `src/agent_platform` in `[tool.mypy] files`).
PLATFORM_TABLES = (("tool", "mypy"),)


def git(*args: str, root: Path = REPO_ROOT) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {done.stderr.strip()} (fetch {BASE_REF} first)")
    return done.stdout


def merge_base(root: Path = REPO_ROOT) -> str:
    return git("merge-base", BASE_REF, "HEAD", root=root).strip()


def changes(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """(status letter, path) of every committed, staged, unstaged and untracked change against the merge base."""
    base = merge_base(root)
    rows = []
    for line in git("diff", "--name-status", "--no-renames", base, root=root).splitlines():
        status, path = line.split("\t", 1)
        rows.append((status[0], path))
    rows += [("A", path) for path in git("ls-files", "--others", "--exclude-standard", root=root).splitlines()]
    return rows


def is_platform_path(path: str) -> bool:
    return path in PLATFORM_FILES or path.startswith(PLATFORM_PREFIXES)


def dig(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        data = data.get(key, {})
    return data


def pyproject_problems(old: str, new: str) -> list[str]:
    before, after = tomllib.loads(old), tomllib.loads(new)
    problems = []
    for table in EXTENSIBLE_TABLES:
        old_table, new_table = dig(before, table), dig(after, table)
        problems += [
            f"pyproject.toml: {'.'.join(table)}.{key} changed"
            for key, value in old_table.items()
            if new_table.get(key) != value
        ]
        for data in (before, after):
            dig(data, table[:-1]).pop(table[-1], None)
    for table in PLATFORM_TABLES:
        for data in (before, after):
            dig(data, table[:-1]).pop(table[-1], None)
    for key, value in before.items():
        if not equal_except_additions(value, after.get(key), (key,)):
            problems.append(f"pyproject.toml: [{key}] changed")
    return problems


def equal_except_additions(old: Any, new: Any, path: tuple[str, ...] = ()) -> bool:
    """`new` equals `old` apart from new tables at the top level or directly under `[tool]`.

    Keys added to an existing table (for example `omit` in a legacy `[tool.coverage.run]`) count as a change; the
    extensible tables are removed before the comparison.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        added = [key for key in new if key not in old]
        if added and (path not in NEW_TABLE_PARENTS or not all(isinstance(new[key], dict) for key in added)):
            return False
        return all(key in new and equal_except_additions(value, new[key], (*path, key)) for key, value in old.items())
    return bool(old == new)


def gitignore_problems(old: str, new: str) -> list[str]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return [] if new_lines[: len(old_lines)] == old_lines else [".gitignore: existing lines changed"]


def problems(root: Path = REPO_ROOT) -> list[str]:
    base = merge_base(root)
    found = []
    for status, path in changes(root):
        if is_platform_path(path):
            continue
        if status == "M" and path == "pyproject.toml":
            found += pyproject_problems(git("show", f"{base}:{path}", root=root), (root / path).read_text())
        elif status == "M" and path == ".gitignore":
            found += gitignore_problems(git("show", f"{base}:{path}", root=root), (root / path).read_text())
        else:
            found.append(f"{path}: {status} outside the platform areas")
    return found


def test_legacy_untouched() -> None:
    assert problems() == []


def run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    run(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    run(tmp_path, "init", "-q", "-b", "develop", str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        run(work, "config", key, value)
    (work / "pyproject.toml").write_text('[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n')
    (work / ".gitignore").write_text("a\n")
    (work / "legacy.py").write_text("x = 1\n")
    run(work, "add", ".")
    run(work, "commit", "-q", "-m", "Base.")
    run(work, "remote", "add", "origin", str(origin))
    run(work, "push", "-q", "origin", "develop")
    run(work, "fetch", "-q", "origin")
    return work


def test_clean_repository_has_no_problems(repo: Path) -> None:
    assert problems(repo) == []


def test_allowed_changes(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\ny = "y:main"\n'
        '[project.optional-dependencies]\nplatform = ["a"]\n[tool.ruff]\nline-length = 120\n'
    )
    (repo / ".gitignore").write_text("a\nb\n")
    (repo / "src" / "agent_runner").mkdir(parents=True)
    (repo / "src" / "agent_runner" / "__init__.py").write_text("")
    (repo / "Makefile").write_text("x:\n")
    run(repo, "add", "Makefile")
    run(repo, "commit", "-q", "-m", "Add.")
    assert problems(repo) == []


def test_forbidden_changes(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[project]\nname = "y"\n[project.scripts]\nx = "z:main"\n')
    (repo / ".gitignore").write_text("b\n")
    (repo / "legacy.py").write_text("x = 2\n")
    (repo / "new_legacy.py").write_text("")
    assert sorted(problems(repo)) == [
        ".gitignore: existing lines changed",
        "legacy.py: M outside the platform areas",
        "new_legacy.py: A outside the platform areas",
        "pyproject.toml: [project] changed",
        "pyproject.toml: project.scripts.x changed",
    ]


def test_platform_files_on_the_base_may_change(repo: Path) -> None:
    platform = repo / "docs" / "platform"
    platform.mkdir(parents=True)
    (platform / "a.md").write_text("a\n")
    (platform / "b.md").write_text("b\n")
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "Platform.")
    run(repo, "push", "-q", "origin", "develop")
    (platform / "a.md").write_text("changed\n")
    (platform / "b.md").unlink()
    assert problems(repo) == []


def test_deleted_file_is_refused(repo: Path) -> None:
    (repo / "legacy.py").unlink()
    assert problems(repo) == ["legacy.py: D outside the platform areas"]


def test_missing_base_fails(repo: Path) -> None:
    run(repo, "remote", "remove", "origin")
    with pytest.raises(pytest.fail.Exception, match="fetch origin/develop first"):
        merge_base(repo)


def test_equal_except_additions() -> None:
    assert equal_except_additions({"tool": {"b": {}}}, {"tool": {"b": {}, "c": {"d": 1}}})
    assert equal_except_additions({"a": {}}, {"a": {}, "n": {"x": 1}})
    assert not equal_except_additions({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}})
    assert not equal_except_additions({"tool": {"b": {}}}, {"tool": {"b": {}, "c": 2}})
    assert not equal_except_additions({"a": {"b": 1}}, {"a": {}})
    assert not equal_except_additions({"a": 1}, {"a": 2})


def test_platform_tables_may_change(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n[tool.mypy]\nfiles = ["a"]\n'
    )
    run(repo, "commit", "-q", "-am", "Types.")
    run(repo, "push", "-q", "origin", "develop")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n[tool.mypy]\nfiles = ["a", "b"]\n'
    )
    assert problems(repo) == []


def test_new_key_in_legacy_table_is_refused(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n[tool.coverage.run]\nbranch = true\n'
    )
    run(repo, "commit", "-q", "-am", "Coverage.")
    run(repo, "push", "-q", "origin", "develop")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n[tool.coverage.run]\nbranch = true\nomit = ["a"]\n'
        "[tool.coverage.paths]\nsource = []\n"
    )
    assert problems(repo) == ["pyproject.toml: [tool] changed"]


def test_legacy_pytest_run_skips_the_platform_suites() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "tests/platform" not in done.stdout
