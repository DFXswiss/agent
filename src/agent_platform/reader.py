"""The one event reader of the tool, like the browser's event reader (04-crypto.md sections 5.12 and 6.4; 06-web.md
section 1.1): every stored event is settled as `verified` (reader checks of `check_event`: author in the verified log
at `lh`, kind rule, header rules, signature, right at `lh`, with the session's device for session kinds when it is
known and decertified platform devices excluded; then opened with the scope key of its epoch), `locked` (it verifies
but the user holds no key of its epoch) or `unverifiable` with the crypto code. An event that names a log head beyond
the verified one makes the reader read the log again once (L10). Unverifiable and locked events are never shown with
content. Every command that reads events goes through `settle` (directly, or through `eventio.open_item`).

Reading pages (HOT-29): `scope_pages` reads the stored events of a scope ascending, one page at a time, so a caller
holds one page and not the whole scope; `scope_events` is all of them. `newest_events` reads the newest `count`
events below a position newest first (`before`, KOR-44) and names the position of the next older ones, so the end of
a long chat costs one request. A hub bounds a page by its count and by its bytes (`agent_hub.events.read.PAGE_BYTES`);
an older hub bounds it by its count only, and a page of large events may then exceed what the signed client accepts
(`response_too_large`): the page is asked for again with half the count, down to one event, and the smaller count is
kept for the following pages. A hub without `before` (it refuses it with `request_invalid`, or answers ascending) is
read ascending instead, keeping only the newest `count` events.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, KeyRing, unverified
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.crypto.log.events import check_event
from agent_runner.crypto.log.verify import VerifiedLog

PAGE = 1000
NEWEST = 10**16 - 1
"""`before` of the newest page: the largest position of the hub's contract (`q_seq`, up to 16 digits)."""
VERIFIED, LOCKED, UNVERIFIABLE = "verified", "locked", "unverifiable"


@dataclass
class Settled:
    """One stored event: its position, the unchecked header (placement only), the status and, if verified, the
    checked header and the body."""

    seq: int
    raw: dict[str, Any]
    status: str
    code: str | None = None
    header: dict[str, Any] = field(default_factory=dict)
    body: object = None


@dataclass(frozen=True)
class Tail:
    """The newest events of a query (`newest_events`): ascending, and `before` of the next older ones (None when no
    older event matches)."""

    items: list[Any]
    next_before: int | None


def _too_large(error: PlatformError) -> bool:
    return error.code == "hub_refused" and error.fields.get("reason") == "response_too_large"


def _page(
    account: Account, scope_id: str, query: dict[str, str], cursor: str, limit: int
) -> tuple[dict[str, Any], int]:
    """One page at `cursor` (`after=N` or `before=N`) and the count it was read with: an answer too large for the
    signed client is asked for again with half the count (module docstring)."""
    while True:
        target = f"/v1/scopes/{segment(scope_id)}/events?{urlencode({**query, 'limit': limit})}&{cursor}"
        try:
            page = account.call("GET", target)
        except PlatformError as error:
            if limit == 1 or not _too_large(error):
                raise
            limit //= 2
            continue
        if not isinstance(page.get("events"), list):
            raise unverified("response_invalid")
        return page, limit


def scope_seq(item: object) -> int:
    """The `scope_seq` of a stored item (0 when it has none; the reader then leaves the item out)."""
    seq = item.get("scope_seq") if isinstance(item, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


def scope_pages(account: Account, scope_id: str, query: dict[str, str], after: int = 0) -> Iterator[list[Any]]:
    """The stored events of the scope matching `query` after `after`, ascending, one page at a time."""
    limit = PAGE
    while True:
        page, limit = _page(account, scope_id, query, f"after={after}", limit)
        yield page["events"]
        following = page.get("next_after")
        if not isinstance(following, int) or following <= after:
            return
        after = following


def scope_events(account: Account, scope_id: str, query: dict[str, str], after: int = 0) -> list[Any]:
    """Every stored event of the scope matching `query`, page by page."""
    return [item for items in scope_pages(account, scope_id, query, after) for item in items]


def newest_events(
    account: Account, scope_id: str, query: dict[str, str], count: int, before: int | None = None
) -> Tail:
    """The newest `count` (at least 1) stored events of the scope matching `query` below `before` (default: all),
    read newest first page by page (module docstring)."""
    found: list[Any] = []
    cursor = NEWEST if before is None else before
    limit = min(count, PAGE)
    while True:
        try:
            page, limit = _page(account, scope_id, query, f"before={cursor}", min(limit, count - len(found)))
        except PlatformError as error:
            if error.fields.get("hub_error") != "request_invalid":
                raise
            return _newest_ascending(account, scope_id, query, count, cursor)
        if "next_before" not in page:
            return _newest_ascending(account, scope_id, query, count, cursor)
        found.extend(page["events"])
        following = page["next_before"]
        if not isinstance(following, int) or following >= cursor:
            return Tail(sorted(found, key=scope_seq), None)
        cursor = following
        if len(found) >= count:
            return Tail(sorted(found, key=scope_seq), cursor)


def _newest_ascending(account: Account, scope_id: str, query: dict[str, str], count: int, before: int) -> Tail:
    """`newest_events` on a hub without `before`: every page ascending, the newest `count` below `before` kept."""
    kept: deque[Any] = deque(maxlen=count)
    dropped = False
    for items in scope_pages(account, scope_id, query):
        for item in items:
            if scope_seq(item) < before:
                dropped = dropped or len(kept) == count
                kept.append(item)
    items = list(kept)
    return Tail(items, scope_seq(items[0]) if dropped else None)


def excluded(account: Account, log: VerifiedLog) -> frozenset[str]:
    """Decertified platform devices of the org (the platform log is read only when the org has platform devices)."""
    if all(p.platform_seq is None for p in log.state.principals.values()):
        return frozenset()
    return account.platform().excluded(log.state)


def settle(account: Account, org: str, item: object, ring: KeyRing, session_device: str | None = None) -> Settled:
    """The status of one stored item `{scope_seq, event}` (module docstring); `ring` holds the user's keys of the
    org (`Account.keys`, read once for many events)."""
    data = item if isinstance(item, dict) else {}
    seq = data.get("scope_seq")
    event = data.get("event")
    raw = event.get("header") if isinstance(event, dict) else None
    found = Settled(
        seq if isinstance(seq, int) and not isinstance(seq, bool) else 0, raw if isinstance(raw, dict) else {}, ""
    )
    log = account.org_log(org)
    lh = found.raw.get("lh")
    if isinstance(lh, int) and lh > log.head.seq:
        account.forget(org)
        log = account.org_log(org)
    try:
        header, _rule = check_event(event, log, session_device=session_device, excluded=excluded(account, log))
    except CryptoError as error:
        found.status, found.code = UNVERIFIABLE, error.code
        return found
    found.header = header
    key = ring.get((str(header["scope"]), str(header["scope_id"]), cast(int, header["epoch"])))
    if key is None:
        found.status = LOCKED
        return found
    try:
        author = log.state.principals[str(header["author"])]
        found.body = parse_json(open_event(key, event, b64u_decode(author.sig)))
    except CryptoError as error:
        found.status, found.code = UNVERIFIABLE, error.code
        return found
    found.status = VERIFIED
    return found
