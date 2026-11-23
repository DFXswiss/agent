"""Device: processes, one-shot commands, HTTP, ports, relays, egress, files."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import tarfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from orchestrator.device import Device, DeviceError, expand, expand_env, run_command

PY = sys.executable


@pytest.fixture
def device(tmp_path: Path) -> Iterator[Device]:
    dev = Device(tmp_path / "run")
    yield dev
    dev.shutdown()


def test_processes(device: Device, tmp_path: Path) -> None:
    env = dict(os.environ)
    pid = device.spawn(
        "echo", [PY, "-c", "import time; print('ready', flush=True); time.sleep(30)"], env, cwd=str(tmp_path)
    )
    assert pid > 0
    assert device.wait_line("echo", "^ready$", 10) == "ready"
    assert device.alive("echo")
    assert device.processes()["echo"]["alive"] is True
    assert device.pids() == [pid]
    with pytest.raises(DeviceError, match="already running"):
        device.spawn("echo", [PY, "-c", "pass"], env)
    assert device.stop("echo") == -15
    assert not device.alive("echo")
    assert device.stop("echo") == -15
    device.spawn("echo", [PY, "-c", "print('again')"], env)
    assert device.wait_exit("echo", 10) == 0
    # A restart starts a fresh log; the earlier ones are kept numbered.
    assert device.log_tail("echo") == "again"
    device.spawn("echo", [PY, "-c", "print('third')"], env)
    assert device.wait_exit("echo", 10) == 0
    assert device.log_tail("echo") == "third"
    logs = device.logs
    assert ((logs / "echo.1.log").read_text(), (logs / "echo.2.log").read_text()) == ("ready\n", "again\n")
    with pytest.raises(DeviceError, match="invalid process name"):
        device.spawn("Bad Name", ["true"], env)
    with pytest.raises(DeviceError, match="cannot start nonexistent"):
        device.spawn("missing", ["/nonexistent"], env)
    with pytest.raises(DeviceError, match="unknown process nope"):
        device.alive("nope")


def test_wait_failures_and_stubborn_processes(device: Device) -> None:
    env = dict(os.environ)
    device.spawn("quits", [PY, "-c", "print('bye')"], env)
    with pytest.raises(DeviceError, match="quits exited with 0:\nbye"):
        device.wait_line("quits", "never", 10)
    stubborn = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
    )
    device.spawn("stubborn", [PY, "-c", stubborn], env)
    device.wait_line("stubborn", "up", 10)
    with pytest.raises(DeviceError, match="no line matching 'never' within 0 s"):
        device.wait_line("stubborn", "never", 0)
    assert device.stop("stubborn", timeout=0.2) == -9
    device.spawn("slow", [PY, "-c", "import time; time.sleep(60)"], env)
    with pytest.raises(DeviceError, match="slow did not finish within 0 s"):
        device.wait_exit("slow", 0.1)
    assert not device.alive("slow")


def test_run_and_expand(device: Device, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    done = device.run(["sh", "-c", "echo $X; pwd"], env={"X": "~/a", "PATH": os.environ["PATH"]}, cwd="~")
    assert done == {"rc": 0, "stdout": f"{tmp_path}/a\n{os.path.realpath(tmp_path)}\n", "stderr": ""}
    assert run_command(["/nonexistent"], None, None, 5)["rc"] == 127
    assert run_command([PY, "-c", "import time; time.sleep(5)"], None, None, 0.1)["stderr"].endswith("TimeoutExpired")
    assert expand(["~/x", "a~/b", "~", "~x"]) == [f"{tmp_path}/x", "a~/b", str(tmp_path), "~x"]
    assert expand_env(None) is None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        body = b'{"ok": true}' if self.path == "/json" else b"plain"
        self.send_response(200 if self.path != "/missing" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_http(device: Device) -> None:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert device.http("GET", f"{base}/json") == {"status": 200, "body": {"ok": True}}
        assert device.http("GET", f"{base}/missing") == {"status": 404, "body": "plain"}
        assert device.http("POST", f"{base}/echo", {"a": 1}) == {"status": 201, "body": {"a": 1}}
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(DeviceError, match=r"GET http://127\.0\.0\.1:1/x: "):
        device.http("GET", "http://127.0.0.1:1/x", timeout=1)


def test_ports_relays_egress_files(device: Device, tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert device.busy_ports([taken]) == [taken]
        with pytest.raises(DeviceError, match=f"cannot listen on 127.0.0.1:{taken}"):
            device.relay_start("bad", ["127.0.0.1", taken], ["127.0.0.1", taken])
    state = device.relay_start("hub", ["127.0.0.1", 0], ["127.0.0.1", 9])
    assert state["cut"] is False
    with pytest.raises(DeviceError, match="relay hub exists"):
        device.relay_start("hub", ["127.0.0.1", 0], ["127.0.0.1", 9])
    assert device.relay_cut("hub")["cut"] is True
    assert device.relay_restore("hub")["cut"] is False
    assert list(device.relays()) == ["hub"]
    device.relay_start("tapped", ["127.0.0.1", 0], ["127.0.0.1", 9], tap=str(tmp_path / "tap"))
    assert (tmp_path / "tap").is_dir()
    with pytest.raises(DeviceError, match="unknown link other"):
        device.relay_cut("other")
    device.egress_start(0.05, ["192.0.2.1"])
    assert device.egress.peers == {"192.0.2.1"}
    device.spawn("sleeper", [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    assert isinstance(device.egress_records(), list)
    (tmp_path / "note.txt").write_text("hello")
    assert device.read_text(str(tmp_path / "note.txt")) == "hello"
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(device.archive())), mode="r:gz") as tar:
        assert "./logs/sleeper.log" in tar.getnames()
    assert device.shutdown() == {"sleeper": -15}
    assert device.relays() == {}
    assert json.loads(json.dumps(device.processes()))["sleeper"]["alive"] is False


def test_abandon_cleanup_and_sampling_watchdog(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    answers = {"list": {"rc": 0, "stdout": "wp-t-e2e-r1-a\nother\n", "stderr": ""}}

    def runner(argv: list[str], env: object, cwd: object, timeout: float, track: object = None) -> dict[str, object]:
        calls.append(list(argv))
        if argv[:2] == ["limactl", "list"]:
            return answers["list"]
        if argv[0] == "ps":
            return {"rc": 1, "stdout": "", "stderr": ""}
        return {"rc": 0, "stdout": "", "stderr": ""}

    dev = Device(tmp_path / "run", runner=runner)
    assert dev.abandon() == []
    with pytest.raises(DeviceError, match="needs a VM prefix"):
        dev.abandon_cleanup("", [])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    dev.abandon_cleanup("wp-t-e2e-", [str(scratch)])
    assert dev.abandon() == ["wp-t-e2e-r1-a"]
    assert ["limactl", "delete", "-f", "wp-t-e2e-r1-a"] in calls
    assert not scratch.exists()
    answers["list"] = {"rc": 1, "stdout": "", "stderr": "broken"}
    assert dev.abandon() == []
    dev.egress_start(60, [], ["wp-t-e2e-"])
    errors = [r.get("error") for r in dev.egress_records() if "error" in r]
    assert errors == ["ps exited with 1", "no successful sampling round during the run"]
    dev.shutdown()
