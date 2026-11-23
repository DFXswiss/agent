"""U-HUB-usage (budget): `platform_budget` of `GET /v1/device/state`, the budget hook H4 of the operation registration
(`subagent.request` with an exhausted platform budget is `denied`/`budget_exceeded` and audited), the edge cases
reached / exceeded / unlimited / zero, the configuration binding and its job, and `/v1/config.models` from the model
catalog (03-api.md sections 2, 4.1, 6.1, 8.3, 11; R-P.13, ADR-026, ADR-031)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from agent_hub.config import load_config
from agent_hub.operations import limits
from agent_hub.registry import discover
from agent_hub.usage import binding, budget, jobs
from tests.platform.hub.conftest import (
    TEST_MODEL_CATALOG,
    FixedClock,
    HubDatabase,
    hub_environ,
    start_hub,
)
from tests.platform.hub.tasks.support import audit_rows
from tests.platform.hub.usage.support import SUBAGENT, Meter


def _spend(meter: Meter, micro: int, day: str = "2026-11-21") -> None:
    """Today's spend of the org: `micro` in total, split over both devices."""
    meter.execute("DELETE FROM hub.usage_daily WHERE org_id = %s AND day = %s", meter.desk.org, day)
    for device, part in ((meter.low, micro // 2), (meter.high, micro - micro // 2)):
        meter.execute(
            "INSERT INTO hub.usage_daily (org_id, day, device_id, cost_micro_usd) VALUES (%s, %s, %s, %s)",
            meter.desk.org,
            day,
            device.id,
            part,
        )


def _subagent(meter: Meter, access: str | None = "platform") -> dict[str, Any]:
    """The decision on a `subagent.request` of a new platform session (a new task, so no subagent limit interferes)."""
    _task, session = meter.session(access)
    return meter.ops.decide(session, SUBAGENT)[1]


# --- platform_budget --------------------------------------------------------------------------------------------------


def test_platform_budget_lists_every_assigned_org(meter: Meter) -> None:
    # listed with platform access and also without it (a revocation keeps existing assignments, ADR-026)
    for granted in (True, False):
        meter.platform_access(granted)
        assert meter.state(meter.low)["platform_budget"] == {
            "usd_per_org_day": 2.5,
            "spent_today_by_org": {meter.desk.org: 0},
        }
    # the spend is the sum over the devices of the org (one row per device; `low`/`high` explicitly, as the scene's
    # `device` is `high` whenever both devices got their UUIDv7 in the same millisecond)
    _task, session = meter.session(device=meter.low)
    _task, other = meter.session(device=meter.high)
    assert meter.usage(session, meter.low, cost_micro_usd=1_234_567) == ("ok", None)
    assert meter.usage(other, meter.high, cost_micro_usd=1_000_000) == ("ok", None)
    assert meter.state(meter.high)["platform_budget"]["spent_today_by_org"] == {meter.desk.org: 2.234567}
    assert meter.rows("SELECT count(*) FROM hub.usage_daily") == [(2,)]
    # a new UTC day starts at zero
    meter.until(datetime(2026, 11, 22, 0, 0, 0, 1000, tzinfo=UTC))
    assert meter.state(meter.low)["platform_budget"]["spent_today_by_org"] == {meter.desk.org: 0}
    # a device without an assignment in the org gets no entry for it
    platform_device, _seq = meter.ops.fleet.platform_device()
    assert meter.state(platform_device)["platform_budget"] == {"usd_per_org_day": 2.5, "spent_today_by_org": {}}


@pytest.mark.parametrize("budget_value", [""])
def test_platform_budget_is_null_without_configuration(meter: Meter) -> None:
    assert meter.state(meter.low)["platform_budget"] is None


# --- H4 ---------------------------------------------------------------------------------------------------------------


def test_the_usage_area_registers_the_budget_hook() -> None:
    assert discover(limits.BUDGET_CHECK) == [("usage", budget.CHECK)]
    assert limits.configure() is budget.check


def test_budget_edges_for_subagent_requests(meter: Meter) -> None:
    meter.state(meter.low)  # binds the hub configuration (2.5 USD)
    assert binding.budget_limit() == Decimal("2.5")
    assert _subagent(meter)["state"] == "accepted"
    _spend(meter, 2_499_999)
    assert _subagent(meter)["state"] == "accepted"
    # exactly reached: exhausted
    _spend(meter, 2_500_000)
    assert _subagent(meter) == {"state": "denied", "error_code": "budget_exceeded"}
    _spend(meter, 9_000_000)
    assert _subagent(meter) == {"state": "denied", "error_code": "budget_exceeded"}
    # access not reported yet: counted as platform work
    assert _subagent(meter, None) == {"state": "denied", "error_code": "budget_exceeded"}
    # sessions with their own credentials spend no platform budget (children use the parent's access)
    assert _subagent(meter, "api_key")["state"] == "accepted"
    assert _subagent(meter, "subscription")["state"] == "accepted"
    # yesterday's spend does not count today
    meter.until(datetime(2026, 11, 22, 0, 0, 0, 1000, tzinfo=UTC))
    assert _subagent(meter)["state"] == "accepted"
    denied = [
        row for row in audit_rows(meter.desk, "operation.denied") if row[3].get("error_code") == "budget_exceeded"
    ]
    assert len(denied) == 3
    # GitHub operations cost no model budget
    _spend(meter, 9_000_000, "2026-11-22")
    _task, session = meter.session()
    assert meter.ops.decide(session, "github.comment")[1] == {"state": "accepted"}


def test_spend_through_usage_items_exhausts_the_budget(meter: Meter) -> None:
    meter.state(meter.low)
    _task, session = meter.session()
    assert meter.usage(session, cost_micro_usd=2_000_000) == ("ok", None)
    assert _subagent(meter)["state"] == "accepted"
    assert meter.usage(session, cost_micro_usd=500_000) == ("ok", None)
    assert _subagent(meter) == {"state": "denied", "error_code": "budget_exceeded"}


@pytest.mark.parametrize("budget_value", ["0"])
def test_a_zero_budget_allows_no_platform_work(meter: Meter) -> None:
    meter.state(meter.low)
    assert binding.budget_limit() == Decimal(0)
    assert _subagent(meter) == {"state": "denied", "error_code": "budget_exceeded"}
    assert _subagent(meter, "api_key")["state"] == "accepted"


@pytest.mark.parametrize("budget_value", [""])
def test_an_unlimited_budget_allows_everything(meter: Meter) -> None:
    meter.state(meter.low)
    assert binding.budget_limit() is None
    _spend(meter, 10**12)
    assert _subagent(meter)["state"] == "accepted"


def test_the_hub_binds_its_configuration_before_the_first_request(meter: Meter) -> None:
    """A-12: the app binds the budget before it serves requests (no request of the usage area needed), so the hook
    never answers "unlimited" for lack of a binding; without one it fails closed."""
    assert binding.bound()
    assert binding.budget_limit() == Decimal("2.5")
    _spend(meter, 10**12)
    assert _subagent(meter) == {"state": "denied", "error_code": "budget_exceeded"}
    binding.unbind()
    assert not binding.bound()
    assert binding.online_window() == 30
    _task, session = meter.session("platform")
    unbound = meter.ops.request(session, SUBAGENT)
    response = meter.ops.register(unbound)
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})
    operation = unbound["operation_id"]
    assert meter.rows("SELECT count(*) FROM hub.operation WHERE id = %s", operation) == [(0,)]
    # the job binds the configuration of the process at start and hourly (as every request of the area does)
    assert [(job.name, job.at_start, job.initial_delay_seconds) for job in jobs.JOBS] == [
        ("usage.bind_config", True, jobs.BIND_INTERVAL_SECONDS)
    ]
    jobs.JOBS[0].run(SimpleNamespace(config=meter.hub.config))
    assert binding.budget_limit() == Decimal("2.5")
    # the runner's retry of the same request (same operation ID) is decided once bound
    retried = meter.ops.register(unbound)
    assert (retried.status_code, retried.json()["state"]) == (200, "denied")
    assert meter.rows("SELECT state FROM hub.operation WHERE id = %s", operation) == [("denied",)]
    binding.unbind()
    meter.scene.writer.browser.get("/v1/me/quotas")
    assert binding.budget_limit() == Decimal("2.5")
    other = dataclasses.replace(meter.hub.config, platform_budget_usd_per_org_day=None, device_online_seconds=45)
    binding.bind(other)
    assert (binding.budget_limit(), binding.online_window()) == (None, 45)


def test_budget_helpers() -> None:
    assert budget.within(2_499_999, Decimal("2.5"))
    assert not budget.within(2_500_000, Decimal("2.5"))
    assert budget.within(1, Decimal("0.000002"))
    assert not budget.within(0, Decimal(0))
    assert str(budget.utc_day(datetime.fromisoformat("2026-11-22T00:30:00+01:00"))) == "2026-11-21"


# --- /v1/config.models ------------------------------------------------------------------------------------------------


def test_config_models_are_the_allow_lists_of_the_catalog(
    migrated_db: HubDatabase, hub_pool: ConnectionPool[Any], clock: FixedClock, tmp_path: Path
) -> None:
    catalog = json.loads(TEST_MODEL_CATALOG.read_text(encoding="utf-8"))
    catalog["access"]["platform"]["allowed"] = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash-1017"]
    catalog["access"]["api_key"]["allowed"] = ["claude-haiku-5-5"]
    catalog["access"]["subscription"]["allowed"] = ["claude-fable-5-2", "claude-opus-5-5"]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    config = load_config(hub_environ(migrated_db, AGENT_HUB_MODEL_CATALOG=str(path)))
    for harness in start_hub(config, hub_pool, clock):
        body = harness.anon.get("/v1/config").json()
        assert body["models"] == {kind: catalog["access"][kind]["allowed"] for kind in catalog["access"]}
        assert body["defaults"] == {"platform": catalog["access"]["platform"]["default"]}
