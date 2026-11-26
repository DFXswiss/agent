"""Room for one more session on a running workgroup VM, measured on the VM (`vm.room`; no fixed number of sessions
per VM and no fixed need per session: the measured use of its sessions decides)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.metrics.parsers import GuestMetrics, GuestSession
from agent_runner.vm import room

MIB = 1024 * 1024
GIB = 1024 * MIB
ROW: Any = SimpleNamespace(id="vm-0000000a")


MB = 1000 * 1000


def measured(used_gib: float, load1: float, sessions: dict[str, int] | list[int] | None = None) -> GuestMetrics:
    found = sessions or {}
    pairs = list(found.items()) if isinstance(found, dict) else [("working", memory) for memory in found]
    listed = tuple(
        GuestSession(f"0193000a-0000-7000-8000-0000000000{index:02d}", state, {"mem_current_bytes": memory})
        for index, (state, memory) in enumerate(pairs)
    )
    return GuestMetrics({"mem_total_bytes": 8 * GIB, "mem_used_bytes": int(used_gib * GIB)}, load1, listed)


def test_one_more_session_fits_while_the_vms_ram_and_cpu_last() -> None:
    # no measured session yet: only the guest's own share stays free
    assert room.judge(measured(2, 1.0), cpus=4, load_max=0.85) == room.Room(True, 6 * 1024, 0, 0.25)
    # the largest session that has not ended is what one more needs (no fixed number)
    busy = measured(5, 1.0, {"working": 2 * GIB, "idle": 1 * GIB, "stopped": 3 * GIB})
    assert room.judge(busy, cpus=4, load_max=0.85) == room.Room(True, 3 * 1024, 2048, 0.25)
    # 1.5 GiB free, 2 GiB needed: no more session on this VM
    assert room.judge(measured(6.5, 1.0, {"working": 2 * GIB}), cpus=4, load_max=0.85).fits is False
    assert room.judge(measured(2, 3.6), cpus=4, load_max=0.85).fits is False  # 90 % load per vCPU
    assert room.judge(measured(9, 0.0), cpus=0, load_max=0.85).available_mib == 0


SESSIONS = [350 * MB, 380 * MB, 410 * MB, 430 * MB, 450 * MB, 480 * MB, 510 * MB]
"""The DFX workgroup VM on 2026-11-26, 18:34: 7 sessions of 350-510 MB, about 3 % of a CPU each."""


def test_the_measurement_of_the_dfx_workgroup_vm() -> None:
    """4 vCPU, 8 GiB: 7 sessions use 3.6 GB (4.3 GB available), load 1.4. One more fits; at the same measured use per
    session the VM's memory runs short at about 13-15 sessions - then the next session goes to a further VM of the
    workgroup (the existing order), the VM does not grow."""
    used = 8 * GIB - 4300 * MB  # 4.3 GB available
    now = room.judge(measured(used / GIB, 1.4, SESSIONS), cpus=4, load_max=0.85)
    assert (now.fits, now.need_mib, round(now.load, 2)) == (True, 486, 0.35)  # 510 MB, the largest session
    per_session = sum(SESSIONS) // len(SESSIONS)
    fitting = [
        n
        for n in range(7, 30)
        if room.judge(
            measured((used + (n - 7) * per_session) / GIB, 1.4, [*SESSIONS, *[per_session] * (n - 7)]), 4, 0.85
        ).fits
    ]
    assert 13 <= max(fitting) + 1 <= 15  # the last VM state that still takes one more, plus that one


class Client:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.closed = False

    async def metrics(self) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


GUEST_ANSWER = {
    "vm": {
        "cpu_usage_usec": 1,
        "mem_used_bytes": 2 * GIB,
        "mem_total_bytes": 8 * GIB,
        "disk_used_bytes": 1,
        "disk_total_bytes": 2,
        "net_rx_bytes": 0,
        "net_tx_bytes": 0,
        "uptime_seconds": 5,
        "load1": 0.5,
    },
    "sessions": [],
}


@pytest.mark.parametrize(
    ("answer", "found"),
    [(GUEST_ANSWER, True), (GuestError("guest_unreachable", "metrics"), False), ({"vm": []}, False)],
)
def test_measure_reads_the_guest_and_closes_it(answer: Any, found: bool) -> None:
    client = Client(answer)

    async def client_for(_row: Any) -> Client:
        return client

    metrics = asyncio.run(room.measure(client_for, ROW))
    assert (metrics is not None) is found
    assert client.closed


def test_measure_without_a_client() -> None:
    async def client_for(_row: Any) -> Client:
        raise RunnerError("vm_token_missing", "metrics")

    assert asyncio.run(room.measure(client_for, ROW)) is None


def test_the_checker_logs_its_judgement_and_an_unknown_vm_keeps_its_room() -> None:
    logs: list[tuple[str, dict[str, Any]]] = []
    answers = [GUEST_ANSWER, GuestError("guest_unreachable", "metrics")]

    async def client(_row: Any) -> Client:
        return Client(answers.pop(0))

    env = SimpleNamespace(client=client, settings=SimpleNamespace(cpus=4), log=lambda n, f: logs.append((n, f)))
    check = room.checker(env, load_max=0.85)
    assert asyncio.run(check(ROW)) is True
    assert asyncio.run(check(ROW)) is True  # not measured: the VM keeps its room
    assert logs == [
        ("vm.session_room", {"vm_id": "vm-0000000a", "fits": "yes", "available_mib": 6144, "need_mib": 0,
                             "load_pct": 12}),
        ("vm.session_room", {"vm_id": "vm-0000000a", "fits": "unknown"}),
    ]  # fmt: skip
