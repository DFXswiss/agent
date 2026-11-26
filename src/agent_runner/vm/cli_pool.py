"""`agent-runner pool status` (docs/platform/spec/05-runner.md sections 1.2, 3.2, 3.4).

Facts (codes, counts and names of the runner itself; never a token or hash):

    runner.pool.vm vm_id=... lima_name=... golden_version=... created_at=... instance=Running|Stopped|missing
    runner.pool size=1 ready=<n> golden=<version|none> refill=<state>
    runner.capacity budget_mib=... used_mib=... slots_total=... slots_free=... pool_ready=... capacity_low=yes|no

`size` is the fixed `POOL_SIZE` (1, ADR-009 addendum; 0 only while an E2E test holds the pool empty with
`TEST_POOL_EMPTY` in test mode). `refill` is `disabled` (held empty), `no_golden` (no ready golden), `full` (pool
complete), `no_capacity` (no free slot) or `pending` (the daemon will add VMs). The command reads the store like
`image list` (a stopped cluster is started for the command) and needs no hub configuration.
"""

from __future__ import annotations

import argparse
from datetime import UTC
from typing import Any

from agent_runner.config import Config
from agent_runner.vm import capacity as capacity_module
from agent_runner.vm import cli as image_cli
from agent_runner.vm import golden

EXIT_OK = 0


def register(subparsers: Any) -> None:
    pool = subparsers.add_parser("pool", help="warm pool of VMs")
    actions = pool.add_subparsers(dest="pool_command", metavar="ACTION", required=True)
    status = actions.add_parser("status", help="pool VMs, refill state and capacity")
    status.set_defaults(handler=cmd_status)


def refill_state(size: int, ready: int, golden_version: str | None, slots_free: int) -> str:
    if size == 0:
        return "disabled"
    if golden_version is None:
        return "no_golden"
    if ready >= size:
        return "full"
    return "pending" if slots_free > 0 else "no_capacity"


def cmd_status(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    with image_cli.existing_store(config, ctx) as dsn:
        if dsn is None:
            rows: list[dict[str, Any]] = []
            slots: list[tuple[str, int]] = []
            ready = None
        else:
            with golden.connect(dsn) as conn:
                rows = conn.execute(
                    "SELECT id, lima_name, golden_version, created_at FROM runner.vm WHERE state = 'pool'"
                    " ORDER BY created_at, id"
                ).fetchall()
                slots = [
                    (row["state"], int(row["memory_mib"]))
                    for row in conn.execute("SELECT state, memory_mib FROM runner.vm WHERE state <> 'deleted'")
                ]
                ready = golden.ready_golden(conn)
    instances: dict[str, str] = {}
    if rows:
        instances = {instance.name: instance.status for instance in image_cli.make_lima(config, ctx.host.env).list()}
    for row in rows:
        ctx.out.fact(
            "pool.vm",
            {
                "vm_id": row["id"],
                "lima_name": row["lima_name"],
                "golden_version": row["golden_version"],
                "created_at": row["created_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "instance": instances.get(row["lima_name"], "missing"),
            },
        )
    capacity = capacity_module.compute(
        physical_mib=capacity_module.physical_memory_mib(),
        reserve_mib=config.memory_reserve_mib,
        vm_memory_mib=config.vm_memory_mib,
        vms=slots,
    )
    version = None if ready is None else ready.version
    ctx.out.fact(
        "pool",
        {
            "size": config.pool_size,
            "ready": len(rows),
            "golden": version or "none",
            "refill": refill_state(config.pool_size, len(rows), version, capacity.slots_free),
        },
    )
    ctx.out.fact(
        "capacity",
        {
            "budget_mib": capacity.budget_mib,
            "used_mib": capacity.used_mib,
            "slots_total": capacity.slots_total,
            "slots_free": capacity.slots_free,
            "pool_ready": capacity.pool_ready,
            "capacity_low": capacity.capacity_low,
        },
    )
    return EXIT_OK
