"""Credential records of the user (04-crypto.md section 4.4, 03-api.md section 4.8, 11-agent-access.md section 8.3).

A record is written as the user: encrypted under the user's newest verified `CK` (self-wraps signed by the identity
only), signed by the identity as author, `PUT /v1/me/credentials/{kind}` with `expected_version` (the stored version;
0 for a new record, which keeps the record ID of the stored one). A stored record is read back only when its author is
the user and its signature verifies under the identity; its body is opened with the `CK` epoch of its header. A
`guard` may decide from the stored record not to write; a `409 version_conflict` is retried once on a re-read record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.errors import PlatformError
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import body_json, format_time, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7

Guard = Callable[[Mapping[str, Any], object, Mapping[str, Any] | None], dict[str, Any] | None]
"""`guard(new body, stored record or None, stored body or None)`: a result to return instead of writing, or None."""


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
