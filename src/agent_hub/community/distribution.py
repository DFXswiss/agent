"""Distribution of the community key (14-community.md sections 3 and 4, ADR-069 decision 3).

- The first member creates epoch 1 with its key check and self-wrap (`create_epoch`, serialised by `EPOCH_LOCK`).
- A member without the key keeps one open **request** with a one-off X25519 key, signed by its identity
  (`put_request`). Holders see open requests only as `{id, kem_pub}` (`open_requests`, each leased to one holder for
  `LEASE_SECONDS`) and wrap the key to them (`wrap_requests`; rule C2). The requester opens the wrap, compares the key
  check and stores its self-wrap (`store_self_wrap`, rule C1; the request is removed) or rejects the wrap (`reject`;
  the wrapper is recorded and never again accepted for that user). A rejection only ever affects the rejecting user, so
  rejecting cannot bar a holder for others; a wrapper that hands out wrong keys is stopped by muting it (a muted user
  may not wrap).
- `state` is the body of `GET /v1/community`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from agent_hub.audit.recorder import unchanged
from agent_hub.community import crypto as community_format
from agent_hub.community.store import (
    LIMITS,
    Connection,
    audit,
    current_epoch,
    head_seq,
    identity,
    is_holder,
    is_moderator,
    refusal,
    require_holder,
    require_member,
)
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError

EPOCH_LOCK = 742180127
"""Advisory lock that serialises the creation of a community key epoch."""
LEASE_SECONDS = 30


def _verify_self_wrap(connection: Connection, user: UUID, epoch: int, wrap_text: str, signature: str) -> bytes:
    keys = cast(tuple[bytes, bytes], identity(connection, user))
    try:
        community_format.verify_community_wrap(
            keys[1], epoch=epoch, recipient=str(user), wrap_text=wrap_text, signature=signature
        )
    except CryptoError as error:
        raise refusal(error) from None
    return b64u_decode(wrap_text)


def _require_identity(connection: Connection, user: UUID) -> None:
    if identity(connection, user) is None:
        raise HubError("forbidden")


def create_epoch(
    connection: Connection,
    user: UUID,
    *,
    epoch: int,
    check_text: str,
    wrap_text: str,
    signature: str,
    now: datetime,
) -> int:
    """Create epoch 1 with the creator's key check and self-wrap (`409 community_epoch_exists` once one exists)."""
    require_member(connection, user)
    _require_identity(connection, user)
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (EPOCH_LOCK,))
    if current_epoch(connection) is not None:
        raise HubError("community_epoch_exists")
    if epoch != 1:
        raise HubError("request_invalid", detail={"pointers": ["/epoch"]})
    wrap_raw = _verify_self_wrap(connection, user, epoch, wrap_text, signature)
    connection.execute(
        "INSERT INTO hub.community_epoch (epoch, key_check, created_by, created_at) VALUES (%s, %s, %s, %s)",
        (epoch, b64u_decode(check_text, 32), user, now),
    )
    connection.execute(
        "INSERT INTO hub.community_wrap (epoch, user_id, wrap, sig, created_at) VALUES (%s, %s, %s, %s, %s)",
        (epoch, user, wrap_raw, b64u_decode(signature, 64), now),
    )
    connection.execute("DELETE FROM hub.community_request WHERE user_id = %s", (user,))
    audit(
        connection,
        now,
        "community.key_create",
        actor=user,
        target_kind="community_epoch",
        target_id=str(epoch),
        detail={"epoch": epoch},
    )
    return epoch


def put_request(
    connection: Connection, user: UUID, *, request_id: UUID, kem_text: str, signature: str, now: datetime
) -> bool:
    """Open (or replace) the user's key request; True when it is new (an identical repeat is False).

    Refused for a holder of the current epoch (`forbidden`), for a request ID already used with other content
    (`id_conflict`) and for a bad signature.
    """
    require_member(connection, user)
    keys = identity(connection, user)
    if keys is None:
        raise HubError("forbidden")
    if is_holder(connection, user, current_epoch(connection)):
        raise HubError("forbidden")
    try:
        community_format.verify_request(
            keys[1], id=str(request_id), kem_pub=kem_text, user=str(user), signature=signature
        )
    except CryptoError as error:
        raise refusal(error) from None
    stored = connection.execute(
        "SELECT user_id, kem_pub, sig FROM hub.community_request WHERE id = %s", (request_id,)
    ).fetchone()
    if stored is not None:
        if stored[0] != user or (bytes(stored[1]), bytes(stored[2])) != (
            b64u_decode(kem_text, 32),
            b64u_decode(signature, 64),
        ):
            raise HubError("id_conflict")
        unchanged()  # the same request again (ADR-063 section 1)
        return False
    connection.execute(
        "INSERT INTO hub.community_request (user_id, id, kem_pub, sig, created_at) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET id = EXCLUDED.id, kem_pub = EXCLUDED.kem_pub, sig = EXCLUDED.sig,"
        " created_at = EXCLUDED.created_at, offered_at = NULL, epoch = NULL, wrap = NULL, wrapped_by = NULL,"
        " wrap_sig = NULL, wrapped_at = NULL",
        (user, request_id, b64u_decode(kem_text, 32), b64u_decode(signature, 64), now),
    )
    audit(
        connection, now, "community.key_request", actor=user, target_kind="community_request", target_id=str(request_id)
    )
    return True


def _may_wrap(connection: Connection, wrapper: UUID) -> None:
    """A muted user does not serve requests (moderation stops a wrapper of wrong keys)."""
    row = connection.execute("SELECT muted_at FROM hub.community_member WHERE user_id = %s", (wrapper,)).fetchone()
    if row is not None and row[0] is not None:
        raise HubError("forbidden")


ENABLED_REQUESTER = (
    "(EXISTS (SELECT 1 FROM hub.principal p WHERE p.id = r.user_id AND p.disabled_at IS NULL)"
    " OR EXISTS (SELECT 1 FROM hub.agent a JOIN hub.principal p ON p.id = a.user_id AND p.disabled_at IS NULL"
    " WHERE a.id = r.user_id AND a.state = 'active' AND a.own_identity))"
)
"""The requester is an enabled user or the active own-identity agent of one (ADR-082 section 9.12)."""


def open_requests(connection: Connection, holder: UUID, limit: int, now: datetime) -> list[dict[str, str]]:
    """Open requests a holder may serve, oldest first; each is leased to the holder for `LEASE_SECONDS`."""
    require_holder(connection, holder)
    _may_wrap(connection, holder)
    rows = connection.execute(
        "SELECT r.user_id, r.id, r.kem_pub FROM hub.community_request r"
        " WHERE r.wrap IS NULL AND r.user_id <> %s AND (r.offered_at IS NULL OR r.offered_at <= %s)"
        f" AND {ENABLED_REQUESTER}"
        " AND NOT EXISTS (SELECT 1 FROM hub.community_reject j WHERE j.wrapper_id = %s AND j.user_id = r.user_id)"
        " ORDER BY r.offered_at NULLS FIRST, r.created_at, r.id LIMIT %s FOR UPDATE OF r SKIP LOCKED",
        (holder, now - timedelta(seconds=LEASE_SECONDS), holder, limit),
    ).fetchall()
    if rows:
        connection.execute(
            "UPDATE hub.community_request SET offered_at = %s WHERE user_id = ANY(%s::uuid[])",
            (now, [row[0] for row in rows]),
        )
    return [{"id": str(row[1]), "kem_pub": b64u_encode(bytes(row[2]))} for row in rows]


def wrap_requests(
    connection: Connection, holder: UUID, wraps: Sequence[Mapping[str, Any]], now: datetime
) -> list[UUID]:
    """Store a holder's wraps on open requests (rule C2); returns the users whose request got the key.

    A wrap of another epoch is `epoch_stale`, a bad signature refuses the whole batch; a request that is gone, already
    wrapped, of the holder itself or of a user who rejected the holder is skipped.
    """
    epoch = require_holder(connection, holder)
    _may_wrap(connection, holder)
    keys = cast(tuple[bytes, bytes], identity(connection, holder))
    checked: list[tuple[UUID, bytes, bytes]] = []
    for item in wraps:
        if item["epoch"] != epoch:
            raise HubError("epoch_stale")
        try:
            community_format.verify_community_wrap(
                keys[1], epoch=epoch, recipient=item["request"], wrap_text=item["wrap"], signature=item["sig"]
            )
        except CryptoError as error:
            raise refusal(error) from None
        checked.append((UUID(item["request"]), b64u_decode(item["wrap"]), b64u_decode(item["sig"], 64)))
    served: list[UUID] = []
    for request_id, raw, sig in checked:
        row = connection.execute(
            "UPDATE hub.community_request r SET epoch = %s, wrap = %s, wrapped_by = %s, wrap_sig = %s, wrapped_at = %s"
            " WHERE r.id = %s AND r.wrap IS NULL AND r.user_id <> %s AND NOT EXISTS"
            " (SELECT 1 FROM hub.community_reject j WHERE j.wrapper_id = %s AND j.user_id = r.user_id)"
            " RETURNING r.user_id",
            (epoch, raw, holder, sig, now, request_id, holder, holder),
        ).fetchone()
        if row is not None:
            served.append(row[0])
            audit(
                connection,
                now,
                "community.key_wrap",
                actor=holder,
                target_kind="community_request",
                target_id=str(request_id),
                detail={"epoch": epoch},
            )
    return served


def is_open_request(connection: Connection, user: UUID, request_id: UUID) -> bool:
    """Whether `request_id` is the user's open request (a repeat of it is no new request)."""
    row = connection.execute(
        "SELECT 1 FROM hub.community_request WHERE user_id = %s AND id = %s", (user, request_id)
    ).fetchone()
    return row is not None


def waiting_unserved(connection: Connection, user: UUID, now: datetime) -> bool:
    """Whether the user's request is open, not wrapped and not leased to a holder (it may need a new announcement)."""
    row = connection.execute(
        "SELECT 1 FROM hub.community_request WHERE user_id = %s AND wrap IS NULL"
        " AND (offered_at IS NULL OR offered_at <= %s)",
        (user, now - timedelta(seconds=LEASE_SECONDS)),
    ).fetchone()
    return row is not None


def reject(connection: Connection, user: UUID, now: datetime) -> UUID:
    """Reject the wrap on the user's request: clear it and record the wrapper; returns the wrapper."""
    row = connection.execute(
        "SELECT id, wrapped_by FROM hub.community_request WHERE user_id = %s AND wrap IS NOT NULL FOR UPDATE", (user,)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    request_id, wrapper = row
    connection.execute(
        "UPDATE hub.community_request SET epoch = NULL, wrap = NULL, wrapped_by = NULL, wrap_sig = NULL,"
        " wrapped_at = NULL, offered_at = NULL WHERE user_id = %s",
        (user,),
    )
    connection.execute(
        "INSERT INTO hub.community_reject (wrapper_id, user_id, rejected_at) VALUES (%s, %s, %s)"
        " ON CONFLICT (wrapper_id, user_id) DO NOTHING",
        (wrapper, user, now),
    )
    audit(
        connection,
        now,
        "community.key_reject",
        actor=user,
        target_kind="user",
        target_id=str(wrapper),
        detail={"request": str(request_id)},
    )
    return cast(UUID, wrapper)


def store_self_wrap(
    connection: Connection, user: UUID, *, epoch: int, wrap_text: str, signature: str, now: datetime
) -> int:
    """Store the user's self-wrap of the current epoch (rule C1) and remove its request; idempotent."""
    require_member(connection, user)
    _require_identity(connection, user)
    current = current_epoch(connection)
    if current is None:
        raise HubError("community_no_key")
    if epoch != current:
        raise HubError("epoch_stale")
    wrap_raw = _verify_self_wrap(connection, user, epoch, wrap_text, signature)
    if _own_wrap(connection, epoch, user) is not None:
        connection.execute("DELETE FROM hub.community_request WHERE user_id = %s", (user,))
        unchanged()
        return epoch
    # C1: without a self-wrap a user may store one only after a holder wrapped the epoch for its request
    opened = connection.execute(
        "SELECT id FROM hub.community_request WHERE user_id = %s AND wrap IS NOT NULL AND epoch = %s FOR UPDATE",
        (user, epoch),
    ).fetchone()
    if opened is None:
        raise HubError("forbidden")
    connection.execute(
        "INSERT INTO hub.community_wrap (epoch, user_id, wrap, sig, created_at) VALUES (%s, %s, %s, %s, %s)",
        (epoch, user, wrap_raw, b64u_decode(signature, 64), now),
    )
    connection.execute("DELETE FROM hub.community_request WHERE user_id = %s", (user,))
    audit(
        connection,
        now,
        "community.key_wrap",
        actor=user,
        target_kind="user",
        target_id=str(user),
        detail={"epoch": epoch},
    )
    return epoch


def _own_wrap(connection: Connection, epoch: int, user: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT wrap, sig FROM hub.community_wrap WHERE epoch = %s AND user_id = %s", (epoch, user)
    ).fetchone()
    return (
        None if row is None else {"epoch": epoch, "wrap": b64u_encode(bytes(row[0])), "sig": b64u_encode(bytes(row[1]))}
    )


def _own_request(connection: Connection, user: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT r.id, r.epoch, r.wrap, r.wrap_sig, r.wrapped_by, COALESCE(i.sig_pub, a.sig_pub)"
        " FROM hub.community_request r LEFT JOIN hub.user_identity i ON i.user_id = r.wrapped_by"
        " LEFT JOIN hub.agent a ON a.id = r.wrapped_by AND a.own_identity WHERE r.user_id = %s",
        (user,),
    ).fetchone()
    if row is None:
        return None
    request_id, epoch, wrap, wrap_sig, wrapped_by, wrapper_sig = row
    wrapped = (
        None
        if wrap is None
        else {
            "epoch": int(epoch),
            "wrap": b64u_encode(bytes(wrap)),
            "sig": b64u_encode(bytes(wrap_sig)),
            "wrapped_by": str(wrapped_by),
            "wrapper_sig": b64u_encode(bytes(wrapper_sig)),
        }
    )
    return {"id": str(request_id), "wrapped": wrapped}


def state(connection: Connection, user: UUID) -> dict[str, Any]:
    """The body of `GET /v1/community`."""
    epoch = current_epoch(connection)
    check = None
    if epoch is not None:
        row = connection.execute("SELECT key_check FROM hub.community_epoch WHERE epoch = %s", (epoch,)).fetchone()
        check = b64u_encode(bytes(cast(tuple[bytes], row)[0]))
    member = connection.execute(
        "SELECT last_read_seq, muted_at FROM hub.community_member WHERE user_id = %s", (user,)
    ).fetchone()
    last_read = 0 if member is None else int(member[0])
    posted = connection.execute("SELECT 1 FROM hub.community_message WHERE author_id = %s LIMIT 1", (user,)).fetchone()
    unread = connection.execute(
        "SELECT count(*) FROM hub.community_message WHERE seq > %s AND author_id <> %s AND deleted_at IS NULL",
        (last_read, user),
    ).fetchone()
    return {
        "epoch": None if epoch is None else {"epoch": epoch, "key_check": check},
        "wrap": None if epoch is None else _own_wrap(connection, epoch, user),
        "request": _own_request(connection, user),
        "member": {
            "joined": member is not None,
            "muted": member is not None and member[1] is not None,
            "posted": posted is not None,
            "last_read_seq": last_read,
        },
        "moderator": is_moderator(connection, user),
        "unread": int(cast(tuple[int], unread)[0]),
        "head_seq": head_seq(connection),
        "limits": dict(LIMITS),
    }
