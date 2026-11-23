"""`POST /v1/session/end` (03-api.md section 4.1): sets `ended_at` of the caller's browser session; the key resolver
(`auth/keys.py:resolve_session`) refuses later requests with the key as `401 key_revoked`."""

from __future__ import annotations

from typing import Any

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction
from agent_hub.http.context import HubContext


def end_session(context: HubContext, principal: Principal) -> dict[str, Any]:
    """End the caller's session (`auth.session_end`)."""
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
    return {}
