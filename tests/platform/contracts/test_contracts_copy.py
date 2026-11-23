"""CON-04: `contracts/` is an exact copy of agent-core's contracts (docs/platform/spec/07-testing.md section 4).

The copy's hash is recomputed and compared with `contracts/VERSION`, and `scripts/sync-contracts.sh` is exercised
against a synthetic agent-core checkout."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "sync-contracts.sh"
SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})


def version_of(root: Path) -> str:
    """SHA-256 over `<sha256 of the file>  <path>\\n` of every file except VERSION, sorted by POSIX path."""
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


def test_copy_hash_matches_version() -> None:
    contracts = REPO_ROOT / "contracts"
    assert (contracts / "VERSION").read_text() == version_of(contracts) + "\n"


def test_copy_holds_the_contracts() -> None:
    """The copy is the full contract set of agent-core, not only README and VERSION."""
    contracts = REPO_ROOT / "contracts"
    for rel in ("common.json", "api/INDEX.json", "enums/error-codes.json", "tools/validate.mjs", "package-lock.json"):
        assert (contracts / rel).is_file(), rel


def make_core(root: Path) -> Path:
    contracts = root / "contracts"
    (contracts / "api").mkdir(parents=True)
    (contracts / "api" / "a.json").write_text('{"a": 1}\n')
    (contracts / "README.md").write_text("contracts\n")
    for skipped in ("node_modules", "__pycache__"):
        (contracts / "tools" / skipped).mkdir(parents=True)
        (contracts / "tools" / skipped / "x.js").write_text("skipped")
    (contracts / ".DS_Store").write_bytes(b"\0")
    (contracts / "VERSION").write_text(version_of(contracts) + "\n")
    return root


def make_repo(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "sync-contracts.sh")
    contracts = root / "contracts"
    (contracts / "old").mkdir(parents=True)
    (contracts / "old" / "stale.json").write_text("{}")
    (contracts / "node_modules").mkdir()
    (contracts / "node_modules" / "keep.js").write_text("local install")
    (contracts / "VERSION").write_text("old\n")
    return root


def sync(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / "sync-contracts.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def test_sync_copies_exactly(tmp_path: Path) -> None:
    core = make_core(tmp_path / "core")
    repo = make_repo(tmp_path / "repo")
    result = sync(repo, str(core))
    assert result.returncode == 0, result.stderr
    copy = repo / "contracts"
    version = (core / "contracts" / "VERSION").read_text()
    assert result.stdout == f"sync-contracts: contracts {version}"
    assert (copy / "VERSION").read_text() == version
    assert (copy / "api" / "a.json").read_text() == '{"a": 1}\n'
    assert not (copy / "old").exists()
    assert not (copy / "tools" / "node_modules").exists()
    assert not (copy / "tools" / "__pycache__").exists()
    assert not (copy / ".DS_Store").exists()
    assert (copy / "node_modules" / "keep.js").read_text() == "local install"
    assert version_of(copy) + "\n" == version
    again = sync(repo, str(core))
    assert again.returncode == 0
    assert again.stdout == result.stdout


def test_sync_refuses_a_stale_source(tmp_path: Path) -> None:
    core = make_core(tmp_path / "core")
    (core / "contracts" / "api" / "a.json").write_text('{"a": 2}\n')
    repo = make_repo(tmp_path / "repo")
    result = sync(repo, str(core))
    assert result.returncode == 1
    assert "contracts/VERSION of the source is stale" in result.stderr
    assert "make contracts-version" in result.stderr
    assert (repo / "contracts" / "old" / "stale.json").exists(), "nothing is copied"


def test_sync_refuses_a_missing_source(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    result = sync(repo, str(tmp_path / "nowhere"))
    assert result.returncode == 1
    assert "VERSION not found" in result.stderr


def test_sync_needs_one_argument(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    for args in ((), ("a", "b")):
        result = sync(repo, *args)
        assert result.returncode == 2
        assert result.stderr.startswith("usage: scripts/sync-contracts.sh <agent-core checkout>")


def test_sync_checks_the_copy(tmp_path: Path) -> None:
    """A copy that does not reproduce VERSION (here: an rsync that copies nothing) fails."""
    core = make_core(tmp_path / "core")
    repo = make_repo(tmp_path / "repo")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "rsync").write_text("#!/bin/sh\nexit 0\n")
    (fake / "rsync").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    result = sync(repo, str(core), env=env)
    assert result.returncode == 1
    assert "the copy does not match contracts/VERSION" in result.stderr
