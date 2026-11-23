"""Egress report of the role topology E2E-32 (07-testing.md sections 2.1 and 5.2; R-3.13, R-P.7, R-P.8).

T2 puts the hub, its PostgreSQL, the browser and the orchestrator on the second device (the hub device of the test) and
the runner, its VMs and the doubles on the runner device. The report reads the egress logs of both devices (the
relays' connection records and the sampler's connections of every process the run started, `egress.py`) and answers
whether the roles worked over the LAN only:

- the hub listens on a private LAN address of the hub device (no public address, no carrier-grade NAT or tunnel range)
  and is reached under a loopback origin (no public name);
- the runner device reaches the hub only through its relay `runner-hub`, whose target is exactly that LAN address and
  the hub port, and that link carried connections;
- every other connection of a runner-device process stays on the device (loopback: its relays to the hub and the
  doubles, its own store, the VMs' forwarded ports) or is the guest operating system's time and name service through a
  Lima host agent (ADR-034); a connection to the hub device on any other port, or anywhere else, is a problem;
- the hub device's processes reach only the loopback, the device's own LAN address (the browser relay connects to the
  hub there) and the runner device;
- no relay of either device targets anything but the loopback or the hub;
- no process the run started is a tunnel program (`TUNNELS`), and SSH runs only towards the run's Lima instances
  (the reverse tunnels of the doubles into the VMs, `tunnels.py`);
- the samplers worked (no sampler error).

`build(...)` returns `{topology, hub, devices: {name: {links, destinations, tunnels}}, problems}`; no problem means the
run's traffic stayed in the LAN and on the devices.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from orchestrator.egress import descendants, is_loopback

HUB_LINK = "runner-hub"
TUNNELS = re.compile(
    r"(^|/)(cloudflared|ngrok|localtunnel|lt|bore|frpc|zrok|tailscaled?|openvpn|wireguard-go|wg-quick)(\s|$)"
)
SSH = re.compile(r"(^|/)ssh(\s|$)")
LIMA_HOST = re.compile(r"^lima-[A-Za-z0-9._-]+$")
# Built from octets (no address literal in the repository).
CGNAT = ipaddress.IPv4Network((ipaddress.IPv4Address(bytes((100, 64, 0, 0))), 10))  # RFC 6598, for the tests
LAN_NETS = tuple(
    ipaddress.IPv4Network((ipaddress.IPv4Address(bytes((first, second, 0, 0))), prefix))
    for first, second, prefix in ((10, 0, 8), (172, 16, 12), (192, 168, 16))
)

Record = dict[str, Any]


@dataclass(frozen=True)
class Roles:
    """What the run configured: topology, the hub's address and port, the origin, the runner device's address, the
    named ports of the run (for readable loopback destinations)."""

    topology: str
    hub_address: str
    hub_port: int
    origin: str
    runner_address: str
    ports: Mapping[int, str] = field(default_factory=dict)


def lan_address(text: str) -> bool:
    """An IPv4 address of a private local network (RFC 1918). Loopback, public addresses, the shared range of carriers
    and overlay tunnels (RFC 6598) and IPv6 (whose unique-local range overlay tunnels use as well) are no LAN here."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.version == 4 and any(address in net for net in LAN_NETS)


def _class(record: Record, roles: Roles, device: str) -> str:
    host, port = str(record.get("host", "")), record.get("port")
    if is_loopback(host):
        return "loopback"
    own = roles.hub_address if device == "hub-device" else roles.runner_address
    if host == own:
        return "own-address"  # a connection between two sockets of the device itself (e.g. its relay to the hub)
    if device == "runner-device" and host == roles.hub_address:
        return "hub" if port == roles.hub_port else "hub-device-other-port"
    if device == "hub-device" and host == roles.runner_address:
        return "runner-device"
    if record.get("allowed") and record.get("command") == "limactl":
        return "vm-os"
    return "outside"


ALLOWED = {
    "runner-device": {"loopback", "own-address", "hub", "vm-os"},
    "hub-device": {"loopback", "own-address", "runner-device"},
}


def _destinations(records: Iterable[Record], roles: Roles, device: str, problems: list[str]) -> list[Record]:
    """Distinct destinations per process name; loopback ones grouped by the run's port names (`other` for the rest:
    a store, a VM's forwarded port, the far end of an accepted connection)."""
    seen: dict[tuple[str, str, str, str], Record] = {}
    for record in records:
        if record.get("source") != "sampler":
            continue
        if "error" in record:
            problems.append(f"{device}: egress sampler failed: {record['error']}")
            continue
        kind = _class(record, roles, device)
        port = record.get("port")
        command = str(record.get("command"))
        if kind == "loopback":
            name = roles.ports.get(port, "other") if isinstance(port, int) else "other"
            key = (command, kind, name, "")
            entry = seen.setdefault(key, {"command": command, "class": kind, "port": name, "count": 0})
        else:
            key = (command, kind, str(record.get("host")), str(port))
            entry = seen.setdefault(
                key, {"command": command, "class": kind, "host": record.get("host"), "port": port, "count": 0}
            )
            if kind not in ALLOWED[device]:
                problems.append(f"{device}: {command} connected to {record.get('remote')} ({kind})")
        entry["count"] += 1
    return [seen[key] for key in sorted(seen)]


def _links(records: Iterable[Record], roles: Roles, device: str, problems: list[str]) -> dict[str, Record]:
    links: dict[str, Record] = {}
    hub = f"{roles.hub_address}:{roles.hub_port}"
    for record in records:
        if "link" not in record:
            continue
        link = links.setdefault(record["link"], {"target": record["target"], "connections": 0, "refused": 0})
        if record.get("event") == "open":
            link["connections"] += 1
        elif record.get("event") == "refused":
            link["refused"] += 1
    for name, link in sorted(links.items()):
        host = str(link["target"]).rpartition(":")[0]
        if not is_loopback(host) and link["target"] != hub:
            problems.append(f"{device}: relay {name} targets {link['target']} (neither loopback nor the hub)")
    return links


def commands(ps_output: str, roots: Iterable[int]) -> list[str]:
    """Command lines of `roots` and their descendants from `ps -A -o pid=,ppid=,command=` output."""
    tree = descendants(ps_output, roots)
    out = []
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and int(fields[0]) in tree:
            out.append(fields[2])
    return out


def tunnel_processes(commands: Iterable[str]) -> list[str]:
    """Commands of the run's processes that open a tunnel: a tunnel program, or SSH to anything but a Lima instance."""
    found = []
    for command in commands:
        argv = command.split()
        if TUNNELS.search(command) or (argv and SSH.search(argv[0]) and not LIMA_HOST.match(argv[-1])):
            found.append(command)
    return found


def build(
    roles: Roles, records: Mapping[str, list[Record]], processes: Mapping[str, list[str]] | None = None
) -> dict[str, Any]:
    """The report for the devices' egress records (`device name → records`) and the command lines of the processes
    the run started on each device."""
    problems: list[str] = []
    if roles.topology != "t2":
        problems.append(f"the role topology needs T2 (hub on the second device), not {roles.topology}")
    if not lan_address(roles.hub_address):
        problems.append(f"the hub listens on {roles.hub_address}, not on a private LAN address")
    origin_host = urlsplit(roles.origin).hostname or ""
    if not is_loopback(origin_host):
        problems.append(f"the hub origin {roles.origin} is not a loopback name (public or LAN name)")
    devices: dict[str, Any] = {}
    for device, rows in records.items():
        tunnels = tunnel_processes((processes or {}).get(device, []))
        problems += [f"{device}: tunnel process {command}" for command in tunnels]
        devices[device] = {
            "links": _links(rows, roles, device, problems),
            "destinations": _destinations(rows, roles, device, problems),
            "tunnels": tunnels,
        }
    hub_link = devices.get("runner-device", {}).get("links", {}).get(HUB_LINK)
    if hub_link is None or not hub_link["connections"]:
        problems.append("the runner device never reached the hub through its relay runner-hub")
    hub = {
        "origin": roles.origin,
        "address": roles.hub_address,
        "port": roles.hub_port,
        "lan": lan_address(roles.hub_address),
    }
    return {"topology": roles.topology, "hub": hub, "devices": devices, "problems": problems}
