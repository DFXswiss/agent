"""GitHub identity records on the device (KOR-47, ADR-078 section 1; 03-api.md section 6.1
`GET /v1/device/github-identities/{id}`; 04-crypto.md section 4.4; 02-data-model.md section 4 `github_identity_cache`).

A GitHub identity is one `github` credential record, named by its record ID. Its owner is a user (a personal account)
or an organization (a company account). Both are sealed in the `cred` scope of their owner (scope ID = the user or
the organization, one key per epoch, held sealed with the storage key in `runner.scope_key`; for an organization the
readers are its member devices, docs/platform/key-model.md KM-35). A user's primary record also stays in the legacy
cache (`cache.py`, `credential_cache`), which is what `opened(context, owner, "github")` yields as before.

- `fetch(context, id)` (also called by the connect flow after it stored a record): `GET
  /v1/device/github-identities/{id}` -> `{owner_id, owner_kind, primary, record}`. The
  record is verified before it is cached: the answer's form, the header (form, `id` = the asked ID, `owner` = the
  answer's owner, `kind` = `github`), the author signature with the author rule of the owner's kind - a user's record
  is authored by the user or a device eligible for the user's `CK` (`cache.author_key`), an organization's record by
  an **active admin** of that organization in the verified membership log (decertified devices are no authors) - and
  no rollback (a version older than the cached one is `version_conflict`). A hub that names another owner kind cannot
  pass: a record named `user` whose owner is an organization the device knows is `header_invalid` (its member devices
  read the organization's `cred` scope but never author its records), and no user ID has an organization log. `404`
  purges the cached row (deleted at the hub); a refused record is not cached, logged, counted and audited
  (`runner.inbound_rejected`, source `credential`, target the record ID); hub refusals other than `404` and transport
  errors propagate.
- `meta(context, id)`: the cached row, fetched on demand when missing (None after `404`).
- `sync_owner(context, owner)`: `GET /v1/device/github-identities?owner_id=` lists the metadata of every `github`
  record of an owner the device is eligible for; records the list names and the cache does not hold in that version
  are fetched (each verified as above), cached records of the owner the list no longer names are purged (deleted at
  the hub). The list is only a hint for what to fetch: nothing of it is trusted beyond the IDs and versions, and an
  entry for another owner or out of form makes the whole answer unusable. A `404` (not eligible, or a hub without the
  list), any other refusal or a transport error means no list (False): nothing is purged.
- `known(context, owner)`: the cached records of the owner, listed first (`sync_owner`) when none is cached.
- `refresh(context, owner)`: the hint `credentials {owner_id}` and `credentials_changed` of the desired state: the
  legacy kinds of a user (`cache.refresh_owner`; skipped for an organization of the device, which has no legacy
  records) and the owner's list (`sync_owner`); without a list every cached identity record of the owner is read
  again by its ID.
- Purges (`404`, or gone from the list) first hand the decrypted body to the listeners of
  `cache.subscribe_before_purge(context, kinds, listener)` with `(owner, "github", body)`, so a token this device
  minted is revoked at GitHub (`github.revoke`, KOR-40) for an identity record as for the legacy record; a record that
  does not open is skipped with a fact line and a failing listener changes nothing.
- `opened(context, row)`: the decrypted body for one use (verified again, key of the record's epoch); the body is
  never written or logged.
- `generation(conn)`: a positive 31-bit number that changes whenever a cached `github` record, a cached identity
  record, a session's or task's choice or a default changes (None when the device has none of them). Held operations
  remember it instead of one record's version (`operations.pipeline`, KOR-25): a refused check is repeated once any
  of them changed.

Every record cached by `fetch` calls the listeners of `cache.subscribe_stored(context, listener)` with
`(owner, "github")`, so held operations are checked again at once.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.credentials import cache
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.orgs import audit
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session
from agent_runner.reconcile import wire
from agent_runner.sync.http import HubRefusal, HubTransportError

KIND = "github"
USER = "user"
ORG = "org"
OWNER_KINDS = frozenset({USER, ORG})


@dataclass(frozen=True)
class Identity:
    """A cached, verified identity record (metadata; the body stays encrypted)."""

    id: str
    owner: str
    owner_kind: str
    primary: bool
    version: int
    record: Mapping[str, Any]


LIST_TARGET = "/v1/device/github-identities"


def _target(identity_id: str) -> str:
    return f"/v1/device/github-identities/{identity_id}"


COLUMNS = "id::text, owner_id::text, owner_kind, is_primary, version, record"


def _row(row: tuple[Any, ...]) -> Identity:
    return Identity(str(row[0]), str(row[1]), str(row[2]), bool(row[3]), int(row[4]), cast(dict[str, Any], row[5]))


async def cached(conn: AsyncConnection[Any], identity_id: str) -> Identity | None:
    cur = await conn.execute(f"SELECT {COLUMNS} FROM runner.github_identity_cache WHERE id = %s", (identity_id,))
    row = await cur.fetchone()
    return None if row is None else _row(row)


async def cached_of(conn: AsyncConnection[Any], owner: str) -> list[Identity]:
    """Every cached identity record of `owner`, by ID."""
    cur = await conn.execute(
        f"SELECT {COLUMNS} FROM runner.github_identity_cache WHERE owner_id = %s ORDER BY id", (owner,)
    )
    return [_row(row) for row in await cur.fetchall()]


async def cached_ids(conn: AsyncConnection[Any]) -> list[str]:
    """The IDs of every cached identity record, sorted."""
    cur = await conn.execute("SELECT id::text FROM runner.github_identity_cache ORDER BY id")
    return [str(row[0]) for row in await cur.fetchall()]


async def _store(conn: AsyncConnection[Any], found: Identity) -> None:
    await conn.execute(
        "INSERT INTO runner.github_identity_cache (id, owner_id, owner_kind, is_primary, version, record)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET owner_id = EXCLUDED.owner_id,"
        " owner_kind = EXCLUDED.owner_kind, is_primary = EXCLUDED.is_primary, version = EXCLUDED.version,"
        " record = EXCLUDED.record WHERE runner.github_identity_cache.version <= EXCLUDED.version",
        (found.id, found.owner, found.owner_kind, found.primary, found.version, Jsonb(dict(found.record))),
    )


async def _before_purge(context: Any, identity_id: str) -> None:
    listeners = [listener for kinds, listener in cache._listeners(context).before if KIND in kinds]
    async with session(context.pool) as conn:
        found = await cached(conn, identity_id)
    if not listeners or found is None:
        return
    try:
        async with opened(context, found) as body:
            for listener in listeners:
                try:
                    await listener(found.owner, KIND, body)
                except Exception as exc:
                    cache._line(context, f"before_purge_failed error={type(exc).__name__}")
    except CredentialUnavailable as error:
        cache._line(context, f"before_purge_skipped id={identity_id} reason={error.reason}")


async def purge(context: Any, identity_id: str) -> bool:
    """Delete a cached identity record (after the before-purge listeners); True when one was cached."""
    await _before_purge(context, identity_id)
    async with session(context.pool) as conn:
        cur = await conn.execute("DELETE FROM runner.github_identity_cache WHERE id = %s", (identity_id,))
    if cur.rowcount == 0:
        return False
    cache._line(context, f"identity_purged id={identity_id}")
    return True


def _author_key(context: Any, book: OrgBook, owner: str, owner_kind: str, author: str) -> bytes:
    if owner_kind == USER:
        if owner in book.logs:  # an organization's ID is never a user's
            raise RecordRefused("header_invalid")
        return cache.author_key(context, book, owner, author)
    log = book.log(owner)
    admin = None if log is None or author in book.excluded(owner) else log.state.member(author)
    if admin is None or admin.role != "admin":
        raise RecordRefused("wrap_unauthorized")
    return b64u_decode(admin.sig)


def _answer(identity_id: str, answer: Mapping[str, object]) -> tuple[str, str, bool, object]:
    owner_kind, primary = answer.get("owner_kind"), answer.get("primary")
    try:
        owner = wire.uuid_text(answer.get("owner_id"), "github_identity")
    except wire.ResponseInvalid:
        raise RecordRefused("header_invalid") from None
    if owner_kind not in OWNER_KINDS or not isinstance(primary, bool):
        raise RecordRefused("header_invalid")
    return owner, str(owner_kind), primary and owner_kind == USER, answer.get("record")


async def verify(context: Any, identity_id: str, owner: str, owner_kind: str, record: object) -> tuple[int, bytes]:
    """`(version, author key)` of a record of `owner` (`RecordRefused` otherwise; see the module docstring)."""
    book = book_for(context)
    async with session(context.pool) as conn:
        await book.ensure_loaded(conn)
    try:
        header = credential_format.check_header(record.get("header") if isinstance(record, dict) else None)
        if (header["id"], header["owner"], header["kind"]) != (identity_id, owner, KIND):
            raise RecordRefused("header_invalid")
        key = _author_key(context, book, owner, owner_kind, str(header["author"]))
        credential_format.verify_credential(record, key)
    except CryptoError as error:
        raise RecordRefused(error.code) from None
    return cast(int, header["version"]), key


async def fetch(context: Any, identity_id: str) -> Identity | None:
    """Fetch, verify and cache one identity record; None after a `404` (purged)."""
    sync = cache._sync(context)
    try:
        answer = await sync.client.request("GET", _target(identity_id))
    except HubRefusal as refusal:
        if refusal.status != 404:
            raise
        await purge(context, identity_id)
        return None
    try:
        owner, owner_kind, primary, record = _answer(identity_id, answer)
        version, _key = await verify(context, identity_id, owner, owner_kind, record)
        found = Identity(identity_id, owner, owner_kind, primary, version, cast(dict[str, Any], record))
        async with session(context.pool) as conn:
            current = await cached(conn, identity_id)
            if current is not None and current.version > version:
                raise RecordRefused("version_conflict")
            await _store(conn, found)
    except RecordRefused as refused:
        cache._line(context, f"identity_refused id={identity_id} code={refused.code}")
        async with session(context.pool) as conn, conn.transaction():
            await audit.reject(
                context,
                sync,
                conn,
                source="credential",
                code=refused.code,
                target_kind="credential",
                target_id=identity_id,
            )
        raise
    await cache._stored(context, owner, KIND)
    return found


async def meta(context: Any, identity_id: str) -> Identity | None:
    """The cached identity record, fetched when it is not cached (None when the hub has none)."""
    async with session(context.pool) as conn:
        found = await cached(conn, identity_id)
    return found if found is not None else await fetch(context, identity_id)


async def refresh_identities(context: Any, owner: str) -> None:
    """Re-read every cached identity record of the owner; a failing one is logged and the others are still read."""
    async with session(context.pool) as conn:
        ids = [found.id for found in await cached_of(conn, owner)]
    for identity_id in ids:
        try:
            await fetch(context, identity_id)
        except Exception as exc:
            detail = f" code={exc.code}" if isinstance(exc, (HubRefusal, RecordRefused)) else ""
            cache._line(context, f"identity_refresh_failed id={identity_id} error={type(exc).__name__}{detail}")


def _listed(owner: str, answer: Mapping[str, object]) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in wire.array(answer.get("identities"), "identities"):
        data = wire.obj(item, "identities")
        if wire.uuid_text(data.get("owner_id"), "identities") != owner:
            raise wire.ResponseInvalid("identities")
        found[wire.uuid_text(data.get("id"), "identities")] = wire.seq(data.get("version"), "identities", 1)
    return found


async def sync_owner(context: Any, owner: str) -> bool:
    """Fetch what the owner's list names anew, purge what it no longer names (module docstring); False without a
    list."""
    try:
        answer = await cache._sync(context).client.request("GET", f"{LIST_TARGET}?owner_id={owner}")
        listed = _listed(owner, answer)
    except (HubRefusal, HubTransportError, wire.ResponseInvalid) as exc:
        cache._line(context, f"identity_list_failed owner={owner} error={type(exc).__name__}")
        return False
    async with session(context.pool) as conn:
        held = {found.id: found.version for found in await cached_of(conn, owner)}
    for identity_id in sorted(set(held) - set(listed)):
        await purge(context, identity_id)
    for identity_id, version in sorted(listed.items()):
        if held.get(identity_id, 0) >= version:
            continue
        try:
            await fetch(context, identity_id)
        except Exception as exc:
            detail = f" code={exc.code}" if isinstance(exc, (HubRefusal, RecordRefused)) else ""
            cache._line(context, f"identity_refresh_failed id={identity_id} error={type(exc).__name__}{detail}")
    return True


async def known(context: Any, owner: str) -> list[Identity]:
    """The cached records of the owner, listed at the hub first when none is cached."""
    async with session(context.pool) as conn:
        found = await cached_of(conn, owner)
    if found:
        return found
    await sync_owner(context, owner)
    async with session(context.pool) as conn:
        return await cached_of(conn, owner)


async def refresh(context: Any, owner: str) -> None:
    """The legacy kinds of a user and the owner's identity records (module docstring)."""
    book = book_for(context)
    async with session(context.pool) as conn:
        await book.ensure_loaded(conn)
    if owner not in book.acting_orgs():
        await cache.refresh_owner(context, owner)
    if not await sync_owner(context, owner):
        await refresh_identities(context, owner)


@asynccontextmanager
async def opened(context: Any, found: Identity) -> AsyncIterator[dict[str, Any]]:
    """The decrypted body of an identity record for the duration of the block (`CredentialUnavailable`)."""
    try:
        _version, author = await verify(context, found.id, found.owner, found.owner_kind, found.record)
    except RecordRefused:
        raise CredentialUnavailable("invalid") from None
    header = cast(dict[str, Any], found.record["header"])
    key = await cache._key(context, found.owner, int(header["epoch"]))
    try:
        body = parse_json(credential_format.open_credential(key, dict(found.record), author))
    except CryptoError:
        raise CredentialUnavailable("invalid") from None
    if not isinstance(body, dict):
        raise CredentialUnavailable("invalid")
    try:
        yield body
    finally:
        body.clear()


async def primary_id(conn: AsyncConnection[Any], user: str) -> str | None:
    """The record ID of the user's cached primary record (legacy cache), None without one."""
    found = await cache.cached(conn, user, KIND)
    if found is None:
        return None
    header = found[1].get("header")
    record_id = header.get("id") if isinstance(header, dict) else None
    return record_id if isinstance(record_id, str) else None


GENERATION_SQL = (
    "SELECT string_agg(x, ',' ORDER BY x) FROM ("
    " SELECT 'c:' || owner_id || ':' || version FROM runner.credential_cache WHERE kind = 'github'"
    " UNION ALL SELECT 'i:' || id || ':' || version FROM runner.github_identity_cache"
    " UNION ALL SELECT 's:' || session_id || ':' || COALESCE(github_identity::text, '') || ':'"
    " || COALESCE(task_github_identity::text, '') FROM runner.github_selection"
    " UNION ALL SELECT 'd:' || scope || ':' || scope_id || ':' || identity_id FROM runner.github_default) AS t(x)"
)


async def generation(conn: AsyncConnection[Any]) -> int | None:
    """See the module docstring; 1 .. 2^31 - 1."""
    cur = await conn.execute(GENERATION_SQL)
    row = await cur.fetchone()
    text = None if row is None else row[0]  # an aggregate always answers one row
    if text is None:
        return None
    return int.from_bytes(hashlib.sha256(str(text).encode()).digest()[:4], "big") % 0x7FFFFFFF + 1
