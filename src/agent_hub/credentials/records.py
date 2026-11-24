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

`delete_record` removes the row (hard delete: nothing of the record remains; the audit keeps kind and version).
`rotate` applies a `CK` rotation atomically: the new epoch (current + 1, else `409 version_conflict`), its wraps (W3 of
`agent_hub.wraps.rules`, including the owner's self-wrap) and every stored record re-encrypted under the new epoch by
the owner (same IDs and kinds, `version` + 1; a missing, extra or stale record is `409 version_conflict`). A refusal of
any part leaves everything unchanged. Each write appends `credential.put` / `credential.delete` / `credential.rotate`
to the audit and writes the change notices of `agent_hub.credentials.notices`.
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
from agent_hub.wraps.rules import cred_epoch, store_wraps

Connection = psycopg.Connection[Any]
KINDS = credential_format.KINDS
CRED = "cred"
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
class Stored:
    """A stored record."""

    id: UUID
    version: int
    header_text: str
    ct: bytes
    sig: bytes


@dataclass(frozen=True)
class Change:
    """The outcome of a write: the version now stored and the devices that got a change notice."""

    version: int
    devices: tuple[UUID, ...]


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


def lock_owner(connection: Connection, owner: UUID) -> None:
    """Serialise the credential writes of one owner (row lock on the principal)."""
    connection.execute("SELECT 1 FROM hub.principal WHERE id = %s FOR UPDATE", (owner,))


def _stored(connection: Connection, owner: UUID, kind: str, *, lock: bool = False) -> Stored | None:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        f"SELECT id, version, header_text, ct, sig FROM hub.credential WHERE owner_id = %s AND kind = %s{suffix}",
        (owner, kind),
    ).fetchone()
    return None if row is None else Stored(row[0], row[1], row[2], bytes(row[3]), bytes(row[4]))


def _audit(
    connection: Connection,
    action: str,
    actor: Author,
    now: datetime,
    *,
    target_kind: str,
    target_id: str,
    detail: Mapping[str, Any],
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor.kind,
            actor_id=actor.id,
            action=action,
            result="ok",
            device_id=actor.device_id,
            target_kind=target_kind,
            target_id=target_id,
            detail=dict(detail),
        ),
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
) -> Change:
    """Store a new version of the owner's record of `kind` (module docstring)."""
    lock_owner(connection, owner)
    header = verify_record(record, owner=owner, kind=kind, author=author, epoch=cred_epoch(connection, str(owner)))
    if header["version"] != expected_version + 1:
        raise HubError("request_invalid", detail={"pointers": ["/record/header/version"]})
    header_text = canonical(header).decode("ascii")
    ct, sig = b64u_decode(record["ct"]), b64u_decode(record["sig"])
    stored = _stored(connection, owner, kind, lock=True)
    if stored is not None and (stored.header_text, stored.ct, stored.sig) == (header_text, ct, sig):
        unchanged()
        return Change(version=stored.version, devices=())
    current = 0 if stored is None else stored.version
    if expected_version != current:
        raise HubError("version_conflict", detail={"expected": current})
    record_id = UUID(cast(str, header["id"]))
    if stored is None:
        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (record_id, owner, kind, header["version"], header["epoch"], header_text, ct, sig, now),
                )
        except psycopg.errors.UniqueViolation:
            raise HubError("id_conflict") from None
    elif stored.id != record_id:
        raise HubError("request_invalid", detail={"pointers": ["/record/header/id"]})
    else:
        connection.execute(
            "UPDATE hub.credential SET version = %s, epoch = %s, header_text = %s, ct = %s, sig = %s, updated_at = %s"
            " WHERE id = %s",
            (header["version"], header["epoch"], header_text, ct, sig, now, record_id),
        )
    version = header["version"]
    _audit(
        connection,
        "credential.put",
        author,
        now,
        target_kind="credential",
        target_id=str(record_id),
        detail={"kind": kind, "version": version},
    )
    devices = notices.note_change(connection, owner, [kind], now, except_device=author.device_id)
    return Change(version=version, devices=tuple(devices))


def delete_record(connection: Connection, *, owner: Author, kind: str, now: datetime) -> Change:
    """Hard-delete the owner's record of `kind` (`404 not_found` when there is none)."""
    lock_owner(connection, owner.id)
    row = connection.execute(
        "DELETE FROM hub.credential WHERE owner_id = %s AND kind = %s RETURNING id, version", (owner.id, kind)
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
        detail={"kind": kind, "version": row[1]},
    )
    devices = notices.note_change(connection, owner.id, [kind], now)
    return Change(version=row[1], devices=tuple(devices))


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
    stored: dict[str, tuple[UUID, int]] = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT kind, id, version FROM hub.credential WHERE owner_id = %s FOR UPDATE", (owner.id,)
        )
    }
    updates: list[tuple[Any, ...]] = []
    rotated_kinds: list[str] = []
    for record in records:
        header = record.get("header")
        kind = header.get("kind") if isinstance(header, dict) else None
        if not isinstance(kind, str) or kind not in KINDS:
            raise HubError("header_invalid")
        checked = verify_record(record, owner=owner.id, kind=kind, author=owner, epoch=epoch)
        previous = stored.pop(kind, None)
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
    devices = notices.note_change(connection, owner.id, rotated_kinds, now)
    return Rotation(devices=tuple(devices), recipients=stored_wraps.recipients)


def list_records(connection: Connection, owner: UUID) -> list[dict[str, Any]]:
    """The metadata of the owner's records, newest change first (then kind descending)."""
    rows = connection.execute(
        "SELECT kind, version, epoch, updated_at FROM hub.credential WHERE owner_id = %s"
        " ORDER BY updated_at DESC, kind DESC",
        (owner,),
    ).fetchall()
    return [
        {"kind": kind, "version": version, "epoch": epoch, "updated_at": format_time(updated_at)}
        for kind, version, epoch, updated_at in rows
    ]


def get_record(connection: Connection, owner: UUID, kind: str) -> dict[str, Any] | None:
    """The stored record of the owner and kind, None if there is none."""
    stored = _stored(connection, owner, kind)
    return None if stored is None else record_json(stored.header_text, stored.ct, stored.sig)
