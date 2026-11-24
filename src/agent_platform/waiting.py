"""Held operations in the task's chat (11-agent-access.md section 8.4, KOR-25, ADR-066; twin of
`web/src/core/github/waiting.ts` and `ui/chat-items/waiting/WaitingCard.tsx`).

The runner holds a `github.*` operation of a session instead of refusing it when the task owner has no usable GitHub
connection and writes one `session.status` event `{state:"waiting", waiting_for, operation_id, operation_type, repo?,
until}`. `task show` and `task follow` fold them as the chat does:

- `parse_waiting`: the body exactly as the contract allows it (no further key, a known reason, a UUIDv7, a dotted
  type, `owner/name`, a time); anything else is no card (the event stays as it is, the web shows a neutral row);
- `fold_waiting`: the waiting events of one reason are one card at the place of the newest of them (`waiting` on that
  event), with every operation it stands for, the operation type of the newest, the repository of the newest that
  named one and the latest `until`; the older ones leave the list;
- `wait_status`: `done` once the viewer is the task owner and has a GitHub connection made at or after the newest
  waiting event (an older one is the connection the runner could not use) or every operation of the card is
  registered at the hub; `expired` after `until` or when every registered operation was refused; else `open`.

The card names the owner (the first non-device author of the task's `task.meta`, a user, as the web's
`selectOwner`), whether the viewer is that owner and, for the owner of an open card, the command that connects GitHub
(`github connect`, or with an existing token) and whether the stored connection is the one that could not be used.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from agent_platform.account import Account
from agent_platform.creds import github_status
from agent_platform.eventio import open_all, pull, rows_of
from agent_platform.hub import segment
from agent_platform.wgsettings import REPO_RE
from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import DEVICE, USER, WG

WAIT_REASONS = ("github_connection",)
TYPE_RE = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")
TYPE_MAX = 64
REQUIRED = frozenset({"state", "waiting_for", "operation_id", "operation_type", "until"})
REFUSED = frozenset({"rejected", "denied", "expired"})
OPERATIONS_LIMIT = 50
CONNECT = "github connect"


def _ms(value: object) -> int | None:
    try:
        return int(parse_time(value).timestamp() * 1000)
    except CryptoError:
        return None


def _fields(body: dict[str, Any]) -> bool:
    kind, repo = body["operation_type"], body.get("repo")
    return (
        body["waiting_for"] in WAIT_REASONS
        and is_uuid7(body["operation_id"])
        and isinstance(kind, str)
        and len(kind) <= TYPE_MAX
        and TYPE_RE.fullmatch(kind) is not None
        and (repo is None or (isinstance(repo, str) and REPO_RE.fullmatch(repo) is not None))
    )


def parse_waiting(body: object) -> dict[str, Any] | None:
    """A waiting body exactly as the contract allows it, else None (module docstring)."""
    if not isinstance(body, dict) or body.get("state") != "waiting" or not REQUIRED <= set(body) <= REQUIRED | {"repo"}:
        return None
    until = _ms(body["until"])
    if not _fields(body) or until is None:
        return None
    return {
        "reason": body["waiting_for"],
        "operation_id": body["operation_id"],
        "operation_type": body["operation_type"],
        "repo": body.get("repo"),
        "until": until,
    }


def _waiting_of(event: Mapping[str, Any]) -> dict[str, Any] | None:
    return parse_waiting(event.get("body")) if event["kind"] == "session.status" else None


def fold_waiting(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events (in chat order) with the waiting events of one reason folded into one card (module docstring)."""
    cards: dict[str, dict[str, Any]] = {}
    last: dict[str, int] = {}
    reasons: dict[int, str] = {}
    for index, event in enumerate(events):
        found = _waiting_of(event)
        if found is None:
            continue
        reason = found["reason"]
        reasons[index] = reason
        known = cards.get(reason)
        operations = [] if known is None else known["operations"]
        cards[reason] = {
            "reason": reason,
            "repo": found["repo"] if found["repo"] is not None else (None if known is None else known["repo"]),
            "operation_type": found["operation_type"],
            "operations": operations if found["operation_id"] in operations else [*operations, found["operation_id"]],
            "until_ms": max(0 if known is None else known["until_ms"], found["until"]),
            "since_ms": _ms(event.get("ts")) or 0,
        }
        last[reason] = index
    folded: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        reason = reasons.get(index)
        if reason is None:
            folded.append(event)
        elif last[reason] == index:
            folded.append({**event, "waiting": cards[reason]})
    return folded


def wait_status(card: Mapping[str, Any], now: int, connected_at: int | None, rows: Mapping[str, str]) -> str:
    """`open`, `done` or `expired` (module docstring); `rows` maps the registered operations to their states."""
    if connected_at is not None and connected_at >= card["since_ms"]:
        return "done"
    states = [rows[operation] for operation in card["operations"] if operation in rows]
    if len(states) == len(card["operations"]):
        return "expired" if all(state in REFUSED for state in states) else "done"
    return "expired" if now >= card["until_ms"] else "open"


def task_owner(account: Account, org: str, wg: str, task: str, metas: Sequence[Mapping[str, Any]] | None) -> str | None:
    """The first non-device author of the task's `task.meta` if it is a user (`selectOwner`); `metas` are the opened
    `task.meta` events when the caller has them, else they are read."""
    if metas is None:
        items, _last = pull(account, wg, {"kinds": "task.meta", "task": task})
        opened, _skipped = open_all(account, org, (WG, wg), items)
        metas = [event for event in opened if event["task"] == task]
    principals = account.org_log(org).state.principals
    for event in sorted(metas, key=lambda found: int(found["seq"])):
        principal = principals.get(event["author"])
        kind = None if principal is None else principal.kind
        if kind != DEVICE:
            return event["author"] if kind == USER else None
    return None


def _operation_states(account: Account, task: str) -> dict[str, str]:
    """The states of the task's operations on the first page of its list (`ensureOperations`)."""
    answer = account.call("GET", f"/v1/tasks/{segment(task)}/operations?{urlencode({'limit': OPERATIONS_LIMIT})}")
    return {
        str(row["operation_id"]): row["state"]
        for row in rows_of(answer, "operations")
        if row.get("task_id") == task and isinstance(row.get("operation_id"), str) and isinstance(row.get("state"), str)
    }


def _connection(account: Account) -> tuple[int | None, bool]:
    """`(time of the verified GitHub connection, whether a stored one could not be used)`."""
    found = github_status(account)
    at = _ms(found["created_at"]) if found["status"] == "connected" else None
    return at, at is not None or found["status"] == "unverifiable"


def annotate(
    account: Account,
    place: tuple[str, str, str],
    events: list[dict[str, Any]],
    metas: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """`events` folded, every card with its state, owner and next step (module docstring); `place` = (org, wg, task)."""
    folded = fold_waiting(events)
    cards = [event["waiting"] for event in folded if "waiting" in event]
    if not cards:
        return folded
    org, wg, task = place
    owner = task_owner(account, org, wg, task, metas)
    is_owner = owner is not None and owner == account.user_id
    connected_at, stale = _connection(account) if is_owner else (None, False)
    rows = _operation_states(account, task)
    now = int(time.time() * 1000)
    for card in cards:
        status = wait_status(card, now, connected_at, rows)
        card.update(owner_id=owner, is_owner=is_owner, status=status)
        if status == "open" and is_owner:
            card.update(reconnect=stale, next=CONNECT)
    return folded
