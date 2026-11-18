"""The shared test vectors `tests/vectors/passkey.json` (PASSKEY-PROTOCOL.md section 13.10).

Every vector runs through the production code of `agent_core.passkey` and `agent_core.signing`;
the expected results in the file come from `tests/passkey_vectors.py`, which writes them from the
specification and never from the library. The first test fails when the file and its generator
disagree; regenerate the file with `python -m tests.passkey_vectors`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_core.passkey.certificate import Certificate, build_statement, statement_challenge, verify_certificate
from agent_core.passkey.command import Command, command_text, content_matches, verify_command_signature
from agent_core.passkey.encoding import fingerprint, session_key_id, verify_session_signature
from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.relying_party import RelyingParty, relying_party
from agent_core.passkey.webauthn import check_counter, verify_registration
from agent_core.signing import parse_time, request_text
from tests.passkey_vectors import VECTORS_PATH, generate, render, unb64

OK = "ok"


def _vectors() -> dict[str, Any]:
    if not VECTORS_PATH.exists():
        pytest.fail(f"{VECTORS_PATH} is missing: run python -m tests.passkey_vectors", pytrace=False)
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _outcome(run: Any) -> str:
    try:
        run()
    except PasskeyRefusal as refusal:
        return refusal.error
    return OK


def _seconds(text: str) -> int:
    value = parse_time(text)
    assert value is not None
    return value


def _certificate(data: dict[str, str]) -> Certificate:
    return Certificate(
        statement=unb64(data["statement"]),
        credential_id=unb64(data["credential_id"]),
        authenticator_data=unb64(data["authenticator_data"]),
        client_data_json=unb64(data["client_data_json"]),
        signature=unb64(data["signature"]),
    )


def test_vectors_file_is_what_the_generator_writes() -> None:
    _vectors()
    text = VECTORS_PATH.read_text(encoding="utf-8")
    assert render(generate(json.loads(text))) == text, (
        "tests/vectors/passkey.json and tests/passkey_vectors.py disagree: run python -m tests.passkey_vectors"
    )


def test_relying_party_vector() -> None:
    party = _vectors()["relying_party"]
    assert relying_party(party["public_url"]) == RelyingParty(origin=party["origin"], rp_id=party["rp_id"])


def test_fingerprint_vectors() -> None:
    vectors = _vectors()["fingerprints"]
    assert vectors
    for vector in vectors:
        assert fingerprint(unb64(vector["public_key"])) == vector["fingerprint"]


def test_statement_vectors() -> None:
    for vector in _vectors()["statements"]:
        statement = build_statement(
            origin=vector["origin"],
            session_public_key=unb64(vector["session_public_key"]),
            hub_value=unb64(vector["hub_value"]),
            expires=_seconds(vector["expires"]),
        )
        assert statement == vector["statement"].encode("utf-8")
        assert statement_challenge(statement) == unb64(vector["challenge"])
        assert session_key_id(statement) == vector["session_key_id"]


def test_request_text_vectors() -> None:
    for vector in _vectors()["request_texts"]:
        text = request_text(
            vector["key_id"],
            vector["method"],
            vector["host"],
            vector["target"],
            vector["time"],
            vector["nonce"],
            vector["body_hash"],
        )
        assert text == vector["text"].encode("utf-8")


def test_command_text_vectors() -> None:
    results = set()
    for vector in _vectors()["command_texts"]:
        command = Command(
            sender=vector["sender"],
            target=vector["target"],
            action=vector["action"],
            sequence=vector["sequence"],
            time=vector["time"],
            content_hash=vector["content_hash"],
        )
        assert _outcome(lambda: command_text(command)) == vector["result"], vector
        if vector["result"] == OK:
            assert command_text(command) == vector["text"].encode("utf-8")
        results.add(vector["result"])
    assert results == {OK, "command_invalid"}


def test_session_signature_vectors() -> None:
    for vector in _vectors()["session_signatures"]:
        valid = verify_session_signature(
            unb64(vector["public_key"]), unb64(vector["signature"]), vector["text"].encode("utf-8")
        )
        assert (OK if valid else "signature_invalid") == vector["result"], vector["name"]


def test_counter_vectors() -> None:
    for vector in _vectors()["counters"]:
        assert _outcome(lambda: check_counter(vector["stored"], vector["received"])) == vector["result"], vector


def test_registration_vectors() -> None:
    results = set()
    for vector in _vectors()["registrations"]:
        party = RelyingParty(origin=vector["origin"], rp_id=vector["rp_id"])

        def run() -> Any:
            return verify_registration(
                credential_id=unb64(vector["credential_id"]),
                client_data_json=unb64(vector["client_data_json"]),
                attestation_object=unb64(vector["attestation_object"]),
                challenge=unb64(vector["challenge"]),
                relying_party=party,
            )

        assert _outcome(run) == vector["result"], vector["name"]
        if vector["result"] == OK:
            registration = run()
            assert registration.public_key == unb64(vector["public_key"]), vector["name"]
            assert registration.sign_count == vector["sign_count"], vector["name"]
        results.add(vector["result"])
    assert results == {
        OK,
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


def _verify(vector: dict[str, Any]) -> Any:
    return verify_certificate(
        _certificate(vector["certificate"]),
        passkey_public_key=unb64(vector["passkey_public_key"]),
        relying_party=RelyingParty(origin=vector["origin"], rp_id=vector["rp_id"]),
        now=_seconds(vector["now"]),
        max_lifetime=vector["max_lifetime"],
    )


def test_certificate_vectors() -> None:
    results = set()
    for vector in _vectors()["certificates"]:
        assert _outcome(lambda: _verify(vector)) == vector["result"], vector["name"]
        if vector["result"] == OK:
            session = _verify(vector)
            assert session.session_key_id == vector["session_key_id"], vector["name"]
            assert session.statement.session_public_key == unb64(vector["session_public_key"]), vector["name"]
            assert session.statement.expires == _seconds(vector["expires"]), vector["name"]
            assert session.sign_count == vector["sign_count"], vector["name"]
        results.add(vector["result"])
    assert results == {
        OK,
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


def _device_checks_2_to_6(vector: dict[str, Any]) -> str:
    """Device checks 2 to 6 of section 9, composed of the library's functions as a device would."""
    bundle = vector["bundle"]
    now = _seconds(vector["now"])
    try:
        session = verify_certificate(
            _certificate(bundle["certificate"]),
            passkey_public_key=unb64(vector["passkey_public_key"]),
            relying_party=RelyingParty(origin=vector["origin"], rp_id=vector["rp_id"]),
            now=now,
            max_lifetime=vector["max_lifetime"],
        )
    except PasskeyRefusal as refusal:
        return f"check_2:{refusal.error}"
    if bundle["sender"] != session.session_key_id:
        return "check_3"
    fields = bundle["command"]
    command = Command(
        sender=bundle["sender"],
        target=fields["target"],
        action=fields["action"],
        sequence=fields["sequence"],
        time=fields["time"],
        content_hash=fields["content_hash"],
    )
    try:
        verify_command_signature(command, unb64(bundle["signature"]), session.statement.session_public_key)
    except PasskeyRefusal:
        return "check_4"
    if not content_matches(command.content_hash, bundle["content"], bundle["content_random"]):
        return "check_5"
    last = vector["last_sequence"]
    if (
        command.target != vector["device_key_id"]
        or abs(_seconds(command.time) - now) > vector["time_window"]
        or (last is not None and command.sequence <= last)
    ):
        return "check_6"
    return OK


def test_bundle_vectors() -> None:
    results = set()
    for vector in _vectors()["bundles"]:
        assert _device_checks_2_to_6(vector) == vector["result"], vector["name"]
        results.add(vector["result"].split(":")[0])
    assert results == {OK, "check_2", "check_3", "check_4", "check_5", "check_6"}
