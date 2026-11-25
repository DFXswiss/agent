"""Registration of an operation at the hub (03-api.md section 8.3, `POST /v1/device/operations`; 05-runner.md section 12
steps 4-5; 04-crypto.md sections 4.10 and 5.12; ADR-012).

`build(...)` writes the registration body once, before the first attempt:

- the encrypted `operation.params` event `{operation_id, type, params, idempotency_key?}` (workgroup scope, the
  session's task and session in the header, `ref` = operation ID), sealed with the **current** workgroup epoch and the
  device key (`sync.seal`). Parameters that would make the event exceed the size limit (E8) are left out (`params:
  {}`) and the operation is rejected locally with `size_exceeded`;
- `local_check` `passed` with the dedupe slot of `dedupe.dedupe_slot` (keyed with the same epoch), or `rejected`
  with the local `error_code` and no slot. An idempotency key the task already used for other content of the same
  type within the type's window (`records.key_conflict`) is rejected with `params_invalid` (`key_conflict`, KOR-28):
  the key marks the same request, it never opens a second slot;
- the plaintext `type` when it is a well-formed operation type (`contracts/common.json#/$defs/operation_type`), else
  `invalid.type` (a guest-rejected request may name anything; the params event keeps the exact type);
- `requested_at` of the journal record when it is a contract time, else the device time;
- `triggered_by` (KOR-47, ADR-078 sections 2 and 6): the acting user stamped at the receipt (`operation.actor_id`,
  `credentials.acting`, HOT-14); left out without one;
- `github_identity`: for `github.*` types the identity the pre-check resolved, for `subagent.request` the child
  session's identity of its parameter (`attribution`); left out without one;
- `child_profile` (KOR-51, ADR-091 section 3): for `subagent.request` the profile its parameter `profile` names, as
  `profiles.check` checked it; left out without one. The hub's answer then carries the child's effective profile
  (`child_profile`, `Decision.child_profile`).

`attribution(context, session, operation, params)` runs after the pre-checks of every operation: for
`subagent.request` with `github_identity`, the check that the acting user may use that identity
(`github.identity.usable`; `identity_forbidden` otherwise, also when the hub refuses the record with `403`; a record
that is gone or refused is `identity_unavailable`).

The body is stored with the operation (`registration`), so every retry sends the same content and the hub answers a
repeat with `replayed:true` instead of `409 id_conflict`.

`Registrar.send(body, deadline)` posts until the hub decides: transport errors (hub unreachable, unverifiable
answers, answers outside the contract) and `429`/`5xx` refusals are retried with backoff (1 s doubling to 30 s,
±20 %, at least `Retry-After`); at `deadline` (10 min after the request reached the runner) it gives up (None, the
operation expires `hub_unreachable`). Any other refusal is final (`refused`): the hub will never record it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner.credentials import identities
from agent_runner.credentials.cache import RecordRefused
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.github import identity
from agent_runner.operations import records
from agent_runner.operations.dedupe import WINDOWS, dedupe_slot
from agent_runner.operations.extensions import OPERATION_ERROR_CODES, CheckFailed
from agent_runner.operations.records import Operation
from agent_runner.orgs.keys import load_scope_key
from agent_runner.profiles.model import Profile
from agent_runner.profiles.model import parse as parse_profile
from agent_runner.sync.backoff import Backoff
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError
from agent_runner.sync.outbox import event_draft
from agent_runner.sync.seal import Sealer, check_draft

TARGET = "/v1/device/operations"
INVALID_TYPE = "invalid.type"
TYPE_RE = re.compile(r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$")
TYPE_MAX = 64
TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATES = frozenset({"accepted", "duplicate", "denied", "rejected"})
ORIGINAL_STATES = frozenset(
    {"received", "rejected", "accepted", "duplicate", "denied", "executing", "succeeded", "failed", "expired"}
)
REFUSED = "refused"


class NoKey(Exception):
    """The device holds no workgroup key yet (the reconcile loop fetches the wrap): registration waits."""


@dataclass(frozen=True)
class Built:
    body: dict[str, Any]
    size_exceeded: bool
    """The parameters did not fit into the params event (the operation is rejected locally)."""
    key_conflict: bool = False
    """The idempotency key was used for another request of the task (the operation is rejected locally)."""


@dataclass(frozen=True)
class Decision:
    state: str
    """`accepted`, `duplicate`, `denied`, `rejected`, or `refused` (a final refusal of the request itself)."""
    error_code: str | None = None
    duplicate_of: str | None = None
    original_state: str | None = None
    original_result_event_id: str | None = None
    child_session_id: str | None = None
    child_profile: Profile | None = None
    child_profile_named: bool = False
    """The answer carried `child_profile` (the registration named one); `child_profile` None then means every right."""
    status: int | None = None
    """HTTP status of a `refused` request."""


async def attribution(context: Any, session: Any, operation: Operation, params: Mapping[str, Any]) -> dict[str, Any]:
    """The checked `github_identity` of a `subagent.request` (module docstring); empty otherwise."""
    chosen = params.get("github_identity")
    if operation.type != "subagent.request" or chosen is None:
        return {}
    user = operation.actor_id
    if user is None:
        raise identity.forbidden()
    try:
        record = await identities.meta(context, str(chosen))
    except HubRefusal as refusal:
        if refusal.status == 403:
            raise identity.forbidden() from None
        record = None
    except RecordRefused:
        record = None
    if record is None:
        raise CheckFailed("identity_unavailable", message="The GitHub identity is not connected.")
    book = await identity.loaded(context)
    usable = identity.usable(context, book, record.owner, record.owner_kind, user=user, org_id=session.org_id)
    if usable is None:
        raise identity.forbidden()
    return {"github_identity": record.id}


def plain_type(op_type: str) -> str:
    return op_type if len(op_type) <= TYPE_MAX and TYPE_RE.fullmatch(op_type) else INVALID_TYPE


def _params_body(op: Operation, params: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"operation_id": op.id, "type": op.type, "params": dict(params)}
    if op.idempotency_key is not None:
        body["idempotency_key"] = op.idempotency_key
    return body


def params_draft(op: Operation, *, at: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return event_draft(
        id=uuid7(),
        org=op.org_id,
        scope="wg",
        scope_id=op.wg_id,
        wg=op.wg_id,
        task=op.task_id,
        session=op.session_id,
        kind="operation.params",
        ref=op.id,
        ts=at,
        body=_params_body(op, params),
    )


async def build(
    conn: AsyncConnection[Any],
    *,
    sealer: Sealer,
    storage_key: bytes,
    op: Operation,
    at: str,
    error_code: str | None,
    params: Mapping[str, Any],
    extra: Mapping[str, object],
    attribution: Mapping[str, Any] | None = None,
) -> Built:
    """The registration body (`NoKey` while the device holds no workgroup key)."""
    noted = attribution or {}
    draft = params_draft(op, at=at, params=op.params)
    size_exceeded = False
    try:
        check_draft(draft)
    except CryptoError as exc:
        draft = params_draft(op, at=at, params={})
        size_exceeded = exc.code == "size_exceeded"
        error_code = error_code or ("size_exceeded" if size_exceeded else "params_invalid")
    sealed = await sealer.seal(conn, draft)
    if sealed is None:
        raise NoKey(op.wg_id)
    header = sealed["header"]
    assert isinstance(header, Mapping)
    epoch = header["epoch"]
    assert isinstance(epoch, int)
    body: dict[str, Any] = {
        "operation_id": op.id,
        "session_id": op.session_id,
        "task_id": op.task_id,
        "wg_id": op.wg_id,
        "type": plain_type(op.type),
        "requested_at": op.requested_at if TIME_RE.fullmatch(op.requested_at) else at,
    }
    if op.actor_id is not None:
        body["triggered_by"] = op.actor_id
    chosen = noted.get("github_identity")
    if isinstance(chosen, str) and (op.type.startswith("github.") or op.type == "subagent.request"):
        body["github_identity"] = chosen
    child_profile = noted.get("child_profile")
    if isinstance(child_profile, str) and op.type == "subagent.request":
        body["child_profile"] = child_profile
    if error_code is not None:
        body.update({"local_check": "rejected", "error_code": error_code, "dedupe": None})
    else:
        key = await load_scope_key(conn, storage_key, "wg", op.wg_id, epoch)
        assert key is not None  # the sealer just used it
        slot = dedupe_slot(
            op.type,
            params,
            session_id=op.session_id,
            task_id=op.task_id,
            idempotency_key=op.idempotency_key,
            extra=extra,
            workgroup_key=key,
            epoch=epoch,
        )
        conflict = (
            slot is not None
            and op.type in WINDOWS
            and op.idempotency_key is not None
            and await records.key_conflict(conn, op, epoch=epoch, tag=str(slot["tag"]), window_s=WINDOWS[op.type])
        )
        if conflict:
            body.update({"local_check": "rejected", "error_code": "params_invalid", "dedupe": None})
            body["params_event"] = sealed
            return Built(body=body, size_exceeded=False, key_conflict=True)
        body.update({"local_check": "passed", "dedupe": slot})
    body["params_event"] = sealed
    return Built(body=body, size_exceeded=size_exceeded)


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise ValueError("uuid")
    return value


DECISION_FIELDS = frozenset(
    {"state", "replayed", "error_code", "duplicate_of", "original", "child_session_id", "child_profile"}
)


def parse_decision(data: Mapping[str, object]) -> Decision:
    """The hub's decision (`api/device-operations.response.json`); ValueError for anything else."""
    state = data.get("state")
    extra = set(data) - DECISION_FIELDS
    if state not in STATES or extra or ("replayed" in data and not isinstance(data["replayed"], bool)):
        raise ValueError("decision")
    if state == "accepted":
        if set(data) - {"state", "replayed", "child_session_id", "child_profile"}:
            raise ValueError("decision")
        child = data.get("child_session_id")
        named = "child_profile" in data
        return Decision(
            "accepted",
            child_session_id=None if child is None else _uuid(child),
            child_profile=parse_profile(data.get("child_profile")) if named else None,
            child_profile_named=named,
        )
    if state == "duplicate":
        original = data.get("original")
        if set(data) - {"state", "replayed", "duplicate_of", "original"} or not isinstance(original, Mapping):
            raise ValueError("decision")
        if set(original) - {"state", "result_event_id"} or original.get("state") not in ORIGINAL_STATES:
            raise ValueError("decision")
        event_id = original.get("result_event_id")
        return Decision(
            "duplicate",
            duplicate_of=_uuid(data.get("duplicate_of")),
            original_state=str(original["state"]),
            original_result_event_id=None if event_id is None else _uuid(event_id),
        )
    code = data.get("error_code")
    if set(data) - {"state", "replayed", "error_code"} or code not in OPERATION_ERROR_CODES:
        raise ValueError("decision")
    return Decision(str(state), error_code=str(code))


class Registrar:
    def __init__(
        self,
        client: HubClient,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        random: Callable[[], float],
        log: Callable[[str], None],
    ) -> None:
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.random = random
        self.log = log

    async def send(self, operation_id: str, body: Mapping[str, Any], deadline: float) -> Decision | None:
        """The hub's decision, or None when `deadline` (pipeline clock) passed without one."""
        backoff = Backoff(random=self.random)
        while True:
            try:
                return parse_decision(await self.client.request("POST", TARGET, dict(body)))
            except HubTransportError as exc:
                delay, reason = backoff.next(), exc.reason
            except HubRefusal as exc:
                if exc.status != 429 and exc.status < 500:
                    return Decision(REFUSED, error_code=exc.code, status=exc.status)
                delay, reason = max(backoff.next(), float(exc.retry_after or 0)), exc.code
            except ValueError:
                delay, reason = backoff.next(), "decision_invalid"
            remaining = deadline - self.clock()
            self.log(f"runner.operations register_retry operation_id={operation_id} reason={reason}")
            if remaining <= 0:
                return None
            await self.sleep(min(delay, remaining))
