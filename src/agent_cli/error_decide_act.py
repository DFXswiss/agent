"""Dispatch one fresh, one-shot session per unconcluded error.seen row."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any

from .store import Store, StoreError, utcnow

DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_POLL_INTERVAL_S = 5.0


def decide_session_id(error_id: str) -> str:
    """Deterministic per-error session id, so a retry after a timeout reuses the
    same session row instead of piling up a new one per attempt."""
    return f"error-decide-{error_id[:8]}"


def _has_conclusion(store: Store, error_id: str) -> bool:
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") not in ("error.fix", "error.skip"):
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and inner.get("error_id") == error_id:
            return True
    return False


def unconcluded_seen_rows(store: Store) -> list[dict[str, Any]]:
    origin = store.device_id()
    rows: list[dict[str, Any]] = []
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "error.seen":
            continue
        rid = row.get("id")
        if not isinstance(rid, str) or rid == "":
            continue
        if _has_conclusion(store, rid):
            continue
        rows.append(row)

    def sort_key(row: dict[str, Any]) -> tuple[str, str]:
        inner = row.get("payload")
        payload = inner if isinstance(inner, dict) else {}
        first = payload.get("first_seen")
        first_s = first if isinstance(first, str) else ""
        return (first_s, str(row.get("id") or ""))

    rows.sort(key=sort_key)
    return rows


def _ensure_decide_session(store: Store, sid: str, now: str) -> None:
    existing = store.row("session", sid)
    if existing is None:
        store.write(
            "session",
            "insert",
            sid,
            {
                "id": sid,
                "kind": "runner",
                "started_at": now,
                "last_seen_at": now,
                "host": socket.gethostname(),
                "status": "active",
                "skills": ["error-fix", "spine", "review-loop", "pr-review"],
            },
        )
        return
    if existing.get("_origin_device_id") != store.device_id():
        raise StoreError(f"session {sid} is owned by another device")
    if existing.get("status") == "closed":
        raise StoreError(f"session {sid} is closed")
    if existing.get("kind") != "runner":
        raise StoreError(f"session {sid} is kind={existing.get('kind')}, error-decide worker must be runner")
    required = ["error-fix", "spine", "review-loop", "pr-review"]
    current_skills = existing.get("skills")
    current = list(current_skills) if isinstance(current_skills, list) else []
    missing = [s for s in required if s not in current]
    if missing:
        updated = dict(existing)
        updated["skills"] = current + missing
        store.write("session", "update", sid, {k: v for k, v in updated.items() if not k.startswith("_")})


def _wait_for_conclusion(
    store: Store,
    error_id: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None],
) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _has_conclusion(store, error_id):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(poll_interval_s)


def scan_error_decide(
    store: Store,
    *,
    start: Callable[[str], None],
    stop: Callable[[str], None],
    knock: Callable[[str, str], None],
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> list[str]:
    # Held for the whole scan so two overlapping invocations don't double-dispatch
    # the same row. This key never collides with error-fix-act's own lock (different
    # pg_advisory_lock hashtext keys), so it doesn't block conclusion writes from the
    # sessions this dispatcher starts.
    with store.exclusive("error-decide-act:" + store.device_id()):
        backlog = unconcluded_seen_rows(store)
        lines: list[str] = []
        for row in backlog:
            error_id = str(row["id"])
            if _has_conclusion(store, error_id):
                continue
            sid = decide_session_id(error_id)
            now = utcnow()
            try:
                _ensure_decide_session(store, sid, now)
                try:
                    start(sid)
                    knock(sid, error_id)
                    decided = _wait_for_conclusion(
                        store,
                        error_id,
                        timeout_s=timeout_s,
                        poll_interval_s=poll_interval_s,
                        sleep=sleep,
                    )
                finally:
                    stop(sid)
            except (StoreError, SystemExit) as exc:
                lines.append(f"error.seen {error_id} error session={sid}: {exc}")
                continue
            if decided:
                lines.append(f"error.seen {error_id} decided session={sid}")
            else:
                lines.append(f"error.seen {error_id} timeout session={sid}")
        return lines
