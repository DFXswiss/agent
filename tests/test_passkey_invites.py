"""The hub, invites (PASSKEY-PROTOCOL.md section 13.2), through the application with a fixed clock
and the software authenticator."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import replace
from typing import Any, Callable

import pytest
import yaml

from agent_core.passkey.encoding import fingerprint, verify_session_signature
from agent_core.passkey.invites import _yaml_text
from agent_core.signing import request_text
from tests.conftest import HUB_KEY_ID
from tests.passkey_hub import (
    ANONYMOUS_ROUTES,
    BEGIN,
    FINISH,
    INVITES,
    NOW,
    OPEN,
    ORIGIN,
    REGISTER,
    HubFactory,
    PasskeyHub,
    b64,
    fmt,
    gather,
    hubs,  # noqa: F401
    new_passkey,
    pkhub,  # noqa: F401
    staff_entry,
    twin,
    unb64,
)
from tests.test_signed_requests import Answer, assert_signed, assert_unsigned, log_lines  # noqa: F401
from tests.webauthn_authenticator import (
    BIGNUM,
    FLOAT_MINUS_7,
    FLOAT_ONE,
    NULL,
    SIMPLE_VALUE,
    TRUE,
    UNKNOWN_TAG,
    Settings,
    SoftAuthenticator,
    attestation_pairs,
    cbor,
    cbor_map,
    cose_key_pairs,
    point_of,
    private_key_from,
    with_key,
    with_pair,
    with_value,
)

INVITE_BODY = b'{"handle":"example-staff","team":"example","lifetime_seconds":3600}'


def refused(answer: Answer, status: int, error: str) -> None:
    assert answer.status == status
    assert answer.json() == {"error": error}


def admin(hub: PasskeyHub, hubs: HubFactory) -> Any:  # noqa: F811
    return hub.sign_in(hubs.passkeys["admin"])


def opened_invite(hub: PasskeyHub, session: Any, **options: Any) -> tuple[dict[str, Any], Answer]:
    invite = hub.create_invite(session, **options)
    opened = hub.open(invite["link"])
    assert opened.status == 200, opened.body
    return invite, opened


def stranger() -> SoftAuthenticator:
    """An authenticator with fresh system randomness, whose credential is in no file."""
    return SoftAuthenticator()


def status_of(hub: PasskeyHub, session: Any, invite_id: str) -> str:
    return next(item["status"] for item in hub.invites(session) if item["invite"] == invite_id)


def insert_invite(
    hub: PasskeyHub,
    invite_id: str,
    *,
    created: int = NOW,
    expires: int = NOW + 3600,
    opened: int | None = None,
    finished: int | None = None,
    received: int | None = None,
    handle: str = "example-staff",
    bootstrap: int = 0,
) -> None:
    hub.store.execute(
        "INSERT INTO passkey_invite (id, token_hash, handle, team, created, expires, created_by, bootstrap, "
        "creation_request, opened, received, credential_id, public_key, proposed_id) "
        "VALUES (?, ?, ?, 'example', ?, ?, 'operator', ?, NULL, ?, ?, ?, ?, ?)",
        (
            invite_id,
            "hash-" + invite_id,
            handle,
            created,
            expires,
            bootstrap,
            opened,
            received,
            None if received is None else b64(invite_id.encode()),
            None if received is None else b64(point_of(private_key_from(lambda size: bytes([7]) * size))),
            None if received is None else f"{handle}-passkey-x",
        ),
    )
    if opened is not None:
        hub.store.execute(
            "INSERT INTO passkey_registration (id, invite_id, challenge, user_id, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("reg-" + invite_id, invite_id, b64(bytes(32)), b64(bytes(16)), opened, finished),
        )


def invite_id_of(number: int) -> str:
    return f"invite.{number:032x}"


# --- creating an invite (section 5.1) -------------------------------------------------------------


def test_an_administrators_session_key_creates_an_invite_with_a_signed_201(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    session = admin(pkhub, hubs)
    wire = pkhub.session_wire(session, method="POST", target=INVITES, body=INVITE_BODY)
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 201, NOW)
    body = answer.json()
    token = body["link"].split("#", 1)[1]
    assert re.fullmatch(r"invite\.[0-9a-f]{32}", body["invite"])
    assert len(token) == 43 and len(unb64(token)) == 32
    assert body == {
        "invite": body["invite"],
        "handle": "example-staff",
        "team": "example",
        "created": fmt(NOW),
        "expires": fmt(NOW + 3600),
        "link": f"{ORIGIN}/invite#{token}",
        "hub_key": HUB_KEY_ID,
    }
    (row,) = pkhub.rows("SELECT * FROM passkey_invite")
    assert row["token_hash"] == b64(hashlib.sha256(unb64(token)).digest())
    assert row["created_by"] == session.id and row["bootstrap"] == 0
    assert token not in json.dumps(pkhub.rows("SELECT * FROM passkey_invite"))
    assert json.loads(row["creation_request"]) == {
        "method": "POST",
        "target": INVITES,
        "host": "testserver",
        "headers": {
            "Agent-Key": session.id,
            "Agent-Time": wire.time,
            "Agent-Nonce": wire.nonce,
            "Agent-Signature": wire.value(b"agent-signature").decode("ascii"),
        },
        "body": b64(INVITE_BODY),
    }


def test_a_gzip_creation_request_is_stored_decoded_and_verifies_with_the_session_row(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    session = admin(pkhub, hubs)
    wire = pkhub.session_wire(session, method="POST", target=INVITES, body=INVITE_BODY)
    coded = replace(wire, body=gzip.compress(INVITE_BODY, mtime=0), headers=[*wire.headers, (b"content-encoding", b"gzip")])
    assert_signed(coded, pkhub.send(coded), 201, NOW)
    (row,) = pkhub.rows("SELECT creation_request FROM passkey_invite")
    stored = json.loads(row["creation_request"])
    assert unb64(stored["body"]) == INVITE_BODY
    (signer,) = pkhub.rows("SELECT * FROM passkey_session WHERE id = ?", (stored["headers"]["Agent-Key"],))
    text = request_text(
        stored["headers"]["Agent-Key"],
        stored["method"],
        stored["host"],
        stored["target"],
        stored["headers"]["Agent-Time"],
        stored["headers"]["Agent-Nonce"],
        hashlib.sha256(unb64(stored["body"])).hexdigest(),
    )
    assert verify_session_signature(unb64(signer["session_public_key"]), unb64(stored["headers"]["Agent-Signature"]), text)
    # The session row keeps the certificate of the signer (section 5.1).
    assert signer["statement"] and signer["signature"] and signer["authenticator_data"] and signer["client_data_json"]


@pytest.mark.parametrize("passkey", ["reader", "staff"])
def test_a_session_key_whose_passkey_lacks_keys_invite_gets_the_signed_scope_missing(
    pkhub: PasskeyHub, hubs: HubFactory, passkey: str  # noqa: F811
) -> None:
    session = pkhub.sign_in(hubs.passkeys[passkey])
    for method, body in (("POST", INVITE_BODY), ("GET", b"")):
        wire = pkhub.session_wire(session, method=method, target=INVITES, body=body)
        answer = pkhub.send(wire)
        assert_signed(wire, answer, 403, NOW)
        refused(answer, 403, "scope_missing")


@pytest.mark.parametrize("key_id", ["example-staff-laptop-1", "example-guard-1"])
def test_an_agent_and_a_service_key_get_the_signed_scope_missing(pkhub: PasskeyHub, key_id: str) -> None:  # noqa: F811
    wire = pkhub.key_wire(key_id, method="POST", target=INVITES, body=INVITE_BODY)
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 403, NOW)
    refused(answer, 403, "scope_missing")


def test_a_webauthn_key_used_directly_and_a_revoked_key_get_the_unsigned_refusal(pkhub: PasskeyHub) -> None:  # noqa: F811
    from tests.passkey_hub import ed_private, signed_wire

    direct = signed_wire(
        "admin-passkey-1", lambda text: ed_private(99).sign(text), method="POST", target=INVITES, body=INVITE_BODY, time_text=pkhub.time()
    )
    answer = pkhub.send(direct)
    refused(answer, 401, "key_kind_not_allowed")
    assert_unsigned(answer)
    revoked = pkhub.send(pkhub.key_wire("example-staff-old", method="POST", target=INVITES, body=INVITE_BODY))
    refused(revoked, 401, "key_revoked")
    assert_unsigned(revoked)


INVALID_INVITES: dict[str, Any] = {
    "handle missing": {"team": "example", "lifetime_seconds": 3600},
    "team missing": {"handle": "example-staff", "lifetime_seconds": 3600},
    "lifetime missing": {"handle": "example-staff", "team": "example"},
    "an extra field": {"handle": "example-staff", "team": "example", "lifetime_seconds": 3600, "x": 1},
    "a handle with uppercase": {"handle": "Example", "team": "example", "lifetime_seconds": 3600},
    "a handle with --": {"handle": "a--b", "team": "example", "lifetime_seconds": 3600},
    "a handle of 40 characters": {"handle": "a" * 40, "team": "example", "lifetime_seconds": 3600},
    "a handle that is not text": {"handle": 7, "team": "example", "lifetime_seconds": 3600},
    "a handle that is an agent login": {"handle": "helperbot", "team": "example", "lifetime_seconds": 3600},
    "an unknown team": {"handle": "example-staff", "team": "nope", "lifetime_seconds": 3600},
    "a team that is not text": {"handle": "example-staff", "team": ["example"], "lifetime_seconds": 3600},
    "a lifetime of 299": {"handle": "example-staff", "team": "example", "lifetime_seconds": 299},
    "a lifetime of 86401": {"handle": "example-staff", "team": "example", "lifetime_seconds": 86401},
    "a lifetime that is a boolean": {"handle": "example-staff", "team": "example", "lifetime_seconds": True},
    "a lifetime that is a string": {"handle": "example-staff", "team": "example", "lifetime_seconds": "3600"},
    "a lifetime that is a float": {"handle": "example-staff", "team": "example", "lifetime_seconds": 3600.0},
    "an array": ["example-staff", "example", 3600],
    "no JSON": b"handle=example-staff",
    "NaN": b'{"handle":"example-staff","team":"example","lifetime_seconds":NaN}',
}


@pytest.mark.parametrize("case", sorted(INVALID_INVITES))
def test_invite_invalid(pkhub: PasskeyHub, hubs: HubFactory, case: str) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    value = INVALID_INVITES[case]
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    wire = pkhub.session_wire(session, method="POST", target=INVITES, body=body)
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 400, NOW)
    refused(answer, 400, "invite_invalid")
    assert pkhub.rows("SELECT id FROM passkey_invite") == []


@pytest.mark.parametrize("lifetime", [300, 86400])
def test_the_lifetime_bounds_are_accepted(pkhub: PasskeyHub, hubs: HubFactory, lifetime: int) -> None:  # noqa: F811
    invite = pkhub.create_invite(admin(pkhub, hubs), lifetime=lifetime)
    assert invite["expires"] == fmt(NOW + lifetime)


def test_the_administrator_may_invite_their_own_handle(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    assert pkhub.create_invite(admin(pkhub, hubs), handle="example-admin")["handle"] == "example-admin"


def _create(hub: PasskeyHub, session: Any) -> Answer:
    return hub.send(hub.session_wire(session, method="POST", target=INVITES, body=INVITE_BODY))


def test_invite_limit_counts_only_open_invites(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    for number in range(49):
        insert_invite(pkhub, invite_id_of(number))
    insert_invite(pkhub, invite_id_of(100), expires=NOW)  # expired
    insert_invite(pkhub, invite_id_of(101), opened=NOW - 10, finished=NOW - 5)  # failed
    insert_invite(pkhub, invite_id_of(102), opened=NOW - 10, finished=NOW - 5, received=NOW - 5)  # received
    insert_invite(pkhub, invite_id_of(103), opened=NOW - 301)  # opened, its ceremony time passed
    assert _create(pkhub, session).status == 201
    answer = _create(pkhub, session)
    refused(answer, 409, "invite_limit")
    assert len(pkhub.rows("SELECT id FROM passkey_invite")) == 54


def test_an_opened_invite_counts_until_its_ceremony_time_passed(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    for number in range(49):
        insert_invite(pkhub, invite_id_of(number))
    insert_invite(pkhub, invite_id_of(100), opened=NOW - 300)
    refused(_create(pkhub, session), 409, "invite_limit")
    pkhub.clock.now = NOW + 1
    assert _create(pkhub, session).status == 201


def test_two_parallel_creations_with_49_open_invites_exactly_one_succeeds(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    session = admin(pkhub, hubs)
    for number in range(49):
        insert_invite(pkhub, invite_id_of(number))
    first = pkhub.session_wire(session, method="POST", target=INVITES, body=INVITE_BODY)
    second = pkhub.session_wire(session, method="POST", target=INVITES, body=INVITE_BODY)
    answers = gather(pkhub, first, second)
    assert sorted(answer.status for answer in answers) == [201, 409]
    assert next(answer for answer in answers if answer.status == 409).json() == {"error": "invite_limit"}
    assert len(pkhub.rows("SELECT id FROM passkey_invite")) == 50


# --- the four routes without a registered key (decision 47, the JSON rule) -----------------------


def _valid_form_bodies(hub: PasskeyHub) -> dict[str, dict[str, Any]]:
    """For each route a body of the right form, which without the defect is not a 400."""
    key = point_of(private_key_from(lambda size: bytes([9]) * size))
    return {
        OPEN: {"token": b64(bytes(32))},
        REGISTER: {
            "registration": b64(bytes(16)),
            "credential_id": b64(bytes(16)),
            "client_data_json": b64(b"{}"),
            "attestation_object": b64(b"\xa0"),
        },
        BEGIN: {"session_public_key": b64(key)},
        FINISH: {
            "sign_in": b64(bytes(16)),
            "credential_id": b64(bytes(16)),
            "authenticator_data": b64(bytes(37)),
            "client_data_json": b64(b"{}"),
            "signature": b64(bytes(8)),
            "user_handle": None,
        },
    }


@pytest.mark.parametrize("route", ANONYMOUS_ROUTES)
@pytest.mark.parametrize("coding", [b"gzip", b"identity", b""])
def test_any_content_encoding_is_request_invalid(pkhub: PasskeyHub, route: str, coding: bytes) -> None:  # noqa: F811
    body = _valid_form_bodies(pkhub)[route]
    baseline = pkhub.post(route, body)
    assert baseline.status != 400
    refused(pkhub.post(route, body, headers=((b"content-encoding", coding),)), 400, "request_invalid")


@pytest.mark.parametrize("route", ANONYMOUS_ROUTES)
@pytest.mark.parametrize("defect", ["NaN", "Infinity", "-Infinity", "a key twice"])
def test_a_constant_or_a_repeated_key_is_request_invalid(pkhub: PasskeyHub, route: str, defect: str) -> None:  # noqa: F811
    body = _valid_form_bodies(pkhub)[route]
    first = next(iter(body))
    text = json.dumps(body)
    if defect == "a key twice":
        raw = text[:-1] + ", " + json.dumps(first) + ": " + json.dumps(body[first]) + "}"
    else:
        raw = text.replace(json.dumps(body[first]), defect, 1)
    refused(pkhub.post(route, raw.encode()), 400, "request_invalid")


@pytest.mark.parametrize("route", ANONYMOUS_ROUTES)
def test_a_body_over_the_limit_is_request_invalid(pkhub: PasskeyHub, route: str) -> None:  # noqa: F811
    raw = json.dumps(_valid_form_bodies(pkhub)[route]).encode()
    # White space after the object keeps it one valid JSON object; only the size breaks the rule.
    assert pkhub.post(route, raw + b" " * (65536 - len(raw))).status != 400
    refused(pkhub.post(route, raw + b" " * (65537 - len(raw))), 400, "request_invalid")


# --- opening an invite (section 5.4) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        json.dumps({"token": b64(bytes(32)), "x": 1}).encode(),
        json.dumps({"token": b64(bytes(31))}).encode(),
        json.dumps({"token": b64(bytes(32)) + "="}).encode(),
        json.dumps({"token": 7}).encode(),
        b"\xff",
    ],
)
def test_open_refuses_a_malformed_body(pkhub: PasskeyHub, body: bytes) -> None:  # noqa: F811
    refused(pkhub.post(OPEN, body), 400, "request_invalid")


def test_open_refuses_an_unknown_token(pkhub: PasskeyHub) -> None:  # noqa: F811
    refused(pkhub.post(OPEN, {"token": b64(bytes(32))}), 401, "invite_unknown")


@pytest.mark.parametrize("offset", [0, 1])
def test_open_at_and_after_the_expiry_is_invite_expired_and_not_marked_opened(
    pkhub: PasskeyHub, hubs: HubFactory, offset: int  # noqa: F811
) -> None:
    invite = pkhub.create_invite(admin(pkhub, hubs), lifetime=300)
    pkhub.clock.now = NOW + 300 + offset
    refused(pkhub.open(invite["link"]), 401, "invite_expired")
    assert pkhub.rows("SELECT opened FROM passkey_invite") == [{"opened": None}]
    assert pkhub.rows("SELECT id FROM passkey_registration") == []


def test_a_second_open_is_invite_used(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    invite, _ = opened_invite(pkhub, admin(pkhub, hubs))
    refused(pkhub.open(invite["link"]), 401, "invite_used")


def test_two_parallel_opens_of_one_invite_exactly_one_succeeds(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    from tests.passkey_hub import anonymous_wire

    invite = pkhub.create_invite(admin(pkhub, hubs))
    body = json.dumps({"token": invite["link"].split("#", 1)[1]}).encode()
    answers = gather(pkhub, anonymous_wire(OPEN, body), anonymous_wire(OPEN, body))
    assert sorted(answer.status for answer in answers) == [200, 401]
    assert next(answer for answer in answers if answer.status == 401).json() == {"error": "invite_used"}
    assert len(pkhub.rows("SELECT id FROM passkey_registration")) == 1


def test_a_failed_registration_insert_leaves_the_invite_not_opened(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    invite = pkhub.create_invite(admin(pkhub, hubs))
    pkhub.store.execute(
        "CREATE TRIGGER registration_refuse BEFORE INSERT ON passkey_registration "
        "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
    )
    refused(pkhub.open(invite["link"]), 500, "internal")
    assert pkhub.rows("SELECT opened FROM passkey_invite") == [{"opened": None}]
    pkhub.store.execute("DROP TRIGGER registration_refuse")
    assert pkhub.open(invite["link"]).status == 200


def test_open_answers_the_registration_options(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    body = opened.json()
    (registration,) = pkhub.rows("SELECT * FROM passkey_registration")
    assert body == {
        "registration": registration["id"],
        "handle": "example-staff",
        "rp_id": "testserver",
        "rp_name": "agent-core",
        "user_id": registration["user_id"],
        "challenge": registration["challenge"],
        "exclude_credentials": body["exclude_credentials"],
        "timeout_ms": 300000,
    }
    assert len(unb64(body["user_id"])) == 16 and len(unb64(body["challenge"])) == 32
    assert len(registration["id"]) == 22 and len(unb64(registration["id"])) == 16


def test_exclude_credentials_lists_the_handles_credentials_revoked_ones_included(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    _, opened = opened_invite(pkhub, admin(pkhub, hubs), handle="other-staff")
    expected = [b64(hubs.passkeys[name].credentials[0].credential_id) for name in ("reader", "revoked")]
    assert opened.json()["exclude_credentials"] == expected
    _, nobody = opened_invite(pkhub, admin(pkhub, hubs), handle="new-person")
    assert nobody.json()["exclude_credentials"] == []


# --- registration (section 5.5) --------------------------------------------------------------------


def _attestation(change: Callable[[list[tuple[bytes, bytes]]], list[tuple[bytes, bytes]]], **options: Any) -> Settings:
    return Settings(attestation_object=lambda data: cbor_map(change(attestation_pairs(data)), width=options.get("width")))


def _att_stmt(raw: bytes) -> Settings:
    statement = cbor_map([(cbor("x"), raw)], width=None)
    return _attestation(lambda pairs: with_value(pairs, "attStmt", statement))


def _cose(change: Callable[[list[tuple[bytes, bytes]]], list[tuple[bytes, bytes]]], **options: Any) -> Settings:
    return Settings(credential_public_key=lambda point: cbor_map(change(cose_key_pairs(point)), width=options.get("width")))


def _same(pairs: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    return pairs


REGISTRATION_REFUSALS: dict[str, tuple[Settings, dict[str, Any], str]] = {
    "type webauthn.get": (Settings(client_type="webauthn.get"), {}, "client_data_invalid"),
    "client data no JSON": (Settings(client_data_json=lambda raw: b"not json"), {}, "client_data_invalid"),
    "a wrong challenge": (Settings(challenge=b64(bytes(32))), {}, "challenge_mismatch"),
    "a wrong origin": (Settings(origin="http://other.example"), {}, "origin_mismatch"),
    "crossOrigin true": (Settings(cross_origin=True), {}, "origin_mismatch"),
    "a topOrigin": (Settings(top_origin=ORIGIN), {}, "origin_mismatch"),
    "an extra key": (
        Settings(attestation_object=lambda data: cbor({"fmt": "none", "attStmt": {}, "authData": data, "x": 1})),
        {},
        "attestation_invalid",
    ),
    "trailing bytes": (
        Settings(attestation_object=lambda data: cbor_map(attestation_pairs(data), width=None) + b"\x00"),
        {},
        "attestation_invalid",
    ),
    "a key written twice": (_attestation(lambda pairs: with_pair(pairs, cbor("fmt"), cbor("none"))), {}, "attestation_invalid"),
    "a non-canonical length": (_attestation(_same, width=1), {}, "attestation_invalid"),
    "an unknown tag in attStmt": (_att_stmt(UNKNOWN_TAG), {}, "attestation_invalid"),
    "a float in attStmt": (_att_stmt(FLOAT_ONE), {}, "attestation_invalid"),
    "a simple value in attStmt": (_att_stmt(SIMPLE_VALUE), {}, "attestation_invalid"),
    "true in attStmt": (_att_stmt(TRUE), {}, "attestation_invalid"),
    "null in attStmt": (_att_stmt(NULL), {}, "attestation_invalid"),
    "authenticator data of 36 bytes": (Settings(authenticator_data_length=36), {}, "authenticator_data_invalid"),
    "a wrong rp id": (Settings(rp_id="other.example"), {}, "rp_id_mismatch"),
    "UP not set": (Settings(up=False), {}, "user_presence_missing"),
    "UV not set": (Settings(uv=False), {}, "user_verification_missing"),
    "AT not set": (Settings(at=False), {}, "authenticator_data_invalid"),
    "BS without BE": (Settings(bs=True), {}, "authenticator_data_invalid"),
    "trailing bytes without ED": (Settings(authenticator_data_suffix=b"\x00"), {}, "authenticator_data_invalid"),
    "a non-canonical credential public key": (_cose(_same, width=1), {}, "authenticator_data_invalid"),
    "3: -7.0": (_cose(lambda pairs: with_value(pairs, 3, FLOAT_MINUS_7)), {}, "authenticator_data_invalid"),
    "-1: true": (_cose(lambda pairs: with_value(pairs, -1, TRUE)), {}, "authenticator_data_invalid"),
    "the key 1.0": (_cose(lambda pairs: with_key(pairs, 1, FLOAT_ONE)), {}, "authenticator_data_invalid"),
    "a tag": (_cose(lambda pairs: with_value(pairs, -1, UNKNOWN_TAG)), {}, "authenticator_data_invalid"),
    "a bignum": (_cose(lambda pairs: with_value(pairs, -1, BIGNUM)), {}, "authenticator_data_invalid"),
    "an extensions map with a float": (
        Settings(ed=True, extensions=cbor_map([(cbor("x"), FLOAT_ONE)], width=None)),
        {},
        "authenticator_data_invalid",
    ),
    "a credential id other than the body's": (Settings(), {"credential_id": b64(b"another-credential")}, "credential_invalid"),
    "a credential id of 15 bytes": (Settings(credential_id_length=15), {}, "credential_invalid"),
    "a credential id of 1024 bytes": (Settings(credential_id_length=1024), {}, "credential_invalid"),
    "an RS256 key": (
        Settings(credential_public_key=lambda point: cbor({1: 3, 3: -257, -1: b"\x01" * 256, -2: b"\x01\x00\x01"})),
        {},
        "credential_invalid",
    ),
    "an Ed25519 key": (
        Settings(credential_public_key=lambda point: cbor({1: 1, 3: -8, -1: 6, -2: b"\x02" * 32})),
        {},
        "credential_invalid",
    ),
    "a point not on the curve": (
        Settings(credential_public_key=lambda point: cbor_map(cose_key_pairs(point[:64] + bytes([point[64] ^ 1])), width=None)),
        {},
        "credential_invalid",
    ),
    # Order: the first failing check wins.
    "R4 before R5": (Settings(client_type="webauthn.get", challenge=b64(bytes(32))), {}, "client_data_invalid"),
    "R6 before R7": (Settings(origin="http://other.example", attestation_object=lambda data: b"\x00"), {}, "origin_mismatch"),
    "R9 before R10": (Settings(rp_id="other.example", up=False), {}, "rp_id_mismatch"),
    "R11 before R12": (Settings(uv=False, bs=True), {}, "user_verification_missing"),
}


@pytest.mark.parametrize("case", sorted(REGISTRATION_REFUSALS))
def test_each_registration_refusal_r4_to_r13(pkhub: PasskeyHub, hubs: HubFactory, case: str) -> None:  # noqa: F811
    settings, changes, error = REGISTRATION_REFUSALS[case]
    invite, opened = opened_invite(pkhub, admin(pkhub, hubs))
    refused(pkhub.register(stranger(), opened, settings, **changes), 401, error)
    session = admin(pkhub, hubs)
    assert status_of(pkhub, session, invite["invite"]) == "failed"


def test_an_extensions_map_with_a_boolean_value_is_accepted(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    answer = pkhub.register(stranger(), opened, Settings(ed=True, extensions=cbor({"credProps": True})))
    assert answer.status == 200


@pytest.mark.parametrize(
    "change",
    [
        lambda body: b"not json",
        lambda body: json.dumps({key: value for key, value in body.items() if key != "attestation_object"}).encode(),
        lambda body: json.dumps({**body, "x": "y"}).encode(),
        lambda body: json.dumps({**body, "client_data_json": body["client_data_json"] + "="}).encode(),
        lambda body: json.dumps({**body, "registration": 7}).encode(),
        lambda body: json.dumps({**body, "x": "a" * 65536}).encode(),
    ],
)
def test_registration_r1_refuses_a_malformed_body(
    pkhub: PasskeyHub, hubs: HubFactory, change: Callable[[dict[str, Any]], bytes]  # noqa: F811
) -> None:
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    body = stranger().create(opened.json(), ORIGIN).body(opened.json()["registration"])
    refused(pkhub.post(REGISTER, change(body)), 400, "request_invalid")
    assert pkhub.rows("SELECT finished FROM passkey_registration") == [{"finished": None}]


def test_registration_r2_refuses_an_unknown_and_a_reused_registration(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    created = stranger().create(opened.json(), ORIGIN)
    unknown = created.body(b64(bytes(16)))
    refused(pkhub.post(REGISTER, unknown), 401, "registration_unknown")
    body = created.body(opened.json()["registration"])
    assert pkhub.post(REGISTER, body).status == 200
    refused(pkhub.post(REGISTER, body), 401, "registration_unknown")


def test_registration_r3_refuses_a_registration_older_than_300_seconds(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    invite, opened = opened_invite(pkhub, admin(pkhub, hubs))
    pkhub.clock.now = NOW + 301
    refused(pkhub.register(stranger(), opened), 401, "registration_expired")
    pkhub.clock.now = NOW
    assert status_of(pkhub, admin(pkhub, hubs), invite["invite"]) == "failed"


def test_registration_r3_accepts_exactly_300_seconds(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    pkhub.clock.now = NOW + 300
    assert pkhub.register(stranger(), opened).status == 200


def test_registration_r3_refuses_an_invite_that_expired_during_the_ceremony(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    invite = pkhub.create_invite(admin(pkhub, hubs), lifetime=300)
    pkhub.clock.now = NOW + 250
    opened = pkhub.open(invite["link"])
    pkhub.clock.now = NOW + 300
    refused(pkhub.register(stranger(), opened), 401, "registration_expired")


def test_registration_r14_refuses_a_credential_in_the_keys_file(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    refused(pkhub.register(twin(2), opened), 409, "credential_known")


def test_registration_r14_refuses_a_credential_received_by_another_invite(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    _, first = opened_invite(pkhub, session)
    _, second = opened_invite(pkhub, session, handle="other-staff")
    assert pkhub.register(twin(500), first).status == 200
    refused(pkhub.register(twin(500), second), 409, "credential_known")


def test_a_registration_that_ends_with_a_500_after_r2_makes_the_invite_failed(
    pkhub: PasskeyHub, hubs: HubFactory, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import agent_core.passkey.http as routes

    def broken(**_options: Any) -> Any:
        raise RuntimeError("injected after R2")

    invite, opened = opened_invite(pkhub, admin(pkhub, hubs))
    monkeypatch.setattr(routes, "verify_registration", broken)
    refused(pkhub.register(stranger(), opened), 500, "internal")
    monkeypatch.undo()
    assert status_of(pkhub, admin(pkhub, hubs), invite["invite"]) == "failed"


# --- a successful registration and the keys-file text (sections 5.5 and 5.6) -----------------------


def _normal_body(invite_id: str, created_by: str, fingerprint_text: str, received: str) -> str:
    return "\n".join(
        [
            f"Adds the passkey received through invite {invite_id} to keys.yaml. Before merging, the administrator compares the fingerprint below with example-staff over a channel other than the invite link (PASSKEY-PROTOCOL.md section 5.5). An operator compares credential_id and public_key in this pull request with the output of invite-list on the hub host (PASSKEY-PROTOCOL.md section 5.6). Before merging, the administrator confirms that the GitHub account with exactly the login example-staff exists and belongs to the person who created this passkey (PASSKEY-PROTOCOL.md section 5.6); without that confirmation this pull request is not merged.",
            "",
            "- Handle: example-staff",
            "- Team: example",
            f"- Created by: {created_by}",
            f"- Fingerprint: {fingerprint_text}",
            f"- Received: {received}",
            "",
            "While GitHub sign-in exists, every member of a team in teams.yaml is a GitHub login that reads the whole team's data. Only after the confirmation above, and only if example-staff is not yet a member of example in teams.yaml, this pull request adds the membership.",
        ]
    )


def test_a_successful_registration_and_its_keys_file_text(hubs: HubFactory) -> None:  # noqa: F811
    taken = staff_entry("example-staff-passkey-1", "example-staff", new_passkey(40), ["record:read"])
    hub = hubs.build([*hubs.entries(), taken])
    session = admin(hub, hubs)
    invite, opened = opened_invite(hub, session)
    hub.clock.now = NOW + 30
    passkey = stranger()
    answer = hub.register(passkey, opened)
    credential = passkey.credentials[0]
    fingerprint_text = fingerprint(credential.public_key)
    assert answer.status == 200
    assert answer.json() == {"handle": "example-staff", "fingerprint": fingerprint_text}
    assert re.fullmatch(r"([0-9a-f]{4} ){7}[0-9a-f]{4}", fingerprint_text)
    (item,) = hub.invites(session)
    assert item == {
        "invite": invite["invite"],
        "handle": "example-staff",
        "team": "example",
        "created": fmt(NOW),
        "expires": fmt(NOW + 3600),
        "status": "received",
        "created_by": session.id,
        "received": fmt(NOW + 30),
        "credential_id": b64(credential.credential_id),
        "public_key": b64(credential.public_key),
        "fingerprint": fingerprint_text,
        "keys_entry": "\n".join(
            [
                "  - id: example-staff-passkey-2",
                "    kind: webauthn",
                "    principal: staff",
                "    handle: example-staff",
                "    team: example",
                f'    credential_id: "{b64(credential.credential_id)}"',
                f'    public_key: "{b64(credential.public_key)}"',
                "    scopes: [record:read, record:write]",
            ]
        ),
        "pull_request_title": "Add passkey example-staff-passkey-2 for example-staff",
        "pull_request_body": _normal_body(invite["invite"], session.id, fingerprint_text, fmt(NOW + 30)),
    }
    # The text pasted under `keys:` is a list entry the loader reads.
    entry = yaml.safe_load("keys:\n" + item["keys_entry"])["keys"][0]
    assert entry["credential_id"] == b64(credential.credential_id) and entry["scopes"] == ["record:read", "record:write"]


def test_a_normal_invite_writes_no_bootstrap_line_when_opened_or_received(
    pkhub: PasskeyHub, hubs: HubFactory, log_lines: list[str]  # noqa: F811
) -> None:
    # Section 5.3: the lines `invite bootstrap opened` and `invite bootstrap received` are written
    # only for a bootstrap invite.
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    assert pkhub.register(stranger(), opened).status == 200
    assert [line for line in log_lines if "invite bootstrap" in line] == []


def test_the_proposed_id_skips_an_id_proposed_for_another_invite(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    _, first = opened_invite(pkhub, session)
    _, second = opened_invite(pkhub, session)
    assert pkhub.register(stranger(), first).status == 200
    assert pkhub.register(stranger(), second).status == 200
    proposed = sorted(row["proposed_id"] for row in pkhub.rows("SELECT proposed_id FROM passkey_invite"))
    assert proposed == ["example-staff-passkey-1", "example-staff-passkey-2"]


def test_two_parallel_registrations_for_one_handle_get_different_proposed_ids(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    from tests.passkey_hub import anonymous_wire

    session = admin(pkhub, hubs)
    _, first = opened_invite(pkhub, session)
    _, second = opened_invite(pkhub, session)
    wires = [
        anonymous_wire(REGISTER, json.dumps(stranger().create(opened.json(), ORIGIN).body(opened.json()["registration"])).encode())
        for opened in (first, second)
    ]
    assert [answer.status for answer in gather(pkhub, *wires)] == [200, 200]
    proposed = sorted(row["proposed_id"] for row in pkhub.rows("SELECT proposed_id FROM passkey_invite"))
    assert proposed == ["example-staff-passkey-1", "example-staff-passkey-2"]


def test_the_pull_request_body_begins_with_the_four_summary_sentences(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    _, opened = opened_invite(pkhub, session)
    assert pkhub.register(stranger(), opened).status == 200
    body = pkhub.invites(session)[0]["pull_request_body"]
    summary = body.split("\n", 1)[0]
    sentences = re.split(r"(?<=[.;]) (?=[A-Z])", summary)
    assert len(sentences) == 4
    assert sentences[3].startswith("Before merging, the administrator confirms that the GitHub account")
    assert body.index("this pull request adds the membership") > body.index(sentences[3])
    assert "Only after the confirmation above" in body.rsplit("\n", 1)[1]


def test_a_handle_that_yaml_reads_as_no_text_is_quoted_in_the_keys_entry(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    _, opened = opened_invite(pkhub, session, handle="123")
    assert pkhub.register(stranger(), opened).status == 200
    text = pkhub.invites(session)[0]["keys_entry"]
    assert '    handle: "123"' in text
    assert yaml.safe_load("keys:\n" + text)["keys"][0]["handle"] == "123"


@pytest.mark.parametrize(("value", "written"), [("example", "example"), ("on", '"on"'), ("[x", '"[x"'), ("12", '"12"')])
def test_yaml_text_quotes_only_what_yaml_would_not_read_as_that_text(value: str, written: str) -> None:
    assert _yaml_text(value) == written


def test_a_received_key_that_is_not_in_the_keys_file_cannot_sign_in(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    _, opened = opened_invite(pkhub, admin(pkhub, hubs))
    passkey = stranger()
    assert pkhub.register(passkey, opened).status == 200
    begun, _ = pkhub.begin()
    refused(pkhub.post(FINISH, pkhub.assertion(passkey, begun)), 401, "credential_unknown")


# --- the list (section 5.2) --------------------------------------------------------------------------


def test_the_list_is_newest_first_by_the_latest_change_with_every_status(hubs: HubFactory) -> None:  # noqa: F811
    hub = hubs.build()
    session = admin(hub, hubs)
    clock = hub.clock
    failed_invite, failed_opened = opened_invite(hub, session)  # opened at NOW
    clock.now = NOW + 10
    later_invite = hub.create_invite(session)
    clock.now = NOW + 20
    assert hub.open(later_invite["link"]).status == 200  # opened between the two
    clock.now = NOW + 40
    refused(hub.register(stranger(), failed_opened, Settings(uv=False)), 401, "user_verification_missing")
    clock.now = NOW + 60
    created_after = hub.create_invite(session)
    received_invite, received_opened = opened_invite(hub, session)
    clock.now = NOW + 65
    expired_invite = hub.create_invite(session, lifetime=300)
    clock.now = NOW + 70
    assert hub.register(stranger(), received_opened).status == 200
    clock.now = NOW + 100
    items = hub.invites(session)
    order = [item["invite"] for item in items]
    assert order.index(created_after["invite"]) < order.index(failed_invite["invite"]) < order.index(later_invite["invite"])
    assert order[0] == received_invite["invite"]
    statuses = {item["invite"]: item["status"] for item in items}
    assert statuses[failed_invite["invite"]] == "failed"
    assert statuses[later_invite["invite"]] == "opened"
    assert statuses[created_after["invite"]] == "pending"
    assert statuses[received_invite["invite"]] == "received"
    assert order == [
        received_invite["invite"],
        expired_invite["invite"],
        created_after["invite"],
        failed_invite["invite"],
        later_invite["invite"],
    ]
    clock.now = NOW + 65 + 300
    statuses = {item["invite"]: item["status"] for item in hub.invites(session)}
    assert statuses[expired_invite["invite"]] == "expired"
    assert statuses[later_invite["invite"]] == "expired"
    text = json.dumps(hub.invites(session))
    for row in hub.rows("SELECT token_hash FROM passkey_invite"):
        assert row["token_hash"] not in text
    assert "token" not in text


def test_two_invites_with_the_same_latest_change_are_ordered_by_invite_id_descending(
    pkhub: PasskeyHub, hubs: HubFactory  # noqa: F811
) -> None:
    session = admin(pkhub, hubs)
    first = pkhub.create_invite(session)
    second = pkhub.create_invite(session)
    expected = sorted([first["invite"], second["invite"]], reverse=True)
    assert [item["invite"] for item in pkhub.invites(session)] == expected


def test_the_list_ignores_its_query(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    pkhub.create_invite(session)
    wire = pkhub.session_wire(session, target=INVITES + "?status=none")
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 200, NOW)
    assert len(answer.json()["invites"]) == 1 and answer.json()["hub_key"] == HUB_KEY_ID


@pytest.mark.parametrize(
    ("row", "status"),
    [
        ({}, "pending"),
        ({"expires": NOW}, "expired"),
        ({"opened": NOW - 300}, "opened"),
        ({"opened": NOW - 301}, "expired"),
        ({"opened": NOW - 10, "expires": NOW}, "expired"),
        ({"opened": NOW - 10, "finished": NOW - 5}, "failed"),
        ({"opened": NOW - 10, "finished": NOW - 5, "received": NOW - 5}, "received"),
    ],
)
def test_every_status_of_section_5_2(pkhub: PasskeyHub, hubs: HubFactory, row: dict[str, int], status: str) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    insert_invite(pkhub, invite_id_of(1), **row)
    assert status_of(pkhub, session, invite_id_of(1)) == status


def test_a_broken_stored_public_key_fails_the_list_loudly(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = admin(pkhub, hubs)
    insert_invite(pkhub, invite_id_of(1), opened=NOW - 10, finished=NOW - 5, received=NOW - 5)
    pkhub.store.execute("UPDATE passkey_invite SET public_key = 'not=canonical'")
    wire = pkhub.session_wire(session, target=INVITES)
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 500, NOW)
    refused(answer, 500, "internal")
