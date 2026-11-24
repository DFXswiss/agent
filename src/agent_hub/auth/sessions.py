"""`POST /v1/session/end` (03-api.md section 4.1): sets `ended_at` of the caller's browser session; the key resolver
(`auth/keys.py:resolve_session`) refuses later requests with the key as `401 key_revoked`; the open streams of the
session end. An agent that calls it is revoked (`agent_hub.agents.revocation`, decisions KOR-24)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from agent_hub.agents.revocation import revoke_agents
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction
from agent_hub.http.context import HubContext
from agent_hub.stream.publish import close_keys


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
