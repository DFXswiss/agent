"""Import boundaries of the united repository (INF-06, ADR-061; 00-overview.md section 5).

One distribution carries the hub, the runner, the guest, the doubles, the agent tool and both legacy packages. What
keeps the parts apart is checked here on the source (every `import`, also inside functions):

- the hub imports nothing of the runner, guest, doubles, agent tool or legacy CLI, and of the legacy hub only the
  modules 00-overview.md section 5 allows;
- the runner and the agent tool import nothing of the hub or the legacy hub; the runner reaches the legacy CLI only
  through the modules section 5 allows;
- guest and doubles use the standard library only (they run inside the VM or as separate processes);
- the hub image installs `agent_hub` and `agent_core` only, with the `hub` extra: every module the hub reaches lies in
  these two packages and every third-party import it reaches is covered by that extra.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}
PLATFORM = ("agent_hub", "agent_runner", "agent_guest", "agent_doubles", "agent_platform")
LEGACY = ("agent_cli", "agent_core")

# 00-overview.md section 5: legacy modules the new code may import read-only (with their parent packages).
HUB_LEGACY = frozenset(
    {
        "agent_core.signing",
        "agent_core.passkey.errors",
        "agent_core.passkey.encoding",
        "agent_core.passkey.webauthn",
        "agent_core.passkey.certificate",
        "agent_core.passkey.relying_party",
        "agent_core.record.connection",
        "agent_core.record.checkpoint",
        "agent_core.record.event",
    }
)
# `agent_cli.store.StoreError` is the error `newest_magic_link` raises (login automation, as built before INF-06).
# `agent_cli.a38_review` and its errors: the A38 guard's parser behind `github.comment` `a38_review` (FIX-02).
RUNNER_LEGACY = frozenset(
    {
        "agent_cli.pg",
        "agent_cli.keys",
        "agent_cli.account_login",
        "agent_cli.store",
        "agent_cli.a38_review",
        "agent_cli.a38_errors",
    }
)
# Packages of the hub image (copied into it); everything the hub reaches must lie inside.
HUB_IMAGE_PACKAGES = ("agent_hub", "agent_core")
# Third-party import names that are not the distribution name, and imports that come with another requirement.
DISTRIBUTION_OF = {"yaml": "pyyaml", "psycopg_pool": "psycopg-pool"}
PROVIDED_BY = {"starlette": "fastapi", "pydantic": "fastapi"}
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def module_name(path: Path, src: Path = SRC) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path, src: Path = SRC) -> set[str]:
    """Absolute names of every module imported by the file (relative imports resolved, `from a import b` as `a.b`)."""
    package = module_name(path, src)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return found


def files_of(package: str, src: Path = SRC) -> Iterator[Path]:
    yield from sorted((src / package).rglob("*.py"))


def resolve(name: str, src: Path = SRC) -> Path | None:
    """The file of a first-party module, or None (not a module of `src`, e.g. a name imported from a module)."""
    base = src.joinpath(*name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def first_party(names: Iterable[str]) -> set[str]:
    return {name for name in names if name.split(".")[0] in PLATFORM + LEGACY}


def third_party(names: Iterable[str]) -> set[str]:
    return {name.split(".")[0] for name in names if name.split(".")[0] not in STDLIB} - set(PLATFORM + LEGACY)


def closure(start: Iterable[Path], src: Path = SRC) -> tuple[set[Path], set[str]]:
    """Files reachable from `start` through first-party imports (with parent packages) and their third-party imports."""
    todo, seen, outside = list(start), set[Path](), set[str]()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        names = imports_of(path, src)
        outside |= third_party(names)
        for name in first_party(names):
            parts = name.split(".")
            for end in range(1, len(parts) + 1):
                target = resolve(".".join(parts[:end]), src)
                if target is not None:
                    todo.append(target)
    return seen, outside


def allowed(name: str, modules: frozenset[str]) -> bool:
    """`name` is an allowed module, one of its parent packages or a name imported from an allowed module."""
    return any(name == module or module.startswith(name + ".") or name.startswith(module + ".") for module in modules)


def violations(package: str, forbidden: tuple[str, ...], legacy: frozenset[str], src: Path = SRC) -> list[str]:
    """Imports of `package` into a forbidden package, or into a legacy package beyond the allowed modules."""
    found = []
    for path in files_of(package, src):
        for name in sorted(first_party(imports_of(path, src))):
            top = name.split(".")[0]
            if top in forbidden or (top in LEGACY and not allowed(name, legacy)):
                found.append(f"{path.relative_to(src)}: {name}")
    return found


def requirement_names(requirements: Iterable[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = REQUIREMENT_NAME.match(requirement)
        names.add((match.group(0) if match else requirement).lower().replace("_", "-"))
    return names


def uncovered(imports: Iterable[str], requirements: Iterable[str]) -> list[str]:
    """Third-party import names that no requirement of the list provides."""
    names = requirement_names(requirements)
    return sorted(
        name
        for name in imports
        if DISTRIBUTION_OF.get(name, name).lower().replace("_", "-") not in names and PROVIDED_BY.get(name) not in names
    )


def test_hub_imports_neither_runner_nor_guest() -> None:
    forbidden = ("agent_runner", "agent_guest", "agent_doubles", "agent_platform", "agent_cli")
    assert violations("agent_hub", forbidden, HUB_LEGACY) == []


@pytest.mark.parametrize("package", ["agent_runner", "agent_platform"])
def test_runner_and_tool_import_nothing_of_the_hub(package: str) -> None:
    assert violations(package, ("agent_hub", "agent_core"), RUNNER_LEGACY) == []


@pytest.mark.parametrize("package", ["agent_guest", "agent_doubles"])
def test_guest_and_doubles_use_the_standard_library_only(package: str) -> None:
    for path in files_of(package):
        names = imports_of(path)
        assert third_party(names) == set(), path
        assert {name.split(".")[0] for name in first_party(names)} <= {package}, path


def test_hub_image_needs_only_its_packages_and_the_hub_extra() -> None:
    reached, outside = closure(files_of("agent_hub"))
    assert {module_name(path).split(".")[0] for path in reached} <= set(HUB_IMAGE_PACKAGES)
    extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]["optional-dependencies"]
    assert uncovered(outside, extras["hub"]) == []
    # the extra names nothing the hub does not reach (except the server runtime the image starts and the WebSocket
    # library of the device channel's server side)
    reached_names = {DISTRIBUTION_OF.get(name, name) for name in outside} | {"uvicorn", "websockets"}
    assert requirement_names(extras["hub"]) <= {name.lower() for name in reached_names}


# The checker itself, on a small tree.


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    files = {
        "agent_hub/__init__.py": "",
        "agent_hub/a.py": "import json\nfrom . import b\nfrom .b import x\nimport fastapi.routing\n",
        "agent_hub/b.py": "def f():\n    from agent_core.signing import y\n    import yaml\n",
        "agent_hub/sub/__init__.py": "from .. import a\nfrom ..b import x\n",
        "agent_core/__init__.py": "import starlette\n",
        "agent_core/signing.py": "import agent_core.teams\n",
        "agent_core/teams.py": "import psycopg_pool\n",
        "agent_core/app.py": "",
        "agent_runner/__init__.py": "import agent_hub\nfrom agent_cli import pg\nfrom agent_cli.main import run\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, "utf-8")
    return tmp_path


def test_imports_are_resolved(tree: Path) -> None:
    assert imports_of(tree / "agent_hub" / "a.py", tree) == {
        "json",
        "agent_hub",
        "agent_hub.b",
        "agent_hub.b.x",
        "fastapi.routing",
    }
    assert imports_of(tree / "agent_hub" / "sub" / "__init__.py", tree) == {
        "agent_hub",
        "agent_hub.a",
        "agent_hub.b",
        "agent_hub.b.x",
    }
    assert module_name(tree / "agent_hub" / "sub" / "__init__.py", tree) == "agent_hub.sub"


def test_closure_follows_first_party_imports(tree: Path) -> None:
    reached, outside = closure([tree / "agent_hub" / "a.py"], tree)
    assert sorted(module_name(path, tree) for path in reached) == [
        "agent_core",
        "agent_core.signing",
        "agent_core.teams",
        "agent_hub",
        "agent_hub.a",
        "agent_hub.b",
    ]
    assert outside == {"fastapi", "yaml", "starlette", "psycopg_pool"}
    assert uncovered(outside, ["fastapi>=1", "PyYAML", "psycopg-pool"]) == []
    assert uncovered(outside, ["PyYAML"]) == ["fastapi", "psycopg_pool", "starlette"]
    assert resolve("agent_hub.b.x", tree) is None


def test_violations_name_the_file_and_import(tree: Path) -> None:
    assert violations("agent_hub", ("agent_runner",), frozenset({"agent_core.signing"}), tree) == []
    assert violations("agent_hub", ("agent_runner",), frozenset(), tree) == [
        "agent_hub/b.py: agent_core.signing",
        "agent_hub/b.py: agent_core.signing.y",
    ]
    assert violations("agent_runner", ("agent_hub",), frozenset({"agent_cli.pg"}), tree) == [
        "agent_runner/__init__.py: agent_cli.main",
        "agent_runner/__init__.py: agent_cli.main.run",
        "agent_runner/__init__.py: agent_hub",
    ]


def test_allowed_and_requirement_names() -> None:
    modules = frozenset({"agent_core.passkey.errors"})
    assert allowed("agent_core", modules)
    assert allowed("agent_core.passkey", modules)
    assert allowed("agent_core.passkey.errors.PasskeyError", modules)
    assert not allowed("agent_core.passkey.storage", modules)
    assert requirement_names(["uvicorn[standard]>=0.32", "PyYAML>=6", "psycopg_pool", "!"]) == {
        "uvicorn",
        "pyyaml",
        "psycopg-pool",
        "!",
    }
