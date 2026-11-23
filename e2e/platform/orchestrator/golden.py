"""Shared golden image cache `cache-golden-<sha12>` (ADR-027 section 7, same rule as `tools/wp/golden-cache.sh`).

`<sha12>` = first 12 hex digits of the SHA-256 over the lines `<sha256(file)>  <path>` of every file below
`images/`, `src/agent_guest/` (and, when present, `contracts/` and `src/agent_runner/vm/`) of the agent checkout,
sorted by path. A run that needs VMs (a selected spec carries `@needs-vm`) ensures the cache on the runner device:
an existing instance is used as is; otherwise it is built with `agent-runner image build` under a lock directory
(`<lock dir>/golden-<sha12>.lock` holding the PID; a lock without a living holder is broken). The cache is a TEST_MODE
golden (`--test-model-port 9`, the runner in TEST_MODE; same as `tools/wp/golden-cache.sh`): only such a golden honours
the hook fault switch (E2E-34) and keeps the root of the Lima default user for the test hooks (SCN-02, ADR-036). The
port is the closed port 9: the guest reaches the doubles through the tunnel on its loopback (ADR-034).

    python -m orchestrator.golden --agent <checkout> --python <agent venv python> --home <scratch> [--lock-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

INPUTS = ("images", "src/agent_guest")
OPTIONAL_INPUTS = ("contracts", "src/agent_runner/vm")
SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".pytest_cache"})
SKIP_FILES = frozenset({".DS_Store"})
CACHE_PREFIX = "cache-"
TEST_MODEL_PORT = 9
EXIT_NOT_YET = 3
LOCK_WAIT_S = 5.0
# Written to stderr when `ensure` had to build the cache (the run refuses that, see `run.Run.ensure_golden`).
BUILT_MARK = "golden: built"

Run = Callable[[list[str], dict[str, str] | None], "subprocess.CompletedProcess[str]"]


class GoldenError(Exception):
    pass


def tree_sha(root: Path) -> str:
    missing = [sub for sub in INPUTS if not (root / sub).is_dir()]
    if missing:
        raise GoldenError(f"golden inputs missing in the agent checkout: {', '.join(missing)}")
    entries = []
    for sub in INPUTS + tuple(s for s in OPTIONAL_INPUTS if (root / s).is_dir()):
        for path in sorted((root / sub).rglob("*")):
            rel = path.relative_to(root)
            if path.is_file() and path.name not in SKIP_FILES and not SKIP_DIRS.intersection(rel.parts[:-1]):
                entries.append((rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(entries))
    return hashlib.sha256(lines.encode()).hexdigest()


def cache_name(root: Path) -> str:
    return f"{CACHE_PREFIX}golden-{tree_sha(root)[:12]}"


def _run(argv: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)


def acquire(lock: Path, *, alive: Callable[[int], bool], sleep: Callable[[float], None] = time.sleep) -> None:
    while True:
        try:
            lock.mkdir(parents=True)
        except FileExistsError:
            holder = (lock / "pid").read_text().strip() if (lock / "pid").exists() else ""
            if holder.isdigit() and not alive(int(holder)):
                shutil.rmtree(lock, ignore_errors=True)
                continue
            sleep(LOCK_WAIT_S)
            continue
        (lock / "pid").write_text(str(os.getpid()))
        return


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure(
    agent: Path, python: str, home: Path, lock_dir: Path, *, run: Run = _run, alive: Callable[[int], bool] = pid_alive
) -> str:
    name = cache_name(agent)
    lock = lock_dir / f"golden-{name.rsplit('-', 1)[1]}.lock"
    acquire(lock, alive=alive)
    try:
        listing = run(["limactl", "list", "--quiet"], None).stdout.split()
        if name in listing:
            return name
        if run([python, "-m", "agent_runner", "image", "--help"], None).returncode != 0:
            raise GoldenError("agent-runner image build does not exist yet (VM-01)")
        env = dict(
            os.environ, AGENT_RUNNER_HOME=str(home), AGENT_RUNNER_VM_PREFIX=CACHE_PREFIX, AGENT_RUNNER_TEST_MODE="1"
        )
        tag = ["--tag", name.rsplit("-", 1)[1], "--test-model-port", str(TEST_MODEL_PORT)]
        built = run([python, "-m", "agent_runner", "image", "build", *tag], env)
        if built.returncode != 0 or name not in run(["limactl", "list", "--quiet"], None).stdout.split():
            raise GoldenError(f"golden build failed: {(built.stdout + built.stderr).strip()[-300:]}")
        print(f"{BUILT_MARK} {name}", file=sys.stderr)
        return name
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def main(argv: Sequence[str] | None = None, *, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.golden", description=__doc__.splitlines()[0])
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--lock-dir", type=Path, default=Path("~/wp/locks"))
    args = parser.parse_args(argv)
    try:
        print(ensure(args.agent.expanduser(), args.python, args.home.expanduser(), args.lock_dir.expanduser(), run=run))
    except GoldenError as exc:
        print(f"golden: {exc}", file=sys.stderr)
        return EXIT_NOT_YET if "does not exist yet" in str(exc) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
