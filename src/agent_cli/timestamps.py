"""Shared parsing for aware ISO-8601 timestamps normalized to UTC."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc_timestamp(value: object) -> datetime | None:
    """Parse an aware ISO-8601 timestamp and normalize its instant to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
