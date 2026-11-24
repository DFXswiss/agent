"""U-HUB-monitoring (series): `GET /v1/monitoring/series` - raw and rollup sources, bucket aggregation, step rules,
validation and the rights of the overview per entity (03-api.md section 4.11; R-7.2, R-7.3, R-7.6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from agent_hub.crypto.encoding import format_time
from agent_hub.monitoring.rollup import roll_up
from tests.platform.hub.monitoring.support import Watch, insert_sample
from tests.platform.hub.orgs.support import new_id


def params(kind: str, entity: str, metric: str, *, start: datetime, end: datetime, step: int) -> dict[str, str]:
    return {
        "entity_kind": kind,
        "entity_id": entity,
        "metric": metric,
        "from": format_time(start),
        "to": format_time(end),
        "step": str(step),
    }


def points(watch: Watch, actor: Any, query: dict[str, str]) -> list[list[Any]]:
    response = actor.browser.get("/v1/monitoring/series", params=query)
    assert response.status_code == 200, response.text
    return list(response.json()["points"])


def base(watch: Watch) -> datetime:
    """A minute boundary 10 minutes before the hub clock."""
    now = watch.now()
    return now.replace(second=0, microsecond=0) - timedelta(minutes=10)


def test_raw_buckets_average_gauges_and_keep_the_newest_counter(watch: Watch) -> None:
    start = base(watch)
    device = watch.own
    for offset, cpu, rx, reachable in (
        (0, 0.5, 10, True),
        (30, 1.5, 40, False),
        (59.999, None, None, True),
        (120, 2.0, 90, True),
    ):
        insert_sample(
            watch.hub, "device", device.id, start + timedelta(seconds=offset), device.id,
            cpu_ratio=cpu, net_rx_bytes=rx, reachable=reachable,
        )  # fmt: skip
    insert_sample(watch.hub, "device", device.id, start + timedelta(seconds=180), device.id, cpu_ratio=9.0)
    end = start + timedelta(seconds=180)
    query = params("device", device.id, "cpu_ratio", start=start, end=end, step=60)
    assert points(watch, watch.member, query) == [
        [format_time(start), 1.0],
        [format_time(start + timedelta(seconds=60)), None],
        [format_time(start + timedelta(seconds=120)), 2.0],
    ]
    assert [value for _, value in points(watch, watch.member, {**query, "metric": "net_rx_bytes"})] == [40, None, 90]
    assert [value for _, value in points(watch, watch.member, {**query, "metric": "reachable"})] == [1, None, 1]
    uneven = points(watch, watch.member, {**query, "to": format_time(end + timedelta(seconds=1))})
    assert len(uneven) == 4
    assert uneven[3] == [format_time(start + timedelta(seconds=180)), 9.0]


def test_rollups_serve_ranges_beyond_the_raw_retention(watch: Watch) -> None:
    now = watch.now()
    old = (now - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    vm = watch.vm(watch.own, watch.mine.default_wg)
    for minutes, cpu, tx in ((0, 1.0, 5), (1, 3.0, 7), (5, 2.0, 11), (11, 4.0, None)):
        insert_sample(
            watch.hub, "vm", vm, old + timedelta(minutes=minutes), watch.own.id, cpu_ratio=cpu, net_tx_bytes=tx
        )
    with watch.hub.pool.connection() as connection:
        roll_up(connection, old + timedelta(hours=1))
    query = params("vm", vm, "cpu_ratio", start=old, end=old + timedelta(minutes=15), step=600)
    # bucket 0: rollups 00:00 (2 samples, avg 2.0) and 00:05 (1 sample, 2.0) -> 2.0; bucket 1: 00:10 -> 4.0
    assert points(watch, watch.admin, query) == [
        [format_time(old), 2.0],
        [format_time(old + timedelta(minutes=10)), 4.0],
    ]
    sent = points(watch, watch.admin, {**query, "metric": "net_tx_bytes"})
    assert [value for _, value in sent] == [11, None]
    query = params("vm", vm, "cpu_ratio", start=old, end=old + timedelta(minutes=10), step=300)
    assert [value for _, value in points(watch, watch.admin, query)] == [2.0, 2.0]


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        ({"entity_kind": "pod"}, "/entity_kind"),
        ({"entity_kind": None}, "/entity_kind"),
        ({"entity_id": "vm-0a1b2c3d"}, "/entity_id"),
        ({"entity_id": None}, "/entity_id"),
        ({"entity_id": "01930014-0000-7000-8000-00000000001A"}, "/entity_id"),
        ({"entity_id": "not-a-uuid"}, "/entity_id"),
        ({"entity_kind": "vm"}, "/entity_id"),
        ({"metric": "state"}, "/metric"),
        ({"metric": None}, "/metric"),
        ({"from": "2026-11-21T12:00:00Z"}, "/from"),
        ({"from": None}, "/from"),
        ({"to": "yesterday"}, "/to"),
        ({"step": None}, "/step"),
        ({"step": "015"}, "/step"),
        ({"step": "14"}, "/step"),
        ({"step": "1e3"}, "/step"),
    ],
)
def test_series_validation(watch: Watch, change: dict[str, str | None], pointer: str) -> None:
    query: dict[str, str | None] = {
        **params("device", watch.own.id, "cpu_ratio", start=base(watch), end=watch.now(), step=60),
        **change,
    }
    sent = {name: value for name, value in query.items() if value is not None}
    response = watch.admin.browser.get("/v1/monitoring/series", params=sent, extensions={"contract": False})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": [pointer]}},
    )


def test_step_and_range_rules(watch: Watch) -> None:
    now = watch.now()
    device = watch.own.id

    def status(start: datetime, end: datetime, step: int) -> int:
        query = params("device", device, "cpu_ratio", start=start, end=end, step=step)
        return int(
            watch.admin.browser.get("/v1/monitoring/series", params=query, extensions={"contract": False}).status_code
        )

    raw_start = now - timedelta(hours=48)
    assert status(raw_start, now, 15) == 200
    assert status(raw_start, raw_start + timedelta(seconds=15 * 20000), 15) == 200
    assert status(raw_start, raw_start + timedelta(seconds=15 * 20000 + 1), 15) == 400
    assert status(now - timedelta(minutes=1), now - timedelta(minutes=1), 15) == 400
    assert status(now, now - timedelta(minutes=1), 15) == 400
    old = now - timedelta(hours=48, milliseconds=1)
    assert status(old, now, 299) == 400
    assert status(old, now, 300) == 200
    assert status(now - timedelta(days=90), now, 3600) == 200


def test_series_rights_follow_the_overview(watch: Watch) -> None:
    start, end = base(watch), watch.now()
    hidden_vm = watch.vm(watch.own, watch.hidden)
    pool_vm = watch.vm(watch.own, None, "pool")
    other_vm = watch.vm(watch.shared, watch.other.default_wg)
    _, session = watch.session(watch.own, watch.mine.default_wg)
    _, other_session = watch.session(watch.shared, watch.other.default_wg, watch.other)
    outsider = watch.world.device()

    def status(actor: Any, kind: str, entity: str) -> int:
        query = params(kind, entity, "mem_used_bytes", start=start, end=end, step=60)
        return int(actor.browser.get("/v1/monitoring/series", params=query).status_code)

    assert status(watch.member, "device", watch.own.id) == 200
    assert status(watch.member, "device", watch.shared.id) == 200
    assert status(watch.other.founder, "device", watch.shared.id) == 200
    assert status(watch.other.founder, "device", watch.own.id) == 404
    assert status(watch.member, "device", outsider.id) == 404
    assert status(watch.member, "device", new_id()) == 404
    assert status(watch.member, "session", session) == 200
    assert status(watch.member, "session", other_session) == 404
    assert status(watch.member, "session", new_id()) == 404
    assert status(watch.member, "vm", hidden_vm) == 404
    assert status(watch.admin, "vm", hidden_vm) == 200
    assert status(watch.admin, "vm", pool_vm) == 404
    assert status(watch.admin, "vm", other_vm) == 404
    assert status(watch.other.founder, "vm", other_vm) == 200
    assert status(watch.admin, "vm", "vm-00000000") == 404
    watch.desk.execute(
        "UPDATE hub.org_member SET removed_seq = 99 WHERE org_id = %s AND principal_id = %s",
        watch.mine.id,
        watch.member.id,
    )
    assert status(watch.member, "device", watch.own.id) == 404
    assert status(watch.member, "session", session) == 404
