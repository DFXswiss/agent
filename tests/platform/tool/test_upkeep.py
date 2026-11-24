"""The browser's jobs around the tool's commands (11-agent-access.md section 8.4; 06-web.md section 11 jobs 3-5;
owner rule E-04): owed rotations and the credential-key check before every writing command, catch-up wrapping after
it, both in `status` and every round of `claude watch`; nothing is sealed under an epoch whose rotation is owed."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_platform import claude
from agent_runner.crypto.log.state import ORG, WG

from .conftest import Tool
from .hubfake import HubFake, User
from .teams import assign, decertify, event_of, grant_platform_access, join, open_with, ring, team, wg_create


def test_a_member_who_left_cannot_read_what_the_admin_writes_next(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    held_by_bob = ring(hub, bob)
    assert bob_tool.run("org", "leave", "--org", org)[1]["changed"] is True
    default = hub.orgs[org].log.state.default_wg
    assert hub.orgs[org].log.state.pending == {(ORG, org), (WG, default), (WG, wg)}
    # the admin's agent writes on the org board: the owed rotations come first
    code, written = tool.run("whiteboard", "add", "--org", org, "--text", "after Bob left")
    assert code == 0, written
    assert sorted(item["scope_id"] for item in written["upkeep"]["rotated"]) == sorted([org, default, wg])
    assert hub.orgs[org].log.state.pending == frozenset()
    event = event_of(hub, org, written["scope_seq"])
    assert event["header"]["epoch"] == 2
    assert open_with(held_by_bob, event, account) is None
    assert open_with(ring(hub, account), event, account) is not None
    # nothing owed any more: the next command reports nothing
    again = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "research")[1]
    assert "upkeep" not in again
    assert open_with(held_by_bob, event_of(hub, wg, again["scope_seq"]), account) is None


def test_nothing_is_sealed_under_an_owed_epoch(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    assert bob_tool.run("org", "leave", "--org", org)[1]["changed"] is True
    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = lambda body, request: (503, {"error": "unavailable"})
    # the rotation fails: a command that does not write there goes on and reports it
    stored = tool.run("credentials", "set-api-key", stdin="sk-test-key")[1]
    assert stored["stored"] is True
    [failure] = stored["upkeep"]["errors"]
    assert (failure["step"], failure["org_id"], failure["hub_error"]) == ("rotation", org, "unavailable")
    # writing into the org is refused: the member who left could read it
    seen = len(hub.events.get(org, []))
    refused = tool.run("whiteboard", "add", "--org", org, "--text", "secret")[1]
    assert (refused["error"], refused["scope"], refused["scope_id"]) == ("rotation_pending", ORG, org)
    assert len(hub.events.get(org, [])) == seen
    del hub.hooks[("POST", f"/v1/orgs/{org}/log")]
    assert tool.run("whiteboard", "add", "--org", org, "--text", "now")[1]["upkeep"]["rotated"][0]["epoch"] == 2


def test_an_ungranted_users_agent_rotates_its_credential_key(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    grant_platform_access(hub, org)
    wg = wg_create(tool, org)
    carol_tool, carol = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:write")
    device, _ = assign(tool, hub, wg)
    assert tool.run("wg", "ungrant", "--wg", wg, "--user", carol.id)[1]["changed"] is True
    assert carol.cred_epoch == 1
    # Carol's next command: her credential key changes away from the device before the record is written
    stored = carol_tool.run("credentials", "set-api-key", stdin="sk-carol-key")[1]
    checked = stored["upkeep"]["credentials"]
    assert (checked["status"], checked["lost_devices"], checked["recipients"]) == ("rotated", [device], 1)
    assert carol.cred_epoch == 2
    assert not any(w["recipient_id"] == device and w["scope_id"] == carol.id and w["epoch"] == 2 for w in hub.wraps)
    assert hub.credentials[(carol.id, "anthropic_api_key")]["record"]["header"]["epoch"] == 2
    assert "upkeep" not in carol_tool.run("credentials", "set-api-key", stdin="sk-carol-key-2")[1]


def test_decertified_devices_are_unassigned_and_missing_keys_wrapped(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    grant_platform_access(hub, org)
    wg = wg_create(tool, org)
    _, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:read")
    device, _ = assign(tool, hub, wg)
    decertify(hub, device)
    status = tool.run("status")[1]
    assert status["upkeep"]["unassigned_devices"] == [device]
    assert wg in [item["scope_id"] for item in status["upkeep"]["rotated"]]
    assert status["upkeep"]["credentials"]["lost_devices"] == [device]
    assert (device, wg) not in hub.orgs[org].log.state.assignments
    # a wrap Bob lacks (the hub lost it) is created after the next writing command
    epoch = hub.orgs[org].log.state.epoch(ORG, org)
    hub.wraps[:] = [
        w for w in hub.wraps if (w["scope"], w["scope_id"], w["epoch"], w["recipient_id"]) != (ORG, org, epoch, bob.id)
    ]
    written = tool.run("whiteboard", "add", "--org", org, "--text", "hello")[1]
    assert written["upkeep"] == {"wrapped": 1}
    assert any((w["scope"], w["scope_id"], w["recipient_id"]) == (ORG, org, bob.id) for w in hub.wraps)


def test_failing_steps_are_reported_not_fatal(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    hub.hooks[("GET", f"/v1/orgs/{org}/catch-up")] = lambda body, request: (503, {"error": "unavailable"})
    written = tool.run("whiteboard", "add", "--org", org, "--text", "x")[1]
    assert written["scope_seq"] > 0
    assert [(e["step"], e["org_id"], e["hub_error"]) for e in written["upkeep"]["errors"]] == [
        ("catch_up", org, "unavailable")
    ]
    hub.hooks.clear()
    hub.hooks[("GET", "/v1/me")] = lambda body, request: (503, {"error": "unavailable"})
    written = tool.run("whiteboard", "add", "--org", org, "--text", "y")[1]
    assert [e["step"] for e in written["upkeep"]["errors"]] == ["orgs", "credentials", "orgs"]


def test_watch_runs_the_jobs_every_round(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude, "time", SimpleNamespace(time=time.time, sleep=lambda seconds: None))
    org = team(tool)
    hub.hooks[("GET", f"/v1/orgs/{org}/catch-up")] = lambda body, request: (503, {"error": "unavailable"})
    document: dict[str, Any] = tool.run("claude", "watch", "--iterations", "2")[1]
    assert (document["runs"], document["stored"]) == (2, 0)
    assert capsys.readouterr().err.count("agent-platform: catch_up failed: hub_refused") == 2
