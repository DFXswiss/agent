"""Membership-log verification L1-L10 and the state transition of every entry type (04-crypto.md sections 4.7, 6.1).

After Spike D `keyflow.verify_log`/`_apply`, extended to every entry type of section 4.7. Each entry is checked
against the state **before** it, in this order (the same in every implementation):

1. format (`entries.check_log_entry`: `header_invalid`, `log_type`, `non_ascii`);
2. L1 `seq` gapless from 0 (`log_chain`; the first entry of a hub append: `seq_conflict`), `org` unchanged
   (`log_chain`); L2 `prev` = hash of the previous entry (`log_chain`);
3. L3 `genesis` exactly at `seq` 0 (`log_genesis`), `subject == by`, `pub.sig` = the pinned founder key and `sig`
   valid under it (`log_genesis`);
4. `accept`: the invitation exists (`invite_not_in_log`), is not revoked (`invite_revoked`), L7 not yet accepted
   (`invite_consumed`), not expired at hub receipt (`invite_expired`, only with `now`), then `by == subject`, kind as
   invited, subject not an active member (a former member of the same kind may rejoin; a user with its earlier
   identity keys), `sig` under `pub.sig` and `inv_sig` under `pk_inv` (`log_accept`);
5. every other type: L4 `by` known and `sig` valid under its key as known before the entry (`log_signature`);
   L8 `by` not removed (`log_rights`);
6. L5 the type rule of section 4.7 (`log_rights`; `last_admin`; `invite_*` for `invite_revoke`);
   L6 `rotate.epoch == current + 1` and `wg_create.epoch == 1` (`log_epoch`).

`now` (hub receipt time) switches on the checks that only the hub can make at append time: invitation expiry and,
for `device_assign` of a platform device, that the org has platform access (ADR-026, the hub answers
`platform_access_denied` before; here `log_rights` as backstop) and that the certification named by `platform_seq`
is still valid. A reader replaying the log checks neither (history stays valid). L9/L10 are `entries.check_extends`
and `require_lh` below.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from agent_hub.crypto.encoding import b64u_decode, parse_time
from agent_hub.crypto.errors import fail
from agent_hub.log import rotation
from agent_hub.log.entries import Head, check_log_entry, entry_hash, log_text, signature_valid
from agent_hub.log.platform import PlatformState
from agent_hub.log.state import (
    ADMIN,
    DEVICE,
    ORG,
    USER,
    WG,
    WRITE,
    Invite,
    OrgState,
    Principal,
    Scope,
    Workgroup,
)

Body = dict[str, object]


def _genesis(body: Body, entry: Body, founder: str) -> OrgState:
    pub = cast(dict[str, str], body["pub"])
    valid = (
        body["subject"] == body["by"]
        and pub["sig"] == founder
        and signature_valid(b64u_decode(founder), entry["sig"], log_text(body))
    )
    if not valid:
        fail("log_genesis")
    org, wg, user = cast(str, body["org"]), cast(str, body["default_wg"]), cast(str, body["by"])
    return OrgState(
        org=org,
        founder=founder,
        default_wg=wg,
        private=cast(bool, body["private"]),
        seq=0,
        head="",
        principals={user: Principal(user, USER, ADMIN, pub["kem"], pub["sig"], 0)},
        invites={},
        workgroups={wg: Workgroup(wg, True, 0)},
        access={},
        assignments={},
        epochs={(ORG, org): 1, (WG, wg): 1},
        epoch_authors={(ORG, org, 1): user, (WG, wg, 1): user},
    )


def _accept(state: OrgState, body: Body, entry: Body, seq: int, now: datetime | None) -> OrgState:
    invite = state.invites.get(cast(str, body["invite_id"]))
    if invite is None:
        fail("invite_not_in_log")
    if invite.revoked_seq is not None:
        fail("invite_revoked")
    if invite.accepted_seq is not None:
        fail("invite_consumed")
    if now is not None and now >= parse_time(invite.expires):
        fail("invite_expired")
    subject = cast(str, body["subject"])
    pub = cast(dict[str, str], body["pub"])
    former = state.principals.get(subject)
    text = log_text(body)
    rejoin_valid = former is None or (
        not former.active
        and former.kind == invite.kind
        # a user's identity never changes (section 4.7: `pub` equals the registered identity)
        and (former.kind == DEVICE or (former.kem, former.sig) == (pub["kem"], pub["sig"]))
    )
    valid = (
        body["by"] == subject
        and body["kind"] == invite.kind
        and rejoin_valid
        and signature_valid(b64u_decode(pub["sig"]), entry["sig"], text)
        and signature_valid(b64u_decode(invite.pk_inv), entry["inv_sig"], text)
    )
    if not valid:
        fail("log_accept")
    role = cast(str, invite.role) if invite.kind == USER else DEVICE
    principal = Principal(subject, invite.kind, role, pub["kem"], pub["sig"], seq)
    access, assignments = dict(state.access), dict(state.assignments)
    for wg, level in invite.wgs.items():
        if invite.kind == USER:
            access[(subject, wg)] = level
        else:
            assignments[(subject, wg)] = seq
    return replace(
        state,
        principals={**state.principals, subject: principal},
        invites={**state.invites, invite.invite_id: replace(invite, accepted_seq=seq)},
        access=access,
        assignments=assignments,
    )


def _rights(allowed: bool) -> None:
    if not allowed:
        fail("log_rights")


def _invite(state: OrgState, body: Body, seq: int) -> OrgState:
    wgs = cast(dict[str, str], body["wgs"])
    user = body["kind"] == USER
    _rights(
        body["invite_id"] not in state.invites
        and all(state.writable(wg) and not (user and wg == state.default_wg) for wg in wgs)
    )
    invite = Invite(
        invite_id=cast(str, body["invite_id"]),
        pk_inv=cast(str, body["pk_inv"]),
        kind=cast(str, body["kind"]),
        role=cast(str | None, body.get("role")),
        wgs=dict(wgs),
        expires=cast(str, body["expires"]),
        seq=seq,
    )
    return replace(state, invites={**state.invites, invite.invite_id: invite})


def _invite_revoke(state: OrgState, body: Body, seq: int) -> OrgState:
    invite = state.invites.get(cast(str, body["invite_id"]))
    if invite is None:
        fail("invite_not_in_log")
    if invite.accepted_seq is not None:
        fail("invite_consumed")
    if invite.revoked_seq is not None:
        fail("invite_revoked")
    return replace(state, invites={**state.invites, invite.invite_id: replace(invite, revoked_seq=seq)})


def _role(state: OrgState, body: Body) -> OrgState:
    principal = state.member(cast(str, body["subject"]))
    if principal is None or principal.kind != USER or principal.role == body["role"]:
        fail("log_rights")
    if principal.role == ADMIN and state.admins() == [principal.id]:
        fail("last_admin")
    after = replace(state, principals={**state.principals, principal.id: replace(principal, role=str(body["role"]))})
    if principal.id == body["by"]:
        # self-demotion: like leaving, the subject cannot rotate what it no longer reads (ADR-058 section 3)
        after = replace(after, pending=after.pending | rotation.required_set(state, after, principal.id))
    return after


def _wg_create(state: OrgState, body: Body, seq: int) -> OrgState:
    wg, by = cast(str, body["wg"]), cast(str, body["by"])
    _rights(wg not in state.workgroups and wg != state.org)
    if body["epoch"] != 1:
        fail("log_epoch")
    return replace(
        state,
        workgroups={**state.workgroups, wg: Workgroup(wg, False, seq)},
        access={**state.access, (by, wg): WRITE},
        epochs={**state.epochs, (WG, wg): 1},
        epoch_authors={**state.epoch_authors, (WG, wg, 1): by},
    )


def _wg_archive(state: OrgState, body: Body, seq: int) -> OrgState:
    wg = cast(str, body["wg"])
    _rights(state.writable(wg) and wg != state.default_wg)
    return replace(state, workgroups={**state.workgroups, wg: replace(state.workgroups[wg], archived_seq=seq)})


def _grant(state: OrgState, body: Body) -> OrgState:
    subject, wg = cast(str, body["subject"]), cast(str, body["wg"])
    found = state.member(subject)
    _rights(
        found is not None
        and found.kind == USER
        and state.writable(wg)
        and wg != state.default_wg
        and state.access.get((subject, wg)) != body["access"]
    )
    return replace(state, access={**state.access, (subject, wg): cast(str, body["access"])})


def _ungrant(state: OrgState, body: Body) -> OrgState:
    key = (cast(str, body["subject"]), cast(str, body["wg"]))
    _rights(state.member(key[0]) is not None and key in state.access)
    access = dict(state.access)
    del access[key]
    return replace(state, access=access)


@dataclass(frozen=True)
class _Context:
    platform: PlatformState | None
    now: datetime | None
    platform_access: bool


def _platform_device(state: OrgState, body: Body, seq: int, ctx: _Context) -> Principal:
    device = cast(str, body["device"])
    cert = None if ctx.platform is None else ctx.platform.certification(cast(int, body["platform_seq"]))
    if cert is None or cert.device != device:
        fail("log_rights")
    # at the hub: the org has platform access (ADR-026) and this certification is still valid
    _rights(ctx.now is None or (ctx.platform_access and cast(PlatformState, ctx.platform).valid(cert.seq)))
    found = state.member(device)
    if found is not None:
        _rights(found.platform_seq == cert.seq)
        return found
    return Principal(device, DEVICE, DEVICE, cert.kem, cert.sig, seq, platform_seq=cert.seq)


def _device_assign(state: OrgState, body: Body, seq: int, ctx: _Context) -> OrgState:
    device, wg = cast(str, body["device"]), cast(str, body["wg"])
    _rights(state.writable(wg) and (device, wg) not in state.assignments)
    if "platform_seq" in body:
        principal = _platform_device(state, body, seq, ctx)
    else:
        found = state.member(device)
        if found is None or found.kind != DEVICE or found.platform_seq is not None:
            fail("log_rights")
        principal = found
    return replace(
        state,
        principals={**state.principals, device: principal},
        assignments={**state.assignments, (device, wg): seq},
    )


def _device_unassign(state: OrgState, body: Body) -> OrgState:
    key = (cast(str, body["device"]), cast(str, body["wg"]))
    _rights(state.assigned(*key))
    assignments = dict(state.assignments)
    del assignments[key]
    return replace(state, assignments=assignments)


def _remove(state: OrgState, body: Body, seq: int) -> OrgState:
    subject, by = cast(str, body["subject"]), cast(str, body["by"])
    found = state.member(subject)
    if found is None or not (subject == by or state.is_admin(by)):
        fail("log_rights")
    if found.role == ADMIN and state.admins() == [subject]:
        fail("last_admin")
    after = replace(
        state,
        principals={**state.principals, subject: replace(found, removed_seq=seq)},
        access={key: value for key, value in state.access.items() if key[0] != subject},
        assignments={key: value for key, value in state.assignments.items() if key[0] != subject},
    )
    if subject == by:
        # leaving: the leaver cannot rotate (L8), so the rotation is owed by the remaining holders
        after = replace(after, pending=after.pending | rotation.required_set(state, after, subject))
    return after


def _rotate(state: OrgState, body: Body) -> OrgState:
    scope = cast(Scope, (body["scope"], body["scope_id"]))
    by = cast(str, body["by"])
    if scope[0] == ORG:
        _rights(scope[1] == state.org and state.is_admin(by))
    else:
        _rights(scope[1] in state.workgroups and (state.is_admin(by) or state.wg_access(by, scope[1]) == WRITE))
    epoch = state.epoch(*scope) + 1
    if body["epoch"] != epoch:
        fail("log_epoch")
    return replace(
        state,
        epochs={**state.epochs, scope: epoch},
        epoch_authors={**state.epoch_authors, (*scope, epoch): by},
        pending=state.pending - {scope},
    )


Transition = Callable[[OrgState, Body, int, _Context], OrgState]
_ADMIN_ONLY: dict[str, Transition] = {
    "invite": lambda state, body, seq, _c: _invite(state, body, seq),
    "invite_revoke": lambda state, body, seq, _c: _invite_revoke(state, body, seq),
    "role": lambda state, body, _s, _c: _role(state, body),
    "wg_create": lambda state, body, seq, _c: _wg_create(state, body, seq),
    "wg_archive": lambda state, body, seq, _c: _wg_archive(state, body, seq),
    "grant": lambda state, body, _s, _c: _grant(state, body),
    "ungrant": lambda state, body, _s, _c: _ungrant(state, body),
    "device_assign": _device_assign,
    "device_unassign": lambda state, body, _s, _c: _device_unassign(state, body),
}
_OTHERS: dict[str, Transition] = {
    "remove": lambda state, body, seq, _c: _remove(state, body, seq),
    "rotate": lambda state, body, _s, _c: _rotate(state, body),
}


def _apply_type(state: OrgState, body: Body, seq: int, ctx: _Context) -> OrgState:
    kind = cast(str, body["type"])
    _rights(kind not in _ADMIN_ONLY or state.is_admin(cast(str, body["by"])))
    return {**_ADMIN_ONLY, **_OTHERS}[kind](state, body, seq, ctx)


def apply(
    state: OrgState | None,
    entry: object,
    *,
    founder: str,
    platform: PlatformState | None = None,
    now: datetime | None = None,
    platform_access: bool = False,
    first_of_append: bool = False,
) -> OrgState:
    """The state after one more entry, or a refusal (checks in the order of the module docstring).

    `founder` is the pinned founder signing key (b64u) checked by `genesis`; `platform` the verified platform-log
    state (needed for `device_assign` with `platform_seq`); `now` the hub receipt time (append mode) and
    `platform_access` the org's flag (ADR-026; only read in append mode, fail-closed).
    """
    body = check_log_entry(entry)
    data = cast(Body, entry)
    seq = 0 if state is None else state.seq + 1
    if body["seq"] != seq:
        fail("seq_conflict" if first_of_append else "log_chain")
    if state is not None and body["org"] != state.org:
        fail("log_chain")
    if body["prev"] != ("" if state is None else state.head):
        fail("log_chain")
    if (state is None) != (body["type"] == "genesis"):
        fail("log_genesis")
    if state is None:
        after = _genesis(body, data, founder)
    elif body["type"] == "accept":
        after = _accept(state, body, data, seq, now)
    else:
        author = state.principals.get(cast(str, body["by"]))
        if author is None or not signature_valid(b64u_decode(author.sig), data["sig"], log_text(body)):
            fail("log_signature")
        _rights(author.active)
        after = _apply_type(state, body, seq, _Context(platform, now, platform_access))
    return replace(after, seq=seq, head=entry_hash(data))


@dataclass(frozen=True)
class VerifiedLog:
    """A verified membership log: its entries and the state after each entry (`states[seq]`)."""

    entries: tuple[Body, ...]
    states: tuple[OrgState, ...]

    @property
    def state(self) -> OrgState:
        """The state at the head."""
        return self.states[-1]

    @property
    def head(self) -> Head:
        """`(seq, hash)` of the last entry."""
        return Head(self.state.seq, self.state.head)

    def state_at(self, seq: int) -> OrgState:
        """The state after entry `seq` (E5 "right at `lh`"); `seq` beyond the head is `log_fork` (L10)."""
        require_lh(self, seq)
        return self.states[seq]

    def epoch_holders(self, scope: str, scope_id: str, epoch: int) -> frozenset[str]:
        """Principals that held the key of `(scope, scope_id, epoch)` by the log: its creator and every principal
        that could read the scope in some state in which this epoch was current."""
        holders = {
            principal
            for state in self.states
            if state.epoch(scope, scope_id) == epoch
            for principal in state.principals
            if state.can_read(principal, scope, scope_id)
        }
        author = self.state.epoch_authors.get((scope, scope_id, epoch))
        return frozenset(holders | ({author} if author is not None else set()))


def append_entries(
    log: VerifiedLog | None,
    entries: Sequence[object],
    *,
    founder: str,
    platform: PlatformState | None = None,
    now: datetime | None = None,
    platform_access: bool = False,
) -> VerifiedLog:
    """Append entries to a verified log (a wrong first `seq` is `seq_conflict`). For a new org `log` is None and
    `founder` the creator's registered signing key; otherwise the log's pinned founder is used. `now` and
    `platform_access` as in `apply`."""
    states = list(() if log is None else log.states)
    added = list(() if log is None else log.entries)
    pinned = founder if log is None else log.state.founder
    for index, entry in enumerate(entries):
        previous = states[-1] if states else None
        states.append(
            apply(
                previous,
                entry,
                founder=pinned,
                platform=platform,
                now=now,
                platform_access=platform_access,
                first_of_append=index == 0,
            )
        )
        added.append(cast(Body, entry))
    if not states:
        fail("log_empty")
    return VerifiedLog(tuple(added), tuple(states))


def verify_log(entries: Sequence[object], founder: str, *, platform: PlatformState | None = None) -> VerifiedLog:
    """Verify a whole log from `seq` 0 against the pinned founder key (L1-L8); `log_empty` for no entries."""
    if not entries:
        fail("log_empty")
    states: list[OrgState] = []
    for entry in entries:
        states.append(apply(states[-1] if states else None, entry, founder=founder, platform=platform))
    return VerifiedLog(tuple(cast(Body, e) for e in entries), tuple(states))


def needs_refresh(log: VerifiedLog, lh: int) -> bool:
    """L10, first half: an event names a head beyond the verified one, so the reader refreshes the log."""
    return lh > log.head.seq


def require_lh(log: VerifiedLog, lh: int) -> None:
    """L10, second half: after the refresh the log must contain `lh` (`log_fork` otherwise)."""
    if needs_refresh(log, lh):
        fail("log_fork")
