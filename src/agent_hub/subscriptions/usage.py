"""Who used how much of which Claude subscription (ADR-100 section 2; 03-api.md section 4.8): the answer of `GET
/v1/me/subscriptions/usage`.

Periods (`period`):

- `five_hour` and `week`: the running window of each subscription as its newest reading names it (`five_hour`,
  `seven_day`): from `resets_at` minus 5 hours (7 days) until now, while `resets_at` lies in the future and the
  reading is readable; then `measured` carries the window's `used_percent`, `resets_at` and the reading's
  `fetched_at`. Without such a reading the last 5 hours (7 days) count and `measured` is null.
- `range`: `since` (required) to `until` (default now), at most 92 days (`agent_hub.analysis.reads.period`); the
  provider measures no quota for an arbitrary span, so `measured` is null.

The tokens are those of `hub.subscription_usage` (`agent_hub.subscriptions.outbox`) in the slots from the slot of the
start up to the end, so a span is exact to five minutes. Per person: requests, the four token counts, `tokens` (their
sum), `sessions` (distinct sessions), `token_share` (the person's tokens over the tokens of everybody on that
subscription in the span; 0 without any) and `estimated_percent` = `token_share` x `measured.used_percent` (null
without `measured`). The provider measures the quota per subscription only, so this share of the quota is an
**estimate**: it splits the measured use - also any use outside the platform - in proportion to the tokens the
platform saw.

Who sees what (ADR-100 section 3): every subscription the caller **owns** is listed (also without any use), with every
person and the `total`; a subscription of another owner is listed when the caller used it in the span, with the
caller's own row only and `total` null. Numbers only: no content, no name, no address (the owner's client opens its
own records for names; another person's subscription is named by its owner).

Order: the caller's own subscriptions in the order of `GET /v1/me/subscriptions` (newest change first), then the
others by owner and ID; people by tokens (descending), then user ID.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.analysis.reads import period
from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.errors import HubError
from agent_hub.subscriptions.outbox import slot_of
from agent_hub.subscriptions.store import owner_records, reading_of

Connection = psycopg.Connection[Any]
PERIODS = ("five_hour", "week", "range")
WINDOWS: Mapping[str, tuple[str, timedelta]] = {
    "five_hour": ("five_hour", timedelta(hours=5)),
    "week": ("seven_day", timedelta(days=7)),
}
COUNTS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
TOKENS = COUNTS[1:]
PEOPLE = (
    "SELECT user_id, " + ", ".join(f"sum({name})" for name in COUNTS) + ", count(DISTINCT session_id)"
    " FROM hub.subscription_usage WHERE credential_id = %s AND slot >= %s AND slot < %s GROUP BY user_id"
)
SESSIONS = (
    "SELECT count(DISTINCT session_id) FROM hub.subscription_usage"
    " WHERE credential_id = %s AND slot >= %s AND slot < %s"
)
FOREIGN = (
    "SELECT DISTINCT u.credential_id, u.owner_id, c.is_primary FROM hub.subscription_usage u"
    " JOIN hub.credential c ON c.id = u.credential_id"
    " WHERE u.user_id = %s AND u.owner_id <> %s AND u.slot >= %s AND u.slot < %s ORDER BY u.owner_id, u.credential_id"
)


@dataclass(frozen=True)
class Span:
    """The span of one subscription and the measured window that belongs to it (None for none)."""

    since: datetime
    until: datetime
    measured: dict[str, Any] | None


def checked(name: str | None, since: str | None, until: str | None, now: datetime) -> tuple[str, Span | None]:
    """The period of a request: its name and, for `range`, its span (`400 request_invalid` with the pointer)."""
    if name not in PERIODS:
        raise HubError("request_invalid", detail={"pointers": ["/period"]})
    if name != "range":
        if since is not None or until is not None:
            raise HubError("request_invalid", detail={"pointers": ["/since" if since is not None else "/until"]})
        return name, None
    found = period({"since": since, "until": until}, now, since_required=True)
    return name, Span(found.since, found.until, None)


def window_span(name: str, reading: Mapping[str, Any] | None, now: datetime) -> Span:
    """The running window `name` of a subscription by its reading (module docstring)."""
    window_id, length = WINDOWS[name]
    if reading is not None and not reading["unreadable"]:
        for window in reading["windows"]:
            resets = window.get("resets_at")
            if window.get("window") != window_id or resets is None:
                continue
            end = parse_time(resets)
            if end <= now:
                break
            fetched = reading["fetched_at"]
            measured = {"used_percent": window["used_percent"], "resets_at": resets, "fetched_at": fetched}
            return Span(end - length, now, measured)
    return Span(now - length, now, None)


def _figures(values: tuple[Any, ...], sessions: int) -> dict[str, int]:
    found = {name: int(value) for name, value in zip(COUNTS, values, strict=True)}
    return {**found, "tokens": sum(found[name] for name in TOKENS), "sessions": sessions}


def subscription_usage(
    connection: Connection,
    *,
    record: UUID,
    owner: UUID,
    primary: bool,
    span: Span,
    caller: UUID,
) -> dict[str, Any]:
    """One entry of the answer (module docstring)."""
    bounds = (record, slot_of(span.since), span.until)
    rows = connection.execute(PEOPLE, bounds).fetchall()
    people: list[dict[str, Any]] = [{"user_id": str(row[0]), **_figures(tuple(row[1:6]), int(row[6]))} for row in rows]
    total_tokens = sum(person["tokens"] for person in people)
    used = None if span.measured is None else float(span.measured["used_percent"])
    for person in people:
        share = person["tokens"] / total_tokens if total_tokens > 0 else 0.0
        person["token_share"] = share
        person["estimated_percent"] = None if used is None else share * used
    people.sort(key=lambda person: (-person["tokens"], person["user_id"]))
    own = owner == caller
    total = None
    if own:
        sums = {name: sum(person[name] for person in people) for name in COUNTS}
        sessions = connection.execute(SESSIONS, bounds).fetchone()
        assert sessions is not None
        total = _figures(tuple(sums[name] for name in COUNTS), int(sessions[0]))
    else:
        people = [person for person in people if person["user_id"] == str(caller)]
    return {
        "subscription_id": str(record),
        "owner_id": str(owner),
        "primary": primary,
        "own": own,
        "since": format_time(span.since),
        "until": format_time(span.until),
        "measured": span.measured,
        "total": total,
        "people": people,
    }


def answer(connection: Connection, caller: UUID, name: str, fixed: Span | None, now: datetime) -> dict[str, Any]:
    """`GET /v1/me/subscriptions/usage` (module docstring)."""

    def span_of(record: UUID, owner: UUID, primary: bool) -> Span:
        return fixed if fixed is not None else window_span(name, reading_of(connection, record, owner, primary), now)

    found = []
    for identity in owner_records(connection, caller):
        span = span_of(identity.id, caller, identity.primary)
        found.append(
            subscription_usage(
                connection, record=identity.id, owner=caller, primary=identity.primary, span=span, caller=caller
            )
        )
    earliest = fixed.since if fixed is not None else now - WINDOWS[name][1]
    latest = fixed.until if fixed is not None else now
    for record, owner, primary in connection.execute(FOREIGN, (caller, caller, slot_of(earliest), latest)).fetchall():
        entry = subscription_usage(
            connection, record=record, owner=owner, primary=primary, span=span_of(record, owner, primary), caller=caller
        )
        if entry["people"]:
            found.append(entry)
    result: dict[str, Any] = {"period": name, "subscriptions": found}
    if fixed is not None:
        result.update(since=format_time(fixed.since), until=format_time(fixed.until))
    return result
