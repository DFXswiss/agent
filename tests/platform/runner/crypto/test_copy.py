"""U-RUN-crypto (copy part): `agent_runner.crypto` is an unchanged copy of the hub's reference crypto.

`src/agent_runner/crypto/ORIGIN.json` names the sha256 of every reference module; `copy_tool.normalize` undoes the
origin paragraph, the renamed import lines and the recorded adaptations, so any other edit of a copy fails
`test_copy_matches_reference` (decisions/CRY-03.md). The reference lives in the same tree (ADR-061):
`test_copies_are_current` fails as soon as a reference changes without a new copy (`make runner-crypto-copy`), and
`test_nothing_in_the_copy_varies_without_a_reference_change` keeps commits, dates and hosts out of the copy (HOT-08).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
PACKAGE = REPO_ROOT / "src" / "agent_runner" / "crypto"
RUNNER_ONLY = {"src/agent_runner/crypto/storage.py", "src/agent_runner/crypto/agents/__init__.py"}
RUNNER_ONLY_TESTS = {
    f"tests/platform/runner/crypto/{name}"
    for name in (
        "copy_tool.py",
        "test_copy.py",
        "test_storage.py",
        "test_runner_vectors.py",
        "test_agent_vectors.py",
        "data/storage.json",
    )
}


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cry03_copy_tool", HERE / "copy_tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def origin() -> dict[str, Any]:
    result: dict[str, Any] = json.loads((PACKAGE / "ORIGIN.json").read_text(encoding="utf-8"))
    return result


def adaptations(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["reference"], item["copy"]) for item in entry["adaptations"]]


def reference_from_copies() -> Any:
    """A `Reference` rebuilt from the copies (normalised), without tests."""
    data = origin()
    files = {}
    for entry in data["files"]:
        text = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")
        files[entry["source"]] = tool.normalize(text, entry["copy"], adaptations(entry))[0]

    def list_dir(directory: str) -> list[str]:
        return sorted(path for path in files if path.rsplit("/", 1)[0] == directory)

    return tool.Reference(list_dir, files.__getitem__)


@pytest.mark.parametrize("entry", origin()["files"], ids=lambda entry: entry["copy"])
def test_copy_matches_reference(entry: dict[str, Any]) -> None:
    text = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")
    reference, source = tool.normalize(text, entry["copy"], adaptations(entry))
    assert tool.sha256(reference) == entry["sha256"], f"{entry['copy']} differs from {entry['source']}"
    assert source == entry["source"]
    assert adaptations(entry) == tool.SOURCE_ADAPTATIONS.get(entry["source"], [])


def test_copies_are_current() -> None:
    """The copies and `ORIGIN.json` are what the tool writes from the references in this tree."""
    files, document = tool.build(tool.tree_reference(REPO_ROOT))
    files[tool.ORIGIN_FILE] = tool.render_origin(document)
    differing = [path for path, text in sorted(files.items()) if (REPO_ROOT / path).read_text(encoding="utf-8") != text]
    assert differing == [], "reference changed: copy again with make runner-crypto-copy"
    assert tool.build(tool.tree_reference(REPO_ROOT)) == (
        {path: text for path, text in files.items() if path != tool.ORIGIN_FILE},
        document,
    )


# Modules the tool may import: none of them reads a commit, a clock, the host or the environment.
TOOL_IMPORTS = {"__future__", "argparse", "collections.abc", "dataclasses", "hashlib", "json", "pathlib", "re", "sys"}
# Values that change from run to run without a reference change.
VARYING = re.compile(
    r"\b[0-9a-f]{7,40}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}:\d{2}\b|\.local\b"
    r"|agent-core|/Users/|/home/",
    re.IGNORECASE,
)


def test_nothing_in_the_copy_varies_without_a_reference_change() -> None:
    """No commit, date, time or host in an origin line or in `ORIGIN.json`, and none the tool could read."""
    data = origin()
    assert set(data) == {"about", "files", "tests"}
    assert all(set(entry) == {"adaptations", "copy", "sha256", "source"} for entry in data["files"])
    assert all(set(entry) == {"copy", "copy_sha256", "sha256", "source"} for entry in data["tests"])
    for key in ("files", "tests"):
        copies = [entry["copy"] for entry in data[key]]
        assert copies == sorted(copies), f"ORIGIN.json {key} not sorted by copy"
    without_adaptations = {**data, "files": [{**entry, "adaptations": []} for entry in data["files"]]}
    assert not VARYING.search(json.dumps(without_adaptations)), "ORIGIN.json carries a varying value"
    for entry in data["files"] + [item for item in data["tests"] if item["copy"].endswith(".py")]:
        first = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8").split("\n", 1)[0]
        assert first in {f'"""Copied from {entry["source"]}.', f'"""Copied from {entry["source"]}."""'}, entry["copy"]
    imported: set[str] = set()
    for node in ast.walk(ast.parse((HERE / "copy_tool.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == TOOL_IMPORTS
    for found in (
        "commit 0123456789ab",
        "at 0123456789ab",
        "2026-11-24",
        "12:00:00",
        "runner" + ".local",
        "agent-core",
    ):
        assert VARYING.search(found), found


def test_origin_lists_every_copied_module() -> None:
    data = origin()
    listed = [entry["copy"] for entry in data["files"]]
    present = {str(path.relative_to(REPO_ROOT)) for path in PACKAGE.rglob("*.py")} - RUNNER_ONLY
    assert sorted(listed) == sorted(present)
    sources = [entry["source"] for entry in data["files"]]
    assert all(tool.target(source, tool.SOURCE_DIRS) == copy for source, copy in zip(sources, listed, strict=True))
    assert set(tool.SOURCE_ADAPTATIONS) <= set(sources)


def test_copies_import_nothing_from_agent_hub() -> None:
    for entry in origin()["files"] + origin()["tests"]:
        text = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")
        assert not re.search(r"^(?:from|import) agent_(?:hub|core)\b", text, re.MULTILINE), entry["copy"]


def test_tool_reproduces_the_copies_and_origin() -> None:
    files, document = tool.build(reference_from_copies())
    for path, text in files.items():
        assert (REPO_ROOT / path).read_text(encoding="utf-8") == text, path
    assert len(files) == len(origin()["files"])
    assert document == {**origin(), "tests": []}


@pytest.mark.parametrize("entry", origin()["tests"], ids=lambda entry: entry["copy"])
def test_copied_test_is_unchanged(entry: dict[str, Any]) -> None:
    assert tool.sha256((REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")) == entry["copy_sha256"]
    assert tool.target(entry["source"], tool.TEST_DIRS) == entry["copy"]


def test_origin_lists_every_copied_test() -> None:
    listed = sorted(entry["copy"] for entry in origin()["tests"])
    present = {
        str(path.relative_to(REPO_ROOT))
        for path in HERE.rglob("*")
        if path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
    }
    assert sorted(present - RUNNER_ONLY_TESTS) == listed


def test_a_changed_copy_is_visible() -> None:
    entry = next(item for item in origin()["files"] if item["copy"].endswith("/aead.py"))
    text = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")
    for changed in (
        text.replace("NONCE_BYTES = 12", "NONCE_BYTES = 16"),
        text.replace("from agent_runner.crypto.errors import fail", "from agent_runner.errors import fail"),
        text + "\n",
    ):
        assert tool.sha256(tool.normalize(changed, entry["copy"], [])[0]) != entry["sha256"]
    with pytest.raises(tool.CopyError, match="does not start with the origin"):
        tool.normalize(text.split("\n\n", 1)[1], entry["copy"], [])


MODULE = '''"""Doc of m.

`agent_hub.log` stays prose.
"""

import agent_hub
from agent_hub.crypto import aead
from agent_hub.log.state import ORG

ROOT = agent_hub.__file__
'''


def test_copy_module_round_trip_with_adaptations() -> None:
    blocks = [
        ("import agent_hub\n", "import agent_runner.crypto\n"),
        ("ROOT = agent_hub", "ROOT = agent_runner.crypto"),
    ]
    copy = tool.copy_module(MODULE, "src/agent_hub/crypto/m.py", blocks)
    assert copy.startswith('"""Copied from src/agent_hub/crypto/m.py.\n\nDoc of m.\n')
    assert "from agent_runner.crypto import aead\n" in copy
    assert "from agent_runner.crypto.log.state import ORG\n" in copy
    assert "`agent_hub.log` stays prose." in copy
    assert tool.normalize(copy, "c", blocks) == (MODULE, "src/agent_hub/crypto/m.py")


def test_copy_module_refusals() -> None:
    with pytest.raises(tool.CopyError, match="found 0 times"):
        tool.copy_module(MODULE, "m.py", [("missing", "x")])
    with pytest.raises(tool.CopyError, match="import of agent_hub is left"):
        tool.copy_module(MODULE, "m.py", [])
    with pytest.raises(tool.CopyError, match="no module docstring"):
        tool.copy_module("X = 1\n", "m.py", [])
    with pytest.raises(tool.CopyError, match="outside the copied directories"):
        tool.target("src/agent_hub/db/x.py", tool.SOURCE_DIRS)
    with pytest.raises(tool.CopyError, match="outside the copied directories"):
        tool.target("src/agent_hub/agents/routes.py", tool.SOURCE_DIRS)
    assert (
        tool.target("src/agent_hub/agents/vectors.py", tool.SOURCE_DIRS) == "src/agent_runner/crypto/agents/vectors.py"
    )


def test_empty_module_round_trip() -> None:
    copy = tool.copy_module("", "src/agent_hub/log/__init__.py", [])
    assert copy == '"""Copied from src/agent_hub/log/__init__.py."""\n'
    assert tool.normalize(copy, "c", []) == ("", "src/agent_hub/log/__init__.py")


def test_copy_test_renames_imports_and_refuses_leftovers() -> None:
    test = '"""T (agent_hub.log)."""\n\nfrom agent_hub.log import verify\n\nX = verify\n'
    copy = tool.copy_test(test, "tests/platform/hub/log/test_x.py", [])
    assert "from agent_runner.crypto.log import verify\n" in copy
    assert "T (agent_hub.log)." in copy
    with pytest.raises(tool.CopyError, match="left in code"):
        tool.copy_test('"""T."""\nX = "agent_hub.zarea"\n', "t.py", [])


def test_main_check_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = reference_from_copies()
    monkeypatch.setattr(tool, "tree_reference", lambda _root: reference)
    # The rebuilt reference has no tests, so only their list in ORIGIN.json differs from the repository.
    assert tool.main(["--check"]) == 1
    assert capsys.readouterr().out == "differs: src/agent_runner/crypto/ORIGIN.json\n"
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    assert tool.main(["--check"]) == 1
    assert "differs: src/agent_runner/crypto/ORIGIN.json" in capsys.readouterr().out
    assert tool.main([]) == 0
    assert "wrote: src/agent_runner/crypto/aead.py" in capsys.readouterr().out
    assert tool.main(["--check"]) == 0
    # Writing again changes nothing.
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert tool.main([]) == 0
    assert capsys.readouterr().out == ""
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_tree_reference_reads_the_working_tree(tmp_path: Path) -> None:
    (tmp_path / "d" / "__pycache__").mkdir(parents=True)
    (tmp_path / "d" / ".hidden.py").write_text("H\n", encoding="utf-8")
    (tmp_path / "d" / "b.py").write_text("B\n", encoding="utf-8")
    (tmp_path / "d" / "a.py").write_text("A\n", encoding="utf-8")
    reference = tool.tree_reference(tmp_path)
    assert reference.list_dir("d") == ["d/__pycache__", "d/a.py", "d/b.py"]
    assert reference.read("d/a.py") == "A\n"
