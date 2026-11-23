"""Outbox item `wrap` (03-api.md section 6.1): one catch-up key wrap, stored exactly as `POST /v1/device/wraps` does.

The W3 check of `agent_hub.wraps.rules` with the device as wrapper (it must hold the scope key, the recipient must be
entitled) refuses a wrap with `400 wrap_unauthorized` (or `header_invalid`/`signature_invalid` for a malformed one),
which the push acknowledges as `rejected`. A wrap that is already stored is ignored. After the commit the recipient of a
new wrap gets the `wraps` frame or hint.
"""

from __future__ import annotations

from agent_hub.device import wraps
from agent_hub.outbox.base import ItemContext
from agent_hub.outbox.bodies import WrapBody


def handle(item: ItemContext, body: WrapBody) -> None:
    """Store the wrap (W3) and announce it after the commit."""
    stored, kinds = wraps.store(item.connection, item.device, [body.wrap], now=item.now, root_key=item.push.root_key)
    item.after.append(lambda: wraps.publish_wraps(stored, kinds))
