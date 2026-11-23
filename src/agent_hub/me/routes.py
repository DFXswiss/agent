"""The signed-in user's own data (03-api.md section 4.1): `GET /v1/me`, the identity bundles and the settings.

- `GET /v1/me`: identity public keys, settings, the orgs of the membership projections (HUB-05; empty until then),
  platform roles, the number of unread notifications, the active passkeys (newest first) and `cred_holders`: the
  current `cred` epoch of the user (newest `scope_epoch`, null before the first) and the devices holding a wrap of it
  (IDs only, the user's own self-wrap left out; KOR-04). Wraps of the `cred` scope stay when a device leaves an org, so
  the user's browser can tell from this list that a device it cannot see in any verified log still holds the `CK` and
  rotate it (AUD-01 A-07).
- `GET /v1/me/bundles/recovery`: the recovery slot (unlock without PRF, 04-crypto.md section 5.4).
- `PUT /v1/me/bundles/{slot}`: `passkey:<own active credential>` (marks the passkey `prf_seen`: the browser could
  derive `KEK_i` from its PRF output) or `recovery` together with `new_recovery_id` (a new recovery code replaces
  the old one) and either a fresh passkey `assertion` of a `POST /v1/me/recovery/begin` ceremony or, without one,
  once after each finished recovery (else `403 forbidden`); `agent_hub.me.recovery_code` (KOR-01). Anything else is
  refused.
- `POST /v1/me/recovery/begin`: the ceremony for that assertion.
- `PUT /v1/me/settings`: `model_access`, `model_id` (null or a model of `/v1/config.models` allowed for the chosen
  access kind; `auto` allows every listed model) and `locale` (02-data-model.md section 2.1).
The read log of the GETs is written by the pipeline (`read_resource`).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Literal, cast

import psycopg
import psycopg.errors

from agent_core.signing import b64url_decode, b64url_encode
from agent_hub.agents.notify import account_change
from agent_hub.auth.ceremonies import credential_reference
from agent_hub.auth.fields import B64u16Id, BundleCt, Empty, ModelId, Strict, decode
from agent_hub.auth.keys import Principal
from agent_hub.config import ACCESS_KINDS, HubConfig
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.me import recovery_code
from agent_hub.me.recovery_code import RecoveryAssertion
from agent_hub.notifications.rules import Notification, publish_rows

router = HubRouter()

MAX_HOLDERS = 10_000
"""The contract's `maxItems` of `cred_holders.devices`."""

_PASSKEY_SLOT_RE = re.compile(r"passkey:([A-Za-z0-9_-]+)")


class BundlePut(Strict):
    bundle_ct: BundleCt
    new_recovery_id: B64u16Id | None = None
    assertion: RecoveryAssertion | None = None


class Settings(Strict):
    model_access: Literal["auto", "subscription", "api_key", "platform"]
    model_id: ModelId | None
    locale: Literal["de", "en"]


def _user(principal: Principal) -> uuid.UUID:
    return cast(uuid.UUID, principal.principal_id)


def settings_body(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"model_access": row[0], "model_id": row[1], "locale": row[2]}


def load_me(context: HubContext, user_id: uuid.UUID) -> dict[str, Any]:
    """The body of `GET /v1/me`."""
    with context.pool.connection() as connection, connection.transaction():
        # one snapshot: the org list and the CK holders must agree (KOR-04)
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        identity = connection.execute(
            "SELECT i.kem_pub, i.sig_pub, COALESCE(s.model_access, 'auto'), s.model_id, COALESCE(s.locale, 'en')"
            " FROM hub.user_identity i LEFT JOIN hub.user_settings s ON s.user_id = i.user_id WHERE i.user_id = %s",
            (user_id,),
        ).fetchone()
        if identity is None:
            raise HubError("not_found")
        orgs = connection.execute(
            "SELECT m.org_id, m.role, o.private, o.default_wg, o.log_head_seq, o.log_head_hash"
            " FROM hub.org_member m JOIN hub.org o ON o.id = m.org_id"
            " WHERE m.principal_id = %s AND m.kind = 'user' AND m.removed_seq IS NULL"
            " ORDER BY o.created_at DESC, o.id DESC",
            (user_id,),
        ).fetchall()
        roles = connection.execute(
            "SELECT role FROM hub.platform_role WHERE user_id = %s AND revoked_seq IS NULL ORDER BY role", (user_id,)
        ).fetchall()
        unread = connection.execute(
            # account notifications (KOR-24) are not counted: a web client of before KOR-24 cannot list them
            "SELECT count(*) FROM hub.notification WHERE recipient_id = %s AND read_at IS NULL AND org_id IS NOT NULL",
            (user_id,),
        ).fetchone()
        passkeys = connection.execute(
            "SELECT credential_id, created_at, last_used_at, prf_seen FROM hub.passkey"
            " WHERE user_id = %s AND removed_at IS NULL ORDER BY created_at DESC, credential_id DESC",
            (user_id,),
        ).fetchall()
        holders = cred_holders(connection, user_id)
    return {
        "user_id": str(user_id),
        "identity": {"kem_pub": b64url_encode(bytes(identity[0])), "sig_pub": b64url_encode(bytes(identity[1]))},
        "settings": settings_body(identity[2:]),
        "orgs": [
            {
                "org_id": str(org_id),
                "role": role,
                "private": private,
                "default_wg": str(default_wg),
                "log_head": {"seq": head_seq, "hash": head_hash},
            }
            for org_id, role, private, default_wg, head_seq, head_hash in orgs
        ],
        "platform_roles": [role for (role,) in roles],
        "unread_notifications": int(cast(tuple[int], unread)[0]),
        "passkeys": [
            {
                "credential_id": b64url_encode(bytes(credential_id)),
                "created_at": format_time(created_at),
                "last_used_at": format_time(last_used_at),
                "prf_seen": prf_seen,
            }
            for credential_id, created_at, last_used_at, prf_seen in passkeys
        ],
        "cred_holders": holders,
    }


def cred_holders(connection: psycopg.Connection[Any], user_id: uuid.UUID) -> dict[str, Any]:
    """The user's current `cred` epoch and the other recipients of its wraps, sorted (module docstring)."""
    row = connection.execute(
        "SELECT max(epoch) FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", (user_id,)
    ).fetchone()
    epoch = cast(tuple[int | None], row)[0]
    devices = connection.execute(
        "SELECT recipient_id FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND epoch = %s"
        " AND recipient_id <> %s ORDER BY recipient_id::text LIMIT %s",
        (user_id, epoch, user_id, MAX_HOLDERS),
    ).fetchall()
    return {"epoch": epoch, "devices": [str(device) for (device,) in devices]}


def load_recovery_bundle(context: HubContext, user_id: uuid.UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        row = connection.execute(
            "SELECT ct FROM hub.identity_bundle WHERE user_id = %s AND slot = 'recovery'", (user_id,)
        ).fetchone()
    if row is None:
        raise HubError("not_found")
    return {"bundle_ct": b64url_encode(bytes(row[0]))}


def put_bundle(
    context: HubContext, user_id: uuid.UUID, slot: str, body: BundlePut, agent_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Store one identity-bundle slot of the caller (`agent_id`: the request of an agent of the user, ADR-056)."""
    now = context.clock()
    ct = decode(body.bundle_ct)
    passkey = _PASSKEY_SLOT_RE.fullmatch(slot)
    credential_id = None if passkey is None else b64url_decode(passkey.group(1))
    if slot == "recovery":
        if body.new_recovery_id is None:
            raise HubError("request_invalid", detail={"pointers": ["/new_recovery_id"]})
    elif credential_id is None or body.new_recovery_id is not None or body.assertion is not None:
        raise HubError("request_invalid")
    ceremony = None if body.assertion is None else recovery_code.take(context, user_id, body.assertion, now)
    notices: list[Notification] = []
    with transaction(context.pool) as connection:
        if credential_id is None:
            proven_by = (
                None
                if ceremony is None
                else recovery_code.verify_assertion(
                    connection,
                    context,
                    user_id=user_id,
                    assertion=cast(RecoveryAssertion, body.assertion),
                    ceremony=ceremony,
                    now=now,
                )
            )
            recovery_code.rotate(
                connection, user_id, cast(str, body.new_recovery_id), proven_by, now, agent_id=agent_id
            )
            notices = account_change(connection, user_id, "recovery_replaced", now)
        else:
            owned = connection.execute(
                "UPDATE hub.passkey SET prf_seen = true WHERE credential_id = %s AND user_id = %s"
                " AND removed_at IS NULL RETURNING credential_id",
                (credential_id, user_id),
            ).fetchone()
            if owned is None:
                raise HubError("forbidden")
        replaced = connection.execute(
            "INSERT INTO hub.identity_bundle (user_id, slot, ct, updated_at) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (user_id, slot) DO UPDATE SET ct = EXCLUDED.ct, updated_at = EXCLUDED.updated_at"
            " RETURNING xmax <> 0",
            (user_id, slot, ct, now),
        ).fetchone()
        if credential_id is not None and replaced is not None and replaced[0]:
            # an agent overwrote the bundle slot of a passkey (KOR-24)
            target = credential_reference(credential_id)
            notices = account_change(connection, user_id, "bundle_replaced", now, target=target)
    publish_rows(notices)
    return {}


def check_model(config: HubConfig, settings: Settings) -> None:
    """`model_id` must be allowed for the access kind (any listed model for `auto`)."""
    if settings.model_id is None:
        return
    allowed = config.model_catalog.allowed
    kinds = ACCESS_KINDS if settings.model_access == "auto" else (settings.model_access,)
    if not any(settings.model_id in allowed[kind] for kind in kinds):
        raise HubError("params_invalid", detail={"pointers": ["/model_id"]})


def put_settings(context: HubContext, user_id: uuid.UUID, settings: Settings) -> dict[str, Any]:
    check_model(context.config, settings)
    with transaction(context.pool) as connection:
        row = connection.execute(
            "INSERT INTO hub.user_settings (user_id, model_access, model_id, locale, updated_at)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET model_access = EXCLUDED.model_access,"
            " model_id = EXCLUDED.model_id, locale = EXCLUDED.locale, updated_at = EXCLUDED.updated_at"
            " RETURNING model_access, model_id, locale",
            (user_id, settings.model_access, settings.model_id, settings.locale, context.clock()),
        ).fetchone()
    return settings_body(cast(tuple[Any, ...], row))


@router.get("/v1/me", auth="browser", right="user", read_resource="me")
async def get_me(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(load_me, context, _user(principal))


@router.get("/v1/me/bundles/recovery", auth="browser", right="user", read_resource="me.bundles.recovery")
async def get_recovery_bundle(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(load_recovery_bundle, context, _user(principal))


@router.put("/v1/me/bundles/{slot}", auth="browser", right="user")
async def put_bundle_slot(slot: str, body: BundlePut, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    agent_id = agent if isinstance(agent, uuid.UUID) else None
    return await asyncio.to_thread(put_bundle, context, _user(principal), slot, body, agent_id)


@router.post("/v1/me/recovery/begin", auth="browser", right="user")
async def begin_recovery_rotation(_body: Empty, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(recovery_code.begin, context, _user(principal))


@router.put("/v1/me/settings", auth="browser", right="user")
async def put_user_settings(body: Settings, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(put_settings, context, _user(principal), body)
