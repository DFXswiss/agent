"""`POST /v1/session/end` and `POST /v1/session/renew` (03-api.md section 4.1).

`end_session` sets `ended_at` of the caller's browser session; the key resolver (`auth/keys.py:resolve_session`)
refuses later requests with the key as `401 key_revoked`; the open streams of the session end. An agent that calls it
is revoked (`agent_hub.agents.revocation`, decisions KOR-24).

`renew_session` keeps a browser signed in (rule E-40, ADR-099): the still valid session key signs the request (the
gate refused it otherwise: expired → `session_expired`, ended, passkey removed or user disabled → `key_revoked`), and
the hub moves `expires_at` of that same session to `SESSION_RENEW_SECONDS` from its clock (never earlier than it was).
The key, its statement and `created_at` stay; the passkey certificate of the login keeps its 12 h bound. Audited as
`auth.session_renew`. An agent has no browser session to renew and a key that does not expire: it gets its key ID
with `expires_at` null, nothing changes (the same answer status as a browser, `agent_hub.agents.scope`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from agent_hub.agents.revocation import revoke_agents
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.auth.keys import Principal
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from agent_hub.stream.publish import close_keys

SESSION_RENEW_SECONDS = 30 * 24 * 3600
"""How long a renewed browser session lasts from the renewal (ADR-099 section 2): a browser that is not used for this
long needs the passkey again."""


def end_session(context: HubContext, principal: Principal) -> dict[str, Any]:
    """End the caller's session (`auth.session_end`) and its open streams. Signed by an agent (KOR-24), the agent ends
    its own access: it is revoked (`agent.revoke`, the user is notified)."""
    user = cast(UUID, principal.principal_id)
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    if isinstance(agent, UUID):
        revoke_agents(context, user, agent)
        return {}
    now = context.clock()
    with transaction(context.pool) as connection:
        connection.execute(
            "UPDATE hub.browser_session SET ended_at = %s WHERE key_id = %s AND ended_at IS NULL",
            (now, principal.key_id),
        )
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=principal.principal_id,
                action="auth.session_end",
                result="ok",
                target_kind="browser_session",
                target_id=principal.key_id,
                detail={"key_id": principal.key_id},
            ),
        )
    close_keys(user, [cast(str, principal.key_id)])
    return {}


def renew_session(context: HubContext, principal: Principal) -> dict[str, Any]:
    """Renew the caller's browser session: `expires_at` becomes now + `SESSION_RENEW_SECONDS` (or stays, if later).
    A session ended between the gate and this update is `401 key_revoked`. An agent (admitted to every browser route,
    `agent_hub.agents.scope`) has no browser session and a key that does not expire: nothing changes, the answer is
    its key ID with `expires_at` null."""
    key_id = cast(str, principal.key_id)
    if principal.kind != "session":
        unchanged()
        return {"key_id": key_id, "expires_at": None}
    now = context.clock()
    with transaction(context.pool) as connection:
        row = connection.execute(
            "UPDATE hub.browser_session SET expires_at = GREATEST(expires_at, %s)"
            " WHERE key_id = %s AND ended_at IS NULL RETURNING expires_at",
            (now + timedelta(seconds=SESSION_RENEW_SECONDS), key_id),
        ).fetchone()
        if row is None:
            raise HubError("key_revoked")
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=principal.principal_id,
                action="auth.session_renew",
                result="ok",
                target_kind="browser_session",
                target_id=key_id,
                detail={"key_id": key_id},
            ),
        )
    return {"key_id": key_id, "expires_at": format_time(row[0])}
