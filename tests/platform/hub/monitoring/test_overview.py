"""U-HUB-monitoring (overview): `GET /v1/monitoring/overview?org=` - the three levels, the rights filter of 08 section 6
(shared platform device, foreign workgroup), `stale`, `capacity_low` and usage (03-api.md section 4.11; R-7.1, R-7.2,
R-7.6)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_hub.monitoring import read
from tests.platform.hub.monitoring.support import Watch
from tests.platform.hub.orgs.support import new_id

USAGE = {
    "requests": 3,
    "input_tokens": 1200,
    "output_tokens": 300,
    "cache_read_tokens": 50,
    "cache_write_tokens": 20,
    "thinking_tokens": 10,
    "cost_micro_usd": 4500,
    "cost_basis": "provider",
    "context_used_tokens": 9000,
    "context_window_tokens": 128000,
    "last_provider": "test-provider",
}


def overview(watch: Watch, actor: Any, org: str | None = None) -> dict[str, Any]:
    response = actor.browser.get("/v1/monitoring/overview", params={"org": org or watch.mine.id})
    assert response.status_code == 200, response.text
    return dict(response.json())


def by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def add_usage(watch: Watch, session: str) -> None:
    columns = ", ".join(["session_id", *USAGE, "updated_at"])
    params = [session, *USAGE.values(), watch.now()]
    watch.desk.execute(f"INSERT INTO hub.usage_counter ({columns}) VALUES ({', '.join(['%s'] * len(params))})", *params)


def test_the_admin_sees_devices_vms_and_sessions(watch: Watch) -> None:
    default, hidden = watch.mine.default_wg, watch.hidden
    vm = watch.vm(watch.own, default)
    hidden_vm = watch.vm(watch.own, hidden, "stopped")
    watch.vm(watch.own, None, "pool")
    watch.vm(watch.own, default, "deleted")
    task, session = watch.session(watch.own, default)
    _, hidden_session = watch.session(watch.own, hidden)
    _, ended = watch.session(watch.own, default, state="stopped")
    _, gone = watch.session(watch.own, default, state="stopped")
    watch.desk.execute("UPDATE hub.session SET ended_at = %s WHERE id = %s", watch.now() - timedelta(minutes=5), ended)
    watch.desk.execute("UPDATE hub.session SET ended_at = %s WHERE id = %s", watch.now() - timedelta(minutes=16), gone)
    add_usage(watch, session)
    own_ts = watch.sample("device", watch.own.id, watch.own, age=20, cpu_ratio=1.25, free_vm_slots=3)
    watch.sample("device", watch.own.id, watch.own, age=40, cpu_ratio=0.25)
    watch.sample("vm", vm, watch.own, age=5)
    watch.sample("session", session, watch.own, age=5, state="working", mem_used_bytes=None)

    body = overview(watch, watch.admin)

    devices = by(body["devices"], "device_id")
    assert set(devices) == {watch.own.id, watch.shared.id}
    own = devices[watch.own.id]
    assert (own["online"], own["capacity_low"], own["stale"]) == (True, False, False)
    assert own["latest"]["ts"] == own_ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert (own["latest"]["cpu_ratio"], own["latest"]["free_vm_slots"]) == (1.25, 3)
    assert (devices[watch.shared.id]["latest"], devices[watch.shared.id]["stale"]) == (None, True)
    vms = by(body["vms"], "vm_id")
    assert set(vms) == {vm, hidden_vm}
    assert vms[vm] == {
        "vm_id": vm,
        "device_id": watch.own.id,
        "wg_id": default,
        "state": "running",
        "latest": vms[vm]["latest"],
        "stale": False,
    }
    assert vms[hidden_vm]["state"] == "stopped"
    assert vms[hidden_vm]["latest"] is None
    sessions = by(body["sessions"], "session_id")
    assert set(sessions) == {session, hidden_session, ended}
    found = sessions[session]
    assert (found["task_id"], found["wg_id"], found["state"]) == (task, default, "working")
    assert found["latest"]["mem_used_bytes"] is None
    assert found["usage"] == {**USAGE, "updated_at": found["usage"]["updated_at"]}
    assert sessions[ended]["usage"] is None


def test_a_member_sees_only_readable_workgroups(watch: Watch) -> None:
    vm = watch.vm(watch.own, watch.mine.default_wg)
    watch.vm(watch.own, watch.hidden)
    _, session = watch.session(watch.own, watch.mine.default_wg)
    watch.session(watch.own, watch.hidden)
    body = overview(watch, watch.member)
    assert {item["device_id"] for item in body["devices"]} == {watch.own.id, watch.shared.id}
    assert [item["vm_id"] for item in body["vms"]] == [vm]
    assert [item["session_id"] for item in body["sessions"]] == [session]


def test_a_shared_platform_device_shows_only_its_totals_to_each_org(watch: Watch) -> None:
    mine_vm = watch.vm(watch.shared, watch.mine.default_wg)
    other_vm = watch.vm(watch.shared, watch.other.default_wg)
    _, mine_session = watch.session(watch.shared, watch.mine.default_wg)
    _, other_session = watch.session(watch.shared, watch.other.default_wg, watch.other)
    watch.sample("device", watch.shared.id, watch.shared, free_vm_slots=1, free_mem_bytes=1 << 30)
    for entity_kind, entity_id in (("vm", mine_vm), ("vm", other_vm), ("session", other_session)):
        watch.sample(entity_kind, entity_id, watch.shared)
    mine = overview(watch, watch.member)
    other = overview(watch, watch.other.founder, watch.other.id)
    shared_mine = by(mine["devices"], "device_id")[watch.shared.id]
    shared_other = by(other["devices"], "device_id")[watch.shared.id]
    assert shared_mine == shared_other
    assert shared_mine["latest"]["free_mem_bytes"] == 1 << 30
    assert [item["vm_id"] for item in mine["vms"]] == [mine_vm]
    assert [item["session_id"] for item in mine["sessions"]] == [mine_session]
    assert [item["device_id"] for item in other["devices"]] == [watch.shared.id]
    assert [item["vm_id"] for item in other["vms"]] == [other_vm]
    assert [item["session_id"] for item in other["sessions"]] == [other_session]


def test_stale_and_capacity_low(watch: Watch) -> None:
    watch.sample("device", watch.own.id, watch.own, age=60)
    watch.sample("device", watch.shared.id, watch.shared, age=60.001)
    watch.desk.set_capacity(
        watch.shared,
        {"vm_memory_budget_mib": 1, "vm_memory_used_mib": 0, "vm_slots_total": 20, "vm_slots_free": 1, "pool_ready": 0},
    )
    devices = by(overview(watch, watch.admin)["devices"], "device_id")
    assert (devices[watch.own.id]["stale"], devices[watch.own.id]["capacity_low"]) == (False, False)
    assert (devices[watch.shared.id]["stale"], devices[watch.shared.id]["capacity_low"]) == (True, True)
    watch.desk.offline(watch.own)
    assert by(overview(watch, watch.admin)["devices"], "device_id")[watch.own.id]["online"] is False


def test_an_org_without_entities(watch: Watch) -> None:
    lonely = watch.world.private_org()
    assert overview(watch, lonely.founder, lonely.id) == {
        "devices": [],
        "vms": [],
        "sessions": [],
        "sessions_total": 0,
        "truncated": False,
    }


def test_sessions_are_bounded_most_recently_active_first(watch: Watch, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-063 section 2 (KOR-35): at most `SESSIONS_MAX` sessions, ordered by the hub time of their newest state
    transition (else their creation), ties by ID, with `sessions_total` and `truncated`."""
    wg = watch.mine.default_wg
    first = watch.session(watch.own, wg)[1]
    watch.hub.clock.advance(5)
    second = watch.session(watch.own, wg)[1]
    watch.hub.clock.advance(5)
    third = watch.session(watch.own, wg)[1]
    with watch.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.session_transition (session_id, seq, state, at, received_at) VALUES (%s, 1, 'working',"
            " %s, %s)",
            (first, watch.now(), watch.now() + timedelta(seconds=1)),
        )
    body = overview(watch, watch.admin)
    assert [item["session_id"] for item in body["sessions"]] == [first, third, second]
    assert (body["sessions_total"], body["truncated"]) == (3, False)
    monkeypatch.setattr(read, "SESSIONS_MAX", 2)
    body = overview(watch, watch.admin)
    assert [item["session_id"] for item in body["sessions"]] == [first, third]
    assert (body["sessions_total"], body["truncated"]) == (3, True)


def test_overview_refusals(watch: Watch) -> None:
    for org in (watch.mine.id, new_id(), "nope"):
        response = watch.other.founder.browser.get(
            "/v1/monitoring/overview", params={"org": org}, extensions={"contract": False}
        )
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    response = watch.admin.browser.get("/v1/monitoring/overview", extensions={"contract": False})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/org"]}},
    )
    response = watch.admin.browser.get(
        "/v1/monitoring/overview", params={"org": watch.mine.id, "x": "1"}, extensions={"contract": False}
    )
    assert response.status_code == 400


def test_a_removed_device_disappears(watch: Watch) -> None:
    watch.desk.execute(
        "UPDATE hub.org_member SET removed_seq = 99 WHERE org_id = %s AND principal_id = %s",
        watch.mine.id,
        watch.own.id,
    )
    assert {item["device_id"] for item in overview(watch, watch.admin)["devices"]} == {watch.shared.id}
    watch.desk.execute("UPDATE hub.device SET capacity = %s WHERE id = %s", Jsonb({}), watch.shared.id)
    assert overview(watch, watch.admin)["devices"][0]["capacity_low"] is True
