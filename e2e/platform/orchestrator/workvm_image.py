"""Shared work image cache `cache-wimg-<sha12>` for the work VM scenarios (agent docs/platform/spec/11-work-vm.md
section 4, ADR-056; same rule as the golden cache in `orchestrator.golden`).

`<sha12>` = first 12 hex digits of the SHA-256 over the lines `<sha256(file)>  <path>` of every file of
`images/workvm.yaml`, `images/workvm/` and `images/versions.json` (and, when present, the build logic
`src/agent_runner/workvm/image.py`) of the agent checkout, sorted by path. A run whose selected specs carry
`@needs-workvm` ensures the cache on the runner device: an existing instance is used as is; otherwise it is built with
`agent-runner workvm image build --name <sha12>` (self mode, prefix `cache-`) under the lock directory
`<lock dir>/wimg-<sha12>.lock`. A build downloads packages, so the run refuses a cache it had to build itself (like
the golden cache): build it before the run with this module.

    python -m orchestrator.workvm_image --agent <checkout> --python <venv python> --home <scratch> [--lock-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from orchestrator.golden import BUILT_MARK, CACHE_PREFIX, Run, _run, acquire, pid_alive

INPUTS = ("images/workvm.yaml", "images/workvm", "images/versions.json")
OPTIONAL_INPUTS = ("src/agent_runner/workvm/image.py",)
SKIP_PARTS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})
SKIP_FILES = frozenset({".DS_Store"})


class WorkImageError(Exception):
    pass


def _files(root: Path, sub: str) -> list[Path]:
    path = root / sub
    if path.is_file():
        return [path]
    return [p for p in sorted(path.rglob("*")) if p.is_file()]


def tree_sha(root: Path) -> str:
    missing = [sub for sub in INPUTS if not (root / sub).exists()]
    if missing:
        raise WorkImageError(f"work image inputs missing in the agent checkout: {', '.join(missing)}")
    entries = []
    for sub in INPUTS + tuple(s for s in OPTIONAL_INPUTS if (root / s).exists()):
        for path in _files(root, sub):
            rel = path.relative_to(root)
            if path.name not in SKIP_FILES and not SKIP_PARTS.intersection(rel.parts[:-1]):
                entries.append((rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(entries))
    return hashlib.sha256(lines.encode()).hexdigest()


def cache_name(root: Path) -> str:
    return f"{CACHE_PREFIX}wimg-{tree_sha(root)[:12]}"


def ensure(
    agent: Path, python: str, home: Path, lock_dir: Path, *, run: Run = _run, alive: Callable[[int], bool] = pid_alive
) -> str:
    name = cache_name(agent)
    sha12 = name.rsplit("-", 1)[1]
    lock = lock_dir / f"wimg-{sha12}.lock"
    acquire(lock, alive=alive)
    try:
        if name in run(["limactl", "list", "--quiet"], None).stdout.split():
            return name
        env = dict(
            os.environ,
            AGENT_RUNNER_HOME=str(home),
            AGENT_RUNNER_VM_PREFIX=CACHE_PREFIX,
            AGENT_RUNNER_TEST_MODE="1",
            AGENT_RUNNER_WORKVM_SLOTS="1",
            AGENT_RUNNER_WORKVM_SLOTS_SELF="1",
        )
        built = run([python, "-m", "agent_runner", "workvm", "image", "build", "--name", sha12], env)
        if built.returncode != 0 or name not in run(["limactl", "list", "--quiet"], None).stdout.split():
            raise WorkImageError(f"work image build failed: {(built.stdout + built.stderr).strip()[-300:]}")
        print(f"{BUILT_MARK} {name}", file=sys.stderr)
        return name
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def main(argv: Sequence[str] | None = None, *, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.workvm_image", description=__doc__.splitlines()[0])
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--lock-dir", type=Path, default=Path("~/wp/locks"))
    args = parser.parse_args(argv)
    try:
        print(ensure(args.agent.expanduser(), args.python, args.home.expanduser(), args.lock_dir.expanduser(), run=run))
    except WorkImageError as exc:
        print(f"workvm image: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
