"""The statement (PASSKEY-PROTOCOL.md section 6.2) and `verify_certificate` (section 6.4), called
directly (section 13.3, "`verify_certificate` directly", and the steps V1 to V11)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from agent_core.passkey.certificate import (
    Certificate,
    build_statement,
    parse_statement,
    statement_challenge,
    verify_certificate,
)
from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.relying_party import RelyingParty
from tests.passkey_vectors import (
    NOW,
    ORIGIN,
    OTHER_ORIGIN,
    RP_ID,
    SESSION_KEY_ISSUE_SECONDS,
    CertificateCase,
    _certificate_cases,
    b64,
    joined,
    statement_lines,
    time_text,
)
from tests.webauthn_authenticator import (
    Settings,
    SoftAuthenticator,
    corrupt,
    other_point_forms,
    point_of,
    private_key_from,
)

PARTY = RelyingParty(origin=ORIGIN, rp_id=RP_ID)
EXPIRES = NOW + SESSION_KEY_ISSUE_SECONDS
SESSION_PUBLIC = point_of(private_key_from(lambda size: bytes([3]) * size))
HUB_VALUE = bytes(range(32))
LINES = statement_lines(SESSION_PUBLIC, HUB_VALUE, EXPIRES)
STATEMENT = joined(LINES)
OTHER_STATEMENT = joined(statement_lines(SESSION_PUBLIC, bytes(32), EXPIRES))
OTHER_PUBLIC = point_of(private_key_from(lambda size: bytes([5]) * size))


def _refusal(error: str) -> Any:
    return pytest.raises(PasskeyRefusal, match=f"^{error}$")


def _passkey() -> SoftAuthenticator:
    passkey = SoftAuthenticator(counter_start=9)
    passkey.create({"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": b64(bytes(32))}, ORIGIN)
    return passkey


def _certificate(passkey: SoftAuthenticator, statement: bytes, *, signed: bytes | None = None, settings: Settings | None = None) -> Certificate:
    asserted = passkey.get(
        {"challenge": b64(hashlib.sha256(signed if signed is not None else statement).digest()), "rp_id": RP_ID},
        ORIGIN,
        settings if settings is not None else Settings(),
    )
    return Certificate(
        statement=statement,
        credential_id=asserted.credential_id,
        authenticator_data=asserted.authenticator_data,
        client_data_json=asserted.client_data_json,
        signature=asserted.signature,
    )


def _verify(passkey: SoftAuthenticator, certificate: Certificate, *, now: int, max_lifetime: int, public_key: bytes | None = None) -> Any:
    return verify_certificate(
        certificate,
        passkey_public_key=public_key if public_key is not None else passkey.credentials[0].public_key,
        relying_party=PARTY,
        now=now,
        max_lifetime=max_lifetime,
    )


# --- section 6.2, the statement ---------------------------------------------------------------


def test_statement_is_five_lines_without_trailing_line_feed() -> None:
    statement = build_statement(origin=ORIGIN, session_public_key=SESSION_PUBLIC, hub_value=HUB_VALUE, expires=EXPIRES)
    assert statement == (
        f"agent-session-v1\n{ORIGIN}\n{b64(SESSION_PUBLIC)}\n{b64(HUB_VALUE)}\n{time_text(EXPIRES)}"
    ).encode()
    assert statement == STATEMENT


def test_statement_parsed_back_into_its_values() -> None:
    parsed = parse_statement(STATEMENT, ORIGIN)
    assert parsed.text == STATEMENT
    assert parsed.origin == ORIGIN
    assert parsed.session_public_key == SESSION_PUBLIC
    assert parsed.hub_value == HUB_VALUE
    assert parsed.expires == EXPIRES
    assert "session_public_key" not in repr(parsed)
    assert "hub_value" not in repr(parsed)


def test_challenge_is_sha256_over_the_statement_bytes() -> None:
    assert statement_challenge(STATEMENT) == hashlib.sha256(STATEMENT).digest()


def test_statement_built_only_from_a_valid_point_and_a_32_byte_hub_value() -> None:
    with pytest.raises(ValueError):
        build_statement(origin=ORIGIN, session_public_key=b"\x04" + bytes(64), hub_value=HUB_VALUE, expires=EXPIRES)
    with pytest.raises(ValueError):
        build_statement(origin=ORIGIN, session_public_key=SESSION_PUBLIC, hub_value=HUB_VALUE[:31], expires=EXPIRES)


@pytest.mark.parametrize(
    "statement",
    [
        joined(LINES[:4]),
        joined([*LINES, "x"]),
        STATEMENT + b"\n",
        b"\n" + STATEMENT,
        joined(["agent-session-v2", *LINES[1:]]),
        joined([LINES[0], OTHER_ORIGIN, *LINES[2:]]),
        joined([LINES[0], ORIGIN + "/", *LINES[2:]]),
        joined([*LINES[:2], b64(b"\x04" + bytes(64)), *LINES[3:]]),
        joined([*LINES[:2], b64(SESSION_PUBLIC[:64]), *LINES[3:]]),
        joined([*LINES[:2], LINES[2] + "=", *LINES[3:]]),
        joined([*LINES[:3], b64(HUB_VALUE[:31]), *LINES[4:]]),
        joined([*LINES[:3], b64(HUB_VALUE + b"\x00"), *LINES[4:]]),
        joined([*LINES[:3], "!", *LINES[4:]]),
        joined([*LINES[:4], "2026-11-17T21:58:00"]),
        joined([*LINES[:4], "2026-02-30T00:00:00Z"]),
        STATEMENT.replace(b"\n", b"\r\n"),
        STATEMENT[:-1] + b"\xff",
        b"",
    ],
    ids=["four lines", "six lines", "trailing line feed", "leading line feed", "another label", "another origin",
         "origin with slash", "key not a point", "key of 64 bytes", "key padded", "hub value of 31 bytes",
         "hub value of 33 bytes", "hub value not base64url", "expiry without Z", "expiry not a date", "CR LF",
         "not UTF-8", "empty"],
)
def test_v1_statement_invalid(statement: bytes) -> None:
    with _refusal("statement_invalid"):
        parse_statement(statement, ORIGIN)


# --- section 6.4, V1 to V11 through the software authenticator ----------------------------------


@pytest.mark.parametrize(
    "case", _certificate_cases(LINES, OTHER_STATEMENT, OTHER_PUBLIC), ids=lambda case: case.name
)
def test_verify_certificate_step(case: CertificateCase) -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, case.statement, signed=case.signed_statement, settings=case.settings)
    if case.result == "ok":
        session = _verify(passkey, certificate, now=case.now, max_lifetime=case.max_lifetime, public_key=case.public_key)
        assert session.session_key_id == "session." + hashlib.sha256(case.statement).hexdigest()[:32]
        assert session.statement.session_public_key == SESSION_PUBLIC
        assert session.statement.expires == EXPIRES
        assert session.sign_count == 9
    else:
        with _refusal(case.result):
            _verify(passkey, certificate, now=case.now, max_lifetime=case.max_lifetime, public_key=case.public_key)


def test_certificate_cases_cover_every_value_of_v1_to_v11() -> None:
    results = {case.result for case in _certificate_cases(LINES, OTHER_STATEMENT, OTHER_PUBLIC)}
    assert results == {
        "ok",
        "statement_invalid",
        "client_data_invalid",
        "challenge_mismatch",
        "origin_mismatch",
        "authenticator_data_invalid",
        "rp_id_mismatch",
        "user_presence_missing",
        "user_verification_missing",
        "signature_invalid",
        "certificate_expiry_invalid",
    }


@pytest.mark.parametrize("max_lifetime", [43200, 3600])
def test_v11_expiry_bound_with_max_lifetime(max_lifetime: int) -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT)
    _verify(passkey, certificate, now=EXPIRES - max_lifetime, max_lifetime=max_lifetime)
    with _refusal("certificate_expiry_invalid"):
        _verify(passkey, certificate, now=EXPIRES - max_lifetime - 1, max_lifetime=max_lifetime)
    with _refusal("certificate_expiry_invalid"):
        _verify(passkey, certificate, now=EXPIRES, max_lifetime=max_lifetime)


def test_device_with_l_3600_refuses_a_fresh_certificate_and_accepts_it_39480_seconds_later() -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT)
    with _refusal("certificate_expiry_invalid"):
        _verify(passkey, certificate, now=NOW, max_lifetime=3600)
    _verify(passkey, certificate, now=NOW + 39480, max_lifetime=3600)
    with _refusal("certificate_expiry_invalid"):
        _verify(passkey, certificate, now=NOW + 39479, max_lifetime=3600)


def test_device_with_l_43200_accepts_a_fresh_certificate_with_its_clock_120_seconds_behind() -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT)
    _verify(passkey, certificate, now=NOW - 120, max_lifetime=43200)
    with _refusal("certificate_expiry_invalid"):
        _verify(passkey, certificate, now=NOW - 121, max_lifetime=43200)


def test_v3_a_valid_assertion_for_another_statement_is_challenge_mismatch() -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT, signed=OTHER_STATEMENT)
    with _refusal("challenge_mismatch"):
        _verify(passkey, certificate, now=NOW, max_lifetime=43200)


def test_steps_run_in_order_and_the_first_failure_wins() -> None:
    passkey = _passkey()
    broken = _certificate(passkey, STATEMENT + b"\n", settings=Settings(origin=OTHER_ORIGIN, signature=corrupt))
    with _refusal("statement_invalid"):
        _verify(passkey, broken, now=NOW, max_lifetime=43200)
    broken = _certificate(passkey, STATEMENT, settings=Settings(origin=OTHER_ORIGIN, uv=False))
    with _refusal("origin_mismatch"):
        _verify(passkey, broken, now=NOW, max_lifetime=43200)
    broken = _certificate(passkey, STATEMENT, settings=Settings(uv=False, signature=corrupt))
    with _refusal("user_verification_missing"):
        _verify(passkey, broken, now=NOW, max_lifetime=43200)
    broken = _certificate(passkey, STATEMENT, settings=Settings(signature=corrupt))
    with _refusal("signature_invalid"):
        _verify(passkey, broken, now=EXPIRES, max_lifetime=43200)


def test_v10_a_passkey_public_key_in_another_form_is_signature_invalid() -> None:
    """Section 2 fixes the 65-byte uncompressed point; the same passkey's key in any other form,
    compressed included, fails V10 with `signature_invalid` and never with another exception."""
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT)
    _verify(passkey, certificate, now=NOW, max_lifetime=43200)
    for _, form in other_point_forms(passkey.credentials[0].private_key):
        with _refusal("signature_invalid"):
            _verify(passkey, certificate, now=NOW, max_lifetime=43200, public_key=form)


def test_verify_certificate_does_not_check_the_counter() -> None:
    passkey = _passkey()
    for counter in (0, 9, 3):
        certificate = _certificate(passkey, STATEMENT, settings=Settings(counter=counter))
        assert _verify(passkey, certificate, now=NOW, max_lifetime=43200).sign_count == counter


def test_certificate_json_form() -> None:
    passkey = _passkey()
    certificate = _certificate(passkey, STATEMENT)
    assert certificate.to_json() == {
        "statement": b64(STATEMENT),
        "credential_id": b64(certificate.credential_id),
        "authenticator_data": b64(certificate.authenticator_data),
        "client_data_json": b64(certificate.client_data_json),
        "signature": b64(certificate.signature),
    }
    assert list(certificate.to_json()) == [
        "statement", "credential_id", "authenticator_data", "client_data_json", "signature",
    ]
    assert b64(certificate.signature) not in repr(certificate)
