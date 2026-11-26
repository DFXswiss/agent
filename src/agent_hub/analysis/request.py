"""The analysis of `POST /v1/tasks` (KOR-53, ADR-097 sections 1 and 2): a task of the kind "analysis".

- `parse(value, now)`: the request's `analysis {scope: "org"|"wg", since?, until?}` checked against the hub's time:
  `until` (default now) at most `FUTURE_SLACK` after now, `since` (default `DEFAULT_DAYS` before `until`) before
  `until` and at most `MAX_DAYS` before it; anything else is `400 request_invalid` with the pointer.
- `Analysis.stored()`: the plaintext record kept as `hub.task.analysis` (`{scope, since, until}`, times in the API form;
  a repetition of the request compares the fields it names with it, before any check against the time).
- `Analysis.profile()`: the snapshot of the built-in profile `analysis` the task gets in its creating transaction:
  reach `all` for scope `org`, `workgroup` for scope `wg` (only the task's workgroup).
- `require_features(connection, org_id, root_key)`: every active device assigned to a workgroup of the organization has
  announced `agent.profiles` and `platform.analysis`, else `409 peer_outdated` with the outdated devices.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agent_hub.credentials.eligibility import org_device_ids
from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.features import AGENT_PROFILES, PLATFORM_ANALYSIS, outdated_devices
from agent_hub.errors import HubError
from agent_hub.profiles import model

SCOPES = ("org", "wg")
DEFAULT_DAYS = 7
MAX_DAYS = 92
FUTURE_SLACK = timedelta(minutes=1)
MAX_DETAIL_DEVICES = 20
FIELDS = frozenset({"scope", "since", "until"})


@dataclass(frozen=True)
class Analysis:
    """A checked analysis request: its scope and period."""

    scope: str
    since: datetime
    until: datetime

    def stored(self) -> dict[str, str]:
        return {"scope": self.scope, "since": format_time(self.since), "until": format_time(self.until)}

    def profile(self) -> model.Profile:
        return model.analysis(model.REACH_ALL if self.scope == "org" else model.REACH_WORKGROUP)


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/analysis/{name}"]})


def _time(value: Mapping[str, Any], name: str) -> datetime | None:
    if name not in value:
        return None
    try:
        return parse_time(value[name])
    except CryptoError:
        raise _invalid(name) from None


def parse(value: Mapping[str, Any], now: datetime) -> Analysis:
    """The checked analysis of a request (module docstring)."""
    unknown = sorted(set(value) - FIELDS)
    if unknown:
        raise _invalid(unknown[0])
    scope = value.get("scope")
    if scope not in SCOPES:
        raise _invalid("scope")
    until = _time(value, "until") or now
    if until > now + FUTURE_SLACK:
        raise _invalid("until")
    since = _time(value, "since") or until - timedelta(days=DEFAULT_DAYS)
    if since >= until or until - since > timedelta(days=MAX_DAYS):
        raise _invalid("since")
    return Analysis(scope=str(scope), since=since, until=until)


def require_features(connection: Any, org_id: UUID, root_key: bytes) -> None:
    """The feature gate of an analysis task (module docstring)."""
    devices = [str(device) for device in org_device_ids(connection, org_id, root_key)]
    outdated = sorted(
        set(outdated_devices(connection, devices, AGENT_PROFILES))
        | set(outdated_devices(connection, devices, PLATFORM_ANALYSIS))
    )
    if outdated:
        raise HubError("peer_outdated", detail={"devices": outdated[:MAX_DETAIL_DEVICES]})
