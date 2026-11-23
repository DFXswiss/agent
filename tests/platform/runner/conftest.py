"""Shared fixtures of the runner suites (ADR-027 section 6; owner RUN-01).

PostgreSQL is real: `AGENT_RUNNER_TEST_PG` names an admin DSN of a running server, otherwise one ephemeral cluster
is created per session with `initdb` (pattern of the legacy `tests/conftest.py`). Every test that asks for
`runner_dsn` gets its own database `t_run_<hex>`, dropped afterwards.

External tools are injected (07-testing.md section 1): `fake_tools` records argv lists and answers from a script,
`fake_clock` is a monotonic clock that only moves when the code under test sleeps, `make_host` builds a `Host`
with all of them and a valid environment.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_cli.pg import create_database, drop_database, ensure_cluster, stop_cluster
from agent_runner.encoding import b64u_encode
from agent_runner.host import Account, Host

TEST_PG_ENV = "AGENT_RUNNER_TEST_PG"
# A syntactically valid Ed25519 public key (32 bytes, b64u); public test data, not a secret.
ROOT_KEY = b64u_encode(bytes(range(32)))


@pytest.fixture(scope="session")
def runner_pg_admin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Admin DSN of the test PostgreSQL server."""
    configured = os.environ.get(TEST_PG_ENV)
    if configured:
        yield configured
        return
    data = tmp_path_factory.mktemp("runner-pg")
    dsn = ensure_cluster(data)
    yield dsn
    stop_cluster(data)


@pytest.fixture
def runner_dsn(runner_pg_admin: str) -> Iterator[str]:
    """DSN of a fresh, empty database for one test."""
    name = "t_run_" + uuid.uuid4().hex[:12]
    dsn = create_database(runner_pg_admin, name)
    yield dsn
    drop_database(runner_pg_admin, name)


@pytest.fixture
def runner_home(tmp_path: Path) -> Path:
    """A not yet existing runner home below a private temporary directory."""
    base = tmp_path / "rh"
    base.mkdir(mode=0o700)
    return base / "home"


def runner_env(home: Path, **extra: str) -> dict[str, str]:
    """A valid environment for the daemon commands (test mode, loopback hub)."""
    env = {
        "AGENT_RUNNER_HOME": str(home),
        "AGENT_RUNNER_HUB_URL": "http://127.0.0.1:9",
        "AGENT_RUNNER_PLATFORM_ROOT_KEY": ROOT_KEY,
        "AGENT_RUNNER_TEST_MODE": "1",
        "PYTEST_CURRENT_TEST": "runner",
        "PATH": "/usr/bin:/bin",
    }
    env.update(extra)
    return env


@dataclass
class FakeTools:
    """Records every tool call; `answers[tool]` is a list of (returncode, stdout, stderr), consumed in order."""

    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    answers: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
    chowns: list[tuple[Path, int, int]] = field(default_factory=list)

    def chown(self, path: Path, uid: int, gid: int) -> None:
        self.chowns.append((path, uid, gid))

    def runner(self, name: str) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
        def call(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append((name, list(argv)))
            queue = self.answers.get(name) or [(0, "", "")]
            code, out, err = queue.pop(0) if len(queue) > 1 else queue[0]
            return subprocess.CompletedProcess([name, *argv], code, out, err)

        return call


@pytest.fixture
def fake_tools() -> FakeTools:
    return FakeTools()


@dataclass
class FakeClock:
    """Monotonic test clock; `sleep` advances it and yields to the event loop."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@dataclass
class Captured:
    host: Host
    stdout: io.StringIO
    stderr: io.StringIO


@pytest.fixture
def make_host(fake_tools: FakeTools, fake_clock: FakeClock, tmp_path: Path) -> Callable[..., Captured]:
    """Factory of a fully injected `Host`; keyword arguments override fields."""

    def build(env: dict[str, str] | None = None, **overrides: object) -> Captured:
        out, err = io.StringIO(), io.StringIO()
        fields: dict[str, object] = {
            "env": env if env is not None else {},
            "platform": "darwin",
            "stdout": out,
            "stderr": err,
            "launchctl": fake_tools.runner("launchctl"),
            "limactl": fake_tools.runner("limactl"),
            "tmux": fake_tools.runner("tmux"),
            "ssh": fake_tools.runner("ssh"),
            "launch_daemons_dir": tmp_path / "LaunchDaemons",
            "clock": fake_clock,
            "sleep": fake_clock.sleep,
            "install_signals": False,
            "user": lambda: "runner",
            "program": lambda: [sys.executable, "-m", "agent_runner", "run"],
            "cli": lambda: [sys.executable, "-m", "agent_runner"],
            "sudo": fake_tools.runner("sudo"),
            "geteuid": lambda: 501,
            "account": lambda name: (
                None if name == "ghost" else Account(uid=501, gid=20, home=tmp_path / "users" / name)
            ),
            "chown": fake_tools.chown,
        }
        fields.update(overrides)
        return Captured(host=Host(**fields), stdout=out, stderr=err)  # type: ignore[arg-type]

    return build


@pytest.fixture
def area_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str]], str]:
    """Create an importable test root package with area sub-packages; returns its name.

    The argument maps `<area>/<module>.py` to source text; every area directory gets an `__init__.py`.
    """
    counter = iter(range(1_000_000))

    def make(files: dict[str, str]) -> str:
        name = f"runner_test_pkg_{uuid.uuid4().hex[:8]}_{next(counter)}"
        root = tmp_path / "pkgs" / name
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            init = path.parent / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
            path.write_text(text, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
        return name

    return make


@pytest.fixture
def env_for() -> Callable[..., dict[str, str]]:
    """`env_for(home, **extra)`: a valid daemon environment (see `runner_env`)."""
    return runner_env


@pytest.fixture
def root_key() -> str:
    return ROOT_KEY
