"""Names of people (ADR-073): `org admit --name`, `org invite --name` and `org member-name` give a provisional name as a
`member.name` event (sealed, the hub never sees it), `org show` names every member with where the name comes from and
writes the user's own name into an org opened without one; an invitation whose account was admitted directly is
closed by the admission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from agent_platform import names as names_module
from agent_platform import orgs
from agent_platform.account import Account, Names
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.identity import account_code, format_account_code
from agent_runner.crypto.log.state import ORG

from .conftest import Tool, make_tool, pair_user
from .hubfake import HubFake, Request, User
from .test_admit import paired, team

EVENTS = r"/v1/events"
REVOKE = r"/v1/orgs/(?P<org>[0-9a-f-]+)/invites/(?P<invite>[A-Za-z0-9_-]+)/revoke"


def intercept(hub: HubFake, pattern: str, handler: Any) -> Any:
    """Answers `POST pattern` with `handler` before the fake's own route; returns the undo."""
    route = ("POST", pattern, handler)
    hub.routes.insert(0, route)
    return lambda: hub.routes.remove(route)


def shown_member(tool: Tool, org: str, user: str) -> dict[str, Any]:
    shown = tool.run("org", "show", "--org", org)[1]
    return next(item for item in shown["members"] if item["principal_id"] == user)


def org_key(tool: Tool, org: str) -> tuple[Account, bytes]:
    opened = Account.open(Home.from_env(tool.env))
    epoch = opened.org_log(org).state.epoch(ORG, org)
    return opened, opened.keys(org)[(ORG, org, epoch)]


def admit(tool: Tool, org: str, who: str, name: str, *extra: str) -> dict[str, Any]:
    code, result = tool.run("org", "admit", "--org", org, "--user", who, "--name", name, *extra)
    assert code == 0, result
    return result


def test_admit_with_a_name_every_member_sees_until_the_own_one(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, bob_tool = paired(hub, tmp_path, "Bob")
    carol, carol_tool = paired(hub, tmp_path, "Carol")
    admitted = admit(tool, org, bob.id, "  Bob Example ")
    assert (admitted["name"], admitted["name_saved"], admitted["closed_invites"]) == ("Bob Example", True, [])
    admit(tool, org, carol.id, "Carol Example")
    # the name is sealed: the hub stores the event, never the name
    stored = [item["event"] for item in hub.events[org] if item["event"]["header"]["kind"] == "member.name"]
    assert [event["header"]["ref"] for event in stored] == [bob.id, carol.id]
    assert "Bob Example" not in json.dumps(hub.events)
    # every member sees it at once, with who gave it (Carol has not opened the org: `org list` does not write)
    carol_tool.run("org", "list")
    assert shown_member(tool, org, bob.id) == {
        "principal_id": bob.id,
        "kind": "user",
        "role": "member",
        "name": "Bob Example",
        "name_source": "admin",
        "name_given_by": account.id,
    }
    # Bob opens the org: his own name is written without a question and replaces the provisional one for everyone
    shown = bob_tool.run("org", "show", "--org", org)[1]
    assert shown["own_name"] == "written"
    assert shown_member(tool, org, bob.id) | {"principal_id": ""} == {
        "principal_id": "",
        "kind": "user",
        "role": "member",
        "name": "Bob",
        "name_source": "own",
        "name_given_by": None,
    }
    assert bob_tool.run("org", "show", "--org", org)[1]["own_name"] == "present"
    # a newer admin-given name never replaces an own name
    assert tool.run("org", "member-name", "--org", org, "--user", bob.id, "--name", "Not Bob")[1]["ok"] is True
    assert shown_member(tool, org, bob.id)["name"] == "Bob"
    # the own name of another org is the account's; an admin-given one never is
    assert Account.open(Home.from_env(carol_tool.env)).display_name() == "Carol"


def test_member_name_by_id_code_and_invitation(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    admit(tool, org, bob.id, "Bob")
    code = format_account_code(account_code(bob.id, bob.identity.public()))
    changed = tool.run("org", "member-name", "--org", org, "--user", code.lower(), "--name", "Robert")[1]
    assert (changed["subject"], changed["name"]) == (bob.id, "Robert")
    assert shown_member(tool, org, bob.id)["name"] == "Robert"
    invited = tool.run("org", "invite", "--org", org, "--name", "Dora")[1]
    assert (invited["name"], invited["name_saved"]) == ("Dora", True)
    plain = tool.run("org", "invite", "--org", org)[1]
    assert "name" not in plain
    shown = tool.run("org", "show", "--org", org)[1]
    names = {item["invite_id"]: item["name"] for item in shown["open_invites"]}
    assert names == {invited["invite_id"]: "Dora", plain["invite_id"]: None}
    assert {item["invite_id"]: item["name"] for item in shown["invites"]}[invited["invite_id"]] == "Dora"
    renamed = tool.run("org", "member-name", "--org", org, "--user", plain["invite_id"], "--name", "Emil")[1]
    assert renamed["subject"] == plain["invite_id"]
    shown = tool.run("org", "show", "--org", org)[1]
    assert {item["invite_id"]: item["name"] for item in shown["open_invites"]}[plain["invite_id"]] == "Emil"


@pytest.mark.parametrize(
    ("who", "name", "error"),
    [
        ("nope", "X", "subject_unknown"),
        ("ABCD-EFGH-IJK9", "X", "subject_unknown"),
        ("AAAA-AAAA-AAAA", "X", "subject_unknown"),
        ("0190f000-0000-7000-8000-0000000000aa", "X", "subject_unknown"),
        ("AAAAAAAAAAAAAAAAAAAAAA", "X", "subject_unknown"),
        ("{bob}", "   ", None),
        ("{bob}", "x" * 81, None),
    ],
)
def test_member_name_refusals(  # noqa: PLR0917 - fixtures and parameters
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, who: str, name: str, error: str | None
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    admit(tool, org, bob.id, "Bob")
    result = tool.run("org", "member-name", "--org", org, "--user", who.replace("{bob}", bob.id), "--name", name)[1]
    assert (result["error"], result.get("reason")) == ("invalid_argument", error)


def test_only_admins_give_names_and_devices_are_no_subjects(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, bob_tool = paired(hub, tmp_path, "Bob")
    admit(tool, org, bob.id, "Bob")
    assert bob_tool.run("org", "member-name", "--org", org, "--user", account.id, "--name", "X")[1]["error"] == (
        "not_admin"
    )
    # the hub (and every reader) refuses a `member.name` of a non-admin or about a non-user (E5, header `subject`)
    opened, key = org_key(bob_tool, org)
    lh = opened.org_log(org).head.seq
    forged = orgs.profile_event(
        opened, org, key, "member.name", {"display_name": "Ada?"}, epoch=1, lh=lh, ref=account.id
    )
    with pytest.raises(PlatformError) as refused:
        opened.call("POST", "/v1/events", {"events": [{"event": forged}]})
    assert refused.value.code == "hub_refused"


def test_admit_closes_the_invitation_the_admitted_account_registered_with(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    carol, _ = paired(hub, tmp_path, "Carol")
    for_bob = tool.run("org", "invite", "--org", org, "--name", "Bob")[1]["invite_id"]
    for_carol = tool.run("org", "invite", "--org", org)[1]["invite_id"]
    hub.invites[for_bob] |= {"registered": True, "registered_user": bob.id}
    hub.invites[for_carol] |= {"registered": True, "registered_user": carol.id}
    statuses = {item["invite_id"]: item["status"] for item in tool.run("org", "show", "--org", org)[1]["invites"]}
    assert statuses == {for_bob: "registered", for_carol: "registered"}
    admitted = admit(tool, org, bob.id, "Bob")
    assert admitted["closed_invites"] == [for_bob]
    assert for_bob not in hub.invites
    state = hub.orgs[org].log.state
    assert state.invites[for_bob].revoked_seq is not None
    # Carol is admitted by a raw admission without closing: her invitation shows as settled
    undo = intercept(hub, REVOKE, lambda request, body, org, invite: (503, {"error": "unavailable"}))
    assert admit(tool, org, carol.id, "Carol")["closed_invites"] == []
    undo()
    shown = tool.run("org", "show", "--org", org)[1]
    row = next(item for item in shown["invites"] if item["invite_id"] == for_carol)
    assert (row["status"], row["registered_user"], row["name"]) == ("settled", carol.id, None)
    assert names_module.close_settled_invites(Account.open(Home.from_env(tool.env)), org, bob.id) == []
    assert names_module.close_settled_invites(Account.open(Home.from_env(tool.env)), org, carol.id) == [for_carol]


def test_a_name_that_cannot_be_saved_leaves_the_admission_and_the_invitation(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")

    def refuse(request: Request, body: Any) -> tuple[int, Any]:
        return 503, {"error": "unavailable"}

    undo = intercept(hub, EVENTS, refuse)
    admitted = admit(tool, org, bob.id, "Bob")
    assert (admitted["name_saved"], admitted["name_error"]) == (False, "hub_refused")
    assert hub.orgs[org].log.state.member(bob.id) is not None
    invited = tool.run("org", "invite", "--org", org, "--name", " Dora ")[1]
    assert (invited["ok"], invited["name"], invited["name_saved"]) == (True, "Dora", False)
    undo()
    intercept(hub, EVENTS, lambda request, body: (200, {"results": []}))
    assert tool.run("org", "member-name", "--org", org, "--user", bob.id, "--name", "Bob")[1]["reason"] == (
        "response_invalid"
    )
    # a refused name is refused before anything is appended
    before = hub.orgs[org].log.head.seq
    assert tool.run("org", "invite", "--org", org, "--name", "")[1]["error"] == "invalid_argument"
    carol, _ = paired(hub, tmp_path, "Carol")
    assert tool.run("org", "admit", "--org", org, "--user", carol.id, "--name", " ")[1]["error"] == "invalid_argument"
    assert hub.orgs[org].log.head.seq == before


def test_own_name_needs_another_org_and_the_org_key(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    # Dan has no org at all: no own name to take
    dan = hub.add_user(private_org=False, anchor="certify")
    dan_tool = make_tool(tmp_path / "dan")
    pair_user(dan_tool, hub, dan)
    admit(tool, org, dan.id, "Dan")
    assert dan_tool.run("org", "show", "--org", org)[1]["own_name"] == "unknown"
    assert shown_member(tool, org, dan.id)["name_source"] == "admin"
    # Eve's agent does not hold the current org key yet
    eve, eve_tool = paired(hub, tmp_path, "Eve")
    admit(tool, org, eve.id, "Eve")
    hub.wraps = [wrap for wrap in hub.wraps if not (wrap["scope_id"] == org and wrap["recipient_id"] == eve.id)]
    assert eve_tool.run("org", "show", "--org", org)[1]["own_name"] == "no_key"
    # other refusals of the hub are not swallowed
    intercept(hub, EVENTS, lambda request, body: (403, {"error": "forbidden"}))
    assert tool.run("org", "show", "--org", org)[1]["own_name"] == "present"
    frank, frank_tool = paired(hub, tmp_path, "Frank")
    admit(tool, org, frank.id, "Frank")
    assert frank_tool.run("org", "show", "--org", org)[1]["own_name"] == "failed"


def test_names_resolve_against_the_current_log() -> None:
    """Admin-given names count only from current admins, during the member's current membership; the invitation's
    name follows the member who joined with it; the own name always ranks first."""

    class Principal:
        def __init__(self, id: str, kind: str, joined_seq: int) -> None:
            self.id, self.kind, self.joined_seq = id, kind, joined_seq

    class Invite:
        def __init__(self, invite_id: str, kind: str, accepted_seq: int | None) -> None:
            self.invite_id, self.kind, self.accepted_seq = invite_id, kind, accepted_seq

    class State:
        principals: ClassVar[dict[str, Principal]] = {
            "ann": Principal("ann", "user", 0),
            "bob": Principal("bob", "user", 5),
            "cy": Principal("cy", "user", 7),
            "dev": Principal("dev", "device", 2),
            "eve": Principal("eve", "user", 9),
        }
        invites: ClassVar[dict[str, Invite]] = {
            "inv-cy": Invite("inv-cy", "user", 7),
            "inv-dev": Invite("inv-dev", "device", 2),
            "inv-open": Invite("inv-open", "user", None),
        }

        def is_admin(self, principal: str) -> bool:
            return principal == "ann"

    names = Names(own={"ann": "Ann"})
    names.given = {
        "ann": [("Not Ann", "ann", 1)],
        "bob": [("Old Bob", "ann", 3), ("Bob", "ann", 5), ("By Bob", "bob", 6)],
        "inv-cy": [("Cy", "ann", 6)],
        "inv-dev": [("Device", "ann", 2)],
        "inv-open": [("Open", "ann", 8), ("Gone admin", "zed", 9)],
        "dev": [("Device", "ann", 3)],
        "eve": [("Too early", "ann", 8)],
    }
    resolved = names.resolve(State())  # type: ignore[arg-type]
    assert resolved.members == {"ann": "Ann", "bob": "Bob", "cy": "Cy"}
    assert resolved.sources == {
        "ann": {"source": "own", "by": None},
        "bob": {"source": "admin", "by": "ann"},
        "cy": {"source": "invitation", "by": "ann"},
    }
    assert resolved.invitations == {"inv-cy": {"name": "Cy", "by": "ann"}, "inv-open": {"name": "Open", "by": "ann"}}


def test_naming_without_the_org_key_and_a_wrong_revocation_answer(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    invite = tool.run("org", "invite", "--org", org)[1]["invite_id"]
    hub.invites[invite] |= {"registered": True, "registered_user": bob.id}
    undo = intercept(hub, REVOKE, lambda request, body, org, invite: (200, {"seq": -1}))
    assert admit(tool, org, bob.id, "Bob")["closed_invites"] == []
    undo()
    # the admin's agent no longer holds the current org key: the name cannot be sealed
    hub.wraps = [wrap for wrap in hub.wraps if not (wrap["scope_id"] == org and wrap["recipient_id"] == account.id)]
    refused = tool.run("org", "member-name", "--org", org, "--user", bob.id, "--name", "Bob")[1]
    assert (refused["error"], refused["reason"]) == ("hub_refused", "key_missing")
