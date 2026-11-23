"""Fixtures of the orchestrator unit tests: fake executables for everything the orchestrator starts.

Real stay: Python, sockets, HTTP, `ps`, `lsof`, `git`, `tar`, `sh`. Replaced (scripts in a temporary `bin/` put
first on PATH): the Python of hub and agent checkout (`fake_python.py`: hub, migrate, doubles, runner), the
PostgreSQL binaries, `node` (web build), `limactl`, `sysctl`, `uv` and the Playwright binary. The orchestrator
gives its processes an allow-list environment, so the fakes read their switches from `$HOME/fake.env` (the test
HOME) and log to `$HOME/fake.log`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from orchestrator import run

SUITE = Path(__file__).resolve().parents[1]
CORE = SUITE.parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SUITE))

SCRIPTS = {
    "initdb": """
        while [ $# -gt 0 ]; do case "$1" in -D) mkdir -p "$2"; shift 2;; *) shift;; esac; done
        [ -z "$FAKE_INITDB_FAIL" ] || { echo "initdb: broken" >&2; exit 1; }
    """,
    "postgres": """
        echo "LOG:  database system is ready to accept connections"
        trap 'exit 0' TERM
        while :; do sleep 0.05; done
    """,
    "psql": """
        for last; do :; done
        echo "psql ${PGOPTIONS:-rw}|$last" >> "$FAKE_LOG"
        [ -z "$FAKE_PSQL_FAIL" ] || { echo "psql: broken" >&2; exit 1; }
        case "$last" in *json_agg*) printf '%s\\n' "${FAKE_PSQL_ROWS:-[]}";; esac
    """,
    "pg_dump": """
        while [ $# -gt 0 ]; do case "$1" in -f) out="$2"; shift 2;; *) shift;; esac; done
        [ -z "$FAKE_DUMP_FAIL" ] || { echo "pg_dump: broken" >&2; exit 1; }
        echo "-- dump ${FAKE_DUMP_TEXT:-}" > "$out"
    """,
    "node": """
        case "$2" in *"minify: true"*) minify=1;; *) minify=0;; esac
        echo "node BUILD_COMMIT=${BUILD_COMMIT:-} KEY=${VITE_PLATFORM_ROOT_KEY:-} MINIFY=$minify" >> "$FAKE_LOG"
        [ -z "$FAKE_NODE_FAIL" ] || { echo "build: broken" >&2; exit 1; }
    """,
    "limactl": """
        [ -z "$FAKE_LIMA_MISSING" ] || exit 127
        list="$HOME/lima.jsonl"
        touch "$list"
        [ -z "$FAKE_LIMA_FAIL" ] || { echo "limactl: broken" >&2; exit 1; }
        case "$1" in
          list) if [ "$2" = "--quiet" ]; then sed 's/.*"name": *"\\([^"]*\\)".*/\\1/' "$list"; else cat "$list"; fi;;
          delete) echo "delete $3" >> "$FAKE_LOG"
            [ -n "$FAKE_LIMA_STICKY" ] || { grep -v "\\"$3\\"" "$list" > "$list.n"; mv "$list.n" "$list"; };;
        esac
    """,
    "sysctl": """
        echo "${FAKE_MEMSIZE:-68719476736}"
    """,
    "uv": """
        echo "uv $*" >> "$FAKE_LOG"
        [ -z "$FAKE_UV_FAIL" ] || { echo "uv: broken" >&2; exit 1; }
    """,
}


def write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


PRELUDE = 'set -a; [ -f "$HOME/fake.env" ] && . "$HOME/fake.env"; set +a; FAKE_LOG="$HOME/fake.log"'


def shell(body: str) -> str:
    lines = [line[8:] if line.startswith("        ") else line for line in body.strip("\n").splitlines()]
    return "#!/bin/sh\n" + PRELUDE + "\n" + "\n".join(lines) + "\n"


@dataclass
class World:
    root: Path
    home: Path
    bin: Path
    agent: Path
    artifacts: Path
    python: Path
    playwright: Path
    environ: dict[str, str]

    @property
    def log(self) -> Path:
        return self.home / "fake.log"

    @property
    def lima(self) -> Path:
        return self.home / "lima.jsonl"

    def logged(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def set(self, **switches: str) -> None:
        """Switches of the fakes (`FAKE_<NAME>=value` in `$HOME/fake.env`)."""
        env = self.home / "fake.env"
        lines = env.read_text().splitlines() if env.exists() else []
        lines += [f"FAKE_{name.upper()}={value}" for name, value in switches.items()]
        env.write_text("\n".join(lines) + "\n")

    def clear(self) -> None:
        (self.home / "fake.env").unlink(missing_ok=True)


def free_base() -> int:
    """A port base of the package range (`WP_PORT_BASE`) or 21000 outside the work-package tools."""
    return int(os.environ.get("WP_PORT_BASE", "21000"))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    bin_dir = tmp_path / "bin"
    for name, body in SCRIPTS.items():
        write_exec(bin_dir / name, shell(body))
    fake_py = (TESTS / "fake_python.py").read_text().replace("#!python", f"#!{sys.executable}", 1)
    fake_py = fake_py.replace("@REAL@", sys.executable)
    agent = tmp_path / "agent"
    (agent / "src" / "agent_runner").mkdir(parents=True)
    (agent / "contracts").mkdir()
    shutil.copy(CORE / "contracts" / "VERSION", agent / "contracts" / "VERSION")
    (agent / "pyproject.toml").write_text('[project]\nname = "agent"\n')
    python = write_exec(agent / ".venv" / "bin" / "python", fake_py)
    digest = hashlib.sha256((agent / "pyproject.toml").read_bytes()).hexdigest()
    (agent / ".venv" / ".e2e-install").write_text(digest + " platform,login\n")
    fake_pw = (TESTS / "fake_playwright.py").read_text().replace("#!python", f"#!{sys.executable}", 1)
    playwright = write_exec(tmp_path / "playwright", fake_pw)
    home = tmp_path / "home"
    home.mkdir()
    environ = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "WP_PORT_BASE": str(free_base()),
        "AGENT_RUNNER_VM_PREFIX": "wp-test-",
        "AGENT_RUNNER_SRC": str(agent),
        "E2E_VM_MEMORY_BUDGET_MIB": "65536",
    }
    monkeypatch.setenv("PATH", environ["PATH"])
    monkeypatch.setenv("HOME", str(home))
    # The suite's real specs may need VMs; whole-run tests opt in to the VM path themselves.
    monkeypatch.setattr(run, "needs_vm", lambda suite, only: False)
    return World(tmp_path, home, bin_dir, agent, tmp_path / "artifacts", python, playwright, environ)
