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
        "people": [],
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
    assert body["people"] == [{"user_id": None, **totals}]


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
        "people": [
            {
                "user_id": None,
                "tokens_micro_usd": 445_200,
                "runtime_micro_usd": 3000,
                "tokens": 1_100_010,
                "runtime_ms": 30_000,
            }
        ],
    }
    assert bill.scene.reader.browser.get(f"/v1/tasks/{other}/charges").json() == {
        "sessions": [],
        "total_micro_usd": 0,
        "people": [],
    }


def test_consumption_per_person(bill: Bill) -> None:
    """KOR-46 (ADR-083 section 2): the consumption items name the person; billing adds a breakdown per person, largest
    amount first, unattributed consumption as `user_id` null; the org is charged as before. Only the author of a
    prompt of the session's task counts: anybody else is stored as unattributed."""
    bill.platform()
    bill.add_prices()
    task, session = bill.meter.session("platform")
    _other_task, other = bill.meter.session("platform")
    scene = bill.scene
    writer, reader = scene.writer.id, scene.reader.id
    scene.draft.add("grant", scene.admin, subject=reader, wg=scene.wg, access="write")
    assert scene.draft.post(scene.admin).status_code == 200
    scene.append(scene.writer, scene.prompt(scene.writer, task))
    scene.append(scene.reader, scene.prompt(scene.reader, task))
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS, triggered_by=writer) == (
        "ok",
        None,
    )
    assert bill.runtime(session, 30_000, triggered_by=reader) == ("ok", None)
    assert bill.runtime(session, 30_000, triggered_by=None) == ("ok", None)
    assert bill.runtime(session, 5_000) == ("ok", None)
    # not an author of a prompt of this task (the admin wrote prompts elsewhere only, a stranger none)
    assert bill.runtime(session, 2_500, triggered_by=scene.admin.id) == ("ok", None)
    assert bill.runtime(session, 2_500, triggered_by=scene.stranger.id) == ("ok", None)
    # the writer wrote no prompt of the other task
    assert bill.runtime(other, 10_000, triggered_by=writer) == ("ok", None)
    charged = bill.rows("SELECT kind, triggered_by::text FROM hub.charge ORDER BY at, origin_seq")
    assert charged == [("tokens", writer), ("runtime", reader)] + [("runtime", None)] * 5
    assert sorted(
        bill.rows("SELECT user_id::text, kind, amount_micro_usd, quantity FROM hub.charge_person_daily")
    ) == sorted(
        [
            ("00000000-0000-0000-0000-000000000000", "runtime", 5000, 50_000),
            (reader, "runtime", 3000, 30_000),
            (writer, "tokens", 445_200, 1_100_010),
        ]
    )
    tokens = {"tokens_micro_usd": 445_200, "runtime_micro_usd": 0, "tokens": 1_100_010, "runtime_ms": 0}
    nobody = {"user_id": None, "tokens_micro_usd": 0, "tokens": 0}
    reading = {"user_id": reader, "tokens_micro_usd": 0, "runtime_micro_usd": 3000, "tokens": 0, "runtime_ms": 30_000}
    billed = scene.admin.browser.get(f"/v1/orgs/{bill.org}/billing").json()
    assert billed["people"] == [
        {"user_id": writer, **tokens},
        {**nobody, "runtime_micro_usd": 5000, "runtime_ms": 50_000},
        reading,
    ]
    assert scene.reader.browser.get(f"/v1/tasks/{task}/charges").json()["people"] == [
        {"user_id": writer, **tokens},
        {**nobody, "runtime_micro_usd": 4000, "runtime_ms": 40_000},
        reading,
    ]
    assert billed["workgroups"][0]["runtime_micro_usd"] == 8000


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
