"""Seeding helpers of the stage-b suites: an execution device assigned to the user's default workgroup, a task, and
a second user who may only read a workgroup."""

from __future__ import annotations

from pathlib import Path

from .conftest import Tool, make_tool, pair_user
from .hubfake import HubFake, User


def private_org(hub: HubFake, user: User) -> str:
    return next(org.id for org in hub.orgs.values() if org.private and org.log.state.member(user.id))


def default_wg(hub: HubFake, user: User) -> str:
    return hub.orgs[private_org(hub, user)].log.state.default_wg


def assign_device(tool: Tool, hub: HubFake, user: User) -> tuple[str, str, str]:
    """`(org, wg, device)`: a platform device assigned to the user's default workgroup by the tool."""
    org = private_org(hub, user)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    code, document = tool.run("device", "assign", "--wg", wg)
    assert code == 0, document
    return org, wg, device


def new_task(tool: Tool, wg: str, text: str = "Fix the login bug") -> str:
    code, document = tool.run("task", "create", "--wg", wg, "--text", text)
    assert code == 0, document
    return str(document["task_id"])


def reader(tool: Tool, hub: HubFake, root: Path) -> tuple[Tool, User, str, str]:
    """`(Bob's tool, Bob, org, wg)`: a team org of the user with a workgroup that Bob, a member, may only read."""
    code, created = tool.run("org", "create", "--name", "Acme")
    assert code == 0, created
    org = str(created["org_id"])
    wg = str(tool.run("wg", "create", "--org", org, "--name", "Research")[1]["wg_id"])
    invited = tool.run("org", "invite", "--org", org, "--role", "member", "--wg", f"{wg}:read")[1]
    other = make_tool(root / "bob")
    bob = hub.add_user(display_name="Bob")
    pair_user(other, hub, bob)
    joined = other.run("org", "join", stdin=invited["invite_link"])[1]
    assert joined["ok"] is True, joined
    return other, bob, org, wg
