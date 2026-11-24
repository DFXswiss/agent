"""Who triggered an operation or a session (ADR-083 section 2; KOR-46) - the interface for reading across workgroups
(KOR-45, A11).

`triggered_by` of `hub.operation` and `hub.session` is the person whose instruction the row follows from: for an
operation the acting user the runner stamped on the turn (`POST /v1/device/operations`, checked to be the author of a
prompt of the task with `wg:write`), for a main session the author of the prompt it was created for, for a child
session the triggering person of the `subagent.request` that created it. KOR-45 intersects that person's rights with
the task owner's; `None` means "only the task owner's rights" (an older runner, a turn with several authors or none,
an unknown ID).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

Connection = psycopg.Connection[Any]


def operation_trigger(connection: Connection, operation_id: UUID | str) -> UUID | None:
    """The person who triggered the operation, None when unknown (module docstring)."""
    row = connection.execute("SELECT triggered_by FROM hub.operation WHERE id = %s", (operation_id,)).fetchone()
    return None if row is None else row[0]


def session_trigger(connection: Connection, session_id: UUID | str) -> UUID | None:
    """The person who triggered the session, None when unknown (module docstring)."""
    row = connection.execute("SELECT triggered_by FROM hub.session WHERE id = %s", (session_id,)).fetchone()
    return None if row is None else row[0]
