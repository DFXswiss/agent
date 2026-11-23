"""Copied from agent-core src/agent_hub/log/wraps.py at 99578e6bf451.

Wrap rules W1-W3 (04-crypto.md sections 4.2 and 6.3).

Entitlement (who may receive a scope key) is the current read right of the verified log: members of the org scope,
users with access to a workgroup (explicit, or inherited as admin, ADR-058) and devices assigned to it, and for the
`cred` scope of a user the owner plus devices assigned to any workgroup in which the owner has **explicit** access
(in any org; section 2, ADR-007, ADR-058 section 1: the inherited read of an admin does not spread credentials).
Decertified platform devices are passed as `excluded`.

- W1: a principal may wrap a scope epoch for itself if it holds the key (it is entitled now; e.g. the self-wrap of
  the package keys after `accept`, section 5.2).
- W2: a holder may wrap for any principal that is entitled now. A holder of `(scope, scope_id, e)` is the creator
  of epoch e, a principal that could read the scope while e was current, or a principal entitled now (it may have
  received e by an earlier wrap; older epochs are wrapped "on request", section 5.10).
- W3: the hub stores a wrap only if W1/W2 hold for `wrapped_by` in its log state; recipients re-check and ignore
  violating wraps (`wrap_unauthorized`).

The epoch must exist: `1 <= e <= current`. The wrapper's signature (section 4.2) is checked first
(`signature_invalid`) under its key from an org in which it is active (else, for older wraps, from any org that knows
it); an unknown wrapper is `wrap_unauthorized`, and at the hub (`require_active`) also a removed one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import cast

from agent_runner.crypto import wrap as wrap_format
from agent_runner.crypto.encoding import b64u_decode, require_keys
from agent_runner.crypto.errors import fail
from agent_runner.crypto.log.state import DEVICE, ORG, WG, OrgState
from agent_runner.crypto.log.verify import VerifiedLog

CRED = "cred"
WRAP_KEYS = frozenset({"scope", "scope_id", "epoch", "recipient_id", "wrap", "sig"})


@dataclass(frozen=True, order=True)
class WrapSlot:
    """`(scope, scope_id, epoch, recipient)`: the key of a stored wrap."""

    scope: str
    scope_id: str
    epoch: int
    recipient: str

    def to_json(self) -> dict[str, object]:
        """The API form (`GET /v1/orgs/{org}/catch-up`)."""
        return {"scope": self.scope, "scope_id": self.scope_id, "epoch": self.epoch, "recipient_id": self.recipient}


def cred_readers(states: Iterable[OrgState], owner: str, excluded: Iterable[str] = ()) -> frozenset[str]:
    """The owner and every active device assigned to a workgroup in which the owner has explicit access, over all
    orgs."""
    skip = set(excluded)
    found = {owner}
    for state in states:
        for device in state.principals.values():
            eligible = any(state.explicit_access(owner, wg) is not None for wg in state.device_wgs(device.id))
            if device.kind == DEVICE and device.id not in skip and eligible:
                found.add(device.id)
    return frozenset(found)


def may_wrap(log: VerifiedLog, slot: WrapSlot, wrapped_by: str, excluded: Iterable[str] = ()) -> bool:
    """W1/W2 for an `org` or `wg` scope of this log."""
    state = log.state
    if slot.scope not in (ORG, WG) or not 1 <= slot.epoch <= state.epoch(slot.scope, slot.scope_id):
        return False
    readers = state.readers(slot.scope, slot.scope_id, excluded)
    if slot.recipient not in readers:
        return False
    holders = log.epoch_holders(slot.scope, slot.scope_id, slot.epoch) | set(readers)
    return wrapped_by == slot.recipient or wrapped_by in holders


def may_wrap_cred(
    states: Sequence[OrgState], slot: WrapSlot, wrapped_by: str, current_epoch: int, excluded: Iterable[str] = ()
) -> bool:
    """W1/W2 for the `cred` scope of user `slot.scope_id` (its epochs live outside the membership logs)."""
    readers = cred_readers(states, slot.scope_id, excluded)
    return (
        slot.scope == CRED
        and 1 <= slot.epoch <= current_epoch
        and slot.recipient in readers
        and (wrapped_by == slot.recipient or wrapped_by in readers)
    )


def _signer(states: Sequence[OrgState], principal: str, require_active: bool) -> bytes:
    """The wrapper's key: from an org in which it is active, else (readers re-checking an older wrap) from any org
    that knows it; `require_active` (the hub storing a new wrap) refuses removed wrappers."""
    active = [state.member(principal) for state in states]
    known = [found for found in active if found is not None]
    if not known and not require_active:
        known = [state.principals[principal] for state in states if principal in state.principals]
    if not known:
        fail("wrap_unauthorized")
    return b64u_decode(known[0].sig)


def _slot_and_signature(wrap: object, wrapped_by: str, states: Sequence[OrgState], require_active: bool) -> WrapSlot:
    data = require_keys(wrap, WRAP_KEYS)
    if not isinstance(data["epoch"], int) or isinstance(data["epoch"], bool):
        fail("header_invalid")
    slot = WrapSlot(
        cast(str, data["scope"]), cast(str, data["scope_id"]), data["epoch"], cast(str, data["recipient_id"])
    )
    signer = _signer(states, wrapped_by, require_active)
    wrap_format.verify_wrap(
        signer,
        scope=slot.scope,
        scope_id=slot.scope_id,
        epoch=slot.epoch,
        recipient=slot.recipient,
        wrap=cast(str, data["wrap"]),
        signature=cast(str, data["sig"]),
    )
    return slot


def check_wrap(
    wrap: object, wrapped_by: str, log: VerifiedLog, excluded: Iterable[str] = (), *, require_active: bool = False
) -> WrapSlot:
    """W3 for an `org`/`wg` wrap `{scope, scope_id, epoch, recipient_id, wrap, sig}` created by `wrapped_by`.

    The hub passes `require_active=True` (a removed principal stores no wraps); recipients re-checking stored wraps
    leave it False, so a wrap stays valid after its wrapper was removed."""
    slot = _slot_and_signature(wrap, wrapped_by, [log.state], require_active)
    if not may_wrap(log, slot, wrapped_by, excluded):
        fail("wrap_unauthorized")
    return slot


def check_cred_wrap(
    wrap: object,
    wrapped_by: str,
    logs: Sequence[VerifiedLog],
    current_epoch: int,
    excluded: Iterable[str] = (),
    *,
    require_active: bool = False,
) -> WrapSlot:
    """W3 for a `cred` wrap; `logs` are the verified logs of the owner's orgs, `current_epoch` the owner's CK epoch."""
    states = [log.state for log in logs]
    slot = _slot_and_signature(wrap, wrapped_by, states, require_active)
    if not may_wrap_cred(states, slot, wrapped_by, current_epoch, excluded):
        fail("wrap_unauthorized")
    return slot
