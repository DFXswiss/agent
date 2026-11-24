"""Devices (stage a, 11-agent-access.md section 8.4; 06-web.md section 7.3): `device list|unassign|rename|invite`
and the owed work after a decertification (`org rotate`), against the hub fake. An unassigned device gets no wrap of
the new epochs and, once it lost access to the user's credential key, no wrap of the new `CK` either."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import devices
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.credential import open_credential
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import ORG, WG
from agent_runner.crypto.wrap import unwrap_key

from .conftest import Tool
from .fakes.membership import extra
from .hubfake import HubFake, User
from .teams import assign, decertify, grant_platform_access, join, private_org, team, wg_create


def own_ck(hub: HubFake, user: User, epoch: int) -> bytes:
    wrap = next(
        w
        for w in hub.wraps
        if (w["scope"], w["scope_id"], w["epoch"], w["recipient_id"]) == ("cred", user.id, epoch, user.id)
    )
    return unwrap_key(
        user.identity.kem, scope="cred", scope_id=user.id, epoch=epoch, recipient=user.id, wrap=wrap["wrap"]
    )


def test_list_and_rename(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = assign(tool, hub, wg)
    extra(hub).online[device] = True
    extra(hub).hub_devices.append({"device_id": "unknown-to-the-log", "online": True})
    code, renamed = tool.run("device", "rename", "--device", device, "--name", " Mac mini ")
    assert code == 0, renamed
    assert (renamed["org_id"], renamed["label"]) == (org, "Mac mini")
    # a forged label is ignored
    forged: Any = json.loads(json.dumps(hub.events[org][-1]))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    hub.events[org].append({**forged, "scope_seq": len(hub.events[org]) + 1})
    listed = tool.run("device", "list", "--org", org)[1]["orgs"]
    row = listed[0]["devices"][0]
    assert row == {
        "device_id": device,
        "label": "Mac mini",
        "kind": "platform",
        "platform_seq": seq,
        "decertified": False,
        "workgroups": [wg],
        "hub": {
            "online": True,
            "last_seen_at": None,
            "agent_version": "1.0.0",
            "capacity": row["hub"]["capacity"],
            "capacity_low": False,
            "state": "active",
        },
    }
    assert tool.run("device", "rename", "--device", device, "--name", "Studio", "--org", org)[1]["label"] == "Studio"
    decertify(hub, device)
    everything = tool.run("device", "list")[1]["orgs"]
    assert [(item["org_id"], [d["label"] for d in item["devices"]]) for item in everything] == [(org, ["Studio"])]
    assert everything[0]["devices"][0]["decertified"] is True
    hub.hooks[("GET", f"/v1/orgs/{org}/devices")] = lambda b, r: (200, {"devices": ["x"]})
    assert tool.run("device", "list", "--org", org)[1]["reason"] == "response_invalid"


def test_list_without_the_platform_root_key(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    state_file = tool.home / "state.json"
    state = json.loads(state_file.read_text())
    del state["platform_root_key"]
    state_file.write_text(json.dumps(state))
    assert tool.run("device", "list", "--org", org)[1]["orgs"] == [{"org_id": org, "devices": []}]


def test_rename_refusals(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = assign(tool, hub, wg)
    for name in ("  ", "two\nlines", "x" * 101):
        assert tool.run("device", "rename", "--device", device, "--name", name)[1]["error"] == "invalid_argument"
    unknown = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    assert tool.run("device", "rename", "--device", unknown, "--name", "x")[1]["error"] == "not_found"
    assert tool.run("device", "rename", "--device", unknown, "--name", "x", "--org", org)[1]["error"] == "not_found"
    hub.hooks[("POST", "/v1/events")] = lambda b, r: (200, {"results": []})
    assert tool.run("device", "rename", "--device", device, "--name", "x")[1]["reason"] == "response_invalid"
    del hub.hooks[("POST", "/v1/events")]
    # the same device in a team org with platform access: the org must be named; a member may not rename
    shared = team(tool)
    grant_platform_access(hub, shared)
    assign(tool, hub, hub.orgs[shared].log.state.default_wg, device, seq)
    assert tool.run("device", "rename", "--device", device, "--name", "x")[1]["reason"] == "org_ambiguous"
    bob_tool, _ = join(hub, tool, shared, tmp_path, "Bob")
    assert bob_tool.run("device", "rename", "--device", device, "--name", "x", "--org", shared)[1]["error"] == (
        "not_admin"
    )
    hub.wraps[:] = [w for w in hub.wraps if not (w["scope"] == ORG and w["recipient_id"] == account.id)]
    assert tool.run("device", "rename", "--device", device, "--name", "x", "--org", org)[1]["reason"] == "epoch_stale"


def test_unassign_rotates_and_the_credential_key_moves_on(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    assert tool.run("credentials", "set-api-key", stdin="sk-test-" + "k" * 20)[1]["stored"] is True
    device, _ = assign(tool, hub, wg)
    code, unassigned = tool.run("device", "unassign", "--wg", wg, "--device", device)
    assert code == 0, unassigned
    assert unassigned["rotated"] == [
        {"scope": ORG, "scope_id": org, "epoch": 2},
        {"scope": WG, "scope_id": wg, "epoch": 2},
    ]
    credentials = unassigned["credentials"]
    assert (credentials["status"], credentials["epoch"], credentials["records"], credentials["dropped"]) == (
        "rotated",
        2,
        1,
        [],
    )
    assert (credentials["recipients"], credentials["lost_devices"]) == (1, [device])
    # the device has no wrap of any new epoch: neither of the org and workgroup keys nor of CK_2
    assert not any(w["recipient_id"] == device and w["epoch"] == 2 for w in hub.wraps)
    assert {w["recipient_id"] for w in hub.wraps if w["scope"] == "cred" and w["epoch"] == 2} == {account.id}
    record = hub.credentials[(account.id, "anthropic_api_key")]
    assert (record["version"], record["record"]["header"]["epoch"]) == (2, 2)
    body = parse_json(open_credential(own_ck(hub, account, 2), record["record"], account.identity.sig_public()))
    assert body == {"key": "sk-test-" + "k" * 20}
    assert "k" * 20 not in json.dumps(unassigned)
    # done once
    assert tool.run("device", "unassign", "--wg", wg, "--device", device)[1]["changed"] is False
    assert tool.run("device", "unassign", "--wg", wg, "--device", "x")[1]["error"] == "invalid_argument"


def test_unassign_needs_an_admin(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    grant_platform_access(hub, org)
    wg = hub.orgs[org].log.state.default_wg
    device, _ = assign(tool, hub, wg)
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    assert bob_tool.run("device", "unassign", "--wg", wg, "--device", device)[1]["error"] == "not_admin"


def test_decertified_device_is_unassigned_by_org_rotate(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, _ = assign(tool, hub, wg)
    decertify(hub, device)
    code, rotated = tool.run("org", "rotate", "--org", org)
    assert code == 0, rotated
    assert (rotated["batches"], rotated["unassigned_devices"]) == (1, [device])
    assert [item["scope"] for item in rotated["rotated"]] == [ORG, WG]
    assert rotated["credentials"]["status"] == "rotated"
    assert not hub.orgs[org].log.state.assigned(device, wg)
    assert not any(w["recipient_id"] == device and w["epoch"] == 2 for w in hub.wraps)


def test_device_invite(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    default = hub.orgs[org].log.state.default_wg
    code, invited = tool.run("device", "invite", "--org", org, "--wg", wg, "--wg", default)
    assert code == 0, invited
    assert invited["secret_fields"] == ["code", "enroll_command"]
    assert invited["enroll_command"] == f"agent-runner enroll --hub {hub.origin} --code {invited['code']}"
    assert invited["wgs"] == sorted([wg, default])
    assert all(invited["code"].encode() not in body for body in hub.bodies)
    found = hub.orgs[org].log.state.invites[invited["invite_id"]]
    assert (found.kind, dict(found.wgs)) == ("device", {wg: "device", default: "device"})
    package = invite_format.open_package(
        invite_format.invite_secrets(invite_format.parse_code(invited["code"])),
        hub.invites[invited["invite_id"]]["package_ct"],
    )
    keys = {(k["scope"], k["scope_id"]): b64u_decode(k["key"], 32) for k in package["keys"]}  # type: ignore[attr-defined]
    assert keys == {
        (ORG, org): hub.org_key(org, account),
        (WG, wg): hub.org_key(org, account, WG, wg),
        (WG, default): hub.org_key(org, account, WG, default),
    }
    assert package["wg_names"] == {wg: "Research", default: "Default"}
    assert "device" in [i["kind"] for i in tool.run("org", "show", "--org", org)[1]["open_invites"]]


def test_device_invite_refusals(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    for argv in ((), ("--wg", "x"), ("--wg", wg, "--expires-hours", "0")):
        assert tool.run("device", "invite", "--org", org, *argv)[1]["error"] == "invalid_argument", argv
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    assert bob_tool.run("device", "invite", "--org", org, "--wg", wg)[1]["error"] == "not_admin"
    hub.hooks[("POST", f"/v1/orgs/{org}/invites")] = lambda b, r: (200, {"invite_id": "x", "seq": 0})
    assert tool.run("device", "invite", "--org", org, "--wg", wg)[1]["reason"] == "response_invalid"
    del hub.hooks[("POST", f"/v1/orgs/{org}/invites")]

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise CryptoError("log_rights")

    monkeypatch.setattr(devices, "build_batch", refused)
    assert tool.run("device", "invite", "--org", org, "--wg", wg)[1]["error"] == "not_admin"
    monkeypatch.undo()
    hub.wraps[:] = [w for w in hub.wraps if not (w["scope_id"] == wg and w["recipient_id"] == account.id)]
    assert tool.run("device", "invite", "--org", org, "--wg", wg)[1]["reason"] == "epoch_stale"
    tool.run("wg", "archive", "--wg", wg)
    assert tool.run("device", "invite", "--org", org, "--wg", wg)[1]["error"] == "invalid_argument"
