"""Fan-out signals of new events: the PostgreSQL channel and the after-commit announcement.

**Channel.** `send` runs `pg_notify(CHANNEL, payload)` for every newly stored event inside the storing transaction;
PostgreSQL delivers it to every `LISTEN`ing connection only when that transaction commits (never on a rollback), so
every hub process (HUB-07's bus) learns of every committed event. The payload is canonical JSON with identifiers and
the order only: `{"org_id", "scope", "scope_id", "scope_seq"}` (well below the 8000-byte limit of PostgreSQL). It
never carries content (header fields beyond these, ciphertext, signatures); a listener reads the event with
`agent_hub.events.read`. `parse` checks a payload and returns it as `Appended`.

**Announcement.** After the caller's commit, `announce(stored)` handles every newly stored (not replayed) event:

- the browser frame `event {scope_id, scope_seq, event}` (or `terminal` for `session.terminal`) to the org's
  browsers through `agent_hub.stream.publish` (the stream filters by the scopes and watched sessions of each browser,
  HUB-07);
- the device hint `events {scope_id, scope_seq}` to every active device that reads the scope, except the author
  (not for `session.terminal`, which device pulls never return);
- the internal signal `event.appended` (`agent_hub.signals`, ADR-027 section 4) with `event_id`, `org_id`, `scope`,
  `scope_id`, `scope_seq`, `kind`, `author_id`, `notify` (1 for a `whiteboard.entry` sent with `notify:true`, else
  0) and, when set, `wg_id`, `task_id`, `session_id` and `ref` (only when `ref` is identifier text in the sense of
  the signals module; read targets with `/` are left out). Subscribers (whiteboard projection and notifications,
  tasks) register in `<area>/signals.py:SUBSCRIBERS`; their failures never affect the request.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import psycopg

from agent_hub import signals
from agent_hub.crypto.encoding import canonical
from agent_hub.stream.publish import Audience, publish

if TYPE_CHECKING:  # the store imports this module
    from agent_hub.events.store import StoredEvent

LOG = logging.getLogger("agent_hub.events")
CHANNEL = "hub_events"
SIGNAL = "event.appended"
TERMINAL_KIND = "session.terminal"
PAYLOAD_KEYS = frozenset({"org_id", "scope", "scope_id", "scope_seq"})
_SCOPES = frozenset({"org", "wg"})
_SIGNAL_TEXT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True)
class Appended:
    """The content of one notification on `CHANNEL`."""

    org_id: UUID
    scope: str
    scope_id: UUID
    scope_seq: int


def payload(stored: StoredEvent) -> str:
    """The notification text of a stored event."""
    body = {"org_id": stored.org_id, "scope": stored.scope, "scope_id": stored.scope_id, "scope_seq": stored.scope_seq}
    return canonical(body).decode("ascii")


def send(connection: psycopg.Connection[Any], stored: StoredEvent) -> None:
    """Queue the notification of a stored event in the current transaction (delivered at commit)."""
    connection.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload(stored)))


def _uuid(value: object) -> UUID:
    parsed = UUID(cast(str, value))
    if str(parsed) != value:
        raise ValueError("not a canonical UUID")
    return parsed


def parse(text: str) -> Appended:
    """A notification payload of `CHANNEL` (`ValueError` for anything else)."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != PAYLOAD_KEYS:
        raise ValueError("not an event notification")
    seq = data["scope_seq"]
    if data["scope"] not in _SCOPES or isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("not an event notification")
    return Appended(_uuid(data["org_id"]), cast(str, data["scope"]), _uuid(data["scope_id"]), seq)


def signal_ids(stored: StoredEvent) -> dict[str, UUID | int | str]:
    """The identifiers of the `event.appended` signal of a stored event."""
    ids: dict[str, UUID | int | str] = {
        "event_id": UUID(stored.id),
        "org_id": UUID(stored.org_id),
        "scope": stored.scope,
        "scope_id": UUID(stored.scope_id),
        "scope_seq": stored.scope_seq,
        "kind": stored.kind,
        "author_id": UUID(stored.author_id),
        "notify": int(stored.notify),
    }
    for name, value in (("wg_id", stored.wg_id), ("task_id", stored.task_id), ("session_id", stored.session_id)):
        if value is not None:
            ids[name] = UUID(value)
    if _SIGNAL_TEXT_RE.fullmatch(stored.ref):
        ids["ref"] = stored.ref
    return ids


def announce(stored: list[StoredEvent]) -> None:
    """Frames, hints and the signal of every newly stored event, after the commit (module docstring)."""
    for item in stored:
        if item.replayed:
            continue
        try:
            _announce(item)
        except Exception:  # the event is committed: a failing fan-out never turns it into an error
            LOG.error("announcing event %s failed", item.id, exc_info=True)


def _announce(item: StoredEvent) -> None:
    frame = "terminal" if item.kind == TERMINAL_KIND else "event"
    data = {"scope_id": item.scope_id, "scope_seq": item.scope_seq, "event": item.event}
    publish(Audience("org", UUID(item.org_id)), frame, data)
    hinted = () if item.kind == TERMINAL_KIND else sorted(item.devices)  # device pulls never return terminal
    for device in hinted:
        publish(Audience("device", UUID(device)), "events", {"scope_id": item.scope_id, "scope_seq": item.scope_seq})
    signals.emit(SIGNAL, **signal_ids(item))
