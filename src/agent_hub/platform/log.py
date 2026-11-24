"""The platform log at the hub (03-api.md section 4.3, 04-crypto.md sections 4.8 and 6.2, ADR-023, ADR-026).

The hub stores the platform log in `hub.platform_entry` and **only verifies** it: every entry must be signed by the
platform root key of the configuration (`AGENT_HUB_PLATFORM_ROOT_KEY`); the hub holds no key that could extend the
log. `append` runs under the advisory lock `PLATFORM_LOG_LOCK` (one appender at a time), checks `expected_seq`
against the stored head (`409 seq_conflict` with `detail.expected_seq`), verifies the batch with P1-P4 and the type
rules of `agent_hub.log.platform` on top of the verified stored log, applies the hub's own rules (invitation lifetimes
of ADR-005, known organizations, unique invitation IDs across platform and org invitations, device IDs), inserts the
rows and updates the projections (`agent_hub.platform.projection`) and the audit chain in the same transaction.

Stored entries were verified when they were appended; the verified state is rebuilt from them on demand and kept in
a small cache keyed by the head hash (a hash chain names the whole log, so equal heads mean equal logs).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.invites import audit_invite_use
from agent_hub.config import LIMITS
from agent_hub.crypto.encoding import b64u_encode, canonical, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.features import LOG_ADMIT, LOG_POLICY, outdated_devices
from agent_hub.errors import ERROR_STATUSES, HubError
from agent_hub.log.entries import Head, entry_hash
from agent_hub.log.platform import PlatformState, VerifiedPlatformLog, append_platform, apply_platform
from agent_hub.platform import projection

PLATFORM_LOG_LOCK = 742180102
"""Advisory lock of platform-log appends (next to the migration lock 742180100 and the audit lock 742180101)."""
INVITE_TYPES = frozenset({"device_invite", "user_invite"})
SUBJECT_FIELDS = ("device", "subject", "org")
# Codes of the log layer that are client-only (03-api.md section 11) and the hub's answer instead.
HUB_CODES = {"platform_root_mismatch": "log_genesis"}


@dataclass(frozen=True)
class AppendResult:
    """The new head and the appended entries (`seq`, body) in order."""

    head: Head
    appended: tuple[tuple[int, dict[str, object]], ...]
    decertified: tuple[str, ...] = field(default=())


class _Cache:
    """The last verified log of this process (any database: the head hash identifies the log)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._log: VerifiedPlatformLog | None = None

    def get(self, root: str, head: Head) -> VerifiedPlatformLog | None:
        with self._lock:
            log = self._log
        if log is not None and log.state.root == root and log.state.head_value() == head:
            return log
        return None

    def put(self, log: VerifiedPlatformLog) -> None:
        with self._lock:
            self._log = log

    def clear(self) -> None:
        with self._lock:
            self._log = None


CACHE = _Cache()


USER_TYPES = frozenset({"user_certify", "user_decertify"})
ROLE_TYPES = frozenset({"role_grant", "role_revoke"})
OPERATOR_ROLE = "operator"
POLICY_TYPES = frozenset({"platform_policy", "org_operator_subscriptions"})
"""ADR-077: entries an older runner does not know; like `USER_TYPES` they wait for the runners' feature `log.policy`
(also a `role_grant` / `role_revoke` of the new role `operator`)."""
MAX_DETAIL_DEVICES = 64


def hub_error(error: CryptoError, pointer: str | None = None) -> HubError:
    """The API refusal for a refusal of the log layer (with the pointer of the offending entry)."""
    code = HUB_CODES.get(error.code, error.code)
    if code not in ERROR_STATUSES:  # every code of the platform verifier is listed; fail closed otherwise
        code = "request_invalid"
    return HubError(code, detail=None if pointer is None else {"pointers": [pointer]})


def stored_head(connection: psycopg.Connection[Any]) -> Head | None:
    """`(seq, hash)` of the last stored entry, None for an empty log."""
    row = connection.execute("SELECT seq, entry_hash FROM hub.platform_entry ORDER BY seq DESC LIMIT 1").fetchone()
    return None if row is None else Head(int(row[0]), str(row[1]))


def stored_entries(
    connection: psycopg.Connection[Any], after: int = -1, limit: int | None = None
) -> list[tuple[int, dict[str, object]]]:
    """`(seq, entry)` of the stored entries after `after`, ascending (at most `limit`)."""
    query = "SELECT seq, entry_text FROM hub.platform_entry WHERE seq > %s ORDER BY seq"
    params: tuple[object, ...] = (after,)
    if limit is not None:
        query += " LIMIT %s"
        params = (after, limit)
    return [(int(seq), cast(dict[str, object], json.loads(text))) for seq, text in connection.execute(query, params)]


def load(connection: psycopg.Connection[Any], root: bytes) -> VerifiedPlatformLog | None:
    """The verified stored log (None while it is empty); a stored log that does not verify is a hard error."""
    head = stored_head(connection)
    if head is None:
        return None
    root_text = b64u_encode(root)
    cached = CACHE.get(root_text, head)
    if cached is not None:
        return cached
    entries = [entry for _seq, entry in stored_entries(connection)]
    try:
        log = append_platform(None, entries, root_text)
    except CryptoError as error:
        raise RuntimeError("the stored platform log does not verify against the configured root key") from error
    CACHE.put(log)
    return log


def state(connection: psycopg.Connection[Any], root: bytes) -> PlatformState | None:
    """The current verified platform state (None while the log is empty)."""
    log = load(connection, root)
    return None if log is None else log.state


def _subject(body: dict[str, object]) -> UUID | None:
    for name in SUBJECT_FIELDS:
        if name in body:
            return UUID(cast(str, body[name]))
    return None


def _check_invite(connection: psycopg.Connection[Any], body: dict[str, object], now: datetime, pointer: str) -> None:
    """ADR-005: an invitation in the platform log expires in the future and within 30 days; its ID is unused."""
    expires = parse_time(body["expires"])
    if not now < expires <= now + timedelta(seconds=LIMITS["invite_max_seconds"]):
        raise HubError("request_invalid", detail={"pointers": [pointer + "/body/expires"]})
    found = connection.execute("SELECT 1 FROM hub.invite WHERE id = %s", (body["invite_id"],)).fetchone()
    if found is not None:
        raise HubError("id_conflict", detail={"pointers": [pointer + "/body/invite_id"]})


def _check_hub_rules(connection: psycopg.Connection[Any], body: dict[str, object], now: datetime, pointer: str) -> None:
    kind = body["type"]
    if kind in INVITE_TYPES:
        _check_invite(connection, body, now, pointer)
    elif kind == "device_certify":
        principal = connection.execute("SELECT kind FROM hub.principal WHERE id = %s", (body["device"],)).fetchone()
        device = connection.execute("SELECT kind FROM hub.device WHERE id = %s", (body["device"],)).fetchone()
        if (principal is not None and principal[0] != "device") or (device is not None and device[0] != "platform"):
            raise HubError("id_conflict", detail={"pointers": [pointer + "/body/device"]})
    if kind in ("org_platform_access", "org_operator_subscriptions"):
        found = connection.execute("SELECT 1 FROM hub.org WHERE id = %s", (body["org"],)).fetchone()
        if found is None:
            raise HubError("not_found", detail={"pointers": [pointer + "/body/org"]})
    if kind in USER_TYPES:
        _require_feature(connection, LOG_ADMIT)
    elif kind in POLICY_TYPES or (kind in ROLE_TYPES and body["role"] == OPERATOR_ROLE):
        _require_feature(connection, LOG_POLICY)


def _require_feature(connection: psycopg.Connection[Any], feature: str) -> None:
    """ADR-060, ADR-077: an older runner verifies the whole platform log and would stop at an unknown type or value
    (and so miss every later decertification); every active platform device must have announced `feature` first."""
    active = [
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM hub.device WHERE kind = 'platform' AND state = 'active' ORDER BY id"
        )
    ]
    outdated = outdated_devices(connection, active, feature)
    if outdated:
        raise HubError("peer_outdated", detail={"devices": outdated[:MAX_DETAIL_DEVICES]})


def append(
    connection: psycopg.Connection[Any],
    root: bytes,
    expected_seq: int,
    entries: Sequence[object],
    now: datetime,
) -> AppendResult:
    """Append a root-signed batch inside the connection's open transaction (all or nothing)."""
    if not entries:
        raise HubError("log_empty")
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (PLATFORM_LOG_LOCK,))
    log = load(connection, root)
    next_seq = 0 if log is None else log.state.seq + 1
    if expected_seq != next_seq:
        raise HubError("seq_conflict", detail={"expected_seq": next_seq})
    root_text = b64u_encode(root)
    states = list(() if log is None else log.states)
    stored_list = list(() if log is None else log.entries)
    appended: list[tuple[int, dict[str, object]]] = []
    decertified: list[str] = []
    for index, entry in enumerate(entries):
        pointer = f"/entries/{index}"
        try:
            states.append(apply_platform(states[-1] if states else None, entry, root_text, first_of_append=index == 0))
        except CryptoError as error:
            raise hub_error(error, pointer) from error
        body = cast(dict[str, object], cast(dict[str, object], entry)["body"])
        seq = states[-1].seq
        _check_hub_rules(connection, body, now, pointer)
        stored = cast(dict[str, object], entry)
        connection.execute(
            "INSERT INTO hub.platform_entry (seq, entry_text, entry_hash, type, subject_id, received_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (seq, canonical(stored).decode("ascii"), entry_hash(stored), body["type"], _subject(body), now),
        )
        projection.apply(connection, body, seq, now)
        if body["type"] == "device_decertify":
            projection.mark_rotation_pending(connection, cast(str, body["device"]))
            decertified.append(cast(str, body["device"]))
        _audit(connection, body, seq, now)
        appended.append((seq, body))
        stored_list.append(stored)
    final = VerifiedPlatformLog(tuple(stored_list), tuple(states))
    # Cached before the commit: harmless, a cache hit needs the stored head to equal the cached one.
    CACHE.put(final)
    return AppendResult(head=final.state.head_value(), appended=tuple(appended), decertified=tuple(decertified))


def _audit(connection: psycopg.Connection[Any], body: dict[str, object], seq: int, now: datetime) -> None:
    kind = cast(str, body["type"])
    device = UUID(cast(str, body["device"])) if "device" in body else None
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="operator",
            action=f"platform.{kind}",
            result="ok",
            device_id=device,
            org_id=UUID(cast(str, body["org"])) if "org" in body else None,
            target_kind="platform_entry",
            target_id=str(seq),
            detail={"seq": seq},
        ),
    )
    if kind == "device_certify":
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="operator",
                action="device.certify",
                result="ok",
                device_id=device,
                target_kind="device",
                target_id=str(device),
                detail={"platform_seq": seq},
            ),
        )
        audit_invite_use(
            connection,
            invite_id=cast(str, body["invite_id"]),
            kind="device",
            actor_kind="operator",
            actor_id=None,
            at=now,
            device_id=device,
        )
