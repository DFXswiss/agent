"""Extension points of the sync area (ADR-027 section 4; decisions/RUN-02.md).

Two points are added next to the ones of `agent_runner.registry`, discovered the same way (`registry.discover`, areas
in sorted order), so later packages add files and never edit a list here:

- `<area>/connect.py:HOOKS`: sequence of `ConnectHook`; after every WebSocket (re)connect the `reconcile` hooks run,
  then the outbox is pushed, then the `pull` hooks run (03-api.md section 6.2).
- `<area>/heartbeat.py:SECTIONS`: sequence of `HeartbeatSection`; each provides one field of the heartbeat frame
  (`capacity`, `sessions` or `vms`).

A heartbeat field without a provider is sent empty (`sessions`/`vms` `[]`, `capacity` all zero), so the device is
still online before VM-02 and RUN-04 register theirs.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runner import registry
from agent_runner.errors import RunnerError

PHASES = ("reconcile", "pull")
HEARTBEAT_FIELDS = ("capacity", "sessions", "vms")
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")


@dataclass(frozen=True)
class ConnectHook:
    phase: str
    name: str
    run: Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class HeartbeatSection:
    field: str
    provide: Callable[[Any], Awaitable[object]]


def _items[T](module: str, attribute: str, kind: type[T], package: str) -> list[tuple[str, T]]:
    found: list[tuple[str, T]] = []
    for area, value in registry.discover(module, attribute, package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.{module}")
        for item in value:
            if not isinstance(item, kind):
                raise RunnerError("registry_invalid", f"{area}.{module}")
            found.append((area, item))
    return found


def connect_hooks(package: str = registry.ROOT_PACKAGE) -> dict[str, list[ConnectHook]]:
    """The connect hooks per phase, in area order then name order; names are unique."""
    phases: dict[str, list[ConnectHook]] = {phase: [] for phase in PHASES}
    seen: set[str] = set()
    for area, hook in _items("connect", "HOOKS", ConnectHook, package):
        if hook.phase not in phases or not _NAME_RE.fullmatch(hook.name):
            raise RunnerError("registry_invalid", f"{area}.connect")
        if hook.name in seen:
            raise RunnerError("registry_duplicate", hook.name)
        seen.add(hook.name)
        phases[hook.phase].append(hook)
    return phases


def heartbeat_sections(package: str = registry.ROOT_PACKAGE) -> dict[str, HeartbeatSection]:
    """The provider of each heartbeat field; a field has at most one provider."""
    sections: dict[str, HeartbeatSection] = {}
    for area, section in _items("heartbeat", "SECTIONS", HeartbeatSection, package):
        if section.field not in HEARTBEAT_FIELDS:
            raise RunnerError("registry_invalid", f"{area}.heartbeat")
        if section.field in sections:
            raise RunnerError("registry_duplicate", section.field)
        sections[section.field] = section
    return sections
