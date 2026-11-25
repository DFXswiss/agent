"""The watched event kinds: the one list of kinds that only a watching browser receives (13-work-vm.md section 13.6,
ADR-016, ADR-085 section 4, E-19).

A watched kind is stored like every event (encrypted, signed, in order) but it is heavy or only of interest while
somebody looks at one session, so it never reaches a reader that did not ask for it:

- `GET /v1/scopes/{scope}/events` returns it only when `kinds` lists it (`agent_hub.events.read`);
- device pulls never return it and no device hint announces it (`agent_hub.device.pull`, `agent_hub.events.notify`);
- the browser stream sends it as its own frame type, only to streams that watch the event's session with the kind's
  stream parameter (`agent_hub.stream.sse`), and a stream replays it after its cursors only when `replay` is set
  (terminal output is small; the pictures of `browser.action` would fill the stream buffer, a viewer reads the record
  with the events route).

The next heavy kind is added here and nowhere else: every filter reads this list.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class WatchedKind:
    """One watched kind: its SSE frame type, the stream parameter that watches a session for it, and whether a stream
    replays it after its cursors."""

    kind: str
    frame: str
    param: str
    replay: bool


WATCHED = (
    WatchedKind("session.terminal", "terminal", "terminal", replay=True),
    WatchedKind("browser.action", "browser_action", "browser", replay=False),
)
BY_KIND = MappingProxyType({item.kind: item for item in WATCHED})
KINDS = frozenset(BY_KIND)
FRAMES = frozenset(item.frame for item in WATCHED)
PARAMS = tuple(item.param for item in WATCHED)
EVENT_FRAME = "event"


def frame_of(kind: str) -> str:
    """The SSE frame type of an event of this kind (`event` for every kind that is not watched)."""
    found = BY_KIND.get(kind)
    return EVENT_FRAME if found is None else found.frame
