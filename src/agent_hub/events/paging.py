"""Paging and hard caps of the list endpoints that were unbounded before ADR-063 section 2 (KOR-35).

A list endpoint answers in one of two modes:

- **paged** (the caller sent `before` and/or `limit`): one page newest first, at most `limit` rows (default
  `LIST_DEFAULT`, max `LIST_MAX`) and `next_before`, the opaque cursor of the last row when more rows follow (None at
  the end). The cursor is the b64u text of the canonical JSON `{"at": <microseconds since the epoch>, "id": <key>}` of
  the row's sort key `(at DESC, id DESC)`, so a page is stable while rows are added (keyset paging, the tie-breaker is
  the unique ID);
- **capped** (neither parameter, the behaviour of old clients): the same answer as before, cut at `cap` rows; a cut
  list carries `truncated: true` and nothing else changes.

`Window.extra(last)` gives the fields the answer adds in each mode.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count

LIST_DEFAULT = 50
LIST_MAX = 200
CAP_LARGE = 10000
"""The cap of the device lists, the whiteboard history and the embedded platform log."""
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def encode_cursor(at: datetime, key: str) -> str:
    """The opaque cursor of a row with the sort key `(at, key)`."""
    micros = (at - EPOCH) // timedelta(microseconds=1)
    return b64u_encode(canonical({"at": micros, "id": key}))


def decode_cursor(text: str, parse_key: Callable[[str], Any] = str) -> tuple[datetime, Any]:
    """`(at, key)` of a cursor, the key converted by `parse_key`; anything else is `400 request_invalid`."""
    try:
        data = json.loads(b64u_decode(text))
        at = EPOCH + timedelta(microseconds=data["at"])
        key = data["id"]
        if not isinstance(key, str) or encode_cursor(at, key) != text:
            raise ValueError("not a cursor")
        return at, parse_key(key)
    except (CryptoError, ValueError, TypeError, KeyError, OverflowError):
        raise HubError("request_invalid", detail={"pointers": ["/before"]}) from None


@dataclass(frozen=True)
class Window:
    """What one list request reads: `limit` rows (the page size, or the cap) after the optional cursor."""

    paged: bool
    limit: int
    before: tuple[datetime, Any] | None = None

    def extra(self, last: tuple[datetime, str] | None) -> dict[str, Any]:
        """The fields the answer adds, given the sort key of the last returned row when more rows follow (else None):
        `next_before` when paged, `truncated: true` when the cap cut the list."""
        if self.paged:
            return {"next_before": None if last is None else encode_cursor(*last)}
        return {} if last is None else {"truncated": True}


def window(before: str | None, limit: str | None, cap: int, parse_key: Callable[[str], Any] = str) -> Window:
    """The window of a request (`400 request_invalid` for a malformed cursor or limit)."""
    if before is None and limit is None:
        return Window(paged=False, limit=cap)
    count = parse_count(limit, LIST_DEFAULT, 1, LIST_MAX)
    return Window(paged=True, limit=count, before=None if before is None else decode_cursor(before, parse_key))


def cut(rows: Sequence[Any], limit: int) -> tuple[list[Any], bool]:
    """The first `limit` rows of a read of `limit + 1` rows and whether there were more."""
    return list(rows[:limit]), len(rows) > limit
