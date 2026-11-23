"""The contracts copy is intact: contracts/VERSION matches its content (CON-04 basis,
docs/platform/spec/07-testing.md section 4). The definition equals scripts/contracts_version.py of agent-core."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})


def contracts_version(root: Path) -> str:
    """SHA-256 over the lines `<sha256 of the file>  <relative path>\\n`, sorted by POSIX path, without VERSION."""
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not SKIPPED_DIRS.intersection(path.relative_to(root).parts[:-1])
        and path.name != ".DS_Store"
        and path.relative_to(root).as_posix() != "VERSION"
    )
    manifest = "".join(f"{hashlib.sha256((root / rel).read_bytes()).hexdigest()}  {rel}\n" for rel in files)
    return hashlib.sha256(manifest.encode()).hexdigest()


def test_version_is_current() -> None:
    assert (REPO_ROOT / "contracts" / "VERSION").read_text() == contracts_version(REPO_ROOT / "contracts") + "\n"


def test_definition(tmp_path: Path) -> None:
    (tmp_path / "api" / "node_modules").mkdir(parents=True)
    (tmp_path / "api" / "a.json").write_text("{}\n")
    (tmp_path / "api" / "VERSION").write_text("hashed\n")
    (tmp_path / "api" / "node_modules" / "x").write_text("skipped")
    (tmp_path / ".DS_Store").write_bytes(b"\0")
    (tmp_path / "VERSION").write_text("skipped\n")
    lines = "".join(
        f"{hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()}  {rel}\n" for rel in ("api/VERSION", "api/a.json")
    )
    assert contracts_version(tmp_path) == hashlib.sha256(lines.encode()).hexdigest()
