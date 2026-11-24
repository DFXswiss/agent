"""Assignment changes of this device (05-runner.md sections 2.2 step 4 and 3.5, 04-crypto.md sections 4.7 and 5.10,
ADR-005, ADR-007, ADR-013).

What ends a workgroup on this device, and what the runner does then:

- verified log, `device_unassign` of the workgroup (reason `unassigned`): sessions `stopping` (`unassigned`) then
  `stopped`, VM deleted; the workgroup key once the log shows its rotation, the org key once the device reads no
  workgroup of the org any more and the org key was rotated;
- verified log, `wg_archive` (`archived`): sessions stopped, VM deleted, keys kept (an archived workgroup stays
  readable);
- verified log, `remove` of this device (`removed`): sessions stopped, VM deleted, keys of the org and its
  workgroups deleted at once;
- verified platform log, `device_decertify` of this platform device (`decertified`), or a signed hub refusal
  `key_revoked` (a removed org device, a decertified device): every session stopped, every workgroup VM deleted,
  every scope key deleted.

Rules:
- Only verified evidence or the hub's authenticated refusal of this device's key ends anything: an org whose copy is
  stopped (rollback, fork) keeps its VM and keys until an operator acted, and nothing is removed because the hub
  stopped listing an org (its sessions are stopped by step 5 when the hub no longer lists them).
- Devices never rotate (`rotate` is written by the browser of an admin or writer in the same atomic batch as the
  `device_unassign` or `remove`); the runner only observes the new epoch. A key is deleted with all its epochs.
- Sessions are stopped through the session service (`schedule_stop`, reason `unassigned`, awaited) before the VM is
  deleted (`VmManager.delete_workgroup`), so the session transitions reach the hub before the VM disappears. A start
  still running for such a session is awaited first.
- Revocation is persisted in `sync_state` (`device_revoked` = `key_revoked` | `decertified`), so `status` and
  `doctor` show it and a restarted daemon repeats the (idempotent) teardown.
- A verified `remove` of this device in every org whose log it holds is persisted as `device_removed` (the org IDs):
  the only signal on which the maintenance daemon of an own device removes the installation (ADR-074 section 5); a
  `key_revoked` answer of the hub alone never does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent_runner.crypto.log.state import ORG, WG
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session
from agent_runner.sessions import records as session_records
from agent_runner.sessions.service import service_for
from agent_runner.sync import outbox
from agent_runner.vm import records as vm_records
from agent_runner.vm.tasks import manager_for

UNASSIGNED = "unassigned"
REVOKED = "device_revoked"
REMOVED = "device_removed"
KEY_REVOKED = "key_revoked"
DECERTIFIED = "decertified"
SCOPES = (ORG, WG)

Slot = tuple[str, str, int]


@dataclass(frozen=True, order=True)
class Removal:
    org: str
    wg: str
    reason: str


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.assignments {text}\n")
    context.log.flush()


async def footprint(pool: Any) -> set[tuple[str, str]]:
    """`(org, wg)` of every workgroup VM that is not deleted and of every session that has not ended."""
    found = {(row.org_id, row.wg_id) for row in await vm_records.live(pool) if row.org_id and row.wg_id}
    async with session(pool) as conn:
        found |= {(item.org_id, item.wg_id) for item in await session_records.listed(conn)}
    return {(str(org), str(wg)) for org, wg in found}


def removals(book: OrgBook, local: Iterable[tuple[str, str]]) -> list[Removal]:
    """The local workgroups the verified logs no longer give this device (see the table), sorted."""
    found: list[Removal] = []
    for org, wg in sorted(set(local)):
        log = book.logs.get(org)
        if log is None or org in book.stopped or book.platform_stopped is not None:
            continue
        state = log.state
        if not book.acting(org):
            principal = state.principals.get(book.device_id)
            if principal is not None and not principal.active:
                found.append(Removal(org, wg, "removed"))
            continue
        if not state.assigned(book.device_id, wg):
            found.append(Removal(org, wg, UNASSIGNED))
        elif not state.writable(wg):
            found.append(Removal(org, wg, "archived"))
    return found


def _org_of(book: OrgBook, scope: str, scope_id: str) -> str | None:
    if scope == ORG:
        return scope_id if scope_id in book.logs else None
    for org, log in book.logs.items():
        if scope_id in log.state.workgroups:
            return org
    return None


def stale_keys(book: OrgBook, held: Iterable[Slot]) -> list[tuple[str, str]]:
    """`(scope, scope_id)` of the `org`/`wg` keys to delete: the device was removed from the org, or it reads the scope
    no more and the log shows a newer epoch than every one it holds (rotation observed)."""
    newest: dict[tuple[str, str], int] = {}
    for scope, scope_id, epoch in held:
        if scope in SCOPES:
            newest[(scope, scope_id)] = max(epoch, newest.get((scope, scope_id), 0))
    found: list[tuple[str, str]] = []
    for (scope, scope_id), epoch in sorted(newest.items()):
        org = _org_of(book, scope, scope_id)
        if org is None or org in book.stopped or book.platform_stopped is not None:
            continue
        state = book.logs[org].state
        principal = state.principals.get(book.device_id)
        removed = principal is not None and not principal.active
        rotated = not state.can_read(book.device_id, scope, scope_id) and state.epoch(scope, scope_id) > epoch
        if removed or rotated:
            found.append((scope, scope_id))
    return found


async def delete_keys(pool: Any, scopes: Iterable[tuple[str, str]] | None) -> int:
    """Delete the keys of the given scopes (every key of every scope with None); returns the number of rows."""
    async with session(pool) as conn, conn.transaction():
        if scopes is None:
            cur = await conn.execute("DELETE FROM runner.scope_key")
            return cur.rowcount
        deleted = 0
        for scope, scope_id in scopes:
            cur = await conn.execute(
                "DELETE FROM runner.scope_key WHERE scope = %s AND scope_id = %s", (scope, scope_id)
            )
            deleted += cur.rowcount
        return deleted


async def stop_sessions(context: Any, wg: str | None) -> int:
    """Stop the sessions of one workgroup (every session with None), reason `unassigned`; returns their number."""
    service = service_for(context)
    if service is None:
        return 0
    if wg is None:
        # revocation: every start or stop in flight first (a start's session row may not exist yet)
        await asyncio.gather(*list(service.tasks.values()), return_exceptions=True)
    async with session(context.pool) as conn:
        chosen = [item.id for item in await session_records.listed(conn) if wg is None or str(item.wg_id) == wg]
    running = [service.tasks[sid] for sid in chosen if sid in service.tasks]
    await asyncio.gather(*running, return_exceptions=True)
    for sid in chosen:
        service.schedule_stop(sid, UNASSIGNED)
    await asyncio.gather(*(service.tasks[sid] for sid in chosen if sid in service.tasks), return_exceptions=True)
    return len(chosen)


async def end_workgroup(context: Any, removal: Removal) -> None:
    if service_for(context) is None:
        return
    stopped = await stop_sessions(context, removal.wg)
    deleted = await manager_for(context).delete_workgroup(removal.wg)
    _line(
        context,
        f"ended org_id={removal.org} wg_id={removal.wg} reason={removal.reason} sessions={stopped}"
        f" vm={'deleted' if deleted is not None else 'none'}",
    )


def removed_everywhere(book: OrgBook) -> str | None:
    """The org IDs (comma-separated) when every verified, acting log that names this device shows it removed."""
    if book.platform_stopped is not None:
        return None
    named = {
        org: log.state.principals[book.device_id]
        for org, log in book.logs.items()
        if org not in book.stopped and book.device_id in log.state.principals
    }
    if not named or any(principal.active for principal in named.values()):
        return None
    return ",".join(sorted(named))


async def mark_removed(context: Any, book: OrgBook) -> None:
    orgs = removed_everywhere(book)
    if orgs is None:
        return
    async with session(context.pool) as conn, conn.transaction():
        if await outbox.get_state(conn, REMOVED) is not None:
            return
        await outbox.set_state(conn, REMOVED, orgs)
    _line(context, f"device_removed orgs={orgs}")


async def apply_assignments(context: Any, book: OrgBook) -> list[Removal]:
    """Reconcile step 4: end the workgroups the verified logs took away, then delete stale keys."""
    pool = context.pool
    await mark_removed(context, book)
    found = removals(book, await footprint(pool))
    for removal in found:
        await end_workgroup(context, removal)
    async with session(pool) as conn:
        cur = await conn.execute("SELECT scope, scope_id::text, epoch FROM runner.scope_key")
        held = [(str(a), str(b), int(c)) for a, b, c in await cur.fetchall()]
    stale = stale_keys(book, held)
    if stale:
        deleted = await delete_keys(pool, stale)
        _line(context, f"keys_deleted scopes={len(stale)} keys={deleted}")
    return found


async def revoked(context: Any) -> str | None:
    async with session(context.pool) as conn:
        return await outbox.get_state(conn, REVOKED)


async def revoke(context: Any, reason: str) -> None:
    """This device may act no more (`key_revoked`, `decertified`): every session stopped, every workgroup VM deleted,
    every scope key deleted; persisted first, so an interrupted teardown is repeated at the next start."""
    async with session(context.pool) as conn, conn.transaction():
        if await outbox.get_state(conn, REVOKED) is None:
            await outbox.set_state(conn, REVOKED, reason)
    stopped = await stop_sessions(context, None)
    manager = manager_for(context)
    vms = 0
    for row in await vm_records.live(context.pool):
        if row.wg_id is not None and await manager.delete_workgroup(str(row.wg_id)) is not None:
            vms += 1
    keys = await delete_keys(context.pool, None)
    _line(context, f"revoked reason={reason} sessions={stopped} vms={vms} keys={keys}")
