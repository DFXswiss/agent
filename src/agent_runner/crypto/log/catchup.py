"""Copied from agent-core src/agent_hub/log/catchup.py at 99578e6bf451.

Catch-up wrapping (04-crypto.md section 5.11, `GET /v1/orgs/{org}/catch-up`).

Any key holder computes from the verified log the recipients that are entitled to the **current** epoch of a scope
but have no wrap yet, restricted to the wraps it may create (W1/W2). Older epochs are wrapped only on request
(section 5.10), with one exception: an admin that reads a workgroup by inheritance gets **every** epoch of it
(`catch_up_epochs`, ADR-058 section 2: an admin reads the whole workgroup, its history included). The hub answers the
endpoint with the same function for the calling principal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from agent_runner.crypto.log.state import WG, OrgState
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WrapSlot, cred_readers, may_wrap, may_wrap_cred


def missing_wraps(
    log: VerifiedLog, wrapper: str, existing: Iterable[WrapSlot], excluded: Iterable[str] = ()
) -> list[WrapSlot]:
    """Sorted `org`/`wg` slots of the current epochs without a wrap that `wrapper` may create."""
    have, skip = set(existing), frozenset(excluded)
    state = log.state
    slots = (
        WrapSlot(scope, scope_id, epoch, recipient)
        for scope, scope_id in state.scopes()
        for recipient in state.readers(scope, scope_id, skip)
        for epoch in catch_up_epochs(state, scope, scope_id, recipient)
    )
    return sorted(slot for slot in slots if slot not in have and may_wrap(log, slot, wrapper, skip))


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
    """Sorted `cred` slots of the owner's current CK epoch without a wrap that `wrapper` may create."""
    have, skip = set(existing), frozenset(excluded)
    states = [log.state for log in logs]
    slots = (WrapSlot(CRED, owner, current_epoch, recipient) for recipient in cred_readers(states, owner, skip))
    return sorted(
        slot for slot in slots if slot not in have and may_wrap_cred(states, slot, wrapper, current_epoch, skip)
    )
