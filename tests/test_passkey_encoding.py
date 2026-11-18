"""Encodings and constants (PASSKEY-PROTOCOL.md sections 2 and 3), called directly.

Pure functions: these tests need no database and no hub.
"""

from __future__ import annotations

import hashlib

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from agent_core.passkey import encoding
from agent_core.passkey.encoding import (
    CBOR_INT_MAX,
    CBOR_INT_MIN,
    P256_ORDER,
    CborInvalid,
    JsonInvalid,
    ceremony_id,
    decode_cbor_item,
    fingerprint,
    invite_id,
    invite_token,
    json_integer,
    p256_point_valid,
    parse_json,
    passkey_signature_canonical,
    session_key_id,
    session_signature_scalars,
    valid_session_key_id,
    verify_passkey_signature,
    verify_session_signature,
)
from agent_core.passkey.relying_party import relying_party
from tests.passkey_vectors import b64
from tests.webauthn_authenticator import (
    BIGNUM,
    FLOAT_MINUS_7,
    NULL,
    SIMPLE_VALUE,
    TRUE,
    UNDEFINED,
    UNKNOWN_TAG,
    cbor,
    cbor_map,
    non_canonical_der,
    point_of,
)


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


# --- constants -----------------------------------------------------------------------------------


def test_constants_have_the_values_of_section_2() -> None:
    assert encoding.SESSION_KEY_MAX_SECONDS == 43200
    assert encoding.SESSION_KEY_ISSUE_SECONDS == 43080
    assert encoding.SESSION_KEY_MAX_SECONDS - encoding.SESSION_KEY_ISSUE_SECONDS == 120
    assert encoding.CEREMONY_SECONDS == 300
    assert encoding.INVITE_MIN_SECONDS == 300
    assert encoding.INVITE_MAX_SECONDS == 86400
    assert encoding.INVITE_OPEN_LIMIT == 50
    assert encoding.PENDING_SIGN_IN_LIMIT == 1000
    assert encoding.ANONYMOUS_BODY_LIMIT == 65536
    assert encoding.COMMAND_TIME_WINDOW == 120
    assert encoding.SEQUENCE_MAX == 2**53 - 1
    assert encoding.P256_ORDER == int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)


# --- fingerprint, ids ----------------------------------------------------------------------------


def test_fingerprint_is_first_16_bytes_of_sha256_in_8_groups_of_4() -> None:
    point = point_of(_key())
    digits = hashlib.sha256(point).hexdigest()[:32]
    expected = " ".join(digits[i : i + 4] for i in range(0, 32, 4))
    assert fingerprint(point) == expected
    assert len(fingerprint(point)) == 39
    assert fingerprint(point) == fingerprint(point).lower()


def test_fingerprint_refuses_a_value_that_is_not_65_bytes() -> None:
    with pytest.raises(ValueError):
        fingerprint(bytes(64))


def test_session_key_id_is_session_and_32_hex_of_sha256_over_the_statement() -> None:
    statement = b"agent-session-v1\nhttps://hub.example\nx\ny\nz"
    assert session_key_id(statement) == "session." + hashlib.sha256(statement).hexdigest()[:32]
    assert len(session_key_id(statement)) == 40
    assert valid_session_key_id(session_key_id(statement))


@pytest.mark.parametrize(
    "value",
    ["session." + "0" * 31, "session." + "0" * 33, "session." + "A" * 32, "sessionx" + "0" * 32, "example-key", None, 1],
)
def test_session_key_id_form_refuses_every_other_text(value: object) -> None:
    assert not valid_session_key_id(value)


def test_invite_id_is_invite_and_32_hex() -> None:
    assert invite_id(bytes(range(16))) == "invite." + bytes(range(16)).hex()


def test_invite_token_is_43_characters_of_base64url() -> None:
    token = invite_token(bytes(32))
    assert len(token) == 43 and token == b64(bytes(32))


def test_ceremony_id_is_22_characters_of_base64url() -> None:
    assert len(ceremony_id(bytes(16))) == 22


@pytest.mark.parametrize(
    ("function", "size"), [(invite_id, 15), (invite_id, 17), (invite_token, 31), (ceremony_id, 17)]
)
def test_ids_refuse_a_random_value_of_another_length(function: object, size: int) -> None:
    with pytest.raises(ValueError):
        function(bytes(size))  # type: ignore[operator]


def test_p256_point_must_lie_on_the_curve() -> None:
    point = point_of(_key())
    assert p256_point_valid(point)
    assert not p256_point_valid(b"\x04" + bytes(64))
    assert not p256_point_valid(point[:64])
    assert not p256_point_valid(b"\x02" + point[1:])


# --- the JSON rule -------------------------------------------------------------------------------


def test_json_rule_parses_an_object() -> None:
    assert parse_json(b'{"a":[1,"x",null,true]}') == {"a": [1, "x", None, True]}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":1}',
        b'{"b":{"a":1,"a":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b"\xff",
        b'\xef\xbb\xbf{"a":1}',
        b"{",
        b"",
        b"[" * 200000 + b"]" * 200000,
    ],
    ids=["repeated key", "nested repeated key", "NaN", "Infinity", "-Infinity", "not UTF-8", "BOM", "truncated", "empty", "deep"],
)
def test_json_rule_refuses(raw: bytes) -> None:
    with pytest.raises(JsonInvalid):
        parse_json(raw)


def test_json_integer_is_exactly_int() -> None:
    assert json_integer(parse_json(b"1"))
    for raw in (b"1.0", b"1e0", b"true", b'"1"'):
        assert not json_integer(parse_json(raw))


# --- CBOR items ----------------------------------------------------------------------------------


def test_cbor_item_accepted_returns_the_item_and_its_end() -> None:
    raw = cbor({"a": [1, b"x", "y"], 1: -2})
    item, end = decode_cbor_item(raw + b"\x00\x01", 0, booleans=False)
    assert item == {"a": [1, b"x", "y"], 1: -2}
    assert end == len(raw)


def test_cbor_item_decoded_at_a_position() -> None:
    raw = b"\xff\xff" + cbor([1, 2])
    item, end = decode_cbor_item(raw, 2, booleans=False)
    assert item == [1, 2] and end == len(raw)


def test_cbor_integer_range_boundaries_accepted() -> None:
    for value in (CBOR_INT_MAX, CBOR_INT_MIN, 0, -1):
        raw = cbor(value)
        assert decode_cbor_item(raw, 0, booleans=False) == (value, len(raw))


@pytest.mark.parametrize(
    "raw",
    [
        BIGNUM,
        bytes.fromhex("c3") + cbor(b"\x01" + bytes(8)),
    ],
    ids=["bignum 2**64", "bignum -2**64 - 1"],
)
def test_cbor_integer_outside_major_types_0_and_1_refused(raw: bytes) -> None:
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)


@pytest.mark.parametrize(
    "raw",
    [
        cbor_map([(cbor("a"), cbor(1))], width=1),
        bytes.fromhex("1800"),
        cbor_map([(cbor("a"), cbor(1)), (cbor("a"), cbor(1))], width=None),
        cbor_map([(cbor("bb"), cbor(1)), (cbor("a"), cbor(1))], width=None),
        bytes.fromhex("5f4101ff"),
        bytes.fromhex("c2") + cbor(b"\x05"),
    ],
    ids=["map length", "integer", "key written twice", "key order", "indefinite length", "bignum of a small value"],
)
def test_cbor_non_canonical_form_refused(raw: bytes) -> None:
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)


@pytest.mark.parametrize(
    "raw",
    [
        FLOAT_MINUS_7,
        UNKNOWN_TAG,
        SIMPLE_VALUE,
        UNDEFINED,
        NULL,
        TRUE,
        bytes.fromhex("f4"),
        cbor_map([(cbor([1]), cbor(1))], width=None),
        cbor_map([(cbor({1: 1}), cbor(1))], width=None),
        bytes.fromhex("c11a00000000"),
        cbor([1, 1.5]),
    ],
    ids=["float", "unknown tag", "simple value", "undefined", "null", "true", "false", "array key", "map key", "datetime tag", "nested float"],
)
def test_cbor_type_outside_int_bytes_text_array_map_refused(raw: bytes) -> None:
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)


def test_cbor_booleans_accepted_only_as_values_where_allowed() -> None:
    raw = cbor_map([(cbor("x"), TRUE), (cbor("y"), cbor([False]))], width=None)
    assert decode_cbor_item(raw, 0, booleans=True)[0] == {"x": True, "y": [False]}
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)
    with pytest.raises(CborInvalid):
        decode_cbor_item(cbor_map([(TRUE, cbor(1))], width=None), 0, booleans=True)
    with pytest.raises(CborInvalid):
        decode_cbor_item(cbor_map([(cbor("x"), NULL)], width=None), 0, booleans=True)


@pytest.mark.parametrize("raw", [b"", bytes.fromhex("a1"), bytes.fromhex("5803ab"), bytes.fromhex("1c")], ids=["empty", "truncated map", "truncated bytes", "reserved"])
def test_cbor_bytes_that_are_no_item_refused(raw: bytes) -> None:
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)


def test_cbor_float_shortest_form_reencodes_equal_so_only_the_type_check_refuses_it() -> None:
    assert cbor2.dumps(cbor2.loads(FLOAT_MINUS_7), canonical=True) == FLOAT_MINUS_7
    with pytest.raises(CborInvalid):
        decode_cbor_item(FLOAT_MINUS_7, 0, booleans=True)


# --- signature encodings -------------------------------------------------------------------------


def test_session_signature_verifies_as_r_s_and_high_s_is_accepted() -> None:
    key = _key()
    text = b"agent-request-v1\nexample"
    r, s = decode_dss_signature(key.sign(text, ec.ECDSA(hashes.SHA256())))
    point = point_of(key)
    low = min(s, P256_ORDER - s)
    for value in (low, P256_ORDER - low):
        assert verify_session_signature(point, r.to_bytes(32, "big") + value.to_bytes(32, "big"), text)
    assert not verify_session_signature(point, r.to_bytes(32, "big") + low.to_bytes(32, "big"), text + b"x")


@pytest.mark.parametrize(
    ("r", "s"), [(0, 1), (1, 0), (P256_ORDER, 1), (1, P256_ORDER)], ids=["r 0", "s 0", "r n", "s n"]
)
def test_session_signature_scalars_from_1_to_n_minus_1(r: int, s: int) -> None:
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert session_signature_scalars(signature) is None
    assert not verify_session_signature(point_of(_key()), signature, b"text")


def test_session_signature_of_another_length_refused() -> None:
    key = _key()
    der = key.sign(b"text", ec.ECDSA(hashes.SHA256()))
    assert session_signature_scalars(bytes(63)) is None
    assert not verify_session_signature(point_of(key), der, b"text")
    assert session_signature_scalars((1).to_bytes(32, "big") * 2) == (1, 1)


def test_passkey_signature_is_canonical_der_of_at_most_72_bytes() -> None:
    key = _key()
    data, client = b"authenticator data", b'{"type":"webauthn.get"}'
    der = key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    assert passkey_signature_canonical(der)
    assert verify_passkey_signature(point_of(key), der, data, client)
    assert not passkey_signature_canonical(non_canonical_der(der))
    assert not verify_passkey_signature(point_of(key), non_canonical_der(der), data, client)
    assert not passkey_signature_canonical(der + bytes(73 - len(der)))
    assert not passkey_signature_canonical(b"\x30\x00garbage")
    assert not passkey_signature_canonical(b"")


def test_passkey_signature_over_other_bytes_refused() -> None:
    key = _key()
    data, client = b"authenticator data", b"{}"
    der = key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    assert not verify_passkey_signature(point_of(key), der, data + b"x", client)
    assert not verify_passkey_signature(point_of(key), der, data, client + b" ")
    assert not verify_passkey_signature(point_of(_key()), der, data, client)


# --- relying party (section 3) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin", "rp_id"),
    [
        ("https://hub.example", "https://hub.example", "hub.example"),
        ("https://hub.example/", "https://hub.example", "hub.example"),
        ("HTTPS://Hub.Example:443", "https://hub.example", "hub.example"),
        ("https://hub.example:8443", "https://hub.example:8443", "hub.example"),
        ("http://localhost:8787", "http://localhost:8787", "localhost"),
        ("http://localhost:80", "http://localhost", "localhost"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example", "xn--bcher-kva.example"),
    ],
)
def test_origin_and_rp_id_from_the_public_url(url: str, origin: str, rp_id: str) -> None:
    party = relying_party(url)
    assert party.origin == origin
    assert party.rp_id == rp_id
