"""L-GST-orchestrator (marker linux, as root): the account of the main orchestrator's agent tool on a real kernel
(05-runner.md section 17.4, ADR-082 section 9.7, key-model.md KM-37/KM-41).

Attack tests: a session user (the UID Claude Code runs as) can neither read the tool's state directory with the key
nor connect to the relay socket; the tool runs as its account with nothing of the control process's environment; the
end of the orchestrator deletes the key.
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import socket
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_guest.apisock import ApiSockServer
from agent_guest.control import bind_unix
from agent_guest.orchestrator import LinuxTool

pytestmark = pytest.mark.linux

ROOT = Path("/opt/agent-test/orchestrator")
SESSION_UID = 29987


@dataclass(frozen=True)
class Accounts:
    tool: str
    session: str
    api: str
    """Stands in for the API process's user `platform` (the group of the MCP socket's directory)."""
    state: Path
    run: Path


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@pytest.fixture
def accounts() -> Iterator[Accounts]:
    """A tool account with its 0700 state directory (as install.sh and tmpfiles make them) and a session user."""
    suffix = secrets.token_hex(3)
    tool, session, api = f"agent-orch-t{suffix}", f"s-orch-t{suffix}", f"p-orch-t{suffix}"
    _run(["groupadd", "--system", tool]).check_returncode()
    _run(["useradd", "--system", "--gid", tool, "--no-create-home", "--home-dir", "/nonexistent", "--shell",
          "/usr/sbin/nologin", tool]).check_returncode()  # fmt: skip
    _run(["useradd", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "--uid",
          str(SESSION_UID), "--user-group", session]).check_returncode()  # fmt: skip
    _run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
          "--user-group", api]).check_returncode()  # fmt: skip
    base = ROOT / suffix
    state, run = base / "lib", base / "run"
    for path, mode in ((base, 0o755), (state, 0o700), (run, 0o710)):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    uid = pwd.getpwnam(tool).pw_uid
    os.chown(state, uid, uid)
    os.chown(run, uid, pwd.getpwnam(api).pw_gid)  # tmpfiles: d /run/agent-orch 0710 agent-orch platform
    try:
        yield Accounts(tool=tool, session=session, api=api, state=state, run=run)
    finally:
        _run(["userdel", api])
        _run(["userdel", session])
        _run(["userdel", tool])
        _run(["groupdel", tool])
        subprocess.run(["rm", "-rf", str(base)], check=False)


def as_user(user: str, code: str) -> subprocess.CompletedProcess[str]:
    return _run(["runuser", "-u", user, "--", "/usr/bin/python3", "-I", "-c", code])


def test_a_session_user_cannot_read_the_key_or_reach_the_relay(accounts: Accounts) -> None:
    key = accounts.state / "state" / "pending.key"
    tool_uid = pwd.getpwnam(accounts.tool).pw_uid
    key.parent.mkdir(mode=0o700)
    key.write_text("private")
    for path in (key.parent, key):
        os.chown(path, tool_uid, tool_uid)
    os.chmod(key, 0o600)
    read = as_user(accounts.session, f"open({str(key)!r}).read()")
    assert read.returncode != 0
    assert "PermissionError" in read.stderr
    listing = as_user(accounts.session, f"import os; os.listdir({str(accounts.state)!r})")
    assert listing.returncode != 0
    # the tool's own account reads it
    assert as_user(accounts.tool, f"print(open({str(key)!r}).read())").stdout.strip() == "private"
    relay = str(accounts.run.parent / "orch.sock")
    server = bind_unix(relay, accounts.tool, lambda path: ApiSockServer(path, {}, None))
    try:
        assert stat.S_IMODE(os.stat(relay).st_mode) == 0o660
        code = f"import socket; s = socket.socket(socket.AF_UNIX); s.connect({relay!r})"
        refused = as_user(accounts.session, code)
        assert refused.returncode != 0
        assert "PermissionError" in refused.stderr
        assert as_user(accounts.tool, code).returncode == 0
    finally:
        server.server_close()


def test_the_tool_runs_as_its_account_with_nothing_inherited(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = accounts.run.parent / "tool.py"
    script.write_text(
        "#!/usr/bin/python3 -I\n"
        "import json, os, sys\n"
        "print(json.dumps({'ok': True, 'uid': os.getuid(), 'env': sorted(os.environ), 'args': sys.argv[1:],"
        " 'stdin': sys.stdin.read()}))\n"
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv("AGENT_SECRET_OF_ROOT", "x")
    tool = LinuxTool(
        str(accounts.run / "orch.sock"),
        str(accounts.run / "mcp.sock"),
        tool=str(script),
        state_dir=str(accounts.state / "state"),
        account=accounts.tool,
    )
    result = tool.run(["pair", "--session"], {"session_id": "s"})
    assert result["uid"] == pwd.getpwnam(accounts.tool).pw_uid
    assert result["args"] == ["pair", "--session"]
    assert json.loads(result["stdin"]) == {"session_id": "s"}
    assert set(result["env"]) <= {"PATH", "LANG", "LC_CTYPE", "AGENT_PLATFORM_HOME", "AGENT_PLATFORM_RELAY"}
    assert {"AGENT_PLATFORM_HOME", "AGENT_PLATFORM_RELAY"} <= set(result["env"])


def test_the_end_deletes_the_key(accounts: Accounts) -> None:
    state = accounts.state / "state"
    (state / "keys").mkdir(parents=True)
    (state / "keys" / "agent.key").write_text("private")
    tool = LinuxTool(str(accounts.run / "orch.sock"), str(accounts.run / "mcp.sock"), state_dir=str(state))
    tool.stop()
    assert list(state.iterdir()) == []


def test_a_session_user_cannot_reach_the_mcp_socket_or_run_the_tool(accounts: Accounts) -> None:
    """Review KOR-57 b no. 12: the MCP socket is reached by the API process only, the tool is run by its account
    only (05-runner.md sections 17.4 and 17.5)."""
    mcp = accounts.run / "mcp.sock"
    serve = (
        "import os, socket, time; s = socket.socket(socket.AF_UNIX); os.umask(0); "
        f"s.bind({str(mcp)!r}); s.listen(1); time.sleep(30)"
    )
    server = subprocess.Popen(["runuser", "-u", accounts.tool, "--", "/usr/bin/python3", "-I", "-c", serve])
    try:
        for _ in range(100):
            if mcp.exists():
                break
            subprocess.run(["sleep", "0.05"], check=False)
        connect = f"import socket; s = socket.socket(socket.AF_UNIX); s.connect({str(mcp)!r})"
        refused = as_user(accounts.session, connect)
        assert refused.returncode != 0
        assert "PermissionError" in refused.stderr
        assert as_user(accounts.api, connect).returncode == 0
    finally:
        server.kill()
        server.wait()
    # the tool's tree as install.sh leaves it: root:agent-orch, directories 0750
    tree = accounts.run.parent / "opt"
    (tree / "bin").mkdir(parents=True)
    launcher = tree / "bin" / "agent-platform"
    launcher.write_text("#!/usr/bin/python3 -I\nprint('ran')\n")
    gid = pwd.getpwnam(accounts.tool).pw_gid
    for path, mode in ((tree, 0o750), (tree / "bin", 0o750), (launcher, 0o750)):
        os.chown(path, 0, gid)
        os.chmod(path, mode)
    denied = _run(["runuser", "-u", accounts.session, "--", str(launcher)])
    assert denied.returncode != 0
    assert "ran" not in denied.stdout
    assert _run(["runuser", "-u", accounts.api, "--", str(launcher)]).returncode != 0
    assert _run(["runuser", "-u", accounts.tool, "--", str(launcher)]).stdout.strip() == "ran"


def test_the_account_of_the_tool_reaches_no_network(accounts: Accounts) -> None:
    """The rule of the image (`agent-sessions.nft`) for the account, in a table of its own: everything it sends is
    dropped, also on loopback; root reaches the same listener."""
    # the rule of images/files/etc/nftables.d/agent-sessions.nft (tests/platform/images/test_files.py keeps it there;
    # the VM of this suite has no images/)
    rule = 'meta skuid "agent-orch" counter drop'
    table = f"agent_orch_test_{secrets.token_hex(3)}"
    rule = rule.replace('"agent-orch"', f'"{accounts.tool}"')
    chain = f"  chain out {{\n    type filter hook output priority 0; policy accept;\n    {rule}\n  }}\n"
    text = f"table inet {table} {{\n{chain}}}\n"
    subprocess.run(["nft", "-f", "-"], input=text, text=True, check=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2)"
        blocked = as_user(accounts.tool, code)
        assert blocked.returncode != 0
        assert "Error" in blocked.stderr or "timed out" in blocked.stderr
        assert as_user(accounts.api, code).returncode == 0
    finally:
        listener.close()
        subprocess.run(["nft", "delete", "table", "inet", table], check=False)
