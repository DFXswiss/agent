"""Creation of an organization (`POST /v1/orgs`, 03-api.md section 4.3, 04-crypto.md section 5.3, ADR-026).

`create_org` runs inside the caller's transaction:

1. the genesis entry is well formed and of type `genesis` (`log_genesis`), authored by the caller (`403 forbidden`);
2. idempotency: an org with the genesis' ID that the caller created with exactly this genesis is answered again with
   `replayed: true`; any other existing org, or a default workgroup ID that is already taken, is `409 id_conflict`;
3. `pub` is the caller's registered identity (`log_genesis`; the founder key L3 is pinned to it);
4. `private: true` needs `platform_invite_id` = an unused, unrevoked platform user invitation whose `registered_user`
   is the caller (`403 platform_invite_required`); it is consumed in the same transaction (direct SQL on `invite`) and
   the org gets `platform_access = true`. A non-private org gets its access from the platform log (normally `false`
   until a root-signed `org_platform_access`); `platform_invite_id` on a non-private genesis is `400 request_invalid`;
5. L1-L8 on the genesis, W3 and distribution: the founder's self-wraps of `OK_1` and `WK_1` (`agent_hub.log.batch`);
6. the events are exactly the profiles of the new org (`org.profile`, `wg.profile`, `member.profile`, each at least
   once, nothing else: `400 kind_not_allowed`/`request_invalid`), checked and stored through the events interface;
7. storage as for every append: org row, entry, projections (default workgroup with `write` for the founder), wraps,
   audit `log.genesis`.

The expiry of a platform invitation governs its registration (`register/begin`), not the genesis that follows within
the 24 h of the registration (decision HUB-05).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.invites import audit_invite_use
from agent_hub.crypto.encoding import canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.log.entries import check_log_entry
from agent_hub.orgs import logs, projection
from agent_hub.orgs.append import (
    Appender,
    AppendResult,
    check,
    hub_error,
    members,
    persist_range,
    store_batch_events,
)

Connection = psycopg.Connection[Any]
PROFILE_KINDS = frozenset({"org.profile", "wg.profile", "member.profile"})


@dataclass(frozen=True)
class Created:
    """The new org (or the replayed one) and, unless replayed, what was stored."""

    org_id: UUID
    default_wg: UUID
    replayed: bool
    result: AppendResult | None


def _genesis_body(genesis: object, appender: Appender) -> dict[str, Any]:
    try:
        body = check_log_entry(genesis)
    except CryptoError as error:
        raise hub_error(error) from None
    if body["type"] != "genesis":
        raise HubError("log_genesis")
    if body["by"] != appender.id:
        raise HubError("forbidden")
    return body


def _existing(connection: Connection, org_id: UUID, default_wg: UUID, genesis: object, appender: Appender) -> bool:
    """True for a replay of this caller's genesis; `id_conflict` for any other use of the IDs."""
    org = logs.org_row(connection, org_id, lock=True)
    if org is not None:
        stored = logs.entries(connection, org_id, limit=1)
        if org.created_by == UUID(appender.id) and canonical(stored[0]) == canonical(genesis):
            return True
        raise HubError("id_conflict")
    taken = connection.execute(
        "SELECT 1 FROM hub.wg WHERE id = ANY(%s::uuid[]) UNION ALL SELECT 1 FROM hub.org WHERE default_wg = %s"
        " UNION ALL SELECT 1 FROM hub.org WHERE id = %s",
        ([org_id, default_wg], default_wg, default_wg),
    ).fetchone()
    if taken is not None:
        raise HubError("id_conflict")
    return False


def _consume_platform_invite(
    connection: Connection, invite_id: str | None, appender: Appender, org_id: UUID, now: datetime
) -> None:
    row = None
    if invite_id is not None:
        row = connection.execute(
            "SELECT registered_user, used_at, revoked_at FROM hub.invite"
            " WHERE id = %s AND scope = 'platform' AND kind = 'user' FOR UPDATE",
            (invite_id,),
        ).fetchone()
    if row is None or row[0] is None or str(row[0]) != appender.id or row[1] is not None or row[2] is not None:
        raise HubError("platform_invite_required")
    connection.execute(
        "UPDATE hub.invite SET used_at = %s, used_by = %s, package_ct = NULL WHERE id = %s",
        (now, appender.id, invite_id),
    )
    audit_invite_use(
        connection,
        invite_id=str(invite_id),
        kind="user",
        actor_kind=appender.kind,
        actor_id=UUID(appender.id),
        at=now,
        org_id=org_id,
    )


def _check_profiles(events: Sequence[object]) -> None:
    kinds = set()
    for event in events:
        header = event.get("header") if isinstance(event, dict) else None
        if not isinstance(header, dict) or not isinstance(header.get("kind"), str):
            raise HubError("header_invalid")
        kinds.add(header["kind"])
    if not kinds <= PROFILE_KINDS:
        raise HubError("kind_not_allowed")
    if kinds != PROFILE_KINDS:
        raise HubError("request_invalid")


def create_org(
    connection: Connection,
    *,
    appender: Appender,
    genesis: object,
    wraps: Sequence[object],
    events: Sequence[object],
    platform_invite_id: str | None,
    now: datetime,
    root_key: bytes,
) -> Created:
    """Check and store a new org atomically (module docstring); the caller commits."""
    body = _genesis_body(genesis, appender)
    org_id, default_wg = UUID(body["org"]), UUID(body["default_wg"])
    if _existing(connection, org_id, default_wg, genesis, appender):
        return Created(org_id, default_wg, replayed=True, result=None)
    identity = logs.identity_keys(connection, appender.id)
    pub = cast(dict[str, str], body["pub"])
    if identity is None or identity != (pub["kem"], pub["sig"]):
        raise HubError("log_genesis")
    private = bool(body["private"])
    if private:
        _consume_platform_invite(connection, platform_invite_id, appender, org_id, now)
    elif platform_invite_id is not None:
        raise HubError("request_invalid")
    platform = logs.platform_state(connection, root_key)
    access = logs.platform_access(org_id, private, platform)
    batch = check(
        None, [genesis], wraps, appender=appender, platform=platform, now=now, access=access, founder=pub["sig"]
    )
    _check_profiles(events)
    log = batch.log
    connection.execute(
        "INSERT INTO hub.org (id, private, platform_access, default_wg, created_by, created_at, log_head_seq,"
        " log_head_hash, rotation_pending) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '[]')",
        (org_id, private, access, default_wg, appender.id, now, log.head.seq, log.head.hash),
    )
    persist_range(connection, org_id, batch, first=0, wraps=wraps, appender=appender, now=now)
    projection.update_org(connection, org_id, log, platform, access)
    stored = store_batch_events(connection, events, log, root_key=root_key, author=appender.id, now=now)
    users, devices = members(log.state)
    logs.remember(org_id, log)
    result = AppendResult(org_id, log.head, log, batch, stored, users=users, devices=devices)
    return Created(org_id, default_wg, replayed=False, result=result)
