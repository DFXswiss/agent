"""Nodes: the local wrapper, the JSON-lines protocol of the remote node, SSH command line."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from orchestrator import node as node_module
from orchestrator.device import Device
from orchestrator.node import LocalNode, Node, NodeError, RemoteNode, serve, ssh_argv
from support import SUITE


def test_base_node_is_abstract() -> None:
    with pytest.raises(NotImplementedError):
        Node().call("alive", name="x")


def test_local_node_wraps_device_errors(tmp_path: Path) -> None:
    local = LocalNode("dev", Device(tmp_path))
    assert local.busy_ports([]) == []
    assert local.relays() == {}
    assert local.processes() == {}
    with pytest.raises(NodeError, match="dev: unknown process x"):
        local.alive("x")
    local.shutdown()


def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteNode:
    monkeypatch.setenv("PYTHONPATH", str(SUITE))
    argv = [sys.executable, "-m", "orchestrator.node", "--run-dir", str(tmp_path / "remote")]
    return RemoteNode("runner-device", argv)


def test_remote_node_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = remote(tmp_path, monkeypatch)
    env = dict(os.environ)
    assert (
        node.spawn("sleeper", [sys.executable, "-c", "print('hi', flush=True); import time; time.sleep(30)"], env) > 0
    )
    assert node.wait_line("sleeper", "hi", 10) == "hi"
    assert node.alive("sleeper")
    assert node.log_tail("sleeper") == "hi"
    assert node.processes()["sleeper"]["alive"]
    assert node.run(["echo", "x"])["stdout"] == "x\n"
    assert node.busy_ports([]) == []
    relay = node.relay_start("model", ("127.0.0.1", 0), ("127.0.0.1", 9))
    assert relay["cut"] is False
    assert node.relay_cut("model")["cut"]
    assert not node.relay_restore("model")["cut"]
    assert list(node.relays()) == ["model"]
    node.relay_start("tapped", ("127.0.0.1", 0), ("127.0.0.1", 9), tap=str(tmp_path / "tap"))
    assert (tmp_path / "tap").is_dir()
    node.egress_start(5.0, [])
    assert isinstance(node.egress_records(), list)
    (tmp_path / "f.txt").write_text("content")
    assert node.read_text(str(tmp_path / "f.txt")) == "content"
    assert node.archive()
    assert node.stop("sleeper") == -15
    node.spawn("quick", [sys.executable, "-c", "pass"], env)
    assert node.wait_exit("quick", 10) == 0
    with pytest.raises(NodeError, match="runner-device: unknown process nope"):
        node.alive("nope")
    with pytest.raises(NodeError, match=r"runner-device: GET http://127\.0\.0\.1:1/: "):
        node.http("GET", "http://127.0.0.1:1/", timeout=1)
    assert node.shutdown() == {"quick": 0, "sleeper": -15}
    assert node.close() == 0
    assert node.close() == 0
    with pytest.raises(NodeError, match="connection to the device ended"):
        node.alive("x")


def test_remote_node_close_kills_a_hanging_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_module, "CLOSE_TIMEOUT_S", 0.1)
    hanging = RemoteNode(
        "dev",
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(30)"],
    )
    assert hanging.close() == -9


def test_serve_answers_errors_and_shuts_down(tmp_path: Path) -> None:
    requests = "\n".join(
        [
            json.dumps({"method": "processes"}),
            json.dumps({"method": "format_disk"}),
            "not json",
            json.dumps({"args": {}}),
            json.dumps({"method": "alive", "args": {"bogus": 1}}),
        ]
    )
    out = io.StringIO()
    assert serve(Device(tmp_path), io.StringIO(requests + "\n"), out) == 0
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0] == {"ok": True, "result": {}}
    assert answers[1] == {"ok": False, "error": "unknown method 'format_disk'"}
    assert [a["ok"] for a in answers[2:]] == [False, False, False]
    assert answers[3]["error"] == "'method'"


def test_main_and_ssh_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"method": "relays"}) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert node_module.main(["--run-dir", str(tmp_path / "d")]) == 0
    assert json.loads(out.getvalue()) == {"ok": True, "result": {}}
    argv = ssh_argv("u@192.0.2.1", "exec python -m orchestrator.node", "/k/known_hosts", "/opt/x/bin")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "UserKnownHostsFile=/k/known_hosts" in argv
    assert argv[-2:] == ["u@192.0.2.1", "export PATH=/opt/x/bin:$PATH; exec python -m orchestrator.node"]
