"""Platform log verification P1-P4 and the certification state (04-crypto.md sections 4.8 and 6.2, ADR-023, ADR-026).

Every entry is signed by the platform root (label `platform-log\\0`); the root public key is pinned by the verifier
(compiled into the web build, configured in hub and runner). Checks per entry, in this order:

1. format (`entries.check_platform_entry`);
2. P1 `seq` gapless from 0 (`log_chain`; the first entry of an append: `seq_conflict`), P2 `prev` (`log_chain`);
3. P3 `root` exactly at `seq` 0 (`log_genesis`) and its `pub` equal to the pinned key (`platform_root_mismatch`);
4. P4 signature under the pinned root key (`log_signature`);
5. type rules (`log_rights` unless stated): invitation IDs unique; `device_certify` names an open `device_invite`
   (`invite_not_in_log`, used once: `invite_consumed`) and carries valid `enroll_sig`/`inv_sig` over the
   enrollment text rebuilt from the entry (`log_accept`), and the device is not currently certified;
   `device_decertify` names a certified device; `role_grant`/`role_revoke` change the role.

A certification is valid until a later `device_decertify` of the device (section 6.2). Validity is per certification
(`valid(seq)`): a device that is decertified and certified again with new keys does not make its old certification,
and the old keys an org learnt from it, valid again (`excluded`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast

from agent_hub.crypto import enroll
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError, fail
from agent_hub.log.entries import Head, check_platform_entry, entry_hash, platform_text, signature_valid
from agent_hub.log.state import OrgState

CERTIFIED = "certified"
DECERTIFIED = "decertified"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Certification:
    """A `device_certify` entry (`seq`) and its end, if any."""

    device: str
    kem: str
    sig: str
    seq: int
    invite_id: str
    decertified_seq: int | None = None


@dataclass(frozen=True)
class PlatformInvite:
    """A `device_invite` or `user_invite` (both carry `pk_inv`; ADR-043 added it to `user_invite`)."""

    invite_id: str
    kind: str
    pk_inv: str
    expires: str
    seq: int
    certified_seq: int | None = None


@dataclass(frozen=True)
class PlatformState:
    """The state after the platform-log entry `seq`."""

    root: str
    seq: int
    head: str
    invites: Mapping[str, PlatformInvite] = field(default_factory=dict)
    certifications: Mapping[int, Certification] = field(default_factory=dict)
    devices: Mapping[str, int] = field(default_factory=dict)
    """`device -> seq` of its latest certification."""
    roles: Mapping[tuple[str, str], int] = field(default_factory=dict)
    org_access: Mapping[str, bool] = field(default_factory=dict)

    def certification(self, seq: int) -> Certification | None:
        """The certification made by the entry `seq` (None if that entry is not a `device_certify`)."""
        return self.certifications.get(seq)

    def status(self, device: str) -> str:
        """`certified`, `decertified` or `unknown`."""
        latest = self.devices.get(device)
        if latest is None:
            return UNKNOWN
        return CERTIFIED if self.certifications[latest].decertified_seq is None else DECERTIFIED

    def valid(self, seq: int) -> bool:
        """The entry `seq` is a `device_certify` that no later `device_decertify` has ended (section 6.2)."""
        found = self.certifications.get(seq)
        return found is not None and found.decertified_seq is None

    def excluded(self, org: OrgState) -> frozenset[str]:
        """Platform devices of an org whose certification (the one their keys come from, `platform_seq`) has ended:
        they are no recipients, readers or authors any more, also after a re-certification with new keys."""
        return frozenset(
            p.id for p in org.principals.values() if p.platform_seq is not None and not self.valid(p.platform_seq)
        )

    def has_role(self, user: str, role: str) -> bool:
        """Platform role (e.g. `inviter`)."""
        return (user, role) in self.roles

    def platform_access(self, org: str, private: bool) -> bool:
        """ADR-026: a private org has platform access; any other org once the latest `org_platform_access` grants it."""
        return private or self.org_access.get(org, False)

    def head_value(self) -> Head:
        """`(seq, hash)` of the last entry."""
        return Head(self.seq, self.head)

    def projection(self) -> dict[str, object]:
        """A JSON view of the state (vectors)."""
        return {
            "head": {"seq": self.seq, "hash": self.head},
            "devices": [
                {"device": device, "status": self.status(device), "certify_seq": seq}
                for device, seq in sorted(self.devices.items())
            ],
            "invites": [
                {"invite_id": inv.invite_id, "kind": inv.kind, "seq": inv.seq, "certified_seq": inv.certified_seq}
                for inv in sorted(self.invites.values(), key=lambda inv: inv.invite_id)
            ],
            "roles": [{"user": user, "role": role} for user, role in sorted(self.roles)],
            "org_access": dict(sorted(self.org_access.items())),
        }


@dataclass(frozen=True)
class VerifiedPlatformLog:
    """Entries and the state after each of them."""

    entries: tuple[dict[str, object], ...]
    states: tuple[PlatformState, ...]

    @property
    def state(self) -> PlatformState:
        """The current state."""
        return self.states[-1]


def _enrollment_valid(body: Mapping[str, object], pk_inv: str) -> bool:
    signed = {
        "device_id": body["device"],
        "kind": "platform",
        "invite_id": body["invite_id"],
        "pub": body["pub"],
        "agent_version": body["agent_version"],
        "os": body["os"],
        "arch": body["arch"],
        "capacity": body["capacity"],
        "enroll_sig": body["enroll_sig"],
        "inv_sig": body["inv_sig"],
    }
    try:
        enroll.verify_enrollment(signed, b64u_decode(pk_inv))
    except CryptoError:
        return False
    return True


def _invite(state: PlatformState, body: dict[str, object], seq: int) -> PlatformState:
    invite_id = cast(str, body["invite_id"])
    if invite_id in state.invites:
        fail("log_rights")
    kind = "device" if body["type"] == "device_invite" else "user"
    invite = PlatformInvite(invite_id, kind, cast(str, body["pk_inv"]), cast(str, body["expires"]), seq)
    return replace(state, invites={**state.invites, invite_id: invite})


def _certify(state: PlatformState, body: dict[str, object], seq: int) -> PlatformState:
    found = state.invites.get(cast(str, body["invite_id"]))
    if found is None or found.kind != "device":
        fail("invite_not_in_log")
    if found.certified_seq is not None:
        fail("invite_consumed")
    if not _enrollment_valid(body, found.pk_inv):
        fail("log_accept")
    device = cast(str, body["device"])
    if state.status(device) == CERTIFIED:
        fail("log_rights")
    pub = cast(dict[str, str], body["pub"])
    cert = Certification(device, pub["kem"], pub["sig"], seq, found.invite_id)
    return replace(
        state,
        invites={**state.invites, found.invite_id: replace(found, certified_seq=seq)},
        certifications={**state.certifications, seq: cert},
        devices={**state.devices, device: seq},
    )


def _decertify(state: PlatformState, body: dict[str, object], seq: int) -> PlatformState:
    device = cast(str, body["device"])
    if state.status(device) != CERTIFIED:
        fail("log_rights")
    latest = state.devices[device]
    ended = replace(state.certifications[latest], decertified_seq=seq)
    return replace(state, certifications={**state.certifications, latest: ended})


def _role(state: PlatformState, body: dict[str, object], seq: int) -> PlatformState:
    key = (cast(str, body["subject"]), cast(str, body["role"]))
    grant = body["type"] == "role_grant"
    if (key in state.roles) == grant:
        fail("log_rights")
    roles = {k: v for k, v in state.roles.items() if k != key}
    return replace(state, roles={**roles, key: seq} if grant else roles)


def _org_access(state: PlatformState, body: dict[str, object], _seq: int) -> PlatformState:
    return replace(state, org_access={**state.org_access, cast(str, body["org"]): cast(bool, body["granted"])})


_TRANSITIONS: dict[str, Callable[[PlatformState, dict[str, object], int], PlatformState]] = {
    "device_invite": _invite,
    "user_invite": _invite,
    "device_certify": _certify,
    "device_decertify": _decertify,
    "role_grant": _role,
    "role_revoke": _role,
    "org_platform_access": _org_access,
}


def apply_platform(
    state: PlatformState | None, entry: object, root: str, *, first_of_append: bool = False
) -> PlatformState:
    """The state after one more entry, or a refusal (P1-P4 and the type rules)."""
    body = check_platform_entry(entry)
    seq = 0 if state is None else state.seq + 1
    if body["seq"] != seq:
        fail("seq_conflict" if first_of_append else "log_chain")
    if body["prev"] != ("" if state is None else state.head):
        fail("log_chain")
    if (state is None) != (body["type"] == "root"):
        fail("log_genesis")
    if state is None and body["pub"] != root:
        fail("platform_root_mismatch")
    if not signature_valid(b64u_decode(root), cast(dict[str, object], entry)["sig"], platform_text(body)):
        fail("log_signature")
    head = entry_hash(cast(dict[str, object], entry))
    if state is None:
        return PlatformState(root=root, seq=0, head=head)
    return replace(_TRANSITIONS[cast(str, body["type"])](state, body, seq), seq=seq, head=head)


def append_platform(log: VerifiedPlatformLog | None, entries: Sequence[object], root: str) -> VerifiedPlatformLog:
    """Append entries to a verified log (the hub's `POST /v1/platform/log`; a wrong first `seq` is `seq_conflict`)."""
    states = list(() if log is None else log.states)
    added = list(() if log is None else log.entries)
    for index, entry in enumerate(entries):
        states.append(apply_platform(states[-1] if states else None, entry, root, first_of_append=index == 0))
        added.append(cast(dict[str, object], entry))
    if not states:
        fail("log_empty")
    return VerifiedPlatformLog(tuple(added), tuple(states))


def verify_platform_log(entries: Sequence[object], root: str) -> VerifiedPlatformLog:
    """Verify a whole platform log against the pinned root key (b64u Ed25519 public key)."""
    if not entries:
        fail("log_empty")
    states: list[PlatformState] = []
    for entry in entries:
        states.append(apply_platform(states[-1] if states else None, entry, root))
    return VerifiedPlatformLog(tuple(cast(dict[str, object], e) for e in entries), tuple(states))
