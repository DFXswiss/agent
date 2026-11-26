"""Subscriptions side by side against the hub double (KOR-73, ADR-099; rule E-33): `add` with an own mailbox, the use
for organizations and a registration (the record written before the login, `slot`, `extended`), `add-many`, the check
for humans (`check`: picture, a person's click, `relay`), `mailbox` (set, clear, check) and the parser of the new
responses."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_platform import devicereq, subscriptions
from agent_platform.errors import PlatformError
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.relay import opened, relay, respond
from .fakes.subscriptions import add, state_of
from .hubfake import HubFake, Json, User
from .seeding import assign_device
from .test_subscriptions import body, sealed, stored

ORG = "01930001-0000-7000-8000-0000000000aa"
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 picture").decode()
VERIFY = {"step": "verify", "frame": 1, "image": {"width": 960, "height": 600, "data": JPEG}}
AUTHORIZE = {"step": "authorize", "url": "https://claude.ai/oauth/authorize?x=1"}
PASSWORD = "mailbox-password-of-the-test"
MAILBOX = ("--mailbox-host", "imap.example.com", "--mailbox-user", "sub@example.com")
MAILBOX_EMAIL = ("--mailbox-email", "sub@example.com")
BOX = {
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_user": "sub@example.com",
    "imap_password": PASSWORD,
    "email": "sub@example.com",
}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch, hub: HubFake) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)
    state_of(hub).announce = True


def parallel_device(tool: Tool, hub: HubFake, account: User) -> str:
    _org, _wg, device = assign_device(tool, hub, account)
    state_of(hub).parallel.add(device)
    return device


def slot_of(hub: HubFake, request_id: str) -> object:
    return relay(hub).requests[request_id]["slot"]


def test_add_with_mailbox_use_and_registration_writes_the_record_first(tool: Tool, hub: HubFake, account: User) -> None:
    parallel_device(tool, hub, account)
    relay(hub).script["claude.login"] = [[(VERIFY, False)]]
    argv = ("subscriptions", "add", "--name", "Ada", "--note", "team", *MAILBOX, *MAILBOX_EMAIL)
    extra = ("--mailbox-password-stdin", "--org", ORG, "--org", ORG, "--register-name", "Ada Example")
    consents = ("--birth-date", "1990-04-01", "--consent", "terms", "--consent", "age", "--consent", "terms")
    code, added = tool.run(*argv, *extra, *consents, "--wait", "5", stdin=PASSWORD + "\n")
    assert code == 0, added
    assert added["step"] == "verify"
    assert added["frame"] == 1
    assert "image" not in added
    assert f"subscriptions check --request {added['request_id']} --out frame.jpg" in added["next"]
    record_id = added["subscription_id"]
    assert stored(hub, account, record_id) == {
        "email": None,
        "kind": "oauth_login",
        "name": "Ada",
        "note": "team",
        "mailbox": BOX,
        "use": {"pool": False, "orgs": [ORG]},
    }
    assert state_of(hub).puts[-1]["use"] == {"pool": False, "orgs": [ORG]}
    assert state_of(hub).puts[-1]["expected_version"] == 0
    assert opened(hub, added["request_id"]) == {
        "mode": "automatic",
        "subscription_id": record_id,
        "extended": True,
        "account": {"name": "Ada Example", "birth_date": "1990-04-01", "consents": ["terms", "age"]},
    }
    assert slot_of(hub, added["request_id"]) == record_id
    assert PASSWORD not in json.dumps(added)
    listed = {item["id"]: item for item in tool.run("subscriptions", "list")[1]["subscriptions"]}
    assert listed[record_id]["mailbox"] == "sub@example.com"
    # an e-mail of an automatic login and the waiting state name their next step
    relay(hub).script["claude.login"] = [[({"step": "progress", "stage": "verified"}, False)]]
    waiting = tool.run(
        "subscriptions", "add", "--name", "B", "--automatic", "--email", "b@example.com", "--org", ORG, "--wait", "0"
    )
    assert waiting[1]["next"] == f"agent-platform subscriptions check --request {waiting[1]['request_id']}"
    assert stored(hub, account, waiting[1]["subscription_id"])["email"] == "b@example.com"
    assert cast(Json, opened(hub, waiting[1]["request_id"]))["email"] == "b@example.com"


@pytest.mark.parametrize(
    "argv",
    [
        (*MAILBOX, "--mailbox-password-stdin"),
        (*MAILBOX, *MAILBOX_EMAIL),
        ("--mailbox-password-stdin",),
        ("--consent", "terms"),
        ("--register-name", "Ada", "--birth-date", "1990-02-30"),
        ("--register-name", "Ada", "--birth-date", "01.04.1990"),
        ("--register-name", "x" * 101),
        ("--org", "not-an-org"),
        ("--mailbox-host", "bad host", "--mailbox-user", "u", *MAILBOX_EMAIL, "--mailbox-password-stdin"),
    ],
)
def test_add_refuses_malformed_options(tool: Tool, hub: HubFake, account: User, argv: tuple[str, ...]) -> None:
    parallel_device(tool, hub, account)
    found = tool.run("subscriptions", "add", "--name", "x", *argv, stdin=PASSWORD)[1]
    assert found["error"] == "invalid_argument", found
    assert relay(hub).requests == {}


def test_new_fields_need_a_device_that_runs_logins_side_by_side(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    found = tool.run("subscriptions", "add", "--name", "x", "--org", ORG)[1]
    assert (found["error"], found["reason"]) == ("no_device", "device_outdated")
    # the new login of a record with its own mailbox too; without one the device from before is fine
    boxed = add(hub, account.id, sealed(hub, account, body(token=None, name="Boxed", mailbox=BOX)))
    assert tool.run("subscriptions", "login", "--id", boxed)[1]["reason"] == "device_outdated"
    relay(hub).script["claude.login"] = [[(AUTHORIZE, False)]]
    plain = add(hub, account.id, sealed(hub, account, body(token=None, name="Plain")))
    again = tool.run("subscriptions", "login", "--id", plain, "--wait", "5")[1]
    assert opened(hub, again["request_id"]) == {"mode": "manual", "subscription_id": plain}
    assert slot_of(hub, again["request_id"]) is None
    state_of(hub).parallel.add(device)
    relay(hub).script["claude.login"] = [[(AUTHORIZE, False)]]
    boxed_again = tool.run("subscriptions", "login", "--id", boxed, "--automatic", "--wait", "5")[1]
    assert opened(hub, boxed_again["request_id"]) == {
        "mode": "automatic",
        "subscription_id": boxed,
        "extended": True,
    }
    assert slot_of(hub, boxed_again["request_id"]) == boxed
    # a record that does not open, or went between the list and its read
    broken = add(hub, account.id, sealed(hub, account, body(), key=b"w" * 32))
    assert tool.run("subscriptions", "login", "--id", broken)[1]["reason"] == "decrypt_failed"
    with pytest.raises(PlatformError) as gone:
        subscriptions._own_mailbox(cast(Any, SimpleNamespace(call=_not_found)), uuid7())
    assert gone.value.code == "not_found"
    with pytest.raises(PlatformError) as refused:
        subscriptions._own_mailbox(cast(Any, SimpleNamespace(call=_forbidden)), uuid7())
    assert refused.value.fields["hub_error"] == "forbidden"


def _forbidden(_method: str, _path: str, _body: object = None) -> Json:
    raise PlatformError("hub_refused", hub_error="forbidden")


def _not_found(_method: str, _path: str, _body: object = None) -> Json:
    raise PlatformError("hub_refused", hub_error="not_found")


def test_add_many_starts_every_login_then_follows_them(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    parallel_device(tool, hub, account)
    entries = [
        {"name": "Done", "automatic": True},
        {"name": "Check", "mailbox": {k: v for k, v in BOX.items() if k != "imap_port"}, "orgs": [ORG]},
        {"name": "Link", "account": {"name": "Ann"}, "email": "ann@example.com"},
        {"name": "Slow", "note": "n"},
    ]
    relay(hub).script["claude.login"] = [
        [({"step": "done", "email": "x@example.com"}, True)],
        [(VERIFY, False)],
        [(AUTHORIZE, False)],
        [({"step": "progress", "stage": "browser_started"}, False)],
    ]
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    code, found = tool.run("subscriptions", "add-many", "--file", str(path), "--wait", "1")
    assert code == 0, found
    rows = found["subscriptions"]
    assert [(row["name"], row["step"]) for row in rows] == [
        ("Done", "done"),
        ("Check", "verify"),
        ("Link", "authorize"),
        ("Slow", "progress"),
    ]
    assert "next" not in rows[0]
    assert "check --request" in rows[1]["next"]
    assert "claude login --request" in rows[2]["next"]
    assert rows[3]["next"] == f"agent-platform subscriptions check --request {rows[3]['request_id']}"
    assert {slot_of(hub, row["request_id"]) for row in rows} == {row["subscription_id"] for row in rows}
    assert stored(hub, account, rows[1]["subscription_id"])["mailbox"] == BOX
    assert cast(Json, opened(hub, rows[2]["request_id"]))["account"] == {"name": "Ann"}
    assert opened(hub, rows[3]["request_id"]) == {
        "mode": "manual",
        "subscription_id": rows[3]["subscription_id"],
        "extended": True,
        "name": "Slow",
        "note": "n",
    }
    # a login that cannot start is reported with its error, the others still run (stdin as the file)
    state_of(hub).refuse_put = True
    relay(hub).script["claude.login"] = [[({"step": "failed", "code": "timeout"}, True)]]
    mixed = [{"name": "Refused", "orgs": [ORG]}, {"name": "Failed"}]
    rows = tool.run("subscriptions", "add-many", "--file", "-", "--wait", "5", stdin=json.dumps(mixed))[1]
    assert [(row["name"], row.get("error"), row.get("step")) for row in rows["subscriptions"]] == [
        ("Refused", "hub_refused", None),
        ("Failed", None, "failed"),
    ]


@pytest.mark.parametrize(
    ("content", "entry"),
    [
        ("[]", None),
        ("{}", None),
        ("not json", None),
        ("[" + ",".join(['{"name": "x"}'] * 41) + "]", None),
        ('[{"name": "x"}, {"name": "x", "other": 1}]', 1),
        ('[{"name": ""}]', 0),
        ('[{"name": "x", "automatic": "yes"}]', 0),
        ('[{"name": "x", "mailbox": "box"}]', 0),
        ('[{"name": "x", "mailbox": {"imap_host": "h"}}]', 0),
        ('[{"name": "x", "account": "Ann"}]', 0),
        ('[{"name": "x", "account": {"name": "Ann", "other": 1}}]', 0),
        ('[{"name": "x", "account": {"name": "Ann", "consents": ["payment"]}}]', 0),
        ('[{"name": "x", "account": {"name": "Ann", "consents": ["age", "age"]}}]', 0),
        ('[{"name": "x", "orgs": "org"}]', 0),
        (json.dumps([{"name": "x", "orgs": [str(uuid7()) for _ in range(51)]}]), 0),
        ('[{"name": "x", "email": "a@example.com"}]', 0),
        ('[{"name": "x", "automatic": true, "email": "bad"}]', 0),
    ],
)
def test_add_many_checks_every_entry_before_sending(
    tool: Tool, hub: HubFake, account: User, content: str, entry: int | None
) -> None:
    parallel_device(tool, hub, account)
    found = tool.run("subscriptions", "add-many", "--file", "-", stdin=content)[1]
    assert found["error"] == "invalid_argument", found
    assert found.get("entry") == entry
    assert relay(hub).requests == {}


def test_add_many_refuses_an_unreadable_or_too_large_file(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parallel_device(tool, hub, account)
    assert tool.run("subscriptions", "add-many", "--file", str(tmp_path / "missing"))[1]["reason"] == "file"
    monkeypatch.setattr(subscriptions, "MAX_FILE", 8)
    big = tmp_path / "big.json"
    big.write_text('[{"name": "x"}]', encoding="utf-8")
    assert tool.run("subscriptions", "add-many", "--file", str(big))[1]["reason"] == "file"
    assert tool.run("subscriptions", "add-many", "--file", str(big), "--wait", "700")[1]["error"] == "invalid_argument"


def test_check_writes_the_picture_and_sends_a_persons_click_or_the_relay(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parallel_device(tool, hub, account)
    relay(hub).script["claude.login"] = [[(VERIFY, False)]]
    root = tool.run("subscriptions", "add", "--name", "C", "--automatic", "--wait", "5")[1]["request_id"]
    out = tmp_path / "frame.jpg"
    code, seen = tool.run("subscriptions", "check", "--request", root, "--out", str(out))
    assert code == 0, seen
    assert seen["picture"] == {"path": str(out), "frame": 1, "width": 960, "height": 600}
    assert out.read_bytes() == base64.b64decode(JPEG)
    assert "check --request" in seen["next"]
    # the click: the device answers with a newer picture
    original = devicereq.submit

    def answering(account_: Any, target: Any, kind: str, body_: Json, *rest: Any, **options: Any) -> Json:
        prepared = original(account_, target, kind, body_, *rest, **options)
        respond(hub, root, {**VERIFY, "frame": 2})
        return prepared

    monkeypatch.setattr(subscriptions, "submit", answering)
    clicked = tool.run("subscriptions", "check", "--request", root, "--click", "0.25,0.5", "--out", str(out))[1]
    assert clicked["picture"]["frame"] == 2
    follow = [item for item in relay(hub).requests.values() if item["follows"] == root]
    assert [opened(hub, item["request_id"]) for item in follow] == [{"click": {"x": 0.25, "y": 0.5}}]
    monkeypatch.setattr(subscriptions, "submit", original)
    # no newer picture: the click waits up to --wait
    unchanged = tool.run("subscriptions", "check", "--request", root, "--click", "0.5,0.5", "--wait", "1")[1]
    assert (unchanged["step"], unchanged["frame"]) == ("verify", 2)
    # relay: the device ends with the link; the wait ends at the new step
    relay(hub).script["claude.login"] = [[]]
    respond(hub, root, {**AUTHORIZE, "automation": "failed", "reason": "human_check"})
    given_up = tool.run("subscriptions", "check", "--request", root, "--relay", "--wait", "5")[1]
    assert given_up["step"] == "authorize"
    assert given_up["reason"] == "human_check"
    assert "claude login --request" in given_up["next"]
    assert "picture" not in given_up
    # a click with --wait 0 returns after one read; a finished login has no next step
    respond(hub, root, {"step": "done"}, final=True)
    done = tool.run("subscriptions", "check", "--request", root, "--wait", "0", "--out", str(tmp_path / "x.jpg"))[1]
    assert done["step"] == "done"
    assert "next" not in done


def test_check_refuses_bad_clicks_and_a_click_of_an_agent(tool: Tool, hub: HubFake, account: User) -> None:
    root = uuid7()
    for argv in (("--click", "0.1,0.2", "--relay"), ("--click", "a,b"), ("--click", "0.5"), ("--click", "1.5,0.5")):
        found = tool.run("subscriptions", "check", "--request", root, *argv)[1]
        assert found["error"] == "invalid_argument", argv
    agent = SimpleNamespace(actor=uuid7(), user_id=uuid7())
    args = SimpleNamespace(wait=0, click="0.5,0.5", relay=False, request=root, out=None)
    with pytest.raises(PlatformError) as refused:
        subscriptions.check(cast(Any, agent), args, cast(Any, None))
    assert (refused.value.code, refused.value.fields["reason"]) == ("invalid_argument", "human_only")


def test_mailbox_sets_clears_and_checks_the_own_mailbox(tool: Tool, hub: HubFake, account: User) -> None:
    device = parallel_device(tool, hub, account)
    record_id = add(hub, account.id, sealed(hub, account, body(name="Team", use={"pool": False, "orgs": [ORG]})))
    argv = ("subscriptions", "mailbox", "--id", record_id, *MAILBOX, *MAILBOX_EMAIL, "--mailbox-password-stdin")
    code, done = tool.run(*argv, stdin=PASSWORD)
    assert code == 0, done
    assert (done["version"], done["mailbox"]) == (2, "sub@example.com")
    assert stored(hub, account, record_id)["mailbox"] == BOX
    assert stored(hub, account, record_id)["use"] == {"pool": False, "orgs": [ORG]}
    relay(hub).script["mailbox.check"] = [[({"step": "done", "valid": True}, True)]]
    checked = tool.run("subscriptions", "mailbox", "--id", record_id, "--check", "--wait", "5")[1]
    assert (checked["step"], checked["valid"], checked["device_id"]) == ("done", True, device)
    assert opened(hub, checked["request_id"]) == {"subscription_id": record_id}
    cleared = tool.run("subscriptions", "mailbox", "--id", record_id, "--clear")[1]
    assert cleared["mailbox"] is None
    assert "mailbox" not in stored(hub, account, record_id)
    for options, stdin in (
        (("--check", "--clear"), ""),
        (("--check", *MAILBOX, *MAILBOX_EMAIL, "--mailbox-password-stdin"), PASSWORD),
        ((), ""),
        (("--clear", *MAILBOX, *MAILBOX_EMAIL, "--mailbox-password-stdin"), PASSWORD),
        (("--wait", "700"), ""),
    ):
        found = tool.run("subscriptions", "mailbox", "--id", record_id, *options, stdin=stdin)[1]
        assert found["error"] == "invalid_argument", options


def test_the_new_responses_are_parsed() -> None:
    parse = devicereq.response_body
    assert parse("claude.login", VERIFY) == VERIFY
    assert parse("claude.login", {**AUTHORIZE, "automation": "failed", "reason": "consent_missing"}) is not None
    for stage in ("mail_code_received", "registered", "verified"):
        assert parse("claude.login", {"step": "progress", "stage": stage}) is not None
    image = VERIFY["image"]
    assert isinstance(image, dict)
    for broken in (
        {**AUTHORIZE, "automation": "failed", "reason": "solved"},
        {**VERIFY, "frame": 0},
        {**VERIFY, "frame": True},
        {**VERIFY, "frame": 101},
        {"step": "verify", "frame": 1},
        {**VERIFY, "image": "jpeg"},
        {**VERIFY, "image": {**image, "width": 0}},
        {**VERIFY, "image": {**image, "height": 5000}},
        {**VERIFY, "image": {**image, "data": "not base64!"}},
        {**VERIFY, "image": {**image, "data": "AA"}},
        {**VERIFY, "image": {**image, "data": 7}},
        {**VERIFY, "image": {**image, "other": 1}},
    ):
        assert parse("claude.login", broken) is None, broken
