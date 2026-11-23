"""Journal handler `operation.request` (`<area>/journal.py:HANDLERS`, ADR-027 section 4; 03-api.md sections 7.2 and
8.1; 05-runner.md section 12 step 2; contract change GST-07).

The guest journals every `request_operation` call as `operation.request {pending_id, operation_id, type, params,
idempotency_key?, requested_at, local_check?, local_error?}`. The handler runs inside the journal page's transaction
(`sessions.consume`): for a session that runs on this VM it records the operation in state `received`
(`records.insert`, idempotent on the operation ID, so a re-read page records nothing twice) and sends `NOTIFY
runner_operation`, which reaches the pipeline's listener after the commit. Everything else - checks, registration,
execution, the answer - happens in the pipeline, never in the journal transaction.

`local_check:"rejected"` is binding: the guest already answered `rejected` with its `local_error`, so the runner
registers the operation as rejected with that error and never executes it. A rejection without a well-formed error
is recorded as `params_invalid`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations import records
from agent_runner.operations.extensions import known_code
from agent_runner.sessions.journal import JournalRecordInvalid

CHANNEL = "runner_operation"
KIND = "operation.request"
KEYS = frozenset(
    {"pending_id", "operation_id", "type", "params", "idempotency_key", "requested_at", "local_check", "local_error"}
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CODE_RE = re.compile(r"^[a-z][a-z_]{0,63}$")
_TIME_RE = re.compile(r"^[0-9T:.Z-]{1,40}$")
_POINTER_RE = re.compile(r"^(/[^/]*)*$")
_UNSTORABLE_RE = re.compile("[\\x00\\ud800-\\udfff]")
MAX_DEPTH = 64


def storable(value: object, depth: int = 0) -> bool:
    """No NUL character or lone surrogate anywhere (PostgreSQL `jsonb` refuses them; a refused record must not stall
    the VM's journal) and a bounded nesting depth."""
    if depth > MAX_DEPTH:
        return False
    if isinstance(value, str):
        return _UNSTORABLE_RE.search(value) is None
    if isinstance(value, dict):
        return all(storable(k, depth + 1) and storable(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return all(storable(item, depth + 1) for item in value)
    return True


def _uuid(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise JournalRecordInvalid(KIND)
    return value


def local_error(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """The guest's binding error of a `local_check:"rejected"` request, None for a request that passed."""
    check = body.get("local_check", "passed")
    if check not in ("passed", "rejected"):
        raise JournalRecordInvalid(KIND)
    if check == "passed":
        return None
    error = body.get("local_error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        return {"code": "params_invalid"}
    found: dict[str, Any] = {"code": known_code(code, "params_invalid")}
    assert isinstance(error, dict)
    pointers = error.get("pointers")
    if (
        isinstance(pointers, list)
        and pointers
        and all(isinstance(p, str) and len(p) <= 256 and _POINTER_RE.fullmatch(p) for p in pointers[:32])
    ):
        found["pointers"] = pointers[:32]
    return found


def parse(body: Mapping[str, Any]) -> dict[str, Any]:
    """The checked fields of an `operation.request` body (`JournalRecordInvalid` for a malformed one)."""
    op_type, params, requested_at = body.get("type"), body.get("params"), body.get("requested_at")
    key = body.get("idempotency_key")
    valid = (
        set(body) <= KEYS
        and isinstance(op_type, str)
        and 1 <= len(op_type) <= 128
        and isinstance(params, dict)
        and isinstance(requested_at, str)
        and _TIME_RE.fullmatch(requested_at) is not None
        and (key is None or (isinstance(key, str) and 1 <= len(key) <= 128))
        and storable(dict(body))
    )
    if not valid:
        raise JournalRecordInvalid(KIND)
    return {
        "pending_id": _uuid(body, "pending_id"),
        "operation_id": _uuid(body, "operation_id"),
        "op_type": op_type,
        "params": params,
        "requested_at": requested_at,
        "idempotency_key": key,
        "local_error": local_error(body),
    }


async def operation_request(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    fields = parse(record.body)
    stored = await records.insert(
        conn,
        session_id=session.id,
        task_id=session.task_id,
        wg_id=session.wg_id,
        org_id=session.org_id,
        vm_id=journal.vm.id,
        **fields,
    )
    if stored:
        await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))


HANDLERS: dict[str, Callable[..., Any]] = {KIND: operation_request}
