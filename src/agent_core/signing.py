"""Signed requests under /api/key/ and the hub's signed answers (KEY-PROTOCOL.md sections 2, 4 to 6).

Every request whose raw path or percent-decoded path starts with `/api/key/` goes through
`KeyGate`. Checks 1 to 8 run before routing; only a request that passed them reaches a key
route, and only key routes answer under `/api/key/`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from .db import Store
    from .keys import KeyEntry, KeyRing

# The hub runs under uvicorn's log configuration; its error logger is the one that has a
# handler at level INFO, so the start line and the service request lines go there.
LOG = logging.getLogger("uvicorn.error")

KEY_PREFIX = "/api/key/"
KEY_PREFIX_RAW = KEY_PREFIX.encode("ascii")
REQUEST_LABEL = "agent-request-v1"
RESPONSE_LABEL = "agent-response-v1"
MAX_BODY_BYTES = 1024 * 1024
TIME_WINDOW_SECONDS = 120
NONCE_BYTES = 16
SIGNATURE_BYTES = 64
ED25519_KEY_BYTES = 32
P256_KEY_BYTES = 65
SEED_RE = re.compile(r"[0-9a-f]{64}")

HEADER_KEY = "agent-key"
HEADER_TIME = "agent-time"
HEADER_NONCE = "agent-nonce"
HEADER_SIGNATURE = "agent-signature"
HEADER_HUB_KEY = "agent-hub-key"
HEADER_HUB_TIME = "agent-hub-time"
HEADER_HUB_SIGNATURE = "agent-hub-signature"

ERROR_SIGNATURE_MISSING = "signature_missing"
ERROR_KEY_UNKNOWN = "key_unknown"
ERROR_KEY_REVOKED = "key_revoked"
ERROR_KEY_EXPIRED = "key_expired"
ERROR_KEY_KIND = "key_kind_not_allowed"
ERROR_TIME_SKEW = "time_skew"
ERROR_SIGNATURE_INVALID = "signature_invalid"
ERROR_NONCE_REPLAYED = "nonce_replayed"
ERROR_SCOPE_MISSING = "scope_missing"
ERROR_SUBJECT = "subject_not_allowed"
ERROR_NOT_FOUND = "not_found"
ERROR_INTERNAL = "internal"

_KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_TIME_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
# A method token (RFC 9110) without lowercase letters.
_METHOD_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Z-]+")

# Ed25519 input validation (section 2): the field prime and the y values of the eight points of
# small order (the identity, the point of order 2, the two of order 4 and the four of order 8).
# The sign bit of x is ignored, so every encoding of these points is refused.
ED25519_FIELD_PRIME = 2**255 - 19
ED25519_ORDER8_Y = 2707385501144840649318225287225658788936804267575313519463743609750303402022
ED25519_SMALL_ORDER_Y = frozenset(
    {
        0,
        1,
        ED25519_FIELD_PRIME - 1,
        ED25519_ORDER8_Y,
        ED25519_FIELD_PRIME - ED25519_ORDER8_Y,
    }
)

Clock = Callable[[], int]


def system_clock() -> int:
    """The hub's clock in whole seconds since the epoch."""
    return int(time.time())


def valid_key_id(value: object) -> bool:
    """The `id` rule of section 3: 3 to 63 of a-z, 0-9 and `-`, starting and ending alphanumeric."""
    return isinstance(value, str) and _KEY_ID_RE.fullmatch(value) is not None


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: object) -> bytes | None:
    """Decode base64url without padding; None unless the text is the canonical encoding."""
    if not isinstance(text, str) or _B64URL_RE.fullmatch(text) is None or len(text) % 4 == 1:
        return None
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(raw) != text:
        return None
    return raw


def parse_time(text: object) -> int | None:
    """`YYYY-MM-DDTHH:MM:SSZ` to whole seconds since the epoch; None for any other form."""
    if not isinstance(text, str):
        return None
    match = _TIME_RE.fullmatch(text)
    if match is None:
        return None
    try:
        moment = datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp())


def format_time(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()


def ed25519_public_key_valid(raw: bytes) -> bool:
    """Refuse a wrong length, a non-canonical y value and every small-order encoding."""
    if len(raw) != ED25519_KEY_BYTES:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= ED25519_FIELD_PRIME:
        return False
    return y not in ED25519_SMALL_ORDER_Y


def p256_public_key_valid(raw: bytes) -> bool:
    """An uncompressed P-256 point that lies on the curve."""
    if len(raw) != P256_KEY_BYTES or raw[0] != 0x04:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return False
    return True


def request_text(
    key_id: str,
    method: str,
    host: str,
    target: str,
    agent_time: str,
    nonce: str,
    body_hash: str,
) -> bytes:
    """The eight lines a caller signs (section 5)."""
    lines = [REQUEST_LABEL, key_id, method, host, target, agent_time, nonce, body_hash]
    return "\n".join(lines).encode("utf-8")


def response_text(
    key_id: str,
    nonce: str,
    agent_time: str,
    status: int,
    hub_time: str,
    body_hash: str,
) -> bytes:
    """The seven lines the hub signs (section 6)."""
    lines = [RESPONSE_LABEL, key_id, nonce, agent_time, str(status), hub_time, body_hash]
    return "\n".join(lines).encode("utf-8")


class HubSigner:
    """The hub's Ed25519 key. Holds the private key object, never the seed text."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key
        public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key_text = b64url_encode(public)

    @staticmethod
    def from_seed_hex(key_id: str, seed_hex: str) -> "HubSigner":
        if not valid_key_id(key_id):
            raise ValueError("the hub key id breaks the id rule")
        if SEED_RE.fullmatch(seed_hex) is None:
            raise ValueError("the hub signing seed must be 64 lowercase hex characters")
        return HubSigner(key_id, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)))

    def sign(self, text: bytes) -> str:
        return b64url_encode(self._private_key.sign(text))

    def __repr__(self) -> str:
        return f"HubSigner(key_id={self.key_id!r}, public_key={self.public_key_text!r})"


@dataclass(frozen=True)
class KeyRequest:
    """A request that passed checks 1 to 8, with the key entry they established."""

    entry: "KeyEntry"
    method: str
    path: str
    raw_path: str
    query: str
    body: bytes


KeyHandler = Callable[[KeyRequest], Awaitable[tuple[int, dict[str, Any]]]]
SubjectOf = Callable[[KeyRequest], "str | None"]


@dataclass(frozen=True)
class KeyRoute:
    method: str
    path: str
    handler: KeyHandler
    scope: str | None
    subject: SubjectOf | None


class KeyRoutes:
    """The only routes under /api/key/. Matching is exact on method and decoded path."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], KeyRoute] = {}

    def add(
        self,
        method: str,
        path: str,
        handler: KeyHandler,
        *,
        scope: str | None,
        subject: SubjectOf | None,
    ) -> None:
        if not path.startswith(KEY_PREFIX):
            raise ValueError(f"a key route must start with {KEY_PREFIX}: {path}")
        if _METHOD_RE.fullmatch(method) is None or method == "HEAD":
            raise ValueError(f"a key route needs an uppercase method other than HEAD: {method}")
        if (method, path) in self._routes:
            raise ValueError(f"key route {method} {path} is already registered")
        self._routes[(method, path)] = KeyRoute(method, path, handler, scope, subject)

    def find(self, method: str, path: str) -> KeyRoute | None:
        return self._routes.get((method, path))

    def registered(self) -> list[tuple[str, str]]:
        return sorted(self._routes)


@dataclass(frozen=True)
class _Refusal:
    status: int
    error: str


@dataclass(frozen=True)
class _Verified:
    request: KeyRequest
    agent_time: str
    nonce: str


def under_key_prefix(scope: Scope) -> bool:
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes) and raw_path.split(b"?", 1)[0].startswith(KEY_PREFIX_RAW):
        return True
    return scope["path"].startswith(KEY_PREFIX)


def _visible_ascii(raw: bytes) -> bool:
    return all(0x21 <= byte <= 0x7E for byte in raw)


def _header_values(scope: Scope) -> dict[str, list[bytes]]:
    out: dict[str, list[bytes]] = {}
    for name, value in scope["headers"]:
        out.setdefault(name.decode("latin-1").lower(), []).append(value)
    return out


def _single_ascii(headers: dict[str, list[bytes]], name: str) -> str | None:
    values = headers.get(name)
    if values is None or len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole request body, or None once it passes the limit or the client is gone."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        body += message.get("body", b"")
        if len(body) > limit:
            return None
        if not message.get("more_body", False):
            return bytes(body)


def gunzip_limited(raw: bytes, limit: int) -> bytes | None:
    """Decode one gzip member; None when it is corrupt, truncated, followed by data or too large.

    Decoding stops as soon as the output passes the limit.
    """
    decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    data = raw
    try:
        while True:
            chunk = decoder.decompress(data, limit + 1 - len(out))
            out += chunk
            if len(out) > limit:
                return None
            if decoder.eof:
                break
            if not chunk and not decoder.unconsumed_tail:
                return None
            data = decoder.unconsumed_tail
    except zlib.error:
        return None
    if decoder.unused_data:
        return None
    return bytes(out)


def _json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _error(error: str) -> dict[str, str]:
    return {"error": error}


async def _send(
    send: Send, status: int, body: bytes, extra_headers: list[tuple[bytes, bytes]]
) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class KeyService:
    """Runs checks 1 to 10 and signs every answer to a request that passed check 8."""

    def __init__(
        self,
        *,
        keys: "KeyRing",
        store: "Store",
        signer: HubSigner,
        host: str,
        clock: Clock,
        routes: KeyRoutes,
    ) -> None:
        self.keys = keys
        self.store = store
        self.signer = signer
        self.host = host
        self.clock = clock
        self.routes = routes

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        head = scope["method"] == "HEAD"
        try:
            outcome = await self._check(scope, receive)
        except Exception:
            LOG.error("key request: a check failed unexpectedly", exc_info=True)
            outcome = _Refusal(500, ERROR_INTERNAL)
        if isinstance(outcome, _Refusal):
            body = b"" if head else _json_bytes(_error(outcome.error))
            await _send(send, outcome.status, body, [])
            return
        request = outcome.request
        status, payload = await self._answer(request)
        body = b"" if head else payload
        hub_time = format_time(self.clock())
        text = response_text(
            request.entry.id, outcome.nonce, outcome.agent_time, status, hub_time, sha256_hex(body)
        )
        headers = [
            (HEADER_HUB_KEY.encode("ascii"), self.signer.key_id.encode("ascii")),
            (HEADER_HUB_TIME.encode("ascii"), hub_time.encode("ascii")),
            (HEADER_HUB_SIGNATURE.encode("ascii"), self.signer.sign(text).encode("ascii")),
        ]
        if request.entry.principal == "service":
            LOG.info(
                "key request %s %s %s %d",
                request.entry.id,
                request.method,
                request.raw_path,
                status,
            )
        await _send(send, status, body, headers)

    async def _check(self, scope: Scope, receive: Receive) -> _Refusal | _Verified:
        missing = _Refusal(401, ERROR_SIGNATURE_MISSING)
        # Check 1: method, request target, the four headers, content coding and body.
        method = scope["method"]
        if _METHOD_RE.fullmatch(method) is None:
            return missing
        raw_path_bytes = scope.get("raw_path")
        if not isinstance(raw_path_bytes, bytes):
            return missing
        raw_path_bytes = raw_path_bytes.split(b"?", 1)[0]
        query_bytes = scope["query_string"]
        if (
            not raw_path_bytes.startswith(b"/")
            or not _visible_ascii(raw_path_bytes)
            or not _visible_ascii(query_bytes)
        ):
            return missing
        headers = _header_values(scope)
        key_id = _single_ascii(headers, HEADER_KEY)
        agent_time = _single_ascii(headers, HEADER_TIME)
        nonce = _single_ascii(headers, HEADER_NONCE)
        signature_text = _single_ascii(headers, HEADER_SIGNATURE)
        if key_id is None or agent_time is None or nonce is None or signature_text is None:
            return missing
        if not valid_key_id(key_id):
            return missing
        sent_at = parse_time(agent_time)
        if sent_at is None:
            return missing
        nonce_raw = b64url_decode(nonce)
        if nonce_raw is None or len(nonce_raw) != NONCE_BYTES:
            return missing
        signature = b64url_decode(signature_text)
        if signature is None or len(signature) != SIGNATURE_BYTES:
            return missing
        codings = headers.get("content-encoding")
        if codings is not None:
            if len(codings) != 1:
                return missing
            if codings[0].decode("latin-1").strip(" \t").lower() != "gzip":
                return missing
        raw_body = await _read_body(receive, MAX_BODY_BYTES)
        if raw_body is None:
            return missing
        body = raw_body if codings is None else gunzip_limited(raw_body, MAX_BODY_BYTES)
        if body is None:
            return missing
        now = self.clock()
        # Checks 2 to 5: the key entry.
        entry = self.keys.get(key_id)
        if entry is None:
            return _Refusal(401, ERROR_KEY_UNKNOWN)
        if entry.revoked_at(now):
            return _Refusal(401, ERROR_KEY_REVOKED)
        if entry.expired_at(now):
            return _Refusal(401, ERROR_KEY_EXPIRED)
        if entry.kind != "ed25519":
            return _Refusal(401, ERROR_KEY_KIND)
        # Check 6: the caller's clock.
        if abs(sent_at - now) > TIME_WINDOW_SECONDS:
            return _Refusal(401, ERROR_TIME_SKEW)
        # Check 7: the signature over the text built from what was received.
        raw_path = raw_path_bytes.decode("ascii")
        query = query_bytes.decode("ascii")
        target = raw_path + ("?" + query if query else "")
        text = request_text(key_id, method, self.host, target, agent_time, nonce, sha256_hex(body))
        try:
            Ed25519PublicKey.from_public_bytes(entry.public_key).verify(signature, text)
        except InvalidSignature:
            return _Refusal(401, ERROR_SIGNATURE_INVALID)
        # Check 8: one atomic insert under the unique constraint.
        try:
            fresh = await asyncio.to_thread(self.store.insert_nonce, key_id, nonce, now)
        except Exception:
            LOG.error("key request: the nonce store failed", exc_info=True)
            return _Refusal(500, ERROR_INTERNAL)
        if not fresh:
            return _Refusal(401, ERROR_NONCE_REPLAYED)
        request = KeyRequest(
            entry=entry,
            method=method,
            path=scope["path"],
            raw_path=raw_path,
            query=query,
            body=body,
        )
        return _Verified(request=request, agent_time=agent_time, nonce=nonce)

    async def _answer(self, request: KeyRequest) -> tuple[int, bytes]:
        """Route, checks 9 and 10, and the endpoint. Any failure here is a signed 500."""
        try:
            route = self.routes.find(request.method, request.path)
            if route is None:
                return 404, _json_bytes(_error(ERROR_NOT_FOUND))
            entry = request.entry
            if route.scope is not None and route.scope not in entry.scopes:
                return 403, _json_bytes(_error(ERROR_SCOPE_MISSING))
            if route.subject is not None and entry.principal == "service":
                subject = route.subject(request)
                if subject is None or entry.subjects is None or subject not in entry.subjects:
                    return 403, _json_bytes(_error(ERROR_SUBJECT))
            status, body = await route.handler(request)
            return status, _json_bytes(body)
        except Exception:
            LOG.error("key request: the endpoint failed", exc_info=True)
            return 500, _json_bytes(_error(ERROR_INTERNAL))


class KeyGate:
    """ASGI middleware: every request under /api/key/ is answered by the key service."""

    def __init__(self, app: ASGIApp, service: KeyService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and under_key_prefix(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.service.handle(scope, receive, send)
            return
        await self.app(scope, receive, send)
