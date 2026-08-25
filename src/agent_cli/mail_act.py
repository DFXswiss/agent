"""Execute pending mail.reply / mail.seen and ingest envelopes via himalaya."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from .runtime import Completed
from .store import Store, StoreError

Runner = Callable[[list[str]], Completed]


class _MailError(Exception):
    """Per-row himalaya failure; never escapes scan_mail."""


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _mark(
    store: Store,
    row: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    updated = _strip(row)
    updated["execution_status"] = status
    if error is None:
        updated.pop("execution_error", None)
    else:
        updated["execution_error"] = str(error)[:500]
    if result is not None:
        updated["result"] = result
    store.write("activity", "update", updated["id"], updated)


def _nonempty_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw != "":
        return raw
    return None


def _optional_str_field(
    payload: dict[str, Any], key: str, *, nonempty: bool = False
) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if not isinstance(raw, str):
        raise _MailError(f"{key} must be a string")
    if nonempty and raw == "":
        raise _MailError(f"{key} must be a non-empty string")
    return raw


def _as_id(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str) and raw.isdigit():
        return raw
    return None


def _run_himalaya(argv: list[str], runner: Runner) -> Completed:
    try:
        return runner(argv)
    except OSError as exc:
        raise _MailError(f"himalaya is not available: {exc}") from exc


def _run_mail_reply(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"mail.reply {rid} error"
    body = _nonempty_str(payload.get("body"))
    if body is None:
        _mark(store, row, status="error", error="mail.reply requires body")
        return f"mail.reply {rid} error"
    to = _nonempty_str(payload.get("to"))
    reply_id = _as_id(payload.get("in_reply_to"))
    if "in_reply_to" in payload and payload.get("in_reply_to") is not None and reply_id is None:
        _mark(store, row, status="error", error="in_reply_to must be an id")
        return f"mail.reply {rid} error"
    if reply_id is not None:
        argv = [
            "himalaya",
            "message",
            "reply",
            reply_id,
            "--body",
            body,
            "--send",
        ]
        done_result: dict[str, Any] = {"in_reply_to": payload.get("in_reply_to")}
    else:
        if to is None:
            _mark(store, row, status="error", error="mail.reply requires to, body")
            return f"mail.reply {rid} error"
        try:
            subject = _optional_str_field(payload, "subject", nonempty=True)
        except _MailError as exc:
            _mark(store, row, status="error", error=str(exc))
            return f"mail.reply {rid} error"
        argv = ["himalaya", "message", "compose", "--to", to]
        if subject is not None:
            argv.extend(["--subject", subject])
        argv.extend(["--body", body, "--send"])
        done_result = {"to": to}
    try:
        completed = _run_himalaya(argv, runner)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "himalaya failed").strip()
            _mark(store, row, status="error", error=detail[:500] or "himalaya failed")
            return f"mail.reply {rid} error"
        _mark(store, row, status="done", result=done_result)
        return f"mail.reply {rid} done"
    except _MailError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"mail.reply {rid} error"


def _run_mail_seen(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"mail.seen {rid} error"
    msg_id = _as_id(payload.get("id"))
    if msg_id is None:
        _mark(store, row, status="error", error="mail.seen requires id")
        return f"mail.seen {rid} error"
    try:
        folder = _optional_str_field(payload, "folder", nonempty=True)
    except _MailError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"mail.seen {rid} error"
    if folder is None:
        folder = "Inbox"
    argv = [
        "himalaya",
        "flag",
        "add",
        "-m",
        folder,
        "--flag",
        "seen",
        msg_id,
    ]
    try:
        completed = _run_himalaya(argv, runner)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "himalaya failed").strip()
            _mark(store, row, status="error", error=detail[:500] or "himalaya failed")
            return f"mail.seen {rid} error"
        _mark(store, row, status="done", result={"id": payload.get("id"), "folder": folder})
        return f"mail.seen {rid} done"
    except _MailError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"mail.seen {rid} error"


def scan_mail(store: Store, runner: Runner) -> list[str]:
    """Execute pending mail.reply and mail.seen owned by this device.

    Other pending types (github / query / subscription) are skipped.
    Returns human-readable status lines, one per handled row.
    """
    lines: list[str] = []
    for row in store.pending_work():
        typ = row.get("type")
        try:
            if typ == "mail.reply":
                lines.append(_run_mail_reply(store, runner, row))
            elif typ == "mail.seen":
                lines.append(_run_mail_seen(store, runner, row))
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            rid = str(row.get("id") or "?")
            _mark(store, row, status="error", error=str(exc))
            label = typ if isinstance(typ, str) else "activity"
            lines.append(f"{label} {rid} error")
    return lines


def _mail_session_id(store: Store) -> str:
    meta = store.meta("mail_session")
    if isinstance(meta, str) and meta != "":
        return meta
    origin = store.device_id()
    for row in store.rows("session"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("status") != "active":
            continue
        sid = row.get("id")
        if isinstance(sid, str) and sid != "":
            return sid
    raise StoreError("no session for mail.ingest")


def _known_ingest_ids(store: Store) -> set[Any]:
    origin = store.device_id()
    known: set[Any] = set()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "mail.ingest":
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and "id" in inner:
            known.add(inner["id"])
            raw = inner["id"]
            if isinstance(raw, int):
                known.add(str(raw))
            elif isinstance(raw, str) and raw.isdigit():
                known.add(int(raw))
    return known


def _envelope_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in item:
        return None
    eid = item["id"]
    payload: dict[str, Any] = {"id": eid}
    sender = item.get("from")
    if not isinstance(sender, str) or sender == "":
        sender = item.get("sender")
    if isinstance(sender, str) and sender != "":
        payload["from"] = sender
    subject = item.get("subject")
    if isinstance(subject, str) and subject != "":
        payload["subject"] = subject
    date = item.get("date")
    if isinstance(date, str) and date != "":
        payload["date"] = date
    return payload


def scan_mail_ingest(store: Store, runner: Runner) -> list[str]:
    """List envelopes via himalaya and insert new mail.ingest rows (no knock)."""
    argv = ["himalaya", "--json", "envelope", "list", "--page-size", "30"]
    try:
        completed = runner(argv)
    except OSError:
        return ["mail.ingest error"]
    if completed.returncode != 0:
        return ["mail.ingest error"]
    raw = (completed.stdout or "").strip()
    if raw == "":
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ["mail.ingest error"]
    if not isinstance(data, list):
        return ["mail.ingest error"]
    session_id = _mail_session_id(store)
    known = _known_ingest_ids(store)
    lines: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        payload = _envelope_payload(item)
        if payload is None:
            continue
        eid = payload["id"]
        if eid in known or (isinstance(eid, int) and str(eid) in known):
            continue
        if isinstance(eid, str) and eid.isdigit() and int(eid) in known:
            continue
        activity_id = str(uuid.uuid4())
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": session_id,
                "type": "mail.ingest",
                "payload": payload,
                "execution_status": "done",
            },
        )
        known.add(eid)
        if isinstance(eid, int):
            known.add(str(eid))
        lines.append(f"mail.ingest {activity_id} done")
    return lines
