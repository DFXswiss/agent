"""Copied from agent-core src/agent_hub/log/state.py at 07f6d21b261a.

The rights state of one organization as derived from its verified membership log (04-crypto.md section 4.7).

`OrgState` is immutable; `verify.apply` returns a new state per entry, so a reader keeps the state at every `seq`
(E5 checks rights "at `lh`"). Derived rights (section 4.7, ADR-024 addendum):

- an active user member reads the org scope and **writes the default workgroup** (not reducible in v1);
- an admin writes the org scope (management, org whiteboard, profiles) but has **no implicit workgroup access**,
  except the creating admin of a workgroup (`wg_create`) who gets `write` on it;
- workgroup access of users comes from `invite.wgs` (applied at `accept`), `grant` and `ungrant`;
- devices read the workgroups they are assigned to (`invite.wgs` at `accept`, `device_assign`, `device_unassign`) and
  the org scope while they have at least one assignment (ADR-007);
- an archived workgroup stays readable but is no longer writable (decision CRY-02).

`remove` deletes every access and assignment of the subject; a former member who accepts a new invitation starts
again from that invitation (rejoin).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

USER = "user"
DEVICE = "device"
ADMIN = "admin"
MEMBER = "member"
READ = "read"
WRITE = "write"
ORG = "org"
WG = "wg"

Scope = tuple[str, str]
"""`(scope, scope_id)` with scope `org` or `wg`."""


@dataclass(frozen=True)
class Principal:
    """A user or device of the org. `role` is `admin`/`member` for users and `device` for devices."""

    id: str
    kind: str
    role: str
    kem: str
    sig: str
    joined_seq: int
    removed_seq: int | None = None
    platform_seq: int | None = None
    """Platform devices: the platform-log `seq` of their `device_certify` (keys come from there)."""

    @property
    def active(self) -> bool:
        """Not removed."""
        return self.removed_seq is None


@dataclass(frozen=True)
class Invite:
    """An `invite` entry and what happened to it."""

    invite_id: str
    pk_inv: str
    kind: str
    role: str | None
    wgs: Mapping[str, str]
    expires: str
    seq: int
    revoked_seq: int | None = None
    accepted_seq: int | None = None


@dataclass(frozen=True)
class Workgroup:
    """A workgroup of the org."""

    id: str
    default: bool
    created_seq: int
    archived_seq: int | None = None


@dataclass(frozen=True)
class OrgState:
    """Rights after the entry `seq` (whose hash is `head`)."""

    org: str
    founder: str
    """The pinned founder signing key (b64u) of `genesis` (L3)."""
    default_wg: str
    private: bool
    seq: int
    head: str
    principals: Mapping[str, Principal]
    invites: Mapping[str, Invite]
    workgroups: Mapping[str, Workgroup]
    access: Mapping[tuple[str, str], str]
    """Explicit `(user, wg) -> read|write`; the default workgroup is derived and never stored here."""
    assignments: Mapping[tuple[str, str], int]
    """`(device, wg) -> seq` of the assignment."""
    epochs: Mapping[Scope, int]
    epoch_authors: Mapping[tuple[str, str, int], str]
    """`(scope, scope_id, epoch) -> principal` that created the epoch (genesis, `wg_create`, `rotate`)."""
    pending: frozenset[Scope] = field(default_factory=frozenset)
    """Scopes whose rotation is still owed (a member left by itself and cannot rotate, decision CRY-02)."""

    # --- principals and roles ---

    def member(self, principal: str) -> Principal | None:
        """The active principal `principal`, else None."""
        found = self.principals.get(principal)
        return found if found is not None and found.active else None

    def is_admin(self, principal: str) -> bool:
        """Active admin."""
        found = self.member(principal)
        return found is not None and found.role == ADMIN

    def admins(self) -> list[str]:
        """Active admins, sorted."""
        return sorted(p.id for p in self.principals.values() if p.active and p.role == ADMIN)

    # --- workgroups ---

    def wg_access(self, principal: str, wg: str) -> str | None:
        """`read`/`write` of an active user on a workgroup (default workgroup: `write`), else None."""
        found = self.member(principal)
        if found is None or found.kind != USER or wg not in self.workgroups:
            return None
        if wg == self.default_wg:
            return WRITE
        return self.access.get((principal, wg))

    def assigned(self, device: str, wg: str) -> bool:
        """An active device assigned to the workgroup."""
        return self.member(device) is not None and (device, wg) in self.assignments

    def device_wgs(self, device: str) -> list[str]:
        """Workgroups an active device is assigned to, sorted."""
        if self.member(device) is None:
            return []
        return sorted(wg for (dev, wg) in self.assignments if dev == device)

    def writable(self, wg: str) -> bool:
        """An existing workgroup that is not archived."""
        found = self.workgroups.get(wg)
        return found is not None and found.archived_seq is None

    def can_write_wg(self, principal: str, wg: str) -> bool:
        """`wg:write` of a user: write access and the workgroup not archived."""
        return self.wg_access(principal, wg) == WRITE and self.writable(wg)

    # --- scopes ---

    def can_read(self, principal: str, scope: str, scope_id: str) -> bool:
        """Read right on an `org` or `wg` scope (the entitlement to its key)."""
        found = self.member(principal)
        if found is None:
            return False
        if scope == ORG:
            return scope_id == self.org and (found.kind == USER or bool(self.device_wgs(principal)))
        if found.kind == USER:
            return self.wg_access(principal, scope_id) is not None
        return self.assigned(principal, scope_id)

    def readers(self, scope: str, scope_id: str, excluded: Iterable[str] = ()) -> list[str]:
        """Principals entitled to the scope key, sorted; `excluded` drops e.g. decertified platform devices."""
        skip = set(excluded)
        return sorted(p for p in self.principals if p not in skip and self.can_read(p, scope, scope_id))

    def scopes(self) -> list[Scope]:
        """Every `org`/`wg` scope of the org, sorted (org first)."""
        return sorted(self.epochs)

    def epoch(self, scope: str, scope_id: str) -> int:
        """Current epoch of a scope (0 for an unknown scope)."""
        return self.epochs.get((scope, scope_id), 0)

    def readable_scopes(self, principal: str) -> list[Scope]:
        """Every scope the principal may read, sorted."""
        return [scope for scope in self.scopes() if self.can_read(principal, *scope)]

    def projection(self) -> dict[str, object]:
        """A JSON view of the state (vectors and the hub projections of 02-data-model.md section 2.2)."""
        return {
            "org": self.org,
            "default_wg": self.default_wg,
            "private": self.private,
            "head": {"seq": self.seq, "hash": self.head},
            "members": [
                {
                    "principal_id": p.id,
                    "kind": p.kind,
                    "role": p.role,
                    "joined_seq": p.joined_seq,
                    "removed_seq": p.removed_seq,
                    "platform_seq": p.platform_seq,
                }
                for p in sorted(self.principals.values(), key=lambda p: p.id)
            ],
            "workgroups": [
                {
                    "wg_id": wg.id,
                    "is_default": wg.default,
                    "created_seq": wg.created_seq,
                    "archived_seq": wg.archived_seq,
                    "epoch": self.epoch(WG, wg.id),
                    "access": {p: a for p in sorted(self.principals) if (a := self.wg_access(p, wg.id)) is not None},
                    "devices": [p for p in sorted(self.principals) if self.assigned(p, wg.id)],
                }
                for wg in sorted(self.workgroups.values(), key=lambda wg: wg.id)
            ],
            "org_epoch": self.epoch(ORG, self.org),
            "invites": [
                {
                    "invite_id": inv.invite_id,
                    "kind": inv.kind,
                    "seq": inv.seq,
                    "revoked_seq": inv.revoked_seq,
                    "accepted_seq": inv.accepted_seq,
                }
                for inv in sorted(self.invites.values(), key=lambda inv: inv.invite_id)
            ],
            "pending_rotation": [{"scope": s, "scope_id": i} for s, i in sorted(self.pending)],
        }
