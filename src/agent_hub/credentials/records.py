"""Credential records at the hub (03-api.md sections 4.8 and 6.1, 02-data-model.md section 2.7, 04-crypto.md 4.4).

The hub stores a record `{header, ct, sig}` as it was sent (`header_text` = canonical header, `ct`, `sig` raw) and sees
only the metadata of 04-crypto.md section 7 (owner, kind, version, epoch). Every write checks, in this order:

1. the record form and the author's signature (`agent_hub.crypto.credential.verify_credential` with the key of the
   expected author: the owner's identity for browser writes and rotations, the device key for device writes) -
   `header_invalid`, `non_ascii`, `size_exceeded`, `signature_invalid`;
2. that the header names the owner, kind and author of the request (`header_invalid`) and that `version` is
   `expected_version + 1` (`request_invalid`, pointer `/record/header/version`);
3. under a lock of the owner (writes of one owner are serialised), the epoch: the owner's current `CK` epoch
   (`epoch_stale`);
4. `expected_version` against the stored version (0 = no record): `409 version_conflict` with `detail.expected`. A
   repeated write of the stored bytes is answered with the stored version (an idempotent retry). An update keeps the
   record ID of the stored record (`request_invalid`, pointer `/record/header/id`); a new record whose ID is used by
   another record is `409 id_conflict`.

**GitHub identities (ADR-078).** An owner may hold up to `MAX_GITHUB_RECORDS` `github` records (each kind other than
`github` stays one record per owner). The owner is a user (`owner_kind = user`, sealed under the user's `CK`) or an
organization (`org`, a company record; never primary). A company record is sealed under the **org credential key**
(the `cred` scope of the org, KM-35), whose only readers are the org's devices assigned to a workgroup: the admin who
writes it creates the next epoch with every write (`key {epoch, wraps}`: epoch = current + 1, else `409
version_conflict`; one wrap of that epoch for every reader device, else `request_invalid` with pointer `/key/wraps`;
the wraps pass W3 of `agent_hub.wraps.rules` with the admin as wrapper), and the record carries that epoch. A user
has at most one **primary** `github` record. `put_record`, `get_record`, `delete_record` and `list_records` (the
endpoints from before) address only user-owned records and, of kind `github`, only the primary record: a create
(`expected_version` 0) without a primary record stores a new primary record, and after the primary record is deleted
they answer as if there were no record (no other record is promoted). `put_identity` / `delete_identity` /
`make_primary` address one `github` record by its ID (`agent_hub.github`): a new user record becomes primary only
when the user has no primary record; more than `MAX_GITHUB_RECORDS` records of one owner are `size_exceeded`.

`delete_record` removes the row (hard delete: nothing of the record remains; the audit keeps kind and version).
`rotate` applies a `CK` rotation atomically: the new epoch (current + 1, else `409 version_conflict`), its wraps (W3 of
`agent_hub.wraps.rules`, including the owner's self-wrap) and every stored record re-encrypted under the new epoch by
the owner (same IDs and kinds, `version` + 1; a missing, extra or stale record is `409 version_conflict`). Of the
`github` records the rotation must hold the primary one; a non-primary one may be re-encrypted with it (same rules)
or left out (it keeps its epoch until a current client re-seals it, ADR-078 section 1). A refusal of
any part leaves everything unchanged. Each write appends `credential.put` / `credential.delete` / `credential.rotate`
to the audit (the first two with `owner_kind`) and writes the change notices of `agent_hub.credentials.notices`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.credentials import notices
from agent_hub.crypto import credential as credential_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import ERROR_STATUSES, HubError
from agent_hub.wraps.rules import cred_epoch, org_cred_readers, store_wraps

Connection = psycopg.Connection[Any]
KINDS = credential_format.KINDS
CRED = "cred"
GITHUB = "github"
USER, ORG = "user", "org"
MAX_GITHUB_RECORDS = 20
"""At most this many `github` records per owner (ADR-078 section 1)."""
_LEGACY = "owner_kind = 'user' AND (kind <> 'github' OR is_primary)"
"""The records the endpoints from before address: user-owned, and of kind `github` only the primary one."""
MAX_CT_CHARS = 87382
"""The `ct` limit of `contracts/common.json` `credential_record` (64 KiB)."""


@dataclass(frozen=True)
class Author:
    """Who writes a record: a user (the owner) or a device, with the Ed25519 public key of its signatures."""

    id: UUID
    kind: str
    sig_public: bytes

    @property
    def device_id(self) -> UUID | None:
        return self.id if self.kind == "device" else None


@dataclass(frozen=True)
class Owner:
    """The owner of a record: a user or an organization (`owner_kind`)."""

    id: UUID
    kind: str


@dataclass(frozen=True)
class Stored:
    """A stored record."""

    id: UUID
    version: int
    header_text: str
    ct: bytes
    sig: bytes


@dataclass(frozen=True)
class Change:
    """The outcome of a write: the version now stored, the devices that got a change notice and the recipients of
    the wraps stored with it (a company record's new key epoch)."""

    version: int
    devices: tuple[UUID, ...]
    recipients: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OrgKey:
    """The new epoch of an organization's credential key that a company record write creates, with its wraps."""

    epoch: int
    wraps: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class Rotation:
    """The outcome of a rotation: the devices with a notice and the recipients of the stored wraps."""

    devices: tuple[UUID, ...]
    recipients: frozenset[str]


def _crypto_refusal(error: CryptoError) -> HubError:
    return HubError(error.code if error.code in ERROR_STATUSES else "request_invalid")


def record_json(header_text: str, ct: bytes, sig: bytes) -> dict[str, Any]:
    """The API form of a stored record."""
    return {"header": json.loads(header_text), "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def identity_key(connection: Connection, user_id: UUID) -> bytes | None:
    """The identity signing key of a registered user."""
    row = connection.execute("SELECT sig_pub FROM hub.user_identity WHERE user_id = %s", (user_id,)).fetchone()
    return None if row is None else bytes(row[0])


def device_key(connection: Connection, device_id: UUID) -> bytes:
    """The signing key of a device (the gate admitted it, so it exists)."""
    row = connection.execute("SELECT sig_pub FROM hub.device WHERE id = %s", (device_id,)).fetchone()
    return bytes(cast(tuple[bytes], row)[0])


def verify_record(record: object, *, owner: UUID, kind: str, author: Author, epoch: int | None) -> dict[str, object]:
    """Steps 1 and 2 of the module docstring plus the epoch (`epoch_stale`); returns the checked header."""
    if isinstance(record, Mapping) and isinstance(record.get("ct"), str) and len(record["ct"]) > MAX_CT_CHARS:
        raise HubError("size_exceeded")
    try:
        header = credential_format.verify_credential(record, author.sig_public)
    except CryptoError as error:
        raise _crypto_refusal(error) from None
    if header["owner"] != str(owner) or header["kind"] != kind or header["author"] != str(author.id):
        raise HubError("header_invalid")
    if header["epoch"] != epoch:
        raise HubError("epoch_stale")
    return header


def lock_owner(connection: Connection, owner: UUID, owner_kind: str = USER) -> None:
    """Serialise the credential writes of one owner (row lock on the user's principal or on the organization)."""
    table = "hub.org" if owner_kind == ORG else "hub.principal"
    connection.execute(f"SELECT 1 FROM {table} WHERE id = %s FOR UPDATE", (owner,))


def _stored(connection: Connection, owner: UUID, kind: str, *, lock: bool = False) -> Stored | None:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        "SELECT id, version, header_text, ct, sig FROM hub.credential WHERE owner_id = %s AND kind = %s"
        f" AND {_LEGACY}{suffix}",
        (owner, kind),
    ).fetchone()
    return None if row is None else Stored(row[0], row[1], row[2], bytes(row[3]), bytes(row[4]))


def _has_primary(connection: Connection, owner: UUID) -> bool:
    row = connection.execute(
        "SELECT 1 FROM hub.credential WHERE owner_id = %s AND kind = 'github' AND is_primary", (owner,)
    ).fetchone()
    return row is not None


def _insert(
    connection: Connection,
    *,
    owner: Owner,
    kind: str,
    header: Mapping[str, Any],
    content: tuple[str, bytes, bytes],
    now: datetime,
    primary: bool,
) -> UUID:
    """Store a new record (`size_exceeded` beyond `MAX_GITHUB_RECORDS`, `409 id_conflict` for a used ID)."""
    if kind == GITHUB:
        count = connection.execute(
            "SELECT count(*) FROM hub.credential WHERE owner_id = %s AND kind = 'github'", (owner.id,)
        ).fetchone()
        if cast(tuple[int], count)[0] >= MAX_GITHUB_RECORDS:
            raise HubError("size_exceeded")
    record_id = UUID(cast(str, header["id"]))
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " owner_kind, created_at, is_primary) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    owner.id,
                    kind,
                    header["version"],
                    header["epoch"],
                    *content,
                    now,
                    owner.kind,
                    now,
                    primary,
                ),
            )
    except psycopg.errors.UniqueViolation:
        raise HubError("id_conflict") from None
    return record_id


def _update(
    connection: Connection, record_id: UUID, header: Mapping[str, Any], content: tuple[str, bytes, bytes], now: datetime
) -> None:
    connection.execute(
        "UPDATE hub.credential SET version = %s, epoch = %s, header_text = %s, ct = %s, sig = %s, updated_at = %s"
        " WHERE id = %s",
        (header["version"], header["epoch"], *content, now, record_id),
    )


def _audit(
    connection: Connection,
    action: str,
    actor: Author,
    now: datetime,
    *,
    target_kind: str,
    target_id: str,
    detail: Mapping[str, Any],
    org_id: UUID | None = None,
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor.kind,
            actor_id=actor.id,
            action=action,
            result="ok",
            org_id=org_id,
            device_id=actor.device_id,
            target_kind=target_kind,
            target_id=target_id,
            detail=dict(detail),
        ),
    )


def _checked_content(
    record: Mapping[str, Any], header: Mapping[str, object], expected_version: int
) -> tuple[str, bytes, bytes]:
    """Step 2: `version` is `expected_version + 1`; the stored form `(header_text, ct, sig)`."""
    if header["version"] != expected_version + 1:
        raise HubError("request_invalid", detail={"pointers": ["/record/header/version"]})
    return canonical(dict(header)).decode("ascii"), b64u_decode(record["ct"]), b64u_decode(record["sig"])


def _put_audit(
    connection: Connection, author: Author, owner: Owner, *, record_id: UUID, version: int, now: datetime, kind: str
) -> None:
    _audit(
        connection,
        "credential.put",
        author,
        now,
        target_kind="credential",
        target_id=str(record_id),
        detail={"kind": kind, "version": version, "owner_kind": owner.kind},
        org_id=owner.id if owner.kind == ORG else None,
    )


def put_record(
    connection: Connection,
    *,
    owner: UUID,
    kind: str,
    record: Mapping[str, Any],
    expected_version: int,
    author: Author,
    now: datetime,
    root_key: bytes,
) -> Change:
    """Store a new version of the user's record of `kind` (of kind `github`: the primary one; module docstring)."""
    lock_owner(connection, owner)
    header = verify_record(record, owner=owner, kind=kind, author=author, epoch=cred_epoch(connection, str(owner)))
    content = _checked_content(record, header, expected_version)
    stored = _stored(connection, owner, kind, lock=True)
    if stored is not None and (stored.header_text, stored.ct, stored.sig) == content:
        unchanged()
        return Change(version=stored.version, devices=())
    current = 0 if stored is None else stored.version
    if expected_version != current:
        raise HubError("version_conflict", detail={"expected": current})
    user = Owner(owner, USER)
    if stored is None:
        record_id = _insert(connection, owner=user, kind=kind, header=header, content=content, now=now, primary=True)
    elif str(stored.id) != header["id"]:
        raise HubError("request_invalid", detail={"pointers": ["/record/header/id"]})
    else:
        record_id = stored.id
        _update(connection, record_id, header, content, now)
    version = cast(int, header["version"])
    _put_audit(connection, author, user, record_id=record_id, version=version, now=now, kind=kind)
    devices = notices.note_change(connection, owner, [kind], now, root_key=root_key, except_device=author.device_id)
    return Change(version=version, devices=tuple(devices))


def delete_record(connection: Connection, *, owner: Author, kind: str, now: datetime, root_key: bytes) -> Change:
    """Hard-delete the user's record of `kind` (of kind `github`: the primary one; `404 not_found` when there is
    none)."""
    lock_owner(connection, owner.id)
    row = connection.execute(
        f"DELETE FROM hub.credential WHERE owner_id = %s AND kind = %s AND {_LEGACY} RETURNING id, version",
        (owner.id, kind),
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    _audit(
        connection,
        "credential.delete",
        owner,
        now,
        target_kind="credential",
        target_id=str(row[0]),
        detail={"kind": kind, "version": row[1], "owner_kind": USER},
    )
    devices = notices.note_change(connection, owner.id, [kind], now, root_key=root_key)
    return Change(version=row[1], devices=tuple(devices))


@dataclass(frozen=True)
class Identity:
    """A stored `github` record with the metadata the identity endpoints show."""

    id: UUID
    owner: Owner
    primary: bool
    version: int
    epoch: int
    header_text: str
    ct: bytes
    sig: bytes
    created_at: datetime
    updated_at: datetime

    def meta(self) -> dict[str, Any]:
        """The `github_identity_meta` object."""
        return {
            "id": str(self.id),
            "owner_id": str(self.owner.id),
            "owner_kind": self.owner.kind,
            "primary": self.primary,
            "version": self.version,
            "epoch": self.epoch,
            "created_at": format_time(self.created_at),
            "updated_at": format_time(self.updated_at),
        }

    def answer(self) -> dict[str, Any]:
        """The body of `GET /v1/github-identities/{id}` and `GET /v1/device/github-identities/{id}`."""
        return {
            "owner_id": str(self.owner.id),
            "owner_kind": self.owner.kind,
            "primary": self.primary,
            "record": record_json(self.header_text, self.ct, self.sig),
        }


IDENTITY_COLUMNS = "id, owner_id, owner_kind, is_primary, version, epoch, header_text, ct, sig, created_at, updated_at"


def identity_of(row: Sequence[Any]) -> Identity:
    """An `Identity` from a row of `IDENTITY_COLUMNS`."""
    return Identity(
        row[0], Owner(row[1], row[2]), row[3], row[4], row[5], row[6], bytes(row[7]), bytes(row[8]), row[9], row[10]
    )


def owner_identities(connection: Connection, owner_id: UUID) -> list[dict[str, Any]]:
    """The metadata of every `github` record of an owner (user or organization), newest change first (then ID
    descending)."""
    rows = connection.execute(
        f"SELECT {IDENTITY_COLUMNS} FROM hub.credential WHERE owner_id = %s AND kind = 'github'"
        " ORDER BY updated_at DESC, id DESC",
        (owner_id,),
    ).fetchall()
    return [identity_of(row).meta() for row in rows]


def load_identity(connection: Connection, record_id: UUID) -> Identity | None:
    """The `github` record with the ID, None if there is none."""
    row = connection.execute(
        f"SELECT {IDENTITY_COLUMNS} FROM hub.credential WHERE id = %s AND kind = 'github'", (record_id,)
    ).fetchone()
    return None if row is None else identity_of(row)


def _new_org_epoch(
    connection: Connection, org: UUID, key: OrgKey, *, author: Author, now: datetime, root_key: bytes
) -> frozenset[str]:
    """Store the next epoch of the org's credential key and its wraps (module docstring); their recipients."""
    current = cred_epoch(connection, str(org)) or 0
    if key.epoch != current + 1:
        raise HubError("version_conflict", detail={"expected": current + 1})
    for index, wrap in enumerate(key.wraps):
        if (wrap.get("scope"), wrap.get("scope_id"), wrap.get("epoch")) != (CRED, str(org), key.epoch):
            raise HubError("request_invalid", detail={"pointers": [f"/key/wraps/{index}"]})
    readers = org_cred_readers(connection, org, root_key)
    if not readers <= {wrap.get("recipient_id") for wrap in key.wraps}:
        raise HubError("request_invalid", detail={"pointers": ["/key/wraps"]})
    connection.execute(
        "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, %s, %s)",
        (org, key.epoch, now),
    )
    stored = store_wraps(connection, list(key.wraps), wrapper=str(author.id), now=now, root_key=root_key)
    return stored.recipients


def put_identity(
    connection: Connection,
    *,
    owner: Owner,
    record_id: UUID,
    record: Mapping[str, Any],
    expected_version: int,
    author: Author,
    now: datetime,
    root_key: bytes,
    key: OrgKey | None = None,
) -> Change:
    """Store a version of the owner's `github` record `record_id` (module docstring): the checks of `put_record`, with
    the record addressed by its ID (a record of another owner or kind under the ID is `409 id_conflict`). A user's
    record is sealed under the user's current `CK` epoch; a company record (owner an organization) comes with `key`,
    the next epoch of the org's credential key, which is stored with its wraps in the same transaction (`key` is
    `request_invalid` for a user's record and required for a company record)."""
    lock_owner(connection, owner.id, owner.kind)
    if (key is not None) != (owner.kind == ORG):
        raise HubError("request_invalid", detail={"pointers": ["/key"]})
    epoch = key.epoch if key is not None else cred_epoch(connection, str(owner.id))
    header = verify_record(record, owner=owner.id, kind=GITHUB, author=author, epoch=epoch)
    if header["id"] != str(record_id):
        raise HubError("request_invalid", detail={"pointers": ["/record/header/id"]})
    content = _checked_content(record, header, expected_version)
    row = connection.execute(
        "SELECT owner_id, kind, version, header_text, ct, sig FROM hub.credential WHERE id = %s FOR UPDATE",
        (record_id,),
    ).fetchone()
    if row is not None and (row[0], row[1]) != (owner.id, GITHUB):
        raise HubError("id_conflict")
    if row is not None and (row[3], bytes(row[4]), bytes(row[5])) == content:
        unchanged()
        return Change(version=row[2], devices=())
    current = 0 if row is None else row[2]
    if expected_version != current:
        raise HubError("version_conflict", detail={"expected": current})
    recipients: frozenset[str] = frozenset()
    if key is not None:
        recipients = _new_org_epoch(connection, owner.id, key, author=author, now=now, root_key=root_key)
    if row is None:
        primary = owner.kind == USER and not _has_primary(connection, owner.id)
        _insert(connection, owner=owner, kind=GITHUB, header=header, content=content, now=now, primary=primary)
    else:
        _update(connection, record_id, header, content, now)
    version = cast(int, header["version"])
    _put_audit(connection, author, owner, record_id=record_id, version=version, now=now, kind=GITHUB)
    devices = notices.note_change(
        connection, owner.id, [GITHUB], now, root_key=root_key, except_device=author.device_id
    )
    return Change(version=version, devices=tuple(devices), recipients=recipients)


def delete_identity(
    connection: Connection, identity: Identity, *, actor: Author, now: datetime, root_key: bytes
) -> Change:
    """Hard-delete a `github` record; the defaults naming it go with it (foreign key), no other record becomes
    primary."""
    lock_owner(connection, identity.owner.id, identity.owner.kind)
    row = connection.execute("DELETE FROM hub.credential WHERE id = %s RETURNING version", (identity.id,)).fetchone()
    if row is None:
        raise HubError("not_found")
    _audit(
        connection,
        "credential.delete",
        actor,
        now,
        target_kind="credential",
        target_id=str(identity.id),
        detail={"kind": GITHUB, "version": row[0], "owner_kind": identity.owner.kind},
        org_id=identity.owner.id if identity.owner.kind == ORG else None,
    )
    devices = notices.note_change(connection, identity.owner.id, [GITHUB], now, root_key=root_key)
    return Change(version=row[0], devices=tuple(devices))


def make_primary(
    connection: Connection, identity: Identity, *, actor: Author, now: datetime, root_key: bytes
) -> Change:
    """Make a user's `github` record the primary one (atomically: the previous primary record loses the mark);
    an unchanged mark is an idempotent repeat."""
    lock_owner(connection, identity.owner.id)
    current = connection.execute(
        "SELECT id FROM hub.credential WHERE owner_id = %s AND kind = 'github' AND is_primary", (identity.owner.id,)
    ).fetchone()
    if current is not None and current[0] == identity.id:
        unchanged()
        return Change(version=identity.version, devices=())
    connection.execute(
        "UPDATE hub.credential SET is_primary = false WHERE owner_id = %s AND kind = 'github' AND is_primary",
        (identity.owner.id,),
    )
    connection.execute("UPDATE hub.credential SET is_primary = true WHERE id = %s", (identity.id,))
    _audit(
        connection,
        "github.primary",
        actor,
        now,
        target_kind="user",
        target_id=str(identity.owner.id),
        detail={"identity_id": str(identity.id)},
    )
    devices = notices.note_change(connection, identity.owner.id, [GITHUB], now, root_key=root_key)
    return Change(version=identity.version, devices=tuple(devices))


def _check_rotation_wraps(wraps: Sequence[Mapping[str, Any]], owner: UUID, epoch: int) -> None:
    for index, wrap in enumerate(wraps):
        if (wrap.get("scope"), wrap.get("scope_id"), wrap.get("epoch")) != (CRED, str(owner), epoch):
            raise HubError("request_invalid", detail={"pointers": [f"/wraps/{index}"]})
    if not any(wrap.get("recipient_id") == str(owner) for wrap in wraps):
        raise HubError("request_invalid", detail={"pointers": ["/wraps"]})


def rotate(
    connection: Connection,
    *,
    owner: Author,
    epoch: int,
    wraps: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    now: datetime,
    root_key: bytes,
) -> Rotation:
    """Rotate the owner's `CK` and re-encrypt every record atomically (module docstring)."""
    lock_owner(connection, owner.id)
    current = cred_epoch(connection, str(owner.id)) or 0
    if epoch != current + 1:
        raise HubError("version_conflict", detail={"expected": current + 1})
    _check_rotation_wraps(wraps, owner.id, epoch)
    stored: dict[str, tuple[UUID, int]] = {}
    optional: dict[str, tuple[UUID, int]] = {}
    for kind, record_id, version, primary in connection.execute(
        "SELECT kind, id, version, is_primary FROM hub.credential WHERE owner_id = %s FOR UPDATE", (owner.id,)
    ):
        if kind == GITHUB and not primary:
            optional[str(record_id)] = (record_id, version)
        else:
            stored[kind] = (record_id, version)
    updates: list[tuple[Any, ...]] = []
    rotated_kinds: list[str] = []
    for record in records:
        header = record.get("header")
        kind = header.get("kind") if isinstance(header, dict) else None
        if not isinstance(kind, str) or kind not in KINDS:
            raise HubError("header_invalid")
        checked = verify_record(record, owner=owner.id, kind=kind, author=owner, epoch=epoch)
        previous = optional.pop(cast(str, checked["id"]), None) or stored.pop(kind, None)
        rotated_kinds.append(kind)
        if previous is None or checked["version"] != previous[1] + 1:
            raise HubError("version_conflict")
        if checked["id"] != str(previous[0]):
            raise HubError("request_invalid", detail={"pointers": ["/records"]})
        updates.append(
            (
                checked["version"],
                epoch,
                canonical(checked).decode("ascii"),
                b64u_decode(record["ct"]),
                b64u_decode(record["sig"]),
                now,
                previous[0],
            )
        )
    if stored:
        raise HubError("version_conflict")
    connection.execute(
        "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, %s, %s)",
        (owner.id, epoch, now),
    )
    stored_wraps = store_wraps(connection, list(wraps), wrapper=str(owner.id), now=now, root_key=root_key)
    for values in updates:
        connection.execute(
            "UPDATE hub.credential SET version = %s, epoch = %s, header_text = %s, ct = %s, sig = %s, updated_at = %s"
            " WHERE id = %s",
            values,
        )
    _audit(
        connection,
        "credential.rotate",
        owner,
        now,
        target_kind="credential_key",
        target_id=str(owner.id),
        detail={"epoch": epoch, "records": len(updates)},
    )
    devices = notices.note_change(connection, owner.id, rotated_kinds, now, root_key=root_key)
    return Rotation(devices=tuple(devices), recipients=stored_wraps.recipients)


def list_records(connection: Connection, owner: UUID) -> list[dict[str, Any]]:
    """The metadata of the user's records (of kind `github` the primary one), newest change first (then kind
    descending)."""
    rows = connection.execute(
        f"SELECT kind, version, epoch, updated_at FROM hub.credential WHERE owner_id = %s AND {_LEGACY}"
        " ORDER BY updated_at DESC, kind DESC",
        (owner,),
    ).fetchall()
    return [
        {"kind": kind, "version": version, "epoch": epoch, "updated_at": format_time(updated_at)}
        for kind, version, epoch, updated_at in rows
    ]


def get_record(connection: Connection, owner: UUID, kind: str) -> dict[str, Any] | None:
    """The stored record of the user and kind (of kind `github` the primary one), None if there is none."""
    stored = _stored(connection, owner, kind)
    return None if stored is None else record_json(stored.header_text, stored.ct, stored.sig)
