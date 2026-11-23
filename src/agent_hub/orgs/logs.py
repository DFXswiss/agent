"""The hub's own verified view of a membership log and of the platform log (04-crypto.md sections 6.1, 6.2).

The hub stores only entries it verified, but it never trusts its tables blindly: every rights decision that needs the
log (appends, wraps, catch-up) replays the stored log with `agent_hub.log.verify` against the pinned founder key (the
registered identity of the creator, L3) and the platform log against the configured platform root key. A verified log
is cached per process under `(org, head seq, head hash)`: the hash chain fixes every entry up to the head, so a cached
log can never disagree with the stored one (also not for an append that was rolled back). Certifications in the
platform log never disappear, so a cached membership log stays valid while the platform log grows.

A stored log that does not verify (tampered tables) raises `StoredLogInvalid`; the request then fails with
`503 unavailable` and never acts on it.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.log.platform import PlatformState, verify_platform_log
from agent_hub.log.verify import VerifiedLog, verify_log

Connection = psycopg.Connection[Any]
CACHE_SIZE = 256


class StoredLogInvalid(RuntimeError):
    """A stored log does not verify (or its founder identity is missing); the message never holds log content."""


class LogCache:
    """A small thread-safe LRU cache."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self._size = size
        self._lock = threading.Lock()
        self._items: OrderedDict[Hashable, object] = OrderedDict()

    def get(self, key: Hashable) -> object | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: Hashable, value: object) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


ORG_LOGS = LogCache()
PLATFORM_LOGS = LogCache()


@dataclass(frozen=True)
class OrgRow:
    """One row of `hub.org`."""

    id: UUID
    private: bool
    platform_access: bool
    default_wg: UUID
    created_by: UUID
    created_at: datetime
    head_seq: int
    head_hash: str
    rotation_pending: tuple[tuple[str, str], ...]


_ORG_COLUMNS = (
    "id, private, platform_access, default_wg, created_by, created_at, log_head_seq, log_head_hash, rotation_pending"
)


def _org(record: Sequence[Any]) -> OrgRow:
    pending = tuple((str(item["scope"]), str(item["scope_id"])) for item in record[8])
    return OrgRow(
        id=record[0],
        private=record[1],
        platform_access=record[2],
        default_wg=record[3],
        created_by=record[4],
        created_at=record[5],
        head_seq=record[6],
        head_hash=record[7],
        rotation_pending=pending,
    )


def org_row(connection: Connection, org_id: UUID, *, lock: bool = False, share: bool = False) -> OrgRow | None:
    """The org row (`SELECT ... FOR UPDATE` with `lock`: appends to one org are serialised, 02-data-model.md 2.2;
    `FOR SHARE` with `share`: a writer of data that depends on the log waits for a running append)."""
    suffix = " FOR UPDATE" if lock else " FOR SHARE" if share else ""
    record = connection.execute(f"SELECT {_ORG_COLUMNS} FROM hub.org WHERE id = %s{suffix}", (org_id,)).fetchone()
    return None if record is None else _org(record)


def replace_head(org: OrgRow, seq: int, entry_hash: str) -> OrgRow:
    """The org row with another head (the rebuild derives the head from the stored entries)."""
    return replace(org, head_seq=seq, head_hash=entry_hash)


def identity_keys(connection: Connection, user_id: UUID | str) -> tuple[str, str] | None:
    """The registered identity `(kem, sig)` (b64u) of a user, None for an unknown user."""
    record = connection.execute(
        "SELECT kem_pub, sig_pub FROM hub.user_identity WHERE user_id = %s", (user_id,)
    ).fetchone()
    return None if record is None else (b64u_encode(bytes(record[0])), b64u_encode(bytes(record[1])))


def platform_state(connection: Connection, root_key: bytes) -> PlatformState | None:
    """The verified platform-log state, None while the platform log is empty."""
    head = connection.execute("SELECT seq, entry_hash FROM hub.platform_entry ORDER BY seq DESC LIMIT 1").fetchone()
    if head is None:
        return None
    key = (head[0], head[1], root_key)
    cached = PLATFORM_LOGS.get(key)
    if cached is not None:
        return cast(PlatformState, cached)
    texts = [row[0] for row in connection.execute("SELECT entry_text FROM hub.platform_entry ORDER BY seq")]
    try:
        state = verify_platform_log([json.loads(text) for text in texts], b64u_encode(root_key)).state
    except CryptoError:
        raise StoredLogInvalid("the stored platform log does not verify") from None
    PLATFORM_LOGS.put(key, state)
    return state


def entries(connection: Connection, org_id: UUID, after: int = -1, limit: int | None = None) -> list[dict[str, Any]]:
    """Stored entries with `seq > after` in ascending order (at most `limit`)."""
    rows = connection.execute(
        "SELECT entry_text FROM hub.membership_entry WHERE org_id = %s AND seq > %s ORDER BY seq LIMIT %s",
        (org_id, after, limit),
    )
    return [cast(dict[str, Any], json.loads(row[0])) for row in rows]


def verified_log(connection: Connection, org: OrgRow, platform: PlatformState | None) -> VerifiedLog:
    """The stored log of `org`, verified (cached by head)."""
    key = (org.id, org.head_seq, org.head_hash)
    cached = ORG_LOGS.get(key)
    if cached is not None:
        return cast(VerifiedLog, cached)
    founder = identity_keys(connection, org.created_by)
    if founder is None:
        raise StoredLogInvalid("the founder of a stored log has no registered identity")
    stored = entries(connection, org.id)
    try:
        log = verify_log(stored, founder[1], platform=platform)
    except CryptoError:
        raise StoredLogInvalid("a stored membership log does not verify") from None
    if log.head.seq != org.head_seq or log.head.hash != org.head_hash:
        raise StoredLogInvalid("the stored head of an org does not match its log")
    ORG_LOGS.put(key, log)
    return log


def remember(org_id: UUID, log: VerifiedLog) -> None:
    """Cache a log the hub has just verified while appending to it."""
    ORG_LOGS.put((org_id, log.head.seq, log.head.hash), log)


def platform_access(org_id: UUID, private: bool, platform: PlatformState | None) -> bool:
    """ADR-026 as the platform log states it: a private org, or the latest `org_platform_access` grants it."""
    return private or (platform is not None and platform.platform_access(str(org_id), private))


def member_orgs(connection: Connection, principal_id: UUID | str) -> list[OrgRow]:
    """The orgs in which the principal is an active member, ordered by org ID."""
    columns = ", ".join(f"o.{name.strip()}" for name in _ORG_COLUMNS.split(","))
    rows = connection.execute(
        f"SELECT {columns} FROM hub.org o JOIN hub.org_member m ON m.org_id = o.id"
        " WHERE m.principal_id = %s AND m.removed_seq IS NULL ORDER BY o.id",
        (principal_id,),
    )
    return [_org(record) for record in rows]
