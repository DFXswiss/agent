"""Outbox item `operation_transition` (03-api.md sections 6.1 and 9, 02-data-model.md section 2.5).

The operation was registered through `POST /v1/device/operations` (HUB-12); the outbox reports how its execution went.
`device:session`: the operation must belong to the pushing device (`404 not_found` for an unknown operation,
`403 forbidden` for another device's). Allowed steps: `accepted` → `executing`; `accepted`/`executing` → `succeeded` or
`failed`; `received`/`accepted`/`executing` → `expired`. The current state again changes nothing (a retry with another
`origin_seq`); every other step - in particular from a final state - is `400 request_invalid`. A `result_event_id` must
name the device's `operation.result` event of this operation (`ref` = operation), which the runner sends earlier in the
outbox.

Effects: the operation row (`state`, `error_code`, `retryable`, `result_event_id`; `started_at` = receive time of
`executing`, `finished_at` = receive time of a final state), an `operation_transition` row (`at` = device clock,
`by_id` = device), the release of the operation's dedupe slot on `failed` and `expired` (02-data-model.md section 2.5),
the audit row `operation.<state>` (actor: the operation's session as `main_agent`/`subagent` on behalf of the
operation's stored `triggered_by`, `device_id` the device, ADR-095 section 4; with the operation's `github_identity`
and `triggered_by`), the refresh of the
task's attention record (`agent_hub.attention.store`, ADR-076)
and the `operation` frame for the org's browsers after the commit. Pings to the
requesting session and other reactions are added by later areas as handlers of the same kind (ADR-030).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from agent_hub.attention import store as attention
from agent_hub.audit.agents import agent_kind
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.operations.steps import identity_detail
from agent_hub.outbox.base import Connection, ItemContext, moment
from agent_hub.outbox.bodies import OperationTransitionBody

ALLOWED_FROM = {
    "executing": frozenset({"accepted"}),
    "succeeded": frozenset({"accepted", "executing"}),
    "failed": frozenset({"accepted", "executing"}),
    "expired": frozenset({"received", "accepted", "executing"}),
}
RELEASES_SLOT = frozenset({"failed", "expired"})
ROW_COLUMNS = (
    "id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state, error_code, retryable, duplicate_of,"
    " attempt, params_event_id, result_event_id, created_at, decided_at, started_at, finished_at, github_identity,"
    " triggered_by"
)
ROW_FIELDS = (
    "operation_id",
    "org_id",
    "wg_id",
    "task_id",
    "session_id",
    "device_id",
    "actor_id",
    "type",
    "state",
    "error_code",
    "retryable",
    "duplicate_of",
    "attempt",
    "params_event_id",
    "result_event_id",
    "created_at",
    "decided_at",
    "started_at",
    "finished_at",
    "github_identity",
    "triggered_by",
)


def _json(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_time(value)
    return value


def operation_row(connection: Connection, operation_id: UUID | str) -> dict[str, Any]:
    """The operation as the `operation` frame carries it (`contracts/api/objects.json` `operation_row`)."""
    row = cast(
        tuple[object, ...],
        connection.execute(f"SELECT {ROW_COLUMNS} FROM hub.operation WHERE id = %s", (operation_id,)).fetchone(),
    )
    return {name: _json(value) for name, value in zip(ROW_FIELDS, row, strict=True)}


def _check_result_event(item: ItemContext, event_id: str, operation_id: UUID) -> None:
    row = item.connection.execute("SELECT kind, author_id, ref FROM hub.event WHERE id = %s", (event_id,)).fetchone()
    if row is None or tuple(row) != ("operation.result", item.device, str(operation_id)):
        raise HubError("request_invalid")


def handle(item: ItemContext, body: OperationTransitionBody) -> None:
    """Apply the reported operation step (module docstring)."""
    connection = item.connection
    found = connection.execute(
        "SELECT o.id, o.org_id, o.wg_id, o.task_id, o.session_id, o.device_id, o.type, o.state, o.duplicate_of,"
        " o.github_identity, o.triggered_by, s.depth FROM hub.operation o JOIN hub.session s ON s.id = o.session_id"
        " WHERE o.id = %s FOR UPDATE OF o",
        (body.operation_id,),
    ).fetchone()
    if found is None:
        raise HubError("not_found")
    operation, org, wg, task, session, device, kind, state, duplicate_of, github_identity, triggered_by, depth = found
    if device != item.device:
        raise HubError("forbidden")
    if state == body.state:
        return
    if state not in ALLOWED_FROM[body.state]:
        raise HubError("request_invalid")
    if body.result_event_id is not None:
        _check_result_event(item, body.result_event_id, operation)
    at = moment(body.at)
    final = body.state != "executing"
    connection.execute(
        "UPDATE hub.operation SET state = %s, error_code = COALESCE(%s, error_code),"
        " retryable = COALESCE(%s, retryable), result_event_id = COALESCE(%s, result_event_id),"
        " started_at = CASE WHEN %s THEN COALESCE(started_at, %s) ELSE started_at END,"
        " finished_at = CASE WHEN %s THEN %s ELSE finished_at END WHERE id = %s",
        (
            body.state,
            body.error_code,
            body.retryable,
            body.result_event_id,
            not final,
            item.now,
            final,
            item.now,
            operation,
        ),
    )
    connection.execute(
        "INSERT INTO hub.operation_transition (operation_id, seq, state, error_code, at, by_id) VALUES"
        " (%s, (SELECT COALESCE(MAX(seq), 0) + 1 FROM hub.operation_transition WHERE operation_id = %s), %s, %s, %s,"
        " %s)",
        (operation, operation, body.state, body.error_code, at, item.device),
    )
    if body.state in RELEASES_SLOT:
        connection.execute("DELETE FROM hub.dedupe_slot WHERE operation_id = %s", (operation,))
    append_audit(
        connection,
        AuditRow(
            at=item.now,
            actor_kind=agent_kind(depth),
            actor_id=session,
            action=f"operation.{body.state}",
            result="ok",
            org_id=org,
            wg_id=wg,
            task_id=task,
            session_id=session,
            device_id=item.device,
            target_kind="operation",
            target_id=str(operation),
            detail={
                "type": kind,
                "error_code": body.error_code,
                "duplicate_of": None if duplicate_of is None else str(duplicate_of),
                "local_check": None,
                **identity_detail(github_identity, triggered_by),
            },
            on_behalf_of=triggered_by,
        ),
    )
    item.effects.org_frame(org, "operation", operation_row(connection, operation))
    attention.refresh(connection, task, item.now, item.effects)
