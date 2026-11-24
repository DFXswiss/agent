"""Copied from agent-core src/agent_hub/log/batch.py at 952bb156fc9c.

The atomic append batch of `POST /v1/orgs/{org}/log` and `POST /v1/orgs` (03-api.md section 4.3, 04-crypto.md
sections 4.7 and 5.10): entries, the rotation obligation and the wraps.

`check_batch` runs, in this order:

1. L1-L8 for every entry (`verify.append_entries`; a wrong first `seq` is `seq_conflict`);
2. the rotation obligation: R = the union of `R(state before, state after, subject)` over the entries, except a
   member that leaves by itself or an admin that demotes itself (it cannot rotate what it no longer reads; its R
   becomes `OrgState.pending`, ADR-058 section 3). If R is not empty the batch
   MUST contain exactly one `rotate` per element of R and no other `rotate`, each after the last entry that
   contributes to R (`rotation_required`);
3. W3 for every wrap of the batch, created by the appending principal `wrapped_by`, which must be active after the
   batch (`signature_invalid`, `wrap_unauthorized`), checked against the state after the batch;
4. distribution of every epoch the batch creates and that is current at its end (`genesis`, `wg_create`, `rotate`):
   a wrap for every entitled recipient (`rotation_required`);
5. `admit` (ADR-060): wraps for the admitted user of the current org epoch and of the current epoch of the default
   workgroup (`log_admit`); further workgroups may follow by catch-up.

Platform access for `device_assign` with `platform_seq` is passed in (`platform_access`, fail-closed); the hub checks
it first to answer `platform_access_denied` (not a log code) and this module refuses with `log_rights` as backstop.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from agent_runner.crypto.errors import fail
from agent_runner.crypto.log.platform import PlatformState
from agent_runner.crypto.log.rotation import entry_subject, required_set
from agent_runner.crypto.log.state import ORG, WG, Scope
from agent_runner.crypto.log.verify import VerifiedLog, append_entries
from agent_runner.crypto.log.wraps import WrapSlot, check_wrap

SELF_OWED = frozenset({"remove", "role"})
"""Entry types whose R is owed by the others (`OrgState.pending`) when the subject is the author itself."""


@dataclass(frozen=True)
class BatchResult:
    """The verified log after the batch, the rotate set R it had to contain and the wraps it stored."""

    log: VerifiedLog
    required: frozenset[Scope]
    wraps: tuple[WrapSlot, ...]


def _created_scopes(body: dict[str, object]) -> list[Scope]:
    kind = body["type"]
    if kind == "genesis":
        return [(ORG, cast(str, body["org"])), (WG, cast(str, body["default_wg"]))]
    if kind == "wg_create":
        return [(WG, cast(str, body["wg"]))]
    if kind == "rotate":
        return [cast(Scope, (body["scope"], body["scope_id"]))]
    return []


def check_batch(
    log: VerifiedLog | None,
    entries: Sequence[object],
    wraps: Sequence[object],
    *,
    wrapped_by: str,
    founder: str = "",
    platform: PlatformState | None = None,
    now: datetime | None = None,
    platform_access: bool = False,
    excluded: Iterable[str] = (),
) -> BatchResult:
    """Check one batch; returns the new verified log. `founder` is needed only for a new org (`log` None),
    `platform_access` is the org's flag (ADR-026) for `device_assign` with `platform_seq`, `excluded` the decertified
    platform devices (`PlatformState.excluded`)."""
    skip = frozenset(excluded)
    after = append_entries(log, entries, founder=founder, platform=platform, now=now, platform_access=platform_access)
    first = len(after.entries) - len(entries)
    bodies = [cast(dict[str, object], entry["body"]) for entry in after.entries[first:]]
    required: set[Scope] = set()
    last_trigger = -1
    rotates: list[tuple[int, Scope]] = []
    for index, body in enumerate(bodies):
        seq = first + index
        if body["type"] == "rotate":
            rotates.append((index, cast(Scope, (body["scope"], body["scope_id"]))))
        subject = entry_subject(body)
        if seq == 0 or (body["type"] in SELF_OWED and subject == body["by"]):
            continue
        lost = required_set(after.states[seq - 1], after.states[seq], subject)
        if lost:
            required |= lost
            last_trigger = index
    if required:
        scopes = [scope for _, scope in rotates]
        if len(scopes) != len(required) or set(scopes) != required or any(i < last_trigger for i, _ in rotates):
            fail("rotation_required")
    stored = tuple(check_wrap(wrap, wrapped_by, after, skip, require_active=True) for wrap in wraps)
    slots = set(stored)
    state = after.state
    for scope in {scope for body in bodies for scope in _created_scopes(body)}:
        epoch = state.epoch(*scope)
        if any(WrapSlot(*scope, epoch, recipient) not in slots for recipient in state.readers(*scope, skip)):
            fail("rotation_required")
    for body in bodies:
        subject = cast(str, body.get("subject"))
        if body["type"] != "admit" or state.member(subject) is None:
            continue
        needed = ((ORG, state.org), (WG, state.default_wg))
        if any(WrapSlot(*scope, state.epoch(*scope), subject) not in slots for scope in needed):
            fail("log_admit")
    return BatchResult(after, frozenset(required), stored)
