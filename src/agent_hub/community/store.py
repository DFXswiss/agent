"""The community area's database work (14-community.md sections 2 and 4, ADR-069).

Every function runs inside the caller's transaction (READ COMMITTED, as `append_audit` requires) and raises `HubError`
for a refusal. The hub never sees a name or a text: messages are verified (format, size, author signature) and stored
as they are; a deletion erases header, ciphertext and signature and keeps the tombstone. Messages get the next `seq`
after the newest under the advisory lock `MESSAGE_LOCK`, so `seq` is gapless and ascending in commit order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.agents.store import acting_user, agent_authors, agent_keys
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.community import crypto as community_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import ERROR_STATUSES, HubError

Connection = psycopg.Connection[Any]

MESSAGE_LOCK = 742180126
"""Advisory lock that serialises the `seq` assignment of community messages."""
MAX_CT_CHARS = (community_format.MAX_CT_BYTES * 4 + 2) // 3
"""The b64u length of the largest allowed ciphertext: anything longer is refused before decoding."""
LIMITS = {
    "text_chars": community_format.MAX_TEXT_CHARS,
    "name_chars": community_format.MAX_NAME_CHARS,
    "burst": 5,
    "refill_seconds": 6,
}


@dataclass(frozen=True)
class Posted:
    """The outcome of a post: the message's `seq` and whether it was an identical repeat."""

    seq: int
    replayed: bool


def audit(
    connection: Connection,
    now: datetime,
    action: str,
    *,
    actor: UUID | None,
    actor_kind: str = "user",
    target_kind: str,
    target_id: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor_kind,
            actor_id=None if actor is None else acting_user(connection, actor),  # an agent: its user (ADR-082)
            action=action,
            result="ok",
            target_kind=target_kind,
            target_id=target_id,
            detail=dict(detail or {}),
        ),
    )


def refusal(error: CryptoError) -> HubError:
    return HubError(error.code if error.code in ERROR_STATUSES else "request_invalid")


def current_epoch(connection: Connection) -> int | None:
    """The newest community key epoch, None before the first."""
    row = connection.execute("SELECT max(epoch) FROM hub.community_epoch").fetchone()
    return None if row is None or row[0] is None else int(row[0])


def identity(connection: Connection, user: UUID) -> tuple[bytes, bytes] | None:
    """`(kem_pub, sig_pub)` of a registered user, or of an own-identity agent while it is its user's active agent (a
    community author of its own, ADR-082 sections 1 and 9.12)."""
    row = connection.execute("SELECT kem_pub, sig_pub FROM hub.user_identity WHERE user_id = %s", (user,)).fetchone()
    return agent_keys(connection, user) if row is None else (bytes(row[0]), bytes(row[1]))


def _member(connection: Connection, user: UUID, *, lock: bool = False) -> tuple[int, datetime | None] | None:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        f"SELECT last_read_seq, muted_at FROM hub.community_member WHERE user_id = %s{suffix}", (user,)
    ).fetchone()
    return None if row is None else (int(row[0]), row[1])


def require_member(connection: Connection, user: UUID, *, lock: bool = False) -> tuple[int, datetime | None]:
    """`409 community_not_member` unless the user joined."""
    found = _member(connection, user, lock=lock)
    if found is None:
        raise HubError("community_not_member")
    return found


def is_moderator(connection: Connection, user: UUID) -> bool:
    """The user is a community moderator (granted by the operator)."""
    row = connection.execute("SELECT 1 FROM hub.community_moderator WHERE user_id = %s", (user,)).fetchone()
    return row is not None


def require_moderator(connection: Connection, user: UUID) -> None:
    """`403 forbidden` unless the user is a moderator."""
    if not is_moderator(connection, user):
        raise HubError("forbidden")


def head_seq(connection: Connection) -> int:
    """The newest message `seq`, 0 without messages."""
    row = connection.execute("SELECT COALESCE(max(seq), 0) FROM hub.community_message").fetchone()
    return int(cast(tuple[int], row)[0])


def join(connection: Connection, user: UUID, now: datetime) -> None:
    """Make the user a member (idempotent)."""
    connection.execute(
        "INSERT INTO hub.community_member (user_id, joined_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user, now),
    )


def is_holder(connection: Connection, user: UUID, epoch: int | None) -> bool:
    """The user has a self-wrap of `epoch`."""
    if epoch is None:
        return False
    row = connection.execute(
        "SELECT 1 FROM hub.community_wrap WHERE epoch = %s AND user_id = %s", (epoch, user)
    ).fetchone()
    return row is not None


def require_holder(connection: Connection, user: UUID) -> int:
    """The current epoch of a user who holds it (`409 community_no_key` otherwise)."""
    epoch = current_epoch(connection)
    if epoch is None or not is_holder(connection, user, epoch):
        raise HubError("community_no_key")
    return epoch


def _message_json(header: Any, ct: bytes, sig: bytes) -> dict[str, Any]:
    return {"header": header, "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def messages(connection: Connection, *, before: int | None, after: int | None, limit: int) -> dict[str, Any]:
    """A page of messages: newest first before `before`, or ascending after `after`; with the authors' keys."""
    if after is not None:
        rows = connection.execute(
            "SELECT seq, id, author_id, created_at, deleted_as, header, ct, sig FROM hub.community_message"
            " WHERE seq > %s ORDER BY seq LIMIT %s",
            (after, limit),
        ).fetchall()
        next_before = None
    else:
        bound = before if before is not None else 2**63 - 1
        rows = connection.execute(
            "SELECT seq, id, author_id, created_at, deleted_as, header, ct, sig FROM hub.community_message"
            " WHERE seq < %s ORDER BY seq DESC LIMIT %s",
            (bound, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_before = str(rows[-1][0]) if more else None
    authors = sorted({row[2] for row in rows if row[4] is None}, key=str)
    keys = (
        connection.execute(
            "SELECT i.user_id, i.sig_pub, m.user_id IS NOT NULL FROM hub.user_identity i"
            " LEFT JOIN hub.community_moderator m ON m.user_id = i.user_id WHERE i.user_id = ANY(%s::uuid[])",
            (authors,),
        ).fetchall()
        if authors
        else []
    )
    found = {str(row[0]) for row in keys}
    agents = agent_authors(connection, [author for author in authors if str(author) not in found])
    return {
        "messages": [
            {
                "seq": int(seq),
                "id": str(message_id),
                "author": str(author),
                "created_at": format_time(created_at),
                "deleted": deleted_as,
                "message": None if deleted_as is not None else _message_json(header, bytes(ct), bytes(sig)),
            }
            for seq, message_id, author, created_at, deleted_as, header, ct, sig in rows
        ],
        "authors": {
            **{
                str(user_id): {"sig_pub": b64u_encode(bytes(sig_pub)), "moderator": bool(moderator)}
                for user_id, sig_pub, moderator in keys
            },
            **agents,
        },
        "next_before": next_before,
    }


def _same(stored: tuple[Any, ...], user: UUID, message: Mapping[str, Any]) -> bool:
    author, header, ct, sig = stored
    return (
        author == user
        and header is not None
        and canonical(header) == canonical(message["header"])
        and b64u_encode(bytes(ct)) == message["ct"]
        and b64u_encode(bytes(sig)) == message["sig"]
    )


def check_message(connection: Connection, user: UUID, message: Mapping[str, Any]) -> tuple[dict[str, object], bytes]:
    """Format, author, size, epoch and signature of a posted message (14-community.md section 4.3)."""
    ct_text = message.get("ct")
    if isinstance(ct_text, str) and len(ct_text) > MAX_CT_CHARS:
        raise HubError("size_exceeded", status=413)
    epoch = require_holder(connection, user)
    keys = cast(tuple[bytes, bytes], identity(connection, user))
    header_value = message.get("header")
    try:
        header = community_format.check_header(header_value)
    except CryptoError as error:
        raise refusal(error) from None
    if header["author"] != str(user):
        raise HubError("forbidden")
    if header["epoch"] != epoch:
        raise HubError("epoch_stale")
    try:
        community_format.verify_message(dict(message), keys[1])
    except CryptoError as error:
        raise refusal(error) from None
    return header, b64u_decode(cast(str, ct_text))


def existing(connection: Connection, user: UUID, message: Mapping[str, Any]) -> Posted | None:
    """An identical repeat of a stored message (`replayed`), None for a new ID; `409 id_conflict` otherwise."""
    header = message.get("header")
    message_id = header.get("id") if isinstance(header, Mapping) else None
    if not isinstance(message_id, str):
        return None
    try:
        parsed = UUID(message_id)
    except ValueError:
        return None
    row = connection.execute(
        "SELECT seq, author_id, header, ct, sig FROM hub.community_message WHERE id = %s", (parsed,)
    ).fetchone()
    if row is None:
        return None
    if not _same(tuple(row[1:]), user, message):
        raise HubError("id_conflict")
    return Posted(seq=int(row[0]), replayed=True)


def post(connection: Connection, user: UUID, message: Mapping[str, Any], now: datetime) -> Posted:
    """Store a checked message under the next `seq` (the caller checked membership, mute and the rate limit)."""
    header, ct = check_message(connection, user, message)
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (MESSAGE_LOCK,))
    repeat = existing(connection, user, message)
    if repeat is not None:
        return repeat
    seq = head_seq(connection) + 1
    connection.execute(
        "INSERT INTO hub.community_message (seq, id, author_id, epoch, created_at, ct_bytes, header, ct, sig)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            seq,
            UUID(cast(str, header["id"])),
            user,
            header["epoch"],
            now,
            len(ct),
            Jsonb(header),
            ct,
            b64u_decode(cast(str, message["sig"]), 64),
        ),
    )
    audit(
        connection,
        now,
        "community.post",
        actor=user,
        target_kind="community_message",
        target_id=cast(str, header["id"]),
        detail={"seq": seq, "ct_bytes": len(ct)},
    )
    return Posted(seq=seq, replayed=False)


def delete(
    connection: Connection, user: UUID, message_id: UUID, now: datetime, *, moderator: UUID | None = None
) -> tuple[str, int]:
    """Delete a message as its author `user` or as a moderator (`moderator`, default `user`: the user of an agent
    author); returns `(deleted_as, seq)` (idempotent)."""
    row = connection.execute(
        "SELECT seq, author_id, deleted_as FROM hub.community_message WHERE id = %s FOR UPDATE", (message_id,)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    seq, author, deleted_as = int(row[0]), row[1], row[2]
    if deleted_as is not None:
        unchanged()  # a repeat of the deletion (ADR-063 section 1)
        return str(deleted_as), seq
    if author == user:
        role = "author"
    elif is_moderator(connection, moderator or user):
        role = "moderator"
    else:
        raise HubError("forbidden")
    connection.execute(
        "UPDATE hub.community_message SET header = NULL, ct = NULL, sig = NULL, deleted_at = %s, deleted_as = %s,"
        " deleted_by = %s WHERE id = %s",
        (now, role, user, message_id),
    )
    audit(
        connection,
        now,
        "community.delete",
        actor=user,
        target_kind="community_message",
        target_id=str(message_id),
        detail={"as": role, "seq": seq},
    )
    return role, seq


def mark_read(connection: Connection, user: UUID, seq: int) -> int:
    """Move the read position forward to `seq` (never back, never beyond the newest message)."""
    require_member(connection, user)
    row = connection.execute(
        "UPDATE hub.community_member SET last_read_seq = GREATEST(last_read_seq, LEAST(%s,"
        " (SELECT COALESCE(max(seq), 0) FROM hub.community_message))) WHERE user_id = %s RETURNING last_read_seq",
        (seq, user),
    ).fetchone()
    return int(cast(tuple[int], row)[0])


def mutes(connection: Connection) -> list[dict[str, str]]:
    """The muted users, newest mute first."""
    rows = connection.execute(
        "SELECT user_id, muted_at FROM hub.community_member WHERE muted_at IS NOT NULL ORDER BY muted_at DESC, user_id"
    ).fetchall()
    return [{"user": str(user_id), "muted_at": format_time(muted_at)} for user_id, muted_at in rows]


def set_mute(connection: Connection, moderator: UUID, target: UUID, muted: bool, now: datetime) -> bool:
    """Mute or unmute a member; True when the state changed (audited)."""
    if muted and is_moderator(connection, target):
        raise HubError("forbidden")
    found = _member(connection, target, lock=True)
    if found is None:
        raise HubError("not_found")
    if (found[1] is not None) == muted:
        unchanged()
        return False
    connection.execute(
        "UPDATE hub.community_member SET muted_at = %s, muted_by = %s WHERE user_id = %s",
        (now if muted else None, moderator if muted else None, target),
    )
    audit(
        connection,
        now,
        "community.mute" if muted else "community.unmute",
        actor=moderator,
        target_kind="user",
        target_id=str(target),
    )
    return True


def moderators(connection: Connection) -> list[dict[str, str]]:
    """The moderators, oldest grant first."""
    rows = connection.execute(
        "SELECT user_id, granted_at FROM hub.community_moderator ORDER BY granted_at, user_id"
    ).fetchall()
    return [{"user": str(user_id), "granted_at": format_time(granted_at)} for user_id, granted_at in rows]


def set_moderator(connection: Connection, target: UUID, granted: bool, now: datetime) -> bool:
    """Grant or revoke the moderator role (operator); True when it changed (audited, actor `operator`)."""
    exists = connection.execute("SELECT 1 FROM hub.principal WHERE id = %s AND kind = 'user'", (target,)).fetchone()
    if exists is None:
        raise HubError("not_found")
    if granted:
        changed = connection.execute(
            "INSERT INTO hub.community_moderator (user_id, granted_at) VALUES (%s, %s)"
            " ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
            (target, now),
        ).fetchone()
    else:
        changed = connection.execute(
            "DELETE FROM hub.community_moderator WHERE user_id = %s RETURNING user_id", (target,)
        ).fetchone()
    if changed is None:
        unchanged()
        return False
    audit(
        connection,
        now,
        "community.moderator_grant" if granted else "community.moderator_revoke",
        actor=None,
        actor_kind="operator",
        target_kind="user",
        target_id=str(target),
    )
    return True
