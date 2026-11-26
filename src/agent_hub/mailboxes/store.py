"""The mailboxes of a user with their release mirror (ADR-099 sections 2 and 3; 03-api.md section 4.8).

A mailbox is a `mailbox` credential record (`agent_hub.credentials.records`, kind `MAILBOX`). The **primary** record
is the mailbox of the automatic login that the endpoints from before address; a user's first mailbox becomes primary,
further ones never do. Every record of the owner is read, written and deleted by its ID here.

- `mailbox_meta(identity, use)`: the `mailbox_meta` object (no owner kind: the owner is always a user).
- `answer(identity)`: the body of `GET /v1/mailboxes/{id}`.
- `own(connection, record_id, user)`: the caller's record, `404 not_found` for anything else (the existence of
  another person's record does not leak).
- `rows_for(connection, user)`: `GET /v1/me/mailboxes` - every record of the user, newest change first (then ID
  descending), each with its release mirror (null when it is released nowhere).
- `set_use(...)`: the plaintext mirror of the sealed release: replaced, or removed when it names no workgroup. Every
  named workgroup must be one where the owner has access now (`403 forbidden` otherwise; the runner checks the
  explicit access again at every read, ADR-099 section 3).
- `released_to(connection, device, wg)`: the records whose mirror names the workgroup and whose owner's `CK` the device
  holds (`GET /v1/device/mailboxes`), newest change first, at most `MAX_RELEASED`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.credentials.eligibility import is_eligible
from agent_hub.credentials.records import IDENTITY_COLUMNS, MAILBOX, Identity, identity_of, load_identity, record_json
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.orgs.rights import wg_access

Connection = psycopg.Connection[Any]
MAX_RELEASED = 200
"""At most this many records answered to a device for one workgroup (the contract's bound)."""


def mailbox_meta(identity: Identity, use: list[str] | None) -> dict[str, Any]:
    """The `mailbox_meta` object."""
    return {
        "id": str(identity.id),
        "owner_id": str(identity.owner.id),
        "primary": identity.primary,
        "version": identity.version,
        "epoch": identity.epoch,
        "created_at": format_time(identity.created_at),
        "updated_at": format_time(identity.updated_at),
        "use": None if use is None else {"wgs": use},
    }


def answer(identity: Identity) -> dict[str, Any]:
    """The body of a read of one record."""
    return {
        "owner_id": str(identity.owner.id),
        "primary": identity.primary,
        "record": record_json(identity.header_text, identity.ct, identity.sig),
    }


def own(connection: Connection, record_id: UUID, user: UUID) -> Identity:
    """The caller's mailbox record, `404 not_found` otherwise."""
    identity = load_identity(connection, record_id, MAILBOX)
    if identity is None or identity.owner.id != user:
        raise HubError("not_found")
    return identity


def rows_for(connection: Connection, user: UUID) -> list[dict[str, Any]]:
    """`GET /v1/me/mailboxes` (module docstring)."""
    uses = {
        row[0]: [str(wg) for wg in row[1]]
        for row in connection.execute(
            "SELECT credential_id, wgs FROM hub.mailbox_use WHERE owner_id = %s", (user,)
        ).fetchall()
    }
    rows = connection.execute(
        f"SELECT {IDENTITY_COLUMNS} FROM hub.credential WHERE owner_id = %s AND kind = %s"
        " ORDER BY updated_at DESC, id DESC",
        (user, MAILBOX),
    ).fetchall()
    found = [identity_of(row) for row in rows]
    return [mailbox_meta(identity, uses.get(identity.id)) for identity in found]


def set_use(connection: Connection, record_id: UUID, owner: UUID, *, wgs: list[str], now: datetime) -> None:
    """The mirror of a mailbox's sealed release (module docstring)."""
    if not wgs:
        connection.execute("DELETE FROM hub.mailbox_use WHERE credential_id = %s", (record_id,))
        return
    if any(wg_access(connection, wg, owner) is None for wg in wgs):
        raise HubError("forbidden")
    connection.execute(
        "INSERT INTO hub.mailbox_use (credential_id, owner_id, wgs, set_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (credential_id) DO UPDATE SET wgs = EXCLUDED.wgs, set_at = EXCLUDED.set_at",
        (record_id, owner, Jsonb(sorted(set(wgs))), now),
    )


RELEASED = (
    f"SELECT {', '.join('c.' + column.strip() for column in IDENTITY_COLUMNS.split(','))}"
    " FROM hub.mailbox_use u JOIN hub.credential c ON c.id = u.credential_id AND c.kind = 'mailbox'"
    " WHERE u.wgs ? %s ORDER BY c.updated_at DESC, c.id DESC"
)


def released_to(connection: Connection, device: UUID, wg: UUID) -> list[dict[str, Any]]:
    """The records released to the workgroup that the device can open (module docstring)."""
    found = []
    for row in connection.execute(RELEASED, (str(wg),)).fetchall():
        identity = identity_of(row)
        if not is_eligible(connection, device, identity.owner.id):
            continue
        found.append({"id": str(identity.id), **answer(identity)})
        if len(found) == MAX_RELEASED:
            break
    return found
