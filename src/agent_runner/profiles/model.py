"""Rights profiles as the runner reads them (ADR-094 sections 1-3; `contracts/common.json#/$defs/agent_profile`).

A profile is `{name, operations, reads}`: the operation types a session may request and the reach of its read tools
(`task` ⊂ `workgroup` ⊂ `all`). None means no profile: every right (A1, E-11). The hub resolves names and computes
the effective profile of every session; the runner only reads them (`state.py`) and applies them:

- `allows(profile, op_type)`: may the session request the type;
- `within(child, parent)`: a child's profile allows nothing its parent does not;
- `tool_type(tool)`: the operation type a platform MCP tool that is no operation stands for (`workvm_*` ->
  `workvm.request`, `browser_*` -> `browser.open`, `contracts/ops/classes.json` `tools`), None for a read tool;
- `parse(value)`: a profile of the wire form (`ValueError` for anything else), None for null.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

REACHES = ("task", "workgroup", "all")
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
TYPE_RE = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")
TOOL_PREFIXES: Mapping[str, str] = {"workvm_": "workvm.request", "browser_": "browser.open"}


@dataclass(frozen=True)
class Profile:
    name: str
    operations: frozenset[str]
    reads: str

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "operations": sorted(self.operations), "reads": self.reads}


def parse(value: object) -> Profile | None:
    """The profile of a wire value (module docstring)."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"name", "operations", "reads"}:
        raise ValueError("profile")
    name, operations, reads = value["name"], value["operations"], value["reads"]
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None or reads not in REACHES:
        raise ValueError("profile")
    if not isinstance(operations, list) or not all(_type(item) for item in operations):
        raise ValueError("profile")
    return Profile(name, frozenset(operations), str(reads))


def _type(item: object) -> bool:
    return isinstance(item, str) and TYPE_RE.fullmatch(item) is not None


def allows(profile: Profile | None, op_type: str) -> bool:
    return profile is None or op_type in profile.operations


def within(child: Profile, parent: Profile | None) -> bool:
    if parent is None:
        return True
    return child.operations <= parent.operations and REACHES.index(child.reads) <= REACHES.index(parent.reads)


def tool_type(tool: str) -> str | None:
    for prefix, op_type in TOOL_PREFIXES.items():
        if tool.startswith(prefix):
            return op_type
    return None


def reach(profile: Profile | None) -> str:
    return "all" if profile is None else profile.reads
