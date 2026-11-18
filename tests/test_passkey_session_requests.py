"""The hub, signed requests by a session key (PASSKEY-PROTOCOL.md section 13.4), through the
application with a fixed clock. The loader rule of section 7.4 is in `tests/test_keys_file.py`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI

from agent_core.signing import KeyRequest
from tests.conftest import HUB_KEY_ID
from tests.passkey_hub import (
    INVITES,
    NOW,
    P256_ORDER,
    SESSION_END,
    WHOAMI,
    HubFactory,
    PasskeyHub,
    Session,
    b64,
    fmt,
    hubs,  # noqa: F401
    new_passkey,
    pkhub,  # noqa: F401
    signed_wire,
    unb64,
)
from tests.test_signed_requests import Answer, assert_signed, assert_unsigned, log_lines  # noqa: F401

CONTROLLER_ROUTE = "/api/key/test-controller"


def refused(answer: Answer, status: int, error: str) -> None:
    assert answer.status == status
    assert answer.json() == {"error": error}
    assert_unsigned(answer)


def signed_refusal(hub: PasskeyHub, wire: Any, status: int, error: str) -> None:
    answer = hub.send(wire)
    assert_signed(wire, answer, status, hub.clock.now)
    assert answer.json() == {"error": error}


async def echo(request: KeyRequest) -> tuple[int, dict[str, Any]]:
    return 200, {"key": request.key_id}


def add_test_routes(app: FastAPI) -> None:
    routes = app.state.key_service.routes
    routes.add("GET", "/api/key/test-release", echo, scope="release:query", subject=None)
    routes.add("POST", CONTROLLER_ROUTE, echo, scope=None, subject=None, controller_only=True)
    routes.add("POST", "/api/key/test-controller-invite", echo, scope="keys:invite", subject=None, controller_only=True)


@pytest.fixture
def hub(pkhub: PasskeyHub) -> PasskeyHub:  # noqa: F811
    add_test_routes(pkhub.app)
    return pkhub


@pytest.fixture
def staff(hub: PasskeyHub, hubs: HubFactory) -> Session:  # noqa: F811
    return hub.sign_in(hubs.passkeys["staff"])


def scalars(session: Session, text: bytes) -> tuple[int, int]:
    return decode_dss_signature(session.private.sign(text, ec.ECDSA(hashes.SHA256())))


def raw_signature(r: int, s: int) -> bytes:
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --- the changed checks of section 7.1 in their order -----------------------------------------------


@pytest.mark.parametrize(
    "key_id",
    [
        "session." + "A" * 32,
        "session." + "a" * 31,
        "session." + "a" * 33,
        "Session." + "a" * 32,
    ],
)
def test_check_1_refuses_a_session_key_id_in_the_wrong_form(hub: PasskeyHub, staff: Session, key_id: str) -> None:
    wire = signed_wire(key_id, lambda text: b"\x01" * 64, time_text=hub.time())
    refused(hub.send(wire), 401, "signature_missing")


def test_check_1_passes_an_id_that_follows_the_keys_file_id_rule_and_check_2_refuses_it(hub: PasskeyHub) -> None:
    # `session-` and 32 letters follow the `id` rule of KEY-PROTOCOL.md section 3, so check 1
    # passes it (section 7.1: the `id` rule or the session key id form) and check 2 finds no entry.
    wire = signed_wire("session-" + "a" * 32, lambda text: b"\x01" * 64, time_text=hub.time())
    refused(hub.send(wire), 401, "key_unknown")


def test_check_1_refuses_a_der_signature_instead_of_r_and_s(hub: PasskeyHub, staff: Session) -> None:
    for _ in range(8):
        wire = signed_wire(staff.id, lambda text: staff.private.sign(text, ec.ECDSA(hashes.SHA256())), time_text=hub.time())
        # DER of two 32-byte integers is 70 to 72 bytes (fewer for a short integer), never 64.
        assert len(unb64(wire.value(b"agent-signature").decode())) != 64
        refused(hub.send(wire), 401, "signature_missing")


def test_check_2_refuses_an_unknown_session_id(hub: PasskeyHub, staff: Session) -> None:
    unknown = Session(id="session." + "0" * 32, private=staff.private, public=staff.public, answer={})
    refused(hub.send(hub.session_wire(unknown)), 401, "key_unknown")


def test_whoami_with_a_session_key_id_that_names_no_session_is_key_unknown(hub: PasskeyHub) -> None:
    # Before this change the same request got `signature_missing` at check 1 (section 1).
    wire = signed_wire("session." + "f" * 32, lambda text: b"\x01" * 64, time_text=hub.time())
    refused(hub.send(wire), 401, "key_unknown")


def test_check_2_refuses_an_ended_session(hub: PasskeyHub, staff: Session) -> None:
    wire = hub.session_wire(staff, method="POST", target=SESSION_END)
    assert hub.send(wire).status == 200
    refused(hub.send(hub.session_wire(staff)), 401, "key_unknown")


def test_check_2_refuses_a_session_whose_passkey_id_is_no_longer_in_the_keys_file(hubs: HubFactory) -> None:  # noqa: F811
    first = hubs.build()
    session = first.sign_in(hubs.passkeys["staff"])
    entries = [entry for entry in hubs.entries() if entry["id"] != "staff-passkey-1"]
    second = hubs.build(entries)
    refused(second.send(second.session_wire(session)), 401, "key_unknown")


def test_check_3_refuses_a_session_whose_passkey_is_revoked(hub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    session = hub.sign_in(hubs.passkeys["later"])
    assert hub.send(hub.session_wire(session)).status == 200
    hub.clock.now = NOW + 3600
    refused(hub.send(hub.session_wire(session)), 401, "key_revoked")


@pytest.mark.parametrize("offset", [0, 1])
def test_check_4_refuses_a_session_at_and_after_its_expiry(hub: PasskeyHub, staff: Session, offset: int) -> None:
    hub.clock.now = NOW + 43080 - 1
    assert hub.send(hub.session_wire(staff)).status == 200
    hub.clock.now = NOW + 43080 + offset
    refused(hub.send(hub.session_wire(staff)), 401, "key_expired")


@pytest.mark.parametrize(
    "change",
    [
        {"public_key": b64(new_passkey(41).credentials[0].public_key)},
        {"credential_id": b64(b"changed-credential-0001")},
        {"handle": "other-staff"},
    ],
)
def test_check_5_refuses_a_session_whose_passkey_entry_changed(hubs: HubFactory, change: dict[str, str]) -> None:  # noqa: F811
    first = hubs.build()
    session = first.sign_in(hubs.passkeys["staff"])
    second = hubs.build(hubs.changed("staff-passkey-1", **change))
    refused(second.send(second.session_wire(session)), 401, "certificate_invalid")
    unchanged = hubs.build()
    assert unchanged.send(unchanged.session_wire(session)).status == 200


def test_check_7_refuses_a_signature_over_another_text(hub: PasskeyHub, staff: Session) -> None:
    refused(hub.send(hub.session_wire(staff, sign_as={"target": INVITES})), 401, "signature_invalid")
    refused(hub.send(hub.session_wire(staff, sign_as={"method": "POST"})), 401, "signature_invalid")
    other = ec.generate_private_key(ec.SECP256R1())
    forged = Session(id=staff.id, private=other, public=staff.public, answer={})
    refused(hub.send(hub.session_wire(forged)), 401, "signature_invalid")


@pytest.mark.parametrize("which", ["r is 0", "s is 0", "r is n", "s is n"])
def test_check_7_refuses_an_r_or_s_of_0_or_n(hub: PasskeyHub, staff: Session, which: str) -> None:
    def sign(text: bytes) -> bytes:
        r, s = scalars(staff, text)
        values = {"r is 0": (0, s), "s is 0": (r, 0), "r is n": (P256_ORDER, s), "s is n": (r, P256_ORDER)}
        return raw_signature(*values[which])

    refused(hub.send(signed_wire(staff.id, sign, time_text=hub.time())), 401, "signature_invalid")


def test_a_high_s_is_accepted(hub: PasskeyHub, staff: Session) -> None:
    def sign(text: bytes) -> bytes:
        r, s = scalars(staff, text)
        return raw_signature(r, s if s > P256_ORDER // 2 else P256_ORDER - s)

    wire = signed_wire(staff.id, sign, time_text=hub.time())
    assert_signed(wire, hub.send(wire), 200, NOW)


def test_check_8_refuses_a_replayed_nonce(hub: PasskeyHub, staff: Session) -> None:
    wire = hub.session_wire(staff)
    assert hub.send(wire).status == 200
    refused(hub.send(wire), 401, "nonce_replayed")
    assert hub.rows("SELECT key_id FROM key_nonce") == [{"key_id": staff.id}]


@pytest.mark.parametrize("offset", [-121, 121])
def test_check_6_is_unchanged_for_a_session_key(hub: PasskeyHub, staff: Session, offset: int) -> None:
    refused(hub.send(hub.session_wire(staff, time_text=fmt(NOW + offset))), 401, "time_skew")
    assert hub.send(hub.session_wire(staff, time_text=fmt(NOW + offset - (1 if offset > 0 else -1)))).status == 200


def test_checks_2_to_5_run_before_checks_6_and_7(hubs: HubFactory) -> None:  # noqa: F811
    first = hubs.build()
    session = first.sign_in(hubs.passkeys["staff"])
    second = hubs.build(hubs.changed("staff-passkey-1", handle="other-staff"))
    bad = signed_wire(session.id, lambda text: b"\x01" * 64, time_text=fmt(NOW - 999))
    refused(second.send(bad), 401, "certificate_invalid")
    unknown = signed_wire("session." + "0" * 32, lambda text: b"\x01" * 64, time_text=fmt(NOW - 999))
    refused(second.send(unknown), 401, "key_unknown")
    assert second.rows("SELECT key_id FROM key_nonce") == []


def test_a_broken_session_row_gives_an_unsigned_500(hub: PasskeyHub, staff: Session) -> None:
    hub.store.execute("UPDATE passkey_session SET session_public_key = 'not=canonical'")
    refused(hub.send(hub.session_wire(staff)), 500, "internal")


# --- checks 9 and 9a -------------------------------------------------------------------------------


def test_check_9_a_session_key_without_the_scope_of_a_route_gets_scope_missing(hub: PasskeyHub, staff: Session) -> None:
    signed_refusal(hub, hub.session_wire(staff, target="/api/key/test-release"), 403, "scope_missing")


def test_a_session_key_has_the_scopes_of_its_passkey_entry_in_the_keys_file_loaded_at_start(hubs: HubFactory) -> None:  # noqa: F811
    first = hubs.build()
    session = first.sign_in(hubs.passkeys["staff"])
    assert first.send(first.session_wire(session, target=INVITES)).status == 403
    second = hubs.build(hubs.changed("staff-passkey-1", scopes=["record:read"]))
    wire = second.session_wire(session)
    answer = second.send(wire)
    assert answer.json()["scopes"] == ["record:read"]


@pytest.mark.parametrize(
    "key_id",
    ["example-staff-laptop-cli", "example-staff-runner-cli", "example-staff-exec", "example-guard-1"],
)
def test_check_9a_refuses_every_key_but_a_controller_device_key(hub: PasskeyHub, key_id: str) -> None:
    signed_refusal(hub, hub.key_wire(key_id, method="POST", target=CONTROLLER_ROUTE), 403, "controller_device_required")


def test_check_9a_refuses_a_session_key(hub: PasskeyHub, staff: Session) -> None:
    signed_refusal(hub, hub.session_wire(staff, method="POST", target=CONTROLLER_ROUTE), 403, "controller_device_required")


def test_check_9a_lets_a_device_key_with_device_controller_pass(hub: PasskeyHub) -> None:
    wire = hub.key_wire("example-staff-laptop-1", method="POST", target=CONTROLLER_ROUTE)
    answer = hub.send(wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.json() == {"key": "example-staff-laptop-1"}


def test_check_9a_passes_a_session_key_on_a_route_not_marked_controller_only(hub: PasskeyHub, staff: Session) -> None:
    assert hub.send(hub.session_wire(staff)).status == 200


def test_check_9_runs_before_check_9a(hub: PasskeyHub) -> None:
    wire = hub.key_wire("example-staff-laptop-cli", method="POST", target="/api/key/test-controller-invite")
    signed_refusal(hub, wire, 403, "scope_missing")


# --- the log line of section 7.1 ------------------------------------------------------------------------


def test_one_log_line_per_session_request_and_no_certificate_signature_or_body(
    hub: PasskeyHub, hubs: HubFactory, log_lines: list[str]  # noqa: F811
) -> None:
    session = hub.sign_in(hubs.passkeys["admin"])
    body = b'{"handle":"example-staff","team":"example","lifetime_seconds":3600}'
    sent = [
        hub.session_wire(session, target=WHOAMI + "?x=1"),
        hub.session_wire(session, method="POST", target=INVITES, body=body),
        hub.session_wire(session, target="/api/key/test-release"),
        hub.session_wire(session, target="/api/key/unknown"),
    ]
    answers = [hub.send(wire) for wire in sent]
    assert [answer.status for answer in answers] == [200, 201, 403, 404]
    replayed = hub.send(sent[0])
    assert replayed.status == 401
    prefix = "uvicorn.error INFO key request "
    assert [line[len(prefix):] for line in log_lines if line.startswith(prefix)] == [
        f"{session.id} admin-passkey-1 GET /api/key/whoami 200",
        f"{session.id} admin-passkey-1 POST /api/key/invites 201",
        f"{session.id} admin-passkey-1 GET /api/key/test-release 403",
        f"{session.id} admin-passkey-1 GET /api/key/unknown 404",
    ]
    (row,) = hub.rows("SELECT * FROM passkey_session")
    token = answers[1].json()["link"].split("#", 1)[1]
    secrets_of_the_session = [
        row["statement"],
        row["signature"],
        row["authenticator_data"],
        row["client_data_json"],
        row["session_public_key"],
        row["passkey_public_key"],
        body.decode(),
        token,
        *(wire.value(b"agent-signature").decode() for wire in sent),
    ]
    for line in log_lines:
        for value in secrets_of_the_session:
            assert value not in line


# --- sign-out (section 7.3) -----------------------------------------------------------------------------


def test_sign_out_answers_a_signed_200_and_then_the_key_is_unknown(hub: PasskeyHub, staff: Session) -> None:
    wire = hub.session_wire(staff, method="POST", target=SESSION_END, body=b"ignored")
    answer = hub.send(wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.body == b'{"ended":true,"hub_key":"' + HUB_KEY_ID.encode() + b'"}'
    assert hub.rows("SELECT ended FROM passkey_session") == [{"ended": NOW}]
    refused(hub.send(hub.session_wire(staff, method="POST", target=SESSION_END)), 401, "key_unknown")
    refused(hub.send(hub.session_wire(staff)), 401, "key_unknown")


def test_a_keys_file_key_gets_session_key_required(hub: PasskeyHub) -> None:
    signed_refusal(hub, hub.key_wire("example-staff-laptop-1", method="POST", target=SESSION_END), 403, "session_key_required")


def test_ending_one_session_leaves_another_session_of_the_same_passkey(hub: PasskeyHub, hubs: HubFactory, staff: Session) -> None:  # noqa: F811
    other = hub.sign_in(hubs.passkeys["staff"])
    assert hub.send(hub.session_wire(staff, method="POST", target=SESSION_END)).status == 200
    assert hub.send(hub.session_wire(other)).status == 200


def test_a_signed_answer_names_the_session_key_id(hub: PasskeyHub, staff: Session) -> None:
    wire = hub.session_wire(staff)
    answer = hub.send(wire)
    assert_signed(wire, answer, 200, NOW)
    assert json.loads(answer.body)["key"] == staff.id
