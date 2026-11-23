"""Device enrollment (`POST /v1/device/enroll`, 03-api.md section 6.1, 04-crypto.md sections 3, 5.8 and 5.9).

The request is signed with the key it registers (proof of possession): its `Agent-Key` must be `device.<32 hex>` of
`device_id` and the signature must verify under `pub.sig` (`agent_hub.auth.gate.verify_enrollment`, checked before
any database work). The invitation proves the right to enroll:

- **Org device** (`kind:"org"`, 04-crypto.md 5.8): `entry` is the `accept` entry of the device (`by` = `subject` =
  `device_id`, `kind:"device"`, `pub` = the body's `pub`, `sig` by the device, `inv_sig` by `sk_inv`). The hub creates
  the device (kind `org`, `owner_org`, state `active`) and appends the entry through the org log service of HUB-05
  (`agent_hub.orgs.append.append_batch`: L1-L8 including the invitation, its expiry and both signatures; the projection
  assigns the workgroups of `invite.wgs`; the invitation is marked used). Answer `{state:"active", org_id}`.
- **Platform device** (`kind:"platform"`, 04-crypto.md 5.9): `enroll_sig` (device identity) and `inv_sig` (`pk_inv`
  of the `device_invite` in the verified platform log) over the enrollment text. The hub creates the device (kind
  `platform`, state `pending_certification`) and the `device_enrollment` row that `agent-hub platform
  certify-pending` lists (HUB-04). Answer `{state:"pending_certification"}`.

An invitation is used once: unknown, used, revoked or expired invitations, an invitation of the other kind and a
platform invitation that already has an enrollment all answer `404 invite_unknown` (one code, no oracle). The device ID
is a client-generated ID (03-api.md section 1): the identical request again returns the original answer; any other
request for a known device ID is `409 id_conflict`. Every enrollment is audited as `device.enroll`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto import enroll as enroll_text
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.log.entries import entry_hash
from agent_hub.orgs import logs
from agent_hub.orgs.append import Appender, AppendResult, append_batch

Connection = psycopg.Connection[Any]
UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
INVITE_ID_PATTERN = r"^[A-Za-z0-9_-]{21}[AQgw]$"
B64U_32_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
B64U_64_PATTERN = r"^[A-Za-z0-9_-]{85}[AQgw]$"
VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$"
SYSTEM_PATTERN = r"^[a-z0-9_]{1,32}$"
MAX_COUNT = 2**53 - 1
ORG_FIELDS = frozenset({"entry"})
PLATFORM_FIELDS = frozenset({"enroll_sig", "inv_sig"})


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Pub(Closed):
    kem: StrictStr = Field(pattern=B64U_32_PATTERN)
    sig: StrictStr = Field(pattern=B64U_32_PATTERN)


class Capacity(Closed):
    vm_memory_budget_mib: StrictInt = Field(ge=0, le=MAX_COUNT)
    vm_memory_used_mib: StrictInt = Field(ge=0, le=MAX_COUNT)
    vm_slots_total: StrictInt = Field(ge=0, le=MAX_COUNT)
    vm_slots_free: StrictInt = Field(ge=0, le=MAX_COUNT)
    pool_ready: StrictInt = Field(ge=0, le=MAX_COUNT)


class AcceptEntry(Closed):
    body: dict[str, Any]
    sig: StrictStr = Field(pattern=B64U_64_PATTERN)
    inv_sig: StrictStr = Field(pattern=B64U_64_PATTERN)


class EnrollRequest(Closed):
    """Body of `POST /v1/device/enroll` (both forms; `check_form` enforces the fields of each)."""

    device_id: StrictStr = Field(pattern=UUID7_PATTERN)
    kind: Literal["org", "platform"]
    invite_id: StrictStr = Field(pattern=INVITE_ID_PATTERN)
    pub: Pub
    agent_version: StrictStr = Field(pattern=VERSION_PATTERN)
    os: StrictStr = Field(pattern=SYSTEM_PATTERN)
    arch: StrictStr = Field(pattern=SYSTEM_PATTERN)
    capacity: Capacity
    entry: AcceptEntry | None = None
    enroll_sig: StrictStr | None = Field(default=None, pattern=B64U_64_PATTERN)
    inv_sig: StrictStr | None = Field(default=None, pattern=B64U_64_PATTERN)

    def check_form(self) -> None:
        """An org enrollment has `entry` and no enrollment signatures; a platform enrollment the opposite."""
        sent = self.model_fields_set
        required, forbidden = (ORG_FIELDS, PLATFORM_FIELDS) if self.kind == "org" else (PLATFORM_FIELDS, ORG_FIELDS)
        missing = sorted(name for name in required if name not in sent or getattr(self, name) is None)
        extra = sorted(name for name in forbidden if name in sent)
        if missing or extra:
            raise HubError("request_invalid", detail={"pointers": [f"/{name}" for name in missing + extra]})

    def signed_body(self) -> dict[str, Any]:
        """The body as sent (the enrollment text and the stored request are built from it)."""
        return self.model_dump(mode="json", exclude_unset=True)

    def sig_public(self) -> bytes:
        """The raw Ed25519 key of the device (the pattern admits exactly 32 bytes)."""
        return b64u_decode(self.pub.sig, 32)


@dataclass(frozen=True)
class Enrolled:
    """The answer of an enrollment and, for a new org enrollment, the stored append (published after the commit)."""

    answer: dict[str, str]
    append: AppendResult | None = None


def _invite(connection: Connection, invite_id: str, scope: str, now: datetime) -> UUID | None:
    """The org of an open device invitation of `scope` (None for a platform invitation); else `invite_unknown`."""
    row = connection.execute(
        "SELECT org_id FROM hub.invite WHERE id = %s AND scope = %s AND kind = 'device' AND used_at IS NULL"
        " AND revoked_at IS NULL AND expires_at > %s FOR UPDATE",
        (invite_id, scope, now),
    ).fetchone()
    if row is None:
        raise HubError("invite_unknown")
    return cast(UUID | None, row[0])


def _known_device(connection: Connection, device: str) -> tuple[str, str | None, bytes, bytes] | None:
    """`(kind, owner_org, kem_pub, sig_pub)` of an existing device; `id_conflict` for a user with that ID."""
    principal = connection.execute("SELECT kind FROM hub.principal WHERE id = %s", (device,)).fetchone()
    if principal is None:
        return None
    row = connection.execute(
        "SELECT kind, owner_org, kem_pub, sig_pub FROM hub.device WHERE id = %s", (device,)
    ).fetchone()
    if principal[0] != "device" or row is None:
        raise HubError("id_conflict")
    return str(row[0]), None if row[1] is None else str(row[1]), bytes(row[2]), bytes(row[3])


def _same_keys(known: tuple[str, str | None, bytes, bytes], body: EnrollRequest) -> bool:
    return known[2] == b64u_decode(body.pub.kem, 32) and known[3] == body.sig_public()


def _insert_device(connection: Connection, body: EnrollRequest, *, kind: str, org: UUID | None, now: datetime) -> None:
    state = "active" if kind == "org" else "pending_certification"
    inserted = connection.execute(
        "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s) ON CONFLICT (id) DO NOTHING"
        " RETURNING 1",
        (body.device_id, now),
    ).fetchone()
    if inserted is None:  # a concurrent enrollment of the same device ID committed first
        raise HubError("id_conflict")
    connection.execute(
        "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os, arch,"
        " capacity, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            body.device_id,
            kind,
            b64u_decode(body.pub.kem, 32),
            body.sig_public(),
            org,
            state,
            now,
            body.agent_version,
            body.os,
            body.arch,
            Jsonb(body.capacity.model_dump()),
            now,
        ),
    )


def _audit(connection: Connection, body: EnrollRequest, org: UUID | None, state: str, now: datetime) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="device",
            actor_id=UUID(body.device_id),
            action="device.enroll",
            result="ok",
            org_id=org,
            device_id=UUID(body.device_id),
            target_kind="invite",
            target_id=body.invite_id,
            detail={"device_kind": body.kind, "state": state},
        ),
    )


def _check_accept(body: EnrollRequest) -> dict[str, Any]:
    """The `accept` entry names this enrollment (the log rules check signatures, invitation and chain)."""
    entry = cast(AcceptEntry, body.entry)
    accept = entry.body
    expected = {
        "type": "accept",
        "by": body.device_id,
        "subject": body.device_id,
        "kind": "device",
        "invite_id": body.invite_id,
        "pub": body.pub.model_dump(),
    }
    wrong = sorted(name for name, value in expected.items() if accept.get(name) != value)
    if wrong:
        raise HubError("request_invalid", detail={"pointers": [f"/entry/body/{name}" for name in wrong]})
    return entry.model_dump(mode="json")


def _replayed_org(connection: Connection, body: EnrollRequest, entry: dict[str, Any]) -> Enrolled:
    known = _known_device(connection, body.device_id)
    org = None if known is None else known[1]
    stored = (
        None
        if known is None or known[0] != "org" or org is None or not _same_keys(known, body)
        else connection.execute(
            "SELECT 1 FROM hub.membership_entry e JOIN hub.org_member m ON m.org_id = e.org_id"
            " AND m.principal_id = %s AND m.removed_seq IS NULL JOIN hub.principal p ON p.id = m.principal_id"
            " AND p.disabled_at IS NULL WHERE e.org_id = %s AND e.entry_hash = %s",
            (body.device_id, org, entry_hash(entry)),
        ).fetchone()
    )
    if stored is None:
        raise HubError("id_conflict")
    return Enrolled({"state": "active", "org_id": str(org)})


def enroll_org(connection: Connection, body: EnrollRequest, *, now: datetime, root_key: bytes) -> Enrolled:
    """An org device: create it and append its `accept` (module docstring)."""
    entry = _check_accept(body)
    if _known_device(connection, body.device_id) is not None:
        return _replayed_org(connection, body, entry)
    org = cast(UUID, _invite(connection, body.invite_id, "org", now))
    if entry["body"].get("org") != str(org):
        raise HubError("request_invalid", detail={"pointers": ["/entry/body/org"]})
    _insert_device(connection, body, kind="org", org=org, now=now)
    seq = entry["body"].get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise HubError("request_invalid", detail={"pointers": ["/entry/body/seq"]})
    result = append_batch(
        connection,
        org_id=org,
        appender=Appender(body.device_id, "device"),
        expected_seq=seq,
        entries=[entry],
        wraps=[],
        events=[],
        now=now,
        root_key=root_key,
    )
    _audit(connection, body, org, "active", now)
    return Enrolled({"state": "active", "org_id": str(org)}, result)


def _replayed_platform(connection: Connection, body: EnrollRequest, signed: dict[str, Any]) -> Enrolled:
    row = connection.execute(
        "SELECT request FROM hub.device_enrollment WHERE device_id = %s AND invite_id = %s",
        (body.device_id, body.invite_id),
    ).fetchone()
    if row is None or row[0] != signed:
        raise HubError("id_conflict")
    return Enrolled({"state": "pending_certification"})


def enroll_platform(connection: Connection, body: EnrollRequest, *, now: datetime, root_key: bytes) -> Enrolled:
    """A platform device: check both enrollment signatures, create it pending and store the enrollment."""
    signed = body.signed_body()
    if _known_device(connection, body.device_id) is not None:
        return _replayed_platform(connection, body, signed)
    _invite(connection, body.invite_id, "platform", now)
    platform = logs.platform_state(connection, root_key)
    invite = None if platform is None else platform.invites.get(body.invite_id)
    taken = connection.execute("SELECT 1 FROM hub.device_enrollment WHERE invite_id = %s", (body.invite_id,)).fetchone()
    if invite is None or invite.pk_inv is None or invite.certified_seq is not None or taken is not None:
        raise HubError("invite_unknown")
    try:
        enroll_text.verify_enrollment(signed, b64u_decode(invite.pk_inv, 32))
    except CryptoError:
        raise HubError("signature_invalid") from None
    _insert_device(connection, body, kind="platform", org=None, now=now)
    connection.execute(
        "INSERT INTO hub.device_enrollment (invite_id, device_id, request, created_at) VALUES (%s, %s, %s, %s)",
        (body.invite_id, body.device_id, Jsonb(signed), now),
    )
    _audit(connection, body, None, "pending_certification", now)
    return Enrolled({"state": "pending_certification"})
