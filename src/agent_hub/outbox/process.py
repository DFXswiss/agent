"""Processing of one outbox push (03-api.md section 6.1, ADR-013, ADR-030).

`process(connection, device, items, now=..., root_key=...)` runs in the caller's transaction and applies the rule set:

1. **Order.** The device's `device_stream` row is created if missing and held `FOR UPDATE`, so two pushes of one
   device are processed one after the other (the second sees what the first stored). The items are taken in request
   order. An item with `origin_seq` ≤ the stream position is a resend: the same item text as its receipt (or as an
   earlier item of the same push) is `replayed`, another text is `409 origin_seq_conflict`; a resend whose receipt
   was pruned (ADR-030) is `replayed`. The next item must be position + 1; anything else is `409 origin_seq_gap` with
   `detail.expected` = stored position + 1. Both refusals end the push before any write, so a refused push changes
   nothing.
2. **Events first.** The `event` items among the new items are stored with **one** `append_events` call (their
   `origin_seq`s, the device as author) before any other effect, which keeps the lock order of `agent_hub.events.store`
   (org rows, scope heads, then the audit lock). If that call refuses, the events are stored one by one in savepoints
   to find the refused ones: the first event whose refusal is `epoch_stale` (E6) gets `status:"reseal"` and processing
   stops there (`acked_through` = the previous item, no later item is processed and none is answered); any other
   content refusal rejects that event. Resealing is only offered for E6: the checks of `append_events` stop at the
   first failing rule, and E7 comes after E6, so an event that also breaks E7 is rejected after its reseal.
3. **Handlers in order.** Every new item up to the stop runs its handler chain (`agent_hub.outbox.chains`) in its own
   savepoint, with the checked body of its kind (`agent_hub.outbox.bodies`). A content refusal (`HubError` with a
   status below 500, a body that breaks its contract, or a value the database refuses as data) rolls the savepoint
   back and acknowledges the item as `rejected` with the error code; the sequence continues.
4. **Bookkeeping.** One receipt per stored item (`ok` or `rejected`, ADR-030), one `outbox.rejected` audit row per
   rejected item, and `device_stream.last_origin_seq` = the last stored item - all in the same transaction.

`acked_through` is the stream position after the push. The returned `Outcome` holds the response and the effects to
send after the commit (`Outcome.flush`). Server-side failures (`5xx` refusals, lost database connections) fail the
whole push; the runner retries it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.errors import HubError
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.outbox.base import Connection, ItemContext, PushContext
from agent_hub.outbox.bodies import EventBody, RawItem, parse
from agent_hub.outbox.chains import chains
from agent_hub.sessions.effects import Effects

LOG = logging.getLogger("agent_hub.outbox")
RESEAL_CODE = "epoch_stale"
DATA_REFUSALS = (psycopg.errors.DataError, psycopg.errors.CheckViolation, UnicodeEncodeError)
"""Refusals of a value (out of range, impossible, not encodable) that make an item a poison item, not a server
failure. Every other exception of a handler (e.g. a unique violation) is a server failure that fails the push."""


def item_digest(item: RawItem) -> bytes:
    """SHA-256 of the canonical item text `{"body":…,"item_kind":…}` (sorted keys, no whitespace, `\\u` escapes for
    everything outside ASCII, so that even a lone surrogate of a malformed body has a digest; ADR-030)."""
    text = json.dumps(
        {"body": item.body, "item_kind": item.item_kind}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(text.encode("ascii")).digest()


@dataclass(frozen=True)
class Planned:
    """One item of the push after the order check."""

    item: RawItem
    digest: bytes
    resend: bool

    @property
    def origin_seq(self) -> int:
        return self.item.origin_seq


@dataclass(frozen=True)
class Result:
    """One entry of `results`."""

    origin_seq: int
    status: str
    error: str | None = None

    def json(self) -> dict[str, Any]:
        found: dict[str, Any] = {"origin_seq": self.origin_seq, "status": self.status}
        if self.error is not None:
            found["error"] = self.error
        return found


@dataclass
class Outcome:
    """The answer of a push and what to send after its commit."""

    acked_through: int
    results: list[Result]
    effects: Effects = field(default_factory=Effects)
    after: list[Callable[[], None]] = field(default_factory=list)

    def answer(self) -> dict[str, Any]:
        """The response body of `POST /v1/device/outbox`."""
        return {"acked_through": self.acked_through, "results": [result.json() for result in self.results]}

    def flush(self) -> None:
        """Send the frames, hints and signals of the stored items (after the commit only)."""
        self.effects.flush()
        calls, self.after = self.after, []
        for call in calls:
            try:
                call()
            except Exception:  # the push is committed: a failing announcement never turns it into an error
                LOG.error("an outbox effect could not be sent", exc_info=True)


def _position(connection: Connection, device: UUID) -> int:
    """The stream position of the device, its `device_stream` row created if missing and held `FOR UPDATE`."""
    connection.execute(
        "INSERT INTO hub.device_stream (device_id, last_origin_seq) VALUES (%s, 0) ON CONFLICT (device_id) DO NOTHING",
        (device,),
    )
    row = connection.execute(
        "SELECT last_origin_seq FROM hub.device_stream WHERE device_id = %s FOR UPDATE", (device,)
    ).fetchone()
    return int(cast(tuple[int], row)[0])


def plan(connection: Connection, device: UUID, items: Sequence[RawItem], last: int) -> list[Planned]:
    """The order check of rule 1 (module docstring); refusals before any write."""
    resent = sorted({item.origin_seq for item in items if item.origin_seq <= last})
    receipts: dict[int, bytes] = {
        int(seq): bytes(digest)
        for seq, digest in connection.execute(
            "SELECT origin_seq, item_sha256 FROM hub.outbox_receipt WHERE device_id = %s AND origin_seq = ANY(%s)",
            (device, resent),
        )
    }
    in_push: dict[int, bytes] = {}
    cursor = last
    planned: list[Planned] = []
    for item in items:
        digest = item_digest(item)
        seq = item.origin_seq
        if seq <= cursor:
            known = in_push.get(seq) if seq > last else receipts.get(seq)
            if known is not None and known != digest:
                raise HubError("origin_seq_conflict")
            planned.append(Planned(item, digest, resend=True))
        elif seq == cursor + 1:
            cursor = seq
            in_push[seq] = digest
            planned.append(Planned(item, digest, resend=False))
        else:
            raise HubError("origin_seq_gap", detail={"expected": last + 1})
    return planned


@dataclass
class EventPhase:
    """The outcome of rule 2: stored events, rejected event items and the item that needs a reseal."""

    stored: dict[int, StoredEvent] = field(default_factory=dict)
    rejected: dict[int, str] = field(default_factory=dict)
    reseal: int | None = None


def _content_refusal(error: HubError) -> str:
    """The code of a content refusal; a server-side refusal fails the push."""
    if error.status >= 500:
        raise error
    return error.code


def store_events(push: PushContext, new: Sequence[Planned]) -> EventPhase:
    """Rule 2 (module docstring)."""
    phase = EventPhase()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for planned in new:
        if planned.item.item_kind != "event":
            continue
        try:
            body = cast(EventBody, parse("event", planned.item.body))
        except HubError as error:
            phase.rejected[planned.origin_seq] = error.code
            continue
        candidates.append((planned.origin_seq, body.event))
    if not candidates:
        return phase
    connection, author = push.connection, str(push.device)
    try:
        with connection.transaction():
            stored = append_events(
                connection,
                [event for _seq, event in candidates],
                author,
                now=push.now,
                root_key=push.root_key,
                origin_seqs=[seq for seq, _event in candidates],
            )
        phase.stored.update((seq, item) for (seq, _event), item in zip(candidates, stored, strict=True))
        return phase
    except HubError as error:
        _content_refusal(error)
    for seq, event in candidates:
        try:
            with connection.transaction():
                found = append_events(
                    connection, [event], author, now=push.now, root_key=push.root_key, origin_seqs=[seq]
                )
            phase.stored[seq] = found[0]
        except HubError as error:
            code = _content_refusal(error)
            if code == RESEAL_CODE:
                phase.reseal = seq
                break
            phase.rejected[seq] = code
    return phase


def run_item(item: ItemContext, raw: RawItem) -> str | None:
    """Rule 3 for one item: None when its chain applied, else the code of its content refusal."""
    try:
        body = parse(raw.item_kind, raw.body)
        with item.connection.transaction():
            for handler in chains()[raw.item_kind]:
                handler(item, body)
    except HubError as error:
        return _content_refusal(error)
    except DATA_REFUSALS:
        return "request_invalid"
    return None


def _audit_rejected(push: PushContext, rejected: Sequence[tuple[Planned, str]]) -> None:
    for planned, code in rejected:
        append_audit(
            push.connection,
            AuditRow(
                at=push.now,
                actor_kind="device",
                actor_id=push.device,
                action="outbox.rejected",
                result="rejected",
                device_id=push.device,
                target_kind="outbox_item",
                target_id=str(planned.origin_seq),
                detail={"origin_seq": planned.origin_seq, "item_kind": planned.item.item_kind, "code": code},
            ),
        )


def process(
    connection: Connection, device: UUID, items: Sequence[RawItem], *, now: datetime, root_key: bytes
) -> Outcome:
    """Apply one push in the caller's transaction (module docstring)."""
    last = _position(connection, device)
    planned = plan(connection, device, items, last)
    push = PushContext(connection=connection, device=device, now=now, root_key=root_key)
    phase = store_events(push, [entry for entry in planned if not entry.resend])
    outcome = Outcome(acked_through=last, results=[])
    receipts: list[tuple[UUID, int, str, bytes, str, str | None, datetime]] = []
    rejected: list[tuple[Planned, str]] = []
    for entry in planned:
        seq = entry.origin_seq
        if entry.resend:
            outcome.results.append(Result(seq, "replayed"))
            continue
        if seq == phase.reseal:
            outcome.results.append(Result(seq, "reseal"))
            break
        code = phase.rejected.get(seq)
        if code is None:
            item = ItemContext(push=push, origin_seq=seq, item_kind=entry.item.item_kind, stored=phase.stored.get(seq))
            code = run_item(item, entry.item)
            if code is None:
                outcome.effects.extend(item.effects)
                outcome.after.extend(item.after)
        if code is None:
            outcome.results.append(Result(seq, "ok"))
        else:
            outcome.results.append(Result(seq, "rejected", code))
            rejected.append((entry, code))
        receipts.append(
            (device, seq, entry.item.item_kind, entry.digest, "ok" if code is None else "rejected", code, now)
        )
        outcome.acked_through = seq
    if receipts:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO hub.outbox_receipt (device_id, origin_seq, item_kind, item_sha256, status, error,"
                " received_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                receipts,
            )
        _audit_rejected(push, rejected)
        connection.execute(
            "UPDATE hub.device_stream SET last_origin_seq = %s WHERE device_id = %s", (outcome.acked_through, device)
        )
    return outcome
