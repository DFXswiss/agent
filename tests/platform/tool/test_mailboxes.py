"""Mailboxes read by the platform against the hub double (KOR-74, ADR-099, rules E-31, E-04): `mailbox list` (name,
address, purpose, release, never the password), `add` (password on stdin, unique name), `change` and `release`
(sealed again by the user with the mirror of the release), `check` (`mailbox.check` with the opened body) and
`delete`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agent_platform import credentials, devicereq
from agent_runner.crypto.credential import new_header, open_credential, seal_credential
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes import mailboxes as fake
from .fakes.mailboxes import KIND, add, state_of
from .fakes.relay import opened, relay
from .hubfake import HubFake, Json, User, now_ms_text
from .seeding import assign_device

PASSWORD = "MAILBOXCANARY-secret-0123"
WG = "01930001-0000-7000-8000-0000000000aa"
WG2 = "01930001-0000-7000-8000-0000000000bb"
DONE = {"step": "done", "valid": True}


@pytest.fixture(autouse=True)
def mailbox_routes(monkeypatch: pytest.MonkeyPatch, hub: HubFake) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)
    fake.install(hub)


def body(**extra: Any) -> Json:
    return {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_user": "bot@example.com",
        "imap_password": PASSWORD,
        "email": "bot@example.com",
        **extra,
    }


def sealed(
    hub: HubFake, user: User, content: object, *, record_id: str | None = None, author: str | None = None, **opt: Any
) -> Json:
    fields = new_header(
        id=record_id or uuid7(),
        owner=user.id,
        kind=opt.get("kind", KIND),
        version=opt.get("version", 1),
        epoch=opt.get("epoch", 1),
        author=author or user.id,
        ts=now_ms_text(),
    )
    signer = hub.platform_devices[author].sig if author in hub.platform_devices else user.identity.sig
    return seal_credential(opt.get("key") or user.cred_keys[1], fields, body_json(content), signer)


def stored(hub: HubFake, user: User, record_id: str) -> Json:
    record = state_of(hub).records[record_id]["record"]
    author = record["header"]["author"]
    key = user.identity.sig_public() if author == user.id else hub.agents[author].sig
    return cast(Json, parse_json(open_credential(user.cred_keys[1], record, key)))


def run_add(tool: Tool, *argv: str, name: str = "signup", stdin: str = PASSWORD + "\n") -> tuple[int, Json]:
    base = ("--email", "bot@example.com", "--imap-host", "imap.example.com", "--imap-user", "bot@example.com")
    return tool.run("mailbox", "add", "--name", name, *base, *argv, stdin=stdin)


def test_add_seals_a_new_mailbox_and_list_shows_it_without_the_password(
    tool: Tool, hub: HubFake, account: User
) -> None:
    code, added = run_add(tool, "--purpose", "sign-up mails", "--wg", WG2, "--wg", WG, "--wg", WG)
    assert code == 0, added
    assert (added["version"], added["name"], added["wgs"]) == (1, "signup", [WG, WG2])
    record_id = added["mailbox_id"]
    assert stored(hub, account, record_id) == body(name="signup", purpose="sign-up mails", use={"wgs": [WG, WG2]})
    assert state_of(hub).puts[-1]["use"] == {"wgs": [WG, WG2]}
    assert state_of(hub).puts[-1]["expected_version"] == 0
    second = run_add(tool, "--imap-port", "143", name="alerts")[1]
    assert stored(hub, account, second["mailbox_id"]) == body(name="alerts", imap_port=143)
    assert state_of(hub).puts[-1]["use"] == {"wgs": []}
    code, listed = tool.run("mailbox", "list")
    assert code == 0, listed
    assert listed["mailboxes"] == [
        {
            "id": record_id,
            "primary": True,
            "name": "signup",
            "email": "bot@example.com",
            "purpose": "sign-up mails",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_user": "bot@example.com",
            "status": "verified",
            "wgs": [WG, WG2],
        },
        {
            "id": second["mailbox_id"],
            "primary": False,
            "name": "alerts",
            "email": "bot@example.com",
            "purpose": None,
            "imap_host": "imap.example.com",
            "imap_port": 143,
            "imap_user": "bot@example.com",
            "status": "verified",
            "wgs": [],
        },
    ]
    assert PASSWORD not in str(listed)
    assert PASSWORD not in str(added)
    # the name is unique among the user's mailboxes; the fields are checked before anything is stored
    assert run_add(tool, name="signup")[1]["reason"] == "name_taken"
    for argv, name, stdin, reason in (
        ((), "Bad Name", PASSWORD, "name"),
        (("--purpose", ""), "x", PASSWORD, "purpose"),
        (("--purpose", "p" * 501), "x", PASSWORD, "purpose"),
        ((), "x", "\n", "mailbox"),
        (("--imap-port", "0"), "x", PASSWORD, "mailbox"),
    ):
        assert run_add(tool, *argv, name=name, stdin=stdin)[1]["reason"] == reason, argv
    assert run_add(tool, "--wg", "not-a-uuid", name="x")[1]["error"] == "invalid_argument"
    many = [arg for _ in range(51) for arg in ("--wg", uuid7())]
    assert run_add(tool, *many, name="x")[1]["reason"] == "wg"
    assert len(state_of(hub).records) == 2


def test_add_retries_a_concurrent_write_once(tool: Tool, hub: HubFake, account: User) -> None:
    state_of(hub).conflicts = 1
    assert run_add(tool)[1]["version"] == 1
    state_of(hub).conflicts = 2
    assert run_add(tool, name="other")[1]["hub_error"] == "version_conflict"
    hub.users[account.id].cred_epoch = 2  # the hub knows a newer CK than the tool can seal under
    assert run_add(tool, name="third")[1]["hub_error"] == "version_conflict"
    hub.users[account.id].cred_epoch = 1
    state_of(hub).refuse_put = True
    assert run_add(tool, name="fourth")[1]["hub_error"] == "forbidden"
    assert len(state_of(hub).records) == 1


def test_list_marks_records_that_do_not_open(tool: Tool, hub: HubFake, account: User) -> None:
    assert tool.run("mailbox", "list")[1]["mailboxes"] == []
    _org, _wg, device = assign_device(tool, hub, account)
    good = add(hub, account.id, sealed(hub, account, body(name="b")), use=[WG])
    by_device = add(hub, account.id, sealed(hub, account, body(name="a", use={"wgs": [WG]}), author=device), use=None)
    codes = {
        add(hub, account.id, sealed(hub, account, body(extra=1))): "params_invalid",
        add(hub, account.id, sealed(hub, account, body(), key=b"w" * 32)): "decrypt_failed",
        add(hub, account.id, sealed(hub, account, body(), epoch=9)): "epoch_stale",
        add(hub, account.id, sealed(hub, account, body(), author=uuid7())): "author_not_writer",
        add(hub, account.id, sealed(hub, account, body(), kind="github")): "header_invalid",
        add(hub, account.id, sealed(hub, account, ["not", "an", "object"])): "params_invalid",
    }
    entries = {item["id"]: item for item in tool.run("mailbox", "list")[1]["mailboxes"]}
    assert {key: entries[key]["code"] for key in codes} == codes
    assert entries[good]["primary"] is True
    assert "mirror" not in entries[good]
    # the sealed release decides; a mirror of the hub that differs is named
    assert (entries[by_device]["wgs"], entries[by_device]["mirror"]) == ([WG], [])
    listed = tool.run("mailbox", "list")[1]["mailboxes"]
    assert [item["id"] for item in listed][:2] == [good, by_device]
    # a record gone between the list and its read is left out; a malformed list is refused
    gone = {"id": uuid7(), "primary": False, "use": None}
    state_of(hub).raw_list = {"mailboxes": [gone]}
    assert tool.run("mailbox", "list")[1]["mailboxes"] == []
    state_of(hub).raw_list = {"mailboxes": [{"id": good}]}
    assert tool.run("mailbox", "list")[1]["reason"] == "response_invalid"
    state_of(hub).raw_list = None
    state_of(hub).refuse_get = True
    assert tool.run("mailbox", "list")[1]["hub_error"] == "forbidden"


def test_change_keeps_what_is_not_given(tool: Tool, hub: HubFake, account: User) -> None:
    record_id = add(hub, account.id, sealed(hub, account, body(name="signup", purpose="p", use={"wgs": [WG]})), [WG])
    other = add(hub, account.id, sealed(hub, account, body(name="other")))
    broken = add(hub, account.id, sealed(hub, account, body(), key=b"w" * 32))
    done = tool.run("mailbox", "change", "--id", record_id, "--purpose", "new", "--imap-port", "143")[1]
    assert (done["version"], done["changed"]) == (2, ["imap_port", "purpose"])
    assert stored(hub, account, record_id) == body(name="signup", purpose="new", imap_port=143, use={"wgs": [WG]})
    assert state_of(hub).puts[-1]["use"] == {"wgs": [WG]}  # every write sends the mirror it sealed
    change: tuple[str, ...] = (
        "--name",
        "renamed",
        "--email",
        "new@example.com",
        "--imap-host",
        "imap.new.test",
        "--imap-user",
        "u",
    )
    state_of(hub).conflicts = 1
    done = tool.run("mailbox", "change", "--id", record_id, *change, "--password-stdin", stdin="new-password\n")[1]
    assert done["changed"] == ["email", "imap_host", "imap_user", "name", "password"]
    assert "new-password" not in str(done)
    assert stored(hub, account, record_id) == {
        **body(name="renamed", purpose="new", imap_port=143, use={"wgs": [WG]}),
        "email": "new@example.com",
        "imap_host": "imap.new.test",
        "imap_user": "u",
        "imap_password": "new-password",
    }
    # its own name again is no clash
    assert tool.run("mailbox", "change", "--id", record_id, "--name", "renamed")[1]["version"] == 4
    for argv, reason in (
        (("--name", "other"), "name_taken"),
        (("--name", "Up"), "name"),
        (("--purpose", ""), "purpose"),
        ((), "nothing_to_change"),
        (("--imap-port", "0"), "mailbox"),
    ):
        assert tool.run("mailbox", "change", "--id", record_id, *argv)[1]["reason"] == reason, argv
    state_of(hub).conflicts = 2
    assert tool.run("mailbox", "change", "--id", other, "--purpose", "x")[1]["hub_error"] == "version_conflict"
    assert tool.run("mailbox", "change", "--id", uuid7(), "--purpose", "x")[1]["error"] == "not_found"
    assert tool.run("mailbox", "change", "--id", broken, "--purpose", "x")[1]["reason"] == "decrypt_failed"


def test_release_seals_the_workgroups_and_sends_the_mirror(tool: Tool, hub: HubFake, account: User) -> None:
    record_id = add(hub, account.id, sealed(hub, account, body(name="signup")))
    done = tool.run("mailbox", "release", "--id", record_id, "--wg", WG2, "--wg", WG)[1]
    assert (done["version"], done["wgs"]) == (2, [WG, WG2])
    assert stored(hub, account, record_id)["use"] == {"wgs": [WG, WG2]}
    assert state_of(hub).records[record_id]["use"] == {"wgs": [WG, WG2]}
    done = tool.run("mailbox", "release", "--id", record_id, "--none")[1]
    assert done["wgs"] == []
    assert "use" not in stored(hub, account, record_id)
    assert state_of(hub).puts[-1]["use"] == {"wgs": []}
    for argv in (("--none", "--wg", WG), ()):
        assert tool.run("mailbox", "release", "--id", record_id, *argv)[1]["reason"] == "wg", argv


def test_check_sends_the_opened_mailbox_to_a_device(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    record_id = add(hub, account.id, sealed(hub, account, body(name="signup")))
    relay(hub).script["mailbox.check"] = [[(DONE, True)]]
    code, done = tool.run("mailbox", "check", "--id", record_id, "--wait", "5")
    assert code == 0, done
    assert (done["mailbox_id"], done["device_id"], done["valid"]) == (record_id, device, True)
    assert opened(hub, done["request_id"]) == {"mailbox": body(name="signup")}
    assert PASSWORD not in str(done)
    relay(hub).script["mailbox.check"] = [[({"step": "failed", "code": "upstream_error"}, True)]]
    failed = tool.run("mailbox", "check", "--id", record_id, "--device", device, "--wait", "5")[1]
    assert (failed["error"], failed["failure"]) == ("device_request_failed", "upstream_error")
    assert tool.run("mailbox", "check", "--id", uuid7())[1]["error"] == "not_found"
    broken = add(hub, account.id, sealed(hub, account, body(), epoch=9))
    assert tool.run("mailbox", "check", "--id", broken)[1]["reason"] == "epoch_stale"


def test_delete_removes_the_record(tool: Tool, hub: HubFake, account: User) -> None:
    record_id = add(hub, account.id, sealed(hub, account, body()))
    deleted = tool.run("mailbox", "delete", "--id", record_id)[1]
    assert (deleted["mailbox_id"], deleted["status"]) == (record_id, "deleted")
    assert record_id not in state_of(hub).records
    assert tool.run("mailbox", "delete", "--id", record_id)[1]["status"] == "deleted"
    state_of(hub).refuse_delete = True
    assert tool.run("mailbox", "delete", "--id", record_id)[1]["hub_error"] == "forbidden"


@pytest.mark.parametrize(
    ("extra", "valid"),
    [
        ({}, True),
        ({"name": "a.b_c-1", "purpose": "x" * 500, "use": {"wgs": [WG, WG2]}}, True),
        ({"use": {"wgs": []}}, True),
        ({"name": "A"}, False),
        ({"name": "a" * 65}, False),
        ({"purpose": ""}, False),
        ({"purpose": "x" * 501}, False),
        ({"purpose": 5}, False),
        ({"use": []}, False),
        ({"use": {"wgs": [WG], "x": 1}}, False),
        ({"use": {"wgs": WG}}, False),
        ({"use": {"wgs": ["no"]}}, False),
        ({"use": {"wgs": [WG, WG]}}, False),
        ({"use": {"wgs": [uuid7() for _ in range(51)]}}, False),
        ({"other": 1}, False),
    ],
)
def test_mailbox_bodies_follow_the_contract(extra: Json, valid: bool) -> None:
    assert credentials.valid_body("mailbox", body(**extra)) is valid
