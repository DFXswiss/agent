"""U-HUB-operations: the constants of the area against the contracts, the budget hook's discovery and the epoch gap
of dedupe slots (04-crypto.md section 4.10)."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

import pytest

from agent_hub.operations import dedupe, limits, types
from agent_hub.registry import RegistryError
from agent_hub.usage import budget
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import FIXED_NOW
from tests.platform.hub.operations.support import Ops, tag


def test_known_types_are_the_contract_types() -> None:
    found = {path.name.removesuffix(".params.json") for path in (CONTRACTS / "ops").glob("*.params.json")}
    assert found == types.OPERATION_TYPES


def test_windows_are_the_contract_windows() -> None:
    data = json.loads((CONTRACTS / "ops" / "dedupe-fields.json").read_text(encoding="utf-8"))
    assert {name: entry["window_seconds"] for name, entry in data["types"].items()} == dict(types.WINDOWS)
    assert "86400" in data["idempotency_key"]["window_rule"]
    assert types.window("task.set_title") == timedelta(seconds=types.IDEMPOTENCY_WINDOW) == timedelta(days=1)


def test_budget_discovery() -> None:
    def check(_request: limits.BudgetRequest) -> bool:
        return False

    assert limits.collect([]) is limits.unlimited
    assert limits.unlimited(limits.BudgetRequest(None, UUID(int=1), UUID(int=2), FIXED_NOW)) is True  # type: ignore[arg-type]
    assert limits.collect([("budget_area", check)]) is check
    with pytest.raises(RegistryError, match="more than one"):
        limits.collect([("a", check), ("b", check)])
    with pytest.raises(RegistryError, match="callable"):
        limits.collect([("a", 3)])
    # the usage area (HUB-14) registers the budget check
    assert limits.configure() is budget.check


def test_epochs_never_match(ops: Ops) -> None:
    """A key rotation inside a window re-allows one duplicate (documented gap): the same tag in another epoch is a
    different slot."""
    _task, session = ops.session()
    holder, _answer = ops.decide(session, dedupe="rotated")
    now = ops.hub.clock()
    wg = UUID(ops.desk.wg)
    with ops.hub.pool.connection() as connection, connection.transaction():
        same = dedupe.claim(connection, dedupe.Slot(wg, 1, tag("rotated")), UUID(int=0), now, now)
        assert same is not None
        assert same.operation_id == UUID(holder)
        newer = UUID(ops.decide(session)[0])
        assert (
            dedupe.claim(connection, dedupe.Slot(wg, 2, tag("rotated")), newer, now + timedelta(hours=1), now) is None
        )
    assert ops.slot_holder("rotated", 1) == holder
    assert ops.slot_holder("rotated", 2) == str(newer)
