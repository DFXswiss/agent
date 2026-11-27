"""Device requests of the runner (05-runner.md sections 2.4 and 11.1, 03-api.md sections 4.10, 6.1 and 6.2,
04-crypto.md section 4.5).

A request is announced by the hint `request {request_id}` or listed in `requests` of the desired state. The service
(`service_for(context)`, one per daemon):

1. **fetches** `GET /v1/device/requests/{id}` (`{envelope}`);
2. **verifies** (05 section 2.4) the envelope form and header (`header_invalid`), that it is the request asked for
   (`header_invalid`) and addressed to this device (`device_not_assigned`), that the user is an active user of an
   acting org whose verified log makes this device eligible for the user's `CK` (`key_unknown` for an unknown user,
   `device_not_assigned` when this device does not serve the user), the user's signature (`signature_invalid`), the
   time (`time_skew`: older than 15 min = expired, or more than 120 s in the future), then **decrypts** with the
   device X25519 key (`decrypt_failed`, `hpke_short`) and requires a JSON object (`request_invalid`). A refused
   request is ignored (never answered: its sender is not trusted), audited once (`runner.inbound_rejected`, source
   `request`, metadata only), counted and remembered as `rejected` in `runner.device_request`. Refusals that can pass
   later - an unknown user or a user not (yet) served, a time in the future, a follow-up whose root has not been
   processed - are audited once per code but not remembered, so the request is verified again when it is offered
   again. The user check (`key_unknown`, `device_not_assigned` of the user) depends only on the device's copy of the
   logs, which may be behind the hub's: a new user's first request can arrive before the copy shows the user. Such a
   refusal is judged only against logs refreshed after the request was fetched (decisions/KOR-17.md, as
   decisions/HOT-04.md for key wraps): the desired state is asked for again and the platform log and the log of every
   org it offers - also an org the device has not verified before, such as the private org a new user just created -
   are verified up to the offered heads (reconcile steps 1 and 2; at most once per `REFRESH_INTERVAL_S`; one refresh
   covers every request fetched before it) and the request is verified again. Entitled now: handled, no audit. Still
   refused: audited as above. While a refresh is not due yet or failed, the request is neither handled nor audited
   but offered again later (`defer`); after `REFRESH_TRIES` failed refreshes it is judged against the copy alone;
3. **dispatches** a root request (`follows` empty) to the handler of its kind (`<area>/requests.py:HANDLERS`,
   `registry.request_handlers`) in its own asyncio task (`Flow`). At most one flow per user, kind and slot is active
   (the slot of a kind is a field of the **decrypted** body, `SLOTS`: `claude.login` - its `subscription_id`, so the
   logins of different subscriptions run side by side, KOR-73, ADR-100 section 2; `vnc.open` - the `id` of its
   screen, so the owner holds one connection per screen, ADR-103; a body without it is a slot of its own): a newer
   root (header time, then ID) replaces the running flow of its slot (the hub has already failed the older chain),
   an older one is answered `{step:"failed", code:"busy"}`; so is a root of a new slot while `MAX_SLOTS` flows of the
   owner and kind run. A kind without handler is answered `failed` `upstream_error`. A follow-up request (`follows` =
   root ID) goes to the running flow of its root when owner and kind match (`Flow.follow_ups`,
   `Flow.wait_follow_up`), else it is refused like a forged request (`forbidden`);
4. **answers** with encrypted, signed responses (`Flow.respond`): HPKE to the requester's X25519 key, signed by
   the device, `seq` 1..n per request without a gap, `final:true` on the last. The next `seq` is stored before the
   response is sent and the same envelope is resent on transport errors (the hub accepts an identical retry); a number
   used up without reaching the hub (cancellation, crash) is healed from the hub's `seq_conflict` `expected_seq`;
5. **ends** a flow: the handler's final response; the request's 15 minutes (`{step:"failed", code:"timeout"}`); a
   handler error or a handler that returns without a final response (`upstream_error`); the hub no longer listing
   the root in two consecutive desired states (answered, expired or replaced: the flow is abandoned without answer);
   shutdown (`device_shutdown`, best effort; the row stays `active` until that answer arrived). A request that was
   active when the daemon stopped is answered `device_shutdown` when it is seen again.

Agents (ADR-082 sections 1 and 9.12): `header.user` may be the active agent of a user. The requester's key is then
the agent's own key from the verified log, the response is sealed to the agent's X25519 key, and the **owner** - whose
`CK` and connections a handler uses (`Flow.user`) - is the agent's user (`OrgState.acting`). An agent that is no agent
of its user any more (`revoked_agents`: superseded or removed in any verified log) is refused like an unknown user
(`key_unknown`). "At most one flow per user, kind and slot" and the follow-up match compare owners, so a user and the
user's agent continue each other's flows.

Local states (`runner.device_request.state`): `active`, `answered` (final response sent), `failed` (closed by the
hub or unanswerable), `abandoned` (no longer listed, replaced or cancelled), `rejected` (failed verification).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner import registry
from agent_runner.crypto import request as request_format
from agent_runner.crypto.encoding import b64u_decode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import AGENT, USER, Principal
from agent_runner.crypto.log.wraps import cred_readers, revoked_agents
from agent_runner.orgs import audit
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session
from agent_runner.reconcile import logs, state, wire
from agent_runner.reconcile.engine import Run
from agent_runner.reconcile.hints import wake
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import SyncRuntime, runtime

EXPIRY_S = 15 * 60
FUTURE_SKEW_S = 120
RESPOND_DELAYS = (1.0, 2.0, 4.0)
MISSING_LIMIT = 2
"""Consecutive desired states without the root after which a flow is abandoned."""
REFRESH_INTERVAL_S = 2.0
"""Least host-clock time between two log refreshes for requests ahead of the device's log copy (a flood of forged
requests of unknown users cannot make the device ask the hub for its logs more often)."""
REFRESH_TRIES = 3
"""Failed log refreshes after which such a request is judged against the device's copy alone."""
MAX_WAITING = 10_000
"""Bound of the memory of requests waiting for a log refresh (the oldest entry is dropped when reached)."""
MAX_SLOTS = 40
"""Flows of one owner and kind at the same time (the number of subscriptions a user may hold, ADR-100 section 2)."""
SLOTS = {"claude.login": "subscription_id", "vnc.open": "id"}
"""The body field that names the slot of a root of the kind (module docstring point 3)."""
ACTIVE, ANSWERED, FAILED, ABANDONED, REJECTED = "active", "answered", "failed", "abandoned", "rejected"
CLOSED = frozenset({ANSWERED, FAILED, ABANDONED, REJECTED})
ATTRIBUTE = "request_service"
NIL = str(uuid.UUID(int=0))

Handler = Callable[["Flow"], Awaitable[None]]


class Refused(Exception):
    """A request failed verification (`code` of `contracts/enums/error-codes.json`). A `transient` refusal may pass
    later (the device's copy of the logs is behind, the clocks drift, a follow-up arrived before its root): it is
    audited once but not remembered, so the request is verified again when it is offered again."""

    def __init__(self, code: str, *, transient: bool = False, log_dependent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient or log_dependent
        self.log_dependent = log_dependent
        """The refusal depends only on the device's copy of the logs (the user check): judged after a refresh."""


@dataclass
class Waiting:
    """A request whose log-dependent refusal waits for a log refresh."""

    seen: int
    """Number of the first fetch of the request that was refused this way (`RequestService.fetches`)."""
    failures: int = 0
    counted: int = 0
    """Number of the last refresh this request was judged after (`RequestService.refreshes`)."""


class RequestClosed(Exception):
    """The hub no longer accepts responses to the request (`code`), or could not be reached for one."""

    def __init__(self, code: str, expected_seq: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.expected_seq = expected_seq


@dataclass(frozen=True)
class Incoming:
    """A verified and decrypted request."""

    id: str
    user: str
    """The requester (`header.user`): a user or the user's agent."""
    kind: str
    follows: str
    ts: float
    body: dict[str, Any]
    user_kem: bytes
    """The requester's X25519 key (the responses are sealed to it)."""
    owner: str
    """The user the request acts for (`acting(user)`): whose `CK` and connections a handler uses."""

    @property
    def slot(self) -> object:
        """The subject of a root of its kind (`SLOTS`), None when the kind has none or the body does not name it."""
        name = SLOTS.get(self.kind)
        return None if name is None else self.body.get(name)


@dataclass
class FollowUp:
    """A follow-up request handed to its root's flow; answered with `Flow.respond(..., to=follow_up)`."""

    request: Incoming


@dataclass
class Flow:
    """One running root request and its handler's view of it."""

    service: RequestService
    request: Incoming
    deadline: float
    """Host clock (monotonic) time at which the request expires."""
    follow_ups: asyncio.Queue[FollowUp] = field(default_factory=asyncio.Queue)
    finished: bool = False
    missing: int = 0
    chain: list[str] = field(default_factory=list)
    """IDs of the follow-ups handed to this flow."""
    task: asyncio.Task[None] | None = None

    @property
    def context(self) -> Any:
        return self.service.context

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def user(self) -> str:
        """The owner: the user the request acts for (also for a request of the user's agent)."""
        return self.request.owner

    @property
    def requester(self) -> str:
        """The principal that signed the request (the user or the user's agent)."""
        return self.request.user

    @property
    def body(self) -> dict[str, Any]:
        return self.request.body

    def remaining(self) -> float:
        """Seconds until the request expires (host clock)."""
        return self.deadline - self.service.clock()

    async def sleep(self, seconds: float) -> None:
        await self.service.sleep(seconds)

    async def respond(self, body: Mapping[str, object], *, final: bool = False, to: FollowUp | None = None) -> None:
        """Send one response (to the root, or to a follow-up of this flow); `final` ends the flow's chain."""
        target = to.request if to is not None else self.request
        await self.service.respond(target, body, final=final)
        if final:
            self.finished = True

    async def wait_follow_up(self, seconds: float) -> FollowUp | None:
        """The next follow-up within `seconds` (real time), None otherwise."""
        try:
            return await asyncio.wait_for(self.follow_ups.get(), timeout=max(seconds, 0.0))
        except TimeoutError:
            return None


def parse_ms_time(text: str) -> float:
    """Unix seconds of a checked `YYYY-MM-DDTHH:MM:SS.mmmZ` header time."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


class RequestService:
    def __init__(self, context: Any, sync: SyncRuntime, handlers: Mapping[str, Handler]) -> None:
        self.context = context
        self.sync = sync
        self.handlers = dict(handlers)
        self.clock: Callable[[], float] = context.host.clock
        self.sleep: Callable[[float], Awaitable[None]] = context.host.sleep
        self.flows: dict[str, Flow] = {}
        """Running flows by root request ID."""
        self.routed: dict[str, Flow] = {}
        """Follow-up request ID -> the flow it was handed to."""
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.locks: dict[str, asyncio.Lock] = {}
        self.audited: set[tuple[str, str]] = set()
        """`(request ID, code)` of transient refusals already audited."""
        self.stopping = False
        self.fetches = 0
        """Requests fetched so far (numbers the fetches; a refresh covers every fetch before it started)."""
        self.refreshes = 0
        """Log refreshes so far."""
        self.refresh_covers = 0
        """`fetches` when the last refresh started."""
        self.refresh_at: float | None = None
        """Host clock time at which the last refresh started."""
        self.refresh_complete = False
        self.waiting: dict[str, Waiting] = {}
        """Requests whose log-dependent refusal waits for a refresh, by ID."""
        self.deferred: dict[str, float] = {}
        """Requests offered again by `timer`, with the host clock time at which they are due."""
        self.timer: asyncio.Task[None] | None = None

    # --- helpers -----------------------------------------------------------------------------------------------

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.requests {text}\n")
        self.context.log.flush()

    @property
    def device_id(self) -> str:
        return str(self.context.identity.device.device_id)

    def lock(self, request_id: str) -> asyncio.Lock:
        return self.locks.setdefault(request_id, asyncio.Lock())

    async def row(self, conn: AsyncConnection[Any], request_id: str) -> tuple[str, int] | None:
        cur = await conn.execute("SELECT state, last_seq FROM runner.device_request WHERE id = %s", (request_id,))
        found = await cur.fetchone()
        return None if found is None else (str(found[0]), int(found[1]))

    async def mark(self, request: Incoming, state: str) -> None:
        async with session(self.context.pool) as conn:
            await conn.execute(
                "INSERT INTO runner.device_request (id, kind, user_id, state) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                (request.id, request.kind, request.user, state),
            )

    # --- intake ------------------------------------------------------------------------------------------------

    def offer(self, request_id: str) -> None:
        """Process the request soon (hint or desired state)."""
        self.queue.put_nowait(request_id)

    def listed(self, request_ids: Sequence[str]) -> None:
        """The active requests of a desired state: offer each; abandon flows missing `MISSING_LIMIT` times in a row."""
        present = set(request_ids)
        for flow in list(self.flows.values()):
            flow.missing = 0 if flow.id in present else flow.missing + 1
            if flow.missing >= MISSING_LIMIT:
                self.line(f"abandoned request_id={flow.id} reason=not_listed")
                self.cancel(flow)
        for request_id in request_ids:
            self.offer(request_id)

    def cancel(self, flow: Flow) -> None:
        """Stop a flow without answer; its bookkeeping is dropped at once (a task cancelled before it ran never
        reaches its own clean-up)."""
        flow.finished = True
        self.forget(flow)
        if flow.task is not None:
            flow.task.cancel()

    def forget(self, flow: Flow) -> None:
        """Drop the flow and its routed follow-ups from memory."""
        if self.flows.get(flow.id) is flow:
            del self.flows[flow.id]
        for request_id in flow.chain:
            self.routed.pop(request_id, None)

    async def process(self, request_id: str) -> None:
        """Fetch, verify and dispatch one request (see the module docstring); never raises for a single request."""
        if request_id in self.flows or request_id in self.routed:
            return
        async with session(self.context.pool) as conn:
            found = await self.row(conn, request_id)
        if found is not None and found[0] in CLOSED:
            return
        try:
            answer = await self.sync.client.request("GET", f"/v1/device/requests/{request_id}")
        except (HubRefusal, HubTransportError) as exc:
            code = exc.code if isinstance(exc, HubRefusal) else exc.reason
            self.line(f"fetch_failed request_id={request_id} code={code}")
            return
        self.fetches += 1
        try:
            request = await self.checked(request_id, answer.get("envelope"), self.fetches)
        except Refused as refused:
            self.waiting.pop(request_id, None)
            await self.reject(request_id, answer.get("envelope"), refused)
            return
        if request is None:
            return  # waits for a log refresh
        self.waiting.pop(request_id, None)
        if found is not None:
            # active before this daemon started: the flow's state is gone
            await self.close_unanswerable(request, "device_shutdown")
            return
        await (self.follow_up(request) if request.follows else self.start(request))

    async def checked(self, request_id: str, envelope: object, fetch: int) -> Incoming | None:
        """The verified request (`Refused` when refused); None while a log-dependent refusal waits for a refresh."""
        try:
            return await self.verify(request_id, envelope)
        except Refused as ahead:
            if not ahead.log_dependent or (request_id, ahead.code) in self.audited:
                raise
        return await self.judge(request_id, envelope, fetch)

    async def judge(self, request_id: str, envelope: object, fetch: int) -> Incoming | None:
        """Verify a request refused against the device's log copy again after a log refresh that started after the
        request was first fetched (`fetch`). Returns the verified request; None when it waits (refresh not due yet, or
        failed fewer than `REFRESH_TRIES` times: offered again later); raises `Refused` when judged."""
        waiting = self.waiting.get(request_id)
        if waiting is None:
            if len(self.waiting) >= MAX_WAITING:
                del self.waiting[next(iter(self.waiting))]
            waiting = self.waiting[request_id] = Waiting(seen=fetch)
        if self.refresh_covers < waiting.seen or waiting.counted == self.refreshes:
            # no refresh since the request came in, or the last one failed and was counted for it already
            due = 0.0 if self.refresh_at is None else self.refresh_at + REFRESH_INTERVAL_S - self.clock()
            if due > 0:
                self.defer(request_id, due)
                return None
            await self.refresh()
        waiting.counted = self.refreshes
        try:
            return await self.verify(request_id, envelope)
        except Refused as again:
            if self.refresh_complete or not again.log_dependent:
                raise
            waiting.failures += 1
            if waiting.failures >= REFRESH_TRIES:
                raise
            self.defer(request_id, REFRESH_INTERVAL_S)
            return None

    async def refresh(self) -> None:
        """Reconcile steps 1 and 2: the desired state, then the platform log and every offered org's log up to the
        offered heads (new orgs included); complete when both steps ran through for an active, certified device."""
        self.refreshes += 1
        self.refresh_covers = self.fetches
        self.refresh_at = self.clock()
        run = Run(context=self.context, sync=self.sync, book=book_for(self.context))
        try:
            await state.fetch_state(run)
            if run.halted is None:
                await logs.sync_logs(run)
            self.refresh_complete = run.halted is None
        except (HubRefusal, HubTransportError, wire.ResponseInvalid):
            self.refresh_complete = False
        self.line(f"logs_refreshed complete={str(self.refresh_complete).lower()}")

    def defer(self, request_id: str, seconds: float) -> None:
        """Offer the request again after `seconds` (host clock); one timer releases every deferred request when due."""
        self.deferred[request_id] = self.clock() + seconds
        if self.timer is None or self.timer.done():
            self.timer = asyncio.create_task(self.release(), name="requests-deferred")

    async def release(self) -> None:
        while self.deferred:
            now = self.clock()
            due = min(self.deferred.values())
            if due > now:
                await self.sleep(due - now)
                continue
            for request_id in sorted(r for r, at in self.deferred.items() if at <= now):
                del self.deferred[request_id]
                self.offer(request_id)

    async def reject(self, request_id: str, envelope: object, refused: Refused) -> None:
        """Audit a refusal (a transient one once per request and code, and ask for a reconcile run so the logs catch
        up); remember a final one as `rejected`."""
        code = refused.code
        if refused.transient:
            if (request_id, code) in self.audited:
                return
            self.audited.add((request_id, code))
            await wake(self.context, {})
        header = envelope.get("header") if isinstance(envelope, dict) else None
        kind = header.get("kind") if isinstance(header, dict) else None
        user = header.get("user") if isinstance(header, dict) else None
        async with session(self.context.pool) as conn, conn.transaction():
            await audit.reject(
                self.context,
                self.sync,
                conn,
                source="request",
                code=code,
                target_kind="device_request",
                target_id=request_id,
            )
            if not refused.transient:
                await conn.execute(
                    "INSERT INTO runner.device_request (id, kind, user_id, state) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                    (
                        request_id,
                        kind if isinstance(kind, str) and len(kind) <= 64 else "unknown",
                        _uuid_or_nil(user),
                        REJECTED,
                    ),
                )
        self.sync.pusher.notify()

    # --- verification ------------------------------------------------------------------------------------------

    def user_principal(self, book: OrgBook, user: str) -> tuple[Principal, str]:
        """The requester - an active user, or the active agent of one that is not revoked - of an acting org that
        makes this device eligible for the owner's `CK`, and the owner (the user, or the agent's user)."""
        orgs = book.acting_orgs()
        states = [log.state for org in orgs if (log := book.log(org)) is not None]
        excluded = {device for org in orgs for device in book.excluded(org)}
        principal = next(
            (found for state in states if (found := state.member(user)) is not None and found.kind in (USER, AGENT)),
            None,
        )
        if principal is None or principal.id in revoked_agents(states):
            raise Refused("key_unknown", log_dependent=True)
        owner = principal.parent or principal.id
        if self.device_id not in cred_readers(states, owner, excluded):
            raise Refused("device_not_assigned", log_dependent=True)
        return principal, owner

    async def verify(self, request_id: str, envelope: object) -> Incoming:
        try:
            data = envelope if isinstance(envelope, dict) else {}
            header = request_format.check_request_header(data.get("header"))
        except CryptoError as error:
            raise Refused(error.code) from None
        if header["id"] != request_id:
            raise Refused("header_invalid")
        if header["device"] != self.device_id:
            raise Refused("device_not_assigned")
        book = book_for(self.context)
        async with session(self.context.pool) as conn:
            await book.ensure_loaded(conn)
        principal, owner = self.user_principal(book, str(header["user"]))
        try:
            plaintext = request_format.open_request(
                self.context.identity.keys.kem_key(), envelope, b64u_decode(principal.sig)
            )
        except CryptoError as error:
            raise Refused(error.code) from None
        ts = parse_ms_time(str(header["ts"]))
        now = self.sync.wall()
        if now - ts > EXPIRY_S:
            raise Refused("time_skew")
        if ts - now > FUTURE_SKEW_S:
            raise Refused("time_skew", transient=True)
        try:
            body = parse_json(plaintext)
        except CryptoError:
            body = None
        if not isinstance(body, dict):
            raise Refused("request_invalid")
        return Incoming(
            id=request_id,
            user=str(header["user"]),
            kind=str(header["kind"]),
            follows=str(header["follows"]),
            ts=ts,
            body=cast(dict[str, Any], body),
            user_kem=b64u_decode(principal.kem),
            owner=owner,
        )

    # --- dispatch ----------------------------------------------------------------------------------------------

    async def follow_up(self, request: Incoming) -> None:
        root = self.flows.get(request.follows)
        header = {"header": {"kind": request.kind, "user": request.user}}
        if root is None:
            # the root may not have been processed yet: refused for now, unless it is closed here already
            async with session(self.context.pool) as conn:
                found = await self.row(conn, request.follows)
            closed = found is not None and found[0] in CLOSED
            await self.reject(request.id, header, Refused("forbidden", transient=not closed))
            return
        if root.finished or (root.user, root.request.kind) != (request.owner, request.kind):
            await self.reject(request.id, header, Refused("forbidden"))
            return
        await self.mark(request, ACTIVE)
        self.routed[request.id] = root
        root.chain.append(request.id)
        root.follow_ups.put_nowait(FollowUp(request))

    async def start(self, request: Incoming) -> None:
        wanted = (request.owner, request.kind, request.slot)
        running = next(
            (f for f in self.flows.values() if not f.finished and (f.user, f.request.kind, f.request.slot) == wanted),
            None,
        )
        if running is not None:
            if (running.request.ts, running.id) > (request.ts, request.id):
                self.line(f"busy request_id={request.id}")
                await self.close_unanswerable(request, "busy")
                return
            self.line(f"abandoned request_id={running.id} reason=replaced")
            self.cancel(running)
        elif sum(not f.finished and (f.user, f.request.kind) == wanted[:2] for f in self.flows.values()) >= MAX_SLOTS:
            self.line(f"busy request_id={request.id} reason=slots")
            await self.close_unanswerable(request, "busy")
            return
        handler = self.handlers.get(request.kind)
        if handler is None:
            self.line(f"no_handler request_id={request.id} kind={request.kind}")
            await self.close_unanswerable(request, "upstream_error")
            return
        await self.mark(request, ACTIVE)
        deadline = self.clock() + (request.ts + EXPIRY_S - self.sync.wall())
        flow = Flow(service=self, request=request, deadline=deadline)
        self.flows[request.id] = flow
        flow.task = asyncio.create_task(self.run_flow(flow, handler), name=f"request-{request.id}")

    async def close_unanswerable(self, request: Incoming, code: str) -> None:
        """Answer `{step:"failed", code}` (final) for a request that gets no flow."""
        try:
            await self.respond(request, {"step": "failed", "code": code}, final=True)
        except RequestClosed as closed:
            self.line(f"respond_failed request_id={request.id} code={closed.code}")
            await self.mark(request, FAILED)

    async def run_flow(self, flow: Flow, handler: Handler) -> None:
        outcome = ANSWERED
        try:
            await asyncio.wait_for(handler(flow), timeout=max(flow.remaining(), 0.0))
            if not flow.finished:
                await flow.respond({"step": "failed", "code": "upstream_error"}, final=True)
        except asyncio.CancelledError:
            # at shutdown the row stays active: the `device_shutdown` answer follows, or the next start gives it
            outcome = ACTIVE if self.stopping else ABANDONED
            raise
        except TimeoutError:
            outcome = await self.fail_flow(flow, "timeout")
        except RequestClosed as closed:
            self.line(f"closed request_id={flow.id} code={closed.code}")
            outcome = FAILED
        except Exception as exc:
            self.line(f"handler_failed request_id={flow.id} error={type(exc).__name__}")
            outcome = await self.fail_flow(flow, "upstream_error")
        finally:
            await self.end_flow(flow, outcome)

    async def fail_flow(self, flow: Flow, code: str) -> str:
        if flow.finished:
            return ANSWERED
        try:
            await flow.respond({"step": "failed", "code": code}, final=True)
        except RequestClosed as closed:
            self.line(f"respond_failed request_id={flow.id} code={closed.code}")
            return FAILED
        return ANSWERED

    async def end_flow(self, flow: Flow, outcome: str) -> None:
        flow.finished = True
        self.forget(flow)
        with contextlib.suppress(Exception):
            async with session(self.context.pool) as conn:
                await conn.execute(
                    "UPDATE runner.device_request SET state = %s, updated_at = now() WHERE id = ANY(%s::uuid[])",
                    (outcome, [flow.id, *flow.chain]),
                )

    # --- responses ---------------------------------------------------------------------------------------------

    async def respond(self, request: Incoming, body: Mapping[str, object], *, final: bool) -> None:
        """Encrypt, sign and send the next response to `request` (`RequestClosed` when the hub refuses it).

        The number is stored before sending. A number used up without reaching the hub (cancellation or a crash during
        the send) leaves a gap the hub answers with `409 seq_conflict` and `expected_seq`: the response is then sealed
        once more with the number the hub expects (only a lower one: a number is never reused upwards)."""
        async with self.lock(request.id):
            async with session(self.context.pool) as conn:
                await conn.execute(
                    "INSERT INTO runner.device_request (id, kind, user_id, state) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (request.id, request.kind, request.user, ACTIVE),
                )
                cur = await conn.execute(
                    "UPDATE runner.device_request SET last_seq = last_seq + 1, updated_at = now() WHERE id = %s "
                    "RETURNING last_seq",
                    (request.id,),
                )
                seq = int(cast(tuple[int], await cur.fetchone())[0])
            try:
                await self.send(request.id, self.seal(request, body, seq=seq, final=final))
            except RequestClosed as closed:
                expected = closed.expected_seq
                if expected is None or not 1 <= expected < seq:
                    raise
                self.line(f"respond_gap request_id={request.id} seq={seq} expected={expected}")
                async with session(self.context.pool) as conn:
                    await conn.execute(
                        "UPDATE runner.device_request SET last_seq = %s, updated_at = now() WHERE id = %s",
                        (expected, request.id),
                    )
                await self.send(request.id, self.seal(request, body, seq=expected, final=final))
            if final:
                await self.mark(request, ANSWERED)
                self.locks.pop(request.id, None)

    def seal(self, request: Incoming, body: Mapping[str, object], *, seq: int, final: bool) -> dict[str, object]:
        header = {
            "v": request_format.VERSION,
            "id": uuid7(),
            "user": request.user,
            "device": self.device_id,
            "kind": request.kind,
            "in_reply_to": request.id,
            "seq": seq,
            "final": final,
            "ts": time_text(self.sync.wall()),
            "enc_alg": request_format.ENC_ALG,
        }
        return request_format.seal_response(
            header, request.user_kem, body_json(dict(body)), self.context.identity.keys.signing_key()
        )

    async def send(self, request_id: str, envelope: Mapping[str, object]) -> None:
        target = f"/v1/device/requests/{request_id}/responses"
        attempt = 0
        while True:
            try:
                await self.sync.client.request("POST", target, {"envelope": dict(envelope)})
                return
            except HubRefusal as refusal:
                expected = refusal.detail.get("expected_seq") if refusal.code == "seq_conflict" else None
                raise RequestClosed(
                    refusal.code, expected if isinstance(expected, int) and not isinstance(expected, bool) else None
                ) from None
            except HubTransportError as error:
                self.line(f"respond_retry request_id={request_id} reason={error.reason}")
                if attempt == len(RESPOND_DELAYS):
                    raise RequestClosed("hub_unreachable") from None
                await self.sleep(RESPOND_DELAYS[attempt])
                attempt += 1

    # --- loop --------------------------------------------------------------------------------------------------

    async def loop(self, stopping: asyncio.Event) -> None:
        """Process offered requests until `stopping`; then end the running flows (`device_shutdown`)."""
        stop = asyncio.ensure_future(stopping.wait())
        try:
            while not stopping.is_set():
                take = asyncio.ensure_future(self.queue.get())
                await asyncio.wait({take, stop}, return_when=asyncio.FIRST_COMPLETED)
                if not take.done():
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
                    break
                await self.process(take.result())
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
            await self.shutdown()

    async def shutdown(self) -> None:
        self.stopping = True
        if self.timer is not None:
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        flows = [flow for flow in self.flows.values() if not flow.finished]
        for flow in flows:
            self.cancel(flow)
        await asyncio.gather(*(flow.task for flow in flows if flow.task is not None), return_exceptions=True)
        for flow in flows:
            try:
                await self.respond(flow.request, {"step": "failed", "code": "device_shutdown"}, final=True)
            except Exception as exc:
                self.line(f"shutdown_respond_failed request_id={flow.id} error={type(exc).__name__}")


def _uuid_or_nil(value: object) -> str:
    """The canonical UUID text of `value`, the nil UUID for anything else (rows of refused requests)."""
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else NIL
    except ValueError:
        return NIL


def service_for(context: Any) -> RequestService | None:
    """The request service of this daemon with the discovered handlers; None while the device is not enrolled."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, RequestService):
        return found
    sync = runtime(context)
    if sync is None:
        return None
    built = RequestService(context, sync, registry.request_handlers(context.host.package))
    setattr(context, ATTRIBUTE, built)
    return built
