"""Claude subscription records on the device (KOR-69, ADR-090 sections 1 and 2; 03-api.md section 6.1
`/v1/device/subscriptions`; 04-crypto.md section 4.4; 02-data-model.md section 4 `subscription_cache`).

A subscription is one `claude_subscription` record of a user, named by its record ID, sealed in the user's `cred`
scope like every record of the user. The primary record is the one of the legacy cache (`cache.py`); the further ones
are created by this device's logins (`store_login`) and by the owner's clients.

- `fetch(context, id)`: `GET /v1/device/subscriptions/{id}` -> `{owner_id, primary, record}`, verified before it is
  cached: the header names the asked ID, the answer's owner and the kind, the author is the owner, the owner's agent
  or an eligible device (`cache.verify_record`), and no rollback (a version older than the cached one is
  `version_conflict`). `404` purges the cached row; a refused record is not cached, logged and audited
  (`runner.inbound_rejected`, source `credential`, target the record ID); other refusals and transport errors
  propagate.
- `sync_owner(context, owner)`: `GET /v1/device/subscriptions?owner_id=` lists the owner's records; records the list
  names in a newer version than cached are fetched, cached ones it no longer names are purged. The list is a hint only
  (IDs and versions); an entry of another owner or out of form makes the whole answer unusable. No list (a refusal, a
  transport error, a malformed answer) purges nothing (False).
- The `credentials` hint and the reconcile step `credentials` call `sync_owner` for every changed owner, so a changed
  use or a new login of a shared subscription reaches the pool (`model.operator`).
- `opened(context, found)`: the decrypted body for one use (verified again); never written or logged.
- `store_login(context, owner, id, fields, request_id)`: the login of a further subscription (ADR-090 section 2): the
  current record is read (`fetch`; none: version 0), its `name`, `note` and `use` are kept unless `fields` names them,
  the new body is sealed under the newest `CK` epoch this device holds (author this device) and stored with `PUT
  /v1/device/subscriptions/{id}` while answering `request_id`; a concurrent write (`version_conflict`) is retried up to
  `cache.PUT_ATTEMPTS` times. The primary record is never written here (`refused primary`): it keeps the form of the
  endpoints from before.
- `reseal(context, found)`: a further subscription under an older epoch than the newest one this device holds is
  sealed again under it (same body, version + 1, without a request; ADR-090 section 1); a concurrent write leaves it
  for the next round.

`CredentialUnavailable` reasons: `key_missing` (no `CK` epoch held), `invalid` (a record that does not verify or open),
`refused <code>` (the hub refused the write).
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.credentials import cache
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused, Sleep
from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.orgs import audit
from agent_runner.orgs.db import session
from agent_runner.reconcile import wire
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError

KIND = "claude_subscription"
LIST_TARGET = "/v1/device/subscriptions"
KEPT = ("name", "note", "use")
"""Body fields a new login keeps from the record it replaces."""


@dataclass(frozen=True)
class Subscription:
    """A cached, verified subscription record (metadata; the body stays encrypted)."""

    id: str
    owner: str
    primary: bool
    version: int
    record: Mapping[str, Any]

    @property
    def epoch(self) -> int:
        return int(cast(Mapping[str, Any], self.record["header"])["epoch"])


COLUMNS = "id::text, owner_id::text, is_primary, version, record"


def _row(row: tuple[Any, ...]) -> Subscription:
    return Subscription(str(row[0]), str(row[1]), bool(row[2]), int(row[3]), cast(dict[str, Any], row[4]))


def _target(record_id: str) -> str:
    return f"{LIST_TARGET}/{record_id}"


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.subscriptions {text}\n")
    context.log.flush()


async def cached(conn: AsyncConnection[Any], record_id: str) -> Subscription | None:
    cur = await conn.execute(f"SELECT {COLUMNS} FROM runner.subscription_cache WHERE id = %s", (record_id,))
    row = await cur.fetchone()
    return None if row is None else _row(row)


async def cached_of(conn: AsyncConnection[Any], owner: str) -> list[Subscription]:
    """Every cached subscription record of `owner`, by ID."""
    cur = await conn.execute(
        f"SELECT {COLUMNS} FROM runner.subscription_cache WHERE owner_id = %s ORDER BY id", (owner,)
    )
    return [_row(row) for row in await cur.fetchall()]


async def _store(conn: AsyncConnection[Any], found: Subscription) -> None:
    await conn.execute(
        "INSERT INTO runner.subscription_cache (id, owner_id, is_primary, version, record)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET owner_id = EXCLUDED.owner_id,"
        " is_primary = EXCLUDED.is_primary, version = EXCLUDED.version, record = EXCLUDED.record"
        " WHERE runner.subscription_cache.version <= EXCLUDED.version",
        (found.id, found.owner, found.primary, found.version, Jsonb(dict(found.record))),
    )


async def purge(context: Any, record_id: str) -> bool:
    """Delete a cached record; True when one was cached."""
    async with session(context.pool) as conn:
        cur = await conn.execute("DELETE FROM runner.subscription_cache WHERE id = %s", (record_id,))
    if cur.rowcount == 0:
        return False
    _line(context, f"purged id={record_id}")
    return True


async def verify(context: Any, record_id: str, owner: str, record: object) -> tuple[dict[str, object], bytes]:
    """The checked header and the author's key (`RecordRefused` otherwise)."""
    header, key = await cache.verify_record(context, owner, KIND, record)
    if header["id"] != record_id:
        raise RecordRefused("header_invalid")
    return header, key


def _answer(answer: Mapping[str, object]) -> tuple[str, bool, object]:
    primary = answer.get("primary")
    try:
        owner = wire.uuid_text(answer.get("owner_id"), "subscription")
    except wire.ResponseInvalid:
        raise RecordRefused("header_invalid") from None
    if not isinstance(primary, bool):
        raise RecordRefused("header_invalid")
    return owner, primary, answer.get("record")


async def fetch(context: Any, record_id: str) -> Subscription | None:
    """Fetch, verify and cache one record; None after a `404` (purged)."""
    sync = cache._sync(context)
    try:
        answer = await sync.client.request("GET", _target(record_id))
    except HubRefusal as refusal:
        if refusal.status != 404:
            raise
        await purge(context, record_id)
        return None
    try:
        owner, primary, record = _answer(answer)
        header, _key = await verify(context, record_id, owner, record)
        found = Subscription(record_id, owner, primary, cast(int, header["version"]), cast(dict[str, Any], record))
        async with session(context.pool) as conn:
            current = await cached(conn, record_id)
            if current is not None and current.version > found.version:
                raise RecordRefused("version_conflict")
            await _store(conn, found)
    except RecordRefused as refused:
        _line(context, f"record_refused id={record_id} code={refused.code}")
        async with session(context.pool) as conn, conn.transaction():
            await audit.reject(
                context,
                sync,
                conn,
                source="credential",
                code=refused.code,
                target_kind="credential",
                target_id=record_id,
            )
        raise
    return found


def _listed(owner: str, answer: Mapping[str, object]) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in wire.array(answer.get("subscriptions"), "subscriptions"):
        data = wire.obj(item, "subscriptions")
        if wire.uuid_text(data.get("owner_id"), "subscriptions") != owner:
            raise wire.ResponseInvalid("subscriptions")
        found[wire.uuid_text(data.get("id"), "subscriptions")] = wire.seq(data.get("version"), "subscriptions", 1)
    return found


async def sync_owner(context: Any, owner: str) -> bool:
    """Fetch what the owner's list names anew, purge what it no longer names (module docstring)."""
    try:
        answer = await cache._sync(context).client.request("GET", f"{LIST_TARGET}?owner_id={owner}")
        listed = _listed(owner, answer)
    except (HubRefusal, HubTransportError, wire.ResponseInvalid) as exc:
        _line(context, f"list_failed owner={owner} error={type(exc).__name__}")
        return False
    async with session(context.pool) as conn:
        held = {found.id: found.version for found in await cached_of(conn, owner)}
    for record_id in sorted(set(held) - set(listed)):
        await purge(context, record_id)
    for record_id, version in sorted(listed.items()):
        if held.get(record_id, 0) >= version:
            continue
        try:
            await fetch(context, record_id)
        except Exception as exc:
            detail = f" code={exc.code}" if isinstance(exc, (HubRefusal, RecordRefused)) else ""
            _line(context, f"fetch_failed id={record_id} error={type(exc).__name__}{detail}")
    return True


async def _open(context: Any, found: Subscription) -> dict[str, Any]:
    try:
        header, author = await verify(context, found.id, found.owner, found.record)
    except RecordRefused:
        raise CredentialUnavailable("invalid") from None
    key = await cache._key(context, found.owner, cast(int, header["epoch"]))
    try:
        body = parse_json(credential_format.open_credential(key, dict(found.record), author))
    except CryptoError:
        raise CredentialUnavailable("invalid") from None
    if not isinstance(body, dict):
        raise CredentialUnavailable("invalid")
    return body


@asynccontextmanager
async def opened(context: Any, found: Subscription) -> AsyncIterator[dict[str, Any]]:
    """The decrypted body of a record for the duration of the block (`CredentialUnavailable`)."""
    body = await _open(context, found)
    try:
        yield body
    finally:
        body.clear()


async def _put(
    context: Any,
    owner: str,
    record_id: str,
    version: int,
    body: Mapping[str, object],
    *,
    request_id: str | None,
    delays: Sequence[float] = (),
    sleep: Sleep | None = None,
) -> int | None:
    """Seal `body` as version `version + 1` under the newest epoch and write it; None after a `version_conflict`."""
    sync, identity = cache._sync(context), context.identity
    epoch = await cache.newest_epoch(context, owner)
    if epoch is None:
        raise CredentialUnavailable("key_missing")
    key = await cache._key(context, owner, epoch)
    header = credential_format.new_header(
        id=record_id,
        owner=owner,
        kind=KIND,
        version=version + 1,
        epoch=epoch,
        author=identity.device.device_id,
        ts=time_text(sync.wall()),
    )
    record = credential_format.seal_credential(key, header, body_json(dict(body)), identity.keys.signing_key())
    out: dict[str, object] = {"owner_id": owner, "record": record, "expected_version": version}
    if request_id is not None:
        out["request_id"] = request_id
    call = functools.partial(sync.client.request, "PUT", _target(record_id), out)
    try:
        answer = await cache._transient(context, call, delays, sleep, "write")
    except HubRefusal as refusal:
        if refusal.code == "version_conflict":
            return None
        raise CredentialUnavailable("refused", refusal.code) from None
    stored = answer.get("version")
    if not isinstance(stored, int) or isinstance(stored, bool) or stored < 1:
        raise CredentialUnavailable("refused", "response_invalid")
    async with session(context.pool) as conn:
        await _store(conn, Subscription(record_id, owner, False, stored, record))
    _line(context, f"stored owner={owner} id={record_id} version={stored}")
    return stored


async def store_login(
    context: Any,
    owner: str,
    record_id: str,
    fields: Mapping[str, object],
    request_id: str,
    *,
    delays: Sequence[float] = (),
    sleep: Sleep | None = None,
) -> int:
    """Write the login of a further subscription (module docstring); the stored version."""
    for _attempt in range(cache.PUT_ATTEMPTS):
        current = await cache._transient(context, lambda: fetch(context, record_id), delays, sleep, "read")
        kept: dict[str, object] = {}
        version = 0
        if current is not None:
            if current.primary or current.owner != owner:
                raise CredentialUnavailable("refused", "primary" if current.primary else "owner")
            async with opened(context, current) as previous:
                kept = {name: previous[name] for name in KEPT if name in previous}
            version = current.version
        body = {**kept, **fields}
        try:
            stored = await _put(
                context, owner, record_id, version, body, request_id=request_id, delays=delays, sleep=sleep
            )
        finally:
            body.clear()
        if stored is not None:
            return stored
    raise CredentialUnavailable("refused", "version_conflict")


async def reseal(context: Any, found: Subscription) -> bool:
    """Seal a further subscription under the newest epoch this device holds (module docstring); True when written."""
    newest = await cache.newest_epoch(context, found.owner)
    if found.primary or newest is None or found.epoch >= newest:
        return False
    try:
        async with opened(context, found) as body:
            stored = await _put(context, found.owner, found.id, found.version, body, request_id=None)
    except (CredentialUnavailable, HubRefusal, HubTransportError) as exc:
        reason = exc.reason if isinstance(exc, CredentialUnavailable) else type(exc).__name__
        _line(context, f"reseal_failed id={found.id} reason={reason}")
        return False
    if stored is None:
        return False
    _line(context, f"resealed id={found.id} epoch={newest}")
    return True
