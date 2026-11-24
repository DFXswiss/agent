"""Signed requests under /api/key/ and the hub's signed answers (KEY-PROTOCOL.md sections 2, 4 to 6).

Every request whose raw path or percent-decoded path starts with `/api/key/` goes through
`KeyGate`. Checks 1 to 8 run before routing; only a request that passed them reaches a key
route, and only key routes answer under `/api/key/`. A route that takes a query validates it
after check 9 and before check 10.

A request may also be signed by a session key that a passkey certified (PASSKEY-PROTOCOL.md
section 7.1): `Agent-Key` is then a session key id, checks 2 to 5 read the stored session and its
passkey entry, and check 7 verifies the 64-byte `r ‖ s` signature with the session public key. The
session store and the P-256 verification come in through `SessionKeys`, so that this module stays
free of the passkey package. Check 9a runs on a route marked controller-only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass, field, replace
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
ERROR_CERTIFICATE_INVALID = "certificate_invalid"
ERROR_CONTROLLER_DEVICE_REQUIRED = "controller_device_required"
ERROR_SUBJECT = "subject_not_allowed"
CONTROLLER_SCOPE = "device:controller"
ERROR_QUERY_INVALID = "query_invalid"
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
        self.public_key = public
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
class SessionKey:
    """A stored session that was not ended (PASSKEY-PROTOCOL.md section 12, `passkey_session`):
    what checks 2 to 5 and 7 of section 7.1 read. The binary values are the raw bytes."""

    id: str
    passkey_id: str
    handle: str
    credential_id: bytes = field(repr=False)
    passkey_public_key: bytes = field(repr=False)
    session_public_key: bytes = field(repr=False)
    expires: int


@dataclass(frozen=True)
class SessionKeys:
    """What the key service needs to check a request signed by a session key.

    `valid_id` is the session key id form of check 1; `find` reads the session of an id once, the
    read of check 2, and returns None for an unknown or ended session; `verify` is check 7 with the
    session public key, the 64-byte signature and the request text.
    """

    valid_id: Callable[[str], bool]
    find: Callable[[str], "SessionKey | None"]
    verify: Callable[[bytes, bytes, bytes], bool]


@dataclass(frozen=True)
class KeyRequest:
    """A request that passed checks 1 to 8, with the key entry they established.

    `headers` are the request headers as received, names in lowercase. `params` is what the
    route's query parser returned; None for a route without one. For a request signed by a session
    key, `session` is its stored session and `entry` the passkey entry that certified it, whose
    scopes the session key has (PASSKEY-PROTOCOL.md section 7.1, check 9).
    """

    entry: "KeyEntry"
    method: str
    path: str
    raw_path: str
    query: str
    body: bytes
    headers: tuple[tuple[str, bytes], ...]
    params: Any = None
    session: SessionKey | None = None

    @property
    def key_id(self) -> str:
        """The `Agent-Key` of the request: the session key id or the keys-file id."""
        return self.entry.id if self.session is None else self.session.id

    def header_values(self, name: str) -> list[bytes]:
        """Every value of the header `name` (lowercase), in the order received."""
        return [value for key, value in self.headers if key == name]


@dataclass(frozen=True)
class KeyAnswer:
    """An answer of an endpoint with the result its log area writes (RECORD-PROTOCOL.md section 13).

    `result` is None when the area derives it from the status and the body.
    """

    status: int
    body: dict[str, Any]
    result: str | None


KeyHandler = Callable[[KeyRequest], Awaitable["tuple[int, dict[str, Any]] | KeyAnswer"]]
SubjectOf = Callable[[KeyRequest], "str | None"]
# Parses the raw query (as signed); None means the query is invalid (signed 400).
QueryParser = Callable[[str], Any]
# Writes the one request line of a log area: the request, the final status and the result.
AreaLog = Callable[[KeyRequest, int, str], None]


@dataclass(frozen=True)
class LogArea:
    """A path prefix under /api/key/ whose requests write their own request line instead of the
    line of section 5 for a `service` key. An endpoint failure in the area is logged by that line
    alone, never with its exception (RECORD-PROTOCOL.md section 13)."""

    prefix: str
    log: AreaLog

    def contains(self, request: KeyRequest) -> bool:
        return request.raw_path.startswith(self.prefix) or request.path.startswith(self.prefix)


def answer_result(status: int, body: dict[str, Any]) -> str:
    """The result of an answer in a log area without its own (RECORD-PROTOCOL.md section 13): `ok`
    for 200, otherwise the `error` of the body. A success of an area with another status, such as
    the `201` of an append, carries its own result. Only the request line of a log area has a
    result; the log lines of KEY-PROTOCOL.md section 5 and PASSKEY-PROTOCOL.md section 7.1 carry
    the status alone, so the key service never derives a result outside an area."""
    if status == 200:
        return "ok"
    return str(body["error"])


@dataclass(frozen=True)
class KeyRoute:
    method: str
    path: str
    handler: KeyHandler
    scope: str | None
    subject: SubjectOf | None
    query: QueryParser | None = None
    controller_only: bool = False


def controller_device_key(request: KeyRequest) -> bool:
    """Check 9a of PASSKEY-PROTOCOL.md section 7.1: the key is the device key of a device (an
    `agent` entry without `program`, section 7.4) and holds `device:controller`. A session key, a
    program key, an executor and a `service` key are not. Check 3 already refused a key revoked at
    now, and the loader keeps at most one unrevoked `agent` entry without `program` per device."""
    entry = request.entry
    return (
        request.session is None
        and entry.principal == "agent"
        and entry.program is None
        and CONTROLLER_SCOPE in entry.scopes
    )


class KeyRoutes:
    """The only routes under /api/key/. Matching is exact on method and decoded path."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], KeyRoute] = {}
        self._areas: list[LogArea] = []

    def add_area(self, prefix: str, log: AreaLog) -> None:
        if not prefix.startswith(KEY_PREFIX) or not prefix.endswith("/"):
            raise ValueError(f"a log area is a prefix under {KEY_PREFIX} that ends with /: {prefix}")
        if any(prefix.startswith(area.prefix) or area.prefix.startswith(prefix) for area in self._areas):
            raise ValueError(f"log area {prefix} overlaps a registered area")
        self._areas.append(LogArea(prefix, log))

    def area_of(self, request: KeyRequest) -> LogArea | None:
        for area in self._areas:
            if area.contains(request):
                return area
        return None

    def add(
        self,
        method: str,
        path: str,
        handler: KeyHandler,
        *,
        scope: str | None,
        subject: SubjectOf | None,
        query: QueryParser | None = None,
        controller_only: bool = False,
    ) -> None:
        if not path.startswith(KEY_PREFIX):
            raise ValueError(f"a key route must start with {KEY_PREFIX}: {path}")
        if _METHOD_RE.fullmatch(method) is None or method == "HEAD":
            raise ValueError(f"a key route needs an uppercase method other than HEAD: {method}")
        if (method, path) in self._routes:
            raise ValueError(f"key route {method} {path} is already registered")
        self._routes[(method, path)] = KeyRoute(
            method, path, handler, scope, subject, query, controller_only
        )

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


def _session_refusal(session: SessionKey, entry: "KeyEntry", now: int) -> _Refusal | None:
    """Checks 3 to 5 of PASSKEY-PROTOCOL.md section 7.1 for a session key, after check 2 found the
    session and its passkey entry: the passkey entry is not revoked, the session's expiry is later
    than now, and the entry is still the `webauthn` entry whose credential id, public key and
    handle were stored with the session at sign-in. These values are public; no secret is
    compared."""
    if entry.revoked_at(now):
        return _Refusal(401, ERROR_KEY_REVOKED)
    if session.expires <= now:
        return _Refusal(401, ERROR_KEY_EXPIRED)
    if (
        entry.kind != "webauthn"
        or entry.credential_id != session.credential_id
        or entry.public_key != session.passkey_public_key
        or entry.handle != session.handle
    ):
        return _Refusal(401, ERROR_CERTIFICATE_INVALID)
    return None


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
        sessions: SessionKeys,
    ) -> None:
        self.keys = keys
        self.store = store
        self.signer = signer
        self.host = host
        self.clock = clock
        self.routes = routes
        self.sessions = sessions

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
        area = self.routes.area_of(request)
        status, payload, result = await self._answer(request, area)
        body = b"" if head else payload
        hub_time = format_time(self.clock())
        text = response_text(
            request.key_id, outcome.nonce, outcome.agent_time, status, hub_time, sha256_hex(body)
        )
        headers = [
            (HEADER_HUB_KEY.encode("ascii"), self.signer.key_id.encode("ascii")),
            (HEADER_HUB_TIME.encode("ascii"), hub_time.encode("ascii")),
            (HEADER_HUB_SIGNATURE.encode("ascii"), self.signer.sign(text).encode("ascii")),
        ]
        if area is not None and result is not None:
            # The request line of the area replaces the `service` line of the next branch
            # (RECORD-PROTOCOL.md section 13), for every principal; `_answer` derives a result for
            # every request in an area. It does not replace the session line below, which carries
            # the passkey id that the request line has no field for.
            area.log(request, status, result)
        elif request.entry.principal == "service":
            LOG.info(
                "key request %s %s %s %d",
                request.entry.id,
                request.method,
                request.raw_path,
                status,
            )
        if request.session is not None:
            # PASSKEY-PROTOCOL.md section 7.1: one line for every request of a session key that
            # passed check 8, also in a log area; no body, certificate or signature.
            LOG.info(
                "key request %s %s %s %s %d",
                request.session.id,
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
        keys_file_id = valid_key_id(key_id)
        if not keys_file_id and not self.sessions.valid_id(key_id):
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
        # Checks 2 to 5: the key entry, or the session and its passkey entry.
        session: SessionKey | None = None
        if keys_file_id:
            entry = self.keys.get(key_id)
            if entry is None:
                return _Refusal(401, ERROR_KEY_UNKNOWN)
            if entry.revoked_at(now):
                return _Refusal(401, ERROR_KEY_REVOKED)
            if entry.expired_at(now):
                return _Refusal(401, ERROR_KEY_EXPIRED)
            if entry.kind != "ed25519":
                return _Refusal(401, ERROR_KEY_KIND)
        else:
            session = await asyncio.to_thread(self.sessions.find, key_id)
            entry = None if session is None else self.keys.get(session.passkey_id)
            if session is None or entry is None:
                return _Refusal(401, ERROR_KEY_UNKNOWN)
            refusal = _session_refusal(session, entry, now)
            if refusal is not None:
                return refusal
        # Check 6: the caller's clock.
        if abs(sent_at - now) > TIME_WINDOW_SECONDS:
            return _Refusal(401, ERROR_TIME_SKEW)
        # Check 7: the signature over the text built from what was received.
        raw_path = raw_path_bytes.decode("ascii")
        query = query_bytes.decode("ascii")
        target = raw_path + ("?" + query if query else "")
        text = request_text(key_id, method, self.host, target, agent_time, nonce, sha256_hex(body))
        if session is not None:
            if not self.sessions.verify(session.session_public_key, signature, text):
                return _Refusal(401, ERROR_SIGNATURE_INVALID)
        else:
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
            headers=tuple((name.decode("latin-1").lower(), value) for name, value in scope["headers"]),
            session=session,
        )
        return _Verified(request=request, agent_time=agent_time, nonce=nonce)

    async def _answer(self, request: KeyRequest, area: LogArea | None) -> tuple[int, bytes, str | None]:
        """Route, check 9, the query, check 10, and the endpoint, with the result for the request
        line of a log area. Any failure here is a signed 500.

        The result is derived only for a request in a log area: outside an area no line carries
        one, so a success with a status other than 200 there, such as the `201` of
        `POST /api/key/invites`, is answered as the endpoint returned it."""
        try:
            answer = await self._route(request)
            result = answer.result
            if result is None and area is not None:
                result = answer_result(answer.status, answer.body)
            return answer.status, _json_bytes(answer.body), result
        except Exception:
            if area is None:
                LOG.error("key request: the endpoint failed", exc_info=True)
            return 500, _json_bytes(_error(ERROR_INTERNAL)), ERROR_INTERNAL

    async def _route(self, request: KeyRequest) -> KeyAnswer:
        route = self.routes.find(request.method, request.path)
        if route is None:
            return KeyAnswer(404, _error(ERROR_NOT_FOUND), None)
        entry = request.entry
        if route.scope is not None and route.scope not in entry.scopes:
            return KeyAnswer(403, _error(ERROR_SCOPE_MISSING), None)
        # Check 9a (PASSKEY-PROTOCOL.md section 7.1), before the query and check 10.
        if route.controller_only and not controller_device_key(request):
            return KeyAnswer(403, _error(ERROR_CONTROLLER_DEVICE_REQUIRED), None)
        if route.query is not None:
            params = route.query(request.query)
            if params is None:
                return KeyAnswer(400, _error(ERROR_QUERY_INVALID), None)
            request = replace(request, params=params)
        if route.subject is not None and entry.principal == "service":
            subject = route.subject(request)
            if subject is None or entry.subjects is None or subject not in entry.subjects:
                return KeyAnswer(403, _error(ERROR_SUBJECT), None)
        answer = await route.handler(request)
        if isinstance(answer, KeyAnswer):
            return answer
        status, body = answer
        return KeyAnswer(status, body, None)


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
