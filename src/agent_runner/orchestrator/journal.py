"""Journal handler `hub.relay` (`<area>/journal.py:HANDLERS`; 05-runner.md section 17.2, ADR-082 sections 6.5 and 9.7).

The agent tool of the main orchestrator has no network in its VM: it signs every hub request with its own key and the
guest journals it as `hub.relay {pending_id, method, target, headers, body}` (body b64u) and waits for the answer.
The handler runs in the journal page's transaction and answers in the background (`Relays`, like the work VM calls):

- forwarded only for a session that runs **on the VM that carried the record**, has not ended, has a `registered` or
  `handed_over` row in `runner.orchestrator` (its keys were registered by this device) whose pairing signs the request
  (`Agent-Key` is `key_id_of(pairing_id)`), and whose organization still fits in the verified log (private, the owner
  its only user, a platform device: `work.org_reason`); any other record is answered
  `{"error": "refused"}` with the log line `relay_refused` and never reaches the hub;
- only to this device's own hub origin, with the method, target, headers and body as checked by `parse` (the closed
  rules of `contracts/guest/journal/hub.relay.json`; a record that breaks them is `JournalRecordInvalid`); the bytes
  are sent unchanged, never signed, opened or altered by the runner; redirects are not followed and the answer is
  read in identity encoding;
- the answer is `{status, headers, body}` with only the headers of `RESPONSE_HEADERS` the hub sent, the body b64u; a
  request body over `MAX_BODY` or an answer over `MAX_RESPONSE` is `{"error": "too_large"}`, a transport failure
  `{"error": "hub_unreachable"}`. The tool verifies the hub's signature itself.

At most `MAX_RELAYS_PER_SESSION` relays of one session run at a time (more are refused); a pending call is taken
once even when its page is read again.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from psycopg import AsyncConnection

from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestError
from agent_runner.orchestrator import store
from agent_runner.orchestrator.env import OrchestratorEnv, orchestrator_for
from agent_runner.orchestrator.work import org_reason
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.sessions.records import FINAL
from agent_runner.vm.records import VmRow

KIND = "hub.relay"
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_TARGET = 4096
MAX_RELAYS_PER_SESSION = 16
PENDING_MEMORY = 4096
ANSWER_ATTEMPTS = 3
REQUEST_HEADERS: Mapping[str, re.Pattern[str]] = {
    "Agent-Key": re.compile(r"^agent\.[0-9a-f]{32}$"),
    "Agent-Time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "Agent-Nonce": re.compile(r"^[A-Za-z0-9_-]{21}[AQgw]$"),
    "Agent-Signature": re.compile(r"^[A-Za-z0-9_-]{85}[AQgw]$"),
    "Content-Type": re.compile(r"^application/json$"),
}
REQUIRED_HEADERS = frozenset(REQUEST_HEADERS) - {"Content-Type"}
RESPONSE_HEADERS: Mapping[str, int] = {
    "Agent-Hub-Key": 256,
    "Agent-Hub-Time": 64,
    "Agent-Hub-Signature": 256,
    "Content-Type": 256,
    "Retry-After": 32,
}
"""The headers of the hub's answer passed on, with the longest value (`contracts/guest/objects.json`)."""
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TARGET_RE = re.compile(r"^/v1/[!-~]*$")


@dataclass(frozen=True)
class Relay:
    pending_id: str
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


def _target(value: object) -> bool:
    if not isinstance(value, str) or len(value) > MAX_TARGET or not _TARGET_RE.fullmatch(value) or "#" in value:
        return False
    path = value.split("?", 1)[0]
    return all(segment not in ("", ".", "..") for segment in path.split("/")[1:])


def _headers(value: object) -> bool:
    return (
        isinstance(value, dict)
        and REQUIRED_HEADERS <= set(value) <= set(REQUEST_HEADERS)
        and all(isinstance(text, str) and REQUEST_HEADERS[name].fullmatch(text) for name, text in value.items())
    )


def parse(body: Mapping[str, Any]) -> Relay:
    """The relay of a `hub.relay` body (`JournalRecordInvalid` otherwise)."""
    pending_id, method, target, headers, raw = (
        body.get(key) for key in ("pending_id", "method", "target", "headers", "body")
    )
    if (
        set(body) != {"pending_id", "method", "target", "headers", "body"}
        or not isinstance(pending_id, str)
        or not _UUID_RE.fullmatch(pending_id)
        or method not in METHODS
        or not _target(target)
        or not _headers(headers)
        or not isinstance(raw, str)
    ):
        raise JournalRecordInvalid(KIND)
    try:
        data = b64u_decode(raw)
    except ValueError:
        raise JournalRecordInvalid(KIND) from None
    assert isinstance(method, str)  # checked above
    assert isinstance(target, str)
    assert isinstance(headers, dict)
    return Relay(pending_id=pending_id, method=method, target=target, headers=dict(headers), body=data)


def _error(code: str) -> dict[str, Any]:
    return {"error": code}


class _TooLarge(Exception):
    pass


class Relays:
    """The background relays of one daemon."""

    def __init__(self) -> None:
        self.answered: dict[str, None] = {}
        self.running: dict[str, int] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def spawn(
        self,
        env: OrchestratorEnv | None,
        vm: VmRow,
        relay: Relay,
        *,
        session_id: str | None,
        sessions: Any,
        allowed: bool,
    ) -> asyncio.Task[None] | None:
        if relay.pending_id in self.answered:
            return None
        self.answered[relay.pending_id] = None
        while len(self.answered) > PENDING_MEMORY:
            del self.answered[next(iter(self.answered))]
        task = asyncio.ensure_future(
            self._answer(env, vm, relay, session_id=session_id, sessions=sessions, allowed=allowed)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @staticmethod
    async def _forward(env: OrchestratorEnv, relay: Relay) -> dict[str, Any]:
        try:
            return await forward(env, relay)
        except Exception as error:  # a relay must always be answered; the type goes to the log only
            env.line(f"relay_failed error={type(error).__name__}")
            return _error("hub_unreachable")

    async def _answer(
        self,
        env: OrchestratorEnv | None,
        vm: VmRow,
        relay: Relay,
        *,
        session_id: str | None,
        sessions: Any,
        allowed: bool,
    ) -> None:
        if env is None or session_id is None or not allowed:
            sessions.log(f"runner.orchestrator relay_refused vm_id={vm.id} session_id={session_id}")
            answer = _error("refused")
        else:
            key = session_id
            self.running[key] = self.running.get(key, 0) + 1
            try:
                if self.running[key] > MAX_RELAYS_PER_SESSION:
                    env.line(f"relay_refused session_id={session_id} reason=busy")
                    answer = _error("refused")
                else:
                    answer = await self._forward(env, relay)
            finally:
                self.running[key] -= 1
        for attempt in range(ANSWER_ATTEMPTS):
            try:
                async with await sessions.client(vm) as client:
                    await client.answer_relay(relay.pending_id, answer)
                return
            except (GuestError, RunnerError) as error:
                sessions.log(
                    f"runner.orchestrator relay_answer_failed pending_id={relay.pending_id} attempt={attempt + 1}"
                    f" error={error.code}"
                )
                await asyncio.sleep(0.2 * (attempt + 1))


async def _read(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_raw():
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def forward(env: OrchestratorEnv, relay: Relay) -> dict[str, Any]:
    """Send the relay's bytes to this device's hub and return its answer as the guest takes it."""
    if len(relay.body) > MAX_BODY:
        return _error("too_large")
    headers = {**relay.headers, "Accept-Encoding": "identity"}
    async with httpx.AsyncClient(
        transport=env.transport, follow_redirects=False, timeout=env.timeout, trust_env=False
    ) as client:
        request = client.build_request(relay.method, env.hub_url + relay.target, headers=headers, content=relay.body)
        try:
            response = await client.send(request, stream=True)
            try:
                status, body = response.status_code, await _read(response)
            finally:
                await response.aclose()
        except _TooLarge:
            return _error("too_large")
        except httpx.HTTPError as error:
            env.line(f"relay_unreachable error={type(error).__name__}")
            return _error("hub_unreachable")
    passed = {
        name: response.headers[name]
        for name, longest in RESPONSE_HEADERS.items()
        if name in response.headers and len(response.headers[name]) <= longest
    }
    return {"status": status, "headers": passed, "body": b64u_encode(body)}


def key_id_of(pairing_id: str) -> str:
    """`agent.<32 hex>`: the key ID the orchestrator's tool signs with (its agent ID is the pairing ID)."""
    return "agent." + uuid.UUID(pairing_id).hex


def relays_for(env: OrchestratorEnv | None, holder: Any) -> Relays:
    """The daemon's `Relays` (kept on the orchestrator environment, or on `holder` without one)."""
    owner = env if env is not None else holder
    found = getattr(owner, "relays", None)
    if isinstance(found, Relays):
        return found
    relays = Relays()
    owner.relays = relays
    return relays


async def hub_relay(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    relay = parse(record.body)
    session = await journal.session(conn, record)
    allowed = False
    env = orchestrator_for(journal.env.context)
    if session is not None and session.state not in FINAL and env is not None:
        row = await store.get(conn, session.id)
        allowed = (
            row is not None
            and row.state in store.RELAYING
            and relay.headers["Agent-Key"] == key_id_of(row.pairing_id)
            and org_reason(env, session) is None
        )
    session_id = None if session is None else session.id
    relays_for(env, journal.env).spawn(
        env, journal.vm, relay, session_id=session_id, sessions=journal.env, allowed=allowed
    )


HANDLERS: dict[str, Callable[..., Any]] = {KIND: hub_relay}
