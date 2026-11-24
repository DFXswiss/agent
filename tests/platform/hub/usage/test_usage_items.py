"""U-HUB-usage (usage items): `usage_counter += delta`, the platform spend per org and UTC day, the live `session` frame
with the counters, replays without double counting and the rights of the item (03-api.md section 6.1, 02-data-model.md
section 2.8; R-P.13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_hub.stream.publish import MemoryPublisher
from agent_hub.usage.outbox import platform_work
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.outbox.support import item, usage_body
from tests.platform.hub.tasks.support import frames
from tests.platform.hub.usage.support import Meter

COUNTERS = (
    "requests, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, thinking_tokens, cost_micro_usd,"
    " cost_basis, context_used_tokens, context_window_tokens, last_provider, updated_at"
)


def test_usage_adds_the_delta_and_publishes_the_counters(meter: Meter, published: MemoryPublisher) -> None:
    _task, session = meter.session()
    published.clear()
    assert meter.usage(session) == ("ok", None)
    second = usage_body(session, requests=2, cost_micro_usd=500)
    second.update(cost_basis="list_price", context_used_tokens=5000, last_provider=None)
    assert meter.status("usage", second) == ("ok", None)
    now = meter.hub.clock()
    assert meter.rows(f"SELECT {COUNTERS} FROM hub.usage_counter WHERE session_id = %s", session) == [
        (3, 200, 40, 10, 6, 14, 2000, "list_price", 5000, 200000, None, now)
    ]
    # task readers see the counters live: one `session` frame per item, for the org, with row and newest transition
    sent = frames(published, "session")
    assert [frame["usage"]["requests"] for frame in sent] == [1, 3]
    assert sent[-1]["session"]["session_id"] == session
    assert sent[-1]["usage"]["cost_micro_usd"] == 2000
    assert {str(entry.audience.id) for entry in published.items() if entry.frame == "session"} == {meter.desk.org}


def test_usage_rights(meter: Meter) -> None:
    _task, foreign = meter.session(device=meter.scene.other_device)
    assert meter.usage(foreign) == ("rejected", "forbidden")
    assert meter.usage(new_id()) == ("rejected", "not_found")
    assert meter.rows("SELECT count(*) FROM hub.usage_counter") == [(0,)]
    assert meter.spent() == []
    assert [row["item_kind"] for row in meter.outbox.rejected_audit()] == ["usage", "usage"]


def test_replays_count_once(meter: Meter) -> None:
    _task, session = meter.session()
    first = item(1, "usage", usage_body(session, cost_micro_usd=700))
    second = item(2, "usage", usage_body(session, cost_micro_usd=300))
    assert meter.outbox.statuses(first, second) == [(1, "ok", None), (2, "ok", None)]
    # the runner resends the same items (lost answer): replayed, no handler runs again
    assert meter.outbox.statuses(first, second) == [(1, "replayed", None), (2, "replayed", None)]
    assert meter.outbox.statuses(second, item(3, "usage", usage_body(session, cost_micro_usd=1))) == [
        (2, "replayed", None),
        (3, "ok", None),
    ]
    assert meter.rows("SELECT requests, cost_micro_usd FROM hub.usage_counter WHERE session_id = %s", session) == [
        (3, 1001)
    ]
    assert meter.spent() == [("2026-11-21", 1001)]


def test_the_spend_belongs_to_the_utc_day_of_the_hub_clock(meter: Meter) -> None:
    _task, session = meter.session()
    meter.until(datetime(2026, 11, 21, 23, 59, 59, 500000, tzinfo=UTC))
    assert meter.usage(session, cost_micro_usd=400_000) == ("ok", None)  # received 23:59:59.800
    assert meter.usage(session, cost_micro_usd=100_000) == ("ok", None)  # received 00:00:00.100 of the next day
    assert meter.spent() == [("2026-11-21", 400_000), ("2026-11-22", 100_000)]
    # the device clock does not matter: a body has no time, the receive time decides
    meter.until(datetime(2026, 11, 22, 23, 59, 59, 999000, tzinfo=UTC) - timedelta(seconds=0.3))
    assert meter.usage(session, cost_micro_usd=1) == ("ok", None)
    assert meter.spent("2026-11-22") == [("2026-11-22", 100_001)]


@pytest.mark.parametrize(
    ("access", "basis", "counted"),
    [
        ("platform", "provider", True),
        ("platform", "list_price", True),
        ("api_key", "list_price", False),
        ("subscription", "subscription_equivalent", False),
        ("api_key", "provider", False),
        (None, "provider", True),
        (None, "list_price", False),
        (None, "subscription_equivalent", False),
    ],
)
def test_only_platform_work_spends_the_platform_budget(
    meter: Meter, access: str | None, basis: str, counted: bool
) -> None:
    assert platform_work(access, basis) is counted
    _task, session = meter.session(access)
    body = usage_body(session, cost_micro_usd=250)
    body["cost_basis"] = basis
    assert meter.status("usage", body) == ("ok", None)
    assert meter.spent() == ([("2026-11-21", 250)] if counted else [])
    assert meter.rows("SELECT cost_micro_usd FROM hub.usage_counter WHERE session_id = %s", session) == [(250,)]


def test_usage_items_and_frames_follow_the_contracts(meter: Meter, published: MemoryPublisher) -> None:
    _task, session = meter.session()
    body = usage_body(session)
    contracts_registry().validate("outbox/usage.json", body)
    assert meter.status("usage", body) == ("ok", None)
    for frame in frames(published, "session"):
        contracts_registry().validate("ws/sse/session.json", frame)
