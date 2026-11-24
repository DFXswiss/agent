"""U-HUB-billing (bounds, ADR-086 section 2, KOR-35): the charge lists of a workgroup and a task are paged with
`before`/`limit` (newest first by the UUIDv7 ID, `next_before`) and, without the parameters, answer as before cut at
their cap with `truncated: true`; the credit history of `GET /v1/orgs/{org}/billing` says when older entries exist."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_hub.billing import credit, routes
from agent_hub.crypto import ids
from agent_hub.events import paging
from tests.platform.hub.billing.support import Bill

TOKENS = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1, "cache_write_tokens": 0}


def _id(ms: int) -> str:
    """A UUIDv7 of the moment `ms` (IDs of increasing `ms` sort in creation order)."""
    return ids.uuid7(1_795_000_000_000 + ms)


def _charge(bill: Bill, *, task: str, session: str, amount: int, at: datetime, kind: str = "tokens") -> None:
    quantity = TOKENS if kind == "tokens" else {"runtime_ms": 1000}
    bill.execute(
        "INSERT INTO hub.charge (id, org_id, wg_id, task_id, session_id, device_id, origin_seq, kind, at,"
        " amount_micro_usd, price_version, quantity) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, 1, %s)",
        ids.uuid7(),
        bill.org,
        bill.meter.desk.wg,
        task,
        session,
        ids.uuid7(),
        kind,
        at,
        amount,
        Jsonb(quantity),
    )


def _get(bill: Bill, path: str) -> dict[str, Any]:
    response = bill.scene.reader.browser.get(path)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _sessions(body: dict[str, Any]) -> list[str]:
    return [row["session_id"] for row in body["sessions"]]


def _tasks(body: dict[str, Any]) -> list[str]:
    return [row["task_id"] for row in body["tasks"]]


@pytest.fixture
def charged_task(bill: Bill) -> tuple[str, list[str]]:
    """A task with three charged sessions (created in this order); the second was charged first."""
    task, _session = bill.meter.session("platform")
    now = bill.hub.clock()
    first, second, third = _id(1), _id(2), _id(3)
    _charge(bill, task=task, session=first, amount=100, at=now - timedelta(minutes=5))
    _charge(bill, task=task, session=first, amount=10, at=now, kind="runtime")
    _charge(bill, task=task, session=second, amount=200, at=now - timedelta(minutes=9))
    _charge(bill, task=task, session=third, amount=300, at=now - timedelta(minutes=1))
    return task, [first, second, third]


def test_task_charges_without_paging_keep_their_order(bill: Bill, charged_task: tuple[str, list[str]]) -> None:
    task, (first, second, third) = charged_task
    body = _get(bill, f"/v1/tasks/{task}/charges")
    assert _sessions(body) == [second, first, third]
    assert body["total_micro_usd"] == 610
    assert "truncated" not in body
    assert "next_before" not in body
    assert body["sessions"][1] == {
        "session_id": first,
        "tokens_micro_usd": 100,
        "runtime_micro_usd": 10,
        "tokens": 16,
        "runtime_ms": 1000,
    }


def test_task_charges_are_cut_at_their_cap(
    bill: Bill, charged_task: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    task, (first, second, _third) = charged_task
    monkeypatch.setattr(routes, "SESSIONS", 2)
    body = _get(bill, f"/v1/tasks/{task}/charges")
    assert (_sessions(body), body["truncated"]) == ([second, first], True)
    # the total is that of the whole task
    assert body["total_micro_usd"] == 610


def test_task_charges_page_by_page(bill: Bill, charged_task: tuple[str, list[str]]) -> None:
    task, (first, second, third) = charged_task
    page = _get(bill, f"/v1/tasks/{task}/charges?limit=2")
    assert (_sessions(page), page["total_micro_usd"]) == ([third, second], 610)
    assert page["next_before"] == paging.encode_cursor(paging.EPOCH, second)
    # a charge of a new session does not move the next page
    _charge(bill, task=task, session=ids.uuid7(), amount=1, at=bill.hub.clock())
    older = _get(bill, f"/v1/tasks/{task}/charges?before={page['next_before']}&limit=2")
    assert (_sessions(older), older["next_before"]) == ([first], None)
    assert "truncated" not in older
    # the default page size
    assert len(_get(bill, f"/v1/tasks/{task}/charges?before={page['next_before']}")["sessions"]) == 1
    newest = _get(bill, f"/v1/tasks/{task}/charges?limit=200")
    assert (len(newest["sessions"]), newest["next_before"], newest["total_micro_usd"]) == (4, None, 611)


def test_workgroup_charges_without_paging_and_cut(bill: Bill, monkeypatch: pytest.MonkeyPatch) -> None:
    now = bill.hub.clock()
    small, large = _id(1), _id(2)
    _charge(bill, task=small, session=_id(3), amount=5, at=now)
    _charge(bill, task=large, session=_id(4), amount=50, at=now)
    # outside the 30 days
    _charge(bill, task=_id(5), session=_id(6), amount=7, at=now - timedelta(days=40))
    path = f"/v1/workgroups/{bill.meter.desk.wg}/charges"
    body = _get(bill, path)
    assert (_tasks(body), "truncated" in body, "next_before" in body) == ([large, small], False, False)
    monkeypatch.setattr(routes, "TASKS", 1)
    body = _get(bill, path)
    assert (_tasks(body), body["truncated"]) == ([large], True)


def test_workgroup_charges_page_by_page(bill: Bill) -> None:
    now = bill.hub.clock()
    tasks = [_id(n) for n in (1, 2, 3)]
    for n, task in enumerate(tasks):
        _charge(bill, task=task, session=_id(10 + n), amount=100 - n, at=now)
    path = f"/v1/workgroups/{bill.meter.desk.wg}/charges"
    page = _get(bill, f"{path}?limit=2")
    assert (_tasks(page), page["next_before"]) == ([tasks[2], tasks[1]], paging.encode_cursor(paging.EPOCH, tasks[1]))
    older = _get(bill, f"{path}?limit=2&before={page['next_before']}")
    assert (_tasks(older), older["next_before"]) == ([tasks[0]], None)


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "limit=x",
        "before=%3F",
        f"before={paging.encode_cursor(paging.EPOCH, 'not-an-id')}",
    ],
)
def test_malformed_paging_is_refused(bill: Bill, charged_task: tuple[str, list[str]], query: str) -> None:
    task, _sessions = charged_task
    for path in (f"/v1/tasks/{task}/charges", f"/v1/workgroups/{bill.meter.desk.wg}/charges"):
        # the contract check of the test client would refuse the request before the hub sees it
        response = bill.scene.reader.browser.get(f"{path}?{query}", extensions={"contract": False})
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid"), (path, query)


def test_org_billing_says_when_the_credit_history_is_cut(bill: Bill, monkeypatch: pytest.MonkeyPatch) -> None:
    path = f"/v1/orgs/{bill.org}/billing"
    with bill.hub.pool.connection() as connection, connection.transaction():
        credit.grant(connection, bill.org, Decimal(2), "welcome", bill.hub.clock())
    body = _get(bill, path)
    assert (len(body["credits"]), "credits_truncated" in body) == (2, False)
    monkeypatch.setattr(routes, "CREDITS", 1)
    body = _get(bill, path)
    assert ([entry["kind"] for entry in body["credits"]], body["credits_truncated"]) == (["grant"], True)
