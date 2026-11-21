"""U-RUN-legacy-untouched (R-P.2): the platform work never changes legacy files (ADR-001).

Compares the working tree with the merge base of `origin/develop`: added files lie only in the platform areas;
of the existing files only `.gitignore` (lines added) and `pyproject.toml` (new sections and new keys in the
extras, scripts and package-data tables) may change.
"""

from __future__ import annotations

import subprocess
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
EXTENSIBLE_TABLES = (
    ("project", "optional-dependencies"),
    ("project", "scripts"),
    ("tool", "setuptools", "package-data"),
)


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
            parent = dig(data, table[:-1])
            if table[-1] in parent:
                parent[table[-1]] = {}
    for key, value in before.items():
        if not equal_except_additions(value, after.get(key)):
            problems.append(f"pyproject.toml: [{key}] changed")
    return problems


def equal_except_additions(old: Any, new: Any) -> bool:
    """`new` equals `old` apart from keys added to tables; emptied extensible tables compare as tables."""
    if isinstance(old, dict) and isinstance(new, dict):
        return all(key in new and equal_except_additions(value, new[key]) for key, value in old.items())
    return bool(old == new)


def gitignore_problems(old: str, new: str) -> list[str]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return [] if new_lines[: len(old_lines)] == old_lines else [".gitignore: existing lines changed"]


def problems(root: Path = REPO_ROOT) -> list[str]:
    base = merge_base(root)
    found = []
    for status, path in changes(root):
        if status == "A" and is_platform_path(path):
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


def test_deleted_file_is_refused(repo: Path) -> None:
    (repo / "legacy.py").unlink()
    assert problems(repo) == ["legacy.py: D outside the platform areas"]


def test_missing_base_fails(repo: Path) -> None:
    run(repo, "remote", "remove", "origin")
    with pytest.raises(pytest.fail.Exception, match="fetch origin/develop first"):
        merge_base(repo)


def test_equal_except_additions() -> None:
    assert equal_except_additions({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}})
    assert not equal_except_additions({"a": {"b": 1}}, {"a": {}})
    assert not equal_except_additions({"a": 1}, {"a": 2})
