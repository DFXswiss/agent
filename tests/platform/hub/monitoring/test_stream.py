"""U-HUB-monitoring (live): `metrics` frames every 15 s through the stream of HUB-07 - audiences, workgroup restriction,
freshness, frame size and the stream's filter for `monitoring=1` (03-api.md section 5; R-7.6)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from agent_hub.monitoring import stream
from agent_hub.monitoring.stream import FRAME_BYTES, frames_of, publish_metrics
from agent_hub.stream.bus import NOTIFY_LIMIT, encode
from agent_hub.stream.publish import MemoryPublisher, Published
from agent_hub.stream.sse import BrowserStream, StreamQuery, readable_scopes
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.monitoring.support import Watch


def metrics(published: MemoryPublisher) -> list[Published]:
    found = [item for item in published.items() if item.frame == "metrics"]
    for item in found:
        contracts_registry().validate("ws/sse/metrics.json", dict(item.data))
    return found


def entities(items: list[Published], org: str, wg: str | None) -> set[str]:
    return {
        sample["entity_id"]
        for item in items
        if str(item.audience.id) == org and (None if item.wg_id is None else str(item.wg_id)) == wg
        for sample in item.data["samples"]
    }


def test_frames_carry_fresh_samples_to_the_orgs_that_see_them(watch: Watch, published: MemoryPublisher) -> None:
    mine, other = watch.mine, watch.other
    vm = watch.vm(watch.own, mine.default_wg)
    hidden_vm = watch.vm(watch.own, watch.hidden)
    other_vm = watch.vm(watch.shared, other.default_wg)
    pool_vm = watch.vm(watch.shared, None, "pool")
    _, session = watch.session(watch.own, mine.default_wg)
    _, other_session = watch.session(watch.shared, other.default_wg, other)
    watch.sample("device", watch.own.id, watch.own, age=40)
    watch.sample("device", watch.own.id, watch.own, age=29, cpu_ratio=2.5)
    watch.sample("device", watch.shared.id, watch.shared)
    for entity_kind, entity_id in (("vm", vm), ("vm", hidden_vm), ("vm", other_vm), ("vm", pool_vm)):
        watch.sample(entity_kind, entity_id, watch.shared if entity_id in (other_vm, pool_vm) else watch.own)
    watch.sample("session", session, watch.own)
    watch.sample("session", other_session, watch.shared)
    watch.sample("vm", "vm-ffffffff", watch.own)  # a VM without a row (reported before its vm_state)
    stale = watch.vm(watch.own, mine.default_wg)
    watch.sample("vm", stale, watch.own, age=31)

    assert publish_metrics(watch.hub.app.state.hub) == 5
    items = metrics(published)
    assert all(item.audience.kind == "org" for item in items)
    assert entities(items, mine.id, None) == {watch.own.id, watch.shared.id}
    assert entities(items, other.id, None) == {watch.shared.id}
    assert entities(items, mine.id, mine.default_wg) == {vm, session}
    assert entities(items, mine.id, watch.hidden) == {hidden_vm}
    assert entities(items, other.id, other.default_wg) == {other_vm, other_session}
    own = next(
        sample
        for item in items
        for sample in item.data["samples"]
        if sample["entity_id"] == watch.own.id and str(item.audience.id) == mine.id
    )
    assert own["sample"]["cpu_ratio"] == 2.5


def test_the_stream_filter_lets_a_member_see_only_readable_frames(watch: Watch, published: MemoryPublisher) -> None:
    watch.sample("device", watch.own.id, watch.own)
    for wg in (watch.mine.default_wg, watch.hidden):
        watch.sample("vm", watch.vm(watch.own, wg), watch.own)
    publish_metrics(watch.hub.app.state.hub)
    items = [item for item in metrics(published) if str(item.audience.id) == watch.mine.id]
    with watch.hub.pool.connection() as connection:
        scopes = readable_scopes(connection, UUID(watch.mine.id), UUID(watch.member.id))
    assert scopes is not None

    def admitted(monitoring: bool) -> set[str | None]:
        query = StreamQuery(org=UUID(watch.mine.id), terminals=frozenset(), cursors={}, monitoring=monitoring)
        browser = BrowserStream(user_id=UUID(watch.member.id), query=query, scopes=scopes, watched=frozenset())
        return {None if item.wg_id is None else str(item.wg_id) for item in items if browser.admits(item)}

    assert admitted(True) == {None, watch.mine.default_wg}
    assert admitted(False) == set()


def test_frames_stay_within_one_bus_notification(watch: Watch, published: MemoryPublisher) -> None:
    sessions = [watch.session(watch.own, watch.mine.default_wg)[1] for _ in range(40)]
    for session in sessions:
        watch.sample("session", session, watch.own, state="working")
    publish_metrics(watch.hub.app.state.hub)
    items = metrics(published)
    assert len(items) > 1
    assert sorted(sample["entity_id"] for item in items for sample in item.data["samples"]) == sorted(sessions)
    for item in items:
        assert len(json.dumps(dict(item.data), separators=(",", ":"))) <= FRAME_BYTES
        assert len(encode("01930000-0000-7000-8000-000000000000", item)) <= NOTIFY_LIMIT


def test_frames_of_splits_in_order() -> None:
    items: list[dict[str, Any]] = [{"entity_id": f"{index:04d}", "pad": "x" * 30} for index in range(10)]
    single = len(json.dumps({"samples": [items[0]]}, separators=(",", ":")))
    frames = frames_of(items, limit=single + 2 * (len(json.dumps(items[0], separators=(",", ":"))) + 1))
    assert [len(frame["samples"]) for frame in frames] == [3, 3, 3, 1]
    assert [item for frame in frames for item in frame["samples"]] == items
    assert frames_of([]) == []
    assert frames_of(items[:1], limit=10) == [{"samples": items[:1]}]  # one sample is never dropped


def test_nothing_fresh_publishes_nothing(watch: Watch, published: MemoryPublisher) -> None:
    watch.sample("device", watch.own.id, watch.own, age=stream.FRESH_SECONDS)
    assert publish_metrics(watch.hub.app.state.hub) == 0
    assert metrics(published) == []
