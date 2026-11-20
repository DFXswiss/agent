"""Verifying a passkey response (PASSKEY-PROTOCOL.md section 4) and a registration (section 5.5,
R4 to R13), called directly with responses of the software authenticator of section 13.1."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.relying_party import RelyingParty
from agent_core.passkey.webauthn import (
    FLAG_AT,
    FLAG_BE,
    FLAG_BS,
    FLAG_ED,
    FLAG_UP,
    FLAG_UV,
    TYPE_CREATE,
    TYPE_GET,
    check_client_data,
    check_counter,
    check_sign_in_authenticator_data,
    credential_public_key,
    verify_registration,
)
from tests.passkey_vectors import (
    ORIGIN,
    OTHER_ORIGIN,
    RP_ID,
    RegistrationCase,
    _registration_cases,
    b64,
    non_canonical_b64,
)
from tests.webauthn_authenticator import Settings, SoftAuthenticator, point_of, private_key_from

PARTY = RelyingParty(origin=ORIGIN, rp_id=RP_ID)
CHALLENGE = bytes(range(32))
CHALLENGE_TEXT = b64(CHALLENGE)
OPTIONS: dict[str, object] = {"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": CHALLENGE_TEXT}


def _refusal(error: str) -> Any:
    return pytest.raises(PasskeyRefusal, match=f"^{error}$")


def _client_data(**members: object) -> bytes:
    data: dict[str, object] = {"type": TYPE_GET, "challenge": CHALLENGE_TEXT, "origin": ORIGIN}
    data.update(members)
    return json.dumps({key: value for key, value in data.items() if value is not ...}, separators=(",", ":")).encode()


def _check(raw: bytes) -> None:
    check_client_data(raw, expected_type=TYPE_GET, challenge=CHALLENGE, origin=ORIGIN)


# --- section 4.1, client data ------------------------------------------------------------------


def test_client_data_accepted_with_crossorigin_false_absent_and_other_members() -> None:
    _check(_client_data(crossOrigin=False))
    _check(_client_data())
    _check(_client_data(other_keys_can_be_added_here="ignored"))


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'"text"',
        b'{"type":"webauthn.get","type":"webauthn.get","challenge":"' + CHALLENGE_TEXT.encode() + b'","origin":"https://hub.example"}',
        _client_data(crossOrigin=False).replace(b"false", b"NaN"),
        _client_data(type=TYPE_CREATE),
        _client_data(type=...),
        _client_data(type=["webauthn.get"]),
    ],
    ids=["not JSON", "array", "string", "repeated key", "NaN", "other type", "type missing", "type not text"],
)
def test_client_data_steps_1_and_2_fail_with_client_data_invalid(raw: bytes) -> None:
    with _refusal("client_data_invalid"):
        _check(raw)


@pytest.mark.parametrize(
    "challenge",
    [b64(bytes(32)), CHALLENGE_TEXT + "A", non_canonical_b64(CHALLENGE_TEXT), ..., 32, CHALLENGE_TEXT + "="],
    ids=["other bytes", "longer", "not canonical", "missing", "not text", "padded"],
)
def test_client_data_step_3_fails_with_challenge_mismatch(challenge: object) -> None:
    with _refusal("challenge_mismatch"):
        _check(_client_data(challenge=challenge))


@pytest.mark.parametrize(
    "members",
    [
        {"origin": OTHER_ORIGIN},
        {"origin": ORIGIN + "/"},
        {"origin": "HTTPS://hub.example"},
        {"origin": ...},
        {"crossOrigin": True},
        {"crossOrigin": "false"},
        {"crossOrigin": None},
        {"crossOrigin": 0},
        {"topOrigin": ORIGIN},
        {"topOrigin": None},
    ],
    ids=["other origin", "trailing slash", "uppercase scheme", "origin missing", "crossOrigin true",
         "crossOrigin text", "crossOrigin null", "crossOrigin 0", "topOrigin", "topOrigin null"],
)
def test_client_data_steps_4_and_5_fail_with_origin_mismatch(members: dict[str, object]) -> None:
    with _refusal("origin_mismatch"):
        _check(_client_data(**members))


def test_client_data_checks_run_in_order() -> None:
    with _refusal("client_data_invalid"):
        _check(_client_data(type=TYPE_CREATE, challenge="x", origin=OTHER_ORIGIN))
    with _refusal("challenge_mismatch"):
        _check(_client_data(challenge="x", origin=OTHER_ORIGIN))


# --- section 4.2, authenticator data of a sign-in ----------------------------------------------


def _authenticator_data(flags: int, *, rp_id: str = RP_ID, counter: int = 0, tail: bytes = b"") -> bytes:
    return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big") + tail


def test_sign_in_authenticator_data_accepted_and_counter_read() -> None:
    head = check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_UV, counter=258), RP_ID)
    assert head.sign_count == 258 and head.flags == FLAG_UP | FLAG_UV
    check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_UV | FLAG_BE | FLAG_BS), RP_ID)
    check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_UV | FLAG_BE), RP_ID)
    check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_UV | FLAG_ED, tail=b"\xa0"), RP_ID)
    check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_UV | FLAG_ED, tail=b"\xff\xff"), RP_ID)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (_authenticator_data(FLAG_UP | FLAG_UV)[:36], "authenticator_data_invalid"),
        (b"", "authenticator_data_invalid"),
        (_authenticator_data(FLAG_UP | FLAG_UV, rp_id="other.example"), "rp_id_mismatch"),
        (_authenticator_data(FLAG_UV), "user_presence_missing"),
        (_authenticator_data(FLAG_UP), "user_verification_missing"),
        (_authenticator_data(FLAG_UP | FLAG_UV | FLAG_BS), "authenticator_data_invalid"),
        (_authenticator_data(FLAG_UP | FLAG_UV | FLAG_AT), "authenticator_data_invalid"),
        (_authenticator_data(FLAG_UP | FLAG_UV | FLAG_AT | FLAG_ED, tail=b"\xa0"), "authenticator_data_invalid"),
        (_authenticator_data(FLAG_UP | FLAG_UV, tail=b"\x00"), "authenticator_data_invalid"),
    ],
    ids=["36 bytes", "empty", "rp id", "UP", "UV", "BS without BE", "AT", "AT with ED", "trailing without ED"],
)
def test_sign_in_authenticator_data_refusals(data: bytes, error: str) -> None:
    with _refusal(error):
        check_sign_in_authenticator_data(data, RP_ID)


def test_sign_in_authenticator_data_checks_run_in_order() -> None:
    with _refusal("rp_id_mismatch"):
        check_sign_in_authenticator_data(_authenticator_data(0, rp_id="other.example"), RP_ID)
    with _refusal("user_presence_missing"):
        check_sign_in_authenticator_data(_authenticator_data(FLAG_BS | FLAG_AT), RP_ID)
    with _refusal("user_verification_missing"):
        check_sign_in_authenticator_data(_authenticator_data(FLAG_UP | FLAG_BS), RP_ID)


# --- section 4.4, the signature counter ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "received"),
    [(None, 0), (None, 5), (0, 0), (0, 1), (5, 6), (0, 0xFFFFFFFF)],
    ids=["no kept value 0", "no kept value 5", "0 then 0", "0 then 1", "5 then 6", "0 then max"],
)
def test_counter_accepted_keeps_the_received_value(stored: int | None, received: int) -> None:
    assert check_counter(stored, received) == received


@pytest.mark.parametrize(
    ("stored", "received"), [(6, 6), (6, 3), (6, 0), (1, 0), (1, 1)], ids=["6 then 6", "6 then 3", "6 then 0", "1 then 0", "1 then 1"]
)
def test_counter_regressed(stored: int, received: int) -> None:
    with _refusal("counter_regressed"):
        check_counter(stored, received)


# --- section 5.5, R4 to R13 ----------------------------------------------------------------------


@pytest.mark.parametrize("case", _registration_cases(CHALLENGE_TEXT), ids=lambda case: case.name)
def test_registration_check(case: RegistrationCase) -> None:
    created = SoftAuthenticator().create(OPTIONS, ORIGIN, case.settings)
    body_id = created.credential_id
    if case.body_credential_id is not None:
        body_id = case.body_credential_id(body_id)

    def run() -> object:
        return verify_registration(
            credential_id=body_id,
            client_data_json=created.client_data_json,
            attestation_object=created.attestation_object,
            challenge=CHALLENGE,
            relying_party=PARTY,
        )

    if case.result == "ok":
        registration = run()
        assert registration.public_key == created.public_key  # type: ignore[attr-defined]
        assert registration.credential_id == created.credential_id  # type: ignore[attr-defined]
        assert registration.sign_count == 0  # type: ignore[attr-defined]
    else:
        with _refusal(case.result):
            run()


def test_registration_cases_cover_every_value_of_r4_to_r13() -> None:
    results = {case.result for case in _registration_cases(CHALLENGE_TEXT)}
    assert results == {
        "ok",
        "client_data_invalid",
        "challenge_mismatch",
        "origin_mismatch",
        "attestation_invalid",
        "authenticator_data_invalid",
        "rp_id_mismatch",
        "user_presence_missing",
        "user_verification_missing",
        "credential_invalid",
    }


def test_registration_checks_run_in_order() -> None:
    def refused(settings: Settings) -> str:
        created = SoftAuthenticator().create(OPTIONS, ORIGIN, settings)
        with pytest.raises(PasskeyRefusal) as caught:
            verify_registration(
                credential_id=created.credential_id,
                client_data_json=created.client_data_json,
                attestation_object=created.attestation_object,
                challenge=CHALLENGE,
                relying_party=PARTY,
            )
        return caught.value.error

    assert refused(Settings(origin=OTHER_ORIGIN, uv=False)) == "origin_mismatch"
    assert refused(Settings(rp_id="other.example", up=False)) == "rp_id_mismatch"
    assert refused(Settings(up=False, uv=False)) == "user_presence_missing"
    assert refused(Settings(uv=False, bs=True)) == "user_verification_missing"
    assert refused(Settings(bs=True, credential_id_length=15)) == "authenticator_data_invalid"
    assert refused(Settings(attestation_object=lambda data: b"\x00", origin=OTHER_ORIGIN)) == "origin_mismatch"


def test_refusal_message_carries_no_input() -> None:
    created = SoftAuthenticator().create(OPTIONS, ORIGIN, Settings(challenge=b64(bytes(32))))
    with pytest.raises(PasskeyRefusal) as caught:
        verify_registration(
            credential_id=created.credential_id,
            client_data_json=created.client_data_json,
            attestation_object=created.attestation_object,
            challenge=CHALLENGE,
            relying_party=PARTY,
        )
    assert str(caught.value) == "challenge_mismatch"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# --- R13 called directly on a decoded map --------------------------------------------------------


def _cose(point: bytes) -> dict[object, object]:
    return {1: 2, 3: -7, -1: 1, -2: point[1:33], -3: point[33:65]}


def test_r13_accepts_an_es256_key_and_returns_the_point() -> None:
    point = point_of(private_key_from(lambda size: bytes([7]) * size))
    assert credential_public_key(_cose(point)) == point


@pytest.mark.parametrize(
    "change",
    [
        lambda key: {**key, -1: True},
        lambda key: {**key, 3: -7.0},
        lambda key: {1.0 if k == 1 else k: v for k, v in key.items()},
        lambda key: {True if k == 1 else k: v for k, v in key.items()},
        lambda key: {**key, 1: 2.0},
        lambda key: {**key, 1: 3},
        lambda key: {**key, 3: -8},
        lambda key: {**key, -1: 2},
        lambda key: {**key, -2: key[-2][:31]},
        lambda key: {**key, -3: bytearray(key[-3])},
        lambda key: {**key, -2: "x" * 32},
        lambda key: {**key, 4: 1},
        lambda key: {k: v for k, v in key.items() if k != -3},
        lambda key: {**key, -3: key[-3][:-1] + bytes([key[-3][-1] ^ 1])},
        lambda key: [key],
        lambda key: {},
    ],
    ids=["-1: True", "3: -7.0", "key 1.0", "key True", "kty float", "kty 3", "alg -8", "crv 2", "x of 31 bytes",
         "y bytearray", "x text", "extra key", "missing key", "not on the curve", "not a map", "empty map"],
)
def test_r13_refuses_with_credential_invalid(change: object) -> None:
    point = point_of(private_key_from(lambda size: bytes([7]) * size))
    with _refusal("credential_invalid"):
        credential_public_key(change(_cose(point)))  # type: ignore[operator]
