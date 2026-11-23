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
7. records the read log row of an authenticated `GET` with a `read_resource`.

A WebSocket upgrade passes steps 1, 2, 4 and 5 and is closed with code 1008 when it fails; streams are never
response-signed.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_hub.auth.gate import PendingSignature, SignedHeaders, response_headers, signed_headers, verify_request
from agent_hub.auth.keys import ANONYMOUS, DEVICE_PREFIX, SESSION_PREFIX, Principal
from agent_hub.clock import epoch_seconds
from agent_hub.errors import INTERNAL_ERROR, HubError, error_bytes
from agent_hub.http.context import PENDING_KEY, PRINCIPAL_KEY, SIGNED_KEY, HubContext
from agent_hub.http.forwarded import client_address
from agent_hub.http.headers import json_response_messages
from agent_hub.http.readlog import read_row
from agent_hub.http.routing import API_PREFIX, REQUEST_BODY_BYTES, RouteSpec, match_route

LOG = logging.getLogger("agent_hub.http")
UNROUTED = RouteSpec(auth="anon", rights=("anon",), read_resource=None, max_body=REQUEST_BODY_BYTES, streaming=False)
REQUIRED_KIND = {"browser": "session", "device": "device", "root": "root"}
POLICY_VIOLATION = 1008
SOURCE_KEY = "agent_hub.source"
_QUERY_NAMES: dict[int, frozenset[str]] = {}


class ClientGone(Exception):
    """The client disconnected while the body was read."""


def key_kind(key_id: str) -> str:
    """The rate-limit class of a key ID (03-api.md section 1)."""
    if key_id.startswith(DEVICE_PREFIX):
        return "device"
    return "session" if key_id.startswith(SESSION_PREFIX) else "root"


def check_auth(spec: RouteSpec, principal: Principal, *, signed: bool) -> None:
    """The authentication class of the route (the right itself is checked by the area's handler)."""
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
        self, context: HubContext, scope: Scope, spec: RouteSpec, signed: SignedHeaders | None, body: bytes
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
        check_auth(spec, principal, signed=True)
        return principal

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        context: HubContext = self.hub.state.hub
        resolve_source(context, scope)
        match = match_route(self.hub.router.routes, scope)
        spec = match.spec or UNROUTED
        try:
            signed = signed_headers(scope)
            self._precheck(context, scope, spec, signed)
            body = await read_body(scope, receive, spec.max_body)
            principal = await self._authenticate(context, scope, spec, signed, body)
            check_query(match.route, scope)
        except ClientGone:
            return
        except HubError as error:
            await self._send_error(context, scope, send, error)
            return
        scope[PRINCIPAL_KEY] = principal
        request: Message = {"type": "http.request", "body": body, "more_body": False}
        status = await self._run(context, scope, spec, prepend(request, receive), send)
        principal = scope[PRINCIPAL_KEY]
        if spec.read_resource is not None and principal.kind != "anonymous":
            self._read_log(context, scope, spec.read_resource, principal, status)

    async def _send_error(self, context: HubContext, scope: Scope, send: Send, error: HubError) -> None:
        status = error.status
        body = b"" if scope.get("method") == "HEAD" else error_bytes(error.code, error.detail)
        extra = [(name.lower().encode("ascii"), value.encode("ascii")) for name, value in error.headers.items()]
        signed = scope.get(SIGNED_KEY)
        if isinstance(signed, SignedHeaders):
            extra += response_headers(context.signer, signed, status, body, epoch_seconds(context.clock))
        for message in json_response_messages(status, body, extra):
            await send(message)

    async def _run(self, context: HubContext, scope: Scope, spec: RouteSpec, receive: Receive, send: Send) -> int:
        status = 500
        started = False
        held: tuple[Message, SignedHeaders] | None = None
        chunks: list[bytes] = []

        async def send_signed(message: Message) -> None:
            nonlocal status, started, held
            if message["type"] == "http.response.start":
                status = int(message["status"])
                signed = scope.get(SIGNED_KEY)
                if isinstance(signed, SignedHeaders) and not spec.streaming:
                    held = (message, signed)
                    return
                started = True
            elif message["type"] == "http.response.body" and held is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = b"" if scope.get("method") == "HEAD" else b"".join(chunks)
                start, signed = held
                signature = response_headers(context.signer, signed, status, body, epoch_seconds(context.clock))
                held = None
                started = True
                await send({**start, "headers": [*start.get("headers", []), *signature]})
                message = {"type": "http.response.body", "body": body}
            await send(message)

        try:
            await self.app(scope, receive, send_signed)
        except Exception:
            LOG.error("request %s %s: the route failed", scope.get("method"), scope["path"], exc_info=True)
            if not started:
                error = HubError(INTERNAL_ERROR)
                status = error.status
                await self._send_error(context, scope, send, error)
        return status

    def _read_log(self, context: HubContext, scope: Scope, resource: str, principal: Principal, status: int) -> None:
        pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"))
        names = [name for name, _ in pairs]
        query = {name: value for name, value in pairs if names.count(name) == 1}
        try:
            row = read_row(
                principal,
                resource=resource,
                status=status,
                path_params=scope.get("path_params", {}),
                query=query,
                at=context.clock(),
            )
            context.read_log.record(row)
        except Exception:
            LOG.error("read log: the row of %s could not be recorded", resource, exc_info=True)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        context: HubContext = self.hub.state.hub
        resolve_source(context, scope)
        spec = match_route(self.hub.router.routes, scope).spec
        connect = await receive()
        try:
            if spec is None:
                raise HubError("not_found")
            signed = signed_headers(scope)
            self._precheck(context, scope, spec, signed)
            principal = await self._authenticate(context, scope, spec, signed, b"")
        except HubError:
            await send({"type": "websocket.close", "code": POLICY_VIOLATION})
            return
        scope[PRINCIPAL_KEY] = principal
        await self.app(scope, prepend(connect, receive), send)
