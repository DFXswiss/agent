"""Storing and listing key wraps (04-crypto.md sections 4.2 and 6.3, 03-api.md section 4.3).

`store_wraps` applies W3 to every wrap with the hub's own verified log state: for an `org`/`wg` scope the log of the
scope's org (`agent_hub.log.wraps.check_wrap`), for the `cred` scope of a user the logs of every org in which that
user or the wrapper is an active member and the user's current CK epoch (`check_cred_wrap`); the wrapper must be an
active principal and the decertified platform devices are no recipients. A request is all or nothing: one wrap that
violates W1/W2 refuses the whole request (`400 wrap_unauthorized`). A wrap
for a slot that already holds a wrap is ignored (wraps are insert-only, 02-data-model.md section 2.2) and counted in
`ignored`.

`wraps_for` lists the wraps stored for one recipient (`GET /v1/wraps`, `GET /v1/device/wraps`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time, require_keys
from agent_hub.crypto.errors import CryptoError, fail
from agent_hub.crypto.ids import is_uuid7
from agent_hub.errors import HubError
from agent_hub.log.platform import PlatformState
from agent_hub.log.state import ORG, WG
from agent_hub.log.verify import VerifiedLog
from agent_hub.log.wraps import CRED, WRAP_KEYS, WrapSlot, check_cred_wrap, check_wrap
from agent_hub.orgs import logs

Connection = psycopg.Connection[Any]


@dataclass(frozen=True)
class Stored:
    """The outcome of `store_wraps`: counts and the recipients of the newly stored wraps."""

    stored: int
    ignored: int
    recipients: frozenset[str]


def scope_org(connection: Connection, scope: str, scope_id: str) -> UUID | None:
    """The org of an `org`/`wg` scope, None if unknown."""
    if scope == ORG:
        row = connection.execute("SELECT id FROM hub.org WHERE id = %s", (scope_id,)).fetchone()
    else:
        row = connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (scope_id,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def cred_epoch(connection: Connection, owner: str) -> int | None:
    """The current CK epoch of a user, None before the first one."""
    row = connection.execute(
        "SELECT max(epoch) FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", (owner,)
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


class _Logs:
    """Verified logs loaded once per request."""

    def __init__(self, connection: Connection, root_key: bytes) -> None:
        self.connection = connection
        self.platform: PlatformState | None = logs.platform_state(connection, root_key)
        self._logs: dict[UUID, VerifiedLog] = {}

    def of(self, org_id: UUID) -> VerifiedLog:
        found = self._logs.get(org_id)
        if found is None:
            org = cast(logs.OrgRow, logs.org_row(self.connection, org_id, share=True))
            found = logs.verified_log(self.connection, org, self.platform)
            self._logs[org_id] = found
        return found

    def excluded(self, log: VerifiedLog) -> frozenset[str]:
        return frozenset() if self.platform is None else self.platform.excluded(log.state)

    def of_members(self, *principals: str) -> list[VerifiedLog]:
        ids = sorted({org.id for principal in principals for org in logs.member_orgs(self.connection, principal)})
        return [self.of(org_id) for org_id in ids]


def _scope_fields(wrap: object) -> tuple[str, str]:
    data = require_keys(wrap, WRAP_KEYS)
    scope, scope_id = data["scope"], data["scope_id"]
    if scope not in (ORG, WG, CRED) or not is_uuid7(scope_id):
        fail("header_invalid")
    return str(scope), str(scope_id)


def check_one(connection: Connection, loaded: _Logs, wrap: object, wrapper: str) -> WrapSlot:
    """W3 for one wrap (refusals as `CryptoError`)."""
    scope, scope_id = _scope_fields(wrap)
    if scope == CRED:
        epoch = cred_epoch(connection, scope_id)
        if epoch is None:
            fail("wrap_unauthorized")
        org_logs = loaded.of_members(scope_id, wrapper)
        excluded = frozenset().union(*(loaded.excluded(log) for log in org_logs))
        return check_cred_wrap(wrap, wrapper, org_logs, epoch, excluded, require_active=True)
    org_id = scope_org(connection, scope, scope_id)
    if org_id is None:
        fail("wrap_unauthorized")
    log = loaded.of(org_id)
    return check_wrap(wrap, wrapper, log, loaded.excluded(log), require_active=True)


def store_wraps(
    connection: Connection, wraps: Sequence[object], *, wrapper: str, now: datetime, root_key: bytes
) -> Stored:
    """Check every wrap (W3) and store the new ones; refusals as `HubError`."""
    loaded = _Logs(connection, root_key)
    try:
        slots = [check_one(connection, loaded, wrap, wrapper) for wrap in wraps]
    except CryptoError as error:
        code = error.code
        raise HubError(code if code in ("header_invalid", "signature_invalid") else "wrap_unauthorized") from None
    stored, recipients = 0, set()
    for slot, wrap in zip(slots, wraps, strict=True):
        data = cast(dict[str, Any], wrap)
        inserted = connection.execute(
            "INSERT INTO hub.key_wrap (scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                slot.scope,
                slot.scope_id,
                slot.epoch,
                slot.recipient,
                b64u_decode(data["wrap"]),
                wrapper,
                b64u_decode(data["sig"]),
                now,
            ),
        ).fetchone()
        if inserted is not None:
            stored += 1
            recipients.add(slot.recipient)
    return Stored(stored=stored, ignored=len(wraps) - stored, recipients=frozenset(recipients))


def wraps_for(
    connection: Connection,
    recipient: UUID | str,
    *,
    scope: str | None = None,
    scope_id: str | None = None,
    epoch: int | None = None,
) -> list[dict[str, Any]]:
    """The stored wraps of a recipient (optionally of one scope / scope ID / epoch), ordered by slot."""
    rows = connection.execute(
        "SELECT scope, scope_id, epoch, recipient_id, wrap, sig, wrapped_by, created_at FROM hub.key_wrap"
        " WHERE recipient_id = %s AND (%s::text IS NULL OR scope = %s) AND (%s::uuid IS NULL OR scope_id = %s)"
        " AND (%s::integer IS NULL OR epoch = %s) ORDER BY scope, scope_id, epoch",
        (recipient, scope, scope, scope_id, scope_id, epoch, epoch),
    )
    return [
        {
            "scope": row[0],
            "scope_id": str(row[1]),
            "epoch": row[2],
            "recipient_id": str(row[3]),
            "wrap": b64u_encode(bytes(row[4])),
            "sig": b64u_encode(bytes(row[5])),
            "wrapped_by": str(row[6]),
            "created_at": format_time(row[7]),
        }
        for row in rows
    ]
