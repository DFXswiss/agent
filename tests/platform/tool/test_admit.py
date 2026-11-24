"""Direct admission of an existing account (04-crypto.md sections 4.7, 4.8 and 5.13, ADR-060): `org admit` proves the
identities of both sides against the platform log (never trusting the hub's identity answer), appends `admit` with the
wraps of the keys the admin holds, and the admitted user's own agent then trusts and shows the org. `org invitees`
lists the accounts of the user's own platform invitations with their verified identities. Admins read every workgroup
by inheritance (ADR-058): the lists show it, and `wg ungrant` touches the explicit access only."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_platform import orgs
from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.identity import account_code, format_account_code
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.state import ORG, WG

from .conftest import Tool, make_tool, pair_user
from .fakes.admission import add_invitee
from .hubfake import HubFake, Json, Request, User


def team(tool: Tool, name: str = "Acme") -> str:
    code, document = tool.run("org", "create", "--name", name)
    assert code == 0, document
    return str(document["org_id"])


def paired(hub: HubFake, root: Path, name: str, anchor: str | None = "certify") -> tuple[User, Tool]:
    """Another existing user with its own agent on another computer."""
    user = hub.add_user(display_name=name, anchor=anchor)
    other = make_tool(root / name.lower())
    pair_user(other, hub, user)
    return user, other


def forge_identity(hub: HubFake, change: Any) -> None:
    """The hub answers identity requests with `change(answer, user)`."""
    original = hub.r_user_identity

    def forged(request: Request, body: Any, user: str) -> tuple[int, Any]:
        status, answer = original(request, body, user)
        return status, change(answer, user)

    hub.override("r_user_identity", forged)


def last_body(hub: HubFake, org: str) -> Json:
    return cast(Json, hub.orgs[org].log.entries[-1]["body"])


def anchor_of(user: User) -> Json:
    assert user.anchor is not None
    return user.anchor


def wraps_for(hub: HubFake, user: User, scope_id: str) -> list[Json]:
    return [wrap for wrap in hub.wraps if wrap["recipient_id"] == user.id and wrap["scope_id"] == scope_id]


def test_admit_by_user_id_with_workgroups(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    default_wg = hub.orgs[org].log.state.default_wg
    held = tool.run("wg", "create", "--org", org, "--name", "Research")[1]["wg_id"]
    unheld = tool.run("wg", "create", "--org", org, "--name", "Finance")[1]["wg_id"]
    # the admin is no reader of `unheld` any more on this computer: its key arrives for Bob by catch-up
    hub.wraps = [wrap for wrap in hub.wraps if not (wrap["scope_id"] == unheld and wrap["recipient_id"] == account.id)]
    bob, bob_tool = paired(hub, tmp_path, "Bob")
    argv = ["org", "admit", "--org", org, "--user", bob.id, "--wg", f"{held}:read", "--wg", unheld]
    code, admitted = tool.run(*argv, "--wg", f"{default_wg}:read")
    assert code == 0, admitted
    assert admitted["user_id"] == bob.id
    assert admitted["account_code"] == format_account_code(account_code(bob.id, bob.identity.public()))
    assert admitted["role"] == "member"
    assert admitted["wgs"] == dict(sorted({held: "read", unheld: "write"}.items()))  # as appended (no default wg)
    state = hub.orgs[org].log.state
    assert admitted["seq"] == hub.orgs[org].log.head.seq
    member = state.member(bob.id)
    assert member is not None
    assert member.role == "member"
    assert (state.wg_access(bob.id, held), state.wg_access(bob.id, unheld)) == ("read", "write")
    body = last_body(hub, org)
    assert body["type"] == "admit"
    assert body["wgs"] == {held: "read", unheld: "write"}
    assert (body["platform_seq"], body["id_sig"]) == (anchor_of(bob)["platform_seq"], "")
    assert (body["by_platform_seq"], body["by_id_sig"]) == (anchor_of(account)["platform_seq"], "")
    assert {w["scope"] for w in wraps_for(hub, bob, org)} == {ORG}
    assert {w["scope"] for w in wraps_for(hub, bob, default_wg)} == {WG}
    assert len(wraps_for(hub, bob, held)) == 1
    assert wraps_for(hub, bob, unheld) == []
    # Bob did nothing; his own agent trusts the org through the admit entry and shows it
    listed = {item["org_id"]: item for item in bob_tool.run("org", "list")[1]["orgs"]}
    assert listed[org]["name"] == "Acme"
    shown = bob_tool.run("org", "show", "--org", org)[1]
    assert shown["name"] == "Acme"
    assert {member["principal_id"] for member in shown["members"]} == {account.id, bob.id}


def test_an_invitation_into_an_org_with_an_admitted_member(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    """The log of the lookup holds an `admit`, whose identities are anchored in the platform log: the joining agent
    reads the platform log to verify it, as the web's `needsPlatform` does."""
    hub.anchor_user(account)
    org = team(tool)
    bob, _bob_tool = paired(hub, tmp_path, "Bob")
    assert tool.run("org", "admit", "--org", org, "--user", bob.id)[1]["ok"] is True
    invited = tool.run("org", "invite", "--org", org)[1]
    carol, carol_tool = paired(hub, tmp_path, "Carol", anchor=None)
    joined = carol_tool.run("org", "join", stdin=invited["invite_link"])[1]
    assert (joined["ok"], joined["org_id"]) == (True, org)
    assert hub.orgs[org].log.state.member(carol.id) is not None


def test_admit_by_account_code_as_admin(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    # Carol registered through a platform invitation: her identity is anchored by the identity proof
    carol, carol_tool = paired(hub, tmp_path, "Carol", anchor="invite")
    code = carol_tool.run("status")[1]["account_code"]
    assert code == format_account_code(account_code(carol.id, carol.identity.public()))
    admitted = tool.run("org", "admit", "--org", org, "--user", code.lower(), "--role", "admin")[1]
    assert (admitted["user_id"], admitted["role"], admitted["account_code"]) == (carol.id, "admin", code)
    assert hub.orgs[org].log.state.is_admin(carol.id)
    body = last_body(hub, org)
    assert body["id_sig"] == anchor_of(carol)["id_sig"] != ""
    lookups = [r for r in hub.requests if r.path == "/v1/users/lookup"]
    assert lookups[-1].query == {"code": code.replace("-", "")}
    shown = carol_tool.run("org", "show", "--org", org)[1]
    assert shown["role"] == "admin"
    # as admin Carol admits Dave herself
    dave, _ = paired(hub, tmp_path, "Dave")
    assert carol_tool.run("org", "admit", "--org", org, "--user", dave.id)[1]["ok"] is True
    assert hub.orgs[org].log.state.member(dave.id) is not None


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda answer, user: {**answer, "pub": Identity.generate().public()}, id="other-keys"),
        pytest.param(lambda answer, user: {**answer, "platform_seq": answer["platform_seq"] + 1}, id="wrong-seq"),
        pytest.param(lambda answer, user: {**answer, "platform_seq": True}, id="bool-seq"),
        pytest.param(lambda answer, user: {**answer, "platform_seq": "1"}, id="text-seq"),
        pytest.param(lambda answer, user: {**answer, "id_sig": "x"}, id="proof-on-certify"),
        pytest.param(lambda answer, user: {k: v for k, v in answer.items() if k != "id_sig"}, id="missing"),
        pytest.param(lambda answer, user: {**answer, "pub": None}, id="pub-not-object"),
        pytest.param(lambda answer, user: {**answer, "pub": {"kem": answer["pub"]["kem"]}}, id="pub-incomplete"),
    ],
)
def test_forged_identity_answers_are_refused(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, change: Any
) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    head = hub.orgs[org].log.head
    forge_identity(hub, lambda answer, user: change(answer, user) if user == bob.id else answer)
    result = tool.run("org", "admit", "--org", org, "--user", bob.id)[1]
    assert (result["error"], result["reason"]) == ("hub_refused", "identity_unproven")
    assert hub.orgs[org].log.head == head


def test_identity_of_another_user_is_refused(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    carol, _ = paired(hub, tmp_path, "Carol")
    # a correctly anchored identity, but of Carol, for the request about Bob
    forge_identity(hub, lambda answer, user: hub.identity_answer(carol) if user == bob.id else answer)
    assert tool.run("org", "admit", "--org", org, "--user", bob.id)[1]["reason"] == "identity_unproven"
    hub.restore("r_user_identity")
    # the hub resolves Bob's code to Carol's (anchored) account: the code of the proven keys does not match
    code = format_account_code(account_code(bob.id, bob.identity.public()))
    hub.hooks[("GET", "/v1/users/lookup")] = lambda body, request: (200, hub.identity_answer(carol))
    assert tool.run("org", "admit", "--org", org, "--user", code)[1]["reason"] == "identity_unproven"
    assert hub.orgs[org].log.state.member(carol.id) is None


def test_admit_refusals(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, bob_tool = paired(hub, tmp_path, "Bob")
    assert tool.run("org", "admit", "--org", org, "--user", "nope")[1]["error"] == "invalid_argument"
    assert tool.run("org", "admit", "--org", org, "--user", "ABCD-EFGH-IJK9")[1]["error"] == "invalid_argument"
    assert tool.run("org", "admit", "--org", org, "--user", bob.id, "--wg", "x")[1]["error"] == "invalid_argument"
    # an unknown code and an unanchored account: the hub's refusal is passed through
    assert tool.run("org", "admit", "--org", org, "--user", "AAAA-AAAA-AAAA")[1]["hub_error"] == "identity_unproven"
    stranger, _ = paired(hub, tmp_path, "Eve", anchor=None)
    assert tool.run("org", "admit", "--org", org, "--user", stranger.id)[1]["hub_error"] == "identity_unproven"
    # an archived workgroup
    wg = tool.run("wg", "create", "--org", org, "--name", "Old")[1]["wg_id"]
    hub.append(org, account, [{"type": "wg_archive", "wg": wg}])
    assert tool.run("org", "admit", "--org", org, "--user", bob.id, "--wg", wg)[1]["error"] == "invalid_argument"
    unknown = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    assert tool.run("org", "admit", "--org", org, "--user", bob.id, "--wg", unknown)[1]["error"] == "invalid_argument"
    # a role outside the two (the command line allows only these; the function checks it too)
    opened = Account.open(Home.from_env(tool.env))
    try:
        with pytest.raises(PlatformError) as refused:
            orgs.org_admit(opened, org, bob.id, "owner", {})
    finally:
        opened.close()
    assert refused.value.code == "invalid_argument"
    # admitted once; a second time is refused before anything is sent
    assert tool.run("org", "admit", "--org", org, "--user", bob.id)[1]["ok"] is True
    again = tool.run("org", "admit", "--org", org, "--user", bob.id)[1]
    assert (again["error"], again["reason"]) == ("invalid_argument", "already_member")
    # Bob is a member, no admin
    carol, _ = paired(hub, tmp_path, "Carol")
    assert bob_tool.run("org", "admit", "--org", org, "--user", carol.id)[1]["error"] == "not_admin"
    assert hub.orgs[org].log.state.member(carol.id) is None


def test_own_identity_must_be_the_own_keys(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    # the admin's own identity is not anchored: the hub refuses
    assert tool.run("org", "admit", "--org", org, "--user", bob.id)[1]["hub_error"] == "identity_unproven"
    # the platform log anchors other keys for the admin's ID: proven, but not this agent's identity
    impostor = User(account.id, Identity.generate())
    hub.anchor_user(impostor)
    hub.override(
        "r_user_identity",
        lambda request, body, user: (200, hub.identity_answer(impostor if user == account.id else hub.users[user])),
    )
    result = tool.run("org", "admit", "--org", org, "--user", bob.id)[1]
    assert (result["error"], result["reason"]) == ("hub_refused", "identity_mismatch")


def test_admit_seq_conflict(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "seq_conflict"}) if len(calls) == 1 else None

    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = conflict
    assert tool.run("org", "admit", "--org", org, "--user", bob.id)[1]["ok"] is True
    assert len(calls) == 2
    carol, _ = paired(hub, tmp_path, "Carol")
    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = lambda b, r: (200, {"head": {"seq": 99, "hash": "x"}})
    assert tool.run("org", "admit", "--org", org, "--user", carol.id)[1]["reason"] == "response_invalid"


def test_admit_refused_by_the_log_rules(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    bob, _ = paired(hub, tmp_path, "Bob")
    # without the default workgroup key the admin cannot give Bob the wraps `admit` owes
    default_wg = hub.orgs[org].log.state.default_wg
    hub.wraps = [w for w in hub.wraps if not (w["scope_id"] == default_wg and w["recipient_id"] == account.id)]
    result = tool.run("org", "admit", "--org", org, "--user", bob.id)[1]
    assert (result["error"], result["reason"]) == ("hub_refused", "epoch_stale")


def test_invitees(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    carol, _ = paired(hub, tmp_path, "Carol", anchor="invite")
    dave, _ = paired(hub, tmp_path, "Dave", anchor=None)
    erin, _ = paired(hub, tmp_path, "Erin")
    frank, _ = paired(hub, tmp_path, "Frank")
    add_invitee(hub, account, carol, minutes_ago=3)
    add_invitee(hub, account, dave, minutes_ago=2)
    add_invitee(hub, account, erin, minutes_ago=1)
    add_invitee(hub, frank, account)  # not the user's invitation
    code, listed = tool.run("org", "invitees")
    assert code == 0, listed
    rows = listed["invitees"]
    assert [row["user_id"] for row in rows] == [erin.id, dave.id, carol.id]
    assert [row["admissible"] for row in rows] == [True, False, True]
    assert rows[2]["account_code"] == format_account_code(account_code(carol.id, carol.identity.public()))
    assert rows[1]["account_code"] is None
    assert listed["org_id"] is None
    # for an org: only the proven accounts that are no member yet; an admitted one leaves the list
    assert tool.run("org", "admit", "--org", org, "--user", erin.id)[1]["ok"] is True
    scoped = tool.run("org", "invitees", "--org", org)[1]
    assert [row["user_id"] for row in scoped["invitees"]] == [carol.id]
    assert tool.run("org", "invitees", "--org", "x")[1]["error"] == "invalid_argument"


def test_invitees_answers_are_verified(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    carol, _ = paired(hub, tmp_path, "Carol", anchor="invite")
    bob, _ = paired(hub, tmp_path, "Bob")
    proven = hub.identity_answer(carol)
    rows: list[Any] = [
        {"user_id": carol.id, "registered_at": "t", "identity": proven},
        {"user_id": bob.id, "registered_at": "t", "identity": proven},  # Carol's identity for Bob
        {"user_id": bob.id, "registered_at": "t", "identity": {**proven, "user_id": bob.id}},
        {"user_id": bob.id, "registered_at": "t"},
        {"user_id": bob.id, "registered_at": 5, "identity": None},
        {"user_id": None, "registered_at": "t", "identity": None},
        "x",
    ]
    hub.hooks[("GET", "/v1/me/invitees")] = lambda body, request: (200, {"invitees": rows})
    listed = tool.run("org", "invitees")[1]["invitees"]
    assert [(row["user_id"], row["admissible"]) for row in listed] == [
        (carol.id, True),
        (bob.id, False),
        (bob.id, False),
        (bob.id, False),
    ]
    hub.hooks[("GET", "/v1/me/invitees")] = lambda body, request: (200, {"invitees": None})
    assert tool.run("org", "invitees")[1]["invitees"] == []


def test_admins_read_every_workgroup_by_inheritance(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    hub.anchor_user(account)
    org = team(tool)
    research = tool.run("wg", "create", "--org", org, "--name", "Research")[1]["wg_id"]
    bob, bob_tool = paired(hub, tmp_path, "Bob")
    assert tool.run("org", "admit", "--org", org, "--user", bob.id, "--role", "admin")[1]["ok"] is True
    # Bob reads Research only as admin: shown as inherited, as the web's member list does
    members = {row["user_id"]: row for row in tool.run("member", "list", "--org", org)[1]["members"]}
    access = {item["wg_id"]: item for item in members[bob.id]["access"]}
    assert (access[research]["access"], access[research]["inherited"]) == ("read", True)
    default = hub.orgs[org].log.state.default_wg
    assert (access[default]["access"], access[default]["inherited"]) == ("write", False)
    mine = {item["wg_id"]: item for item in members[account.id]["access"]}
    assert mine[research]["inherited"] is False
    workgroups = {row["wg_id"]: row for row in bob_tool.run("wg", "list", "--org", org)[1]["workgroups"]}
    assert (workgroups[research]["my_access"], workgroups[research]["my_access_inherited"]) == ("read", True)
    shown = {row["wg_id"]: row for row in tool.run("org", "show", "--org", org)[1]["workgroups"]}
    assert shown[research]["my_access_inherited"] is False
    # ungrant acts on the explicit access only: nothing to take from an inherited read
    head = hub.orgs[org].log.head
    assert tool.run("wg", "ungrant", "--wg", research, "--user", bob.id)[1]["changed"] is False
    assert hub.orgs[org].log.head == head
    # an explicit grant makes it explicit; ungranting it leaves the inherited read
    assert tool.run("wg", "grant", "--wg", research, "--user", bob.id, "--access", "write")[1]["ok"] is True
    members = {row["user_id"]: row for row in tool.run("member", "list", "--org", org)[1]["members"]}
    access = {item["wg_id"]: item for item in members[bob.id]["access"]}
    assert (access[research]["access"], access[research]["inherited"]) == ("write", False)
    assert tool.run("wg", "ungrant", "--wg", research, "--user", bob.id)[1]["changed"] is True
    state = hub.orgs[org].log.state
    assert (state.wg_access(bob.id, research), state.inherited(bob.id, research)) == ("read", True)
