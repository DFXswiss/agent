"""Reading events of a scope like the browser's event reader (04-crypto.md sections 5.12 and 6.4; 06-web.md section
1.1): every stored event is settled as `verified` (reader checks of `check_event`: author, kind rule, header rules,
signature, right at `lh`; then opened with the scope key of its epoch), `locked` (it verifies but the user holds no key
of its epoch) or `unverifiable` with the crypto code. An event that names a log head beyond the verified one makes the
reader read the log again once (L10). Unverifiable and locked events are never shown with content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, KeyRing, unverified
from agent_platform.hub import segment
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.crypto.log.events import check_event

PAGE = 1000
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


def scope_events(account: Account, scope_id: str, query: dict[str, str], after: int = 0) -> list[Any]:
    """Every stored event of the scope matching `query`, page by page."""
    found: list[Any] = []
    while True:
        target = f"/v1/scopes/{segment(scope_id)}/events?{urlencode({**query, 'limit': PAGE})}&after={after}"
        page = account.call("GET", target)
        items = page.get("events")
        if not isinstance(items, list):
            raise unverified("response_invalid")
        found.extend(items)
        following = page.get("next_after")
        if not isinstance(following, int) or following <= after:
            return found
        after = following


def settle(account: Account, org: str, item: object, ring: KeyRing) -> Settled:
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
        header, _rule = check_event(event, log)
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
