"""Membership changes that take access away (stage a, 11-agent-access.md section 8.4; 04-crypto.md sections 4.7,
5.10): `member list|role|remove`, `org leave|invite-revoke|rotate`, `wg ungrant|archive`, against the hub fake that
runs the hub's batch rules (rotation obligation, distribution of new epochs). A removed member's key ring cannot open
what is written after the removal; the remaining members can."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import membership
from agent_platform.account import Account
from agent_platform.batch import build_batch
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.state import ORG, WG

from .conftest import Tool
from .hubfake import HubFake, User, now_ms_text
from .teams import assign, decertify, event_of, grant_platform_access, join, open_with, ring, team, wg_create


def kind(entry: Any) -> str:
    return str(entry["body"]["type"])


def types_after(hub: HubFake, org: str, seq: int) -> list[str]:
    return [kind(entry) for entry in hub.orgs[org].log.entries[seq + 1 :]]


def test_build_batch_adds_the_rotation_obligation(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    _, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    log = hub.orgs[org].log
    held = {(ORG, org, 1): hub.org_key(org, account), (WG, wg, 1): hub.org_key(org, account, WG, wg)}
    common: dict[str, Any] = {"org": org, "author": account.id, "identity": account.identity, "ts": now_ms_text()}
    built = build_batch(log, fields=[{"type": "remove", "subject": bob.id}], held=held, **common)
    default = log.state.default_wg
    assert [kind(entry) for entry in built.entries] == ["remove", "rotate", "rotate", "rotate"]
    assert built.required == ((ORG, org), (WG, default), (WG, wg))
    assert set(built.new_keys) == {(ORG, org, 2), (WG, default, 2), (WG, wg, 2)}
    assert {w["recipient_id"] for w in built.wraps} == {account.id}
    # the hub's rule: without the rotations the batch is refused
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(log, built.entries[:1], [], wrapped_by=account.id)
    # leaving owes nothing in the batch: the rotation becomes pending
    left = build_batch(
        log,
        org=org,
        author=bob.id,
        identity=bob.identity,
        fields=[{"type": "remove", "subject": bob.id}],
        ts=now_ms_text(),
        held={},
    )
    assert [kind(entry) for entry in left.entries] == ["remove"]
    assert left.log.state.pending == {(ORG, org), (WG, default), (WG, wg)}


def test_member_remove_rotates_and_the_removed_member_cannot_read(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    carol_tool, carol = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:read")
    held_by_bob = ring(hub, bob)
    seq = hub.orgs[org].log.head.seq
    code, removed = tool.run("member", "remove", "--org", org, "--user", bob.id)
    assert code == 0, removed
    default = hub.orgs[org].log.state.default_wg
    assert removed["changed"] is True
    assert removed["rotated"] == [
        {"scope": ORG, "scope_id": org, "epoch": 2},
        {"scope": WG, "scope_id": default, "epoch": 2},
        {"scope": WG, "scope_id": wg, "epoch": 2},
    ]
    assert removed["pending_rotation"] == {"batches": 0, "rotated": [], "unassigned_devices": []}
    assert removed["credentials"]["status"] == "not_owed"
    assert types_after(hub, org, seq) == ["remove", "rotate", "rotate", "rotate"]
    # the hub deleted Bob's wraps of this org and no wrap of a new epoch names him
    assert not any(slot[1] in (org, default, wg) for slot in ring(hub, bob))
    # written after the removal: an org-board entry and a workgroup-board entry
    org_entry = tool.run("whiteboard", "add", "--org", org, "--text", "after the removal")[1]
    wg_entry = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "research news")[1]
    for scope, written in ((org, org_entry), (wg, wg_entry)):
        event = event_of(hub, scope, written["scope_seq"])
        assert event["header"]["epoch"] == 2
        assert open_with(held_by_bob, event, account) is None
        assert open_with(ring(hub, account), event, account) is not None
        assert open_with(ring(hub, carol), event, account) is not None
    # the remaining reader sees both, the removed member no longer reads the org
    listed = carol_tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]
    assert [item["text"] for item in listed["items"]] == ["research news"]
    assert bob_tool.run("org", "show", "--org", org)[1]["hub_error"] == "not_found"
    # done once: the planner finds nothing; removing oneself is leaving
    again = tool.run("member", "remove", "--org", org, "--user", bob.id)[1]
    assert (again["changed"], "pending_rotation" in again) == (False, False)
    assert tool.run("member", "remove", "--org", org, "--user", account.id)[1]["reason"] == "use_org_leave"
    assert carol_tool.run("member", "remove", "--org", org, "--user", account.id)[1]["error"] == "not_admin"
    assert tool.run("member", "remove", "--org", org, "--user", "x")[1]["error"] == "invalid_argument"


def test_member_list_role_and_last_admin(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:read")
    default = hub.orgs[org].log.state.default_wg
    listed = tool.run("member", "list", "--org", org)[1]
    assert (listed["can_manage"], listed["rotation_pending"]) == (True, [])
    ada, second = listed["members"]
    assert (ada["user_id"], ada["name"], ada["role"], ada["is_me"], ada["last_admin"]) == (
        account.id,
        "Ada",
        "admin",
        True,
        True,
    )
    assert ada["access"] == [
        {"wg_id": default, "access": "write", "default": True, "inherited": False},
        {"wg_id": wg, "access": "write", "default": False, "inherited": False},
    ]
    assert (second["user_id"], second["name"], second["role"], second["last_admin"]) == (bob.id, "Bob", "member", False)
    assert second["access"][1] == {"wg_id": wg, "access": "read", "default": False, "inherited": False}
    assert bob_tool.run("member", "list", "--org", org)[1]["can_manage"] is False
    # the only admin can neither be demoted nor leave
    assert (
        tool.run("member", "role", "--org", org, "--user", account.id, "--role", "member")[1]["error"] == "last_admin"
    )
    assert tool.run("org", "leave", "--org", org)[1]["error"] == "last_admin"
    assert bob_tool.run("member", "role", "--org", org, "--user", bob.id, "--role", "admin")[1]["error"] == "not_admin"
    unknown = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    assert tool.run("member", "role", "--org", org, "--user", unknown, "--role", "admin")[1]["error"] == "not_found"
    promoted = tool.run("member", "role", "--org", org, "--user", bob.id, "--role", "admin")[1]
    assert (promoted["changed"], promoted["role"], promoted["rotated"]) == (True, "admin", [])
    assert tool.run("member", "role", "--org", org, "--user", bob.id, "--role", "admin")[1]["changed"] is False
    assert hub.orgs[org].log.state.is_admin(bob.id)
    # with a second admin Ada may step down; then Bob is the last admin
    assert tool.run("member", "role", "--org", org, "--user", account.id, "--role", "member")[1]["changed"] is True
    rows = bob_tool.run("member", "list", "--org", org)[1]["members"]
    assert [(row["name"], row["last_admin"]) for row in rows] == [("Bob", True), ("Ada", False)]


def test_leave_makes_the_rotation_pending_and_the_others_rotate(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    carol_tool, carol = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:write")
    default = hub.orgs[org].log.state.default_wg
    seq = hub.orgs[org].log.head.seq
    code, left = bob_tool.run("org", "leave", "--org", org)
    assert code == 0, left
    assert (left["changed"], left["rotated"], left["credentials"]["status"]) == (True, [], "not_owed")
    assert types_after(hub, org, seq) == ["remove"]
    assert hub.orgs[org].log.state.pending == {(ORG, org), (WG, default), (WG, wg)}
    pending = tool.run("member", "list", "--org", org)[1]["rotation_pending"]
    assert pending == [
        {"scope": ORG, "scope_id": org},
        {"scope": WG, "scope_id": default},
        {"scope": WG, "scope_id": wg},
    ]
    # a writer rotates the workgroups it writes, not the org scope
    rotated = carol_tool.run("org", "rotate", "--org", org)[1]
    assert (rotated["batches"], [item["scope_id"] for item in rotated["rotated"]]) == (1, sorted([default, wg]))
    assert hub.orgs[org].log.state.pending == {(ORG, org)}
    # the admin's next change runs the owed rotation as a follow-up (like the browser's job)
    role = tool.run("member", "role", "--org", org, "--user", carol.id, "--role", "admin")[1]
    assert role["pending_rotation"]["rotated"] == [{"scope": ORG, "scope_id": org, "epoch": 2}]
    assert hub.orgs[org].log.state.pending == frozenset()
    assert tool.run("org", "rotate", "--org", org)[1] == {
        "ok": True,
        "org_id": org,
        "batches": 0,
        "rotated": [],
        "unassigned_devices": [],
    }
    # Bob holds no key of this org any more
    assert not any(slot[1] in (org, default, wg) for slot in ring(hub, bob))
    assert bob_tool.run("org", "show", "--org", org)[1]["hub_error"] == "not_found"


def test_org_rotate_without_the_platform_root_key(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    state_file = tool.home / "state.json"
    state = json.loads(state_file.read_text())
    del state["platform_root_key"]
    state_file.write_text(json.dumps(state))
    assert tool.run("org", "rotate", "--org", org)[1]["batches"] == 0


def test_ungrant_rotates_the_workgroup(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    held_by_bob = ring(hub, bob)
    code, ungranted = tool.run("wg", "ungrant", "--wg", wg, "--user", bob.id)
    assert code == 0, ungranted
    assert ungranted["rotated"] == [{"scope": WG, "scope_id": wg, "epoch": 2}]
    assert (ungranted["wg_id"], ungranted["user_id"], ungranted["credentials"]["status"]) == (wg, bob.id, "not_owed")
    assert hub.orgs[org].log.state.wg_access(bob.id, wg) is None
    written = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "only for writers")[1]
    event = event_of(hub, wg, written["scope_seq"])
    assert open_with({**held_by_bob, **ring(hub, bob)}, event, account) is None
    assert open_with(ring(hub, account), event, account) == {"level": "workgroup", "text": "only for writers"}
    # Bob still reads the org board, not the workgroup board
    assert bob_tool.run("whiteboard", "list", "--org", org)[1]["ok"] is True
    assert bob_tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]["hub_error"] == "not_found"
    assert tool.run("wg", "ungrant", "--wg", wg, "--user", bob.id)[1]["changed"] is False
    default = hub.orgs[org].log.state.default_wg
    assert tool.run("wg", "ungrant", "--wg", default, "--user", bob.id)[1]["reason"] == "default_workgroup"
    assert bob_tool.run("wg", "ungrant", "--wg", wg, "--user", account.id)[1]["error"] == "not_admin"
    assert tool.run("wg", "ungrant", "--wg", "x", "--user", bob.id)[1]["error"] == "invalid_argument"


def test_archive(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    archived = tool.run("wg", "archive", "--wg", wg)[1]
    assert (archived["changed"], archived["rotated"], "credentials" in archived) == (True, [], False)
    assert not hub.orgs[org].log.state.writable(wg)
    assert tool.run("wg", "archive", "--wg", wg)[1]["changed"] is False
    default = hub.orgs[org].log.state.default_wg
    assert tool.run("wg", "archive", "--wg", default)[1]["reason"] == "default_workgroup"
    assert bob_tool.run("wg", "archive", "--wg", default)[1]["error"] == "not_admin"
    assert tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "x")[1]["error"] == "no_write_access"
    assert [row["archived"] for row in tool.run("wg", "list", "--org", org)[1]["workgroups"]] == [False, True]


def test_invite_revoke(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    invite = invited["invite_id"]
    assert [item["invite_id"] for item in tool.run("org", "show", "--org", org)[1]["open_invites"]] == [invite]
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "seq_conflict"}) if len(calls) == 1 else None

    path = f"/v1/orgs/{org}/invites/{invite}/revoke"
    hub.hooks[("POST", path)] = conflict
    code, revoked = tool.run("org", "invite-revoke", "--org", org, "--invite", invite)
    assert code == 0, revoked
    assert (revoked["changed"], revoked["seq"], len(calls)) == (True, hub.orgs[org].log.head.seq, 2)
    del hub.hooks[("POST", path)]
    assert tool.run("org", "show", "--org", org)[1]["open_invites"] == []
    assert tool.run("org", "invite-revoke", "--org", org, "--invite", invite)[1]["changed"] is False
    assert (
        tool.run("org", "invite-revoke", "--org", org, "--invite", "AAAAAAAAAAAAAAAAAAAAAA")[1]["error"] == "not_found"
    )
    # the invitee is refused by its own check of the log
    other = tool.run("org", "invite", "--org", org)[1]
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    hub.hooks[("POST", f"/v1/orgs/{org}/invites/{other['invite_id']}/revoke")] = lambda b, r: (200, {"seq": 0})
    assert tool.run("org", "invite-revoke", "--org", org, "--invite", other["invite_id"])[1]["reason"] == (
        "response_invalid"
    )
    assert bob_tool.run("org", "invite-revoke", "--org", org, "--invite", other["invite_id"])[1]["error"] == "not_admin"


def test_seq_conflict_is_retried_on_a_reread_log(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    _, bob = join(hub, tool, org, tmp_path, "Bob")
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "seq_conflict"}) if len(calls) == 1 else None

    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = conflict
    removed = tool.run("member", "remove", "--org", org, "--user", bob.id)[1]
    assert (removed["changed"], len(calls)) == (True, 2)
    assert hub.orgs[org].log.state.member(bob.id) is None


def test_follow_ups_report_their_refusal(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    _, bob = join(hub, tool, org, tmp_path, "Bob")
    hub.hooks[("GET", "/v1/me/credentials")] = lambda b, r: (503, {"error": "unavailable"})
    original = hub.r_platform_log
    hub.override("r_platform_log", lambda r, b: (200, {**original(r, b)[1], "head": {"seq": 99}}))
    removed = tool.run("member", "remove", "--org", org, "--user", bob.id)[1]
    assert removed["changed"] is True
    assert removed["pending_rotation"] == {"error": "hub_refused", "reason": "log_chain"}
    assert removed["credentials"]["error"] == "hub_refused"


def test_crypto_refusals_of_the_planner(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    _, bob = join(hub, tool, org, tmp_path, "Bob")

    def bad(log: Any, me: str) -> membership.Fields:
        return [{"type": "grant", "subject": bob.id, "wg": hub.orgs[org].log.state.default_wg, "access": "read"}]

    opened = Account.open(Home.from_env(tool.env))
    try:
        with pytest.raises(PlatformError) as caught:
            membership.submit(opened, org, bad)
    finally:
        opened.close()
    assert caught.value.code == "not_admin"


def test_two_owed_batches_in_one_run(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    grant_platform_access(hub, org)
    wg = wg_create(tool, org)
    default = hub.orgs[org].log.state.default_wg
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    device, _ = assign(tool, hub, wg)
    assert bob_tool.run("org", "leave", "--org", org)[1]["changed"] is True
    decertify(hub, device)
    rotated = tool.run("org", "rotate", "--org", org)[1]
    assert (rotated["batches"], rotated["unassigned_devices"]) == (2, [device])
    assert sorted((item["scope"], item["scope_id"]) for item in rotated["rotated"]) == sorted(
        [(ORG, org), (WG, wg), (WG, default)]
    )
    assert hub.orgs[org].log.state.pending == frozenset()


def test_leaving_again_finds_nothing_to_do(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob")
    assert bob_tool.run("org", "leave", "--org", org)[1]["changed"] is True
    original = hub.r_log
    # a hub that still serves the log to the former member: the planner finds nothing to do
    hub.override("r_log", lambda request, body, org: original(dataclasses.replace(request, user=account.id), body, org))
    assert bob_tool.run("org", "leave", "--org", org)[1] == {"ok": True, "org_id": org, "changed": False}
