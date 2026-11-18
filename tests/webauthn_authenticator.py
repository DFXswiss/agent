"""A software authenticator for the passkey tests (PASSKEY-PROTOCOL.md section 13.1).

Built on `cryptography` and `cbor2` (decision 4). It creates real P-256 key pairs, answers
`create` and `get` exactly as a browser hands the fields to the page, and can be told to produce
every malformed variant the tests need: another origin or rp id, any flag set or cleared, any
credential id length from 0 to 65535 bytes, a fixed, increasing or given counter, `crossOrigin`,
`topOrigin`, another client data `type` or challenge, bytes appended to or cut from the
authenticator data, raw CBOR in place of the attestation object, the credential public key or the
extensions map (non-canonical, a key written twice, a tag, a float, a simple value, `true`,
`false`, `null`), and a corrupted signature.

The source of randomness and the signer are injected, so that the generator of the shared test
vectors (`tests/passkey_vectors.py`) can run it deterministically.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Callable

import cbor2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.signing import b64url_decode, b64url_encode

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40
FLAG_ED = 0x80

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


# A client data member that the authenticator leaves out.
ABSENT = _Absent()

Random = Callable[[int], bytes]


def system_random(size: int) -> bytes:
    return secrets.token_bytes(size)


class Signer:
    """Signs with `cryptography`'s ECDSA over P-256 and SHA-256 and returns DER."""

    def sign(self, private_key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
        return private_key.sign(message, ec.ECDSA(hashes.SHA256()))


def private_key_from(random: Random) -> ec.EllipticCurvePrivateKey:
    """A P-256 private key whose scalar comes from 32 bytes of `random`."""
    scalar = int.from_bytes(random(32), "big") % (P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def point_of(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    """The 65-byte uncompressed point of a key."""
    return private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


# --- raw CBOR pieces -----------------------------------------------------------------------------


def cbor(value: object) -> bytes:
    """The canonical encoding of one value by `cbor2`."""
    return cbor2.dumps(value, canonical=True)


def cbor_head(major: int, length: int, *, width: int | None) -> bytes:
    """The head of a CBOR item; `width` forces 1, 2, 4 or 8 length bytes (non-canonical when the
    length would fit shorter), None writes the shortest form."""
    if width is None:
        if length < 24:
            return bytes([major << 5 | length])
        width = 1 if length < 0x100 else 2 if length < 0x10000 else 4 if length < 0x100000000 else 8
    info = {1: 24, 2: 25, 4: 26, 8: 27}[width]
    return bytes([major << 5 | info]) + length.to_bytes(width, "big")


Pairs = list[tuple[bytes, bytes]]


def cbor_map(pairs: Pairs, *, width: int | None) -> bytes:
    """A map written from raw (key, value) pairs in the given order; a pair may repeat a key."""
    return cbor_head(5, len(pairs), width=width) + b"".join(key + value for key, value in pairs)


def with_value(pairs: Pairs, key: object, value: bytes) -> Pairs:
    """The pairs with the value of `key` replaced by raw bytes."""
    encoded = cbor(key)
    return [(k, value if k == encoded else v) for k, v in pairs]


def with_key(pairs: Pairs, key: object, raw_key: bytes) -> Pairs:
    """The pairs with the key `key` written as the raw bytes `raw_key` (for example a float)."""
    encoded = cbor(key)
    return [(raw_key if k == encoded else k, v) for k, v in pairs]


def with_pair(pairs: Pairs, raw_key: bytes, value: bytes) -> Pairs:
    """The pairs and one more pair at the end (a key written twice when it repeats one)."""
    return [*pairs, (raw_key, value)]


def without_key(pairs: Pairs, key: object) -> Pairs:
    encoded = cbor(key)
    return [(k, v) for k, v in pairs if k != encoded]


def cose_key_pairs(point: bytes) -> Pairs:
    """The credential public key of an ES256 credential, in canonical order: 1, 3, -1, -2, -3."""
    return [
        (cbor(1), cbor(2)),
        (cbor(3), cbor(-7)),
        (cbor(-1), cbor(1)),
        (cbor(-2), cbor(point[1:33])),
        (cbor(-3), cbor(point[33:65])),
    ]


def attestation_pairs(authenticator_data: bytes) -> Pairs:
    """The attestation object of format `none` in canonical order: `fmt`, `attStmt`, `authData`."""
    return [
        (cbor("fmt"), cbor("none")),
        (cbor("attStmt"), cbor({})),
        (cbor("authData"), cbor(authenticator_data)),
    ]


# Raw items for the malformed variants of section 13.2.
FLOAT_MINUS_7 = bytes.fromhex("f9c700")  # -7.0 in its shortest form
FLOAT_ONE = bytes.fromhex("f93c00")  # 1.0 in its shortest form
TRUE = bytes.fromhex("f5")
FALSE = bytes.fromhex("f4")
NULL = bytes.fromhex("f6")
UNDEFINED = bytes.fromhex("f7")
SIMPLE_VALUE = bytes.fromhex("f0")  # simple value 16
UNKNOWN_TAG = bytes.fromhex("d9fde8") + cbor(1)  # tag 65000 around 1
BIGNUM = bytes.fromhex("c2") + cbor(b"\x01" + b"\x00" * 8)  # tag 2: 2**64


def non_canonical_der(signature: bytes) -> bytes:
    """A DER signature with one needless leading zero byte in `r`: the same integers, other bytes."""
    r_length = signature[3]
    r_value = signature[4 : 4 + r_length]
    rest = signature[4 + r_length :]
    r_part = b"\x02" + bytes([r_length + 1]) + b"\x00" + r_value
    body = r_part + rest
    return b"\x30" + bytes([len(body)]) + body


def corrupt(signature: bytes) -> bytes:
    """The DER signature with the last byte of `s` changed: valid DER, wrong signature."""
    return signature[:-1] + bytes([signature[-1] ^ 0x01])


# --- the authenticator -----------------------------------------------------------------------------


@dataclass
class Settings:
    """What one `create` or `get` call writes. The defaults are what a browser and a platform
    authenticator send for this document's options."""

    origin: str | None = None
    rp_id: str | None = None
    up: bool = True
    uv: bool = True
    be: bool = False
    bs: bool = False
    at: bool | None = None
    ed: bool = False
    credential_id_length: int | None = None
    counter: int | None = None
    client_type: str | None = None
    challenge: str | None = None
    cross_origin: object = False
    top_origin: object = ABSENT
    authenticator_data_suffix: bytes = b""
    authenticator_data_length: int | None = None
    credential_public_key: Callable[[bytes], bytes] | None = None
    extensions: bytes | None = None
    attestation_object: Callable[[bytes], bytes] | None = None
    client_data_json: Callable[[bytes], bytes] | None = None
    signature: Callable[[bytes], bytes] | None = None


@dataclass
class Credential:
    credential_id: bytes
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    public_key: bytes
    rp_id: str
    user_handle: bytes
    counter: int


@dataclass(frozen=True)
class Created:
    """The fields of `POST /auth/invite/finish` as raw bytes, and the created public key."""

    credential_id: bytes
    client_data_json: bytes
    attestation_object: bytes
    public_key: bytes

    def body(self, registration: str) -> dict[str, str]:
        return {
            "registration": registration,
            "credential_id": b64url_encode(self.credential_id),
            "client_data_json": b64url_encode(self.client_data_json),
            "attestation_object": b64url_encode(self.attestation_object),
        }


@dataclass(frozen=True)
class Asserted:
    """The fields of `POST /auth/passkey/finish` as raw bytes."""

    credential_id: bytes
    authenticator_data: bytes
    client_data_json: bytes
    signature: bytes
    user_handle: bytes

    def body(self, sign_in: str) -> dict[str, str]:
        return {
            "sign_in": sign_in,
            "credential_id": b64url_encode(self.credential_id),
            "authenticator_data": b64url_encode(self.authenticator_data),
            "client_data_json": b64url_encode(self.client_data_json),
            "signature": b64url_encode(self.signature),
            "user_handle": b64url_encode(self.user_handle),
        }


def client_data(
    *, type_: str, challenge: str, origin: str, cross_origin: object, top_origin: object
) -> bytes:
    """Client data as a browser serializes it: compact JSON, members in this order."""
    members: dict[str, object] = {"type": type_, "challenge": challenge, "origin": origin}
    if cross_origin is not ABSENT:
        members["crossOrigin"] = cross_origin
    if top_origin is not ABSENT:
        members["topOrigin"] = top_origin
    return json.dumps(members, separators=(",", ":")).encode("utf-8")


class SoftAuthenticator:
    """A passkey authenticator in software. `counter_step` 0 keeps the counter fixed, 1 increases
    it by one on every `get`; `Settings.counter` writes a given value instead."""

    def __init__(
        self,
        *,
        random: Random = system_random,
        signer: Signer | None = None,
        counter_start: int = 0,
        counter_step: int = 0,
    ) -> None:
        self.random = random
        self.signer = signer if signer is not None else Signer()
        self.counter_start = counter_start
        self.counter_step = counter_step
        self.credentials: list[Credential] = []

    def _flags(self, settings: Settings, *, at_default: bool) -> int:
        at = at_default if settings.at is None else settings.at
        flags = 0
        for wanted, bit in (
            (settings.up, FLAG_UP),
            (settings.uv, FLAG_UV),
            (settings.be, FLAG_BE),
            (settings.bs, FLAG_BS),
            (at, FLAG_AT),
            (settings.ed, FLAG_ED),
        ):
            if wanted:
                flags |= bit
        return flags

    def _authenticator_data(
        self, *, rp_id: str, flags: int, counter: int, attested: bytes, settings: Settings
    ) -> bytes:
        data = hashlib.sha256(rp_id.encode("utf-8")).digest() + bytes([flags]) + counter.to_bytes(4, "big")
        data += attested
        if flags & FLAG_ED:
            data += settings.extensions if settings.extensions is not None else cbor({})
        data += settings.authenticator_data_suffix
        if settings.authenticator_data_length is not None:
            data = data[: settings.authenticator_data_length]
        return data

    def create(self, options: dict[str, object], origin: str, settings: Settings | None = None) -> Created:
        """`navigator.credentials.create` with the options of section 5.5: `rp_id`, `user_id` and
        `challenge` as the hub hands them out (base64url)."""
        settings = settings if settings is not None else Settings()
        rp_id = str(options["rp_id"])
        private_key = private_key_from(self.random)
        public_key = point_of(private_key)
        length = settings.credential_id_length
        if length is None:
            length = 16 + self.random(1)[0] % 49
        credential_id = self.random(length) if length else b""
        user_handle = b64url_decode(options["user_id"])
        assert user_handle is not None
        counter = self.counter_start if settings.counter is None else settings.counter
        self.credentials.append(
            Credential(credential_id, private_key, public_key, rp_id, user_handle, counter)
        )
        key_bytes = (
            settings.credential_public_key(public_key)
            if settings.credential_public_key is not None
            else cbor_map(cose_key_pairs(public_key), width=None)
        )
        attested = self.random(16) + len(credential_id).to_bytes(2, "big") + credential_id + key_bytes
        flags = self._flags(settings, at_default=True)
        authenticator_data = self._authenticator_data(
            rp_id=settings.rp_id or rp_id,
            flags=flags,
            counter=counter,
            attested=attested if flags & FLAG_AT else b"",
            settings=settings,
        )
        attestation = (
            settings.attestation_object(authenticator_data)
            if settings.attestation_object is not None
            else cbor_map(attestation_pairs(authenticator_data), width=None)
        )
        data = client_data(
            type_=settings.client_type or "webauthn.create",
            challenge=settings.challenge if settings.challenge is not None else str(options["challenge"]),
            origin=settings.origin or origin,
            cross_origin=settings.cross_origin,
            top_origin=settings.top_origin,
        )
        if settings.client_data_json is not None:
            data = settings.client_data_json(data)
        return Created(credential_id, data, attestation, public_key)

    def credential_for(self, rp_id: str) -> Credential:
        """The newest credential of an rp id (a discoverable credential, `allowCredentials: []`)."""
        for credential in reversed(self.credentials):
            if credential.rp_id == rp_id:
                return credential
        raise LookupError("the authenticator holds no credential for this rp id")

    def get(self, options: dict[str, object], origin: str, settings: Settings | None = None) -> Asserted:
        """`navigator.credentials.get` with the options of section 6.3: `challenge` (base64url)
        and `rp_id`. Signs exactly `authenticator_data ‖ SHA-256(client_data_json)` (section 4.3)."""
        settings = settings if settings is not None else Settings()
        rp_id = str(options["rp_id"])
        credential = self.credential_for(rp_id)
        if settings.counter is not None:
            counter = settings.counter
        else:
            credential.counter += self.counter_step
            counter = credential.counter
        authenticator_data = self._authenticator_data(
            rp_id=settings.rp_id or rp_id,
            flags=self._flags(settings, at_default=False),
            counter=counter,
            attested=b"",
            settings=settings,
        )
        data = client_data(
            type_=settings.client_type or "webauthn.get",
            challenge=settings.challenge if settings.challenge is not None else str(options["challenge"]),
            origin=settings.origin or origin,
            cross_origin=settings.cross_origin,
            top_origin=settings.top_origin,
        )
        if settings.client_data_json is not None:
            data = settings.client_data_json(data)
        signature = self.signer.sign(
            credential.private_key, authenticator_data + hashlib.sha256(data).digest()
        )
        if settings.signature is not None:
            signature = settings.signature(signature)
        return Asserted(
            credential.credential_id, authenticator_data, data, signature, credential.user_handle
        )
