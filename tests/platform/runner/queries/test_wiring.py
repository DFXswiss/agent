"""U-RUN-queries wiring: the read tools are registered at `query_tools.py:TOOLS` (ADR-038) and the consumer at
`consumers.py:CONSUMERS` (ADR-039); the closed value sets of the area equal `contracts/`."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runner.inbox.dispatch import consumers
from agent_runner.queries import device_api, projection
from agent_runner.queries.query_tools import TOOLS
from agent_runner.subagents.extensions import query_tools
from agent_runner.subagents.journal import READ_TOOLS

ROOT = Path(__file__).resolve().parents[4]


def _enum(name: str) -> set[str]:
    return set(json.loads((ROOT / "contracts" / "common.json").read_text())["$defs"][name]["enum"])


def test_every_forwarded_read_has_a_provider() -> None:
    # get_subagent_result belongs to RUN-11, get_notifications is answered in the guest (RUN-14)
    assert set(TOOLS) == READ_TOOLS - {"get_subagent_result", "get_notifications"}
    found = query_tools()
    assert all(found[name] is tool for name, tool in TOOLS.items())


def test_the_consumer_takes_the_read_kinds() -> None:
    mine = [c for c in consumers() if c.name == "queries.read"]
    assert len(mine) == 1
    assert mine[0].kinds == projection.KINDS


def test_the_value_sets_are_the_contract_ones() -> None:
    assert _enum("task_state") == device_api.TASK_STATES
    assert _enum("session_state") == device_api.SESSION_STATES
    assert _enum("operation_state") == device_api.OPERATION_STATES
