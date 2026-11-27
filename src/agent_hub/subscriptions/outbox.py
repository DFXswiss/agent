"""Outbox handler extension of the subscriptions area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030; ADR-102
section 1): the tokens of a `usage` item on the Claude subscription that served it.

`usage` (after the usage area's base handler, which already checked that the session runs on the pushing device): an
item with `subscription_id` adds its delta (requests and the four token counts; thinking tokens are part of the output)
to `hub.subscription_usage` under the subscription, the five-minute slot of its request time (`at`, never after the
receive time; the receive time without one), the person and the session. The person is the item's `triggered_by` when
that person wrote a prompt of the session's task (`agent_hub.billing.outbox.attributed`, as for the charges), else
the session's owner. The row is kept only when the record is a `claude_subscription` that may serve the session: the
primary record of the session's owner (own sessions) or a further record whose use mirror names the session's
organization or the pool (ADR-090 section 3); anything else - a record that is gone, of another person, or not shared
with that organization - is dropped without an error, so a device can neither refuse nor misplace an item by naming
a wrong record. An item without `subscription_id` (an older runner, an access that is no subscription record) records
nothing here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from agent_hub.billing.outbox import attributed, measured
from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import UsageBody

SLOT = timedelta(minutes=5)
COUNTS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_ADD = ", ".join(f"{name} = hub.subscription_usage.{name} + EXCLUDED.{name}" for name in COUNTS)
UPSERT = (
    "INSERT INTO hub.subscription_usage (credential_id, owner_id, user_id, org_id, session_id, slot,"
    f" {', '.join(COUNTS)})"
    " SELECT c.id, c.owner_id, COALESCE(%(person)s::uuid, s.owner_id), s.org_id, s.id, %(slot)s,"
    " %(requests)s, %(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s, %(cache_write_tokens)s"
    " FROM hub.credential c JOIN hub.session s ON s.id = %(session)s"
    " WHERE c.id = %(subscription)s AND c.kind = 'claude_subscription'"
    " AND ((c.is_primary AND c.owner_id = s.owner_id) OR EXISTS (SELECT 1 FROM hub.subscription_use u"
    " WHERE u.credential_id = c.id AND (u.pool OR u.orgs ? s.org_id::text)))"
    f" ON CONFLICT (credential_id, slot, user_id, session_id) DO UPDATE SET {_ADD}"
)


def slot_of(at: datetime) -> datetime:
    """The start of the five-minute slot of `at`."""
    return at - timedelta(minutes=at.minute % 5, seconds=at.second, microseconds=at.microsecond)


def record_usage(item: ItemContext, body: UsageBody) -> None:
    """Add the item's tokens to its subscription (module docstring)."""
    if body.subscription_id is None:
        return
    row = item.connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (body.session_id,)).fetchone()
    assert row is not None  # the base handler refused an unknown session
    delta = body.delta
    item.connection.execute(
        UPSERT,
        {
            "person": attributed(item, row[0], body.triggered_by),
            "slot": slot_of(measured(item, body.at)),
            "session": body.session_id,
            "subscription": body.subscription_id,
            **{name: getattr(delta, name) for name in COUNTS},
        },
    )


HANDLERS: Mapping[str, Handler] = {"usage": record_usage}
