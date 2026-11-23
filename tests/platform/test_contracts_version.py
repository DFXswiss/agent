"""contracts/VERSION is current (CON-04 basis, docs/platform/spec/07-testing.md section 4)."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("contracts_version", REPO_ROOT / "scripts" / "contracts_version.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def test_version_is_current() -> None:
    version = (REPO_ROOT / "contracts" / "VERSION").read_text()
    assert version == script.compute(REPO_ROOT / "contracts") + "\n", "run `make contracts-version`"


def test_default_root_is_the_contracts_directory() -> None:
    assert script.DEFAULT_ROOT == REPO_ROOT / "contracts"


def _tree(root: Path) -> None:
    (root / "api").mkdir(parents=True)
    (root / "api" / "a.json").write_text("{}\n")
    (root / "README.md").write_text("x\n")
    (root / "VERSION").write_text("old\n")
    (root / "api" / "VERSION").write_text("nested files named VERSION are hashed\n")
    (root / ".DS_Store").write_bytes(b"\0")
    for skipped in ("node_modules", "__pycache__", ".pytest_cache"):
        (root / "tools" / skipped).mkdir(parents=True, exist_ok=True)
        (root / "tools" / skipped / "x").write_text("ignored")


def test_manifest_definition(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert script.contract_files(tmp_path) == ["README.md", "api/VERSION", "api/a.json"]
    lines = "".join(
        f"{hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()}  {rel}\n"
        for rel in ("README.md", "api/VERSION", "api/a.json")
    )
    assert script.compute(tmp_path) == hashlib.sha256(lines.encode()).hexdigest()


def test_write_then_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _tree(tmp_path)
    assert script.main(["--root", str(tmp_path), "--check"]) == 1
    assert "stale: old" in capsys.readouterr().err
    assert script.main(["--root", str(tmp_path), "--write"]) == 0
    value = capsys.readouterr().out.strip()
    assert (tmp_path / "VERSION").read_text() == value + "\n"
    assert script.main(["--root", str(tmp_path), "--check"]) == 0


def test_check_without_version_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "x").write_text("x")
    assert script.main(["--root", str(tmp_path), "--check"]) == 1
    assert "stale: missing" in capsys.readouterr().err


def test_print_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == hashlib.sha256(b"").hexdigest()
    assert not (tmp_path / "VERSION").exists()
