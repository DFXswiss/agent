"""orchestrator/egress_report.py and `GET /egress/report` (E2E-32): the role topology stays in the LAN and on the
devices; every way out is a problem."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path
from typing import Any

from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.egress_report import CGNAT, Roles, build, commands, lan_address, tunnel_processes
from orchestrator.node import Node
from support import CORE

HUB = str(ipaddress.IPv4Address(bytes((192, 168, 7, 20))))  # a private LAN (RFC 1918), built from octets
# An address of the shared range of carriers and tunnels.
CARRIER = str(CGNAT.network_address + 0x10203)
RUNNER = str(ipaddress.IPv4Address(bytes((192, 168, 7, 90))))
ROLES = Roles("t2", HUB, 21006, "http://localhost:21007", RUNNER, {21010: "model-relay", 21007: "public"})


def sample(command: str, host: str, port: int | None, allowed: bool = True) -> dict[str, Any]:
    remote = f"{host}:{port}"
    return {"source": "sampler", "command": command, "remote": remote, "host": host, "port": port, "allowed": allowed}


def link(name: str, target: str, event: str = "open") -> dict[str, Any]:
    return {"link": name, "event": event, "client": "127.0.0.1:50000", "target": target}


def good_records() -> dict[str, list[dict[str, Any]]]:
    return {
        "hub-device": [
            link("browser-hub", f"{HUB}:21006"),
            sample("python3.12", "127.0.0.1", 21005),
            sample("python3.12", RUNNER, 53211),  # the runner's connection, accepted by the hub
            sample("python3.12", HUB, 53300),  # the browser relay's connection from the device's own address
        ],
        "runner-device": [
            link("runner-hub", f"{HUB}:21006"),
            link("runner-hub", f"{HUB}:21006"),
            link("model", "127.0.0.1:21009"),
            link("runner-hub", f"{HUB}:21006", "refused"),
            {"link": "runner-hub", "event": "close", "client": "x", "target": f"{HUB}:21006"},
            sample("python3.12", "127.0.0.1", 21007),
            sample("python3.12", "127.0.0.1", 21007),
            sample("python3.12", "127.0.0.1", 60001),
            sample("python3.12", "127.0.0.1", None),
            sample("limactl", "192.0.2.1", 53),
            sample("python3.12", HUB, 21006),
        ],
    }


def test_lan_addresses() -> None:
    assert lan_address(HUB)
    for bad in (
        "127.0.0.1",
        "0.0.0.0",
        "1.1.1.1",
        "192.0.2.20",
        CARRIER,
        "::1",
        "2001:db8::1",
        "localhost",
        "x.example",
    ):
        assert not lan_address(bad), bad


def test_a_clean_role_topology_has_no_problem() -> None:
    processes = {"runner-device": ["/usr/bin/ssh -F x -N -R 127.0.0.1:1:127.0.0.1:1 lima-wp-x-e2e-arp-1"]}
    report = build(ROLES, good_records(), processes)
    assert report["problems"] == []
    assert report["hub"] == {"origin": "http://localhost:21007", "address": HUB, "port": 21006, "lan": True}
    runner = report["devices"]["runner-device"]
    assert runner["links"]["runner-hub"] == {"target": f"{HUB}:21006", "connections": 2, "refused": 1}
    assert runner["tunnels"] == []
    assert runner["destinations"] == [
        {"command": "limactl", "class": "vm-os", "host": "192.0.2.1", "port": 53, "count": 1},
        {"command": "python3.12", "class": "hub", "host": HUB, "port": 21006, "count": 1},
        {"command": "python3.12", "class": "loopback", "port": "other", "count": 2},
        {"command": "python3.12", "class": "loopback", "port": "public", "count": 2},
    ]
    hub = report["devices"]["hub-device"]["destinations"]
    assert [item["class"] for item in hub] == ["loopback", "own-address", "runner-device"]


def test_every_way_out_is_a_problem() -> None:
    records = good_records()
    records["runner-device"] += [
        sample("python3.12", HUB, 22),
        sample("python3.12", "1.1.1.1", 443, allowed=False),
        sample("limactl", "203.0.113.7", 443, allowed=False),
        {"source": "sampler", "allowed": False, "error": "lsof exited with 2"},
        link("github", "1.1.1.1:443"),
    ]
    records["hub-device"].append(sample("node", "198.51.100.4", 443, allowed=False))
    processes = {"hub-device": ["/opt/homebrew/bin/cloudflared tunnel run"], "runner-device": ["ssh -R 9:x:9 me@host"]}
    roles = Roles("t1", CARRIER, 21006, "https://agent.example.com", RUNNER)
    problems = build(roles, records, processes)["problems"]
    assert problems == [
        "the role topology needs T2 (hub on the second device), not t1",
        f"the hub listens on {CARRIER}, not on a private LAN address",
        "the hub origin https://agent.example.com is not a loopback name (public or LAN name)",
        "hub-device: tunnel process /opt/homebrew/bin/cloudflared tunnel run",
        f"hub-device: relay browser-hub targets {HUB}:21006 (neither loopback nor the hub)",
        f"hub-device: python3.12 connected to {HUB}:53300 (outside)",  # not its own address under these roles
        "hub-device: node connected to 198.51.100.4:443 (outside)",
        "runner-device: tunnel process ssh -R 9:x:9 me@host",
        "runner-device: relay github targets 1.1.1.1:443 (neither loopback nor the hub)",
        f"runner-device: relay runner-hub targets {HUB}:21006 (neither loopback nor the hub)",
        f"runner-device: python3.12 connected to {HUB}:21006 (outside)",
        f"runner-device: python3.12 connected to {HUB}:22 (outside)",
        "runner-device: python3.12 connected to 1.1.1.1:443 (outside)",
        "runner-device: limactl connected to 203.0.113.7:443 (outside)",
        "runner-device: egress sampler failed: lsof exited with 2",
    ]
    # with the right roles only the real ways out stay
    problems = build(ROLES, records, processes)["problems"]
    assert f"runner-device: python3.12 connected to {HUB}:22 (hub-device-other-port)" in problems
    assert len(problems) == 8


def test_the_runner_must_reach_the_hub() -> None:
    records = good_records()
    records["runner-device"] = [r for r in records["runner-device"] if r.get("link") != "runner-hub"]
    assert build(ROLES, records)["problems"] == ["the runner device never reached the hub through its relay runner-hub"]
    assert build(ROLES, {"hub-device": []})["problems"] == [
        "the runner device never reached the hub through its relay runner-hub"
    ]


def test_commands_and_tunnels() -> None:
    ps = "  1     0 /sbin/launchd\n 10     1 python -m agent_runner run\n 11    10 /usr/bin/ssh -N lima-wp-a\n bad\n"
    ps += " 12    11 cloudflared tunnel\n 20     1 other\n"
    assert commands(ps, [10]) == ["python -m agent_runner run", "/usr/bin/ssh -N lima-wp-a", "cloudflared tunnel"]
    assert tunnel_processes(["ngrok http 80", "/usr/bin/ssh -N lima-wp-a", "ssh", "tailscaled", "sshd: x", ""]) == [
        "ngrok http 80",
        "ssh",
        "tailscaled",
    ]


class ReportNode(Node):
    def __init__(self, name: str, records: list[dict[str, Any]], ps: str, rc: int = 0) -> None:
        self.name = name
        self.records = records
        self.ps = ps
        self.rc = rc

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "egress_records":
            return self.records
        if op == "processes":
            return {"hub": {"pid": 10, "alive": True}, "old": {"pid": 11, "alive": False}}
        assert op == "run"
        assert args["argv"][0] == "ps"
        return {"rc": self.rc, "stdout": self.ps, "stderr": ""}


def test_route(tmp_path: Path) -> None:
    config = RunConfig(
        topology="t2", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="w-"
    )
    records = good_records()
    hub = ReportNode("hub-device", records["hub-device"], " 10 1 python -m agent_hub serve\n 11 1 cloudflared\n")
    runner = ReportNode("runner-device", records["runner-device"], " 10 1 python -m agent_runner run\n")
    ctx = Context(config, hub, runner, sys.executable, bind=HUB, peer=RUNNER)
    status, report = dispatch(discover(), ctx, "GET", "/egress/report", None)
    assert status == 200
    assert report["problems"] == []
    assert report["hub"]["origin"] == "http://localhost:21007"
    public = [d for d in report["devices"]["runner-device"]["destinations"] if d["port"] == "public"]
    assert public == [{"command": "python3.12", "class": "loopback", "port": "public", "count": 2}]
    runner.rc = 1
    status, answer = dispatch(discover(), ctx, "GET", "/egress/report", None)
    assert (status, answer["error"]) == (500, "failed")
    # one device (T1): one entry, a topology problem
    single = Context(RunConfig(**{**config.__dict__, "topology": "t1"}), hub, hub, sys.executable)
    report = dispatch(discover(), single, "GET", "/egress/report", None)[1]
    assert list(report["devices"]) == ["hub-device"]
    assert report["problems"][0].startswith("the role topology needs T2")
