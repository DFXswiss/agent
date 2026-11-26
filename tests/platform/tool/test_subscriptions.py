"""Claude subscriptions logged in centrally against the hub double (ADR-090, rules E-30, E-31): `subscriptions list`
(status, use and the last reading, never a token), `add` and `login` (a `claude.login` with the subscription's ID to a
device that announced `subscriptions`), `rename`, `note`, `logout` (sealed again by the user) and `remove`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agent_platform import devicereq, subscriptions
from agent_runner.crypto.credential import new_header, open_credential, seal_credential
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.ids import uuid7

from .conftest import Tool, wait_for
from .fakes.relay import opened, relay
from .fakes.subscriptions import KIND, add, state_of
from .hubfake import HubFake, Json, User, now_ms_text
from .seeding import assign_device

TOKEN = "double-SUBSCRIPTIONCANARY-0123456789"
AUTHORIZE = {"step": "authorize", "url": "https://claude.ai/oauth/authorize?x=1"}
ORG = "01930001-0000-7000-8000-0000000000aa"


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch, hub: HubFake) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)
    state_of(hub).announce = True


def body(token: str | None = TOKEN, **extra: Any) -> Json:
    found: Json = {"email": "team@example.com", "kind": "setup_token", **extra}
    if token is not None:
        found["token"] = token
    return found


def sealed(
    hub: HubFake, user: User, content: object, *, record_id: str | None = None, author: str | None = None, **opt: Any
) -> Json:
    fields = new_header(
        id=record_id or uuid7(),
        owner=user.id,
        kind=KIND,
        version=opt.get("version", 1),
        epoch=opt.get("epoch", 1),
        author=author or user.id,
        ts=now_ms_text(),
    )
    signer = hub.platform_devices[author].sig if author in hub.platform_devices else user.identity.sig
    return seal_credential(opt.get("key") or user.cred_keys[1], fields, body_json(content), signer)


def reading(week: float | None, *, unreadable: bool = False) -> Json:
    windows = (
        []
        if week is None
        else [
            {"window": "seven_day", "used_percent": week, "resets_at": None, "severity": None},
            {"window": "five_hour", "used_percent": 1.0, "resets_at": None, "severity": None},
            "noise",
        ]
    )
    return {"fetched_at": now_ms_text(), "device_id": uuid7(), "unreadable": unreadable, "windows": windows}


def stored(hub: HubFake, user: User, record_id: str) -> Json:
    record = state_of(hub).records[record_id]["record"]
    author = record["header"]["author"]
    key = user.identity.sig_public() if author == user.id else hub.agents[author].sig
    return cast(Json, parse_json(open_credential(user.cred_keys[1], record, key)))


def test_add_and_login_send_the_subscription_to_a_capable_device(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    relay(hub).script["claude.login"] = [[(AUTHORIZE, False)]]
    seen = len(tool.opened())
    code, added = tool.run("subscriptions", "add", "--name", "Team 2", "--note", "reviews", "--wait", "5")
    assert code == 0, added
    assert (added["step"], added["url"]) == ("authorize", AUTHORIZE["url"])
    assert f"claude login --request {added['request_id']} --code" in added["next"]
    assert wait_for(lambda: tool.opened()[seen:]) == [AUTHORIZE["url"]]
    assert opened(hub, added["request_id"]) == {
        "mode": "manual",
        "subscription_id": added["subscription_id"],
        "name": "Team 2",
        "note": "reviews",
    }
    relay(hub).script["claude.login"] = [[({"step": "done", "email": "x@example.com"}, True)]]
    done = tool.run("subscriptions", "add", "--name", "Mail", "--automatic", "--email", "a+2@example.com")[1]
    assert done["step"] == "done"
    assert "next" not in done
    assert cast(Json, opened(hub, done["request_id"]))["email"] == "a+2@example.com"
    # log in again: the subscription's ID only
    record_id = add(hub, account.id, sealed(hub, account, body(token=None, name="Old")))
    again = tool.run("subscriptions", "login", "--id", record_id, "--device", device, "--wait", "0")[1]
    assert opened(hub, again["request_id"]) == {"mode": "manual", "subscription_id": record_id}
    for argv in (
        ("add", "--name", ""),
        ("add", "--name", "a\nb"),
        ("add", "--name", "x", "--note", "n" * 501),
        ("add", "--name", "x", "--email", "a@example.com"),
        ("add", "--name", "x", "--automatic", "--email", "bad"),
        ("add", "--name", "x", "--wait", "700"),
    ):
        assert tool.run("subscriptions", *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("subscriptions", "login", "--id", uuid7())[1]["error"] == "not_found"


def test_only_devices_that_announced_subscriptions_are_used(tool: Tool, hub: HubFake, account: User) -> None:
    assert tool.run("subscriptions", "add", "--name", "x")[1]["reason"] == "device_not_assigned"
    _org, _wg, device = assign_device(tool, hub, account)
    state_of(hub).outdated.add(device)
    assert tool.run("subscriptions", "add", "--name", "x")[1]["reason"] == "device_outdated"
    assert tool.run("subscriptions", "add", "--name", "x", "--device", device)[1]["reason"] == "device_outdated"
    state_of(hub).outdated.clear()
    relay(hub).script["claude.login"] = [[({"step": "failed", "code": "login_cli_failed"}, True)]]
    failed = tool.run("subscriptions", "add", "--name", "x", "--wait", "5")[1]
    assert (failed["error"], failed["failure"]) == ("device_request_failed", "login_cli_failed")


def test_list_shows_status_use_and_reading_never_a_token(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    primary = sealed(hub, account, body())
    hub.credentials[(account.id, KIND)] = {"record": primary, "version": 1}
    state_of(hub).readings[account.id] = reading(80.0)
    use = {"pool": True, "orgs": [ORG]}
    full = add(hub, account.id, sealed(hub, account, body(name="Team", note="n", use=use)), reading(10.0))
    out = add(hub, account.id, sealed(hub, account, body(token=None, name="Out")))
    refused = add(hub, account.id, sealed(hub, account, body(name="R"), author=device), reading(None, unreadable=True))
    bad_use = add(hub, account.id, sealed(hub, account, body(use={"pool": True, "orgs": [ORG, ORG]})))
    wrong_key = add(hub, account.id, sealed(hub, account, body(), key=b"w" * 32))
    stale = add(hub, account.id, sealed(hub, account, body(), epoch=9))
    stranger = add(hub, account.id, sealed(hub, account, body(), author=uuid7()))
    code, listed = tool.run("subscriptions", "list")
    assert code == 0, listed
    entries = {item["id"]: item for item in listed["subscriptions"]}
    assert [item["id"] for item in listed["subscriptions"]][:2] == [full, primary["header"]["id"]]
    assert entries[full] | {"measured_at": None, "device_id": None} == {
        "id": full,
        "primary": False,
        "week_percent": 10.0,
        "week_resets_at": None,
        "window_percent": 1.0,
        "window_resets_at": None,
        "measured_at": None,
        "device_id": None,
        "name": "Team",
        "note": "n",
        "email": "team@example.com",
        "status": "logged_in",
        "use": use,
        "login": "setup_token",
        "expires_at": None,
        "plan": None,
    }
    assert (entries[primary["header"]["id"]]["name"], entries[primary["header"]["id"]]["use"]) == (
        "own sessions",
        "own_sessions",
    )
    assert entries[out]["status"] == "not_logged_in"
    assert entries[out]["next"] == f"agent-platform subscriptions login --id {out}"
    assert entries[out]["use"] == {"pool": False, "orgs": []}
    assert entries[out]["measured_at"] is None
    assert (entries[refused]["status"], entries[refused]["since"]) == ("not_readable", entries[refused]["measured_at"])
    assert entries[refused]["next"] == f"agent-platform subscriptions login --id {refused}"  # a setup token: log in
    assert "next" not in entries[primary["header"]["id"]]
    assert {key: entries[key]["code"] for key in (bad_use, wrong_key, stale, stranger)} == {
        bad_use: "params_invalid",
        wrong_key: "decrypt_failed",
        stale: "epoch_stale",
        stranger: "author_not_writer",
    }
    assert TOKEN not in str(listed)
    # a record gone between the list and its read is left out
    meta = {key: value for key, value in entries[out].items() if key in ("id", "primary")}
    gone = {**meta, "owner_id": account.id, "version": 1, "epoch": 1, "reading": None}
    state_of(hub).raw_list = {"subscriptions": [{**gone, "created_at": now_ms_text(), "updated_at": now_ms_text()}]}
    del state_of(hub).records[out]
    assert tool.run("subscriptions", "list")[1]["subscriptions"] == []
    state_of(hub).raw_list = {"subscriptions": [{"id": out}]}
    assert tool.run("subscriptions", "list")[1]["reason"] == "response_invalid"


def test_rename_note_logout_and_remove_seal_the_record_again(tool: Tool, hub: HubFake, account: User) -> None:
    record_id = add(hub, account.id, sealed(hub, account, body(name="Old", note="n")))
    assert tool.run("subscriptions", "rename", "--id", record_id, "--name", "New")[1]["version"] == 2
    assert stored(hub, account, record_id)["name"] == "New"
    tool.run("subscriptions", "note", "--id", record_id, "--note", "for the review org")
    assert stored(hub, account, record_id)["note"] == "for the review org"
    tool.run("subscriptions", "note", "--id", record_id, "--clear")
    assert "note" not in stored(hub, account, record_id)
    state_of(hub).conflicts = 1
    logged_out = tool.run("subscriptions", "logout", "--id", record_id)[1]
    assert (logged_out["status"], logged_out["revoke_at"]) == ("not_logged_in", "https://claude.ai/settings")
    assert stored(hub, account, record_id) == {"email": "team@example.com", "kind": "setup_token", "name": "New"}
    state_of(hub).conflicts = 2
    assert tool.run("subscriptions", "rename", "--id", record_id, "--name", "X")[1]["hub_error"] == "version_conflict"
    for argv in (
        ("note", "--id", record_id),
        ("note", "--id", record_id, "--note", "a", "--clear"),
        ("rename", "--id", record_id, "--name", ""),
    ):
        assert tool.run("subscriptions", *argv)[1]["error"] == "invalid_argument", argv
    primary = sealed(hub, account, body())
    hub.credentials[(account.id, KIND)] = {"record": primary, "version": 1}
    for command in ("logout", "login"):
        assert tool.run("subscriptions", command, "--id", primary["header"]["id"])[1]["reason"] == "primary"
    assert tool.run("subscriptions", "logout", "--id", uuid7())[1]["error"] == "not_found"
    broken = add(hub, account.id, sealed(hub, account, body(), key=b"w" * 32))
    assert tool.run("subscriptions", "rename", "--id", broken, "--name", "x")[1]["reason"] == "decrypt_failed"
    state_of(hub).puts.clear()
    other = add(hub, account.id, sealed(hub, account, body()))
    state_of(hub).records[other]["owner_id"] = "nobody"
    assert tool.run("subscriptions", "rename", "--id", other, "--name", "x")[1]["error"] == "not_found"
    assert tool.run("subscriptions", "remove", "--id", record_id)[1]["status"] == "removed"
    assert record_id not in state_of(hub).records
    assert tool.run("subscriptions", "remove", "--id", record_id)[1]["status"] == "removed"
    state_of(hub).refuse_delete = True
    assert tool.run("subscriptions", "remove", "--id", broken)[1]["hub_error"] == "forbidden"


def test_use_seals_the_choice_and_sends_its_mirror(tool: Tool, hub: HubFake, account: User) -> None:
    record_id = add(hub, account.id, sealed(hub, account, body(name="Team")))
    other = "01930001-0000-7000-8000-0000000000bb"
    done = tool.run("subscriptions", "use", "--id", record_id, "--org", ORG, "--org", other, "--org", ORG)[1]
    assert done["use"] == {"pool": False, "orgs": sorted([ORG, other])}
    assert stored(hub, account, record_id)["use"] == {"pool": False, "orgs": sorted([ORG, other])}
    assert state_of(hub).puts[-1]["use"] == {"pool": False, "orgs": sorted([ORG, other])}
    tool.run("subscriptions", "use", "--id", record_id, "--pool")
    assert stored(hub, account, record_id)["use"] == {"pool": True, "orgs": []}
    tool.run("subscriptions", "rename", "--id", record_id, "--name", "Kept")
    assert state_of(hub).puts[-1]["use"] == {"pool": True, "orgs": []}  # every write sends the mirror it sealed
    tool.run("subscriptions", "use", "--id", record_id, "--measure")
    assert "use" not in stored(hub, account, record_id)
    assert state_of(hub).puts[-1]["use"] == {"pool": False, "orgs": []}
    for argv in (("--measure", "--pool"), ()):
        assert tool.run("subscriptions", "use", "--id", record_id, *argv)[1]["reason"] == "use", argv


def test_malformed_and_refused_records_end_with_their_code(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    assign_device(tool, hub, account)
    extra = add(hub, account.id, sealed(hub, account, body(unknown=True)))
    bad_use = add(hub, account.id, sealed(hub, account, body(use={"pool": True})))
    foreign = add(hub, account.id, sealed(hub, account, body(name="Team")))
    record = state_of(hub).records[foreign]["record"]
    state_of(hub).records[foreign]["record"] = {**record, "header": {**record["header"], "kind": "github"}}
    entries = {item["id"]: item for item in tool.run("subscriptions", "list")[1]["subscriptions"]}
    assert {key: entries[key]["code"] for key in (bad_use, foreign)} == {
        bad_use: "params_invalid",
        foreign: "header_invalid",
    }
    assert entries[extra]["status"] == "logged_in"  # a field of a later version is kept, not refused (KOR-71)
    assert subscriptions.valid_body({"email": None}) is False  # the kind is required
    # a refusal other than a concurrent write is not tried again
    kept = add(hub, account.id, sealed(hub, account, body(name="Kept")))
    state_of(hub).refuse_put = True
    assert tool.run("subscriptions", "rename", "--id", kept, "--name", "x")[1]["hub_error"] == "forbidden"
    assert len(state_of(hub).puts) == 1
    # a device the verified logs give no keys for is skipped
    monkeypatch.setattr(subscriptions, "device_keys", lambda _account, _device: None)
    assert tool.run("subscriptions", "add", "--name", "x")[1]["reason"] == "device_outdated"


FULL: dict[str, Any] = {
    "refresh_token": "refresh-token-of-the-test",
    "expires_at": "2026-11-26T06:00:00.000Z",
    "scopes": ["user:inference", "user:profile"],
    "subscription_type": "max",
    "rate_limit_tier": "default_claude_max_20x",
    "kind": "oauth_login",
}


def test_full_logins_show_expiry_plan_and_log_in_again(tool: Tool, hub: HubFake, account: User) -> None:
    """KOR-71, ADR-092: a full login lists its expiry and plan; a login the device could not renew is `relogin`
    with the command that logs it in; a field this version does not know is kept when the record is changed;
    `logout` drops both tokens."""
    _org, _wg, device = assign_device(tool, hub, account)
    full = add(hub, account.id, sealed(hub, account, body(name="Team", **FULL, later="kept")), reading(5.0))
    marked = {k: v for k, v in FULL.items() if k != "refresh_token"}
    refused = add(hub, account.id, sealed(hub, account, body(None, name="R", relogin="renewal_refused", **marked),
                                          author=device))  # fmt: skip
    odd = add(hub, account.id, sealed(hub, account, body(**{**FULL, "relogin": "other"})))
    odd_plan = add(hub, account.id, sealed(hub, account, body(**{**FULL, "subscription_type": "a b"})))
    odd_scopes = add(hub, account.id, sealed(hub, account, body(**{**FULL, "scopes": "user:profile"})))
    odd_token = add(hub, account.id, sealed(hub, account, body(**{**FULL, "refresh_token": "with space"})))
    entries = {item["id"]: item for item in tool.run("subscriptions", "list")[1]["subscriptions"]}
    assert {key: entries[full][key] for key in ("status", "login", "expires_at", "plan")} == {
        "status": "logged_in",
        "login": "oauth_login",
        "expires_at": "2026-11-26T06:00:00.000Z",
        "plan": {"type": "max", "tier": "default_claude_max_20x"},
    }
    assert (entries[refused]["status"], entries[refused]["next"]) == (
        "relogin",
        f"agent-platform subscriptions login --id {refused}",
    )
    for broken in (odd, odd_plan, odd_scopes, odd_token):
        assert entries[broken]["code"] == "params_invalid"
    assert "refresh-token-of-the-test" not in str(entries)
    assert tool.run("subscriptions", "rename", "--id", full, "--name", "New")[1]["version"] == 2
    assert stored(hub, account, full)["later"] == "kept"
    assert stored(hub, account, full)["refresh_token"] == "refresh-token-of-the-test"
    tool.run("subscriptions", "logout", "--id", full)
    after = stored(hub, account, full)
    assert "token" not in after
    assert "refresh_token" not in after
    assert after["name"] == "New"
    tool.run("subscriptions", "logout", "--id", refused)
    assert "relogin" not in stored(hub, account, refused)
