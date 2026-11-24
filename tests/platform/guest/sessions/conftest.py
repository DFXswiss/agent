"""Fixtures of U-GST-sessions: a session manager with a fake operating system and a recording tmux runner.

The manager runs against the real control process of the guest fixtures (journal, session cache, schemas); only the
root-only calls (``System``) and the tmux binary are replaced. The Linux suite (``guest_linux``) runs the same code
with ``LinuxSystem`` and real tmux/systemd.
"""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_guest import sessions, tmux
from agent_guest.control import ControlContext
from agent_guest.control_routes import capture as capture_routes
from agent_guest.driver import ClaudeCodeDriver

SESSION = "01a1ee56-5e00-7003-8f1b-340691d99f03"
TASK = "01a1ee56-5e00-7005-8f1b-340691d99f05"
PARENT = "01a1ee56-5e00-7006-8f1b-340691d99f06"
PROMPT_ID = "01a1ee56-5e00-7007-8f1b-340691d99f07"
RESUME = "5d0c9a3e-7f61-4b6e-9a42-3c1d2e4f5a6b"
RULES = "# agent.dfx.swiss session rules\n\nYou must not run programs.\n"
CONTEXT = "- Organization: Example (`01a1ee56-5e00-7001-8f1b-340691d99f01`)"
INTERPRETER = ("/usr/bin/python3.12", "/usr/lib/python3.12/lib-dynload")
EXEC_PATHS = ("/usr/lib/aarch64-linux-gnu/libc.so.6", "/usr/lib/ld-linux-aarch64.so.1")
LIMITS = {"memory_max": 4294967296, "cpu_quota": 200, "tasks_max": 256, "runtime_max_sec": 28800}
VERSION = "2.1.330"


def request(**changes: Any) -> dict[str, Any]:
    """A valid ``POST /v1/sessions`` body (main session, platform access) with ``changes`` applied."""
    body: dict[str, Any] = {
        "session_id": SESSION,
        "task_id": TASK,
        "parent_session_id": None,
        "depth": 0,
        "harness": {"context_markdown": CONTEXT},
        "model": {
            "access": "platform",
            "model_id": "deepseek/deepseek-v4.1-flash-1017",
            "env": {"ANTHROPIC_MODEL": "deepseek/deepseek-v4.1-flash-1017"},
        },
        "initial_prompt": {"prompt_id": PROMPT_ID, "text": "Summarise the repository."},
        "limits": dict(LIMITS),
    }
    body.update(copy.deepcopy(changes))
    return body


@dataclass
class FakeSystem:
    """``sessions.System`` in memory: users, chowns and stopped units are recorded; failures can be injected."""

    used: set[int] = field(default_factory=set)
    users: dict[str, int] = field(default_factory=dict)
    chowned: list[tuple[str, int]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    fail_add: bool = False
    fail_remove: bool = False
    oom: set[str] = field(default_factory=set)
    oom_checked: list[tuple[str, float | None]] = field(default_factory=list)

    def used_ids(self) -> set[int]:
        return set(self.used) | set(self.users.values())

    def user_uid(self, name: str) -> int | None:
        return self.users.get(name)

    def add_user(self, name: str, uid: int, home: str) -> None:
        if self.fail_add:
            raise OSError("useradd failed")
        self.users[name] = uid

    def remove_user(self, name: str) -> bool:
        self.removed.append(name)
        if self.fail_remove:
            return False
        self.users.pop(name, None)
        return True

    def chown(self, path: str, uid: int) -> None:
        self.chowned.append((path, uid))

    def stop_unit(self, name: str) -> None:
        self.stopped.append(name)

    def oom_killed(self, name: str, since: float | None = None) -> bool:
        self.oom_checked.append((name, since))
        return name in self.oom


@dataclass
class TmuxRunner:
    """Stands in for ``subprocess.run`` of ``tmux.Tmux``: records argv, answers with ``codes[subcommand]``."""

    calls: list[list[str]] = field(default_factory=list)
    codes: dict[str, int] = field(default_factory=dict)
    stdout: str = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        assert kwargs["env"] == tmux.CLIENT_ENV
        sub = argv[argv.index("-S") + 2]
        return subprocess.CompletedProcess(argv, self.codes.get(sub, 0), self.stdout, "")

    def subcommands(self) -> list[str]:
        return [argv[argv.index("-S") + 2] for argv in self.calls]


@pytest.fixture
def session_paths(tmp_path: Path) -> sessions.Paths:
    rules = tmp_path / "RULES.md"
    rules.write_text(RULES, encoding="utf-8")
    return sessions.Paths(
        sessions_root=str(tmp_path / "srv"),
        harness_dir=str(tmp_path / "run" / "harness"),
        rules_file=str(rules),
        launch_dir=str(tmp_path / "run" / "launch"),
        meta_dir=str(tmp_path / "lib" / "sessions"),
    )


@pytest.fixture
def fake_system() -> FakeSystem:
    return FakeSystem()


@pytest.fixture
def tmux_runner() -> TmuxRunner:
    return TmuxRunner()


@pytest.fixture
def manager(
    context: ControlContext,
    fake_system: FakeSystem,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
) -> Iterator[sessions.SessionManager]:
    created = sessions.SessionManager(
        context=context,
        system=fake_system,
        tmux=tmux.Tmux(socket="/run/agent-guest/tmux.sock", run=tmux_runner, scope=False),
        driver=ClaudeCodeDriver(VERSION),
        paths=session_paths,
        interpreter=INTERPRETER,
        exec_paths=EXEC_PATHS,
        watch_interval=0.01,
        tmux_check=0.05,
    )
    sessions.install(context, created)
    try:
        yield created
    finally:
        created.close()
        stop_captures(context)


def stop_captures(context: ControlContext) -> None:
    """End every capture the test left running (follower, recorder, scanner threads), so that no thread of this test
    reads its closed journal later and writes to the stderr of another test."""
    found = vars(context).get("_capture")
    if isinstance(found, capture_routes.Capture):
        for session_id in list(found.sessions):
            found.agent_ended(session_id)
