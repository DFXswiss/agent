"""In-app notifications (03-api.md section 4.9, 02-data-model.md section 2.6, ADR-020): the rules that create them.

A `notification` row is content-free: recipient, kind, org, workgroup, task, a reference ID and two times. Rules:

- `whiteboard.entry` (`notify_whiteboard`, inside the transaction that stores and projects a new entry sent with
  `notify:true`): one row for every user who reads the entry's level - org level: the active user members of the org;
  workgroup and task level: the active users with access to the workgroup - except the author, `ref` = the entry. The
  same transaction writes the session pings (`agent_hub.notifications.pings`) and the audit row `whiteboard.notify`
  (`{level, recipients}`, `event_id` = the entry's event; always written, also with no recipient, because the
  whiteboard rebuild reads the entry's `notify` flag from it).
- `task.idle` (`task_rule`): to the task owner when the task became `idle` because its main session went
  `working` -> `idle` after at least `IDLE_AFTER` of `working` (device times of the two session transitions);
  `created_at` = the hub receive time of the `idle` transition.
- `task.failed` (`task_rule`): to the task owner when the task became `failed`; `created_at` = the task transition.
  Both task rules notify an owner only while the owner still reads the task's workgroup. No `task.idle` is written
  while the task's attention record is `needs_decision` or `blocked` (`task.attention` covers it, ADR-064 section 5).
- `task.attention` (`attention_rule`, KOR-39): to the task owner (while the owner reads the workgroup) when the task's
  attention record entered an occasion of `needs_decision` or `blocked` and still is in it, once per occasion
  (`ref` = `<category>:<reason>:<seq>`). Causes that concern the whole workgroup (`WG_WIDE`: no computer, computer
  unreachable, all busy) notify an owner once per workgroup and reason while an earlier such notification is unread
  (`ref` = `<category>:<reason>:wg`). A `blocked` record of a failed task is covered by `task.failed` and notifies
  nothing; `created_at` = the hub time of the rule.
- `invite.accepted` (`invite_rule`): to the admin who created the org invitation (while still an admin of the org;
  never to the accepting subject itself), `ref` = the subject (user or device).
- `rotation.required` (`rotation_rule`): to every admin of an org whose `rotation_pending` is not empty, unless the
  admin still has an unread `rotation.required` of that org (one open reminder per admin and org).

The after-commit rules run from the subscribers of `agent_hub.notifications.signals` and are idempotent: a row whose
recipient, kind, org, task, reference and time match an existing row is not written again (task and invitation rules),
so a repeated signal never notifies twice. Every created row goes to the recipient's browsers as the stream frame
`notification` (`publish_rows`, after the commit).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.attention.derive import BLOCKED, OCCASIONS, WG_WIDE
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.notifications import pings
from agent_hub.stream.publish import Audience, publish
from agent_hub.whiteboards.projection import NOTIFY_ACTION, ORG, BoardEvent

Connection = psycopg.Connection[Any]
WHITEBOARD, TASK_IDLE, TASK_FAILED = "whiteboard.entry", "task.idle", "task.failed"
TASK_ATTENTION = "task.attention"
INVITE_ACCEPTED, ROTATION_REQUIRED = "invite.accepted", "rotation.required"
IDLE_AFTER = timedelta(seconds=10)
COLUMNS = "id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at, read_at"


@dataclass(frozen=True)
class Notification:
    """One notification row."""

    id: UUID
    recipient_id: UUID
    kind: str
    org_id: UUID | None
    wg_id: UUID | None
    task_id: UUID | None
    ref: str | None
    created_at: datetime
    read_at: datetime | None

    def to_json(self) -> dict[str, Any]:
        """The `notification_row` of the API and of the stream frame."""
        return {
            "notification_id": str(self.id),
            "kind": self.kind,
            "org_id": None if self.org_id is None else str(self.org_id),
            "wg_id": None if self.wg_id is None else str(self.wg_id),
            "task_id": None if self.task_id is None else str(self.task_id),
            "ref": self.ref,
            "created_at": format_time(self.created_at),
            "read_at": None if self.read_at is None else format_time(self.read_at),
        }


def row(values: tuple[Any, ...]) -> Notification:
    """A `Notification` from a row in the order of `COLUMNS`."""
    return Notification(*values)


def new_id(at: datetime) -> UUID:
    """A UUIDv7 with the time of `at`."""
    return UUID(uuid7(int(at.timestamp() * 1000)))


_INSERT = (
    "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at)"
    " SELECT %(id)s, %(recipient)s, %(kind)s, %(org)s, %(wg)s, %(task)s, %(ref)s, %(at)s"
)
_ONCE = (
    " WHERE NOT EXISTS (SELECT 1 FROM hub.notification WHERE recipient_id = %(recipient)s AND kind = %(kind)s"
    " AND org_id = %(org)s AND task_id IS NOT DISTINCT FROM %(task)s AND ref IS NOT DISTINCT FROM %(ref)s"
    " AND created_at = %(at)s)"
)


def _write(
    connection: Connection,
    recipient: UUID,
    kind: str,
    at: datetime,
    *,
    org_id: UUID,
    wg_id: UUID | None = None,
    task_id: UUID | None = None,
    ref: str | None = None,
    once: bool = False,
) -> list[Notification]:
    """Write one row and return it; with `once` nothing (empty list) if a row with the same recipient, kind, org,
    task, ref and time exists."""
    values = {"id": new_id(at), "recipient": recipient, "kind": kind, "org": org_id, "wg": wg_id, "task": task_id}
    values.update({"ref": ref, "at": at})
    rows = connection.execute(_INSERT + (_ONCE if once else "") + f" RETURNING {COLUMNS}", values).fetchall()
    return [row(found) for found in rows]


def _wg_reader(connection: Connection, wg_id: UUID, user_id: UUID) -> bool:
    found = connection.execute(
        "SELECT 1 FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id JOIN hub.org_member m ON m.org_id = w.org_id"
        " AND m.principal_id = a.user_id AND m.removed_seq IS NULL"
        " WHERE a.wg_id = %s AND a.user_id = %s AND a.revoked_seq IS NULL",
        (wg_id, user_id),
    ).fetchone()
    return found is not None


def whiteboard_recipients(connection: Connection, event: BoardEvent) -> list[UUID]:
    """The users who read the level of the entry, without its author, sorted."""
    if event.level == ORG:
        rows = connection.execute(
            "SELECT principal_id FROM hub.org_member WHERE org_id = %s AND kind = 'user' AND removed_seq IS NULL"
            " AND principal_id <> %s ORDER BY principal_id",
            (event.org_id, event.author_id),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT a.user_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
            " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.user_id AND m.removed_seq IS NULL"
            " WHERE a.wg_id = %s AND a.revoked_seq IS NULL AND a.user_id <> %s ORDER BY a.user_id",
            (event.wg_id, event.author_id),
        ).fetchall()
    return [found[0] for found in rows]


def notify_whiteboard(connection: Connection, event: BoardEvent) -> int:
    """The notification rows, session pings and the audit row of a new entry sent with `notify:true`; the number
    of recipients (module docstring)."""
    recipients = whiteboard_recipients(connection, event)
    for recipient in recipients:
        _write(
            connection,
            recipient,
            WHITEBOARD,
            event.received_at,
            org_id=event.org_id,
            wg_id=event.wg_id,
            task_id=event.task_id,
            ref=str(event.ref),
        )
    pings.create_whiteboard_pings(connection, event)
    append_audit(
        connection,
        AuditRow(
            at=event.received_at,
            actor_kind="user",
            actor_id=event.author_id,
            action=NOTIFY_ACTION,
            result="ok",
            org_id=event.org_id,
            wg_id=event.wg_id,
            task_id=event.task_id,
            target_kind="whiteboard_entry",
            target_id=str(event.ref),
            detail={"level": event.level, "recipients": len(recipients)},
            event_id=event.id,
        ),
    )
    return len(recipients)


def whiteboard_rows(connection: Connection, entry_id: UUID) -> list[Notification]:
    """The notification rows of an entry (sent after the commit)."""
    rows = connection.execute(
        f"SELECT {COLUMNS} FROM hub.notification WHERE kind = %s AND ref = %s ORDER BY recipient_id",
        (WHITEBOARD, str(entry_id)),
    ).fetchall()
    return [row(found) for found in rows]


_IDLE_AT = (
    "SELECT i.received_at FROM hub.session_transition i"
    " JOIN hub.session_transition w ON w.session_id = i.session_id AND w.seq = i.seq - 1"
    " WHERE i.session_id = (SELECT id FROM hub.session WHERE task_id = %(task)s AND depth = 0"
    " ORDER BY created_at DESC, id DESC LIMIT 1)"
    " AND i.seq = (SELECT max(seq) FROM hub.session_transition WHERE session_id = i.session_id AND state = 'idle')"
    " AND w.state = 'working' AND i.at - w.at >= %(after)s"
)
_FAILED_AT = (
    "SELECT at FROM hub.task_transition WHERE task_id = %(task)s AND state = 'failed' ORDER BY seq DESC LIMIT 1"
)


def task_rule(connection: Connection, task_id: UUID, state: str) -> list[Notification]:
    """`task.idle` / `task.failed` for a task that entered `state` (module docstring)."""
    if state not in ("idle", "failed"):
        return []
    task = connection.execute("SELECT org_id, wg_id, owner_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    if task is None or not _wg_reader(connection, task[1], task[2]):
        return []
    if state == "idle" and _needs_attention(connection, task_id):
        return []
    kind, query = (TASK_IDLE, _IDLE_AT) if state == "idle" else (TASK_FAILED, _FAILED_AT)
    at = connection.execute(query, {"task": task_id, "after": IDLE_AFTER}).fetchone()
    if at is None:
        return []
    return _write(connection, task[2], kind, at[0], org_id=task[0], wg_id=task[1], task_id=task_id, once=True)


def _needs_attention(connection: Connection, task_id: UUID) -> bool:
    found = connection.execute(
        "SELECT 1 FROM hub.task_attention WHERE task_id = %s AND category = ANY(%s)", (task_id, sorted(OCCASIONS))
    ).fetchone()
    return found is not None


@dataclass(frozen=True)
class Occasion:
    """The ids of the signal `task.attention` (`agent_hub.attention.store`)."""

    task_id: UUID
    category: str
    reason: str
    seq: int
    task_state: str


def attention_rule(connection: Connection, occasion: Occasion, now: datetime) -> list[Notification]:
    """`task.attention` for a task whose record entered an occasion (module docstring)."""
    if occasion.category == BLOCKED and occasion.task_state == "failed":
        return []
    found = connection.execute(
        "SELECT a.org_id, a.wg_id, a.owner_id FROM hub.task_attention a"
        " WHERE a.task_id = %s AND a.category = %s AND a.reason = %s AND a.seq = %s",
        (occasion.task_id, occasion.category, occasion.reason, occasion.seq),
    ).fetchone()
    if found is None or not _wg_reader(connection, found[1], found[2]):
        return []
    org_id, wg_id, owner_id = found
    wide = occasion.reason in WG_WIDE
    ref = f"{occasion.category}:{occasion.reason}:{'wg' if wide else occasion.seq}"
    duplicate = connection.execute(
        "SELECT 1 FROM hub.notification WHERE recipient_id = %s AND kind = %s AND org_id = %s AND ref = %s"
        " AND (CASE WHEN %s THEN wg_id = %s AND read_at IS NULL ELSE task_id = %s END)",
        (owner_id, TASK_ATTENTION, org_id, ref, wide, wg_id, occasion.task_id),
    ).fetchone()
    if duplicate is not None:
        return []
    return _write(
        connection, owner_id, TASK_ATTENTION, now, org_id=org_id, wg_id=wg_id, task_id=occasion.task_id, ref=ref
    )


def invite_rule(
    connection: Connection, org_id: UUID, invite_id: str, subject_id: UUID, now: datetime
) -> list[Notification]:
    """`invite.accepted` to the inviting admin (module docstring)."""
    found = connection.execute(
        "SELECT i.created_by FROM hub.invite i JOIN hub.org_member m ON m.org_id = i.org_id"
        " AND m.principal_id = i.created_by AND m.role = 'admin' AND m.removed_seq IS NULL"
        " WHERE i.id = %s AND i.org_id = %s AND i.created_by <> %s",
        (invite_id, org_id, subject_id),
    ).fetchone()
    if found is None:
        return []
    duplicate = connection.execute(
        "SELECT 1 FROM hub.notification WHERE recipient_id = %s AND kind = %s AND org_id = %s AND ref = %s",
        (found[0], INVITE_ACCEPTED, org_id, str(subject_id)),
    ).fetchone()
    if duplicate is not None:
        return []
    return _write(connection, found[0], INVITE_ACCEPTED, now, org_id=org_id, ref=str(subject_id))


def rotation_rule(connection: Connection, org_ids: Iterable[UUID], now: datetime) -> list[Notification]:
    """`rotation.required` to the admins of the orgs with a pending rotation (module docstring)."""
    created: list[Notification] = []
    for org_id in sorted(set(org_ids)):
        admins = connection.execute(
            "SELECT m.principal_id FROM hub.org_member m JOIN hub.org o ON o.id = m.org_id"
            " WHERE m.org_id = %s AND m.role = 'admin' AND m.removed_seq IS NULL AND o.rotation_pending <> '[]'"
            " AND NOT EXISTS (SELECT 1 FROM hub.notification n WHERE n.recipient_id = m.principal_id"
            " AND n.kind = %s AND n.org_id = m.org_id AND n.read_at IS NULL) ORDER BY m.principal_id",
            (org_id, ROTATION_REQUIRED),
        ).fetchall()
        for (admin,) in admins:
            created.extend(_write(connection, admin, ROTATION_REQUIRED, now, org_id=org_id))
    return created


def orgs_of_device(connection: Connection, device_id: UUID) -> list[UUID]:
    """The orgs in which the device is a member (a decertification marks their rotation pending)."""
    rows = connection.execute(
        "SELECT org_id FROM hub.org_member WHERE principal_id = %s AND kind = 'device' ORDER BY org_id", (device_id,)
    ).fetchall()
    return [found[0] for found in rows]


def publish_rows(rows: Iterable[Notification]) -> None:
    """The stream frame `notification` of every row to its recipient's browsers (after the commit); an account
    notification (no org, KOR-24) reaches only the streams opened with `account=1` (`agent_hub.stream.sse`)."""
    for notification in rows:
        publish(Audience("user", notification.recipient_id), "notification", notification.to_json())
