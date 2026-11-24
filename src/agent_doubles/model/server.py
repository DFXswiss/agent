"""The HTTP server of the model double and its control routes under ``/_double/``.

Ported from the Spike C prototype (``modeldouble/server.py``). Routes (everything Claude Code 2.1.330 was seen to
call, docs/platform/spec/05-runner.md section 10.2):

- ``HEAD /api/hello``, ``GET /api/hello``: the connectivity check Claude Code sends at start.
- ``POST /v1/messages`` (Claude Code adds ``?beta=true``): the scripted answer, streamed as server-sent events when
  the request has ``"stream": true``, else one JSON message.
- ``POST /v1/messages/count_tokens``: a deterministic estimate.
- ``GET /v1/key``: the OpenRouter key document (``{"data": {...}}``, zero spend, no limit) that the runner's spend
  poller of the platform key reads every 900 s (05-runner.md section 10.6). It is a background read, not part of a
  conversation: journaled but not counted (an idle double stays idle) and independent of the access kind; a missing
  bearer token is refused (``401``, a failure), and so is any other token than the platform key when the double knows
  it (``key_token``, CLI ``--key-token-env``: the run's key file of the runners).

- ``/v1/keys`` and ``/v1/keys/{hash}``: the OpenRouter key management API (`agent_doubles.model.keys`), journaled
  but not counted; it needs the management token (``management_token``, CLI ``--management-token-env``). A key
  created there is accepted by ``POST /v1/messages`` in place of the configured access: ``401`` while it is
  disabled, ``402`` once its usage reached its limit, otherwise the answer's cost is added to its usage.

Any other route answers ``404 not_found_error``, is journaled and recorded as a failure, so a test notices when a
new Claude Code version starts calling something new (05 §15). Requests under ``/v1/`` must carry the credential of
the configured access kind (`agent_doubles.model.access`), else ``401 authentication_error`` and a failure.

Control routes (not journaled, not counted):

- ``GET /_double/state``: the state document (`agent_doubles.common.state`) with ``scenario``, ``consumed`` and
  ``remaining``; ``ok`` means every step was consumed and nothing unexpected arrived.
- ``GET /_double/journal``: every journaled request since the last reset.
- ``GET /_double/scenario``: name, description, step and side-step ids, access kind of the loaded scenario.
- ``POST /_double/scenario``: ``{"scenario": {...}}`` or ``{"packaged": "<name>"}``, optional ``"variables"`` and
  ``"access"`` (``{"kind", "token"}`` or null); switches the scenario and restarts its progress (the journal stays).
- ``POST /_double/reset``: restarts the progress of the loaded scenario and clears the journal in memory.
- ``GET /_double/keys``: ``{"keys": [KEY, ...]}``, the created keys without their plaintexts.
"""

from __future__ import annotations

import hmac
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import parse_qs, urlsplit

from agent_doubles.common.journal import Entry, Journal, decode_body, redact_headers
from agent_doubles.common.server import BodyError, DoubleHandler, DoubleServer
from agent_doubles.common.state import state_document
from agent_doubles.model import wire
from agent_doubles.model.access import Access, AccessError, parse_access
from agent_doubles.model.keys import DEFAULT_PAGE_SIZE, KEYS_PATH, KeyRequestError, KeyStore, SubKey
from agent_doubles.model.scenario import (
    ErrorReply,
    OpenRouter,
    Scenario,
    ScenarioError,
    Step,
    capture_values,
    expand,
    last_user_message,
    packaged_scenario,
    parse_scenario,
    tool_results,
)

DOUBLE_NAME = "model"
DEFAULT_MODEL = "double-model"
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
KEY_PATH = "/v1/key"
KEY_DOCUMENT: dict[str, Any] = {
    "data": {
        "label": "double-platform-key",
        "usage": 0,
        "usage_daily": 0,
        "usage_weekly": 0,
        "usage_monthly": 0,
        "limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "is_free_tier": False,
    }
}
_UNCHANGED: Any = object()


@dataclass
class _Progress:
    """Where the loaded scenario stands; replaced on reset and on a scenario switch."""

    variables: dict[str, str]
    index: int = 0
    count: int = 0
    requests: int = 0
    consumed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    issued_tool_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Choice:
    """The prepared answer to a request, or the refusal text and its status (recorded as failure).

    `blocks` are the reply blocks with templates filled in and tool ids issued; `error` is the filled-in body of a
    scripted error; `openrouter` are the extras of the scenario the step belongs to.
    """

    step: Step | None
    refusal: str | None = None
    status: int = 400
    blocks: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    openrouter: OpenRouter | None = None


class ModelDouble:
    """A running model double for one scenario at a time.

    `access`: when set, requests under ``/v1/`` must carry its credential (else 401). `key_token`: when set, the key
    document ``GET /v1/key`` answers only ``Authorization: Bearer <key_token>`` (the platform key). `variables`
    override the scenario's default template variables. `management_token` switches the key management API on
    (`keys`, ``key_page_size`` keys per listed page).
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        journal_path: str | Path | None = None,
        access: Access | None = None,
        variables: Mapping[str, str] | None = None,
        key_token: str | None = None,
        management_token: str | None = None,
        key_page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._lock = threading.Lock()
        # Set by `stop`: a step that waits out its `delay_ms` ends at once instead of answering a stopped double.
        self.stopping = threading.Event()
        self.scenario = scenario
        self.access = access
        self.key_token = key_token
        self.keys = KeyStore(management_token, key_page_size)
        self._overrides = dict(variables or {})
        self._progress = self._fresh()
        self.journal = Journal(DOUBLE_NAME, journal_path)
        self._server = DoubleServer(_handler_for(self), host, port)

    # --- lifecycle ----------------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The base URL to put in ``ANTHROPIC_BASE_URL`` (or the gateway's upstream)."""
        return self._server.url

    def start(self) -> Self:
        """Serve in a background thread."""
        self.stopping.clear()
        self._server.start()
        return self

    def serve_forever(self) -> None:
        """Serve in the calling thread until `stop` is called from another thread."""
        self._server.serve_forever()

    def stop(self) -> None:
        """Stop serving and close the socket; a delayed answer in flight ends without being sent."""
        self.stopping.set()
        self._server.stop()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- control ------------------------------------------------------------------------------

    def _fresh(self) -> _Progress:
        return _Progress(variables={**self.scenario.variables, **self._overrides})

    def load(
        self,
        scenario: Scenario,
        *,
        variables: Mapping[str, str] | None = None,
        access: Access | None = _UNCHANGED,
    ) -> None:
        """Switch to `scenario` with fresh progress; `access` changes only when given (None switches it off)."""
        with self._lock:
            self.scenario = scenario
            self._overrides = dict(variables or {})
            if access is not _UNCHANGED:
                self.access = access
            self._progress = self._fresh()

    def reset(self) -> None:
        """Restart the progress of the loaded scenario and clear the journal in memory."""
        with self._lock:
            self._progress = self._fresh()
        self.journal.clear()

    def describe(self) -> dict[str, Any]:
        """The loaded scenario as ``GET /_double/scenario`` reports it."""
        with self._lock:
            return {
                "name": self.scenario.name,
                "description": self.scenario.description,
                "steps": [s.id for s in self.scenario.steps],
                "side": [s.id for s in self.scenario.side],
                "access": self.access.kind if self.access else None,
            }

    # --- inspection ---------------------------------------------------------------------------

    @property
    def exchanges(self) -> list[dict[str, Any]]:
        """The journal records since the last reset."""
        return self.journal.records()

    @property
    def failures(self) -> list[str]:
        """Every failure of the current progress."""
        with self._lock:
            return list(self._progress.failures)

    def state(self) -> dict[str, Any]:
        """Progress through the script and every failure so far."""
        with self._lock:
            progress = self._progress
            steps = self.scenario.steps
            remaining: list[str] = []
            if progress.index < len(steps):
                current = steps[progress.index]
                remaining = [current.id] * (current.repeat - progress.count)
                remaining += [s.id for s in steps[progress.index + 1 :] for _ in range(s.repeat)]
            return state_document(
                DOUBLE_NAME,
                done=not remaining,
                failures=progress.failures,
                requests=progress.requests,
                scenario=self.scenario.name,
                consumed=list(progress.consumed),
                remaining=remaining,
            )

    # --- request handling (called from handler threads) ---------------------------------------

    def count_request(self) -> None:
        with self._lock:
            self._progress.requests += 1

    def fail(self, text: str) -> None:
        with self._lock:
            self._progress.failures.append(text)

    def refusal(self, headers: Mapping[str, str]) -> str | None:
        """Why the request's credential is refused; None when no access is configured or it matches."""
        access = self.access
        return access.refusal(headers) if access is not None else None

    def choose(self, body: dict[str, Any], headers: Mapping[str, str], request: str) -> Choice:
        """The prepared answer to `body`, or a refusal (recorded as failure).

        A step is consumed only when its answer could be prepared: a template naming an unknown value is a failure
        that leaves the step in place, so a retry by Claude Code does not drift into the next step.
        """
        with self._lock:
            progress = self._progress
            progress.variables.update(capture_values(self.scenario.captures, body))
            for result in tool_results(last_user_message(body)):
                if result.tool_use_id not in progress.issued_tool_ids:
                    progress.failures.append(f"tool_result for unknown id {result.tool_use_id}")
            side = next((s for s in self.scenario.side if not s.expect.mismatches(body, headers)), None)
            if side is not None:
                return self._prepare(side, body, request, consume=False)
            if progress.index >= len(self.scenario.steps):
                text = "request after the end of the script"
                progress.failures.append(text)
                return Choice(None, text)
            step = self.scenario.steps[progress.index]
            missing = step.expect.mismatches(body, headers)
            if missing:
                text = f"step {step.id}: " + "; ".join(missing)
                progress.failures.append(text)
                return Choice(None, text)
            return self._prepare(step, body, request, consume=True)

    def _prepare(self, step: Step, body: dict[str, Any], request: str, *, consume: bool) -> Choice:
        """Fill in the step's templates and issue its tool ids; consume it when asked (caller holds the lock)."""
        progress = self._progress
        variables = {**progress.variables, **capture_values(step.captures, body)}
        try:
            if step.error is not None:
                error = expand(wire.error_for(step.error, request), variables)
                blocks: list[dict[str, Any]] = []
            else:
                assert step.reply is not None
                error = None
                content = expand(list(step.reply.content), variables)
                prefix = "toolu_double_" + _UNSAFE_ID.sub("_", step.id)
                blocks = wire.resolve_blocks(content, prefix)
                if any(b["id"] in progress.issued_tool_ids for b in blocks if b["type"] == "tool_use"):
                    # A repeated step or a side step answers again: tool ids stay unique per conversation.
                    blocks = wire.resolve_blocks(content, f"{prefix}_{request.rsplit('_', 1)[1]}")
        except ScenarioError as exc:
            text = f"step {step.id}: {exc}"
            progress.failures.append(text)
            return Choice(None, text, status=500)
        progress.variables = variables
        progress.issued_tool_ids.update(b["id"] for b in blocks if b["type"] == "tool_use")
        if consume:
            progress.consumed.append(step.id)
            progress.count += 1
            if progress.count == step.repeat:
                progress.index += 1
                progress.count = 0
        return Choice(step, blocks=blocks, error=error, openrouter=self.scenario.openrouter)

    def control_load(self, payload: Any) -> None:
        """Apply a ``POST /_double/scenario`` payload; raises ScenarioError or AccessError when it is invalid."""
        if not isinstance(payload, dict) or not set(payload) <= {"scenario", "packaged", "variables", "access"}:
            raise ScenarioError("payload must be an object with scenario or packaged, variables, access")
        if ("scenario" in payload) == ("packaged" in payload):
            raise ScenarioError("payload needs exactly one of scenario and packaged")
        if "packaged" in payload:
            if not isinstance(payload["packaged"], str):
                raise ScenarioError("packaged must be a scenario name")
            scenario = packaged_scenario(payload["packaged"])
        else:
            scenario = parse_scenario(payload["scenario"])
        variables = payload.get("variables", {})
        if not isinstance(variables, dict) or not all(isinstance(v, str) for v in variables.values()):
            raise ScenarioError("variables must map names to strings")
        access = parse_access(payload["access"]) if "access" in payload else _UNCHANGED
        self.load(scenario, variables=variables, access=access)


def _handler_for(double: ModelDouble) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        pass

    Handler.double = double
    return Handler


def request_id(entry: Entry) -> str:
    """The request id the double reports for a journal entry."""
    return f"req_double_{entry.seq:04d}"


def message_id(entry: Entry) -> str:
    """The message id the double assigns to the answer of a journal entry."""
    return f"msg_double_{entry.seq:04d}"


class _Handler(DoubleHandler):
    server_version = "model-double/1"
    double: ClassVar[ModelDouble]

    def do_HEAD(self) -> None:
        self._route()

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_PUT(self) -> None:
        self._route()

    def do_DELETE(self) -> None:
        self._route()

    def do_PATCH(self) -> None:
        self._route()

    # --- plumbing -----------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}

    def _route(self) -> None:
        parts = urlsplit(self.path)
        try:
            raw = self.read_body()
            problem: BodyError | None = None
        except BodyError as exc:
            raw, problem = b"", exc
        if parts.path.startswith("/_double/"):
            self._control(parts.path, raw, problem)
            return
        entry = self.double.journal.open(
            self.command, parts.path, parts.query, redact_headers(self.headers.items()), decode_body(raw)
        )
        if parts.path == KEY_PATH and self.command == "GET":
            try:
                self._key_document(entry)
            finally:
                self.double.journal.close(entry)
            return
        if parts.path == KEYS_PATH or parts.path.startswith(KEYS_PATH + "/"):
            try:
                self._manage(parts.path[len(KEYS_PATH) :], parts.query, entry, problem)
            finally:
                self.double.journal.close(entry)
            return
        self.double.count_request()
        try:
            if problem is not None:
                error_type = "request_too_large" if problem.status == 413 else "invalid_request_error"
                self._refuse(entry, problem.status, error_type, problem.message)
            else:
                self._dispatch(parts.path, entry)
        finally:
            self.double.journal.close(entry)

    def _api_error(
        self, entry: Entry, status: int, error_type: str, text: str, headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        entry.status = status
        self.send_json(
            status, wire.error_body(error_type, text, request_id(entry)), (("request-id", request_id(entry)), *headers)
        )

    def _refuse(self, entry: Entry, status: int, error_type: str, text: str) -> None:
        """An error that is also a failure of the scenario (unexpected request)."""
        entry.note = text
        self.double.fail(text)
        self._api_error(entry, status, error_type, text)

    def _key_document(self, entry: Entry) -> None:
        authorization = self._headers().get("authorization", "")
        if not authorization.startswith("Bearer "):
            self._refuse(entry, 401, "authentication_error", "key document requested without a bearer token")
            return
        expected = self.double.key_token
        if expected is not None and not hmac.compare_digest(authorization.encode(), f"Bearer {expected}".encode()):
            self._refuse(entry, 401, "authentication_error", "key document requested with a wrong bearer token")
            return
        entry.status = 200
        self.send_json(200, KEY_DOCUMENT, (("request-id", request_id(entry)),))

    def _dispatch(self, path: str, entry: Entry) -> None:
        if path == "/api/hello" and self.command in {"HEAD", "GET"}:
            entry.status = 200
            self.send_json(200, {"ok": True}, (("request-id", request_id(entry)),))
            return
        known = {"/v1/messages", "/v1/messages/count_tokens"}
        if path not in known or self.command != "POST":
            self._refuse(entry, 404, "not_found_error", f"unhandled route {self.command} {path}")
            return
        headers = self._headers()
        sub = None if "x-api-key" in headers else self.double.keys.by_bearer(headers.get("authorization", ""))
        reason = self._access_refusal(headers) if sub is None else self._sub_key_refusal(sub)
        if reason is not None:
            status, error_type, text = reason
            entry.note = text
            if sub is None:
                self.double.fail(text)
            self._api_error(entry, status, error_type, text)
            return
        if not isinstance(entry.body, dict):
            self._refuse(entry, 400, "invalid_request_error", "body is not a JSON object")
            return
        if path == "/v1/messages/count_tokens":
            entry.status = 200
            self.send_json(200, {"input_tokens": wire.request_tokens(entry.body)}, (("request-id", request_id(entry)),))
            return
        self._messages(entry.body, headers, entry, sub)

    def _access_refusal(self, headers: Mapping[str, str]) -> tuple[int, str, str] | None:
        reason = self.double.refusal(headers)
        return None if reason is None else (401, "authentication_error", reason)

    @staticmethod
    def _sub_key_refusal(sub: SubKey) -> tuple[int, str, str] | None:
        """A created key that is switched off or whose usage reached its limit (not a failure: the provider's rule)."""
        if sub.disabled:
            return 401, "authentication_error", "key disabled"
        if sub.exhausted:
            return 402, "payment_required", "key limit reached"
        return None

    def _manage(self, rest: str, query: str, entry: Entry, problem: BodyError | None) -> None:
        """The key management API (`agent_doubles.model.keys`)."""
        keys = self.double.keys
        if not keys.authorized(self._headers().get("authorization", "")):
            self._refuse(entry, 401, "authentication_error", "key management needs the management key")
            return
        try:
            if problem is not None:
                raise KeyRequestError(problem.message)
            self._manage_route(rest, query, entry)
        except KeyRequestError as exc:
            self._api_error(entry, 400, "invalid_request_error", str(exc))

    def _manage_route(self, rest: str, query: str, entry: Entry) -> None:
        keys, command, body = self.double.keys, self.command, entry.body
        if rest in ("", "/"):
            if command == "POST":
                created, secret = keys.create(body)
                self._send_key(entry, 201, {"data": created.document(), "key": secret})
            elif command == "GET":
                offset = parse_qs(query).get("offset", ["0"])[0]
                if not offset.isdigit():
                    raise KeyRequestError("offset must be a non-negative integer")
                self._send_key(entry, 200, {"data": keys.page(int(offset))})
            else:
                self._refuse(entry, 405, "invalid_request_error", f"unhandled route {command} {KEYS_PATH}")
            return
        key_hash = rest[1:]
        if command == "GET":
            found = keys.get(key_hash)
        elif command == "PATCH":
            found = keys.patch(key_hash, body)
        elif command == "DELETE":
            found = None
            if keys.delete(key_hash):
                self._send_key(entry, 200, {"deleted": True})
                return
        else:
            self._refuse(entry, 405, "invalid_request_error", f"unhandled route {command} {KEYS_PATH}/{{hash}}")
            return
        if found is None:
            self._api_error(entry, 404, "not_found_error", "key not found")
            return
        self._send_key(entry, 200, {"data": found.document()})

    def _send_key(self, entry: Entry, status: int, payload: dict[str, Any]) -> None:
        entry.status = status
        self.send_json(status, payload, (("request-id", request_id(entry)),))

    def _control(self, path: str, raw: bytes, problem: BodyError | None) -> None:
        double = self.double
        routes = {
            "/_double/state": ("GET",),
            "/_double/journal": ("GET",),
            "/_double/scenario": ("GET", "POST"),
            "/_double/reset": ("POST",),
            "/_double/keys": ("GET",),
        }
        if path not in routes:
            self.send_json(404, {"error": "unknown control route"})
            return
        if self.command not in routes[path]:
            self.send_json(405, {"error": "method not allowed"}, (("allow", ", ".join(routes[path])),))
            return
        if problem is not None:
            self.send_json(problem.status, {"error": problem.message})
            return
        if path == "/_double/state":
            self.send_json(200, double.state())
        elif path == "/_double/journal":
            self.send_json(200, double.exchanges)
        elif path == "/_double/reset":
            double.reset()
            self.send_json(200, double.state())
        elif path == "/_double/keys":
            self.send_json(200, {"keys": double.keys.documents()})
        elif self.command == "GET":
            self.send_json(200, double.describe())
        else:
            try:
                double.control_load(decode_body(raw))
            except (ScenarioError, AccessError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, double.describe())

    # --- the messages route -------------------------------------------------------------------

    def _messages(self, body: dict[str, Any], headers: Mapping[str, str], entry: Entry, sub: SubKey | None) -> None:
        choice = self.double.choose(body, headers, request_id(entry))
        if choice.step is None:
            entry.note = choice.refusal
            if choice.status == 500:
                self._api_error(entry, 500, "api_error", f"model double: {choice.refusal}")
            else:
                self._api_error(entry, 400, "invalid_request_error", f"model double: {choice.refusal}")
            return
        step = choice.step
        entry.extra["step"] = step.id
        if step.delay_ms and self.double.stopping.wait(step.delay_ms / 1000):
            entry.note = "double stopped during the delay"
            self.close_connection = True
            return
        if step.error is not None:
            self._error(step.error, choice, body, entry)
        else:
            self._reply(step, choice, body, entry, sub)

    def _error(self, error: ErrorReply, choice: Choice, body: dict[str, Any], entry: Entry) -> None:
        payload = choice.error
        assert payload is not None
        if error.in_stream and body.get("stream"):
            entry.status = 200
            entry.note = f"stream error {error.type}"
            self.start_chunked(200, "text/event-stream; charset=utf-8", self._stream_headers(entry))
            usage = {"input_tokens": 1, "output_tokens": 0}
            model = str(body.get("model", ""))
            start = wire.stream_events(message_id(entry), model, [], "end_turn", usage, chunk_size=1)[0]
            self.write_chunk(wire.encode_event(*start))
            self.write_chunk(wire.encode_event("error", payload))
            self.end_chunked()
            return
        entry.status = error.status
        entry.note = f"error {error.status} {error.type}"
        self.send_json(error.status, payload, (("request-id", request_id(entry)), *error.headers))

    def _stream_headers(self, entry: Entry, *extra: tuple[str, str]) -> tuple[tuple[str, str], ...]:
        return (("cache-control", "no-cache"), ("request-id", request_id(entry)), *extra)

    def _reply(self, step: Step, choice: Choice, body: dict[str, Any], entry: Entry, sub: SubKey | None) -> None:
        reply = step.reply
        assert reply is not None
        blocks = choice.blocks
        stop = wire.stop_reason(reply, blocks)
        usage = wire.usage_for(reply, body, blocks)
        if sub is not None:
            self.double.keys.charge(sub, wire.openrouter_usage(usage, reply.cost)["cost"])
        start_usage = usage
        model = reply.model or str(body.get("model", DEFAULT_MODEL))
        provider: str | None = None
        extra: tuple[tuple[str, str], ...] = ()
        if choice.openrouter is not None:
            provider = reply.provider or choice.openrouter.provider
            usage = wire.openrouter_usage(usage, reply.cost)
            extra = (("x-generation-id", f"gen-double-{entry.seq:04d}"),)
        entry.status = 200
        if not body.get("stream"):
            message = wire.message(message_id(entry), model, blocks, stop, usage, provider=provider)
            self.send_json(200, message, (("request-id", request_id(entry)), *extra))
            return
        self.start_chunked(200, "text/event-stream; charset=utf-8", self._stream_headers(entry, *extra))
        events = wire.stream_events(
            message_id(entry),
            model,
            blocks,
            stop,
            usage,
            chunk_size=reply.chunk_size,
            start_usage=start_usage,
            provider=provider,
        )
        for name, data in events:
            self.write_chunk(wire.encode_event(name, data))
        self.end_chunked()
