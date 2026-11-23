"""U-GST-sessions/control: a restarted control process attaches again to its sessions, and an OOM kill is reported as
`oom` (05-runner.md sections 5.1, 6.1, 6.3, 7; 03-api.md section 11; AUD-01 A-05 and B-16, decisions/KOR-02.md).

A restart is simulated by dropping the session manager (its watcher threads end, units and tmux sessions stay) and
building a new one on the same journal; the Linux suite runs the same against real units and tmux.
"""

from __future__ import annotations

import dataclasses
import json
import stat
import subprocess
import time
import types
from pathlib import Path
from typing import Any

import pytest

from agent_guest import control, delivery, registry, sessions, tmux
from agent_guest.control import ControlContext, ControlService
from agent_guest.control_routes import capture as capture_routes
from agent_guest.control_routes import prompts as prompt_routes
from agent_guest.control_routes import sessions as session_routes
from agent_guest.driver import MAX_ARGV_PROMPT_BYTES
from tests.platform.guest.sessions.conftest import (
    PROMPT_ID,
    SESSION,
    FakeSystem,
    TmuxRunner,
    request,
)

UNIT = f"agent-session-{SESSION}"


def records(context: ControlContext) -> list[dict[str, Any]]:
    found, _ = context.journal.read(after=0, limit=1000)
    return [record["body"] for record in found if record["kind"] == "session.state"]


def states(context: ControlContext) -> list[str]:
    return [body["state"] for body in records(context)]


def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def restarted(manager: sessions.SessionManager, **changes: Any) -> sessions.SessionManager:
    """The manager of a restarted control process: same journal, files and tmux, nothing in memory."""
    manager.close()
    again = sessions.SessionManager(
        context=manager.context,
        system=changes.get("system", manager.system),
        tmux=manager.tmux,
        driver=manager.driver,
        paths=manager.paths,
        interpreter=manager.interpreter,
        exec_paths=manager.exec_paths,
        monotonic=changes.get("monotonic", manager.monotonic),
        watch_interval=0.01,
        tmux_check=0.05,
    )
    sessions.install(manager.context, again)
    return again


def meta(paths: sessions.Paths) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(Path(paths.meta_dir, f"{SESSION}.json").read_text())
    return value


# -- OOM (B-16) -----------------------------------------------------------------------------------------------------


def test_an_agent_killed_by_the_oom_killer_fails_with_oom(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    clock = [100.0]
    manager.monotonic = lambda: clock[0]
    manager.create(request())
    manager.start(SESSION)
    fake_system.oom.add(UNIT)
    clock[0] += 60
    Path(manager._running[SESSION].status_file).write_text("137\n")
    wait_for(lambda: states(context)[-1] == "failed")
    assert records(context)[-1]["reason"] == "oom"
    assert fake_system.oom_checked == [(UNIT, context.journal.clock.now())]  # systemd's entries since the run began
    wait_for(lambda: UNIT in fake_system.stopped)


def test_a_launcher_failure_stays_sandbox_failed_even_after_an_oom_kill(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    manager.start(SESSION)
    fake_system.oom.add(UNIT)
    Path(manager._running[SESSION].status_file).write_text("126\n")
    wait_for(lambda: states(context)[-1] == "failed")
    assert records(context)[-1]["reason"] == "sandbox_failed"


class Journalctl:
    def __init__(self, stdout: str = "", code: int = 0, error: BaseException | None = None) -> None:
        self.stdout, self.code, self.error = stdout, code, error
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        assert kwargs["env"] == sessions.SYSTEM_ENV
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def test_linux_system_reads_oom_kills_from_the_cgroup_or_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "CGROUP_ROOT", str(tmp_path))
    events = tmp_path / "system.slice" / f"{UNIT}.service" / "memory.events"
    events.parent.mkdir(parents=True)
    events.write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n")
    quiet = Journalctl()
    assert sessions.LinuxSystem(run=quiet).oom_killed(UNIT)
    assert quiet.calls == []
    events.write_text("oom_kill 0\nbroken\noom_kill x\n")
    assert not sessions.LinuxSystem(run=quiet).oom_killed(UNIT)
    assert quiet.calls[-1] == [
        "journalctl",
        "_PID=1",
        f"UNIT={UNIT}.service",
        "--output",
        "cat",
        "--no-pager",
        "--quiet",
    ]
    assert not sessions.LinuxSystem(run=quiet).oom_killed(UNIT, 1700000000.5)
    assert quiet.calls[-1][-1] == "--since=@1700000000"
    events.unlink()  # the unit was collected: its result is in the journal
    result = f"{UNIT}.service: Failed with result 'oom-kill'.\n"
    assert sessions.LinuxSystem(run=Journalctl(result)).oom_killed(UNIT)
    assert not sessions.LinuxSystem(run=Journalctl(result, code=1)).oom_killed(UNIT)
    assert not sessions.LinuxSystem(run=Journalctl(error=OSError("no journalctl"))).oom_killed(UNIT)
    timeout = subprocess.TimeoutExpired(["journalctl"], 10)
    assert not sessions.LinuxSystem(run=Journalctl(error=timeout)).oom_killed(UNIT)
    events.parent.joinpath("memory.events").write_bytes(b"\xff")
    assert not sessions.LinuxSystem(run=Journalctl()).oom_killed(UNIT)


# -- re-attachment (A-05) -------------------------------------------------------------------------------------------


def test_the_run_is_remembered_without_the_prompt(
    manager: sessions.SessionManager, session_paths: sessions.Paths
) -> None:
    text = "y" * (MAX_ARGV_PROMPT_BYTES + 1)
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    run = meta(session_paths)["run"]
    assert run["status_file"] == manager._running[SESSION].status_file
    assert run["deferred_prompt_id"] == PROMPT_ID
    assert isinstance(run["started_at"], float)
    assert "yyyy" not in json.dumps(meta(session_paths))


def test_a_restarted_manager_watches_the_running_agent_again(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    clock = [100.0]
    manager.monotonic = lambda: clock[0]
    text = "y" * (MAX_ARGV_PROMPT_BYTES + 1)
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    status_file = manager._running[SESSION].status_file
    again = restarted(manager)
    context.journal.clock.advance(60)  # type: ignore[attr-defined]  # the control process was down for a minute
    assert SESSION not in again._deferred  # nothing in memory yet
    assert again.reattach() == {SESSION: "watched"}
    assert again._running[SESSION].status_file == status_file
    assert again._deferred[SESSION] == (PROMPT_ID, text)  # restored from its root-only file
    assert again.reattach() == {}  # watched already
    Path(status_file).write_text("0\n")
    wait_for(lambda: states(context)[-1] == "failed")
    assert records(context)[-1]["reason"] == "agent_exited"  # the run started 60 s before its end


def test_without_the_run_the_end_is_seen_when_the_tmux_session_is_gone(
    manager: sessions.SessionManager,
    context: ControlContext,
    session_paths: sessions.Paths,
    tmux_runner: TmuxRunner,
) -> None:
    manager.create(request())
    manager.start(SESSION)
    again = restarted(manager)
    for run in ({}, {"status_file": "/etc/passwd", "started_at": "soon"}, {"status_file": 5, "started_at": 1e12}):
        value = meta(session_paths)
        value["run"] = run
        Path(session_paths.meta_dir, f"{SESSION}.json").write_text(json.dumps(value))
        again.reattach()
        assert again._running[SESSION].status_file == ""
        again._running.pop(SESSION)
    value = meta(session_paths)
    value["run"] = "broken"
    Path(session_paths.meta_dir, f"{SESSION}.json").write_text(json.dumps(value))
    again.reattach()
    tmux_runner.codes["has-session"] = 1
    wait_for(lambda: states(context)[-1] == "failed")
    assert records(context)[-1]["reason"] == "agent_start_failed"  # no start time known: counted from now


def test_a_lost_pending_start_fails_and_an_interrupted_stop_is_finished(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem
) -> None:
    manager.create(request())
    again = restarted(manager)
    assert again.reattach() == {SESSION: "failed"}
    assert records(context)[-1] == {"state": "failed", "reason": "agent_start_failed", "at": records(context)[-1]["at"]}
    assert again.reattach() == {}  # ended sessions are left alone


def test_an_interrupted_stop_is_finished(manager: sessions.SessionManager, context: ControlContext) -> None:
    manager.create(request())
    manager.start(SESSION)
    session = context.state.session(SESSION)
    assert session is not None
    manager._transition(session, "stopping")  # the process died during the stop
    again = restarted(manager)
    assert again.reattach() == {SESSION: "stopped"}
    assert states(context)[-2:] == ["stopping", "stopped"]


def interrupted_suspend(manager: sessions.SessionManager, fake_system: FakeSystem) -> None:
    """What a control process leaves that died in ``systemctl stop`` of a suspend (no ``finally`` of it runs): the
    session ``idle``, its unit stopping, the suspend recorded in the meta file."""
    manager.create(request())
    manager.start(SESSION)
    session = manager.context.state.session(SESSION)
    assert session is not None
    manager.context.state.save_session(dataclasses.replace(session, state="idle"))
    original = manager._transition

    def died(session: Any, state: str) -> None:
        raise SystemExit("agent-guest-control killed")

    def stop_unit(name: str) -> None:
        fake_system.stopped.append(name)
        manager._transition = died  # type: ignore[method-assign]  # nothing after this point is written

    fake_system.stop_unit = stop_unit  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        manager.stop(SESSION, "suspended")
    manager._transition = original  # type: ignore[method-assign]
    fake_system.stop_unit = types.MethodType(FakeSystem.stop_unit, fake_system)  # type: ignore[method-assign]


def test_an_interrupted_suspend_ends_suspended(
    manager: sessions.SessionManager, context: ControlContext, fake_system: FakeSystem, session_paths: sessions.Paths
) -> None:
    """NA-03 (AUD-02): the session must end ``suspended`` - not watched again and failed ``agent_exited`` - so the next
    follow-up prompt can resume it."""
    interrupted_suspend(manager, fake_system)
    assert meta(session_paths)["suspending"] is True
    again = restarted(manager)
    stops = fake_system.stopped.count(UNIT)
    assert again.reattach() == {SESSION: "suspended"}
    assert states(context)[-1] == "suspended"
    assert "failed" not in states(context)
    assert "suspending" not in meta(session_paths)
    assert fake_system.stopped.count(UNIT) > stops  # the stop was repeated
    assert again.reattach() == {}
    # The session can be resumed like any suspended one.
    again.create(request(resume_claude_session_id="5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69"))
    assert states(context)[-1] == "sandbox_ready"


def test_a_finished_suspend_leaves_no_mark(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    manager.create(request())
    manager.start(SESSION)
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(dataclasses.replace(session, state="idle"))
    manager.stop(SESSION, "suspended")
    assert states(context)[-1] == "suspended"
    assert "suspending" not in meta(session_paths)
    # A mark without a running session (a crash after the state was written) is ignored.
    value = meta(session_paths)
    value["suspending"] = True
    Path(session_paths.meta_dir, f"{SESSION}.json").write_text(json.dumps(value))
    again = restarted(manager)
    assert again.reattach() == {}
    # A resume writes the meta file anew: the stale mark cannot suspend the resumed session after a later restart.
    again.create(request(resume_claude_session_id="5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69"))
    again.start(SESSION)
    assert "suspending" not in meta(session_paths)
    assert restarted(again).reattach() == {SESSION: "watched"}


def test_a_suspend_that_cannot_be_recorded_is_logged(
    manager: sessions.SessionManager,
    context: ControlContext,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager.create(request())
    manager.start(SESSION)
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(dataclasses.replace(session, state="idle"))

    write = manager._write_meta

    def refuse(session_id: str, value: dict[str, Any]) -> None:
        if value.get("suspending"):
            raise OSError("disk full")  # the suspend mark; everything else is written
        write(session_id, value)

    monkeypatch.setattr(manager, "_write_meta", refuse)
    manager.stop(SESSION, "suspended")
    assert states(context)[-1] == "suspended"
    assert "suspend of a session not recorded" in capsys.readouterr().err


def test_sessions_the_new_manager_already_handles_are_skipped(
    manager: sessions.SessionManager, context: ControlContext
) -> None:
    manager.create(request())
    assert manager.reattach() == {}  # its pending start is known
    manager.start(SESSION)
    assert manager.reattach() == {}  # its agent is watched


def test_a_run_that_cannot_be_recorded_is_logged(
    manager: sessions.SessionManager, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.create(request())

    def refuse(session_id: str, value: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_meta", refuse)
    assert manager.start(SESSION)
    assert "run of a session not recorded" in capsys.readouterr().err


# -- start-up hooks -------------------------------------------------------------------------------------------------


def test_startup_hooks_are_discovered_and_failures_logged(
    monkeypatch: pytest.MonkeyPatch, context: ControlContext, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    good = types.ModuleType("agent_guest.control_routes.zz_good")
    good.STARTUP = lambda ctx: calls.append("good")  # type: ignore[attr-defined]
    bad = types.ModuleType("agent_guest.control_routes.zz_bad")

    def fail(ctx: ControlContext) -> None:
        raise RuntimeError("boom")

    bad.STARTUP = fail  # type: ignore[attr-defined]
    plain = types.ModuleType("agent_guest.control_routes.zz_plain")
    monkeypatch.setattr(registry, "modules", lambda package: [bad, plain, good])
    hooks = control.startup_hooks()
    assert [name for name, _ in hooks] == ["agent_guest.control_routes.zz_bad", "agent_guest.control_routes.zz_good"]
    control.run_startup(context, hooks)
    assert calls == ["good"]
    assert "startup hook agent_guest.control_routes.zz_bad failed" in capsys.readouterr().err
    broken = types.ModuleType("agent_guest.control_routes.zz_broken")
    broken.STARTUP = "not callable"  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "modules", lambda package: [broken])
    with pytest.raises(registry.RegistryError, match="registry_invalid"):
        control.startup_hooks()


def test_the_control_service_runs_the_hooks_when_it_starts(
    monkeypatch: pytest.MonkeyPatch, control: ControlService
) -> None:
    seen: list[ControlContext] = []
    module = types.ModuleType("agent_guest.control_routes.zz_seen")
    module.STARTUP = seen.append  # type: ignore[attr-defined]
    module.ROUTES = []  # type: ignore[attr-defined]
    original = registry.modules
    monkeypatch.setattr(registry, "modules", lambda package: [*original(package), module])
    service = ControlService(control.config, control.clock)
    control.stop()
    try:
        started = service.start()
        assert seen == [started]
    finally:
        service.stop()


def test_the_registered_hooks_attach_sessions_again(
    manager: sessions.SessionManager, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_routes.startup(context)  # nothing to do without sessions
    capture_routes.startup(context)
    prompt_routes.startup(context)
    manager.create(request())
    manager.start(SESSION)
    again = restarted(manager)
    attached: list[str] = []
    ready: list[str] = []

    class Capture:
        def attach_running(self) -> None:
            attached.append("all")

    class Delivery:
        def agent_starting(self, session_id: str) -> None:
            ready.append(session_id)

    monkeypatch.setattr(capture_routes, "capture", lambda ctx: Capture())
    monkeypatch.setattr(delivery, "manager", lambda ctx: Delivery())
    for module in (capture_routes, prompt_routes, session_routes):
        module.startup(context)
    assert (attached, ready) == (["all"], [SESSION])
    session = context.state.session(SESSION)
    assert session is not None
    context.state.save_session(dataclasses.replace(session, state="idle"))
    prompt_routes.startup(context)  # only `agent_starting` sessions wait for their TUI
    assert ready == [SESSION]
    assert SESSION in again._running
    assert {capture_routes.STARTUP, prompt_routes.STARTUP, session_routes.STARTUP} == {
        capture_routes.startup,
        prompt_routes.startup,
        session_routes.startup,
    }
    assert tmux.session_name(SESSION) == f"s-{SESSION}"


def test_a_deferred_prompt_survives_a_restart_until_it_is_handed_out(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    text = "y" * (MAX_ARGV_PROMPT_BYTES + 1)
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    prompt_file = Path(session_paths.meta_dir, f"{SESSION}.prompt")
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600
    found = delivery.manager(context)
    assert not found._initial_pending(SESSION)  # deferred: registered, but held by the manager
    again = restarted(manager)
    assert found._initial_pending(SESSION)  # nobody holds it ...
    again.reattach()
    assert not found._initial_pending(SESSION)  # ... until the restarted manager restored it
    assert again.deferred_prompt(SESSION) == (PROMPT_ID, text)
    assert not prompt_file.exists()  # handed out: the file is gone
    again = restarted(again)
    again._running.clear()
    again.reattach()
    assert SESSION not in again._deferred
    found.close()


def test_a_damaged_or_foreign_prompt_file_is_not_restored(
    manager: sessions.SessionManager, session_paths: sessions.Paths
) -> None:
    text = "y" * (MAX_ARGV_PROMPT_BYTES + 1)
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    prompt_file = Path(session_paths.meta_dir, f"{SESSION}.prompt")
    for content in ("[]", "{broken", json.dumps({"prompt_id": "other", "text": "x"}), json.dumps({"prompt_id": 1})):
        prompt_file.write_text(content)
        again = restarted(manager)
        again.reattach()
        assert SESSION not in again._deferred
        manager = again


def test_stop_and_cleanup_remove_the_prompt_file(
    manager: sessions.SessionManager, context: ControlContext, session_paths: sessions.Paths
) -> None:
    text = "y" * (MAX_ARGV_PROMPT_BYTES + 1)
    manager.create(request(initial_prompt={"prompt_id": PROMPT_ID, "text": text}))
    manager.start(SESSION)
    prompt_file = Path(session_paths.meta_dir, f"{SESSION}.prompt")
    manager.stop(SESSION)
    assert not prompt_file.exists()
    prompt_file.write_text("{}")
    value = meta(session_paths)
    value["stopped_at"] = 0
    Path(session_paths.meta_dir, f"{SESSION}.json").write_text(json.dumps(value))
    assert manager.cleanup() == [SESSION]
    assert not prompt_file.exists()
