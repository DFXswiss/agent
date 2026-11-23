"""Fixtures of the Linux-only guest tests (marker `linux`, docs/platform/spec/07-testing.md section 1).

They run as root on a Linux kernel with Landlock ABI >= 4 and systemd: in the `guest-ci` Lima VM
(`make platform-ci-linux`) or in the GitHub `ubuntu-24.04` job. A missing prerequisite fails, it never skips.

The fixtures provide what the golden image provides in production: a stand-in for the Claude binary, the exec
paths of `build.json` (computed with `ldd` like the golden build), `/srv/sessions` as a `noexec` mount, the guest
directories hidden from sessions, a listener on the platform port 7700 and the egress rules of 05-runner.md
section 4.3 (a test copy of the nftables table under its own name).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import agent_guest
from agent_guest.sandbox import unit
from agent_guest.sandbox.selftest import ThrowawaySessions

SRC = Path(agent_guest.__file__).resolve().parents[1]
# Never /opt/claude or /opt/agent-guest: a guest-ci VM cloned from a golden image has the real files there.
TEST_ROOT = Path("/opt/agent-test")
CLAUDE_STUB = TEST_ROOT / "claude"
# Session units cannot read a checkout below /home (ProtectHome=yes, the GitHub runner's case): they run copies of
# the two standalone scripts. The in-process tests import the checkout itself (that is what coverage measures).
SANDBOX_DIR = TEST_ROOT / "sandbox"
LAUNCHER = SANDBOX_DIR / "landlock.py"
PROBE = SANDBOX_DIR / "probe.py"
NFT_TABLE = "agent_sessions_test"
NFT_RULES = f"""
table inet {NFT_TABLE} {{
  chain out {{
    type filter hook output priority 0; policy accept;
    meta skuid 20000-29999 oif lo tcp dport 7700 accept
    meta skuid 20000-29999 ct state established,related accept
    meta skuid 20000-29999 counter drop
  }}
}}
"""


@dataclass(frozen=True)
class Guest:
    python: str
    lib_dynload: str
    exec_paths: tuple[str, ...]
    claude: str
    interpreter: str


@pytest.fixture(scope="session", autouse=True)
def linux_root() -> None:
    if platform.system() != "Linux" or os.geteuid() != 0:
        pytest.fail("the Linux-only guest tests need Linux and root (make platform-ci-linux)")
    if shutil.which("systemd-run") is None or shutil.which("nft") is None:
        pytest.fail("systemd-run and nft are required")


def ldd_paths(*files: str) -> list[str]:
    """Resolved shared objects of the files (the golden build's `exec_libs` computation, 05 section 3.1)."""
    found: set[str] = set()
    for path in files:
        out = subprocess.run(["ldd", path], capture_output=True, text=True, check=True).stdout
        for match in re.findall(r"(/\S+\.so[^\s]*)", out):
            found.add(os.path.realpath(match))
    return sorted(found)


@pytest.fixture(scope="session")
def guest() -> Guest:
    python, lib_dynload = unit.interpreter_paths("/usr/bin/python3")
    SANDBOX_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    for script in (LAUNCHER, PROBE):
        shutil.copyfile(SRC / "agent_guest" / "sandbox" / script.name, script)
        script.chmod(0o644)
    shutil.copyfile("/usr/bin/echo", CLAUDE_STUB)
    CLAUDE_STUB.chmod(0o755)
    ctypes_so = next(Path(lib_dynload).glob("_ctypes.cpython-*.so"))
    exec_paths = ldd_paths(str(CLAUDE_STUB), python, str(ctypes_so))
    interpreter = next(path for path in exec_paths if "/ld-linux" in path)
    return Guest(python, lib_dynload, tuple(exec_paths), str(CLAUDE_STUB), interpreter)


@pytest.fixture(scope="session", autouse=True)
def sessions_mount() -> Iterator[None]:
    """`/srv/sessions` as its own `noexec,nosuid,nodev` mount (fstab bind mount of the golden image)."""
    root = Path(unit.SESSIONS_ROOT)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    mounted = subprocess.run(["mountpoint", "-q", str(root)], check=False).returncode != 0
    if mounted:
        subprocess.run(["mount", "--bind", str(root), str(root)], check=True)
        subprocess.run(["mount", "-o", "remount,bind,noexec,nosuid,nodev", str(root)], check=True)
    yield
    if mounted:
        subprocess.run(["umount", str(root)], check=True)


@pytest.fixture(scope="session", autouse=True)
def guest_dirs() -> None:
    """Directories the session units hide or need (created by the golden image in production)."""
    secrets_dir = Path("/etc/agent-guest")
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    (secrets_dir / "control-token").write_text("test\n")
    Path(unit.HARNESS_DIR).mkdir(mode=0o711, parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def platform_listener() -> Iterator[None]:
    """A TCP listener on 127.0.0.1:7700 standing in for agent-guest-api (left alone when one runs already)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", unit.PLATFORM_PORT))
    except OSError:
        server.close()
        yield
        return
    server.listen(16)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield
    stop.set()
    server.close()
    thread.join(timeout=5)


@pytest.fixture(scope="session", autouse=True)
def egress_rules() -> Iterator[None]:
    """The session part of the nftables table of 05 section 4.3 (SBX-01 ships the production file)."""
    subprocess.run(["nft", "-f", "-"], input=NFT_RULES, text=True, check=True)
    yield
    subprocess.run(["nft", "delete", "table", "inet", NFT_TABLE], check=True)


@pytest.fixture
def sessions() -> Iterator[ThrowawaySessions]:
    """A session (user, directories, harness) and a second "other" session; removed afterwards."""
    created = ThrowawaySessions()
    try:
        created.create()
        yield created
    finally:
        created.remove()


@pytest.fixture
def spec(guest: Guest, sessions: ThrowawaySessions) -> unit.SessionSpec:
    return unit.SessionSpec(
        session_id=sessions.own,
        token="T" * 43,
        python=guest.python,
        lib_dynload=guest.lib_dynload,
        exec_paths=guest.exec_paths,
        claude=guest.claude,
        launcher=str(LAUNCHER),
    )
