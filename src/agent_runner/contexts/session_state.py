"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`; ADR-072 sections 4 and 7,
review KOR-33 nos. 15 and 16).

In the journal page's transaction, for main sessions:

- `idle`, `suspended`, `stopped`, `failed`: a checkpoint becomes due (`store.mark_due`) and the export loop is woken
  (the guest keeps the directories after an end, so the export still works);
- the restore part (`restore.observe`): `agent_starting` of a session that starts from a checkpoint sends its audit
  row `session.context_restored` (only now: the guest accepted the restore and the agent starts); `working`, `idle`
  or `suspended` closes its start window; `failed` with `agent_start_failed`/`sandbox_failed` inside that window
  marks its checkpoint as not to be tried again on this device. A restored session that fails later is not
  restored again automatically either way: sessions are final, the next prompt starts a new one (decisions/KOR-33).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from agent_runner.contexts import restore, store
from agent_runner.contexts.env import existing
from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession

CHECKPOINT_STATES = frozenset({"idle", "suspended", "stopped", "failed"})


async def observe(journal: Any, conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    if session.depth != 0:
        return
    env = journal.env
    await restore.observe(conn, env.outbox, session, body)
    if body.get("state") not in CHECKPOINT_STATES:
        return
    await store.mark_due(
        conn,
        session_id=session.id,
        task_id=session.task_id,
        org_id=session.org_id,
        wg_id=session.wg_id,
        at=datetime.fromtimestamp(env.wall(), UTC),
    )
    found = existing(env.context)
    if found is not None:
        found.wake.set()


OBSERVERS = [StateObserver("contexts.checkpoint", observe)]
