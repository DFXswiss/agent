"""Terminal storage at the hub (ADR-016 "storage grows with terminal volume … measured in M3"; AUD-01 B-19).

Every `session.terminal` chunk is an event row of `hub.event` (`ct` ciphertext, `header_text`, 64-byte signature,
`size` = the plaintext body size the header states). Per session: number of chunks, ciphertext bytes, row payload
bytes (ciphertext + header + signature), plaintext bytes and the time between the first and the last chunk the hub
received. The summary adds the bytes per chunk and the payload rate per session hour (sessions with a time span only),
plus the total size of `hub.event` with indexes and TOAST (`pg_total_relation_size`) and its row count. Rows are
appended to `<artifacts>/perf-terminal-storage.csv`.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

COLUMNS = ("iteration", "label", "session_id", "chunks", "ct_bytes", "row_bytes", "plain_bytes", "span_s")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECONDS_PER_HOUR = 3600.0


def valid_sessions(sessions: object) -> list[str] | None:
    if not isinstance(sessions, list) or not all(isinstance(s, str) and _UUID_RE.fullmatch(s) for s in sessions):
        return None
    return list(sessions)


def sessions_sql(session_ids: Sequence[str] = ()) -> str:
    """One row per session with terminal chunks (all sessions, or the given ones; IDs checked by the caller)."""
    where = "kind = 'session.terminal'"
    if session_ids:
        where += " AND session_id IN (" + ", ".join(f"'{s}'" for s in session_ids) + ")"
    return (
        "SELECT session_id::text, count(*)::int AS chunks, sum(octet_length(ct))::bigint AS ct_bytes,"
        " sum(octet_length(ct) + octet_length(header_text) + octet_length(sig))::bigint AS row_bytes,"
        " sum(size)::bigint AS plain_bytes,"
        " extract(epoch FROM max(received_at) - min(received_at))::float8 AS span_s"
        f" FROM hub.event WHERE {where} GROUP BY session_id ORDER BY session_id"
    )


def relation_sql() -> str:
    return (
        "SELECT pg_total_relation_size('hub.event')::bigint AS event_total_bytes,"
        " (SELECT count(*) FROM hub.event)::bigint AS event_rows"
    )


def summarize(rows: Sequence[dict[str, Any]], relation: dict[str, Any]) -> dict[str, Any]:
    chunks = sum(int(row["chunks"]) for row in rows)
    row_bytes = sum(int(row["row_bytes"]) for row in rows)
    timed = [row for row in rows if float(row["span_s"]) > 0]
    timed_bytes = sum(int(row["row_bytes"]) for row in timed)
    timed_hours = sum(float(row["span_s"]) for row in timed) / SECONDS_PER_HOUR
    return {
        "sessions": len(rows),
        "chunks": chunks,
        "ct_bytes": sum(int(row["ct_bytes"]) for row in rows),
        "row_bytes": row_bytes,
        "plain_bytes": sum(int(row["plain_bytes"]) for row in rows),
        "bytes_per_chunk": row_bytes / chunks if chunks else None,
        "row_bytes_per_session_hour": timed_bytes / timed_hours if timed else None,
        "relation": relation,
    }


def write_csv(path: Path, iteration: str, label: str, rows: Sequence[dict[str, Any]]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([iteration, label, *(row[column] for column in COLUMNS[2:])])
