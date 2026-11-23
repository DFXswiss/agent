"""Projection rebuilders of the orgs area (ADR-027 section 4: `<area>/projections.py:REBUILDERS`).

`REBUILDERS` maps a name to `rebuild(connection, config) -> int`: it runs inside the caller's transaction (the rebuild
CLI of DR-01 runs every discovered rebuilder in one transaction), recomputes its projections from the append-only
sources and returns the number of objects rebuilt. `orgs` rebuilds, for every org, `org_member`, `wg`, `wg_access`,
`wg_device`, the `org`/`wg` rows of `scope_epoch` and the derived columns of `org` (head, `platform_access`,
`rotation_pending`) from `membership_entry` and the platform log (02-data-model.md section 1.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.config import HubConfig
from agent_hub.orgs import logs, projection

Connection = psycopg.Connection[Any]
Rebuilder = Callable[[Connection, HubConfig], int]


def rebuild_org(connection: Connection, org_id: UUID, root_key: bytes) -> None:
    """Rebuild the projections of one org from its stored log."""
    org = logs.org_row(connection, org_id, lock=True)
    if org is None:
        raise LookupError("unknown org")
    platform = logs.platform_state(connection, root_key)
    stored = connection.execute(
        "SELECT seq, received_at, entry_hash FROM hub.membership_entry WHERE org_id = %s ORDER BY seq", (org_id,)
    ).fetchall()
    head = stored[-1]
    # The head columns are derived as well: verify the log up to its last stored entry.
    log = logs.verified_log(connection, logs.replace_head(org, head[0], head[2]), platform)
    projection.clear_org(connection, org_id, log.state.workgroups)
    projection.project_log(connection, org_id, log, {row[0]: row[1] for row in stored})
    projection.update_org(connection, org_id, log, platform, logs.platform_access(org.id, org.private, platform))


def rebuild_orgs(connection: Connection, config: HubConfig) -> int:
    """Rebuild every org; the number of orgs."""
    ids = [row[0] for row in connection.execute("SELECT id FROM hub.org ORDER BY id")]
    for org_id in ids:
        rebuild_org(connection, org_id, config.platform_root_key)
    return len(ids)


REBUILDERS: Mapping[str, Rebuilder] = {"orgs": rebuild_orgs}
