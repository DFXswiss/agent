"""Registration of an operation: `POST /v1/device/operations` (03-api.md sections 8.3, 9; ADR-002, ADR-003, ADR-012).

The runner registers every operation a session requests - also one its local checks rejected - before it executes
anything. The hub never sees parameters: they travel in the encrypted `operation.params` event of the request. One
registration runs in one transaction:

1. `requested_at` lies at most `FUTURE_SKEW` ahead of the hub clock; `device:session`: the session exists
   (`404 not_found`) and runs on the device (`403 forbidden`).
2. The `operation.params` event is stored with the event rules (`agent_hub.events.store.append_events`, author = the
   device); its header must be of kind `operation.params` and name the operation (`ref`), the request's workgroup
   (`scope_id`), task and session, and the epoch of the dedupe tag; the request's task and workgroup must be the
   session's (`400 request_invalid` with the pointer otherwise). Then the global audit lock is taken (`append_events`
   takes it already when it stores the event; a repetition stores nothing), so every registration holds it before the
   task row and the dedupe slot - the lock order events → audit → task → slot of every registration.
3. Idempotency: the operation row is inserted in state `received` (transition 1, `at` = `requested_at`, by the
   device). An operation ID that exists already is a repetition: with the same content (session, task, workgroup,
   type, `requested_at`, dedupe slot, params event, local check and its error code) the original decision is
   answered again with `"replayed": true`, otherwise `409 id_conflict`.
4. The task row is locked (lock order of `agent_hub.sessions.taskstate`), then the checks run in the order of 03-api.md
   section 8.3; the first that fails decides:
   - H1 the session has not ended (`stopped`/`failed`) and its task is not closed - else `denied` (`rights_denied`);
   - `local_check: "rejected"` - `rejected` with the runner's error code, without H2-H6;
   - H2 the task owner (the actor) has `wg:write` on the workgroup now - else `denied` (`rights_denied`);
   - H2a the GitHub identity (ADR-078 sections 2, 3 and 6; else `denied` with `rights_denied`): `triggered_by` names
     an active member of the task's org; `github_identity` names an existing `github` record that the acting human
     (`triggered_by`, else the task owner) may use in the hub's lenient form (`agent_hub.github.identities`: own, or a
     company record of an org in which the human is an active member); a `github.*` type without `github_identity`
     (an older runner; `github.request_connection` and `github.list_identities` act with no identity) is denied while
     the session or the task names an identity or a default of the task's workgroup or org exists, so an old runner
     never acts with another account than the chosen one;
   - H2b the rights profile (ADR-094 section 3; else `denied` with `rights_denied`): the type is allowed by the
     session's effective profile (`agent_hub.profiles.store.effective`: the task's and every snapshot along the chain
     of parents; none = every type); for `subagent.request` a `child_profile` resolves in the task's workgroup and is
     within the requesting session's effective profile;
   - H3 the type is known (`agent_hub.operations.types`) - else `rejected` (`unknown_type`);
   - H4 the type's limits (`agent_hub.operations.limits`) - else `denied` (`limit_exceeded`/`budget_exceeded`); only a
     main session may ask a person: `human.question` / `human.approval` of a subagent (depth > 0) is `denied`
     (`rights_denied`, ADR-076 section 3);
   - H5/H6 with a dedupe slot: a held slot makes the operation a `duplicate` of its holder, otherwise the operation
     takes the slot for the type's window (`agent_hub.operations.dedupe`); without a slot it is accepted directly.
5. The decision is written: the operation row (`state`, `error_code`, `duplicate_of`, `decided_at`), transition 2
   (`at` = hub time, `by_id` NULL = the hub) and the audit row `operation.<state>` (actor: the device; detail with
   the local check, `github_identity` and `triggered_by`). An accepted `subagent.request` creates the child session
   (`agent_hub.operations.children`; its `github_identity` is the registration's, else the parent session's, its
   `triggered_by` the registration's, ADR-083 section 2, its `agent_profile` the snapshot of `child_profile`, ADR-094
   section 1) and answers its ID, and when the registration named a `child_profile` the child's effective profile
   (`child_profile`). A `subagent.request` naming a `github_identity` passes the feature gate of the org first (`409
   peer_outdated`, nothing stored).

6. The task's attention record is refreshed (`agent_hub.attention.store`, ADR-076; the last lock of the transaction).

After the commit the caller sends the recorded effects: the `operation` frame for the org's browsers (and, with a
child, its `session`/`task` frames and the `session.start` hint).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import psycopg
from pydantic import Field, StringConstraints

from agent_hub.attention import store as attention
from agent_hub.audit.chain import AUDIT_LOCK
from agent_hub.audit.recorder import unchanged
from agent_hub.auth.fields import B64u32, Strict, Uuid7
from agent_hub.config import DEFAULT_SUBAGENT_LIMITS, SubagentLimits
from agent_hub.errors import HubError
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.github import identities
from agent_hub.operations import children, dedupe, limits
from agent_hub.operations.children import Parent
from agent_hub.operations.steps import OperationRef, add_transition, audit_step
from agent_hub.operations.types import HUMAN_TYPES, OPERATION_TYPES, SUBAGENT_REQUEST, window
from agent_hub.orgs import rights
from agent_hub.outbox.base import moment
from agent_hub.outbox.bodies import OperationErrorCode, Time
from agent_hub.outbox.handlers_operation import operation_row
from agent_hub.profiles import model as profile_model
from agent_hub.profiles import store as profiles
from agent_hub.sessions.derive import CLOSED, FINAL
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import TaskRow, lock_task

Connection = psycopg.Connection[Any]
OperationType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$", max_length=64)]
Epoch = Annotated[int, Field(ge=1, le=2**31 - 1)]
DECISION_SEQ = 2
PARAMS_KIND = "operation.params"
GITHUB_PREFIX = "github."
IDENTITY_FREE = frozenset({"github.request_connection", "github.list_identities"})
"""The `github.*` types that act with no identity (no backstop for them, H2a)."""
FUTURE_SKEW = timedelta(seconds=120)
"""How far `requested_at` may lie ahead of the hub clock (the signature window of 03-api.md section 1)."""


class DedupeRequest(Strict):
    """The dedupe slot of the request (04-crypto.md section 4.10)."""

    epoch: Epoch
    tag: B64u32


class RegistrationRequest(Strict):
    """Body of `POST /v1/device/operations` (`contracts/api/device-operations.request.json`)."""

    operation_id: Uuid7
    session_id: Uuid7
    task_id: Uuid7
    wg_id: Uuid7
    type: OperationType
    requested_at: Time
    local_check: Literal["passed", "rejected"]
    error_code: OperationErrorCode | None = None
    dedupe: DedupeRequest | None
    params_event: dict[str, Any]
    github_identity: Uuid7 | None = None
    triggered_by: Uuid7 | None = None
    child_profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")] | None = None

    def check_form(self) -> None:
        """The error code comes exactly with `local_check: "rejected"` (the contract's `oneOf`)."""
        if (self.local_check == "rejected") != (self.error_code is not None):
            raise HubError("request_invalid", detail={"pointers": ["/error_code"]})


@dataclass
class Outcome:
    """The answer of a registration and what to announce after the commit."""

    body: dict[str, Any]
    effects: Effects = field(default_factory=Effects)


@dataclass(frozen=True)
class _Session:
    org_id: UUID
    wg_id: UUID
    task_id: UUID
    device_id: UUID | None
    vm_id: str | None
    depth: int


def _invalid(*pointers: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": list(pointers)})


def _session(connection: Connection, session_id: UUID, device: UUID) -> _Session:
    row = connection.execute(
        "SELECT org_id, wg_id, task_id, device_id, vm_id, depth FROM hub.session WHERE id = %s", (session_id,)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    found = _Session(*row)
    if found.device_id != device:
        raise HubError("forbidden")
    return found


def _check_header(request: RegistrationRequest, stored: StoredEvent) -> None:
    header = stored.event["header"]
    expected = {
        "kind": PARAMS_KIND,
        "ref": request.operation_id,
        "scope_id": request.wg_id,
        "task": request.task_id,
        "session": request.session_id,
    }
    wrong = [f"/params_event/header/{name}" for name, value in expected.items() if header[name] != value]
    if request.dedupe is not None and header["epoch"] != request.dedupe.epoch:
        wrong.append("/dedupe/epoch")
    if wrong:
        raise _invalid(*wrong)


def _check_session(request: RegistrationRequest, session: _Session) -> None:
    wrong = [
        pointer
        for pointer, given, actual in (
            ("/task_id", request.task_id, session.task_id),
            ("/wg_id", request.wg_id, session.wg_id),
        )
        if given != str(actual)
    ]
    if wrong:
        raise _invalid(*wrong)


def _answer(
    connection: Connection,
    state: str,
    error_code: str | None,
    duplicate_of: UUID | None,
    child: UUID | None,
    *,
    with_profile: bool = False,
) -> dict[str, Any]:
    """The response body of a decision; `child_profile` (the child's effective profile) only for a registration that
    named one (an older runner never does and would not know the field)."""
    body: dict[str, Any] = {"state": state}
    if state in ("denied", "rejected"):
        body["error_code"] = error_code
    elif state == "duplicate":
        original = cast(
            tuple[str, UUID | None],
            connection.execute(
                "SELECT state, result_event_id FROM hub.operation WHERE id = %s", (duplicate_of,)
            ).fetchone(),
        )
        body["duplicate_of"] = str(duplicate_of)
        body["original"] = {"state": original[0]}
        if original[1] is not None:
            body["original"]["result_event_id"] = str(original[1])
    elif child is not None:
        body["child_session_id"] = str(child)
        if with_profile:
            found = profiles.effective(connection, child)
            body["child_profile"] = None if found is None else found.as_json()
    return body


def _one(connection: Connection, query: str, params: tuple[object, ...]) -> tuple[Any, ...]:
    """The one row of a query whose row exists (the registration that stored it committed as a whole)."""
    return cast(tuple[Any, ...], connection.execute(query, params).fetchone())


def _text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _replay(connection: Connection, request: RegistrationRequest, params_event_id: str) -> Outcome:
    """The original decision of a repeated registration, or `409 id_conflict` for other content."""
    operation = UUID(request.operation_id)
    row = _one(
        connection,
        "SELECT session_id, task_id, wg_id, type, created_at, dedupe_epoch, dedupe_tag, params_event_id, duplicate_of,"
        " github_identity, triggered_by FROM hub.operation WHERE id = %s",
        (operation,),
    )
    decision = _one(
        connection,
        "SELECT state, error_code FROM hub.operation_transition WHERE operation_id = %s AND seq = %s",
        (operation, DECISION_SEQ),
    )
    audited = _one(
        connection,
        "SELECT detail->>'local_check' FROM hub.audit WHERE task_id = %s AND target_kind = 'operation'"
        " AND target_id = %s ORDER BY seq LIMIT 1",
        (request.task_id, request.operation_id),
    )
    dedupe_given = (None, None) if request.dedupe is None else (request.dedupe.epoch, request.dedupe.tag)
    stored = (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        row[3],
        row[4],
        row[5],
        row[6],
        str(row[7]),
        audited[0],
        _text(row[9]),
        _text(row[10]),
    )
    given = (
        request.session_id,
        request.task_id,
        request.wg_id,
        request.type,
        moment(request.requested_at),
        *dedupe_given,
        params_event_id,
        request.local_check,
        request.github_identity,
        request.triggered_by,
    )
    local_error_matches = request.local_check == "passed" or tuple(decision) == ("rejected", request.error_code)
    if stored != given or not local_error_matches:
        raise HubError("id_conflict")
    child = children.child_of(connection, operation)
    body = _answer(connection, decision[0], decision[1], row[8], child, with_profile=request.child_profile is not None)
    unchanged()
    return Outcome({**body, "replayed": True})


def _limits(
    connection: Connection,
    request: RegistrationRequest,
    task: TaskRow,
    parent: Parent,
    *,
    now: datetime,
    subagent_limits: SubagentLimits,
) -> str | None:
    """H4: the error code of an exceeded limit of the type, None within the limits."""
    if request.type in HUMAN_TYPES:
        return "rights_denied" if parent.depth > 0 else None
    if request.type != SUBAGENT_REQUEST:
        return None
    exceeded = limits.subagent_limit(connection, task.id, parent.depth, subagent_limits)
    if exceeded is not None:
        return exceeded
    budget = limits.BudgetRequest(connection=connection, org_id=task.org_id, session_id=parent.id, now=now)
    verdict = limits.budget_check(budget)
    if verdict is True:
        return None
    return verdict if isinstance(verdict, str) else "budget_exceeded"


def _identity_allowed(connection: Connection, request: RegistrationRequest, task: TaskRow) -> bool:
    """H2a (module docstring)."""
    if request.triggered_by is not None and not identities.is_member(connection, task.org_id, request.triggered_by):
        return False
    if request.github_identity is not None:
        human = request.triggered_by or task.owner_id
        return identities.usable_by(connection, request.github_identity, human)
    if not request.type.startswith(GITHUB_PREFIX) or request.type in IDENTITY_FREE:
        return True
    chosen = cast(
        tuple[UUID | None, UUID | None],
        connection.execute(
            "SELECT s.github_identity, t.github_identity FROM hub.session s JOIN hub.task t ON t.id = s.task_id"
            " WHERE s.id = %s",
            (request.session_id,),
        ).fetchone(),
    )
    return chosen == (None, None) and not identities.has_default(connection, task.org_id, task.wg_id)


def _profile_allowed(connection: Connection, request: RegistrationRequest, task: TaskRow) -> bool:
    """H2b (module docstring)."""
    profile = profiles.effective(connection, request.session_id)
    if not profile_model.allows(profile, request.type):
        return False
    if request.type != SUBAGENT_REQUEST or request.child_profile is None:
        return True
    child = profiles.resolve(connection, task.org_id, task.wg_id, request.child_profile)
    return child is not None and profile_model.within(child, profile)


def _refusal(
    connection: Connection,
    request: RegistrationRequest,
    task: TaskRow,
    *,
    session_state: str,
    parent: Parent,
    now: datetime,
    subagent_limits: SubagentLimits,
) -> tuple[str, str | None] | None:
    """The refusal of checks H1-H4 (module docstring, step 4), None when they pass."""
    if session_state in FINAL or task.state == CLOSED:
        return "denied", "rights_denied"
    if request.local_check == "rejected":
        return "rejected", request.error_code
    if not rights.can(connection, task.owner_id, rights.WG_WRITE, wg_id=task.wg_id):
        return "denied", "rights_denied"
    if not (_identity_allowed(connection, request, task) and _profile_allowed(connection, request, task)):
        return "denied", "rights_denied"
    if request.type not in OPERATION_TYPES:
        return "rejected", "unknown_type"
    exceeded = _limits(connection, request, task, parent, now=now, subagent_limits=subagent_limits)
    return None if exceeded is None else ("denied", exceeded)


def _decide(
    connection: Connection,
    request: RegistrationRequest,
    task: TaskRow,
    *,
    session_state: str,
    parent: Parent,
    now: datetime,
    subagent_limits: SubagentLimits,
) -> tuple[str, str | None, dedupe.Held | None]:
    """The decision of checks H1-H6 (module docstring, step 4)."""
    refused = _refusal(
        connection,
        request,
        task,
        session_state=session_state,
        parent=parent,
        now=now,
        subagent_limits=subagent_limits,
    )
    if refused is not None:
        return refused[0], refused[1], None
    if request.dedupe is None:
        return "accepted", None, None
    slot = dedupe.Slot(wg_id=task.wg_id, epoch=request.dedupe.epoch, tag=request.dedupe.tag)
    held = dedupe.claim(connection, slot, UUID(request.operation_id), now + window(request.type), now)
    return ("duplicate", None, held) if held is not None else ("accepted", None, None)


def register(
    connection: Connection,
    device: UUID,
    request: RegistrationRequest,
    *,
    now: datetime,
    root_key: bytes,
    subagent_limits: SubagentLimits = DEFAULT_SUBAGENT_LIMITS,
) -> Outcome:
    """Register and decide one operation in the caller's transaction (module docstring)."""
    request.check_form()
    requested_at = moment(request.requested_at)
    if requested_at > now + FUTURE_SKEW:
        raise _invalid("/requested_at")
    session = _session(connection, UUID(request.session_id), device)
    stored = append_events(connection, [request.params_event], str(device), now=now, root_key=root_key)[0]
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
    _check_header(request, stored)
    _check_session(request, session)
    if request.type == SUBAGENT_REQUEST and request.github_identity is not None:
        identities.require_feature(connection, session.org_id, root_key)
    operation = UUID(request.operation_id)
    inserted = connection.execute(
        "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
        " dedupe_epoch, dedupe_tag, params_event_id, created_at, github_identity, triggered_by)"
        " SELECT %s, org_id, wg_id, task_id, id, device_id, owner_id, %s, 'received', %s, %s, %s, %s, %s, %s"
        " FROM hub.session WHERE id = %s ON CONFLICT (id) DO NOTHING RETURNING id",
        (
            operation,
            request.type,
            None if request.dedupe is None else request.dedupe.epoch,
            None if request.dedupe is None else request.dedupe.tag,
            stored.id,
            requested_at,
            request.github_identity,
            request.triggered_by,
            request.session_id,
        ),
    ).fetchone()
    if inserted is None:
        return _replay(connection, request, stored.id)
    add_transition(connection, operation, "received", None, at=requested_at, by_id=device)
    task = cast(TaskRow, lock_task(connection, session.task_id))
    state_row = cast(
        tuple[str],
        connection.execute("SELECT state FROM hub.session WHERE id = %s", (request.session_id,)).fetchone(),
    )
    parent = Parent(id=UUID(request.session_id), device_id=device, vm_id=session.vm_id, depth=session.depth)
    state, error_code, held = _decide(
        connection,
        request,
        task,
        session_state=state_row[0],
        parent=parent,
        now=now,
        subagent_limits=subagent_limits,
    )
    duplicate_of = None if held is None else held.operation_id
    connection.execute(
        "UPDATE hub.operation SET state = %s, error_code = %s, duplicate_of = %s, decided_at = %s WHERE id = %s",
        (state, error_code, duplicate_of, now, operation),
    )
    add_transition(connection, operation, state, error_code, at=now, by_id=None)
    ref = OperationRef(
        id=operation,
        org_id=task.org_id,
        wg_id=task.wg_id,
        task_id=task.id,
        session_id=parent.id,
        device_id=device,
        type=request.type,
        github_identity=None if request.github_identity is None else UUID(request.github_identity),
        triggered_by=None if request.triggered_by is None else UUID(request.triggered_by),
    )
    audit_step(
        connection,
        ref,
        state,
        now,
        actor_kind="device",
        actor_id=device,
        error_code=error_code,
        duplicate_of=duplicate_of,
        local_check=request.local_check,
    )
    outcome = Outcome({})
    child = None
    if state == "accepted" and request.type == SUBAGENT_REQUEST:
        child = children.create_child(
            connection,
            task,
            parent,
            operation,
            now,
            effects=outcome.effects,
            github_identity=request.github_identity,
            triggered_by=None if request.triggered_by is None else UUID(request.triggered_by),
            agent_profile=None
            if request.child_profile is None
            else profiles.resolve(connection, task.org_id, task.wg_id, request.child_profile),
        )
    outcome.body = _answer(
        connection, state, error_code, duplicate_of, child, with_profile=request.child_profile is not None
    )
    outcome.effects.org_frame(task.org_id, "operation", operation_row(connection, operation))
    attention.refresh(connection, task.id, now, outcome.effects)
    return outcome
