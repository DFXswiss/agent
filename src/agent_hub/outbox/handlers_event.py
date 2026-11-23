"""Outbox item `event` (03-api.md section 6.1): a device-authored event, appended with E1-E9.

The events of a push are stored by the push itself before any handler runs (`agent_hub.outbox.process`: one
`append_events` call with every `event` item and its `origin_seq`, so the lock order of `agent_hub.events.store`
holds); the right `device:wg` is E5 and "the session belongs to the device" is E7 of that call. This handler only
announces a newly stored event after the commit (live frame, device hints, signal `event.appended`); an event that was
already stored under its `id` (E9 replay) announces nothing.
"""

from __future__ import annotations

from typing import cast

from agent_hub.events.notify import announce
from agent_hub.events.store import StoredEvent
from agent_hub.outbox.base import ItemContext
from agent_hub.outbox.bodies import EventBody


def handle(item: ItemContext, _body: EventBody) -> None:
    """Announce the event of the push's event phase after the commit."""
    stored = cast(StoredEvent, item.stored)
    item.after.append(lambda: announce([stored]))
