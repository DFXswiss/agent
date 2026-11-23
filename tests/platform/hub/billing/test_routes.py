"""U-HUB-billing (routes): `GET /v1/orgs/{org}/billing`, `GET /v1/workgroups/{wg}/charges` and
`GET /v1/tasks/{task}/charges`
- contents, rights (`404 not_found` for foreign and unknown IDs) and read log rows (12-billing.md section 6)."""

from __future__ import annotations

from decimal import Decimal

from agent_hub.billing import credit
from tests.platform.hub.billing.support import FLASH, Bill
from tests.platform.hub.orgs.support import new_id

TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 10, "cache_write_tokens": 0}


def _charged(bill: Bill) -> tuple[str, str]:
    bill.platform()
    bill.add_prices()
    task, session = bill.meter.session("platform")
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session, 30_000) == ("ok", None)
    return task, session


def test_org_billing_without_prices(bill: Bill) -> None:
    response = bill.scene.reader.browser.get(f"/v1/orgs/{bill.org}/billing")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "currency": "USD",
        "balance_micro_usd": 5_000_000,
        "exhausted": False,
        "billing_active": False,
        "price_list": None,
        "credits": [],
        "days": [],
        "workgroups": [],
    }


def test_org_billing_with_charges_and_credits(bill: Bill) -> None:
    _charged(bill)
    with bill.hub.pool.connection() as connection, connection.transaction():
        credit.grant(connection, bill.org, Decimal(2), "welcome", bill.hub.clock())
    body = bill.scene.admin.browser.get(f"/v1/orgs/{bill.org}/billing").json()
    assert body["balance_micro_usd"] == 5_000_000 + 2_000_000 - 445_200 - 3000
    assert (body["exhausted"], body["billing_active"]) == (False, True)
    assert body["price_list"]["version"] == 1
    assert body["price_list"]["body"]["currency"] == "USD"
    assert [(entry["kind"], entry["amount_micro_usd"], entry["actor"]) for entry in body["credits"]] == [
        ("grant", 2_000_000, "operator"),
        ("start", 5_000_000, "hub"),
    ]
    totals = {"tokens_micro_usd": 445_200, "runtime_micro_usd": 3000, "tokens": 1_100_010, "runtime_ms": 30_000}
    assert body["days"] == [{"day": "2026-11-21", **totals}]
    assert body["workgroups"] == [{"wg_id": bill.meter.desk.wg, **totals}]


def test_charges_of_a_workgroup_and_a_task(bill: Bill) -> None:
    task, session = _charged(bill)
    other, _session = bill.meter.session("platform")
    wg = bill.scene.reader.browser.get(f"/v1/workgroups/{bill.meter.desk.wg}/charges")
    assert wg.status_code == 200, wg.text
    assert wg.json() == {
        "tasks": [
            {
                "task_id": task,
                "tokens_micro_usd": 445_200,
                "runtime_micro_usd": 3000,
                "tokens": 1_100_010,
                "runtime_ms": 30_000,
            }
        ],
        "total_micro_usd": 448_200,
    }
    found = bill.scene.reader.browser.get(f"/v1/tasks/{task}/charges").json()
    assert found == {
        "sessions": [
            {
                "session_id": session,
                "tokens_micro_usd": 445_200,
                "runtime_micro_usd": 3000,
                "tokens": 1_100_010,
                "runtime_ms": 30_000,
            }
        ],
        "total_micro_usd": 448_200,
    }
    assert bill.scene.reader.browser.get(f"/v1/tasks/{other}/charges").json() == {"sessions": [], "total_micro_usd": 0}


def test_foreign_and_unknown_ids_are_not_found(bill: Bill) -> None:
    task, _session = _charged(bill)
    stranger = bill.scene.stranger.browser
    for path in (
        f"/v1/orgs/{bill.org}/billing",
        f"/v1/workgroups/{bill.meter.desk.wg}/charges",
        f"/v1/tasks/{task}/charges",
        f"/v1/tasks/{new_id()}/charges",
    ):
        response = stranger.get(path)
        assert (response.status_code, response.json()["error"]) == (404, "not_found"), path
