"""Routes of the accounts area (ADR-060, 03-api.md section 4.1): the admissible identity of an account.

- `GET /v1/users/{user}/identity`: the public identity of `user` with its anchor in the platform log
  (`agent_hub.users.identity`);
- `GET /v1/users/lookup?code=`: the same for the account with this account code (the code every user sees in its
  settings; no search by name, no list of accounts);
- `GET /v1/me/invitees`: the accounts registered through the caller's own platform invitations;
- `GET /v1/platform/users/{user}` (root-signed, the operator CLI): the registered identity of an active user for
  `agent-hub platform certify-user` (`404 not_found` otherwise).

Every signed-in user (and its agent, ADR-056) may ask: admitting needs an admin of an org, which the membership-log
rule enforces at the hub and in every reader, and the answers contain public keys only. An unknown, disabled or
unanchored account is `404 identity_unproven` alike. The pipeline writes the read log (`read.user.identity`,
`read.user.lookup`, `read.me.invitees`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.identity import parse_account_code
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import logs
from agent_hub.users import identity
from agent_hub.workgroups.routes import path_id

router = HubRouter()


def _found(answer: dict[str, Any] | None) -> dict[str, Any]:
    if answer is None:
        raise HubError("identity_unproven")
    return answer


def _identity(context: HubContext, user: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        platform = logs.platform_state(connection, context.config.platform_root_key)
        return _found(identity.proven_identity(connection, platform, user))


def _lookup(context: HubContext, code: str) -> dict[str, Any]:
    with context.pool.connection() as connection:
        platform = logs.platform_state(connection, context.config.platform_root_key)
        return _found(identity.identity_by_code(connection, platform, code))


def _invitees(context: HubContext, user: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        platform = logs.platform_state(connection, context.config.platform_root_key)
        return {"invitees": identity.invitees(connection, platform, user)}


@router.get("/v1/users/lookup", auth="browser", right="user", read_resource="user.lookup")
async def get_user_lookup(context: Context, principal: CurrentPrincipal, *, code: str = "") -> dict[str, Any]:
    try:
        parsed = parse_account_code(code)
    except CryptoError as error:
        raise HubError(error.code) from None
    return await asyncio.to_thread(_lookup, context, parsed)


@router.get("/v1/users/{user}/identity", auth="browser", right="user", read_resource="user.identity")
async def get_user_identity(context: Context, principal: CurrentPrincipal, user: str) -> dict[str, Any]:
    try:
        target = path_id(user)
    except HubError:
        raise HubError("identity_unproven") from None
    return await asyncio.to_thread(_identity, context, target)


@router.get("/v1/me/invitees", auth="browser", right="user", read_resource="me.invitees")
async def get_me_invitees(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(_invitees, context, cast(UUID, principal.principal_id))


def _registered(context: HubContext, user: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        platform = logs.platform_state(connection, context.config.platform_root_key)
        found = identity.registered_identity(connection, platform, user)
    if found is None:
        raise HubError("not_found")
    return found


@router.get("/v1/platform/users/{user}", auth="root", right="root", read_resource="platform.user")
async def get_platform_user(context: Context, user: str) -> dict[str, Any]:
    return await asyncio.to_thread(_registered, context, path_id(user))
