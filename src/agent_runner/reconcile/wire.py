"""Shape checks of the hub answers the reconcile steps and the inbox use (03-api.md section 6.1, `contracts/api/`).

The hub is untrusted (00-overview.md section 3, B2): an answer is only used after its signature was verified
(`HubClient`), and only the fields a step needs are read, each checked here. A malformed answer raises
`ResponseInvalid` (a `ValueError`), which fails the step like a transport error; the content of signed objects
inside (log entries, wraps, events) is verified separately by the crypto rules.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from agent_runner.crypto.log.entries import Head

_HASH_RE = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_SEQ = 2**53 - 1


class ResponseInvalid(ValueError):
    """A verified hub answer without the expected shape."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def uuid_text(value: object, what: str) -> str:
    """A canonical lower-case UUID."""
    try:
        text = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        text = None
    if text is None or text != value:
        raise ResponseInvalid(what)
    return text


def seq(value: object, what: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_SEQ:
        raise ResponseInvalid(what)
    return value


def obj(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResponseInvalid(what)
    return value


def array(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResponseInvalid(what)
    return value


def head(value: object, what: str) -> Head:
    """A `log_head` `{seq, hash}`."""
    data = obj(value, what)
    found = data.get("hash")
    if set(data) != {"seq", "hash"} or not isinstance(found, str) or not _HASH_RE.fullmatch(found):
        raise ResponseInvalid(what)
    return Head(seq(data["seq"], what), found)


def device_state(answer: Mapping[str, Any]) -> str:
    """`active` or `pending_certification`."""
    state = answer.get("state")
    if state not in ("active", "pending_certification"):
        raise ResponseInvalid("state")
    return str(state)


def desired_orgs(answer: Mapping[str, Any]) -> list[tuple[str, Head]]:
    """`orgs[]` of an active device's state: `(org_id, log_head)`."""
    found: list[tuple[str, Head]] = []
    for item in array(answer.get("orgs"), "orgs"):
        data = obj(item, "orgs")
        if set(data) != {"org_id", "log_head"}:
            raise ResponseInvalid("orgs")
        found.append((uuid_text(data["org_id"], "orgs"), head(data["log_head"], "orgs")))
    return found


def log_page(answer: Mapping[str, Any]) -> tuple[list[object], Head, int | None]:
    """`{entries:[{seq, entry}], head, next_after}` of `/v1/device/logs` and `/v1/device/platform-log`.

    The `seq` of each item must equal the `seq` of the entry body (the verification uses the body)."""
    entries: list[object] = []
    for item in array(answer.get("entries"), "entries"):
        data = obj(item, "entries")
        body = data.get("entry", {}).get("body") if isinstance(data.get("entry"), dict) else None
        if set(data) != {"seq", "entry"} or not isinstance(body, dict) or body.get("seq") != seq(data["seq"], "seq"):
            raise ResponseInvalid("entries")
        entries.append(data["entry"])
    after = answer.get("next_after")
    return entries, head(answer.get("head"), "head"), None if after is None else seq(after, "next_after")


def last_origin_seq(answer: Mapping[str, Any]) -> int:
    return seq(answer.get("last_origin_seq"), "last_origin_seq")
