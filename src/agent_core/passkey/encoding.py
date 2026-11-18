"""Encodings and constants of PASSKEY-PROTOCOL.md section 2.

Pure functions: no clock, no environment, no file. The encodings of KEY-PROTOCOL.md section 2
(canonical base64url, the time form, the P-256 point) come from `agent_core.signing`.
"""

from __future__ import annotations

import io
import json
import re

import cbor2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from ..signing import b64url_encode, p256_public_key_valid

# The constants of section 2.
SESSION_KEY_MAX_SECONDS = 43200
SESSION_KEY_ISSUE_SECONDS = 43080
CEREMONY_SECONDS = 300
INVITE_MIN_SECONDS = 300
INVITE_MAX_SECONDS = 86400
INVITE_OPEN_LIMIT = 50
PENDING_SIGN_IN_LIMIT = 1000
ANONYMOUS_BODY_LIMIT = 65536
COMMAND_TIME_WINDOW = 120
SEQUENCE_MAX = 9007199254740991

# The sizes of the encodings of section 2.
P256_POINT_BYTES = 65
SESSION_SIGNATURE_BYTES = 64
PASSKEY_SIGNATURE_MAX_BYTES = 72
CREDENTIAL_ID_MIN_BYTES = 16
CREDENTIAL_ID_MAX_BYTES = 1023
HUB_VALUE_BYTES = 32
REGISTRATION_CHALLENGE_BYTES = 32
USER_ID_BYTES = 16
INVITE_TOKEN_BYTES = 32
INVITE_ID_RANDOM_BYTES = 16
CEREMONY_ID_BYTES = 16
FINGERPRINT_BYTES = 16
SESSION_KEY_ID_BYTES = 16

SESSION_KEY_PREFIX = "session."
INVITE_ID_PREFIX = "invite."

# The order n of P-256 (SEC 2, secp256r1): `r` and `s` of a session key signature are from 1 to n - 1.
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

# Check 3 of "CBOR items": the range of CBOR major types 0 and 1.
CBOR_INT_MIN = -(2**64)
CBOR_INT_MAX = 2**64 - 1

_SESSION_KEY_ID_RE = re.compile(r"session\.[0-9a-f]{32}")
_SCALAR_BYTES = 32


class JsonInvalid(ValueError):
    """A text that breaks the JSON rule of section 2. The message carries no part of the text."""


class CborInvalid(ValueError):
    """A CBOR item that fails one of the three checks of section 2. The message carries no byte."""


def sha256(data: bytes) -> bytes:
    """SHA-256 through `cryptography`."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize()


def fingerprint(public_key: bytes) -> str:
    """The first 16 bytes of SHA-256 over the 65-byte point, as 8 groups of 4 lowercase hex."""
    if len(public_key) != P256_POINT_BYTES:
        raise ValueError("a fingerprint is taken of a 65-byte P-256 point")
    digits = sha256(public_key)[:FINGERPRINT_BYTES].hex()
    return " ".join(digits[index : index + 4] for index in range(0, len(digits), 4))


def session_key_id(statement: bytes) -> str:
    """`session.` and the first 16 bytes of SHA-256 over the statement bytes as lowercase hex."""
    return SESSION_KEY_PREFIX + sha256(statement)[:SESSION_KEY_ID_BYTES].hex()


def valid_session_key_id(value: object) -> bool:
    """The session key id form: `session.` and 32 lowercase hex characters, 40 characters."""
    return isinstance(value, str) and _SESSION_KEY_ID_RE.fullmatch(value) is not None


def _exact_length(raw: bytes, size: int) -> bytes:
    if len(raw) != size:
        raise ValueError(f"the random value must be {size} bytes")
    return raw


def invite_id(random_bytes: bytes) -> str:
    """`invite.` and 16 random bytes as 32 lowercase hex characters."""
    return INVITE_ID_PREFIX + _exact_length(random_bytes, INVITE_ID_RANDOM_BYTES).hex()


def invite_token(random_bytes: bytes) -> str:
    """32 random bytes as base64url, 43 characters."""
    return b64url_encode(_exact_length(random_bytes, INVITE_TOKEN_BYTES))


def ceremony_id(random_bytes: bytes) -> str:
    """A sign-in id or a registration id: 16 random bytes as base64url, 22 characters."""
    return b64url_encode(_exact_length(random_bytes, CEREMONY_ID_BYTES))


def p256_point_valid(raw: bytes) -> bool:
    """A 65-byte uncompressed point on P-256 (the session public key and the passkey public key)."""
    return p256_public_key_valid(raw)


def _public_key(point: bytes) -> ec.EllipticCurvePublicKey:
    """The key object of a point; a point that is not valid raises `ValueError`."""
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)


def session_signature_scalars(signature: bytes) -> tuple[int, int] | None:
    """`r` and `s` of a session key signature (64 bytes `r ‖ s`), each from 1 to n - 1; else None.

    A high `s` is accepted.
    """
    if len(signature) != SESSION_SIGNATURE_BYTES:
        return None
    r = int.from_bytes(signature[:_SCALAR_BYTES], "big")
    s = int.from_bytes(signature[_SCALAR_BYTES:], "big")
    if not (1 <= r < P256_ORDER and 1 <= s < P256_ORDER):
        return None
    return r, s


def verify_session_signature(public_key: bytes, signature: bytes, text: bytes) -> bool:
    """ECDSA over P-256 with SHA-256 by a session key over `text` (sections 7 and 8).

    `public_key` is a valid 65-byte point (the caller holds it validated); the signature has the
    session key signature encoding of section 2.
    """
    scalars = session_signature_scalars(signature)
    if scalars is None:
        return False
    try:
        _public_key(public_key).verify(
            encode_dss_signature(*scalars), text, ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature:
        return False
    return True


def passkey_signature_canonical(signature: bytes) -> bool:
    """The passkey signature encoding of section 2: DER of at most 72 bytes that re-encodes equal."""
    if len(signature) > PASSKEY_SIGNATURE_MAX_BYTES:
        return False
    try:
        r, s = decode_dss_signature(signature)
        again = encode_dss_signature(r, s)
    except (ValueError, OverflowError):
        return False
    return again == signature


def verify_passkey_signature(
    public_key: bytes, signature: bytes, authenticator_data: bytes, client_data_json: bytes
) -> bool:
    """Section 4.3: the assertion signature over `authenticator_data ‖ SHA-256(client_data_json)`.

    `public_key` is the 65-byte point of the keys-file entry, valid by the loader.
    """
    if not passkey_signature_canonical(signature):
        return False
    signed = authenticator_data + sha256(client_data_json)
    try:
        _public_key(public_key).verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def _object_without_repeated_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise JsonInvalid("a key is written twice")
        out[key] = value
    return out


def _refuse_constant(name: str) -> object:
    raise JsonInvalid("NaN, Infinity and -Infinity are not JSON")


def parse_json(raw: bytes) -> object:
    """The JSON rule of section 2: UTF-8, Python's `json`, no repeated key and no constant.

    Raises `JsonInvalid` for every text that breaks the rule.
    """
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_repeated_key,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise JsonInvalid("the text breaks the JSON rule of section 2") from exc


def json_integer(value: object) -> bool:
    """A JSON integer of section 2: the parsed type is exactly `int` (never `bool` or `float`)."""
    return type(value) is int


def _cbor_types_valid(item: object, *, booleans: bool, size: int) -> bool:
    """Checks 2 and 3 of "CBOR items", at every depth, without recursion.

    `booleans` admits `true` and `false` as values, never as map keys: only for the extensions map.

    `cbor2` resolves the value-sharing tags 28 and 29, so one decoded value can stand for several
    places of the item. A tag-free item never does, so two refusals follow and lose no valid item:
    - An array or map object met a second time (also inside itself) is refused: a tag-free item
      decodes to a tree of fresh arrays and maps. The walk visits each container once and ends.
    - `size` is the number of bytes from the item's first byte to the end of the data. A tag-free
      item writes every text and byte string out in those bytes, so the lengths of all of them
      together never exceed `size`; a larger total comes from a shared string and is refused. Check
      1 therefore never encodes more than a small multiple of `size` bytes.
    """
    pending: list[tuple[object, bool]] = [(item, False)]
    seen: set[int] = set()
    remaining = size
    while pending:
        value, is_key = pending.pop()
        kind = type(value)
        if kind is int:
            if not CBOR_INT_MIN <= value <= CBOR_INT_MAX:  # type: ignore[operator]
                return False
        elif kind is list or kind is dict:
            if id(value) in seen:
                return False
            seen.add(id(value))
            if kind is list:
                pending.extend((element, False) for element in value)  # type: ignore[attr-defined]
            else:
                for key, element in value.items():  # type: ignore[attr-defined]
                    pending.append((key, True))
                    pending.append((element, False))
        elif kind is bool and booleans and not is_key:
            continue
        elif kind is bytes or kind is str:
            remaining -= len(value)  # type: ignore[arg-type]
            if remaining < 0:
                return False
        else:
            return False
    return True


# What `cbor2` raises for bytes that are no CBOR item: its decode errors (including the end of the
# data) and the plain errors a semantic tag decoder or a nesting depth can raise.
_CBOR_DECODE_FAILURES = (cbor2.CBORDecodeError, ValueError, TypeError, OverflowError, RecursionError)


def decode_cbor_item(data: bytes, start: int, *, booleans: bool) -> tuple[object, int]:
    """Decode one CBOR item at `start` and run the three checks of "CBOR items" (section 2).

    Returns the item and the position after it; raises `CborInvalid`. The item is decoded with
    `cbor2.CBORDecoder` over an `io.BytesIO` positioned at `start`. Check 1 compares the bytes at
    `start` with `cbor2.dumps(item, canonical=True)`: CBOR is self-delimiting, so those bytes
    equal the canonical form exactly when the item's own bytes do, and the item then ends where
    the canonical form ends.
    """
    stream = io.BytesIO(data)
    stream.seek(start)
    try:
        item = cbor2.CBORDecoder(stream).decode()
    except _CBOR_DECODE_FAILURES as exc:
        raise CborInvalid("the bytes are no CBOR item") from exc
    if not _cbor_types_valid(item, booleans=booleans, size=len(data) - start):
        raise CborInvalid("the CBOR item holds a type, an integer or a shared value not allowed")
    canonical = cbor2.dumps(item, canonical=True)
    end = start + len(canonical)
    if data[start:end] != canonical:
        raise CborInvalid("the CBOR item is not in canonical form")
    return item, end
