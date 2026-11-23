"""U-HUB-billing (credit): accounts with the start credit only while billing is active, the settle of pending charges,
operator grants and sets, a balance set by hand in the database (also under concurrent bookings), the history with
its audit rows, and the settle job's announcements (12-billing.md sections 3 and 8, ADR-059 sections 5 and 6)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from agent_hub import signals
from agent_hub.billing import credit, jobs
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.billing.support import FLASH, Bill
from tests.platform.hub.tasks.support import audit_rows

TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 0, "cache_write_tokens": 0}


def _connection(bill: Bill) -> Any:
    return bill.hub.pool.connection()


def test_no_account_and_nothing_exhausted_while_billing_is_inactive(bill: Bill) -> None:
    assert bill.settle() == []
    assert bill.account() is None
    with _connection(bill) as connection:
        assert credit.exhausted(connection, bill.org, bill.hub.clock()) is False
        assert credit.balance(connection, bill.org) == 5_000_000


def test_accounts_get_the_start_credit_when_billing_starts(bill: Bill) -> None:
    bill.add_prices()
    assert bill.settle() == []  # the first pass only records the states
    assert bill.account() == (5_000_000, 5_000_000)
    assert bill.entries() == [("start", 5_000_000, 5_000_000, None, "hub")]
    rows = [row for row in audit_rows(bill.meter.desk, "credit.changed") if row[2] == bill.org]
    assert [row[3] for row in rows] == [
        {"kind": "start", "amount_micro_usd": 5_000_000, "balance_micro_usd": 5_000_000, "actor": "hub"}
    ]
    # a second pass changes nothing
    assert bill.settle() == []
    assert len(bill.entries()) == 1


@pytest.mark.parametrize("start_credit", ["0"])
def test_without_start_credit_billing_exhausts_and_announces(bill: Bill, published: MemoryPublisher) -> None:
    bill.settle()  # records "not exhausted" while inactive
    seen: list[str] = []
    found = {name: list(items) for name, items in signals.configure().items()}
    found.setdefault("credit.changed", []).append(lambda _n, ids: seen.append(str(ids["org_id"])))
    signals.configure(found)
    try:
        published.clear()
        bill.add_prices()
        assert bill.org in bill.settle()
    finally:
        signals.configure()
    assert bill.org in seen  # every org without credit flips, also the stranger's
    hinted = {str(entry.audience.id) for entry in published.items() if entry.frame == "reconcile"}
    assert {bill.device.id, bill.scene.other_device.id} <= hinted
    with _connection(bill) as connection:
        assert credit.exhausted(connection, bill.org, bill.hub.clock()) is True


def test_charges_settle_into_the_balance(bill: Bill) -> None:
    bill.platform()
    bill.add_prices()
    _task, session = bill.meter.session("platform")
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS) == ("ok", None)
    assert bill.account() is None  # the push never touches the account
    with _connection(bill) as connection:
        assert credit.balance(connection, bill.org) == 5_000_000  # start credit, the pending charge has no account yet
    bill.settle()
    assert bill.account() == (5_000_000 - 445_200, 5_000_000 - 445_200)
    assert bill.pending() == 0
    assert bill.runtime(session, 30_000) == ("ok", None)
    with _connection(bill) as connection:
        assert credit.balance(connection, bill.org) == 5_000_000 - 445_200 - 3000
    bill.settle()
    assert bill.account() == (4_551_800, 4_551_800)


def test_operator_grants_and_sets(bill: Bill) -> None:
    now = bill.hub.clock()
    with _connection(bill) as connection, connection.transaction():
        assert credit.grant(connection, bill.org, Decimal("10.5"), "  welcome  ", now).balance_micro_usd == 15_500_000
        assert credit.grant(connection, bill.org, Decimal(-1), "correction", now).balance_micro_usd == 14_500_000
        assert credit.set_balance(connection, bill.org, Decimal(20), "reset", now).balance_micro_usd == 20_000_000
        for wrong in ("", " ", "x" * 501):
            with pytest.raises(credit.CreditInvalid, match="the reason"):
                credit.grant(connection, bill.org, Decimal(1), wrong, now)
        with pytest.raises(credit.CreditInvalid, match="out of range"):
            credit.grant(connection, bill.org, Decimal(10**9 + 1), "big", now)
        with pytest.raises(credit.CreditInvalid, match="out of range"):
            credit.set_balance(connection, bill.org, Decimal(-(10**9) - 1), "big", now)
    assert bill.entries() == [
        ("start", 5_000_000, 5_000_000, None, "hub"),
        ("grant", 10_500_000, 15_500_000, "welcome", "operator"),
        ("grant", -1_000_000, 14_500_000, "correction", "operator"),
        ("set", 5_500_000, 20_000_000, "reset", "operator"),
    ]
    actors = [row[0] for row in audit_rows(bill.meter.desk, "credit.changed") if row[2] == bill.org]
    assert actors == ["hub", "operator", "operator", "operator"]


def test_a_balance_set_by_hand_is_recorded_and_survives_concurrent_bookings(bill: Bill) -> None:
    bill.platform()
    bill.add_prices()
    bill.settle()
    _task, session = bill.meter.session("platform")
    # a booking is pending, then the owner sets 50 USD directly in the database, then another booking arrives
    assert bill.runtime(session, 30_000) == ("ok", None)
    bill.execute(
        "INSERT INTO hub.credit_account (org_id, balance_micro_usd) VALUES (%s, 50000000)"
        " ON CONFLICT (org_id) DO UPDATE SET balance_micro_usd = EXCLUDED.balance_micro_usd",
        bill.org,
    )
    assert bill.runtime(session, 30_000) == ("ok", None)
    bill.settle()
    # the hand-set value counts from when it was set: 50 USD minus both pending bookings (they settle after it)
    assert bill.account() == (50_000_000 - 6000, 50_000_000 - 6000)
    assert bill.entries()[-1] == ("manual", 45_000_000, 50_000_000, None, "database")
    # adding by hand
    bill.execute(
        "UPDATE hub.credit_account SET balance_micro_usd = balance_micro_usd + 1000000 WHERE org_id = %s", bill.org
    )
    bill.settle()
    assert bill.account() == (50_994_000, 50_994_000)
    assert bill.entries()[-1] == ("manual", 1_000_000, 50_994_000, None, "database")


def test_a_hand_made_account_of_an_org_gets_no_start_credit(bill: Bill) -> None:
    bill.add_prices()
    bill.execute("INSERT INTO hub.credit_account (org_id, balance_micro_usd) VALUES (%s, 7000000)", bill.org)
    bill.settle()
    assert bill.entries() == [("manual", 7_000_000, 7_000_000, None, "database")]
    assert bill.account() == (7_000_000, 7_000_000)


def test_exhausted_orgs_and_balances(bill: Bill) -> None:
    now = bill.hub.clock()
    bill.add_prices()
    with _connection(bill) as connection, connection.transaction():
        credit.set_balance(connection, bill.org, Decimal(0), "empty", now)
        assert credit.exhausted_orgs(connection, [bill.org], now) == {bill.org: True}
        credit.set_balance(connection, bill.org, Decimal("0.000001"), "one micro", now)
        assert credit.exhausted(connection, bill.org, now) is False
        # before the first version nothing is exhausted
        assert credit.exhausted(connection, bill.org, now - timedelta(days=10)) is False


def test_the_job_is_registered_at_start() -> None:
    (job,) = jobs.JOBS
    assert (job.name, job.interval_seconds, job.at_start) == ("billing.settle", 5.0, True)


def test_due_lists_every_kind_of_work(bill: Bill) -> None:
    now = bill.hub.clock()
    with _connection(bill) as connection:
        assert credit.due(connection, now) == []
    bill.add_prices()
    with _connection(bill) as connection:
        assert bill.org in credit.due(connection, now)
