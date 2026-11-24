"""Base handlers of the outbox items `usage` and `quota` (03-api.md section 6.1, 02-data-model.md section 2.8; ADR-027
section 4 `<area>/outbox.py:HANDLERS`, claimed as base handlers with `BASE_KINDS`, ADR-037).

`usage` (`device:session`: the session must run on the pushing device, else `404 not_found` / `403 forbidden`):

- the seven counters are added to `usage_counter`; `cost_basis`, the context fill and the provider are the latest
  reported values;
- for platform work the `cost_micro_usd` delta is added to `usage_daily` of the session's org and the UTC day of the
  hub's receive time (`agent_hub.usage.budget`). Platform work is a session with `model_access = platform`; a session
  whose access is not reported yet counts when the cost is the provider's (`cost_basis = provider`, which only the
  platform's OpenRouter access reports, ADR-010), so a missing report cannot hide platform spend;
- the `session` frame (row, newest transition and the counters as `usage`) is recorded for the org's browsers that may
  read the workgroup, so task readers see the counters live.

Strict `origin_seq` order and the receipts of the outbox make every delta count exactly once (a replay is `replayed`
and runs no handler).

`quota`: the windows of one owner. Only the device that polls the owner's quota now (`agent_hub.usage.polling`) may
report them; any other report - another eligible device, an owner the device may not serve - is `403 forbidden`, which
the outbox acknowledges as `rejected` and audits as `outbox.rejected`. Each window keeps the newest fetch: an older
report than the stored one (a late retry, a report of the previous polling device) leaves the row unchanged. A report
`unavailable: true` (the usage endpoint refused the owner's token) drops the owner's windows fetched at or before it,
so `GET /v1/me/quotas` answers no rows and the web shows "not available" instead of an old snapshot (KOR-05).

Either form may carry `limited` (KOR-50, ADR-080 section 6): the latest observed limit of the owner's subscription,
kept in `hub.quota_limit` (a newer observation replaces an older one, never the reverse). A report without it that was
fetched after the stored limit was observed releases it: its `until` becomes the fetch time unless it lies earlier,
and the time of the limit stays visible.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agent_hub.errors import HubError
from agent_hub.outbox.base import Handler, ItemContext, moment, session_on_device
from agent_hub.outbox.bodies import QuotaBody, UsageBody
from agent_hub.sessions import rows
from agent_hub.usage import binding
from agent_hub.usage.budget import utc_day
from agent_hub.usage.polling import polls_for

COUNTERS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "thinking_tokens",
    "cost_micro_usd",
)
_ADD = ", ".join(f"{name} = hub.usage_counter.{name} + EXCLUDED.{name}" for name in COUNTERS)
_LATEST = ", ".join(
    f"{name} = EXCLUDED.{name}"
    for name in ("cost_basis", "context_used_tokens", "context_window_tokens", "last_provider", "updated_at")
)
UPSERT_COUNTER = (
    f"INSERT INTO hub.usage_counter (session_id, {', '.join(COUNTERS)}, cost_basis, context_used_tokens,"
    " context_window_tokens, last_provider, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    f" ON CONFLICT (session_id) DO UPDATE SET {_ADD}, {_LATEST}"
)
UPSERT_DAILY = (
    "INSERT INTO hub.usage_daily (org_id, day, device_id, cost_micro_usd) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (org_id, day, device_id) DO UPDATE"
    " SET cost_micro_usd = hub.usage_daily.cost_micro_usd + EXCLUDED.cost_micro_usd"
)
UPSERT_QUOTA = (
    'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, severity, fetched_at, device_id)'
    ' VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (owner_id, "window") DO UPDATE SET'
    " used_percent = EXCLUDED.used_percent, resets_at = EXCLUDED.resets_at, severity = EXCLUDED.severity,"
    " fetched_at = EXCLUDED.fetched_at, device_id = EXCLUDED.device_id"
    " WHERE hub.quota_snapshot.fetched_at <= EXCLUDED.fetched_at"
)
DROP_QUOTA = "DELETE FROM hub.quota_snapshot WHERE owner_id = %s AND fetched_at <= %s"
MARK_UNREADABLE = (
    "INSERT INTO hub.quota_unreadable (owner_id, since, device_id) SELECT %s, %s, %s"
    " WHERE NOT EXISTS (SELECT 1 FROM hub.quota_snapshot WHERE owner_id = %s AND fetched_at > %s)"
    " ON CONFLICT (owner_id) DO NOTHING"
)
CLEAR_UNREADABLE = "DELETE FROM hub.quota_unreadable WHERE owner_id = %s AND since <= %s"
UPSERT_LIMIT = (
    "INSERT INTO hub.quota_limit (owner_id, at, until, readable, device_id, fetched_at) VALUES (%s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (owner_id) DO UPDATE SET at = EXCLUDED.at, until = EXCLUDED.until, readable = EXCLUDED.readable,"
    " device_id = EXCLUDED.device_id, fetched_at = EXCLUDED.fetched_at WHERE hub.quota_limit.at <= EXCLUDED.at"
)
RELEASE_LIMIT = (
    "UPDATE hub.quota_limit SET until = %s WHERE owner_id = %s AND at < %s AND (until IS NULL OR until > %s)"
)
PLATFORM = "platform"
PROVIDER_COST = "provider"


def platform_work(model_access: str | None, cost_basis: str) -> bool:
    """The usage spends platform budget (module docstring)."""
    return model_access == PLATFORM or (model_access is None and cost_basis == PROVIDER_COST)


def handle_usage(item: ItemContext, body: UsageBody) -> None:
    """Add the delta to the session's counters and, for platform access, to the org's day (module docstring)."""
    org_id, _wg, _task, session = session_on_device(item, body.session_id)
    delta = body.delta
    item.connection.execute(
        UPSERT_COUNTER,
        (
            session,
            *(getattr(delta, name) for name in COUNTERS),
            body.cost_basis,
            body.context_used_tokens,
            body.context_window_tokens,
            body.last_provider,
            item.now,
        ),
    )
    row = item.connection.execute("SELECT model_access FROM hub.session WHERE id = %s", (session,)).fetchone()
    if platform_work(cast(tuple[str | None], row)[0], body.cost_basis):
        item.connection.execute(UPSERT_DAILY, (org_id, utc_day(item.now), item.device, delta.cost_micro_usd))
    frame = rows.session_frame(item.connection, session)
    item.effects.org_frame(org_id, "session", {**frame, "usage": rows.usage_json(item.connection, session)})


def handle_quota(item: ItemContext, body: QuotaBody) -> None:
    """Store the windows of the owner when this device polls them (module docstring)."""
    if not polls_for(item.connection, item.device, body.owner_id, item.now, binding.online_window()):
        raise HubError("forbidden")
    fetched = moment(body.fetched_at)
    limited = body.limited
    if limited is None:
        item.connection.execute(RELEASE_LIMIT, (fetched, body.owner_id, fetched, fetched))
    else:
        until = None if limited.until is None else moment(limited.until)
        observed = (body.owner_id, moment(limited.at), until, limited.readable, item.device, fetched)
        item.connection.execute(UPSERT_LIMIT, observed)
    if body.unavailable:
        item.connection.execute(DROP_QUOTA, (body.owner_id, fetched))
        # a late report after a newer successful read leaves no marker (review L5)
        item.connection.execute(MARK_UNREADABLE, (body.owner_id, fetched, item.device, body.owner_id, fetched))
        return
    item.connection.execute(CLEAR_UNREADABLE, (body.owner_id, fetched))
    values = [
        (
            body.owner_id,
            entry.window,
            entry.used_percent,
            None if entry.resets_at is None else moment(entry.resets_at),
            entry.severity,
            fetched,
            item.device,
        )
        for entry in body.windows
    ]
    with item.connection.cursor() as cursor:
        cursor.executemany(UPSERT_QUOTA, values)


HANDLERS: Mapping[str, Handler] = {"usage": handle_usage, "quota": handle_quota}
BASE_KINDS = frozenset(HANDLERS)
"""This area's handlers are the base handlers of these kinds (`agent_hub.outbox.chains`, ADR-037)."""
