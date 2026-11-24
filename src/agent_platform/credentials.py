"""Credential records of the user (04-crypto.md section 4.4, 03-api.md section 4.8, 11-agent-access.md section 8.3).

`KINDS` and `valid_body` are the one copy of the body checks (`contracts/credentials/<kind>.json`, as the web's
`credentials/records.ts` checks them) that `credentials list`, `github status` and the credential-key rotation use.

A record is written as the user: encrypted under the user's newest verified `CK` (self-wraps signed by the identity
only), signed by the identity as author, `PUT /v1/me/credentials/{kind}` with `expected_version` (the stored version;
0 for a new record, which keeps the record ID of the stored one). A stored record is read back only when its author is
the user and its signature verifies under the identity; its body is opened with the `CK` epoch of its header. A
`guard` may decide from the stored record not to write; a `409 version_conflict` is retried once on a re-read record.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.errors import PlatformError
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import body_json, format_time, parse_json, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7

KINDS = ("github", "anthropic_api_key", "claude_subscription", "mailbox")
LOGIN_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
EMAIL_RE = re.compile(r"[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}")
PRINTABLE_RE = re.compile(r"[!-~]{1,4096}")
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
IMAP_USER_RE = re.compile(r"[ -~]{1,256}")
SCOPE_RE = re.compile(r"[a-z0-9:_-]{1,64}")
ORIGINS = ("setup_token", "login_token")
SOURCES = ("device_flow", "token")
"""How a GitHub connection was made: the GitHub App's device flow or an existing token (KOR-25); records written
before carry no `source` and came from the device flow."""

Guard = Callable[[Mapping[str, Any], object, Mapping[str, Any] | None], dict[str, Any] | None]
"""`guard(new body, stored record or None, stored body or None)`: a result to return instead of writing, or None."""


# --- bodies ---


def _text(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def is_email(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 254 and _text(value, EMAIL_RE)


def _time(value: object) -> bool:
    try:
        parse_time(value)
    except CryptoError:
        return False
    return True


def _count(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def valid_scopes(value: object) -> bool:
    """At most 64 scopes of the contract's form."""
    return isinstance(value, list) and len(value) <= 64 and all(_text(scope, SCOPE_RE) for scope in value)


def _github(body: dict[str, Any]) -> bool:
    return (
        set(body) - {"source"} == {"token", "login", "user_id", "scopes", "created_at"}
        and _text(body["token"], PRINTABLE_RE)
        and _text(body["login"], LOGIN_RE)
        and _count(body["user_id"], 1, 2**53 - 1)
        and valid_scopes(body["scopes"])
        and body.get("source", SOURCES[0]) in SOURCES
        and _time(body["created_at"])
    )


def valid_mailbox(body: dict[str, Any]) -> bool:
    password = body.get("imap_password")
    return (
        set(body) == {"imap_host", "imap_port", "imap_user", "imap_password", "email"}
        and _text(body["imap_host"], HOST_RE)
        and _count(body["imap_port"], 1, 65535)
        and _text(body["imap_user"], IMAP_USER_RE)
        and isinstance(password, str)
        and 1 <= len(password) <= 1024
        and is_email(body["email"])
    )


def _subscription(body: dict[str, Any]) -> bool:
    keys = {"token", "email", "kind", *(("expires_at",) if "expires_at" in body else ())}
    return (
        set(body) == keys
        and ("expires_at" not in body or _time(body["expires_at"]))
        and _text(body["token"], PRINTABLE_RE)
        and (body["email"] is None or is_email(body["email"]))
        and body["kind"] in ORIGINS
    )


def valid_body(kind: str, body: object) -> bool:
    """Whether `body` is exactly what `contracts/credentials/<kind>.json` allows for a record of `kind`."""
    if not isinstance(body, dict):
        return False
    if kind == "anthropic_api_key":
        return set(body) == {"key"} and _text(body["key"], PRINTABLE_RE)
    if kind == "github":
        return _github(body)
    if kind == "mailbox":
        return valid_mailbox(body)
    return _subscription(body)


# --- records ---


def read_record(account: Account, kind: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`(stored record, its body)`: the body only when the user authored it and a verified `CK` opens it."""
    try:
        answer = account.call("GET", f"/v1/me/credentials/{kind}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None, None
        raise
    record = answer.get("record")
    if not isinstance(record, dict):
        raise unverified("response_invalid")
    try:
        header = verify_credential(record, account.identity.sig_public())
        key = account.cred_keys()[int(str(header["epoch"]))]
        body = parse_json(open_credential(key, record, account.identity.sig_public()))
    except (CryptoError, KeyError, ValueError):
        return record, None
    if header["owner"] != account.user_id or header["author"] != account.user_id or not isinstance(body, dict):
        return record, None
    return record, body


def _put(account: Account, kind: str, body: dict[str, Any], guard: Guard | None) -> dict[str, Any]:
    record, current = read_record(account, kind)
    skipped = None if guard is None else guard(body, record, current)
    if skipped is not None:
        return skipped
    header = record.get("header") if record is not None else None
    version = int(header["version"]) if isinstance(header, dict) else 0
    record_id = str(header["id"]) if isinstance(header, dict) else uuid7()
    epoch, key = account.cred_key()
    fields = new_header(
        id=record_id,
        owner=account.user_id,
        kind=kind,
        version=version + 1,
        epoch=epoch,
        author=account.user_id,
        ts=format_time(datetime.now(UTC)),
    )
    sealed = seal_credential(key, fields, body_json(body), account.identity.sig)
    answer = account.call("PUT", f"/v1/me/credentials/{kind}", {"record": sealed, "expected_version": version})
    return {"stored": True, "version": answer.get("version")}


def put_record(account: Account, kind: str, body: dict[str, Any], guard: Guard | None = None) -> dict[str, Any]:
    """Store a new version of the user's record of `kind`; one retry after `409 version_conflict`."""
    try:
        return _put(account, kind, body, guard)
    except PlatformError as error:
        if error.fields.get("hub_error") != "version_conflict":
            raise
    return _put(account, kind, body, guard)
