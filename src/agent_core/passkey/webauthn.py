"""Verifying a passkey response (PASSKEY-PROTOCOL.md section 4) and a registration (section 5.5).

Pure functions: no clock, no environment, no file and no database. Every failure raises a
`PasskeyRefusal` with the value the specification names; the checks run in the order of the
specification and the first failure wins. The checks R1 to R3 and R14 of section 5.5 need the
request, the database or the keys file and are not here.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

from ..signing import b64url_decode
from .encoding import (
    CREDENTIAL_ID_MAX_BYTES,
    CREDENTIAL_ID_MIN_BYTES,
    CborInvalid,
    JsonInvalid,
    decode_cbor_item,
    p256_point_valid,
    parse_json,
    sha256,
)
from .errors import (
    ATTESTATION_INVALID,
    AUTHENTICATOR_DATA_INVALID,
    CHALLENGE_MISMATCH,
    CLIENT_DATA_INVALID,
    COUNTER_REGRESSED,
    CREDENTIAL_INVALID,
    ORIGIN_MISMATCH,
    RP_ID_MISMATCH,
    USER_PRESENCE_MISSING,
    USER_VERIFICATION_MISSING,
    PasskeyRefusal,
)
from .relying_party import RelyingParty

TYPE_CREATE = "webauthn.create"
TYPE_GET = "webauthn.get"

# The flags of the authenticator data (section 4.2).
FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40
FLAG_ED = 0x80

# The layout of the authenticator data (sections 4.2 and 5.5, R12).
RP_ID_HASH_BYTES = 32
FLAGS_OFFSET = 32
COUNTER_OFFSET = 33
AUTHENTICATOR_DATA_HEAD_BYTES = 37
AAGUID_BYTES = 16
CREDENTIAL_ID_LENGTH_BYTES = 2

# The credential public key of R13: a COSE key of type EC2 (2), algorithm ES256 (-7), curve P-256 (1).
COSE_KTY = 1
COSE_ALG = 3
COSE_CRV = -1
COSE_X = -2
COSE_Y = -3
COSE_KEYS = frozenset({COSE_KTY, COSE_ALG, COSE_CRV, COSE_X, COSE_Y})
COSE_KTY_EC2 = 2
COSE_ALG_ES256 = -7
COSE_CRV_P256 = 1
COORDINATE_BYTES = 32

ATTESTATION_KEYS = frozenset({"fmt", "attStmt", "authData"})


@dataclass(frozen=True)
class AuthenticatorData:
    """The parsed first 37 bytes of authenticator data that passed section 4.2 steps 1 to 5."""

    flags: int
    sign_count: int


@dataclass(frozen=True)
class Registration:
    """A registration that passed R4 to R13: the credential id and the 65-byte public key."""

    credential_id: bytes = field(repr=False)
    public_key: bytes = field(repr=False)
    sign_count: int
    flags: int


def check_client_data(
    client_data_json: bytes, *, expected_type: str, challenge: bytes, origin: str
) -> None:
    """Section 4.1, steps 1 to 5, on the exact bytes the browser returned.

    Steps 1 and 2 fail with `client_data_invalid`, step 3 with `challenge_mismatch`, steps 4 and 5
    with `origin_mismatch`. Any other member is ignored.
    """
    try:
        data = parse_json(client_data_json)
    except JsonInvalid:
        raise PasskeyRefusal(CLIENT_DATA_INVALID) from None
    if type(data) is not dict or data.get("type") != expected_type:
        raise PasskeyRefusal(CLIENT_DATA_INVALID)
    received = b64url_decode(data.get("challenge"))
    if received is None or not hmac.compare_digest(received, challenge):
        raise PasskeyRefusal(CHALLENGE_MISMATCH)
    if data.get("origin") != origin:
        raise PasskeyRefusal(ORIGIN_MISMATCH)
    if ("crossOrigin" in data and data["crossOrigin"] is not False) or "topOrigin" in data:
        raise PasskeyRefusal(ORIGIN_MISMATCH)


def _check_head(authenticator_data: bytes, rp_id: str) -> AuthenticatorData:
    """Section 4.2 steps 1 to 5; step 1 runs before any byte is read."""
    if len(authenticator_data) < AUTHENTICATOR_DATA_HEAD_BYTES:
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    expected = sha256(rp_id.encode("utf-8"))
    if not hmac.compare_digest(authenticator_data[:RP_ID_HASH_BYTES], expected):
        raise PasskeyRefusal(RP_ID_MISMATCH)
    flags = authenticator_data[FLAGS_OFFSET]
    if not flags & FLAG_UP:
        raise PasskeyRefusal(USER_PRESENCE_MISSING)
    if not flags & FLAG_UV:
        raise PasskeyRefusal(USER_VERIFICATION_MISSING)
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    sign_count = int.from_bytes(
        authenticator_data[COUNTER_OFFSET:AUTHENTICATOR_DATA_HEAD_BYTES], "big"
    )
    return AuthenticatorData(flags=flags, sign_count=sign_count)


def check_sign_in_authenticator_data(authenticator_data: bytes, rp_id: str) -> AuthenticatorData:
    """Section 4.2 steps 1 to 6 for a sign-in (V5 to V9).

    Step 6: flag AT not set; without flag ED exactly 37 bytes. With ED the bytes after the first 37
    are not parsed; the signature covers them.
    """
    head = _check_head(authenticator_data, rp_id)
    if head.flags & FLAG_AT or (
        not head.flags & FLAG_ED and len(authenticator_data) != AUTHENTICATOR_DATA_HEAD_BYTES
    ):
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    return head


def check_counter(stored: int | None, received: int) -> int:
    """Section 4.4: the value to keep after an accepted sign-in, or `counter_regressed`.

    Accepted when no value is kept, when both are 0, and when `received` is higher than `stored`;
    every other case is refused. The kept value is then `received`.
    """
    if stored is None or received > stored or (stored == 0 and received == 0):
        return received
    raise PasskeyRefusal(COUNTER_REGRESSED)


def _attested_item(authenticator_data: bytes, start: int, *, booleans: bool) -> tuple[object, int]:
    try:
        return decode_cbor_item(authenticator_data, start, booleans=booleans)
    except CborInvalid:
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID) from None


def _attested_credential(authenticator_data: bytes, flags: int) -> tuple[bytes, object]:
    """R12, section 4.2 step 7: flag AT and the layout after the first 37 bytes.

    The AAGUID (16 bytes), the credential id length L (2 bytes, big-endian), the credential id
    (L bytes), one CBOR item (the credential public key), with flag ED one CBOR map (the extensions
    map, where `true` and `false` are accepted as values), then nothing.
    """
    if not flags & FLAG_AT:
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    length_start = AUTHENTICATOR_DATA_HEAD_BYTES + AAGUID_BYTES
    id_start = length_start + CREDENTIAL_ID_LENGTH_BYTES
    length = int.from_bytes(authenticator_data[length_start:id_start], "big")
    id_end = id_start + length
    # Data that ends inside the AAGUID or the length field also ends before `id_end`.
    if len(authenticator_data) < id_end:
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    key_item, end = _attested_item(authenticator_data, id_end, booleans=False)
    if flags & FLAG_ED:
        extensions, end = _attested_item(authenticator_data, end, booleans=True)
        if type(extensions) is not dict:
            raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    if end != len(authenticator_data):
        raise PasskeyRefusal(AUTHENTICATOR_DATA_INVALID)
    return authenticator_data[id_start:id_end], key_item


def credential_public_key(item: object) -> bytes:
    """R13 on a decoded credential public key: the 65-byte point `0x04 ‖ x ‖ y`.

    A map with exactly the keys 1, 3, -1, -2 and -3, each of type exactly `int`; the values of 1,
    3 and -1 of type exactly `int` and equal to 2, -7 and 1; the values of -2 and -3 of type
    exactly `bytes` of 32 bytes; the point on P-256. This holds also without the type check of
    R12, so `-1: True`, `3: -7.0` and a key `1.0` are refused here as well.
    """
    if type(item) is not dict or set(map(type, item)) != {int} or set(item) != COSE_KEYS:
        raise PasskeyRefusal(CREDENTIAL_INVALID)
    kty, alg, crv = item[COSE_KTY], item[COSE_ALG], item[COSE_CRV]
    if set(map(type, (kty, alg, crv))) != {int} or (kty, alg, crv) != (
        COSE_KTY_EC2,
        COSE_ALG_ES256,
        COSE_CRV_P256,
    ):
        raise PasskeyRefusal(CREDENTIAL_INVALID)
    x, y = item[COSE_X], item[COSE_Y]
    if set(map(type, (x, y))) != {bytes} or len(x) != COORDINATE_BYTES or len(y) != COORDINATE_BYTES:
        raise PasskeyRefusal(CREDENTIAL_INVALID)
    point = b"\x04" + x + y
    if not p256_point_valid(point):
        raise PasskeyRefusal(CREDENTIAL_INVALID)
    return point


def _authenticator_data_of(attestation_object: bytes) -> bytes:
    """R7: one CBOR map passing the checks of "CBOR items", exactly `fmt` (text), `attStmt`
    (map) and `authData` (bytes), and no data after it. `fmt` and `attStmt` are otherwise ignored."""
    try:
        item, end = decode_cbor_item(attestation_object, 0, booleans=False)
    except CborInvalid:
        raise PasskeyRefusal(ATTESTATION_INVALID) from None
    if (
        end != len(attestation_object)
        or type(item) is not dict
        or set(item) != ATTESTATION_KEYS
        or type(item["fmt"]) is not str
        or type(item["attStmt"]) is not dict
        or type(item["authData"]) is not bytes
    ):
        raise PasskeyRefusal(ATTESTATION_INVALID)
    return item["authData"]


def verify_registration(
    *,
    credential_id: bytes,
    client_data_json: bytes,
    attestation_object: bytes,
    challenge: bytes,
    relying_party: RelyingParty,
) -> Registration:
    """R4 to R13 of section 5.5, in this order, for the decoded fields of the finish body.

    `challenge` is the registration's 32-byte challenge. R4 and R5 to R6 are section 4.1 with type
    `webauthn.create`; R8 to R11 are section 4.2 steps 1 to 4; R12 is steps 5 and 7.
    """
    check_client_data(
        client_data_json,
        expected_type=TYPE_CREATE,
        challenge=challenge,
        origin=relying_party.origin,
    )
    authenticator_data = _authenticator_data_of(attestation_object)
    head = _check_head(authenticator_data, relying_party.rp_id)
    attested_id, key_item = _attested_credential(authenticator_data, head.flags)
    if (
        not CREDENTIAL_ID_MIN_BYTES <= len(attested_id) <= CREDENTIAL_ID_MAX_BYTES
        or not hmac.compare_digest(attested_id, credential_id)
    ):
        raise PasskeyRefusal(CREDENTIAL_INVALID)
    public_key = credential_public_key(key_item)
    return Registration(
        credential_id=attested_id,
        public_key=public_key,
        sign_count=head.sign_count,
        flags=head.flags,
    )
