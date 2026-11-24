"""Building membership-log batches like the web client (`web/src/core/log/rotation.ts` `buildBatch`, 04-crypto.md
sections 4.7 and 5.10, 03-api.md section 4.3).

`build_batch` signs the requested entries as the user and computes the rotation obligation R over them (the union of
`required_set(before, after, subject)` per entry; a member that removes itself owes nothing, its R becomes the
log's `pending`, which `rotation.rotate_pending` serves). It appends one `rotate` per element of R after the requested
entries, draws a fresh key for every epoch the batch creates (`genesis`, `wg_create`, `rotate`), wraps those keys for
every reader after the batch (decertified platform devices excluded) and wraps the current epochs the user holds for
every principal the batch newly entitles (grant, device assignment, accept; with `history` also the older epochs of a
granted workgroup); then it runs the hub's own check (`check_batch`) on its result before anything is sent. The
checking code is the copy of the reference (`agent_runner.crypto.log`). The caller keeps the new keys of the scopes
the user still reads (`Account.learn`), so events of the same batch and later ones use the new epochs. Workgroups
named in `by_catch_up` may lack a key the user does not hold (an `admit` of an admin that is no member there,
ADR-060): those wraps are left to the holders' catch-up.

With `held_only` the wraps for newly entitled principals cover only the current epochs the user holds (an assignment
to the whole organization wraps `OK` and every `WK` the admin has; the remaining keys reach the device by catch-up,
ADR-075 section 5); otherwise a missing key refuses the batch (`epoch_stale`).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import wrap as wrap_format
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import fail
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.entries import VERSION, Head, entry_hash, sign_log_entry
from agent_runner.crypto.log.platform import PlatformState
from agent_runner.crypto.log.rotation import entry_subject, required_set
from agent_runner.crypto.log.state import ORG, WG, OrgState, Scope
from agent_runner.crypto.log.verify import VerifiedLog, append_entries
from agent_runner.crypto.log.wraps import WrapSlot

KeyRing = Mapping[tuple[str, str, int], bytes]


@dataclass(frozen=True)
class Built:
    """The request body (`expected_seq`, `entries`, `wraps`), the created keys, R and the log after the batch."""

    expected_seq: int
    entries: list[dict[str, object]]
    wraps: list[dict[str, object]]
    new_keys: dict[tuple[str, str, int], bytes]
    log: VerifiedLog
    required: tuple[Scope, ...] = ()

    def body(self, events: Sequence[object] = ()) -> dict[str, object]:
        return {"expected_seq": self.expected_seq, "entries": self.entries, "wraps": self.wraps, "events": list(events)}


def sign_entries(
    org: str,
    head: Head | None,
    author: str,
    key: Ed25519PrivateKey,
    fields: Sequence[Mapping[str, object]],
    *,
    ts: str,
    invite: Ed25519PrivateKey | None = None,
) -> list[dict[str, object]]:
    """Entries with `org, seq, prev, by, ts, v` filled in, chained from `head` (None: a new log)."""
    seq = 0 if head is None else head.seq + 1
    prev = "" if head is None else head.hash
    signed: list[dict[str, object]] = []
    for item in fields:
        body = {**item, "org": org, "seq": seq, "prev": prev, "by": author, "ts": ts, "v": VERSION}
        entry = sign_log_entry(body, key, invite if item.get("type") == "accept" else None)
        signed.append(entry)
        seq, prev = seq + 1, entry_hash(entry)
    return signed


def _created(body: Mapping[str, object]) -> list[Scope]:
    kind = body["type"]
    if kind == "genesis":
        return [(ORG, cast(str, body["org"])), (WG, cast(str, body["default_wg"]))]
    if kind == "wg_create":
        return [(WG, cast(str, body["wg"]))]
    if kind == "rotate":
        return [(cast(str, body["scope"]), cast(str, body["scope_id"]))]
    return []


def _required(log: VerifiedLog, requested: Sequence[Mapping[str, object]]) -> list[Scope]:
    """R over the requested entries of `log` (the log after them); a self-remove owes no rotation."""
    first = len(log.entries) - len(requested)
    found: set[Scope] = set()
    for index, entry in enumerate(requested):
        seq = first + index
        body = cast(dict[str, object], entry["body"])
        if seq == 0 or (body["type"] == "remove" and entry_subject(body) == body["by"]):
            continue
        found |= required_set(log.states[seq - 1], log.states[seq], entry_subject(body))
    return sorted(found)


def _kem(state: OrgState, recipient: str) -> bytes:
    return b64u_decode(state.principals[recipient].kem, 32)


def build_batch(
    log: VerifiedLog | None,
    *,
    org: str,
    author: str,
    identity: Identity,
    fields: Sequence[Mapping[str, object]],
    ts: str,
    held: KeyRing,
    platform: PlatformState | None = None,
    invite: Ed25519PrivateKey | None = None,
    history: bool = False,
    by_catch_up: frozenset[str] = frozenset(),
    held_only: bool = False,
) -> Built:
    """The batch of the module docstring; refusals are `CryptoError`s with the hub's codes."""
    founder = identity.public()["sig"]
    head = None if log is None else log.head
    requested = sign_entries(org, head, author, identity.sig, fields, ts=ts, invite=invite)
    trial = append_entries(log, requested, founder=founder, platform=platform)
    required = _required(trial, requested)
    rotations = [
        {"type": "rotate", "scope": scope, "scope_id": scope_id, "epoch": trial.state.epoch(scope, scope_id) + 1}
        for scope, scope_id in required
    ]
    rotate_entries = sign_entries(org, trial.head, author, identity.sig, rotations, ts=ts) if rotations else []
    after = append_entries(trial, rotate_entries, founder=founder, platform=platform) if rotate_entries else trial
    entries = [*requested, *rotate_entries]
    state = after.state
    skip = frozenset() if platform is None else platform.excluded(state)
    created = sorted({scope for entry in entries for scope in _created(cast(dict[str, object], entry["body"]))})
    new_keys = {(scope, scope_id, state.epoch(scope, scope_id)): secrets.token_bytes(32) for scope, scope_id in created}
    before = None if log is None else log.state
    grants = {
        (cast(str, item["subject"]), cast(str, item["wg"])) for item in fields if history and item["type"] == "grant"
    }
    slots: list[WrapSlot] = [
        WrapSlot(scope, scope_id, epoch, recipient)
        for (scope, scope_id, epoch) in new_keys
        for recipient in state.readers(scope, scope_id, skip)
    ]
    for scope, scope_id in state.scopes():
        if (scope, scope_id) in created:
            continue
        current = state.epoch(scope, scope_id)
        for recipient in state.readers(scope, scope_id, skip):
            if cast(OrgState, before).can_read(recipient, scope, scope_id):
                continue
            oldest = 1 if scope == WG and (recipient, scope_id) in grants else current
            slots += [WrapSlot(scope, scope_id, epoch, recipient) for epoch in range(oldest, current + 1)]
    ring = {**held, **new_keys}
    wraps: list[dict[str, object]] = []
    for slot in sorted(slots):
        key = ring.get((slot.scope, slot.scope_id, slot.epoch))
        if key is None and slot.scope == WG and slot.scope_id in by_catch_up:
            continue  # a key the user does not hold: its holders deliver it by catch-up (04-crypto.md section 5.11)
        if key is None:
            if held_only:
                continue
            fail("epoch_stale")
        wraps.append(make_wrap(identity, slot, _kem(state, slot.recipient), key))
    check_batch(log, entries, wraps, wrapped_by=author, founder=founder, platform=platform, excluded=skip)
    return Built(0 if head is None else head.seq + 1, entries, wraps, new_keys, after, tuple(required))


def make_wrap(identity: Identity, slot: WrapSlot, recipient_kem: bytes, key: bytes) -> dict[str, object]:
    """A signed wrap record `{scope, scope_id, epoch, recipient_id, wrap, sig}`."""
    fields: dict[str, Any] = {
        "scope": slot.scope,
        "scope_id": slot.scope_id,
        "epoch": slot.epoch,
        "recipient": slot.recipient,
    }
    sealed = wrap_format.wrap_key(recipient_kem, **fields, key=key)
    signature = wrap_format.sign_wrap(identity.sig, **fields, wrap=sealed)
    return {
        "scope": slot.scope,
        "scope_id": slot.scope_id,
        "epoch": slot.epoch,
        "recipient_id": slot.recipient,
        "wrap": sealed,
        "sig": signature,
    }
