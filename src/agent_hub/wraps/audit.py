"""The audit row of submitted key wraps (`wrap.put`, ADR-063 section 2): one row per request of a user
(`POST /v1/wraps`) or a device (`POST /v1/device/wraps`) with the number stored and the number ignored."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.wraps.rules import Stored


def audit_wraps(
    connection: psycopg.Connection[Any], stored: Stored, *, actor_kind: str, actor_id: UUID, now: datetime
) -> None:
    """Append `wrap.put` for the wraps of one request (inside its transaction)."""
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor_kind,
            actor_id=actor_id,
            device_id=actor_id if actor_kind == "device" else None,
            action="wrap.put",
            result="ok",
            detail={"stored": stored.stored, "ignored": stored.ignored},
        ),
    )
