"""Egress log and check (07-testing.md sections 2.1 and 5.2: "no outbound connection except to the doubles").

Two sources feed the log of a device (`egress.jsonl`, one JSON record per line):
- the relays: every connection to the hub or a double, by link name (allowed by construction);
- the sampler: the TCP/UDP connections of every process the run started - long-running ones, one-shot commands while
  they run, their descendants, and processes whose command line names one of the run's patterns (the Lima host agents
  of the run's VMs detach from their parent) - read with `ps` and `lsof` every `interval` seconds. The sampler fails
  closed: a failing `ps`/`lsof` or an error in its thread is written as a violation. A connection to a loopback
  address stays on the device; any other
  remote address is egress and allowed only when it is the peer device of the topology (T2: the hub device for the
  runner device and vice versa; inbound connections show the peer as remote address, too). The one exception: a Lima
  host agent's connections to port 123 (time sync) or to port 53 of one of the device's resolvers (`/etc/resolv.conf`,
  name lookups) belong to the guest operating system (`vm_os`, ADR-034); they are logged and allowed. Any other
  connection of a VM (e.g. HTTPS, or DNS to another server) stays a violation. Second exception: the host agent of a
  work VM (instance name `…wvm-<8 hex>`, agent ADR-056): a work VM is the session's own Ubuntu machine with internet
  by design (its DNS goes to Cloudflare's public resolvers, its operating system updates itself); its connections are
  logged with `workvm: true` and allowed. The scenarios themselves use only servers inside the VM.
Processes are started with `HTTP(S)_PROXY` on the closed loopback port 9, so a client that honours the proxy
variables cannot connect anywhere unexpected at all; the sampler catches clients that ignore them.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

Record = dict[str, Any]
Command = Callable[[list[str]], tuple[int, str]]
PS = ["ps", "-A", "-o", "pid=,ppid=,command="]
LSOF_NOTHING_FOUND = 1
# NTP and DNS of the guest operating system (systemd-timesyncd, resolver) leave through the Lima host agent; they
# carry no platform content and production VMs need them. DNS only to the device's own resolvers; everything else a
# VM sends out is a violation.
NTP_PORT = 123
DNS_PORT = 53
RESOLV_CONF = Path("/etc/resolv.conf")
WORKVM_AGENT = re.compile(r"limactl.* hostagent .*wvm-[0-9a-f]{8}")


def _rows(ps_output: str) -> list[tuple[int, int, str]]:
    rows = []
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            rows.append((int(fields[0]), int(fields[1]), fields[2] if len(fields) == 3 else ""))
    return rows


def matching(ps_output: str, patterns: Iterable[str]) -> set[int]:
    """PIDs whose command line contains one of `patterns`."""
    wanted = [p for p in patterns if p]
    return {pid for pid, _, command in _rows(ps_output) if any(p in command for p in wanted)}


def workvm_agents(ps_output: str) -> set[int]:
    """PIDs of the Lima host agents of work VMs (their command line names the instance `…wvm-<8 hex>`)."""
    return {pid for pid, _, command in _rows(ps_output) if WORKVM_AGENT.search(command)}


def descendants(ps_output: str, roots: Iterable[int]) -> set[int]:
    """PIDs of `roots` and all their descendants from `ps -A -o pid=,ppid=,command=` output."""
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in _rows(ps_output):
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    todo = list(roots)
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo.extend(children.get(pid, []))
    return found


def _split_endpoint(text: str) -> tuple[str, int | None]:
    host, _, port = text.rpartition(":")
    host = host.strip("[]")
    return host, int(port) if port.isdigit() else None


def connections(lsof_output: str) -> list[Record]:
    """Connections (`pid`, `command`, `remote`, `host`, `port`) from `lsof -F pcn` output; listeners are skipped."""
    out: list[Record] = []
    pid = 0
    command = ""
    for line in lsof_output.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value)
        elif tag == "c":
            command = value
        elif tag == "n" and "->" in value:
            remote = value.split("->", 1)[1].split(" ", 1)[0]
            host, port = _split_endpoint(remote)
            out.append({"pid": pid, "command": command, "remote": remote, "host": host, "port": port})
    return out


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def resolvers(text: str) -> set[str]:
    """The name servers of `/etc/resolv.conf` content (the device's resolvers)."""
    found = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            found.add(fields[1])
    return found


def read_resolvers(path: Path = RESOLV_CONF) -> set[str]:
    try:
        return resolvers(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return set()


def vm_os(conn: Record, dns: AbstractSet[str]) -> bool:
    """Time sync (NTP, any server) and name lookups at one of the device's own resolvers of a VM's operating system,
    carried by its Lima host agent (ADR-034)."""
    if conn["command"] != "limactl":
        return False
    return bool(conn["port"] == NTP_PORT or (conn["port"] == DNS_PORT and conn["host"] in dns))


def classify(conn: Record, peers: set[str], dns: AbstractSet[str] = frozenset()) -> bool:
    """True when the connection is allowed: loopback, the peer device of the topology, or VM OS time/DNS."""
    return is_loopback(conn["host"]) or conn["host"] in peers or vm_os(conn, dns)


class EgressLog:
    """Thread-safe JSON-lines log of one device plus the sampler of its started processes."""

    def __init__(self, path: Path, run: Command, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.run = run
        self.clock = clock
        self.peers: set[str] = set()
        self.dns: set[str] = set()
        self.match: list[str] = []
        self.samples = 0
        self._lock = threading.Lock()
        self._seen: set[tuple[int, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Record) -> None:
        line = json.dumps(record, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def error(self, text: str) -> None:
        self.write({"ts": self.clock(), "source": "sampler", "allowed": False, "error": text})

    def sample(self, roots: Iterable[int]) -> None:
        """One sampling round; every new connection is written once, a failing tool as a violation."""
        roots = list(roots)
        if not roots and not self.match:
            return
        rc, listing = self.run(PS)
        if rc != 0:
            self.error(f"ps exited with {rc}")
            return
        pids = descendants(listing, roots) | matching(listing, self.match)
        workvm = workvm_agents(listing)
        if not pids:
            return
        rc, listing = self.run(["lsof", "-nP", "-a", "-i", "-p", ",".join(str(p) for p in sorted(pids)), "-F", "pcn"])
        if rc not in (0, LSOF_NOTHING_FOUND):
            self.error(f"lsof exited with {rc}")
            return
        self.samples += 1
        for conn in connections(listing):
            key = (conn["pid"], conn["remote"])
            if key in self._seen:
                continue
            self._seen.add(key)
            if conn["pid"] in workvm and not classify(conn, self.peers, self.dns):
                self.write({"ts": self.clock(), "source": "sampler", "allowed": True, "workvm": True, **conn})
                continue
            allowed = classify(conn, self.peers, self.dns)
            self.write({"ts": self.clock(), "source": "sampler", "allowed": allowed, **conn})

    def start(self, roots: Callable[[], list[int]], interval: float) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.sample(roots())
                except Exception as exc:
                    self.error(f"sampler failed: {type(exc).__name__}")

        self._thread = threading.Thread(target=loop, name="egress-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def records(self) -> list[Record]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]


def violations(records: Iterable[Record]) -> list[Record]:
    """Sampler records of connections that left the device to a non-peer address, and sampler failures."""
    return [record for record in records if record.get("source") == "sampler" and not record.get("allowed")]
