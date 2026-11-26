"""The audit rows of an export verify as the hub hashed them (DR-01, step `verify` of `agent-hub restore`): the chain
check of a restore reads every hashed column of a row, also `agent_id` (KOR-24) and `on_behalf_of` (KOR-52, ADR-095
section 4), which are hashed only when set."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit, verify_rows
from agent_hub.backup.restore import stored_audit
from agent_hub.backup.tables import encode_row, schema_tables
from tests.platform.hub.conftest import FIXED_NOW, uuid7


def test_exported_rows_of_agents_and_persons_verify(writer: psycopg.Connection[Any]) -> None:
    session = uuid7()
    plain = AuditRow(
        at=FIXED_NOW,
        actor_kind="main_agent",
        actor_id=session,
        action="session.requested",
        result="ok",
        session_id=session,
        target_kind="session",
        target_id=str(session),
        detail={"reason": None, "model_access": None, "model_id": None},
    )
    for row in (plain, replace(plain, on_behalf_of=uuid7()), replace(plain, agent_id=uuid7(), on_behalf_of=uuid7())):
        append_audit(writer, row)
    table = schema_tables(writer)["audit"]
    records = writer.execute(f"SELECT {', '.join(table.names)} FROM hub.audit ORDER BY seq").fetchall()
    stored = [stored_audit(encode_row(table, record)) for record in records]
    assert [item.row.on_behalf_of is not None for item in stored] == [False, True, True]
    assert stored[2].row.agent_id is not None
    assert verify_rows(stored, 1, None).ok
