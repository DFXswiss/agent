"""Journal analysis of the model double: what Claude Code sent, summarised per route.

`summarize_endpoints` turns journal records (common format, `agent_doubles.common.journal`) into the shape Spike C
recorded in ``data/double-endpoints.json``: per route the request count, every header name with its values
(volatile values replaced by ``<varies>``), the body keys and selected body fields. Comparing the summary of a run
with the recorded one is the Claude Code upgrade guard of docs/platform/spec/05-runner.md section 15: a new route,
header or body field shows up as a difference.

Pure module: no clock, no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

VOLATILE_HEADERS = frozenset(
    {"content-length", "host", "connection", "x-claude-code-session-id", "x-stainless-retry-count"}
)
FIELDS = (
    "model",
    "max_tokens",
    "stream",
    "thinking",
    "output_config",
    "context_management",
    "tool_choice",
    "temperature",
)
VARIES = "<varies>"


def route_key(record: dict[str, Any]) -> str:
    """``METHOD path[?query]`` of a record."""
    query = record.get("query") or ""
    return f"{record['method']} {record['path']}" + (f"?{query}" if query else "")


def _body_fields(body: dict[str, Any], fields: dict[str, set[str]], body_keys: set[str]) -> None:
    body_keys.update(body)
    for key in FIELDS:
        if key in body:
            fields.setdefault(key, set()).add(json.dumps(body[key], sort_keys=True))
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    roles = sorted({str(m.get("role")) for m in messages})
    fields.setdefault("message_roles", set()).add(json.dumps(roles))
    blocks = {
        str(b.get("type"))
        for m in messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
    }
    fields.setdefault("message_block_types", set()).update(blocks)
    system = body.get("system")
    cached = sum(1 for b in system if isinstance(b, dict) and b.get("cache_control")) if isinstance(system, list) else 0
    fields.setdefault("system_blocks_with_cache_control", set()).add(str(cached))
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
    fields.setdefault("tool_keys", set()).update(k for t in tools for k in t)


def summarize_endpoints(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Routes, header values, body keys and fields of every recorded request (sets as sorted lists)."""
    routes: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = routes.setdefault(route_key(record), {"count": 0, "headers": {}, "body_keys": set(), "fields": {}})
        entry["count"] += 1
        for name, value in record["headers"].items():
            entry["headers"].setdefault(name, set()).add(VARIES if name in VOLATILE_HEADERS else value)
        if isinstance(record.get("body"), dict):
            _body_fields(record["body"], entry["fields"], entry["body_keys"])
    return {
        key: {
            "count": entry["count"],
            "headers": {name: sorted(values) for name, values in entry["headers"].items()},
            "body_keys": sorted(entry["body_keys"]),
            "fields": {name: sorted(values) for name, values in entry["fields"].items()},
        }
        for key, entry in routes.items()
    }


def unknown_names(summary: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    """Routes, header names and body keys of `summary` that `recorded` does not know, as readable texts."""
    out: list[str] = []
    for key, entry in summary.items():
        known = recorded.get(key)
        if known is None:
            out.append(f"route {key}")
            continue
        out.extend(f"header {name} on {key}" for name in entry["headers"] if name not in known["headers"])
        out.extend(f"body key {name} on {key}" for name in entry["body_keys"] if name not in known["body_keys"])
    return out
