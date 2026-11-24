"""The request pipeline of the hub (03-api.md section 1): one ASGI middleware in front of every route.

For an HTTP request it

1. finds the route and its declaration (`agent_hub.http.routing`); a request without a route is handled like an
   anonymous route (`404 not_found` from the router),
2. parses the signature headers (`auth/gate.py`) and applies, on API paths, the anonymous rate limit to unsigned and
   enrolment requests and the failed-signature limit of the source to signed ones, before the body is read (the
   source is the peer address, or the client a trusted reverse proxy names: `agent_hub.http.forwarded`),
3. reads the body within the route's limit (1 MiB, 4 MiB for event batches; `413 size_exceeded`) and refuses a
   `Content-Encoding` (`400 request_invalid`: the body hash covers the bytes as sent),
4. runs the signature gate (a failed attempt counts against the source's failed-signature limit), then the limit of
   the key, and refuses query parameters the route does not declare (`400 request_invalid`, API paths),
5. enforces the route's authentication class (`401 signature_missing` without a signature, `403 forbidden` for the
   wrong kind of key),
6. calls the route with the body replayed, signs every non-streaming response to a signed request with
   `agent-response-v1`, answers an exception of the route with `503 unavailable` (never its text),
7. records the read of an authenticated `GET` with a `read_resource` before its answer leaves the hub (the first read
   of a key in its window is written, repeats are counted: `agent_hub.http.readlog`; a read that cannot be recorded
   is answered `503 unavailable` instead),
8. settles the audit of a write (`POST`, `PUT`, `PATCH`, `DELETE`) before its answer leaves the hub: the route ran
   under `agent_hub.audit.recorder.recording()`, and `agent_hub.http.writelog.settle` adds `request.write` or
   `request.refused` when the route's committed rows do not record the outcome. A write refused after its principal
   was known (wrong key kind, an undeclared query parameter) is settled the same way; one refused before (signature,
   limits, body) or answered `429` is not (ADR-063 section 1).

A WebSocket upgrade passes steps 1, 2, 4 and 5 and is closed with code 1008 when it fails; streams are never
response-signed.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_hub.agents.keys import touch
from agent_hub.agents.scope import agent_allowed
from agent_hub.audit.chain import ACTING_AGENT
from agent_hub.audit.recorder import Recording, recording
from agent_hub.auth.gate import PendingSignature, SignedHeaders, response_headers, signed_headers, verify_request
from agent_hub.auth.keys import AGENT_PREFIX, ANONYMOUS, DEVICE_PREFIX, SESSION_PREFIX, Principal
from agent_hub.clock import epoch_seconds
from agent_hub.errors import INTERNAL_ERROR, HubError, error_bytes
from agent_hub.http.context import PENDING_KEY, PRINCIPAL_KEY, SIGNED_KEY, HubContext
from agent_hub.http.forwarded import client_address
from agent_hub.http.headers import json_response_messages
from agent_hub.http.readlog import query_key, read_row
from agent_hub.http.routing import API_PREFIX, REQUEST_BODY_BYTES, RouteMatch, RouteSpec, match_route
from agent_hub.http.writelog import WRITE_METHODS, error_code, guard_row, settle

LOG = logging.getLogger("agent_hub.http")
UNROUTED = RouteSpec(auth="anon", rights=("anon",), read_resource=None, max_body=REQUEST_BODY_BYTES, streaming=False)
REQUIRED_KIND = {"browser": "session", "device": "device", "agent": "agent", "root": "root"}
POLICY_VIOLATION = 1008
SOURCE_KEY = "agent_hub.source"
_QUERY_NAMES: dict[int, frozenset[str]] = {}


def route_path(route: BaseRoute | None) -> str | None:
    """The path template of a matched route (`/v1/orgs/{org}`), None without a route."""
    return route.path if isinstance(route, APIRoute | APIWebSocketRoute) else None


def acting_agent(principal: Principal) -> UUID | None:
    """The agent ID of an agent principal (written into every audit row of its request, ADR-056), else None."""
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    return agent if isinstance(agent, UUID) else None


class ClientGone(Exception):
    """The client disconnected while the body was read."""


class ReadNotRecorded(Exception):
    """The read log could not record a read: the answer must not leave the hub."""


def key_kind(key_id: str) -> str:
    """The rate-limit class of a key ID (03-api.md section 1)."""
    if key_id.startswith(DEVICE_PREFIX):
        return "device"
    if key_id.startswith(AGENT_PREFIX):
        return "agent"
    return "session" if key_id.startswith(SESSION_PREFIX) else "root"


def check_auth(
    spec: RouteSpec, principal: Principal, *, signed: bool, method: str = "GET", path: str | None = None
) -> None:
    """The authentication class of the route (the right itself is checked by the area's handler).

    An active agent (ADR-056) passes every route a browser session of its user passes, except the WebAuthn
    ceremonies (`agent_hub.agents.scope`)."""
    if principal.kind == "agent":
        if not agent_allowed(method, path or "", spec.auth, principal.attributes.get("state")):
            raise HubError("forbidden")
        return
    if spec.auth == "anon" or (spec.auth == "device_enroll" and signed):
        return
    if spec.auth == "anon_or_browser":
        if principal.kind not in ("anonymous", "session"):
            raise HubError("forbidden")
        return
    if principal.kind == REQUIRED_KIND.get(spec.auth):
        return
    raise HubError("signature_missing" if principal.kind == "anonymous" else "forbidden")


def query_names(route: BaseRoute) -> frozenset[str]:
    """The query parameters a route declares (its own and those of its dependencies)."""
    names = _QUERY_NAMES.get(id(route))
    if names is None:
        found: set[str] = set()
        pending: list[Dependant] = [route.dependant] if isinstance(route, APIRoute | APIWebSocketRoute) else []
        while pending:
            dependant = pending.pop()
            found.update(param.alias for param in dependant.query_params)
            pending.extend(dependant.dependencies)
        names = frozenset(found)
        _QUERY_NAMES[id(route)] = names
    return names


def check_query(route: BaseRoute | None, scope: Scope) -> None:
    """03-api.md section 1: an unknown query parameter of an API request is `400 request_invalid`."""
    if route is None or not scope["path"].startswith(API_PREFIX):
        return
    sent = {name for name, _ in parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)}
    if not sent <= query_names(route):
        raise HubError("request_invalid")


def _content_length(scope: Scope, limit: int) -> None:
    for name, value in scope["headers"]:
        lowered = name.lower()
        if lowered == b"content-encoding":
            raise HubError("request_invalid")
        if lowered == b"content-length":
            if not value.isdigit():
                raise HubError("request_invalid")
            if int(value) > limit:
                raise HubError("size_exceeded", status=413)


async def read_body(scope: Scope, receive: Receive, limit: int) -> bytes:
    """The whole request body within `limit` bytes."""
    _content_length(scope, limit)
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise ClientGone
        body += message.get("body", b"")
        if len(body) > limit:
            raise HubError("size_exceeded", status=413)
        if not message.get("more_body", False):
            return bytes(body)


def prepend(first: Message, receive: Receive) -> Receive:
    """A `receive` that yields `first` once, then the client's further messages (disconnect)."""
    sent = False

    async def receive_again() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return first
        return await receive()

    return receive_again


def single_query(scope: Scope) -> dict[str, str]:
    """The query parameters that occur exactly once (a repeated one names nothing for the audit)."""
    pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"))
    names = [name for name, _ in pairs]
    return {name: value for name, value in pairs if names.count(name) == 1}


def peer_address(scope: Scope) -> str:
    """The peer address of the connection."""
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def resolve_source(context: HubContext, scope: Scope) -> None:
    """Find the source address of the request once, before any limit (`agent_hub.http.forwarded`)."""
    config = context.config
    scope[SOURCE_KEY] = client_address(
        peer_address(scope), scope.get("headers", []), config.trusted_proxies, config.forwarded_header
    )


def source_address(scope: Scope) -> str:
    """The source address of the request: as the pipeline resolved it (a forwarding header only from a trusted
    proxy), the peer address otherwise."""
    return str(scope.get(SOURCE_KEY) or peer_address(scope))


class HubPipeline:
    """ASGI middleware implementing the pipeline; `hub` is the FastAPI app whose routes and context it uses."""

    def __init__(self, app: ASGIApp, hub: FastAPI) -> None:
        self.app = app
        self.hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def _limit(self, context: HubContext, kind: str, identity: str) -> None:
        retry_after = context.limiter.hit(kind, identity)
        if retry_after is not None:
            raise HubError("rate_limited", headers={"Retry-After": str(retry_after)})

    def _precheck(self, context: HubContext, scope: Scope, spec: RouteSpec, signed: SignedHeaders | None) -> None:
        """Checks before the body is read: the anonymous limit of the source (taken by unsigned and enrolment
        requests), the failed-signature limit of the source (a source whose signatures failed 30 times in a minute
        gets no further gate work) and the authentication class of an unsigned request."""
        if scope["path"].startswith(API_PREFIX):
            source = source_address(scope)
            if signed is None or spec.auth == "device_enroll":
                self._limit(context, "anonymous", source)
            else:
                retry_after = context.limiter.blocked("failed", source)
                if retry_after is not None:
                    raise HubError("rate_limited", headers={"Retry-After": str(retry_after)})
        if signed is None:
            check_auth(spec, ANONYMOUS, signed=False)

    async def _authenticate(
        self,
        context: HubContext,
        scope: Scope,
        spec: RouteSpec,
        signed: SignedHeaders | None,
        body: bytes,
        *,
        path: str | None,
    ) -> Principal:
        if signed is None:
            return ANONYMOUS
        if spec.auth == "device_enroll":
            scope[PENDING_KEY] = PendingSignature(signed=signed, body=body)
            return ANONYMOUS
        try:
            principal = await verify_request(context, context.resolvers, scope, signed, body)
        except HubError:
            if scope["path"].startswith(API_PREFIX):
                key_id = signed.key_id
                if scope.get(SIGNED_KEY) is signed:
                    # a refusal after a verified signature (`key_revoked`) is no failed signature of the source, but
                    # it counts against the key's own limit (its holder gets no more answers than before)
                    self._limit(context, key_kind(key_id), key_id)
                else:
                    self._limit(context, "failed", source_address(scope))
            raise
        scope[SIGNED_KEY] = signed
        self._limit(context, principal.kind, str(principal.key_id))
        scope[PRINCIPAL_KEY] = principal  # known from here on: a refusal of a write is audited (ADR-063)
        check_auth(spec, principal, signed=True, method=scope.get("method", "GET"), path=path)
        agent = acting_agent(principal)
        if agent is not None and principal.attributes.get("state") == "active":
            # the use of an agent is recorded only for a verified signature (review L2, decisions KOR-24)
            await asyncio.to_thread(touch, context, agent)
        return principal

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        context: HubContext = self.hub.state.hub
        resolve_source(context, scope)
        match = match_route(self.hub.router.routes, scope)
        spec = match.spec or UNROUTED
        write = match.route is not None and scope.get("method") in WRITE_METHODS
        try:
            signed = signed_headers(scope)
            self._precheck(context, scope, spec, signed)
            body = await read_body(scope, receive, spec.max_body)
            principal = await self._authenticate(context, scope, spec, signed, body, path=route_path(match.route))
            scope[PRINCIPAL_KEY] = principal
            check_query(match.route, scope)
        except ClientGone:
            return
        except HubError as error:
            if write and error.code != "rate_limited" and isinstance(scope.get(PRINCIPAL_KEY), Principal):
                await self._settle(context, scope, match, Recording(), status=error.status, code=error.code)
            await self._send_error(context, scope, send, error)
            return
        request: Message = {"type": "http.request", "body": body, "more_body": False}
        acting = ACTING_AGENT.set(acting_agent(principal))
        try:
            if write:
                with recording() as recorded:
                    await self._run(context, scope, spec, prepend(request, receive), send, match=match, noted=recorded)
            else:
                await self._run(context, scope, spec, prepend(request, receive), send, match=match)
        finally:
            ACTING_AGENT.reset(acting)

    async def _settle(
        self, context: HubContext, scope: Scope, match: RouteMatch, noted: Recording, *, status: int, code: str
    ) -> None:
        """Settle the audit of a write (module docstring step 8); a failure is logged, the answer still goes out."""
        principal: Principal = scope[PRINCIPAL_KEY]
        row = guard_row(
            principal,
            method=str(scope.get("method")),
            route=str(route_path(match.route)),
            status=status,
            code=code,
            path_params=match.path_params,
            query=single_query(scope),
            at=context.clock(),
        )
        try:
            query = query_key(scope.get("query_string", b""))
            await asyncio.to_thread(settle, context.pool, context.read_log, noted, row, status=status, query=query)
        except Exception:
            LOG.error("audit: the write %s %s could not be settled", row.detail["method"], row.detail["route"])

    async def _send_error(self, context: HubContext, scope: Scope, send: Send, error: HubError) -> None:
        status = error.status
        body = b"" if scope.get("method") == "HEAD" else error_bytes(error.code, error.detail)
        extra = [(name.lower().encode("ascii"), value.encode("ascii")) for name, value in error.headers.items()]
        signed = scope.get(SIGNED_KEY)
        if isinstance(signed, SignedHeaders):
            extra += response_headers(context.signer, signed, status, body, epoch_seconds(context.clock))
        for message in json_response_messages(status, body, extra):
            await send(message)

    async def _run(
        self,
        context: HubContext,
        scope: Scope,
        spec: RouteSpec,
        receive: Receive,
        send: Send,
        *,
        match: RouteMatch,
        noted: Recording | None = None,
    ) -> int:
        """Run the route. A response is held until its last chunk when it must be signed or, for a write (`noted` is
        its recorder), settled; a read with a `read_resource` is recorded before its first byte leaves."""
        status = 500
        started = False
        held: Message | None = None
        chunks: list[bytes] = []
        write = noted is not None

        async def release(start: Message) -> None:
            nonlocal started
            body = b"" if scope.get("method") == "HEAD" else b"".join(chunks)
            headers = list(start.get("headers", []))
            signed = scope.get(SIGNED_KEY)
            if isinstance(signed, SignedHeaders):
                headers += response_headers(context.signer, signed, status, body, epoch_seconds(context.clock))
            if noted is not None:
                await self._settle(context, scope, match, noted, status=status, code=error_code(status, body))
            else:
                await self._admit_read(context, scope, spec, status)
            started = True
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        async def send_held(message: Message) -> None:
            nonlocal status, started, held
            if message["type"] == "http.response.start":
                status = int(message["status"])
                if write or (isinstance(scope.get(SIGNED_KEY), SignedHeaders) and not spec.streaming):
                    held = message
                    return
                if status >= 300 or not spec.streaming:
                    # an accepted stream records its own reads, so it can drop the stream when that fails (review M3)
                    await self._admit_read(context, scope, spec, status)
                started = True
            elif message["type"] == "http.response.body" and held is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                start, held = held, None
                await release(start)
                return
            await send(message)

        try:
            await self.app(scope, receive, send_held)
        except Exception as exc:
            if isinstance(exc, ReadNotRecorded):
                LOG.error("request %s %s: the read could not be recorded", scope.get("method"), scope["path"])
            else:
                LOG.error("request %s %s: the route failed", scope.get("method"), scope["path"], exc_info=True)
            if not started:
                error = HubError(INTERNAL_ERROR)
                status = error.status
                if noted is not None:
                    await self._settle(context, scope, match, noted, status=status, code=error.code)
                await self._send_error(context, scope, send, error)
        return status

    async def _admit_read(self, context: HubContext, scope: Scope, spec: RouteSpec, status: int) -> None:
        """Record the read before its answer leaves the hub (`ReadNotRecorded` when that fails)."""
        principal = scope.get(PRINCIPAL_KEY)
        if spec.read_resource is None or not isinstance(principal, Principal) or principal.kind == "anonymous":
            return
        try:
            row = read_row(
                principal,
                resource=spec.read_resource,
                status=status,
                path_params=scope.get("path_params", {}),
                query=single_query(scope),
                at=context.clock(),
            )
            await asyncio.to_thread(context.read_log.admit, row, query_key(scope.get("query_string", b"")))
        except Exception as exc:
            raise ReadNotRecorded from exc

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        context: HubContext = self.hub.state.hub
        resolve_source(context, scope)
        match = match_route(self.hub.router.routes, scope)
        spec = match.spec
        connect = await receive()
        try:
            if spec is None:
                raise HubError("not_found")
            signed = signed_headers(scope)
            self._precheck(context, scope, spec, signed)
            principal = await self._authenticate(context, scope, spec, signed, b"", path=route_path(match.route))
        except HubError:
            await send({"type": "websocket.close", "code": POLICY_VIOLATION})
            return
        scope[PRINCIPAL_KEY] = principal
        await self.app(scope, prepend(connect, receive), send)
