"""After-commit notification work on internal signals (ADR-027 section 4, 03-api.md section 4.9, ADR-020).

- `event.appended` (events) of a `whiteboard.entry` with `notify` 1: the `notification` frames of the rows the storing
  transaction wrote and the `ping` hints to the devices of the pinged sessions;
- `task.state` (sessions): `task.idle` / `task.failed` to the owner;
- `invite.accepted` (invites): `invite.accepted` to the inviting admin;
- `log.appended` (orgs) and `device.decertified` (platform): `rotation.required` to the admins of the orgs with a
  pending rotation.

Signals carry identifiers only and no app context: like the other areas' subscribers, these read and write with the
pool of a bound hub context (bound by the job `notifications.bind_context`, `agent_hub.notifications.jobs`) and do
nothing before one is bound. A subscriber only queues the work on one background thread (the emitter may run on the
event loop); a failure there is logged and never reaches the emitter. Rows are written in their own transaction,
frames and hints are published after it committed. `wait` drains the queue (tests, shutdown).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID

from agent_hub import signals
from agent_hub.db.pool import transaction
from agent_hub.notifications import pings, rules
from agent_hub.stream.publish import hint

LOG = logging.getLogger("agent_hub.notifications")
_context: dict[str, Any] = {}
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notifications")
_pending: list[Future[None]] = []
Work = Callable[[Any], None]


def bind(context: Any) -> None:
    """Remember the hub context whose pool the subscribers use."""
    _context["hub"] = context


def run(work: Work) -> None:
    """Run one piece of work with the bound context (logged on failure)."""
    context = _context.get("hub")
    if context is None or context.pool.closed:
        return
    try:
        work(context)
    except Exception:
        LOG.error("notification work failed", exc_info=True)


def _queue(work: Work) -> None:
    if _context.get("hub") is not None:
        _pending[:] = [future for future in _pending if not future.done()]
        _pending.append(_worker.submit(run, work))


def wait() -> None:
    """Wait until every queued piece of work has run."""
    while _pending:
        _pending.pop(0).result()


def _whiteboard(entry_id: UUID, event_id: UUID) -> Work:
    def work(context: Any) -> None:
        with transaction(context.pool) as connection:
            created = connection.execute(
                "SELECT 1 FROM hub.whiteboard_entry WHERE id = %s AND created_event_id = %s", (entry_id, event_id)
            ).fetchone()
            if created is None:  # a later event with a known entry ID: it notified nobody
                return
            rows = rules.whiteboard_rows(connection, entry_id)
            hints = pings.ping_hints(connection, entry_id)
        rules.publish_rows(rows)
        for device, body in hints:
            hint(device, "ping", body)

    return work


def _rows(produce: Callable[[Any, Any], list[rules.Notification]]) -> Work:
    def work(context: Any) -> None:
        with transaction(context.pool) as connection:
            rows = produce(connection, context.clock())
        rules.publish_rows(rows)

    return work


def on_event(_name: str, ids: Mapping[str, Any]) -> None:
    """`event.appended`: the live part of a notified whiteboard entry."""
    if ids["kind"] == rules.WHITEBOARD and ids["notify"] == 1:
        _queue(_whiteboard(UUID(ids["ref"]), ids["event_id"]))


def on_task_state(_name: str, ids: Mapping[str, Any]) -> None:
    """`task.state` (`task_id`, `state`)."""
    task, state = ids["task_id"], ids["state"]
    _queue(_rows(lambda connection, _now: rules.task_rule(connection, task, state)))


def on_invite_accepted(_name: str, ids: Mapping[str, Any]) -> None:
    """`invite.accepted` (`org_id`, `invite_id`, `subject_id`)."""
    org, invite, subject = ids["org_id"], ids["invite_id"], ids["subject_id"]
    _queue(_rows(lambda connection, now: rules.invite_rule(connection, org, invite, subject, now)))


def on_log_appended(_name: str, ids: Mapping[str, Any]) -> None:
    """`log.appended` (`org_id`, `seq`): the org may owe a rotation now."""
    org = ids["org_id"]
    _queue(_rows(lambda connection, now: rules.rotation_rule(connection, [org], now)))


def on_device_decertified(_name: str, ids: Mapping[str, Any]) -> None:
    """`device.decertified` (`device`): every org of the device owes a rotation."""
    device = ids["device"]

    def produce(connection: Any, now: Any) -> list[rules.Notification]:
        return rules.rotation_rule(connection, rules.orgs_of_device(connection, device), now)

    _queue(_rows(produce))


SUBSCRIBERS: Mapping[str, Sequence[signals.Subscriber]] = {
    "event.appended": [on_event],
    "task.state": [on_task_state],
    "invite.accepted": [on_invite_accepted],
    "log.appended": [on_log_appended],
    "device.decertified": [on_device_decertified],
}
