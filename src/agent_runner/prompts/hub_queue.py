"""What the hub says about the prompt queue, from the last desired state (`GET /v1/device/state` section
`prompt_queue`, ADR-083 sections 2 and 3).

- `api(desired)`: the hub sends the section, so it takes the new fields of ADR-083 - `triggered_by` of the outbox
  `usage` and `runtime` items and the outbox audit `session.prompt_withdrawn`. An older hub has no section and refuses
  unknown fields; the runner then sends none of them. (`triggered_by` of an operation registration belongs to KOR-47,
  `operations/register.py`.)
- `parse(desired)`: `(via_agent, withdrawn)`, the event IDs of pending prompts of this device's sessions the hub
  received from the author's agent, and those whose author asked to withdraw them (both empty without the section).
  A malformed section raises `wire.ResponseInvalid` like every other malformed answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runner.reconcile import wire

SECTION = "prompt_queue"


def api(desired: Mapping[str, Any] | None) -> bool:
    return desired is not None and isinstance(desired.get(SECTION), Mapping)


def parse(desired: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    if SECTION not in desired:
        return frozenset(), frozenset()
    data = wire.obj(desired[SECTION], SECTION)
    if set(data) != {"via_agent", "withdrawn"}:
        raise wire.ResponseInvalid(SECTION)
    return _ids(data["via_agent"]), _ids(data["withdrawn"])


def _ids(value: object) -> frozenset[str]:
    return frozenset(wire.uuid_text(item, SECTION) for item in wire.array(value, SECTION))
