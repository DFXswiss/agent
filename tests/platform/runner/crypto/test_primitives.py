"""Copied from tests/platform/hub/crypto/test_primitives.py.

U-HUB-crypto: errors, encodings, UUIDv7, HKDF labels, AES-GCM, HPKE and raw key formats (04-crypto.md 1-3)."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hpke as pyca_hpke
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_runner.crypto import aead, encoding, errors, hpke, ids, invite, kdf, keys
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError

SUITE = pyca_hpke.Suite(pyca_hpke.KEM.X25519, pyca_hpke.KDF.HKDF_SHA256, pyca_hpke.AEAD.AES_256_GCM)


def refused(code: str) -> pytest.RaisesExc[CryptoError]:
    """`pytest.raises` for exactly this error code."""
    return pytest.raises(CryptoError, match=f"^{code}$")


# --- errors ---


SPEC = Path(__file__).resolve().parents[4] / "docs" / "platform" / "spec" / "04-crypto.md"


def _section_8() -> tuple[set[str], set[str]]:
    """The code lists `Crypto/format:` and `Log:` of 04-crypto.md section 8."""
    section = SPEC.read_text(encoding="utf-8").split("## 8. Error codes")[1].split("\n## ")[0]
    crypto, log = section.split("Crypto/format:")[1].split("Log:")
    return set(re.findall(r"`([a-z_]+)`", crypto)), set(re.findall(r"`([a-z_]+)`", log))


def test_error_codes_are_the_lists_of_section_8() -> None:
    crypto, log = _section_8()
    assert crypto == errors.CRYPTO_CODES
    assert log == errors.LOG_CODES
    assert {"account_code_invalid", "log_admit"} <= errors.CODES
    assert errors.CODES == errors.CRYPTO_CODES | errors.LOG_CODES
    error = CryptoError("decrypt_failed")
    assert error.code == "decrypt_failed"
    assert str(error) == "decrypt_failed"


def test_unknown_error_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown crypto error code"):
        CryptoError("something")


def _refusals() -> list[Callable[[], object]]:
    sk = X25519PrivateKey.generate()
    ident = keys.Identity.generate()
    point = keys.p256_public_raw(keys.p256_private(bytes(31) + b"\x07"))
    secrets = invite.invite_secrets(bytes(16))
    sealed = invite.seal_package(
        secrets,
        {
            "org": ids.uuid7(),
            "org_name": "o",
            "wg_names": {},
            "founder": b64u_encode(bytes(32)),
            "head": {"seq": 0, "hash": b64u_encode(bytes(32))},
            "keys": [{"scope": "org", "scope_id": ids.uuid7(), "epoch": 1, "key": b64u_encode(bytes(32))}],
            "hub_key": {"id": "hub", "public_key": b64u_encode(bytes(32))},
            "v": 1,
        },
    )
    return [
        lambda: encoding.parse_json(b'{"secret":"hunter2",}'),
        lambda: encoding.body_json({"a": float("inf")}),
        lambda: encoding.parse_time("2026-02-30T12:00:00.000Z"),
        lambda: aead.open_(bytes(32), bytes(12), bytes(20), b""),
        lambda: hpke.open_(sk, b"", bytes(48)),
        lambda: keys.ed25519_verify(ident.sig_public(), bytes(64), b"m"),
        lambda: keys.ed25519_verify(bytes(32), bytes(64), b"m"),
        lambda: keys.ed25519_verify_b64u(ident.sig_public(), "Zh", b"m"),
        lambda: keys.p256_verify_raw(point, bytes(31) + b"\x01" + bytes(31) + b"\x01", b"m"),
        lambda: keys.p256_verify_raw(b"\x04" + bytes(64), bytes(31) + b"\x01" + bytes(31) + b"\x01", b"m"),
        lambda: invite.open_package(invite.invite_secrets(b"\x01" * 16), sealed),
    ]


def test_refusals_carry_no_exception_context() -> None:
    """A handled exception (e.g. a JSON decode error holding the plaintext) never becomes `__context__`."""
    for call in _refusals():
        with pytest.raises(CryptoError) as refused_info:
            call()
        assert refused_info.value.__context__ is None
        assert refused_info.value.__cause__ is None


# --- encoding ---


@pytest.mark.parametrize(
    "raw", [b"", b"f", b"fo", b"foo", b"\xfb\xff\xfe", pytest.param(os.urandom(33), id="random33")]
)
def test_b64u_round_trip(raw: bytes) -> None:
    text = b64u_encode(raw)
    assert "=" not in text
    assert b64u_decode(text) == raw
    assert b64u_decode(text, len(raw)) == raw


@pytest.mark.parametrize("text", ["Zg==", "Zh", "Zm9vY", "+/", "Zm9v ", "Zm9v\n", "é", 5, None, b"Zm9v"])
def test_b64u_refuses_non_canonical_input(text: object) -> None:
    with refused("header_invalid"):
        b64u_decode(text)


def test_b64u_length() -> None:
    with refused("header_invalid"):
        b64u_decode("Zm9v", 4)


def test_canonical_sorts_and_compacts() -> None:
    value = {"b": [1, {"y": None, "x": True}], "a": "", "B": False}
    assert encoding.canonical(value) == b'{"B":false,"a":"","b":[1,{"x":true,"y":null}]}'
    assert encoding.canonical(("t",)) == b'["t"]'
    assert encoding.canonical("\\u0041") == b'"\\\\u0041"'
    assert encoding.canonical(2**53 - 1) == b"9007199254740991"
    assert encoding.canonical(-(2**53 - 1)) == b"-9007199254740991"


@pytest.mark.parametrize(
    "value",
    [{"a": "é"}, {"é": 1}, ["\x00"], "\x7f", {"a": {"b": ["\t"]}}, {"a": 1.5, "b": "é"}, [{"x": 2**60}, ("é",)]],
)
def test_canonical_refuses_non_ascii(value: object) -> None:
    with refused("non_ascii"):
        encoding.canonical(value)


@pytest.mark.parametrize(
    "value", [{"a": 1.0}, 2**53, -(2**53), {1: "a"}, b"x", {"a": {1, 2}}, float("nan"), MappingProxyType({"a": 1})]
)
def test_canonical_refuses_other_types(value: object) -> None:
    with refused("header_invalid"):
        encoding.canonical(value)


def test_body_json_keeps_non_ascii() -> None:
    assert encoding.body_json({"b": "Grüsse", "a": 1.5}) == '{"a":1.5,"b":"Grüsse"}'.encode()
    assert encoding.body_json("\x01") == b'"\\u0001"'


@pytest.mark.parametrize("value", [float("inf"), {"a": "\ud800"}, {"a": object()}])
def test_body_json_refuses_non_json(value: object) -> None:
    with refused("header_invalid"):
        encoding.body_json(value)


def test_parse_json_is_strict() -> None:
    assert encoding.parse_json('{"a":[1,"ü"]}'.encode()) == {"a": [1, "ü"]}
    for raw in (b'{"a":1,"a":2}', b"NaN", b'{"a":Infinity}', b"\xff", b"{", b'{"a":-Infinity}'):
        with refused("header_invalid"):
            encoding.parse_json(raw)


def test_times() -> None:
    moment = datetime(2026, 11, 21, 13, 0, 0, 123999, tzinfo=timezone(timedelta(hours=1)))
    text = encoding.format_time(moment)
    assert text == "2026-11-21T12:00:00.123Z"
    assert encoding.parse_time(text) == datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)
    for bad in (
        "2026-11-21T12:00:00Z",
        "2026-13-01T00:00:00.000Z",
        "2026-11-21 12:00:00.000Z",
        7,
        "\uff12026-11-21T12:00:00.000Z",
    ):
        with refused("header_invalid"):
            encoding.parse_time(bad)


def test_require_helpers() -> None:
    assert encoding.require_bytes(b"ab", 2) == b"ab"
    with refused("prf_length"):
        encoding.require_bytes(b"a", 2, "prf_length")
    with refused("header_invalid"):
        encoding.require_bytes("ab", 2)
    assert encoding.require_keys({"a": 1}, frozenset({"a"})) == {"a": 1}
    maps: list[object] = [{"a": 1, "b": 2}, {}, ["a"]]
    for item in maps:
        with refused("header_invalid"):
            encoding.require_keys(item, frozenset({"a"}))
    assert encoding.require_int(3, 1) == 3
    for value in (True, 0, 2**53, "3", 1.0):
        with refused("header_invalid"):
            encoding.require_int(value, 1)
    with refused("header_invalid"):
        encoding.require_int(5, 1, 4)


# --- ids ---


def test_uuid7_layout() -> None:
    value = ids.uuid7(0x0123456789AB, b"\xff" * 10)
    assert value == "01234567-89ab-7fff-bfff-ffffffffffff"
    assert ids.is_uuid7(value)
    assert ids.uuid7_ms(value) == 0x0123456789AB
    assert ids.uuid7(0, bytes(10)) == "00000000-0000-7000-8000-000000000000"


def test_uuid7_random_and_now() -> None:
    first, second = ids.uuid7(), ids.uuid7()
    assert first != second
    assert ids.is_uuid7(first)
    assert abs(ids.uuid7_ms(first) - datetime.now(UTC).timestamp() * 1000) < 60_000


@pytest.mark.parametrize(
    "value",
    [
        "01234567-89AB-7fff-bfff-ffffffffffff",
        "01234567-89ab-4fff-bfff-ffffffffffff",
        "01234567-89ab-7fff-7fff-ffffffffffff",
        "0123456789ab7fffbfffffffffffffff",
        None,
    ],
)
def test_is_uuid7_refuses(value: object) -> None:
    assert not ids.is_uuid7(value)


def test_uuid7_refuses_bad_parameters() -> None:
    for ms, rand in ((-1, bytes(10)), (2**48, bytes(10)), (0, bytes(9))):
        with refused("header_invalid"):
            ids.uuid7(ms, rand)
    with refused("header_invalid"):
        ids.uuid7_ms("not-a-uuid")


# --- kdf ---


def _hkdf(ikm: bytes, info: bytes, salt: bytes | None = None, length: int = 32) -> bytes:
    return HKDF(hashes.SHA256(), length, salt, info).derive(ikm)


def test_labels_and_derivations_match_section_3() -> None:
    p = b"agent.dfx.swiss/v1/"
    assert p == encoding.P
    code, prf, cred = os.urandom(16), os.urandom(32), os.urandom(20)
    key, salt, seed = os.urandom(32), os.urandom(32), os.urandom(32)
    assert kdf.prf_input() == hashlib.sha256(b"agent.dfx.swiss/prf/v1").digest()
    assert kdf.kek_from_prf(prf, cred) == _hkdf(prf, p + b"kek\x00" + cred)
    assert kdf.kek_from_recovery_code(code) == _hkdf(code, p + b"recovery-kek")
    assert kdf.recovery_id_raw(code) == _hkdf(code, p + b"recovery-id")[:16]
    assert kdf.invite_id_raw(code) == _hkdf(code, p + b"invite-id")[:16]
    assert kdf.package_key(code) == _hkdf(code, p + b"invite-package")
    assert kdf.invite_sign_seed(code) == _hkdf(code, p + b"invite-sign")
    assert kdf.event_key(key, salt) == _hkdf(key, p + b"event", salt)
    assert kdf.dedupe_key(key) == _hkdf(key, p + b"dedupe")
    assert kdf.credential_key(key, salt) == _hkdf(key, p + b"credential", salt)
    assert kdf.storage_key(seed) == _hkdf(seed, p + b"runner-storage")
    assert kdf.hkdf(key, b"x") == _hkdf(key, b"x", bytes(32))
    labels = [
        kdf.LABEL_BUNDLE,
        kdf.LABEL_WRAP,
        kdf.LABEL_EVENT_SIG,
        kdf.LABEL_LOG,
        kdf.LABEL_PLATFORM_LOG,
        kdf.LABEL_INVITE_PACKAGE,
        kdf.LABEL_CREDENTIAL_SIG,
        kdf.LABEL_REQUEST_WRAP,
        kdf.LABEL_REQUEST_SIG,
        kdf.LABEL_DEDUPE,
        kdf.LABEL_RECOVERY_PROOF,
        kdf.LABEL_ENROLL,
    ]
    assert [label.decode() for label in labels] == [
        "bundle\0",
        "wrap\0",
        "event-sig\0",
        "log\0",
        "platform-log\0",
        "invite-package\0",
        "credential-sig\0",
        "request-wrap\0",
        "request-sig\0",
        "dedupe\0",
        "recovery-proof\0",
        "enroll\0",
    ]


def test_kdf_length_errors() -> None:
    with refused("prf_length"):
        kdf.kek_from_prf(bytes(31), b"c")
    with refused("prf_length"):
        kdf.kek_from_prf(bytes(33), b"c")
    for call in (kdf.kek_from_recovery_code, kdf.recovery_id_raw):
        with refused("recovery_code_length"):
            call(bytes(15))
    for call in (kdf.invite_id_raw, kdf.package_key, kdf.invite_sign_seed):
        with refused("invite_code_length"):
            call(bytes(17))
    calls: list[Callable[[], bytes]] = [
        lambda: kdf.event_key(bytes(31), bytes(32)),
        lambda: kdf.event_key(bytes(32), bytes(16)),
        lambda: kdf.credential_key(bytes(32), b""),
        lambda: kdf.credential_key(bytes(16), bytes(32)),
        lambda: kdf.dedupe_key(bytes(16)),
        lambda: kdf.storage_key(bytes(64)),
    ]
    for thunk in calls:
        with refused("header_invalid"):
            thunk()


# --- aead ---


def test_aead_round_trip_and_refusals() -> None:
    key, nonce = os.urandom(32), aead.random_nonce()
    ct = aead.seal(key, nonce, b"plain", b"aad")
    assert len(ct) == 5 + aead.TAG_BYTES
    assert aead.open_(key, nonce, ct, b"aad") == b"plain"
    for args in (
        (os.urandom(32), nonce, ct, b"aad"),
        (key, nonce, ct, b"other"),
        (key, nonce, ct[:-1] + bytes([ct[-1] ^ 1]), b"aad"),  # always a different tag byte
    ):
        with refused("decrypt_failed"):
            aead.open_(*args)
    with refused("header_invalid"):
        aead.seal(os.urandom(16), nonce, b"", b"")
    with refused("header_invalid"):
        aead.open_(key, bytes(8), ct, b"aad")
    blob = aead.seal_blob(key, b"x", b"a")
    assert aead.open_blob(key, blob, b"a") == b"x"
    assert aead.seal_blob(key, b"x", b"a", bytes(12))[:12] == bytes(12)
    with refused("decrypt_failed"):
        aead.open_blob(key, blob[:27], b"a")


# --- hpke ---


def test_hpke_matches_pyca_in_both_directions() -> None:
    sk = X25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    info = b"info"
    assert hpke.open_(sk, info, SUITE.encrypt(b"from pyca", sk.public_key(), info=info)) == b"from pyca"
    assert SUITE.decrypt(hpke.seal(pk, info, b"from us"), sk, info=info) == b"from us"
    eph = X25519PrivateKey.generate()
    fixed = hpke.seal(pk, info, b"x", eph)
    assert fixed[:32] == eph.public_key().public_bytes_raw()
    assert fixed == hpke.seal(pk, info, b"x", eph)
    assert SUITE.decrypt(fixed, sk, info=info) == b"x"


def test_hpke_setup_values_match_seal() -> None:
    sk, eph = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    values = hpke.setup_values(eph, pk, b"info")
    assert sorted(values) == ["base_nonce", "key", "shared_secret"]
    assert len(values["key"]) == 32
    assert len(values["base_nonce"]) == 12
    sealed = hpke.seal(pk, b"info", b"x", eph)
    assert aead.seal(values["key"], values["base_nonce"], b"x", b"") == sealed[32:]


def test_hpke_suite_ids() -> None:
    assert hpke.KEM_SUITE_ID == b"KEM\x00\x20"
    assert hpke.HPKE_SUITE_ID == b"HPKE\x00\x20\x00\x01\x00\x02"


def test_hpke_refusals() -> None:
    sk = X25519PrivateKey.generate()
    sealed = hpke.seal(sk.public_key().public_bytes_raw(), b"i", b"")
    assert len(sealed) == 48
    assert hpke.open_(sk, b"i", sealed) == b""
    with refused("hpke_short"):
        hpke.open_(sk, b"i", sealed[:47])
    with refused("decrypt_failed"):
        hpke.open_(sk, b"other", sealed)
    with refused("decrypt_failed"):
        hpke.open_(X25519PrivateKey.generate(), b"i", sealed)
    with refused("decrypt_failed"):
        hpke.open_(sk, b"i", bytes(32) + sealed[32:])
    with refused("decrypt_failed"):
        hpke.seal(bytes(32), b"i", b"x")
    with refused("header_invalid"):
        hpke.seal(bytes(31), b"i", b"x")


# --- keys ---


def test_identity_round_trip() -> None:
    ident = keys.Identity.generate()
    copy = keys.Identity.from_raw(keys.x25519_private_raw(ident.kem), keys.ed25519_seed(ident.sig))
    assert copy.public() == ident.public()
    assert b64u_decode(ident.public()["kem"]) == ident.kem_public() == keys.x25519_public_raw(ident.kem)
    assert b64u_decode(ident.public()["sig"]) == ident.sig_public() == keys.ed25519_public_raw(ident.sig)
    with refused("header_invalid"):
        keys.Identity.from_raw(bytes(31), bytes(32))
    with refused("header_invalid"):
        keys.ed25519_private(bytes(33))


def test_ed25519() -> None:
    ident = keys.Identity.generate()
    sig = keys.ed25519_sign(ident.sig, b"m")
    keys.ed25519_verify(ident.sig_public(), sig, b"m")
    keys.ed25519_verify_b64u(ident.sig_public(), b64u_encode(sig), b"m")
    other = keys.Identity.generate().sig_public()
    for public, signature, message in (
        (other, sig, b"m"),
        (ident.sig_public(), sig, b"n"),
        (ident.sig_public(), sig[:63], b"m"),
        (ident.sig_public()[:31], sig, b"m"),
        (bytes(32), sig, b"m"),
    ):
        with refused("signature_invalid"):
            keys.ed25519_verify(public, signature, message)
    for text in (None, "*", b64u_encode(sig) + "="):
        with refused("signature_invalid"):
            keys.ed25519_verify_b64u(ident.sig_public(), text, b"m")


# The libsodium blacklist of small-order Ed25519 encodings (8 torsion points plus the non-canonical y = p, p + 1).
SMALL_ORDER_KEYS = [
    bytes.fromhex(text)
    for text in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    )
]
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493


def _sign_bit(key: bytes) -> bytes:
    return key[:31] + bytes([key[31] | 0x80])


@pytest.mark.parametrize(
    "public", SMALL_ORDER_KEYS + [_sign_bit(key) for key in SMALL_ORDER_KEYS], ids=lambda key: key.hex()[:8]
)
def test_ed25519_refuses_small_order_keys(public: bytes) -> None:
    """R = key, S = 0 verifies any message under a small-order key in OpenSSL; the hardened verify refuses it."""
    for message in (b"", b"m", b"any message"):
        with refused("signature_invalid"):
            keys.ed25519_verify(public, public + bytes(32), message)
        with refused("signature_invalid"):
            keys.ed25519_verify_b64u(public, b64u_encode(public + bytes(32)), message)


def test_ed25519_identity_forgery_is_what_the_library_accepts() -> None:
    """The reason for the blacklist: pyca alone accepts the identity-key forgery (if this fails, the library
    changed and the check is still harmless)."""
    identity = SMALL_ORDER_KEYS[1]
    Ed25519PublicKey.from_public_bytes(identity).verify(identity + bytes(32), b"any message")


def test_ed25519_refuses_non_canonical_s() -> None:
    ident = keys.Identity.generate()
    sig = keys.ed25519_sign(ident.sig, b"m")
    keys.ed25519_verify(ident.sig_public(), sig, b"m")
    s = int.from_bytes(sig[32:], "little")
    for bad in (s + ED25519_ORDER, ED25519_ORDER, 2**256 - 1):
        with refused("signature_invalid"):
            keys.ed25519_verify(ident.sig_public(), sig[:32] + bad.to_bytes(32, "little"), b"m")


def test_p256_raw_formats() -> None:
    session = keys.p256_private(os.urandom(31) + b"\x01")
    point = keys.p256_public_raw(session)
    assert len(point) == 65
    assert point[0] == 4
    sig = keys.p256_sign_raw(session, b"text")
    assert len(sig) == 64
    keys.p256_verify_raw(point, sig, b"text")
    deterministic = keys.p256_sign_raw(session, b"text", deterministic=True)
    assert deterministic == keys.p256_sign_raw(session, b"text", deterministic=True)
    keys.p256_verify_raw(point, deterministic, b"text")
    n = keys.P256_ORDER
    r_high_s = sig[:32] + (n - int.from_bytes(sig[32:], "big")).to_bytes(32, "big")
    keys.p256_verify_raw(point, r_high_s, b"text")  # a high s is accepted, as in agent_core.passkey
    bad_point = b"\x04" + bytes(64)
    compressed = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point).public_bytes(
        Encoding.X962, PublicFormat.CompressedPoint
    )
    for public, signature, message in (
        (point, sig, b"other"),
        (point, sig[:63], b"text"),
        (point, bytes(32) + sig[32:], b"text"),
        (point, sig[:32] + n.to_bytes(32, "big"), b"text"),
        (compressed + bytes(32), sig, b"text"),
        (compressed, sig, b"text"),
        (bad_point, sig, b"text"),
    ):
        with refused("signature_invalid"):
            keys.p256_verify_raw(public, signature, message)


@pytest.mark.parametrize("scalar", [bytes(32), keys.P256_ORDER.to_bytes(32, "big"), bytes(31)])
def test_p256_private_refuses(scalar: bytes) -> None:
    with refused("header_invalid"):
        keys.p256_private(scalar)
