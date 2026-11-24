"""Where a personal `github.connect` stores the connection (KOR-47, ADR-076 section 1; 03-api.md section 6.1).

`store(flow, body, identity_id)` writes the credential body of the requesting user `U` as this device, answering the
request, and returns the record ID of the identity it filled:

- `identity_id` named: that record of `U` (the "reconnect" of a listed account, same ID, version + 1). When it is
  `U`'s primary record (the ID of `GET /v1/device/credentials/{U}/github`) the legacy path below writes it; any other
  ID is read with `GET /v1/device/github-identities/{id}` for its version: `404`, a record of another owner or an
  organization's record is `CredentialUnavailable("missing")` (the device never learns or reveals whose it is).
- otherwise (and always for the operator's copy, `replace_primary`, which replaces the primary record as before)
  `U`'s primary record is read (`cache.fetch`, then opened in memory): it is updated when it holds the same
  GitHub account (`user_id`) or cannot be opened (as before KOR-47, a new connection replaces a record that no
  longer verifies); there is none: it is created. These are the legacy path `cache.put` (`PUT
  /v1/device/credentials/{U}/github`, the hub makes a first record primary).
- else a **new** record with a new UUIDv7 ID: `PUT /v1/device/github-identities/{id}` `{owner_id, record,
  expected_version: 0, request_id}`.

A reconnect of the same GitHub account (`user_id`) without a new signing key carries the replaced version's
`signing_key` over (it stays registered at GitHub and usable); a body with a new key replaces it, another account
takes nothing over, and a version that does not verify or open on this device carries nothing.

An identity record is sealed like `cache.put` seals the primary one: under the newest `CK` epoch of `U` this device
holds (`key_missing` without one), author this device, version = current + 1; `409 version_conflict` reads the
version again and retries (at most `cache.PUT_ATTEMPTS` writes); a read or write lost in transport
(`hub_unreachable`) is sent again after each of `RESPOND_DELAYS`, the write with the same bytes (KOR-18). Another
refusal is `CredentialUnavailable("refused", code)`. After the write the record is read back into the identity cache
(`credentials.identities.fetch`; a failure there is only logged: the hint of the next change reads it again).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from agent_runner.credentials import cache, identities
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.wraps import CRED
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import load_scope_key
from agent_runner.requests.service import RESPOND_DELAYS, Flow
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import SyncRuntime

KIND = "github"


def _target(identity_id: str) -> str:
    return f"/v1/device/github-identities/{identity_id}"


async def _again[T](flow: Flow, call: Callable[[], Awaitable[T]], step: str) -> T:
    """`call()`, sent again after each of `RESPOND_DELAYS` while it fails with `hub_unreachable`."""
    attempt = 0
    while True:
        try:
            return await call()
        except HubTransportError as error:
            if error.reason != "hub_unreachable" or attempt == len(RESPOND_DELAYS):
                raise
            flow.service.line(f"github_connect store_retry step={step} attempt={attempt + 1} {error.describe()}")
            await flow.sleep(RESPOND_DELAYS[attempt])
            attempt += 1


async def identity_version(flow: Flow, identity_id: str) -> int | None:
    """The hub's version of `identity_id` as a record of the requesting user (`identity_record`)."""
    found = await identity_record(flow, identity_id)
    return None if found is None else found[0]


async def identity_record(flow: Flow, identity_id: str) -> tuple[int, object] | None:
    """`(version, record)` of `identity_id` as a record of the requesting user, None after `404`; a record of anybody
    else is `CredentialUnavailable("missing")`, a header that is not a record header `CredentialUnavailable("invalid")`.
    Only the form is checked (as `cache.current_slot` does): the version only feeds `expected_version`, the record is
    verified before it is opened (`_opened_identity`)."""
    client = flow.service.sync.client
    try:
        answer = await _again(flow, lambda: client.request("GET", _target(identity_id)), "read")
    except HubRefusal as refusal:
        if refusal.status != 404:
            raise
        return None
    if (answer.get("owner_id"), answer.get("owner_kind")) != (flow.user, identities.USER):
        raise CredentialUnavailable("missing")
    record = answer.get("record")
    try:
        header = credential_format.check_header(record.get("header") if isinstance(record, dict) else None)
    except CryptoError:
        raise CredentialUnavailable("invalid") from None
    if (header["id"], header["owner"], header["kind"]) != (identity_id, flow.user, KIND):
        raise CredentialUnavailable("invalid")
    return cast(int, header["version"]), record


async def _opened_identity(flow: Flow, record: object) -> dict[str, Any] | None:
    """The body of an identity record of the requesting user, None when it does not verify or open on this device."""
    try:
        header, author = await cache.verify_record(flow.context, flow.user, KIND, record)
    except RecordRefused:
        return None
    async with session(flow.context.pool) as conn:
        key = await load_scope_key(conn, flow.service.sync.storage_key, CRED, flow.user, cast(int, header["epoch"]))
    if key is None:
        return None
    try:
        body = parse_json(credential_format.open_credential(key, cast(dict[str, Any], record), author))
    except CryptoError:
        return None
    return body if isinstance(body, dict) else None


def _carry(body: dict[str, Any], previous: Mapping[str, Any] | None) -> None:
    """A reconnect of the same GitHub account without a new signing key keeps the previous version's key (it stays
    registered at GitHub); a body with a new key, or another account, takes nothing over."""
    if "signing_key" in body or previous is None or previous.get("user_id") != body["user_id"]:
        return
    key = previous.get("signing_key")
    if key is not None:
        body["signing_key"] = key


async def _key(flow: Flow, sync: SyncRuntime) -> tuple[int, bytes]:
    epoch = await cache.newest_epoch(flow.context, flow.user)
    if epoch is None:
        raise CredentialUnavailable("key_missing")
    async with session(flow.context.pool) as conn:
        key = await load_scope_key(conn, sync.storage_key, CRED, flow.user, epoch)
    assert key is not None  # `newest_epoch` names a slot the device holds
    return epoch, key


async def put_identity(flow: Flow, identity_id: str, body: Mapping[str, object], version: int) -> int:
    """Write `body` as version `version + 1` of the user's identity record `identity_id` (module docstring); the
    stored version."""
    sync, identity = flow.service.sync, flow.context.identity
    epoch, key = await _key(flow, sync)
    plaintext = body_json(dict(body))
    for _attempt in range(cache.PUT_ATTEMPTS):
        header = credential_format.new_header(
            id=identity_id,
            owner=flow.user,
            kind=KIND,
            version=version + 1,
            epoch=epoch,
            author=identity.device.device_id,
            ts=time_text(sync.wall()),
        )
        record = credential_format.seal_credential(key, header, plaintext, identity.keys.signing_key())
        sent = {"owner_id": flow.user, "record": record, "expected_version": version, "request_id": flow.id}
        try:
            answer = await _again(
                flow, functools.partial(sync.client.request, "PUT", _target(identity_id), sent), "write"
            )
        except HubRefusal as refusal:
            if refusal.code != "version_conflict":
                raise CredentialUnavailable("refused", refusal.code) from None
            version = await identity_version(flow, identity_id) or 0
            continue
        stored = answer.get("version")
        if not isinstance(stored, int) or isinstance(stored, bool) or stored < 1:
            raise CredentialUnavailable("refused", "response_invalid")
        flow.service.line(f"github_connect stored identity_id={identity_id} version={stored}")
        await _cache(flow, identity_id)
        return stored
    raise CredentialUnavailable("refused", "version_conflict")


async def _cache(flow: Flow, identity_id: str) -> None:
    try:
        await identities.fetch(flow.context, identity_id)
    except Exception as exc:
        flow.service.line(f"github_connect cache_failed identity_id={identity_id} error={type(exc).__name__}")


async def _put_primary(flow: Flow, body: Mapping[str, object]) -> str:
    """The legacy write of the user's primary record; its record ID."""
    await cache.put(flow.context, flow.user, KIND, body, flow.id, delays=RESPOND_DELAYS, sleep=flow.sleep)
    async with session(flow.context.pool) as conn:
        found = await cache.cached(conn, flow.user, KIND)
    assert found is not None  # `put` caches what it stored
    return str(found[1]["header"]["id"])


async def _primary_body(flow: Flow) -> dict[str, Any] | None:
    """`user_id` and `signing_key` of the user's primary record, None when it cannot be read or opened."""
    try:
        await _again(flow, lambda: cache.fetch(flow.context, flow.user, KIND), "read")
        async with cache.opened(flow.context, flow.user, KIND) as opened:
            return {key: opened[key] for key in ("user_id", "signing_key") if key in opened}
    except (RecordRefused, CredentialUnavailable):
        return None


def _account(previous: Mapping[str, Any] | None) -> int | None:
    found = None if previous is None else previous.get("user_id")
    return found if isinstance(found, int) and not isinstance(found, bool) else None


async def store(flow: Flow, body: dict[str, Any], identity_id: str | None, *, replace_primary: bool = False) -> str:
    """Store `body` for the requesting user (module docstring); the record ID of the identity. `replace_primary`
    (the operator's copy for a user of `OPERATOR_GITHUB_USERS`) always writes the primary record, as before KOR-47.
    The signing key of the version replaced is carried over into `body` (`_carry`)."""
    primary_version, primary_id = await _again(flow, lambda: cache.current_slot(flow.context, flow.user, KIND), "read")
    if identity_id is not None:
        if primary_version > 0 and identity_id == primary_id:
            _carry(body, await _primary_body(flow))
            return await _put_primary(flow, body)
        found = await identity_record(flow, identity_id)
        if found is None:
            raise CredentialUnavailable("missing")
        _carry(body, await _opened_identity(flow, found[1]))
        await put_identity(flow, identity_id, body, found[0])
        return identity_id
    if primary_version == 0:
        return await _put_primary(flow, body)
    previous = await _primary_body(flow)
    if replace_primary or _account(previous) in (None, body["user_id"]):
        _carry(body, previous)
        return await _put_primary(flow, body)
    created = uuid7()
    await put_identity(flow, created, body, 0)
    return created
