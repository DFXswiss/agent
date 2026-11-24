"""Projections of the platform log (02-data-model.md sections 1.3, 2.1 and 2.2).

`apply(connection, body, seq, at)` updates the hub tables for one platform-log entry, in the transaction that appends
it (`at` = its receive time). The same function replays the log in `projections.rebuild`, so incremental state and
rebuilt state agree:

- `device_invite`, `user_invite`: an `invite` row (scope `platform`, kind `device`/`user`, `created_by` NULL for the
  operator) with the entry's `pk_inv` (ADR-043: `register/finish` checks `inv_sig` under it);
- `device_certify`: `principal` and `device` row of kind `platform` in state `active` with `certified_seq` and the
  certified keys (a new row also takes version, OS, architecture and capacity of the entry; an existing row keeps the
  values its heartbeats report), the enrollment's `certified_at`, the device invitation's `used_at`/`used_by`;
- `device_decertify`: device state `decertified` (the gate refuses the device with `key_revoked`, HUB-08);
- `role_grant` / `role_revoke`: `platform_role` (`granted_seq`, `revoked_seq`);
- `org_platform_access`: `org.platform_access_grant` and `org.platform_access` (a private org keeps `true`, ADR-026);
- `platform_policy` (ADR-077): `platform_policy`; the switch `open_platform_access` derives `org.platform_access` of
  every org again (`private OR COALESCE(platform_access_grant, on)`: an explicit `org_platform_access` wins);
- `org_operator_subscriptions`: nothing (the runner and the clients read it from the verified platform log, ADR-077);
- `root`, `user_certify`, `user_decertify`: nothing (the certifications of users are read from the verified platform
  log, ADR-060).

`mark_rotation_pending` is an effect of a new decertification, not a projection (rebuild leaves `rotation_pending`
alone): every org in which the device is assigned to a workgroup or a member gets the scopes the device could read
(`wg` per assigned workgroup and the `org` scope) in `org.rotation_pending`; the next admin browser unassigns the device
and rotates them (04-crypto.md section 4.7).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.crypto.encoding import b64u_decode
from agent_hub.log.rotation import as_json
from agent_hub.log.state import ORG, WG, Scope

Projector = Callable[[psycopg.Connection[Any], dict[str, object], int, datetime], None]


def _invite(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    kind = "device" if body["type"] == "device_invite" else "user"
    connection.execute(
        "INSERT INTO hub.invite (id, scope, kind, created_by, created_at, expires_at, pk_inv)"
        " VALUES (%s, 'platform', %s, NULL, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (body["invite_id"], kind, at, body["expires"], b64u_decode(cast(str, body["pk_inv"]), 32)),
    )


def _certify(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    device = body["device"]
    pub = cast(dict[str, str], body["pub"])
    connection.execute(
        "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s) ON CONFLICT (id) DO NOTHING",
        (device, at),
    )
    connection.execute(
        "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, certified_seq,"
        " agent_version, os, arch, capacity, updated_at)"
        " VALUES (%s, 'platform', %s, %s, NULL, 'active', %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET kem_pub = EXCLUDED.kem_pub, sig_pub = EXCLUDED.sig_pub, state = 'active',"
        " certified_seq = EXCLUDED.certified_seq, updated_at = GREATEST(hub.device.updated_at, EXCLUDED.updated_at)"
        " WHERE hub.device.kind = 'platform'",
        (
            device,
            b64u_decode(pub["kem"], 32),
            b64u_decode(pub["sig"], 32),
            at,
            seq,
            body["agent_version"],
            body["os"],
            body["arch"],
            Jsonb(body["capacity"]),
            at,
        ),
    )
    connection.execute(
        "UPDATE hub.device_enrollment SET certified_at = %s WHERE invite_id = %s AND device_id = %s",
        (at, body["invite_id"], device),
    )
    connection.execute(
        "UPDATE hub.invite SET used_at = COALESCE(used_at, %s), used_by = COALESCE(used_by, %s)"
        " WHERE id = %s AND scope = 'platform'",
        (at, device, body["invite_id"]),
    )


def _decertify(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    connection.execute(
        "UPDATE hub.device SET state = 'decertified', updated_at = GREATEST(updated_at, %s)"
        " WHERE id = %s AND kind = 'platform'",
        (at, body["device"]),
    )


def _role(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    if body["type"] == "role_grant":
        connection.execute(
            "INSERT INTO hub.platform_role (user_id, role, granted_seq, revoked_seq) VALUES (%s, %s, %s, NULL)"
            " ON CONFLICT (user_id, role) DO UPDATE SET granted_seq = EXCLUDED.granted_seq, revoked_seq = NULL",
            (body["subject"], body["role"], seq),
        )
    else:
        connection.execute(
            "UPDATE hub.platform_role SET revoked_seq = %s WHERE user_id = %s AND role = %s",
            (seq, body["subject"], body["role"]),
        )


OPEN_ACCESS = "open_platform_access"


def _org_access(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    connection.execute(
        "UPDATE hub.org SET platform_access_grant = %s, platform_access = (private OR %s) WHERE id = %s",
        (body["granted"], body["granted"], body["org"]),
    )


def _policy(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    connection.execute(
        'INSERT INTO hub.platform_policy (name, "on", seq) VALUES (%s, %s, %s)'
        ' ON CONFLICT (name) DO UPDATE SET "on" = EXCLUDED."on", seq = EXCLUDED.seq',
        (body["name"], body["on"], seq),
    )
    if body["name"] == OPEN_ACCESS:
        connection.execute(
            "UPDATE hub.org SET platform_access = (private OR COALESCE(platform_access_grant, %s))"
            " WHERE platform_access IS DISTINCT FROM (private OR COALESCE(platform_access_grant, %s))",
            (body["on"], body["on"]),
        )


def _nothing(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    return None


PROJECTORS: dict[str, Projector] = {
    "root": _nothing,
    "device_invite": _invite,
    "user_invite": _invite,
    "device_certify": _certify,
    "device_decertify": _decertify,
    "role_grant": _role,
    "role_revoke": _role,
    "org_platform_access": _org_access,
    "user_certify": _nothing,
    "user_decertify": _nothing,
    "platform_policy": _policy,
    "org_operator_subscriptions": _nothing,
}


def apply(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, at: datetime) -> None:
    """Project one verified entry."""
    PROJECTORS[cast(str, body["type"])](connection, body, seq, at)


def affected_scopes(connection: psycopg.Connection[Any], device: str) -> dict[str, set[Scope]]:
    """Per org (text ID) the scopes a platform device can read: its assigned workgroups and the org scope."""
    found: dict[str, set[Scope]] = {}
    assigned = connection.execute(
        "SELECT wg.org_id, wg_device.wg_id FROM hub.wg_device JOIN hub.wg ON wg.id = wg_device.wg_id"
        " WHERE wg_device.device_id = %s AND wg_device.unassigned_seq IS NULL",
        (device,),
    )
    for org, wg in assigned:
        found.setdefault(str(org), set()).update({(WG, str(wg)), (ORG, str(org))})
    member = connection.execute(
        "SELECT org_id FROM hub.org_member WHERE principal_id = %s AND kind = 'device' AND removed_seq IS NULL",
        (device,),
    )
    for (org,) in member:
        found.setdefault(str(org), set()).add((ORG, str(org)))
    return found


def mark_rotation_pending(connection: psycopg.Connection[Any], device: str) -> list[str]:
    """Add the scopes of a decertified device to `rotation_pending` of every affected org; the org IDs."""
    affected = affected_scopes(connection, device)
    for org, scopes in sorted(affected.items()):
        row = connection.execute("SELECT rotation_pending FROM hub.org WHERE id = %s FOR UPDATE", (org,)).fetchone()
        if row is None:
            continue
        current = {(str(item["scope"]), str(item["scope_id"])) for item in row[0]}
        merged = as_json(frozenset(current | scopes))
        connection.execute("UPDATE hub.org SET rotation_pending = %s WHERE id = %s", (Jsonb(merged), org))
    return sorted(affected)
