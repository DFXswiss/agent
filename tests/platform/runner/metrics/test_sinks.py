"""U-RUN-metrics sinks: textfile renderer (HELP/TYPE, escaping, IDs-only labels, cardinality), atomic write with mode
0644, hub samples against the contract, the 48 h drop rule and the counter registry (08-monitoring.md sections 2-4)."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.crypto.errors import CryptoError
from agent_runner.github import operator as operator_github
from agent_runner.metrics import counters, outbox, textfile
from agent_runner.metrics.device import DeviceValues
from agent_runner.metrics.session import SessionEntry
from agent_runner.metrics.snapshot import DeviceEntry, Snapshot, VmEntry
from agent_runner.model.operator import Summary
from agent_runner.orgs import audit
from agent_runner.store.pool import open_pool
from agent_runner.sync import outbox as sync_outbox
from agent_runner.sync.runtime import SyncRuntime
from agent_runner.vm import tasks as vm_tasks
from agent_runner.vm.capacity import Capacity

from .conftest import FIXTURES

REPO = Path(__file__).parents[4]
STORAGE_KEY = bytes(range(32))
DEVICE_ID = "01930014-0000-7000-8000-000000000014"
WG = "01930002-0000-7000-8000-00000000000a"
S1 = "01a1ee56-5e00-7003-8f1b-340691d99f03"
S2 = "01a1ee56-5e00-7003-8f1b-340691d99f04"
AT = datetime(2026, 11, 21, 12, 0, 0, 250000, tzinfo=UTC)
FULL = {
    "cpu_usage_usec": 2500000,
    "mem_current_bytes": 734003200,
    "mem_max_bytes": 4294967296,
    "tasks_current": 17,
    "io_read_bytes": 1052672,
    "io_write_bytes": 4194304,
    "workspace_bytes": 123,
    "oom_kills": 1,
    "hook_denies": 2,
    "hook_errors": 0,
    "sandbox_denials": 3,
    "egress_denied": 9,
    "model_input_tokens": 1200,
    "model_output_tokens": 300,
    "model_request_bytes": 4096,
    "model_response_bytes": 2048,
}


def snapshot(**overrides: Any) -> Snapshot:
    values = DeviceValues(1.5, 10 * 1024**3, 32 * 1024**3, 100, 1000, 5, 6, 3600, True)
    device = DeviceEntry(
        values=values,
        capacity=Capacity(budget_mib=65536, used_mib=16384, slots_total=8, slots_free=6, pool_ready=1),
        hub_connected=True,
        sync_fatal=False,
        backlog=3,
        counters={"agent_runner_device_verify_failures_total": 2, "agent_runner_device_metrics_dropped_total": 0,
                  "agent_runner_vm_start_failures_total": 1, "agent_runner_operator_github_connects_total": 0,
                  "agent_runner_operator_github_connect_failures_total": 0},
    )  # fmt: skip
    running = VmEntry(
        vm_id="vm-0a1b2c3d",
        wg_id=WG,
        state="running",
        probe_ok=True,
        cpu_ratio=0.75,
        memory_bytes=1500000000,
        memory_limit_bytes=8 * 1024**3,
        disk_used_bytes=3049128 * 1024,
        disk_limit_bytes=20 * 1024**3,
        uptime_seconds=120,
        net_rx_bytes=1000,
        net_tx_bytes=2000,
        sessions={"working": 1, "stopped": 1},
    )
    pool = replace(running, vm_id="vm-0000beef", wg_id=None, state="pool", probe_ok=False, cpu_ratio=None,
                   memory_bytes=None, sessions={})  # fmt: skip
    stopped = replace(running, vm_id="vm-0000dead", state="stopped", probe_ok=None, sessions={}, cpu_ratio=None,
                      memory_bytes=None, disk_used_bytes=None, uptime_seconds=None, net_rx_bytes=None,
                      net_tx_bytes=None)  # fmt: skip
    working = SessionEntry(S1, "vm-0a1b2c3d", "working", 300, 0.5, FULL, True, False)
    ended = SessionEntry(S2, "vm-0a1b2c3d", "stopped", 60, None, {**FULL, "egress_denied": None}, False, True)
    base = Snapshot(wall=1795600000.25, at=AT, device=device, vms=(running, pool, stopped), sessions=(working, ended))
    return replace(base, **overrides)


# -- textfile -----------------------------------------------------------------------------------------------------


def test_rendered_textfile_matches_the_promtool_checked_sample() -> None:
    """`fixtures/agent_runner.prom` is the sample `promtool check metrics` (Prometheus 3.11.0) passed on
    (board report RUN-15; the two gauges of the outbox scope queues were added by KOR-10 in the same form, checked by
    reading); the renderer must keep producing exactly it."""
    text = textfile.render(textfile.metrics_of(snapshot()))
    assert text == (FIXTURES / "agent_runner.prom").read_text(encoding="utf-8")


def test_every_metric_has_help_and_type_and_labels_are_ids() -> None:
    text = textfile.render(textfile.metrics_of(snapshot(), spend={"day": 1.25}))
    names = {line.split()[2] for line in text.splitlines() if line.startswith("# HELP")}
    assert names == {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")}
    assert 'agent_runner_device_model_spend_usd{window="day"} 1.25' in text
    expected = {
        "agent_runner_textfile_timestamp_seconds", "agent_runner_device_up", "agent_runner_device_hub_connected",
        "agent_runner_device_sync_fatal", "agent_runner_device_outbox_backlog", "agent_runner_device_vm_slots",
        "agent_runner_device_outbox_held", "agent_runner_device_outbox_held_scopes",
        "agent_runner_device_vm_slots_free", "agent_runner_device_vm_memory_budget_bytes",
        "agent_runner_device_vm_memory_used_bytes", "agent_runner_device_pool_ready",
        "agent_runner_device_capacity_low", "agent_runner_device_verify_failures_total",
        "agent_runner_device_metrics_dropped_total", "agent_runner_vm_start_failures_total",
        "agent_runner_operator_github_connects_total", "agent_runner_operator_github_connect_failures_total",
        "agent_runner_device_model_spend_usd", "agent_runner_vm_probe_ok", "agent_runner_vm_state",
        "agent_runner_vm_cpu_ratio", "agent_runner_vm_memory_bytes", "agent_runner_vm_memory_limit_bytes",
        "agent_runner_vm_disk_used_bytes", "agent_runner_vm_disk_limit_bytes", "agent_runner_vm_uptime_seconds",
        "agent_runner_vm_network_receive_bytes_total", "agent_runner_vm_network_transmit_bytes_total",
        "agent_runner_vm_sessions", "agent_runner_session_cpu_ratio", "agent_runner_session_memory_bytes",
        "agent_runner_session_memory_limit_bytes", "agent_runner_session_workspace_bytes",
        "agent_runner_session_runtime_seconds", "agent_runner_session_io_read_bytes_total",
        "agent_runner_session_io_write_bytes_total", "agent_runner_session_oom_kills_total",
        "agent_runner_session_hook_denies_total", "agent_runner_session_hook_errors_total",
        "agent_runner_session_sandbox_denials_total", "agent_runner_session_egress_denied_total",
        "agent_runner_session_model_tokens_total",
    }  # fmt: skip
    assert names == expected
    labelled = [line[line.index("{") + 1 : line.index("}")] for line in text.splitlines() if "{" in line]
    labels = {pair.split("=")[0] for inner in labelled for pair in inner.split(",")}
    assert labels == {"vm", "wg", "state", "session", "direction", "window"}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        name = line.split("{")[0].split()[0]
        if name.endswith("_total"):
            assert f"# TYPE {name} counter" in text


def test_without_spend_the_metric_is_left_out_and_unmeasured_values_too() -> None:
    text = textfile.render(textfile.metrics_of(snapshot()))
    assert "agent_runner_device_model_spend_usd" not in text
    assert 'agent_runner_vm_memory_bytes{vm="vm-0000beef"' not in text
    assert 'agent_runner_vm_probe_ok{vm="vm-0000dead"' not in text  # stopped VMs have no probe series
    assert 'agent_runner_vm_probe_ok{vm="vm-0000beef",wg=""} 0' in text
    assert f'agent_runner_session_egress_denied_total{{vm="vm-0a1b2c3d",session="{S2}"}}' not in text
    assert f'agent_runner_session_cpu_ratio{{vm="vm-0a1b2c3d",session="{S2}"}}' not in text


def test_cardinality_only_listed_sessions_have_series() -> None:
    text = textfile.render(textfile.metrics_of(snapshot(sessions=())))
    assert "agent_runner_session_" not in text
    assert text.count("agent_runner_vm_state{") == 3 * 5


def test_render_escapes_label_values_and_help_and_merges_names() -> None:
    first = textfile.Metric("x_total", "counter", "line\\one\nline two")
    first.add((("vm", 'a"b\\c\nd'),), 1)
    first.add((), None)
    second = textfile.Metric("x_total", "gauge", "ignored")
    second.add((("vm", "e"),), 2.5)
    empty = textfile.Metric("empty", "gauge", "never shown")
    flag = textfile.Metric("flag", "gauge", "bool")
    flag.add((), False)
    assert textfile.render([first, second, empty, flag]) == (
        "# HELP x_total line\\\\one\\nline two\n"
        "# TYPE x_total counter\n"
        'x_total{vm="a\\"b\\\\c\\nd"} 1\n'
        'x_total{vm="e"} 2.5\n'
        "# HELP flag bool\n"
        "# TYPE flag gauge\n"
        "flag 0\n"
    )


def test_write_atomic_replaces_the_file_with_mode_0644(tmp_path: Path) -> None:
    os.umask(0o077)
    try:
        target = textfile.write_atomic(tmp_path, "first 1\n")
        assert target == tmp_path / "agent_runner.prom"
        textfile.write_atomic(tmp_path, "second 2\n")
    finally:
        os.umask(0o022)
    assert target.read_text(encoding="utf-8") == "second 2\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert sorted(item.name for item in tmp_path.iterdir()) == ["agent_runner.prom"]
    assert not textfile.TEMP_PREFIX.endswith(".prom")


def test_write_atomic_never_leaves_a_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    textfile.write_atomic(tmp_path, "good 1\n")
    seen: list[str] = []

    def short(descriptor: int, data: bytes) -> int:
        seen.extend(item.name for item in tmp_path.iterdir() if item.name != "agent_runner.prom")
        return 1

    monkeypatch.setattr("agent_runner.metrics.textfile.os.write", short)
    with pytest.raises(OSError, match="short write"):
        textfile.write_atomic(tmp_path, "bad 2\n")
    monkeypatch.undo()
    assert len(seen) == 1
    assert seen[0].startswith(".agent_runner.prom.")
    assert not seen[0].endswith(".prom")

    def refuse(source: str, target: Path) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("agent_runner.metrics.textfile.os.replace", refuse)
    with pytest.raises(PermissionError):
        textfile.write_atomic(tmp_path, "bad 3\n")
    assert sorted(item.name for item in tmp_path.iterdir()) == ["agent_runner.prom"]
    assert (tmp_path / "agent_runner.prom").read_text(encoding="utf-8") == "good 1\n"


# -- hub samples --------------------------------------------------------------------------------------------------


def test_samples_match_the_outbox_contract() -> None:
    snap = snapshot()
    samples = outbox.samples_of(snap, DEVICE_ID, snap.sessions)
    schemas = SchemaSet(str(REPO / "contracts"))
    assert schemas.errors("outbox/metrics.json", {"samples": samples}) == []
    device, running, pool, stopped, working, ended = samples
    assert device["entity_kind"] == "device"
    assert device["ts"] == "2026-11-21T12:00:00.250Z"
    assert (device["free_vm_slots"], device["free_mem_bytes"]) == (6, (65536 - 16384) * 1024 * 1024)
    assert (device["state"], device["reachable"]) == ("active", True)
    assert (running["reachable"], running["state"], running["cpu_ratio"]) == (True, "running", 0.75)
    assert (pool["reachable"], stopped["reachable"]) == (False, False)
    assert (working["net_rx_bytes"], working["net_tx_bytes"], working["disk_used_bytes"]) == (2048, 4096, 123)
    assert (ended["state"], ended["reachable"], ended["uptime_seconds"]) == ("stopped", False, 60)
    fatal = replace(snap, device=replace(snap.device, sync_fatal=True))
    assert outbox.samples_of(fatal, DEVICE_ID, [])[0]["state"] == "sync_fatal"
    unknown = SessionEntry(S1, "vm-0a1b2c3d", "requested", 0, None, None, False, False)
    assert outbox.samples_of(snap, DEVICE_ID, [unknown])[-1]["mem_used_bytes"] is None


class FakePusher:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


def sync_runtime(key: bytes = STORAGE_KEY) -> SyncRuntime:
    return SyncRuntime(client=None, pusher=FakePusher(), ws=None, storage_key=key, wall=time.time)  # type: ignore[arg-type]


def sample(index: int) -> dict[str, Any]:
    return {"entity_id": index}


async def stored(pool: Any) -> tuple[list[list[dict[str, Any]]], list[int]]:
    async with pool.connection() as conn:
        items = await sync_outbox.load_batch(conn, STORAGE_KEY, 0, 1000)
        cur = await conn.execute("SELECT jsonb_array_length(samples) FROM runner.metric_pending ORDER BY id")
        pending = [int(row[0]) for row in await cur.fetchall()]
    return [item.body["samples"] for item in items if item.item_kind == "metrics"], pending


def test_sink_stores_rounds_while_disconnected_and_drops_after_48_h(store_dsn: str) -> None:
    sink, sync = outbox.OutboxSink(), sync_runtime()

    async def body() -> tuple[list[int], tuple[list[list[dict[str, Any]]], list[int]], tuple[Any, list[int]]]:
        pool = await open_pool(store_dsn)
        try:
            results = [
                await sink.offer(pool, sync, 0.0, [sample(1), sample(2)], connected=False, backlog=0),
                await sink.offer(pool, sync, 100.0, [sample(3)], connected=True, backlog=outbox.BACKLOG_LIMIT),
            ]
            held = await stored(pool)
            results.append(
                await sink.offer(pool, sync, outbox.MAX_AGE_S + 50.0, [sample(4)], connected=True, backlog=0)
            )
            return results, held, await stored(pool)
        finally:
            await pool.close()

    results, held, after = asyncio.run(body())
    assert results == [0, 0, 2]
    assert held == ([], [2, 1])
    assert sink.dropped == 2  # the round taken at 0 s was older than 48 h when it could be pushed
    assert after == ([[sample(3), sample(4)]], [])
    assert sync.pusher.notified == 1  # type: ignore[attr-defined]


def test_sink_moves_a_bounded_number_of_samples_per_round(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "MOVE_SAMPLES", 5)
    monkeypatch.setattr(outbox, "MAX_SAMPLES", 2)
    sink, sync = outbox.OutboxSink(), sync_runtime()
    rounds = [[sample(3 * index + offset) for offset in range(3)] for index in range(3)]

    async def body() -> tuple[list[int], tuple[list[list[dict[str, Any]]], list[int]]]:
        pool = await open_pool(store_dsn)
        try:
            moved = [await sink.offer(pool, sync, 1.0, batch, connected=False, backlog=0) for batch in rounds[:2]]
            moved.append(await sink.offer(pool, sync, 2.0, rounds[2], connected=True, backlog=0))
            return moved, await stored(pool)
        finally:
            await pool.close()

    moved, (items, pending) = asyncio.run(body())
    assert moved == [0, 0, 3]  # the first round only: a second would pass MOVE_SAMPLES
    assert items == [rounds[0][:2], rounds[0][2:]]
    assert pending == [3, 3]


def test_sink_without_enrollment_stores_nothing_and_keeps_rows_when_the_outbox_fails(store_dsn: str) -> None:
    sink = outbox.OutboxSink()

    async def body() -> tuple[int, list[int]]:
        pool = await open_pool(store_dsn)
        try:
            skipped = await sink.offer(pool, None, 0.0, [sample(0)], connected=True, backlog=0)
            with pytest.raises(CryptoError):
                await sink.offer(pool, sync_runtime(b"short"), 1.0, [sample(1)], connected=True, backlog=0)
            return skipped, (await stored(pool))[1]
        finally:
            await pool.close()

    skipped, pending = asyncio.run(body())
    assert skipped == 0
    assert pending == [1]


# -- counter registry ---------------------------------------------------------------------------------------------


def test_device_counters_read_their_sources() -> None:
    context = SimpleNamespace()
    assert counters.device_values(context, 4) == {
        "agent_runner_device_verify_failures_total": 0,
        "agent_runner_device_metrics_dropped_total": 4,
        "agent_runner_vm_start_failures_total": 0,
        "agent_runner_operator_github_connects_total": 0,
        "agent_runner_operator_github_connect_failures_total": 0,
    }
    audit.counters(context)[audit.VERIFY_FAILURES] += 3
    vars(context)[vm_tasks.MANAGER_KEY] = SimpleNamespace(env=SimpleNamespace(start_failures=2))
    operator_github.state_for(context).connects += 5
    operator_github.state_for(context).failures += 1
    assert counters.device_values(context, 0) == {
        "agent_runner_device_verify_failures_total": 3,
        "agent_runner_device_metrics_dropped_total": 0,
        "agent_runner_vm_start_failures_total": 2,
        "agent_runner_operator_github_connects_total": 5,
        "agent_runner_operator_github_connect_failures_total": 1,
    }
    assert {counter.name for counter in counters.SESSION_COUNTERS} >= {"agent_runner_session_model_tokens_total"}
    assert (
        Counter(counter.name for counter in counters.SESSION_COUNTERS)["agent_runner_session_model_tokens_total"] == 2
    )


def test_the_operator_subscriptions_are_listed_by_state_and_file_without_tokens() -> None:
    """KOR-23 (ADR-052): only while the operator's pool is configured; the label `file` is the file name."""
    summary = Summary(
        problem=None,
        states={"usable": 2, "blocked": 1, "exhausted": 3, "deferred": 0, "ignored": 1},
        sessions={"b": 2, "a": 0},
        newest_age_s=12.34567,
    )
    text = textfile.render(textfile.metrics_of(snapshot(), operator=summary))
    assert text.startswith(textfile.render(textfile.metrics_of(snapshot())))
    tail = text[len(textfile.render(textfile.metrics_of(snapshot()))) :]
    assert tail.splitlines() == [
        "# HELP agent_runner_operator_subscription_files Token files of the operator's subscriptions by state.",
        "# TYPE agent_runner_operator_subscription_files gauge",
        'agent_runner_operator_subscription_files{state="usable"} 2',
        'agent_runner_operator_subscription_files{state="blocked"} 1',
        'agent_runner_operator_subscription_files{state="exhausted"} 3',
        'agent_runner_operator_subscription_files{state="deferred"} 0',
        'agent_runner_operator_subscription_files{state="ignored"} 1',
        "# HELP agent_runner_operator_subscription_sessions Sessions bound to a token file of the operator's "
        "subscriptions.",
        "# TYPE agent_runner_operator_subscription_sessions gauge",
        'agent_runner_operator_subscription_sessions{file="a"} 0',
        'agent_runner_operator_subscription_sessions{file="b"} 2',
        "# HELP agent_runner_operator_subscription_newest_change_age_seconds Seconds since the newest change of a "
        "token file of the operator's subscriptions.",
        "# TYPE agent_runner_operator_subscription_newest_change_age_seconds gauge",
        "agent_runner_operator_subscription_newest_change_age_seconds 12.346",
    ]
    states = dict.fromkeys(("usable", "blocked", "exhausted", "deferred", "ignored"), 0)
    empty = Summary(problem="mode", states=states, sessions={}, newest_age_s=None)
    text = textfile.render(textfile.metrics_of(snapshot(), operator=empty))
    assert "agent_runner_operator_subscription_files" in text
    assert "agent_runner_operator_subscription_sessions{" not in text
    assert "agent_runner_operator_subscription_newest_change_age_seconds " not in text
