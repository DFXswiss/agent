"""U-HUB-device-api (state): `GET /v1/device/state` - base fields, the section registry and the device's view
(03-api.md section 6.1, ADR-027 section 4)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_hub.config import SubagentLimits
from agent_hub.device import state
from agent_hub.device.access import device_view
from agent_hub.device.state import Section, StateRequest
from agent_hub.registry import DEVICE_STATE_SECTIONS, RegistryError, discover
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import PlatformLog, World, insert_platform_entries

EMPTY_SECTIONS: dict[str, Any] = {
    "sessions": [],
    "pending_prompts": [],
    "delivered_prompt_event_ids": {},
    "requests": [],
    "pings": [],
    "credentials_changed": [],
    "quota_poll_owners": [],
    "platform_budget": None,
    "billing": {"api": 1, "active": False, "orgs": {}},
    "github_defaults": [],
    "model_fallback": {"api": 1},  # KOR-50; dfx_keys only for the issuer, dfx_key_held only for org devices
    "prompt_queue": {"via_agent": [], "withdrawn": []},
    "failover_seconds": 300,  # ADR-087
    "failovers": [],
    "limits": {
        "api": 1,
        "subagent_max_depth": 5,
        "subagent_max_running": 50,
        "subagent_max_total": 0,
        "subagent_max_runtime_seconds": 0,
    },
    "orchestrators": [],  # KOR-57 (ADR-082 section 6)
    "subscriptions": {"api": 2, "shared": []},  # KOR-69 (ADR-090 sections 3 and 4), KOR-75 (ADR-103)
    "vm_switch": [],  # KOR-70 (ADR-093 section 5)
    "vm_switch_at": {},  # HOT-24 (ADR-093 addendum)
    "vm_upgrade_version": 2,
    "agent_profiles": {"api": 1, "sessions": {}, "workgroups": {}},  # KOR-51
}


def test_the_limits_section_carries_the_subagent_settings(fleet: Fleet) -> None:
    """ADR-088 section 2: the one source of the runner's subagent limits."""
    device, _ = fleet.platform_device()
    app_state = fleet.hub.app.state
    original = app_state.hub
    settings = SubagentLimits(max_depth=0, max_running=7, max_total=100, max_runtime_seconds=3600)
    app_state.hub = replace(original, config=replace(original.config, subagent_limits=settings))
    try:
        answer = fleet.client(device).get("/v1/device/state").json()
    finally:
        app_state.hub = original
    assert answer["limits"] == {
        "api": 1,
        "subagent_max_depth": 0,
        "subagent_max_running": 7,
        "subagent_max_total": 100,
        "subagent_max_runtime_seconds": 3600,
    }


def test_unassigned_platform_device(fleet: Fleet) -> None:
    device, _ = fleet.platform_device()
    answer = fleet.client(device).get("/v1/device/state").json()
    assert answer == {
        "state": "active",
        "last_origin_seq": 0,
        "orgs": [],
        "assigned": [],
        "rotation_pending": [],
        **EMPTY_SECTIONS,
        "hub_time": "2026-11-21T12:00:00.123Z",
    }
    fleet.execute("INSERT INTO hub.device_stream (device_id, last_origin_seq) VALUES (%s, 41)", device.id)
    assert fleet.client(device).get("/v1/device/state").json()["last_origin_seq"] == 41


def test_assigned_org_device(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    head = fleet.rows("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", scene.org)[0]
    answer = fleet.client(scene.device).get("/v1/device/state").json()
    assert answer["orgs"] == [{"org_id": scene.org, "log_head": {"seq": head[0], "hash": head[1]}}]
    assert answer["assigned"] == [{"org_id": scene.org, "wg_id": scene.wg}]
    assert answer["rotation_pending"] == []
    pending = [
        {"scope": "org", "scope_id": scene.org},
        {"scope": "wg", "scope_id": scene.wg},
        {"scope": "wg", "scope_id": scene.draft.default_wg},
    ]
    fleet.execute("UPDATE hub.org SET rotation_pending = %s WHERE id = %s", Jsonb(pending), scene.org)
    answer = fleet.client(scene.device).get("/v1/device/state").json()
    assert answer["rotation_pending"] == sorted([scene.org, scene.wg])


def test_device_of_an_org_without_assignment_reads_no_org_scope(world: World, fleet: Fleet) -> None:
    draft = world.public_org()
    device = fleet.org_device(draft, {})
    answer = fleet.client(device).get("/v1/device/state").json()
    assert answer["orgs"][0]["org_id"] == draft.id
    assert answer["assigned"] == []
    with fleet.hub.pool.connection() as connection:
        view = device_view(connection, device.id, fleet.hub.config.platform_root_key)
    assert view.orgs[0].scopes() == []
    assert view.scope(draft.id) is None
    assert view.org(draft.default_wg) is None


def test_decertified_certification_excludes_the_org(world: World, fleet: Fleet) -> None:
    """A platform device whose org keys come from an ended certification no longer reads that org."""
    draft = world.private_org()
    device = world.device("platform")
    log = PlatformLog()
    seq = log.certify(device)
    insert_platform_entries(world.hub.pool, log.entries, world.hub.clock())
    draft.add("device_assign", draft.founder, device=device.id, wg=draft.default_wg, platform_seq=seq)
    draft.append(draft.founder, draft.wraps(draft.founder, "wg", draft.default_wg, 1, [device]))
    with fleet.hub.pool.connection() as connection:
        view = device_view(connection, device.id, fleet.hub.config.platform_root_key)
    assert view.assigned() == [(draft.id, draft.default_wg)]
    log.add("device_decertify", device=device.id)
    insert_platform_entries(world.hub.pool, log.entries[-1:], world.hub.clock(), first=len(log.entries) - 1)
    with fleet.hub.pool.connection() as connection:
        view = device_view(connection, device.id, fleet.hub.config.platform_root_key)
    assert view.orgs == ()


def test_sections_fill_their_fields(fleet: Fleet) -> None:
    device, _ = fleet.platform_device()
    seen: list[StateRequest] = []

    def tasks(request: StateRequest) -> dict[str, Any]:
        seen.append(request)
        return {"sessions": [], "delivered_prompt_event_ids": {str(request.device_id): []}}

    def budget(request: StateRequest) -> dict[str, Any]:
        return {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {}}}

    state.configure(
        [
            ("tasks", Section(("sessions", "delivered_prompt_event_ids"), tasks)),
            ("usage", Section(("platform_budget",), budget)),
        ]
    )
    answer = fleet.client(device).get("/v1/device/state").json()
    assert answer["delivered_prompt_event_ids"] == {device.id: []}
    assert answer["platform_budget"] == {"usd_per_org_day": 5, "spent_today_by_org": {}}
    assert answer["pings"] == []
    assert str(seen[0].device_id) == device.id
    assert seen[0].now == fleet.hub.clock()
    assert seen[0].view.orgs == ()


def test_section_with_other_fields_fails_the_request(fleet: Fleet) -> None:
    device, _ = fleet.platform_device()
    state.configure([("tasks", Section(("sessions",), lambda request: {"pings": []}))])
    response = fleet.client(device).get("/v1/device/state")
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})


def test_section_registry_rules() -> None:
    def provide(request: StateRequest) -> dict[str, Any]:
        return {}

    for found, message in (
        ([("a", object())], "must be a Section"),
        ([("a", Section((), provide))], "must be a Section"),
        ([("a", Section(("title",), provide))], "no section field"),
        ([("a", Section(("pings",), provide)), ("b", Section(("pings",), provide))], "provided by a and b"),
    ):
        with pytest.raises(RegistryError, match=message):
            state.collect(found)
    state._state.clear()
    assert state.sections() == state.collect(discover(DEVICE_STATE_SECTIONS))
