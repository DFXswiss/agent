"""U-GST-sessions on Linux (marker ``linux``): real users, directories, tmux, ``systemd-run`` units and Landlock.

The control process runs in the test process with its real control socket; ``POST /v1/sessions`` and
``POST /v1/sessions/{id}/control`` go over that socket. The session manager uses ``LinuxSystem`` and a real root tmux
server; only the paths of the golden image are replaced by test paths (``/opt/agent-test``, a test tmux socket).

* fake agent: a static "sleeper" ELF that blocks in ``ppoll`` until a signal (it ignores Claude's options, which no
  stock binary does); the ``echo`` stub of the conftest stands for an agent that exits at once
* real agent: Claude Code (pinned, checksum-verified native package) against the model double, reached as
  ``http://127.0.0.1:7700/model`` exactly like the guest model gateway (a small forwarding stand-in strips ``/model``)
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import http.client
import http.server
import json
import os
import platform
import pwd
import socket
import stat
import struct
import subprocess
import tarfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

import agent_guest
from agent_doubles.model import ModelDouble, packaged_scenario
from agent_guest import delivery, sessions, tmux, toolsready
from agent_guest.clock import uuid7
from agent_guest.config import load_config
from agent_guest.control import ControlContext, ControlService, run_startup, startup_hooks
from agent_guest.control_routes import capture
from agent_guest.driver import ClaudeCodeDriver
from agent_guest.sandbox import unit
from agent_guest.state import StateError
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import request
from tests.platform.guest_linux.conftest import LAUNCHER, SANDBOX_DIR, TEST_ROOT, Guest, ldd_paths

# real Claude Code in the VM (download on a cold cache, model turns): longer than the 180 s default limit (FIX-01)
pytestmark = [pytest.mark.linux, pytest.mark.timeout(900)]

REPO = Path(agent_guest.__file__).resolve().parents[2]
SLEEPER = TEST_ROOT / "sleeper"
MCP_CONFIG = Path("/etc/agent-test/mcp.json")
TMUX_SOCKET = "/run/agent-guest/tmux-test.sock"
CONTROL_TOKEN = "Z3N0MDItbGludXgtY29udHJvbC10b2tlbi0xMjM0NTY3OA"
CACHE = Path("/var/lib/guest-ci/cache")
CLAUDE_DIR = TEST_ROOT / "claude-code"
CLAUDE_VERSION = "2.1.330"
CLAUDE_PACKAGES = {
    "aarch64": (
        "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-2.1.330.tgz",
        "5460d42c696670f4fd2a7ec619719edda14a576b28ee3c289b348ade96efbf6d",
    ),
    "x86_64": (
        "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.330.tgz",
        "40ff30c5768723958032132065e6fcdd1e2dc3a2b3ff9a0bf36c6620ef1c40ef",
    ),
}
LIMITS = {"memory_max": 1073741824, "cpu_quota": 50, "tasks_max": 64, "runtime_max_sec": 1800}
RULES = "# agent.dfx.swiss session rules\n\nYou must not run programs.\n"
FIXED_ENV = {key for key, _ in unit.FIXED_MODEL_ENV} | {key for key, _ in unit.SESSION_ENV}
SYSTEMD_ENV = {
    "USER",
    "LOGNAME",
    "INVOCATION_ID",
    "SYSTEMD_EXEC_PID",
    "MEMORY_PRESSURE_WATCH",
    "MEMORY_PRESSURE_WRITE",
}
SESSION_ENV = FIXED_ENV | {"HOME", "CLAUDE_CONFIG_DIR", "PLATFORM_SESSION_TOKEN", "ANTHROPIC_AUTH_TOKEN"}


# -- the fake agent ---------------------------------------------------------------------------------------------


def sleeper_elf(machine: str) -> bytes:
    """A static ELF64 whose code loops over ``ppoll(NULL, 0, NULL, NULL)``: alive until a signal ends it."""
    if machine == "aarch64":
        # mov x0..x3, #0; mov x8, #73 (ppoll); svc #0; b start
        code = struct.pack("<7I", 0xD2800000, 0xD2800001, 0xD2800002, 0xD2800003, 0xD2800928, 0xD4000001, 0x17FFFFFA)
        e_machine = 0xB7
    else:
        # xor edi,edi; xor esi,esi; xor edx,edx; xor r10d,r10d; mov eax,271 (ppoll); syscall; jmp start
        code = bytes.fromhex("31ff31f631d24531d2b80f0100000f05ebee")
        e_machine = 0x3E
    base, header_size = 0x400000, 64 + 56
    size = header_size + len(code)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    header = ident + struct.pack("<HHIQQQIHHHHHH", 2, e_machine, 1, base + header_size, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    program = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, size, size, 0x1000)
    return header + program + code


@pytest.fixture(scope="module")
def sleeper() -> str:
    SLEEPER.write_bytes(sleeper_elf(platform.machine()))
    SLEEPER.chmod(0o755)
    probe = subprocess.Popen([str(SLEEPER), "--restricted", "--tools", "Read"])
    try:
        time.sleep(0.3)
        assert probe.poll() is None, "the sleeper must stay alive"
    finally:
        probe.terminate()
        probe.wait(5)
    return str(SLEEPER)


# -- control process and manager ----------------------------------------------------------------------------


@dataclass
class Rig:
    context: ControlContext
    client: ControlClient
    manager: sessions.SessionManager
    tmux: tmux.Tmux

    def pane(self, session_id: str) -> str:
        try:
            return self.tmux.capture(tmux.session_name(session_id))
        except tmux.TmuxError:
            return ""


def systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=False).stdout.strip()


def unit_property(session_id: str, name: str) -> str:
    return systemctl("show", "-p", name, "--value", unit.UNIT_PREFIX + session_id)


def wait_for(predicate: Callable[[], object], timeout: float, describe: Callable[[], str] = lambda: "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not reached within {timeout} s\n{describe()}")
        time.sleep(0.1)


def states(context: ControlContext, session_id: str) -> list[str]:
    records, _ = context.journal.read(after=0, limit=1000)
    return [r["body"]["state"] for r in records if r["kind"] == "session.state" and r["session_id"] == session_id]


def new_id() -> str:
    """A UUIDv7-shaped session ID whose user name prefix is unique (random first 12 hex digits)."""
    raw = bytearray(os.urandom(16))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


@pytest.fixture
def make_rig(tmp_path: Path, guest: Guest) -> Iterator[Callable[[str, tuple[str, ...]], Rig]]:
    """Build the control process and a session manager for an agent binary; tears everything down afterwards."""
    created: list[tuple[ControlService, sessions.SessionManager]] = []
    client_tmux = tmux.Tmux(socket=TMUX_SOCKET)

    def build(claude: str, exec_paths: tuple[str, ...]) -> Rig:
        token = tmp_path / "control-token"
        token.write_text(CONTROL_TOKEN + "\n")
        token.chmod(0o600)
        rules = tmp_path / "RULES.md"
        rules.write_text(RULES)
        config = load_config(
            {
                "AGENT_GUEST_CONTROL_SOCKET": str(tmp_path / "control.sock"),
                "AGENT_GUEST_API_SOCKET": str(tmp_path / "api.sock"),
                "AGENT_GUEST_CONTROL_GROUP": "root",
                "AGENT_GUEST_API_GROUP": "root",
                "AGENT_GUEST_CONTROL_TOKEN_FILE": str(token),
                "AGENT_GUEST_JOURNAL": str(tmp_path / "state" / "journal.db"),
                "AGENT_GUEST_BUILD_FILE": str(tmp_path / "missing-build.json"),
                "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(tmp_path / "missing-install.json"),
                "AGENT_GUEST_CONTRACTS": str(REPO / "contracts"),
            }
        )
        service = ControlService(config)
        context = service.start()
        manager = sessions.SessionManager(
            context=context,
            system=sessions.LinuxSystem(),
            tmux=client_tmux,
            driver=ClaudeCodeDriver(CLAUDE_VERSION, binary=claude, mcp_config=str(MCP_CONFIG)),
            paths=sessions.Paths(
                rules_file=str(rules),
                meta_dir=str(tmp_path / "meta"),
                claude=claude,
                launcher=str(LAUNCHER),
                mcp_config=str(MCP_CONFIG),
                python=guest.python,
            ),
            interpreter=(guest.python, guest.lib_dynload),
            exec_paths=exec_paths,
        )
        sessions.install(context, manager)
        created.append((service, manager))
        _Forward.contexts.append(context)
        return Rig(context, ControlClient(config.control_socket, CONTROL_TOKEN), manager, client_tmux)

    MCP_CONFIG.parent.mkdir(mode=0o755, exist_ok=True)
    mcp = {"mcpServers": {"platform": {"type": "http", "url": "http://127.0.0.1:7700/mcp"}}}
    MCP_CONFIG.write_text(json.dumps(mcp))
    MCP_CONFIG.chmod(0o644)
    try:
        yield build
    finally:
        _Forward.contexts.clear()
        for service, manager in created:
            for session in manager.context.state.sessions():
                if session.state not in sessions.FINAL_STATES:
                    manager.stop(session.id)
            for session in manager.context.state.sessions():
                manager._write_meta(session.id, {"stopped_at": 0})
            manager.cleanup()
            manager.close()
            service.stop()
        subprocess.run(["tmux", "-S", TMUX_SOCKET, "kill-server"], capture_output=True, check=False)


def create(rig: Rig, session_id: str, **changes: Any) -> None:
    reply = rig.client.request("POST", "/v1/sessions", request(session_id=session_id, **changes))
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"}), reply.body


def stop(rig: Rig, session_id: str) -> None:
    reply = rig.client.request("POST", f"/v1/sessions/{session_id}/control", {"action": "stop"})
    assert (reply.status, reply.json()) == (200, {}), reply.body


def wait_active(rig: Rig, session_id: str) -> None:
    wait_for(lambda: unit_property(session_id, "ActiveState") == "active", 20, lambda: rig.pane(session_id))


def main_pid(session_id: str) -> int:
    return int(unit_property(session_id, "MainPID") or "0")


# -- tests ----------------------------------------------------------------------------------------------------


def check_agent_process(rig: Rig, sid: str, name: str, uid: int) -> int:
    """The unit's main process: session UID/GID, no privileges, the allow-listed environment, token nowhere visible."""
    pid = main_pid(sid)
    status = dict(
        line.split(":\t", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":\t" in line
    )
    assert status["Uid"].split() == [str(uid)] * 4
    assert status["Gid"].split() == [str(uid)] * 4
    assert status["NoNewPrivs"].strip() == "1"
    assert status["CapBnd"].strip() == "0000000000000000"
    environ = dict(
        item.split("=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if item
    )
    # Nothing of the control process is inherited; systemd itself adds its service variables (decisions GST-02).
    assert set(environ) - SYSTEMD_ENV == SESSION_ENV | {"ANTHROPIC_MODEL"}
    assert environ["USER"] == name
    assert (environ["TERM"], environ["LANG"], environ["PATH"]) == ("xterm-256color", "C.UTF-8", "/usr/bin:/bin")
    assert environ["HOME"] == f"/srv/sessions/{sid}/home"
    assert environ["CLAUDE_CONFIG_DIR"] == f"/srv/sessions/{sid}/home/.claude"
    assert environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:7700/model"
    token = environ["PLATFORM_SESSION_TOKEN"]
    assert environ["ANTHROPIC_AUTH_TOKEN"] == token
    session = rig.context.state.session(sid)
    assert session is not None
    assert hashlib.sha256(token.encode()).hexdigest() == session.token_sha256
    # The token is on no command line of the VM (``/proc/<pid>/cmdline`` is readable by every user).
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            assert token.encode() not in cmdline.read_bytes(), cmdline
        except (FileNotFoundError, ProcessLookupError):
            continue
    return pid


def test_session_with_the_fake_agent_runs_isolated_and_stops(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    rig = make_rig(sleeper, guest.exec_paths)
    sid = new_id()
    create(rig, sid, limits=LIMITS)
    name = unit.user_name(sid)
    entry = pwd.getpwnam(name)
    assert 20000 <= entry.pw_uid < 30000
    assert entry.pw_gid == entry.pw_uid
    assert entry.pw_shell == "/usr/sbin/nologin"
    assert entry.pw_dir == f"/srv/sessions/{sid}/home"
    base = os.stat(f"/srv/sessions/{sid}")
    assert (stat.S_IMODE(base.st_mode), base.st_uid, base.st_gid) == (0o711, 0, 0)
    for sub in ("/work", "/home", "/home/.claude"):
        info = os.stat(f"/srv/sessions/{sid}{sub}")
        assert (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) == (0o700, entry.pw_uid, entry.pw_uid)
    harness_file = os.stat(f"/run/agent-guest/harness/{sid}.md")
    assert (stat.S_IMODE(harness_file.st_mode), harness_file.st_uid) == (0o444, 0)

    assert sessions.credential_provisioned(rig.context, sid)
    wait_for(lambda: unit_property(sid, "ActiveState") == "active", 20, lambda: rig.pane(sid))
    wait_for(
        lambda: Path(f"/proc/{main_pid(sid)}/exe").exists() and os.readlink(f"/proc/{main_pid(sid)}/exe") == sleeper, 10
    )
    pid = check_agent_process(rig, sid, name, entry.pw_uid)
    assert os.readlink(f"/proc/{pid}/cwd") == f"/srv/sessions/{sid}/work"
    assert unit_property(sid, "User") == name
    # The request's limits (other than the defaults of unit.py) override the defaults.
    assert unit_property(sid, "MemoryMax") == "1073741824"
    assert unit_property(sid, "TasksMax") == "64"
    assert unit_property(sid, "CPUQuotaPerSecUSec") == "500ms"
    assert unit_property(sid, "RuntimeMaxUSec") == "30min"
    assert unit_property(sid, "TimeoutStopUSec") == "10s"
    assert unit_property(sid, "IOAccounting") == "yes"  # io.stat from the first byte (KOR-02, B-17)
    assert Path(f"/sys/fs/cgroup/system.slice/{unit.UNIT_PREFIX}{sid}.service/io.stat").exists()
    assert rig.tmux.has_session(tmux.session_name(sid))
    assert states(rig.context, sid) == ["sandbox_ready", "agent_starting"]

    stop(rig, sid)
    assert unit_property(sid, "ActiveState") in ("inactive", "")
    assert not Path(f"/proc/{pid}").exists()
    wait_for(lambda: not rig.tmux.has_session(tmux.session_name(sid)), 10)
    time.sleep(0.5)
    assert states(rig.context, sid) == ["sandbox_ready", "agent_starting", "stopping", "stopped"]
    assert os.path.isdir(f"/srv/sessions/{sid}/work")


def test_two_sessions_cannot_see_each_other(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    rig = make_rig(sleeper, guest.exec_paths)
    own, other = new_id(), new_id()
    for sid in (own, other):
        create(rig, sid)
        assert rig.manager.start(sid)
    for sid in (own, other):
        wait_active(rig, sid)
    own_uid, other_uid = (pwd.getpwnam(unit.user_name(sid)).pw_uid for sid in (own, other))
    assert own_uid != other_uid
    other_pid = main_pid(other)
    Path(f"/srv/sessions/{other}/work/secret.txt").write_text("other session\n")

    script = SANDBOX_DIR / "visibility.py"
    script.write_text(
        "import errno, json, os, socket, sys\n"
        "other, other_pid = sys.argv[1], sys.argv[2]\n"
        "def probe(path):\n"
        "    try:\n"
        "        os.stat(path)\n"
        "        return 'visible'\n"
        "    except OSError as error:\n"
        "        return errno.errorcode[error.errno]\n"
        "def connect(path):\n"
        "    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    try:\n"
        "        client.connect(path)\n"
        "        return 'connected'\n"
        "    except OSError as error:\n"
        "        return errno.errorcode[error.errno]\n"
        "def listing(path):\n"
        "    try:\n"
        "        return sorted(os.listdir(path))\n"
        "    except OSError as error:\n"
        "        return errno.errorcode[error.errno]\n"
        "print(json.dumps({'uid': os.getuid(), 'sessions': listing('/srv/sessions'),\n"
        "    'harness': listing('/run/agent-guest/harness'),\n"
        "    'other_dir': probe('/srv/sessions/' + other),\n"
        "    'other_secret': probe('/srv/sessions/' + other + '/work/secret.txt'),\n"
        "    'other_proc': probe('/proc/' + other_pid),\n"
        "    'other_harness': probe('/run/agent-guest/harness/' + other + '.md'),\n"
        "    'launch': probe('/run/agent-guest/launch/' + other + '.json'),\n"
        "    'tmux': connect(sys.argv[3]),\n"
        "    'pids': sorted(int(p) for p in os.listdir('/proc') if p.isdigit())}))\n"
    )
    script.chmod(0o644)
    spec = unit.SessionSpec(
        session_id=own,
        token="P" * 43,
        python=guest.python,
        lib_dynload=guest.lib_dynload,
        exec_paths=guest.exec_paths,
        claude=sleeper,
        launcher=str(LAUNCHER),
    )
    program = [guest.python, "-I", str(script), other, str(other_pid), TMUX_SOCKET]
    command = unit.launcher_command(spec, program, extra_exec=[guest.python], extra_ro=[str(script)])
    argv = unit.systemd_run(spec, command, unit_name=f"agent-gst02-probe-{uuid.uuid4().hex}", mode="pipe")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["uid"] == own_uid
    # Landlock does not even allow listing the parents; the mount namespace alone would show only the own entry.
    assert seen["sessions"] in ([own], "EACCES")
    assert seen["harness"] in ([f"{own}.md"], "EACCES")
    assert seen["other_dir"] == "ENOENT"
    assert seen["other_secret"] == "ENOENT"
    assert seen["other_proc"] == "ENOENT"
    assert seen["other_harness"] == "ENOENT"
    assert seen["launch"] in ("ENOENT", "EACCES")
    assert seen["tmux"] in ("ENOENT", "EACCES")
    assert other_pid not in seen["pids"]
    for sid, uid in ((own, own_uid), (other, other_uid)):
        info = os.stat(f"/srv/sessions/{sid}/work")
        assert (info.st_uid, stat.S_IMODE(info.st_mode)) == (uid, 0o700)


def test_an_agent_that_exits_at_once_fails_the_start(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest
) -> None:
    rig = make_rig(guest.claude, guest.exec_paths)
    sid = new_id()
    create(rig, sid)
    assert rig.manager.start(sid)
    wait_for(lambda: "failed" in states(rig.context, sid), 30, lambda: str(states(rig.context, sid)))
    # the waiting initial prompt is refused after the failure (HOT-09): read the session's failure record
    records, _ = rig.context.journal.read(after=0, limit=1000)
    failed = [r["body"] for r in records if r["kind"] == "session.state" and r["body"]["state"] == "failed"]
    assert [body["reason"] for body in failed] == ["agent_start_failed"]
    wait_for(lambda: not rig.tmux.has_session(tmux.session_name(sid)), 10)


def test_a_launcher_failure_fails_the_sandbox(make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest) -> None:
    """The launcher exits 126 when it cannot exec the agent (here: the agent path is a directory)."""
    rig = make_rig(str(SANDBOX_DIR), guest.exec_paths)
    sid = new_id()
    create(rig, sid)
    assert rig.manager.start(sid)
    wait_for(lambda: "failed" in states(rig.context, sid), 30, lambda: str(states(rig.context, sid)))
    # other records (the agent's end, terminal capture, the refusal of the waiting initial prompt) may follow the
    # failure: read the session's failure record
    records, _ = rig.context.journal.read(after=0, limit=1000)
    own = [r["body"] for r in records if r["kind"] == "session.state" and r["session_id"] == sid]
    failed = [body for body in own if body["state"] == "failed"]
    assert failed == [{"state": "failed", "reason": "sandbox_failed", "at": failed[0]["at"]}]


def test_suspend_and_resume_start_the_same_session_again(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    """A resumed session gets a new tmux session (and scope) and a new unit, as the same user."""
    rig = make_rig(sleeper, guest.exec_paths)
    sid = new_id()
    create(rig, sid)
    assert rig.manager.start(sid)
    wait_active(rig, sid)
    first_pid = main_pid(sid)
    rig.manager.stop(sid, "suspended")
    assert unit_property(sid, "ActiveState") in ("inactive", "")
    resume = str(uuid.uuid4())
    create(rig, sid, resume_claude_session_id=resume)
    assert sessions.credential_provisioned(rig.context, sid)
    wait_active(rig, sid)
    second_pid = main_pid(sid)
    assert second_pid != first_pid
    # systemd reports the unit active while its new main process may still be in the spawn stage (named "(thon3.12)",
    # still root): wait until it carries the session user's IDs
    uid = f"\t{pwd.getpwnam(unit.user_name(sid)).pw_uid}"
    wait_for(lambda: Path(f"/proc/{second_pid}/status").read_text().count(uid) >= 2, 10)
    assert rig.tmux.has_session(tmux.session_name(sid))
    time.sleep(1)
    assert states(rig.context, sid) == [
        "sandbox_ready",
        "agent_starting",
        "suspended",
        "sandbox_ready",
        "agent_starting",
    ]
    stop(rig, sid)


# -- real Claude Code against the model double --------------------------------------------------------------


@pytest.fixture(scope="module")
def claude_binary() -> str:
    url, digest = CLAUDE_PACKAGES[platform.machine()]
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / url.rsplit("/", 1)[1]
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        with urllib.request.urlopen(url, timeout=300) as response:
            data = response.read()
        assert hashlib.sha256(data).hexdigest() == digest, "Claude Code package checksum mismatch"
        archive.write_bytes(data)
    target = CLAUDE_DIR / "claude"
    CLAUDE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        member = bundle.extractfile("package/claude")
        assert member is not None
        target.write_bytes(member.read())
    target.chmod(0o755)
    version = subprocess.run([str(target), "--version"], capture_output=True, text=True, check=True, timeout=60)
    assert version.stdout.startswith(CLAUDE_VERSION)
    return str(target)


class _Forward(http.server.BaseHTTPRequestHandler):
    """``/model/<path>`` -> the model double ``/<path>`` (what the guest gateway does, without credentials); ``/mcp``
    stands in for the platform MCP server: ``initialize``, ``ping`` and an empty ``tools/list``, which marks the
    starting sessions of the rigs as having their tools (HOT-09: the first prompt waits for them, ``toolsready.py``)."""

    protocol_version = "HTTP/1.1"
    upstream: tuple[str, int] = ("127.0.0.1", 0)
    contexts: ClassVar[list[ControlContext]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _mcp(self) -> None:
        if self.command != "POST":
            self._empty(405)
            return
        length = int(self.headers.get("Content-Length") or "0")
        message = json.loads(self.rfile.read(length) or b"{}")
        if "id" not in message:
            self._empty(202)
            return
        method = message.get("method")
        answer: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize":
            version = message.get("params", {}).get("protocolVersion", "2025-06-18")
            answer["result"] = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": "stand-in", "version": "0"}}  # fmt: skip
        elif method == "tools/list":
            answer["result"] = {"tools": []}
            for context in self.contexts:
                for session in context.state.sessions():
                    if session.state == "agent_starting":
                        with contextlib.suppress(StateError):
                            toolsready.mark(context, session.id, 0, session.token_sha256)
        elif method == "ping":
            answer["result"] = {}
        else:
            answer["error"] = {"code": -32601, "message": "Method not found."}
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        if self.path.split("?", 1)[0] == "/mcp":
            self._mcp()
            return
        if not self.path.startswith("/model/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection(*self.upstream, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
        connection.request(self.command, self.path[len("/model") :], body=body, headers=headers)
        response = connection.getresponse()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := response.read1(65536):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        connection.close()

    do_GET = do_POST = do_HEAD = _forward


def _release_platform_port() -> None:
    """Shut down the conftest's stand-in listener on 7700 (the forwarder needs the port)."""
    for item in gc.get_objects():
        if isinstance(item, socket.socket) and item.fileno() != -1:
            try:
                name = item.getsockname()
            except OSError:
                continue
            if name == ("127.0.0.1", unit.PLATFORM_PORT):
                with contextlib.suppress(OSError):
                    item.shutdown(socket.SHUT_RDWR)
                item.close()


@pytest.fixture
def model_gateway() -> Iterator[ModelDouble]:
    double = ModelDouble(packaged_scenario("hello"))
    double.start()
    _release_platform_port()
    _Forward.upstream = ("127.0.0.1", int(double.url.rsplit(":", 1)[1]))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", unit.PLATFORM_PORT), _Forward)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True)
    thread.start()
    try:
        yield double
    finally:
        server.shutdown()
        server.server_close()
        double.stop()
        _restore_platform_listener()


def _restore_platform_listener() -> None:
    """Later tests expect something listening on 7700 again (conftest contract)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", unit.PLATFORM_PORT))
    server.listen(16)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=serve, daemon=True).start()


def test_real_claude_code_answers_through_the_session(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, claude_binary: str, model_gateway: ModelDouble
) -> None:
    ctypes_so = next(Path(guest.lib_dynload).glob("_ctypes.cpython-*.so"))
    rig = make_rig(claude_binary, tuple(ldd_paths(claude_binary, guest.python, str(ctypes_so))))
    sid = new_id()
    model = {"access": "platform", "model_id": "double-model", "env": {"ANTHROPIC_MODEL": "double-model"}}
    create(rig, sid, model=model, initial_prompt={"prompt_id": uuid7(time.time()), "text": "What is 2+2?"})
    assert sessions.credential_provisioned(rig.context, sid)

    def describe() -> str:
        return f"pane:\n{rig.pane(sid)}\ndouble:\n{json.dumps(model_gateway.state(), indent=1)[:4000]}"

    started = time.monotonic()
    wait_for(lambda: "2+2 is 4." in rig.pane(sid), 90, describe)
    print(f"answer in the pane after {time.monotonic() - started:.1f} s:\n{rig.pane(sid)}")  # visible with -s
    assert unit_property(sid, "ActiveState") == "active"
    # Claude Code writes its transcript below CLAUDE_CONFIG_DIR; it may paint the answer before the file is written
    projects = Path(f"/srv/sessions/{sid}/home/.claude/projects")
    wait_for(lambda: list(projects.rglob("*.jsonl")), 30, describe)
    state = model_gateway.state()
    assert state["ok"], describe()
    stop(rig, sid)
    wait_for(lambda: not rig.tmux.has_session(tmux.session_name(sid)), 20)
    assert states(rig.context, sid)[-1] == "stopped"


# -- user database ---------------------------------------------------------------------------------------------


def test_session_users_get_no_subordinate_id_ranges() -> None:
    """Many session users in one VM: none gets a /etc/subuid or /etc/subgid range (they ran out after five, ATS-01)."""
    system = sessions.LinuxSystem()
    names = [f"s-atk{index:09d}" for index in range(12)]
    try:
        for name in names:
            system.add_user(name, sessions.free_uid(system.used_ids()), "/nonexistent")
            assert system.user_uid(name) is not None
        for path in ("/etc/subuid", "/etc/subgid"):
            entries = Path(path).read_text().splitlines() if Path(path).exists() else []
            assert not [line for line in entries if line.split(":", 1)[0] in names]
    finally:
        for name in names:
            system.remove_user(name)


# -- restart of the control process (KOR-02, AUD-01 A-05) -----------------------------------------------------


def terminal_text(context: ControlContext, session_id: str) -> str:
    records, _ = context.journal.read(after=0, limit=10000)
    return "".join(
        frame[2]
        for record in records
        if record["kind"] == "session.terminal" and record["session_id"] == session_id
        for frame in record["body"]["frames"]
    )


def type_into(rig: Rig, session_id: str, text: str) -> None:
    """Keys into the pane: the fake agent never reads them, the pty echoes them (terminal output of the session)."""
    pane = f"={tmux.session_name(session_id)}:"
    subprocess.run(rig.tmux.argv("send-keys", "-t", pane, "-l", "--", text), check=True, env=tmux.CLIENT_ENV)


def crash(rig: Rig) -> None:
    """What a killed `agent-guest-control` leaves: no thread of it runs on and nothing is flushed; units, tmux and
    the files on disk (journal, meta, cast file) stay. The process-wide objects are dropped from the context."""
    rig.manager.close()
    current = capture.capture(rig.context)
    for found in list(current.sessions.values()):
        found.stop.set()
        if found.recorder is not None and found.recorder._thread is not None:
            found.recorder._stop.set()
            found.recorder._thread.join(5)
        if found.follower is not None:
            found.follower.stop()
    delivery.manager(rig.context).close()
    for name in ("_session_manager", "_capture", "_prompt_delivery"):
        vars(rig.context).pop(name, None)


def test_a_restarted_control_process_sees_the_agent_end_and_records_its_output(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    rig = make_rig(sleeper, guest.exec_paths)
    sid = new_id()
    create(rig, sid, limits=LIMITS)
    assert sessions.credential_provisioned(rig.context, sid)
    started = time.monotonic()
    wait_active(rig, sid)
    type_into(rig, sid, "before-restart")
    wait_for(lambda: "before-restart" in terminal_text(rig.context, sid), 10, lambda: rig.pane(sid))
    crash(rig)

    again = sessions.SessionManager(
        context=rig.context,
        system=sessions.LinuxSystem(),
        tmux=rig.tmux,
        driver=rig.manager.driver,
        paths=rig.manager.paths,
        interpreter=rig.manager.interpreter,
        exec_paths=rig.manager.exec_paths,
    )
    sessions.install(rig.context, again)
    run_startup(rig.context, startup_hooks())
    assert sid in again._running
    assert unit_property(sid, "ActiveState") == "active"

    type_into(rig, sid, "after-restart")
    wait_for(lambda: "after-restart" in terminal_text(rig.context, sid), 10, lambda: rig.pane(sid))
    assert "recorder restarted" in terminal_text(rig.context, sid)  # the marker of the continued recording
    # the end of the agent: its process is killed outside the control process; the re-attached watcher sees it and
    # counts the run from its start before the restart (beyond the start window: `agent_exited`)
    time.sleep(max(0.0, sessions.START_WINDOW_SECONDS + 0.5 - (time.monotonic() - started)))
    os.kill(main_pid(sid), 9)
    wait_for(lambda: "failed" in states(rig.context, sid), 20, lambda: str(states(rig.context, sid)))
    records, _ = rig.context.journal.read(after=0, limit=10000)
    reason = [
        r["body"].get("reason")
        for r in records
        if r["kind"] == "session.state" and r["session_id"] == sid and r["body"]["state"] == "failed"
    ][-1]
    assert reason == "agent_exited"
    wait_for(lambda: not rig.tmux.has_session(tmux.session_name(sid)), 10)


def test_a_control_restart_during_a_suspend_ends_the_session_suspended(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    """NA-03 (AUD-02): `agent-guest-control` dies while the suspend waits for `systemctl stop` of the session unit; the
    restarted process finishes the suspend - the session ends `suspended`, not `failed agent_exited` - and the session
    can be resumed (a new start request with the same ID)."""
    rig = make_rig(sleeper, guest.exec_paths)
    sid = new_id()
    create(rig, sid, limits=LIMITS)
    assert sessions.credential_provisioned(rig.context, sid)
    wait_active(rig, sid)
    session = rig.context.state.session(sid)
    assert session is not None
    rig.context.state.save_session(replace(session, state="idle"))
    entered, released = threading.Event(), threading.Event()
    real_stop = rig.manager.system.stop_unit

    def stalled_stop(name: str) -> None:
        entered.set()
        released.wait(30)  # the dying process never returns from here
        raise SystemExit("agent-guest-control killed")

    rig.manager.system.stop_unit = stalled_stop  # type: ignore[method-assign]
    suspending = threading.Thread(target=lambda: _suspend_ignoring_exit(rig, sid), daemon=True)
    suspending.start()
    assert entered.wait(10)
    crash(rig)
    rig.manager.system.stop_unit = real_stop  # type: ignore[method-assign]

    again = sessions.SessionManager(
        context=rig.context, system=sessions.LinuxSystem(), tmux=rig.tmux, driver=rig.manager.driver,
        paths=rig.manager.paths, interpreter=rig.manager.interpreter, exec_paths=rig.manager.exec_paths,
    )  # fmt: skip
    sessions.install(rig.context, again)
    run_startup(rig.context, startup_hooks())
    assert states(rig.context, sid)[-1] == "suspended", states(rig.context, sid)
    assert "failed" not in states(rig.context, sid)
    wait_for(lambda: unit_property(sid, "ActiveState") in ("inactive", ""), 20)
    assert not rig.tmux.has_session(tmux.session_name(sid))
    # The next follow-up prompt resumes it: the runner sends the start request again.
    create(rig, sid, limits=LIMITS, resume_claude_session_id="5f0c6a3e-2b1d-4c8e-9a7f-1e2d3c4b5a69")
    assert sessions.credential_provisioned(rig.context, sid)
    wait_active(rig, sid)
    again.stop(sid)  # the rig's teardown knows only the first manager
    again.close()
    # The "dead" process writes nothing any more; only then its blocked thread may end.
    dead = rig.manager
    for name in ("_transition", "_mark_stopped", "_set_suspending"):
        setattr(dead, name, lambda *args: None)
    released.set()
    suspending.join(10)


def _suspend_ignoring_exit(rig: Rig, sid: str) -> None:
    with contextlib.suppress(SystemExit):
        rig.manager.stop(sid, "suspended")


def test_a_session_whose_start_was_lost_fails_after_a_restart(
    make_rig: Callable[[str, tuple[str, ...]], Rig], guest: Guest, sleeper: str
) -> None:
    rig = make_rig(sleeper, guest.exec_paths)
    sid = new_id()
    create(rig, sid, limits=LIMITS)
    crash(rig)
    again = sessions.SessionManager(
        context=rig.context, system=sessions.LinuxSystem(), tmux=rig.tmux, driver=rig.manager.driver,
        paths=rig.manager.paths, interpreter=rig.manager.interpreter, exec_paths=rig.manager.exec_paths,
    )  # fmt: skip
    sessions.install(rig.context, again)
    run_startup(rig.context, startup_hooks())
    assert states(rig.context, sid) == ["sandbox_ready", "failed"]


def test_a_unit_killed_by_the_oom_killer_is_reported() -> None:
    system = sessions.LinuxSystem()
    name = f"{unit.UNIT_PREFIX}oom-{uuid.uuid4().hex[:12]}"
    hog = "b = bytearray(1 << 30)"
    subprocess.run(
        ["systemd-run", "--wait", "--collect", "--quiet", f"--unit={name}", "-p", "MemoryMax=64M",
         "-p", "MemorySwapMax=0", "/usr/bin/python3", "-c", hog],
        capture_output=True, check=False, timeout=120,
    )  # fmt: skip
    wait_for(lambda: system.oom_killed(name), 20)
    calm = f"{unit.UNIT_PREFIX}calm-{uuid.uuid4().hex[:12]}"
    subprocess.run(["systemd-run", "--wait", "--collect", "--quiet", f"--unit={calm}", "/bin/true"], check=True)
    assert not system.oom_killed(calm)
