"""Execute pending subscription.set and query.request activities against the hub."""

from __future__ import annotations

import uuid
from typing import Any

from .hub import Hub, HubError
from .store import Store


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _error_text(exc: BaseException) -> str:
    return str(exc)[:500]


def _mark(store: Store, row: dict[str, Any], *, status: str, error: str | None = None) -> None:
    updated = _strip(row)
    updated["execution_status"] = status
    if error is None:
        updated.pop("execution_error", None)
    else:
        updated["execution_error"] = error
    store.write("activity", "update", updated["id"], updated)


def _run_subscription_set(store: Store, hub: Hub, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    inner = row.get("payload")
    if not isinstance(inner, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"subscription.set {rid} error"
    subscriptions = inner.get("subscriptions")
    if not isinstance(subscriptions, list):
        _mark(store, row, status="error", error="payload.subscriptions must be a list")
        return f"subscription.set {rid} error"
    try:
        hub.put_subscriptions(subscriptions)
    except HubError as exc:
        _mark(store, row, status="error", error=_error_text(exc))
        return f"subscription.set {rid} error"
    _mark(store, row, status="done")
    return f"subscription.set {rid} done"


def _run_query_request(store: Store, hub: Hub, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    inner = row.get("payload")
    if not isinstance(inner, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"query.request {rid} error"
    match = inner.get("match")
    if not isinstance(match, dict):
        _mark(store, row, status="error", error="payload.match must be an object")
        return f"query.request {rid} error"
    try:
        result = hub.query(match)
    except HubError as exc:
        _mark(store, row, status="error", error=_error_text(exc))
        return f"query.request {rid} error"
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        _mark(store, row, status="error", error="query response missing rows")
        return f"query.request {rid} error"
    rows = result["rows"]
    result_id = str(uuid.uuid4())
    session_id = row.get("session_id")
    try:
        store.write(
            "activity",
            "insert",
            result_id,
            {
                "id": result_id,
                "session_id": session_id,
                "type": "query.result",
                "payload": {"request_id": rid, "rows": rows},
                "execution_status": "done",
            },
        )
    except Exception as exc:
        _mark(store, row, status="error", error=_error_text(exc))
        return f"query.request {rid} error"
    _mark(store, row, status="done")
    return f"query.request {rid} done result={result_id}"


def scan_pending(store: Store, hub: Hub) -> list[str]:
    """Run executors for subscription.set and query.request. Other types stay pending."""
    lines: list[str] = []
    for row in store.pending_work():
        typ = row.get("type")
        if typ == "subscription.set":
            lines.append(_run_subscription_set(store, hub, row))
        elif typ == "query.request":
            lines.append(_run_query_request(store, hub, row))
    return lines
