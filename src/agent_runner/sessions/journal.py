"""Journal handlers of the session area (`<area>/journal.py:HANDLERS`, ADR-027 section 4; 03-api.md sections 6.1,
7.2 and 10; 05-runner.md sections 5.3-5.5, 6.1, 6.4, 10.4; 04-crypto.md section 5.12).

The journal loop (`sessions/consume.py`) hands every record of a VM's guest journal to the handler of its kind, in
journal order, inside the transaction that also stores the VM's `guest_cursor`; the guest is acknowledged only after
that commit. A record is only acted upon for a session this device runs **on this VM** (the VM is untrusted towards
the host, 00-overview.md section 3 B4: a guest never speaks for another VM's sessions).

- `session.state`: `session_transition` through `records.advance` with the device time (`prompt_accepted` ->
  `working`; a repeated state with a new `claude_session_id` only updates the ID, decisions/GST-05.md item 8);
  `failed` (only with a reason a guest can know, `GUEST_FAILURES`) also writes a `session.status` event
  (`events.fail`); `prompt_refused` writes the audit `session.prompt_refused`; then the observers of
  `session_state.py:OBSERVERS`.
- `session.output`, `session.tool`, `session.hook`, `session.file`, `session.terminal`: device event of the same kind
  (wg scope, the session's task and session in the header, the record's time when it is a valid contract time),
  sealed at the first push with the current epoch key of the workgroup and the device key (`sync.seal`); size limits
  of E8 (terminal 64 KiB; a well-formed terminal record beyond it becomes several events, `sync.seal.split_terminal`).
- `session.hook` with a PreToolUse `deny`: additionally the audit `hook.denied` with tool name and rule number only
  (never the path).
- `usage`: handled by the model area (`model/journal.py`, RUN-12).
- `guest.error`: a fact line `runner.journal guest_error` with the code; `credential_missing` of a session on this VM
  (its gateway lost the credential with a restart of the guest's API process) also provisions the session's
  credential again at once (`model.gateways.credential_missing`; KOR-02).

A record whose body cannot be turned into an item (`JournalRecordInvalid`, a draft refused by the seal checks) is
skipped with a fact line; the cursor moves on, so one bad record never blocks the journal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.guestlink.client import JournalRecord
from agent_runner.model import gateways
from agent_runner.sessions import records
from agent_runner.sessions.events import fail, session_event
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.seal import split_terminal

EVENT_KINDS = ("session.output", "session.tool", "session.hook", "session.file", "session.terminal")
# contracts `time`: YYYY-MM-DDTHH:MM:SS.mmmZ with valid field ranges
_TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
_CLAUDE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATE_SPECIAL = frozenset({"prompt_accepted", "prompt_refused"})
REFUSED_REASONS = frozenset({"unregistered", "delivery_failed"})
# Failure reasons a guest can know; the others (VM, capacity, device, assignment, model access) belong to the runner
# and the hub, so a guest claiming them is refused (a `capacity_exhausted` would make the hub re-schedule).
GUEST_FAILURES = frozenset(
    {
        "sandbox_failed",
        "agent_start_failed",
        "agent_exited",
        "prompt_refused",
        "delivery_failed",
        "runtime_exceeded",
        "budget_exceeded",
        "credit_exhausted",
        "oom",
    }
)
DENY_RULES = range(2, 7)
MAX_COUNT = 2**53 - 1


class JournalRecordInvalid(ValueError):
    """A record that cannot be acted upon (shape or values)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _ts(record: JournalRecord, journal: Any) -> str:
    """The record's time for event headers when it is well-formed, else the device time."""
    return record.ts if _TIME_RE.fullmatch(record.ts) else str(journal.env.at())


async def _audit(
    journal: Any,
    conn: AsyncConnection[Any],
    session: LocalSession,
    *,
    action: str,
    result: str,
    detail: dict[str, object],
) -> None:
    body = {
        "action": action,
        "result": result,
        "task_id": session.task_id,
        "session_id": session.id,
        "target_kind": "session",
        "target_id": session.id,
        "detail": detail,
    }
    await journal.env.outbox.enqueue(conn, "audit", body)


# -- session.state ---------------------------------------------------------------------------------------------------


def _state_fields(record: JournalRecord) -> tuple[str, str | None, str | None]:
    """`(state, reason, claude_session_id)` of a `session.state` record, checked (`JournalRecordInvalid`)."""
    body = record.body
    state, reason, claude_id = body.get("state"), body.get("reason"), body.get("claude_session_id")
    if not isinstance(state, str) or (state not in records.STATES and state not in STATE_SPECIAL):
        raise JournalRecordInvalid(record.kind)
    if claude_id is not None and (not isinstance(claude_id, str) or not _CLAUDE_ID_RE.fullmatch(claude_id)):
        raise JournalRecordInvalid(record.kind)
    if state == "prompt_refused":
        valid = reason in REFUSED_REASONS and isinstance(body.get("prompt_id"), str)
    elif state == "failed":
        valid = reason in GUEST_FAILURES
    else:
        valid = reason is None or (isinstance(reason, str) and _CODE_RE.fullmatch(reason) is not None)
    if not valid:
        raise JournalRecordInvalid(record.kind)
    return state, cast(str | None, reason), claude_id


async def session_state(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    state, reason, claude_id = _state_fields(record)
    # transitions carry the device time (05-runner.md section 6.1), never the guest's clock
    at = str(journal.env.at())
    fields: dict[str, object] = {}
    if claude_id is not None and claude_id != session.claude_session_id:
        fields["claude_session_id"] = claude_id
    if state == "prompt_refused":
        detail: dict[str, object] = {"reason": reason}
        await _audit(journal, conn, session, action="session.prompt_refused", result="rejected", detail=detail)
        if fields:
            await records.update(conn, session.id, **fields)
    else:
        target = "working" if state == "prompt_accepted" else state
        outbox = journal.env.outbox
        if target == "failed":
            assert reason is not None
            moved = await fail(conn, outbox, session.id, reason, at=at, fields=fields)
        else:
            moved = await records.advance(conn, outbox, session.id, target, at=at, reason=reason, fields=fields)
        if moved is not None:
            journal.reached(session.id, target)
    updated = await records.get(conn, session.id)
    assert updated is not None
    for observer in journal.observers:
        await observer.handle(journal, conn, updated, record.body)


# -- device events ---------------------------------------------------------------------------------------------------


def _hook_denied(body: Mapping[str, Any]) -> dict[str, object] | None:
    """The `hook.denied` detail of a PreToolUse deny (tool and rule only), else None."""
    tool, rule = body.get("tool"), body.get("rule")
    if body.get("event") != "PreToolUse" or body.get("decision") != "deny":
        return None
    if not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool) or type(rule) is not int or rule not in DENY_RULES:
        return None
    return {"tool": tool, "rule": rule}


def event_handler(kind: str) -> Callable[[Any, AsyncConnection[Any], JournalRecord], Any]:
    async def handle(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
        session = await journal.session(conn, record)
        if session is None:
            return
        ts = _ts(record, journal)
        bodies = split_terminal(record.body) if kind == "session.terminal" else [record.body]
        for body in bodies:
            await session_event(conn, journal.env.outbox, session, kind=kind, body=body, ts=ts)
        if kind == "session.hook":
            detail = _hook_denied(record.body)
            if detail is not None:
                await _audit(journal, conn, session, action="hook.denied", result="denied", detail=detail)

    return handle


# -- guest errors ----------------------------------------------------------------------------------------------------


async def guest_error(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    code = record.body.get("code")
    shown = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "unknown"
    journal.env.log(f"runner.journal guest_error vm_id={journal.vm.id} code={shown} seq={record.seq}")
    if code == "credential_missing":
        session = await journal.session(conn, record)
        if session is not None:
            gateways.credential_missing(journal.env, session)


HANDLERS: dict[str, Callable[..., Any]] = {
    "session.state": session_state,
    **{kind: event_handler(kind) for kind in EVENT_KINDS},
    "guest.error": guest_error,
}
