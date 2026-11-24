"""Devices and placement of the agent tool (ADR-075, 11-agent-access.md section 8.4): `device list`, `device assign
--org` (org devices, platform devices of the log, platform candidates), `device unassign --org` with its rotations,
`wg placement`, `task create --device --vm`, `task show` (main session, waiting reason) and the waiting reasons of
`diagnose`. Every batch runs the reference checks in the hub fake, so a missing rotation or wrap is refused."""

from __future__ import annotations

from typing import Any

import pytest

from agent_platform.batch import build_batch
from agent_platform.diagnose import WAITING
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.crypto.log.state import ORG, WG

from .conftest import Tool
from .fakes.account_views import platform_entry
from .fakes.membership import extra
from .hubfake import HubFake, User, now_ms_text

TS = "2026-01-01T00:00:00.000Z"


def private_org(hub: HubFake, user: User) -> str:
    return next(org.id for org in hub.orgs.values() if org.private and org.log.state.member(user.id))


def team(tool: Tool, name: str = "Acme") -> str:
    code, document = tool.run("org", "create", "--name", name)
    assert code == 0, document
    return str(document["org_id"])


def org_device(hub: HubFake, org: str, admin: User, wgs: list[str]) -> str:
    """An org device that accepted an invitation of `admin` (explicitly assigned to `wgs`)."""
    found = hub.orgs[org]
    secrets_of = invite_format.invite_secrets(invite_format.generate_code())
    entry = {
        "type": "invite",
        "invite_id": secrets_of.invite_id,
        "pk_inv": secrets_of.pk_inv,
        "kind": "device",
        "wgs": dict.fromkeys(wgs, "device"),
        "expires": "2999-01-01T00:00:00.000Z",
    }
    built = build_batch(found.log, org=org, author=admin.id, identity=admin.identity, fields=[entry], ts=TS, held={})
    device, ident = uuid7(), Identity.generate()
    accept = {
        "type": "accept",
        "invite_id": secrets_of.invite_id,
        "subject": device,
        "kind": "device",
        "pub": ident.public(),
    }
    built = build_batch(
        built.log,
        org=org,
        author=device,
        identity=ident,
        fields=[accept],
        ts=TS,
        held={},
        invite=secrets_of.signing,
        held_only=True,
    )
    found.log = built.log
    return device


def slots_of(hub: HubFake, recipient: str) -> set[tuple[str, str, int]]:
    return {(w["scope"], w["scope_id"], w["epoch"]) for w in hub.wraps if w["recipient_id"] == recipient}


# --- device list ---


def test_device_list_shows_the_vms_and_the_organization(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    default = hub.orgs[org].log.state.default_wg
    device = org_device(hub, org, account, [default])
    assert tool.run("device", "assign", "--org", org, "--device", device)[1]["ok"] is True
    later = tool.run("wg", "create", "--org", org, "--name", "Ops")[1]["wg_id"]
    vms = [{"vm_id": "vm-0000000a", "wg_id": default, "state": "running", "sessions": 3}]
    extra(hub).vms[device] = vms
    extra(hub).online[device] = True
    listed = tool.run("device", "list", "--org", org)[1]
    assert listed["ok"] is True, listed
    (shown,) = [item for item in listed["orgs"][0]["devices"] if item["device_id"] == device]
    # the verified log assigns it to the organization (every workgroup, also a later one); the hub adds VMs and load
    assert shown["organization"] is True
    assert set(shown["workgroups"]) == {default, later}
    assert (shown["hub"]["vms"], shown["hub"]["online"]) == (vms, True)


# --- assignment to the organization: platform devices ---


def test_platform_device_for_the_whole_organization(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    default = hub.orgs[org].log.state.default_wg
    device, seq = hub.certify_device()
    other, _ = hub.certify_device()
    hub.candidates[default] = [
        "junk",
        {"device_id": other, "platform_seq": seq},
        {"device_id": device, "platform_seq": seq},
    ]
    # --device picks that candidate only
    assert tool.run("device", "assign", "--org", org, "--device", uuid7())[1]["error"] == "no_device"
    document = tool.run("device", "assign", "--org", org, "--device", device)[1]
    assert document["ok"] is True, document
    assert (document["device_id"], document["cred_wrap"], document["keys_pending"]) == (device, True, [])
    state = hub.orgs[org].log.state
    assert state.org_assigned(device)
    assert {(ORG, org, 1), (WG, default, 1), ("cred", account.id, 1)} <= slots_of(hub, device)
    # assigned already: refused before the hub; a per-workgroup assignment skips it
    again = tool.run("device", "assign", "--org", org, "--device", device)[1]
    assert (again["error"], again["reason"]) == ("invalid_argument", "already_assigned")
    assert tool.run("device", "assign", "--wg", default)[1]["error"] == "no_device"
    # a workgroup created later is wrapped for the organization's device in its own batch
    wg2 = tool.run("wg", "create", "--org", org, "--name", "Ops")[1]["wg_id"]
    assert (WG, wg2, 1) in slots_of(hub, device)
    assert tool.run("org", "show", "--org", org)[1]["org_devices"] == [device]
    (listed,) = tool.run("device", "list", "--org", org)[1]["orgs"][0]["devices"]
    assert (listed["organization"], set(listed["workgroups"])) == (True, {default, wg2})
    # ending it rotates every workgroup and the org scope (nothing else is left)
    ended = tool.run("device", "unassign", "--org", org, "--device", device)[1]
    assert ended["ok"] is True, ended
    expected = sorted([(ORG, org), (WG, default), (WG, wg2)])
    assert [(r["scope"], r["scope_id"]) for r in ended["rotated"]] == expected
    assert all(r["epoch"] == 2 for r in ended["rotated"])
    state = hub.orgs[org].log.state
    assert not state.org_assigned(device)
    assert not any(slot[2] == 2 for slot in slots_of(hub, device))
    assert (WG, wg2, 2) in slots_of(hub, account.id)
    # assigned again: the device is in the log, its platform_seq comes from there
    hub.candidates[default] = []
    assert tool.run("device", "assign", "--org", org, "--device", device)[1]["device_id"] == device
    assert hub.orgs[org].log.state.org_assigned(device)
    # without --device: the first candidate not yet assigned to the organization
    third, third_seq = hub.certify_device()
    hub.candidates[default] = [
        {"device_id": device, "platform_seq": seq},
        {"device_id": third, "platform_seq": third_seq},
    ]
    assert tool.run("device", "assign", "--org", org)[1]["device_id"] == third


def test_platform_device_of_the_log_that_was_decertified(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    default = hub.orgs[org].log.state.default_wg
    device, seq = hub.certify_device()
    hub.candidates[default] = [{"device_id": device, "platform_seq": seq}]
    assert tool.run("device", "assign", "--wg", default)[1]["device_id"] == device
    last = hub.platform_entries[-1]
    hub._platform_append(
        {
            "seq": len(hub.platform_entries),
            "prev": entry_hash(last),
            "type": "device_decertify",
            "device": device,
            "by": "platform-root",
            "ts": now_ms_text(),
            "v": 1,
        }
    )
    assert tool.run("device", "assign", "--org", org, "--device", device)[1]["error"] == "no_device"


def test_org_assignment_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    team_org = team(tool)
    device, seq = hub.certify_device()
    hub.candidates[hub.orgs[team_org].log.state.default_wg] = [{"device_id": device, "platform_seq": seq}]
    # a platform device needs platform access
    assert tool.run("device", "assign", "--org", team_org)[1]["reason"] == "platform_access_denied"
    for argv in (
        ("device", "assign", "--org", team_org, "--device", "x"),
        ("device", "assign", "--wg", uuid7(), "--device", device),
        ("device", "assign", "--wg", uuid7(), "--org", team_org),
        ("device", "assign"),
        ("device", "unassign", "--org", team_org, "--device", "x"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    ended = tool.run("device", "unassign", "--org", team_org, "--device", device)[1]
    assert (ended["error"], ended["reason"]) == ("invalid_argument", "not_assigned")


# --- assignment to the organization: org devices ---


def test_org_device_for_the_whole_organization(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    default = hub.orgs[org].log.state.default_wg
    wg2 = tool.run("wg", "create", "--org", org, "--name", "Ops")[1]["wg_id"]
    device = org_device(hub, org, account, [default])
    # the admin lost its wrap of wg2: that key is pending (catch-up), the batch still goes out
    hub.wraps[:] = [w for w in hub.wraps if not (w["scope_id"] == wg2 and w["recipient_id"] == account.id)]
    document = tool.run("device", "assign", "--org", org, "--device", device)[1]
    assert document["ok"] is True, document
    assert document["keys_pending"] == [{"scope": WG, "scope_id": wg2}]
    assert document["cred_wrap"] is True
    assert hub.orgs[org].log.state.assigned(device, wg2)
    # the explicit assignment stays after the organization assignment ends: only wg2 is rotated
    ended = tool.run("device", "unassign", "--org", org, "--device", device)[1]
    assert ended["ok"] is True, ended
    assert ended["rotated"] == [{"scope": WG, "scope_id": wg2, "epoch": 2}]
    state = hub.orgs[org].log.state
    assert state.assigned(device, default)
    assert not state.assigned(device, wg2)


# --- placement ---


def test_wg_placement(tool: Tool, hub: HubFake, account: User) -> None:
    wg = hub.orgs[private_org(hub, account)].log.state.default_wg
    device = uuid7()
    assert tool.run("wg", "placement", "--wg", wg)[1]["placement"] is None
    chosen = tool.run("wg", "placement", "--wg", wg, "--device", device, "--vm", "vm-0000000a")[1]
    assert chosen["placement"] == {"device_id": device, "vm_id": "vm-0000000a"}
    assert tool.run("wg", "placement", "--wg", wg)[1]["placement"] == {"device_id": device, "vm_id": "vm-0000000a"}
    assert tool.run("wg", "placement", "--wg", wg, "--auto")[1]["placement"] is None
    for argv in (
        ("wg", "placement", "--wg", "x"),
        ("wg", "placement", "--wg", wg, "--device", "x"),
        ("wg", "placement", "--wg", wg, "--vm", "vm-0000000a"),
        ("wg", "placement", "--wg", wg, "--device", device, "--vm", "vm-1"),
        ("wg", "placement", "--wg", wg, "--device", device, "--auto"),
        ("wg", "placement", "--wg", wg, "--auto", "--vm", "vm-0000000a"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    hub.hooks[("GET", f"/v1/workgroups/{wg}/placement")] = lambda b, r: (200, {"placement": "x"})
    assert tool.run("wg", "placement", "--wg", wg)[1]["reason"] == "response_invalid"


def test_task_create_with_placement_and_show(tool: Tool, hub: HubFake, account: User) -> None:
    wg = hub.orgs[private_org(hub, account)].log.state.default_wg
    device = uuid7()
    created = tool.run("task", "create", "--wg", wg, "--text", "Hi", "--device", device, "--vm", "vm-0000000b")[1]
    assert created["placement"] == {"device_id": device, "vm_id": "vm-0000000b"}
    task = created["task_id"]
    assert hub.tasks[task]["task"]["placement"] == {"device_id": device, "vm_id": "vm-0000000b"}
    only_device = tool.run("task", "create", "--wg", wg, "--text", "Hi", "--device", device)[1]
    assert only_device["placement"] == {"device_id": device}
    assert hub.tasks[only_device["task_id"]]["task"]["placement"] == {"device_id": device, "vm_id": None}
    plain = tool.run("task", "create", "--wg", wg, "--text", "Hi")[1]
    assert plain["placement"] is None
    for argv in (
        ("task", "create", "--wg", wg, "--text", "Hi", "--vm", "vm-0000000b"),
        ("task", "create", "--wg", wg, "--text", "Hi", "--device", "x"),
        ("task", "create", "--wg", wg, "--text", "Hi", "--device", device, "--vm", "bad"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    # show: waiting with a reason, no main session yet
    row = hub.tasks[task]["task"]
    row |= {"state": "scheduling", "state_reason": "placement_full", "main_session_id": None}
    shown = tool.run("task", "show", "--task", task)[1]
    assert (shown["waiting"], shown["main_session"]) == ("placement_full", None)
    assert shown["placement"] == {"device_id": device, "vm_id": "vm-0000000b"}
    row["state_reason"] = "<script>"
    assert tool.run("task", "show", "--task", task)[1]["waiting"] == "task_waiting"
    # running: device and VM of the main session
    session = uuid7()
    row |= {"state": "working", "state_reason": None, "main_session_id": session}
    hub.tasks[task]["sessions"] = [
        "junk",
        {"session": "x"},
        {"session": {"session_id": uuid7(), "state": "stopped", "device_id": device, "vm_id": "vm-0000000c"}},
        {"session": {"session_id": session, "state": "working", "device_id": device, "vm_id": "vm-0000000b"}},
    ]
    shown = tool.run("task", "show", "--task", task)[1]
    assert shown["waiting"] is None
    assert shown["main_session"] == {"session_id": session, "state": "working", "device_id": device,
                                     "vm_id": "vm-0000000b"}  # fmt: skip
    hub.tasks[task]["sessions"] = None
    assert tool.run("task", "show", "--task", task)[1]["main_session"] is None


# --- diagnose ---


@pytest.mark.parametrize("reason", sorted(WAITING))
def test_diagnose_waiting_reasons(tool: Tool, hub: HubFake, account: User, reason: str) -> None:
    task_id = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    hub.tasks[task_id] = {"task": {"state": "scheduling", "state_reason": reason}, "transitions": []}
    finding = tool.run("diagnose", "--task", task_id)[1]["findings"][-1]
    assert (finding["reason"], finding["next"]) == (reason, WAITING[reason])


def test_diagnose_falls_back_to_the_transition_reason(tool: Tool, hub: HubFake, account: User) -> None:
    task_id = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    task: dict[str, Any] = {
        "task": {"state": "scheduling", "state_reason": "<x>"},
        "transitions": [{"reason": "keys_pending"}],
    }
    hub.tasks[task_id] = task
    assert tool.run("diagnose", "--task", task_id)[1]["findings"][-1]["reason"] == "keys_pending"


# --- the first task of an admin assigns a device (ADR-078, rule E-05) ---


def test_an_admins_task_assigns_a_platform_device_first(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    created = tool.run("task", "create", "--wg", wg, "--text", "Fix the login bug")[1]
    assert created.get("assigned_device") == device, created
    assert hub.orgs[org].log.state.assigned(device, wg)
    assert tool.run("task", "create", "--wg", wg, "--text", "More")[1]["assigned_device"] is None  # assigned already
    chosen = tool.run("task", "create", "--wg", wg, "--text", "There", "--device", device)[1]
    assert chosen["assigned_device"] is None  # a choice is the user's: nothing is assigned on top
    # a team organization without platform access waits (the operators are asked); with the switch it is assigned
    team_org = team(tool)
    team_wg = hub.orgs[team_org].log.state.default_wg
    hub.candidates[team_wg] = [{"device_id": device, "platform_seq": seq}]
    assert tool.run("task", "create", "--wg", team_wg, "--text", "Fix it")[1]["assigned_device"] is None
    platform_entry(hub, type="platform_policy", name="open_platform_access", on=True)
    opened = tool.run("task", "create", "--wg", team_wg, "--text", "Fix it")[1]
    assert opened.get("assigned_device") == device, opened
    # no candidate: the task is created and waits
    empty_wg = tool.run("wg", "create", "--org", team_org, "--name", "Ops")[1]["wg_id"]
    assert tool.run("task", "create", "--wg", empty_wg, "--text", "Wait")[1]["assigned_device"] is None
    # a hub without a platform log yet answers `not_found`: nothing to assign either
    missing = tool.run("wg", "create", "--org", team_org, "--name", "Missing")[1]["wg_id"]
    hub.hooks[("GET", f"/v1/workgroups/{missing}/device-candidates")] = lambda body, request: (
        404,
        {"error": "not_found"},
    )
    assert tool.run("task", "create", "--wg", missing, "--text", "Wait")[1]["assigned_device"] is None
    # any other failure of the assignment is reported, the task is not sent
    broken = tool.run("wg", "create", "--org", team_org, "--name", "Broken")[1]["wg_id"]
    hub.hooks[("GET", f"/v1/workgroups/{broken}/device-candidates")] = lambda body, request: (200, {"candidates": 1})
    assert tool.run("task", "create", "--wg", broken, "--text", "Never")[1]["ok"] is False
