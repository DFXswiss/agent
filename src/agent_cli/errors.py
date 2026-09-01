"""Scan a configured log source and record error.seen rows."""

from __future__ import annotations

import hashlib
import json
import netrc
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .skills import has_skill
from .store import Store, StoreError, utcnow

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_AUTHORIZATION = re.compile(r"(?i)(authorization:\s*).+")
_COOKIE = re.compile(r"(?i)((?:set-)?cookie:\s*).+")
_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")
_JSON_SECRET = re.compile(
    r'(?i)("[^"]*(?:password|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret|authorization|passwd|access_key)[^"]*"\s*:\s*")[^"]*(")'
)
_HEX = re.compile(r"\b[a-fA-F0-9]{20,}\b")
_OTEL_TRACE_ID = re.compile(r"\btrace_id=[0-9a-fA-F]{32}\b")
_OTEL_SPAN_ID = re.compile(r"\bspan_id=[0-9a-fA-F]{16}\b")
_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_-]*(?:password|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret|authorization|passwd|access_key)[A-Za-z0-9_-]*\s*[:=]\s*\S+"
)
_AKIA = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CLASS = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ACCESS_LOG = re.compile(
    r"^(?:OPTIONS|GET|POST|PUT|PATCH|DELETE|HEAD|TRACE|CONNECT)\s+\S+\s+\d{3}\b"
)
_ERROR_LEVEL = re.compile(r"\b(?:ERROR|FATAL|PANIC|CRITICAL)\b")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_CREDENTIAL_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "user",
        "username",
        "apikey",
        "accesstoken",
        "accesskey",
        "authorization",
        "passwd",
        "clientsecret",
    }
)
_CREDENTIAL_TOKENS = frozenset(
    {
        "password",
        "token",
        "secret",
        "apikey",
        "accesstoken",
        "accesskey",
        "authorization",
        "passwd",
        "clientsecret",
    }
)


def _norm_cred(name: object) -> str:
    return "".join(str(name).lower().split()).replace("-", "").replace("_", "")


def _is_cred_name(name: object) -> bool:
    normalized = _norm_cred(name)
    if normalized in _CREDENTIAL_KEYS:
        return True
    return any(token in normalized for token in _CREDENTIAL_TOKENS)


Fetch = Callable[[dict[str, Any], str | None], tuple[list[dict[str, Any]], str | None]]


def config_path(home: Path) -> Path:
    return Path(home) / "error-fix.json"


def cursor_path(home: Path) -> Path:
    return Path(home) / "error-fix.cursor"


def _contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_cred_name(key) or _contains_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    return False


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
    if _contains_credentials(data):
        raise StoreError("error-fix.json must not contain credentials")
    sid = data.get("session_id")
    if not isinstance(sid, str) or sid == "":
        raise StoreError("error-fix.json session_id is missing")
    url = data.get("url")
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise StoreError("error-fix.json url must not contain credentials")
        params = (
            *parse_qs(parsed.query, keep_blank_values=True),
            *parse_qs(parsed.fragment, keep_blank_values=True),
        )
        if any(_is_cred_name(name) for name in params):
            raise StoreError("error-fix.json url must not contain credentials")
    for key in ("line_must_match", "line_must_not_match"):
        if key not in data:
            continue
        value = data.get(key)
        if not isinstance(value, str) or value == "":
            raise StoreError(f"error-fix.json {key} is invalid")
        try:
            re.compile(value)
        except re.error as exc:
            raise StoreError(f"error-fix.json {key} is invalid") from exc
    return data


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def is_incident_line(line: str, cfg: dict[str, Any] | None = None) -> bool:
    plain = strip_ansi(line)
    if isinstance(cfg, dict):
        must_not = cfg.get("line_must_not_match")
        if isinstance(must_not, str) and must_not != "":
            if re.search(must_not, plain):
                return False
        must = cfg.get("line_must_match")
        if isinstance(must, str) and must != "":
            if re.search(must, plain) is None:
                return False
    if _ACCESS_LOG.search(plain.lstrip()):
        return False
    if _ERROR_LEVEL.search(plain):
        return True
    if error_class(plain) != "error":
        return True
    return False


def redact(text: str) -> str:
    out = _AUTHORIZATION.sub(r"\1[redacted]", text)
    out = _BEARER.sub("[redacted]", out)
    out = _COOKIE.sub(r"\1[redacted]", out)
    out = _USERINFO.sub(r"\1[redacted]@", out)
    out = _JSON_SECRET.sub(r"\1[redacted]\2", out)
    out = _SECRET.sub("[redacted]", out)
    out = _AKIA.sub("[redacted]", out)
    out = _JWT.sub("[redacted]", out)
    out = _EMAIL.sub("[redacted]", out)
    out = _OTEL_TRACE_ID.sub("trace_id=[redacted]", out)
    out = _OTEL_SPAN_ID.sub("span_id=[redacted]", out)
    out = _HEX.sub("[redacted]", out)
    return out


def fingerprint(*, service: str, error_class: str, stack_sig: str, environment: str) -> str:
    return f"{service}|{error_class}|{stack_sig}|{environment}"


def line_fingerprint(*, server: str, container: str, line: str) -> str:
    """sha256(server + newline + container + newline + exact line)."""
    return hashlib.sha256(f"{server}\n{container}\n{line}".encode("utf-8")).hexdigest()


def error_class(line: str) -> str:
    match = _CLASS.search(strip_ansi(line))
    if match is None:
        return "error"
    return match.group(1)


def stack_sig(line: str) -> str:
    norm = redact(strip_ansi(line))
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
    matches: list[dict[str, Any]] = []
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "error.seen":
            continue
        if row.get("session_id") != session_id:
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and inner.get("fingerprint") == fp:
            matches.append(row)
    if not matches:
        return None
    open_rows = [
        row for row in matches if not incident_closed(store, session_id, str(row.get("id") or ""))
    ]

    def rank(row: dict[str, Any]) -> tuple[str, str, str]:
        inner = row.get("payload")
        payload = inner if isinstance(inner, dict) else {}
        return (
            str(payload.get("last_seen") or ""),
            str(payload.get("first_seen") or ""),
            str(row.get("id") or ""),
        )

    return max(open_rows or matches, key=rank)


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
    user_set = isinstance(user, str) and user != ""
    pass_set = isinstance(password, str) and password != ""
    if user_set != pass_set:
        raise StoreError("error-fix env auth is incomplete")
    request_kwargs: dict[str, Any] = {}
    if user_set and pass_set:
        request_kwargs["auth"] = (user, password)
    else:
        try:
            request_kwargs["auth"] = httpx.NetRCAuth()
        except (OSError, netrc.NetrcParseError) as exc:
            raise StoreError("error-fix netrc is missing or invalid") from exc
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    try:
        with httpx.Client(trust_env=True, timeout=30.0) as client:
            response = client.get(url, params=params, **request_kwargs)
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
        server = None
        container = None
        if isinstance(labels, dict):
            raw_svc = labels.get("service") or labels.get("compose_service")
            if isinstance(raw_svc, str) and raw_svc:
                service = raw_svc
            raw_env = labels.get("environment")
            if isinstance(raw_env, str) and raw_env:
                environment = raw_env
            raw_server = labels.get("server")
            if isinstance(raw_server, str) and raw_server:
                server = raw_server
            raw_container = labels.get("container_name")
            if isinstance(raw_container, str) and raw_container:
                container = raw_container
        for pair in values:
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            ts_raw, line = pair[0], pair[1]
            if not isinstance(line, str) or line == "":
                continue
            ts_iso: str | None = None
            ts_ns: int | None = None
            if isinstance(ts_raw, str) and ts_raw.isdigit():
                try:
                    ts_ns = int(ts_raw)
                    ts_iso = _ns_to_iso(ts_ns)
                except (OverflowError, OSError, ValueError):
                    continue
            elif isinstance(ts_raw, int) and not isinstance(ts_raw, bool):
                try:
                    ts_ns = ts_raw
                    ts_iso = _ns_to_iso(ts_raw)
                except (OverflowError, OSError, ValueError):
                    continue
            if ts_ns is None or ts_iso is None:
                continue
            item: dict[str, Any] = {"ts": ts_iso, "line": line, "_ts_ns": ts_ns}
            if service:
                item["service"] = service
            if environment:
                item["environment"] = environment
            if server:
                item["server"] = server
            if container:
                item["container"] = container
            lines.append(item)
            if latest_ns is None or ts_ns > latest_ns:
                latest_ns = ts_ns
    lines.sort(key=lambda row: int(row["_ts_ns"]) if isinstance(row.get("_ts_ns"), int) else 0)
    for row in lines:
        row.pop("_ts_ns", None)
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
    with store.exclusive("error-fix-scan:" + store.device_id()):
        cursor = None
        try:
            raw = cursor_file.read_text(encoding="utf-8").strip()
            cursor = raw if raw else None
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError("error-fix.cursor is not readable") from exc
        lines, new_cursor = fetch(cfg, cursor)
        lines = [item for item in lines if isinstance(item, dict)]
        lines.sort(key=lambda row: str(row.get("ts") or ""))
        return _apply_lines(store, cfg, session_id, lines, new_cursor, cursor_file)


def _apply_lines(
    store: Store,
    cfg: dict[str, Any],
    session_id: str,
    lines: list[dict[str, Any]],
    new_cursor: str | None,
    cursor_file: Path,
) -> tuple[list[str], list[str]]:
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
        if not is_incident_line(line, cfg):
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
        redacted = redact(strip_ansi(line))
        excerpt = redacted[:500]
        cls = error_class(redacted)
        fp = fingerprint(
            service=service,
            error_class=cls,
            stack_sig=stack_sig(redacted),
            environment=environment,
        )
        server = item.get("server")
        container = item.get("container")
        line_fp = None
        if isinstance(server, str) and server and isinstance(container, str) and container:
            try:
                line_fp = line_fingerprint(server=server, container=container, line=line)
            except UnicodeEncodeError:
                line_fp = None
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
            if line_fp is not None:
                payload_obj["line_fingerprint"] = line_fp
            else:
                payload_obj.pop("line_fingerprint", None)
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
        if line_fp is not None:
            payload_obj["line_fingerprint"] = line_fp
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
        try:
            cursor_file.write_text(new_cursor + "\n", encoding="utf-8")
            os.chmod(cursor_file, 0o600)
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError("error-fix.cursor is not writable") from exc
    return created, enriched
