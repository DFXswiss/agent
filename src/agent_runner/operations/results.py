"""Results of original operations for duplicates (03-api.md section 9 "Result delivery", section 10
`operation.result`; 05-runner.md section 12 step 6).

- `keep(...)`: the inbox hands every verified `operation.result` event (any device of the workgroup; E1-E5 with the
  author device assigned to the workgroup at `lh`) to this store. The header's `ref` must name the operation of the
  body and the body must be a final result (`succeeded` with `result`, or `failed`/`rejected`/`denied`/`expired` with
  an `error`), else it is refused (`result_invalid`). One event per operation (the first one wins).
- `original_outcome(conn, storage_key, operation_id, wg_id=..., op_type=...)`: the final outcome of an operation of
  the same workgroup from the local row when it ran on this device (same type; `succeeded`/`failed`), else from its
  stored `operation.result` event of that workgroup (opened into memory); None while neither is final or known.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.operations import records
from agent_runner.operations.stored import open_stored

INVALID = "result_invalid"
ERROR_STATUSES = frozenset({"failed", "rejected", "denied", "expired"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CODE_RE = re.compile(r"^[a-z][a-z_]{0,63}$")


@dataclass(frozen=True)
class Outcome:
    status: str
    """`succeeded`, or the error status of the original (`failed`, `rejected`, `denied`, `expired`)."""
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def outcome_of(body: Mapping[str, Any]) -> Outcome | None:
    """The outcome of an `operation.result` body, None when it is not a well-formed final result."""
    status, result, error = body.get("status"), body.get("result"), body.get("error")
    connection = body.get("connection_of")
    if "connection_of" in body and not (isinstance(connection, str) and _UUID_RE.fullmatch(connection)):
        return None
    keys = set(body) - {"connection_of"}
    if status == "succeeded" and isinstance(result, dict) and keys == {"operation_id", "status", "result"}:
        return Outcome("succeeded", result=result)
    well_formed = isinstance(error, dict) and isinstance(error.get("code"), str) and _CODE_RE.fullmatch(error["code"])
    if status in ERROR_STATUSES and well_formed and keys == {"operation_id", "status", "error"}:
        assert isinstance(error, dict)
        return Outcome(str(status), error=error)
    return None


def valid(header: Mapping[str, object], body: Mapping[str, Any]) -> bool:
    ref = header.get("ref")
    return (
        isinstance(ref, str)
        and _UUID_RE.fullmatch(ref) is not None
        and body.get("operation_id") == ref
        and outcome_of(body) is not None
    )


async def keep(
    conn: AsyncConnection[Any],
    *,
    operation_id: str,
    event_id: str,
    org_id: str,
    wg_id: str,
    author_sig: str,
    sealed: Mapping[str, object],
) -> bool:
    cur = await conn.execute(
        "INSERT INTO runner.operation_result (operation_id, event_id, org_id, wg_id, author_sig, sealed)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (operation_id, event_id, org_id, wg_id, author_sig, Jsonb(dict(sealed))),
    )
    return cur.rowcount == 1


def _local(row: records.Operation) -> Outcome | None:
    if row.state == "succeeded" and row.result is not None:
        return Outcome("succeeded", result=row.result)
    if row.state == "failed" and row.error_code is not None:
        error: dict[str, Any] = {"code": row.error_code}
        if row.retryable is not None:
            error["retryable"] = row.retryable
        return Outcome("failed", error=error)
    return None


async def original_outcome(
    conn: AsyncConnection[Any], storage_key: bytes, operation_id: str, *, wg_id: str, op_type: str
) -> Outcome | None:
    """Only an original of the same workgroup counts (a guest receives plaintext of its own workgroup only, 00 §3
    B4), and a local original only of the same type; anything else the hub names is ignored."""
    row = await records.get(conn, operation_id)
    if row is not None and (row.wg_id != wg_id or row.type != op_type):
        return None
    if row is not None:
        found = _local(row)
        if found is not None:
            return found
    cur = await conn.execute(
        "SELECT wg_id::text, author_sig, sealed FROM runner.operation_result WHERE operation_id = %s AND wg_id = %s",
        (operation_id, wg_id),
    )
    stored = await cur.fetchone()
    if stored is None:
        return None
    body = await open_stored(conn, storage_key, str(stored[0]), stored[2], str(stored[1]))
    return None if body is None else outcome_of(body)
