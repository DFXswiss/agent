"""Compute, check or write `contracts/VERSION` (docs/platform/spec/07-testing.md section 4).

VERSION is the SHA-256 over the manifest lines `<sha256 of the file>  <relative path>\\n` of every file under
`contracts/` except `VERSION`, sorted by POSIX path. The same definition is recomputed by the copy check in the
agent repository.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "contracts"


def contract_files(root: Path) -> list[str]:
    """The POSIX paths, relative to `root`, of every hashed file in sorted order."""
    found = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or SKIPPED_DIRS.intersection(rel.parts[:-1]):
            continue
        if rel.name == ".DS_Store" or rel.as_posix() == "VERSION":
            continue
        found.append(rel.as_posix())
    return sorted(found)


def compute(root: Path) -> str:
    manifest = hashlib.sha256()
    for rel in contract_files(root):
        digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        manifest.update(f"{digest}  {rel}\n".encode())
    return manifest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="contracts/VERSION")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write VERSION")
    mode.add_argument("--check", action="store_true", help="exit 1 if VERSION is stale")
    args = parser.parse_args(argv)
    value = compute(args.root)
    version_file = args.root / "VERSION"
    if args.write:
        version_file.write_text(value + "\n")
    elif args.check:
        current = version_file.read_text().strip() if version_file.is_file() else ""
        if current != value:
            print(f"contracts/VERSION is stale: {current or 'missing'} != {value}", file=sys.stderr)
            return 1
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
