#!/usr/bin/env bash
# Copy contracts/ from an agent-core checkout into this repository (docs/platform/spec/07-testing.md section 4,
# ADR-027 section 3). The copy is exact: files missing in agent-core are deleted here; node_modules, caches and
# .DS_Store are never copied. The source must be consistent: its contracts/VERSION must equal the SHA-256 over the
# manifest lines `<sha256 of the file>  <path>\n` of all other files, sorted by POSIX path; otherwise nothing is
# copied. The copy is checked the same way afterwards.
#
#   scripts/sync-contracts.sh <agent-core checkout>
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/sync-contracts.sh <agent-core checkout>" >&2
  exit 2
fi
source_dir="$1/contracts"
target_dir="$(cd "$(dirname "$0")/.." && pwd)/contracts"
if [ ! -f "$source_dir/VERSION" ]; then
  echo "sync-contracts: $source_dir/VERSION not found" >&2
  exit 1
fi

# Prints "ok" or "stale <recorded> <computed>" for a contracts directory (same definition as
# agent-core scripts/contracts_version.py and tests/platform/test_contracts_version.py).
check_version() {
  python3 -I - "$1" <<'PY'
import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
skipped = {"node_modules", "__pycache__", ".pytest_cache"}
files = sorted(
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file()
    and not skipped.intersection(path.relative_to(root).parts[:-1])
    and path.name != ".DS_Store"
    and path.relative_to(root).as_posix() != "VERSION"
)
manifest = "".join(f"{hashlib.sha256((root / rel).read_bytes()).hexdigest()}  {rel}\n" for rel in files)
computed = hashlib.sha256(manifest.encode()).hexdigest()
recorded = (root / "VERSION").read_text().strip()
print("ok" if recorded == computed else f"stale {recorded} {computed}")
PY
}

state="$(check_version "$source_dir")"
if [ "$state" != "ok" ]; then
  echo "sync-contracts: contracts/VERSION of the source is stale (${state#stale }); run make contracts-version in agent-core" >&2
  exit 1
fi
mkdir -p "$target_dir"
rsync -a --delete --exclude node_modules --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store \
  "$source_dir/" "$target_dir/"
state="$(check_version "$target_dir")"
if [ "$state" != "ok" ]; then
  echo "sync-contracts: the copy does not match contracts/VERSION (${state#stale })" >&2
  exit 1
fi
echo "sync-contracts: contracts $(cat "$target_dir/VERSION")"
