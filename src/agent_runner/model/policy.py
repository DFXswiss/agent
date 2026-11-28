"""What the hub says about the model fallback, from the last desired state (`GET /v1/device/state`; KOR-50, ADR-081
sections 1, 5 and 6).

- `api(desired)`: the hub accepts the new items and fields of ADR-081 (`model_fallback.api >= 1`): the outbox item
  `operator_quota`, `limited` of `quota`, `wait_until`/`dfx_choice` of `session_transition`, the session failure reason
  `model_quota_exhausted` and the audit action `session.model_switched`. An older hub has no `model_fallback`; the
  runner then sends none of them (the hub would refuse them). `renewed_api(desired)` (`api >= 2`, HOT-26): the hub
  also accepts the causes `renewed` (a refused access continued) and `model_unavailable` (a session waits for a
  subscription with access to its model) of `session.model_switched`.
- `settings(desired, session_id)`: the owner's fallback settings (`fallback.own_to_dfx`, default off;
  `fallback.operator_to_platform`, default on) and the task's consent (`model_consent`, default false) delivered with
  the session's entry of `sessions`. A missing or malformed value means the default (an older hub delivers none). A
  session the hub does not list yet - a subagent starts right after its `subagent.request`, before the next desired
  state names it - takes them from the entry of its parent, else of another session of the same task and owner
  (`kin`): the values are the owner's and the task's, the same for each of their sessions (HOT-31).
- `preference(desired, ...)`: the owner's access preference from such an entry, for the same sessions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.sessions.desired import PREFERENCES


@dataclass(frozen=True)
class Settings:
    own_to_dfx: bool = False
    operator_to_platform: bool = True
    consent: bool = False

    @property
    def crossing(self) -> bool:
        """A step from the owner's own access to a `dfx` access is allowed (the setting or the task's consent)."""
        return self.own_to_dfx or self.consent


RENEWED_API = 2
"""`model_fallback.api` of a hub that also accepts the cause `renewed` of `session.model_switched` (HOT-26)."""


def api(desired: Mapping[str, Any] | None, version: int = 1) -> bool:
    found = None if desired is None else desired.get("model_fallback")
    value = found.get("api") if isinstance(found, Mapping) else None
    return isinstance(value, int) and not isinstance(value, bool) and value >= version


def renewed_api(desired: Mapping[str, Any] | None) -> bool:
    """The hub accepts the cause `renewed` of `session.model_switched` (`model_fallback.api >= 2`, HOT-26)."""
    return api(desired, RENEWED_API)


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


def kin(
    desired: Mapping[str, Any] | None, *, task_id: str, owner_id: str, parent_id: str | None = None
) -> Mapping[str, Any] | None:
    """An entry of `sessions` of the same task and owner - the parent's first -, None when there is none. It stands in
    for a session the hub does not list yet: a subagent is started at once after its `subagent.request`, before the
    next desired state names it. The owner's settings (`fallback`, `model_access_pref`) are the same for every session
    of the owner and the consent for every session of the task, so the entry carries the values of that session."""
    listed = None if desired is None else desired.get("sessions")
    same = [
        item
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, Mapping) and item.get("task_id") == task_id and item.get("owner_id") == owner_id
    ]
    for item in same:
        if parent_id is not None and item.get("session_id") == parent_id:
            return item
    return same[0] if same else None


def preference(
    desired: Mapping[str, Any] | None, *, task_id: str, owner_id: str, parent_id: str | None = None
) -> str | None:
    """The owner's access preference from an entry of the same task and owner (`kin`), None without a valid one."""
    found = kin(desired, task_id=task_id, owner_id=owner_id, parent_id=parent_id)
    value = None if found is None else found.get("model_access_pref")
    return value if isinstance(value, str) and value in PREFERENCES else None


def settings(
    desired: Mapping[str, Any] | None,
    session_id: str,
    *,
    task_id: str | None = None,
    owner_id: str | None = None,
    parent_id: str | None = None,
) -> Settings:
    """The settings of the session's entry; for a session that is not listed (yet) those of an entry of the same task
    and owner (`kin`), so a subagent or a resumed session follows the same crossing rules as its main session."""
    found = entry(desired, session_id)
    if found is None and task_id is not None and owner_id is not None:
        found = kin(desired, task_id=task_id, owner_id=owner_id, parent_id=parent_id)
    found = found or {}
    fallback = found.get("fallback")
    owner = fallback if isinstance(fallback, Mapping) else {}
    return Settings(
        own_to_dfx=_flag(owner, "own_to_dfx", False),
        operator_to_platform=_flag(owner, "operator_to_platform", True),
        consent=_flag(found, "model_consent", False),
    )
