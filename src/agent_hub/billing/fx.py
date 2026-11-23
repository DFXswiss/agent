"""The USD/CHF rate for the books (owner's decisions 18 and 19; 12-billing.md section 10, ADR-059 section 9).

USD is the only currency of prices, balances, the interface and the APIs. For the books every booking - a charge, a
start credit, an operator credit or set, a hand-set balance - additionally keeps its **CHF equivalent**, computed
once when it is booked and never again:

- `hub.fx_rate` [AO] holds the rates the operator sets (`agent-hub billing fx set`), one per calendar day; the hub asks
  no outside service (it has no internet access). A **correction** is a further row of the same day with a reason; it
  applies to bookings made after it and never changes a booking already made.
- **Rate of a booking**: the newest row of the latest day at or before the booking's day in Europe/Zurich (computed
  by PostgreSQL, which carries its own time zone data). A missing day (weekend, holiday, a job that failed) takes the
  day before; the booking records the rate's day, so this stays visible. Billing never stops for a missing rate; a
  booking without any rate at all (only possible before the first rate, which `price-list add` requires) keeps no
  equivalent and is logged.
- **Rate format**: CHF per 1 USD as an exact decimal with at most 8 places, stored as `chf_per_usd_e8` (x 10^8).
- **Rounding**: `amount_micro_chf = round_half_even(amount_micro_usd x chf_per_usd_e8 / 10^8)`, once per booking. Sums
  in CHF are the sums of the booked equivalents (the export lists every booking, so each total can be recomputed).
- **Stale rate**: older than `STALE_DAYS` days (Europe/Zurich) - the settle job logs a warning every hour and
  `agent-hub billing fx show` exits with 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit

Connection = psycopg.Connection[Any]
SCALE = 10**8
MAX_RATE = Decimal(100)
STALE_DAYS = 4
"""A rate older than this many days is stale (a long weekend with a holiday has 3 days without a rate)."""
ZONE = "Europe/Zurich"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,64}$")
_RATE_RE = re.compile(r"^(?:0|[1-9][0-9]{0,2})\.[0-9]{1,8}$|^[1-9][0-9]{0,2}$")


class FxInvalid(ValueError):
    """A rate the operator cannot set (format, range, missing reason for a correction)."""


@dataclass(frozen=True)
class Rate:
    n: int
    day: date
    chf_per_usd_e8: int

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.chf_per_usd_e8) / SCALE


def parse_rate(text: str) -> int:
    """`0.8812` -> 88120000 (CHF per USD x 10^8); at most 8 decimals, above 0 and at most 100."""
    if not _RATE_RE.fullmatch(text):
        raise FxInvalid("the rate must be a decimal number of CHF per USD with at most 8 decimals")
    value = Decimal(text)
    if value <= 0 or value > MAX_RATE:
        raise FxInvalid(f"the rate must be above 0 and at most {MAX_RATE}")
    return int(value * SCALE)


def chf(amount_micro_usd: int, chf_per_usd_e8: int) -> int:
    """The CHF equivalent in micro-CHF, rounded once, half-even (module docstring)."""
    exact = Decimal(amount_micro_usd) * Decimal(chf_per_usd_e8) / Decimal(SCALE)
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def zurich_day(connection: Connection, at: datetime) -> date:
    """The calendar day of `at` in Europe/Zurich."""
    row = cast(tuple[date], connection.execute("SELECT (%s::timestamptz AT TIME ZONE %s)::date", (at, ZONE)).fetchone())
    return row[0]


def rate_for(connection: Connection, at: datetime) -> Rate | None:
    """The rate of a booking at `at` (module docstring); None before the first rate."""
    row = connection.execute(
        "SELECT n, day, chf_per_usd_e8 FROM hub.fx_rate WHERE day <= (%s::timestamptz AT TIME ZONE %s)::date"
        " ORDER BY day DESC, n DESC LIMIT 1",
        (at, ZONE),
    ).fetchone()
    return None if row is None else Rate(int(row[0]), row[1], int(row[2]))


def equivalent(connection: Connection, at: datetime, amount_micro_usd: int) -> tuple[Any, ...]:
    """`(fx_n, fx_day, chf_per_usd_e8, amount_micro_chf)` of a booking (all None without any rate)."""
    rate = rate_for(connection, at)
    if rate is None:
        return (None, None, None, None)
    return (rate.n, rate.day, rate.chf_per_usd_e8, chf(amount_micro_usd, rate.chf_per_usd_e8))


def any_rate(connection: Connection) -> bool:
    return connection.execute("SELECT 1 FROM hub.fx_rate LIMIT 1").fetchone() is not None


def set_rate(connection: Connection, day: date, text: str, *, source: str, reason: str | None, now: datetime) -> Rate:
    """Add the rate of `day`; a further rate of a day that has one is a correction and needs a reason."""
    value = parse_rate(text)
    if not _SOURCE_RE.fullmatch(source):
        raise FxInvalid("the source must be one word of letters, digits and . _ : / - (at most 64)")
    note = None if reason is None else reason.strip()
    if note is not None and (not note or len(note) > 500):
        raise FxInvalid("the reason must be a text of 1 to 500 characters")
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('hub.fx_rate'))")
    exists = connection.execute("SELECT 1 FROM hub.fx_rate WHERE day = %s", (day,)).fetchone() is not None
    if exists and note is None:
        raise FxInvalid(f"{day.isoformat()} has a rate: a correction needs --reason (bookings made keep their rate)")
    row = cast(tuple[int], connection.execute("SELECT COALESCE(max(n), 0) + 1 FROM hub.fx_rate").fetchone())
    n = int(row[0])
    connection.execute(
        "INSERT INTO hub.fx_rate (n, day, chf_per_usd_e8, source, set_at, actor, reason)"
        " VALUES (%s, %s, %s, %s, %s, 'operator', %s)",
        (n, day, value, source, now, note),
    )
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="operator",
            action="billing.fx_set",
            result="ok",
            target_kind="fx_rate",
            target_id=day.isoformat(),
            detail={
                "day": day.isoformat(),
                "chf_per_usd_e8": value,
                "source": source,
                "kind": "correction" if exists else "first",
            },
        ),
    )
    return Rate(n, day, value)


def rates(connection: Connection, first: date | None = None, last: date | None = None) -> list[dict[str, Any]]:
    """The rates of the days `first`..`last` (all without bounds), every correction included, oldest first."""
    rows = connection.execute(
        "SELECT n, day, chf_per_usd_e8, source, set_at, reason FROM hub.fx_rate"
        " WHERE (%s::date IS NULL OR day >= %s) AND (%s::date IS NULL OR day <= %s) ORDER BY day, n",
        (first, first, last, last),
    ).fetchall()
    return [
        {
            "n": int(n),
            "day": day.isoformat(),
            "chf_per_usd": str(Decimal(value) / SCALE),
            "source": source,
            "set_at": set_at.isoformat(),
            "reason": reason,
        }
        for n, day, value, source, set_at, reason in rows
    ]


def age_days(connection: Connection, now: datetime) -> int | None:
    """Days between the newest rate's day and today in Europe/Zurich; None without a rate."""
    newest = cast(tuple[date | None], connection.execute("SELECT max(day) FROM hub.fx_rate").fetchone())[0]
    if newest is None:
        return None
    return (zurich_day(connection, now) - newest).days
