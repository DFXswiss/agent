"""Usage parsers of the quota polling (05-runner.md sections 1.5 and 10.6, 00-overview.md section 5).

Copied and adapted from the legacy `agent_cli.usage` (origin: `src/agent_cli/usage.py`, functions
`windows_snapshot`, `spend_snapshot` and their helpers `_resolve`, `_finite`, `_window_reset`, `_usage_window`). The
legacy functions read field names from `drivers.json` adapter specs and return legacy store payloads; the copy is
bound to the two documents the runner polls and returns the shapes the runner needs:

- `windows_snapshot(document)`: the Anthropic OAuth usage document (`GET <ANTHROPIC_USAGE_URL>`) -> the `windows` of
  the outbox item `quota` (03-api.md section 6.1). Window mapping of 00-overview.md section 5: `five_hour` ->
  `five_hour`; the weekly window (`seven_day`, or the first all-models limit `weekly_all` when `seven_day` has no
  value) -> `seven_day`; every per-model weekly limit (`weekly_scoped`, model named by `scope.model.display_name`
  or `.id`) -> `weekly:<model>` (characters outside `[A-Za-z0-9._-]` become `_`, at most 64; the first limit of a
  model wins). `used_percent` from `utilization` (windows) or `percent` (limits), `resets_at` rounded to the
  nearest minute (the endpoint reports about a second of jitter; legacy rule) in the contract time form, `severity`
  of the entry when it is a short lower-case word. A window without a value is left out, never shown as 0 %; the
  list keeps at most 16 windows (contract maximum).
- `spend_snapshot(document)`: the OpenRouter key document (`GET <openrouter>/api/v1/key`, `{"data": {...}}`) -> the
  spend of the platform key in USD: `total`, `daily`, `weekly`, `monthly` (required finite numbers) and `limit`,
  `limit_remaining` (null when the key has no limit; an absent field reads as null), `limit_reset` (any scalar).

A field that is missing or of the wrong type raises `UsageInvalid(field)`; nothing is coerced. No value is logged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_WINDOWS = 16
MAX_PERCENT = 1000.0
WEEKLY_ALL = "weekly_all"
WEEKLY_MODEL = "weekly_scoped"
MODEL_NAMES = ("display_name", "id")
SPEND_FIELDS = {"total": "usage", "daily": "usage_daily", "weekly": "usage_weekly", "monthly": "usage_monthly"}
NULLABLE_SPEND = {"limit": "limit", "limit_remaining": "limit_remaining"}
_MISSING = object()
_SEVERITY_RE = re.compile(r"^[a-z_]{1,32}$")
_MODEL_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")


class UsageInvalid(ValueError):
    """A usage document that does not have the expected form (`field` names the place)."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _finite(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageInvalid(field)
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise UsageInvalid(field)
    return float(value)


def _percent(field: str, value: Any) -> float:
    used = _finite(field, value)
    if not 0 <= used <= MAX_PERCENT:
        raise UsageInvalid(field)
    return used


def _window_reset(value: Any, field: str) -> str | None:
    """Reset time rounded to the nearest minute (30 seconds and more round up) in UTC, or None."""
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise UsageInvalid(field)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise UsageInvalid(field) from None
    if when.tzinfo is None:
        raise UsageInvalid(field)
    try:
        when = when.astimezone(UTC)
        minute = when.replace(second=0, microsecond=0)
        if when - minute >= timedelta(seconds=30):
            minute += timedelta(minutes=1)
    except OverflowError:
        raise UsageInvalid(field) from None
    return minute.strftime("%Y-%m-%dT%H:%M:00.000Z")


def _severity(entry: Mapping[str, Any]) -> str | None:
    value = entry.get("severity")
    return value if isinstance(value, str) and _SEVERITY_RE.fullmatch(value) else None


def _usage_window(raw: Any, field: str) -> dict[str, Any] | None:
    """A reported window object, or None when the document has no value for it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UsageInvalid(field)
    used = raw.get("utilization")
    if used is None:
        return None
    return {
        "used_percent": _percent(f"{field}.utilization", used),
        "resets_at": _window_reset(raw.get("resets_at"), f"{field}.resets_at"),
        "severity": _severity(raw),
    }


def _model_name(item: Mapping[str, Any]) -> str | None:
    scope = item.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    if not isinstance(model, dict):
        return None
    for key in MODEL_NAMES:
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            return _MODEL_CHAR_RE.sub("_", value.strip())[:64]
    return None


def windows_snapshot(document: Any) -> list[dict[str, Any]]:
    """The quota windows of an OAuth usage document (module docstring)."""
    if not isinstance(document, dict):
        raise UsageInvalid("document")
    limits = document.get("limits")
    if limits is None:
        limits = []
    if not isinstance(limits, list):
        raise UsageInvalid("limits")
    weekly_all: dict[str, Any] | None = None
    per_model: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(limits):
        field = f"limits[{index}]"
        if not isinstance(item, dict):
            raise UsageInvalid(field)
        kind = item.get("kind")
        if kind not in (WEEKLY_ALL, WEEKLY_MODEL) or item.get("percent") is None:
            continue
        window = {
            "used_percent": _percent(f"{field}.percent", item.get("percent")),
            "resets_at": _window_reset(item.get("resets_at"), f"{field}.resets_at"),
            "severity": _severity(item),
        }
        if kind == WEEKLY_ALL:
            weekly_all = weekly_all or window
            continue
        name = _model_name(item)
        if name is not None:
            per_model.setdefault(f"weekly:{name}", window)
    five_hour = _usage_window(document.get("five_hour"), "five_hour")
    seven_day = _usage_window(document.get("seven_day"), "seven_day") or weekly_all
    found: list[dict[str, Any]] = []
    named: list[tuple[str, dict[str, Any] | None]] = [("five_hour", five_hour), ("seven_day", seven_day)]
    for label, entry in [*named, *per_model.items()]:
        if entry is not None:
            found.append({"window": label, **entry})
    return found[:MAX_WINDOWS]


def _resolve(root: Any, field: str) -> Any:
    return root.get(field, _MISSING) if isinstance(root, dict) else _MISSING


def spend_snapshot(document: Any) -> dict[str, Any]:
    """The spend of the platform key from an OpenRouter key document (module docstring)."""
    data = _resolve(document, "data")
    if not isinstance(data, dict):
        raise UsageInvalid("data")
    snapshot: dict[str, Any] = {}
    for name, field in SPEND_FIELDS.items():
        value = _resolve(data, field)
        if value is _MISSING:
            raise UsageInvalid(field)
        snapshot[name] = _finite(field, value)
    for name, field in NULLABLE_SPEND.items():
        value = data.get(field)
        snapshot[name] = None if value is None else _finite(field, value)
    reset = data.get("limit_reset")
    if isinstance(reset, (dict, list, bool)):
        raise UsageInvalid("limit_reset")
    snapshot["limit_reset"] = reset
    return snapshot
