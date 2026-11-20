"""The hub, commands (PASSKEY-PROTOCOL.md section 13.5), through the application with a fixed clock."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tests.conftest import HUB_KEY_ID
from tests.passkey_hub import (
    COMMANDS,
    NOW,
    HubFactory,
    PasskeyHub,
    Session,
    b64,
    command_text,
    fmt,
    gather,
    hubs,  # noqa: F401
    pkhub,  # noqa: F401
    session_signature,
)
from tests.test_signed_requests import Answer, assert_signed, assert_unsigned

SEQUENCE_MAX = 9007199254740991
TARGET = "example-staff-laptop-1"


@pytest.fixture
def staff(pkhub: PasskeyHub, hubs: HubFactory) -> Session:  # noqa: F811
    return pkhub.sign_in(hubs.passkeys["staff"])


def command(
    session: Session,
    *,
    target: str = TARGET,
    action: str = "noop",
    sequence: Any = 1,
    time_text: str = fmt(NOW),
    content_hash: str = "-",
    signed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A body of section 8.2, signed over the text of the given fields, or of `signed` instead."""
    fields = {"sender": session.id, "target": target, "action": action, "sequence": sequence, "time": time_text, "content_hash": content_hash}
    over = {**fields, **(signed or {})}
    text = command_text(over["sender"], over["target"], over["action"], over["sequence"], over["time"], over["content_hash"])
    return {
        "command": {"target": target, "action": action, "sequence": sequence, "time": time_text, "content_hash": content_hash},
        "signature": b64(session_signature(session.private, text)),
    }


def send(hub: PasskeyHub, session: Session, body: Any) -> tuple[Any, Answer]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    wire = hub.session_wire(session, method="POST", target=COMMANDS, body=raw)
    return wire, hub.send(wire)


def signed_refusal(hub: PasskeyHub, session: Session, body: Any, status: int, error: str) -> None:
    wire, answer = send(hub, session, body)
    assert_signed(wire, answer, status, hub.clock.now)
    assert answer.json() == {"error": error}


def test_a_noop_command_gets_the_signed_200_with_the_exact_body(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    wire, answer = send(pkhub, staff, command(staff))
    assert_signed(wire, answer, 200, NOW)
    text = command_text(staff.id, TARGET, "noop", 1, fmt(NOW))
    assert answer.body == json.dumps(
        {
            "command": hashlib.sha256(text).hexdigest(),
            "target": TARGET,
            "action": "noop",
            "sequence": 1,
            "delivered": False,
            "hub_key": HUB_KEY_ID,
        },
        separators=(",", ":"),
    ).encode()
    assert pkhub.rows("SELECT * FROM command_sequence") == [{"sender": staff.id, "last_sequence": 1}]


def test_c1_an_agent_key_with_device_controller_is_sender_not_allowed(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    wire = pkhub.key_wire(TARGET, method="POST", target=COMMANDS, body=json.dumps(command(staff)).encode())
    answer = pkhub.send(wire)
    assert_signed(wire, answer, 403, NOW)
    assert answer.json() == {"error": "sender_not_allowed"}


def test_a_session_key_without_record_write_gets_scope_missing(pkhub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    reader = pkhub.sign_in(hubs.passkeys["reader"])
    signed_refusal(pkhub, reader, command(reader), 403, "scope_missing")


def _without(field: str) -> Any:
    def change(body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "command": {key: value for key, value in body["command"].items() if key != field}}

    return change


def _with(**fields: Any) -> Any:
    def change(body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "command": {**body["command"], **fields}}

    return change


C2_CASES: dict[str, Any] = {
    "target missing": _without("target"),
    "action missing": _without("action"),
    "sequence missing": _without("sequence"),
    "time missing": _without("time"),
    "content_hash missing": _without("content_hash"),
    "an extra command field": _with(extra="x"),
    "signature missing": lambda body: {"command": body["command"]},
    "an extra body field": lambda body: {**body, "extra": 1},
    "an unknown action": _with(action="reboot"),
    "allowlist.add": _with(action="allowlist.add"),
    "grant": _with(action="grant"),
    "grant.remove": _with(action="grant.remove"),
    "masking": _with(action="masking"),
    "a content hash other than -": _with(content_hash="0" * 64),
    "a sequence of 0": _with(sequence=0),
    "SEQUENCE_MAX + 1": _with(sequence=SEQUENCE_MAX + 1),
    "a sequence as a string": _with(sequence="1"),
    "a sequence as a boolean": _with(sequence=True),
    "a sequence as the float 1.0": _with(sequence=1.0),
    "a time not in the time form": _with(time="2027-01-15 08:00:00"),
    "a target outside the id rule": _with(target="Example"),
    "a signature of 63 bytes": lambda body: {**body, "signature": b64(bytes(63))},
    "no JSON object": lambda body: [body],
}


@pytest.mark.parametrize("case", sorted(C2_CASES))
def test_c2_command_invalid(pkhub: PasskeyHub, staff: Session, case: str) -> None:  # noqa: F811
    signed_refusal(pkhub, staff, C2_CASES[case](command(staff)), 400, "command_invalid")


@pytest.mark.parametrize("raw", [b'{"command":{"target":"x","action":"noop","sequence":01}}', b"not json", b'{"a":NaN}'])
def test_c2_a_sequence_with_a_leading_zero_and_a_body_that_is_no_json(pkhub: PasskeyHub, staff: Session, raw: bytes) -> None:  # noqa: F811
    signed_refusal(pkhub, staff, raw, 400, "command_invalid")


def test_c2_accepts_sequence_max(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    _, answer = send(pkhub, staff, command(staff, sequence=SEQUENCE_MAX))
    assert answer.status == 200 and answer.json()["sequence"] == SEQUENCE_MAX


@pytest.mark.parametrize(("offset", "status"), [(-121, 400), (121, 400), (-120, 200), (120, 200)])
def test_c3_a_time_more_than_120_seconds_off_is_command_time_skew(
    pkhub: PasskeyHub, staff: Session, offset: int, status: int  # noqa: F811
) -> None:
    wire, answer = send(pkhub, staff, command(staff, time_text=fmt(NOW + offset)))
    assert_signed(wire, answer, status, NOW)
    if status == 400:
        assert answer.json() == {"error": "command_time_skew"}


@pytest.mark.parametrize(
    "signed",
    [
        {"target": "example-staff-laptop-cli"},
        {"action": "reboot"},
        {"sequence": 2},
        {"time": fmt(NOW + 1)},
        {"sender": "session." + "0" * 32},
        {"content_hash": "0" * 64},
    ],
)
def test_c4_a_signature_over_another_field_is_command_signature_invalid(
    pkhub: PasskeyHub, staff: Session, signed: dict[str, Any]  # noqa: F811
) -> None:
    signed_refusal(pkhub, staff, command(staff, signed=signed), 403, "command_signature_invalid")


def test_c4_a_signature_by_another_key_is_command_signature_invalid(pkhub: PasskeyHub, hubs: HubFactory, staff: Session) -> None:  # noqa: F811
    other = pkhub.sign_in(hubs.passkeys["staff"])
    body = command(other)
    body["command"] = command(staff)["command"]
    signed_refusal(pkhub, staff, body, 403, "command_signature_invalid")


@pytest.mark.parametrize(
    "target",
    [
        "other-staff-laptop-1",
        "example-staff-old",
        "example-staff-laptop-cli",
        "example-staff-runner-cli",
        "staff-passkey-1",
        "example-guard-1",
        "nobody-laptop-1",
    ],
)
def test_c5_target_not_allowed(pkhub: PasskeyHub, staff: Session, target: str) -> None:  # noqa: F811
    signed_refusal(pkhub, staff, command(staff, target=target), 403, "target_not_allowed")


def test_c5_allows_an_executor_device_key_of_the_handle(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    _, answer = send(pkhub, staff, command(staff, target="example-staff-exec"))
    assert answer.status == 200


def test_c6_a_repeated_and_a_lower_sequence_are_sequence_not_higher(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    assert send(pkhub, staff, command(staff, sequence=5))[1].status == 200
    signed_refusal(pkhub, staff, command(staff, sequence=5), 409, "sequence_not_higher")
    signed_refusal(pkhub, staff, command(staff, sequence=4), 409, "sequence_not_higher")
    assert send(pkhub, staff, command(staff, sequence=6))[1].status == 200
    assert pkhub.rows("SELECT last_sequence FROM command_sequence") == [{"last_sequence": 6}]


def test_c6_a_command_refused_before_c6_uses_no_sequence(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    signed_refusal(pkhub, staff, command(staff, sequence=3, target="example-staff-old"), 403, "target_not_allowed")
    assert pkhub.rows("SELECT * FROM command_sequence") == []


def test_c6_each_sender_has_its_own_sequence(pkhub: PasskeyHub, hubs: HubFactory, staff: Session) -> None:  # noqa: F811
    other = pkhub.sign_in(hubs.passkeys["staff"])
    assert send(pkhub, staff, command(staff, sequence=1))[1].status == 200
    assert send(pkhub, other, command(other, sequence=1))[1].status == 200


def test_two_parallel_commands_with_the_same_sequence_exactly_one_is_accepted(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    wires = [
        pkhub.session_wire(staff, method="POST", target=COMMANDS, body=json.dumps(command(staff, sequence=7)).encode())
        for _ in range(2)
    ]
    answers = gather(pkhub, *wires)
    assert sorted(answer.status for answer in answers) == [200, 409]
    assert next(answer for answer in answers if answer.status == 409).json() == {"error": "sequence_not_higher"}


@pytest.mark.parametrize(
    ("body", "error", "status"),
    [
        (lambda staff: _with(action="grant")(command(staff, time_text=fmt(NOW - 999))), "command_invalid", 400),
        (lambda staff: command(staff, time_text=fmt(NOW - 999), signed={"sequence": 9}), "command_time_skew", 400),
        (lambda staff: command(staff, target="example-staff-old", signed={"sequence": 9}), "command_signature_invalid", 403),
    ],
)
def test_the_checks_c2_to_c5_stop_at_the_first_failure(
    pkhub: PasskeyHub, staff: Session, body: Any, error: str, status: int  # noqa: F811
) -> None:
    signed_refusal(pkhub, staff, body(staff), status, error)


def test_a_command_signed_by_an_expired_session_key_is_refused_at_check_4(pkhub: PasskeyHub, staff: Session) -> None:  # noqa: F811
    pkhub.clock.now = NOW + 43080
    wire, answer = send(pkhub, staff, command(staff, time_text=fmt(NOW + 43080)))
    assert answer.status == 401 and answer.json() == {"error": "key_expired"}
    assert_unsigned(answer)
