"""Key lookup of the signature gate (03-api.md section 1, ADR-027 section 4).

The gate finds the public key of a signed request through key resolvers discovered as `<area>/keys.py:RESOLVERS`: a
mapping from a key-ID prefix ending in `.` (`session.` → HUB-03, `device.` → HUB-08, `agent.` → KOR-24) or an exact
key ID (`platform-root` → `config_api/keys.py`, from the configuration) to a `KeyResolver`. A resolver runs in a worker
thread, may use the database pool and returns the `ResolvedKey` (who signs and how to verify), None for an unknown
key, or raises `HubError` with a code of 03-api.md section 11 (`key_revoked`, `session_expired`, ...). It never
verifies the signature itself; the gate does that, checks the time window and the nonce.

Besides the interface this module registers the browser-session resolver (HUB-03, `RESOLVERS` at the end): a
browser signs every request with its session key `session.<32 hex>` (P-256 raw `r‖s`); the resolver finds the
`browser_session` row - unknown → `key_unknown` (the gate), ended, its passkey removed or its user disabled →
`401 key_revoked`, expired → `401 session_expired`. HUB-08 registers the device resolver in `device/keys.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg_pool import ConnectionPool

from agent_core.passkey.encoding import verify_session_signature
from agent_hub.clock import Clock
from agent_hub.config import HubConfig
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import ed25519_verify
from agent_hub.errors import HubError

PrincipalKind = Literal["anonymous", "session", "device", "agent", "root"]
ROOT_KEY_ID = "platform-root"
SESSION_PREFIX = "session."
DEVICE_PREFIX = "device."
AGENT_PREFIX = "agent."
# The key-ID forms of 03-api.md section 1 and 11-agent-access.md section 4; anything else is a malformed `Agent-Key`.
KEY_ID_RE = re.compile(r"session\.[0-9a-f]{32}|device\.[0-9a-f]{32}|agent\.[0-9a-f]{32}|platform-root")

Verify = Callable[[bytes, bytes], bool]


@dataclass(frozen=True)
class Principal:
    """Who sent a request: anonymous, a browser session (user), a device, an agent acting for its user (ADR-056) or
    the platform root (operator).

    `principal_id` is the user ID of a session or an agent, or the device ID; `actor_kind` is the audit actor kind;
    `attributes` carries what the resolver knows and the area checks need (e.g. the device state for `device:any`).
    """

    kind: PrincipalKind
    key_id: str | None
    principal_id: UUID | None
    actor_kind: str
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


ANONYMOUS = Principal(kind="anonymous", key_id=None, principal_id=None, actor_kind="anonymous")


@dataclass(frozen=True)
class ResolvedKey:
    """The principal behind a key ID and the verification of its signatures (`verify(signature, text)`).

    `refusal`: a known key whose holder is refused (`key_revoked`): the gate verifies the request's signature with the
    key first and then refuses with this code; the refusal is response-signed, so only the key's holder gets a signed
    refusal (03-api.md section 1: every response to a signed request is signed; a runner trusts only a verified
    `key_revoked`, 05-runner.md section 2).
    """

    principal: Principal
    verify: Verify
    refusal: str | None = None


class KeyContext(Protocol):
    """What a resolver may use (satisfied by `agent_hub.http.context.HubContext`)."""

    @property
    def config(self) -> HubConfig: ...

    @property
    def pool(self) -> ConnectionPool[Any]: ...

    @property
    def clock(self) -> Clock: ...


KeyResolver = Callable[[KeyContext, str], ResolvedKey | None]


def ed25519_verifier(public_key: bytes) -> Verify:
    """Verification of Ed25519 signatures (devices, platform root) by a 32-byte public key, through the hardened
    `agent_hub.crypto.keys.ed25519_verify` (small-order keys and non-canonical `S` refused, ATS-03). A malformed key
    raises `ValueError` here, as before."""
    Ed25519PublicKey.from_public_bytes(public_key)

    def verify(signature: bytes, text: bytes) -> bool:
        try:
            ed25519_verify(public_key, signature, text)
        except CryptoError:
            return False
        return True

    return verify


def session_verifier(public_key: bytes) -> Verify:
    """Verification of browser session signatures: P-256 raw `r‖s` by the 65-byte uncompressed point
    (`agent_core.passkey.encoding.verify_session_signature`)."""

    def verify(signature: bytes, text: bytes) -> bool:
        return verify_session_signature(public_key, signature, text)

    return verify


def resolver_for(resolvers: Mapping[str, KeyResolver], key_id: str) -> KeyResolver | None:
    """The resolver registered for the exact key ID, else for its prefix (the part up to and including the first
    `.`)."""
    exact = resolvers.get(key_id)
    if exact is not None:
        return exact
    prefix, dot, _ = key_id.partition(".")
    return resolvers.get(prefix + dot) if dot else None


def check_resolvers(resolvers: Mapping[str, KeyResolver]) -> None:
    """Refuse a registration key that is neither a prefix ending in `.` nor an exact key ID of 03-api.md section 1."""
    for key, resolver in resolvers.items():
        prefix_form = key in (SESSION_PREFIX, DEVICE_PREFIX, AGENT_PREFIX)
        if not (prefix_form or KEY_ID_RE.fullmatch(key)) or not callable(resolver):
            raise ValueError(f"key resolver {key!r}: key must be 'session.', 'device.', 'agent.' or an exact key ID")


def resolve_session(context: KeyContext, key_id: str) -> ResolvedKey | None:
    """The principal and verifier of a browser session key (02-data-model.md section 2.1 `browser_session`)."""
    with context.pool.connection() as connection:
        row = connection.execute(
            "SELECT s.user_id, s.public_key, s.expires_at,"
            " s.ended_at IS NOT NULL OR p.removed_at IS NOT NULL OR u.disabled_at IS NOT NULL"
            " FROM hub.browser_session s JOIN hub.passkey p ON p.credential_id = s.credential_id"
            " JOIN hub.principal u ON u.id = s.user_id WHERE s.key_id = %s",
            (key_id,),
        ).fetchone()
    if row is None:
        return None
    user_id, public_key, expires_at, revoked = row
    if revoked:
        raise HubError("key_revoked")
    if expires_at <= context.clock():
        raise HubError("session_expired")
    principal = Principal(kind="session", key_id=key_id, principal_id=user_id, actor_kind="user")
    return ResolvedKey(principal=principal, verify=session_verifier(bytes(public_key)))


RESOLVERS: Mapping[str, KeyResolver] = {SESSION_PREFIX: resolve_session}
