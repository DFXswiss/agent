"""U-HUB-telemetry: the anonymous UI health report `POST /v1/telemetry/ui` and its read by platform operators
`GET /v1/platform/ui-health` (ADR-086 section 5, 03-api.md section 4.11, 08-monitoring.md section 6.1): daily counters
per version and view type only, clamped values, unknown view types and versions the hub does not serve as `other`,
a lock per day, 90 days kept, closed bodies, the anonymous rate limit and the right `platform:inviter`."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

import pytest

from agent_hub import __version__
from agent_hub.http.limits import ANONYMOUS_PER_MINUTE
from agent_hub.registry import JOBS, concat_sequences
from agent_hub.telemetry import jobs as telemetry_jobs
from agent_hub.telemetry import store
from agent_hub.telemetry.views import LIMITS
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.platform.conftest import Platform

Json = dict[str, Any]


def report(version: str = "0.1.0", /, **views: tuple[int, int, int]) -> Json:
    return {
        "version": version,
        "views": {name: {"count": c, "blocked_ms": b, "max_bucket": m} for name, (c, b, m) in views.items()},
    }


def send(hub: HubHarness, body: Any, *, check: bool = True) -> Any:
    return hub.anon.post("/v1/telemetry/ui", json=body, extensions={"contract": check})


def stored(hub: HubHarness) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(
            connection.execute(
                "SELECT day::text, version, view, reports, blocks, blocked_ms, max_bucket FROM hub.ui_health_daily"
                " ORDER BY day, version, view"
            ).fetchall()
        )


def inviter(plat: Platform, hub: HubHarness) -> SignedClient:
    client = hub.browser()
    assert client.key is not None
    plat.append("role_grant", subject=str(client.key.principal.principal_id), role="inviter")
    return client


def test_reports_add_up_per_day_version_and_view(hub: HubHarness) -> None:
    response = send(hub, report(**{"task.chat": (3, 1450, 2), "monitoring": (1, 230, 0)}))
    assert (response.status_code, response.json()) == (200, {})
    assert send(hub, report(**{"task.chat": (2, 600, 1)})).status_code == 200
    hub.clock.advance(86400)
    assert send(hub, report(**{"task.chat": (1, 40000, 6)})).status_code == 200
    assert stored(hub) == [
        ("2026-11-21", "0.1.0", "monitoring", 1, 1, 230, 0),
        ("2026-11-21", "0.1.0", "task.chat", 2, 5, 2050, 2),
        ("2026-11-22", "0.1.0", "task.chat", 1, 1, 40000, 6),
    ]


def test_unknown_views_count_as_other_and_values_are_clamped(hub: HubHarness) -> None:
    huge = 2**53 - 1
    body = report(**{"task.newtab": (2, 700, 1), "brand-new": (1, 300, 9), "other": (1, 200, 0), "home": (huge,) * 3})
    assert send(hub, body).status_code == 200
    assert stored(hub) == [
        ("2026-11-21", "0.1.0", "home", 1, LIMITS["count"], LIMITS["blocked_ms"], LIMITS["max_bucket"]),
        ("2026-11-21", "0.1.0", "other", 1, 4, 1200, LIMITS["max_bucket"]),
    ]
    assert send(hub, {"version": "0.1.0", "views": {}}).status_code == 200
    assert len(stored(hub)) == 2


def test_only_the_served_version_is_kept_by_name(hub: HubHarness) -> None:
    """Review (KOR-35): a version the hub does not serve - an older tab, an invented one - counts as `other`, so
    anonymous reports cannot fill a day with versions."""
    assert frozenset({__version__}) == store.SERVED
    for version in (__version__, "0.0.9", "9.9.9", "other", __version__):
        assert send(hub, report(version, home=(1, 300, 1))).status_code == 200
    assert [(row[1], row[3]) for row in stored(hub)] == [(__version__, 2), ("other", 3)]


def test_the_reports_of_a_day_wait_for_its_lock(hub: HubHarness) -> None:
    """The count-then-insert of a day runs under a transaction-scoped advisory lock of that day."""
    day = hub.clock().date()
    with hub.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s, %s)", (store.LOCK_ID, day.toordinal()))
        sent = threading.Thread(target=lambda: send(hub, report(home=(1, 300, 1))))
        sent.start()
        sent.join(0.5)
        assert sent.is_alive()  # waits for the day's lock
        assert stored(hub) == []
        holder.execute("SELECT pg_advisory_unlock(%s, %s)", (store.LOCK_ID, day.toordinal()))
    sent.join(10)
    assert [row[3] for row in stored(hub)] == [1]


def test_counters_older_than_90_days_are_purged(hub: HubHarness) -> None:
    assert [job.name for job in telemetry_jobs.JOBS] == ["telemetry.ui_health_purge"]
    assert "telemetry.ui_health_purge" in [job.name for job in concat_sequences(JOBS)]
    assert send(hub, report(home=(1, 300, 1))).status_code == 200
    hub.clock.advance(86400 * (store.RETENTION_DAYS - 1))
    assert send(hub, report(home=(1, 300, 1))).status_code == 200
    context = hub.app.state.hub
    assert telemetry_jobs.purge_pass(context) == 0  # the first day is 89 days old
    hub.clock.advance(86400)
    assert telemetry_jobs.purge_pass(context) == 1
    assert [row[0] for row in stored(hub)] == [(hub.clock().date() - timedelta(days=1)).isoformat()]


@pytest.mark.parametrize(
    "body",
    [
        {"version": "0.1.0"},
        {"views": {}},
        {"version": "0.1.0", "views": {}, "user": "x"},
        {"version": "-1 beta", "views": {}},
        {"version": "0.1.0", "views": {"Home": {"count": 1, "blocked_ms": 300, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"task.chat.x": {"count": 1, "blocked_ms": 300, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"a" * 65: {"count": 1, "blocked_ms": 300, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"home": {"count": -1, "blocked_ms": 300, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"home": {"count": True, "blocked_ms": 300, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"home": {"count": 1, "blocked_ms": 2.5, "max_bucket": 0}}},
        {"version": "0.1.0", "views": {"home": {"count": 1, "blocked_ms": 300}}},
        {"version": "0.1.0", "views": {"home": {"count": 1, "blocked_ms": 300, "max_bucket": 0, "task": "x"}}},
        {"version": "0.1.0", "views": {"home": {"count": 2**53, "blocked_ms": 300, "max_bucket": 0}}},
        {
            "version": "0.1.0",
            "views": {
                f"v-{chr(97 + n // 26)}{chr(97 + n % 26)}": {"count": 1, "blocked_ms": 300, "max_bucket": 0}
                for n in range(65)
            },
        },
        [],
    ],
)
def test_malformed_reports_are_refused(hub: HubHarness, body: Any) -> None:
    response = send(hub, body, check=False)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")
    assert stored(hub) == []


def test_a_report_may_hold_64_views_within_8_kib(hub: HubHarness) -> None:
    views = {f"v-{chr(97 + n // 26)}{chr(97 + n % 26)}": (1, 300, 0) for n in range(64)}
    assert send(hub, report(**views)).status_code == 200
    assert stored(hub) == [("2026-11-21", "0.1.0", "other", 1, 64, 19200, 0)]
    oversized = json.dumps({**report(), "padding": "x" * LIMITS["body_bytes"]})
    response = hub.anon.post(
        "/v1/telemetry/ui",
        content=oversized,
        headers={"Content-Type": "application/json"},
        extensions={"contract": False},
    )
    assert (response.status_code, response.json()["error"]) == (413, "size_exceeded")


def test_the_anonymous_rate_limit_applies(hub: HubHarness) -> None:
    for _ in range(ANONYMOUS_PER_MINUTE):
        assert send(hub, report(home=(1, 300, 0))).status_code == 200
    response = send(hub, report(home=(1, 300, 0)))
    assert (response.status_code, response.json()["error"]) == (429, "rate_limited")
    assert stored(hub)[0][3] == ANONYMOUS_PER_MINUTE


def test_operators_read_the_last_days(plat: Platform, hub: HubHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    client = inviter(plat, hub)
    assert client.get("/v1/platform/ui-health").json() == {"from": "2026-11-15", "rows": []}
    hub.clock.advance(-86400 * 8)
    send(hub, report(home=(1, 300, 0)))
    hub.clock.advance(86400 * 7)
    send(hub, report(home=(2, 500, 1)))
    hub.clock.advance(86400)
    send(hub, report("0.0.8", **{"task.chat": (1, 300, 0)}))
    send(hub, report(**{"task.chat": (1, 300, 0)}))
    rows = client.get("/v1/platform/ui-health").json()
    assert rows == {
        "from": "2026-11-15",
        "rows": [
            {
                "day": "2026-11-21",
                "version": "0.1.0",
                "view": "task.chat",
                "reports": 1,
                "count": 1,
                "blocked_ms": 300,
                "max_bucket": 0,
            },
            {
                "day": "2026-11-21",
                "version": "other",
                "view": "task.chat",
                "reports": 1,
                "count": 1,
                "blocked_ms": 300,
                "max_bucket": 0,
            },
            {
                "day": "2026-11-20",
                "version": "0.1.0",
                "view": "home",
                "reports": 1,
                "count": 2,
                "blocked_ms": 500,
                "max_bucket": 1,
            },
        ],
    }
    assert len(client.get("/v1/platform/ui-health", params={"days": "9"}).json()["rows"]) == 4
    assert len(client.get("/v1/platform/ui-health", params={"days": "1"}).json()["rows"]) == 2
    monkeypatch.setattr(store, "ROWS_MAX", 1)
    capped = client.get("/v1/platform/ui-health").json()
    assert (len(capped["rows"]), capped["truncated"]) == (1, True)
    hub.flush_read_log()
    # one row per distinct query (no parameter, days=9, days=1); the repeats are counted (ADR-063 section 3)
    assert plat.query("SELECT count(*) FROM hub.audit WHERE action = 'read.platform.ui_health'") == [(3,)]


def test_only_platform_operators_read(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    response = hub.browser().get("/v1/platform/ui-health")
    assert (response.status_code, response.json()["error"]) == (403, "forbidden")
    assert hub.anon.get("/v1/platform/ui-health").status_code == 401
    client = inviter(plat, hub)
    for days in ("0", "91", "x"):
        response = client.get("/v1/platform/ui-health", params={"days": days}, extensions={"contract": False})
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid"), days


def test_the_report_holds_no_identity() -> None:
    """The table and the request carry no field that could name a person, org, device, address or object."""
    schema = json.loads((CONTRACTS / "api" / "telemetry-ui.request.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]) == {"version", "views"}
    assert set(schema["$defs"]["view_counts"]["properties"]) == {"count", "blocked_ms", "max_bucket"}


def test_days_reach_back_from_today(hub: HubHarness) -> None:
    today = hub.clock().date()
    with hub.pool.connection() as connection:
        assert store.rows(connection, today, 3)["from"] == (today - timedelta(days=2)).isoformat()
