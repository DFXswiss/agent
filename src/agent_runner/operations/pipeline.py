"""The operation pipeline "register-then-execute" (05-runner.md section 12 steps 2-7; 03-api.md sections 8.1, 8.3, 9,
12; 04-crypto.md section 4.10; ADR-012).

`Pipeline` (one per daemon, `pipeline_for(context)`) works every operation the journal handler recorded
(`journal.py`, state `received`) to its end, each in its own asyncio task, woken by `NOTIFY runner_operation` (task
`operations`) and by a scan every `PERIOD_S`:

1. **Checks** (`_check`): workgroup settings of the latest verified `settings.workgroup` event (defaults otherwise),
   the owner's GitHub connection, the task's commits, then the formal checks (`checks.check`), the type enabled on
   this device (an executor, and a pre-check for `extensions.NEEDS_PRECHECK`, else `unknown_type`) and the type's
   pre-check (`extensions.PRECHECKS`; any error other than `CheckFailed` is `upstream_error`). The guest's
   `local_check:"rejected"` skips them: its error is binding.
2. **Registration** (`register.build`, `register.Registrar`): the encrypted `operation.params` event, the dedupe
   tag with the current workgroup epoch, `local_check` and the error code, stored before the first attempt; posted
   with backoff while the hub is unreachable. While the device holds no workgroup key the registration waits for it.
   No decision 10 min after the request reached the runner: `expired` with `hub_unreachable` (the session gets
   `failed`, `retryable:true`; the hub never knew the operation, so no event or transition is written). A final
   refusal of the request (`4xx` other than `429`) ends `failed` (`rights_denied` for `403`, else `upstream_error`).
3. **Decision**: `accepted` -> execute; `duplicate` -> the original's result (below); `denied`/`rejected` -> the
   answer (with the local error and its pointers when the runner or guest rejected it).
4. **Execution** (`_execute`): outbox `operation_transition executing`, then the executor of `EXECUTORS` with
   retries for retryable failures - at most 3 more attempts after 2 s, 8 s, 30 s (at least `retry_after`; a
   `retry_after` above 60 s ends the retries) - then `failed` with `retryable:true`. Non-retryable failures end at
   once; an executor that raises anything else is `upstream_error`. An operation found `executing` when the pipeline
   starts (the daemon stopped during the executor) ends `failed` `timeout`, `retryable:true`: it is never executed
   twice.
5. **Result** (`_finish`): in **one** transaction the outbox `event` `operation.result` (encrypted `{operation_id,
   status, result|error}`), then the `operation_transition` (`succeeded`/`failed` with `result_event_id`), and the
   answer stored with the row; then `POST /v1/pending/{id}/answer`. A result that does not fit into an event (E8)
   ends `failed` `size_exceeded`. A result with `existing:true` (an open pull request found by the executor) is
   answered `status:"duplicate"` without `duplicate_of`.
6. **Duplicates** (`_duplicate`): the original's outcome from its local row or its verified `operation.result` event
   (`results.original_outcome`, an inbox pull per poll while the original is not local), polled every second
   within the MCP wait (20 s from the request): `duplicate` with `duplicate_of` and the original's result; an original
   that failed gives `failed` with its error; nothing final in time gives `pending` with `duplicate_of`.
7. **Answer** (`_deliver`): up to 3 attempts (1 s, 2 s); a refusal of the guest (the pending call is unknown or
   answered) ends the attempts. If the MCP call had already returned `pending` (19 s or more since the request reached
   the runner; the guest waits 20 s), the `operation.finished` notifier (RUN-14, `extensions.finished_notifier`) is
   called for a final answer; without a notifier it is logged.

`answer(operation_id)` and `wait_answer(operation_id, seconds)` give the current MCP outcome of an operation for
`get_operation` (RUN-08).
"""

from __future__ import annotations

import asyncio
import random as _random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg

from agent_runner import registry
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.operations import checks, records, register, results
from agent_runner.operations import settings as workgroup_settings
from agent_runner.operations.extensions import (
    NEEDS_PRECHECK,
    CheckFailed,
    Executor,
    Notify,
    OperationContext,
    OperationFailed,
    Precheck,
    PrecheckRequest,
    PrecheckResult,
    finished_notifier,
    known_code,
    message_text,
    prechecks,
)
from agent_runner.operations.records import Operation
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions import records as session_records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.http import HubClient
from agent_runner.sync.outbox import event_draft
from agent_runner.sync.seal import Sealer, check_draft
from agent_runner.vm import records as vm_records

ATTRIBUTE = "operation_pipeline"
MCP_WAIT_S = 20.0
PENDING_AFTER_S = 19.0
EXPIRY_S = 600.0
RETRY_DELAYS = (2.0, 8.0, 30.0)
RETRY_AFTER_MAX_S = 60.0
PERIOD_S = 5.0
KEY_WAIT_S = 2.0
DUPLICATE_POLL_S = 1.0
ANSWER_ATTEMPTS = 3
ANSWER_BACKOFF_S = 1.0
ANSWER_WINDOW_S = 900.0
ACCEPTED_EXPIRY_S = 1740.0
"""The hub expires an operation 30 min in `accepted` without `executing` (03 §12); acceptance is never earlier than
the request, so an operation whose request is older than this (with a minute of margin) is not executed any more."""
STORE_ATTEMPTS = 3
ERROR_KEYS = frozenset({"code", "message", "pointers", "retryable"})

Refresh = Callable[[], Awaitable[object]]


async def _no_refresh() -> None:
    return None


def clean_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """The members of an operation error object that the contract allows (`ops/objects.json#/$defs/error`)."""
    out: dict[str, Any] = {"code": known_code(error.get("code"))}
    message, pointers, retryable = message_text(error.get("message")), error.get("pointers"), error.get("retryable")
    if message is not None:
        out["message"] = message
    if isinstance(pointers, list) and pointers and all(isinstance(p, str) for p in pointers):
        out["pointers"] = pointers[:32]
    if isinstance(retryable, bool):
        out["retryable"] = retryable
    return out


class Pipeline:
    def __init__(
        self,
        env: SessionEnv,
        *,
        client: HubClient,
        sealer: Sealer,
        executors: Mapping[str, Executor],
        prechecks: Mapping[str, Precheck] | None = None,
        notify: Notify | None = None,
        refresh: Refresh = _no_refresh,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = _random.random,
    ) -> None:
        self.env = env
        self.sealer = sealer
        self.executors = dict(executors)
        self.prechecks = dict(prechecks or {})
        self.notify = notify
        self.refresh = refresh
        self.sleep = sleep
        self.registrar = register.Registrar(client, clock=env.clock, sleep=sleep, random=random, log=env.log)
        self.trigger = asyncio.Event()
        self.running: dict[str, asyncio.Task[None]] = {}
        self.finished: set[str] = set()
        """Operations whose `operation.finished` notifier ran in this daemon (at most once each)."""
        self.picked: dict[str, tuple[float, float]] = {}
        """operation ID -> (pipeline clock when picked up, seconds since the request at that time)."""

    # -- small helpers -------------------------------------------------------------------------------------------

    def wake(self) -> None:
        self.trigger.set()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.env.wall(), UTC)

    def line(self, text: str) -> None:
        self.env.log(f"runner.operations {text}")

    def elapsed(self, op: Operation) -> float:
        """Seconds since the request: the larger of the store age of the row and the guest's `requested_at` at
        pick-up (a runner that lagged behind the journal or restarted), then the pipeline clock."""
        if op.id not in self.picked:
            lag = max(op.age, self._since_request(op.requested_at), 0.0)
            self.picked[op.id] = (self.env.clock(), lag)
        started, lag = self.picked[op.id]
        return lag + self.env.clock() - started

    def _since_request(self, requested_at: str) -> float:
        """Seconds since the guest's `requested_at` (device wall clock), 0 when it is not a contract time."""
        try:
            at = datetime.strptime(requested_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            return 0.0
        return self.env.wall() - at.timestamp()

    async def load(self, operation_id: str) -> Operation | None:
        async with store_session(self.env.db) as conn:
            return await records.get(conn, operation_id)

    async def _reload(self, operation_id: str) -> Operation:
        found = await self.load(operation_id)
        assert found is not None
        return found

    async def _session(self, op: Operation) -> LocalSession | None:
        async with store_session(self.env.db) as conn:
            return await session_records.get(conn, op.session_id)

    def enabled(self, op_type: str) -> bool:
        return op_type in self.executors and (op_type not in NEEDS_PRECHECK or op_type in self.prechecks)

    # -- the loop ------------------------------------------------------------------------------------------------

    async def scan(self) -> list[str]:
        """Start a task for every operation with work left that is not being worked on; returns the started IDs."""
        async with store_session(self.env.db) as conn:
            found = await records.pending_work(conn, ANSWER_WINDOW_S)
        started = [operation_id for operation_id in found if operation_id not in self.running]
        for operation_id in started:
            self.running[operation_id] = asyncio.ensure_future(self._run(operation_id))
        return started

    async def _run(self, operation_id: str) -> None:
        try:
            await self.process(operation_id)
        except Exception as exc:
            self.line(f"process_failed operation_id={operation_id} error={type(exc).__name__}")
        finally:
            self.running.pop(operation_id, None)
            self.picked.pop(operation_id, None)

    async def settle(self) -> None:
        """Wait until every running operation task ended (tests, shutdown)."""
        while self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)

    async def wait(self, seconds: float, stopping: asyncio.Event) -> None:
        waiters = {
            asyncio.ensure_future(self.trigger.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(self.sleep(seconds)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def loop(self, stopping: asyncio.Event) -> None:
        """Scan on every wake-up and every `PERIOD_S` until `stopping`; a failed scan is logged and repeated."""
        try:
            while not stopping.is_set():
                self.trigger.clear()
                try:
                    await self.scan()
                except Exception as exc:
                    self.line(f"scan_failed error={type(exc).__name__}")
                await self.wait(PERIOD_S, stopping)
        finally:
            for task in list(self.running.values()):
                task.cancel()
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)

    # -- one operation -------------------------------------------------------------------------------------------

    async def process(self, operation_id: str) -> None:
        op = await self.load(operation_id)
        if op is None:
            return
        self.elapsed(op)
        if op.state == "received":
            op = await self._register(op)
        if op.state == "accepted" and op.age >= ACCEPTED_EXPIRY_S:
            op = await self._expire(op, "timeout")
        elif op.state == "accepted":
            op = await self._execute(op)
        elif op.state == "executing":
            op = await self._finish(op, failure=OperationFailed("timeout", retryable=True))
        if op.state == "duplicate" and op.answer is None:
            op = await self._duplicate(op)
        if op.answer is not None and op.answered_at is None:
            await self._deliver(op)

    # -- checks and registration ---------------------------------------------------------------------------------

    async def _check(self, op: Operation, session: LocalSession) -> dict[str, Any]:
        env = self.env
        async with store_session(env.db) as conn:
            current = await workgroup_settings.load(conn, env.storage_key, op.wg_id)
            owner = session.owner_id
            connected = owner is not None and await records.github_connected(conn, owner)
            commits = await records.task_commits(conn, op.task_id) if op.type == "github.push" else set()
        request = checks.CheckRequest(
            op_type=op.type,
            params=op.params,
            session=session,
            settings=current,
            catalog=env.catalog,
            connected=connected,
            task_commits=frozenset(commits),
        )
        params = checks.check(request)
        if not self.enabled(op.type):
            raise CheckFailed("unknown_type", message="The operation type is not enabled on this device.")
        precheck = self.prechecks.get(op.type)
        found = PrecheckResult()
        if precheck is not None:
            try:
                found = await precheck(PrecheckRequest(env.context, op, session, current, params))
            except CheckFailed:
                raise
            except Exception as exc:
                self.line(f"precheck_failed operation_id={op.id} error={type(exc).__name__}")
                raise CheckFailed("upstream_error", message="A pre-check of the operation failed.") from None
        if any(name not in found.dedupe for name in NEEDS_PRECHECK.get(op.type, ())):
            raise CheckFailed("unknown_type", message="The operation type is not enabled on this device.")
        return {"params": params, "dedupe": dict(found.dedupe), "data": dict(found.data)}

    async def _prepare(self, op: Operation, deadline: float) -> Operation | None:
        """Checks and the stored registration body; None when no workgroup key arrived before `deadline`."""
        error = op.local_error
        checked: dict[str, Any] = {}
        session = await self._session(op)
        if error is None and session is None:
            error = CheckFailed("not_found", message="The session is not known on this device.").error()
        if error is None:
            assert session is not None
            try:
                checked = await self._check(op, session)
            except CheckFailed as failed:
                error = failed.error()
        while True:
            try:
                async with self.env.transaction() as conn:
                    built = await register.build(
                        conn,
                        sealer=self.sealer,
                        storage_key=self.env.storage_key,
                        op=op,
                        at=self.env.at(),
                        error_code=None if error is None else str(error["code"]),
                        params=checked.get("params", op.params),
                        extra=checked.get("dedupe", {}),
                    )
                    if built.size_exceeded and error is None:
                        error = {"code": "size_exceeded", "message": "The parameters are too large."}
                    if error is not None and op.local_error is None:
                        checked = {"error": error}
                    slot = built.body["dedupe"]
                    tag = None if slot is None else slot["tag"]
                    await records.update(conn, op.id, registration=built.body, checked=checked, dedupe_tag=tag)
                return await self._reload(op.id)
            except register.NoKey:
                remaining = deadline - self.env.clock()
                if remaining <= 0:
                    return None
                self.line(f"waiting_for_key operation_id={op.id}")
                await self.sleep(min(KEY_WAIT_S, remaining))

    async def _register(self, op: Operation) -> Operation:
        deadline = self.env.clock() + max(0.0, EXPIRY_S - self.elapsed(op))
        if op.registration is None:
            prepared = await self._prepare(op, deadline)
            if prepared is None:
                return await self._expire(op)
            op = prepared
        assert op.registration is not None
        decision = await self.registrar.send(op.id, op.registration, deadline)
        if decision is None:
            return await self._expire(op)
        return await self._decide(op, decision)

    async def _store(self, write: Callable[[Any], Awaitable[None]]) -> None:
        """One transaction, tried `STORE_ATTEMPTS` times on store errors (1 s, 2 s): an outcome that exists must not
        be lost to a dropped connection (the next scan would find the row `executing` and fail it `timeout`)."""
        attempt = 0
        while True:
            try:
                async with self.env.transaction() as conn:
                    await write(conn)
                return
            except psycopg.Error as exc:
                attempt += 1
                self.line(f"store_failed attempt={attempt} error={type(exc).__name__}")
                if attempt == STORE_ATTEMPTS:
                    raise
                await self.sleep(ANSWER_BACKOFF_S * 2 ** (attempt - 1))

    async def _final(
        self,
        op: Operation,
        state: str,
        answer: dict[str, Any],
        *,
        error_code: str | None,
        retryable: bool | None,
        event: bool = True,
        transition: Mapping[str, Any] | None = None,
    ) -> Operation:
        """A final state the hub decided (`rejected`, `denied`), an expiry or a refusal: the `operation.result` event
        with the error (when the hub knows the operation, `event`), the transition (`expired`) and the answer."""
        fields: dict[str, object] = {"state": state, "error_code": error_code, "answer": answer}
        if retryable is not None:
            fields["retryable"] = retryable
        if op.local_error is not None:
            # the guest answered the session itself (`rejected` at once); nothing is sent again
            fields["answered_at"] = self.now()
        at, event_id = self.env.at(), uuid7()
        error = answer.get("error")

        async def write(conn: Any) -> None:
            if event:
                body = {"operation_id": op.id, "status": state, "error": error}
                await self.env.outbox.enqueue(conn, "event", self._result_draft(op, event_id, body, at))
                fields["result_event_id"] = event_id
            if transition is not None:
                # a transition is only written for an operation the hub knows, always after its result event
                item = {**transition, "result_event_id": event_id, "at": at}
                await self.env.outbox.enqueue(conn, "operation_transition", item)
            await records.update(conn, op.id, **fields)

        await self._store(write)
        self.line(f"final operation_id={op.id} state={state} error_code={error_code}")
        return await self._reload(op.id)

    def _local_error(self, op: Operation) -> dict[str, Any] | None:
        if op.local_error is not None:
            return op.local_error
        error = (op.checked or {}).get("error")
        return error if isinstance(error, dict) else None

    async def _decide(self, op: Operation, decision: register.Decision) -> Operation:
        local = self._local_error(op)
        if local is not None and decision.state in ("accepted", "duplicate"):
            return await self._contradicted(op, decision, clean_error(local))
        if decision.state == "accepted":
            async with self.env.transaction() as conn:
                await records.update(conn, op.id, state="accepted", child_session_id=decision.child_session_id)
            return await self._reload(op.id)
        if decision.state == "duplicate":
            async with self.env.transaction() as conn:
                await records.update(conn, op.id, state="duplicate", duplicate_of=decision.duplicate_of)
            return await self._reload(op.id)
        if decision.state == register.REFUSED:
            code = "rights_denied" if decision.status == 403 else "upstream_error"
            error = {"code": code, "message": "The hub refused the registration."}
            answer = {"operation_id": op.id, "status": "failed", "error": error}
            return await self._final(op, "failed", answer, error_code=code, retryable=False, event=False)
        assert decision.error_code is not None
        error = {"code": decision.error_code}
        if decision.state == "rejected" and local is not None:
            error = clean_error(local)
        answer = {"operation_id": op.id, "status": decision.state, "error": error}
        return await self._final(op, decision.state, answer, error_code=decision.error_code, retryable=None)

    async def _contradicted(self, op: Operation, decision: register.Decision, error: dict[str, Any]) -> Operation:
        """The hub accepted (or found a duplicate for) an operation registered as rejected: the hub is not trusted
        (00 §3 B2), so it is never executed and no original's result is returned. An `accepted` one is ended
        `failed` with the local code (event and transition release the hub's slot); the session gets `rejected`."""
        self.line(f"hub_contradiction operation_id={op.id} decision={decision.state}")
        answer = {"operation_id": op.id, "status": "rejected", "error": error}
        if decision.state == "duplicate":
            return await self._final(op, "rejected", answer, error_code=error["code"], retryable=None)
        transition = {"operation_id": op.id, "state": "failed", "error_code": error["code"], "retryable": False}
        failed = {"code": error["code"], "retryable": False}
        result = {**answer, "status": "failed", "error": failed}
        op = await self._final(op, "failed", result, error_code=error["code"], retryable=False, transition=transition)
        async with self.env.transaction() as conn:
            await records.update(conn, op.id, answer=answer)
        return await self._reload(op.id)

    async def _expire(self, op: Operation, code: str = "hub_unreachable") -> Operation:
        """`expired`: no decision within 10 min (`hub_unreachable`), or `accepted` too long (`timeout`). When the
        registration was sent, the hub may know the operation: the event and the `expired` transition release its
        slot (a hub that does not know it skips the items)."""
        message = "The hub could not be reached." if code == "hub_unreachable" else "The operation was not started."
        error = {"code": code, "message": message, "retryable": True}
        answer = {"operation_id": op.id, "status": "failed", "error": error}
        known = op.registration is not None
        transition = {"operation_id": op.id, "state": "expired", "error_code": code} if known else None
        return await self._final(
            op, "expired", answer, error_code=code, retryable=True, event=known, transition=transition
        )

    # -- execution -----------------------------------------------------------------------------------------------

    async def _execute(self, op: Operation) -> Operation:
        executor = self.executors.get(op.type)
        session = await self._session(op)
        if executor is None or session is None:
            return await self._finish(op, failure=OperationFailed("unknown_type" if session else "not_found"))
        transition = {"operation_id": op.id, "state": "executing", "at": self.env.at()}
        async with self.env.transaction() as conn:
            await self.env.outbox.enqueue(conn, "operation_transition", transition)
            await records.update(conn, op.id, state="executing")
        op = await self._reload(op.id)
        result, failure = await self._attempts(op, session, executor)
        return await self._finish(op, result=result, failure=failure)

    async def _attempts(
        self, op: Operation, session: LocalSession, executor: Executor
    ) -> tuple[dict[str, Any] | None, OperationFailed | None]:
        checked = op.checked or {}
        params = checked.get("params", op.params)
        data = checked.get("data", {})
        attempt = 0
        while True:
            attempt += 1
            async with self.env.transaction() as conn:
                await records.update(conn, op.id, attempt=attempt)
            ctx = OperationContext(self.env.context, op, session, params, data, attempt, self.env)
            try:
                result: object = await executor(ctx)
            except OperationFailed as failed:
                failure = failed
            except Exception as exc:
                self.line(f"executor_failed operation_id={op.id} error={type(exc).__name__}")
                return None, OperationFailed("upstream_error")
            else:
                if isinstance(result, dict):
                    return result, None
                return None, OperationFailed("upstream_error")
            if not failure.retryable:
                return None, failure
            retry_after = failure.retry_after or 0.0
            if attempt > len(RETRY_DELAYS) or retry_after > RETRY_AFTER_MAX_S:
                return None, OperationFailed(failure.code, retryable=True, message=failure.message)
            self.line(f"retry operation_id={op.id} attempt={attempt} code={failure.code}")
            await self.sleep(max(RETRY_DELAYS[attempt - 1], retry_after))

    def _result_draft(self, op: Operation, event_id: str, body: dict[str, Any], at: str) -> dict[str, Any]:
        return event_draft(
            id=event_id,
            org=op.org_id,
            scope="wg",
            scope_id=op.wg_id,
            wg=op.wg_id,
            task=op.task_id,
            session=op.session_id,
            kind="operation.result",
            ref=op.id,
            ts=at,
            body=body,
        )

    async def _finish(
        self, op: Operation, *, result: dict[str, Any] | None = None, failure: OperationFailed | None = None
    ) -> Operation:
        """`operation.result` event, then the transition, and the stored answer, in one transaction."""
        at, event_id = self.env.at(), uuid7()
        if failure is None:
            assert result is not None
            draft = self._result_draft(
                op, event_id, {"operation_id": op.id, "status": "succeeded", "result": result}, at
            )
            try:
                check_draft(draft)
            except CryptoError:
                failure = OperationFailed("size_exceeded", message="The result is too large.")
        if failure is None:
            assert result is not None
            status = "duplicate" if result.get("existing") is True else "succeeded"
            answer: dict[str, Any] = {"operation_id": op.id, "status": status, "result": result}
            transition: dict[str, Any] = {"operation_id": op.id, "state": "succeeded", "result_event_id": event_id}
            fields: dict[str, object] = {"state": "succeeded", "result": result}
        else:
            code = known_code(failure.code)
            error: dict[str, Any] = {"code": code, "retryable": failure.retryable}
            message = message_text(failure.message)
            if message is not None:
                error["message"] = message
            body = {"operation_id": op.id, "status": "failed", "error": error}
            draft = self._result_draft(op, event_id, body, at)
            answer = {"operation_id": op.id, "status": "failed", "error": error}
            transition = {
                "operation_id": op.id,
                "state": "failed",
                "error_code": code,
                "retryable": failure.retryable,
                "result_event_id": event_id,
            }
            fields = {"state": "failed", "error_code": code, "retryable": failure.retryable}
        transition["at"] = at

        async def write(conn: Any) -> None:
            await self.env.outbox.enqueue(conn, "event", draft)
            await self.env.outbox.enqueue(conn, "operation_transition", transition)
            await records.update(conn, op.id, result_event_id=event_id, answer=answer, **fields)

        await self._store(write)
        self.line(f"final operation_id={op.id} state={fields['state']} error_code={fields.get('error_code')}")
        return await self._reload(op.id)

    # -- duplicates ----------------------------------------------------------------------------------------------

    async def _original(self, conn: Any, op: Operation) -> results.Outcome | None:
        assert op.duplicate_of is not None
        return await results.original_outcome(
            conn, self.env.storage_key, op.duplicate_of, wg_id=op.wg_id, op_type=op.type
        )

    async def _original_is_local(self, original: str) -> bool:
        return await self.load(original) is not None

    async def _duplicate(self, op: Operation) -> Operation:
        assert op.duplicate_of is not None
        deadline = self.env.clock() + max(0.0, MCP_WAIT_S - self.elapsed(op))
        local = await self._original_is_local(op.duplicate_of)
        while True:
            async with store_session(self.env.db) as conn:
                outcome = await self._original(conn, op)
            remaining = deadline - self.env.clock()
            if outcome is not None or remaining <= 0:
                break
            if not local:
                try:
                    await self.refresh()
                except Exception as exc:
                    self.line(f"refresh_failed operation_id={op.id} error={type(exc).__name__}")
            await self.sleep(min(DUPLICATE_POLL_S, remaining))
        answer = self.duplicate_answer(op, outcome)
        async with self.env.transaction() as conn:
            await records.update(conn, op.id, answer=answer)
        return await self._reload(op.id)

    @staticmethod
    def duplicate_answer(op: Operation, outcome: results.Outcome | None) -> dict[str, Any]:
        if outcome is None:
            return {"operation_id": op.id, "status": "pending", "duplicate_of": op.duplicate_of}
        if outcome.status == "succeeded":
            return {
                "operation_id": op.id,
                "status": "duplicate",
                "duplicate_of": op.duplicate_of,
                "result": outcome.result,
            }
        assert outcome.error is not None
        return {"operation_id": op.id, "status": "failed", "error": clean_error(outcome.error)}

    # -- answer --------------------------------------------------------------------------------------------------

    async def _deliver(self, op: Operation) -> None:
        assert op.answer is not None
        row = await vm_records.get(self.env.db, op.vm_id)
        delivered = row is None
        if row is None:
            self.line(f"answer_unroutable operation_id={op.id} vm_id={op.vm_id}")
        for attempt in range(0 if row is None else ANSWER_ATTEMPTS):
            assert row is not None
            try:
                async with await self.env.client(row) as client:
                    await client.answer_pending(op.pending_id, {"status": "ok", "result": op.answer})
                delivered = True
            except GuestRefused as exc:
                self.line(f"answer_refused operation_id={op.id} error={exc.error}")
                delivered = True
            except GuestError as exc:
                self.line(f"answer_failed operation_id={op.id} attempt={attempt + 1} error={exc.code}")
                if attempt + 1 < ANSWER_ATTEMPTS:
                    await self.sleep(ANSWER_BACKOFF_S * 2**attempt)
                continue
            break
        if delivered:
            async with self.env.transaction() as conn:
                await records.update(conn, op.id, answered_at=self.now())
        late = op.answer.get("status") != "pending" and self.elapsed(op) >= PENDING_AFTER_S
        if delivered and late and op.local_error is None and op.id not in self.finished:
            self.finished.add(op.id)
            await self._notify(op)

    async def _notify(self, op: Operation) -> None:
        if self.notify is None:
            self.line(f"finished_unrouted operation_id={op.id} session_id={op.session_id}")
            return
        try:
            await self.notify(self.env.context, op)
        except Exception as exc:
            self.line(f"notify_failed operation_id={op.id} error={type(exc).__name__}")

    # -- get_operation (RUN-08) ----------------------------------------------------------------------------------

    async def answer(self, operation_id: str) -> dict[str, Any] | None:
        """The current MCP outcome of an operation of this device (`pending` while not final), None when unknown."""
        op = await self.load(operation_id)
        if op is None:
            return None
        if op.answer is not None and op.answer.get("status") != "pending":
            return op.answer
        if op.duplicate_of is not None:
            async with store_session(self.env.db) as conn:
                outcome = await self._original(conn, op)
            return self.duplicate_answer(op, outcome)
        return {"operation_id": op.id, "status": "pending"}

    async def wait_answer(self, operation_id: str, seconds: float) -> dict[str, Any] | None:
        """`answer` once it is final, or the last one after `seconds` (polled every second)."""
        deadline = self.env.clock() + seconds
        while True:
            found = await self.answer(operation_id)
            remaining = deadline - self.env.clock()
            if found is None or found["status"] != "pending" or remaining <= 0:
                return found
            await self.sleep(min(DUPLICATE_POLL_S, remaining))


def existing(context: Any) -> Pipeline | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, Pipeline) else None


def install(context: Any, pipeline: Pipeline) -> Pipeline:
    setattr(context, ATTRIBUTE, pipeline)
    return pipeline


def pipeline_for(context: Any) -> Pipeline | None:
    """The pipeline of this daemon over the session area's collaborators; None while the device is not enrolled."""
    found = existing(context)
    if found is not None:
        return found
    from agent_runner.inbox.pull import inbox_for  # noqa: PLC0415 - the inbox imports the consumers of this area
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points
    from agent_runner.sync.runtime import runtime  # noqa: PLC0415

    service = service_for(context)
    sync = runtime(context)
    if service is None or sync is None:
        return None
    package = context.host.package
    inbox = inbox_for(context)

    async def refresh() -> object:
        return None if inbox is None else await inbox.pull_all()

    built = Pipeline(
        service.env,
        client=sync.client,
        sealer=sync.pusher.sealer,
        executors=registry.executors(package),
        prechecks=prechecks(package),
        notify=finished_notifier(package),
        refresh=refresh,
        sleep=context.host.sleep,
    )
    return install(context, built)
