"""What an outbox item handler gets and may do (ADR-027 section 4, ADR-030).

A handler is `handler(item, body)`: `item` is the `ItemContext` of one outbox item inside the push transaction, `body`
the checked body of its kind (`agent_hub.outbox.bodies`). It applies the item's effect with `item.connection` and
raises `HubError` to refuse the item for a content reason (`400`/`403`/`404`/`409` codes: the item is acknowledged as
`rejected`, its savepoint is rolled back and the sequence continues). A `5xx` refusal or any other exception fails the
whole push, which the runner retries. Effects after the commit are recorded, never sent directly: frames, hints and
signals in `item.effects` (`agent_hub.sessions.effects.Effects`), other calls in `item.after`. A refused item's
recorded effects are dropped with its savepoint.

Lock order inside the push (`agent_hub.events.store`, `agent_hub.sessions.taskstate`): the events of the push are
stored first (org rows, scope heads, audit lock), then the handlers run in `origin_seq` order; a handler locks task
rows before session rows and writes its audit rows last.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.access import DeviceView, device_view
from agent_hub.errors import HubError
from agent_hub.events.store import StoredEvent
from agent_hub.sessions.effects import Effects

Connection = psycopg.Connection[Any]


@dataclass
class PushContext:
    """What every item of one push shares: the transaction, the device, the receive time and the device's view."""

    connection: Connection
    device: UUID
    now: datetime
    root_key: bytes
    _view: DeviceView | None = None

    def view(self) -> DeviceView:
        """What the device may read (`agent_hub.device.access`), loaded once per push from the verified logs."""
        if self._view is None:
            self._view = device_view(self.connection, self.device, self.root_key)
        return self._view


@dataclass
class ItemContext:
    """One outbox item while its handlers run (module docstring)."""

    push: PushContext
    origin_seq: int
    item_kind: str
    stored: StoredEvent | None = None
    """For an `event` item: the stored (or found again) event of the push's event phase."""
    effects: Effects = field(default_factory=Effects)
    after: list[Callable[[], None]] = field(default_factory=list)

    @property
    def connection(self) -> Connection:
        return self.push.connection

    @property
    def device(self) -> UUID:
        return self.push.device

    @property
    def now(self) -> datetime:
        return self.push.now

    def view(self) -> DeviceView:
        return self.push.view()


Handler = Callable[[ItemContext, Any], None]


def moment(text: str) -> datetime:
    """A contract time of a body as a datetime (`request_invalid` for an impossible date such as 02-31)."""
    try:
        return parse_time(text)
    except CryptoError:
        raise HubError("request_invalid") from None


def session_on_device(item: ItemContext, session_id: str | UUID) -> tuple[UUID, UUID, UUID, UUID]:
    """`device:session`: `(org_id, wg_id, task_id, session_id)` of a session that runs on the pushing device.

    An unknown session is `404 not_found`, a session of another device (or without device) `403 forbidden`.
    """
    row = item.connection.execute(
        "SELECT org_id, wg_id, task_id, id, device_id FROM hub.session WHERE id = %s", (str(session_id),)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    if row[4] != item.device:
        raise HubError("forbidden")
    return row[0], row[1], row[2], row[3]
