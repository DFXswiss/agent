"""Post-processors of new events inside the storing transaction (the "registered post-processors" of HUB-06).

Some effects of an event must commit together with it (02-data-model.md section 1.3: projections in the same
transaction as their source row), e.g. the whiteboard projection and the notification rows of a `whiteboard.entry`
with `notify:true` (HUB-15) or the task bookkeeping of a prompt (HUB-09). An area registers them, without editing a
shared list (ADR-027 section 4), as `agent_hub/<area>/event_hooks.py:HOOKS`: a sequence of callables
`hook(connection, stored)` that `append_events` calls once per call, after every event of the call is stored, with
the newly stored events (`StoredEvent`, replays excluded; `notify` is the request flag). Hooks run in sorted area
order; an exception refuses the whole call (the caller's transaction rolls back) - a hook that must not block the
write belongs to the after-commit signal `event.appended` instead (`agent_hub.events.notify`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import psycopg

from agent_hub.registry import ExtensionPoint, RegistryError, concat_sequences

if TYPE_CHECKING:  # the store imports this module
    from agent_hub.events.store import StoredEvent

EVENT_HOOKS = ExtensionPoint("event_hooks", "HOOKS")
Hook = Callable[[psycopg.Connection[Any], "list[StoredEvent]"], object]

_lock = threading.Lock()
_state: dict[str, tuple[Hook, ...]] = {}


def configure(hooks: Iterable[Hook] | None = None) -> tuple[Hook, ...]:
    """Set the hooks (None discovers `<area>/event_hooks.py:HOOKS`) and return them."""
    found = tuple(concat_sequences(EVENT_HOOKS) if hooks is None else hooks)
    if not all(callable(hook) for hook in found):
        raise RegistryError("event_hooks.py HOOKS must hold callables")
    with _lock:
        _state["hooks"] = found
    return found


def current() -> tuple[Hook, ...]:
    """The current hooks (discovered on first use)."""
    found = _state.get("hooks")
    return found if found is not None else configure()


def run(connection: psycopg.Connection[Any], stored: list[StoredEvent]) -> None:
    """Call every hook with the newly stored events of one call (nothing to do without new events)."""
    if not stored:
        return
    for hook in current():
        hook(connection, stored)
