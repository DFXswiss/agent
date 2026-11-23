"""U-HUB-monitoring (work VMs, KOR-29): work VMs appear in the overview under their device and session, their series
and live samples follow the rights of their session's workgroup, and they roll up like every entity (agent
docs/platform/spec/13-work-vm.md section 10; 03-api.md section 4.11)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.crypto.encoding import format_time
from agent_hub.monitoring.read import parse_series
from agent_hub.monitoring.rollup import roll_up
from agent_hub.monitoring.stream import publish_metrics
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.monitoring.support import Watch
from tests.platform.hub.monitoring.test_stream import entities, metrics

WVM = "wvm-0a1b2c3d"
HIDDEN_WVM = "wvm-11111111"
OTHER_WVM = "wvm-22222222"


def overview(watch: Watch, actor: Any, org: str | None = None) -> dict[str, Any]:
    response = actor.browser.get("/v1/monitoring/overview", params={"org": org or watch.mine.id})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_the_overview_lists_the_work_vms_of_visible_sessions(watch: Watch) -> None:
    default = watch.mine.default_wg
    task, session = watch.session(watch.own, default)
    _, hidden_session = watch.session(watch.own, watch.hidden)
    _, other_session = watch.session(watch.shared, watch.other.default_wg, watch.other)
    _, gone_session = watch.session(watch.own, default, state="stopped")
    watch.desk.execute(
        "UPDATE hub.session SET ended_at = %s WHERE id = %s", watch.now() - timedelta(minutes=16), gone_session
    )
    watch.sample("workvm", WVM, watch.own, age=90, session=session, cpu_ratio=1.0)
    watch.sample("workvm", WVM, watch.own, age=5, session=session, cpu_ratio=3.5, state="running")
    watch.sample("workvm", "wvm-33333333", watch.own, age=70, session=session)  # torn down a minute ago: stale
    watch.sample("workvm", "wvm-44444444", watch.own, age=16 * 60, session=session)  # torn down long ago
    watch.sample("workvm", HIDDEN_WVM, watch.own, session=hidden_session)
    watch.sample("workvm", OTHER_WVM, watch.shared, session=other_session)
    watch.sample("workvm", "wvm-55555555", watch.own, session=gone_session)

    member = overview(watch, watch.member)
    assert [item["workvm_id"] for item in member["workvms"]] == [WVM, "wvm-33333333"]
    found = member["workvms"][0]
    assert found == {
        "workvm_id": WVM,
        "device_id": watch.own.id,
        "session_id": session,
        "task_id": task,
        "wg_id": default,
        "state": "running",
        "latest": found["latest"],
        "stale": False,
    }
    assert found["latest"]["cpu_ratio"] == 3.5
    assert member["workvms"][1]["stale"] is True
    admin = overview(watch, watch.admin)
    assert {item["workvm_id"] for item in admin["workvms"]} == {WVM, "wvm-33333333", HIDDEN_WVM}
    other = overview(watch, watch.other.founder, watch.other.id)
    assert [item["workvm_id"] for item in other["workvms"]] == [OTHER_WVM]


def test_work_vm_series_and_rights(watch: Watch) -> None:
    _, session = watch.session(watch.own, watch.mine.default_wg)
    _, hidden_session = watch.session(watch.own, watch.hidden)
    start = watch.now().replace(second=0, microsecond=0) - timedelta(minutes=10)
    watch.sample("workvm", WVM, watch.own, age=(watch.now() - start).total_seconds() - 1, session=session, cpu_ratio=2)
    watch.sample("workvm", HIDDEN_WVM, watch.own, session=hidden_session)

    def query(entity: str) -> dict[str, str]:
        return {
            "entity_kind": "workvm",
            "entity_id": entity,
            "metric": "cpu_ratio",
            "from": format_time(start),
            "to": format_time(start + timedelta(minutes=2)),
            "step": "60",
        }

    response = watch.member.browser.get("/v1/monitoring/series", params=query(WVM))
    assert response.status_code == 200, response.text
    assert [value for _, value in response.json()["points"]] == [2.0, None]
    assert watch.member.browser.get("/v1/monitoring/series", params=query(HIDDEN_WVM)).status_code == 404
    assert watch.admin.browser.get("/v1/monitoring/series", params=query(HIDDEN_WVM)).status_code == 200
    assert watch.admin.browser.get("/v1/monitoring/series", params=query("wvm-99999999")).status_code == 404
    assert watch.other.founder.browser.get("/v1/monitoring/series", params=query(WVM)).status_code == 404
    bad = watch.admin.browser.get(
        "/v1/monitoring/series", params={**query(WVM), "entity_id": "vm-0a1b2c3d"}, extensions={"contract": False}
    )
    assert bad.json() == {"error": "request_invalid", "detail": {"pointers": ["/entity_id"]}}
    assert parse_series(query(WVM), watch.now()).entity_kind == "workvm"


def test_live_work_vm_samples_reach_the_readers_of_the_session(watch: Watch, published: MemoryPublisher) -> None:
    _, session = watch.session(watch.own, watch.mine.default_wg)
    _, hidden_session = watch.session(watch.own, watch.hidden)
    watch.sample("workvm", WVM, watch.own, session=session)
    watch.sample("workvm", HIDDEN_WVM, watch.own, session=hidden_session)
    assert publish_metrics(watch.hub.app.state.hub) == 2
    items = metrics(published)
    assert entities(items, watch.mine.id, watch.mine.default_wg) == {WVM}
    assert entities(items, watch.mine.id, watch.hidden) == {HIDDEN_WVM}
    assert entities(items, watch.mine.id, None) == set()


def test_work_vm_samples_roll_up(watch: Watch) -> None:
    _, session = watch.session(watch.own, watch.mine.default_wg)
    watch.sample("workvm", WVM, watch.own, age=30, session=session, cpu_ratio=1.0)
    watch.sample("workvm", WVM, watch.own, age=20, session=session, cpu_ratio=3.0)
    with watch.hub.pool.connection() as connection:
        roll_up(connection, watch.now())
        rows = connection.execute(
            "SELECT entity_kind, sum(samples) FROM hub.metric_rollup WHERE entity_id = %s GROUP BY entity_kind", (WVM,)
        ).fetchall()
    assert rows == [("workvm", 2)]
