"""Scan a configured log source and record error.seen rows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .skills import has_skill
from .store import Store, StoreError, utcnow

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_HEX = re.compile(r"\b[a-fA-F0-9]{20,}\b")
_SECRET = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*\S+")
_CLASS = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")

Fetch = Callable[[dict[str, Any], str | None], tuple[list[dict[str, Any]], str | None]]


def config_path(home: Path) -> Path:
    return Path(home) / "error-fix.json"


def cursor_path(home: Path) -> Path:
    return Path(home) / "error-fix.cursor"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise StoreError(f"error-fix.json not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("error-fix.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise StoreError("error-fix.json is not an object")
    sid = data.get("session_id")
    if not isinstance(sid, str) or sid == "":
        raise StoreError("error-fix.json session_id is missing")
    return data


def redact(text: str) -> str:
    out = _BEARER.sub("[redacted]", text)
    out = _SECRET.sub("[redacted]", out)
    out = _EMAIL.sub("[redacted]", out)
    out = _HEX.sub("[redacted]", out)
    return out


def fingerprint(*, service: str, error_class: str, stack_sig: str, environment: str) -> str:
    return f"{service}|{error_class}|{stack_sig}|{environment}"


def error_class(line: str) -> str:
    match = _CLASS.search(line)
    if match is None:
        return "error"
    return match.group(1)


def stack_sig(line: str) -> str:
    norm = redact(line)
    norm = _UUID.sub("", norm)
    norm = _DIGITS.sub("", norm)
    norm = _SPACE.sub(" ", norm).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def incident_closed(store: Store, session_id: str, error_id: str) -> bool:
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "error.skip":
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and inner.get("error_id") == error_id:
            return True
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("session_id") != session_id:
            continue
        inner = row.get("payload")
        if not isinstance(inner, dict) or inner.get("error_id") != error_id:
            continue
        if row.get("state") in ("done", "failed"):
            return True
    return False


def _latest_seen(store: Store, session_id: str, fp: str) -> dict[str, Any] | None:
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "error.seen":
            continue
        if row.get("session_id") != session_id:
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and inner.get("fingerprint") == fp:
            return row
    return None


def _iso_to_ns(iso: str) -> int:
    stamp = iso[:-1] if iso.endswith("Z") else iso
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _ns_to_iso(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_fetch(cfg: dict[str, Any], cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    url = cfg.get("url")
    if not isinstance(url, str) or url == "":
        raise StoreError("error-fix.json url is missing")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname in (None, ""):
        raise StoreError("error-fix.json url is invalid")
    query = cfg.get("query")
    if not isinstance(query, str) or query == "":
        raise StoreError("error-fix.json query is missing")
    limit = cfg.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = 100
    now = datetime.now(timezone.utc)
    end_ns = int(now.timestamp() * 1_000_000_000)
    if isinstance(cursor, str) and cursor.isdigit():
        start_ns = int(cursor) + 1
    elif isinstance(cursor, str) and cursor:
        try:
            start_ns = _iso_to_ns(cursor) + 1
        except ValueError as exc:
            raise StoreError("error-fix.cursor is not a timestamp") from exc
    else:
        start_ns = int((now - timedelta(hours=1)).timestamp() * 1_000_000_000)
    user = os.environ.get("AGENT_ERROR_FIX_USER")
    password = os.environ.get("AGENT_ERROR_FIX_PASSWORD")
    request_auth: Any = httpx.NetRCAuth()
    if isinstance(user, str) and user != "" and isinstance(password, str) and password != "":
        request_auth = (user, password)
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    try:
        with httpx.Client(trust_env=True, timeout=30.0) as client:
            response = client.get(url, params=params, auth=request_auth)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise StoreError("error-fix fetch failed") from exc
    except json.JSONDecodeError as exc:
        raise StoreError("error-fix fetch returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise StoreError("error-fix fetch returned invalid JSON")
    result = data.get("data")
    streams: list[Any] = []
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        streams = result["result"]
    lines: list[dict[str, Any]] = []
    latest_ns: int | None = start_ns - 1 if start_ns > 0 else None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream")
        values = stream.get("values")
        if not isinstance(values, list):
            continue
        service = None
        environment = None
        if isinstance(labels, dict):
            raw_svc = labels.get("service") or labels.get("compose_service")
            if isinstance(raw_svc, str) and raw_svc:
                service = raw_svc
            raw_env = labels.get("environment")
            if isinstance(raw_env, str) and raw_env:
                environment = raw_env
        for pair in values:
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            ts_raw, line = pair[0], pair[1]
            if not isinstance(line, str) or line == "":
                continue
            ts_iso = utcnow()
            ts_ns: int | None = None
            if isinstance(ts_raw, str) and ts_raw.isdigit():
                try:
                    ts_ns = int(ts_raw)
                    ts_iso = _ns_to_iso(ts_ns)
                except (OverflowError, OSError, ValueError):
                    ts_iso = utcnow()
            elif isinstance(ts_raw, int) and not isinstance(ts_raw, bool):
                try:
                    ts_ns = ts_raw
                    ts_iso = _ns_to_iso(ts_raw)
                except (OverflowError, OSError, ValueError):
                    ts_iso = utcnow()
            item: dict[str, Any] = {"ts": ts_iso, "line": line}
            if service:
                item["service"] = service
            if environment:
                item["environment"] = environment
            lines.append(item)
            if ts_ns is not None and (latest_ns is None or ts_ns > latest_ns):
                latest_ns = ts_ns
    lines.sort(key=lambda row: str(row.get("ts") or ""))
    new_cursor = str(latest_ns) if latest_ns is not None else cursor
    return lines, new_cursor


def scan_errors(store: Store, fetch: Fetch) -> tuple[list[str], list[str]]:
    cfg = load_config(config_path(store.home))
    session_id = cfg["session_id"]
    session = store.row("session", session_id)
    if session is None:
        raise StoreError(f"session {session_id} does not exist")
    if session.get("_origin_device_id") != store.device_id():
        raise StoreError(f"session {session_id} is not owned by this device")
    if not has_skill(session, "error-fix"):
        raise StoreError(f"session {session_id} does not have skill error-fix")
    cursor_file = cursor_path(store.home)
    cursor = None
    if cursor_file.is_file():
        raw = cursor_file.read_text(encoding="utf-8").strip()
        cursor = raw if raw else None
    lines, new_cursor = fetch(cfg, cursor)
    lines = [item for item in lines if isinstance(item, dict)]
    lines.sort(key=lambda row: str(row.get("ts") or ""))
    created: list[str] = []
    enriched: list[str] = []
    default_service = cfg.get("service")
    default_env = cfg.get("environment")
    default_repo = cfg.get("repo")
    for item in lines:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        if not isinstance(line, str) or line == "":
            continue
        ts = item.get("ts")
        if not isinstance(ts, str) or ts == "":
            ts = utcnow()
        service = item.get("service")
        if not isinstance(service, str) or service == "":
            service = default_service if isinstance(default_service, str) and default_service else "unknown"
        environment = item.get("environment")
        if not isinstance(environment, str) or environment == "":
            environment = default_env if isinstance(default_env, str) and default_env else "unknown"
        repo = item.get("repo")
        if not isinstance(repo, str) or repo == "":
            repo = default_repo if isinstance(default_repo, str) and default_repo else None
        excerpt = redact(line)[:500]
        cls = error_class(excerpt)
        fp = fingerprint(
            service=service,
            error_class=cls,
            stack_sig=stack_sig(excerpt),
            environment=environment,
        )
        existing = _latest_seen(store, session_id, fp)
        if existing is not None and not incident_closed(store, session_id, str(existing.get("id") or "")):
            inner = existing.get("payload")
            payload_obj = dict(inner) if isinstance(inner, dict) else {}
            count = payload_obj.get("count")
            if isinstance(count, bool) or not isinstance(count, int):
                count = 1
            payload_obj["count"] = count + 1
            payload_obj["last_seen"] = ts
            payload_obj["excerpt"] = excerpt
            updated = _strip(existing)
            updated["payload"] = payload_obj
            rid = str(existing.get("id") or "")
            store.write("activity", "update", rid, updated)
            enriched.append(rid)
            continue
        aid = str(uuid.uuid4())
        payload_obj = {
            "fingerprint": fp,
            "service": service,
            "environment": environment,
            "class": cls,
            "count": 1,
            "first_seen": ts,
            "last_seen": ts,
            "excerpt": excerpt,
            "evidence": None,
        }
        if isinstance(repo, str) and repo:
            payload_obj["repo"] = repo
        store.write(
            "activity",
            "insert",
            aid,
            {
                "id": aid,
                "session_id": session_id,
                "type": "error.seen",
                "payload": payload_obj,
                "execution_status": "done",
            },
        )
        created.append(aid)
    if new_cursor is not None:
        cursor_file.write_text(new_cursor + "\n", encoding="utf-8")
        os.chmod(cursor_file, 0o600)
    return created, enriched
