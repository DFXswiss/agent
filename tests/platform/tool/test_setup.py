"""Setup commands (11-agent-access.md sections 8.3 and 8.4): settings, API key, organizations, invitations, joining,
workgroups, the platform device, diagnose, the generic call; each verifies what it uses, so a forged log, a foreign
wrap or a forged platform log is refused."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from agent_platform import account as account_module
from agent_platform import claude, orgs
from agent_platform.account import Account
from agent_platform.batch import build_batch
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.credential import new_header, open_credential, seal_credential
from agent_runner.crypto.encoding import b64u_encode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.state import ORG, WG
from agent_runner.crypto.log.wraps import WrapSlot

from .conftest import Tool, browser_confirms, make_tool, pair_user, post_form, start_pair
from .hubfake import HubFake, User


def private_org(hub: HubFake, user: User) -> str:
    return next(org.id for org in hub.orgs.values() if org.private and org.log.state.member(user.id))


def team(tool: Tool, name: str = "Acme") -> str:
    code, document = tool.run("org", "create", "--name", name)
    assert code == 0, document
    return str(document["org_id"])


# --- settings and API key ---


def test_settings_set(tool: Tool, hub: HubFake, account: User) -> None:
    document = tool.run("settings", "set", "--model-access", "subscription", "--locale", "de")[1]
    assert document["settings"] == {"model_access": "subscription", "model_id": None, "locale": "de"}
    document = tool.run("settings", "set", "--model", "m-1")[1]
    assert document["settings"] == {"model_access": "subscription", "model_id": "m-1", "locale": "de"}
    assert tool.run("settings", "set", "--model", "")[1]["settings"]["model_id"] is None
    hub.hooks[("GET", "/v1/me")] = lambda b, r: None
    account.settings = "broken"  # type: ignore[assignment]
    hub.hooks.clear()
    original = hub.r_me

    def me(request: Any, body: Any) -> tuple[int, Any]:
        status, answer = original(request, body)
        return status, {**answer, "settings": None}

    hub.override("r_me", me)
    assert tool.run("settings", "set")[1]["settings"] == {"model_access": "auto", "model_id": None, "locale": "en"}


def test_set_api_key(tool: Tool, hub: HubFake, account: User) -> None:
    document = tool.run("credentials", "set-api-key", stdin="sk-test-" + "k" * 20 + "\n")[1]
    assert (document["stored"], document["kind"]) == (True, "anthropic_api_key")
    record = hub.credentials[(account.id, "anthropic_api_key")]["record"]
    assert parse_json(open_credential(account.cred_keys[1], record, account.identity.sig_public())) == {
        "key": "sk-test-" + "k" * 20
    }
    assert "k" * 20 not in json.dumps(document)
    assert tool.run("credentials", "set-api-key", stdin="  ")[1]["error"] == "invalid_argument"
    assert tool.run("credentials", "set-api-key", stdin="has space")[1]["error"] == "invalid_argument"


# --- organizations ---


def test_org_list_create_show(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    listed = tool.run("org", "list")[1]["orgs"]
    assert {item["name"] for item in listed} == {"Private", "Acme"}
    shown = tool.run("org", "show", "--org", org)[1]
    assert shown["name"] == "Acme"
    assert shown["private"] is False
    assert shown["role"] == "admin"
    assert shown["members"] == [{"principal_id": account.id, "kind": "user", "role": "admin", "name": "Ada"}]
    assert [wg["name"] for wg in shown["workgroups"]] == ["Default"]
    assert hub.orgs[org].log.state.founder == account.identity.public()["sig"]


def test_org_create_needs_a_display_name(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(private_org=False)
    pair_user(tool, hub, user)
    assert tool.run("org", "create", "--name", "Acme")[1]["reason"] == "display_name_missing"
    created = tool.run("org", "create", "--name", "Acme", "--display-name", " Bea ")[1]
    shown = tool.run("org", "show", "--org", created["org_id"])[1]
    assert shown["members"][0]["name"] == "Bea"
    assert tool.run("org", "create", "--name", " ")[1]["error"] == "invalid_argument"


def test_org_create_in_german(tool: Tool, hub: HubFake, account: User) -> None:
    account.settings["locale"] = "de"
    org = team(tool)
    assert tool.run("wg", "list", "--org", org)[1]["workgroups"][0]["name"] == "Standard"


def test_org_create_answer_must_match(tool: Tool, hub: HubFake, account: User) -> None:
    hub.hooks[("POST", "/v1/orgs")] = lambda b, r: (200, {"org_id": "x", "default_wg": "y"})
    assert tool.run("org", "create", "--name", "Acme")[1]["reason"] == "response_invalid"


def test_org_show_of_an_unknown_org(tool: Tool, account: User) -> None:
    result = tool.run("org", "show", "--org", "01a1ee56-5e00-7242-9e24-f87629ad2977")[1]
    assert result["hub_error"] == "not_found"


def test_forged_log_is_refused(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    original = hub.r_log

    def forged(request: Any, body: Any, org: str) -> tuple[int, Any]:
        status, answer = original(request, body, org)
        entry = answer["entries"][0]["entry"]
        answer["entries"][0]["entry"] = {**entry, "sig": entry["sig"][::-1]}
        return status, answer

    hub.override("r_log", forged)
    result = tool.run("org", "show", "--org", org)[1]
    assert (result["error"], result["reason"]) == ("hub_refused", "log_genesis")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda answer: {**answer, "head": {"seq": 9, "hash": "x"}}, "log_chain"),
        (lambda answer: {**answer, "entries": []}, "log_genesis"),
        (lambda answer: {**answer, "entries": "x"}, "response_invalid"),
        (lambda answer: {**answer, "head": None}, "response_invalid"),
        (lambda answer: {**answer, "next_after": "x"}, "response_invalid"),
        (lambda answer: {**answer, "entries": [*answer["entries"], {"entry": {"body": "x"}}]}, "header_invalid"),
    ],
)
def test_bad_log_pages(
    tool: Tool, hub: HubFake, account: User, change: Callable[[dict[str, Any]], Any], reason: str
) -> None:
    org = private_org(hub, account)
    original = hub.r_log
    hub.override("r_log", lambda request, body, org: (200, change(original(request, body, org)[1])))
    assert tool.run("org", "show", "--org", org)[1]["reason"] == reason


def test_log_pages_are_followed(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_module, "PAGE", 1)
    org = team(tool)
    tool.run("wg", "create", "--org", org, "--name", "One")
    tool.run("wg", "create", "--org", org, "--name", "Two")
    assert len(tool.run("wg", "list", "--org", org)[1]["workgroups"]) == 3
    original = hub.r_log

    def stuck(request: Any, body: Any, org: str) -> tuple[int, Any]:
        status, answer = original(request, body, org)
        return status, {**answer, "next_after": 0 if request.query.get("after") else answer["next_after"]}

    hub.override("r_log", stuck)
    assert tool.run("wg", "list", "--org", org)[1]["reason"] == "response_invalid"


def test_log_of_another_founder_is_untrusted(tool: Tool, hub: HubFake, account: User) -> None:
    """A hub that writes an org in which the user "accepted" with a key that is not the user's: the log verifies
    (L1-L8), but neither genesis nor an accept of the user carries the user's key, so the founder is untrusted."""
    stranger = hub.add_user(private_org=False)
    org = hub.create_org(stranger, private=False, name="Trap", display_name="Eve")
    found = hub.orgs[org]
    code = invite_format.generate_code()
    secrets_of = invite_format.invite_secrets(code)
    invite = {
        "type": "invite",
        "invite_id": secrets_of.invite_id,
        "pk_inv": secrets_of.pk_inv,
        "kind": "user",
        "role": "member",
        "wgs": {},
        "expires": "2999-01-01T00:00:00.000Z",
    }
    ts = "2026-01-01T00:00:00.000Z"
    found.log = build_batch(
        found.log, org=org, author=stranger.id, identity=stranger.identity, fields=[invite], ts=ts, held={}
    ).log
    fake = Identity.generate()
    accept = {"type": "accept", "invite_id": secrets_of.invite_id, "subject": account.id, "kind": "user"}
    built = build_batch(
        found.log,
        org=org,
        author=account.id,
        identity=fake,
        fields=[{**accept, "pub": fake.public()}],
        ts=ts,
        held={(ORG, org, 1): hub.org_key(org, stranger), (WG, found.log.state.default_wg, 1): b"w" * 32},
        invite=secrets_of.signing,
    )
    found.log = check_batch(found.log, built.entries, built.wraps, wrapped_by=account.id).log
    assert tool.run("org", "show", "--org", org)[1]["reason"] == "founder_untrusted"
    # status skips the org it cannot verify
    assert tool.run("status")[1]["display_name"] == "Ada"


def test_events_must_be_a_list_and_bad_events_are_skipped(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    hub.store_event(org, {"header": {"kind": "org.profile", "author": "nobody"}})
    hub.store_event(org, {"header": {"kind": "org.profile", "author": account.id, "lh": 0, "epoch": 1}, "ct": "x"})
    assert tool.run("org", "show", "--org", org)[1]["name"] == "Private"
    hub.hooks[("GET", f"/v1/scopes/{org}/events")] = lambda b, r: (200, {"events": "x"})
    assert tool.run("org", "show", "--org", org)[1]["reason"] == "response_invalid"
    assert tool.run("org", "list")[1]["orgs"][0]["name"] is None


def test_event_pages_are_followed(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    original = hub.r_events

    def paged(request: Any, body: Any, scope: str) -> tuple[int, Any]:
        status, answer = original(request, body, scope)
        after = int(request.query.get("after", "0"))
        first = answer["events"][:1]
        more = bool(answer["events"][1:])
        return status, {**answer, "events": first, "next_after": first[0]["scope_seq"] if more else after}

    hub.override("r_events", paged)
    shown = tool.run("org", "show", "--org", org)[1]
    assert (shown["name"], shown["members"][0]["name"]) == ("Private", "Ada")


def test_names_of_non_admins_and_wrong_refs_are_ignored(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    key = hub.org_key(org, account)
    hub.add_event(org, account, key, "member.profile", {"display_name": "Mallory"}, lh=0, ref="someone-else")
    hub.add_event(org, account, key, "org.profile", ["not", "an", "object"], lh=0)  # type: ignore[arg-type]
    hub.add_event(org, account, key, "org.profile", {"name": 5}, lh=0)
    shown = tool.run("org", "show", "--org", org)[1]
    assert shown["name"] == "Private"
    assert shown["members"][0]["name"] == "Ada"


# --- invitations and joining ---


def test_invite_and_join(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = tool.run("wg", "create", "--org", org, "--name", "Research")[1]["wg_id"]
    invited = tool.run("org", "invite", "--org", org, "--role", "admin", "--wg", f"{wg}:read")[1]
    assert invited["secret_fields"] == ["invite_link"]
    assert invited["invite_link"].startswith(f"{hub.origin}/invite#")
    code = invited["invite_link"].split("#", 1)[1]
    assert all(code.encode() not in body for body in hub.bodies)
    # the invitee: an existing user with its own agent on another computer
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    joined = other_tool.run("org", "join", stdin=invited["invite_link"] + "&p=x&f=y\n")[1]
    assert (joined["org_id"], joined["role"], joined["org_name"]) == (org, "admin", "Acme")
    state = hub.orgs[org].log.state
    assert state.is_admin(bob.id)
    assert state.wg_access(bob.id, wg) == "read"
    shown = other_tool.run("org", "show", "--org", org)[1]
    assert {member["name"] for member in shown["members"]} == {"Ada", "Bob"}
    # the hub serves wraps of other members too: valid ones for someone else are not used as keys
    hub.override("r_wraps", lambda request, body: (200, {"wraps": hub.wraps}))
    assert tool.run("org", "invite", "--org", org)[1]["ok"] is True
    hub.restore("r_wraps")
    # used once
    again = other_tool.run("org", "join", env={"AGENT_PLATFORM_INVITE": invited["invite_link"]})[1]
    assert again["reason"] == "invite_consumed"
    # the inviter grants Bob write access with history
    granted = tool.run("wg", "grant", "--wg", wg, "--user", bob.id, "--access", "write", "--history")[1]
    assert granted["access"] == "write"
    assert hub.orgs[org].log.state.wg_access(bob.id, wg) == "write"


def test_org_show_lists_the_invitation_states(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KOR-36: the inviter sees per invitation whether it is open, opened, registered or accepted."""
    org = team(tool)
    links = [tool.run("org", "invite", "--org", org)[1] for _ in range(4)]
    untouched, opened, registered, accepted = (str(item["invite_id"]) for item in links)
    hub.invites[opened]["opened_at"] = "2026-11-23T20:00:00.000Z"
    hub.invites[registered]["registered"] = True
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    assert other_tool.run("org", "join", stdin=links[3]["invite_link"])[1]["org_id"] == org
    # an open invitation the hub does not list, or lists as used, is not shown (as in the web client)
    gone = str(tool.run("org", "invite", "--org", org)[1]["invite_id"])
    del hub.invites[gone]
    used = str(tool.run("org", "invite", "--org", org)[1]["invite_id"])
    hub.invites[used]["state"] = "used"
    shown = tool.run("org", "show", "--org", org)[1]
    states = {item["invite_id"]: item for item in shown["invites"]}
    assert [item["invite_id"] for item in shown["invites"]] == [accepted, registered, opened, untouched]
    assert {key: item["status"] for key, item in states.items()} == {
        untouched: "open",
        opened: "opened",
        registered: "registered",
        accepted: "accepted",
    }
    assert states[opened]["opened_at"] == "2026-11-23T20:00:00.000Z"
    assert states[accepted]["accepted_by"] == bob.id
    assert states[accepted]["opened_at"] is not None
    assert states[untouched]["accepted_by"] is None
    assert states[untouched]["role"] == "member"
    # the open ones stay in `open_invites` as before
    assert {item["invite_id"] for item in shown["open_invites"]} == {untouched, opened, registered, gone, used}
    # a member who is not an admin gets no invitation states (the hub list is for admins)
    assert "invites" not in other_tool.run("org", "show", "--org", org)[1]
    # expired invitations (accepted ones too) leave the list
    later = datetime.now(UTC) + timedelta(days=60)
    monkeypatch.setattr(orgs, "datetime", type("Later", (), {"now": staticmethod(lambda tz: later)}))
    assert tool.run("org", "show", "--org", org)[1]["invites"] == []
    monkeypatch.undo()
    # the hub list fails: the command reports it like every other call
    hub.hooks[("GET", f"/v1/orgs/{org}/invites")] = lambda b, r: (503, {"error": "unavailable"})
    assert tool.run("org", "show", "--org", org)[1]["hub_error"] == "unavailable"


def test_join_errors(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    assert other_tool.run("org", "join")[1]["error"] == "invalid_argument"
    assert other_tool.run("org", "join", stdin="https://x/invite#nonsense")[1]["error"] == "invalid_argument"
    original = hub.r_lookup

    def tampered(change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        hub.override("r_lookup", lambda request, body: (200, change(original(request, body)[1])))

    tampered(lambda answer: {**answer, "package_ct": answer["package_ct"][:-4] + "AAAA"})
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["reason"] == "invite_code_invalid"
    tampered(lambda answer: {**answer, "log": None})
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["reason"] == "log_empty"
    tampered(lambda answer: {**answer, "log": answer["log"][:1]})
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["reason"] == "log_rollback"
    tampered(lambda answer: {**answer, "log_head": {"seq": 0, "hash": "x"}})
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["reason"] == "log_chain"
    hub.override("r_lookup", original)
    second = tool.run("org", "invite", "--org", org)[1]
    hub.orgs[org].log = hub.orgs[org].log  # the second invitation stays open
    invite_id = second["invite_id"]
    hub.invites[invite_id]["package_ct"] = hub.invites[invited["invite_id"]]["package_ct"]
    assert other_tool.run("org", "join", stdin=second["invite_link"])[1]["reason"] == "invite_code_invalid"
    assert other_tool.run("org", "join", stdin="https://x/invite#AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA")[1]["hub_error"] == (
        "invite_unknown"
    )


def test_join_seq_conflict_is_retried(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "seq_conflict"}) if len(calls) == 1 else None

    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = conflict
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["ok"] is True
    assert len(calls) == 2
    third = tool.run("org", "invite", "--org", org)[1]
    carol_tool = make_tool(tmp_path / "carol")
    carol = hub.add_user(display_name="Carol")
    pair_user(carol_tool, hub, carol)
    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = lambda b, r: (409, {"error": "seq_conflict"})
    assert carol_tool.run("org", "join", stdin=third["invite_link"])[1]["hub_error"] == "seq_conflict"


def test_join_whose_answer_got_lost(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    """KOR-36: the hub stored the `accept` but its answer got lost; the membership and the verified log say joined."""
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    path = ("POST", f"/v1/orgs/{org}/log")
    sent: list[int] = []

    def refused(body: Any, request: Any) -> tuple[int, Any]:
        sent.append(1)
        return 503, {"error": "unavailable"}

    # not stored, and the membership cannot be read either: the failure is reported
    hub.hooks[path] = refused
    hub.hooks[("GET", "/v1/me")] = lambda body, request: (503, {"error": "unavailable"}) if sent else None
    failed = other_tool.run("org", "join", "--display-name", "Bob", stdin=invited["invite_link"])[1]
    assert failed["hub_error"] == "unavailable"
    del hub.hooks[("GET", "/v1/me")]
    # not stored, and no member: the failure is reported
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["hub_error"] == "unavailable"

    def stored_but_lost(body: Any, request: Any) -> tuple[int, Any]:
        hub.r_log_append(request, body, org)
        return 503, {"error": "unavailable"}

    hub.hooks[path] = stored_but_lost
    joined = other_tool.run("org", "join", stdin=invited["invite_link"])[1]
    assert (joined["ok"], joined["org_id"], joined["role"]) == (True, org, "member")
    assert hub.orgs[org].log.state.member(bob.id) is not None


def test_joined_log_checks_the_accept(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    unused = tool.run("org", "invite", "--org", org)[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["ok"] is True
    log = hub.orgs[org].log
    head = log.head.to_json()

    def joined(user: str, invite: str, package_head: dict[str, Any]) -> Any:
        stub: Any = type(
            "Stub", (), {"user_id": user, "forget": lambda self, o: None, "org_log": lambda self, o: log}
        )()
        return orgs.joined_log(stub, org, invite, package_head)

    assert joined(bob.id, invited["invite_id"], head) is log
    for user, invite, package_head, reason in (
        (bob.id, unused["invite_id"], head, "log_accept"),
        (bob.id, "unknown", head, "log_accept"),
        (account.id, invited["invite_id"], head, "log_accept"),
        (uuid7(), invited["invite_id"], head, "log_accept"),
        (bob.id, invited["invite_id"], {"seq": 1, "hash": "x"}, "log_rollback"),
    ):
        with pytest.raises(PlatformError) as refused:
            joined(user, invite, package_head)
        assert refused.value.fields["reason"] == reason, (user, invite)


def test_join_without_the_org_key_sends_no_profile(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    invited = tool.run("org", "invite", "--org", org)[1]
    secrets_of = invite_format.invite_secrets(orgs.parse_link(invited["invite_link"]))
    package = invite_format.open_package(secrets_of, hub.invites[invited["invite_id"]]["package_ct"])
    package["keys"] = [key for key in cast(list[dict[str, Any]], package["keys"]) if key["scope"] != ORG]
    hub.invites[invited["invite_id"]]["package_ct"] = invite_format.seal_package(secrets_of, package)
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["reason"] == "epoch_stale"


def test_invite_refusals(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    assert tool.run("org", "invite", "--org", org, "--expires-hours", "0")[1]["error"] == "invalid_argument"
    assert tool.run("org", "invite", "--org", org, "--wg", "nope")[1]["error"] == "invalid_argument"
    unknown = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    assert tool.run("org", "invite", "--org", org, "--wg", unknown)[1]["error"] == "invalid_argument"
    hub.hooks[("POST", f"/v1/orgs/{org}/invites")] = lambda b, r: (200, {"invite_id": "x", "seq": 1})
    assert tool.run("org", "invite", "--org", org)[1]["reason"] == "response_invalid"
    hub.hooks.clear()
    # a member who is not admin
    invited = tool.run("org", "invite", "--org", org, "--role", "member")[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    other_tool.run("org", "join", stdin=invited["invite_link"])
    for argv in (
        ["org", "invite", "--org", org],
        ["wg", "create", "--org", org, "--name", "X"],
        ["device", "assign", "--wg", hub.orgs[org].log.state.default_wg],
    ):
        assert other_tool.run(*argv)[1]["error"] == "not_admin", argv
    assert (
        other_tool.run("wg", "grant", "--wg", hub.orgs[org].log.state.default_wg, "--user", bob.id)[1]["error"]
        == "not_admin"
    )


def test_invite_seq_conflict(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "seq_conflict"}) if len(calls) == 1 else None

    hub.hooks[("POST", f"/v1/orgs/{org}/invites")] = conflict
    assert tool.run("org", "invite", "--org", org)[1]["ok"] is True
    assert len(calls) == 2


def test_foreign_wraps_are_ignored(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    # a stranger (not a member) wraps a key for the user: W1/W2 fail, the wrap is ignored, the real one is used
    stranger = hub.add_user()
    hub.add_wrap(stranger, WrapSlot(ORG, org, 1, account.id), account.identity.kem_public(), b"s" * 32)
    assert tool.run("org", "invite", "--org", org)[1]["ok"] is True
    # without the user's own wraps the keys are missing
    hub.wraps = [wrap for wrap in hub.wraps if not (wrap["scope_id"] == org and wrap["recipient_id"] == account.id)]
    hub.add_wrap(stranger, WrapSlot(ORG, org, 1, account.id), account.identity.kem_public(), b"s" * 32)
    assert tool.run("org", "invite", "--org", org)[1]["reason"] == "epoch_stale"
    assert tool.run("wg", "create", "--org", org, "--name", "R")[1]["reason"] == "epoch_stale"
    hub.hooks[("GET", "/v1/wraps")] = lambda b, r: (200, {"wraps": "x"})
    assert tool.run("org", "invite", "--org", org)[1]["reason"] == "response_invalid"


def test_wrap_for_someone_else_is_ignored(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    record = next(wrap for wrap in hub.wraps if wrap["scope"] == WG and wrap["recipient_id"] == account.id)
    hub.wraps.append({**record, "recipient_id": account.id, "scope_id": hub.orgs[org].log.state.default_wg})
    original = hub.r_wraps

    def other_recipient(request: Any, body: Any) -> tuple[int, Any]:
        status, answer = original(request, body)
        return status, {"wraps": [{**w, "recipient_id": "x"} if w["scope"] == WG else w for w in answer["wraps"]]}

    hub.override("r_wraps", other_recipient)
    assert tool.run("org", "invite", "--org", org)[1]["reason"] == "epoch_stale"


# --- workgroups ---


def test_wg_grant_errors(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    wg = hub.orgs[org].log.state.default_wg
    assert tool.run("wg", "grant", "--wg", wg, "--user", "nope")[1]["error"] == "invalid_argument"
    unknown = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    assert tool.run("wg", "grant", "--wg", unknown, "--user", unknown)[1]["error"] == "no_org"
    # a grant to someone who is no member is refused before anything is sent
    assert tool.run("wg", "grant", "--wg", wg, "--user", unknown)[1]["error"] == "not_found"


def test_append_reports_crypto_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    """A batch the log rules refuse is never sent (here: a grant to someone who is no member)."""
    org = team(tool)
    wg = hub.orgs[org].log.state.default_wg
    head = hub.orgs[org].log.head
    opened = Account.open(Home.from_env(tool.env))
    try:
        with pytest.raises(PlatformError) as caught:
            orgs._append(opened, org, [{"type": "grant", "subject": uuid7(), "wg": wg, "access": "read"}])
    finally:
        opened.close()
    assert caught.value.code == "not_admin"
    assert hub.orgs[org].log.head == head


def test_wg_grant_follows_the_web(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    """`grantPlan`: neither the default nor an archived workgroup (`forbidden`); nothing to do for the same access."""
    org = team(tool)
    default = hub.orgs[org].log.state.default_wg
    research = tool.run("wg", "create", "--org", org, "--name", "Research")[1]["wg_id"]
    old = tool.run("wg", "create", "--org", org, "--name", "Old")[1]["wg_id"]
    invited = tool.run("org", "invite", "--org", org)[1]
    other_tool = make_tool(tmp_path / "other")
    bob = hub.add_user(display_name="Bob")
    pair_user(other_tool, hub, bob)
    assert other_tool.run("org", "join", stdin=invited["invite_link"])[1]["ok"] is True
    assert tool.run("wg", "archive", "--wg", old)[1]["changed"] is True
    for wg in (default, old):
        refused = tool.run("wg", "grant", "--wg", wg, "--user", bob.id)[1]
        assert (refused["error"], refused["reason"]) == ("invalid_argument", "forbidden")
    head = hub.orgs[org].log.head
    granted = tool.run("wg", "grant", "--wg", research, "--user", bob.id, "--access", "read")[1]
    assert (granted["changed"], granted["access"], granted["seq"]) == (True, "read", head.seq + 1)
    again = tool.run("wg", "grant", "--wg", research, "--user", bob.id, "--access", "read")[1]
    assert (again["changed"], hub.orgs[org].log.head.seq) == (False, head.seq + 1)
    assert tool.run("wg", "grant", "--wg", research, "--user", bob.id)[1]["changed"] is True
    assert hub.orgs[org].log.state.explicit_access(bob.id, research) == "write"


def test_wg_create_seq_conflict_gives_up(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = lambda b, r: (409, {"error": "seq_conflict"})
    assert tool.run("wg", "create", "--org", org, "--name", "R")[1]["hub_error"] == "seq_conflict"
    hub.hooks[("POST", f"/v1/orgs/{org}/log")] = lambda b, r: (200, {"head": {"seq": 99, "hash": "x"}})
    assert tool.run("wg", "create", "--org", org, "--name", "R")[1]["reason"] == "response_invalid"


# --- platform device ---


def test_device_assign(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = hub.certify_device()
    other, other_seq = hub.certify_device()
    hub.candidates[wg] = [
        "junk",
        {"device_id": device, "platform_seq": True},
        {"device_id": other, "platform_seq": seq},
        {"device_id": device, "platform_seq": seq},
    ]
    document = tool.run("device", "assign", "--wg", wg)[1]
    assert (document["device_id"], document["cred_wrap"]) == (device, True)
    state = hub.orgs[org].log.state
    assert state.assigned(device, wg)
    slots = {(w["scope"], w["scope_id"], w["recipient_id"]) for w in hub.wraps}
    assert {(ORG, org, device), (WG, wg, device), ("cred", account.id, device)} <= slots
    # the next candidate: the assigned device is skipped
    hub.candidates[wg].append({"device_id": other, "platform_seq": other_seq})
    assert tool.run("device", "assign", "--wg", wg)[1]["device_id"] == other
    # and the log with platform devices verifies against the platform log
    assert len(tool.run("org", "show", "--org", org)[1]["workgroups"][0]["devices"]) == 2
    assert tool.run("device", "assign", "--wg", wg)[1]["error"] == "no_device"


def test_device_assign_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    team_org = team(tool)
    team_wg = hub.orgs[team_org].log.state.default_wg
    assert tool.run("device", "assign", "--wg", team_wg)[1]["reason"] == "platform_access_denied"
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    hub.hooks[("GET", f"/v1/workgroups/{wg}/device-candidates")] = lambda b, r: (200, {"candidates": None})
    assert tool.run("device", "assign", "--wg", wg)[1]["reason"] == "response_invalid"
    hub.hooks.clear()
    hub.platform_entries[0] = {**hub.platform_entries[0], "sig": hub.platform_entries[0]["sig"][::-1]}
    assert tool.run("device", "assign", "--wg", wg)[1]["error"] == "hub_refused"


def test_device_assign_platform_head_must_match(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    original = hub.r_platform_log
    hub.override("r_platform_log", lambda r, b: (200, {**original(r, b)[1], "head": {"seq": 7}}))
    assert tool.run("device", "assign", "--wg", wg)[1]["reason"] == "log_chain"


def test_device_assign_with_another_root_key(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    other_root = "A" * 43
    result = tool.run("device", "assign", "--wg", wg, "--platform-root-key", other_root)[1]
    assert result["error"] == "hub_refused"
    assert tool.run("device", "assign", "--wg", wg, "--platform-root-key", "short")[1]["error"] == "invalid_argument"


def test_device_assign_without_root_key(tool: Tool, hub: HubFake, account: User) -> None:
    state_file = tool.home / "state.json"
    state = json.loads(state_file.read_text())
    del state["platform_root_key"]
    state_file.write_text(json.dumps(state))
    wg = hub.orgs[private_org(hub, account)].log.state.default_wg
    assert tool.run("device", "assign", "--wg", wg)[1]["reason"] == "platform_root_key_missing"


def test_device_assign_without_credential_key(tool: Tool, hub: HubFake, capsys: pytest.CaptureFixture[str]) -> None:
    user = hub.add_user(cred=False)
    pair_user(tool, hub, user)
    wg = hub.orgs[private_org(hub, user)].log.state.default_wg
    device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    assert tool.run("device", "assign", "--wg", wg)[1]["cred_wrap"] is False
    assert "no verified credential key" in capsys.readouterr().err


# --- diagnose ---


def test_diagnose(tool: Tool, hub: HubFake, account: User) -> None:
    findings = tool.run("diagnose")[1]
    assert findings["healthy"] is False
    assert {item["reason"] for item in findings["findings"]} == {"claude_missing", "no_device"}
    hub.devices[account.id] = [{"device_id": "d", "online": False}]
    tool.run("settings", "set", "--model-access", "api_key")
    reasons = {item["reason"] for item in tool.run("diagnose")[1]["findings"]}
    assert reasons == {"api_key_missing", "device_offline"}
    tool.run("credentials", "set-api-key", stdin="key-" + "x" * 10)
    hub.devices[account.id] = [{"device_id": "d", "online": True}, "junk"]
    assert tool.run("diagnose")[1] == {"ok": True, "healthy": True, "model_access": "api_key", "findings": []}
    tool.run("settings", "set", "--model-access", "platform")
    assert tool.run("diagnose")[1]["healthy"] is True


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (None, "claude_missing"),
        ({"token": "t", "email": None, "kind": "setup_token"}, None),
        (
            {"token": "t", "email": None, "kind": "login_token", "expires_at": "2000-01-01T00:00:00.000Z"},
            "claude_expired",
        ),
        ({"token": "t", "email": None, "kind": "login_token", "expires_at": "garbage"}, "claude_expired"),
        ({"token": "t", "email": None, "kind": "login_token", "expires_at": "2999-01-01T00:00:00.000Z"}, None),
    ],
)
def test_diagnose_subscription(
    tool: Tool, hub: HubFake, account: User, body: dict[str, Any] | None, reason: str | None
) -> None:
    hub.devices[account.id] = [{"device_id": "d", "online": True}]
    tool.run("settings", "set", "--model-access", "subscription")
    if body is not None:
        opened = Account.open(Home.from_env(tool.env))
        try:
            claude.store(opened, body, replace=True)
        finally:
            opened.close()
    found = [item["reason"] for item in tool.run("diagnose")[1]["findings"]]
    assert found == ([] if reason is None else [reason])


def test_diagnose_unreadable_and_auto(tool: Tool, hub: HubFake, account: User) -> None:
    hub.devices[account.id] = [{"device_id": "d", "online": True}]
    tool.run("credentials", "set-api-key", stdin="key-" + "x" * 10)
    assert tool.run("diagnose")[1]["healthy"] is True  # auto with an API key
    hub.credentials[(account.id, "claude_subscription")] = {
        "record": {"header": {"epoch": 1}, "ct": "x", "sig": "y"},
        "version": 1,
    }
    tool.run("settings", "set", "--model-access", "subscription")
    assert [f["reason"] for f in tool.run("diagnose")[1]["findings"]] == ["claude_unreadable"]


@pytest.mark.parametrize(
    ("task", "reason"),
    [
        ({"task": {"state": "failed"}, "transitions": []}, "task_failed"),
        ({"task": {"state": "closed"}}, "task_closed"),
        ({"task": {"state": "working"}}, "task_running"),
        ({"task": {"state": "scheduling"}, "transitions": [{"reason": "capacity_exhausted"}]}, "capacity_exhausted"),
        ({"task": {"state": "scheduling"}, "transitions": [{"reason": "<script>"}]}, "task_waiting"),
        ({"task": "x", "transitions": ["x"]}, "task_waiting"),
    ],
)
def test_diagnose_task(tool: Tool, hub: HubFake, account: User, task: dict[str, Any], reason: str) -> None:
    task_id = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    hub.tasks[task_id] = task
    findings = tool.run("diagnose", "--task", task_id)[1]["findings"]
    assert findings[-1]["reason"] == reason
    assert "<script>" not in json.dumps(findings)


def test_diagnose_unknown_task_and_no_org(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(private_org=False)
    pair_user(tool, hub, user)
    result = tool.run("diagnose", "--task", "01a1ee56-5e00-7242-9e24-f87629ad2977")[1]
    assert (result["error"], result["hub_error"]) == ("invalid_argument", "not_found")
    hub.hooks[("GET", "/v1/tasks/01a1ee56-5e00-7242-9e24-f87629ad2977")] = lambda b, r: (403, {"error": "forbidden"})
    assert tool.run("diagnose", "--task", "01a1ee56-5e00-7242-9e24-f87629ad2977")[1]["hub_error"] == "forbidden"
    assert "no_org" in {item["reason"] for item in tool.run("diagnose")[1]["findings"]}


# --- the generic call, status, display name ---


def test_api(tool: Tool, hub: HubFake, account: User) -> None:
    assert tool.run("api", "get", "/v1/me")[1]["answer"]["user_id"] == account.id
    body = '{"model_access":"auto","model_id":null,"locale":"de"}'
    assert tool.run("api", "PUT", "/v1/me/settings", "--body", body)[1]["answer"]["locale"] == "de"
    assert tool.run("api", "PUT", "/v1/me/settings", "--body", "-", stdin=body)[1]["ok"] is True
    for argv in (["TRACE", "/v1/me"], ["GET", "v1/me"], ["PUT", "/v1/me/settings", "--body", "{"]):
        assert tool.run("api", *argv)[1]["error"] == "invalid_argument"
    assert tool.run("api", "GET", "/v1/nothing")[1]["hub_error"] == "not_found"


def test_status_with_unverifiable_profiles(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    hub.hooks[("GET", f"/v1/scopes/{org}/events")] = lambda b, r: (403, {"error": "forbidden"})
    hub.credentials[(account.id, "github")] = {"record": {"header": {"epoch": 1}}, "version": 3}
    original = hub.r_credentials
    hub.override("r_credentials", lambda r, b: (200, {"credentials": [*original(r, b)[1]["credentials"], "junk"]}))
    status = tool.run("status")[1]
    assert status["display_name"] is None
    assert status["credentials"] == [{"kind": "github", "version": 3, "epoch": 1, "updated_at": "x"}]


def test_identity_mismatch_in_me(tool: Tool, hub: HubFake, account: User) -> None:
    other = hub.add_user()
    original = hub.r_me
    hub.override(
        "r_me", lambda r, b: (200, {**original(r, b)[1], "identity": {"kem_pub": other.identity.public()["kem"]}})
    )
    assert tool.run("status")[1]["reason"] == "identity_mismatch"


def test_crypto_refusal_mapping() -> None:
    assert orgs.crypto_refusal(CryptoError("log_rights")).code == "not_admin"
    assert orgs.crypto_refusal(CryptoError("epoch_stale")).fields == {"reason": "epoch_stale"}
    with pytest.raises(PlatformError):
        orgs.display_name("x" * 81)


def test_status_without_any_display_name(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(display_name="")
    pair_user(tool, hub, user)
    hub.create_org(user, private=False, name="Second", display_name="")
    assert tool.run("status")[1]["display_name"] is None


def test_record_signed_by_the_user_for_another_author_is_unreadable(tool: Tool, hub: HubFake, account: User) -> None:
    header = new_header(
        id="01a1ee56-5e00-7242-9e24-f87629ad2977",
        owner=account.id,
        kind="claude_subscription",
        version=1,
        epoch=1,
        author="01a1ee56-5e00-7242-9e24-f87629ad2978",
        ts="2026-01-01T00:00:00.000Z",
    )
    body = body_json({"token": "t", "email": None, "kind": "login_token"})
    record = seal_credential(account.cred_keys[1], header, body, account.identity.sig)
    hub.credentials[(account.id, "claude_subscription")] = {"record": record, "version": 1}
    row = tool.run("status")[1]["credentials"][0]
    assert (row["kind"], row["token_kind"]) == ("claude_subscription", None)


def test_join_a_device_invitation_is_refused(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    found = hub.orgs[org]
    code = invite_format.generate_code()
    secrets_of = invite_format.invite_secrets(code)
    wg = found.log.state.default_wg
    entry = {
        "type": "invite",
        "invite_id": secrets_of.invite_id,
        "pk_inv": secrets_of.pk_inv,
        "kind": "device",
        "wgs": {wg: "device"},
        "expires": "2999-01-01T00:00:00.000Z",
    }
    built = build_batch(
        found.log,
        org=org,
        author=account.id,
        identity=account.identity,
        fields=[entry],
        ts="2026-01-01T00:00:00.000Z",
        held={},
    )
    found.log = built.log
    package = {
        "org": org,
        "org_name": "Acme",
        "wg_names": {},
        "founder": found.log.state.founder,
        "head": found.log.head.to_json(),
        "keys": [{"scope": ORG, "scope_id": org, "epoch": 1, "key": b64u_encode(hub.org_key(org, account))}],
        "hub_key": hub.r_config(None, None)[1]["hub_key"],  # type: ignore[arg-type]
        "v": 1,
    }
    hub.invites[secrets_of.invite_id] = {"org": org, "package_ct": invite_format.seal_package(secrets_of, package)}
    other_tool = make_tool(tmp_path / "other")
    pair_user(other_tool, hub, hub.add_user(display_name="Bob"))
    link = f"{hub.origin}/invite#{invite_format.format_code(code)}"
    assert other_tool.run("org", "join", stdin=link)[1]["reason"] == "invite_not_in_log"


def test_anonymous_answer_that_is_no_json(tool: Tool) -> None:
    class Plain(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"html")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Plain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = tool.run("pair", "--hub", f"http://127.0.0.1:{server.server_address[1]}")[1]
    finally:
        server.shutdown()
        server.server_close()
    assert result["reason"] == "response_invalid"


def test_pair_reports_no_display_name_when_me_fails_later(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    calls: list[int] = []

    def me(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return None if len(calls) == 1 else (503, {"error": "unavailable"})

    hub.hooks[("GET", "/v1/me")] = me
    run, link = start_pair(tool, hub)
    assert post_form(int(link["c"]), browser_confirms(hub, user, link))[0] == 303
    result = run.join()
    assert (result["ok"], result["display_name"]) == (True, None)
