"""U-HUB-models (DFX keys): DFX model access on own hardware (ADR-081 section 7, KOR-50) - the issuer election, the
requests and revocations of the job `models.dfx_keys`, the headroom, `dfx_key_issued` and the bookings of
`dfx_key_usage` (cumulative, split over the device's sessions by largest remainder, the rest with the org)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from agent_hub.billing import binding, bookkeeping
from agent_hub.billing.outbox import NO_WG
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.models import dfx
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.billing.support import price_list
from tests.platform.hub.models.support import ENVELOPE, Keys
from tests.platform.hub.orgs.support import new_id


def _hinted(published: MemoryPublisher) -> set[str]:
    return {str(entry.audience.id) for entry in published.items() if entry.frame == "reconcile"}


# --- issuer and requests ----------------------------------------------------------------------------------------------


def test_only_the_online_announced_platform_device_is_the_issuer(keys: Keys) -> None:
    keys.bill.add_prices()
    keys.bill.platform(keys.issuer)
    assert "dfx_keys" not in keys.state()  # a platform device without a management key
    assert keys.state(keys.device)["dfx_key_held"] == []  # an org device always gets the list
    # an org device cannot announce itself
    assert keys.status("dfx_key_state", {"issuer": True}, keys.device) == ("rejected", "forbidden")
    keys.issuer_ready()
    assert keys.state()["dfx_keys"] == {"requests": [], "keys": []}
    assert "dfx_key_held" not in keys.state()
    assert keys.status("dfx_key_state", {"issuer": True}) == ("ok", None)  # again: still the issuer
    # offline: nobody issues
    keys.bill.meter.desk.online(keys.issuer, seen=keys.hub.clock() - timedelta(seconds=31))
    with keys.hub.pool.connection() as connection:
        assert dfx.issuer(connection, keys.hub.clock(), 30) is None
    assert keys.status("dfx_key_state", {"issuer": False}) == ("ok", None)
    assert "dfx_keys" not in keys.state()
    assert keys.rows("SELECT count(*) FROM hub.dfx_issuer") == [(0,)]


def test_requests_need_active_billing_and_credit(keys: Keys, published: MemoryPublisher) -> None:
    issuer = keys.issuer_ready()
    assert keys.plan() == []  # no price list: no request
    assert keys.rows("SELECT count(*) FROM hub.dfx_key") == [(0,)]
    keys.bill.add_prices()
    published.clear()
    assert keys.plan() == [UUID(issuer.id)]
    assert _hinted(published) == {issuer.id}
    request = keys.request_id()
    kem = keys.rows("SELECT kem_pub FROM hub.device WHERE id = %s", keys.device.id)[0][0]
    assert keys.requests() == [
        {"request_id": request, "org_id": keys.org, "device_id": keys.device.id, "device_kem": b64u_encode(bytes(kem))}
    ]
    assert keys.plan() == []  # one live request per org device and org
    # an exhausted org gets no request listed (and no new one)
    keys.execute(
        "INSERT INTO hub.credit_account (org_id, balance_micro_usd, booked_micro_usd) VALUES (%s, 0, 0)", keys.org
    )
    assert keys.requests() == []


@pytest.mark.parametrize("start_credit", ["0"])
def test_an_exhausted_org_gets_no_request(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    assert keys.plan() == []
    assert keys.rows("SELECT count(*) FROM hub.dfx_key") == [(0,)]


def test_a_device_that_leaves_is_revoked_and_its_final_spend_closes_the_key(
    keys: Keys, published: MemoryPublisher
) -> None:
    issuer = keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    held = keys.state(keys.device)["dfx_key_held"]
    assert held == [
        {"org_id": keys.org, "key_ref": "dfx-key-0001", "envelope": ENVELOPE, "issuer_device_id": issuer.id}
    ]
    keys.execute(
        "UPDATE hub.org_member SET removed_seq = joined_seq + 100 WHERE org_id = %s AND principal_id = %s",
        keys.org,
        keys.device.id,
    )
    published.clear()
    assert keys.plan() == [UUID(keys.device.id), UUID(issuer.id)]
    assert _hinted(published) == {keys.device.id, issuer.id}
    envelope, _booked, revoked, closed = keys.key()
    assert (envelope, revoked is not None, closed) == (None, True, None)
    assert keys.listed() == [
        {
            "key_ref": "dfx-key-0001",
            "org_id": keys.org,
            "device_id": keys.device.id,
            "headroom_usd": 0.0,
            "disabled": True,
            "revoke": True,
        }
    ]
    # a spend measured before the revocation is booked, the key stays listed
    assert keys.spend(1000, at=revoked - timedelta(seconds=1)) == ("ok", None)
    assert keys.key()[3] is None
    keys.hub.clock.advance(2)
    assert keys.spend(1000, at=keys.hub.clock()) == ("ok", None)
    assert keys.key()[3] is not None
    assert keys.listed() == []
    assert keys.spend(5000) == ("ok", None)  # a late report of a closed key books nothing
    assert keys.key()[1] == 1000


def test_a_request_revoked_before_its_issue_keeps_no_envelope(keys: Keys, published: MemoryPublisher) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.plan()
    request = keys.request_id()
    keys.execute("UPDATE hub.device SET state = 'removed' WHERE id = %s", keys.device.id)
    keys.plan()
    published.clear()
    assert keys.issue(request) == ("ok", None)
    assert _hinted(published) == set()
    assert keys.key()[0] is None
    assert [key["revoke"] for key in keys.listed()] == [True]


# --- issue ------------------------------------------------------------------------------------------------------------


def test_an_issued_key_is_stored_once_and_reaches_the_org_device(keys: Keys, published: MemoryPublisher) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.plan()
    request = keys.request_id()
    assert keys.issue(new_id()) == ("rejected", "not_found")
    assert keys.issue(request, org_id=new_id()) == ("rejected", "request_invalid")
    assert keys.issue(request, device_id=keys.issuer.id) == ("rejected", "request_invalid")
    for broken in ({"key_ref": "dfx-k1"}, {"envelope": "A"}, {"envelope": "AB"}):
        body = {"request_id": request, "org_id": keys.org, "device_id": keys.device.id, "key_ref": "dfx-key-0001"}
        assert keys.raw_status("dfx_key_issued", {**body, "envelope": ENVELOPE, **broken}) == (
            "rejected",
            "request_invalid",
        )
    published.clear()
    assert keys.issue(request) == ("ok", None)
    assert _hinted(published) == {keys.device.id}
    assert keys.requests() == []
    assert keys.issue(request) == ("ok", None)  # the same issue again
    assert keys.issue(request, "dfx-key-0002") == ("rejected", "id_conflict")
    # a key reference of another request
    other = new_id()
    keys.execute(
        "INSERT INTO hub.dfx_key (request_id, org_id, device_id, requested_at) VALUES (%s, %s, %s, %s)",
        other,
        keys.org,
        new_id(),
        keys.hub.clock(),
    )
    assert keys.status(
        "dfx_key_issued",
        {
            "request_id": other,
            "org_id": keys.org,
            "device_id": keys.rows("SELECT device_id::text FROM hub.dfx_key WHERE request_id = %s", other)[0][0],
            "key_ref": "dfx-key-0001",
            "envelope": ENVELOPE,
        },
    ) == ("rejected", "id_conflict")
    # only a platform device issues
    assert keys.status(
        "dfx_key_issued",
        {
            "request_id": request,
            "org_id": keys.org,
            "device_id": keys.device.id,
            "key_ref": "dfx-key-0001",
            "envelope": ENVELOPE,
        },
        keys.device,
    ) == ("rejected", "forbidden")


# --- headroom ---------------------------------------------------------------------------------------------------------


def test_the_headroom_divides_the_credit_over_the_keys(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    binding.bind(keys.hub.config)
    # 5 USD start credit / factor 1.06
    assert [key["headroom_usd"] for key in keys.listed()] == [4.716981]
    keys.execute(
        "INSERT INTO hub.dfx_key (request_id, org_id, device_id, requested_at, issuer_device_id, key_ref, issued_at)"
        " VALUES (%s, %s, %s, %s, %s, 'dfx-key-0002', %s)",
        new_id(),
        keys.org,
        new_id(),
        keys.hub.clock(),
        keys.issuer.id,
        keys.hub.clock(),
    )
    assert [(key["headroom_usd"], key["disabled"]) for key in keys.listed()] == [(2.358490, False)] * 2
    keys.execute(
        "INSERT INTO hub.credit_account (org_id, balance_micro_usd, booked_micro_usd) VALUES (%s, -1, -1)", keys.org
    )
    assert [(key["headroom_usd"], key["disabled"]) for key in keys.listed()] == [(0.0, True)] * 2


def test_rooms_follow_the_price_list_and_the_cap(keys: Keys) -> None:
    binding.bind(keys.hub.config)
    now = keys.hub.clock()
    with keys.hub.pool.connection() as connection:
        assert dfx.rooms(connection, [keys.org], now, Decimal(5)) == {keys.org: dfx.Room(0.0, True)}
    keys.bill.add_prices()
    with keys.hub.pool.connection() as connection:
        assert dfx.rooms(connection, [keys.org], now, Decimal("1.5")) == {keys.org: dfx.Room(1.5, False)}
    free = price_list()
    free["tokens"]["factors"]["platform"] = 0
    keys.bill.add_prices(free, valid_from=now - timedelta(seconds=1), version=2)
    with keys.hub.pool.connection() as connection:
        assert dfx.rooms(connection, [keys.org], now, Decimal(5)) == {keys.org: dfx.Room(5.0, False)}


def test_without_billing_every_key_is_disabled(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    with keys.hub.pool.connection() as connection:
        found = dfx.issuer_state(connection, keys.hub.clock() - timedelta(days=30), Decimal(5))
    assert found == {
        "requests": [],
        "keys": [
            {
                "key_ref": "dfx-key-0001",
                "org_id": keys.org,
                "device_id": keys.device.id,
                "headroom_usd": 0.0,
                "disabled": True,
                "revoke": False,
            }
        ],
    }


# --- bookings ---------------------------------------------------------------------------------------------------------


def test_split_by_largest_remainder() -> None:
    assert dfx.split(10, [("a", 1), ("b", 1), ("c", 1)]) == [("a", 4), ("b", 3), ("c", 3)]
    assert dfx.split(1060, [("a", 300), ("b", 100), (None, 600)]) == [("a", 318), ("b", 106), (None, 636)]
    assert dfx.split(7, [("a", 2), ("b", 5)]) == [("a", 2), ("b", 5)]
    assert dfx.split(5, [("a", 1), ("b", 2)]) == [("a", 2), ("b", 3)]


def test_the_spend_is_booked_once_and_split_over_the_sessions(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    _task, first = keys.bill.meter.session("platform")
    _task, second = keys.bill.meter.session("platform")
    assert keys.cost(first, 300) == ("ok", None)
    assert keys.cost(second, 100) == ("ok", None)
    assert keys.cost(second, 0) == ("ok", None)  # nothing to weigh
    assert keys.bill.usage(first, device=keys.device, tokens={"cost_micro_usd": 50}) == ("ok", None)  # not DFX
    # the org device is never charged per request
    assert keys.bill.charges() == []
    assert keys.spend(1000) == ("ok", None)
    ordered = sorted([first, second])
    weights = {first: 300, second: 100}
    expected_amounts = dict(zip(ordered, [318, 106] if ordered[0] == first else [106, 318], strict=True))
    found = keys.charges()
    assert [(session, part) for session, part, _amount, _q in found] == [(ordered[0], 0), (ordered[1], 1), (None, 2)]
    assert [amount for session, _part, amount, _q in found[:2]] == [expected_amounts[s] for s in ordered]
    assert found[2][2] == 636
    assert found[2][3] == {
        "source": "provider_key",
        "key_ref": "dfx-key-0001",
        "cost_micro_usd": 600,
        "device_id": keys.device.id,
    }
    assert [q["cost_micro_usd"] for _s, _p, _a, q in found[:2]] == [weights[s] for s in ordered]
    assert keys.bill.pending() == 1060
    assert keys.rows("SELECT count(*) FROM hub.dfx_key_cost") == [(0,)]
    days = keys.rows(
        "SELECT wg_id::text, device_id::text, amount_micro_usd, quantity FROM hub.charge_daily ORDER BY wg_id"
    )
    wg = keys.bill.meter.desk.wg
    assert sorted(days) == sorted([(str(NO_WG), keys.device.id, 636, 0), (wg, keys.device.id, 424, 0)])
    # the same or a lower figure books nothing
    assert keys.spend(1000) == ("ok", None)
    assert keys.spend(900) == ("ok", None)
    assert len(keys.charges()) == 3
    # the provider reports less than the session did: the session gets it all, the rest of its cost waits
    assert keys.cost(first, 500) == ("ok", None)
    assert keys.spend(1100) == ("ok", None)
    assert [(session, amount) for session, _part, amount, _q in keys.charges()[3:]] == [(first, 106)]
    assert keys.rows("SELECT cost_micro_usd FROM hub.dfx_key_cost") == [(400,)]
    assert keys.key()[1] == 1100
    # the billing reads: the share without a session counts in the days, not in the workgroups
    body = keys.bill.scene.admin.browser.get(f"/v1/orgs/{keys.org}/billing").json()
    assert [entry["wg_id"] for entry in body["workgroups"]] == [wg]
    assert sum(day["tokens_micro_usd"] for day in body["days"]) == 1060 + 106
    with keys.hub.pool.connection() as connection:
        today = keys.hub.clock().date()
        rows = bookkeeping.bookings(connection, today - timedelta(days=1), today + timedelta(days=1), None)
    references = [row["reference"] for row in rows if row["kind"] == "tokens"]
    assert sum(1 for reference in references if reference.endswith(" key:dfx-key-0001")) == 1
    assert sum(1 for reference in references if " session:" in reference) == 3


def test_a_split_where_the_sessions_reported_more_than_the_provider(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    _task, first = keys.bill.meter.session("platform")
    _task, second = keys.bill.meter.session("platform")
    assert keys.cost(first, 600) == ("ok", None)
    assert keys.cost(second, 600) == ("ok", None)
    assert keys.spend(1000) == ("ok", None)
    assert sorted(amount for _s, _p, amount, _q in keys.charges()) == [530, 530]
    assert sorted(row[0] for row in keys.rows("SELECT cost_micro_usd FROM hub.dfx_key_cost")) == [100, 100]


def test_without_a_price_list_the_figure_advances_without_a_charge(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    at = keys.hub.clock() - timedelta(days=30)  # before the price list
    assert keys.spend(2000, at=at) == ("ok", None)
    assert (keys.key()[1], keys.charges()) == (2000, [])


def test_usage_reports_are_checked(keys: Keys) -> None:
    keys.issuer_ready()
    keys.bill.add_prices()
    keys.issued()
    assert keys.spend(10, key_ref="dfx-unknown1") == ("rejected", "not_found")
    assert keys.spend(10, device_id=keys.issuer.id) == ("rejected", "request_invalid")
    assert keys.spend(10, org_id=new_id()) == ("rejected", "request_invalid")
    body = {
        "key_ref": "dfx-key-0001",
        "org_id": keys.org,
        "device_id": keys.device.id,
        "spent_micro_usd": 10,
        "at": keys.bill.meter.time(-1),
    }
    assert keys.status("dfx_key_usage", body, keys.device) == ("rejected", "forbidden")
    assert keys.key()[1] == 0


def test_only_an_org_device_with_a_live_key_weighs_its_sessions(keys: Keys) -> None:
    keys.bill.add_prices()
    _task, session = keys.bill.meter.session("platform")
    assert keys.cost(session, 300) == ("ok", None)  # no key yet
    assert keys.rows("SELECT count(*) FROM hub.dfx_key_cost") == [(0,)]
    keys.issuer_ready()
    keys.issued()
    assert keys.cost(session, 300) == ("ok", None)
    assert keys.rows("SELECT cost_micro_usd FROM hub.dfx_key_cost") == [(300,)]
    # a platform device's request is charged by the billing area, not weighed
    _task, platform_session = keys.bill.meter.session("platform", device=keys.issuer)
    assert keys.cost(platform_session, 700, device=keys.issuer) == ("ok", None)
    assert keys.rows("SELECT cost_micro_usd FROM hub.dfx_key_cost") == [(300,)]
