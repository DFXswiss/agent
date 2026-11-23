"""Test kit of the RUN-14 suites: the RUN-04 session kit with a guest that keeps pings, local sessions on a VM and the
installed `Pinger`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner.pings.send import Pinger, install
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow

from ..reconcile.rig import Env, standard_world
from ..sessions.kit import NOW, ORG, OWNER, TASK, WG, WG_OTHER, FakeGuest, Kit, kit, session_id

TASK_OTHER = "01930004-0000-7000-8000-000000000002"
ENTRY = "01930006-0000-7000-8000-000000000001"


def ping_id(n: int) -> str:
    return f"01930007-0000-7000-8000-{n:012d}"


@dataclass
class PingGuest(FakeGuest):
    """`FakeGuest` with `POST /v1/sessions/{id}/pings`."""

    pings: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def ping(self, sid: str, ping: Mapping[str, Any]) -> None:
        self._call("ping", (sid, dict(ping)))
        self.pings.append((sid, dict(ping)))


@dataclass
class PKit:
    kit: Kit
    guest: PingGuest
    pinger: Pinger

    @property
    def env(self) -> Env:
        return self.kit.env

    @property
    def lines(self) -> list[str]:
        return self.kit.lines

    async def session(
        self,
        n: int,
        *,
        state: str = "working",
        wg_id: str = WG,
        task_id: str = TASK,
        vm: bool = True,
        ended_ago_s: float | None = None,
    ) -> str:
        """A local session in `state`, on the workgroup's running VM unless `vm` is False."""
        sid = session_id(n)
        row = await self.kit.vms.ensure_vm(ORG, wg_id) if vm else None
        async with self.env.pool.connection() as conn:
            await records.insert(
                conn,
                session_id=sid,
                task_id=task_id,
                org_id=ORG,
                wg_id=wg_id,
                parent_id=None,
                depth=0,
                owner_id=OWNER,
                created_at=NOW,
            )
            await records.update(conn, sid, state=state, vm_id=None if row is None else row.id)
            if ended_ago_s is not None:
                await conn.execute(
                    "UPDATE runner.session SET ended_at = now() - make_interval(secs => %s) WHERE id = %s",
                    (ended_ago_s, sid),
                )
        return sid

    async def vm(self, wg_id: str = WG) -> VmRow:
        row = await vm_records.for_workgroup(self.env.pool, wg_id)
        assert row is not None
        return row

    async def rows(self) -> list[tuple[Any, ...]]:
        return await self.env.rows(
            "SELECT id::text, session_id::text, kind, level, ref::text, sent_at IS NOT NULL,"
            " delivered_at IS NOT NULL FROM runner.ping ORDER BY created_at, id"
        )

    async def journal(self, records_: list[tuple[str | None, str, dict[str, Any]]], wg_id: str = WG) -> VmJournal:
        """Hand journal records of the workgroup's VM to the ping handler."""
        from agent_runner.pings.journal import HANDLERS  # noqa: PLC0415

        reader = VmJournal(self.kit.session_env, await self.vm(wg_id), handlers=HANDLERS)
        for sid, kind, body in records_:
            self.guest.emit(sid, kind, body)
        await reader.handle(self.guest.records[-len(records_) :])
        return reader


async def pkit(env: Env) -> PKit:
    guest = PingGuest()
    built = await kit(env, guest=guest)
    pinger = install(env.context, Pinger(built.session_env))
    return PKit(kit=built, guest=guest, pinger=pinger)


def run(rig: Any, body: Callable[[PKit], Awaitable[None]]) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(await pkit(env))

    asyncio.run(main())


__all__ = ["ENTRY", "ORG", "TASK", "TASK_OTHER", "WG", "WG_OTHER", "PKit", "PingGuest", "ping_id", "run"]
