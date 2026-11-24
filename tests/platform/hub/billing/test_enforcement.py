"""U-HUB-billing (enforcement): no new paid start - the scheduler skips the platform devices of an exhausted org, uses
its own devices, waits with `credit_exhausted` and places the task once credit is added; the budget hook H4 answers
`credit_exhausted`; the `billing` section of the device state (12-billing.md sections 5 and 7, ADR-062 section 6)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.billing import credit
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.scheduler.core import Candidate, Capacity, NoPlacement, choose
from tests.platform.hub.billing.support import Bill
from tests.platform.hub.usage.support import SUBAGENT

FREE = Capacity(vm_memory_budget_mib=8192, vm_slots_total=2, vm_slots_free=2)


def test_choose_skips_unpaid_devices() -> None:
    paid = Candidate("b", online=True, capacity=FREE)
    unpaid = Candidate("a", online=True, capacity=FREE, unpaid=True)
    assert choose([unpaid, paid]).device_id == "b"  # type: ignore[union-attr]
    assert choose([unpaid]) == NoPlacement("credit_exhausted")
    # an offline unpaid device does not make the reason credit
    full = Candidate("c", online=True, capacity=Capacity(vm_slots_total=2))
    offline = Candidate("d", online=False, capacity=FREE, unpaid=True)
    assert choose([full, offline]) == NoPlacement("capacity_exhausted")


def _exhaust(bill: Bill) -> None:
    with bill.hub.pool.connection() as connection, connection.transaction():
        credit.set_balance(connection, bill.org, Decimal(0), "used up", bill.hub.clock())


def test_an_exhausted_org_waits_and_starts_after_a_credit(bill: Bill) -> None:
    desk = bill.meter.desk
    bill.platform(bill.scene.device)
    bill.platform(bill.scene.other_device)
    bill.add_prices()
    _exhaust(bill)
    desk.online(bill.scene.device)
    task, session = desk.started()
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("scheduling", "credit_exhausted")
    assert desk.session(session)["device_id"] is None
    # a credit: the signal places the task
    with bill.hub.pool.connection() as connection, connection.transaction():
        credit.grant(connection, bill.org, Decimal(1), "top up", bill.hub.clock())
    scheduler_signals.bind(bill.hub.app.state.hub)
    signals.emit("credit.changed", org_id=UUID(bill.org))
    scheduler_signals.wait()
    assert desk.session(session)["device_id"] == bill.scene.device.id


def test_own_devices_stay_usable_without_credit(bill: Bill) -> None:
    desk = bill.meter.desk
    bill.platform(bill.scene.device)
    bill.add_prices()
    _exhaust(bill)
    desk.online(bill.scene.device)
    desk.online(bill.scene.other_device)
    _task, session = desk.started()
    assert desk.session(session)["device_id"] == bill.scene.other_device.id


def test_the_budget_hook_denies_subagents_on_dfx_hardware(bill: Bill) -> None:
    bill.add_prices()
    _exhaust(bill)
    # an org device: own hardware is never refused for credit
    _task, own = bill.meter.session("api_key")
    assert bill.meter.ops.decide(own, SUBAGENT)[1]["state"] == "accepted"
    bill.platform()
    _task, paid = bill.meter.session("api_key")
    assert bill.meter.ops.decide(paid, SUBAGENT)[1] == {"state": "denied", "error_code": "credit_exhausted"}


@pytest.mark.parametrize("start_credit", ["0"])
def test_the_device_state_section(bill: Bill) -> None:
    state = bill.meter.state(bill.device)["billing"]
    assert state == {"api": 1, "active": False, "orgs": {bill.org: {"exhausted": False}}}
    bill.add_prices()
    state = bill.meter.state(bill.device)["billing"]
    assert state == {"api": 1, "active": True, "orgs": {bill.org: {"exhausted": True}}}
