"""U-RUN-crypto (copy part): `agent_runner.crypto` is an unchanged copy of agent-core's reference crypto.

`src/agent_runner/crypto/ORIGIN.json` names the reference commit and the sha256 of every reference module;
`copy_tool.normalize` undoes the origin paragraph, the renamed import lines and the recorded adaptations, so any
other edit of a copy fails `test_copy_matches_reference` (decisions/CRY-03.md describes the update).
"""

from __future__ import annotations

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

    return tool.Reference(data["commit"], list_dir, files.__getitem__)


@pytest.mark.parametrize("entry", origin()["files"], ids=lambda entry: entry["copy"])
def test_copy_matches_reference(entry: dict[str, Any]) -> None:
    text = (REPO_ROOT / entry["copy"]).read_text(encoding="utf-8")
    reference, source, commit = tool.normalize(text, entry["copy"], adaptations(entry))
    assert tool.sha256(reference) == entry["sha256"], f"{entry['copy']} differs from {entry['source']}"
    assert source == entry["source"]
    assert commit == origin()["commit"][:12]
    assert adaptations(entry) == tool.SOURCE_ADAPTATIONS.get(entry["source"], [])


def test_origin_lists_every_copied_module() -> None:
    data = origin()
    assert re.fullmatch(r"[0-9a-f]{40}", data["commit"])
    assert data["repository"] == "agent-core"
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
    copy = tool.copy_module(MODULE, "src/agent_hub/crypto/m.py", "a" * 40, blocks)
    assert copy.startswith('"""Copied from agent-core src/agent_hub/crypto/m.py at aaaaaaaaaaaa.\n\nDoc of m.\n')
    assert "from agent_runner.crypto import aead\n" in copy
    assert "from agent_runner.crypto.log.state import ORG\n" in copy
    assert "`agent_hub.log` stays prose." in copy
    assert tool.normalize(copy, "c", blocks) == (MODULE, "src/agent_hub/crypto/m.py", "aaaaaaaaaaaa")


def test_copy_module_refusals() -> None:
    with pytest.raises(tool.CopyError, match="found 0 times"):
        tool.copy_module(MODULE, "m.py", "a" * 40, [("missing", "x")])
    with pytest.raises(tool.CopyError, match="import of agent_hub is left"):
        tool.copy_module(MODULE, "m.py", "a" * 40, [])
    with pytest.raises(tool.CopyError, match="no module docstring"):
        tool.copy_module("X = 1\n", "m.py", "a" * 40, [])
    with pytest.raises(tool.CopyError, match="outside the copied directories"):
        tool.target("src/agent_hub/db/x.py", tool.SOURCE_DIRS)
    with pytest.raises(tool.CopyError, match="outside the copied directories"):
        tool.target("src/agent_hub/agents/routes.py", tool.SOURCE_DIRS)
    assert (
        tool.target("src/agent_hub/agents/vectors.py", tool.SOURCE_DIRS) == "src/agent_runner/crypto/agents/vectors.py"
    )


def test_empty_module_round_trip() -> None:
    copy = tool.copy_module("", "src/agent_hub/log/__init__.py", "b" * 40, [])
    assert copy == '"""Copied from agent-core src/agent_hub/log/__init__.py at bbbbbbbbbbbb."""\n'
    assert tool.normalize(copy, "c", []) == ("", "src/agent_hub/log/__init__.py", "bbbbbbbbbbbb")


def test_copy_test_renames_imports_and_refuses_leftovers() -> None:
    test = '"""T (agent_hub.log)."""\n\nfrom agent_hub.log import verify\n\nX = verify\n'
    copy = tool.copy_test(test, "tests/platform/hub/log/test_x.py", "c" * 40, [])
    assert "from agent_runner.crypto.log import verify\n" in copy
    assert "T (agent_hub.log)." in copy
    with pytest.raises(tool.CopyError, match="left in code"):
        tool.copy_test('"""T."""\nX = "agent_hub.zarea"\n', "t.py", "c" * 40, [])


def test_main_check_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = reference_from_copies()
    monkeypatch.setattr(tool, "git_reference", lambda _path, _rev: reference)
    # The rebuilt reference has no tests, so only their list in ORIGIN.json differs from the repository.
    assert tool.main(["--agent-core", "unused", "--rev", "HEAD", "--check"]) == 1
    assert capsys.readouterr().out == "differs: src/agent_runner/crypto/ORIGIN.json\n"
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    assert tool.main(["--agent-core", "unused", "--rev", "HEAD", "--check"]) == 1
    assert "differs: src/agent_runner/crypto/ORIGIN.json" in capsys.readouterr().out
    assert tool.main(["--agent-core", "unused", "--rev", "HEAD"]) == 0
    assert "wrote: src/agent_runner/crypto/aead.py" in capsys.readouterr().out
    assert tool.main(["--agent-core", "unused", "--rev", "HEAD", "--check"]) == 0
