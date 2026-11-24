"""Copy the reference crypto of the hub into `agent_runner.crypto` (ADR-027 section 2, decisions/CRY-03.md, HOT-08).

`agent` must not import `agent_hub` (00-overview.md section 5), so the runner keeps a copy of the reference packages
`agent_hub.crypto` and `agent_hub.log` and of their unit tests:

| Reference                        | Copy                                          |
|----------------------------------|-----------------------------------------------|
| `src/agent_hub/crypto/<m>.py`    | `src/agent_runner/crypto/<m>.py`              |
| `src/agent_hub/log/<m>.py`       | `src/agent_runner/crypto/log/<m>.py`          |
| `src/agent_hub/agents/vectors.py`| `src/agent_runner/crypto/agents/vectors.py`   |
| `tests/platform/hub/crypto/...`  | `tests/platform/runner/crypto/...`            |
| `tests/platform/hub/log/...`     | `tests/platform/runner/crypto/log/...`        |

A copied module differs from its reference only by

1. a first docstring paragraph `Copied from <source>.` (the reference path in this repository),
2. its import lines (`agent_hub.crypto` -> `agent_runner.crypto`, `agent_hub.log` -> `agent_runner.crypto.log`), and
3. the explicit `SOURCE_ADAPTATIONS` below (exact text blocks), which are recorded in `ORIGIN.json`.

`ORIGIN.json` lists every copied module with the sha256 of its reference bytes; `test_copy.py` undoes 1-3 on the
copy (`normalize`) and compares the hash, so any edit of a copy that is not an adaptation is visible. Tests are
copied with renamed import lines and `TEST_ADAPTATIONS`; `ORIGIN.json` records the hash of the reference test and
of the copied test (`copy_sha256`), so a hand edit of a copied test is visible as well.

The reference is the working tree of this repository (ADR-061: reference and copy live in one tree), committed or
not. Nothing that changes from run to run without a reference change (commit, date, host) enters a copy or
`ORIGIN.json`, so writing twice changes nothing and a package changes exactly the copies of the references it
changed, plus their entries in `ORIGIN.json` (decisions/HOT-08.md).

Usage (from the repository root): `make runner-crypto-copy` writes, `make runner-crypto-copy-check` only reports, or

    python tests/platform/runner/crypto/copy_tool.py [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
ORIGIN_FILE = "src/agent_runner/crypto/ORIGIN.json"
# (reference directory, copy directory) for modules and for tests.
SOURCE_DIRS = (
    ("src/agent_hub/crypto", "src/agent_runner/crypto"),
    ("src/agent_hub/log", "src/agent_runner/crypto/log"),
)
# Single reference modules of other hub areas: their vector sections (KOR-24, `contracts/vectors/agent.json`).
SOURCE_FILES = (("src/agent_hub/agents/vectors.py", "src/agent_runner/crypto/agents/vectors.py"),)
TEST_DIRS = (
    ("tests/platform/hub/crypto", "tests/platform/runner/crypto"),
    ("tests/platform/hub/log", "tests/platform/runner/crypto/log"),
)
# Longest prefix first; the copy side is matched the same way when a copy is normalised.
MODULE_NAMES = (
    ("agent_hub.agents.vectors", "agent_runner.crypto.agents.vectors"),
    ("agent_hub.agents", "agent_runner.crypto.agents"),
    ("agent_hub.log", "agent_runner.crypto.log"),
    ("agent_hub.crypto", "agent_runner.crypto"),
)
_IMPORT_LINE = re.compile(r"^(?:from|import) ", re.MULTILINE)
_ORIGIN_LINE = re.compile(r'^"""Copied from (?P<source>\S+)\.(?:"""\n$|\n\n)')

# Exact text blocks of a reference module and their replacement in the copy (vector discovery of the copied areas).
SOURCE_ADAPTATIONS: dict[str, list[tuple[str, str]]] = {
    "src/agent_hub/log/vectors.py": [
        # After renaming, agent_runner.crypto.vectors sorts after agent_runner.crypto.log (ruff isort).
        (
            "from agent_hub.crypto.vectors import Section, fixed, fixed_id\n"
            "from agent_hub.log import catchup, events, platform, rotation, verify, wraps\n"
            "from agent_hub.log.batch import check_batch\n"
            "from agent_hub.log.entries import Head, check_extends, entry_hash, sign_log_entry, sign_platform_entry\n",
            "from agent_hub.log import catchup, events, platform, rotation, verify, wraps\n"
            "from agent_hub.log.batch import check_batch\n"
            "from agent_hub.log.entries import Head, check_extends, entry_hash, sign_log_entry, sign_platform_entry\n"
            "from agent_hub.crypto.vectors import Section, fixed, fixed_id\n",
        ),
    ],
    "src/agent_hub/crypto/vectors.py": [
        (
            "- `agent_hub/<area>/vectors.py` with a module attribute `SECTIONS` (e.g. `agent_hub.log.vectors`), and\n",
            "- `agent_runner/crypto/<area>/vectors.py` with a module attribute `SECTIONS` (e.g.\n"
            "  `agent_runner.crypto.log.vectors`), and\n",
        ),
        ("import agent_hub\n", "import agent_runner.crypto\n"),
        (
            "PACKAGE_ROOT = Path(agent_hub.__file__).parent\n",
            "PACKAGE_ROOT = Path(agent_runner.crypto.__file__).parent\n",
        ),
        (
            '    """`SECTIONS` of this module, of every `agent_hub/<area>/vectors.py` and of '
            "`<tools_dir>/vectors_*.py`.\n",
            '    """`SECTIONS` of this module, of every `<package_root>/<area>/vectors.py` and of '
            "`<tools_dir>/vectors_*.py`.\n",
        ),
        (
            '            module = importlib.import_module(f"agent_hub.{area}.vectors")\n',
            '            module = importlib.import_module(f"agent_runner.crypto.{area}.vectors")\n',
        ),
    ],
}

# Exact text blocks of a reference test and their replacement (after the names are replaced).
TEST_ADAPTATIONS: dict[str, list[tuple[str, str]]] = {
    "tests/platform/hub/crypto/test_agent.py": [
        # After renaming, agent_runner.crypto.agents sorts after agent_runner.crypto (ruff isort).
        (
            "from agent_hub.agents import vectors as agent_vectors\n"
            "from agent_hub.crypto import agent, hpke, keys, vectors\n",
            "from agent_hub.crypto import agent, hpke, keys, vectors\n"
            "from agent_hub.agents import vectors as agent_vectors\n",
        ),
    ],
    "tests/platform/hub/crypto/test_vectors.py": [
        # contracts/tools/make_vectors.py imports agent_hub; the hub suite runs it, the runner copy must not.
        ("import subprocess\n", ""),
        (
            "\n\ndef test_make_vectors_script_is_a_thin_caller() -> None:\n"
            "    result = subprocess.run(\n"
            '        [sys.executable, str(CONTRACTS / "tools" / "make_vectors.py"), "--check"],\n'
            "        capture_output=True,\n"
            "        text=True,\n"
            "        check=False,\n"
            "    )\n"
            "    assert result.returncode == 0, result.stderr\n",
            "",
        ),
        (
            '        "from agent_hub.crypto.vectors import Section\\nSECTIONS = [Section(',
            '        "from agent_runner.crypto.vectors import Section\\nSECTIONS = [Section(',
        ),
        (
            '    fake = types.ModuleType("agent_hub.zarea.vectors")\n',
            '    fake = types.ModuleType("agent_runner.crypto.zarea.vectors")\n',
        ),
        (
            '    monkeypatch.setitem(sys.modules, "agent_hub.zarea.vectors", fake)\n',
            '    monkeypatch.setitem(sys.modules, "agent_runner.crypto.zarea.vectors", fake)\n',
        ),
        (
            '        "from agent_hub.crypto.vectors import Section\\nSECTIONS = (Section(',
            '        "from agent_runner.crypto.vectors import Section\\nSECTIONS = (Section(',
        ),
    ],
    "tests/platform/hub/log/test_vectors_log.py": [
        ("REPO_ROOT = Path(__file__).resolve().parents[4]\n", "REPO_ROOT = Path(__file__).resolve().parents[5]\n"),
    ],
}


class CopyError(Exception):
    """The reference or the copy does not have the expected shape."""


@dataclass(frozen=True)
class Reference:
    """Read access to the reference files by repository path."""

    list_dir: Callable[[str], list[str]]
    read: Callable[[str], str]


def tree_reference(root: Path) -> Reference:
    """The reference files in the working tree at `root` (committed or not)."""

    def list_dir(directory: str) -> list[str]:
        return sorted(f"{directory}/{path.name}" for path in (root / directory).iterdir())

    def read(path: str) -> str:
        return (root / path).read_text(encoding="utf-8")

    return Reference(list_dir, read)


def rename_imports(text: str, names: Sequence[tuple[str, str]]) -> str:
    """Replace the module names on import lines only (`from x import ...`, `import x`)."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _IMPORT_LINE.match(line):
            renamed = line
            for old, new in names:
                renamed = re.sub(rf"\b{re.escape(old)}\b", new, renamed)
            lines[index] = renamed
    return "".join(lines)


def replace_block(text: str, old: str, new: str, where: str) -> str:
    """Replace `old` by `new`; `old` must occur exactly once."""
    if text.count(old) != 1:
        raise CopyError(f"{where}: adaptation block found {text.count(old)} times: {old[:60]!r}")
    return text.replace(old, new)


def origin_line(source: str) -> str:
    return f"Copied from {source}."


def add_origin(text: str, source: str) -> str:
    """Prepend the origin paragraph to the module docstring (an empty module gets a one-line docstring)."""
    line = origin_line(source)
    if not text:
        return f'"""{line}"""\n'
    if not text.startswith('"""'):
        raise CopyError(f"{source}: no module docstring")
    return f'"""{line}\n\n{text[3:]}'


def strip_origin(text: str, copy: str) -> tuple[str, str]:
    """`(text without the origin paragraph, source)`."""
    match = _ORIGIN_LINE.match(text)
    if match is None:
        raise CopyError(f"{copy}: the module docstring does not start with the origin")
    rest = '"""' + text[match.end() :] if match.group(0).endswith("\n\n") else text[match.end() :]
    return rest, match["source"]


def copy_module(text: str, source: str, adaptations: Sequence[tuple[str, str]]) -> str:
    """The copy of one reference module."""
    for old, new in adaptations:
        text = replace_block(text, old, new, source)
    text = rename_imports(text, MODULE_NAMES)
    if re.search(r"^(?:from|import) agent_hub\b", text, re.MULTILINE):
        raise CopyError(f"{source}: an import of agent_hub is left")
    return add_origin(text, source)


def normalize(copy_text: str, copy: str, adaptations: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """Undo `copy_module`: `(reference text, source)`."""
    text, source = strip_origin(copy_text, copy)
    text = rename_imports(text, [(new, old) for old, new in MODULE_NAMES])
    for old, new in reversed(adaptations):
        text = replace_block(text, rename_imports(new, [(n, o) for o, n in MODULE_NAMES]), old, copy)
    return text, source


def copy_test(text: str, source: str, adaptations: Sequence[tuple[str, str]]) -> str:
    """The copy of one reference test file: the adaptations, then the import lines renamed.

    Prose (docstrings, comments) keeps naming the reference modules; module names inside code strings are
    adaptations.
    """
    for old, new in adaptations:
        text = replace_block(text, old, new, source)
    text = rename_imports(text, MODULE_NAMES)
    if re.search(r"^(?:from|import) agent_hub\b|[\"']agent_hub\.|[\"']from agent_hub\b", text, re.MULTILINE):
        raise CopyError(f"{source}: agent_hub is left in code")
    return add_origin(text, source)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def target(path: str, dirs: Sequence[tuple[str, str]]) -> str:
    for reference_file, copy_file in SOURCE_FILES:
        if path == reference_file:
            return copy_file
    for reference_dir, copy_dir in dirs:
        if path.startswith(reference_dir + "/"):
            return copy_dir + path[len(reference_dir) :]
    raise CopyError(f"{path}: outside the copied directories")


def _walk(reference: Reference, directory: str) -> list[str]:
    found: list[str] = []
    for path in reference.list_dir(directory):
        if path.endswith(".py") or path.endswith(".json"):
            found.append(path)
        elif "." not in path.rsplit("/", 1)[-1] and not path.endswith("__pycache__"):
            found += _walk(reference, path)
    return found


def build(reference: Reference) -> tuple[dict[str, str], dict[str, object]]:
    """`(files to write by repository path, ORIGIN.json document)`."""
    files: dict[str, str] = {}
    entries: list[dict[str, object]] = []
    sources = [path for directory, _copy_dir in SOURCE_DIRS for path in reference.list_dir(directory)]
    sources += [reference_file for reference_file, _copy_file in SOURCE_FILES]
    for path in sources:
        if not path.endswith(".py"):
            continue
        text = reference.read(path)
        adaptations = SOURCE_ADAPTATIONS.get(path, [])
        copy = target(path, SOURCE_DIRS)
        files[copy] = copy_module(text, path, adaptations)
        entries.append(
            {
                "adaptations": [{"copy": new, "reference": old} for old, new in adaptations],
                "copy": copy,
                "sha256": sha256(text),
                "source": path,
            }
        )
    tests: list[dict[str, object]] = []
    for reference_dir, _copy_dir in TEST_DIRS:
        for path in _walk(reference, reference_dir):
            text = reference.read(path)
            copy = target(path, TEST_DIRS)
            files[copy] = copy_test(text, path, TEST_ADAPTATIONS.get(path, [])) if path.endswith(".py") else text
            tests.append({"copy": copy, "copy_sha256": sha256(files[copy]), "sha256": sha256(text), "source": path})
    origin: dict[str, object] = {
        "about": "Copies of src/agent_hub/{crypto,log}, src/agent_hub/agents/vectors.py and of their tests "
        "(ADR-027 section 2). sha256 is the hash of the reference file, copy_sha256 the hash of a copied test file; "
        "tests/platform/runner/crypto/copy_tool.py (make runner-crypto-copy) writes and checks the copies.",
        "files": entries,
        "tests": tests,
    }
    return files, origin


def render_origin(origin: dict[str, object]) -> str:
    return json.dumps(origin, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="report differences, write nothing")
    args = parser.parse_args(argv)
    files, origin = build(tree_reference(REPO_ROOT))
    files[ORIGIN_FILE] = render_origin(origin)
    differing = []
    for path, text in sorted(files.items()):
        file = REPO_ROOT / path
        if not file.is_file() or file.read_text(encoding="utf-8") != text:
            differing.append(path)
            if not args.check:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text, encoding="utf-8")
    for path in differing:
        print(("differs: " if args.check else "wrote: ") + path)
    return 1 if args.check and differing else 0


if __name__ == "__main__":
    sys.exit(main())
