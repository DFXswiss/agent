"""TUI knock: LISTEN agent_inbox and send only `da ist Post id <uuid>`."""

from __future__ import annotations

from typing import Any

from .runtime import Runtime
from .store import Store, StoreError

KNOCK_PREFIX = "da ist Post id "


def knock_text(activity_id: str) -> str:
    if activity_id == "":
        raise StoreError("knock activity id is empty")
    return f"{KNOCK_PREFIX}{activity_id}"


def target_session_id(activity: dict[str, Any]) -> str | None:
    typ = activity.get("type")
    if typ in ("pr.merged", "issue.assigned"):
        sid = activity.get("session_id")
        return sid if isinstance(sid, str) and sid else None
    if typ == "message":
        inner = activity.get("payload")
        if isinstance(inner, dict):
            to_s = inner.get("to_session")
            if isinstance(to_s, str) and to_s:
                return to_s
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
