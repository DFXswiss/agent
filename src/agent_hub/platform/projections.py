"""Rebuild of the platform-log projections (02-data-model.md section 1.3, ADR-027 section 4: `REBUILDERS`).

`rebuild(connection)` derives the projections of `agent_hub.platform.projection` again from the stored platform log
(entries in `seq` order with their receive times): `platform_role` and `platform_policy` from scratch,
`org.platform_access` from `private` plus the latest `org_platform_access` or the switch (ADR-077), the state,
`certified_seq` and keys of platform devices, the platform invitation rows of the log and the certification marks.
Stored entries were verified when they were appended (`hub.platform_entry` is append-only), so the rebuild needs no
key. Run it inside one transaction.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

import psycopg

from agent_hub.platform import projection


def rebuild(connection: psycopg.Connection[Any]) -> int:
    """Replay the platform log into the projections; the number of entries replayed."""
    connection.execute("DELETE FROM hub.platform_role")
    connection.execute("DELETE FROM hub.platform_policy")
    connection.execute(
        "UPDATE hub.org SET platform_access = private, platform_access_grant = NULL"
        " WHERE platform_access <> private OR platform_access_grant IS NOT NULL"
    )
    rows = connection.execute("SELECT seq, entry_text, received_at FROM hub.platform_entry ORDER BY seq").fetchall()
    for seq, text, received_at in rows:
        body = cast(dict[str, object], json.loads(text)["body"])
        projection.apply(connection, body, int(seq), received_at)
    return len(rows)


REBUILDERS: Mapping[str, Callable[[psycopg.Connection[Any]], int]] = {"platform": rebuild}
