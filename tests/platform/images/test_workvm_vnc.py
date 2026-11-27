"""U-RUN-vnc (image): the VNC helper `workvm-vnc` of the work image (ADR-103 section 2).

The helper runs with the system Python against a fake server program (`WORKVM_VNC_SERVER`): `-storepasswd` writes the
password file, otherwise it listens on the loopback port and answers every connection with its version line and an
echo. `WORKVM_VNC_START=direct` starts it without systemd, `WORKVM_VNC_DIR` and `WORKVM_VNC_PORT` keep the test apart
from a real server. The static checks of the image wiring always run."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "images" / "workvm" / "files" / "usr" / "local" / "bin" / "workvm-vnc"
PROVISION = REPO_ROOT / "images" / "workvm" / "provision.sh"
FAKE_SERVER = """
import socket, sys, threading
args = sys.argv[1:]
if args[0] == "-storepasswd":
    open(args[2], "w").write("stored:" + args[1])
    sys.exit(0)
port = int(args[args.index("-rfbport") + 1])
assert "-localhost" in args and "-rfbauth" in args and args[args.index("-display") + 1] == ":99"
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
def serve(conn):
    conn.sendall(b"RFB 003.008\\n")
    while data := conn.recv(4096):
        conn.sendall(data)
    conn.close()
while True:
    conn, _ = server.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Helper:
    def __init__(self, root: Path) -> None:
        server = root / "fake-x11vnc"
        server.write_text(f"#!{sys.executable}\n{FAKE_SERVER}", encoding="utf-8")
        server.chmod(0o755)
        self.state = root / "state"
        self.env = dict(
            os.environ,
            WORKVM_VNC_DIR=str(self.state),
            WORKVM_VNC_PORT=str(free_port()),
            WORKVM_VNC_SERVER=str(server),
            WORKVM_VNC_START="direct",
        )

    def run(self, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            input=stdin,
            env=self.env,
            capture_output=True,
            timeout=30,
            check=False,
        )


@pytest.fixture
def helper(tmp_path: Path) -> Iterator[Helper]:
    found = Helper(tmp_path)
    try:
        yield found
    finally:
        found.run("stop")


def test_the_image_installs_the_helper_the_display_and_the_server() -> None:
    text = PROVISION.read_text(encoding="utf-8")
    assert 'install -m 0755 "$FILES/usr/local/bin/workvm-vnc" /usr/local/bin/workvm-vnc' in text
    assert "xvfb x11vnc" in text
    assert "/usr/local/bin/workvm-vnc selftest" in text
    assert text.index("workvm-vnc selftest") < text.index('touch "$GUARD"')
    source = HELPER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env python3\n")
    assert 'os.environ.get("WORKVM_VNC_DIR", "/run/workvm-vnc")' in source
    assert '"-localhost",' in source
    assert '"-rfbauth", password_file(),' in source


def test_start_serves_the_display_for_the_one_time_password_only_on_the_loopback(helper: Helper) -> None:
    started = helper.run("start", stdin=b"Ab3dEf7h\n")
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout) == {"ok": True}
    stored = helper.state / "passwd"
    assert stored.read_text() == "stored:Ab3dEf7h"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert helper.state.stat().st_mode & 0o777 == 0o700
    joined = subprocess.run(
        [sys.executable, str(HELPER), "connect"],
        input=b"hello",
        env=helper.env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert joined.returncode == 0
    assert joined.stdout == b"RFB 003.008\nhello"
    again = helper.run("start", stdin=b"Zz9yXx8w\n")  # a running server is replaced
    assert json.loads(again.stdout) == {"ok": True}
    assert stored.read_text() == "stored:Zz9yXx8w"
    assert helper.run("stop").returncode == 0
    assert not stored.exists()
    assert helper.run("connect").returncode == 1  # no server any more


@pytest.mark.parametrize("password", [b"short\n", b"Ab3dEf7h9\n", b"Ab3dEf7!\n", b""])
def test_a_password_of_the_wrong_form_starts_nothing(helper: Helper, password: bytes) -> None:
    refused = helper.run("start", stdin=password)
    assert refused.returncode == 1
    assert json.loads(refused.stdout) == {"ok": False}
    assert not (helper.state / "passwd").exists()


def test_usage_and_selftest(helper: Helper, tmp_path: Path) -> None:
    assert helper.run().returncode == 2
    assert helper.run("dance").returncode == 2
    missing = dict(helper.env, WORKVM_VNC_SERVER=str(tmp_path / "nothing"), PATH="/nonexistent")
    selftest = subprocess.run(
        [sys.executable, str(HELPER), "selftest"], env=missing, capture_output=True, timeout=30, check=False
    )
    assert selftest.returncode == 1
    assert b"missing" in selftest.stderr
    if shutil.which("Xvfb") is not None:
        found = subprocess.run(
            [sys.executable, str(HELPER), "selftest"], env=helper.env, capture_output=True, timeout=30, check=False
        )
        assert json.loads(found.stdout) == {"ok": True}
