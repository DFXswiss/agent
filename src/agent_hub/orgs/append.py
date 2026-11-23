"""Atomic appends to a membership log (`POST /v1/orgs/{org}/log`, 03-api.md section 4.3; 04-crypto.md 4.7, 5.10, 6.1).

`append_batch` runs inside the caller's transaction, under `SELECT ... FOR UPDATE` of the org row, in this order:

1. the org exists (`404 not_found`);
2. every entry is authored by the appending principal (`403 forbidden`: a client never submits entries of others),
   `expected_seq` is the current head + 1 (`409 seq_conflict`, `detail.expected_seq` only for members);
3. `device_assign` with `platform_seq` only for an org with platform access (`403 platform_access_denied`, ADR-026;
   the hub reads the access from the verified platform log);
4. L1-L8 before every entry against the hub's own verified log, the rotation obligation (the hub computes R itself,
   `409 rotation_required`) and W3 for every wrap plus the distribution of every new epoch (`agent_hub.log.batch`),
   with `now` (invitation expiry, certification validity) and the decertified platform devices excluded;
5. `accept` of a user: `pub` is the appender's registered identity (`log_accept`) and the invitation is not bound to
   another registered user (`409 invite_registered`, 04-crypto.md section 5.2a); the invitation (of a user or a device)
   is marked used, its package deleted and the use audited (`invite.use`);
6. storage: the entries (`membership_entry`), the projections (`agent_hub.orgs.projection`), the wraps, the deletion
   of the wraps of removed principals in this org, the head, `platform_access` and `rotation_pending` of the org and
   one audit row `log.<type>` per entry;
7. the events of the batch through `agent_hub.events.store.append_events` (E1-E9 against the log after the batch,
   after its epochs are stored so that E6 sees the new rotation time).

Refusals of the log rules keep their codes (03-api.md section 11). Other areas append through `append_batch` with an
`extra` step that runs in the same transaction (HUB-11 stores the invitation package with its `invite` entry, HUB-08
appends the device `accept` of an enrollment). The browser route refuses `invite`/`invite_revoke` entries (they exist
only together with their invitation row, `POST /v1/orgs/{org}/invites`) and device `accept` entries (decision HUB-05).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.invites import audit_invite_use
from agent_hub.crypto.encoding import b64u_decode, canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import ERROR_STATUSES, HubError
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.log.batch import BatchResult, check_batch
from agent_hub.log.entries import Head, entry_hash
from agent_hub.log.platform import PlatformState
from agent_hub.log.state import DEVICE, USER, WG, OrgState
from agent_hub.log.verify import VerifiedLog
from agent_hub.orgs import logs, projection

Connection = psycopg.Connection[Any]
Body = dict[str, Any]
CRYPTO_FALLBACK = {"log_fork": "header_invalid"}


@dataclass(frozen=True)
class Appender:
    """The principal that appends: a user (browser session) or a device."""

    id: str
    kind: str


@dataclass(frozen=True)
class AppendResult:
    """What an append stored; the route publishes the live frames from it after the commit."""

    org_id: UUID
    head: Head
    log: VerifiedLog
    batch: BatchResult
    events: list[StoredEvent]
    replayed: bool = False
    users: frozenset[str] = field(default_factory=frozenset)
    """Active user members after the batch (receivers of `log`)."""
    devices: frozenset[str] = field(default_factory=frozenset)
    """Active devices after the batch (receivers of the `log` hint)."""


Extra = Callable[[Connection, AppendResult], None]


def hub_error(error: CryptoError) -> HubError:
    """The API refusal of a crypto/log refusal (the codes are the same; reader-only codes become their hub form)."""
    code = CRYPTO_FALLBACK.get(error.code, error.code)
    return HubError(code if code in ERROR_STATUSES else "request_invalid")


def _body(entry: object) -> Body | None:
    if isinstance(entry, dict) and isinstance(entry.get("body"), dict):
        return cast(Body, entry["body"])
    return None


def require_authored(entries: Sequence[object], appender: Appender) -> None:
    """Every (well-formed) entry is by the appender; malformed entries are left to the format check."""
    for entry in entries:
        body = _body(entry)
        if body is not None and body.get("by") != appender.id:
            raise HubError("forbidden")


def _target(body: Body) -> tuple[str | None, str | None, str | None, str | None]:
    """`(wg, device, target_kind, target_id)` of an entry for its audit row."""
    kind = body["type"]
    if kind in ("device_assign", "device_unassign"):
        return body["wg"], body["device"], "device", body["device"]
    if kind == "rotate":
        return (body["scope_id"] if body["scope"] == WG else None), None, "scope", body["scope_id"]
    if kind in ("wg_create", "wg_archive"):
        return body["wg"], None, "wg", body["wg"]
    if kind in ("invite", "invite_revoke", "accept"):
        return None, None, "invite", body["invite_id"]
    if kind == "genesis":
        return body["default_wg"], None, "org", body["org"]
    return body.get("wg"), None, "principal", body["subject"]


def _audit(connection: Connection, org: UUID, body: Body, appender: Appender, now: datetime) -> None:
    wg, device, target_kind, target_id = _target(body)
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=appender.kind,
            actor_id=UUID(appender.id),
            action=f"log.{body['type']}",
            result="ok",
            org_id=org,
            wg_id=None if wg is None else UUID(wg),
            device_id=None if device is None else UUID(device),
            target_kind=target_kind,
            target_id=target_id,
            detail={"seq": body["seq"]},
        ),
    )


def _subject(body: Body) -> str | None:
    found = body.get("subject", body.get("device"))
    return None if found is None else str(found)


def _store_wraps(connection: Connection, batch: BatchResult, wraps: Sequence[object], by: str, now: datetime) -> None:
    for slot, wrap in zip(batch.wraps, wraps, strict=True):
        data = cast(dict[str, Any], wrap)
        connection.execute(
            "INSERT INTO hub.key_wrap (scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                slot.scope,
                slot.scope_id,
                slot.epoch,
                slot.recipient,
                b64u_decode(data["wrap"]),
                by,
                b64u_decode(data["sig"]),
                now,
            ),
        )


def delete_wraps_of(connection: Connection, state: OrgState, principal: str) -> None:
    """Delete the wraps of the org's scopes stored for a removed principal (02-data-model.md section 2.2)."""
    connection.execute(
        "DELETE FROM hub.key_wrap WHERE recipient_id = %s"
        " AND ((scope = 'org' AND scope_id = %s) OR (scope = 'wg' AND scope_id = ANY(%s::uuid[])))",
        (principal, state.org, sorted(state.workgroups)),
    )


def _accepts(connection: Connection, org: UUID, bodies: Sequence[Body], appender: Appender, now: datetime) -> None:
    for body in bodies:
        if body["type"] != "accept":
            continue
        if body["kind"] == USER:
            identity = logs.identity_keys(connection, appender.id)
            pub = cast(dict[str, str], body["pub"])
            if identity != (pub["kem"], pub["sig"]):
                raise HubError("log_accept")
            row = connection.execute(
                "SELECT registered_user FROM hub.invite WHERE id = %s AND org_id = %s FOR UPDATE",
                (body["invite_id"], org),
            ).fetchone()
            if row is not None and row[0] is not None and str(row[0]) != appender.id:
                raise HubError("invite_registered")
        connection.execute(
            "UPDATE hub.invite SET used_at = %s, used_by = %s, package_ct = NULL WHERE id = %s AND org_id = %s",
            (now, body["subject"], body["invite_id"], org),
        )
        device = body["kind"] != USER
        audit_invite_use(
            connection,
            invite_id=str(body["invite_id"]),
            kind="device" if device else "user",
            actor_kind=appender.kind,
            actor_id=UUID(appender.id),
            at=now,
            org_id=org,
            device_id=UUID(str(body["subject"])) if device else None,
        )


def members(state: OrgState) -> tuple[frozenset[str], frozenset[str]]:
    """Active users and active devices of the org."""
    active = [p for p in state.principals.values() if p.active]
    return (
        frozenset(p.id for p in active if p.kind == USER),
        frozenset(p.id for p in active if p.kind == DEVICE),
    )


def wants_platform_device(entries: Sequence[object]) -> bool:
    """The batch assigns a platform device (`device_assign` with `platform_seq`)."""
    return any(
        (body := _body(entry)) is not None and body.get("type") == "device_assign" and "platform_seq" in body
        for entry in entries
    )


def check(
    log: VerifiedLog | None,
    entries: Sequence[object],
    wraps: Sequence[object],
    *,
    appender: Appender,
    platform: PlatformState | None,
    now: datetime,
    access: bool,
    founder: str = "",
) -> BatchResult:
    """`agent_hub.log.batch.check_batch` with the hub's parameters; refusals as `HubError`."""
    excluded = frozenset() if platform is None or log is None else platform.excluded(log.state)
    try:
        return check_batch(
            log,
            entries,
            wraps,
            wrapped_by=appender.id,
            founder=founder,
            platform=platform,
            now=now,
            platform_access=access,
            excluded=excluded,
        )
    except CryptoError as error:
        raise hub_error(error) from None


def store_batch_events(
    connection: Connection,
    events: Sequence[object],
    log: VerifiedLog,
    *,
    root_key: bytes,
    author: str,
    now: datetime,
) -> list[StoredEvent]:
    """The batch's events through the events area, checked against the log after the batch (refusals as `HubError`)."""
    return append_events(connection, events, author, now=now, root_key=root_key, logs={log.state.org: log})


def append_batch(
    connection: Connection,
    *,
    org_id: UUID,
    appender: Appender,
    expected_seq: int,
    entries: Sequence[object],
    wraps: Sequence[object],
    events: Sequence[object],
    now: datetime,
    root_key: bytes,
    extra: Extra | None = None,
) -> AppendResult:
    """Check and store one batch atomically (module docstring); the caller commits."""
    org = logs.org_row(connection, org_id, lock=True)
    if org is None:
        raise HubError("not_found")
    require_authored(entries, appender)
    platform = logs.platform_state(connection, root_key)
    log = logs.verified_log(connection, org, platform)
    if expected_seq != org.head_seq + 1:
        # the head is disclosed to members only (a non-member learns no more than that the org exists)
        member = log.state.member(appender.id) is not None
        raise HubError("seq_conflict", detail={"expected_seq": org.head_seq + 1} if member else None)
    access = logs.platform_access(org.id, org.private, platform)
    if wants_platform_device(entries) and not access:
        raise HubError("platform_access_denied")
    batch = check(log, entries, wraps, appender=appender, platform=platform, now=now, access=access)
    after = batch.log
    bodies = [cast(Body, entry["body"]) for entry in after.entries[log.head.seq + 1 :]]
    _accepts(connection, org_id, bodies, appender, now)
    persist_range(connection, org_id, batch, first=log.head.seq + 1, wraps=wraps, appender=appender, now=now)
    stored = store_batch_events(connection, events, after, root_key=root_key, author=appender.id, now=now)
    projection.update_org(connection, org_id, after, platform, access)
    users, devices = members(after.state)
    result = AppendResult(org_id, after.head, after, batch, stored, users=users, devices=devices)
    if extra is not None:
        extra(connection, result)
    logs.remember(org_id, after)
    return result


def persist_range(
    connection: Connection,
    org: UUID,
    batch: BatchResult,
    *,
    first: int,
    wraps: Sequence[object],
    appender: Appender,
    now: datetime,
) -> None:
    """Store the entries `first..head` of the batch with their projections and audit rows, then its wraps."""
    log = batch.log
    for seq in range(first, log.head.seq + 1):
        entry = log.entries[seq]
        body = cast(Body, entry["body"])
        connection.execute(
            "INSERT INTO hub.membership_entry (org_id, seq, entry_text, entry_hash, type, by_id, subject_id,"
            " received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org,
                seq,
                canonical(entry).decode("ascii"),
                entry_hash(entry),
                body["type"],
                body["by"],
                _subject(body),
                now,
            ),
        )
        before = log.states[seq - 1] if seq > 0 else None
        projection.project_entry(connection, org, before, log.states[seq], seq=seq, at=now)
        if body["type"] == "remove":
            delete_wraps_of(connection, log.states[seq], str(body["subject"]))
        _audit(connection, org, body, appender, now)
    _store_wraps(connection, batch, wraps, appender.id, now)
