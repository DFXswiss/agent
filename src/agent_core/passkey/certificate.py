"""The statement (PASSKEY-PROTOCOL.md section 6.2) and `verify_certificate` (section 6.4).

Pure functions: "now" and the bound `max_lifetime` are inputs; nothing reads a clock. The signature
counter is not checked here; only the hub does that (S7, `webauthn.check_counter`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..signing import b64url_decode, b64url_encode, format_time, parse_time
from .encoding import (
    HUB_VALUE_BYTES,
    p256_point_valid,
    session_key_id,
    sha256,
    verify_passkey_signature,
)
from .errors import CERTIFICATE_EXPIRY_INVALID, SIGNATURE_INVALID, STATEMENT_INVALID, PasskeyRefusal
from .relying_party import RelyingParty
from .webauthn import TYPE_GET, check_client_data, check_sign_in_authenticator_data

STATEMENT_LABEL = "agent-session-v1"
STATEMENT_LINES = 5


@dataclass(frozen=True)
class Statement:
    """A statement that passed V1. `text` is the statement bytes."""

    text: bytes = field(repr=False)
    origin: str
    session_public_key: bytes = field(repr=False)
    hub_value: bytes = field(repr=False)
    expires: int


@dataclass(frozen=True)
class Certificate:
    """The five values of a certificate (section 6.2), as the raw bytes."""

    statement: bytes = field(repr=False)
    credential_id: bytes = field(repr=False)
    authenticator_data: bytes = field(repr=False)
    client_data_json: bytes = field(repr=False)
    signature: bytes = field(repr=False)

    def to_json(self) -> dict[str, str]:
        """The JSON form of section 6.2: each value as base64url, in the order shown there."""
        return {
            "statement": b64url_encode(self.statement),
            "credential_id": b64url_encode(self.credential_id),
            "authenticator_data": b64url_encode(self.authenticator_data),
            "client_data_json": b64url_encode(self.client_data_json),
            "signature": b64url_encode(self.signature),
        }


@dataclass(frozen=True)
class CertifiedSession:
    """What a certificate that passed V1 to V11 certifies."""

    session_key_id: str
    statement: Statement
    sign_count: int


def build_statement(
    *, origin: str, session_public_key: bytes, hub_value: bytes, expires: int
) -> bytes:
    """The five lines of section 6.2, joined by `\\n`, without a trailing line feed.

    Raises `ValueError` for a session public key that is no valid P-256 point or a hub value that
    is not 32 bytes; the hub builds a statement only from a key that passed B1.
    """
    if not p256_point_valid(session_public_key):
        raise ValueError("the session public key is not a valid P-256 point")
    if len(hub_value) != HUB_VALUE_BYTES:
        raise ValueError("the hub value must be 32 bytes")
    lines = (
        STATEMENT_LABEL,
        origin,
        b64url_encode(session_public_key),
        b64url_encode(hub_value),
        format_time(expires),
    )
    return "\n".join(lines).encode("utf-8")


def statement_challenge(statement: bytes) -> bytes:
    """The WebAuthn challenge of a sign-in: SHA-256 over the statement bytes."""
    return sha256(statement)


def parse_statement(statement: bytes, origin: str) -> Statement:
    """V1: UTF-8 of exactly five lines joined by `\\n`, without a trailing line feed; line 1 the
    label, line 2 exactly the origin, line 3 a valid 65-byte P-256 point and line 4 32 bytes, each
    as canonical base64url, line 5 in the time form. Every failure is `statement_invalid`."""
    try:
        text = statement.decode("utf-8")
    except UnicodeDecodeError:
        raise PasskeyRefusal(STATEMENT_INVALID) from None
    lines = text.split("\n")
    if len(lines) != STATEMENT_LINES or lines[0] != STATEMENT_LABEL or lines[1] != origin:
        raise PasskeyRefusal(STATEMENT_INVALID)
    session_public_key = b64url_decode(lines[2])
    hub_value = b64url_decode(lines[3])
    expires = parse_time(lines[4])
    if (
        session_public_key is None
        or not p256_point_valid(session_public_key)
        or hub_value is None
        or len(hub_value) != HUB_VALUE_BYTES
        or expires is None
    ):
        raise PasskeyRefusal(STATEMENT_INVALID)
    return Statement(
        text=statement,
        origin=origin,
        session_public_key=session_public_key,
        hub_value=hub_value,
        expires=expires,
    )


def verify_certificate(
    certificate: Certificate,
    *,
    passkey_public_key: bytes,
    relying_party: RelyingParty,
    now: int,
    max_lifetime: int,
) -> CertifiedSession:
    """Section 6.4: the steps V1 to V11 in this order; raises the `error` of the first that fails.

    `passkey_public_key` is the 65-byte point to verify with (the keys-file entry on the hub, the
    allow-list entry on a device); `now` is the verifier's clock in whole seconds; `max_lifetime`
    is `SESSION_KEY_MAX_SECONDS` on the hub and the configured lifetime on a device. The expected
    challenge is SHA-256 over the statement bytes, computed here. The credential id is part of the
    certificate but no step reads it: the caller found the public key by it.
    """
    statement = parse_statement(certificate.statement, relying_party.origin)
    check_client_data(
        certificate.client_data_json,
        expected_type=TYPE_GET,
        challenge=statement_challenge(certificate.statement),
        origin=relying_party.origin,
    )
    head = check_sign_in_authenticator_data(certificate.authenticator_data, relying_party.rp_id)
    if not verify_passkey_signature(
        passkey_public_key,
        certificate.signature,
        certificate.authenticator_data,
        certificate.client_data_json,
    ):
        raise PasskeyRefusal(SIGNATURE_INVALID)
    if not now < statement.expires <= now + max_lifetime:
        raise PasskeyRefusal(CERTIFICATE_EXPIRY_INVALID)
    return CertifiedSession(
        session_key_id=session_key_id(certificate.statement),
        statement=statement,
        sign_count=head.sign_count,
    )
