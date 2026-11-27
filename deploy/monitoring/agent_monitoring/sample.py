"""A sample textfile written by the runner's own renderer (`agent_runner.metrics.textfile`), for the test stack.

    python -m agent_monitoring.sample --dir DIR [--capacity-low] [--interval S --count N]

The snapshot has one device, a running VM with a working and an ended session, a pool VM whose probe failed and a
stopped VM, so every metric of the table of 08-monitoring.md section 3 (with the ADR-042 name of the slot total and
a daily model spend) has at least one series. `--capacity-low` leaves no VM slot free (`agent_runner_device_capacity_low
1`). The file is written like the runner writes it (`write_atomic`: temp file, mode 0644, `rename`), once, or
`--count` times every `--interval` seconds with a fresh timestamp and growing counters.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_runner.metrics import textfile
from agent_runner.metrics.device import DeviceValues
from agent_runner.metrics.session import SessionEntry
from agent_runner.metrics.snapshot import DeviceEntry, Snapshot, VmEntry
from agent_runner.vm.capacity import Capacity

WG = "01930002-0000-7000-8000-00000000000a"
WORKING = "01a1ee56-5e00-7003-8f1b-340691d99f03"
ENDED = "01a1ee56-5e00-7003-8f1b-340691d99f04"
RUNNING_VM, POOL_VM, STOPPED_VM = "vm-0a1b2c3d", "vm-0000beef", "vm-0000dead"
GIB = 1024**3
SPEND = {"day": 1.25}


def snapshot(wall: float, step: int = 0, capacity_low: bool = False) -> Snapshot:
    """The sample at Unix time `wall`; counters grow with `step`."""
    capacity = Capacity(
        budget_mib=65536, used_mib=16384, slots_total=8, slots_free=0 if capacity_low else 6, pool_ready=1
    )
    device = DeviceEntry(
        values=DeviceValues(1.5, 10 * GIB, 32 * GIB, 100 * GIB, 1000 * GIB, 5, 6, 3600, True),
        capacity=capacity,
        hub_connected=True,
        sync_fatal=False,
        backlog=3,
        counters={
            "agent_runner_device_verify_failures_total": 2,
            "agent_runner_device_metrics_dropped_total": 0,
            "agent_runner_vm_start_failures_total": 1,
            "agent_runner_operator_github_connects_total": 0,
            "agent_runner_operator_github_connect_failures_total": 0,
            "agent_runner_workvm_starts_total": 3 + step,
            "agent_runner_workvm_cold_starts_total": 1 + step,
            "agent_runner_workvm_start_queue_seconds_total": 0.5 + 2.0 * step,
            "agent_runner_workvm_start_clone_seconds_total": 12.25 + 12.0 * step,
            "agent_runner_workvm_start_boot_seconds_total": 30.0 + 25.0 * step,
            "agent_runner_workvm_start_ready_seconds_total": 4.125 + 4.0 * step,
        },
    )
    running = VmEntry(
        vm_id=RUNNING_VM,
        wg_id=WG,
        state="running",
        probe_ok=True,
        cpu_ratio=0.75,
        memory_bytes=1_500_000_000,
        memory_limit_bytes=8 * GIB,
        disk_used_bytes=3 * GIB,
        disk_limit_bytes=20 * GIB,
        uptime_seconds=120 + 15 * step,
        net_rx_bytes=1000 + 4096 * step,
        net_tx_bytes=2000 + 2048 * step,
        sessions={"working": 1, "stopped": 1},
    )
    pool = replace(running, vm_id=POOL_VM, wg_id=None, state="pool", probe_ok=False, sessions={})
    stopped = replace(
        running,
        vm_id=STOPPED_VM,
        state="stopped",
        probe_ok=None,
        cpu_ratio=None,
        memory_bytes=None,
        disk_used_bytes=None,
        uptime_seconds=None,
        net_rx_bytes=None,
        net_tx_bytes=None,
        sessions={},
    )
    values: dict[str, int | None] = {
        "cpu_usage_usec": 2_500_000,
        "mem_current_bytes": 734_003_200,
        "mem_max_bytes": 4 * GIB,
        "io_read_bytes": 1_052_672 + 8192 * step,
        "io_write_bytes": 4_194_304 + 16384 * step,
        "workspace_bytes": 123_456,
        "oom_kills": 1,
        "hook_denies": 2,
        "hook_errors": 0,
        "sandbox_denials": 1,
        "egress_denied": 9,
        "model_input_tokens": 1200 + 100 * step,
        "model_output_tokens": 300 + 25 * step,
    }
    sessions = (
        SessionEntry(WORKING, RUNNING_VM, "working", 300 + 15 * step, 0.5, values, True, False),
        SessionEntry(ENDED, RUNNING_VM, "stopped", 60, None, values, False, True),
    )
    at = datetime.fromtimestamp(wall, UTC)
    return Snapshot(wall=wall, at=at, device=device, vms=(running, pool, stopped), sessions=sessions)


def write(directory: Path, wall: float, step: int = 0, capacity_low: bool = False) -> Path:
    metrics = textfile.metrics_of(snapshot(wall, step, capacity_low), spend=SPEND)
    return textfile.write_atomic(directory, textfile.render(metrics))


def run(
    directory: Path,
    *,
    capacity_low: bool,
    count: int,
    interval: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    path = write(directory, clock(), 0, capacity_low)
    for step in range(1, count):
        sleep(interval)
        path = write(directory, clock(), step, capacity_low)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_monitoring.sample", description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, required=True, help="the runner's TEXTFILE_DIR (must exist)")
    parser.add_argument("--capacity-low", action="store_true", help="no free VM slot")
    parser.add_argument("--count", type=int, default=1, help="number of writes (default 1)")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between writes (default 15)")
    args = parser.parse_args(argv)
    path = run(args.dir, capacity_low=args.capacity_low, count=max(args.count, 1), interval=args.interval)
    print(f"sample: wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
