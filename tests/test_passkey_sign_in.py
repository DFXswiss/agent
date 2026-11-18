"""The hub, sign-in and the certificate (PASSKEY-PROTOCOL.md section 13.3), through the application
with a fixed clock and the software authenticator. `verify_certificate` called directly (V1 and
V11) is in `tests/test_passkey_certificate.py`; the loader rule for `expires` on a `staff` entry is
in `tests/test_keys_file.py`."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.passkey.encoding import session_key_id
from tests.conftest import HUB_KEY_ID
from tests.passkey_hub import (
    BEGIN,
    FINISH,
    ISSUE_SECONDS,
    NOW,
    ORIGIN,
    RP_ID,
    HubFactory,
    PasskeyHub,
    anonymous_wire,
    b64,
    fmt,
    gather,
    hubs,  # noqa: F401
    pkhub,  # noqa: F401
    unb64,
)
from tests.test_signed_requests import Answer, assert_signed
from tests.webauthn_authenticator import Settings, SoftAuthenticator, corrupt, non_canonical_der, point_of


def refused(answer: Answer, status: int, error: str) -> None:
    assert answer.status == status
    assert answer.json() == {"error": error}


def counter(hub: PasskeyHub, passkey_id: str) -> int | None:
    rows = hub.rows("SELECT sign_count FROM passkey_counter WHERE passkey_id = ?", (passkey_id,))
    return rows[0]["sign_count"] if rows else None


# --- a successful sign-in ----------------------------------------------------------------------------


def test_a_successful_sign_in_stores_the_session_and_whoami_answers_it(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    begun, private = pkhub.begin()
    assert begun.status == 200
    began = begun.json()
    statement = began["statement"].encode("utf-8")
    lines = began["statement"].split("\n")
    assert lines[0] == "agent-session-v1" and lines[1] == ORIGIN and lines[2] == b64(point_of(private))
    assert len(unb64(lines[3])) == 32 and lines[4] == fmt(NOW + ISSUE_SECONDS)
    assert began == {
        "sign_in": began["sign_in"],
        "statement": began["statement"],
        "challenge": b64(hashlib.sha256(statement).digest()),
        "rp_id": RP_ID,
        "timeout_ms": 300000,
    }
    assert len(began["sign_in"]) == 22
    passkey = hubs.passkeys["staff"]
    assertion = pkhub.assertion(passkey, begun)
    finish = pkhub.post(FINISH, assertion)
    expected_id = session_key_id(statement)
    assert finish.status == 200
    assert finish.json() == {
        "session_key": expected_id,
        "passkey": "staff-passkey-1",
        "handle": "example-staff",
        "expires": fmt(NOW + ISSUE_SECONDS),
    }
    (row,) = pkhub.rows("SELECT * FROM passkey_session")
    credential = passkey.credentials[0]
    assert row == {
        "id": expected_id,
        "passkey_id": "staff-passkey-1",
        "handle": "example-staff",
        "credential_id": b64(credential.credential_id),
        "passkey_public_key": b64(credential.public_key),
        "session_public_key": b64(point_of(private)),
        "statement": b64(statement),
        "authenticator_data": assertion["authenticator_data"],
        "client_data_json": assertion["client_data_json"],
        "signature": assertion["signature"],
        "created": NOW,
        "expires": NOW + ISSUE_SECONDS,
        "ended": None,
    }
    assert pkhub.rows("SELECT * FROM passkey_sign_in") == []
    from tests.passkey_hub import Session

    session = Session(id=expected_id, private=private, public=point_of(private), answer=finish.json())
    wire = pkhub.session_wire(session)
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == json.dumps(
        {
            "key": expected_id,
            "kind": "session",
            "principal": "staff",
            "handle": "example-staff",
            "team": "example",
            "scopes": ["record:read", "record:write"],
            "hub_key": HUB_KEY_ID,
            "passkey": "staff-passkey-1",
            "expires": fmt(NOW + ISSUE_SECONDS),
        },
        separators=(",", ":"),
    ).encode()


def test_a_user_handle_of_null_is_accepted(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    begun, _ = pkhub.begin()
    assertion = pkhub.assertion(hubs.passkeys["staff"], begun)
    assertion["user_handle"] = None
    assert pkhub.post(FINISH, assertion).status == 200


# --- begin (section 6.1) ------------------------------------------------------------------------------


def _key(seed: int) -> str:
    return b64(point_of(ec.derive_private_key(seed, ec.SECP256R1())))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session_public_key": _key(5), "x": 1},
        {"session_public_key": _key(5) + "="},
        {"session_public_key": 5},
        {"session_public_key": b64(b"\x04" + bytes(64))},
        {"session_public_key": b64(point_of(ec.derive_private_key(5, ec.SECP256R1()))[:33])},
        ["session_public_key"],
    ],
)
def test_begin_refuses_a_missing_extra_or_malformed_field_and_an_invalid_point(pkhub: PasskeyHub, body: Any) -> None:  # noqa: F811
    refused(pkhub.post(BEGIN, body), 400, "request_invalid")
    assert pkhub.rows("SELECT id FROM passkey_sign_in") == []


def test_begin_refuses_the_key_of_a_stored_session_and_of_a_pending_sign_in(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = pkhub.sign_in(hubs.passkeys["staff"])
    refused(pkhub.post(BEGIN, {"session_public_key": b64(session.public)}), 409, "session_key_known")
    pending = {"session_public_key": _key(6)}
    assert pkhub.post(BEGIN, pending).status == 200
    pkhub.clock.now = NOW + 300
    refused(pkhub.post(BEGIN, pending), 409, "session_key_known")


def test_begin_with_the_key_of_a_pending_sign_in_older_than_300_seconds_is_accepted(pkhub: PasskeyHub) -> None:  # noqa: F811
    pending = {"session_public_key": _key(6)}
    assert pkhub.post(BEGIN, pending).status == 200
    pkhub.clock.now = NOW + 301
    assert pkhub.post(BEGIN, pending).status == 200
    assert pkhub.rows("SELECT created FROM passkey_sign_in") == [{"created": NOW + 301}]


def _fill_pending(hub: PasskeyHub, count: int, created: int) -> None:
    for number in range(count):
        hub.store.execute(
            "INSERT INTO passkey_sign_in (id, statement, session_public_key, created) VALUES (?, ?, ?, ?)",
            (f"pending-{created}-{number}", b64(b"statement"), f"key-{created}-{number}", created),
        )


def test_too_many_pending_at_the_limit(pkhub: PasskeyHub) -> None:  # noqa: F811
    _fill_pending(pkhub, 1000, NOW)
    refused(pkhub.post(BEGIN, {"session_public_key": _key(7)}), 429, "too_many_pending")


def test_pending_sign_ins_older_than_300_seconds_are_deleted_also_when_the_begin_is_refused(pkhub: PasskeyHub) -> None:  # noqa: F811
    _fill_pending(pkhub, 3, NOW - 301)
    _fill_pending(pkhub, 1000, NOW - 300)
    refused(pkhub.post(BEGIN, {"session_public_key": _key(7)}), 429, "too_many_pending")
    assert pkhub.rows("SELECT COUNT(*) AS n FROM passkey_sign_in WHERE created = ?", (NOW - 301,)) == [{"n": 0}]
    assert pkhub.rows("SELECT COUNT(*) AS n FROM passkey_sign_in") == [{"n": 1000}]


def test_two_parallel_begins_with_999_pending_sign_ins_exactly_one_is_accepted(pkhub: PasskeyHub) -> None:  # noqa: F811
    _fill_pending(pkhub, 999, NOW)
    wires = [anonymous_wire(BEGIN, json.dumps({"session_public_key": _key(seed)}).encode()) for seed in (8, 9)]
    answers = gather(pkhub, *wires)
    assert sorted(answer.status for answer in answers) == [200, 429]
    assert pkhub.rows("SELECT COUNT(*) AS n FROM passkey_sign_in") == [{"n": 1000}]


# --- finish (section 6.3), S1 to S7 in their order ------------------------------------------------


def _assertion(hub: PasskeyHub, passkey: SoftAuthenticator, settings: Settings | None = None) -> dict[str, Any]:
    begun, _ = hub.begin()
    return hub.assertion(passkey, begun, settings)


@pytest.mark.parametrize(
    "change",
    [
        lambda body: b"not json",
        lambda body: json.dumps({key: value for key, value in body.items() if key != "signature"}).encode(),
        lambda body: json.dumps({key: value for key, value in body.items() if key != "user_handle"}).encode(),
        lambda body: json.dumps({**body, "x": "y"}).encode(),
        lambda body: json.dumps({**body, "signature": body["signature"] + "="}).encode(),
        lambda body: json.dumps({**body, "user_handle": "a="}).encode(),
        lambda body: json.dumps({**body, "sign_in": None}).encode(),
        lambda body: json.dumps(body).encode() + b" " * 65537,
    ],
)
def test_finish_s1_refuses_a_malformed_body(
    pkhub: PasskeyHub, hubs: HubFactory, change: Callable[[dict[str, Any]], bytes]  # noqa: F811
) -> None:
    body = _assertion(pkhub, hubs.passkeys["staff"])
    refused(pkhub.post(FINISH, change(body)), 400, "request_invalid")
    assert len(pkhub.rows("SELECT id FROM passkey_sign_in")) == 1


def test_finish_s2_refuses_a_reused_challenge(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    body = _assertion(pkhub, hubs.passkeys["staff"])
    assert pkhub.post(FINISH, body).status == 200
    refused(pkhub.post(FINISH, body), 401, "challenge_unknown")


def test_finish_s2_consumes_the_sign_in_also_when_a_later_check_refuses(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    body = _assertion(pkhub, hubs.passkeys["staff"], Settings(uv=False))
    refused(pkhub.post(FINISH, body), 401, "user_verification_missing")
    assert pkhub.rows("SELECT id FROM passkey_sign_in") == []


def test_finish_301_seconds_after_begin_without_a_begin_in_between_is_challenge_expired(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    body = _assertion(pkhub, hubs.passkeys["staff"])
    pkhub.clock.now = NOW + 301
    refused(pkhub.post(FINISH, body), 401, "challenge_expired")


def test_finish_exactly_300_seconds_after_begin_is_accepted(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    body = _assertion(pkhub, hubs.passkeys["staff"])
    pkhub.clock.now = NOW + 300
    assert pkhub.post(FINISH, body).status == 200


def test_finish_301_seconds_after_begin_with_a_begin_in_between_is_challenge_unknown(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    body = _assertion(pkhub, hubs.passkeys["staff"])
    pkhub.clock.now = NOW + 301
    assert pkhub.post(BEGIN, {"session_public_key": _key(11)}).status == 200
    refused(pkhub.post(FINISH, body), 401, "challenge_unknown")


def test_finish_s4_refuses_an_unknown_credential(pkhub: PasskeyHub) -> None:  # noqa: F811
    stranger = SoftAuthenticator()
    stranger.create({"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": b64(bytes(32))}, ORIGIN)
    refused(pkhub.post(FINISH, _assertion(pkhub, stranger)), 401, "credential_unknown")


def test_finish_s5_refuses_a_revoked_entry_and_accepts_one_revoked_in_the_future(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    refused(pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["revoked"])), 401, "key_revoked")
    assert pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["later"])).status == 200


S6_REFUSALS: dict[str, tuple[Settings, str]] = {
    "type webauthn.create": (Settings(client_type="webauthn.create"), "client_data_invalid"),
    "the challenge of another statement": (
        Settings(challenge=b64(hashlib.sha256(b"agent-session-v1\nanother statement").digest())),
        "challenge_mismatch",
    ),
    "a wrong origin": (Settings(origin="http://other.example"), "origin_mismatch"),
    "crossOrigin true": (Settings(cross_origin=True), "origin_mismatch"),
    "a topOrigin": (Settings(top_origin=ORIGIN), "origin_mismatch"),
    "authenticator data of 36 bytes": (Settings(authenticator_data_length=36), "authenticator_data_invalid"),
    "a wrong rp id": (Settings(rp_id="other.example"), "rp_id_mismatch"),
    "UP not set": (Settings(up=False), "user_presence_missing"),
    "UV not set": (Settings(uv=False), "user_verification_missing"),
    "BS without BE": (Settings(bs=True), "authenticator_data_invalid"),
    "AT set": (Settings(at=True), "authenticator_data_invalid"),
    "trailing bytes without ED": (Settings(authenticator_data_suffix=b"\x00"), "authenticator_data_invalid"),
    "a wrong signature": (Settings(signature=corrupt), "signature_invalid"),
    "a signature that is not canonical DER": (Settings(signature=non_canonical_der), "signature_invalid"),
}


@pytest.mark.parametrize("case", sorted(S6_REFUSALS))
def test_finish_s6_refuses_each_step_v2_to_v10(pkhub: PasskeyHub, hubs: HubFactory, case: str) -> None:  # noqa: F811
    settings, error = S6_REFUSALS[case]
    refused(pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["staff"], settings)), 401, error)
    assert pkhub.rows("SELECT id FROM passkey_session") == []


def test_a_synced_passkey_with_be_and_bs_and_an_extensions_map_is_accepted(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    assert pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["staff"], Settings(be=True, bs=True))).status == 200
    assert pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["staff"], Settings(ed=True))).status == 200


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        (Settings(client_type="webauthn.create", origin="http://other.example"), "client_data_invalid"),
        (Settings(origin="http://other.example", rp_id="other.example"), "origin_mismatch"),
        (Settings(rp_id="other.example", up=False), "rp_id_mismatch"),
        (Settings(uv=False, signature=corrupt), "user_verification_missing"),
    ],
)
def test_the_steps_of_s6_stop_at_the_first_failure(
    pkhub: PasskeyHub, hubs: HubFactory, settings: Settings, error: str  # noqa: F811
) -> None:
    refused(pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["staff"], settings)), 401, error)


def test_s3_runs_before_s4_and_s4_before_s6(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    stranger = SoftAuthenticator()
    stranger.create({"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": b64(bytes(32))}, ORIGIN)
    late = _assertion(pkhub, stranger, Settings(uv=False))
    pkhub.clock.now = NOW + 301
    refused(pkhub.post(FINISH, late), 401, "challenge_expired")
    refused(pkhub.post(FINISH, _assertion(pkhub, stranger, Settings(uv=False))), 401, "credential_unknown")
    refused(pkhub.post(FINISH, _assertion(pkhub, hubs.passkeys["revoked"], Settings(uv=False))), 401, "key_revoked")


# --- the counter (section 4.4, S7) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "received", "accepted"),
    [(0, 0, True), (5, 6, True), (6, 6, False), (6, 3, False), (6, 0, False)],
)
def test_the_counter(pkhub: PasskeyHub, hubs: HubFactory, stored: int, received: int, accepted: bool) -> None:  # noqa: F811
    passkey = hubs.passkeys["staff"]
    assert pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=stored))).status == 200
    answer = pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=received)))
    if accepted:
        assert answer.status == 200
        assert counter(pkhub, "staff-passkey-1") == received
    else:
        refused(answer, 401, "counter_regressed")
        assert counter(pkhub, "staff-passkey-1") == stored
        assert len(pkhub.rows("SELECT id FROM passkey_session")) == 1


def test_a_sign_in_refused_at_s6_leaves_the_counter_unchanged(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    passkey = hubs.passkeys["staff"]
    assert pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=5))).status == 200
    refused(pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=9, uv=False))), 401, "user_verification_missing")
    assert counter(pkhub, "staff-passkey-1") == 5


def test_a_failed_session_insert_gives_a_500_no_session_and_an_unchanged_counter(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    passkey = hubs.passkeys["staff"]
    assert pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=5))).status == 200
    pkhub.store.execute(
        "CREATE TRIGGER session_refuse BEFORE INSERT ON passkey_session "
        "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
    )
    body = _assertion(pkhub, passkey, Settings(counter=6))
    refused(pkhub.post(FINISH, body), 500, "internal")
    assert counter(pkhub, "staff-passkey-1") == 5
    assert len(pkhub.rows("SELECT id FROM passkey_session")) == 1
    assert pkhub.rows("SELECT id FROM passkey_sign_in") == []


def test_two_parallel_sign_ins_with_stored_5_that_both_send_6_exactly_one_is_accepted(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    passkey = hubs.passkeys["staff"]
    assert pkhub.post(FINISH, _assertion(pkhub, passkey, Settings(counter=5))).status == 200
    bodies = [_assertion(pkhub, passkey, Settings(counter=6)) for _ in range(2)]
    answers = gather(pkhub, *(anonymous_wire(FINISH, json.dumps(body).encode()) for body in bodies))
    assert sorted(answer.status for answer in answers) == [200, 401]
    assert next(answer for answer in answers if answer.status == 401).json() == {"error": "counter_regressed"}
    assert counter(pkhub, "staff-passkey-1") == 6
