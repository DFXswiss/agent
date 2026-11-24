"""The export of the bookings for the books (owner's decision 19; 12-billing.md section 10, ADR-062 section 9).

`bookings(connection, first, last, org)` lists every booking whose day in Europe/Zurich lies in `first`..`last`
(optionally of one organization), oldest first: the charges (`tokens`, `runtime`) and the credit history entries
(`start`, `grant`, `set`, `manual`). Each row carries the time, the organization (cost bearer), the kind, the amount in
USD, the rate, the rate's day, the CHF equivalent booked with it and the reference (charge ID with workgroup, task and
session - or with the per-device DFX key for a share no session reported, ADR-081 section 7 - or the number of the
credit entry). Amounts are micro-units; the CHF column is the equivalent stored with
the booking (never recomputed), so the sum of the column is the CHF total of the period. No content of tasks.

The operator command `agent-hub billing export` writes it as JSON lines (default) or CSV; the export is audited
(`billing.export` with the period, the organization and the number of rows).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.billing.fx import ZONE

Connection = psycopg.Connection[Any]
FIELDS = (
    "at",
    "org_id",
    "kind",
    "amount_micro_usd",
    "chf_per_usd",
    "fx_day",
    "amount_micro_chf",
    "reference",
)
_WINDOW = "(at AT TIME ZONE %s)::date BETWEEN %s AND %s AND (%s::uuid IS NULL OR org_id = %s)"


def bookings(connection: Connection, first: date, last: date, org: UUID | None) -> list[dict[str, Any]]:
    """The rows of the period (module docstring)."""
    window = (ZONE, first, last, org, org)
    charges = connection.execute(
        "SELECT at, org_id, kind, amount_micro_usd, chf_per_usd_e8, fx_day, amount_micro_chf,"
        " 'charge:' || id || COALESCE(' wg:' || wg_id || ' task:' || task_id || ' session:' || session_id,"
        " ' key:' || (quantity->>'key_ref'))"
        f" FROM hub.charge WHERE {_WINDOW}",
        window,
    ).fetchall()
    credits = connection.execute(
        "SELECT at, org_id, kind, amount_micro_usd, chf_per_usd_e8, fx_day, amount_micro_chf, 'credit:' || n"
        f" FROM hub.credit_entry WHERE {_WINDOW}",
        window,
    ).fetchall()
    rows = sorted([*charges, *credits], key=lambda row: (row[0], str(row[7])))
    return [_row(row) for row in rows]


def _row(row: Sequence[Any]) -> dict[str, Any]:
    at, org, kind, usd, rate, fx_day, chf, reference = row
    return {
        "at": at.isoformat(),
        "org_id": str(org),
        "kind": kind,
        "amount_micro_usd": int(usd),
        "chf_per_usd": None if rate is None else f"{int(rate) / 10**8:.8f}",
        "fx_day": None if fx_day is None else fx_day.isoformat(),
        "amount_micro_chf": None if chf is None else int(chf),
        "reference": reference,
    }


def as_csv(rows: Sequence[dict[str, Any]]) -> str:
    """The rows as CSV with a header line (empty fields for a booking without equivalent)."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row[key] is None else row[key] for key in FIELDS})
    return out.getvalue()


def audit_export(
    connection: Connection, period: tuple[date, date], org: UUID | None, count: int, now: datetime
) -> None:
    first, last = period
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="operator",
            action="billing.export",
            result="ok",
            org_id=org,
            target_kind="billing",
            target_id=f"{first.isoformat()}..{last.isoformat()}",
            detail={"from": first.isoformat(), "to": last.isoformat(), "rows": count},
        ),
    )
