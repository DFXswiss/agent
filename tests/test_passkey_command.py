"""The command text (PASSKEY-PROTOCOL.md section 8.1) and the pure checks C2 to C4 of section 8.2,
called directly (the command cases of section 13.5 that need no hub), and device check 5 of
section 9."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from agent_core.passkey.command import (
    Command,
    CommandRequest,
    check_command_time,
    command_text,
    content_hash,
    content_matches,
    parse_command_body,
    parse_command_text,
    verify_command_signature,
)
from agent_core.passkey.encoding import SEQUENCE_MAX
from agent_core.passkey.errors import PasskeyRefusal
from tests.passkey_vectors import NOW, b64, time_text
from tests.webauthn_authenticator import point_of

SENDER = "session." + "5d1c" * 8
OTHER_SENDER = "session." + "0" * 32
TARGET = "example-staff-laptop-1"
FIELDS: dict[str, object] = {
    "target": TARGET,
    "action": "noop",
    "sequence": 1,
    "time": time_text(NOW),
    "content_hash": "-",
}
SESSION_KEY = ec.derive_private_key(12345, ec.SECP256R1())
SESSION_PUBLIC = point_of(SESSION_KEY)


def _refusal(error: str) -> Any:
    return pytest.raises(PasskeyRefusal, match=f"^{error}$")


def _command(sender: str = SENDER, **change: object) -> Command:
    fields = {**FIELDS, **change}
    return Command(sender=sender, **fields)  # type: ignore[arg-type]


def _lines(sender: str, fields: dict[str, object]) -> bytes:
    values = [fields[name] for name in ("target", "action", "sequence", "time", "content_hash")]
    return "\n".join(["agent-command-v1", sender, *(str(value) for value in values)]).encode()


def _sign(text: bytes) -> bytes:
    r, s = decode_dss_signature(SESSION_KEY.sign(text, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _body(fields: dict[str, object] | None = None, *, signature: object = None, **extra: object) -> bytes:
    command = FIELDS if fields is None else fields
    value = signature if signature is not None else b64(_sign(_lines(SENDER, command)))
    return json.dumps({"command": command, "signature": value, **extra}).encode()


# --- section 8.1, the command text ---------------------------------------------------------------


def test_command_text_is_seven_lines_without_trailing_line_feed() -> None:
    assert command_text(_command()) == (
        f"agent-command-v1\n{SENDER}\n{TARGET}\nnoop\n1\n{time_text(NOW)}\n-"
    ).encode()


def test_command_text_parsed_back_into_its_fields() -> None:
    for sequence in (1, 10, SEQUENCE_MAX):
        command = _command(sequence=sequence)
        assert parse_command_text(command_text(command)) == command


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": 0},
        {"sequence": SEQUENCE_MAX + 1},
        {"sequence": -1},
        {"sequence": True},
        {"sequence": 1.0},
        {"sequence": "1"},
        {"action": "grant"},
        {"action": "allowlist.add"},
        {"action": "grant.remove"},
        {"action": "masking"},
        {"action": "Noop"},
        {"content_hash": "a" * 64},
        {"content_hash": ""},
        {"target": "Example-Laptop"},
        {"target": "session." + "0" * 32},
        {"time": "2026-11-17T10:00:00"},
        {"time": 0},
    ],
    ids=lambda change: repr(change),
)
def test_command_text_refuses_a_field_outside_the_form(change: dict[str, object]) -> None:
    with _refusal("command_invalid"):
        command_text(_command(**change))


@pytest.mark.parametrize("sender", ["example-staff-laptop-1", "session." + "A" * 32, "session.0"])
def test_command_sender_is_a_session_key_id(sender: str) -> None:
    with _refusal("command_invalid"):
        command_text(_command(sender=sender))


@pytest.mark.parametrize(
    "text",
    [
        _lines(SENDER, FIELDS) + b"\n",
        b"\n".join(_lines(SENDER, FIELDS).split(b"\n")[:6]),
        _lines(SENDER, FIELDS).replace(b"agent-command-v1", b"agent-request-v1"),
        _lines(SENDER, {**FIELDS, "sequence": "01"}),
        _lines(SENDER, {**FIELDS, "sequence": "+1"}),
        _lines(SENDER, {**FIELDS, "sequence": "1" * 17}),
        _lines(SENDER, {**FIELDS, "sequence": "1" * 5000}),
        _lines(SENDER, {**FIELDS, "sequence": "١"}),
        _lines(SENDER, {**FIELDS, "sequence": "9007199254740992"}),
        _lines(SENDER, {**FIELDS, "action": "grant"}),
        _lines(SENDER, FIELDS) + b"\xff",
    ],
    ids=["trailing line feed", "six lines", "another label", "leading zero", "sign", "17 digits", "many digits",
         "non-ASCII digit", "SEQUENCE_MAX + 1", "unknown action", "not UTF-8"],
)
def test_parse_command_text_refuses(text: bytes) -> None:
    with _refusal("command_invalid"):
        parse_command_text(text)


# --- C2, the body ----------------------------------------------------------------------------------


def test_c2_accepts_the_body_of_section_8_2() -> None:
    request = parse_command_body(_body(), SENDER)
    assert request.command == _command()
    assert request.sent_at == NOW
    assert len(request.signature) == 64


@pytest.mark.parametrize("missing", sorted(FIELDS))
def test_c2_each_field_missing(missing: str) -> None:
    fields = {name: value for name, value in FIELDS.items() if name != missing}
    with _refusal("command_invalid"):
        parse_command_body(_body(fields), SENDER)


@pytest.mark.parametrize(
    "body",
    [
        _body({**FIELDS, "extra": 1}),
        _body(extra=1),
        json.dumps({"command": FIELDS}).encode(),
        json.dumps({"signature": b64(bytes(64))}).encode(),
        json.dumps([FIELDS]).encode(),
        json.dumps({"command": [FIELDS], "signature": b64(bytes(64))}).encode(),
        _body({**FIELDS, "action": "unknown"}),
        _body({**FIELDS, "action": "allowlist.add"}),
        _body({**FIELDS, "action": "grant"}),
        _body({**FIELDS, "action": "grant.remove"}),
        _body({**FIELDS, "action": "masking"}),
        _body({**FIELDS, "content_hash": hashlib.sha256(b"x").hexdigest()}),
        _body({**FIELDS, "sequence": 0}),
        _body({**FIELDS, "sequence": SEQUENCE_MAX + 1}),
        _body({**FIELDS, "sequence": "1"}),
        _body({**FIELDS, "sequence": True}),
        _body({**FIELDS, "sequence": 1.0}),
        _body().replace(b'"sequence": 1', b'"sequence": 01'),
        _body().replace(b'"sequence": 1', b'"sequence": 1e0'),
        _body({**FIELDS, "time": "2026-11-17T10:00:00+00:00"}),
        _body({**FIELDS, "target": 1}),
        _body(signature=b64(bytes(63))),
        _body(signature=b64(bytes(64)) + "="),
        _body(signature=7),
        _body().replace(b'"action"', b'"action": "noop", "action"'),
        _body().replace(b'"sequence": 1', b'"sequence": NaN'),
        b"\xff",
    ],
    ids=["extra command field", "extra body field", "signature missing", "command missing", "not an object",
         "command not an object", "unknown action", "allowlist.add", "grant", "grant.remove", "masking",
         "content hash", "sequence 0", "SEQUENCE_MAX + 1", "sequence string", "sequence boolean", "sequence 1.0",
         "sequence leading zero", "sequence exponent", "time form", "target not text", "signature 63 bytes",
         "signature padded", "signature not text", "key written twice", "NaN", "not UTF-8"],
)
def test_c2_command_invalid(body: bytes) -> None:
    with _refusal("command_invalid"):
        parse_command_body(body, SENDER)


def test_c2_sequence_max_accepted() -> None:
    assert parse_command_body(_body({**FIELDS, "sequence": SEQUENCE_MAX}), SENDER).command.sequence == SEQUENCE_MAX


# --- C3, the time --------------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [-120, 0, 120])
def test_c3_time_within_120_seconds_accepted(offset: int) -> None:
    check_command_time(CommandRequest(command=_command(), signature=bytes(64), sent_at=NOW + offset), NOW)


@pytest.mark.parametrize("offset", [-121, 121])
def test_c3_time_121_seconds_off_is_command_time_skew(offset: int) -> None:
    with _refusal("command_time_skew"):
        check_command_time(CommandRequest(command=_command(), signature=bytes(64), sent_at=NOW + offset), NOW)


# --- C4, the signature -----------------------------------------------------------------------------


def test_c4_signature_over_the_command_text_verifies() -> None:
    request = parse_command_body(_body(), SENDER)
    verify_command_signature(request.command, request.signature, SESSION_PUBLIC)


@pytest.mark.parametrize(
    ("sender", "signed"),
    [
        (SENDER, {**FIELDS, "target": "example-staff-desktop-1"}),
        (SENDER, {**FIELDS, "action": "grant"}),
        (SENDER, {**FIELDS, "sequence": 2}),
        (SENDER, {**FIELDS, "time": time_text(NOW + 1)}),
        (OTHER_SENDER, FIELDS),
    ],
    ids=["target", "action", "sequence", "time", "sender"],
)
def test_c4_signature_over_another_field_is_command_signature_invalid(sender: str, signed: dict[str, object]) -> None:
    signature = _sign(_lines(sender, signed))
    with _refusal("command_signature_invalid"):
        verify_command_signature(_command(), signature, SESSION_PUBLIC)


def test_c4_signature_of_another_key_is_command_signature_invalid() -> None:
    other = ec.derive_private_key(54321, ec.SECP256R1())
    with _refusal("command_signature_invalid"):
        verify_command_signature(_command(), _sign(command_text(_command())), point_of(other))


# --- device check 5, the content -------------------------------------------------------------------


def test_content_hash_is_sha256_hex_over_the_random_value_and_the_utf8_content() -> None:
    random_value = bytes(range(16))
    assert content_hash(random_value, "grüezi") == hashlib.sha256(random_value + "grüezi".encode()).hexdigest()
    with pytest.raises(ValueError):
        content_hash(bytes(15), "x")


def test_content_matches_line_7() -> None:
    random_value = bytes(range(16))
    line = hashlib.sha256(random_value + b"content").hexdigest()
    assert content_matches("-", None, None)
    assert not content_matches("-", "x", None)
    assert not content_matches("-", None, b64(random_value))
    assert content_matches(line, "content", b64(random_value))
    assert not content_matches(line, "other", b64(random_value))
    assert not content_matches(line, None, b64(random_value))
    assert not content_matches(line, "content", None)
    assert not content_matches(line, "content", b64(bytes(15)))
    assert not content_matches(line, "content", b64(random_value) + "=")
    assert not content_matches(line, "\ud800", b64(random_value))
