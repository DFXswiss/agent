"""``GET /v1/metrics`` values on a real kernel (08-monitoring.md section 7 integration, RUN-15).

A session unit with the production name ``agent-session-<id>`` (``sandbox/unit.py``) runs a load: it spins the CPU,
holds about 160 MiB, writes a file and sends UDP packets to a documentation address that the egress filter drops.
The collector of ``agent_guest.metrics`` reads the VM from ``/proc``/``statvfs``, the session from cgroup v2 and the
egress count from its own nftables chain; the values must move under the load and match the contract.

The measured values are printed as one line ``metrics-under-load {...}`` (visible with ``-s``, for the report).
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any, cast

import pytest

from agent_guest import metrics
from agent_guest.clock import SystemClock
from agent_guest.control import ControlContext
from agent_guest.journal import Journal
from agent_guest.sandbox import unit
from agent_guest.sandbox.selftest import ThrowawaySessions
from agent_guest.schema import SchemaSet
from agent_guest.state import Session, State

from .conftest import NFT_TABLE, SRC, Guest

pytestmark = pytest.mark.linux

LOAD_SECONDS = 8
LOAD = textwrap.dedent(
    """
    import os, socket, sys, time
    held = bytearray(160 * 1024 * 1024)
    for index in range(0, len(held), 4096):
        held[index] = 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    end = time.monotonic() + float(sys.argv[1])
    next_io = 0.0
    while time.monotonic() < end:
        if time.monotonic() >= next_io:
            next_io = time.monotonic() + 0.5
            with open("load.bin", "wb") as handle:
                handle.write(os.urandom(1024 * 1024))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                sock.sendto(b"x", ("192.0.2.1", 9))
            except OSError:
                pass
    """
)


def context_for(tmp_path: Path, session: Session) -> ControlContext:
    journal = Journal.open(str(tmp_path / "journal.db"), SystemClock(), SchemaSet(str(SRC.parent / "contracts")))
    state = State(journal)
    state.save_session(session)
    return ControlContext(
        config=cast(Any, None),
        schemas=SchemaSet(str(SRC.parent / "contracts")),
        journal=journal,
        state=state,
        pending=cast(Any, None),
        info=cast(Any, None),
    )


def contract_errors(schemas: SchemaSet, body: dict[str, Any]) -> list[Any]:
    """Contract check; the throw-away session has a UUIDv4 ID where production IDs are v7, so a copy gets one."""
    copied = json.loads(json.dumps(body))
    for item in copied["sessions"]:
        item["session_id"] = "01a1ee56-5e00-7003-8f1b-340691d99f03"
    return list(schemas.errors("guest/metrics.response.json", copied))


def filter_drops() -> int:
    """Packets dropped by the session rule of the test copy of the egress filter (``conftest.NFT_TABLE``)."""
    listed = subprocess.run(
        ["nft", "-j", "list", "table", "inet", NFT_TABLE], capture_output=True, text=True, check=True
    )
    for item in json.loads(listed.stdout)["nftables"]:
        rule = item.get("rule")
        if rule and any("drop" in expr for expr in rule["expr"]):
            return int(next(expr["counter"]["packets"] for expr in rule["expr"] if "counter" in expr))
    raise AssertionError("no drop rule")


def wait_for(path: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.isdir(path):
        assert time.monotonic() < deadline, f"no cgroup {path}"
        time.sleep(0.05)


def test_vm_values_from_the_real_kernel() -> None:
    values = metrics.vm_values(metrics.Sources())
    assert values["cpu_usage_usec"] > 0
    assert 0 < values["mem_used_bytes"] < values["mem_total_bytes"]
    assert 0 < values["disk_used_bytes"] < values["disk_total_bytes"]
    assert values["net_rx_bytes"] > 0
    assert values["uptime_seconds"] > 0
    assert values["load1"] >= 0


def test_session_values_move_under_load(
    tmp_path: Path, spec: unit.SessionSpec, sessions: ThrowawaySessions, guest: Guest
) -> None:
    uid = pwd.getpwnam(sessions.user).pw_uid
    session = Session(
        id=sessions.own,
        uid=uid,
        token_sha256="0" * 64,
        state="working",
        workspace=f"{unit.session_dir(sessions.own)}/work",
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )
    context = context_for(tmp_path, session)
    collector = metrics.Collector()
    schemas = context.schemas
    try:
        before = collector.collect(context)  # adds the egress chain before any packet of the session
        assert contract_errors(schemas, before) == []
        assert before["sessions"][0]["cpu_usage_usec"] == 0  # no unit yet
        assert before["sessions"][0]["egress_denied"] == 0

        drops_before = filter_drops()
        name = unit.UNIT_PREFIX + sessions.own
        argv = unit.systemd_run(spec, [guest.python, "-I", "-c", LOAD, str(LOAD_SECONDS)], unit_name=name, mode="pipe")
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(metrics.unit_cgroup(metrics.CGROUP_ROOT, sessions.own))
            time.sleep(2.0)
            early = collector.collect(context)
            early_at = time.monotonic()
            time.sleep(LOAD_SECONDS / 2)
            late = collector.collect(context)
            elapsed = time.monotonic() - early_at
        finally:
            _, error = process.communicate(timeout=60)
        print("metrics-under-load", json.dumps({"before": before, "early": early, "late": late}))
        assert process.returncode == 0, error
        assert contract_errors(schemas, early) == []
        assert contract_errors(schemas, late) == []
        first, second = early["sessions"][0], late["sessions"][0]
        # One core spinning (with an fsync every 0.5 s): at least a quarter of a core over the measured interval. Half a
        # core was asserted before and failed once at 46 % on a loaded runner device (shared vCPU and disk).
        assert second["cpu_usage_usec"] - first["cpu_usage_usec"] >= elapsed * 0.25 * 1_000_000
        assert second["mem_current_bytes"] >= 150 * 1024 * 1024
        assert second["mem_max_bytes"] == 4 * 1024**3
        assert second["tasks_current"] >= 1
        # IO accounting is turned on by the first collect of the running unit; the load writes 1 MiB every 0.5 s.
        assert second["io_write_bytes"] - first["io_write_bytes"] >= 2 * 1024 * 1024
        assert second["egress_denied"] >= 5
        # Counting changes no decision: the filter still dropped every counted packet.
        assert filter_drops() - drops_before >= second["egress_denied"]
        assert second["oom_kills"] == 0
        assert late["vm"]["cpu_usage_usec"] > early["vm"]["cpu_usage_usec"]
    finally:
        assert collector.egress.sync({}) == {}
        subprocess.run(["nft", "delete", "table", "inet", metrics.NFT_TABLE], check=True)
        context.journal.close()


OOM_PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    hog = "b = []\\nwhile True:\\n    b.append(bytearray(16 * 1024 * 1024))\\n"
    child = subprocess.run([sys.executable, "-I", "-c", hog])
    print(child.returncode, flush=True)
    time.sleep(float(sys.argv[1]))
    """
)


def test_oom_kill_is_counted(spec: unit.SessionSpec, sessions: ThrowawaySessions, guest: Guest) -> None:
    """A process of the unit over the unit's memory limit is killed by the kernel; with ``OOMPolicy=continue`` the
    unit (and its cgroup) lives on and ``memory.events oom_kill`` shows the kill."""
    name = unit.UNIT_PREFIX + sessions.own
    argv = unit.systemd_run(
        spec,
        [guest.python, "-I", "-c", OOM_PARENT, "6"],
        unit_name=name,
        mode="pipe",
        extra_properties=("MemoryMax=96M", "MemorySwapMax=0", "OOMPolicy=continue"),
    )
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        directory = metrics.unit_cgroup(metrics.CGROUP_ROOT, sessions.own)
        wait_for(directory)
        deadline = time.monotonic() + 30
        values = metrics.cgroup_values(directory)
        while values["oom_kills"] == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
            values = metrics.cgroup_values(directory)
        assert values["oom_kills"] == 1
        assert values["mem_max_bytes"] == 96 * 1024 * 1024
    finally:
        output, error = process.communicate(timeout=60)
    assert output.strip() == "-9", error
