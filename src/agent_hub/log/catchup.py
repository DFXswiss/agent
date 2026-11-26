"""Catch-up wrapping (04-crypto.md section 5.11, `GET /v1/orgs/{org}/catch-up`).

Any key holder computes from the verified log the recipients that are entitled to the **current** epoch of a scope
but have no wrap yet, restricted to the wraps it may create (W1/W2). Older epochs are wrapped only on request
(section 5.10), with one exception: an admin that reads a workgroup by inheritance gets **every** epoch of it
(`catch_up_epochs`, ADR-058 section 2: an admin reads the whole workgroup, its history included). The active agent of a
user (ADR-082 section 9.4) gets what its user gets, and in addition every slot its user has since its current
membership (`agent_slots`: also older epochs delivered on request or after a promotion, but none current before the
user's last `accept`/`admit`). A person of the support roster (ADR-096) gets every epoch of every workgroup released
to support (`support_slots`), so a person that joins support later receives the existing releases from any online
holder. The hub answers the endpoint with the same function for the calling principal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import chain

from agent_hub.log.state import WG, OrgState, Roster
from agent_hub.log.verify import VerifiedLog
from agent_hub.log.wraps import CRED, WrapSlot, cred_readers, may_wrap, may_wrap_cred


def missing_wraps(
    log: VerifiedLog,
    wrapper: str,
    existing: Iterable[WrapSlot],
    excluded: Iterable[str] = (),
    roster: Roster | None = None,
) -> list[WrapSlot]:
    """Sorted `org`/`wg` slots of the current epochs without a wrap that `wrapper` may create (and every epoch of a
    released workgroup for the support persons of `roster`)."""
    have, skip, people = set(existing), frozenset(excluded), roster or {}
    state = log.state
    slots = (
        WrapSlot(scope, scope_id, epoch, recipient)
        for scope, scope_id in state.scopes()
        for recipient in state.readers(scope, scope_id, skip)
        for epoch in catch_up_epochs(state, scope, scope_id, recipient)
    )
    candidates = set(chain(slots, agent_slots(log, have), support_slots(state, people)))
    return sorted(slot for slot in candidates if slot not in have and may_wrap(log, slot, wrapper, skip, people))


def support_slots(state: OrgState, roster: Roster) -> set[WrapSlot]:
    """Every epoch of every workgroup released to support, for every person of the roster (ADR-096)."""
    return {
        WrapSlot(WG, wg, epoch, person)
        for wg in state.support
        for person in roster
        for epoch in range(1, state.epoch(WG, wg) + 1)
    }


def agent_slots(log: VerifiedLog, existing: Iterable[WrapSlot]) -> set[WrapSlot]:
    """Every slot of a user implies the same slot for the user's active agent (ADR-082 section 9.4), for the epochs
    since the user's current membership: an epoch at least the one current when the user (re)joined."""
    state = log.state
    return {
        WrapSlot(slot.scope, slot.scope_id, slot.epoch, agent.id)
        for slot in existing
        if (agent := state.agent_of(slot.recipient)) is not None
        and slot.epoch >= log.states[state.principals[slot.recipient].joined_seq].epoch(slot.scope, slot.scope_id)
    }


def catch_up_epochs(state: OrgState, scope: str, scope_id: str, recipient: str) -> range:
    """The epochs catch-up gives an entitled recipient: the current one, and for an admin reading a workgroup by
    inheritance every epoch from 1 (ADR-058 section 2)."""
    current = state.epoch(scope, scope_id)
    return range(1 if scope == WG and state.inherited(recipient, scope_id) else current, current + 1)


def missing_cred_wraps(
    logs: Sequence[VerifiedLog],
    owner: str,
    wrapper: str,
    *,
    current_epoch: int,
    existing: Iterable[WrapSlot],
    excluded: Iterable[str] = (),
) -> list[WrapSlot]:
    """Sorted `cred` slots of the owner's current CK epoch without a wrap that `wrapper` may create, and for the
    owner's agents every CK epoch the owner has a wrap of (ADR-082 section 9.4)."""
    have, skip = set(existing), frozenset(excluded)
    states = [log.state for log in logs]
    readers = cred_readers(states, owner, skip)
    agents = readers & {agent.id for state in states if (agent := state.agent_of(owner)) is not None}
    older = (
        WrapSlot(CRED, owner, slot.epoch, agent)
        for slot in have
        if (slot.scope, slot.scope_id, slot.recipient) == (CRED, owner, owner)
        for agent in agents
    )
    slots = set(chain((WrapSlot(CRED, owner, current_epoch, recipient) for recipient in readers), older))
    return sorted(
        slot for slot in slots if slot not in have and may_wrap_cred(states, slot, wrapper, current_epoch, skip)
    )
