"""Revocation of agents (11-agent-access.md section 5.3, ADR-056 section 5): `DELETE /v1/me/agents/{id}`,
`DELETE /v1/me/agents` and `POST /v1/session/end` signed by an agent (it ends its own access, decisions KOR-24).

In one transaction the agents are revoked (state, time, sealed identity deleted, audit `agent.revoke`) and, when an
agent did it, the user is notified once per revoked agent (`agent_revoked:<acting agent>:<revoked agent>`). After the
commit the notifications are published, the open streams of the revoked keys are ended in every hub process and
the device of a revoked session orchestrator gets the hint `session.orchestrator` (the guest ends it).
"""

from __future__ import annotations

from uuid import UUID

from agent_hub.agents import orchestrator, store
from agent_hub.agents.keys import key_id_of
from agent_hub.agents.notify import account_change
from agent_hub.db.pool import transaction
from agent_hub.http.context import HubContext
from agent_hub.notifications.rules import publish_rows
from agent_hub.stream.publish import close_keys


def revoke_agents(context: HubContext, user: UUID, agent_id: UUID | None) -> list[UUID]:
    """Revoke the agent `agent_id` of the user (`404 not_found` for an unknown or foreign one), or every confirmed or
    active agent of the user for None; the IDs revoked now (an agent revoked before is not repeated)."""
    now = context.clock()
    with transaction(context.pool) as connection:
        if agent_id is None:
            revoked = store.revoke_all(connection, user, now)
        else:
            revoked = [agent_id] if store.revoke(connection, user, agent_id, now) else []
        notices = [
            notice
            for agent in revoked
            for notice in account_change(connection, user, "agent_revoked", now, target=agent)
        ]
        hints = orchestrator.session_hints(connection, revoked)
    publish_rows(notices)
    close_keys(user, [key_id_of(agent) for agent in revoked])
    orchestrator.publish_hints(hints)
    return revoked
