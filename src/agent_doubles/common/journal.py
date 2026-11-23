"""The request journal shared by every double.

One record per request, in the order the requests arrived. The JSON form of a record (also one line of the
journal file) is::

    {"seq": 3, "double": "model", "method": "POST", "path": "/v1/messages", "query": "beta=true",
     "headers": {"x-api-key": "<redacted 15 chars>", "anthropic-version": "2023-06-01"},
     "body": {...}, "status": 200, "note": null, ...double-specific fields (the model double adds "step")}

- ``headers``: lower-case names; the values of credential headers are reduced to the scheme and the length, so a
  journal never contains a credential.
- ``body``: the decoded JSON value, else at most `BODY_TEXT_LIMIT` characters of text, else null.
- ``seq`` grows by one per request for the life of the process; `Journal.clear` drops the records in memory but
  never restarts the numbering, so the file stays unambiguous.

The file is append-only and written when a request is finished.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"})
BODY_TEXT_LIMIT = 2000


def redact_value(name: str, value: str) -> str:
    """`value` of header `name` as the journal shows it: credentials reduced to scheme and length."""
    if name.lower() not in SECRET_HEADERS:
        return value
    scheme, _, rest = value.partition(" ")
    if rest and name.lower() in {"authorization", "proxy-authorization"}:
        return f"{scheme} <redacted {len(rest)} chars>"
    return f"<redacted {len(value)} chars>"


def redact_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """The headers with lower-case names and redacted credentials; repeated headers are joined with ``, ``."""
    out: dict[str, str] = {}
    for name, value in headers:
        key = name.lower()
        shown = redact_value(key, value)
        out[key] = f"{out[key]}, {shown}" if key in out else shown
    return out


def decode_body(raw: bytes) -> Any:
    """The body as the journal keeps it: decoded JSON, else bounded text, else None for an empty body."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:BODY_TEXT_LIMIT].decode("utf-8", "replace")


@dataclass
class Entry:
    """One request and how the double answered it. Handlers fill `status`, `note` and `extra` while answering."""

    seq: int
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: Any
    status: int = 0
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self, double: str) -> dict[str, Any]:
        """The record in the common journal format."""
        return {
            "seq": self.seq,
            "double": double,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "headers": self.headers,
            "body": self.body,
            "status": self.status,
            "note": self.note,
            **self.extra,
        }


class Journal:
    """The thread-safe journal of one double, optionally mirrored to a JSON-lines file."""

    def __init__(self, double: str, path: str | Path | None = None) -> None:
        self.double = double
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._seq = 0

    def open(self, method: str, path: str, query: str, headers: dict[str, str], body: Any) -> Entry:
        """Record a new request (headers already redacted) and return its entry."""
        with self._lock:
            self._seq += 1
            entry = Entry(self._seq, method, path, query, headers, body)
            self._entries.append(entry)
            return entry

    def close(self, entry: Entry) -> None:
        """Append the finished entry to the journal file, when there is one."""
        if self.path is None:
            return
        line = json.dumps(entry.to_json(self.double), separators=(",", ":"), sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def records(self) -> list[dict[str, Any]]:
        """Every record in memory, oldest first, in the common format."""
        with self._lock:
            return [entry.to_json(self.double) for entry in self._entries]

    def clear(self) -> None:
        """Drop the records in memory; the numbering and the file continue."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
