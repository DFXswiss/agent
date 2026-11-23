"""U-HUB-billing (bookings): token and runtime charges from the outbox - the four cases of idea 46, the price version
at the measurement time, replays without a second charge, rights and limits of `runtime` items, the pending charges
and the daily sums (12-billing.md sections 3, 4 and 7, ADR-059 sections 1, 3 and 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.billing.support import FLASH, Bill, price_list
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.outbox.support import item, usage_body

TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 0, "cache_write_tokens": 0}
FLASH_PLATFORM = 445_200  # (300000 + 120000) x 1.06


def _scene(bill: Bill, access: str | None = "platform", *, platform: bool = True, prices: bool = True) -> str:
    if platform:
        bill.platform()
    if prices:
        bill.add_prices()
    _task, session = bill.meter.session(access)
    bill.execute("UPDATE hub.session SET model_id = %s WHERE id = %s", FLASH, session)
    return session


def test_nothing_is_charged_without_a_price_list(bill: Bill) -> None:
    bill.platform()
    _task, session = bill.meter.session("platform")
    assert bill.usage(session, tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session) == ("ok", None)
    assert bill.charges() == []


def test_dfx_hardware_and_dfx_access_charge_tokens_and_runtime(bill: Bill) -> None:
    session = _scene(bill)
    assert bill.usage(
        session, model=FLASH, access_origin="platform", at=format_time(bill.hub.clock()), tokens=TOKENS
    ) == (
        "ok",
        None,
    )
    assert bill.runtime(session, 30_000) == ("ok", None)
    charges = bill.charges()
    assert [(kind, amount, version) for kind, amount, version, _q in charges] == [
        ("tokens", FLASH_PLATFORM, 1),
        ("runtime", 3000, 1),
    ]
    assert charges[0][3] == {**TOKENS, "model": FLASH, "access": "platform"}
    assert charges[1][3] == {"runtime_ms": 30_000, "vm_kind": "session", "size": "session", "vm_id": "vm-0000abcd"}
    assert bill.pending() == FLASH_PLATFORM + 3000
    assert bill.rows("SELECT kind, amount_micro_usd, quantity FROM hub.charge_daily ORDER BY kind") == [
        ("runtime", 3000, 30_000),
        ("tokens", FLASH_PLATFORM, 1_100_000),
    ]


def test_dfx_hardware_and_own_access_charge_runtime_only(bill: Bill) -> None:
    session = _scene(bill, "api_key")
    assert bill.usage(session, cost_basis="list_price", tokens=TOKENS) == ("ok", None)
    assert bill.usage(session, access_origin="own", tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session) == ("ok", None)
    assert [kind for kind, *_rest in bill.charges()] == ["runtime"]


def test_own_hardware_is_never_charged(bill: Bill) -> None:
    # own hardware + own access, and own hardware reporting DFX access (cannot occur today: the hub decides)
    session = _scene(bill, "platform", platform=False)
    assert bill.usage(session, access_origin="platform", tokens=TOKENS) == ("ok", None)
    assert bill.usage(session, tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session) == ("ok", None)
    assert bill.charges() == []


def test_operators_use_their_factor(bill: Bill) -> None:
    session = _scene(bill, "subscription")
    assert bill.usage(session, model="claude-sonnet-5-5", access_origin="operator", tokens=TOKENS) == ("ok", None)
    # (1M x 2 + 0.1M x 10) x 1 = 3 USD
    assert [(amount, q["access"], q["model"]) for _k, amount, _v, q in bill.charges()] == [
        (3_000_000, "operator", "claude-sonnet-5-5")
    ]


def test_an_older_runner_is_charged_by_the_session(bill: Bill) -> None:
    # no dfx_access, model or at: platform access (or unknown access with the provider basis) counts, the session's
    # model prices it
    session = _scene(bill, "platform")
    assert bill.usage(session, tokens=TOKENS) == ("ok", None)
    unknown = _scene(bill, None, prices=False)
    bill.execute("UPDATE hub.session SET model_id = NULL WHERE id = %s", unknown)
    assert bill.usage(unknown, tokens=TOKENS) == ("ok", None)
    amounts = [(amount, q["model"]) for _k, amount, _v, q in bill.charges()]
    # the session without a model is priced by `default` (8/40): (8 + 4) x 1.06 = 12.72 USD
    assert amounts == [(FLASH_PLATFORM, FLASH), (12_720_000, None)]


def test_the_price_version_of_the_measurement_time(bill: Bill) -> None:
    session = _scene(bill)
    bill.meter.until(bill.hub.clock() + timedelta(minutes=10))  # the session runs for 10 minutes
    now = bill.hub.clock()
    bill.add_prices(
        price_list(tokens={**price_list()["tokens"], "factors": {"platform": 2, "operator": 1}}),
        now - timedelta(minutes=1),
        version=2,
    )
    earlier = format_time(now - timedelta(minutes=5))
    assert bill.usage(session, access_origin="platform", at=earlier, model=FLASH, tokens=TOKENS) == ("ok", None)
    assert bill.usage(session, access_origin="platform", model=FLASH, tokens=TOKENS) == ("ok", None)
    # a time after the receive time is clamped to it
    future = format_time(now + timedelta(minutes=5))
    assert bill.usage(session, access_origin="platform", at=future, model=FLASH, tokens=TOKENS) == ("ok", None)
    assert [(amount, version) for _k, amount, version, _q in bill.charges()] == [
        (FLASH_PLATFORM, 1),
        (840_000, 2),
        (840_000, 2),
    ]
    # a time before the session (before the first version even) is clamped to the session's creation: the version in
    # force then (1) prices it, a backdated time cannot make a request free
    before = format_time(now - timedelta(days=3))
    assert bill.usage(session, access_origin="platform", at=before, tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session, 1000, to=before, extra={"from": before}) == ("rejected", "request_invalid")
    oldest = bill.charges()[0]
    assert (oldest[1], oldest[2]) == (FLASH_PLATFORM, 1)
    assert len(bill.charges()) == 4


def test_runtime_before_the_first_version_is_free(bill: Bill) -> None:
    session = _scene(bill)
    now = bill.hub.clock()
    end = now - timedelta(days=3)
    body = {"from": format_time(end - timedelta(seconds=30)), "to": format_time(end)}
    assert bill.runtime(session, 30_000, extra=body) == ("ok", None)
    assert bill.charges() == []


def test_replays_charge_once(bill: Bill) -> None:
    session = _scene(bill)
    first = item(1, "usage", {**usage_body(session, **TOKENS), "access_origin": "platform", "model": FLASH})
    assert bill.meter.outbox.statuses(first) == [(1, "ok", None)]
    assert bill.meter.outbox.statuses(first) == [(1, "replayed", None)]
    assert len(bill.charges()) == 1
    assert bill.pending() == FLASH_PLATFORM


def test_runtime_items_rights_and_limits(bill: Bill) -> None:
    session = _scene(bill)
    assert bill.runtime(new_id()) == ("rejected", "not_found")
    # a session of another device of the same org is accepted (moved sessions keep their late items)
    _task, foreign = bill.meter.session(device=bill.scene.other_device)
    assert bill.runtime(foreign) == ("ok", None)
    # more runtime than the interval, an interval in the future, a reversed interval, a too long item
    now = bill.hub.clock()
    assert bill.runtime(session, 30_000, extra={"from": format_time(now - timedelta(seconds=10))}) == (
        "rejected",
        "request_invalid",
    )
    later = now + timedelta(seconds=400)
    assert bill.runtime(
        session, 1000, to=format_time(later), extra={"from": format_time(later - timedelta(seconds=1))}
    ) == (
        "rejected",
        "request_invalid",
    )
    assert bill.runtime(session, 1000, to=format_time(now - timedelta(seconds=5))) == ("rejected", "request_invalid")
    assert bill.runtime(session, 3_600_001, valid=False) == ("rejected", "request_invalid")
    assert bill.runtime(session, vm_kind="other", valid=False) == ("rejected", "request_invalid")
    # a work VM is priced by its size
    assert bill.runtime(session, 30_000, vm_kind="work", size="large") == ("ok", None)
    assert [amount for _k, amount, _v, _q in bill.charges()] == [3000, 6000]


def test_a_device_that_does_not_serve_the_org_is_refused(bill: Bill) -> None:
    session = _scene(bill)
    stranger, _seq = bill.meter.ops.fleet.platform_device()
    assert bill.runtime(session, device=stranger) == ("rejected", "forbidden")
    assert bill.charges() == []


@pytest.mark.parametrize("size", ["standard", "xlarge"])
def test_work_vm_sizes(bill: Bill, size: str) -> None:
    session = _scene(bill)
    assert bill.runtime(session, 3_600_000, vm_kind="work", size=size) == ("ok", None)
    assert [amount for _k, amount, _v, _q in bill.charges()] == [360_000 if size == "standard" else 1_440_000]


def test_a_second_booking_of_the_same_outbox_item_is_refused_by_the_key(bill: Bill) -> None:
    session = _scene(bill)
    # a charge for the device's next outbox position exists already (the unique key is the second line of defence
    # behind the receipts): the handler books nothing more - no second pending charge, no second daily sum
    position = bill.meter.seqs.get(bill.device.id, 0) + 1
    bill.execute(
        "INSERT INTO hub.charge (id, org_id, wg_id, task_id, session_id, device_id, origin_seq, kind, at,"
        " amount_micro_usd, price_version, quantity) SELECT %s, org_id, wg_id, task_id, id, %s, %s, 'tokens', %s, 1, 1,"
        " '{}' FROM hub.session WHERE id = %s",
        new_id(),
        bill.device.id,
        position,
        bill.hub.clock(),
        session,
    )
    assert bill.usage(session, access_origin="platform", tokens=TOKENS) == ("ok", None)
    assert len(bill.charges()) == 1
    assert bill.pending() == 0
    assert bill.rows("SELECT count(*) FROM hub.charge_daily") == [(0,)]
