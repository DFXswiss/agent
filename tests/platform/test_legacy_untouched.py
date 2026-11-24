"""U-HUB-legacy-untouched and U-RUN-legacy-untouched (R-P.2): the platform work never changes legacy files (ADR-001).

The repository unites the former repositories `agent` and `agent-core` (INF-06, ADR-061), so there are two legacy
bases: the merge base with `origin/develop` (legacy `agent_cli` and its files) and `CORE_BASE`, the last `develop`
commit of the former agent-core repository (legacy `agent_core` and its files; part of this history since the
unification). Added, changed and removed files lie only in the platform areas; a legacy file of one origin is
unchanged against its own base. The files both origins had are united: `pyproject.toml` (agent side: new sections,
new keys in the extras, scripts and package-data tables, new dependencies after the existing ones; agent-core side:
every dependency, script, package-data entry and test extra entry kept), `.gitignore` (agent lines first and
unchanged, every agent-core line kept) and the files of `MERGED_FILES`, which carry the content of both and were
reviewed with the unification; they may change but not disappear.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_REF = "origin/develop"
# `develop` of the former agent-core repository when it was united with this one; it does not move any more.
CORE_BASE = "cbe3dbd8bee1ae36be0f3301927c3c63fbacc36a"
# `develop` of this repository when the history of agent-core was merged into it (the first parent of that merge); once
# `origin/develop` is the united tree, the merge base with it holds the files of both origins, so the agent side of the
# comparison is this commit (KOR-36: without it every package was refused for the unification's own changes).
AGENT_BASE = "64f0be0a56efbdacbfa0568fe0f59b61ec9cb079"

PLATFORM_PREFIXES = (
    "src/agent_hub/",
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
    "web/",
    "e2e/platform/",
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
# Lists that may only grow at the end (the dependencies of the legacy hub of agent-core joined those of agent_cli).
GROWING_LISTS = (("project", "dependencies"),)
# Files of both origins whose content was united (INF-06): {path: (keeps every line of the agent base, keeps every line
# of the agent-core base)}, lines compared without leading `#` and surrounding blanks (headings were moved one level
# down) and apart from the lines the union rewrote (`REWRITTEN_LINES`). Where only the agent base is kept, the agent
# version serves both (the guard and test workflows, the A38 configuration, CONTRIBUTING with a section of agent-core).
MERGED_FILES = {
    ".github/a38.json": (True, False),
    ".github/workflows/a38-guard.yml": (True, False),
    ".github/workflows/test.yml": (True, False),
    "CONTRIBUTING.md": (True, False),
    "README.md": (True, True),
    "tests/conftest.py": (True, True),
}
REWRITTEN_LINES = frozenset(
    {
        "Local session-store client. Record sessions, activities and (when a skill is attached) tasks on this machine,"
        " then pair the device to the [agent-core](https://github.com/DFXswiss/agent-core) hub with GitHub.",
        "agent-core",
        "Hub for the [agent](https://github.com/DFXswiss/agent) session store.",
        "from agent_cli.pg import create_database, drop_database, ensure_cluster, stop_cluster",
        "dsn = ensure_cluster(data)",
        "stop_cluster(data)",
        "dsn = create_database(pg_admin_dsn, name)",
        "drop_database(pg_admin_dsn, name)",
        'if request.node.get_closest_marker("no_pg") is not None:',
    }
)
# Legacy workflows of agent-core that publish its image: unchanged apart from the guard lines that keep them in the
# former repository (they must not publish an image or notify a deployment from this one).
GUARDED_WORKFLOWS = frozenset(
    {
        ".github/workflows/agent-core-dev.yaml",
        ".github/workflows/agent-core-prd.yaml",
        ".github/workflows/auto-release-pr.yaml",
    }
)
GUARD_LINES = (
    "    # Legacy image of the former agent-core repository: only there (INF-06, ADR-061).",
    "    if: github.repository == 'DFXswiss/agent-core'",
)
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def git(*args: str, root: Path = REPO_ROOT) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {done.stderr.strip()} (fetch {BASE_REF} first)")
    return done.stdout


def merge_base(root: Path = REPO_ROOT) -> str:
    """The agent base: the merge base with `origin/develop`, or `AGENT_BASE` once that lies on the united history."""
    base = git("merge-base", BASE_REF, "HEAD", root=root).strip()
    united = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", AGENT_BASE, base], capture_output=True, check=False
    )
    return AGENT_BASE if united.returncode == 0 else base


def core_base(root: Path = REPO_ROOT, ref: str = CORE_BASE) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"{ref} is missing: the history of the former agent-core repository belongs to this repository")
    return done.stdout.strip()


def is_ancestor(older: str, newer: str, root: Path = REPO_ROOT) -> bool:
    done = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer], check=False)
    return done.returncode == 0


def agent_origin(agent_base: str, core: str, root: Path = REPO_ROOT) -> str:
    """The agent side of the legacy files: `agent_base`, or - once `origin/develop` holds the unification (`core` is
    an ancestor of `agent_base`) - the first parent of the merge that brought `core` in (agent's own `develop` before
    INF-06). The united files were checked against both origins when they were united."""
    if not is_ancestor(core, agent_base, root):
        return agent_base
    merges = git("rev-list", "--merges", "--ancestry-path", "--reverse", f"{core}..{agent_base}", root=root).split()
    return next(
        first
        for first in (git("rev-parse", f"{merge}^1", root=root).strip() for merge in merges)
        if not is_ancestor(core, first, root)
    )


def changes(root: Path = REPO_ROOT, base: str | None = None) -> dict[str, str]:
    """{path: status letter} of every committed, staged, unstaged and untracked change against `base`."""
    base = base or merge_base(root)
    rows = {}
    for line in git("diff", "--name-status", "--no-renames", base, root=root).splitlines():
        status, path = line.split("\t", 1)
        rows[path] = status[0]
    rows.update(dict.fromkeys(git("ls-files", "--others", "--exclude-standard", root=root).splitlines(), "A"))
    return rows


def paths_of(base: str, root: Path = REPO_ROOT) -> set[str]:
    return set(git("ls-tree", "-r", "--name-only", base, root=root).splitlines())


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
    for table in GROWING_LISTS:
        old_list, new_list = dig(before, table), dig(after, table)
        if isinstance(old_list, list) and (not isinstance(new_list, list) or new_list[: len(old_list)] != old_list):
            problems.append(f"pyproject.toml: {'.'.join(table)} changed")
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


def requirement_names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = REQUIREMENT_NAME.match(requirement)
        names.add((match.group(0) if match else requirement).lower().replace("_", "-"))
    return names


def core_pyproject_problems(old: str, new: str) -> list[str]:
    """The agent-core side of the united pyproject.toml: nothing the legacy hub needs went away."""
    before, after = tomllib.loads(old), tomllib.loads(new)
    found = []
    missing = requirement_names(dig(before, ("project",)).get("dependencies", [])) - requirement_names(
        dig(after, ("project",)).get("dependencies", [])
    )
    found += [f"pyproject.toml: agent-core dependency {name} missing" for name in sorted(missing)]
    for table in (("project", "scripts"), ("tool", "setuptools", "package-data")):
        new_table = dig(after, table)
        found += [
            f"pyproject.toml: agent-core {'.'.join(table)}.{key} changed"
            for key, value in dig(before, table).items()
            if new_table.get(key) != value
        ]
    test_extra = dig(after, ("project", "optional-dependencies")).get("test", [])
    found += [
        f"pyproject.toml: agent-core test extra {entry} missing"
        for entry in dig(before, ("project", "optional-dependencies")).get("test", [])
        if entry not in test_extra
    ]
    return found


def gitignore_problems(old: str, new: str) -> list[str]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return [] if new_lines[: len(old_lines)] == old_lines else [".gitignore: existing lines changed"]


def gitignore_union_problems(old: str, new: str) -> list[str]:
    missing = [line for line in old.splitlines() if line not in new.splitlines()]
    return [f".gitignore: agent-core line {line!r} missing" for line in missing]


def normalized(text: str) -> set[str]:
    return {line.lstrip("#").strip() for line in text.splitlines()} - {""}


def lost_lines(old: str, new: str) -> list[str]:
    return sorted(normalized(old) - normalized(new) - REWRITTEN_LINES)


def guarded_problems(path: str, core: str, root: Path) -> list[str]:
    current = root / path
    if not current.is_file():
        return [f"{path}: D outside the platform areas"]
    lines = current.read_text().split("\n")
    if not all(line in lines for line in GUARD_LINES):
        return [f"{path}: guard missing"]
    kept = "\n".join(line for line in lines if line not in GUARD_LINES)
    return [] if kept == git("show", f"{core}:{path}", root=root) else [f"{path}: M outside the platform areas"]


def united_problems(path: str, agent_base: str, core: str, root: Path) -> list[str]:
    """A file both origins had: pyproject.toml, .gitignore or one of MERGED_FILES; anything else stays as it was."""
    current = root / path
    if not current.is_file():
        return [f"{path}: D outside the platform areas"]
    text = current.read_text()
    if path == "pyproject.toml":
        return pyproject_problems(git("show", f"{agent_base}:{path}", root=root), text) + core_pyproject_problems(
            git("show", f"{core}:{path}", root=root), text
        )
    if path == ".gitignore":
        return gitignore_problems(git("show", f"{agent_base}:{path}", root=root), text) + gitignore_union_problems(
            git("show", f"{core}:{path}", root=root), text
        )
    if path in MERGED_FILES:
        found = []
        for keep, base, origin in zip(MERGED_FILES[path], (agent_base, core), ("agent", "agent-core"), strict=True):
            if keep:
                found += [
                    f"{path}: {origin} line lost: {line}"
                    for line in lost_lines(git("show", f"{base}:{path}", root=root), text)
                ]
        return found
    return [f"{path}: M outside the platform areas"]


def problems(root: Path = REPO_ROOT, core_ref: str = CORE_BASE) -> list[str]:
    agent_base, core = merge_base(root), core_base(root, core_ref)
    origin = agent_origin(agent_base, core, root)
    agent_rows = changes(root, agent_base)
    # once `origin/develop` is united, its state is the reviewed one: only what this branch changes counts
    core_rows = changes(root, core) if origin == agent_base else agent_rows
    agent_paths, core_paths = paths_of(origin, root), paths_of(core, root)
    found = []
    for path in sorted(set(agent_rows) | set(core_rows)):
        if is_platform_path(path):
            continue
        if path in agent_paths and path in core_paths:
            found += united_problems(path, agent_base, core, root)
        elif path in GUARDED_WORKFLOWS and path in core_paths:
            found += guarded_problems(path, core, root)
        elif path in agent_paths or path in core_paths:
            status = agent_rows.get(path) if path in agent_paths else core_rows.get(path)
            if status is not None:
                found.append(f"{path}: {status} outside the platform areas")
        else:
            found.append(f"{path}: A outside the platform areas")
    return found


def test_legacy_untouched() -> None:
    assert problems() == []


def run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


CORE = "core-develop"
AGENT_PYPROJECT = '[project]\nname = "x"\n[project.scripts]\nx = "x:main"\n'
CORE_PYPROJECT = (
    '[project]\nname = "c"\ndependencies = ["fastapi>=1"]\n[project.scripts]\nc = "c:main"\n'
    '[project.optional-dependencies]\ntest = ["pytest"]\n[tool.setuptools.package-data]\nc = ["static/*"]\n'
)
UNITED_PYPROJECT = (
    '[project]\nname = "x"\ndependencies = ["fastapi>=1"]\n[project.scripts]\nx = "x:main"\nc = "c:main"\n'
    '[project.optional-dependencies]\ntest = ["pytest"]\n[tool.setuptools.package-data]\nc = ["static/*"]\n'
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An agent base pushed to `origin/develop`, an unrelated agent-core base `core-develop`, both united locally."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    run(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    run(tmp_path, "init", "-q", "-b", "develop", str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        run(work, "config", key, value)
    (work / "pyproject.toml").write_text(AGENT_PYPROJECT)
    (work / ".gitignore").write_text("a\n")
    (work / "legacy.py").write_text("x = 1\n")
    (work / "README.md").write_text("agent\n")
    (work / "LICENSE").write_text("MIT\n")
    run(work, "add", ".")
    run(work, "commit", "-q", "-m", "Base.")
    run(work, "remote", "add", "origin", str(origin))
    run(work, "push", "-q", "origin", "develop")
    run(work, "fetch", "-q", "origin")
    run(work, "checkout", "-q", "--orphan", CORE)
    run(work, "rm", "-rqf", ".")
    (work / "pyproject.toml").write_text(CORE_PYPROJECT)
    (work / ".gitignore").write_text("c\n")
    (work / "core_legacy.py").write_text("y = 1\n")
    (work / "README.md").write_text("core\n")
    (work / "LICENSE").write_text("MIT\n")
    run(work, "add", ".")
    run(work, "commit", "-q", "-m", "Core base.")
    run(work, "checkout", "-q", "develop")
    run(work, "merge", "-q", "-s", "ours", "--allow-unrelated-histories", "-m", "Unite.", CORE)
    run(work, "checkout", CORE, "--", "core_legacy.py")
    (work / "pyproject.toml").write_text(UNITED_PYPROJECT)
    (work / ".gitignore").write_text("a\nc\n")
    (work / "README.md").write_text("agent\ncore\n")
    run(work, "commit", "-q", "-am", "United.")
    return work


def test_clean_repository_has_no_problems(repo: Path) -> None:
    assert problems(repo, CORE) == []


def test_a_united_develop_counts_only_the_changes_of_the_branch(repo: Path) -> None:
    """After the unification `origin/develop` holds both histories: the legacy files of either origin that develop
    changed are its reviewed state, a change on the branch is still found."""
    united, agent = (git("rev-parse", ref, root=repo).strip() for ref in ("HEAD", "origin/develop"))
    (repo / "core_legacy.py").write_text("y = 2\n")
    run(repo, "commit", "-q", "-am", "Develop changes a legacy file.")
    run(repo, "checkout", "-q", "-b", "side")
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("n\n")
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "Side.")
    run(repo, "checkout", "-q", "develop")
    run(repo, "merge", "-q", "--no-ff", "-m", "A later merge.", "side")  # its first parent holds `core` already
    run(repo, "push", "-q", "origin", "develop")
    run(repo, "fetch", "-q", "origin")
    assert agent_origin(merge_base(repo), core_base(repo, CORE), repo) == agent
    assert agent_origin(united, core_base(repo, CORE), repo) == agent
    assert problems(repo, CORE) == []
    (repo / "legacy.py").write_text("x = 2\n")
    assert problems(repo, CORE) == ["legacy.py: M outside the platform areas"]


def test_repository_without_the_core_history_fails(repo: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="history of the former agent-core repository"):
        core_base(repo, "0" * 40)


def test_allowed_changes(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        UNITED_PYPROJECT.replace('dependencies = ["fastapi>=1"]', 'dependencies = ["fastapi>=1", "cbor2"]').replace(
            'c = "c:main"\n', 'c = "c:main"\ny = "y:main"\n'
        )
        + "[tool.ruff]\nline-length = 120\n"
    )
    (repo / ".gitignore").write_text("a\nb\nc\n")
    (repo / "README.md").write_text("united\n# agent\n## core\n")
    for package in ("agent_runner", "agent_hub"):
        (repo / "src" / package).mkdir(parents=True)
        (repo / "src" / package / "__init__.py").write_text("")
    (repo / "web").mkdir()
    (repo / "web" / "a.ts").write_text("")
    (repo / "Makefile").write_text("x:\n")
    run(repo, "add", "Makefile")
    run(repo, "commit", "-q", "-m", "Add.")
    assert problems(repo, CORE) == []


def test_forbidden_changes(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[project]\nname = "y"\n[project.scripts]\nx = "z:main"\n')
    (repo / ".gitignore").write_text("b\n")
    (repo / "legacy.py").write_text("x = 2\n")
    (repo / "core_legacy.py").write_text("y = 2\n")
    (repo / "new_legacy.py").write_text("")
    (repo / "LICENSE").write_text("changed\n")
    assert sorted(problems(repo, CORE)) == [
        ".gitignore: agent-core line 'c' missing",
        ".gitignore: existing lines changed",
        "LICENSE: M outside the platform areas",
        "core_legacy.py: M outside the platform areas",
        "legacy.py: M outside the platform areas",
        "new_legacy.py: A outside the platform areas",
        "pyproject.toml: [project] changed",
        "pyproject.toml: agent-core dependency fastapi missing",
        "pyproject.toml: agent-core project.scripts.c changed",
        "pyproject.toml: agent-core test extra pytest missing",
        "pyproject.toml: agent-core tool.setuptools.package-data.c changed",
        "pyproject.toml: project.scripts.x changed",
    ]


def test_dependencies_only_grow_at_the_end(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT.replace('["fastapi>=1"]', '["cbor2", "fastapi>=1"]'))
    run(repo, "commit", "-q", "-am", "Base deps.")
    run(repo, "push", "-q", "origin", "develop")
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT)
    assert problems(repo, CORE) == ["pyproject.toml: project.dependencies changed"]
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT.replace('dependencies = ["fastapi>=1"]\n', ""))
    assert "pyproject.toml: project.dependencies changed" in problems(repo, CORE)


def test_requirement_names() -> None:
    assert requirement_names(["uvicorn[standard]>=0.32", "PyYAML>=6,<7", "psycopg_pool", "!"]) == {
        "uvicorn",
        "pyyaml",
        "psycopg-pool",
        "!",
    }


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
    assert problems(repo, CORE) == []


def test_deleted_files_are_refused(repo: Path) -> None:
    (repo / "legacy.py").unlink()
    (repo / "core_legacy.py").unlink()
    (repo / "README.md").unlink()
    assert sorted(problems(repo, CORE)) == [
        "README.md: D outside the platform areas",
        "core_legacy.py: D outside the platform areas",
        "legacy.py: D outside the platform areas",
    ]


def test_missing_base_fails(repo: Path) -> None:
    run(repo, "remote", "remove", "origin")
    with pytest.raises(pytest.fail.Exception, match="fetch origin/develop first"):
        merge_base(repo)


def test_merged_file_keeps_the_lines_of_its_bases(repo: Path) -> None:
    (repo / "README.md").write_text("## agent\n# core\n")
    assert problems(repo, CORE) == []
    (repo / "README.md").write_text("agent\n")
    assert problems(repo, CORE) == ["README.md: agent-core line lost: core"]
    assert lost_lines("a\nagent-core\n\n", "# a\n") == []


def test_guarded_workflow(repo: Path) -> None:
    flow = repo / ".github" / "workflows" / "agent-core-dev.yaml"
    run(repo, "checkout", "-q", CORE)
    flow.parent.mkdir(parents=True)
    flow.write_text("jobs:\n  a:\n    runs-on: x\n")
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "Flow.")
    run(repo, "checkout", "-q", "develop")
    run(repo, "merge", "-q", "--no-edit", CORE)
    assert problems(repo, CORE) == [".github/workflows/agent-core-dev.yaml: guard missing"]
    flow.write_text("jobs:\n  a:\n" + "\n".join(GUARD_LINES) + "\n    runs-on: x\n")
    assert problems(repo, CORE) == []
    flow.write_text("jobs:\n  a:\n" + "\n".join(GUARD_LINES) + "\n    runs-on: y\n")
    assert problems(repo, CORE) == [".github/workflows/agent-core-dev.yaml: M outside the platform areas"]
    flow.unlink()
    assert problems(repo, CORE) == [".github/workflows/agent-core-dev.yaml: D outside the platform areas"]


def test_merged_files_cover_the_files_of_both_origins() -> None:
    """Every legacy file the two origins share is one of the united files (checked against the real bases)."""
    core = core_base()
    agent_base = agent_origin(merge_base(), core)
    shared = {path for path in paths_of(agent_base) & paths_of(core) if not is_platform_path(path)}
    assert shared - {"LICENSE", ".github/pr-guard.json"} == set(MERGED_FILES) | {"pyproject.toml", ".gitignore"}
    for path in ("LICENSE", ".github/pr-guard.json"):
        assert git("rev-parse", f"{agent_base}:{path}") == git("rev-parse", f"{core}:{path}")


def test_equal_except_additions() -> None:
    assert equal_except_additions({"tool": {"b": {}}}, {"tool": {"b": {}, "c": {"d": 1}}})
    assert equal_except_additions({"a": {}}, {"a": {}, "n": {"x": 1}})
    assert not equal_except_additions({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}})
    assert not equal_except_additions({"tool": {"b": {}}}, {"tool": {"b": {}, "c": 2}})
    assert not equal_except_additions({"a": {"b": 1}}, {"a": {}})
    assert not equal_except_additions({"a": 1}, {"a": 2})


def test_platform_tables_may_change(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT + '[tool.mypy]\nfiles = ["a"]\n')
    run(repo, "commit", "-q", "-am", "Types.")
    run(repo, "push", "-q", "origin", "develop")
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT + '[tool.mypy]\nfiles = ["a", "b"]\n')
    assert problems(repo, CORE) == []


def test_new_key_in_legacy_table_is_refused(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(UNITED_PYPROJECT + "[tool.coverage.run]\nbranch = true\n")
    run(repo, "commit", "-q", "-am", "Coverage.")
    run(repo, "push", "-q", "origin", "develop")
    (repo / "pyproject.toml").write_text(
        UNITED_PYPROJECT + '[tool.coverage.run]\nbranch = true\nomit = ["a"]\n[tool.coverage.paths]\nsource = []\n'
    )
    assert problems(repo, CORE) == ["pyproject.toml: [tool] changed"]


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
