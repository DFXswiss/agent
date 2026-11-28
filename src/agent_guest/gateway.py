"""The model gateway ``/model/...`` of the API process (05-runner.md §10.2-§10.4, ADR-010, ADR-025).

Claude Code in a session talks to ``ANTHROPIC_BASE_URL=http://127.0.0.1:7700/model`` with its session token; the
gateway is the only way to a model provider (nftables lets only the ``platform`` user out, 05 §4.3):

1. **Session token only** - ``Authorization: Bearer <token>`` or ``x-api-key: <token>`` (one value; two different
   values or a repeated header authorize nobody), resolved by the control process (``session.resolve``, only the
   SHA-256 leaves this process) and bound to the UID of the connecting process (``peer.py``). Otherwise
   ``401 authentication_error``; after too many failed checks of that UID ``429 rate_limit_error`` with ``Retry-After``
   (``authguard.py``, ATK-36).
2. **Body** - a JSON object with a string ``model`` and none of the OpenRouter routing fields ``ROUTING_FIELDS``
   (``400 invalid_request_error`` otherwise; nothing is forwarded). At most ``MAX_MODEL_REQUESTS`` model requests
   wait for their upstream at a time (else ``529 overloaded_error``), so hooks and MCP keep handler slots.
3. **Credential** - taken over from the control process (``credential.take``, serialized) into
   ``CredentialStore``. None provisioned (a restart of this process lost it, KOR-09): journal
   ``guest.error credential_missing`` (the runner provisions again or ends the session with
   ``model_access_unavailable``) and hold the request without a model slot, taking over every
   ``CREDENTIAL_POLL_SECONDS``, until the credential arrives - then it goes on as if it had been there - at most
   ``CREDENTIAL_WAIT_SECONDS`` and only while the session is active; then ``503 api_error``. One request per session
   waits and reports, again every ``CREDENTIAL_REPORT_SECONDS``: the runner ignores a report within its 10 s cooldown
   after a provisioning. Without the hold Claude Code gave up after three retries of the ``503`` (about 4.5 s) and the
   turn ended with an API error.
4. **Turn** - a ``/v1/messages`` request that can make the agent act (a non-empty ``tools``, a ``tool_choice`` or an
   ``mcp_servers``) only while the session's turn is confirmed (op ``turn.check``: the ``UserPromptSubmit`` hook
   accepted a registered prompt): else ``403 permission_error`` and ``guest.error turn_unconfirmed`` (ADR-064 section
   1; a failing op refuses as well). Suppressed only by the fault switch ``open`` of a TEST_MODE golden.
   **Model** - must be in the session's allow-list: else ``400 model_not_allowed`` in the Anthropic error shape.
5. **Budget** - daily platform budget of the provisioning plus the cost observed since: else ``429 budget_exceeded``.
6. **Forward** - to ``<upstream base><path>`` plus the query parameters of ``FORWARDED_QUERY`` (Claude Code's
   ``beta=true``; any other parameter is dropped, so a session cannot add provider options, ATK-39) with the
   injected credential (``Credential.upstream_headers``),
   ``Accept-Encoding: identity``; a provider that cannot be reached is ``502 api_error``.
7. **Stream back unchanged** - status, body and the end-to-end headers; chunks of at most 64 KiB (``read1``), so
   server-sent events reach Claude Code as they arrive. ``UsageMeter`` reads the usage on the way.
8. **Journal ``usage``** once per request after the response ended (also for refusals 4-6 and provider errors, and
   for a response that is dropped unread: ``Relay``), with
   request/response bytes and status; ``provider_outside_allowlist`` when OpenRouter named a provider outside the
   provisioned list (never blocked, 05 §10.2). OpenRouter ``usage.cost`` is charged against the budget. An error
   answer of the provider whose message says the subscription does not cover the model ("Usage credits are required
   for this model", ``refusal_of``) adds ``refusal: "model_not_in_plan"`` (HOT-26: no used-up quota).

``count_tokens`` goes the same way but writes no ``usage`` (it bills nothing and would distort the context figure of
the latest request, 05 §10.4). ``HEAD|GET /model/api/hello`` answers 200 locally without a token. Every other path under
``/model`` answers ``404 not_found_error`` after authentication and journals ``guest.error gateway_route``.

Secrets: the credential lives in ``CredentialStore``; the session token is hashed before it crosses ``api.sock``;
neither appears in a response, a journal record or a log line.
"""

from __future__ import annotations

import contextlib
import http.client
import math
import re
import ssl
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from typing import Any, NoReturn
from urllib.parse import urlencode

from agent_guest import peer
from agent_guest.apisock import ApiClient, ApiSockError, AuthLimited
from agent_guest.gateway_credentials import Credential, CredentialStore, parse_credential
from agent_guest.routing import Request, Response, json_response, parse_json

CHUNK = 64 * 1024
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 600.0
CREDENTIAL_WAIT_SECONDS = 22.0
CREDENTIAL_REPORT_SECONDS = 10.5
CREDENTIAL_POLL_SECONDS = 0.2
CREDENTIAL_WAITERS = 4
"""Requests of one session that may wait for its credential at a time (each holds a handler slot of the server);
any further one is answered ``503`` at once."""
MAX_USAGE_JSON = 16 * 1024 * 1024
MAX_SSE_EVENT = 1024 * 1024
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
PROVIDER_RE = re.compile(r"[ -~]{1,64}")
COUNT_MAX = 9007199254740991
# Response headers that the gateway sets itself or that only describe the upstream connection.
RESPONSE_DROPPED = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-type",
        "cache-control",
        "date",
        "server",
        "set-cookie",
        "trailer",
        "upgrade",
        "proxy-authenticate",
    }
)
REQUEST_ID_HEADERS = ("request-id", "x-request-id", "x-generation-id")
# Body fields that would let OpenRouter serve another model or provider than the checked one.
ROUTING_FIELDS = frozenset({"models", "provider", "route", "transforms", "preset"})
# The only query parameter forwarded to the provider (Claude Code sends `?beta=true`).
FORWARDED_QUERY = frozenset({"beta"})
# Model requests may hold at most this many of the API process's 64 handler slots (hooks and MCP keep the rest).
MAX_MODEL_REQUESTS = 32


def anthropic_error(status: int, error_type: str, message: str, headers: dict[str, str] | None = None) -> Response:
    """An error in the shape of the Anthropic Messages API (Claude Code shows ``message``)."""
    response = json_response({"type": "error", "error": {"type": error_type, "message": message}}, status)
    response.headers.update(headers or {})
    return response


def session_token(headers: Message, *, api_key: bool) -> str | None:
    """The bearer token (and, for the gateway, ``x-api-key``); ``None`` if absent or ambiguous."""
    found: set[str] = set()
    authorization = headers.get_all("Authorization") or []
    keys = (headers.get_all("x-api-key") or []) if api_key else []
    if len(authorization) > 1 or len(keys) > 1:
        return None
    if authorization:
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        found.add(token.strip())
    if keys and keys[0].strip():
        found.add(keys[0].strip())
    return found.pop() if len(found) == 1 else None


def authenticate(control: ApiClient, request: Request, *, api_key: bool, table: str) -> str | None:
    """The ID of the active session the request's token belongs to and that the connecting UID owns.

    ``ApiSockError`` if control is unreachable, ``AuthLimited`` while the failure limit of the UID holds."""
    token = session_token(request.headers, api_key=api_key)
    session = control.resolve_session(token, peer.peer_uid(table, request.server, request.peer))
    return None if session is None else str(session["id"])


class _Slot:
    """One of the ``MAX_MODEL_REQUESTS`` slots, held or not; ``give_back`` releases it only when held."""

    def __init__(self, slots: threading.BoundedSemaphore) -> None:
        self.slots = slots
        self.held = False

    def take(self) -> bool:
        self.held = self.slots.acquire(blocking=False)
        return self.held

    def give_back(self) -> None:
        if self.held:
            self.slots.release()
            self.held = False


def _overloaded() -> Response:
    return anthropic_error(529, "overloaded_error", "Too many model requests in this VM; retry shortly.")


def rate_limited(error: AuthLimited) -> Response:
    """The gateway's answer while the failure limit holds (Anthropic error shape)."""
    message = "Too many failed session-token checks; retry later."
    return anthropic_error(429, "rate_limit_error", message, {"Retry-After": str(error.retry_after)})


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= COUNT_MAX else None


def _separator(buffer: bytearray, start: int) -> tuple[int, int]:
    """Position and length of the first event separator (blank line, LF or CRLF) at or after ``start``."""
    found = [
        (at, size) for at, size in ((buffer.find(b"\n\n", start), 2), (buffer.find(b"\r\n\r\n", start), 4)) if at >= 0
    ]
    return min(found) if found else (-1, 0)


MODEL_NOT_IN_PLAN = "model_not_in_plan"
MODEL_REFUSALS = (
    "usage credits are required",
    "not available on your plan",
    "not included in your plan",
    "does not have access to this model",
    "do not have access to this model",
)
"""Parts of the provider's error message (lower case) that say the subscription does not cover the requested model."""


def refusal_of(answer: object) -> str | None:
    """`MODEL_NOT_IN_PLAN` for a provider error answer whose message names one of `MODEL_REFUSALS`, else None (HOT-26:
    a `429` of this kind is no used-up quota)."""
    error = answer.get("error") if isinstance(answer, dict) and answer.get("type") == "error" else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    lowered = message.lower()
    return MODEL_NOT_IN_PLAN if any(part in lowered for part in MODEL_REFUSALS) else None


@dataclass
class UsageMeter:
    """Reads the usage of a Messages response: server-sent events (``message_start``/``message_delta``) or JSON."""

    streaming: bool
    tokens: dict[str, int] = field(default_factory=dict)
    thinking: int = 0
    cost: float | None = None
    provider: str | None = None
    refusal: str | None = None
    """`MODEL_NOT_IN_PLAN` when the provider's error answer says the subscription does not cover the model."""
    _buffer: bytearray = field(default_factory=bytearray)
    _skipping: bool = False
    _overflow: bool = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if not self.streaming:
            if len(self._buffer) > MAX_USAGE_JSON:
                self._overflow = True
                self._buffer.clear()
            return
        start = 0
        while True:
            end, size = _separator(self._buffer, start)
            if end < 0:
                del self._buffer[:start]
                if len(self._buffer) > MAX_SSE_EVENT:
                    self._skipping = True
                    self._buffer.clear()
                return
            if self._skipping:
                self._skipping = False
            else:
                self._event(bytes(self._buffer[start:end]).replace(b"\r\n", b"\n"))
            start = end + size

    def finish(self) -> None:
        if not self.streaming and not self._overflow:
            with contextlib.suppress(ValueError):
                found = parse_json(bytes(self._buffer))
                self._message(found)
                self.refusal = refusal_of(found)
        self._buffer.clear()

    def _event(self, raw: bytes) -> None:
        if b"message_start" not in raw and b"message_delta" not in raw:
            return
        data = b"\n".join(line[5:].lstrip(b" ") for line in raw.split(b"\n") if line.startswith(b"data:"))
        try:
            event = parse_json(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start":
            self._message(event.get("message"))
        elif event.get("type") == "message_delta":
            self._usage(event.get("usage"))

    def _message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        provider = message.get("provider")
        if isinstance(provider, str) and PROVIDER_RE.fullmatch(provider):
            self.provider = provider
        self._usage(message.get("usage"))

    def _usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = _count(usage.get(name))
            if value is not None:
                self.tokens[name] = value
        details = usage.get("output_tokens_details")
        thinking = _count(details.get("thinking_tokens")) if isinstance(details, dict) else None
        if thinking is not None:
            self.thinking = thinking
        cost = usage.get("cost")
        if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
            self.cost = float(cost)


class _Refusal(Exception):
    """Ends a request before or instead of forwarding it, with the response to send."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status)
        self.response = response


MESSAGES_PATH = "/v1/messages"


def can_act(data: dict[str, Any]) -> bool:
    """The request can make the agent act: tools, a tool choice or provider-side MCP servers."""
    tools, servers = data.get("tools"), data.get("mcp_servers")
    return bool(tools) or data.get("tool_choice") is not None or bool(servers)


@dataclass
class _Call:
    """One authenticated model request on its way through the gateway."""

    session_id: str
    model: str
    body: bytes
    record_usage: bool
    acts: bool = False
    request_id: str = ""
    context_window: int = 0


class Gateway:
    """Per API process: the credential store and the forwarding logic."""

    def __init__(
        self,
        control: ApiClient,
        store: CredentialStore | None = None,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        tls: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        peer_table: str = "",
        fail_open: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.control = control
        self.peer_table = peer_table
        self.fail_open = fail_open
        self.store = store or CredentialStore()
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.tls = tls
        self._slots = threading.BoundedSemaphore(MAX_MODEL_REQUESTS)
        self._take_lock = threading.Lock()
        # Replaced (never changed) under the take lock; read without it.
        self._active: frozenset[str] = frozenset()
        self._wait_lock = threading.Lock()
        self._waits: dict[str, list[Any]] = {}  # session -> [event of the waiting request, number of followers]
        self.sleep = sleep
        self.monotonic = monotonic

    # ---------------------------------------------------------------------------------------------------- routes

    def forward(self, request: Request, path: str, *, record_usage: bool) -> Response:
        slot = _Slot(self._slots)
        if not slot.take():
            return _overloaded()
        try:
            call = self._call(request, record_usage)
            if path == MESSAGES_PATH and call.acts and not self._confirmed(call.session_id):
                self._append("guest.error", {"code": "turn_unconfirmed"}, call.session_id)
                self._refuse(call, 403, "permission_error", "The platform did not confirm a prompt for this turn.")
            found = self._credential(call.session_id)
            if found is None:
                # The wait for a new provisioning holds no slot: it must not block the other sessions of the VM.
                slot.give_back()
                found = self._await_credential(call.session_id)
                if not slot.take():
                    return _overloaded()
            credential = self._admit(call, found)
            connection, upstream = self._send(call, credential, request, path)
        except _Refusal as refusal:
            return refusal.response
        finally:
            slot.give_back()
        call.request_id = next(
            (
                value
                for value in map(upstream.getheader, REQUEST_ID_HEADERS)
                if value and REQUEST_ID_RE.fullmatch(value)
            ),
            "",
        )
        content_type = upstream.getheader("Content-Type") or "application/json"
        headers: dict[str, str] = {}
        for name, value in upstream.getheaders():
            if name.lower() not in RESPONSE_DROPPED:
                headers[name] = f"{headers[name]}, {value}" if name in headers else value
        meter = UsageMeter(streaming=content_type.lower().startswith("text/event-stream"))
        stream = self._relay(connection, upstream, call, credential, meter)
        return Response(status=upstream.status, body=stream, content_type=content_type, headers=headers)

    def unknown(self, request: Request) -> Response:
        try:
            session_id = authenticate(self.control, request, api_key=True, table=self.peer_table)
        except AuthLimited as error:
            return rate_limited(error)
        if session_id is None:
            return anthropic_error(401, "authentication_error", "The session token is missing or not valid.")
        self._append("guest.error", {"code": "gateway_route"}, session_id)
        return anthropic_error(404, "not_found_error", "The gateway does not serve this route.")

    # --------------------------------------------------------------------------------------------------- helpers

    def _call(self, request: Request, record_usage: bool) -> _Call:
        try:
            session_id = authenticate(self.control, request, api_key=True, table=self.peer_table)
        except AuthLimited as error:
            raise _Refusal(rate_limited(error)) from None
        if session_id is None:
            raise _Refusal(anthropic_error(401, "authentication_error", "The session token is missing or not valid."))
        assert request.stream is not None
        body = request.stream.read()
        try:
            data = parse_json(body)
        except ValueError:
            data = None
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, str):
            message = "The request body must be a JSON object with a model."
            raise _Refusal(anthropic_error(400, "invalid_request_error", message))
        assert isinstance(data, dict)
        if ROUTING_FIELDS & set(data):
            message = "Model or provider routing fields are not allowed."
            raise _Refusal(anthropic_error(400, "invalid_request_error", message))
        return _Call(session_id, model, body, record_usage, acts=can_act(data))

    def _confirmed(self, session_id: str) -> bool:
        """The session's turn is confirmed (op ``turn.check``); a failing op confirms nothing."""
        if self.fail_open:
            return True
        try:
            answer = self.control.call("turn.check", {"session_id": session_id})
        except ApiSockError:
            return False
        return answer.get("confirmed") is True

    def _admit(self, call: _Call, found: tuple[Credential, float] | None) -> Credential:
        """The session's credential if there is one, the model is allowed and the budget not used up; else
        ``_Refusal``."""
        if found is None:
            self._refuse(call, 503, "api_error", "Model access is not available for this session right now.")
        credential, observed = found
        window = credential.policy.context_window(call.model)
        if window is None:
            self._refuse(call, 400, "model_not_allowed", "The model is not allowed for this session.")
        call.context_window = window
        if not credential.policy.within_budget(observed):
            self._refuse(call, 429, "budget_exceeded", "The daily model budget of the organization is used up.")
        return credential

    def _send(
        self, call: _Call, credential: Credential, request: Request, path: str
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        query = {name: value for name, value in request.query.items() if name in FORWARDED_QUERY}
        target = credential.base_path + path + (f"?{urlencode(query)}" if query else "")
        connection: http.client.HTTPConnection | None = None
        try:
            connection = self._connect(credential)
            connection.putrequest("POST", target, skip_accept_encoding=True)
            for name, value in credential.upstream_headers(request.headers.items()):
                connection.putheader(name, value)
            connection.putheader("Accept-Encoding", "identity")
            connection.putheader("Content-Length", str(len(call.body)))
            connection.endheaders(call.body)
            return connection, connection.getresponse()
        except (OSError, http.client.HTTPException, ValueError):
            if connection is not None:
                connection.close()
            self._refuse(call, 502, "api_error", "The model provider could not be reached.")

    def _refuse(self, call: _Call, status: int, error_type: str, message: str) -> NoReturn:
        response = anthropic_error(status, error_type, message)
        if call.record_usage:
            assert isinstance(response.body, bytes)
            self._usage(call, status, len(response.body), UsageMeter(streaming=False), outside=False)
        raise _Refusal(response)

    def _await_credential(self, session_id: str) -> tuple[Credential, float] | None:
        """The credential the runner provisions after ``credential_missing``; ``None`` after ``CREDENTIAL_WAIT_SECONDS``
        or once the session is no longer active. One request per session reports and takes over; the session's other
        requests wait for it. The report is repeated every ``CREDENTIAL_REPORT_SECONDS``: the runner ignores a report
        within its cooldown after a provisioning (``model.gateways.COOLDOWN_S``, 10 s), so one lands after it."""
        with self._wait_lock:
            entry = self._waits.get(session_id)
            leader = entry is None
            if entry is None:
                entry = self._waits[session_id] = [threading.Event(), 0]
            elif entry[1] >= CREDENTIAL_WAITERS - 1:
                return None
            entry[1] += 0 if leader else 1
        waiting: threading.Event = entry[0]
        if not leader:
            waiting.wait(CREDENTIAL_WAIT_SECONDS + 2 * CREDENTIAL_REPORT_SECONDS)
            return self.store.lookup(session_id)
        try:
            return self._take_over(session_id)
        finally:
            with self._wait_lock:
                del self._waits[session_id]
            waiting.set()

    def _take_over(self, session_id: str) -> tuple[Credential, float] | None:
        self._append("guest.error", {"code": "credential_missing"}, session_id)
        reported = started = self.monotonic()
        while self.monotonic() - started < CREDENTIAL_WAIT_SECONDS and session_id in self._active:
            self.sleep(CREDENTIAL_POLL_SECONDS)
            found = self._credential(session_id)
            if found is not None:
                return found
            if self.monotonic() - reported >= CREDENTIAL_REPORT_SECONDS:
                self._append("guest.error", {"code": "credential_missing"}, session_id)
                reported = self.monotonic()
        return None

    def _credential(self, session_id: str) -> tuple[Credential, float] | None:
        # One take at a time: a parallel request must not miss a slot another request emptied but has not stored
        # yet, and an older list of active sessions must never be applied after a newer take.
        with self._take_lock:
            taken = self.control.call("credential.take", {"session_id": session_id})
            self.store.retain(taken["active"])
            self._active = frozenset(taken["active"])
            if taken["credential"] is not None:
                self.store.provide(session_id, parse_credential(taken["credential"]))
            return self.store.lookup(session_id)

    def _connect(self, credential: Credential) -> http.client.HTTPConnection:
        connection: http.client.HTTPConnection
        if credential.scheme == "https":
            connection = http.client.HTTPSConnection(
                credential.host, credential.port, timeout=self.connect_timeout, context=self.tls()
            )
        else:
            connection = http.client.HTTPConnection(credential.host, credential.port, timeout=self.connect_timeout)
        connection.connect()
        assert connection.sock is not None
        connection.sock.settimeout(self.read_timeout)
        return connection

    def _relay(
        self,
        connection: http.client.HTTPConnection,
        upstream: http.client.HTTPResponse,
        call: _Call,
        credential: Credential,
        meter: UsageMeter,
    ) -> Relay:
        return Relay(self, _Stream(connection, upstream, call, credential, meter))

    def _finish(self, stream: _Stream) -> None:
        """Once per forwarded request, however its response ends: close, charge, journal ``usage``."""
        stream.connection.close()
        stream.meter.finish()
        charge = None
        if stream.meter.cost is not None:
            charge = self.store.charge(stream.call.session_id, stream.credential, stream.meter.cost)
        seq = None
        if stream.call.record_usage:
            outside = stream.credential.policy.provider_outside(stream.meter.provider)
            seq = self._usage(stream.call, stream.upstream.status, stream.sent, stream.meter, outside=outside)
        # a cost without a `usage` record reaches no one and stays the session's own (gateway_credentials)
        if charge is not None and stream.call.record_usage and MODEL_ID_RE.fullmatch(stream.call.model):
            self.store.journaled(charge, seq)

    def _usage(self, call: _Call, status: int, response_bytes: int, meter: UsageMeter, *, outside: bool) -> int | None:
        """Journal ``usage``; its ``seq``, or None when nothing was journaled."""
        if not MODEL_ID_RE.fullmatch(call.model):
            return None
        body: dict[str, Any] = {
            "request_id": call.request_id or f"gw_{uuid.uuid4().hex}",
            "model": call.model,
            "input_tokens": meter.tokens.get("input_tokens", 0),
            "output_tokens": meter.tokens.get("output_tokens", 0),
            "cache_read_tokens": meter.tokens.get("cache_read_input_tokens", 0),
            "cache_write_tokens": meter.tokens.get("cache_creation_input_tokens", 0),
            "thinking_tokens": meter.thinking,
            "context_window_tokens": call.context_window,
            "request_bytes": len(call.body),
            "response_bytes": response_bytes,
            "status": status,
        }
        if meter.provider is not None:
            body["provider"] = meter.provider
        if outside:
            body["provider_outside_allowlist"] = True
        if meter.cost is not None:
            body["cost_usd"] = meter.cost
        if meter.refusal is not None and status >= 400:
            body["refusal"] = meter.refusal
        return self._append("usage", body, call.session_id)

    def _append(self, kind: str, body: dict[str, Any], session_id: str) -> int | None:
        try:
            return self.control.append(kind, body, session_id)
        except ApiSockError as error:
            sys.stderr.write(f"agent-guest-api: journal {kind} failed: {error.code}\n")
            return None


@dataclass
class _Stream:
    """One forwarded request whose response is being relayed."""

    connection: http.client.HTTPConnection
    upstream: http.client.HTTPResponse
    call: _Call
    credential: Credential
    meter: UsageMeter
    sent: int = 0


class Relay:
    """The response body of a forwarded request (chunks of at most ``CHUNK`` bytes as they arrive).

    The end of the request (``Gateway._finish``: close, charge, ``usage``) runs exactly once: after the last chunk, on
    a read error, on ``close()`` - or when the body is dropped without being read to its end (the client went away
    before or while it was written, or the handler timed out after the headers: AUD-01 A-26). A generator's
    ``finally`` would not run in the first of these cases, because a generator that never started has nothing to
    finish.
    """

    def __init__(self, gateway: Gateway, stream: _Stream) -> None:
        self._stream = stream
        self._finish = gateway._finish
        # Backstop for a body dropped unread. The garbage collector may call it in any thread, even one that holds a
        # lock the finish needs (credential store, api.sock client): the finish then runs in a thread of its own.
        self._end = weakref.finalize(self, _finish_in_thread, gateway._finish, stream)

    def __iter__(self) -> Relay:
        return self

    def __next__(self) -> bytes:
        if not self._end.alive:
            raise StopIteration
        try:
            chunk = self._stream.upstream.read1(CHUNK)
        except BaseException:
            self.close()
            raise
        if not chunk:
            self.close()
            raise StopIteration
        self._stream.meter.feed(chunk)
        self._stream.sent += len(chunk)
        return chunk

    def close(self) -> None:
        """End the request now, in this thread (once: ``detach`` claims the end atomically)."""
        if self._end.detach() is not None:
            self._finish(self._stream)


def _finish_in_thread(finish: Callable[[_Stream], None], stream: _Stream) -> None:
    threading.Thread(target=finish, args=(stream,), name="agent-guest-usage", daemon=True).start()


_GATEWAYS: weakref.WeakKeyDictionary[ApiClient, Gateway] = weakref.WeakKeyDictionary()
_GATEWAYS_LOCK = threading.Lock()


def gateway_for(control: ApiClient, peer_table: str = "", *, fail_open: bool = False) -> Gateway:
    """The gateway of the API process that owns ``control`` (one credential store per process); ``fail_open`` only for
    the fault switch ``open`` of a TEST_MODE golden (``fault.guard_suppressed``)."""
    with _GATEWAYS_LOCK:
        found = _GATEWAYS.get(control)
        if found is None:
            found = _GATEWAYS[control] = Gateway(control, peer_table=peer_table, fail_open=fail_open)
        return found
