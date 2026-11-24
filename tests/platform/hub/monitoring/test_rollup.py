"""U-HUB-monitoring (rollups): 5-minute rollups of gauges and counters, bucket boundaries, idempotence, late samples,
catching up after an outage and retention on the fake clock (02-data-model.md section 2.8; R-7.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from agent_hub.jobs import Job
from agent_hub.monitoring import jobs
from agent_hub.monitoring.rollup import BUCKET_SECONDS, RollupPass, bucket_start, roll_up
from agent_hub.registry import JOBS, concat_sequences
from tests.platform.hub.conftest import FixedClock, HubHarness
from tests.platform.hub.monitoring.support import insert_sample
from tests.platform.hub.orgs.support import new_id

T0 = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)
DEVICE = new_id()
OTHER_DEVICE = new_id()
ROLLUP_COLUMNS = (
    "device_id, samples, cpu_ratio_avg, cpu_ratio_max, mem_used_bytes_avg, mem_used_bytes_max, free_vm_slots_avg,"
    " free_vm_slots_max, free_mem_bytes_avg, free_mem_bytes_max, net_rx_bytes, net_tx_bytes, uptime_seconds,"
    " reachable, state"
)


def rollups(hub: HubHarness, entity: str = DEVICE) -> dict[datetime, tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        rows = connection.execute(
            f"SELECT ts, {ROLLUP_COLUMNS} FROM hub.metric_rollup WHERE entity_id = %s ORDER BY ts", (entity,)
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def run(hub: HubHarness, now: datetime | None = None) -> RollupPass:
    with hub.pool.connection() as connection:
        return roll_up(connection, now or hub.clock())


def sample(hub: HubHarness, seconds: float, **values: Any) -> None:
    insert_sample(hub, "device", DEVICE, T0 + timedelta(seconds=seconds), DEVICE, **values)


def count(hub: HubHarness, table: str) -> int:
    with hub.pool.connection() as connection:
        row = connection.execute(f"SELECT count(*) FROM hub.{table}").fetchone()
    assert row is not None
    return int(row[0])


def test_bucket_start() -> None:
    assert bucket_start(T0 + timedelta(seconds=299, microseconds=999999)) == T0
    assert bucket_start(T0 + timedelta(seconds=300)) == T0 + timedelta(seconds=300)
    assert BUCKET_SECONDS == 300


def test_gauges_average_and_maximum_counters_keep_the_newest_value(hub: HubHarness) -> None:
    sample(hub, 0, cpu_ratio=0.5, mem_used_bytes=1000, free_vm_slots=2, free_mem_bytes=10, net_rx_bytes=5)
    sample(hub, 100, cpu_ratio=1.5, mem_used_bytes=2001, free_vm_slots=5, free_mem_bytes=None, net_rx_bytes=50)
    sample(hub, 299.999, cpu_ratio=None, mem_used_bytes=None, reachable=False, state="stopping", net_rx_bytes=None)
    sample(hub, 300, cpu_ratio=4.0, net_rx_bytes=1, uptime_seconds=None, net_tx_bytes=None)
    insert_sample(hub, "device", DEVICE, T0 + timedelta(seconds=600), OTHER_DEVICE, cpu_ratio=0.0)
    insert_sample(hub, "vm", "vm-0a1b2c3d", T0, DEVICE, cpu_ratio=7.0)
    result = run(hub)
    assert result == RollupPass(rolled_up=4, samples_pruned=0, rollups_pruned=0)
    found = rollups(hub)
    # mem_used_bytes: round(avg(1000, 2001)) = 1501 (PostgreSQL rounds half away from zero)
    assert found[T0] == (UUID(DEVICE), 3, 1.0, 1.5, 1501, 2001, 3.5, 5, 10, 10, 50, 200, 60, False, "stopping")
    second = found[T0 + timedelta(seconds=300)]
    assert second[1:4] == (1, 4.0, 4.0)
    assert second[10:13] == (1, None, None)
    assert found[T0 + timedelta(seconds=600)][0] == UUID(OTHER_DEVICE)
    assert rollups(hub, "vm-0a1b2c3d")[T0][2] == 7.0


def test_a_pass_is_idempotent(hub: HubHarness) -> None:
    sample(hub, 0, cpu_ratio=1.0)
    sample(hub, 60, cpu_ratio=3.0)
    run(hub)
    first = rollups(hub)
    run(hub)
    run(hub)
    assert rollups(hub) == first
    assert count(hub, "metric_rollup") == 1


def test_late_samples_recompute_their_bucket(hub: HubHarness) -> None:
    sample(hub, 0, cpu_ratio=1.0)
    run(hub)
    sample(hub, 30, cpu_ratio=3.0)  # arrives after the pass (outbox backlog)
    run(hub)
    assert rollups(hub)[T0][1:3] == (2, 2.0)


def test_the_cursor_skips_samples_seen_long_ago(hub: HubHarness) -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    sample(hub, 0, cpu_ratio=1.0, received_at=long_ago)
    run(hub)
    # a sample stored long before the cursor (more than the late-commit overlap) is not looked at again
    insert_sample(hub, "device", DEVICE, T0 + timedelta(seconds=10), DEVICE, cpu_ratio=5.0, received_at=long_ago)
    run(hub)
    assert rollups(hub)[T0][1:3] == (1, 1.0)
    # a further sample of the bucket recomputes it from every raw sample
    sample(hub, 20, cpu_ratio=3.0)
    run(hub)
    assert rollups(hub)[T0][1:3] == (3, 3.0)


def test_a_pass_after_an_outage_catches_up(hub: HubHarness) -> None:
    run(hub)
    # two hours of samples stored while no pass ran (the job's process was down)
    for minute in range(0, 120, 5):
        insert_sample(hub, "device", DEVICE, T0 - timedelta(hours=10, minutes=minute), DEVICE)
    result = run(hub)
    assert result.rolled_up == 24
    assert len(rollups(hub)) == 24


def test_retention_on_the_fake_clock(hub: HubHarness, clock: FixedClock) -> None:
    now = clock()
    for age in (timedelta(hours=47), timedelta(hours=48, seconds=1), timedelta(days=89), timedelta(days=91)):
        insert_sample(hub, "device", DEVICE, now - age, DEVICE)
    result = run(hub)
    assert result.samples_pruned == 3
    assert result.rollups_pruned == 1
    assert count(hub, "metric_sample") == 1
    kept = [timedelta(days=89), timedelta(hours=48, seconds=1), timedelta(hours=47)]
    assert sorted(rollups(hub)) == [bucket_start(now - age) for age in kept]
    clock.advance(48 * 3600)
    assert run(hub).samples_pruned == 1
    assert count(hub, "metric_sample") == 0


def test_a_late_sample_never_replaces_a_complete_old_rollup(hub: HubHarness, clock: FixedClock) -> None:
    now = clock()
    old = bucket_start(now - timedelta(hours=60))
    for seconds, cpu in ((0, 1.0), (60, 3.0)):
        insert_sample(hub, "device", DEVICE, old + timedelta(seconds=seconds), DEVICE, cpu_ratio=cpu)
    run(hub, old + timedelta(hours=1))  # rolled up while the raw samples were kept
    run(hub)  # pruned
    assert count(hub, "metric_sample") == 0
    insert_sample(hub, "device", DEVICE, old + timedelta(seconds=120), DEVICE, cpu_ratio=9.0)
    newer = bucket_start(now - timedelta(hours=50))
    insert_sample(hub, "device", DEVICE, newer, DEVICE, cpu_ratio=9.0)
    run(hub)
    found = rollups(hub)
    assert found[old][1:3] == (2, 2.0)
    assert found[newer][1:3] == (1, 9.0)


def test_a_lost_cursor_row_is_recreated(hub: HubHarness) -> None:
    with hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.metric_rollup_cursor")
    sample(hub, 0)
    assert run(hub).rolled_up == 1
    with hub.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM hub.metric_rollup_cursor").fetchone() == (1,)


def test_the_jobs_are_registered_and_run(hub: HubHarness) -> None:
    found = {job.name: job for job in concat_sequences(JOBS) if isinstance(job, Job)}
    assert found["monitoring.rollup"].interval_seconds == 300
    assert found["monitoring.metrics_frames"].interval_seconds == 15
    sample(hub, 0)
    assert jobs.run_rollup(hub.app.state.hub).rolled_up == 1
