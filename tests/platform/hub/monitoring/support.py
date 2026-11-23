"""Test support of the monitoring suite (U-HUB-monitoring).

`Watch` is a private org `mine` (founder `admin`, a member `member`, an org device `own` assigned to the default
workgroup, and a workgroup `hidden` that only the admin reads) and a second private org `other` (founder
`other.founder`, a stranger to `mine`); the certified platform device `shared` is assigned to the default workgroups of
both. The desk of the tasks suites (`tests/platform/hub/tasks/support.py`) provides presence, capacity and VM rows.

Samples are written as the outbox handler of HUB-10 stores them (`hub.metric_sample`), optionally with the time the
hub received them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id
from tests.platform.hub.tasks.support import Desk

SAMPLE_COLUMNS = (
    "cpu_ratio",
    "mem_used_bytes",
    "mem_limit_bytes",
    "disk_used_bytes",
    "disk_limit_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
    "uptime_seconds",
    "reachable",
    "state",
    "free_vm_slots",
    "free_mem_bytes",
)


def values(**overrides: Any) -> dict[str, Any]:
    """The values of one sample (a running entity at half a core)."""
    found: dict[str, Any] = {
        "cpu_ratio": 0.5,
        "mem_used_bytes": 1000,
        "mem_limit_bytes": 4000,
        "disk_used_bytes": 2000,
        "disk_limit_bytes": 8000,
        "net_rx_bytes": 100,
        "net_tx_bytes": 200,
        "uptime_seconds": 60,
        "reachable": True,
        "state": "running",
        "free_vm_slots": None,
        "free_mem_bytes": None,
    }
    found.update(overrides)
    return found


def insert_sample(
    hub: Any,
    kind: str,
    entity_id: str,
    ts: datetime,
    device_id: str,
    *,
    received_at: datetime | None = None,
    session_id: str | None = None,
    **overrides: Any,
) -> None:
    """One raw sample (with `received_at` when given, else the column default; `session_id` for a work VM)."""
    row = values(**overrides)
    columns = ["entity_kind", "entity_id", "ts", "device_id", *SAMPLE_COLUMNS]
    params: list[Any] = [kind, entity_id, ts, device_id, *(row[name] for name in SAMPLE_COLUMNS)]
    if received_at is not None:
        columns.append("received_at")
        params.append(received_at)
    if session_id is not None:
        columns.append("session_id")
        params.append(session_id)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            f"INSERT INTO hub.metric_sample ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
            params,
        )


@dataclass
class Watch:
    """The monitoring scene (module docstring)."""

    world: World
    fleet: Fleet
    desk: Desk
    mine: OrgDraft
    member: Actor
    own: Actor
    hidden: str
    shared: Actor
    other: OrgDraft

    @classmethod
    def build(cls, world: World, fleet: Fleet, desk: Desk) -> Watch:
        mine, other = world.private_org(), world.private_org()
        member, own = world.user(), world.device()
        world.join(mine, member)
        world.join(mine, own, wgs={mine.default_wg: "device"})
        hidden = new_id()
        mine.add("wg_create", mine.founder, wg=hidden, epoch=1)
        response = mine.post(mine.founder, mine.wraps(mine.founder, "wg", hidden, 1, [mine.founder]))
        assert response.status_code == 200, response.text
        shared, seq = fleet.platform_device()
        for draft in (mine, other):
            draft.add("device_assign", draft.founder, device=shared.id, wg=draft.default_wg, platform_seq=seq)
            response = draft.post(draft.founder, draft.wraps(draft.founder, "wg", draft.default_wg, 1, [shared]))
            assert response.status_code == 200, response.text
        desk.online(shared)
        desk.online(own)
        return cls(world, fleet, desk, mine, member, own, hidden, shared, other)

    @property
    def admin(self) -> Actor:
        return self.mine.founder

    @property
    def hub(self) -> Any:
        return self.desk.hub

    def now(self) -> datetime:
        return self.hub.clock()  # type: ignore[no-any-return]

    def vm(self, device: Actor, wg: str | None, state: str = "running") -> str:
        """A VM row of the device (for the workgroup, or a pool VM for None)."""
        vm_id = self.desk.vm(device, state, wg or self.mine.default_wg)
        if wg is None:
            self.desk.execute("UPDATE hub.vm SET wg_id = NULL WHERE id = %s", vm_id)
        return vm_id

    def sample(
        self, kind: str, entity_id: str, device: Actor, *, age: float = 5, session: str | None = None, **overrides: Any
    ) -> datetime:
        """A sample `age` seconds old by the hub clock (of the work VM's `session`); its time."""
        ts = self.now() - timedelta(seconds=age)
        insert_sample(self.hub, kind, entity_id, ts, device.id, session_id=session, **overrides)
        return ts

    def session(
        self, device: Actor, wg: str, org: OrgDraft | None = None, *, state: str = "working"
    ) -> tuple[str, str]:
        """A task and its session on the device in the workgroup (of `mine` unless `org`)."""
        task, session = new_id(), new_id()
        now = self.now()
        draft = org or self.mine
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
                (task, draft.id, wg, draft.founder.id, now, now, now),
            )
            connection.execute(
                "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s)",
                (session, task, draft.id, wg, draft.founder.id, device.id, state, now),
            )
        return task, session
