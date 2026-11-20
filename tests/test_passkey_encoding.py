"""Encodings and constants (PASSKEY-PROTOCOL.md sections 2 and 3), called directly.

Pure functions: these tests need no database and no hub.
"""

from __future__ import annotations

import decimal
import hashlib
import re

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
    SHARED_ARRAY,
    SHARED_ARRAY_CYCLE,
    SHARED_MAP_CYCLE,
    SIMPLE_VALUE,
    TRUE,
    UNDEFINED,
    UNKNOWN_TAG,
    cbor,
    cbor_map,
    non_canonical_der,
    other_point_forms,
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


FORM_KEY = ec.derive_private_key(7, ec.SECP256R1())
OTHER_FORMS = other_point_forms(FORM_KEY)


@pytest.mark.parametrize("form", [form for _, form in OTHER_FORMS], ids=[name for name, _ in OTHER_FORMS])
def test_a_key_in_another_form_verifies_no_signature(form: bytes) -> None:
    """Section 2: a P-256 public key is the 65-byte uncompressed point only. Every other form,
    the same key compressed included, is no valid point and verifies a valid signature by that
    key to False, never to True or an exception."""
    assert not p256_point_valid(form)
    text = b"agent-command-v1\nexample"
    r, s = decode_dss_signature(FORM_KEY.sign(text, ec.ECDSA(hashes.SHA256())))
    session_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert verify_session_signature(point_of(FORM_KEY), session_signature, text)
    assert verify_session_signature(form, session_signature, text) is False
    data, client = b"authenticator data", b"{}"
    der = FORM_KEY.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    assert verify_passkey_signature(point_of(FORM_KEY), der, data, client)
    assert verify_passkey_signature(form, der, data, client) is False


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


@pytest.mark.parametrize(
    "raw",
    [
        b'{"token":"SECRET-TOKEN-\xff"}',
        b'{"token":"SECRET-TOKEN",',
        b'{"token":"SECRET-TOKEN","token":"SECRET-TOKEN"}',
        b'{"token":"SECRET-TOKEN","n":NaN}',
        b'[{"token":"SECRET-TOKEN"}' + b"[" * 200000,
    ],
    ids=["not UTF-8", "truncated", "repeated key", "NaN", "deep"],
)
def test_json_refusal_chains_no_exception_and_holds_no_input(raw: bytes) -> None:
    """The failure of `json` holds the input (`JSONDecodeError.doc`, `UnicodeDecodeError.object`);
    the refusal chains none, so no traceback or log of it can show an invite token or a body."""
    with pytest.raises(JsonInvalid) as refused:
        parse_json(raw)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    for shown in (str(refused.value), repr(refused.value)):
        assert "SECRET" not in shown and "xff" not in shown
    assert str(refused.value) == "the text breaks the JSON rule of section 2"


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


def _shared_arrays(depth: int) -> bytes:
    """Arrays of two references to one shared array, `depth` levels deep: 2**depth paths to `[]`."""
    raw = cbor([])
    for index in reversed(range(depth)):
        raw = bytes.fromhex("82d81c") + raw + bytes.fromhex("d81d") + cbor(index)
    return raw


@pytest.mark.parametrize(
    "raw",
    [
        SHARED_ARRAY_CYCLE,
        SHARED_MAP_CYCLE,
        cbor_map([(cbor("x"), SHARED_ARRAY_CYCLE)], width=None),
        SHARED_ARRAY,
        _shared_arrays(64),
        bytes.fromhex("82d81c50") + bytes(16) + bytes.fromhex("d81d00"),
        bytes.fromhex("82d81c70") + b"a" * 16 + bytes.fromhex("d81d00"),
    ],
    ids=[
        "array that holds itself",
        "map that holds itself",
        "cycle inside a map",
        "shared array",
        "shared arrays 64 deep",
        "shared byte string",
        "shared text",
    ],
)
def test_cbor_shared_value_refused_by_the_type_check(raw: bytes) -> None:
    """Tags 28 and 29 decode to one array or map object met twice or inside itself, or to strings
    longer in total than the item's bytes. Check 2 refuses it at once (the message names it),
    before check 1 would encode the item again."""
    assert _shared_arrays(1) == SHARED_ARRAY
    for booleans in (False, True):
        with pytest.raises(CborInvalid, match="shared value"):
            decode_cbor_item(raw, 0, booleans=booleans)


def test_cbor_strings_filling_the_item_exactly_accepted() -> None:
    """The string lengths of a tag-free item never exceed its bytes, also when the data ends with
    the item."""
    raw = cbor([bytes(16), "a" * 16])
    assert decode_cbor_item(raw, 0, booleans=False) == ([bytes(16), "a" * 16], len(raw))
    assert decode_cbor_item(b"\xff" + cbor(""), 1, booleans=False) == ("", 2)


@pytest.mark.parametrize("raw", [b"", bytes.fromhex("a1"), bytes.fromhex("5803ab"), bytes.fromhex("1c")], ids=["empty", "truncated map", "truncated bytes", "reserved"])
def test_cbor_bytes_that_are_no_item_refused(raw: bytes) -> None:
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=False)


# The innermost item `18 00` is the integer 0 in a non-canonical form: it passes the type check, so
# a nesting the decoder accepts reaches `cbor2.dumps` at the full depth.
NON_CANONICAL_ZERO = bytes.fromhex("1800")


@pytest.mark.parametrize(
    "raw",
    [
        b"\x81" * 400 + NON_CANONICAL_ZERO,
        b"\x81" * 400 + NULL,
        b"\x81" * 400,
        b"\x81" * 60000 + NON_CANONICAL_ZERO,
        b"\x81" * 60000,
        b"\xa1\x00" * 400 + NON_CANONICAL_ZERO,
        b"\xa1\x00" * 400 + NULL,
        b"\xa1\x00" * 60000 + NON_CANONICAL_ZERO,
        b"\xa1\x00" * 60000,
        b"\x81\xa1\x00" * 30000 + NON_CANONICAL_ZERO,
    ],
    ids=[
        "400 arrays",
        "400 arrays around null",
        "400 arrays truncated",
        "60000 arrays",
        "60000 arrays truncated",
        "400 maps",
        "400 maps around null",
        "60000 maps",
        "60000 maps truncated",
        "60000 arrays and maps",
    ],
)
def test_cbor_deep_nesting_refused(raw: bytes) -> None:
    """Whatever depth the decoder or the encoder of `cbor2` refuses with `RecursionError`, the
    item is refused with `CborInvalid`; every item here is also invalid at any depth they accept."""
    for booleans in (False, True):
        with pytest.raises(CborInvalid):
            decode_cbor_item(raw, 0, booleans=booleans)


@pytest.mark.parametrize(
    "raw",
    [
        bytes.fromhex("d81e820100"),
        bytes.fromhex("c5821b7fffffffffffffff01"),
        bytes.fromhex("c4821b7fffffffffffffff01"),
        bytes.fromhex("d8236128"),
        bytes.fromhex("d825420000"),
    ],
    ids=["rational with denominator 0", "bigfloat with a huge exponent", "decimal with a huge exponent", "regular expression that does not compile", "uuid of 2 bytes"],
)
def test_cbor_semantic_tag_whose_decoder_fails_refused(raw: bytes) -> None:
    """A known tag whose decoder in `cbor2` fails, with an exception that need not be a
    `CBORDecodeError` or a `ValueError`, or that decodes to a type outside check 2."""
    with pytest.raises(CborInvalid):
        decode_cbor_item(raw, 0, booleans=True)


class _FailingDecoder:
    """Stands in for `cbor2.CBORDecoder` and raises the given exception from `decode`."""

    failure: BaseException

    def __init__(self, stream: object) -> None:
        self.stream = stream

    def decode(self) -> object:
        raise self.failure


DECODER_FAILURES = [
    RecursionError(),
    ZeroDivisionError(),
    decimal.Overflow(),
    re.error("x"),
    LookupError(),
    AttributeError(),
    TypeError(),
    cbor2.CBORDecodeError(),
]


@pytest.mark.parametrize("failure", DECODER_FAILURES, ids=lambda failure: type(failure).__name__)
def test_cbor_every_decoder_exception_is_the_refusal(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FailingDecoder, "failure", failure, raising=False)
    monkeypatch.setattr(cbor2, "CBORDecoder", _FailingDecoder)
    with pytest.raises(CborInvalid, match="^the bytes are no CBOR item$") as refused:
        decode_cbor_item(cbor([1]), 0, booleans=False)
    assert refused.value.__cause__ is None and refused.value.__context__ is None


@pytest.mark.parametrize(
    "failure", [RecursionError(), cbor2.CBOREncodeError(), ValueError(), ZeroDivisionError()], ids=lambda failure: type(failure).__name__
)
def test_cbor_every_encoder_exception_is_the_refusal(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cbor2.dumps` of check 1 recurses once per nesting level, like the decoder."""

    def dumps(item: object, **options: object) -> bytes:
        raise failure

    monkeypatch.setattr(cbor2, "dumps", dumps)
    with pytest.raises(CborInvalid, match="^the CBOR item cannot be encoded again$") as refused:
        decode_cbor_item(bytes.fromhex("8101"), 0, booleans=False)
    assert refused.value.__cause__ is None and refused.value.__context__ is None


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(1), MemoryError()], ids=lambda failure: type(failure).__name__)
def test_cbor_interrupt_exit_and_memory_error_pass(failure: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FailingDecoder, "failure", failure, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(cbor2, "CBORDecoder", _FailingDecoder)
        with pytest.raises(type(failure)):
            decode_cbor_item(cbor([1]), 0, booleans=False)

    def dumps(item: object, **options: object) -> bytes:
        raise failure

    monkeypatch.setattr(cbor2, "dumps", dumps)
    with pytest.raises(type(failure)):
        decode_cbor_item(bytes.fromhex("8101"), 0, booleans=False)


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
