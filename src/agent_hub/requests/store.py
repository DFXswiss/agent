"""Device requests and their responses at the hub (03-api.md sections 4.10 and 6.1, 02-data-model.md section 2.7,
04-crypto.md section 4.5, ADR-035).

The hub routes end-to-end encrypted envelopes between a user and one device and never sees their content: it checks
the envelope form and the sender's signature (`agent_hub.crypto.request`), the addressing and the state machine.

States (`device_request.state`): `pending` (stored) → `delivered` (the device fetched it) → `answered` (a response
with `final:true` arrived; success or error is inside the ciphertext); `expired` 15 min after creation without a final
response; `failed` when a newer request of the same kind replaced it or the device is no longer eligible. Every
transition is audited as `device_request.<state>`.

A **chain** is a request with `follows = ""` (its root) and the follow-up requests that name it in `follows` (e.g. the
authorization code typed during `claude.login`). Rules:

- the envelope is signed by its sender `header.user`: the user (registered identity) or the user's own-identity agent
  (its key, certificate checked; ADR-082 section 1) - the request belongs to the user either way;
- the device must be eligible for the user (`agent_hub.credentials.eligibility`, the set of `GET /v1/me/devices`),
  else `403 device_not_assigned`;
- at most one active root per user, device, kind and `slot` (the plaintext subject of the root, e.g. the subscription a
  `claude.login` fills; KOR-73, ADR-099 section 2 - NULL is a slot of its own): a new root replaces an active one of
  the same slot, whose chain becomes `failed`; roots of different slots run side by side;
- a `github.connect` for a company identity names the organization (`org_id`, ADR-078 section 1): only an active
  admin of it may send one, and only of kind `github.connect` (`403 forbidden` otherwise); it is stored as
  `device_request.org_id`, and the device stores no record while answering it (`agent_hub.github.routes`);
- a follow-up is accepted only while its root (same user - senders compared through the user they act for, ADR-082
  section 9.12 -, device and kind, itself no follow-up) is `pending` or
  `delivered` and not past its expiry; anything else is `400 params_invalid` (pointer `/envelope/header/follows`;
  one answer for unknown, foreign and closed roots);
- a final response to any request of a chain answers the whole chain; when a root expires, its active follow-ups
  expire with it;
- responses carry `seq` 1, 2, ... without a gap (`409 seq_conflict` with `detail.expected_seq`); the same response
  again is accepted as a retry; new responses are accepted only while the request is active (`403 forbidden`).

Expiry is enforced lazily inside every write that depends on it and by the job `requests.expire`
(`agent_hub.requests.jobs`), which also fails the active requests of devices that are no longer eligible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.agents.store import agent_author_key
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.config import LIMITS
from agent_hub.credentials.eligibility import eligible_device_ids, is_eligible
from agent_hub.credentials.records import device_key, identity_key
from agent_hub.crypto import request as request_format
from agent_hub.crypto.encoding import canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import ERROR_STATUSES, HubError
from agent_hub.orgs import rights

Connection = psycopg.Connection[Any]
PENDING, DELIVERED, ANSWERED, FAILED, EXPIRED = "pending", "delivered", "answered", "failed", "expired"
ACTIVE = (PENDING, DELIVERED)
TTL = timedelta(seconds=LIMITS["device_request_seconds"])
FOLLOWS_POINTER = "/envelope/header/follows"
REQUEST_KINDS = frozenset(
    {"github.connect", "claude.login", "credential.check", "mailbox.check", "github.pull_status", "vnc.open"}
)
MAX_ENVELOPE_CT_CHARS = 349526
"""The `ct` limit of `contracts/common.json` `device_request_envelope`/`device_response_envelope` (256 KiB)."""
MAX_RESPONSES = 100
"""At most this many responses per request (`contracts/api/requests-get.response.json`)."""
CREDENTIAL_KINDS = {"github.connect": frozenset({"github"}), "claude.login": frozenset({"claude_subscription"})}
"""The credential kinds a device may write while answering a request of the kind (03-api.md section 6.1)."""

_COLUMNS = "id, user_id, device_id, kind, state, envelope, created_at, expires_at"


@dataclass(frozen=True)
class Actor:
    """Who caused a transition: a user, a device or the hub (jobs)."""

    kind: str
    id: UUID | None


HUB = Actor("hub", None)


@dataclass(frozen=True)
class RequestRow:
    """One `device_request` row."""

    id: UUID
    user_id: UUID
    device_id: UUID
    kind: str
    state: str
    envelope: str
    created_at: datetime
    expires_at: datetime

    @property
    def follows(self) -> str:
        """The `follows` header field (`""` for a root)."""
        return cast(str, json.loads(self.envelope)["header"]["follows"])

    @property
    def root(self) -> UUID:
        """The root of the request's chain."""
        return UUID(self.follows) if self.follows else self.id

    def active(self, now: datetime) -> bool:
        """`pending`/`delivered` and not past its expiry."""
        return self.state in ACTIVE and self.expires_at > now

    def effective_state(self, now: datetime) -> str:
        """The state, `expired` for an active request past its expiry that the job has not closed yet."""
        return EXPIRED if self.state in ACTIVE and self.expires_at <= now else self.state


@dataclass(frozen=True)
class Created:
    """The outcome of `create_request`."""

    row: RequestRow
    replayed: bool
    replaced: bool = False
    """An active request of the same kind was replaced (its chain is `failed`)."""


@dataclass(frozen=True)
class Responded:
    """The outcome of `add_response`: the stored `seq` and whether it was stored now (False for a retry)."""

    user_id: UUID
    seq: int
    stored: bool


def envelope_too_large(envelope: dict[str, Any]) -> bool:
    """The envelope's `ct` text is longer than the contract allows."""
    ct = envelope.get("ct")
    return isinstance(ct, str) and len(ct) > MAX_ENVELOPE_CT_CHARS


def _crypto_refusal(error: CryptoError) -> HubError:
    return HubError(error.code if error.code in ERROR_STATUSES else "request_invalid")


def _row(record: Any) -> RequestRow:
    return RequestRow(*record)


def load(connection: Connection, request_id: UUID, *, lock: bool = False) -> RequestRow | None:
    """One request, None if unknown."""
    suffix = " FOR UPDATE" if lock else ""
    record = connection.execute(
        f"SELECT {_COLUMNS} FROM hub.device_request WHERE id = %s{suffix}", (request_id,)
    ).fetchone()
    return None if record is None else _row(record)


def _set_state(
    connection: Connection, row: RequestRow, state: str, *, now: datetime, actor: Actor, error_code: str | None = None
) -> None:
    connection.execute("UPDATE hub.device_request SET state = %s WHERE id = %s", (state, row.id))
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor.kind,
            actor_id=actor.id,
            action=f"device_request.{state}",
            result="ok",
            device_id=row.device_id,
            target_kind="device_request",
            target_id=str(row.id),
            detail={"kind": row.kind, "error_code": error_code},
        ),
    )


def end_chain(connection: Connection, root: UUID, state: str, *, now: datetime, actor: Actor) -> list[RequestRow]:
    """Close every active request of a chain with `state`; the closed requests."""
    rows = [
        _row(record)
        for record in connection.execute(
            f"SELECT {_COLUMNS} FROM hub.device_request WHERE state IN ('pending', 'delivered')"
            " AND (id = %(root)s OR envelope::jsonb #>> '{header,follows}' = %(root)s::text)"
            " ORDER BY created_at, id FOR UPDATE",
            {"root": root},
        )
    ]
    for row in rows:
        _set_state(connection, row, state, now=now, actor=actor)
    return rows


def expire_due(connection: Connection, now: datetime, *, user_id: UUID | None = None) -> int:
    """Close the active requests past their expiry (of one user, or all) with their chains; the number closed.

    A follow-up is created after its root and expires after it, so a due request always closes its whole chain."""
    closed = 0
    while True:
        record = connection.execute(
            f"SELECT {_COLUMNS} FROM hub.device_request WHERE state IN ('pending', 'delivered') AND expires_at <= %s"
            " AND (%s::uuid IS NULL OR user_id = %s) ORDER BY expires_at, id LIMIT 1 FOR UPDATE",
            (now, user_id, user_id),
        ).fetchone()
        if record is None:
            return closed
        closed += len(end_chain(connection, _row(record).root, EXPIRED, now=now, actor=HUB))


def fail_ineligible(connection: Connection, now: datetime) -> int:
    """Fail the active requests whose device is no longer eligible for their user; the number failed."""
    failed = 0
    rows = [
        _row(record)
        for record in connection.execute(
            f"SELECT {_COLUMNS} FROM hub.device_request WHERE state IN ('pending', 'delivered')"
            " ORDER BY created_at, id FOR UPDATE"
        )
    ]
    for row in rows:
        if not is_eligible(connection, row.device_id, row.user_id):
            _set_state(connection, row, FAILED, now=now, actor=HUB, error_code="device_not_assigned")
            failed += 1
    return failed


def _lock_user(connection: Connection, user_id: UUID) -> None:
    connection.execute("SELECT 1 FROM hub.principal WHERE id = %s FOR UPDATE", (user_id,))


def _check_follows(connection: Connection, header: dict[str, object], user_id: UUID, now: datetime) -> RequestRow:
    original = load(connection, UUID(cast(str, header["follows"])), lock=True)
    if (
        original is None
        or original.user_id != user_id
        or str(original.device_id) != header["device"]
        or original.kind != header["kind"]
        or original.follows
        or not original.active(now)
    ):
        raise HubError("params_invalid", detail={"pointers": [FOLLOWS_POINTER]})
    return original


def request_org(connection: Connection, request_id: UUID) -> UUID | None:
    """The organization a request names (`github.connect` for a company identity), None for a user's request."""
    row = connection.execute("SELECT (SELECT org_id FROM hub.device_request WHERE id = %s)", (request_id,)).fetchone()
    return cast(UUID | None, cast(tuple[Any], row)[0])


def _checked_header(
    connection: Connection, user_id: UUID, envelope: dict[str, Any], org_id: UUID | None, sender: UUID
) -> dict[str, object]:
    """The request's verified header: the sender's signature (the user or the user's own-identity agent), a known
    kind, the size, the sender and the organization."""
    user_sig = identity_key(connection, user_id) if sender == user_id else agent_author_key(connection, user_id, sender)
    if user_sig is None:
        raise HubError("forbidden")
    try:
        header = request_format.verify_request(envelope, user_sig)
    except CryptoError as error:
        raise _crypto_refusal(error) from None
    if header["kind"] not in REQUEST_KINDS:
        raise HubError("header_invalid")
    if envelope_too_large(envelope):
        raise HubError("size_exceeded")
    if header["user"] != str(sender):
        raise HubError("forbidden")
    if org_id is not None and (
        header["kind"] != "github.connect" or not rights.can(connection, user_id, rights.ORG_ADMIN, org_id=org_id)
    ):
        raise HubError("forbidden")
    return header


def create_request(
    connection: Connection,
    user_id: UUID,
    envelope: dict[str, Any],
    now: datetime,
    *,
    org_id: UUID | None = None,
    sender: UUID | None = None,
    slot: UUID | None = None,
) -> Created:
    """Store a new request of the user (module docstring); an identical repeat is answered as `replayed`. `sender` is
    the user's own-identity agent that signs the envelope (default: the user); `slot` the root's subject (only kept for
    a root)."""
    header = _checked_header(connection, user_id, envelope, org_id, sender or user_id)
    text = canonical(envelope).decode("ascii")
    _lock_user(connection, user_id)
    request_id = UUID(cast(str, header["id"]))
    existing = load(connection, request_id)
    if existing is not None:
        if existing.user_id == user_id and existing.envelope == text and request_org(connection, request_id) == org_id:
            unchanged()
            return Created(row=existing, replayed=True)
        raise HubError("id_conflict")
    device_id = UUID(cast(str, header["device"]))
    if device_id not in eligible_device_ids(connection, user_id):
        raise HubError("device_not_assigned")
    expire_due(connection, now, user_id=user_id)
    expires_at = now + TTL
    if header["follows"]:
        expires_at = min(expires_at, _check_follows(connection, header, user_id, now).expires_at)
    replaced = False
    if header["follows"]:
        slot = None
    else:
        for record in connection.execute(
            f"SELECT {_COLUMNS} FROM hub.device_request WHERE user_id = %s AND device_id = %s AND kind = %s"
            " AND slot IS NOT DISTINCT FROM %s"
            " AND state IN ('pending', 'delivered') AND envelope::jsonb #>> '{header,follows}' = ''"
            " ORDER BY created_at, id",
            (user_id, device_id, header["kind"], slot),
        ).fetchall():
            end_chain(connection, _row(record).id, FAILED, now=now, actor=Actor("user", user_id))
            replaced = True
    row = RequestRow(request_id, user_id, device_id, str(header["kind"]), PENDING, text, now, expires_at)
    try:
        with connection.transaction():
            connection.execute(
                f"INSERT INTO hub.device_request ({_COLUMNS}, org_id, slot)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.id,
                    row.user_id,
                    row.device_id,
                    row.kind,
                    row.state,
                    row.envelope,
                    row.created_at,
                    row.expires_at,
                    org_id,
                    slot,
                ),
            )
    except psycopg.errors.UniqueViolation:
        raise HubError("id_conflict") from None
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user_id,
            action="device_request.pending",
            result="ok",
            device_id=device_id,
            target_kind="device_request",
            target_id=str(row.id),
            detail={"kind": row.kind, "error_code": None},
        ),
    )
    return Created(row=row, replayed=False, replaced=replaced)


def responses(connection: Connection, request_id: UUID) -> list[dict[str, Any]]:
    """The responses of a request in `seq` order."""
    return [
        {"seq": seq, "envelope": json.loads(envelope), "created_at": format_time(created_at)}
        for seq, envelope, created_at in connection.execute(
            "SELECT seq, envelope, created_at FROM hub.device_response WHERE request_id = %s ORDER BY seq",
            (request_id,),
        )
    ]


def user_view(connection: Connection, user_id: UUID, request_id: UUID, now: datetime) -> dict[str, Any]:
    """`GET /v1/device-requests/{id}` for its user (`404 not_found` for anybody else)."""
    row = load(connection, request_id)
    if row is None or row.user_id != user_id:
        raise HubError("not_found")
    return {
        "request_id": str(row.id),
        "kind": row.kind,
        "device_id": str(row.device_id),
        "state": row.effective_state(now),
        "created_at": format_time(row.created_at),
        "expires_at": format_time(row.expires_at),
        "responses": responses(connection, row.id),
    }


def _addressed(connection: Connection, device_id: UUID, request_id: UUID) -> RequestRow:
    """The request, locked after its root (the lock order of `end_chain`, so no deadlock with it)."""
    found = load(connection, request_id)
    if found is not None and found.follows:
        load(connection, found.root, lock=True)
    row = load(connection, request_id, lock=True)
    if row is None or row.device_id != device_id or not is_eligible(connection, device_id, row.user_id):
        raise HubError("not_found")
    return row


def device_fetch(connection: Connection, device_id: UUID, request_id: UUID, now: datetime) -> dict[str, Any]:
    """`GET /v1/device/requests/{id}`: the envelope for the addressed device; a pending request becomes `delivered`."""
    row = _addressed(connection, device_id, request_id)
    if row.state == PENDING and row.active(now):
        _set_state(connection, row, DELIVERED, now=now, actor=Actor("device", device_id))
    return {"envelope": json.loads(row.envelope)}


def add_response(
    connection: Connection, device_id: UUID, request_id: UUID, envelope: dict[str, Any], now: datetime
) -> Responded:
    """`POST /v1/device/requests/{id}/responses` (module docstring)."""
    row = _addressed(connection, device_id, request_id)
    try:
        header = request_format.verify_response(envelope, device_key(connection, device_id))
    except CryptoError as error:
        raise _crypto_refusal(error) from None
    # the response names the request's sender: the user, or the user's own-identity agent (ADR-082)
    addressed = (str(row.id), str(device_id), row.kind)
    senders = (str(row.user_id), json.loads(row.envelope)["header"]["user"])
    if (header["in_reply_to"], header["device"], header["kind"]) != addressed or header["user"] not in senders:
        raise HubError("header_invalid")
    if envelope_too_large(envelope):
        raise HubError("size_exceeded")
    seq = cast(int, header["seq"])
    text = canonical(envelope).decode("ascii")
    stored = connection.execute(
        "SELECT envelope FROM hub.device_response WHERE request_id = %s AND seq = %s", (row.id, seq)
    ).fetchone()
    last = connection.execute(
        "SELECT COALESCE(max(seq), 0) FROM hub.device_response WHERE request_id = %s", (row.id,)
    ).fetchone()
    expected = int(cast(tuple[int], last)[0]) + 1
    if stored is not None and stored[0] == text:
        return Responded(user_id=row.user_id, seq=seq, stored=False)
    if not row.active(now):
        raise HubError("forbidden")
    if seq != expected:
        raise HubError("seq_conflict", detail={"expected_seq": expected})
    if seq > MAX_RESPONSES:
        raise HubError("size_exceeded")
    connection.execute(
        "INSERT INTO hub.device_response (request_id, seq, envelope, created_at) VALUES (%s, %s, %s, %s)",
        (row.id, seq, text, now),
    )
    actor = Actor("device", device_id)
    if row.state == PENDING:
        _set_state(connection, row, DELIVERED, now=now, actor=actor)
    if header["final"]:
        end_chain(connection, row.root, ANSWERED, now=now, actor=actor)
    return Responded(user_id=row.user_id, seq=seq, stored=True)


def active_for_device(connection: Connection, device_id: UUID, now: datetime) -> list[dict[str, str]]:
    """The active requests addressed to the device (oldest first) whose user it still serves."""
    rows = [
        _row(record)
        for record in connection.execute(
            f"SELECT {_COLUMNS} FROM hub.device_request WHERE device_id = %s AND state IN ('pending', 'delivered')"
            " AND expires_at > %s ORDER BY created_at, id",
            (device_id, now),
        )
    ]
    return [{"request_id": str(row.id)} for row in rows if is_eligible(connection, device_id, row.user_id)]


def authorizes_write(
    connection: Connection, device_id: UUID, owner_id: UUID, *, request_id: UUID, kind: str, now: datetime
) -> bool:
    """A device may write the owner's credential of `kind` only while answering an active request of the owner
    addressed to it whose kind produces that credential (`CREDENTIAL_KINDS`)."""
    row = load(connection, request_id)
    return (
        row is not None
        and row.user_id == owner_id
        and row.device_id == device_id
        and row.active(now)
        and kind in CREDENTIAL_KINDS.get(row.kind, frozenset())
    )
