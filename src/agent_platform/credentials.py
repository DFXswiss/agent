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
from agent_runner.crypto.ids import is_uuid7, uuid7

KINDS = ("github", "anthropic_api_key", "claude_subscription", "mailbox")
LOGIN_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
EMAIL_RE = re.compile(r"[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}")
PRINTABLE_RE = re.compile(r"[!-~]{1,4096}")
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
IMAP_USER_RE = re.compile(r"[ -~]{1,256}")
SCOPE_RE = re.compile(r"[a-z0-9:_-]{1,64}")
NAME_RE = re.compile(r"[ -;=?-~]{1,255}")
MAILBOX_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAILBOX_FIELDS = frozenset({"imap_host", "imap_port", "imap_user", "imap_password", "email"})
MAILBOX_OPTIONAL = frozenset({"name", "purpose", "use"})
MAX_PURPOSE = 500
MAX_RELEASES = 50
SEED_RE = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
SSH_PUBLIC_RE = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{68}( [!-~]{1,64})?")
ORIGINS = ("setup_token", "login_token")
SOURCES = ("device_flow", "token", "operator")
"""How a GitHub connection was made: the GitHub App's device flow, an existing token (KOR-25) or the operator's token
stored as an organization's account (KOR-47); records written before carry no `source` and came from the device
flow."""
GITHUB_OPTIONAL = frozenset({"source", "name", "signing_key"})

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


def _signing_key(value: object) -> bool:
    """The identity's own SSH signing key (`signing_key` of `contracts/credentials/github.json`, ADR-078 section 5)."""
    return (
        isinstance(value, dict)
        and {"alg", "seed", "public"} <= set(value) <= {"alg", "seed", "public", "github_key_id"}
        and value["alg"] == "ssh-ed25519"
        and _text(value["seed"], SEED_RE)
        and _text(value["public"], SSH_PUBLIC_RE)
        and ("github_key_id" not in value or _count(value["github_key_id"], 1, 2**53 - 1))
    )


MINTED = ("ghu_", "gho_")
"""Prefixes of the tokens a device flow mints (GitHub App user token, OAuth App token; KOR-65)."""


def revocable(body: dict[str, Any]) -> bool:
    """Whether the devices revoke the token of a `github` body at GitHub when it is removed: a token the device flow
    minted (`source` device flow and a prefix of `MINTED`). The one rule of `github status`, `github accounts` and
    the web (`isRevocable`); KOR-67."""
    return body.get("source", SOURCES[0]) == SOURCES[0] and str(body["token"]).startswith(MINTED)


def valid_github(body: dict[str, Any]) -> bool:
    """A `github` body (one GitHub identity, ADR-078): `source`, `name` and `signing_key` are optional."""
    return (
        set(body) - GITHUB_OPTIONAL == {"token", "login", "user_id", "scopes", "created_at"}
        and _text(body["token"], PRINTABLE_RE)
        and _text(body["login"], LOGIN_RE)
        and _count(body["user_id"], 1, 2**53 - 1)
        and valid_scopes(body["scopes"])
        and body.get("source", SOURCES[0]) in SOURCES
        and ("name" not in body or _text(body["name"], NAME_RE))
        and ("signing_key" not in body or _signing_key(body["signing_key"]))
        and _time(body["created_at"])
    )


def valid_release(value: object) -> bool:
    """`use` of a mailbox body: `{wgs}`, distinct workgroup IDs (ADR-099 section 3)."""
    if not isinstance(value, dict) or set(value) != {"wgs"}:
        return False
    wgs = value["wgs"]
    return (
        isinstance(wgs, list)
        and len(wgs) <= MAX_RELEASES
        and all(is_uuid7(wg) for wg in wgs)
        and len(set(wgs)) == len(wgs)
    )


def valid_mailbox(body: dict[str, Any]) -> bool:
    """A body of `contracts/credentials/mailbox.json`; `name`, `purpose` and `use` are optional (ADR-099 section 2)."""
    password = body.get("imap_password")
    purpose = body.get("purpose", "-")
    return (
        MAILBOX_FIELDS <= set(body) <= MAILBOX_FIELDS | MAILBOX_OPTIONAL
        and _text(body["imap_host"], HOST_RE)
        and _count(body["imap_port"], 1, 65535)
        and _text(body["imap_user"], IMAP_USER_RE)
        and isinstance(password, str)
        and 1 <= len(password) <= 1024
        and is_email(body["email"])
        and ("name" not in body or _text(body["name"], MAILBOX_NAME_RE))
        and isinstance(purpose, str)
        and 1 <= len(purpose) <= MAX_PURPOSE
        and ("use" not in body or valid_release(body["use"]))
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
        return valid_github(body)
    if kind == "mailbox":
        return valid_mailbox(body)
    return _subscription(body)


# --- records ---


def read_record(account: Account, kind: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`(stored record, its body)`: the body only when the user, this agent or a former agent of the user authored it
    and a verified `CK` opens it."""
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
        found = record.get("header")
        author = account.author_key(str(found.get("author") if isinstance(found, dict) else None))
        header = verify_credential(record, author)
        key = account.cred_keys()[int(str(header["epoch"]))]
        body = parse_json(open_credential(key, record, author))
    except (CryptoError, KeyError, ValueError):
        return record, None
    if header["owner"] != account.user_id or not isinstance(body, dict):
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
        author=account.actor,
        ts=format_time(datetime.now(UTC)),
    )
    sealed = seal_credential(key, fields, body_json(body), account.signer.sig)
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
