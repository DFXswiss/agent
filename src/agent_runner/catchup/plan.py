"""Catch-up planning of the runner (05-runner.md section 2.5, 04-crypto.md sections 5.11 and 6.3 rule W2).

The device computes, from its verified logs only, the wraps it may create (`crypto.log.catchup`, the same functions the
hub uses for `GET /v1/orgs/{org}/catch-up`):

- `org`/`wg` scopes of every acting org: the recipients entitled to the **current** epoch (members of the org,
  members and devices of a workgroup, admins reading a workgroup by inheritance, decertified platform devices
  excluded) for which this device is a holder (W1: itself; W2: it is entitled now or held the epoch by the log);
  older epochs are only wrapped on request (5.10), except for an admin reading a workgroup by inheritance, who gets
  every epoch from 1 (`catch_up_epochs`, ADR-058 section 2);
- `cred` scope of a user: the owner and the devices assigned to a workgroup in which the owner has explicit access,
  over every acting org (an admin's inherited read does not count, ADR-058 section 1). The device cannot read a
  user's current `CK` epoch from the logs; it plans for the newest epoch it holds (the hub refuses an epoch beyond
  the owner's current one, and an older epoch only gives the recipient a key it might need for older records).

A slot is planned only when the device holds the key of that epoch. The device does not see which wraps the hub
already stores for others: slots uploaded once (stored or ignored as existing) are remembered per daemon (`done`), so
every slot is sent at most once per daemon; slots the hub refused are skipped until a verified log head changes or
`RETRY_AFTER_S` (15 minutes, host clock) have passed (`refused`; ADR-058 section 6: a hub of an older version refuses
wraps a newer runner may create, and the retry starts the back-fill by itself once the hub is updated). The plan is
sorted by slot and cut to `MAX_WRAPS` (200 per reconcile run).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agent_runner.crypto.log.catchup import missing_cred_wraps, missing_wraps
from agent_runner.crypto.log.state import DEVICE
from agent_runner.crypto.log.wraps import CRED, WrapSlot
from agent_runner.orgs.book import OrgBook

MAX_WRAPS = 200
RETRY_AFTER_S = 15 * 60
"""Host-clock seconds after which a refused slot is offered again even when no verified head changed."""
ATTRIBUTE = "catchup_memory"

Slot = tuple[str, str, int]
Heads = tuple[tuple[str, int, str], ...]


@dataclass
class Memory:
    """What this daemon uploaded (`done`) and what the hub refused at which verified heads and host-clock time
    (`refused`)."""

    done: set[WrapSlot] = field(default_factory=set)
    refused: dict[WrapSlot, tuple[Heads, float]] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic

    def waiting(self, now: Heads) -> set[WrapSlot]:
        """Refused slots not offered again yet: same verified heads and less than `RETRY_AFTER_S` ago."""
        since = self.clock() - RETRY_AFTER_S
        return {slot for slot, (at, when) in self.refused.items() if at == now and when > since}


def heads(book: OrgBook) -> Heads:
    """The verified heads of the acting orgs (a change may turn a refused slot into an allowed one)."""
    found = []
    for org in book.acting_orgs():
        head = book.logs[org].head
        found.append((org, head.seq, head.hash))
    return tuple(found)


def _cred_owners(book: OrgBook, held: Iterable[Slot]) -> dict[str, int]:
    """Owner -> newest `CK` epoch the device holds (users only: a `cred` scope is keyed by its owner)."""
    owners: dict[str, int] = {}
    for scope, scope_id, epoch in held:
        if scope == CRED:
            owners[scope_id] = max(epoch, owners.get(scope_id, 0))
    known = {
        principal.id
        for org in book.acting_orgs()
        for principal in book.logs[org].state.principals.values()
        if principal.kind != DEVICE
    }
    return {owner: epoch for owner, epoch in owners.items() if owner in known}


def plan(book: OrgBook, held: Iterable[Slot], memory: Memory, limit: int | None = None) -> list[WrapSlot]:
    """The slots this device uploads in this run (sorted, at most `limit`, default `MAX_WRAPS`)."""
    holding = set(held)
    now = heads(book)
    skip = set(memory.done) | memory.waiting(now)
    chosen: set[WrapSlot] = set()
    acting = book.acting_orgs()
    for org in acting:
        for slot in missing_wraps(book.logs[org], book.device_id, skip, book.excluded(org)):
            if (slot.scope, slot.scope_id, slot.epoch) in holding:
                chosen.add(slot)
    logs = [book.logs[org] for org in acting]
    excluded = frozenset().union(*(book.excluded(org) for org in acting))
    for owner, epoch in sorted(_cred_owners(book, holding).items()):
        chosen.update(
            missing_cred_wraps(logs, owner, book.device_id, current_epoch=epoch, existing=skip, excluded=excluded)
        )
    return sorted(chosen)[: MAX_WRAPS if limit is None else limit]


def recipient_kem(book: OrgBook, slot: WrapSlot) -> str:
    """The recipient's X25519 public key from the verified logs (the org of the scope first)."""
    orgs = book.acting_orgs()
    if slot.scope != CRED:
        org = slot.scope_id if slot.scope == "org" else book.org_of_wg(slot.scope_id)
        orgs = [org, *orgs] if org is not None else orgs
    for org in orgs:
        principal = book.logs[org].state.principals.get(slot.recipient)
        if principal is not None:
            return principal.kem
    raise KeyError(slot.recipient)


def memory_for(context: Any) -> Memory:
    """The memory of this daemon, kept on the context; refusal times use the host clock."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Memory):
        return found
    made = Memory(clock=context.host.clock)
    setattr(context, ATTRIBUTE, made)
    return made


def refuse(memory: Memory, slots: Iterable[WrapSlot], at: Heads) -> None:
    """Remember slots the hub refused at the verified heads `at`, now on the memory's clock."""
    when = memory.clock()
    for slot in slots:
        memory.refused[slot] = (at, when)


def finish(memory: Memory, slots: Iterable[WrapSlot]) -> None:
    for slot in slots:
        memory.done.add(slot)
        memory.refused.pop(slot, None)
