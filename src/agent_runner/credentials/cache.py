"""Credential cache of the runner (05-runner.md section 2.6, 03-api.md section 6.1, 04-crypto.md section 4.4,
02-data-model.md section 4 `credential_cache`).

Records are end-to-end encrypted under the owner's credential key `CK` (scope `cred`, scope ID = owner, one key per
epoch, held sealed with the storage key in `runner.scope_key`). The cache keeps the record exactly as the hub stores it
(`record` jsonb: header, ciphertext, signature) with its `version`; the plaintext body exists only in memory for the
duration of one use (`opened`) and is never written, logged or put into an answer.

- `fetch(context, owner, kind)`: `GET /v1/device/credentials/{owner}/{kind}`; the record is verified before it is
  cached: form and author signature, the header names the owner and kind asked for, the author is the owner or a
  device eligible for the owner's `CK` in an acting org (`crypto.log.wraps.cred_readers`, decertified devices
  excluded; the keys come from the verified logs) and its version is not older than the cached one (no rollback).
  `404` purges the cached record (deleted at the hub: disconnect, 03-api.md section 4.8). A refused record is not
  cached, logged, counted (`verify_failures`) and audited (`runner.inbound_rejected`, source `credential`, target
  `<owner>:<kind>`, metadata only; KOR-07); a hub refusal or transport error leaves the cache unchanged.
- `refresh_owner(context, owner)`: re-reads every kind of an owner (hint `credentials {owner_id}` or
  `credentials_changed` of the desired state; the hub clears its change notice per kind it is read).
- `opened(context, owner, kind)`: the decrypted body for one use; a missing record is fetched first ("on demand").
- `put(context, owner, kind, body, request_id)`: a record authored by this device while answering a device request
  of the owner (`PUT /v1/device/credentials/{owner}/{kind}`): encrypted under the newest `CK` epoch the device holds,
  version = current + 1 (`409 version_conflict` re-reads and retries, at most `PUT_ATTEMPTS` times). The current
  record is only read for its version and ID (`current_slot`), so a record whose author no longer verifies (a removed
  device) can be replaced by a new connection. With `delays` (KOR-18; the request flows pass `RESPOND_DELAYS`) a read
  or write that failed with `hub_unreachable` is sent again after each delay: the write with the **same** record bytes,
  which the hub answers with the stored version when the lost attempt had arrived, so a lost answer never stores a
  second version. Answers that do not verify and refusals are not repeated.

The cache never goes back to an older version (the upsert keeps a newer row).

Purges call the listeners registered with `subscribe(context, listener)` (`listener(owner, kind)`), so the model
gateways (RUN-12) can withdraw a deleted credential from running sessions (05 section 2.6). Before the cached record
is deleted, the listeners of `subscribe_before_purge(context, listener)` get its decrypted body for one use
(`listener(owner, kind, body)`), so the GitHub area can revoke a token the platform minted (`github.revoke`, KOR-40);
a record that does not open is skipped with a fact line, and a failing listener changes nothing. Every record cached
by `fetch` or `put` calls the listeners of `subscribe_stored(context, listener)`, so operations held for the owner's
GitHub connection are checked again at once (KOR-25, 05-runner.md section 12 step 3).
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import b64u_decode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.wraps import CRED, cred_readers
from agent_runner.orgs import audit
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import held_slots, load_scope_key
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import SyncRuntime, runtime

KINDS = tuple(sorted(credential_format.KINDS))
PUT_ATTEMPTS = 3
ATTRIBUTE = "credential_listeners"

Listener = Callable[[str, str], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class CredentialUnavailable(Exception):
    """A credential cannot be used: `missing` (no record), `key_missing` (no `CK` of the record's epoch on this
    device), `invalid` (the record does not open or its body is not an object), `refused` (the hub refused a write:
    `code`), `unenrolled` or `no_actor` (an operation without an acting user, `acting.opened`)."""

    def __init__(self, reason: str, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class RecordRefused(Exception):
    """A fetched record failed verification (`code`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


BeforePurge = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class Listeners:
    items: list[Listener] = field(default_factory=list)
    before: list[tuple[frozenset[str], BeforePurge]] = field(default_factory=list)
    stored: list[Listener] = field(default_factory=list)


def _listeners(context: Any) -> Listeners:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Listeners):
        return found
    created = Listeners()
    setattr(context, ATTRIBUTE, created)
    return created


def subscribe(context: Any, listener: Listener) -> None:
    """Call `listener(owner, kind)` after every purge of a cached record."""
    _listeners(context).items.append(listener)


def subscribe_before_purge(context: Any, kinds: frozenset[str], listener: BeforePurge) -> None:
    """Call `listener(owner, kind, body)` with the decrypted body before a cached record of `kinds` is purged."""
    _listeners(context).before.append((kinds, listener))


def subscribe_stored(context: Any, listener: Listener) -> None:
    """Call `listener(owner, kind)` after every record this device cached (fetched or written)."""
    _listeners(context).stored.append(listener)


async def _stored(context: Any, owner: str, kind: str) -> None:
    for listener in list(_listeners(context).stored):
        try:
            await listener(owner, kind)
        except Exception as exc:
            _line(context, f"listener_failed error={type(exc).__name__}")


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.credentials {text}\n")
    context.log.flush()


def _sync(context: Any) -> SyncRuntime:
    found = runtime(context)
    if found is None:
        raise CredentialUnavailable("unenrolled")
    return found


def _target(owner: str, kind: str) -> str:
    return f"/v1/device/credentials/{owner}/{kind}"


async def cached(conn: AsyncConnection[Any], owner: str, kind: str) -> tuple[int, dict[str, Any]] | None:
    """`(version, record)` of the cached record."""
    cur = await conn.execute(
        "SELECT version, record FROM runner.credential_cache WHERE owner_id = %s AND kind = %s", (owner, kind)
    )
    row = await cur.fetchone()
    return None if row is None else (int(row[0]), cast(dict[str, Any], row[1]))


async def _store(conn: AsyncConnection[Any], owner: str, kind: str, version: int, record: Mapping[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (owner_id, kind) DO UPDATE SET version = EXCLUDED.version, record = EXCLUDED.record "
        "WHERE runner.credential_cache.version <= EXCLUDED.version",
        (owner, kind, version, Jsonb(dict(record))),
    )


async def _before_purge(context: Any, owner: str, kind: str, listeners: list[BeforePurge]) -> None:
    async with session(context.pool) as conn:
        found = await cached(conn, owner, kind)
    if found is None:
        return
    try:
        body = await _open(context, owner, kind, found[1])
    except CredentialUnavailable as error:
        _line(context, f"before_purge_skipped owner={owner} kind={kind} reason={error.reason}")
        return
    try:
        for listener in listeners:
            try:
                await listener(owner, kind, body)
            except Exception as exc:
                _line(context, f"before_purge_failed error={type(exc).__name__}")
    finally:
        body.clear()


async def purge(context: Any, owner: str, kind: str) -> bool:
    """Delete a cached record; True when one was cached (the listeners are called then)."""
    before = [listener for kinds, listener in _listeners(context).before if kind in kinds]
    if before:
        await _before_purge(context, owner, kind, before)
    async with session(context.pool) as conn:
        cur = await conn.execute("DELETE FROM runner.credential_cache WHERE owner_id = %s AND kind = %s", (owner, kind))
    if cur.rowcount == 0:
        return False
    _line(context, f"purged owner={owner} kind={kind}")
    for listener in list(_listeners(context).items):
        try:
            await listener(owner, kind)
        except Exception as exc:
            _line(context, f"listener_failed error={type(exc).__name__}")
    return True


def eligible(context: Any, owner: str) -> bool:
    """This device is an eligible reader of the owner's `CK` in an acting org (the verified logs)."""
    book = book_for(context)
    states = [log.state for org in book.acting_orgs() if (log := book.log(org)) is not None]
    excluded = {device for org in book.acting_orgs() for device in book.excluded(org)}
    return context.identity.device.device_id in cred_readers(states, owner, excluded)


def author_key(context: Any, book: OrgBook, owner: str, author: str) -> bytes:
    """The Ed25519 key of a record's author: this device, or the owner or an eligible device of an acting org."""
    identity = context.identity
    if author == identity.device.device_id:
        return b64u_decode(identity.keys.sig_public)
    states = [log.state for org in book.acting_orgs() if (log := book.log(org)) is not None]
    excluded = {device for org in book.acting_orgs() for device in book.excluded(org)}
    if author not in cred_readers(states, owner, excluded):
        raise RecordRefused("wrap_unauthorized")
    for state in states:
        found = state.member(author)
        if found is not None:
            return b64u_decode(found.sig)
    raise RecordRefused("key_unknown")


async def verify_record(context: Any, owner: str, kind: str, record: object) -> tuple[dict[str, object], bytes]:
    """The checked header and the author's key of a record (`RecordRefused` otherwise)."""
    book = book_for(context)
    async with session(context.pool) as conn:
        await book.ensure_loaded(conn)
    try:
        header = credential_format.check_header(record.get("header") if isinstance(record, dict) else None)
        if (header["owner"], header["kind"]) != (owner, kind):
            raise RecordRefused("header_invalid")
        key = author_key(context, book, owner, str(header["author"]))
        credential_format.verify_credential(record, key)
    except CryptoError as error:
        raise RecordRefused(error.code) from None
    return header, key


async def fetch(context: Any, owner: str, kind: str) -> dict[str, Any] | None:
    """Fetch, verify and cache one record; None after a `404` (purged). Hub refusals other than `404` and transport
    errors propagate; a refused record raises `RecordRefused` (logged and counted)."""
    sync = _sync(context)
    try:
        answer = await sync.client.request("GET", _target(owner, kind))
    except HubRefusal as refusal:
        if refusal.status != 404:
            raise
        await purge(context, owner, kind)
        return None
    record = answer.get("record")
    try:
        header, _key = await verify_record(context, owner, kind, record)
        async with session(context.pool) as conn:
            current = await cached(conn, owner, kind)
            if current is not None and current[0] > cast(int, header["version"]):
                raise RecordRefused("version_conflict")
            await _store(conn, owner, kind, cast(int, header["version"]), cast(dict[str, Any], record))
    except RecordRefused as refused:
        _line(context, f"record_refused owner={owner} kind={kind} code={refused.code}")
        async with session(context.pool) as conn, conn.transaction():
            await audit.reject(
                context,
                sync,
                conn,
                source="credential",
                code=refused.code,
                target_kind="credential",
                target_id=f"{owner}:{kind}",
            )
        raise
    await _stored(context, owner, kind)
    return cast(dict[str, Any], record)


async def refresh_owner(context: Any, owner: str) -> None:
    """Re-read every kind of the owner; a failing kind is logged and the others are still read."""
    for kind in KINDS:
        try:
            await fetch(context, owner, kind)
        except Exception as exc:
            detail = f" code={exc.code}" if isinstance(exc, (HubRefusal, RecordRefused)) else ""
            _line(context, f"refresh_failed owner={owner} kind={kind} error={type(exc).__name__}{detail}")


async def _key(context: Any, owner: str, epoch: int) -> bytes:
    async with session(context.pool) as conn:
        key = await load_scope_key(conn, _sync(context).storage_key, CRED, owner, epoch)
    if key is None:
        raise CredentialUnavailable("key_missing")
    return key


@asynccontextmanager
async def opened(context: Any, owner: str, kind: str) -> AsyncIterator[dict[str, Any]]:
    """The decrypted body of the owner's credential for the duration of the block (fetched first when not cached)."""
    async with session(context.pool) as conn:
        found = await cached(conn, owner, kind)
    try:
        record = found[1] if found is not None else await fetch(context, owner, kind)
    except RecordRefused:
        raise CredentialUnavailable("invalid") from None
    if record is None:
        raise CredentialUnavailable("missing")
    body = await _open(context, owner, kind, record)
    try:
        yield body
    finally:
        body.clear()


async def _open(context: Any, owner: str, kind: str, record: object) -> dict[str, Any]:
    """The decrypted body of a record (verified first); `CredentialUnavailable("invalid")` when it does not open."""
    try:
        header, author = await verify_record(context, owner, kind, record)
    except RecordRefused:
        raise CredentialUnavailable("invalid") from None
    key = await _key(context, owner, cast(int, header["epoch"]))
    try:
        body = parse_json(credential_format.open_credential(key, cast(dict[str, Any], record), author))
    except CryptoError:
        raise CredentialUnavailable("invalid") from None
    if not isinstance(body, dict):
        raise CredentialUnavailable("invalid")
    return body


async def newest_epoch(context: Any, owner: str) -> int | None:
    """The newest `CK` epoch of the owner this device holds."""
    async with session(context.pool) as conn:
        slots = await held_slots(conn)
    epochs = [epoch for scope, scope_id, epoch in slots if scope == CRED and scope_id == owner]
    return max(epochs, default=None)


async def current_slot(context: Any, owner: str, kind: str) -> tuple[int, str]:
    """`(version, record ID)` of the hub's current record, `(0, new ID)` without one. Only the header form is checked:
    the values only feed `expected_version` (checked by the hub) and the ID of the replacing record, so a record whose
    author can no longer be verified (e.g. a removed device) does not block a new connection."""
    try:
        answer = await _sync(context).client.request("GET", _target(owner, kind))
    except HubRefusal as refusal:
        if refusal.status != 404:
            raise
        await purge(context, owner, kind)
        return 0, uuid7()
    record = answer.get("record")
    try:
        header = credential_format.check_header(record.get("header") if isinstance(record, dict) else None)
    except CryptoError:
        raise CredentialUnavailable("invalid") from None
    return cast(int, header["version"]), str(header["id"])


async def _transient[T](
    context: Any, call: Callable[[], Awaitable[T]], delays: Sequence[float], sleep: Sleep | None, step: str
) -> T:
    """`call()`, sent again after each of `delays` while it fails with `hub_unreachable` (module docstring)."""
    attempt = 0
    while True:
        try:
            return await call()
        except HubTransportError as error:
            if error.reason != "hub_unreachable" or sleep is None or attempt == len(delays):
                raise
            _line(context, f"store_retry step={step} attempt={attempt + 1} {error.describe()}")
            await sleep(delays[attempt])
            attempt += 1


async def put(
    context: Any,
    owner: str,
    kind: str,
    body: Mapping[str, object],
    request_id: str,
    *,
    delays: Sequence[float] = (),
    sleep: Sleep | None = None,
) -> int:
    """Write the owner's credential as this device (answering request `request_id`); the stored version. `delays` and
    `sleep` repeat a read or write lost in transport (module docstring)."""
    sync, identity = _sync(context), context.identity
    epoch = await newest_epoch(context, owner)
    if epoch is None:
        raise CredentialUnavailable("key_missing")
    key = await _key(context, owner, epoch)
    plaintext = body_json(dict(body))
    target = _target(owner, kind)
    for _attempt in range(PUT_ATTEMPTS):
        version, record_id = await _transient(
            context, lambda: current_slot(context, owner, kind), delays, sleep, "read"
        )
        header = credential_format.new_header(
            id=record_id,
            owner=owner,
            kind=kind,
            version=version + 1,
            epoch=epoch,
            author=identity.device.device_id,
            ts=time_text(sync.wall()),
        )
        record = credential_format.seal_credential(key, header, plaintext, identity.keys.signing_key())
        body_out = {"record": record, "expected_version": version, "request_id": request_id}
        try:
            answer = await _transient(
                context, functools.partial(sync.client.request, "PUT", target, body_out), delays, sleep, "write"
            )
        except HubRefusal as refusal:
            if refusal.code == "version_conflict":
                continue
            raise CredentialUnavailable("refused", refusal.code) from None
        stored = answer.get("version")
        if not isinstance(stored, int) or isinstance(stored, bool) or stored < 1:
            raise CredentialUnavailable("refused", "response_invalid")
        async with session(context.pool) as conn:
            await _store(conn, owner, kind, stored, record)
        _line(context, f"stored owner={owner} kind={kind} version={stored}")
        await _stored(context, owner, kind)
        return stored
    raise CredentialUnavailable("refused", "version_conflict")
