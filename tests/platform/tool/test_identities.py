"""GitHub identities against the hub double (ADR-078): `github accounts|primary|remove|rewrap|default set`, `github
connect --org|--identity|--signing-key` with the company account sealed for the organization's devices,
`task create --github-identity`, `task github-identity`, the identities of `task operations` and the re-sealing of
further own accounts after a rotation of `CK`."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import devicereq, identities
from agent_platform.account import Account
from agent_platform.credentials import valid_body
from agent_platform.errors import PlatformError
from agent_platform.hub import refused
from agent_platform.state import Home
from agent_runner.crypto.credential import new_header, open_credential, seal_credential
from agent_runner.crypto.encoding import b64u_encode, body_json, parse_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.wrap import unwrap_key

from .conftest import Tool
from .fakes.identities import ORG, USER, add_record, state_of
from .fakes.relay import opened, relay, respond
from .fakes.sessions import add_operation, add_session
from .fakes.sessions import state_of as sessions_of
from .hubfake import HubFake, Json, User, now_ms_text
from .seeding import assign_device, new_task, reader
from .test_devicereq import fake_gh

SEED = b64u_encode(b"s" * 32)
PUBLIC = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"k" * 32).decode()
SIGNING_KEY = {"alg": "ssh-ed25519", "seed": SEED, "public": PUBLIC, "github_key_id": 9}
BODY = {"token": "gho_secret", "login": "octo", "user_id": 7, "scopes": ["repo"], "created_at": now_ms_text()}
COMPANY = {**BODY, "token": "ghp_company", "login": "acme-bot", "user_id": 8, "source": "token"}
DEVICE_CODE = {
    "step": "device_code",
    "user_code": "ABCD-1234",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devicereq, "POLL_S", 0.01)


def own_record(
    hub: HubFake,
    user: User,
    body: object = BODY,
    *,
    author: str | None = None,
    epoch: int = 1,
    version: int = 1,
    record_id: str | None = None,
) -> Json:
    """A record of the user's `github` kind (written by the user, or by the device `author`)."""
    fields = new_header(
        id=record_id or uuid7(),
        owner=user.id,
        kind="github",
        version=version,
        epoch=epoch,
        author=author or user.id,
        ts=now_ms_text(),
    )
    signer = hub.platform_devices[author].sig if author is not None else user.identity.sig
    return seal_credential(user.cred_keys.get(epoch, b"k" * 32), fields, body_json(body), signer)


def primary(hub: HubFake, user: User, body: object = BODY, **options: Any) -> str:
    record = own_record(hub, user, body, **options)
    hub.credentials[(user.id, "github")] = {"record": record, "version": record["header"]["version"]}
    return str(record["header"]["id"])


def further(hub: HubFake, user: User, body: object = BODY, **options: Any) -> str:
    return add_record(hub, user.id, USER, own_record(hub, user, body, **options))


def company(hub: HubFake, user: User, org: str, *, epoch: int = 1) -> str:
    """A company record of `org` as stored (the tool never opens it)."""
    fields = new_header(id=uuid7(), owner=org, kind="github", version=1, epoch=epoch, author=user.id, ts=now_ms_text())
    state_of(hub).org_epochs[org] = max(epoch, state_of(hub).org_epochs.get(org, 0))
    return add_record(hub, org, ORG, seal_credential(b"c" * 32, fields, body_json(COMPANY), user.identity.sig))


def serve(answer: object) -> Any:
    """A hook answering `answer` with `200`."""
    return lambda body, request: (200, answer)


def entries(tool: Tool) -> dict[str, Json]:
    code, listed = tool.run("github", "accounts")
    assert code == 0, listed
    return {entry["id"]: entry for entry in listed["identities"]}


def company_refusal(tool: Tool, org: str) -> str:
    """The code with which `identities.store_company` refuses the tool's user for `org`."""
    opened_by = Account.open(Home.from_env(tool.env))
    try:
        with pytest.raises(PlatformError) as caught:
            identities.store_company(opened_by, org, COMPANY, None)
    finally:
        opened_by.close()
    return caught.value.code


# --- github accounts ---


def test_accounts_lists_own_and_company_identities(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    assert tool.run("github", "accounts")[1] == {"ok": True, "identities": [], "defaults": []}
    main = primary(hub, account, {**BODY, "token": "ghu_minted"}, author=device)
    named = further(hub, account, {**BODY, "login": "octo-work", "name": "Octo Work", "signing_key": SIGNING_KEY})
    stale = further(hub, account, epoch=9)
    shared = company(hub, account, org)
    state_of(hub).defaults[("org", org)] = {"identity_id": shared, "set_by": account.id, "set_at": now_ms_text()}
    state_of(hub).defaults[("wg", wg)] = {"identity_id": shared, "set_by": account.id, "set_at": now_ms_text()}
    code, listed = tool.run("github", "accounts")
    assert code == 0, listed
    found = {entry["id"]: entry for entry in listed["identities"]}
    assert found[main]["status"] == "verified"
    assert (found[main]["primary"], found[main]["login"], found[main]["revocable"]) == (True, "octo", True)
    assert "name" not in found[main]
    assert (found[named]["name"], found[named]["signing"], found[named]["primary"]) == ("Octo Work", True, False)
    assert (found[stale]["status"], found[stale]["code"]) == ("unverifiable", "epoch_stale")
    assert found[shared] == {
        "id": shared,
        "owner_kind": "org",
        "primary": False,
        "status": "company",
        "org_id": org,
        "created_at": found[shared]["created_at"],
        "version": 1,
        "epoch": 1,
        "updated_at": found[shared]["updated_at"],
    }
    assert [(item["scope"], item["scope_id"]) for item in listed["defaults"]] == [("org", org), ("wg", wg)]
    text = json.dumps(listed)
    assert all(secret not in text for secret in ("gho_secret", "ghu_minted", "ghp_company", SEED))
    # a record gone between the list and its read is left out; any other failure of the read fails the command
    path = f"/v1/github-identities/{named}"
    hub.hooks[("GET", path)] = lambda body, request: (404, {"error": "not_found"})
    assert named not in entries(tool)
    hub.hooks[("GET", path)] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("github", "accounts")[1]["hub_error"] == "unavailable"
    del hub.hooks[("GET", path)]


def test_own_records_are_checked_like_the_primary_one(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    stranger, _seq = hub.certify_device()
    cases = {
        further(hub, account, {**BODY, "name": "a<b"}): "params_invalid",
        further(hub, account, {**BODY, "signing_key": {**SIGNING_KEY, "alg": "rsa"}}): "params_invalid",
        further(hub, account, author=stranger): "author_not_writer",
        further(hub, account, author=device): None,
    }
    found = entries(tool)
    for identity, code in cases.items():
        assert found[identity].get("code") == code, identity
    # what the hub serves for a record is checked: owner, kind, ID, author, signature
    identity = next(iter(cases))
    answer = {"owner_id": account.id, "owner_kind": USER, "primary": False}
    record = state_of(hub).records[identity]["record"]
    forged: list[tuple[Json, str]] = [
        ({**answer, "owner_id": uuid7(), "record": record}, "header_invalid"),
        ({**answer, "owner_kind": ORG, "record": record}, "header_invalid"),
        ({**answer, "record": "x"}, "header_invalid"),
        ({**answer, "record": {**record, "header": {**record["header"], "id": uuid7()}}}, "header_invalid"),
        ({**answer, "record": {**record, "header": {**record["header"], "kind": "mailbox"}}}, "header_invalid"),
        ({**answer, "record": {**record, "header": {**record["header"], "author": 5}}}, "author_not_writer"),
        ({**answer, "record": {**record, "header": {**record["header"], "version": 2}}}, "signature_invalid"),
    ]
    for served, code in forged:
        hub.hooks[("GET", f"/v1/github-identities/{identity}")] = serve(served)
        assert entries(tool)[identity]["code"] == code
    # a malformed list is refused as a whole
    good: Json = {"identities": [], "defaults": []}
    meta = {
        "id": identity,
        "owner_id": account.id,
        "owner_kind": USER,
        "primary": False,
        "version": 1,
        "epoch": 1,
        "created_at": "t",
        "updated_at": "t",
    }
    default = {"scope": "org", "scope_id": uuid7(), "identity_id": identity, "set_by": account.id, "set_at": "t"}
    for answer_body in (
        {**good, "identities": None},
        {**good, "defaults": "x"},
        {**good, "identities": ["x"]},
        {**good, "identities": [{**meta, "owner_kind": "team"}]},
        {**good, "identities": [{**meta, "primary": 1}]},
        {**good, "identities": [{**meta, "version": True}]},
        {**good, "identities": [{**meta, "epoch": 0}]},
        {**good, "identities": [{**meta, "id": 5}]},
        {**good, "defaults": [{**default, "scope": "team"}]},
        {**good, "defaults": [{**default, "set_at": None}]},
        {**good, "defaults": [5]},
    ):
        hub.hooks[("GET", "/v1/me/github-identities")] = serve(answer_body)
        assert tool.run("github", "accounts")[1]["reason"] == "response_invalid", answer_body


# --- primary, remove ---


def test_primary_and_remove(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, _device = assign_device(tool, hub, account)
    main = primary(hub, account, {**BODY, "token": "ghu_minted"})
    other = further(hub, account, {**BODY, "login": "octo-work"})
    assert tool.run("github", "primary", "--identity", other)[1] == {"ok": True, "identity_id": other, "primary": True}
    found = entries(tool)
    assert (found[other]["primary"], found[main]["primary"]) == (True, False)
    assert tool.run("github", "status")[1]["login"] == "octo-work"
    assert tool.run("github", "primary", "--identity", "x")[1]["error"] == "invalid_argument"
    assert tool.run("github", "primary", "--identity", uuid7())[1]["hub_error"] == "not_found"
    # an own record: whether the devices revoke the token at GitHub (minted by the platform) or where to revoke it
    removed = tool.run("github", "remove", "--identity", main)[1]
    assert removed == {
        "ok": True,
        "status": "removed",
        "identity_id": main,
        "owner_kind": "user",
        "primary": False,
        "revoked_at_github": True,
    }
    removed = tool.run("github", "remove", "--identity", other)[1]
    assert (removed["primary"], removed["revoked_at_github"]) == (True, False)
    assert removed["revoke_at"] == "https://github.com/settings/applications"
    assert tool.run("github", "status")[1]["status"] == "disconnected"
    # a record that does not open, or is gone when read, names the page as well
    broken = further(hub, account, epoch=9)
    assert tool.run("github", "remove", "--identity", broken)[1]["revoked_at_github"] is False
    gone = further(hub, account)
    hub.hooks[("GET", f"/v1/github-identities/{gone}")] = lambda body, request: (404, {"error": "not_found"})
    assert tool.run("github", "remove", "--identity", gone)[1]["revoke_at"].startswith("https://")
    # a company record (as admin), an unknown record (404 counts as done) and other refusals
    shared = company(hub, account, org)
    assert tool.run("github", "remove", "--identity", shared)[1] == {
        "ok": True,
        "status": "removed",
        "identity_id": shared,
        "owner_kind": "org",
        "primary": False,
    }
    unknown = uuid7()
    assert tool.run("github", "remove", "--identity", unknown)[1] == {
        "ok": True,
        "status": "removed",
        "identity_id": unknown,
    }
    hub.hooks[("DELETE", f"/v1/github-identities/{unknown}")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("github", "remove", "--identity", unknown)[1]["hub_error"] == "unavailable"
    assert tool.run("github", "remove", "--identity", "x")[1]["error"] == "invalid_argument"


# --- defaults and the task's identity ---


def test_defaults_and_task_identity(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, _device = assign_device(tool, hub, account)
    shared = company(hub, account, org)
    own = primary(hub, account)
    assert tool.run("github", "default", "set", "--org", org, "--identity", shared)[1] == {
        "ok": True,
        "scope": "org",
        "scope_id": org,
        "identity_id": shared,
    }
    assert tool.run("github", "default", "set", "--wg", wg, "--identity", shared)[1]["scope"] == "wg"
    listed = tool.run("github", "accounts")[1]["defaults"]
    assert [(item["scope"], item["identity_id"]) for item in listed] == [("org", shared), ("wg", shared)]
    assert tool.run("github", "default", "set", "--wg", wg, "--none")[1]["identity_id"] is None
    refused_own = tool.run("github", "default", "set", "--org", org, "--identity", own)[1]
    assert (refused_own["hub_error"], refused_own["status"]) == ("identity_forbidden", 403)
    for argv in (
        ("--org", org),
        ("--org", org, "--wg", wg, "--none"),
        ("--org", org, "--identity", shared, "--none"),
        ("--org", "x", "--none"),
        ("--org", org, "--identity", "x"),
    ):
        assert tool.run("github", "default", "set", *argv)[1]["error"] == "invalid_argument", argv
    # devices that did not announce the feature: the refusal names them
    outdated = [uuid7()]
    state_of(hub).outdated = outdated
    blocked = tool.run("github", "default", "set", "--org", org, "--identity", shared)[1]
    assert (blocked["error"], blocked["hub_error"], blocked["devices"]) == ("hub_refused", "peer_outdated", outdated)
    assert tool.run("github", "default", "set", "--org", org, "--none")[1]["ok"] is True
    # the task's identity: at creation and changed later
    code, created = tool.run("task", "create", "--wg", wg, "--text", "Fix it", "--github-identity", own)
    assert (code, created["ok"]) == (1, False)
    assert created["devices"] == outdated
    state_of(hub).outdated = []
    code, created = tool.run("task", "create", "--wg", wg, "--text", "Fix it", "--github-identity", own)
    assert code == 0, created
    task = created["task_id"]
    assert (created["github_identity"], hub.tasks[task]["task"]["github_identity"]) == (own, own)
    assert "github_identity" not in tool.run("task", "create", "--wg", wg, "--text", "Automatic")[1]
    assert tool.run("task", "create", "--wg", wg, "--text", "x", "--github-identity", "x")[1]["error"] == (
        "invalid_argument"
    )
    assert tool.run("task", "github-identity", "--task", task, "--identity", shared)[1] == {
        "ok": True,
        "task_id": task,
        "github_identity": shared,
    }
    assert tool.run("task", "github-identity", "--task", task, "--none")[1]["github_identity"] is None
    assert hub.tasks[task]["task"]["github_identity"] is None
    forbidden = tool.run("task", "github-identity", "--task", task, "--identity", uuid7())[1]
    assert forbidden["hub_error"] == "identity_forbidden"
    assert tool.run("task", "github-identity", "--task", "x", "--none")[1]["error"] == "invalid_argument"
    assert tool.run("task", "github-identity", "--task", task)[1]["error"] == "invalid_argument"


def test_refusals_keep_their_detail() -> None:
    devices = [uuid7(), uuid7()]
    error = refused("peer_outdated", 409, detail={"devices": devices})
    assert (error.fields["devices"], error.detail) == (devices, {"devices": devices})
    cases: list[tuple[str, dict[str, object] | None]] = [
        ("peer_outdated", {"devices": []}),
        ("peer_outdated", {"devices": ["x"]}),
        ("peer_outdated", {"devices": "x"}),
        ("peer_outdated", None),
        ("forbidden", {"devices": devices}),
    ]
    for code, detail in cases:
        assert "devices" not in refused(code, 409, detail=detail).fields
    assert refused("version_conflict", 409, detail={"expected": 3}).detail == {"expected": 3}
    assert PlatformError("internal").detail == {}


# --- company accounts ---


def device_opens(hub: HubFake, device: str, org: str, identity: str) -> object:
    """The company record as the org's device opens it: its wrap of the record's epoch, then the record."""
    row = state_of(hub).records[identity]
    epoch = row["record"]["header"]["epoch"]
    wrap = next(
        item
        for item in hub.wraps
        if (item["scope"], item["scope_id"], item["epoch"], item["recipient_id"]) == ("cred", org, epoch, device)
    )
    key = unwrap_key(
        hub.platform_devices[device].kem, scope="cred", scope_id=org, epoch=epoch, recipient=device, wrap=wrap["wrap"]
    )
    author = hub.users[row["record"]["header"]["author"]]
    return parse_json(open_credential(key, row["record"], author.identity.sig_public()))


def test_company_account_connect(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    done = {"step": "done", "login": "acme-bot", "source": "token", "signing": "registered", "body": COMPANY}
    relay(hub).script["github.connect"] = [[(done, True)]]
    argv = ("github", "connect", "--org", org, "--token", "-", "--signing-key", "--wait", "5")
    code, connected = tool.run(*argv, stdin="ghp_company\n")
    assert code == 0, connected
    identity = connected["identity_id"]
    assert connected == {
        "ok": True,
        "request_id": connected["request_id"],
        "device_id": device,
        "status": "connected",
        "identity_id": identity,
        "owner": "org",
        "org_id": org,
        "version": 1,
        "login": "acme-bot",
        "source": "token",
        "signing": "registered",
    }
    # the org goes along in plaintext for the hub's admin check, the options only inside the sealed request
    request = relay(hub).requests[connected["request_id"]]
    assert request["org_id"] == org
    assert opened(hub, connected["request_id"]) == {"token": "ghp_company", "org_id": org, "signing_key": True}
    # sealed under epoch 1 of the org's credential key, wrapped for the org's device only; never printed
    assert device_opens(hub, device, org, identity) == COMPANY
    stored = state_of(hub).records[identity]
    assert (stored["owner_id"], stored["record"]["header"]["author"], stored["epoch"]) == (org, account.id, 1)
    assert {item["recipient_id"] for item in hub.wraps if (item["scope"], item["scope_id"]) == ("cred", org)} == {
        device
    }
    assert "ghp_company" not in json.dumps(connected)
    assert entries(tool)[identity]["status"] == "company"
    # a reconnect fills the same record (version 2) under a new epoch; the device flow's answer without `signing`
    renewed = {**COMPANY, "token": "ghu_renewed", "source": "device_flow"}
    relay(hub).script["github.connect"] = [
        [(DEVICE_CODE, False), ({"step": "done", "login": "acme-bot", "body": renewed}, True)]
    ]
    code, again = tool.run("github", "connect", "--org", org, "--identity", identity, "--wait", "5")
    assert code == 0, again
    assert (again["identity_id"], again["version"], again["source"]) == (identity, 2, "device_flow")
    assert "signing" not in again
    assert opened(hub, again["request_id"]) == {"org_id": org}
    assert device_opens(hub, device, org, identity) == renewed
    assert state_of(hub).records[identity]["epoch"] == 2


def test_company_flow_continued_and_refused(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    relay(hub).script["github.connect"] = [[(DEVICE_CODE, False)]]
    code, started = tool.run("github", "connect", "--org", org, "--wait", "5")
    assert (code, started["step"], started["device_id"]) == (0, "device_code", device)
    request = started["request_id"]
    respond(hub, request, {"step": "done", "login": "acme-bot", "body": COMPANY}, final=True)
    # without `--org` the body of a company answer is neither stored nor printed
    refused_body = tool.run("github", "connect", "--request", request, "--wait", "0")[1]
    assert (refused_body["error"], refused_body["reason"]) == ("invalid_argument", "org_missing")
    assert "ghp_company" not in json.dumps(refused_body)
    code, stored = tool.run("github", "connect", "--org", org, "--request", request, "--wait", "0")
    assert (code, stored["status"], stored["version"]) == (0, "connected", 1), stored
    # a company answer without a body
    relay(hub).script["github.connect"] = [[({"step": "done", "login": "acme-bot"}, True)]]
    failed = tool.run("github", "connect", "--org", org, "--wait", "5")[1]
    assert (failed["error"], failed["failure"]) == ("device_request_failed", "params_invalid")
    # unknown record to reconnect, malformed options, a device of another org, a non-admin, an org without devices
    relay(hub).script["github.connect"] = [[({"step": "done", "login": "acme-bot", "body": COMPANY}, True)]]
    assert tool.run("github", "connect", "--org", org, "--identity", uuid7(), "--wait", "5")[1]["error"] == (
        "not_found"
    )
    for argv in (("--org", "x"), ("--identity", "x"), ("--org", org, "--identity", "x")):
        assert tool.run("github", "connect", *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("github", "connect", "--org", org, "--device", uuid7())[1]["error"] == "no_device"
    other, bob, acme, _research = reader(tool, hub, tmp_path)
    assert tool.run("github", "connect", "--org", acme)[1]["error"] == "no_device"
    assert other.run("github", "connect", "--org", acme)[1]["error"] == "not_admin"
    assert company_refusal(other, acme) == "not_admin"
    assert company_refusal(tool, acme) == "no_device"
    assert bob.id != account.id


def test_company_writes_that_meet_another_admin(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    state = state_of(hub)
    opened_by = Account.open(Home.from_env(tool.env))
    try:
        # a removed record raised the epoch: the hub's `detail.expected` names it (the version was right)
        state.org_epochs[org] = 4
        first = identities.store_company(opened_by, org, COMPANY, None)
        assert (first["version"], first["epoch"]) == (1, 5)
        assert device_opens(hub, device, org, first["identity_id"]) == COMPANY
        # another admin wrote the record meanwhile: the retry takes the new version and epoch from the fresh list
        target = f"/v1/github-identities/{first['identity_id']}"

        def other_admin_wrote(body: Any, request: Any) -> tuple[int, Any]:
            del hub.hooks[("PUT", target)]
            row = state.records[first["identity_id"]]
            row["version"], row["epoch"] = 2, 6
            state.org_epochs[org] = 6
            return 409, {"error": "version_conflict", "detail": {"expected": 2}}

        hub.hooks[("PUT", target)] = other_admin_wrote
        second = identities.store_company(opened_by, org, COMPANY, first["identity_id"])
        assert (second["version"], second["epoch"]) == (3, 7)
        # twice in a row, other refusals and an answer that names another version
        hub.hooks[("PUT", target)] = lambda body, request: (409, {"error": "epoch_stale"})
        with pytest.raises(PlatformError) as caught:
            identities.store_company(opened_by, org, COMPANY, first["identity_id"])
        assert caught.value.fields == {"hub_error": "version_conflict", "status": 409}
        hub.hooks[("PUT", target)] = lambda body, request: (500, {"error": "unavailable"})
        with pytest.raises(PlatformError) as caught:
            identities.store_company(opened_by, org, COMPANY, first["identity_id"])
        assert caught.value.fields["hub_error"] == "unavailable"
        hub.hooks[("PUT", target)] = lambda body, request: (200, {"version": 99})
        with pytest.raises(PlatformError) as caught:
            identities.store_company(opened_by, org, COMPANY, first["identity_id"])
        assert caught.value.fields["reason"] == "response_invalid"
    finally:
        opened_by.close()
    # a conflict whose detail names no usable epoch
    details: list[dict[str, object]] = [{"expected": True}, {"expected": 0}, {"expected": "5"}]
    for detail in details:
        error = PlatformError("hub_refused")
        error.detail = detail
        assert identities._expected(error) is None


# --- own connects ---


def test_own_connect_options(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device = assign_device(tool, hub, account)
    identity = uuid7()
    done = {"step": "done", "login": "octo", "identity_id": identity, "signing": "scope_missing"}
    relay(hub).script["github.connect"] = [[(done, True)]]
    code, connected = tool.run("github", "connect", "--identity", identity, "--signing-key", "--wait", "5")
    assert code == 0, connected
    assert (connected["identity_id"], connected["signing"], connected["device_id"]) == (
        identity,
        "scope_missing",
        device,
    )
    assert opened(hub, connected["request_id"]) == {"identity_id": identity, "signing_key": True}
    assert relay(hub).requests[connected["request_id"]]["org_id"] is None


def test_response_and_body_contracts() -> None:
    done = {"step": "done", "login": "octo"}
    check = devicereq.response_body
    assert check("github.connect", {**done, "identity_id": uuid7(), "signing": "refused", "body": COMPANY}) is not None
    assert check("github.connect", {**done, "source": "operator"}) is not None
    for wrong in ({"identity_id": "x"}, {"signing": "maybe"}, {"body": "x"}, {"body": {**COMPANY, "login": "a b"}}):
        assert check("github.connect", {**done, **wrong}) is None, wrong
    assert valid_body("github", {**BODY, "name": "Octo", "source": "operator", "signing_key": SIGNING_KEY})
    assert valid_body("github", {**BODY, "signing_key": {k: v for k, v in SIGNING_KEY.items() if k != "github_key_id"}})
    for wrong in (
        {"name": ""},
        {"name": "a>b"},
        {"signing_key": "x"},
        {"signing_key": {**SIGNING_KEY, "extra": 1}},
        {"signing_key": {k: v for k, v in SIGNING_KEY.items() if k != "seed"}},
        {"signing_key": {**SIGNING_KEY, "seed": "short"}},
        {"signing_key": {**SIGNING_KEY, "public": "ssh-rsa AAAA"}},
        {"signing_key": {**SIGNING_KEY, "github_key_id": 0}},
        {"signing_key": {**SIGNING_KEY, "github_key_id": True}},
    ):
        assert not valid_body("github", {**BODY, **wrong}), wrong


# --- re-sealing under the newest key ---


def test_further_accounts_follow_a_rotation(tool: Tool, hub: HubFake, account: User) -> None:
    assign_device(tool, hub, account)
    main = primary(hub, account)
    moved = further(hub, account, {**BODY, "login": "octo-work"})
    assert tool.run("github", "rewrap")[1]["resealed"] == 0
    code, rotated = tool.run("credentials", "rotate", "--force")
    assert code == 0, rotated
    assert (rotated["status"], rotated["records"], rotated["github_resealed"]) == ("rotated", 1, 1)
    row = state_of(hub).records[moved]
    assert (row["epoch"], row["version"], row["record"]["header"]["author"]) == (2, 2, account.id)
    found = entries(tool)
    assert (found[moved]["status"], found[moved]["login"], found[main]["epoch"]) == ("verified", "octo-work", 2)
    assert tool.run("github", "rewrap")[1]["resealed"] == 0
    # records gone, not opening or written meanwhile wait for the next run; a failing hub is reported
    gone = further(hub, account)
    broken = further(hub, account, epoch=9)
    busy = further(hub, account)
    for identity in (gone, broken, busy):
        state_of(hub).records[identity]["epoch"] = 1
    hub.hooks[("GET", f"/v1/github-identities/{gone}")] = lambda body, request: (404, {"error": "not_found"})
    hub.hooks[("PUT", f"/v1/github-identities/{busy}")] = lambda body, request: (409, {"error": "version_conflict"})
    assert tool.run("github", "rewrap")[1]["resealed"] == 0
    hub.hooks[("PUT", f"/v1/github-identities/{busy}")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("github", "rewrap")[1]["hub_error"] == "unavailable"
    rotated = tool.run("credentials", "rotate", "--force")[1]
    assert (rotated["status"], rotated["github_rewrap_error"]) == ("rotated", "unavailable")


# --- the identities of operations ---


def test_operations_name_their_identity(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    own = primary(hub, account)
    shared = company(hub, account, org)
    broken = further(hub, account, epoch=9)  # an own record that does not open is named like any other account
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    rows = {}
    for identity in (own, shared, broken, None):
        operation = add_operation(hub, task, session)
        sessions_of(hub).operations[operation]["operation"] |= {"github_identity": identity, "triggered_by": account.id}
        rows[operation] = identity
    code, listed = tool.run("task", "operations", "--task", task)
    assert code == 0, listed
    names = {row["operation_id"]: row.get("github_account") for row in listed["operations"]}
    by_identity = {rows[operation]: name for operation, name in names.items()}
    assert by_identity[own] == {"kind": "login", "login": "octo"}
    assert by_identity[shared] == {"kind": "company", "org_id": org}
    assert by_identity[None] is None
    assert sorted(name["kind"] for name in by_identity.values() if name is not None) == ["company", "login", "other"]
    assert {row["triggered_by"] for row in listed["operations"]} == {account.id}
    first = next(operation for operation, identity in rows.items() if identity == own)
    one = tool.run("task", "operations", "--task", task, "--operation", first)[1]
    assert one["operation"]["github_account"] == {"kind": "login", "login": "octo"}
    # without the list every identity is `other`
    hub.hooks[("GET", "/v1/me/github-identities")] = lambda body, request: (500, {"error": "unavailable"})
    one = tool.run("task", "operations", "--task", task, "--operation", first)[1]
    assert one["operation"]["github_account"] == {"kind": "other"}
    unnamed = next(operation for operation, identity in rows.items() if identity is None)
    assert (
        "github_account" not in tool.run("task", "operations", "--task", task, "--operation", unnamed)[1]["operation"]
    )


def test_company_account_from_the_github_cli_login_and_app_choice(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path
) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    gh = fake_gh(tmp_path, 'test "$*" = "auth token --hostname github.com" && printf "ghp_company\\n"')
    relay(hub).script["github.connect"] = [[({"step": "done", "login": "acme-bot", "body": COMPANY}, True)]]
    argv = ("github", "connect", "--from-gh", "--org", org, "--wait", "5")
    code, connected = tool.run(*argv, env={"AGENT_PLATFORM_GH": gh})
    assert (code, connected["status"], connected["owner"]) == (0, "connected", "org"), connected
    assert opened(hub, connected["request_id"]) == {"token": "ghp_company", "org_id": org}
    assert device_opens(hub, device, org, connected["identity_id"]) == COMPANY
    # the device flow of a chosen app, for an own account to reconnect
    identity = uuid7()
    relay(hub).script["github.connect"] = [[({"step": "done", "login": "octo", "identity_id": identity}, True)]]
    code, own = tool.run("github", "connect", "--app", "oauth_app", "--identity", identity, "--wait", "5")
    assert code == 0, own
    assert opened(hub, own["request_id"]) == {"app": "oauth_app", "identity_id": identity}
