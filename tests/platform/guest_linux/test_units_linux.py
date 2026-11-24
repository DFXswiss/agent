"""U-GST-sessions on Linux (marker ``linux``): the guest's control process as a real systemd unit (KOR-09).

``guest_unit.py`` runs ``agent-guest-control`` from the checkout in a transient unit with ``Restart=always`` and the
session machinery of ``make_rig`` (test paths, the fake agent); the test talks to it only over the control socket,
as the runner does. ``systemctl restart`` of that unit is the real restart of the control process (AUD-02 NB-14: the
A-05 test of ``test_sessions_linux.py`` replays it inside the test process). ``agent-guest-api`` runs as a unit of
its own as well (A-04). Afterwards ``clean_up`` opens the same files in-process, without start-up hooks.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.model import ModelDouble
from agent_doubles.model.scenario import parse_scenario
from agent_guest import sessions, tmux
from agent_guest.clock import uuid7
from agent_guest.config import GuestConfig, load_config
from agent_guest.control import ControlService
from agent_guest.driver import ClaudeCodeDriver
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import request
from tests.platform.guest_linux.conftest import LAUNCHER, Guest, ldd_paths
from tests.platform.guest_linux.test_sessions_linux import (
    CLAUDE_VERSION,
    CONTROL_TOKEN,
    LIMITS,
    MCP_CONFIG,
    REPO,
    RULES,
    TMUX_SOCKET,
    _release_platform_port,
    _restore_platform_listener,
    claude_binary,
    new_id,
    sleeper,
    systemctl,
    unit_property,
    wait_for,
)

pytestmark = pytest.mark.linux

__all__ = ["claude_binary", "sleeper"]  # fixtures of the session suite, used here

GUEST_UNIT = Path(__file__).with_name("guest_unit.py")
UNIT_PREFIX = "agent-guest-control-test-"
# The settings of images/files/etc/systemd/system/agent-guest-control.service (the working directory is /: the test
# VM has no /opt/agent-guest; the configuration is passed as AGENT_GUEST_* instead of the production defaults).
API_PREFIX = "agent-guest-api-test-"
# images/files/etc/systemd/system/agent-guest-api.service, but as root (the test VM has no user `platform`) and
# without ProtectHome/PrivateTmp (the checkout and the test's files are below /var/lib and /tmp).
API_PROPERTIES = [
    "--property=Restart=always",
    "--property=RestartSec=1s",
    "--property=StartLimitIntervalSec=0",
    "--property=NoNewPrivileges=yes",
    "--property=UMask=0077",
    "--property=InaccessiblePaths=-/run/systemd/transient",
]
UNIT_PROPERTIES = [
    "--property=Restart=always",
    "--property=RestartSec=1s",
    "--property=StartLimitIntervalSec=0",
    "--property=NoNewPrivileges=yes",
    "--property=UMask=0077",
]
CREDENTIAL = {
    "upstream_base_url": "http://127.0.0.1:9",
    "auth_header": "authorization",
    "auth_value": "Bearer test-router-key-not-used",
    "extra_headers": [],
    "access": "platform",
    "models_allowed": [{"model_id": "deepseek/deepseek-v4.1-flash-1017", "context_window": 1000000}],
}


@dataclass
class ControlUnit:
    name: str
    client: ControlClient
    api_name: str | None = None

    def main_pid(self, name: str | None = None) -> int:
        return int(systemctl("show", "-p", "MainPID", "--value", name or self.name) or "0")

    def api_listening(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex(("127.0.0.1", 7700)) == 0

    def restart_api(self) -> None:
        assert self.api_name is not None
        before = self.main_pid(self.api_name)
        done = subprocess.run(["systemctl", "restart", self.api_name], capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        wait_for(lambda: self.main_pid(self.api_name) not in (0, before) and self.api_listening(), 30)

    def answering(self) -> bool:
        try:
            return self.client.request("GET", "/v1/journal?after=0&limit=1").status == 200
        except OSError:
            return False

    def records(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        after = 0
        while True:
            page = self.client.request("GET", f"/v1/journal?after={after}&limit=1000").json()
            found.extend(page["records"])
            if not page["records"]:
                return found
            after = page["records"][-1]["seq"]

    def states(self, session_id: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.records() if r["kind"] == "session.state" and r["session_id"] == session_id]

    def terminal(self, session_id: str) -> str:
        return "".join(
            frame[2]
            for record in self.records()
            if record["kind"] == "session.terminal" and record["session_id"] == session_id
            for frame in record["body"]["frames"]
        )


StartUnits = Callable[[str, tuple[str, ...], bool], ControlUnit]


@pytest.fixture
def start_units(tmp_path: Path, guest: Guest) -> Iterator[StartUnits]:
    """``start(agent, exec_paths, api)``: the control unit for that agent binary, with ``api`` also
    ``agent-guest-api`` (the production module ``agent_guest.server`` on 127.0.0.1:7700) as a unit of its own."""
    started: list[tuple[ControlUnit, dict[str, str], dict[str, Any]]] = []

    def start(agent: str, exec_paths: tuple[str, ...], api: bool) -> ControlUnit:
        token = tmp_path / "control-token"
        token.write_text(CONTROL_TOKEN + "\n")
        token.chmod(0o600)
        rules = tmp_path / "RULES.md"
        rules.write_text(RULES)
        MCP_CONFIG.parent.mkdir(mode=0o755, exist_ok=True)
        MCP_CONFIG.write_text(
            json.dumps({"mcpServers": {"platform": {"type": "http", "url": "http://127.0.0.1:7700/mcp"}}})
        )
        MCP_CONFIG.chmod(0o644)
        spec = {
            "source": str(REPO / "src"),
            "tmux_socket": TMUX_SOCKET,
            "claude_version": CLAUDE_VERSION,
            "claude": agent,
            "mcp_config": str(MCP_CONFIG),
            "paths": {
                "rules_file": str(rules),
                "meta_dir": str(tmp_path / "meta"),
                "claude": agent,
                "launcher": str(LAUNCHER),
                "mcp_config": str(MCP_CONFIG),
                "python": guest.python,
            },
            "interpreter": [guest.python, guest.lib_dynload],
            "exec_paths": list(exec_paths),
        }
        spec_file = tmp_path / "unit-spec.json"
        spec_file.write_text(json.dumps(spec))
        # This rig installs no managed settings, so no hook ever confirms a turn: the units run with the test switch
        # `open` of a TEST_MODE install record, which keeps the gateway's turn gate out of the way (ADR-064 section 3).
        install = tmp_path / "golden-install.json"
        install.write_text(json.dumps({"test_mode": True}))
        environment = {
            "AGENT_GUEST_CONTROL_SOCKET": str(tmp_path / "control.sock"),
            "AGENT_GUEST_API_SOCKET": str(tmp_path / "api.sock"),
            "AGENT_GUEST_CONTROL_GROUP": "root",
            "AGENT_GUEST_API_GROUP": "root",
            "AGENT_GUEST_CONTROL_TOKEN_FILE": str(token),
            "AGENT_GUEST_JOURNAL": str(tmp_path / "state" / "journal.db"),
            "AGENT_GUEST_BUILD_FILE": str(tmp_path / "missing-build.json"),
            "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(tmp_path / "missing-install.json"),
            "AGENT_GUEST_CONTRACTS": str(REPO / "contracts"),
            "AGENT_GUEST_FAULT_HOOKS": "open",
            "AGENT_GUEST_GOLDEN_INSTALL_FILE": str(install),
        }
        # Units of an earlier run killed hard would still serve the shared test tmux socket or port 7700.
        subprocess.run(["systemctl", "stop", f"{UNIT_PREFIX}*", f"{API_PREFIX}*"], capture_output=True, check=False)
        name = f"{UNIT_PREFIX}{uuid.uuid4().hex[:8]}"
        run_unit(name, environment, [guest.python, "-I", str(GUEST_UNIT), str(spec_file)], UNIT_PROPERTIES)
        found = ControlUnit(name, ControlClient(str(tmp_path / "control.sock"), CONTROL_TOKEN))
        started.append((found, environment, spec))
        wait_for(found.answering, 30, lambda: systemctl("status", name))
        if api:
            _release_platform_port()
            found.api_name = f"{API_PREFIX}{uuid.uuid4().hex[:8]}"
            api_environment = {
                "AGENT_GUEST_API_SOCKET": environment["AGENT_GUEST_API_SOCKET"],
                "AGENT_GUEST_CONTRACTS": environment["AGENT_GUEST_CONTRACTS"],
                "AGENT_GUEST_BUILD_FILE": environment["AGENT_GUEST_BUILD_FILE"],
                "AGENT_GUEST_CLAUDE_INSTALL_FILE": environment["AGENT_GUEST_CLAUDE_INSTALL_FILE"],
                "AGENT_GUEST_PEER_TABLE": "/proc/net/tcp",
                "AGENT_GUEST_FAULT_HOOKS": environment["AGENT_GUEST_FAULT_HOOKS"],
                "AGENT_GUEST_GOLDEN_INSTALL_FILE": environment["AGENT_GUEST_GOLDEN_INSTALL_FILE"],
                "PYTHONPATH": str(REPO / "src"),
            }
            run_unit(found.api_name, api_environment, [guest.python, "-s", "-m", "agent_guest.server"], API_PROPERTIES)
            wait_for(found.api_listening, 30, lambda: systemctl("status", str(found.api_name)))
        return found

    try:
        yield start
    finally:
        for found, environment, spec in started:
            for name in (found.name, found.api_name):
                if name is not None:
                    subprocess.run(["systemctl", "stop", name], capture_output=True, check=False)
                    subprocess.run(["systemctl", "reset-failed", name], capture_output=True, check=False)
            if found.api_name is not None:
                _restore_platform_listener()
            clean_up(load_config(environment), spec)


def run_unit(name: str, environment: dict[str, str], command: list[str], properties: list[str]) -> None:
    setenv = [f"--setenv={key}={value}" for key, value in environment.items()]
    started = subprocess.run(
        ["systemd-run", f"--unit={name}", "--quiet", "--working-directory=/", *properties, *setenv, *command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr


@pytest.fixture
def control_unit(start_units: StartUnits, guest: Guest, sleeper: str) -> ControlUnit:
    return start_units(sleeper, guest.exec_paths, False)


def clean_up(config: GuestConfig, spec: dict[str, Any]) -> None:
    """After the unit stopped: the same files in-process, without the start-up hooks (no production defaults), its
    sessions stopped and removed like ``make_rig``'s teardown does."""
    service = ControlService(config, startup=[])
    context = service.start()
    manager = sessions.SessionManager(
        context=context,
        system=sessions.LinuxSystem(),
        tmux=tmux.Tmux(socket=spec["tmux_socket"]),
        driver=ClaudeCodeDriver(spec["claude_version"], binary=spec["claude"], mcp_config=spec["mcp_config"]),
        paths=sessions.Paths(**spec["paths"]),
        interpreter=(spec["interpreter"][0], spec["interpreter"][1]),
        exec_paths=tuple(spec["exec_paths"]),
    )
    sessions.install(context, manager)
    try:
        for session in context.state.sessions():
            if session.state not in sessions.FINAL_STATES:
                manager.stop(session.id)
            manager._write_meta(session.id, {"stopped_at": 0})
        manager.cleanup()
    finally:
        manager.close()
        service.stop()


def type_into(session_id: str, text: str) -> None:
    pane = f"={tmux.session_name(session_id)}:"
    argv = tmux.Tmux(socket=TMUX_SOCKET).argv("send-keys", "-t", pane, "-l", "--", text)
    subprocess.run(argv, check=True, env=tmux.CLIENT_ENV)


def test_a_real_restart_of_the_control_unit_keeps_the_running_session(control_unit: ControlUnit) -> None:
    """NB-14 (AUD-02): ``systemctl restart`` of ``agent-guest-control`` while a session runs - the session unit and its
    terminal stay, the restarted process records the output again (marker ``recorder restarted``) and sees the
    agent's end (``agent_exited``)."""
    sid = new_id()
    created = control_unit.client.request("POST", "/v1/sessions", request(session_id=sid, limits=LIMITS))
    assert (created.status, created.json()) == (200, {"state": "sandbox_ready"}), created.body
    put = control_unit.client.request("PUT", f"/v1/sessions/{sid}/credential", CREDENTIAL)
    assert put.status == 200, put.body
    wait_for(lambda: unit_property(sid, "ActiveState") == "active", 30, lambda: str(control_unit.states(sid)))
    started = time.monotonic()
    agent_pid = int(unit_property(sid, "MainPID"))
    type_into(sid, "before-restart")
    wait_for(lambda: "before-restart" in control_unit.terminal(sid), 15)

    before = control_unit.main_pid()
    restarted = subprocess.run(["systemctl", "restart", control_unit.name], capture_output=True, text=True, check=False)
    assert restarted.returncode == 0, restarted.stderr
    wait_for(lambda: control_unit.main_pid() not in (0, before) and control_unit.answering(), 30)
    # the start-up hooks run after the sockets serve: the recorder is attached again when its marker is recorded
    wait_for(lambda: "recorder restarted" in control_unit.terminal(sid), 20, lambda: control_unit.terminal(sid)[-500:])

    assert unit_property(sid, "ActiveState") == "active"
    assert int(unit_property(sid, "MainPID")) == agent_pid  # the agent was never touched
    type_into(sid, "after-restart")
    wait_for(lambda: "after-restart" in control_unit.terminal(sid), 15, lambda: control_unit.terminal(sid)[-500:])
    assert "recorder restarted" in control_unit.terminal(sid)
    assert control_unit.states(sid)[-1]["state"] in ("agent_starting", "prompt_delivering", "working")
    # beyond the start window counted from the start before the restart: `agent_exited`
    time.sleep(max(0.0, sessions.START_WINDOW_SECONDS + 0.5 - (time.monotonic() - started)))
    os.kill(agent_pid, 9)
    wait_for(lambda: control_unit.states(sid)[-1]["state"] == "failed", 30, lambda: str(control_unit.states(sid)))
    assert control_unit.states(sid)[-1]["reason"] == "agent_exited"


# -------------------------------------------------------------------------------------------- agent-guest-api (A-04)

GATEWAY_SCENARIO = {
    "name": "kor-09-api-restart",
    "description": "Two turns of real Claude Code through the guest gateway; agent-guest-api restarts between them.",
    "steps": [
        {
            "id": "first",
            "expect": {"user_text_contains": "FIRST-TURN", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "First answer: OTTER-1190."}]},
        },
        {
            "id": "second",
            "expect": {"user_text_contains": "SECOND-TURN", "has_tools": True, "stream": True},
            "reply": {"content": [{"type": "text", "text": "Second answer: HERON-2271."}]},
        },
    ],
    "side": [{"id": "title", "expect": {"has_tools": False}, "reply": {"content": [{"type": "text", "text": "T"}]}}],
}


COOLDOWN_S = 10.0  # agent_runner.model.gateways.COOLDOWN_S
LATENCY_S = 0.5


@dataclass
class StandInRunner:
    """The runner's handling of ``credential_missing`` (``model.gateways.credential_missing``): a report within
    ``COOLDOWN_S`` of its last provisioning is ignored, any other one is provisioned at once (``LATENCY_S``)."""

    unit: ControlUnit
    session_id: str
    credential: dict[str, Any]
    last: float = float("-inf")
    reports: list[tuple[float, bool]] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def provision(self) -> None:
        put = self.unit.client.request("PUT", f"/v1/sessions/{self.session_id}/credential", self.credential)
        assert put.status == 200, put.body
        self.last = time.monotonic()

    def run(self) -> None:
        after = max((r["seq"] for r in self.unit.records()), default=0)
        while not self.stop.wait(0.1):
            for record in self.unit.client.request("GET", f"/v1/journal?after={after}&limit=1000").json()["records"]:
                after = record["seq"]
                missing = record["kind"] == "guest.error" and record["body"].get("code") == "credential_missing"
                if not missing or record["session_id"] != self.session_id:
                    continue
                now = time.monotonic()
                taken = now - self.last >= COOLDOWN_S
                self.reports.append((now, taken))
                if taken and not self.stop.wait(LATENCY_S):
                    self.provision()


def pane(session_id: str) -> str:
    try:
        return tmux.Tmux(socket=TMUX_SOCKET).capture(tmux.session_name(session_id))
    except tmux.TmuxError:
        return ""


@pytest.mark.parametrize("recent", [False, True], ids=["runner-ready", "runner-in-cooldown"])
def test_a_real_restart_of_the_api_unit_between_two_turns(
    start_units: StartUnits, guest: Guest, claude_binary: str, recent: bool
) -> None:
    """A-04 rest (AUD-01, AUD-02), real Claude Code through the real gateway unit: `agent-guest-api` keeps the
    session's credential in memory only, so after a restart the session's first model request finds none. The runner
    provisions it again after `credential_missing`, but ignores a report within its 10 s cooldown after a provisioning
    (here: one the API process took just before the restart). The turn after the restart must be answered either way.
    Before the correction (immediate 503): answered after 1.1 s when the runner took the report (Claude Code retried),
    an API error after 4.7 s in the cooldown case (all four reports ignored)."""
    double = ModelDouble(parse_scenario(GATEWAY_SCENARIO)).start()
    try:
        ctypes_so = next(Path(guest.lib_dynload).glob("_ctypes.cpython-*.so"))
        units = start_units(claude_binary, tuple(ldd_paths(claude_binary, guest.python, str(ctypes_so))), True)
        sid = new_id()
        body = request(
            session_id=sid,
            model={"access": "platform", "model_id": "double-model", "env": {"ANTHROPIC_MODEL": "double-model"}},
        )
        del body["initial_prompt"]
        created = units.client.request("POST", "/v1/sessions", body)
        assert created.status == 200, created.body
        credential = {**CREDENTIAL, "upstream_base_url": double.url,
                      "models_allowed": [{"model_id": "double-model", "context_window": 200000}]}  # fmt: skip
        assert units.client.request("PUT", f"/v1/sessions/{sid}/credential", credential).status == 200

        def describe() -> str:
            return f"pane:\n{pane(sid)}\nstates: {units.states(sid)}\ndouble: {json.dumps(double.state())[:3000]}"

        wait_for(lambda: any(b["state"] == "prompt_delivering" for b in units.states(sid)), 60, describe)
        first = {"prompt_id": uuid7(time.time()), "text": "FIRST-TURN: answer briefly."}
        assert units.client.request("POST", f"/v1/sessions/{sid}/prompts", first).status == 200
        wait_for(lambda: "OTTER-1190" in pane(sid), 90, describe)
        # This rig installs no managed settings, so no hook runs: the delivery of the first prompt ends with its
        # acceptance wait (refused); the second prompt is delivered after it. The gateway is what is under test here.
        wait_for(lambda: any(b.get("prompt_id") == first["prompt_id"] for b in units.states(sid)), 30, describe)

        runner = StandInRunner(units, sid, credential)
        if recent:
            # The runner provisioned the session (and the API process took it) just before the restart: its last
            # provisioning is recent, nothing waits in the hand-off slot.
            runner.last = time.monotonic()
        watcher = threading.Thread(target=runner.run, daemon=True)
        watcher.start()
        units.restart_api()
        started = time.monotonic()
        prompt = {"prompt_id": uuid7(time.time()), "text": "SECOND-TURN: answer briefly."}
        assert units.client.request("POST", f"/v1/sessions/{sid}/prompts", prompt).status == 200
        wait_for(lambda: "HERON-2271" in pane(sid) or "API Error" in pane(sid), 120, describe)
        runner.stop.set()
        watcher.join(5)
        print(f"recent={recent}: second turn after {time.monotonic() - started:.1f} s, reports {runner.reports}")
        assert runner.reports, "the restarted gateway reported no credential_missing"
        assert runner.reports[0][1] is not recent  # in the cooldown the first report is ignored
        assert "API Error" not in pane(sid), describe()
        assert "HERON-2271" in pane(sid)
        assert double.state()["ok"], describe()
    finally:
        double.stop()
