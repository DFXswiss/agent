"""Reading and writing encrypted events of a scope for the commands of `sessions`, `wgsettings` and `inbox` (06-web.md
section 4, 04-crypto.md section 6; twin of `web/src/core/tasks/pull.ts` and of the web client's event reader).

`pull` reads every page of `GET /v1/scopes/{scope}/events` after a position. `open_item` runs the reader checks of
`check_event` (author in the verified log at `lh`, kind rule, header rules, signature, write right; with the
session's device for session kinds; decertified platform devices excluded) and opens the body with the user's key of
the event's epoch. An event that does not verify is None (the caller counts it); one whose key the user does not hold
is `locked`. `seal` builds a user event of the workgroup scope under the current epoch and `post_event` sends it with
`POST /v1/events`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, unverified
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.orgs import now_text
from agent_runner.crypto.encoding import b64u_decode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import new_header, open_event, seal_event
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.events import check_event
from agent_runner.crypto.log.state import WG
from agent_runner.crypto.log.verify import VerifiedLog

PAGE = 1000
HEADER_FIELDS = ("id", "kind", "author", "ts", "wg", "task", "session", "ref")


def uuid_argument(value: str) -> str:
    """A UUIDv7 option (`invalid_argument` otherwise)."""
    if not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def rows_of(answer: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    """The list `name` of an answer, its objects only (`response_invalid` when it is not a list)."""
    items = answer.get(name)
    if not isinstance(items, list):
        raise unverified("response_invalid")
    return [item for item in items if isinstance(item, dict)]


def pull(account: Account, scope: str, query: Mapping[str, str], after: int = 0) -> tuple[list[Any], int]:
    """Every stored event of `scope` matching `query` after `after`, and the position of the last one read."""
    found: list[Any] = []
    last = after
    while True:
        target = f"/v1/scopes/{segment(scope)}/events?{urlencode({**query, 'after': after, 'limit': PAGE})}"
        page = account.call("GET", target)
        items = page.get("events")
        if not isinstance(items, list):
            raise unverified("response_invalid")
        for item in items:
            seq = item.get("scope_seq") if isinstance(item, dict) else None
            last = max(last, seq) if isinstance(seq, int) else last
        found.extend(items)
        following = page.get("next_after")
        if not isinstance(following, int) or following <= after:
            return found, last
        after = following


def excluded(account: Account, log: VerifiedLog) -> frozenset[str]:
    """Decertified platform devices of the org (the platform log is read only when the org has platform devices)."""
    if all(p.platform_seq is None for p in log.state.principals.values()):
        return frozenset()
    return account.platform().excluded(log.state)


def open_item(
    account: Account, org: str, scope: tuple[str, str], item: object, session_device: str | None = None
) -> dict[str, Any] | None:
    """The verified event `{seq, id, kind, author, ts, wg, task, session, ref, body|locked}` of the scope
    `(scope kind, scope id)`, or None when it does not verify (module docstring)."""
    log = account.org_log(org)
    try:
        data = cast(dict[str, Any], item)
        event = data["event"]
        header, _rule = check_event(event, log, session_device=session_device, excluded=excluded(account, log))
        if (header["scope"], header["scope_id"]) != scope:
            return None
        opened: dict[str, Any] = {"seq": data.get("scope_seq"), **{name: header[name] for name in HEADER_FIELDS}}
        key = account.keys(org).get((scope[0], scope[1], cast(int, header["epoch"])))
        if key is None:
            return {**opened, "locked": True}
        author = log.state.principals[cast(str, header["author"])]
        return {**opened, "body": parse_json(open_event(key, event, b64u_decode(author.sig)))}
    except (CryptoError, KeyError, TypeError):
        return None


def open_all(
    account: Account, org: str, scope: tuple[str, str], items: list[Any], session_device: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """The verified events of `items` and the number left out."""
    opened = [open_item(account, org, scope, item, session_device) for item in items]
    found = [event for event in opened if event is not None]
    return found, len(opened) - len(found)


def seal(account: Account, org: str, wg: str, kind: str, body: object, **extra: str) -> dict[str, object]:
    """A user event of `kind` for the workgroup scope under its current epoch (`epoch_stale` without the key)."""
    log = account.org_log(org)
    epoch = log.state.epoch(WG, wg)
    key = account.keys(org).get((WG, wg, epoch))
    if key is None:
        raise PlatformError("hub_refused", reason="epoch_stale")
    header = new_header(
        id=uuid7(),
        org=org,
        scope=WG,
        scope_id=wg,
        epoch=epoch,
        author=account.user_id,
        kind=kind,
        ts=now_text(),
        lh=log.head.seq,
        wg=wg,
        **extra,
    )
    return seal_event(key, header, body_json(body), account.identity.sig)


def post_event(account: Account, event: Mapping[str, Any]) -> int:
    """`POST /v1/events` with one event; its `scope_seq` (the answer must name the event)."""
    answer = account.call("POST", "/v1/events", {"events": [{"event": event}]})
    results = answer.get("results")
    result = results[0] if isinstance(results, list) and results else None
    if not isinstance(result, dict) or result.get("id") != event["header"]["id"]:
        raise unverified("response_invalid")
    seq = result.get("scope_seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise unverified("response_invalid")
    return seq
