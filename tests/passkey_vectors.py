"""The generator of the shared test vectors `tests/vectors/passkey.json` (PASSKEY-PROTOCOL.md 13.10).

Regenerate the file from the repository root with `python -m tests.passkey_vectors`.

Every input comes from a fixed seed, so the generator is deterministic except for one thing:
ECDSA signing in `cryptography` is randomized. The file therefore lists every signature it uses
under `signatures`, keyed by public key and SHA-256 of the signed message, and the generator reuses
a listed signature whenever it still verifies over the same message. Generating again from the
file reproduces the file byte for byte; `tests/test_passkey_vectors.py` fails when it does not.

The expected result of every vector is written here from the specification, never computed by
the library under test; the texts, the fingerprints and the session key ids are computed here with
`hashlib` and plain string joins, independently of `agent_core.passkey`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from tests.webauthn_authenticator import (
    BIGNUM,
    FALSE,
    FLOAT_MINUS_7,
    FLOAT_ONE,
    NULL,
    P256_ORDER,
    SIMPLE_VALUE,
    TRUE,
    UNDEFINED,
    UNKNOWN_TAG,
    ABSENT,
    Settings,
    Signer,
    SoftAuthenticator,
    attestation_pairs,
    cbor,
    cbor_map,
    corrupt,
    cose_key_pairs,
    non_canonical_der,
    point_of,
    private_key_from,
    with_key,
    with_pair,
    with_value,
    without_key,
)

VECTORS_PATH = Path(__file__).resolve().parent / "vectors" / "passkey.json"

OK = "ok"
PUBLIC_URL = "https://hub.example"
ORIGIN = "https://hub.example"
RP_ID = "hub.example"
OTHER_ORIGIN = "https://other.example"
OTHER_RP_ID = "other.example"
NOW = int(datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc).timestamp())
SESSION_KEY_MAX_SECONDS = 43200
SESSION_KEY_ISSUE_SECONDS = 43080
SEQUENCE_MAX = 9007199254740991
DEVICE_KEY_ID = "example-staff-laptop-1"
PROGRAM_KEY_ID = "example-staff-laptop-1-cli"
OTHER_DEVICE_KEY_ID = "example-staff-desktop-1"
TIME_WINDOW = 120
STATEMENT_LABEL = "agent-session-v1"
COMMAND_LABEL = "agent-command-v1"
REQUEST_LABEL = "agent-request-v1"
PASSKEY_COUNTER = 7


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def non_canonical_b64(text: str) -> str:
    """The same bytes with unused low bits set in the last character: not canonical."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return text[:-1] + alphabet[alphabet.index(text[-1]) + 1]


def time_text(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def fingerprint_text(point: bytes) -> str:
    digits = sha256(point)[:16].hex()
    return " ".join(digits[index : index + 4] for index in range(0, 32, 4))


def session_id_of(statement: bytes) -> str:
    return "session." + sha256(statement)[:16].hex()


class DeterministicRandom:
    """Bytes from SHA-256 over a seed and a counter: the same seed gives the same bytes."""

    def __init__(self, seed: str) -> None:
        self.seed = seed.encode("utf-8")
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        out = b""
        while len(out) < size:
            out += sha256(self.seed + self.counter.to_bytes(8, "big"))
            self.counter += 1
        return out[:size]


def _verifies(public_key: ec.EllipticCurvePublicKey, signature: bytes, message: bytes) -> bool:
    try:
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


class CachingSigner(Signer):
    """Reuses a listed signature that verifies over the same message, else signs anew."""

    def __init__(self, previous: list[dict[str, str]]) -> None:
        self._cache = {(item["public_key"], item["message_sha256"]): item["signature"] for item in previous}
        self.used: dict[tuple[str, str], str] = {}

    def sign(self, private_key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
        """The same message signed twice in one run gets the same signature, so that every vector
        that holds it, and the listing, agree."""
        key = (b64(point_of(private_key)), hashlib.sha256(message).hexdigest())
        cached = self.used.get(key, self._cache.get(key))
        signature = unb64(cached) if cached is not None else b""
        if not _verifies(private_key.public_key(), signature, message):
            signature = super().sign(private_key, message)
        self.used[key] = b64(signature)
        return signature

    def listing(self) -> list[dict[str, str]]:
        return [
            {"public_key": public_key, "message_sha256": digest, "signature": signature}
            for (public_key, digest), signature in sorted(self.used.items())
        ]


def p1363(der: bytes) -> bytes:
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def high_s(der: bytes) -> bytes:
    """The same signature with `s` replaced by n - s when `s` is low: still valid, `s` high."""
    r, s = decode_dss_signature(der)
    return encode_dss_signature(r, s if s > P256_ORDER // 2 else P256_ORDER - s)


def statement_lines(session_public_key: bytes, hub_value: bytes, expires: int) -> list[str]:
    return [STATEMENT_LABEL, ORIGIN, b64(session_public_key), b64(hub_value), time_text(expires)]


def joined(lines: list[str]) -> bytes:
    return "\n".join(lines).encode("utf-8")


def command_lines(sender: str, command: dict[str, object]) -> list[str]:
    return [
        COMMAND_LABEL,
        sender,
        str(command["target"]),
        str(command["action"]),
        str(command["sequence"]),
        str(command["time"]),
        str(command["content_hash"]),
    ]


# --- registrations (section 5.5, R4 to R13) --------------------------------------------------------


@dataclass
class RegistrationCase:
    name: str
    result: str
    settings: Settings
    body_credential_id: Callable[[bytes], bytes] | None = None


def _attestation_with_statement(value: bytes) -> Callable[[bytes], bytes]:
    def build(authenticator_data: bytes) -> bytes:
        statement = cbor_map([(cbor("x"), value)], width=None)
        return cbor_map(with_value(attestation_pairs(authenticator_data), "attStmt", statement), width=None)

    return build


def _key(change: Callable[[list[tuple[bytes, bytes]]], list[tuple[bytes, bytes]]]) -> Callable[[bytes], bytes]:
    return lambda point: cbor_map(change(cose_key_pairs(point)), width=None)


def _off_curve(point: bytes) -> bytes:
    y = point[33:65]
    return cbor_map(with_value(cose_key_pairs(point), -3, cbor(y[:-1] + bytes([y[-1] ^ 0x01]))), width=None)


def _registration_cases(challenge_text: str) -> list[RegistrationCase]:
    boolean_extension = cbor_map([(cbor("credProps"), TRUE)], width=None)
    return [
        RegistrationCase("accepted", OK, Settings()),
        RegistrationCase("accepted synced passkey with BE and BS", OK, Settings(be=True, bs=True)),
        RegistrationCase("accepted credential id of 16 bytes", OK, Settings(credential_id_length=16)),
        RegistrationCase("accepted credential id of 1023 bytes", OK, Settings(credential_id_length=1023)),
        RegistrationCase(
            "accepted extensions map with a boolean value", OK, Settings(ed=True, extensions=boolean_extension)
        ),
        RegistrationCase(
            "accepted extensions map with false", OK,
            Settings(ed=True, extensions=cbor_map([(cbor("credProps"), FALSE)], width=None)),
        ),
        RegistrationCase("R4 type webauthn.get", "client_data_invalid", Settings(client_type="webauthn.get")),
        RegistrationCase(
            "R4 client data not JSON", "client_data_invalid", Settings(client_data_json=lambda data: b"{not json")
        ),
        RegistrationCase(
            "R4 client data not UTF-8", "client_data_invalid", Settings(client_data_json=lambda data: data + b"\xff")
        ),
        RegistrationCase(
            "R4 client data with a key written twice",
            "client_data_invalid",
            Settings(client_data_json=lambda data: data[:-1] + b',"type":"webauthn.create"}'),
        ),
        RegistrationCase(
            "R4 client data with NaN",
            "client_data_invalid",
            Settings(client_data_json=lambda data: data.replace(b'"crossOrigin":false', b'"crossOrigin":NaN')),
        ),
        RegistrationCase(
            "R4 client data with Infinity",
            "client_data_invalid",
            Settings(client_data_json=lambda data: data.replace(b'"crossOrigin":false', b'"crossOrigin":Infinity')),
        ),
        RegistrationCase(
            "R4 client data with -Infinity",
            "client_data_invalid",
            Settings(client_data_json=lambda data: data.replace(b'"crossOrigin":false', b'"crossOrigin":-Infinity')),
        ),
        RegistrationCase(
            "R4 client data not an object", "client_data_invalid", Settings(client_data_json=lambda data: b"[" + data + b"]")
        ),
        RegistrationCase(
            "R5 another challenge", "challenge_mismatch", Settings(challenge=b64(sha256(b"another challenge")))
        ),
        RegistrationCase(
            "R5 challenge not canonical base64url", "challenge_mismatch", Settings(challenge=non_canonical_b64(challenge_text))
        ),
        RegistrationCase("R6 another origin", "origin_mismatch", Settings(origin=OTHER_ORIGIN)),
        RegistrationCase("R6 crossOrigin true", "origin_mismatch", Settings(cross_origin=True)),
        RegistrationCase("R6 topOrigin present", "origin_mismatch", Settings(top_origin=OTHER_ORIGIN)),
        RegistrationCase(
            "R7 extra key",
            "attestation_invalid",
            Settings(attestation_object=lambda data: cbor({"fmt": "none", "attStmt": {}, "authData": data, "extra": 1})),
        ),
        RegistrationCase(
            "R7 missing authData",
            "attestation_invalid",
            Settings(attestation_object=lambda data: cbor_map(without_key(attestation_pairs(data), "authData"), width=None)),
        ),
        RegistrationCase(
            "R7 trailing bytes",
            "attestation_invalid",
            Settings(attestation_object=lambda data: cbor_map(attestation_pairs(data), width=None) + b"\x00"),
        ),
        RegistrationCase(
            "R7 key written twice",
            "attestation_invalid",
            Settings(
                attestation_object=lambda data: cbor_map(
                    with_pair(attestation_pairs(data), cbor("fmt"), cbor("none")), width=None
                )
            ),
        ),
        RegistrationCase(
            "R7 non-canonical map length",
            "attestation_invalid",
            Settings(attestation_object=lambda data: cbor_map(attestation_pairs(data), width=1)),
        ),
        RegistrationCase(
            "R7 unknown tag in attStmt", "attestation_invalid", Settings(attestation_object=_attestation_with_statement(UNKNOWN_TAG))
        ),
        RegistrationCase(
            "R7 float in attStmt", "attestation_invalid", Settings(attestation_object=_attestation_with_statement(FLOAT_ONE))
        ),
        RegistrationCase(
            "R7 simple value in attStmt",
            "attestation_invalid",
            Settings(attestation_object=_attestation_with_statement(SIMPLE_VALUE)),
        ),
        RegistrationCase(
            "R7 true in attStmt", "attestation_invalid", Settings(attestation_object=_attestation_with_statement(TRUE))
        ),
        RegistrationCase(
            "R7 null in attStmt", "attestation_invalid", Settings(attestation_object=_attestation_with_statement(NULL))
        ),
        RegistrationCase(
            "R7 undefined in attStmt", "attestation_invalid", Settings(attestation_object=_attestation_with_statement(UNDEFINED))
        ),
        RegistrationCase(
            "R7 authData not bytes",
            "attestation_invalid",
            Settings(attestation_object=lambda data: cbor({"fmt": "none", "attStmt": {}, "authData": "text"})),
        ),
        RegistrationCase(
            "R8 authenticator data of 36 bytes", "authenticator_data_invalid", Settings(authenticator_data_length=36)
        ),
        RegistrationCase("R9 another rp id", "rp_id_mismatch", Settings(rp_id=OTHER_RP_ID)),
        RegistrationCase("R10 UP not set", "user_presence_missing", Settings(up=False)),
        RegistrationCase("R11 UV not set", "user_verification_missing", Settings(uv=False)),
        RegistrationCase("R12 BS without BE", "authenticator_data_invalid", Settings(bs=True)),
        RegistrationCase("R12 AT not set", "authenticator_data_invalid", Settings(at=False)),
        RegistrationCase(
            "R12 trailing bytes without ED", "authenticator_data_invalid", Settings(authenticator_data_suffix=b"\x00")
        ),
        RegistrationCase(
            "R12 data ends inside the credential id", "authenticator_data_invalid", Settings(authenticator_data_length=70)
        ),
        RegistrationCase(
            "R12 non-canonical credential public key",
            "authenticator_data_invalid",
            Settings(credential_public_key=lambda point: cbor_map(cose_key_pairs(point), width=1)),
        ),
        RegistrationCase(
            "R12 credential public key with a key written twice",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_pair(pairs, cbor(1), cbor(2)))),
        ),
        RegistrationCase(
            "R12 credential public key 3: -7.0",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, 3, FLOAT_MINUS_7))),
        ),
        RegistrationCase(
            "R12 credential public key -1: true",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, -1, TRUE))),
        ),
        RegistrationCase(
            "R12 credential public key with the key 1.0",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_key(pairs, 1, FLOAT_ONE))),
        ),
        RegistrationCase(
            "R12 credential public key with a tag",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, 3, UNKNOWN_TAG))),
        ),
        RegistrationCase(
            "R12 credential public key with a bignum",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, 3, BIGNUM))),
        ),
        RegistrationCase(
            "R12 credential public key with null",
            "authenticator_data_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, -1, NULL))),
        ),
        RegistrationCase(
            "R12 extensions map with a float value",
            "authenticator_data_invalid",
            Settings(ed=True, extensions=cbor_map([(cbor("credProps"), FLOAT_ONE)], width=None)),
        ),
        RegistrationCase(
            "R12 extensions map with a boolean key",
            "authenticator_data_invalid",
            Settings(ed=True, extensions=cbor_map([(TRUE, cbor(1))], width=None)),
        ),
        RegistrationCase(
            "R12 extensions item not a map", "authenticator_data_invalid", Settings(ed=True, extensions=cbor([1]))
        ),
        RegistrationCase(
            "R12 ED set without an extensions map", "authenticator_data_invalid", Settings(ed=True, extensions=b"")
        ),
        RegistrationCase(
            "R13 credential id differs from the body",
            "credential_invalid",
            Settings(),
            body_credential_id=lambda raw: bytes([raw[0] ^ 0x01]) + raw[1:],
        ),
        RegistrationCase("R13 credential id of 15 bytes", "credential_invalid", Settings(credential_id_length=15)),
        RegistrationCase("R13 credential id of 1024 bytes", "credential_invalid", Settings(credential_id_length=1024)),
        RegistrationCase(
            "R13 RS256 credential key",
            "credential_invalid",
            Settings(credential_public_key=lambda point: cbor({1: 3, 3: -257, -1: b"\x01" * 256, -2: b"\x01\x00\x01"})),
        ),
        RegistrationCase(
            "R13 Ed25519 credential key",
            "credential_invalid",
            Settings(credential_public_key=lambda point: cbor({1: 1, 3: -8, -1: 6, -2: point[1:33]})),
        ),
        RegistrationCase("R13 point not on the curve", "credential_invalid", Settings(credential_public_key=_off_curve)),
        RegistrationCase(
            "R13 credential public key not a map", "credential_invalid", Settings(credential_public_key=lambda point: cbor([1]))
        ),
        RegistrationCase(
            "R13 coordinate of 31 bytes",
            "credential_invalid",
            Settings(credential_public_key=_key(lambda pairs: with_value(pairs, -2, cbor(bytes(31))))),
        ),
    ]


def _registrations() -> list[dict[str, object]]:
    challenge = DeterministicRandom("registration challenge")(32)
    challenge_text = b64(challenge)
    options: dict[str, object] = {"rp_id": RP_ID, "user_id": b64(bytes(range(16))), "challenge": challenge_text}
    vectors: list[dict[str, object]] = []
    for case in _registration_cases(challenge_text):
        authenticator = SoftAuthenticator(random=DeterministicRandom("registration " + case.name))
        created = authenticator.create(options, ORIGIN, case.settings)
        body_id = created.credential_id
        if case.body_credential_id is not None:
            body_id = case.body_credential_id(body_id)
        vector: dict[str, object] = {
            "name": case.name,
            "credential_id": b64(body_id),
            "client_data_json": b64(created.client_data_json),
            "attestation_object": b64(created.attestation_object),
            "challenge": challenge_text,
            "origin": ORIGIN,
            "rp_id": RP_ID,
            "result": case.result,
        }
        if case.result == OK:
            vector["public_key"] = b64(created.public_key)
            vector["sign_count"] = 0
        vectors.append(vector)
    return vectors


# --- certificates (section 6.4, V1 to V11) ----------------------------------------------------------


@dataclass
class CertificateCase:
    name: str
    result: str
    statement: bytes
    settings: Settings = field(default_factory=Settings)
    now: int = NOW
    max_lifetime: int = SESSION_KEY_MAX_SECONDS
    signed_statement: bytes | None = None
    public_key: bytes | None = None


def _certificate_cases(
    lines: list[str], other_statement: bytes, other_public_key: bytes
) -> list[CertificateCase]:
    statement = joined(lines)
    expires = NOW + SESSION_KEY_ISSUE_SECONDS

    def changed(index: int, value: str) -> bytes:
        copy = list(lines)
        copy[index] = value
        return joined(copy)

    return [
        CertificateCase("accepted", OK, statement),
        CertificateCase("accepted synced passkey with BE and BS", OK, statement, Settings(be=True, bs=True)),
        CertificateCase(
            "accepted bytes after the first 37 with ED", OK, statement, Settings(ed=True, extensions=cbor({"x": 1}))
        ),
        CertificateCase("accepted high s", OK, statement, Settings(signature=high_s)),
        CertificateCase(
            "accepted expiry exactly now + 43200", OK, statement, now=expires - 43200, max_lifetime=43200
        ),
        CertificateCase("accepted expiry exactly now + 3600", OK, statement, now=expires - 3600, max_lifetime=3600),
        CertificateCase("accepted expiry one second after now", OK, statement, now=expires - 1),
        CertificateCase("V1 four lines", "statement_invalid", joined(lines[:4])),
        CertificateCase("V1 six lines", "statement_invalid", joined([*lines, "extra"])),
        CertificateCase("V1 trailing line feed", "statement_invalid", statement + b"\n"),
        CertificateCase("V1 another label", "statement_invalid", changed(0, "agent-session-v2")),
        CertificateCase("V1 another origin", "statement_invalid", changed(1, OTHER_ORIGIN)),
        CertificateCase("V1 key not a point", "statement_invalid", changed(2, b64(b"\x04" + bytes(64)))),
        CertificateCase("V1 key not canonical base64url", "statement_invalid", changed(2, non_canonical_b64(lines[2]))),
        CertificateCase("V1 hub value of 31 bytes", "statement_invalid", changed(3, b64(unb64(lines[3])[:31]))),
        CertificateCase("V1 expiry not in the time form", "statement_invalid", changed(4, lines[4].replace("T", " "))),
        CertificateCase("V1 not UTF-8", "statement_invalid", statement[:-1] + b"\xff"),
        CertificateCase("V2 type webauthn.create", "client_data_invalid", statement, Settings(client_type="webauthn.create")),
        CertificateCase(
            "V2 client data with a key written twice",
            "client_data_invalid",
            statement,
            Settings(client_data_json=lambda data: data[:-1] + b',"type":"webauthn.get"}'),
        ),
        CertificateCase(
            "V3 valid assertion for another statement", "challenge_mismatch", statement, signed_statement=other_statement
        ),
        CertificateCase("V4 another origin", "origin_mismatch", statement, Settings(origin=OTHER_ORIGIN)),
        CertificateCase("V4 crossOrigin true", "origin_mismatch", statement, Settings(cross_origin=True)),
        CertificateCase("V4 topOrigin present", "origin_mismatch", statement, Settings(top_origin=OTHER_ORIGIN)),
        CertificateCase(
            "V4 crossOrigin absent is accepted", OK, statement, Settings(cross_origin=ABSENT)
        ),
        CertificateCase(
            "V5 authenticator data of 36 bytes", "authenticator_data_invalid", statement, Settings(authenticator_data_length=36)
        ),
        CertificateCase("V6 another rp id", "rp_id_mismatch", statement, Settings(rp_id=OTHER_RP_ID)),
        CertificateCase("V7 UP not set", "user_presence_missing", statement, Settings(up=False)),
        CertificateCase("V8 UV not set", "user_verification_missing", statement, Settings(uv=False)),
        CertificateCase("V9 BS without BE", "authenticator_data_invalid", statement, Settings(bs=True)),
        CertificateCase("V9 AT set", "authenticator_data_invalid", statement, Settings(at=True)),
        CertificateCase(
            "V9 trailing bytes without ED",
            "authenticator_data_invalid",
            statement,
            Settings(authenticator_data_suffix=b"\x00"),
        ),
        CertificateCase("V10 corrupted signature", "signature_invalid", statement, Settings(signature=corrupt)),
        CertificateCase(
            "V10 signature not canonical DER", "signature_invalid", statement, Settings(signature=non_canonical_der)
        ),
        CertificateCase(
            "V10 signature with a trailing byte",
            "signature_invalid",
            statement,
            Settings(signature=lambda signature: signature + b"\x00"),
        ),
        CertificateCase(
            "V10 signature in r || s form",
            "signature_invalid",
            statement,
            Settings(signature=p1363),
        ),
        CertificateCase("V10 another public key", "signature_invalid", statement, public_key=other_public_key),
        CertificateCase(
            "V11 expiry beyond now + 43200",
            "certificate_expiry_invalid",
            statement,
            now=expires - 43201,
            max_lifetime=43200,
        ),
        CertificateCase(
            "V11 expiry beyond now + 3600", "certificate_expiry_invalid", statement, now=expires - 3601, max_lifetime=3600
        ),
        CertificateCase("V11 expiry at now, 43200", "certificate_expiry_invalid", statement, now=expires, max_lifetime=43200),
        CertificateCase("V11 expiry at now, 3600", "certificate_expiry_invalid", statement, now=expires, max_lifetime=3600),
        CertificateCase("V11 expiry before now", "certificate_expiry_invalid", statement, now=expires + 1),
    ]


def _certificate_json(statement: bytes, asserted: object) -> dict[str, str]:
    return {
        "statement": b64(statement),
        "credential_id": b64(asserted.credential_id),  # type: ignore[attr-defined]
        "authenticator_data": b64(asserted.authenticator_data),  # type: ignore[attr-defined]
        "client_data_json": b64(asserted.client_data_json),  # type: ignore[attr-defined]
        "signature": b64(asserted.signature),  # type: ignore[attr-defined]
    }


def _assert_for(passkey: SoftAuthenticator, statement: bytes, settings: Settings) -> object:
    return passkey.get({"challenge": b64(sha256(statement)), "rp_id": RP_ID}, ORIGIN, settings)


# --- the generator -------------------------------------------------------------------------------------


def generate(previous: dict[str, object] | None) -> dict[str, object]:
    """Every vector of section 13.10; `previous` is the current file, whose signatures are reused."""
    listed = previous["signatures"] if previous is not None else []
    signer = CachingSigner(listed)  # type: ignore[arg-type]
    passkey = SoftAuthenticator(random=DeterministicRandom("passkey"), signer=signer, counter_start=PASSKEY_COUNTER)
    passkey.create({"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": b64(bytes(32))}, ORIGIN)
    passkey_public = passkey.credentials[0].public_key
    other_public = point_of(private_key_from(DeterministicRandom("another passkey")))
    session_private = private_key_from(DeterministicRandom("session key"))
    session_public = point_of(session_private)
    hub_value = DeterministicRandom("hub value")(32)
    expires = NOW + SESSION_KEY_ISSUE_SECONDS
    lines = statement_lines(session_public, hub_value, expires)
    statement = joined(lines)
    session_id = session_id_of(statement)
    other_statement = joined(statement_lines(session_public, DeterministicRandom("another hub value")(32), expires))

    certificates: list[dict[str, object]] = []
    accepted_certificate: dict[str, str] = {}
    for case in _certificate_cases(lines, other_statement, other_public):
        signed = case.signed_statement if case.signed_statement is not None else case.statement
        asserted = _assert_for(passkey, signed, case.settings)
        certificate = _certificate_json(case.statement, asserted)
        vector: dict[str, object] = {
            "name": case.name,
            "certificate": certificate,
            "passkey_public_key": b64(case.public_key if case.public_key is not None else passkey_public),
            "origin": ORIGIN,
            "rp_id": RP_ID,
            "now": time_text(case.now),
            "max_lifetime": case.max_lifetime,
            "result": case.result,
        }
        if case.result == OK:
            vector["session_key_id"] = session_id_of(case.statement)
            vector["session_public_key"] = b64(session_public)
            vector["expires"] = time_text(expires)
            vector["sign_count"] = PASSKEY_COUNTER
        if case.name == "accepted":
            accepted_certificate = certificate
        certificates.append(vector)

    request = {
        "key_id": session_id,
        "method": "POST",
        "host": RP_ID,
        "target": "/api/key/commands",
        "time": time_text(NOW),
        "nonce": b64(DeterministicRandom("nonce")(16)),
        "body_hash": hashlib.sha256(b'{"example":true}').hexdigest(),
    }
    request_text = joined(
        [REQUEST_LABEL, *(str(request[name]) for name in ("key_id", "method", "host", "target", "time", "nonce", "body_hash"))]
    )
    request["text"] = request_text.decode("utf-8")

    vectors: dict[str, object] = {
        "description": (
            "Shared test vectors of PASSKEY-PROTOCOL.md section 13.10. Binary values are base64url "
            "without padding; times are YYYY-MM-DDTHH:MM:SSZ; texts are UTF-8 strings. A result is "
            "'ok' or the error value of the specification; a bundle result is 'ok', "
            "'check_2:<error of verify_certificate>' or 'check_<n>' for device checks 3 to 6 of "
            "section 9. 'signatures' lists the ECDSA signatures the generator reuses, because ECDSA "
            "signing is randomized; it is not a vector."
        ),
        "relying_party": {"public_url": PUBLIC_URL, "origin": ORIGIN, "rp_id": RP_ID},
        "fingerprints": [
            {"public_key": b64(point), "fingerprint": fingerprint_text(point)}
            for point in (passkey_public, session_public, other_public)
        ],
        "statements": [
            {
                "origin": ORIGIN,
                "session_public_key": b64(session_public),
                "hub_value": b64(hub_value),
                "expires": time_text(expires),
                "statement": statement.decode("utf-8"),
                "challenge": b64(sha256(statement)),
                "session_key_id": session_id,
            }
        ],
        "request_texts": [request],
        "command_texts": _command_texts(session_id),
        "session_signatures": _session_signatures(signer, session_private, request_text),
        "counters": _counters(),
        "registrations": _registrations(),
        "certificates": certificates,
        "bundles": _bundles(signer, session_private, session_id, accepted_certificate, passkey, lines),
    }
    vectors["signatures"] = signer.listing()
    return vectors


def _command_texts(session_id: str) -> list[dict[str, object]]:
    base: dict[str, object] = {
        "sender": session_id,
        "target": DEVICE_KEY_ID,
        "action": "noop",
        "sequence": 1,
        "time": time_text(NOW),
        "content_hash": "-",
    }
    cases: list[tuple[dict[str, object], str]] = [
        ({}, OK),
        ({"sequence": SEQUENCE_MAX}, OK),
        ({"sequence": 0}, "command_invalid"),
        ({"sequence": SEQUENCE_MAX + 1}, "command_invalid"),
        ({"action": "grant"}, "command_invalid"),
        ({"action": "allowlist.add"}, "command_invalid"),
        ({"content_hash": "a" * 64}, "command_invalid"),
        ({"target": "Example-Laptop"}, "command_invalid"),
        ({"sender": "session.ABC"}, "command_invalid"),
        ({"time": "2026-11-17 10:00:00Z"}, "command_invalid"),
    ]
    vectors: list[dict[str, object]] = []
    for change, result in cases:
        fields = {**base, **change}
        sender = str(fields.pop("sender"))
        vector: dict[str, object] = {"sender": sender, **fields, "result": result}
        vector["text"] = joined(command_lines(sender, fields)).decode("utf-8") if result == OK else None
        vectors.append(vector)
    return vectors


def _session_signatures(
    signer: CachingSigner, session_private: ec.EllipticCurvePrivateKey, text: bytes
) -> list[dict[str, object]]:
    der = signer.sign(session_private, text)
    r, s = decode_dss_signature(der)
    low = min(s, P256_ORDER - s)
    n = P256_ORDER.to_bytes(32, "big")
    r_bytes = r.to_bytes(32, "big")
    cases: list[tuple[str, bytes, bytes, str]] = [
        ("accepted low s", text, r_bytes + low.to_bytes(32, "big"), OK),
        ("accepted high s", text, r_bytes + (P256_ORDER - low).to_bytes(32, "big"), OK),
        ("r of 0", text, bytes(32) + low.to_bytes(32, "big"), "signature_invalid"),
        ("s of 0", text, r_bytes + bytes(32), "signature_invalid"),
        ("r of n", text, n + low.to_bytes(32, "big"), "signature_invalid"),
        ("s of n", text, r_bytes + n, "signature_invalid"),
        ("another text", text + b"x", r_bytes + low.to_bytes(32, "big"), "signature_invalid"),
        ("DER instead of r || s", text, der, "signature_invalid"),
    ]
    public = b64(point_of(session_private))
    return [
        {"name": name, "public_key": public, "text": signed.decode("utf-8"), "signature": b64(signature), "result": result}
        for name, signed, signature, result in cases
    ]


def _counters() -> list[dict[str, object]]:
    cases: list[tuple[int | None, int, str]] = [
        (None, 0, OK),
        (None, 5, OK),
        (0, 0, OK),
        (0, 1, OK),
        (5, 6, OK),
        (6, 6, "counter_regressed"),
        (6, 3, "counter_regressed"),
        (6, 0, "counter_regressed"),
        (0, 0xFFFFFFFF, OK),
    ]
    return [{"stored": stored, "received": received, "result": result} for stored, received, result in cases]


def _bundles(
    signer: CachingSigner,
    session_private: ec.EllipticCurvePrivateKey,
    session_id: str,
    certificate: dict[str, str],
    passkey: SoftAuthenticator,
    lines: list[str],
) -> list[dict[str, object]]:
    command: dict[str, object] = {
        "target": DEVICE_KEY_ID,
        "action": "noop",
        "sequence": 1,
        "time": time_text(NOW),
        "content_hash": "-",
    }
    statement = joined(lines)
    broken = _certificate_json(statement, _assert_for(passkey, statement, Settings(signature=corrupt)))

    def bundle(
        *,
        sender: str = session_id,
        cert: dict[str, str] = certificate,
        fields: dict[str, object] = command,
        signed_fields: dict[str, object] | None = None,
        content: object = None,
    ) -> dict[str, object]:
        text = joined(command_lines(sender, signed_fields if signed_fields is not None else fields))
        return {
            "sender": sender,
            "certificate": cert,
            "command": fields,
            "signature": b64(p1363(signer.sign(session_private, text))),
            "content": content,
            "content_random": None,
        }

    cases: list[tuple[str, dict[str, object], str]] = [
        ("accepted", bundle(), OK),
        ("check 2 certificate signature invalid", bundle(cert=broken), "check_2:signature_invalid"),
        ("check 3 another sender", bundle(sender="session." + "0" * 32), "check_3"),
        (
            "check 4 signature over another target",
            bundle(signed_fields={**command, "target": OTHER_DEVICE_KEY_ID}),
            "check_4",
        ),
        ("check 5 content for -", bundle(content="x"), "check_5"),
        ("check 6 program key as target", bundle(fields={**command, "target": PROGRAM_KEY_ID}), "check_6"),
        ("check 6 time outside the window", bundle(fields={**command, "time": time_text(NOW - 121)}), "check_6"),
        ("check 6 sequence not higher", bundle(), "check_6"),
    ]
    return [
        {
            "name": name,
            "bundle": value,
            "origin": ORIGIN,
            "rp_id": RP_ID,
            "now": time_text(NOW),
            "max_lifetime": SESSION_KEY_MAX_SECONDS,
            "device_key_id": DEVICE_KEY_ID,
            "time_window": TIME_WINDOW,
            "last_sequence": 1 if name == "check 6 sequence not higher" else None,
            "passkey_public_key": b64(passkey.credentials[0].public_key),
            "result": result,
        }
        for name, value, result in cases
    ]


def render(vectors: dict[str, object]) -> str:
    return json.dumps(vectors, indent=1, ensure_ascii=True) + "\n"


def load() -> dict[str, object]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def main() -> None:
    previous = load() if VECTORS_PATH.exists() else None
    VECTORS_PATH.parent.mkdir(exist_ok=True)
    VECTORS_PATH.write_text(render(generate(previous)), encoding="utf-8")


if __name__ == "__main__":
    main()
