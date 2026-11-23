"""The signature gate of the hub API (03-api.md section 1).

A signed request carries `Agent-Key`, `Agent-Time`, `Agent-Nonce` and `Agent-Signature` over the eight lines of
`agent_core.signing.request_text`: `agent-request-v1`, key ID, METHOD, the hub's host line (from
`AGENT_HUB_PUBLIC_URL`, never the `Host` header), the target as sent (raw path and `?query`), the time
`YYYY-MM-DDTHH:MM:SSZ`, the nonce (b64u of 16 bytes) and `sha256_hex(body)`. The gate

1. parses the headers (all four or none; anything malformed: `401 signature_missing`),
2. checks the time window of 120 s (`401 time_skew`),
3. finds the key through the discovered resolvers (`auth/keys.py`; none or unknown: `401 key_unknown`; the resolver
   may refuse with `session_expired`, ...; a revoked device key is returned with `refusal="key_revoked"`, which the
   gate raises after the signature verified, response-signed),
4. verifies the signature over the text built from what was received (a changed body changes the hash:
   `401 signature_invalid`),
5. records the nonce for 600 s (`401 nonce_replayed`).

Every non-streaming response to a request that passed the gate is signed with `agent-response-v1`
(`response_headers`). Device enrolment proves possession of a key that is not registered yet: the pipeline keeps the
parsed headers and the body, and the enrolment route completes the check with `verify_enrollment`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request
from starlette.types import Scope

from agent_core.signing import (
    HEADER_HUB_KEY,
    HEADER_HUB_SIGNATURE,
    HEADER_HUB_TIME,
    HubSigner,
    b64url_decode,
    format_time,
    parse_time,
    request_text,
    response_text,
    sha256_hex,
)
from agent_hub.auth.keys import KEY_ID_RE, KeyContext, KeyResolver, Principal, Verify, resolver_for
from agent_hub.clock import epoch_seconds
from agent_hub.errors import HubError
from agent_hub.http.context import PENDING_KEY, PRINCIPAL_KEY, SIGNED_KEY
from agent_hub.http.nonces import insert_nonce

TIME_WINDOW_SECONDS = 120
NONCE_BYTES = 16
SIGNATURE_BYTES = 64
HEADER_KEY = b"agent-key"
HEADER_TIME = b"agent-time"
HEADER_NONCE = b"agent-nonce"
HEADER_SIGNATURE = b"agent-signature"
SIGNED_HEADERS = (HEADER_KEY, HEADER_TIME, HEADER_NONCE, HEADER_SIGNATURE)


@dataclass(frozen=True)
class SignedHeaders:
    """The parsed signature headers of one request."""

    key_id: str
    agent_time: str
    sent_at: int
    nonce: str
    signature: bytes


@dataclass(frozen=True)
class PendingSignature:
    """A signed request whose key the route itself must supply (device enrolment)."""

    signed: SignedHeaders
    body: bytes


def _visible_ascii(raw: bytes) -> bool:
    return all(0x21 <= byte <= 0x7E for byte in raw)


def signed_headers(scope: Scope) -> SignedHeaders | None:
    """The signature headers; None when the request carries none of them, `signature_missing` when they are
    incomplete, repeated or malformed."""
    values: dict[bytes, list[bytes]] = {}
    for name, value in scope["headers"]:
        lowered = name.lower()
        if lowered in SIGNED_HEADERS:
            values.setdefault(lowered, []).append(value)
    if not values:
        return None
    texts: list[str] = []
    for name in SIGNED_HEADERS:
        found = values.get(name, [])
        if len(found) != 1 or not _visible_ascii(found[0]):
            raise HubError("signature_missing")
        texts.append(found[0].decode("ascii"))
    key_id, agent_time, nonce, signature_text = texts
    sent_at = parse_time(agent_time)
    nonce_raw = b64url_decode(nonce)
    signature = b64url_decode(signature_text)
    if (
        KEY_ID_RE.fullmatch(key_id) is None
        or sent_at is None
        or nonce_raw is None
        or len(nonce_raw) != NONCE_BYTES
        or signature is None
        or len(signature) != SIGNATURE_BYTES
    ):
        raise HubError("signature_missing")
    return SignedHeaders(key_id=key_id, agent_time=agent_time, sent_at=sent_at, nonce=nonce, signature=signature)


def request_target(scope: Scope) -> str:
    """The target as sent: the raw path and, when present, `?` and the raw query (visible ASCII only)."""
    raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
    path = raw_path.split(b"?", 1)[0]
    query = scope.get("query_string", b"")
    if not path.startswith(b"/") or not _visible_ascii(path) or not _visible_ascii(query):
        raise HubError("signature_missing")
    return path.decode("ascii") + ("?" + query.decode("ascii") if query else "")


def check_time(context: KeyContext, signed: SignedHeaders) -> None:
    """The time window of 120 s (checked before the key lookup, so stale requests cost no database work)."""
    if abs(signed.sent_at - epoch_seconds(context.clock)) > TIME_WINDOW_SECONDS:
        raise HubError("time_skew")


async def verify_with(context: KeyContext, scope: Scope, signed: SignedHeaders, body: bytes, verify: Verify) -> None:
    """Checks 3 to 5: time window, signature over the received request, nonce."""
    now = context.clock()
    check_time(context, signed)
    method = scope.get("method", "GET")
    text = request_text(
        signed.key_id,
        method,
        context.config.public.host,
        request_target(scope),
        signed.agent_time,
        signed.nonce,
        sha256_hex(body),
    )
    if not verify(signed.signature, text):
        raise HubError("signature_invalid")
    if not await asyncio.to_thread(insert_nonce, context.pool, signed.key_id, signed.nonce, now):
        raise HubError("nonce_replayed")


async def verify_request(
    context: KeyContext,
    resolvers: Mapping[str, KeyResolver],
    scope: Scope,
    signed: SignedHeaders,
    body: bytes,
) -> Principal:
    """The whole gate for a request whose key a resolver knows; the principal on success."""
    check_time(context, signed)
    resolver = resolver_for(resolvers, signed.key_id)
    resolved = None if resolver is None else await asyncio.to_thread(resolver, context, signed.key_id)
    if resolved is None:
        raise HubError("key_unknown")
    await verify_with(context, scope, signed, body, resolved.verify)
    if resolved.refusal is not None:
        scope[SIGNED_KEY] = signed  # the holder of the key signed it: the refusal is response-signed
        raise HubError(resolved.refusal)
    return resolved.principal


async def verify_enrollment(request: Request, context: KeyContext, verify: Verify, principal: Principal) -> None:
    """Complete the gate of an enrolment request with the key from its body (proof of possession).

    `principal` must carry the key ID of the request's `Agent-Key` (the enrolled device signs with its own key). On
    success the request counts as signed by `principal` and its response is signed.
    """
    pending = request.scope.get(PENDING_KEY)
    if not isinstance(pending, PendingSignature) or principal.key_id != pending.signed.key_id:
        raise HubError("signature_missing")
    await verify_with(context, request.scope, pending.signed, pending.body, verify)
    request.scope[PRINCIPAL_KEY] = principal
    request.scope[SIGNED_KEY] = pending.signed


def response_headers(
    signer: HubSigner, signed: SignedHeaders, status: int, body: bytes, hub_seconds: int
) -> list[tuple[bytes, bytes]]:
    """The `agent-response-v1` headers of a response to a signed request."""
    hub_time = format_time(hub_seconds)
    text = response_text(signed.key_id, signed.nonce, signed.agent_time, status, hub_time, sha256_hex(body))
    return [
        (HEADER_HUB_KEY.encode("ascii"), signer.key_id.encode("ascii")),
        (HEADER_HUB_TIME.encode("ascii"), hub_time.encode("ascii")),
        (HEADER_HUB_SIGNATURE.encode("ascii"), signer.sign(text).encode("ascii")),
    ]
