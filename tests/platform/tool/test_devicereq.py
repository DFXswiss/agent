"""Device requests and credential records against the hub double: `github status|connect|disconnect`, `claude login`,
`credentials list|set-mailbox|check|delete`. The device of the fake answers with sealed, signed responses; the tool
checks them like the web client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import devicereq
from agent_runner.crypto.credential import new_header, seal_credential
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.entries import entry_hash

from .conftest import Tool, make_tool, pair_user, wait_for
from .fakes.relay import opened, relay, respond
from .hubfake import HubFake, User, now_ms_text
from .seeding import assign_device, default_wg, private_org

DEVICE_CODE = {
    "step": "device_code",
    "user_code": "ABCD-1234",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
}
GITHUB_BODY = {"token": "gho_x", "login": "octo", "user_id": 7, "scopes": ["repo"], "created_at": now_ms_text()}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)


def device_record(hub: HubFake, user: User, author: str, kind: str, body: object, *, epoch: int = 1) -> None:
    """A credential record written by a device (or forged with another epoch)."""
    stored = hub.credentials.get((user.id, kind))
    version = 1 if stored is None else stored["version"] + 1
    fields = new_header(
        id=uuid7(), owner=user.id, kind=kind, version=version, epoch=epoch, author=author, ts=now_ms_text()
    )
    signer = hub.platform_devices[author].sig if author in hub.platform_devices else user.identity.sig
    key = user.cred_keys.get(epoch, b"k" * 32)
    hub.credentials[(user.id, kind)] = {
        "record": seal_credential(key, fields, body_json(body), signer),
        "version": version,
    }


def test_github_connect_flow(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    assert tool.run("github", "status")[1]["status"] == "disconnected"
    relay(hub).script["github.connect"] = [[(DEVICE_CODE, False)]]
    seen = len(tool.opened())
    code, started = tool.run("github", "connect", "--wait", "5")
    assert code == 0, started
    assert (started["step"], started["user_code"], started["device_id"]) == ("device_code", "ABCD-1234", device)
    assert wait_for(lambda: tool.opened()[seen:]) == [DEVICE_CODE["verification_uri"]]
    request = started["request_id"]
    assert opened(hub, request) == {}
    waiting = tool.run("github", "connect", "--request", request, "--wait", "0")[1]
    assert waiting["step"] == "device_code"
    respond(hub, request, {"step": "done", "login": "octo"}, final=True)
    device_record(hub, account, device, "github", GITHUB_BODY)
    done = tool.run("github", "connect", "--request", request, "--wait", "0")[1]
    assert (done["step"], done["login"]) == ("done", "octo")
    status = tool.run("github", "status")[1]
    assert (status["status"], status["login"], status["author"]) == ("connected", "octo", device)
    assert "token" not in json.dumps(status)
    # a token of a device flow (here the OAuth App's, KOR-65) is revoked by the devices on disconnect
    assert (status["source"], status["scopes"], status["revocable"]) == ("device_flow", ["repo"], True)
    gone = tool.run("github", "disconnect")[1]
    assert (gone["status"], gone["revoked_at_github"]) == ("disconnected", True)
    assert tool.run("github", "disconnect")[1]["status"] == "disconnected"
    assert tool.run("github", "status")[1]["status"] == "disconnected"


def test_github_connect_with_an_existing_token(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    done = {"step": "done", "login": "octo", "scopes": ["repo", "read:org"], "source": "token"}
    relay(hub).script["github.connect"] = [[(done, True)]]
    seen = len(tool.opened())
    code, connected = tool.run("github", "connect", "--token", "-", "--wait", "5", stdin="  ghp_secret123\n")
    assert code == 0, connected
    assert (connected["step"], connected["login"], connected["scopes"], connected["source"]) == (
        "done",
        "octo",
        ["repo", "read:org"],
        "token",
    )
    # the token goes only into the request sealed to the device, trimmed like the web's field; no page is opened
    assert opened(hub, connected["request_id"]) == {"token": "ghp_secret123"}
    assert "ghp_secret123" not in json.dumps(connected)
    assert all(b"ghp_secret123" not in body for body in hub.bodies)
    assert tool.opened()[seen:] == []
    device_record(hub, account, device, "github", {**GITHUB_BODY, "token": "ghp_secret123", "source": "token"})
    status = tool.run("github", "status")[1]
    assert (status["source"], status["scopes"], status["revocable"]) == ("token", ["repo"], False)
    assert "ghp_secret123" not in json.dumps(status)
    # a token the platform did not mint stays valid at GitHub: the result names the page to revoke it
    assert tool.run("github", "disconnect")[1] == {
        "ok": True,
        "status": "disconnected",
        "revoked_at_github": False,
        "revoke_at": "https://github.com/settings/applications",
    }
    # a token of the device flow (GitHub App user token) is revoked by the devices
    device_record(hub, account, device, "github", {**GITHUB_BODY, "token": "ghu_x", "source": "device_flow"})
    assert tool.run("github", "status")[1]["revocable"] is True
    assert tool.run("github", "disconnect")[1] == {"ok": True, "status": "disconnected", "revoked_at_github": True}
    # what GitHub refused comes back as the device's failure code
    for failure in ("token_refused", "scope_missing", "sso_required"):
        relay(hub).script["github.connect"] = [[({"step": "failed", "code": failure}, True)]]
        refused = tool.run("github", "connect", "--token", "-", "--wait", "5", stdin="ghp_x")[1]
        assert (refused["error"], refused["failure"]) == ("device_request_failed", failure)
    # never from the command line, never empty, never with spaces; not together with --request
    for argv, stdin in (
        (("--token", "ghp_x"), ""),
        (("--token", "-", "--request", uuid7()), "ghp_x"),
    ):
        assert tool.run("github", "connect", *argv, stdin=stdin)[1]["error"] == "invalid_argument", argv
    for stdin in ("", "  \n", "ghp x", "x" * 4097):
        missing = tool.run("github", "connect", "--token", "-", stdin=stdin)[1]
        assert (missing["error"], missing["reason"]) == ("invalid_argument", "token_missing"), stdin


def test_flows_that_fail_or_wait(tool: Tool, hub: HubFake, account: User) -> None:
    assign_device(tool, hub, account)
    relay(hub).script["github.connect"] = [[({"step": "failed", "code": "upstream_error"}, True)]]
    failed = tool.run("github", "connect", "--wait", "5")[1]
    assert (failed["error"], failed["failure"]) == ("device_request_failed", "upstream_error")
    started = tool.run("github", "connect", "--wait", "0")[1]
    assert started["step"] == "waiting"
    request = started["request_id"]
    stored = relay(hub).requests[request]
    for state, failure in (("expired", "timeout"), ("failed", "request_failed")):
        stored["state"] = state
        assert tool.run("github", "connect", "--request", request, "--wait", "0")[1]["failure"] == failure
    stored["state"] = "delivered"
    # answers that do not verify are no answers: another device's signature, a body the contract does not allow,
    # a wrong position, a missing envelope
    other, _seq = hub.certify_device()
    respond(hub, request, {"step": "done", "login": "octo"}, sign_with=other)
    respond(hub, request, {"step": "done", "login": "not a login"})
    respond(hub, request, {"step": "failed", "code": "made_up"})
    respond(hub, request, {"step": "done", "login": "octo"})
    stored["responses"][-1]["seq"] = 99
    stored["responses"].append({"seq": 5, "created_at": now_ms_text()})
    stored["responses"].append({"seq": "x", "envelope": stored["responses"][0]["envelope"]})
    assert tool.run("github", "connect", "--request", request, "--wait", "0")[1]["step"] == "waiting"
    # the waiting loop polls until the answer is there
    relay(hub).script["github.connect"] = [[]]
    later = tool.run("github", "connect", "--wait", "0")[1]["request_id"]
    path = f"/v1/device-requests/{later}"
    calls = []

    def answer_on_second_read(body: Any, request: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            respond(hub, later, DEVICE_CODE)

    hub.hooks[("GET", path)] = answer_on_second_read
    assert tool.run("github", "connect", "--request", later, "--wait", "5")[1]["step"] == "device_code"
    for argv in (("--wait", "601"), ("--wait", "-1"), ("--request", "x")):
        assert tool.run("github", "connect", *argv)[1]["error"] == "invalid_argument", argv


def test_claude_login_relay(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    authorize = {"step": "authorize", "url": "https://claude.ai/oauth/authorize?x=1"}
    relay(hub).script["claude.login"] = [[(authorize, False)], [({"step": "done", "email": "ada@example.com"}, True)]]
    seen = len(tool.opened())
    code, started = tool.run("claude", "login", "--wait", "5")
    assert code == 0, started
    assert (started["step"], started["url"]) == ("authorize", authorize["url"])
    assert wait_for(lambda: tool.opened()[seen:]) == [authorize["url"]]
    request = started["request_id"]
    assert opened(hub, request) == {"mode": "manual"}
    code, done = tool.run("claude", "login", "--request", request, "--code", "--wait", "5", stdin=" code#123 \n")
    assert code == 0, done
    assert (done["step"], done["email"]) == ("done", "ada@example.com")
    assert opened(hub, done["request_id"]) == {"code": "code#123"}
    assert relay(hub).requests[done["request_id"]]["follows"] == request
    assert tool.run("claude", "login", "--request", request, "--wait", "0")[1]["step"] == "authorize"
    # the automatic mode reports progress; a failed automation falls back to the relay
    progress = {"step": "progress", "stage": "browser_started"}
    relay(hub).script["claude.login"] = [[(progress, False)], [({**authorize, "automation": "failed"}, False)]]
    automatic = tool.run("claude", "login", "--mode", "automatic", "--wait", "0")[1]
    assert (automatic["step"], automatic["stage"]) == ("progress", "browser_started")
    assert opened(hub, automatic["request_id"]) == {"mode": "automatic"}
    fallback = tool.run("claude", "login", "--device", device, "--wait", "5")[1]
    assert fallback["automation"] == "failed"
    relay(hub).script["claude.login"] = [[({"step": "done"}, True)], [({"step": "done", "email": "bad"}, True)]]
    assert "email" not in tool.run("claude", "login", "--wait", "5")[1]
    assert tool.run("claude", "login", "--wait", "0")[1]["step"] == "waiting"
    for argv, stdin in (
        (("--code",), "x"),
        (("--request", request, "--code"), " "),
        (("--request", request, "--code"), "a b"),
    ):
        assert tool.run("claude", "login", *argv, stdin=stdin)[1]["error"] == "invalid_argument", argv
    github = tool.run("github", "connect", "--wait", "0")[1]["request_id"]
    assert tool.run("claude", "login", "--request", github)[1]["reason"] == "request_kind"


def test_credential_checks(tool: Tool, hub: HubFake, account: User) -> None:
    assign_device(tool, hub, account)
    tool.run("credentials", "set-api-key", stdin="sk-ant-x\n")
    relay(hub).script["credential.check"] = [[({"step": "done", "valid": True}, True)]]
    code, checked = tool.run("credentials", "check", "--kind", "anthropic_api_key", "--wait", "5")
    assert code == 0, checked
    assert (checked["step"], checked["valid"]) == ("done", True)
    assert opened(hub, checked["request_id"]) == {"kind": "anthropic_api_key"}
    started = tool.run("credentials", "check", "--kind", "mailbox", "--wait", "0")[1]
    assert opened(hub, started["request_id"]) == {}
    respond(hub, started["request_id"], {"step": "done", "valid": False}, final=True)
    resumed = tool.run("credentials", "check", "--request", started["request_id"], "--wait", "0")[1]
    assert resumed["valid"] is False
    assert tool.run("credentials", "check")[1]["error"] == "invalid_argument"


def test_device_choice(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    second, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": second, "platform_seq": seq}]
    tool.run("device", "assign", "--wg", wg)
    relay(hub).offline.add(min(device, second))
    online = max(device, second)
    assert tool.run("github", "connect", "--wait", "0")[1]["device_id"] == online
    relay(hub).offline.add(online)
    assert tool.run("github", "connect", "--wait", "0")[1]["device_id"] == min(device, second)
    # the hub lists a device twice (two orgs) and one the logs do not know
    hub.devices[account.id] = [
        {"device_id": uuid7(), "online": True},
        {"device_id": device, "online": False},
        {"device_id": device, "online": True},
        {"device_id": 5},
    ]
    assert tool.run("github", "connect", "--wait", "0", "--device", second)[1]["device_id"] == second
    assert tool.run("github", "connect", "--wait", "0")[1]["device_id"] == device
    for refused in (uuid7(), account.id):
        assert tool.run("github", "connect", "--device", refused)[1]["reason"] == "device_not_assigned"
    assert tool.run("github", "connect", "--device", "x")[1]["error"] == "invalid_argument"
    # an org whose log does not verify is skipped
    original = hub.r_me
    hub.override(
        "r_me",
        lambda request, body: (
            200,
            {**original(request, body)[1], "orgs": [{"org_id": uuid7()}, *original(request, body)[1]["orgs"]]},
        ),
    )
    assert tool.run("github", "connect", "--wait", "0", "--device", device)[1]["ok"] is True
    hub.override("r_me", original)
    # a device whose platform certification ended is no recipient any more
    head = hub.platform_entries[-1]
    hub._platform_append(
        {
            "seq": len(hub.platform_entries),
            "prev": entry_hash(head),
            "type": "device_decertify",
            "device": device,
            "by": "platform-root",
            "ts": now_ms_text(),
            "v": 1,
        }
    )
    assert tool.run("github", "connect", "--device", device)[1]["reason"] == "device_not_assigned"
    assert org


def test_a_missing_device_is_assigned_first(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    assert tool.run("github", "connect")[1]["error"] == "no_device"
    device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    started = tool.run("github", "connect", "--wait", "0")[1]
    assert started["device_id"] == device
    assert hub.orgs[private_org(hub, account)].log.state.assigned(device, wg)


def test_no_org_no_device(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(private_org=False)
    other = make_tool(tool.root.parent / "lonely")
    pair_user(other, hub, user)
    assert other.run("github", "connect")[1]["error"] == "no_device"


def test_malformed_request_answers(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    hub.hooks[("POST", "/v1/device-requests")] = lambda body, request: (200, {"request_id": uuid7(), "state": "x"})
    assert tool.run("github", "connect")[1]["reason"] == "response_invalid"
    del hub.hooks[("POST", "/v1/device-requests")]
    request = tool.run("github", "connect", "--wait", "0")[1]["request_id"]
    path = f"/v1/device-requests/{request}"
    view = {"request_id": request, "kind": "github.connect", "device_id": device, "state": "pending", "responses": []}
    hub.hooks[("GET", path)] = lambda body, r: (200, {**view, "kind": "claude.login"})
    assert tool.run("github", "connect", "--request", request)[1]["reason"] == "request_kind"
    hub.hooks[("GET", path)] = lambda body, r: (200, {**view, "device_id": uuid7()})
    assert tool.run("github", "connect", "--request", request)[1]["reason"] == "device_not_assigned"
    calls: list[int] = []

    def first_good_then_bad(body: Any, r: Any) -> tuple[int, Any]:
        calls.append(1)
        return 200, view if len(calls) == 1 else {**view, "state": None}

    hub.hooks[("GET", path)] = first_good_then_bad
    assert tool.run("github", "connect", "--request", request)[1]["reason"] == "response_invalid"


def test_credentials_list_and_records(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    assert tool.run("credentials", "list")[1]["credentials"] == []
    tool.run("credentials", "set-api-key", stdin="sk-ant-x\n")
    code, mailbox = tool.run(
        "credentials",
        "set-mailbox",
        "--imap-host",
        "imap.example.com",
        "--imap-user",
        "ada",
        "--email",
        "ada@example.com",
        stdin="pass word\n",
    )
    assert code == 0, mailbox
    subscription = {"token": "t", "email": None, "kind": "login_token", "expires_at": now_ms_text()}
    device_record(hub, account, device, "claude_subscription", subscription)
    device_record(hub, account, device, "github", GITHUB_BODY)
    code, listed = tool.run("credentials", "list")
    assert code == 0, listed
    rows = {row["kind"]: row for row in listed["credentials"]}
    assert [row["kind"] for row in listed["credentials"]] == [
        "github",
        "anthropic_api_key",
        "claude_subscription",
        "mailbox",
    ]
    assert (rows["mailbox"]["email"], rows["mailbox"]["host"]) == ("ada@example.com", "imap.example.com")
    assert rows["claude_subscription"]["origin"] == "login_token"
    assert {row["status"] for row in rows.values()} == {"verified"}
    text = json.dumps(listed)
    assert all(secret not in text for secret in ("pass word", "sk-ant-x", "gho_x"))
    # records that do not open
    for body, epoch, expected in (
        ({**GITHUB_BODY, "login": "bad login"}, 1, "params_invalid"),
        ({"key": "x"}, 1, "params_invalid"),
        ({**GITHUB_BODY, "created_at": "yesterday"}, 1, "params_invalid"),
        ({**GITHUB_BODY, "source": "other"}, 1, "params_invalid"),
        ({**GITHUB_BODY, "extra": 1}, 1, "params_invalid"),
        (GITHUB_BODY, 9, "epoch_stale"),
    ):
        device_record(hub, account, device, "github", body, epoch=epoch)
        assert tool.run("github", "status")[1] == {"ok": True, "status": "unverifiable", "code": expected}
    stranger, _seq = hub.certify_device()
    device_record(hub, account, stranger, "github", GITHUB_BODY)
    assert tool.run("github", "status")[1]["code"] == "author_not_writer"
    record = hub.credentials[(account.id, "github")]["record"]
    record["header"]["author"] = device
    assert tool.run("github", "status")[1]["code"] == "signature_invalid"
    record["header"]["owner"] = uuid7()
    assert tool.run("credentials", "list")[1]["credentials"][0]["code"] == "header_invalid"
    cases: list[tuple[object, str]] = [
        ("x", "header_invalid"),
        ({"owner": account.id, "kind": "github", "author": 5}, "author_not_writer"),
    ]
    for header, expected in cases:

        def served(body: Any, request: Any, header: object = header) -> tuple[int, Any]:
            return 200, {"record": {**record, "header": header}}

        hub.hooks[("GET", "/v1/me/credentials/github")] = served
        assert tool.run("github", "status")[1]["code"] == expected
    del hub.hooks[("GET", "/v1/me/credentials/github")]
    # the list names kinds the tool does not know, or a record that is gone
    hub.hooks[("GET", "/v1/me/credentials")] = lambda body, request: (
        200,
        {"credentials": [{"kind": "other"}, {"kind": "github"}]},
    )
    hub.credentials.pop((account.id, "github"))
    assert tool.run("credentials", "list")[1]["credentials"] == []
    hub.hooks[("GET", "/v1/me/credentials/github")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("credentials", "list")[1]["hub_error"] == "unavailable"
    del hub.hooks[("GET", "/v1/me/credentials")]
    # delete
    assert tool.run("credentials", "delete", "--kind", "mailbox")[1] == {"ok": True, "kind": "mailbox", "deleted": True}
    assert (account.id, "mailbox") not in hub.credentials
    assert tool.run("credentials", "delete", "--kind", "mailbox")[1]["deleted"] is True
    hub.hooks[("DELETE", "/v1/me/credentials/mailbox")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("credentials", "delete", "--kind", "mailbox")[1]["hub_error"] == "unavailable"
    assert tool.run("credentials", "delete", "--kind", "other")[1]["error"] == "invalid_argument"


def test_mailbox_input(tool: Tool, hub: HubFake, account: User) -> None:
    base = ["credentials", "set-mailbox", "--imap-host", "imap.example.com", "--imap-user", "ada"]
    for extra, stdin in (
        (["--email", "not-an-email"], "pw"),
        (["--email", "ada@example.com", "--imap-port", "0"], "pw"),
        (["--email", "ada@example.com"], ""),
        (["--email", "ada@example.com", "--imap-host", "bad host"], "pw"),
    ):
        assert tool.run(*base, *extra, stdin=stdin)[1]["error"] == "invalid_argument", extra
    assert tool.run(*base, "--email", "ada@example.com", "--imap-port", "143", stdin="pw")[1]["version"] == 1


def test_contract_checks() -> None:
    assert devicereq.response_body("github.connect", "x") is None
    done = {"step": "done", "login": "octo"}
    assert devicereq.response_body("github.connect", {**done, "scopes": [], "source": "device_flow"}) is not None
    assert devicereq.response_body("github.connect", {**done, "source": "other"}) is None
    assert devicereq.response_body("github.connect", {**done, "scopes": ["Repo"]}) is None
    assert devicereq.response_body("github.connect", {**done, "scopes": "repo"}) is None
    assert devicereq.response_body("github.connect", {**done, "extra": 1}) is None
    assert devicereq.response_body("github.connect", {**DEVICE_CODE, "expires_in": 0}) is None
    assert devicereq.response_body("github.connect", {**DEVICE_CODE, "verification_uri": "ftp://x"}) is None
    assert devicereq.response_body("claude.login", {"step": "authorize", "url": "https://x", "automation": "x"}) is None
    assert devicereq.response_body("claude.login", {"step": "progress", "stage": "x"}) is None
    assert devicereq.response_body("credential.check", {"step": "done", "valid": True, "x": 1}) is None
    assert devicereq.response_body("mailbox.check", {"step": "done", "valid": False}) == {
        "step": "done",
        "valid": False,
    }


def fake_gh(tmp_path: Path, script: str) -> str:
    """A stand-in for the GitHub CLI: a shell script that answers `gh auth token --hostname github.com`."""
    program = tmp_path / "gh"
    program.write_text(f"#!/bin/sh\n{script}\n")
    program.chmod(0o755)
    return str(program)


def test_github_connect_from_the_github_cli_login(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    _org, _wg, _device = assign_device(tool, hub, account)
    done = {"step": "done", "login": "octo", "scopes": ["repo", "workflow"], "source": "token"}
    relay(hub).script["github.connect"] = [[(done, True)]]
    gh = fake_gh(tmp_path, 'test "$*" = "auth token --hostname github.com" && printf "gho_fromcli\\n"')
    code, connected = tool.run("github", "connect", "--from-gh", "--wait", "5", env={"AGENT_PLATFORM_GH": gh})
    assert code == 0, connected
    assert (connected["step"], connected["login"]) == ("done", "octo")
    # the token of the login goes only into the request sealed to the device; nothing is opened in a browser
    assert opened(hub, connected["request_id"]) == {"token": "gho_fromcli"}
    assert "gho_fromcli" not in json.dumps(connected)


@pytest.mark.parametrize(
    "case",
    [(None, "gh_missing"), ("echo 'not logged in' >&2; exit 1", "gh_not_logged_in"), ("printf ''", "gh_not_logged_in")],
)
def test_github_connect_from_gh_without_a_login(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, case: tuple[str | None, str]
) -> None:
    script, reason = case
    assign_device(tool, hub, account)
    if script is None:  # a file that is not a program
        (tmp_path / "gh").write_text("")
        program = str(tmp_path / "gh")
    else:
        program = fake_gh(tmp_path, script)
    code, failed = tool.run("github", "connect", "--from-gh", env={"AGENT_PLATFORM_GH": program})
    assert code != 0
    assert (failed["error"], failed["reason"]) == ("github_login_missing", reason)
    assert "gh auth login" in failed["next"]


def test_github_connect_from_gh_that_cannot_start(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    assign_device(tool, hub, account)
    broken = tmp_path / "gh"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    failed = tool.run("github", "connect", "--from-gh", env={"AGENT_PLATFORM_GH": str(broken)})[1]
    assert (failed["error"], failed["reason"]) == ("github_login_missing", "gh_failed")


def test_github_connect_names_the_device_flow(tool: Tool, hub: HubFake, account: User) -> None:
    assign_device(tool, hub, account)
    relay(hub).script["github.connect"] = [[(DEVICE_CODE, False)]]
    started = tool.run("github", "connect", "--app", "github_app", "--wait", "5")[1]
    assert opened(hub, started["request_id"]) == {"app": "github_app"}


@pytest.mark.parametrize(
    "argv",
    [
        ("--from-gh", "--token", "-"),
        ("--from-gh", "--app", "oauth_app"),
        ("--app", "oauth_app", "--request", "0193002d-0000-7000-8000-000000000001"),
        ("--token", "x"),
    ],
)
def test_github_connect_takes_one_way(tool: Tool, hub: HubFake, account: User, argv: tuple[str, ...]) -> None:
    assign_device(tool, hub, account)
    assert tool.run("github", "connect", *argv)[1]["error"] == "invalid_argument"
