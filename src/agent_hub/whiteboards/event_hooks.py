"""The in-transaction post-processor of whiteboard events (`agent_hub.events.hooks`, ADR-027 section 4).

For the newly stored events of one `append_events` call it projects every whiteboard event in call order
(`agent_hub.whiteboards.projection`) and, for an entry that this call created and that was sent with `notify:true`,
writes the notification rows, the session pings and the audit row `whiteboard.notify`
(`agent_hub.notifications.rules.notify_whiteboard`) - all in the storing transaction, so the projection, the
notifications and the event commit or roll back together. The live frames and `ping` hints follow after the commit
(`agent_hub.notifications.signals`, signal `event.appended`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from agent_hub.events.hooks import Hook
from agent_hub.events.store import StoredEvent
from agent_hub.notifications import rules
from agent_hub.whiteboards import projection


def project(connection: psycopg.Connection[Any], stored: list[StoredEvent]) -> None:
    """Project the whiteboard events of one call and fan out the notified new entries (module docstring)."""
    flags = {item.id: item.notify for item in stored if item.kind in projection.KINDS}
    if not flags:
        return
    for event in projection.board_events(connection, list(flags)):
        notify = flags[str(event.id)]
        if projection.apply(connection, event, notify=notify) and notify:
            rules.notify_whiteboard(connection, event)


HOOKS: Sequence[Hook] = (project,)
