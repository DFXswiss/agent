"""VM runtime to the second (KOR-31, ADR-062 section 3, 12-billing.md section 4; concept idea 46).

Daemon task `billing_meter` (`billing/tasks.py`): every `TICK_S` (5 s) on a **platform device** (an org device is own
hardware and sends nothing), while the hub bills (`billing.api` and a price list in force), each running VM is
measured from `runner.billing_meter.measured_through` to now and the interval is attributed:

- the **session VM** of a workgroup (`runner.vm`, state `running`) is shared by the sessions that hold it: the
  interval is split evenly, with the largest remainder, between the sessions on it in a holding state (`HOLDING`:
  from `agent_starting` through `idle`, `resuming`, `stopping`; never `suspended`) - so the shares add up to the
  interval and a shared VM is never charged twice. A VM no session holds (warm pool, idle before the idle stop) is not
  charged.
- other VMs come from the extension point `<area>/billing_vms.py:VMS`, a callable `VMS(context, conn)` returning
  `MeteredVm`s (the temporary work VMs of KOR-29: `vm_kind` `work`, their size and the requesting session, which
  bears the whole interval).

One `runtime` item per session and interval (at most `MAX_ITEM_MS`; a longer gap is sent in pieces) and the new
`measured_through` are written in one transaction: a crash measures nothing twice and loses nothing. A VM seen for
the first time starts at now (a release never charges the past); a VM that is not running any more loses its row, so
the gap of a stopped VM or of a daemon that was down while the VM was stopped is free (its end is unknown). The
attribution is exact to the tick: a session that starts or stops between two ticks is counted from or until the
tick (at most `TICK_S`). While the hub does not bill, the meter rows are dropped (billing switched on again starts
then, never earlier); a clock that goes back never moves `measured_through` back (no interval twice).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from agent_runner import registry
from agent_runner.billing import state
from agent_runner.model.gateways import running_service
from agent_runner.sessions.records import time_text

TICK_S = 5.0
MAX_ITEM_MS = 3_600_000
HOLDING = frozenset({"agent_starting", "prompt_delivering", "working", "idle", "resuming", "stopping"})
SESSION_SIZE = "session"


@dataclass(frozen=True)
class MeteredVm:
    """A running VM and who bears its runtime."""

    vm_id: str
    vm_kind: str
    size: str
    sessions: tuple[str, ...]


VmSource = Callable[[Any, AsyncConnection[Any]], Awaitable[Sequence[MeteredVm]]]


def split(total_ms: int, parts: int) -> list[int]:
    """`total_ms` in `parts` shares that add up to it (largest remainder: the first shares get one more)."""
    base, rest = divmod(total_ms, parts)
    return [base + (1 if index < rest else 0) for index in range(parts)]


def pieces(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """`[start, end]` in intervals of at most `MAX_ITEM_MS`."""
    found: list[tuple[datetime, datetime]] = []
    step = timedelta(milliseconds=MAX_ITEM_MS)
    current = start
    while current < end:
        upto = min(current + step, end)
        found.append((current, upto))
        current = upto
    return found


async def session_vms(_context: Any, conn: AsyncConnection[Any]) -> list[MeteredVm]:
    """The running workgroup VMs with the sessions that hold them."""
    cursor = await conn.execute(
        "SELECT v.id, array_remove(array_agg(s.id::text ORDER BY s.id), NULL) FROM runner.vm v"
        " LEFT JOIN runner.session s ON s.vm_id = v.id AND s.state = ANY(%s)"
        " WHERE v.state = 'running' GROUP BY v.id ORDER BY v.id",
        (sorted(HOLDING),),
    )
    rows = await cursor.fetchall()
    return [MeteredVm(str(vm), SESSION_SIZE, SESSION_SIZE, tuple(sessions)) for vm, sessions in rows]


def sources() -> list[VmSource]:
    found: list[VmSource] = [session_vms]
    found.extend(value for _area, value in registry.discover("billing_vms", "VMS") if callable(value))
    return found


async def measure(context: Any, conn: AsyncConnection[Any], outbox: Any, now: datetime) -> int:
    """One pass in the caller's transaction (module docstring); the number of `runtime` items written."""
    vms: list[MeteredVm] = []
    for source in sources():
        vms.extend(await source(context, conn))
    cursor = await conn.execute("SELECT vm_id, measured_through FROM runner.billing_meter FOR UPDATE")
    through = {str(vm): moment for vm, moment in await cursor.fetchall()}
    running = {vm.vm_id for vm in vms}
    gone = sorted(set(through) - running)
    if gone:
        await conn.execute("DELETE FROM runner.billing_meter WHERE vm_id = ANY(%s)", (gone,))
    written = 0
    for vm in vms:
        start = through.get(vm.vm_id)
        await conn.execute(
            "INSERT INTO runner.billing_meter (vm_id, measured_through) VALUES (%s, %s)"
            " ON CONFLICT (vm_id) DO UPDATE SET measured_through ="
            " GREATEST(runner.billing_meter.measured_through, EXCLUDED.measured_through)",
            (vm.vm_id, now),
        )
        if start is None or start >= now or not vm.sessions:
            continue
        for begin, end in pieces(start, now):
            total = int((end - begin) / timedelta(milliseconds=1))
            for session_id, share in zip(vm.sessions, split(total, len(vm.sessions)), strict=True):
                if share <= 0:
                    continue
                await outbox.enqueue(
                    conn,
                    "runtime",
                    {
                        "session_id": session_id,
                        "vm_id": vm.vm_id,
                        "vm_kind": vm.vm_kind,
                        "size": vm.size,
                        "from": time_text(begin.timestamp()),
                        "to": time_text(end.timestamp()),
                        "runtime_ms": share,
                    },
                )
                written += 1
    return written


def whole_ms(wall: float) -> datetime:
    """The wall time in whole milliseconds (the precision of the contracts' times)."""
    ms = int(wall * 1000)
    return datetime.fromtimestamp(ms // 1000, UTC) + timedelta(milliseconds=ms % 1000)


def metering(env: Any) -> bool:
    """This device meters now: a platform device and a hub that bills."""
    return bool(env.device_kind == "platform" and state.active(env.desired))


async def tick(context: Any, service: Any) -> int:
    env = service.env
    if not metering(env):
        # nothing is measured while the hub does not bill: forget where measuring stood, so that billing switched on
        # (again) starts at that moment and never charges the time in between
        if env.device_kind == "platform":
            async with env.transaction() as conn:
                await conn.execute("DELETE FROM runner.billing_meter")
        return 0
    now = whole_ms(env.wall())
    async with env.transaction() as conn:
        return await measure(context, conn, env.outbox, now)


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.billing {text}\n")
    context.log.flush()


async def run(context: Any, service_of: Callable[[Any], Any] | None = None) -> None:
    find = service_of or running_service
    while not context.stopping.is_set():
        service = find(context)
        if service is not None:
            try:
                await tick(context, service)
            except Exception as exc:
                _line(context, f"meter_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)
