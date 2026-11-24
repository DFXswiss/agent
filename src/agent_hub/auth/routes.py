"""Routes of the authentication area (03-api.md section 4.1): passkey registration and login, session end, recovery
(`agent_hub.auth.recovery`), the removal of a passkey (`agent_hub.auth.passkeys_manage`) and the withdrawal of an
unfinished registration (`agent_hub.auth.jobs.abandon_registration`, KOR-22).

The handlers run their database work in a worker thread; refusals leave as `{"error": "<code>"}`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request

from agent_hub.auth import jobs, login, passkeys_manage, recovery, register, sessions
from agent_hub.auth.fields import Empty
from agent_hub.http.context import Context, CurrentPrincipal
from agent_hub.http.pipeline import source_address
from agent_hub.http.routing import HubRouter

router = HubRouter()


@router.post("/v1/passkeys/register/begin", auth="anon_or_browser", right=("anon", "user"))
async def register_begin(body: register.RegisterBegin, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(register.begin, context, body, principal)


@router.post("/v1/passkeys/register/finish", auth="anon_or_browser", right=("anon", "user"))
async def register_finish(
    body: register.RegisterFinish, context: Context, principal: CurrentPrincipal
) -> dict[str, Any]:
    return await asyncio.to_thread(register.finish, context, body, principal)


@router.post("/v1/passkeys/login/begin", auth="anon", right="anon")
async def login_begin(body: login.LoginBegin, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(login.begin, context, body)


@router.post("/v1/passkeys/login/finish", auth="anon", right="anon")
async def login_finish(body: login.LoginFinish, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(login.finish, context, body)


@router.post("/v1/session/end", auth="browser", right="user")
async def session_end(_body: Empty, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(sessions.end_session, context, principal)


def recovery_limit(request: Request) -> recovery.SourceLimit:
    """The per-source limit of `recovery/begin` of this hub process (kept in the app state)."""
    state = request.app.state
    limit: recovery.SourceLimit | None = getattr(state, "recovery_limit", None)
    if limit is None:
        limit = state.recovery_limit = recovery.SourceLimit()
    return limit


@router.post("/v1/recovery/begin", auth="anon", right="anon")
async def recovery_begin(body: recovery.RecoveryBegin, context: Context, request: Request) -> dict[str, Any]:
    source = source_address(request.scope)
    return await asyncio.to_thread(recovery.begin, context, body, source, recovery_limit(request))


@router.post("/v1/recovery/finish", auth="anon", right="anon")
async def recovery_finish(body: recovery.RecoveryFinish, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(recovery.finish, context, body)


@router.delete("/v1/passkeys/{credential_id}", auth="browser", right="user")
async def remove_passkey(credential_id: str, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(passkeys_manage.remove, context, principal, credential_id)


@router.delete("/v1/me/registration", auth="browser", right="user")
async def abandon_registration(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await asyncio.to_thread(jobs.abandon_registration, context, principal.principal_id)
