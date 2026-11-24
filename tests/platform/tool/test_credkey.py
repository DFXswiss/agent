"""`credentials rotate` and the credential-key check after membership changes (stage a, 04-crypto.md sections 4.4,
5.10; the web client's job 5): when it is owed, what is re-encrypted, dropped or blocks, and the hub's rules for
`POST /v1/me/credentials/rotate` in the fake."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_platform import credkey
from agent_runner.crypto.credential import new_header, open_credential, seal_credential
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.wrap import unwrap_key

from .conftest import Tool, pair_user
from .hubfake import HubFake, User, now_ms_text
from .teams import assign, decertify, grant_platform_access, join, private_org, team, wg_create

API_KEY = {"key": "sk-test-" + "k" * 20}
GITHUB = {"token": "ghp_x", "login": "octo", "user_id": 7, "scopes": ["repo"], "created_at": "2026-01-01T00:00:00.000Z"}
MAILBOX = {"imap_host": "imap.test", "imap_port": 993, "imap_user": "u", "imap_password": "p", "email": "a@b.test"}
SUBSCRIPTION = {"token": "t", "email": None, "kind": "setup_token"}


def seal(
    hub: HubFake,
    user: User,
    kind: str,
    body: object,
    *,
    author: str | None = None,
    signer: Identity | None = None,
    epoch: int = 1,
    key: bytes | None = None,
) -> None:
    """A stored record of `user` (by default written by the user under `CK_1`)."""
    header = new_header(
        id=uuid7(), owner=user.id, kind=kind, version=1, epoch=epoch, author=author or user.id, ts=now_ms_text()
    )
    record = seal_credential(key or user.cred_keys[1], header, body_json(body), (signer or user.identity).sig)
    hub.credentials[(user.id, kind)] = {"record": record, "version": 1}


def key_of(hub: HubFake, user: User, epoch: int) -> bytes:
    """The user's `CK` of `epoch` from its self-wrap."""
    own = next(
        w
        for w in hub.wraps
        if (w["scope"], w["scope_id"], w["epoch"], w["recipient_id"]) == ("cred", user.id, epoch, user.id)
    )
    return unwrap_key(
        user.identity.kem, scope="cred", scope_id=user.id, epoch=epoch, recipient=user.id, wrap=own["wrap"]
    )


def holders(hub: HubFake, epoch: int, devices: list[str]) -> None:
    """`GET /v1/me` names `devices` as holders of `CK_epoch`."""
    original = hub.r_me
    hub.override(
        "r_me", lambda r, b: (200, {**original(r, b)[1], "cred_holders": {"epoch": epoch, "devices": devices}})
    )


@pytest.mark.parametrize(
    ("kind", "body", "valid"),
    [
        ("anthropic_api_key", API_KEY, True),
        ("anthropic_api_key", {"key": "has space"}, False),
        ("anthropic_api_key", {"key": "k", "x": 1}, False),
        ("anthropic_api_key", ["key"], False),
        ("github", GITHUB, True),
        ("github", {**GITHUB, "login": "bad login"}, False),
        ("github", {**GITHUB, "user_id": 0}, False),
        ("github", {**GITHUB, "user_id": True}, False),
        ("github", {**GITHUB, "scopes": "repo"}, False),
        ("github", {**GITHUB, "scopes": ["Repo"]}, False),
        ("github", {**GITHUB, "created_at": "yesterday"}, False),
        ("github", {**GITHUB, "extra": 1}, False),
        ("mailbox", MAILBOX, True),
        ("mailbox", {**MAILBOX, "imap_port": 0}, False),
        ("mailbox", {**MAILBOX, "imap_password": ""}, False),
        ("mailbox", {**MAILBOX, "email": "no-at"}, False),
        ("mailbox", {**MAILBOX, "email": "a" * 250 + "@b.test"}, False),
        ("mailbox", {**MAILBOX, "imap_host": "bad host"}, False),
        ("claude_subscription", SUBSCRIPTION, True),
        ("claude_subscription", {"token": "t", "email": "a@b.test", "kind": "login_token"}, True),
        (
            "claude_subscription",
            {**SUBSCRIPTION, "kind": "login_token", "expires_at": "2026-01-01T00:00:00.000Z"},
            True,
        ),
        ("claude_subscription", {**SUBSCRIPTION, "expires_at": "x"}, False),
        ("claude_subscription", {**SUBSCRIPTION, "kind": "other"}, False),
        ("claude_subscription", {**SUBSCRIPTION, "email": "x"}, False),
        ("claude_subscription", {"token": "t", "kind": "setup_token"}, False),
    ],
)
def test_bodies(kind: str, body: object, valid: bool) -> None:
    assert credkey.valid_body(kind, body) is valid


def test_rotation_owed() -> None:
    now = datetime.now(UTC)
    assert credkey.rotation_owed({}, None, now) is False
    assert credkey.rotation_owed({"d": now}, None, now) is True
    assert credkey.rotation_owed({"d": now - timedelta(days=1)}, now + timedelta(hours=1), now) is True
    assert credkey.rotation_owed({"d": now - timedelta(days=1)}, now, now) is False
    assert credkey.rotation_owed({"d": now - timedelta(seconds=10)}, now, now) is True


def test_force_rotates_and_reencrypts(tool: Tool, hub: HubFake, account: User) -> None:
    seal(hub, account, "anthropic_api_key", API_KEY)
    seal(hub, account, "mailbox", MAILBOX)
    assert tool.run("credentials", "rotate")[1] == {"ok": True, "status": "not_owed", "lost_devices": []}
    code, rotated = tool.run("credentials", "rotate", "--force")
    assert code == 0, rotated
    assert (rotated["status"], rotated["epoch"], rotated["records"], rotated["recipients"]) == ("rotated", 2, 2, 1)
    assert account.cred_epoch == 2
    key = key_of(hub, account, 2)
    for kind, body in (("anthropic_api_key", API_KEY), ("mailbox", MAILBOX)):
        row = hub.credentials[(account.id, kind)]
        assert (row["version"], row["record"]["header"]["epoch"]) == (2, 2)
        assert parse_json(open_credential(key, row["record"], account.identity.sig_public())) == body
    assert "k" * 20 not in json.dumps(rotated)
    # the tool now writes under the new key
    assert tool.run("credentials", "set-api-key", stdin="sk-new-key")[1]["stored"] is True
    assert hub.credentials[(account.id, "anthropic_api_key")]["record"]["header"]["epoch"] == 2


def test_device_records_are_reencrypted_or_dropped(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, _ = assign(tool, hub, wg)
    signer = hub.platform_devices[device]
    seal(hub, account, "github", GITHUB, author=device, signer=signer)
    rotated = tool.run("credentials", "rotate", "--force")[1]
    assert (rotated["records"], rotated["dropped"], rotated["recipients"]) == (1, [], 2)
    row = hub.credentials[(account.id, "github")]["record"]
    assert (row["header"]["author"], row["header"]["epoch"]) == (account.id, 2)
    assert {w["recipient_id"] for w in hub.wraps if w["scope"] == "cred" and w["epoch"] == 2} == {account.id, device}
    # a record the device wrote under CK_2 before it lost access is deleted, not re-signed
    seal(hub, account, "mailbox", MAILBOX, author=device, signer=signer, epoch=2, key=key_of(hub, account, 2))
    unassigned = tool.run("device", "unassign", "--wg", wg, "--device", device)[1]
    credentials = unassigned["credentials"]
    assert (credentials["status"], credentials["dropped"], credentials["records"]) == ("rotated", ["mailbox"], 1)
    assert (account.id, "mailbox") not in hub.credentials
    assert not any(w["recipient_id"] == device and w["epoch"] == 3 for w in hub.wraps)


def test_unreadable_records_block_the_rotation(tool: Tool, hub: HubFake, account: User) -> None:
    seal(hub, account, "anthropic_api_key", {"key": "has space"})
    code, blocked = tool.run("credentials", "rotate", "--force")
    assert (code, blocked["error"], blocked["kinds"]) == (1, "credentials_blocked", ["anthropic_api_key"])
    seal(hub, account, "anthropic_api_key", API_KEY, epoch=5, key=b"k" * 32)
    seal(hub, account, "mailbox", MAILBOX, author=uuid7(), signer=Identity.generate())
    hub.credentials[(account.id, "github")] = {
        "record": {"header": {"author": account.id, "owner": uuid7(), "kind": "github", "epoch": 1}},
        "version": 1,
    }
    seal(hub, account, "claude_subscription", SUBSCRIPTION)
    blocked = tool.run("credentials", "rotate", "--force")[1]
    assert blocked["kinds"] == ["github", "anthropic_api_key", "mailbox"]
    assert account.cred_epoch == 1
    assert len([key for key in hub.credentials if key[0] == account.id]) == 4


def test_holder_losses(tool: Tool, hub: HubFake, account: User) -> None:
    stranger = uuid7()
    seal(hub, account, "mailbox", MAILBOX, author=stranger, signer=Identity.generate())
    holders(hub, 7, [stranger])  # holders of another epoch are ignored
    assert tool.run("credentials", "rotate")[1]["status"] == "not_owed"
    hub.restore("r_me")
    holders(hub, 1, [stranger, account.id])
    rotated = tool.run("credentials", "rotate")[1]
    assert (rotated["status"], rotated["lost_devices"], rotated["dropped"]) == ("rotated", [stranger], ["mailbox"])


def test_epoch_start_unknown_or_in_the_future_fails_closed(tool: Tool, hub: HubFake, account: User) -> None:
    own = next(w for w in hub.wraps if w["scope"] == "cred")
    hub.wraps.append(dict(own))  # a second self-wrap of the same epoch
    hub.wraps.append({**own, "epoch": 3, "sig": own["sig"][::-1]})  # does not verify: not a key
    holders(hub, 1, [uuid7()])
    original = hub.r_wraps

    def created(at: str) -> None:
        hub.override(
            "r_wraps", lambda r, b: (200, {"wraps": [{**w, "created_at": at} for w in original(r, b)[1]["wraps"]]})
        )

    created("garbage")
    assert tool.run("credentials", "rotate")[1]["status"] == "rotated"
    created((datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    hub.restore("r_me")
    holders(hub, 2, [uuid7()])
    assert tool.run("credentials", "rotate")[1]["epoch"] == 3


def test_leaving_rotates_the_leavers_key(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    grant_platform_access(hub, org)
    wg = wg_create(tool, org)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:read")
    device, _ = assign(tool, hub, wg)
    left = bob_tool.run("org", "leave", "--org", org)[1]
    credentials = left["credentials"]
    assert (credentials["status"], credentials["lost_devices"], credentials["recipients"]) == ("rotated", [device], 1)
    assert bob.cred_epoch == 2


def test_without_a_credential_key(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(cred=False)
    pair_user(tool, hub, user)
    assert tool.run("credentials", "rotate")[1]["status"] == "not_owed"
    assert tool.run("credentials", "rotate", "--force")[1]["reason"] == "cred_key_missing"


def test_version_conflict_is_retried_once(tool: Tool, hub: HubFake, account: User) -> None:
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "version_conflict"}) if len(calls) == 1 else None

    hub.hooks[("POST", "/v1/me/credentials/rotate")] = conflict
    assert tool.run("credentials", "rotate", "--force")[1]["epoch"] == 2
    hub.hooks[("POST", "/v1/me/credentials/rotate")] = lambda b, r: (409, {"error": "version_conflict"})
    assert tool.run("credentials", "rotate", "--force")[1]["hub_error"] == "version_conflict"


def test_hub_answers(tool: Tool, hub: HubFake, account: User) -> None:
    hub.hooks[("GET", "/v1/me/credentials")] = lambda b, r: (200, {"credentials": [{"kind": "other"}]})
    assert tool.run("credentials", "rotate", "--force")[1]["reason"] == "response_invalid"
    # listed, but gone when fetched (deleted meanwhile): skipped
    hub.hooks[("GET", "/v1/me/credentials")] = lambda b, r: (200, {"credentials": [{"kind": "github"}]})
    assert tool.run("credentials", "rotate", "--force")[1]["records"] == 0
    hub.hooks[("GET", "/v1/me/credentials/github")] = lambda b, r: (200, {"record": None})
    assert tool.run("credentials", "rotate", "--force")[1]["records"] == 0
    hub.hooks[("GET", "/v1/me/credentials/github")] = lambda b, r: (503, {"error": "unavailable"})
    assert tool.run("credentials", "rotate", "--force")[1]["hub_error"] == "unavailable"
    hub.hooks.clear()
    original = hub.r_me
    for found in (None, {"epoch": "1", "devices": []}, {"epoch": 1, "devices": [5]}):
        hub.override("r_me", lambda r, b, found=found: (200, {**original(r, b)[1], "cred_holders": found}))
        assert tool.run("credentials", "rotate")[1]["status"] == "not_owed"


def test_dropping_tolerates_a_missing_record_and_reports_other_failures(
    tool: Tool, hub: HubFake, account: User
) -> None:
    stranger = uuid7()
    holders(hub, 1, [stranger])
    seal(hub, account, "mailbox", MAILBOX, author=stranger, signer=Identity.generate())
    hub.hooks[("DELETE", "/v1/me/credentials/mailbox")] = lambda b, r: (503, {"error": "unavailable"})
    assert tool.run("credentials", "rotate")[1]["hub_error"] == "unavailable"
    assert account.cred_epoch == 1
    del hub.hooks[("DELETE", "/v1/me/credentials/mailbox")]
    original = hub.r_credential_get

    def gone_after_reading(request: Any, body: Any, kind: str) -> tuple[int, Any]:
        answer = original(request, body, kind)
        hub.credentials.pop((account.id, kind), None)
        return answer

    hub.override("r_credential_get", gone_after_reading)
    rotated = tool.run("credentials", "rotate")[1]
    assert (rotated["status"], rotated["dropped"]) == ("rotated", ["mailbox"])


def test_orgs_that_do_not_verify_entitle_nobody(tool: Tool, hub: HubFake, account: User) -> None:
    target = private_org(hub, account)
    original = hub.r_log

    def forged(request: Any, body: Any, org: str) -> tuple[int, Any]:
        status, answer = original(request, body, org)
        return status, {**answer, "head": {"seq": 99, "hash": "x"}} if org == target else answer

    hub.override("r_log", forged)
    assert tool.run("credentials", "rotate", "--force")[1]["recipients"] == 1
    hub.hooks[("GET", f"/v1/orgs/{target}/log")] = lambda b, r: (401, {"error": "key_unknown"})
    assert tool.run("credentials", "rotate", "--force")[1]["error"] == "not_paired"


def test_hub_refuses_a_wrong_rotation(tool: Tool, hub: HubFake, account: User) -> None:
    """The fake's own rules (as the hub's), through the generic call with hand-made bodies."""
    seal(hub, account, "anthropic_api_key", API_KEY)
    mine = {"scope": "cred", "scope_id": account.id, "epoch": 2}
    for body, code in (
        ({"epoch": 2, "wraps": [], "records": []}, "request_invalid"),
        ({"epoch": 5, "wraps": [mine], "records": []}, "version_conflict"),
        (
            {"epoch": 2, "wraps": [{**mine, "scope": "org", "recipient_id": account.id}], "records": []},
            "request_invalid",
        ),
        ({"epoch": 2, "wraps": [{**mine, "recipient_id": "x"}], "records": []}, "request_invalid"),
        ({"epoch": 2, "wraps": [{**mine, "recipient_id": account.id}], "records": []}, "version_conflict"),
    ):
        answer = tool.run("api", "POST", "/v1/me/credentials/rotate", "--body", json.dumps(body))[1]
        assert answer["hub_error"] == code, body


def test_healed_and_decertified_losses(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = hub.orgs[org].log.state.default_wg
    device, seq = assign(tool, hub, wg)
    assert tool.run("device", "unassign", "--wg", wg, "--device", device)[1]["credentials"]["epoch"] == 2
    assign(tool, hub, wg, device, seq)
    # the device reads again: the earlier loss is healed
    assert tool.run("credentials", "rotate")[1]["status"] == "not_owed"
    decertify(hub, device)
    rotated = tool.run("credentials", "rotate")[1]
    assert (rotated["status"], rotated["lost_devices"], rotated["recipients"]) == ("rotated", [device], 1)
