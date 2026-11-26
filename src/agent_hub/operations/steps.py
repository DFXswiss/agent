"""Writing one step of an operation: the `operation_transition` row and the audit row `operation.<state>`.

Both the registration (`agent_hub.operations.register`) and the expiry job (`agent_hub.operations.jobs`) record their
steps here; the outbox base handler (HUB-10) writes the steps the runner reports in the same form. Audit details are
metadata only (`contracts/audit/operation.<state>.json`): type, error code, the original of a duplicate and the
runner's local check (registration only), the GitHub identity the operation acts with and the human who triggered it
(ADR-078 section 6; null when the registration named none). The audit `result` follows the decision: `rejected`,
`denied` and `duplicate` keep their name, `expired` is `failed`, everything else `ok`. A step the device reports is
the agent's (ADR-095 section 4): the registration and the outbox name the operation's session as actor
(`main_agent`/`subagent`) and its stored `triggered_by` as `on_behalf_of`; the expiry job writes as the hub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit

Connection = psycopg.Connection[Any]
AUDIT_RESULTS = {"rejected": "rejected", "denied": "denied", "duplicate": "duplicate", "expired": "failed"}
FINISHED_SIGNAL = "operation.finished"
"""Signal after an operation reached `succeeded`, `failed` or `expired` (ids: operation, session, task, org, state)."""
FINISHED_STATES = frozenset({"succeeded", "failed", "expired"})


@dataclass(frozen=True)
class OperationRef:
    """The identifiers of an operation that every step needs."""

    id: UUID
    org_id: UUID
    wg_id: UUID
    task_id: UUID
    session_id: UUID
    device_id: UUID
    type: str
    github_identity: UUID | None = None
    triggered_by: UUID | None = None


def identity_detail(github_identity: UUID | None, triggered_by: UUID | None) -> dict[str, str | None]:
    """The audit detail fields `github_identity` and `triggered_by` of every `operation.*` row (ADR-078 section 6)."""
    return {
        "github_identity": None if github_identity is None else str(github_identity),
        "triggered_by": None if triggered_by is None else str(triggered_by),
    }


def add_transition(
    connection: Connection, operation: UUID, state: str, error_code: str | None, *, at: datetime, by_id: UUID | None
) -> None:
    """Append the next `operation_transition` row."""
    connection.execute(
        "INSERT INTO hub.operation_transition (operation_id, seq, state, error_code, at, by_id) VALUES (%s,"
        " (SELECT COALESCE(MAX(seq), 0) + 1 FROM hub.operation_transition WHERE operation_id = %s), %s, %s, %s, %s)",
        (operation, operation, state, error_code, at, by_id),
    )


def audit_step(
    connection: Connection,
    operation: OperationRef,
    state: str,
    now: datetime,
    *,
    actor_kind: str,
    actor_id: UUID | None,
    error_code: str | None = None,
    duplicate_of: UUID | None = None,
    local_check: str | None = None,
    on_behalf_of: UUID | None = None,
) -> None:
    """The audit row `operation.<state>` (module docstring)."""
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=f"operation.{state}",
            result=AUDIT_RESULTS.get(state, "ok"),
            org_id=operation.org_id,
            wg_id=operation.wg_id,
            task_id=operation.task_id,
            session_id=operation.session_id,
            device_id=operation.device_id,
            target_kind="operation",
            target_id=str(operation.id),
            detail={
                "type": operation.type,
                "error_code": error_code,
                "duplicate_of": None if duplicate_of is None else str(duplicate_of),
                "local_check": local_check,
                **identity_detail(operation.github_identity, operation.triggered_by),
            },
            on_behalf_of=on_behalf_of,
        ),
    )
