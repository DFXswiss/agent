"""Resource values of this VM and its sessions for ``GET /v1/metrics`` (03-api.md section 7.1, 08-monitoring.md
section 1, ``guest/metrics.response.json``).

The control process (root) reads everything itself; nothing here runs inside a session or trusts a session's files:

* VM: CPU time of all CPUs from ``/proc/stat`` (busy jiffies: user, nice, system, irq, softirq, steal; guest time is
  part of user) in microseconds, ``MemTotal - MemAvailable`` of ``/proc/meminfo``, the root filesystem from
  ``statvfs("/")``, the byte counters of every interface except ``lo`` from ``/proc/net/dev``, ``/proc/uptime`` and the
  1-minute load of ``/proc/loadavg``. A missing or malformed source fails the call (``503 unavailable``): the runner
  then reports the VM's probe as failed instead of a made-up value.
* Session: the cgroup v2 directory of its unit ``system.slice/agent-session-<id>.service`` (``sandbox/unit.py``):
  ``cpu.stat usage_usec``, ``memory.current``, ``memory.max`` (``max`` -> null), ``memory.events oom_kill``,
  ``pids.current`` and the summed ``rbytes``/``wbytes`` of ``io.stat``. Without the unit (not started yet, suspended,
  ended) the values are 0 and the limit null; a malformed single file counts as 0.
* IO accounting: the session units set no ``IOAccounting=`` and ``system.slice`` delegates only ``memory pids`` by
  default, so ``io.stat`` would never exist. The first call that finds a session's unit without ``io.stat`` turns it
  on for that unit (``systemctl set-property --runtime <unit> IOAccounting=yes``, once per session); IO before that
  call (at most one sampling interval) is not counted.
* ``workspace_bytes``: the sum of the regular files the workspace scanner of GST-05 walks
  (``WorkspaceScanner._walk``: owned by the session user, no symlinks, no ``.git``, at most ``MAX_ENTRIES`` entries),
  cached for ``WORKSPACE_TTL`` seconds per session because a walk costs more than the runner's 2 s budget allows on
  every call.
* Counters (additive contract fields, contracts/changes/RUN-15.md), counted over the journal records of the session:
  ``hook_denies`` (``session.hook`` with decision ``deny`` or ``block``), ``hook_errors`` (``guest.error``
  ``hook_handler_failed``), ``model_input_tokens`` (input + cache read + cache write), ``model_output_tokens``
  (output + thinking), ``model_request_bytes`` and ``model_response_bytes`` of the ``usage`` records. The journal is
  read incrementally (records after the last one seen); records are pruned only 24 h after the runner acknowledged
  them, so a 15 s sampler never misses one. After a restart of this process the counting starts again from the records
  the journal still holds; when older records were pruned meanwhile the value is lower, a counter reset Prometheus
  handles. The counts of sessions that are no longer active are forgotten.
* ``egress_denied``: packets of the session user that the egress filter of 05-runner.md section 4.3 refuses
  (``EgressCounters``). The production table counts all session users together, so this module keeps its own
  count-only table ``inet agent_metrics``: a base chain at priority -10 (before the filter at 0) jumps for the
  session's user to the session's own chain ``s<session id hex>``, which returns for exactly what the filter accepts
  (platform port on ``lo``, established flows) and counts the rest. The chain belongs to the session, not to the UID:
  session UIDs are reused, and a new session never inherits the count of an earlier one. The table holds no ``drop``
  and no ``accept`` that could matter: an accept in one base chain never overrides a drop in another, so the filter
  stays the only decision. The chain of a session is added at ``sandbox_ready`` (in a background thread, so the
  session start never waits for ``nft``) and on every call, and removed once the session is no longer active.
  Without ``nft`` (or on any failure) the field is left out.

Sessions are the cached active ones (state not ``stopped``/``failed``), at most 64, sorted by ID; the runner keeps the
last values of an ended session itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_guest.control import ControlContext
from agent_guest.routing import HttpError
from agent_guest.sandbox.unit import UNIT_PREFIX
from agent_guest.scanner import WorkspaceScanner
from agent_guest.state import INACTIVE_STATES, Session

PROC_ROOT = "/proc"
CGROUP_ROOT = "/sys/fs/cgroup"
UNIT_SLICE = "system.slice"
ROOT_FS = "/"
MAX_SESSIONS = 64
WORKSPACE_TTL = 60.0
NFT_TABLE = "agent_metrics"
NFT_PRIORITY = -10
NFT_TIMEOUT = 5.0
PLATFORM_PORT = 7700
MAX_COUNT = 2**53 - 1
COUNTED_KINDS = ("session.hook", "usage", "guest.error")
DENY_DECISIONS = frozenset({"deny", "block"})
HOOK_ERROR = "hook_handler_failed"
INPUT_TOKENS = ("input_tokens", "cache_read_tokens", "cache_write_tokens")
OUTPUT_TOKENS = ("output_tokens", "thinking_tokens")
COUNTERS = (
    "hook_denies",
    "hook_errors",
    "model_input_tokens",
    "model_output_tokens",
    "model_request_bytes",
    "model_response_bytes",
)
_CHAIN_RE = re.compile(r"s([0-9a-f]{32})")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class MetricsUnavailable(Exception):
    """A VM source could not be read or parsed."""


# -- small readers ------------------------------------------------------------------------------------------------


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _count(value: int) -> int:
    return max(0, min(value, MAX_COUNT))


def _required(path: str) -> str:
    text = read_text(path)
    if text is None:
        raise MetricsUnavailable(path)
    return text


# -- VM -----------------------------------------------------------------------------------------------------------


def parse_proc_stat(text: str, clock_ticks: int) -> int:
    """Busy CPU time of all CPUs in microseconds from the aggregate ``cpu`` line."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            try:
                user, nice, system, _idle, _iowait, irq, softirq, steal = (int(item) for item in parts[1:9])
            except ValueError:
                break
            busy = user + nice + system + irq + softirq + steal
            return _count(busy * 1_000_000 // clock_ticks)
    raise MetricsUnavailable("stat")


def parse_meminfo(text: str) -> tuple[int, int]:
    """``(used, total)`` bytes: ``MemTotal - MemAvailable`` and ``MemTotal``."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in ("MemTotal", "MemAvailable") and len(parts) == 2 and parts[0].isdigit() and parts[1] == "kB":
            values[name] = int(parts[0]) * 1024
    if len(values) != 2:
        raise MetricsUnavailable("meminfo")
    total = values["MemTotal"]
    return _count(max(total - values["MemAvailable"], 0)), _count(total)


def parse_net_dev(text: str) -> tuple[int, int]:
    """Received and transmitted bytes of every interface except ``lo``."""
    received = transmitted = 0
    seen = False
    for line in text.splitlines()[2:]:
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if not separator or len(fields) < 16 or not all(item.isdigit() for item in fields):
            raise MetricsUnavailable("net/dev")
        seen = True
        if name.strip() == "lo":
            continue
        received += int(fields[0])
        transmitted += int(fields[8])
    if not seen:
        raise MetricsUnavailable("net/dev")
    return _count(received), _count(transmitted)


def parse_uptime(text: str) -> int:
    try:
        return _count(int(float(text.split(maxsplit=1)[0])))
    except (IndexError, ValueError):
        raise MetricsUnavailable("uptime") from None


def parse_loadavg(text: str) -> float:
    try:
        value = float(text.split(maxsplit=1)[0])
    except (IndexError, ValueError):
        raise MetricsUnavailable("loadavg") from None
    if not value >= 0 or value == float("inf"):
        raise MetricsUnavailable("loadavg")
    return value


@dataclass
class Sources:
    """Where the values come from; tests point the roots at fixture trees and replace the tools."""

    proc_root: str = PROC_ROOT
    cgroup_root: str = CGROUP_ROOT
    root_fs: str = ROOT_FS
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs
    clock_ticks: int = field(default_factory=lambda: os.sysconf("SC_CLK_TCK"))
    run: Runner = subprocess.run
    monotonic: Callable[[], float] = time.monotonic


def vm_values(sources: Sources) -> dict[str, Any]:
    proc = sources.proc_root
    used, total = parse_meminfo(_required(f"{proc}/meminfo"))
    received, transmitted = parse_net_dev(_required(f"{proc}/net/dev"))
    try:
        fs = sources.statvfs(sources.root_fs)
    except OSError:
        raise MetricsUnavailable("statvfs") from None
    return {
        "cpu_usage_usec": parse_proc_stat(_required(f"{proc}/stat"), sources.clock_ticks),
        "mem_used_bytes": used,
        "mem_total_bytes": total,
        "disk_used_bytes": _count((fs.f_blocks - fs.f_bfree) * fs.f_frsize),
        "disk_total_bytes": _count(fs.f_blocks * fs.f_frsize),
        "net_rx_bytes": received,
        "net_tx_bytes": transmitted,
        "uptime_seconds": parse_uptime(_required(f"{proc}/uptime")),
        "load1": parse_loadavg(_required(f"{proc}/loadavg")),
    }


# -- session cgroup -----------------------------------------------------------------------------------------------


def unit_cgroup(cgroup_root: str, session_id: str) -> str:
    return f"{cgroup_root}/{UNIT_SLICE}/{UNIT_PREFIX}{session_id}.service"


def io_accounting_argv(session_id: str) -> list[str]:
    return ["systemctl", "set-property", "--runtime", f"{UNIT_PREFIX}{session_id}.service", "IOAccounting=yes"]


def _keyed(text: str | None, key: str) -> int:
    """The value of ``key`` in a flat-keyed cgroup file (``cpu.stat``, ``memory.events``); 0 when absent."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key and parts[1].isdigit():
            return _count(int(parts[1]))
    return 0


def _single(text: str | None) -> int:
    value = (text or "").strip()
    return _count(int(value)) if value.isdigit() else 0


def _limit(text: str | None) -> int | None:
    value = (text or "").strip()
    return _count(int(value)) if value.isdigit() else None


def parse_io_stat(text: str | None) -> tuple[int, int]:
    """Summed ``rbytes`` and ``wbytes`` of all devices of ``io.stat``."""
    read = written = 0
    for line in (text or "").splitlines():
        for item in line.split()[1:]:
            name, _, value = item.partition("=")
            if value.isdigit():
                if name == "rbytes":
                    read += int(value)
                elif name == "wbytes":
                    written += int(value)
    return _count(read), _count(written)


def cgroup_values(directory: str) -> dict[str, Any]:
    io_read, io_write = parse_io_stat(read_text(f"{directory}/io.stat"))
    return {
        "cpu_usage_usec": _keyed(read_text(f"{directory}/cpu.stat"), "usage_usec"),
        "mem_current_bytes": _single(read_text(f"{directory}/memory.current")),
        "mem_max_bytes": _limit(read_text(f"{directory}/memory.max")),
        "tasks_current": _single(read_text(f"{directory}/pids.current")),
        "io_read_bytes": io_read,
        "io_write_bytes": io_write,
        "oom_kills": _keyed(read_text(f"{directory}/memory.events"), "oom_kill"),
    }


# -- journal counters ---------------------------------------------------------------------------------------------


def _number(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    return value if type(value) is int and value >= 0 else 0


class JournalCounters:
    """Per-session counters of the journal records after ``last_seq`` (see the module docstring)."""

    def __init__(self) -> None:
        self.last_seq = 0
        self.counts: dict[str, dict[str, int]] = {}

    def _add(self, session_id: str, name: str, amount: int) -> None:
        counts = self.counts.setdefault(session_id, dict.fromkeys(COUNTERS, 0))
        counts[name] = _count(counts[name] + amount)

    def count(self, session_id: str, kind: str, body: Mapping[str, Any]) -> None:
        if kind == "session.hook":
            self._add(session_id, "hook_denies", 1 if body.get("decision") in DENY_DECISIONS else 0)
        elif kind == "guest.error":
            self._add(session_id, "hook_errors", 1 if body.get("code") == HOOK_ERROR else 0)
        else:
            self._add(session_id, "model_input_tokens", sum(_number(body, key) for key in INPUT_TOKENS))
            self._add(session_id, "model_output_tokens", sum(_number(body, key) for key in OUTPUT_TOKENS))
            self._add(session_id, "model_request_bytes", _number(body, "request_bytes"))
            self._add(session_id, "model_response_bytes", _number(body, "response_bytes"))

    def update(self, context: ControlContext) -> None:
        marks = ", ".join("?" for _ in COUNTED_KINDS)
        with context.journal.transaction() as db:
            rows = db.execute(
                f"SELECT seq, session_id, kind, body FROM journal WHERE seq > ? AND session_id IS NOT NULL"
                f" AND kind IN ({marks}) ORDER BY seq",
                (self.last_seq, *COUNTED_KINDS),
            ).fetchall()
        for seq, session_id, kind, body in rows:
            self.last_seq = seq
            value = json.loads(body)
            self.count(session_id, kind, value if isinstance(value, dict) else {})

    def forget(self, active: set[str]) -> None:
        """Drop the counts of sessions that are no longer active."""
        self.counts = {key: value for key, value in self.counts.items() if key in active}

    def of(self, session_id: str) -> dict[str, int]:
        return dict(self.counts.get(session_id, dict.fromkeys(COUNTERS, 0)))


# -- egress counters ----------------------------------------------------------------------------------------------


def base_script() -> list[str]:
    return [
        f"add table inet {NFT_TABLE}",
        f"add chain inet {NFT_TABLE} out {{ type filter hook output priority {NFT_PRIORITY}; policy accept; }}",
    ]


def chain_name(session_id: str) -> str:
    return "s" + session_id.replace("-", "")


def add_script(session_id: str, uid: int) -> list[str]:
    chain = chain_name(session_id)
    return [
        f"add chain inet {NFT_TABLE} {chain}",
        f"add rule inet {NFT_TABLE} {chain} oif lo tcp dport {PLATFORM_PORT} return",
        f"add rule inet {NFT_TABLE} {chain} ct state established,related return",
        f"add rule inet {NFT_TABLE} {chain} counter",
        f"add rule inet {NFT_TABLE} out meta skuid {uid} jump {chain}",
    ]


def remove_script(chain: str, handles: Iterable[int]) -> list[str]:
    lines = [f"delete rule inet {NFT_TABLE} out handle {handle}" for handle in handles]
    return [*lines, f"flush chain inet {NFT_TABLE} {chain}", f"delete chain inet {NFT_TABLE} {chain}"]


@dataclass
class Ruleset:
    """What ``nft -j -a list table`` shows of the count-only table."""

    exists: bool = False
    chains: set[str] = field(default_factory=set)
    jumps: dict[str, list[int]] = field(default_factory=dict)
    packets: dict[str, int] = field(default_factory=dict)


def _jump_target(expressions: list[Any]) -> str | None:
    for item in expressions:
        jump = item.get("jump") if isinstance(item, dict) else None
        target = jump.get("target") if isinstance(jump, dict) else None
        match = _CHAIN_RE.fullmatch(target) if isinstance(target, str) else None
        if match is not None:
            return match.group(0)
    return None


def _counter(expressions: list[Any]) -> int | None:
    for item in expressions:
        counter = item.get("counter") if isinstance(item, dict) else None
        if isinstance(counter, dict) and type(counter.get("packets")) is int:
            return _count(counter["packets"])
    return None


def parse_ruleset(text: str) -> Ruleset:
    """The table from ``nft -j`` output (raises ``ValueError`` for anything else than nft JSON)."""
    document = json.loads(text)
    items = document["nftables"] if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ValueError("not nft json")
    found = Ruleset(exists=True)
    for item in items:
        chain = item.get("chain") if isinstance(item, dict) else None
        rule = item.get("rule") if isinstance(item, dict) else None
        if isinstance(chain, dict) and isinstance(chain.get("name"), str):
            if _CHAIN_RE.fullmatch(chain["name"]):
                found.chains.add(chain["name"])
        elif isinstance(rule, dict) and isinstance(rule.get("expr"), list):
            expressions, name = rule["expr"], rule.get("chain")
            if name == "out" and type(rule.get("handle")) is int:
                target = _jump_target(expressions)
                if target is not None:
                    found.jumps.setdefault(target, []).append(rule["handle"])
            packets = _counter(expressions)
            if isinstance(name, str) and _CHAIN_RE.fullmatch(name) and packets is not None:
                found.packets[name] = packets
    return found


class EgressCounters:
    """Count-only nftables chains per session user (see the module docstring)."""

    def __init__(self, run: Runner) -> None:
        self.run = run
        self.lock = threading.Lock()

    def _nft(self, argv: list[str], script: str | None = None) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.run(
                ["nft", *argv], input=script, capture_output=True, text=True, timeout=NFT_TIMEOUT, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _list(self) -> Ruleset | None:
        result = self._nft(["-j", "-a", "list", "table", "inet", NFT_TABLE])
        if result is None:
            return None
        if result.returncode != 0:
            # A missing table is the only expected refusal; everything else means nft cannot be used.
            return Ruleset() if "No such file or directory" in result.stderr else None
        try:
            return parse_ruleset(result.stdout)
        except (ValueError, KeyError, AttributeError):
            return None

    def sync(self, users: Mapping[str, int]) -> dict[str, int] | None:
        """Chains for exactly the sessions of ``users`` (session ID -> UID); returns the packet counts per session read
        before the change (None: no nft)."""
        wanted = {chain_name(session_id): (session_id, uid) for session_id, uid in users.items()}
        with self.lock:
            current = self._list()
            if current is None:
                return None
            script: list[str] = [] if current.exists else base_script()
            for chain in sorted(set(wanted) - current.chains):
                script += add_script(*wanted[chain])
            for chain in sorted(current.chains - set(wanted)):
                script += remove_script(chain, current.jumps.get(chain, []))
            if script:
                applied = self._nft(["-f", "-"], "\n".join(script) + "\n")
                if applied is None or applied.returncode != 0:
                    return None
            return {session_id: current.packets.get(chain, 0) for chain, (session_id, _) in wanted.items()}


# -- collector ----------------------------------------------------------------------------------------------------


def active_sessions(context: ControlContext) -> list[Session]:
    found = [session for session in context.state.sessions() if session.state not in INACTIVE_STATES]
    return sorted(found, key=lambda session: session.id)[:MAX_SESSIONS]


class Collector:
    """The metrics state of one control process: journal position, workspace cache, egress chains."""

    def __init__(self, sources: Sources | None = None) -> None:
        self.sources = sources or Sources()
        self.journal = JournalCounters()
        self.egress = EgressCounters(self.sources.run)
        self.workspace: dict[str, tuple[float, int]] = {}
        self.io_enabled: set[str] = set()
        self.egress_thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def enable_io(self, session_id: str, directory: str) -> None:
        """Turn IO accounting on for a running unit without ``io.stat`` (once per session; failures are ignored)."""
        if session_id in self.io_enabled or not os.path.isdir(directory) or os.path.exists(f"{directory}/io.stat"):
            return
        self.io_enabled.add(session_id)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self.sources.run(
                io_accounting_argv(session_id), capture_output=True, text=True, timeout=NFT_TIMEOUT, check=False
            )

    def workspace_bytes(self, context: ControlContext, session: Session) -> int:
        now = self.sources.monotonic()
        cached = self.workspace.get(session.id)
        if cached is not None and now - cached[0] < WORKSPACE_TTL:
            return cached[1]
        total = [0]

        def visit(_fd: int, _rel: str, status: os.stat_result) -> None:
            total[0] += status.st_size

        if session.uid >= 0:
            # The session base holds `work`; the same directory the capture's scanner uses (Capture.base).
            base = session.workspace.rpartition("/")[0]
            WorkspaceScanner(context.journal, session.id, base, session.uid)._walk(visit)
        value = _count(total[0])
        self.workspace[session.id] = (now, value)
        return value

    def sync_egress(self, sessions: list[Session]) -> dict[str, int] | None:
        return self.egress.sync({session.id: session.uid for session in sessions if session.uid >= 0})

    def collect(self, context: ControlContext) -> dict[str, Any]:
        with self.lock:
            try:
                vm = vm_values(self.sources)
            except MetricsUnavailable:
                raise HttpError("unavailable") from None
            sessions = active_sessions(context)
            self.journal.update(context)
            egress = self.sync_egress(sessions)
            known = {session.id for session in sessions}
            self.workspace = {key: value for key, value in self.workspace.items() if key in known}
            self.io_enabled &= known
            self.journal.forget(known)
            listed: list[dict[str, Any]] = []
            for session in sessions:
                directory = unit_cgroup(self.sources.cgroup_root, session.id)
                self.enable_io(session.id, directory)
                values: dict[str, Any] = {"session_id": session.id}
                values.update(cgroup_values(directory))
                values["workspace_bytes"] = self.workspace_bytes(context, session)
                values["state"] = session.state
                values.update(self.journal.of(session.id))
                if egress is not None and session.id in egress:
                    values["egress_denied"] = egress[session.id]
                listed.append(values)
            return {"vm": vm, "sessions": listed}


_COLLECTOR = "_metrics_collector"
_collector_lock = threading.Lock()


def install(context: ControlContext, created: Collector) -> Collector:
    vars(context)[_COLLECTOR] = created
    return created


def collector(context: ControlContext) -> Collector:
    """The collector of this control process (created on first use)."""
    with _collector_lock:
        existing = vars(context).get(_COLLECTOR)
        if isinstance(existing, Collector):
            return existing
        return install(context, Collector())
