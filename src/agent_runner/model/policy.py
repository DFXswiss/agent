"""What the hub says about the model fallback, from the last desired state (`GET /v1/device/state`; KOR-50, ADR-081
sections 1, 5 and 6).

- `api(desired)`: the hub accepts the new items and fields of ADR-081 (`model_fallback.api >= 1`): the outbox item
  `operator_quota`, `limited` of `quota`, `wait_until`/`dfx_choice` of `session_transition`, the session failure reason
  `model_quota_exhausted` and the audit action `session.model_switched`. An older hub has no `model_fallback`; the
  runner then sends none of them (the hub would refuse them).
- `settings(desired, session_id)`: the owner's fallback settings (`fallback.own_to_dfx`, default off;
  `fallback.operator_to_platform`, default on) and the task's consent (`model_consent`, default false) delivered with
  the session's entry of `sessions`. A missing or malformed value means the default (an older hub delivers none).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settings:
    own_to_dfx: bool = False
    operator_to_platform: bool = True
    consent: bool = False

    @property
    def crossing(self) -> bool:
        """A step from the owner's own access to a `dfx` access is allowed (the setting or the task's consent)."""
        return self.own_to_dfx or self.consent


def api(desired: Mapping[str, Any] | None) -> bool:
    found = None if desired is None else desired.get("model_fallback")
    value = found.get("api") if isinstance(found, Mapping) else None
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def entry(desired: Mapping[str, Any] | None, session_id: str) -> Mapping[str, Any] | None:
    """The session's entry of `sessions` in the desired state, None when it is not listed."""
    listed = None if desired is None else desired.get("sessions")
    for item in listed if isinstance(listed, list) else []:
        if isinstance(item, Mapping) and item.get("session_id") == session_id:
            return item
    return None


def _flag(source: Mapping[str, Any], name: str, default: bool) -> bool:
    value = source.get(name)
    return value if isinstance(value, bool) else default


def settings(desired: Mapping[str, Any] | None, session_id: str) -> Settings:
    found = entry(desired, session_id) or {}
    fallback = found.get("fallback")
    owner = fallback if isinstance(fallback, Mapping) else {}
    return Settings(
        own_to_dfx=_flag(owner, "own_to_dfx", False),
        operator_to_platform=_flag(owner, "operator_to_platform", True),
        consent=_flag(found, "model_consent", False),
    )
