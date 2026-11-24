"""Checking and storing events (03-api.md section 4.4, 04-crypto.md sections 4.3, 5.12, 6.4; 02-data-model.md 2.3).

`append_events(connection, events, author, ...)` is the one way into `hub.event` for every hub area: `POST /v1/events`,
the events of a genesis and of a membership-log batch (orgs), `POST /v1/tasks` (tasks), the device outbox (outbox,
with `origin_seqs`) and operation parameters (operations). It runs inside the caller's transaction and is all or
nothing: every event is checked before the first one is stored, and the caller's rollback removes everything.

Per event, in order (the first failing check gives the code, 03-api.md section 11):

1. the object and header format (E1/E2, `agent_hub.crypto.event.check_header`);
2. the header's `author` is the appending principal (`403 forbidden`: nobody submits events signed by somebody else);
3. `notify` only on `whiteboard.entry` (`400 request_invalid`);
4. E9 by `id`: an event with a known `id` (stored, or earlier in the same call) is an idempotent success
   (`replayed`) with byte-identical content and `409 event_conflict` otherwise; a replay is not checked again, so
   an outbox retry after a rotation or a membership change still gets its original result;
5. the org of the header exists (`404 not_found`) and its stored log verifies (`503 unavailable`); the org row is
   held `FOR SHARE`, so a concurrent membership append (which holds it `FOR UPDATE`) cannot change the rights between
   the check and the commit;
6. E1-E8 against the hub's verified log (`agent_hub.log.events.check_event_hub`): `lh` within the log, author an
   active principal at `lh` and not a decertified platform device, E3 kind/scope/author matrix and header rules, E8
   size, E4 signature, E5 right at `lh` **and** at receipt, E6 current epoch or the previous one within 600 s after
   the rotation (receipt time of the newest `scope_epoch` row; never for removed authors), E7 the session of a session
   kind runs on the author device (`hub.session`, rows of the tasks area: same session, task and workgroup).

Then storage: the org rows of the call are held `FOR SHARE` in sorted order, the `scope_head` rows of its scopes
are created if missing and locked in sorted order (no deadlock between two calls that touch the same scopes), E9 is
repeated under that lock (a concurrent identical submission that committed meanwhile is a replay, not a unique
violation), every new event gets `scope_seq = seq + 1` by `UPDATE ... RETURNING` (gapless per scope: a rollback undoes
the increment), the extracted columns of 02-data-model.md section 2.3, the audit row `event.append` (metadata only;
not for the session's own output kinds, which the audit job summarises: `agent_hub.audit.eventbatch`)
and a `pg_notify` on `agent_hub.events.notify.CHANNEL` (delivered by PostgreSQL only at commit). Last, the registered
post-processors run in the same transaction (`agent_hub.events.hooks`). After the commit the caller passes the result
to `agent_hub.events.notify.announce` (live frames, device hints and the internal signal `event.appended`).

Lock order: org rows, scope heads, then the global audit lock (`append_audit`). A caller must therefore call
`append_events` **before** it writes any audit row in the same transaction (e.g. the outbox: one call with every
`event` item of a push, `origin_seqs` per event, before the audit rows of the other items), otherwise it can deadlock
with a concurrent `POST /v1/events` that holds a scope head and waits for the audit lock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.eventbatch import BATCHED_KINDS
from agent_hub.audit.recorder import deferred, unchanged
from agent_hub.crypto import event as event_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, parse_time, require_keys
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import ERROR_STATUSES, HubError
from agent_hub.events import hooks
from agent_hub.events import notify as event_notify
from agent_hub.log.events import DEVICE_ASSIGNED_WG_SESSION, KIND_RULES, NOTIFY_ALLOWED, check_event_hub, check_replay
from agent_hub.log.platform import PlatformState
from agent_hub.log.state import AGENT, DEVICE, USER
from agent_hub.log.verify import VerifiedLog
from agent_hub.orgs import logs

Connection = psycopg.Connection[Any]
EVENT_KEYS = frozenset({"header", "ct", "sig"})
SESSION_KINDS = frozenset(rule.kind for rule in KIND_RULES if rule.right == DEVICE_ASSIGNED_WG_SESSION)
"""The device-authored session kinds (right `device_assigned_wg_session`, E7)."""
CRYPTO_FALLBACK = {"log_fork": "header_invalid"}
"""Reader-only codes and their hub form (an `lh` beyond the hub's head is a malformed header at the hub)."""


@dataclass(frozen=True)
class StoredEvent:
    """One event of an `append_events` call: stored now, or found again (`replayed`)."""

    id: str
    org_id: str
    scope: str
    scope_id: str
    scope_seq: int
    kind: str
    author_id: str
    event: dict[str, Any]
    replayed: bool
    wg_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    ref: str = ""
    notify: bool = False
    devices: frozenset[str] = field(default_factory=frozenset)
    """Active devices that read the scope (without the author): receivers of the `events` hint."""

    def result(self) -> dict[str, Any]:
        """The item of `results` in the response of `POST /v1/events`."""
        return {"id": self.id, "scope_id": self.scope_id, "scope_seq": self.scope_seq, "replayed": self.replayed}


@dataclass
class _Checked:
    """A new event that passed every check, waiting for storage."""

    index: int
    header: dict[str, Any]
    event: dict[str, Any]
    log: VerifiedLog
    author_kind: str
    notify: bool
    devices: frozenset[str]
    actor_id: str
    """The audit actor: the author, or the user of an agent author (ADR-082)."""


def hub_error(error: CryptoError) -> HubError:
    """The API refusal of a crypto/log refusal (same codes; reader-only codes become their hub form)."""
    code = CRYPTO_FALLBACK.get(error.code, error.code)
    return HubError(code if code in ERROR_STATUSES else "request_invalid")


def _none(value: object) -> str | None:
    return None if value == "" else cast(str, value)


def stored_event(connection: Connection, event_id: str) -> tuple[dict[str, Any], str, int] | None:
    """The stored event with this `id` as `(event, scope_id, scope_seq)`, None if unknown."""
    record = connection.execute(
        "SELECT header_text, ct, sig, scope_id, scope_seq FROM hub.event WHERE id = %s", (event_id,)
    ).fetchone()
    if record is None:
        return None
    event = {"header": json.loads(record[0]), "ct": b64u_encode(bytes(record[1])), "sig": b64u_encode(bytes(record[2]))}
    return event, str(record[3]), int(record[4])


def rotated_at(connection: Connection, scope: str, scope_id: str) -> datetime | None:
    """The receipt time of the scope's current epoch (E6 grace period), None for an unknown scope."""
    record = connection.execute(
        "SELECT created_at FROM hub.scope_epoch WHERE scope = %s AND scope_id = %s ORDER BY epoch DESC LIMIT 1",
        (scope, scope_id),
    ).fetchone()
    return None if record is None else cast(datetime, record[0])


def session_device(connection: Connection, header: Mapping[str, object]) -> str | None:
    """E7: the device the header's session runs on, if the session belongs to the header's task and workgroup."""
    record = connection.execute(
        "SELECT device_id FROM hub.session WHERE id = %s AND task_id = %s AND wg_id = %s",
        (header["session"], header["task"], header["scope_id"]),
    ).fetchone()
    return None if record is None or record[0] is None else str(record[0])


class _Logs:
    """The verified logs of the orgs one call touches (given by the caller or loaded `FOR SHARE`)."""

    def __init__(self, connection: Connection, given: Mapping[str, VerifiedLog] | None, root_key: bytes) -> None:
        self._connection = connection
        self._restricted = given is not None
        self._logs = dict(given or {})
        self._root_key = root_key
        self._excluded: dict[str, frozenset[str]] = {}

    def lock(self, orgs: set[str]) -> None:
        """Load (and hold `FOR SHARE`) the logs of `orgs` in sorted order; with given logs, other orgs are refused."""
        for org in sorted(orgs):
            self.log(org)

    def log(self, org: str) -> tuple[VerifiedLog, frozenset[str]]:
        if org not in self._logs and self._restricted:
            raise HubError("header_invalid")
        if org not in self._logs:
            row = logs.org_row(self._connection, UUID(org), share=True)
            if row is None:
                raise HubError("not_found")
            self._logs[org] = logs.verified_log(self._connection, row, self._platform())
        log = self._logs[org]
        if org not in self._excluded:
            platform = self._platform()
            self._excluded[org] = frozenset() if platform is None else platform.excluded(log.state)
        return log, self._excluded[org]

    def _platform(self) -> PlatformState | None:
        return logs.platform_state(self._connection, self._root_key)


def _org_of(header: Mapping[str, object]) -> str:
    return str(UUID(cast(str, header["org"])))


def _check(
    connection: Connection,
    index: int,
    event: object,
    header: dict[str, Any],
    registry: _Logs,
    *,
    now: datetime,
    notify: bool,
) -> _Checked:
    log, excluded = registry.log(_org_of(header))
    scope, scope_id = cast(str, header["scope"]), cast(str, header["scope_id"])
    device = session_device(connection, header) if header["kind"] in SESSION_KINDS else None
    check_event_hub(
        event,
        log,
        now=now,
        rotated_at=rotated_at(connection, scope, scope_id),
        session_device=device,
        excluded=excluded,
    )
    author = cast(str, header["author"])
    readers = log.state.readers(scope, scope_id, excluded)
    devices = frozenset(p for p in readers if p != author and log.state.principals[p].kind == DEVICE)
    principal = log.state.principals[author]
    return _Checked(
        index=index,
        header=header,
        event=cast(dict[str, Any], event),
        log=log,
        author_kind=principal.kind,
        notify=notify,
        devices=devices,
        actor_id=principal.parent or author,
    )


def _replayed(header: Mapping[str, Any], event: dict[str, Any], scope_id: str, scope_seq: int) -> StoredEvent:
    unchanged()
    return StoredEvent(
        id=cast(str, header["id"]),
        org_id=cast(str, header["org"]),
        scope=cast(str, header["scope"]),
        scope_id=scope_id,
        scope_seq=scope_seq,
        kind=cast(str, header["kind"]),
        author_id=cast(str, header["author"]),
        event=event,
        replayed=True,
    )


def _lock_heads(connection: Connection, checked: Sequence[_Checked]) -> None:
    """Create the missing `scope_head` rows and lock them all, in sorted order (no deadlocks between calls)."""
    scopes = sorted({(str(item.header["scope_id"]), str(item.header["scope"])) for item in checked})
    for scope_id, scope in scopes:
        connection.execute(
            "INSERT INTO hub.scope_head (scope_id, scope, seq) VALUES (%s, %s, 0) ON CONFLICT (scope_id) DO NOTHING",
            (scope_id, scope),
        )
    connection.execute(
        "SELECT scope_id FROM hub.scope_head WHERE scope_id = ANY(%s) ORDER BY scope_id FOR UPDATE",
        ([UUID(scope_id) for scope_id, _scope in scopes],),
    )


def _store(connection: Connection, item: _Checked, *, now: datetime, origin_seq: int | None) -> StoredEvent:
    """Store one checked event under the next `scope_seq` of its (locked) scope head."""
    header, data = item.header, item.event
    event_id, kind = cast(str, header["id"]), cast(str, header["kind"])
    scope, scope_id, author = cast(str, header["scope"]), cast(str, header["scope_id"]), cast(str, header["author"])
    seq_row = connection.execute(
        "UPDATE hub.scope_head SET seq = seq + 1 WHERE scope_id = %s RETURNING seq", (scope_id,)
    ).fetchone()
    scope_seq = int(cast(tuple[int], seq_row)[0])
    ct = b64u_decode(data["ct"])
    wg = scope_id if scope == "wg" else _none(header["wg"])
    task, session = _none(header["task"]), _none(header["session"])
    connection.execute(
        "INSERT INTO hub.event (scope_id, scope_seq, id, scope, org_id, wg_id, task_id, session_id, author_id,"
        " author_kind, kind, ref, epoch, ts, lh, header_text, ct, sig, size, origin_device_id, origin_seq,"
        " received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            scope_id,
            scope_seq,
            event_id,
            scope,
            header["org"],
            wg,
            task,
            session,
            author,
            item.author_kind,
            kind,
            header["ref"],
            header["epoch"],
            parse_time(header["ts"]),
            header["lh"],
            canonical(header).decode("ascii"),
            ct,
            b64u_decode(data["sig"]),
            len(ct),
            None if origin_seq is None else author,
            origin_seq,
            now,
        ),
    )
    if kind in BATCHED_KINDS:  # the session's own output is summarised by the audit job (ADR-063 section 4)
        deferred()
    else:
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind=USER if item.author_kind == AGENT else item.author_kind,
                actor_id=UUID(item.actor_id),
                action="event.append",
                result="ok",
                org_id=UUID(cast(str, header["org"])),
                wg_id=None if wg is None else UUID(wg),
                task_id=None if task is None else UUID(task),
                session_id=None if session is None else UUID(session),
                device_id=UUID(author) if item.author_kind == DEVICE else None,
                target_kind="event",
                target_id=event_id,
                detail={"kind": kind, "scope": scope, "scope_seq": scope_seq, "ct_bytes": len(ct)},
                event_id=UUID(event_id),
            ),
        )
    stored = StoredEvent(
        id=event_id,
        org_id=cast(str, header["org"]),
        scope=scope,
        scope_id=scope_id,
        scope_seq=scope_seq,
        kind=kind,
        author_id=author,
        event=data,
        replayed=False,
        wg_id=wg,
        task_id=task,
        session_id=session,
        ref=cast(str, header["ref"]),
        notify=item.notify,
        devices=item.devices,
    )
    event_notify.send(connection, stored)
    return stored


def _recheck(connection: Connection, item: _Checked) -> StoredEvent | None:
    """E9 again under the scope-head lock: a concurrent call that stored the same `id` has committed by now."""
    found = stored_event(connection, cast(str, item.header["id"]))
    if found is None or not check_replay(found[0], item.event):
        return None
    return _replayed(item.header, item.event, found[1], found[2])


def append_events(
    connection: Connection,
    events: Sequence[object],
    author: str,
    *,
    now: datetime,
    root_key: bytes,
    logs: Mapping[str, VerifiedLog] | None = None,
    notify: Sequence[bool] | None = None,
    origin_seqs: Sequence[int] | None = None,
) -> list[StoredEvent]:
    """Check and store `events` of `author` atomically in the caller's transaction (module docstring).

    `logs` maps an org ID to the verified log to check against instead of the stored one (the orgs area passes the
    log after its batch, whose entries are stored in the same transaction); the call is then restricted to these orgs
    (an event of another org is `header_invalid`). `notify` holds the `notify` flag of every event (default: none
    set); `origin_seqs` the `origin_seq` of every event of a device outbox push (`origin_device_id` = author). Refusals
    raise `HubError`; the results keep the order of `events`. Lock order (callers must keep it): org rows
    (`FOR SHARE`, sorted), `scope_head` rows (sorted), then the audit lock - so write no audit row in the transaction
    before this call (module docstring).
    """
    flags = list(notify) if notify is not None else [False] * len(events)
    if len(flags) != len(events):
        raise ValueError("notify must have one flag per event")
    origins: list[int | None] = list(origin_seqs) if origin_seqs is not None else [None] * len(events)
    if len(origins) != len(events):
        raise ValueError("origin_seqs must have one origin_seq per event")
    registry = _Logs(connection, logs, root_key)
    results: list[StoredEvent | tuple[bool, int]] = []
    pending: list[tuple[int, object, dict[str, Any], bool]] = []
    seen: dict[str, tuple[dict[str, Any], int]] = {}
    try:
        for index, (event, flag) in enumerate(zip(events, flags, strict=True)):
            header = event_format.check_header(require_keys(event, EVENT_KEYS)["header"])
            if header["author"] != author:
                raise HubError("forbidden")
            if flag and header["kind"] not in NOTIFY_ALLOWED:
                raise HubError("request_invalid")
            event_id = cast(str, header["id"])
            if event_id in seen:
                earlier, position = seen[event_id]
                check_replay(earlier, event)
                results.append((True, position))
                continue
            found = stored_event(connection, event_id)
            if found is not None and check_replay(found[0], event):
                results.append(_replayed(header, cast(dict[str, Any], event), found[1], found[2]))
                continue
            seen[event_id] = (cast(dict[str, Any], event), index)
            pending.append((index, event, header, flag))
            results.append((False, index))
        registry.lock({_org_of(header) for _index, _event, header, _flag in pending})
        checked = {
            index: _check(connection, index, event, header, registry, now=now, notify=flag)
            for index, event, header, flag in pending
        }
        if checked:
            _lock_heads(connection, list(checked.values()))
        stored: dict[int, StoredEvent] = {}
        for index, item in checked.items():
            stored[index] = _recheck(connection, item) or _store(connection, item, now=now, origin_seq=origins[index])
    except CryptoError as error:
        raise hub_error(error) from None
    final = [_resolve(item, stored) for item in results]
    hooks.run(connection, [item for item in final if not item.replayed])
    return final


def _resolve(item: StoredEvent | tuple[bool, int], stored: Mapping[int, StoredEvent]) -> StoredEvent:
    """The result of one position: a replay of a stored event, the event stored for this position, or a repetition
    of an event earlier in the same call (a replay of that one)."""
    if isinstance(item, StoredEvent):
        return item
    repeat, index = item
    found = stored[index]
    return replace(found, replayed=True, notify=False, devices=frozenset()) if repeat else found
