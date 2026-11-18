"""The command text (PASSKEY-PROTOCOL.md section 8.1) and the pure checks C2 to C4 of section 8.2.

Pure functions: "now" is an input. C1 (the request's key is a session key), C5 (the target is a
device key of the session's handle) and C6 (the sequence is higher than the last accepted one)
need the session row, the keys file or the database and are not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..signing import b64url_decode, parse_time, sha256_hex, valid_key_id
from .encoding import (
    COMMAND_TIME_WINDOW,
    SEQUENCE_MAX,
    SESSION_SIGNATURE_BYTES,
    JsonInvalid,
    json_integer,
    parse_json,
    valid_session_key_id,
    verify_session_signature,
)
from .errors import (
    COMMAND_INVALID,
    COMMAND_SIGNATURE_INVALID,
    COMMAND_TIME_SKEW,
    PasskeyRefusal,
)

COMMAND_LABEL = "agent-command-v1"
COMMAND_LINES = 7
# The closed list of actions of this build step. Every action of the list carries no content, so
# line 7 of every command is `-`.
ACTIONS = ("noop",)
NO_CONTENT = "-"
CONTENT_RANDOM_BYTES = 16
BODY_FIELDS = frozenset({"command", "signature"})
COMMAND_FIELDS = frozenset({"target", "action", "sequence", "time", "content_hash"})

# Line 5: decimal, no sign, no leading zero; at most the 16 digits of SEQUENCE_MAX, so that the
# conversion never meets Python's limit on the digits of an integer.
_SEQUENCE_RE = re.compile(r"[1-9][0-9]{0,15}")


@dataclass(frozen=True)
class Command:
    """The fields of a command text. `sequence` is an integer, `time` the time text."""

    sender: str
    target: str
    action: str
    sequence: int
    time: str
    content_hash: str


@dataclass(frozen=True)
class CommandRequest:
    """The body of `POST /api/key/commands` that passed C2, with the sender's time in seconds."""

    command: Command
    signature: bytes = field(repr=False)
    sent_at: int


def _command_valid(command: Command) -> bool:
    """The form of section 8.1 for every line: line 2 a session key id (the only sender of this
    step), line 3 a keys-file id, line 4 in the closed list, line 5 an integer from 1 to
    `SEQUENCE_MAX`, line 6 the time form, line 7 `-` because no action of the list carries
    content."""
    return (
        valid_session_key_id(command.sender)
        and valid_key_id(command.target)
        and command.action in ACTIONS
        and json_integer(command.sequence)
        and 1 <= command.sequence <= SEQUENCE_MAX
        and parse_time(command.time) is not None
        and command.content_hash == NO_CONTENT
    )


def command_text(command: Command) -> bytes:
    """The seven lines of section 8.1, joined by `\\n`, without a trailing line feed.

    Raises `command_invalid` for a field that breaks the form of section 8.1.
    """
    if not _command_valid(command):
        raise PasskeyRefusal(COMMAND_INVALID)
    lines = (
        COMMAND_LABEL,
        command.sender,
        command.target,
        command.action,
        str(command.sequence),
        command.time,
        command.content_hash,
    )
    return "\n".join(lines).encode("utf-8")


def parse_command_text(text: bytes) -> Command:
    """A command text back into its fields; `command_invalid` unless it is exactly the form that
    `command_text` writes."""
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        raise PasskeyRefusal(COMMAND_INVALID) from None
    lines = decoded.split("\n")
    if (
        len(lines) != COMMAND_LINES
        or lines[0] != COMMAND_LABEL
        or _SEQUENCE_RE.fullmatch(lines[4]) is None
    ):
        raise PasskeyRefusal(COMMAND_INVALID)
    command = Command(
        sender=lines[1],
        target=lines[2],
        action=lines[3],
        sequence=int(lines[4]),
        time=lines[5],
        content_hash=lines[6],
    )
    if not _command_valid(command):
        raise PasskeyRefusal(COMMAND_INVALID)
    return command


def parse_command_body(body: bytes, sender: str) -> CommandRequest:
    """C2: one JSON object (section 2) with exactly `command` and `signature`; `command` with
    exactly the five fields, each in the form of section 8.1 (`sequence` a JSON integer); the
    action in the list; `content_hash` `-`; `signature` canonical base64url of 64 bytes.

    `sender` is the request's `Agent-Key`, a session key id after C1.
    """
    try:
        data = parse_json(body)
    except JsonInvalid:
        raise PasskeyRefusal(COMMAND_INVALID) from None
    if type(data) is not dict or set(data) != BODY_FIELDS:
        raise PasskeyRefusal(COMMAND_INVALID)
    fields = data["command"]
    signature = b64url_decode(data["signature"])
    if (
        type(fields) is not dict
        or set(fields) != COMMAND_FIELDS
        or signature is None
        or len(signature) != SESSION_SIGNATURE_BYTES
    ):
        raise PasskeyRefusal(COMMAND_INVALID)
    command = Command(
        sender=sender,
        target=fields["target"],
        action=fields["action"],
        sequence=fields["sequence"],
        time=fields["time"],
        content_hash=fields["content_hash"],
    )
    sent_at = parse_time(command.time)
    if sent_at is None or not _command_valid(command):
        raise PasskeyRefusal(COMMAND_INVALID)
    return CommandRequest(command=command, signature=signature, sent_at=sent_at)


def check_command_time(request: CommandRequest, now: int) -> None:
    """C3: `time` is at most `COMMAND_TIME_WINDOW` seconds from now, in either direction."""
    if abs(request.sent_at - now) > COMMAND_TIME_WINDOW:
        raise PasskeyRefusal(COMMAND_TIME_SKEW)


def verify_command_signature(command: Command, signature: bytes, session_public_key: bytes) -> None:
    """C4 and device check 4: the session key signature over the command text built from the
    fields and the sender."""
    if not verify_session_signature(session_public_key, signature, command_text(command)):
        raise PasskeyRefusal(COMMAND_SIGNATURE_INVALID)


def content_hash(content_random: bytes, content: str) -> str:
    """Line 7 of a command that carries content: SHA-256 hex over the 16-byte random value
    followed by the UTF-8 content."""
    if len(content_random) != CONTENT_RANDOM_BYTES:
        raise ValueError("the content random value must be 16 bytes")
    return sha256_hex(content_random + content.encode("utf-8"))


def content_matches(line_7: str, content: object, content_random: object) -> bool:
    """Device check 5: for `-` both `content` and `content_random` are None; otherwise
    `content_random` decodes to 16 bytes and the hash over it and the UTF-8 `content` is line 7."""
    if line_7 == NO_CONTENT:
        return content is None and content_random is None
    random_value = b64url_decode(content_random)
    if type(content) is not str or random_value is None or len(random_value) != CONTENT_RANDOM_BYTES:
        return False
    try:
        expected = content_hash(random_value, content)
    except UnicodeEncodeError:
        return False
    return expected == line_7
