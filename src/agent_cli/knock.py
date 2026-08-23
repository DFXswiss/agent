"""TUI knock: LISTEN agent_inbox and send only `da ist Post id <uuid>`."""

from __future__ import annotations

from typing import Any

from .runtime import Runtime
from .store import Store, StoreError
from .watch import acked_assigned_ids, pending_assigned, refresh_assigned_queue_files

KNOCK_PREFIX = "da ist Post id "


def knock_text(activity_id: str) -> str:
    if activity_id == "":
        raise StoreError("knock activity id is empty")
    return f"{KNOCK_PREFIX}{activity_id}"


def target_session_id(activity: dict[str, Any]) -> str | None:
    typ = activity.get("type")
    if typ in ("pr.merged", "issue.assigned", "error.seen"):
        sid = activity.get("session_id")
        return sid if isinstance(sid, str) and sid else None
    if typ == "message":
        inner = activity.get("payload")
        if isinstance(inner, dict):
            to_s = inner.get("to_session")
            if isinstance(to_s, str) and to_s:
                return to_s
    return None


def assigned_inflight_id(store: Store, session_id: str) -> str | None:
    acked = acked_assigned_ids(store, session_id)
    for row in store.rows("activity"):
        if row.get("type") != "issue.assigned":
            continue
        if row.get("session_id") != session_id:
            continue
        if row.get("_origin_device_id") != store.device_id():
            continue
        aid = row.get("id")
        if not isinstance(aid, str) or aid in acked:
            continue
        if store.wake_delivered(aid):
            return aid
    return None


def deliver(store: Store, runtime: Runtime, activity_id: str) -> str:
    """Send the knock or leave it queued. Returns sent|queued|unread|missing."""
    if store.wake_delivered(activity_id):
        return "sent"
    activity = store.row("activity", activity_id)
    if activity is None:
        return "missing"
    sid = target_session_id(activity)
    if sid is None:
        return "missing"
    session = store.row("session", sid)
    if session is None or session.get("_origin_device_id") != store.device_id():
        store.enqueue_wake(activity_id, sid)
        return "unread"
    if session.get("status") != "active":
        store.enqueue_wake(activity_id, sid)
        return "unread"
    raw = session.get("runtime")
    meta = raw if isinstance(raw, dict) else {}
    control = meta.get("control")
    pane = meta.get("tmux_pane")
    target = pane if isinstance(pane, str) and pane else None
    if target is None:
        stored = meta.get("tmux_session")
        target = stored if isinstance(stored, str) and stored else None
    if control != "attached" or not runtime.exists(sid, target=target):
        store.enqueue_wake(activity_id, sid)
        return "unread"
    if runtime.is_busy(sid):
        store.enqueue_wake(activity_id, sid)
        return "queued"
    if activity.get("type") == "issue.assigned":
        if activity.get("_origin_device_id") != store.device_id():
            return "missing"
        if activity_id in acked_assigned_ids(store, sid):
            store.claim_wake(activity_id)
            return "sent"
        pending = pending_assigned(store, sid)
        head = pending[0].get("id") if pending else None
        if isinstance(head, str) and head != activity_id:
            store.enqueue_wake(activity_id, sid)
            return "queued"
        inflight = assigned_inflight_id(store, sid)
        if inflight is not None and inflight != activity_id:
            store.enqueue_wake(activity_id, sid)
            return "queued"
        refresh_assigned_queue_files(store, sid, activity)
    if not store.claim_wake(activity_id):
        return "sent"
    text = knock_text(activity_id)
    try:
        runtime.input_text(sid, text, target=target)
        runtime.input_key(sid, "enter", target=target)
    except BaseException:
        store.unclaim_wake(activity_id)
        raise
    return "sent"


def drain(store: Store, runtime: Runtime) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in store.pending_wakes():
        status = deliver(store, runtime, row["activity_id"])
        out.append((row["activity_id"], status))
    return out


def listen_once(store: Store, runtime: Runtime, timeout: float = 2.0) -> str | None:
    import psycopg

    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute("LISTEN agent_inbox")
        drain(store, runtime)
        for notify in conn.notifies(timeout=timeout):
            payload = notify.payload
            if isinstance(payload, str) and payload:
                deliver(store, runtime, payload)
                return payload
            break
    return None
