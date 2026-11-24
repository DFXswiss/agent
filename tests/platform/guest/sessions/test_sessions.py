"""U-GST-sessions: session creation, start, end, stop, resume and retention (05-runner.md §4.3, §6.1, §6.3)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pwd
import stat
import subprocess
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from agent_guest import harness, registry, sessions, tmux, toolsready
from agent_guest.apisock import token_sha256
from agent_guest.control import ControlContext
from agent_guest.control_routes import hooks as control_hooks
from agent_guest.driver import ClaudeCodeDriver
from agent_guest.prompts import prompt_sha256
from agent_guest.sandbox import unit
from agent_guest.state import Session
from tests.platform.guest.conftest import FakeClock
from tests.platform.guest.sessions.conftest import (
    PARENT,
    PROMPT_ID,
    RESUME,
    RULES,
    SESSION,
    TASK,
    FakeSystem,
    TmuxRunner,
    request,
)

LONG_PROMPT = 100 * 1024 + 1
"""Longer than any command line would take (every initial prompt waits for the TUI anyway, HOT-09)."""

USER = unit.user_name(SESSION)
RULES_VERSION = hashlib.sha256(RULES.encode()).hexdigest()


def records(context: ControlContext, kind: str = "session.state") -> list[dict[str, Any]]:
    found, _ = context.journal.read(after=0, limit=1000)
    return [record["body"] for record in found if record["kind"] == kind]


def states(context: ControlContext) -> list[str]:
    return [body["state"] for body in records(context)]


def failure(context: ControlContext) -> str | None:
    """The reason of the session's ``failed`` record (an undelivered initial prompt is refused after it, HOT-09)."""
    found = [record for record in records(context) if record["state"] == "failed"]
    return str(found[-1]["reason"]) if found else None


def launch(paths: sessions.Paths) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(Path(paths.launch_dir, f"{SESSION}.json").read_text())
    return value


def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def finish(manager: sessions.SessionManager, code: int) -> None:
    """The pane command of the current run ended with ``code``."""
    Path(manager._running[SESSION].status_file).write_text(f"{code}\n")


def unit_env(paths: sessions.Paths) -> dict[str, str | None]:
    """``--setenv`` of the launch file: value, or ``None`` for a variable taken from the launch ``env``."""
    content = launch(paths)
    argv = content["argv"][: content["argv"].index("--")]
    found: dict[str, str | None] = {}
    for arg in argv:
        if arg.startswith("--setenv="):
            name, sep, value = arg.removeprefix("--setenv=").partition("=")
            found[name] = value if sep else None
    return found


# -- pure helpers ---------------------------------------------------------------------------------------------


def test_free_uid_skips_users_and_groups() -> None:
    assert sessions.free_uid(set()) == 20000
    assert sessions.free_uid({20000, 20001, 20003, 19999}) == 20002
    with pytest.raises(sessions.SessionError) as raised:
        sessions.free_uid(set(range(20000, 30000)))
    assert raised.value.code == "sandbox_failed"


def test_tokens_are_32_random_bytes_b64u() -> None:
    first, second = sessions.new_token(), sessions.new_token()
    assert len(first) == 43
    assert first != second
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_pre_seeded_claude_state_trusts_the_workspace() -> None:
    assert json.loads(sessions.claude_json("/srv/sessions/x/work")) == {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "projects": {"/srv/sessions/x/work": {"hasTrustDialogAccepted": True}},
    }


# -- creation -------------------------------------------------------------------------------------------------


def test_create_builds_user_directories_harness_and_records(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    session_paths: sessions.Paths,
) -> None:
    fake_system.used = {20000, 20001}
    manager.create(request())
    assert fake_system.users == {USER: 20002}
    base = Path(session_paths.sessions_root, SESSION)
    assert stat.S_IMODE(base.stat().st_mode) == 0o711
    tree = [base / "work", base / "home", base / "home" / ".claude"]
    for path in tree:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    seeded = base / "home" / ".claude" / ".claude.json"
    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600
    assert json.loads(seeded.read_text())["projects"] == {str(base / "work"): {"hasTrustDialogAccepted": True}}
    assert fake_system.chowned == [(str(path), 20002) for path in [*tree, seeded]]
    harness_file = Path(session_paths.harness_dir, f"{SESSION}.md")
    assert stat.S_IMODE(harness_file.stat().st_mode) == 0o444
    text = harness_file.read_text()
    assert text.startswith(RULES.rstrip("\n") + "\n\n# Context of this session")
    assert "Restarted session" not in text
    session = context.state.session(SESSION)
    assert session is not None
    assert (session.uid, session.state, session.workspace) == (20002, "sandbox_ready", str(base / "work"))
    assert session.claude_session_id is None
    assert len(session.token_sha256) == 64
    assert records(context) == [
        {"state": "sandbox_ready", "harness_version": RULES_VERSION, "at": records(context)[0]["at"]}
    ]
    meta = json.loads(Path(session_paths.meta_dir, f"{SESSION}.json").read_text())
    assert meta["stopped_at"] is None
    assert len(meta["request_sha256"]) == 64
    assert meta["identity"] == {"task_id": TASK, "parent_session_id": None, "depth": 0}


def test_the_token_is_only_stored_as_hash(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths, tmp_path: Path
) -> None:
    manager.create(request())
    manager.start(SESSION)
    content = launch(session_paths)
    env = content["env"]
    token = env["PLATFORM_SESSION_TOKEN"]
    assert env == {"PLATFORM_SESSION_TOKEN": token, "ANTHROPIC_AUTH_TOKEN": token}
    assert not any(token in arg for arg in content["argv"]), "never on a command line"
    assert unit_env(session_paths)["PLATFORM_SESSION_TOKEN"] is None
    assert len(token) == 43
    session = context.state.session(SESSION)
    assert session is not None
    assert session.token_sha256 == token_sha256(token)
    assert context.state.resolve(token_sha256(token)) is not None
    database = Path(context.config.journal).read_bytes() + b"".join(
        path.read_bytes() for path in Path(context.config.journal).parent.glob("journal.db*")
    )
    assert token.encode() not in database
    assert token not in Path(session_paths.meta_dir, f"{SESSION}.json").read_text()


def test_environment_is_exactly_the_allow_list(manager: sessions.SessionManager, session_paths: sessions.Paths) -> None:
    manager.create(request())
    manager.start(SESSION)
    argv = launch(session_paths)["argv"]
    keys = set(unit_env(session_paths))
    fixed = {key for key, _ in unit.FIXED_MODEL_ENV} | {key for key, _ in unit.SESSION_ENV}
    assert keys == fixed | {
        "ANTHROPIC_MODEL",
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "PLATFORM_SESSION_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
    }
    assert not any(arg.startswith(("--setenv=AGENT_", "-E")) for arg in argv)


@pytest.mark.parametrize(
    "changes",
    [
        {"depth": 1},
        {"parent_session_id": PARENT},
        {"model": {"access": "subscription", "model_id": None, "env": {"ANTHROPIC_MODEL": "m"}}},
        {"harness": {"context_markdown": " \n "}},
        {"harness": {"context_markdown": "a\x00b"}},
        {"initial_prompt": {"prompt_id": PROMPT_ID, "text": "a\x00b"}},
    ],
)
def test_inconsistent_requests_are_invalid(
    manager: sessions.SessionManager, context: ControlContext, changes: dict[str, Any]
) -> None:
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request(**changes))
    assert raised.value.code == "request_invalid"
    assert context.state.session(SESSION) is None


def test_subagent_session_and_subscription_access(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    body = request(
        parent_session_id=PARENT,
        depth=1,
        model={"access": "subscription", "model_id": None, "env": {}},
        limits={"memory_max": 1073741824, "cpu_quota": 100, "tasks_max": 64, "runtime_max_sec": 1800},
    )
    body.pop("initial_prompt")
    manager.create(body)
    assert f"`{PARENT}` (you are a subagent session, depth 1)" in Path(session_paths.harness_file(SESSION)).read_text()
    assert manager.start(SESSION)
    argv = launch(session_paths)["argv"]
    props = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
    assert "RuntimeMaxSec=30min" in props
    assert props[-5:] == [
        "MemoryMax=1073741824",
        "CPUQuota=100%",
        "TasksMax=64",
        "RuntimeMaxSec=1800",
        "TimeoutStopSec=10s",
    ]
    assert "--model" not in argv
    assert argv[-1] == session_paths.harness_file(SESSION)
    with context.journal.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM prompt_registry").fetchone()[0] == 0


def test_repeated_request_is_answered_again_and_a_different_one_conflicts(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    manager.create(request())
    assert len(fake_system.users) == 1
    assert states(context) == ["sandbox_ready"]
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request(task_id=PARENT))
    assert raised.value.code == "id_conflict"


def test_existing_user_name_fails_the_sandbox_and_rolls_back(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem, session_paths: sessions.Paths
) -> None:
    fake_system.users[USER] = 25000
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request())
    assert raised.value.code == "sandbox_failed"
    assert fake_system.users == {USER: 25000}
    assert fake_system.removed == []
    assert not Path(session_paths.sessions_root, SESSION).exists()
    session = context.state.session(SESSION)
    assert session is not None
    assert (session.state, session.uid) == ("failed", -1)
    assert failure(context) == "sandbox_failed"
    with pytest.raises(sessions.SessionError) as again:
        manager.create(request())
    assert again.value.code == "state_conflict"


def refuse_write(path: str, text: str) -> None:
    raise OSError("read-only file system")


def test_failure_after_the_user_was_added_removes_what_was_created(
    manager: sessions.SessionManager,
    fake_system: FakeSystem,
    session_paths: sessions.Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "write", refuse_write)
    with pytest.raises(sessions.SessionError):
        manager.create(request())
    assert fake_system.removed == [USER]
    assert not Path(session_paths.sessions_root, SESSION).exists()


def test_a_leftover_directory_is_never_deleted(
    manager: sessions.SessionManager, fake_system: FakeSystem, session_paths: sessions.Paths
) -> None:
    leftover = Path(session_paths.sessions_root, SESSION)
    leftover.mkdir(parents=True)
    (leftover / "keep").write_text("x")
    with pytest.raises(sessions.SessionError):
        manager.create(request())
    assert (leftover / "keep").exists()
    assert fake_system.removed == [USER]


def test_failing_useradd_fails_the_sandbox(manager: sessions.SessionManager, fake_system: FakeSystem) -> None:
    fake_system.fail_add = True
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request())
    assert raised.value.code == "sandbox_failed"


@pytest.mark.parametrize("broken", ["rules", "exec_paths", "interpreter"])
def test_incomplete_image_fails_before_anything_is_created(
    *,
    context: ControlContext,
    fake_system: FakeSystem,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
    tmp_path: Path,
    broken: str,
) -> None:
    paths = session_paths
    if broken == "rules":
        Path(paths.rules_file).unlink()
    created = sessions.SessionManager(
        context=context,
        system=fake_system,
        tmux=tmux.Tmux(run=tmux_runner, scope=False),
        driver=ClaudeCodeDriver("2.1.330"),
        paths=sessions.Paths(
            **{**paths.__dict__, "exec_paths_file": str(tmp_path / "missing.json"), "python": str(tmp_path / "python3")}
        ),
        interpreter=None if broken == "interpreter" else ("/usr/bin/python3.12", "/usr/lib/python3.12/lib-dynload"),
        exec_paths=None if broken == "exec_paths" else ("/usr/lib/libc.so.6",),
    )
    with pytest.raises(sessions.SessionError) as raised:
        created.create(request())
    assert raised.value.code == "sandbox_failed"
    assert fake_system.users == {}
    session = context.state.session(SESSION)
    assert session is not None
    assert session.state == "failed"
    assert failure(context) == "sandbox_failed"


# -- start ----------------------------------------------------------------------------------------------------


def test_start_launches_the_unit_in_tmux_and_journals_agent_starting(
    manager: sessions.SessionManager,
    context: ControlContext,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
) -> None:
    events: list[tuple[str, str]] = []
    module = types.ModuleType("agent_guest.control_routes.fake_events")
    module.SESSION_EVENTS = {  # type: ignore[attr-defined]
        "sandbox_ready": lambda ctx, sid: events.append(("sandbox_ready", sid)),
        "agent_starting": lambda ctx, sid: events.append(("agent_starting", sid)),
    }
    original = registry.modules
    try:
        registry.modules = lambda package: [*original(package), module]
        manager.create(request())
        assert manager.start(SESSION) is True
    finally:
        registry.modules = original
    assert events == [("sandbox_ready", SESSION), ("agent_starting", SESSION)]
    launch_file = f"{session_paths.launch_dir}/{SESSION}.json"
    new_session = tmux_runner.calls[0]
    assert new_session[3:] == [
        "new-session", "-d", "-s", f"s-{SESSION}", "-x", "160", "-y", "48", "--",
        session_paths.helper_python, "-I", session_paths.helper, "exec", launch_file,
    ]  # fmt: skip
    content = launch(session_paths)
    assert content["status"] == manager._running[SESSION].status_file
    assert content["status"].startswith(f"{session_paths.launch_dir}/{SESSION}.")
    argv = content["argv"]
    assert argv[:5] == ["systemd-run", "--pty", f"--unit=agent-session-{SESSION}", "--collect", "--quiet"]
    claude = argv[argv.index("/opt/claude/claude", argv.index("--")) :]
    # HOT-09: the command line ends with the model, the prompt waits for the TUI and the tools
    assert claude[-2:] == ["--model", "deepseek/deepseek-v4.1-flash-1017"]
    assert "Summarise the repository." not in argv
    assert claude[claude.index("--append-system-prompt-file") + 1] == session_paths.harness_file(SESSION)
    launcher = argv[argv.index("--") + 1 : argv.index("/opt/claude/claude", argv.index("--"))]
    assert launcher[:3] == ["/usr/bin/python3.12", "-I", unit.LAUNCHER]
    assert states(context) == ["sandbox_ready", "agent_starting"]
    assert records(context)[1]["harness_version"] == RULES_VERSION
    session = context.state.session(SESSION)
    assert session is not None
    assert session.state == "agent_starting"
    with context.journal.transaction() as db:
        rows = db.execute("SELECT session_id, sha256, used_at FROM prompt_registry").fetchall()
    assert rows == [(SESSION, prompt_sha256("Summarise the repository."), None)]
    assert manager.start(SESSION) is False
    assert manager.deferred_prompt(SESSION) == (PROMPT_ID, "Summarise the repository.")
    assert manager.deferred_prompt(SESSION) is None


def test_start_of_an_unknown_session(manager: sessions.SessionManager) -> None:
    with pytest.raises(sessions.SessionError) as raised:
        manager.start(SESSION)
    assert raised.value.code == "not_found"


def test_a_long_initial_prompt_is_registered_and_deferred_like_every_other(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    text = "y" * LONG_PROMPT
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    argv = launch(session_paths)["argv"]
    assert text not in argv
    assert argv[-1] == "deepseek/deepseek-v4.1-flash-1017"
    assert manager.deferred_prompt(SESSION) == (PROMPT_ID, text)
    assert manager.deferred_prompt(SESSION) is None
    with context.journal.transaction() as db:
        assert db.execute("SELECT sha256 FROM prompt_registry").fetchone()[0] == prompt_sha256(text)


def test_a_prompt_the_registry_refuses_is_not_delivered(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    manager.create(request())
    with context.journal.transaction() as db:
        db.execute(
            "INSERT INTO prompt_registry (session_id, sha256, prompt_id, registered_at, used_at)"
            " VALUES (?, ?, ?, 'x', 'y')",
            (SESSION, prompt_sha256("Summarise the repository."), PROMPT_ID),
        )
    assert manager.start(SESSION)
    argv = launch(session_paths)["argv"]
    assert "Summarise the repository." not in argv
    assert argv[-1] == "deepseek/deepseek-v4.1-flash-1017"


def test_tmux_failure_fails_the_start(
    manager: sessions.SessionManager, context: ControlContext, tmux_runner: TmuxRunner, session_paths: sessions.Paths
) -> None:
    tmux_runner.codes["new-session"] = 1
    manager.create(request())
    assert manager.start(SESSION) is False
    assert states(context) == ["sandbox_ready", "agent_starting", "failed"]
    assert failure(context) == "agent_start_failed"
    assert not Path(session_paths.launch_dir, f"{SESSION}.json").exists()
    assert json.loads(Path(session_paths.meta_dir, f"{SESSION}.json").read_text())["stopped_at"] is not None


def test_unusable_unit_at_start_fails_the_sandbox(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.create(request())
    manager.exec_paths = ("relative/lib.so",)
    assert manager.start(SESSION) is False
    assert records(context)[-1] == {"state": "failed", "reason": "sandbox_failed", "at": records(context)[-1]["at"]}


# -- end of the agent ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "elapsed", "reason"),
    [(126, 0.0, "sandbox_failed"), (1, 0.0, "agent_start_failed"), (0, 60.0, "agent_exited")],
)
def test_unexpected_end_fails_the_session(
    *,
    manager: sessions.SessionManager,
    context: ControlContext,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
    code: int,
    elapsed: float,
    reason: str,
) -> None:
    clock = [100.0]
    manager.monotonic = lambda: clock[0]
    manager.create(request(resume_claude_session_id=RESUME))
    manager.start(SESSION)
    clock[0] += elapsed
    finish(manager, code)
    wait_for(lambda: "failed" in states(context))
    [failed] = [record for record in records(context) if record["state"] == "failed"]
    assert failed == {"state": "failed", "reason": reason, "claude_session_id": RESUME, "at": failed["at"]}
    wait_for(lambda: "kill-session" in tmux_runner.subcommands())
    assert not Path(session_paths.launch_dir, f"{SESSION}.status").exists()


def test_a_vanished_tmux_session_without_status_is_an_end(
    manager: sessions.SessionManager, context: ControlContext, tmux_runner: TmuxRunner
) -> None:
    tmux_runner.codes["has-session"] = 1
    manager.create(request())
    manager.start(SESSION)
    wait_for(lambda: "failed" in states(context))
    assert failure(context) == "agent_start_failed"


def test_a_status_written_while_the_session_vanished_is_used(
    manager: sessions.SessionManager, context: ControlContext, tmux_runner: TmuxRunner, session_paths: sessions.Paths
) -> None:
    original = tmux_runner.__call__

    def vanish(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "has-session" in argv:
            finish(manager, 126)
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **kwargs)

    manager.tmux = tmux.Tmux(run=vanish, scope=False)
    real_read = tmux.read_status
    reads = [0]

    def read(path: str) -> int | None:
        reads[0] += 1
        return None if reads[0] == 1 else real_read(path)

    tmux.read_status, saved = read, tmux.read_status
    try:
        manager.tmux_check = 0.0
        manager.create(request())
        manager.start(SESSION)
        wait_for(lambda: "failed" in states(context))
    finally:
        tmux.read_status = saved
    assert failure(context) == "sandbox_failed"


def test_agent_ended_event_and_failing_callbacks_are_logged(
    manager: sessions.SessionManager,
    context: ControlContext,
    session_paths: sessions.Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ended = threading.Event()

    def broken(ctx: ControlContext, sid: str) -> None:
        raise RuntimeError("recorder down")

    module = types.ModuleType("agent_guest.control_routes.fake_capture")
    module.SESSION_EVENTS = {"agent_ended": lambda ctx, sid: ended.set(), "sandbox_ready": broken}  # type: ignore[attr-defined]
    original = registry.modules
    registry.modules = lambda package: [*original(package), module]
    try:
        manager.create(request())
        manager.start(SESSION)
        finish(manager, 0)
        assert ended.wait(5)
    finally:
        registry.modules = original
    assert "session event sandbox_ready failed" in capsys.readouterr().err
    assert context.state.session(SESSION) is not None


def test_a_running_agent_is_watched_until_its_status_appears(
    manager: sessions.SessionManager, context: ControlContext, tmux_runner: TmuxRunner, session_paths: sessions.Paths
) -> None:
    manager.tmux_check = 0.0
    manager.create(request())
    manager.start(SESSION)
    wait_for(lambda: tmux_runner.subcommands().count("has-session") >= 3)
    assert states(context)[-1] == "agent_starting"
    finish(manager, 0)
    wait_for(lambda: "failed" in states(context))


def test_stop_without_meta_file(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    manager.create(request())
    Path(session_paths.meta_dir, f"{SESSION}.json").unlink()
    manager.stop(SESSION)
    assert states(context)[-1] == "stopped"
    assert not Path(session_paths.meta_dir, f"{SESSION}.json").exists()


def test_close_ends_the_watchers_without_touching_the_session(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    manager.create(request())
    manager.start(SESSION)
    manager.close()
    assert states(context) == ["sandbox_ready", "agent_starting"]


# -- stop and suspend ------------------------------------------------------------------------------------------


def test_stop_stops_the_unit_closes_the_window_and_keeps_the_directories(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
) -> None:
    manager.create(request())
    manager.start(SESSION)
    watcher = manager._running[SESSION].thread
    assert watcher is not None
    manager.stop(SESSION)
    finish(manager, -15)
    watcher.join(5)  # the watcher polls `has-session` until it sees the end of the pane: let it finish first
    assert not watcher.is_alive()
    assert fake_system.stopped == [f"agent-session-{SESSION}"] * 2
    # after the closed window nothing but the watcher's polling reaches tmux
    assert [sub for sub in tmux_runner.subcommands() if sub != "has-session"][-1] == "kill-session"
    assert states(context) == ["sandbox_ready", "agent_starting", "stopping", "stopped"]
    assert Path(session_paths.sessions_root, SESSION, "work").is_dir()
    assert Path(session_paths.harness_file(SESSION)).exists()
    session = context.state.session(SESSION)
    assert session is not None
    assert session.state == "stopped"
    assert context.state.resolve(session.token_sha256) is None
    meta = json.loads(Path(session_paths.meta_dir, f"{SESSION}.json").read_text())
    assert meta["stopped_at"] == context.journal.clock.now()
    manager.stop(SESSION)
    assert fake_system.stopped == [f"agent-session-{SESSION}"] * 2
    assert states(context)[-1] == "stopped"


def test_stop_before_the_start_and_of_unknown_sessions(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    with pytest.raises(sessions.SessionError):
        manager.stop(SESSION)
    manager.create(request())
    manager.stop(SESSION)
    assert states(context) == ["sandbox_ready", "stopping", "stopped"]
    assert manager.start(SESSION) is False
    with pytest.raises(ValueError, match="paused"):
        manager.stop(SESSION, "paused")


def test_stop_while_a_stop_runs_returns(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    manager.start(SESSION)
    inner: list[None] = []

    def reenter(name: str) -> None:
        manager.stop(SESSION)
        inner.append(None)

    fake_system.stop_unit = reenter  # type: ignore[method-assign]
    manager.stop(SESSION)
    assert inner == [None, None]
    assert states(context)[-2:] == ["stopping", "stopped"]


def test_suspend_and_resume_with_a_new_token(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    session_paths: sessions.Paths,
) -> None:
    manager.create(request())
    with pytest.raises(sessions.SessionError) as raised:
        manager.stop(SESSION, "suspended")
    assert raised.value.code == "state_conflict"
    manager.start(SESSION)
    first = context.state.session(SESSION)
    manager.stop(SESSION, "suspended")
    manager.stop(SESSION, "suspended")
    assert states(context) == ["sandbox_ready", "agent_starting", "suspended"]
    resumed = request(resume_claude_session_id=RESUME)
    resumed.pop("initial_prompt")
    manager.create(resumed)
    second = context.state.session(SESSION)
    assert first is not None
    assert second is not None
    assert second.token_sha256 != first.token_sha256
    assert second.uid == first.uid
    assert (second.state, second.claude_session_id) == ("sandbox_ready", RESUME)
    assert "## Restarted session" in Path(session_paths.harness_file(SESSION)).read_text()
    assert len(fake_system.users) == 1
    assert manager.start(SESSION)
    argv = launch(session_paths)["argv"]
    assert argv[argv.index("--resume") + 1] == RESUME
    assert records(context)[-1]["claude_session_id"] == RESUME


def test_the_late_end_of_a_suspended_run_leaves_the_resumed_session_alone(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    """The watcher of the suspended run may see its pane end only after the resume (AUD-02 F4): no failure then."""
    manager.create(request())
    manager.start(SESSION)
    suspended_run = manager._running[SESSION]
    manager.stop(SESSION, "suspended")
    resumed = request(resume_claude_session_id=RESUME)
    resumed.pop("initial_prompt")
    manager.create(resumed)
    manager._ended(SESSION, suspended_run, -1, 100.0)
    assert states(context) == ["sandbox_ready", "agent_starting", "suspended", "sandbox_ready"]
    assert manager.start(SESSION)


def test_resume_keeps_the_known_claude_session(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.create(request(resume_claude_session_id=RESUME))
    manager.start(SESSION)
    manager.stop(SESSION, "suspended")
    manager.create(request())
    session = context.state.session(SESSION)
    assert session is not None
    assert session.claude_session_id == RESUME


def test_resume_needs_the_same_user(
    manager: sessions.SessionManager, fake_system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.create(request())
    manager.start(SESSION)
    manager.stop(SESSION, "suspended")
    fake_system.users[USER] = 29999
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request())
    assert raised.value.code == "sandbox_failed"
    fake_system.users[USER] = 20000
    monkeypatch.setattr(harness, "write", refuse_write)
    with pytest.raises(sessions.SessionError) as again:
        manager.create(request())
    assert again.value.code == "sandbox_failed"


def test_stopped_sessions_cannot_be_created_again(manager: sessions.SessionManager) -> None:
    manager.create(request())
    manager.stop(SESSION)
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request())
    assert raised.value.code == "state_conflict"


# -- retention -------------------------------------------------------------------------------------------------


def test_cleanup_removes_sessions_seven_days_after_their_end(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    session_paths: sessions.Paths,
    clock: FakeClock,
) -> None:
    manager.create(request())
    manager.stop(SESSION)
    clock.advance(sessions.RETENTION_SECONDS - 1)
    assert manager.cleanup() == []
    clock.advance(1)
    assert manager.cleanup() == [SESSION]
    assert not Path(session_paths.sessions_root, SESSION).exists()
    assert not Path(session_paths.harness_file(SESSION)).exists()
    assert not Path(session_paths.meta_dir, f"{SESSION}.json").exists()
    assert fake_system.removed == [USER]
    assert manager.cleanup() == []


def test_cleanup_keeps_active_sessions_and_spares_foreign_users(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem, clock: FakeClock
) -> None:
    other = "01a1ee56-5e01-7008-8f1b-340691d99f08"
    manager.create(request())
    manager.create(request(session_id=other))
    manager.stop(other)
    fake_system.users[USER] = 20000
    fake_system.users[unit.user_name(other)] = 26000
    clock.advance(sessions.RETENTION_SECONDS)
    assert manager.cleanup() == [other]
    assert fake_system.removed == []


def test_cleanup_of_a_failed_creation(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem, clock: FakeClock
) -> None:
    fake_system.fail_add = True
    with pytest.raises(sessions.SessionError):
        manager.create(request())
    clock.advance(sessions.RETENTION_SECONDS)
    assert manager.cleanup() == []
    context.state.save_session(Session(SESSION, -1, "0" * 64, "failed", "/w", None, "2026-11-21T10:00:00.000Z"))
    manager._write_meta(SESSION, {"stopped_at": 0})
    assert manager.cleanup() == [SESSION]
    assert fake_system.removed == []


def test_unreadable_meta_counts_as_missing(manager: sessions.SessionManager, session_paths: sessions.Paths) -> None:
    Path(session_paths.meta_dir).mkdir(parents=True)
    path = Path(session_paths.meta_dir, f"{SESSION}.json")
    path.write_text("[]")
    assert manager._meta(SESSION) == {}
    path.write_text("{")
    assert manager._meta(SESSION) == {}


def test_cleanup_loop_runs_until_closed(manager: sessions.SessionManager, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[int] = []

    def cleanup() -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("busy")
        manager.close()
        return []

    manager.cleanup = cleanup  # type: ignore[method-assign]
    manager.cleanup_loop(0.001)
    assert len(calls) == 2
    assert "OSError" in capsys.readouterr().err


# -- extension points and process-wide access ------------------------------------------------------------------


def fake_modules(monkeypatch: pytest.MonkeyPatch, **attributes: Any) -> None:
    module = types.ModuleType("agent_guest.control_routes.fake")
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setattr(registry, "modules", lambda package: [module])


def handler(context: ControlContext, session_id: str) -> None:
    return None


def test_control_actions_are_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_modules(monkeypatch)
    assert sessions.control_actions() == {}
    fake_modules(monkeypatch, CONTROL_ACTIONS={"interrupt": handler})
    assert sessions.control_actions() == {"interrupt": handler}


@pytest.mark.parametrize(
    "attributes",
    [
        {"CONTROL_ACTIONS": {"stop": handler}},
        {"CONTROL_ACTIONS": ["interrupt"]},
        {"CONTROL_ACTIONS": {"interrupt": "not callable"}},
        {"SESSION_EVENTS": {"started": handler}},
        {"SESSION_EVENTS": {1: handler}},
    ],
)
def test_invalid_registrations_are_refused(monkeypatch: pytest.MonkeyPatch, attributes: dict[str, Any]) -> None:
    fake_modules(monkeypatch, **attributes)
    discover = sessions.control_actions if "CONTROL_ACTIONS" in attributes else sessions.session_events
    with pytest.raises(registry.RegistryError):
        discover()


def test_duplicate_actions_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    first = types.ModuleType("a")
    first.CONTROL_ACTIONS = {"interrupt": handler}  # type: ignore[attr-defined]
    second = types.ModuleType("b")
    second.CONTROL_ACTIONS = {"interrupt": handler}  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "modules", lambda package: [first, second])
    with pytest.raises(registry.RegistryError):
        sessions.control_actions()


def test_default_manager_is_created_once(context: ControlContext) -> None:
    created = sessions.manager(context)
    try:
        assert sessions.manager(context) is created
        assert isinstance(created.system, sessions.LinuxSystem)
        assert created.tmux.socket == tmux.SOCKET
        assert created.tmux.scope
        assert created.driver.version == context.info.claude_version
        assert created.paths == sessions.Paths()
    finally:
        created.close()
    assert created._cleanup is not None
    assert not created._cleanup.is_alive()


def test_module_level_entry_points(manager: sessions.SessionManager, context: ControlContext) -> None:
    text = "z" * LONG_PROMPT
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    assert sessions.credential_provisioned(context, SESSION) is True
    assert sessions.credential_provisioned(context, SESSION) is False
    assert sessions.deferred_prompt(context, SESSION) == (PROMPT_ID, text)
    sessions.stop_session(context, SESSION)
    assert states(context)[-1] == "stopped"


# -- LinuxSystem with a recording runner ----------------------------------------------------------------------


class Runs:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = codes or {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        assert kwargs["env"] == sessions.SYSTEM_ENV
        return subprocess.CompletedProcess(argv, self.codes.get(argv[0], 0), "", "")


def test_linux_system_commands() -> None:
    runs = Runs()
    system = sessions.LinuxSystem(run=runs)
    system.add_user(USER, 20005, "/srv/sessions/x/home")
    system.remove_user(USER)
    system.stop_unit("agent-session-x")
    assert runs.calls == [
        ["groupadd", "--gid", "20005", USER],
        ["useradd", "--uid", "20005", "--gid", "20005", "--no-create-home", "--home-dir", "/srv/sessions/x/home",
         "-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0", "--shell", "/usr/sbin/nologin", USER],
        ["userdel", USER],
        ["groupdel", USER],
        ["systemctl", "stop", "agent-session-x"],
    ]  # fmt: skip


def test_linux_system_failures() -> None:
    failing_group = sessions.LinuxSystem(run=Runs({"groupadd": 9}))
    with pytest.raises(OSError, match="groupadd"):
        failing_group.add_user(USER, 20005, "/h")
    runs = Runs({"useradd": 1})
    with pytest.raises(OSError, match="useradd"):
        sessions.LinuxSystem(run=runs).add_user(USER, 20005, "/h")
    assert runs.calls[-1] == ["groupdel", USER]


def test_linux_system_reads_the_user_database() -> None:
    """``chown`` needs root and is covered by the Linux suite."""
    system = sessions.LinuxSystem()
    assert os.getuid() in system.used_ids()
    assert os.getgid() in system.used_ids()
    assert system.user_uid(pwd.getpwuid(os.getuid()).pw_name) == os.getuid()
    assert system.user_uid("s-000000000000") is None


# -- review follow-ups -----------------------------------------------------------------------------------------


def test_a_replay_after_a_lost_pending_start_prepares_it_again(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    manager.create(request())
    first = context.state.session(SESSION)
    manager._starts.clear()  # what a restart of the control process loses
    assert manager.start(SESSION) is False
    manager.create(request())
    second = context.state.session(SESSION)
    assert first is not None
    assert second is not None
    assert second.token_sha256 != first.token_sha256
    assert "Restarted session" not in Path(session_paths.harness_file(SESSION)).read_text()
    assert manager.start(SESSION) is True


def test_a_resume_with_another_identity_conflicts(manager: sessions.SessionManager) -> None:
    manager.create(request())
    manager.start(SESSION)
    manager.stop(SESSION, "suspended")
    for changes in ({"task_id": PARENT}, {"depth": 1, "parent_session_id": PARENT}):
        with pytest.raises(sessions.SessionError) as raised:
            manager.create(request(**changes))
        assert raised.value.code == "id_conflict"


def test_the_watcher_of_an_earlier_run_never_touches_a_resumed_session(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    manager.start(SESSION)
    old = manager._running[SESSION]
    manager.stop(SESSION, "suspended")
    manager.create(request(resume_claude_session_id=RESUME))
    manager.start(SESSION)
    new = manager._running[SESSION]
    assert new.status_file != old.status_file
    Path(old.status_file).write_text("1\n")
    wait_for(lambda: not Path(old.status_file).exists())
    time.sleep(0.1)
    assert manager._running[SESSION] is new
    assert states(context)[-1] == "agent_starting"
    assert fake_system.stopped == [f"agent-session-{SESSION}"] * 2


def test_a_failed_session_has_its_unit_stopped(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    manager.start(SESSION)
    finish(manager, 1)
    wait_for(lambda: fake_system.stopped == [f"agent-session-{SESSION}"])


def test_unencodable_text_and_unusable_model_ids_are_invalid(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    for changes in (
        {"harness": {"context_markdown": "a\ud800b"}},
        {"initial_prompt": {"prompt_id": PROMPT_ID, "text": "\udfff"}},
        {"model": {"access": "platform", "model_id": "-p", "env": {}}},
    ):
        with pytest.raises(sessions.SessionError) as raised:
            manager.create(request(**changes))
        assert raised.value.code == "request_invalid"
    assert context.state.session(SESSION) is None


def test_cleanup_retries_a_user_that_could_not_be_removed(
    manager: sessions.SessionManager, fake_system: FakeSystem, session_paths: sessions.Paths, clock: FakeClock
) -> None:
    manager.create(request())
    manager.stop(SESSION)
    clock.advance(sessions.RETENTION_SECONDS)
    fake_system.fail_remove = True
    assert manager.cleanup() == []
    assert Path(session_paths.meta_dir, f"{SESSION}.json").exists()
    fake_system.fail_remove = False
    assert manager.cleanup() == [SESSION]


def test_linux_system_timeouts_and_removal_result() -> None:
    def slow(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 60)

    assert sessions.LinuxSystem(run=slow).remove_user(USER) is False
    assert sessions.LinuxSystem(run=Runs()).remove_user(USER) is True
    assert sessions.LinuxSystem(run=Runs({"userdel": 6})).remove_user(USER) is False


def test_another_request_with_the_same_identity_conflicts(manager: sessions.SessionManager) -> None:
    manager.create(request())
    with pytest.raises(sessions.SessionError) as raised:
        manager.create(request(limits={"memory_max": 1, "cpu_quota": 1, "tasks_max": 1, "runtime_max_sec": 1}))
    assert raised.value.code == "id_conflict"


# -- restart (HOT-09: the agent did not list the platform tools in time) --------------------------------------------


def set_state(context: ControlContext, state: str) -> None:
    current = context.state.session(SESSION)
    assert current is not None
    context.state.save_session(dataclasses.replace(current, state=state))


def test_restart_runs_the_agent_again_with_a_new_token_and_the_prompt_still_waiting(
    manager: sessions.SessionManager,
    context: ControlContext,
    tmux_runner: TmuxRunner,
    session_paths: sessions.Paths,
    fake_system: FakeSystem,
) -> None:
    events: list[str] = []
    module = types.ModuleType("agent_guest.control_routes.fake_restart_events")
    module.SESSION_EVENTS = {  # type: ignore[attr-defined]
        "agent_starting": lambda ctx, sid: events.append("agent_starting"),
        "agent_ended": lambda ctx, sid: events.append("agent_ended"),
    }
    manager.create(request())
    original = registry.modules
    try:
        registry.modules = lambda package: [*original(package), module]
        assert manager.start(SESSION)
        first_run = manager._running[SESSION]
        first = context.state.session(SESSION)
        assert first is not None
        toolsready.mark(context, SESSION, 17, first.token_sha256)
        assert manager.restart(SESSION)
    finally:
        registry.modules = original
    assert events == ["agent_starting", "agent_ended", "agent_starting"]
    second = context.state.session(SESSION)
    assert first is not None
    assert second is not None
    assert second.token_sha256 != first.token_sha256
    assert not toolsready.ready(context, SESSION)  # the new run must list the tools itself
    assert unit_env(session_paths)["PLATFORM_SESSION_TOKEN"] is None  # the token travels in the launch env only
    assert second.state == "agent_starting"
    assert states(context) == ["sandbox_ready", "agent_starting"]  # nothing journaled for the restart
    assert first_run.stopped
    assert manager._running[SESSION] is not first_run
    restart_start = manager._running[SESSION].start
    assert restart_start is not None
    assert restart_start.token == ""
    assert tmux_runner.subcommands().count("new-session") == 2
    assert "kill-session" in tmux_runner.subcommands()
    assert manager._deferred[SESSION] == (PROMPT_ID, "Summarise the repository.")
    manager._ended(SESSION, first_run, -1, 1.0)  # the end of the old run fails nothing
    assert states(context) == ["sandbox_ready", "agent_starting"]


def test_restart_only_for_a_starting_session_with_a_known_run(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    assert not manager.restart(SESSION)  # unknown
    manager.create(request())
    assert not manager.restart(SESSION)  # not started
    manager.start(SESSION)
    run = manager._running[SESSION]
    saved, run.start = run.start, None  # a run re-attached after a restart of the control process
    assert not manager.restart(SESSION)
    run.start = saved
    manager._stopping.add(SESSION)
    assert not manager.restart(SESSION)
    manager._stopping.discard(SESSION)
    set_state(context, "prompt_delivering")
    assert not manager.restart(SESSION)


def test_restart_stops_when_the_session_ended_meanwhile(
    manager: sessions.SessionManager,
    context: ControlContext,
    fake_system: FakeSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager.create(request())
    manager.start(SESSION)

    def ended_meanwhile(name: str) -> None:
        set_state(context, "failed")

    monkeypatch.setattr(fake_system, "stop_unit", ended_meanwhile)
    assert not manager.restart(SESSION)
    assert SESSION not in manager._running


def test_a_restart_that_cannot_launch_fails_the_session(
    manager: sessions.SessionManager, context: ControlContext, tmux_runner: TmuxRunner
) -> None:
    manager.create(request())
    manager.start(SESSION)
    tmux_runner.codes["new-session"] = 1
    assert not manager.restart(SESSION)
    assert failure(context) == "agent_start_failed"


def test_restart_session_uses_the_process_manager(manager: sessions.SessionManager, context: ControlContext) -> None:
    sessions.install(context, manager)
    manager.create(request())
    manager.start(SESSION)
    assert sessions.restart_session(context, SESSION)


def test_cleanup_forgets_the_tools_mark(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    manager.create(request())
    manager.start(SESSION)
    started = context.state.session(SESSION)
    assert started is not None
    toolsready.mark(context, SESSION, 17, started.token_sha256)
    manager.stop(SESSION)
    meta_file = Path(session_paths.meta_dir, f"{SESSION}.json")
    meta_file.write_text(json.dumps({**json.loads(meta_file.read_text()), "stopped_at": 0}))
    assert manager.cleanup() == [SESSION]
    with context.journal.transaction() as db:
        assert db.execute("SELECT 1 FROM guest_meta WHERE key LIKE 'tools_ready:%'").fetchone() is None


def test_the_depth_comes_from_the_stored_identity(manager: sessions.SessionManager, context: ControlContext) -> None:
    assert manager.depth(SESSION) is None
    manager._write_meta(SESSION, {"identity": {"depth": 0}})
    assert control_hooks.session_depth(context, SESSION) == 0
    manager._write_meta(SESSION, {"identity": {"depth": 2}})
    assert manager.depth(SESSION) == 2
    broken: dict[str, Any]
    for broken in ({"identity": []}, {"identity": {"depth": "0"}}, {"identity": {"depth": True}}):
        manager._write_meta(SESSION, broken)
        assert manager.depth(SESSION) is None
