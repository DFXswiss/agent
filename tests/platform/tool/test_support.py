"""Release of a workgroup to DFX support (ADR-096, owner rule E-04): `wg support grant|revoke|list` against the hub fake
that runs the hub's batch rules with the support roster of the platform log, the catch-up wrapping of released epochs
to support persons and the rotation of released workgroups the hub flags after a person left support."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from agent_platform import keywraps
from agent_platform.account import Account
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .fakes.support import end_support, make_support, support
from .hubfake import HubFake, User
from .teams import join, open_with, ring, team, wg_create


def types_after(hub: HubFake, org: str, seq: int) -> list[str]:
    return [str(cast(Any, entry)["body"]["type"]) for entry in hub.orgs[org].log.entries[seq + 1 :]]


def epochs_of(hub: HubFake, user: User, wg: str) -> list[int]:
    return sorted(slot[2] for slot in ring(hub, user) if slot[:2] == (WG, wg))


def person(hub: HubFake, name: str | None = None) -> User:
    found = hub.add_user(private_org=False, cred=False)
    make_support(hub, found, name)
    return found


def info_events(hub: HubFake, wg: str) -> list[dict[str, Any]]:
    return [item["event"] for item in hub.events.get(wg, []) if item["event"]["header"]["kind"] == "support.info"]


def test_grant_wraps_the_epochs_to_support_and_writes_the_info(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    carol_tool, carol = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:read")
    assert tool.run("wg", "ungrant", "--wg", wg, "--user", bob.id)[1]["rotated"][-1] == {
        "scope": WG,
        "scope_id": wg,
        "epoch": 2,
    }
    sam, tom = person(hub, "Sam"), person(hub)
    make_support(hub, carol)  # a member that reads the workgroup holds its keys already
    seq = hub.orgs[org].log.head.seq
    code, granted = tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")
    assert code == 0, granted
    assert (granted["changed"], granted["wg_id"], granted["access"], granted["rotated"]) == (True, wg, "read", [])
    assert granted["people"] == sorted([sam.id, tom.id])
    assert types_after(hub, org, seq) == ["support_grant"]
    assert hub.orgs[org].log.state.support == {wg: ("read", seq + 1)}
    assert epochs_of(hub, sam, wg) == [1, 2] == epochs_of(hub, tom, wg)
    # the names support needs, sealed under the workgroup's current key; only users that read it, by verified name
    [event] = info_events(hub, wg)
    header = event["header"]
    assert (header["scope"], header["wg"], header["task"], header["session"], header["ref"]) == (WG, wg, "", "", "")
    assert (header["epoch"], header["author"], header["lh"]) == (2, account.id, seq + 1)
    body = open_with(ring(hub, sam), event, account)
    assert body == {"org_name": "Acme", "wg_name": "Research", "members": {account.id: "Ada", carol.id: "Carol"}}
    # the same access again is refused locally; the other access changes the release
    seen = len(hub.orgs[org].log.entries)
    again = tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")[1]
    assert (again["error"], again["reason"], again["access"]) == ("invalid_argument", "already_released", "read")
    assert "wg support list" in again["next"]
    assert len(hub.orgs[org].log.entries) == seen
    assert tool.run("wg", "support", "grant", "--wg", wg, "--access", "write")[1]["changed"] is True
    assert hub.orgs[org].log.state.support_release(wg) == "write"
    # a reader may not release; a writer may, with only the epochs it holds
    assert carol_tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")[1]["error"] == "no_write_access"
    other = wg_create(tool, org, "Other")
    tool.run("wg", "grant", "--wg", other, "--user", bob.id)
    tool.run("wg", "ungrant", "--wg", other, "--user", bob.id)
    tool.run("wg", "grant", "--wg", other, "--user", bob.id)
    # Bob keeps only the current epoch of `other` (the older wrap is gone): epoch 1 reaches support by catch-up
    hub.wraps[:] = [
        w for w in hub.wraps if not (w["scope_id"] == other and w["recipient_id"] == bob.id and w["epoch"] == 1)
    ]
    uma = person(hub)
    by_writer = bob_tool.run("wg", "support", "grant", "--wg", other, "--access", "read")[1]
    # Carol reads no `other`: as a support person she gets its key now
    assert by_writer["people"] == sorted([sam.id, tom.id, uma.id, carol.id])
    assert epochs_of(hub, uma, other) == [2]
    assert tool.run("wg", "support", "grant", "--wg", "x", "--access", "read")[1]["error"] == "invalid_argument"


def test_grant_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    archived = wg_create(tool, org, "Old")
    assert tool.run("wg", "archive", "--wg", archived)[1]["changed"] is True
    refused = tool.run("wg", "support", "grant", "--wg", archived, "--access", "write")[1]
    assert (refused["error"], refused["reason"]) == ("invalid_argument", "forbidden")
    # without the workgroup's key nothing is sent
    hub.wraps[:] = [w for w in hub.wraps if not (w["scope_id"] == wg and w["recipient_id"] == account.id)]
    seen = len(hub.orgs[org].log.entries)
    stale = tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")[1]
    assert (stale["error"], stale["reason"]) == ("hub_refused", "epoch_stale")
    assert len(hub.orgs[org].log.entries) == seen


def test_revoke_rotates_and_support_reads_nothing_new(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    carol_tool, carol = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:read")
    sam = person(hub, "Sam")
    assert tool.run("wg", "support", "grant", "--wg", wg, "--access", "write")[1]["people"] == [sam.id]
    assert carol_tool.run("wg", "support", "revoke", "--wg", wg)[1]["error"] == "no_write_access"
    seq = hub.orgs[org].log.head.seq
    code, revoked = tool.run("wg", "support", "revoke", "--wg", wg)
    assert code == 0, revoked
    assert (revoked["changed"], revoked["wg_id"], revoked["rotated"]) == (
        True,
        wg,
        [{"scope": WG, "scope_id": wg, "epoch": 2}],
    )
    assert revoked["pending_rotation"]["batches"] == 0
    assert types_after(hub, org, seq) == ["support_revoke", "rotate"]
    assert hub.orgs[org].log.state.support == {}
    assert epochs_of(hub, sam, wg) == []
    # what is written next opens for the members, not for support
    written = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "after")[1]
    event = next(item["event"] for item in hub.events[wg] if item["scope_seq"] == written["scope_seq"])
    assert event["header"]["epoch"] == 2
    assert 2 in epochs_of(hub, carol, wg)
    # nothing to revoke any more
    assert tool.run("wg", "support", "revoke", "--wg", wg)[1]["changed"] is False


def test_list_names_the_releases_and_the_support_persons(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    quiet = wg_create(tool, org, "Quiet")
    assert tool.run("wg", "support", "list")[1] == {"ok": True, "releases": []}
    sam, tom = person(hub, "Sam"), person(hub)
    seq = hub.orgs[org].log.head.seq + 1
    tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")
    code, listed = tool.run("wg", "support", "list")
    assert code == 0, listed
    [release] = listed["releases"]
    assert (release["org_id"], release["wg_id"], release["wg_name"], release["access"], release["seq"]) == (
        org,
        wg,
        "Research",
        "read",
        seq,
    )
    assert isinstance(release["since"], str)
    by_id = {row["user_id"]: row for row in release["people"]}
    assert (by_id[sam.id]["name"], by_id[tom.id]["name"]) == ("Sam", None)
    assert by_id[sam.id]["mark"].startswith("#")
    assert len(by_id[sam.id]["mark"]) == 5
    assert tool.run("wg", "support", "list", "--wg", quiet)[1]["releases"] == []
    # a hub row of another position gives no time; odd rows are ignored; a person outside the roster is not shown
    stranger = uuid7()
    hub.hooks[("GET", f"/v1/orgs/{org}/support")] = lambda body, request: (
        200,
        {
            "releases": [{"wg_id": wg, "access": "read", "granted_seq": seq + 5, "granted_at": "x"}, "odd"],
            "people": [{"user_id": sam.id, "name": 5}, {"user_id": stranger, "name": "Eve"}],
        },
    )
    [shifted] = tool.run("wg", "support", "list", "--wg", wg)[1]["releases"]
    assert shifted["since"] is None
    assert [(row["user_id"], row["name"]) for row in shifted["people"]] == sorted([(sam.id, None), (tom.id, None)])
    hub.hooks[("GET", f"/v1/orgs/{org}/support")] = lambda body, request: (
        200,
        {"releases": [{"wg_id": wg, "granted_seq": seq, "granted_at": 7}], "people": []},
    )
    assert tool.run("wg", "support", "list", "--wg", wg)[1]["releases"][0]["since"] is None
    # an org whose log does not verify is left out
    original = hub.r_me
    hub.override(
        "r_me",
        lambda request, body: (
            200,
            {**original(request, body)[1], "orgs": [{"org_id": uuid7()}, *original(request, body)[1]["orgs"]]},
        ),
    )
    assert [row["wg_id"] for row in tool.run("wg", "support", "list")[1]["releases"]] == [wg]


def test_catch_up_gives_a_new_support_person_every_released_epoch(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    _bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    tool.run("wg", "ungrant", "--wg", wg, "--user", bob.id)
    tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")
    uma = person(hub, "Uma")
    assert epochs_of(hub, uma, wg) == []
    code, done = tool.run("org", "catch-up", "--org", org)
    assert code == 0, done
    assert done["wrapped"] == 2
    assert epochs_of(hub, uma, wg) == [1, 2]
    # a person outside the roster, or old epochs of a workgroup without a release, get nothing
    end_support(hub, uma)
    other = wg_create(tool, org, "Other")
    stranger = person(hub)
    offer = [
        {"scope": "wg", "scope_id": wg, "epoch": 1, "recipient_id": uma.id},
        {"scope": "wg", "scope_id": other, "epoch": 1, "recipient_id": stranger.id},
    ]
    hub.hooks[("GET", f"/v1/orgs/{org}/catch-up")] = lambda body, request: (200, {"missing": offer})
    assert tool.run("org", "catch-up", "--org", org)[1]["wrapped"] == 0


def test_the_roster_is_read_only_with_a_release_and_a_root_key(tool: Tool, hub: HubFake, account: User) -> None:
    state = hub.orgs[team(tool)].log.state
    released = dataclasses.replace(state, support={state.default_wg: ("read", 1)})
    without_root = cast(Account, SimpleNamespace(platform_root=None))
    assert keywraps.support_roster(without_root, released) == {}
    assert keywraps.support_roster(without_root, state) == {}


def test_released_workgroups_the_hub_flags_are_rotated_once(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    plain = wg_create(tool, org, "Plain")
    tool.run("wg", "support", "grant", "--wg", wg, "--access", "read")
    support(hub).pending[org] = [
        {"scope": "org", "scope_id": org},
        {"scope": "wg", "scope_id": plain},
        {"scope": "wg", "scope_id": wg},
    ]
    code, rotated = tool.run("org", "rotate", "--org", org)
    assert code == 0, rotated
    assert (rotated["batches"], rotated["rotated"]) == (1, [{"scope": WG, "scope_id": wg, "epoch": 2}])
    # the hub still lists it (as a stale answer would): the new epoch is not rotated again in this run, the next
    # writing command rotates once more for the epoch the hub names now
    written = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "x")[1]
    assert written["upkeep"]["rotated"] == [{"org_id": org, "scope": WG, "scope_id": wg, "epoch": 3}]
    support(hub).pending[org] = []
    assert "upkeep" not in tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "y")[1]
