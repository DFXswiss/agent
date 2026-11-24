"""`passkey list|remove|add`, `recovery new`, `agents list|revoke`, `platform invite|list|revoke` and `security`
against the hub double."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from agent_platform import security
from agent_runner.crypto.bundle import RECOVERY_SLOT, open_bundle, seal_bundle
from agent_runner.crypto.encoding import P, b64u_decode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import hkdf, kek_from_recovery_code
from agent_runner.crypto.keys import ed25519_seed
from agent_runner.crypto.recovery import generate_recovery_code, parse_recovery_code, recovery_id

from .conftest import Tool, wait_for
from .fakes.account_views import grant_inviter, views
from .hubfake import AgentRow, HubFake, User, now_ms_text


def passkey(credential: str) -> dict[str, Any]:
    return {"credential_id": credential, "created_at": now_ms_text(), "last_used_at": None, "prf_seen": True}


def test_passkeys(tool: Tool, hub: HubFake, account: User) -> None:
    views(hub).passkeys[account.id] = [passkey("AAAA"), passkey("BBBB")]
    code, listed = tool.run("passkey", "list")
    assert code == 0, listed
    assert ([row["credential_id"] for row in listed["passkeys"]], listed["recovery_code"]) == (["AAAA", "BBBB"], False)
    assert tool.run("passkey", "remove", "--id", "BBBB")[1]["removed"] == "BBBB"
    assert tool.run("passkey", "remove", "--id", "AAAA")[1]["hub_error"] == "last_passkey"
    assert tool.run("passkey", "remove", "--id", "CCCC")[1]["hub_error"] == "not_found"
    for bad in ("a/b", ""):
        assert tool.run("passkey", "remove", "--id", bad)[1]["error"] == "invalid_argument"
    original = hub.r_me
    hub.override("r_me", lambda request, body: (200, {**original(request, body)[1], "passkeys": None}))
    assert tool.run("passkey", "list")[1]["reason"] == "response_invalid"
    hub.override("r_me", original)
    seen = len(tool.opened())
    code, added = tool.run("passkey", "add")
    assert (code, added["opened"]) == (0, f"{hub.origin}/settings#settings-passkeys")
    assert wait_for(lambda: tool.opened()[seen:]) == [added["opened"]]


def test_recovery_code(tool: Tool, hub: HubFake, account: User) -> None:
    # the unset slot of a registration without code: no recovery code
    nonce = hkdf(ed25519_seed(account.identity.sig), P + b"recovery-unset\x00" + account.id.encode())[:12]
    unset = seal_bundle(
        kek_from_recovery_code(generate_recovery_code()), account.id, RECOVERY_SLOT, account.identity, nonce
    )
    views(hub).bundles[account.id] = {"bundle_ct": unset, "recovery_id": "x"}
    assert tool.run("passkey", "list")[1]["recovery_code"] is False
    code, made = tool.run("recovery", "new")
    assert code == 0, made
    assert made["secret_fields"] == ["recovery_code"]
    raw = parse_recovery_code(made["recovery_code"])
    stored = views(hub).bundles[account.id]
    assert stored["recovery_id"] == recovery_id(raw)
    opened = open_bundle(kek_from_recovery_code(raw), account.id, RECOVERY_SLOT, stored["bundle_ct"])
    assert opened.public() == account.identity.public()
    assert all(made["recovery_code"].encode() not in body for body in hub.bodies)
    assert tool.run("passkey", "list")[1]["recovery_code"] is True
    # a lost answer: the repeat is refused, but the stored slot is ours
    put = ("PUT", "/v1/me/bundles/recovery")

    def stored_then_refused(body: Any, request: Any) -> tuple[int, Any]:
        views(hub).bundles[account.id] = {"bundle_ct": body["bundle_ct"], "recovery_id": body["new_recovery_id"]}
        return 403, {"error": "forbidden"}

    hub.hooks[put] = stored_then_refused
    assert tool.run("recovery", "new")[1]["ok"] is True
    hub.hooks[put] = lambda body, request: (403, {"error": "forbidden"})
    assert tool.run("recovery", "new")[1]["hub_error"] == "forbidden"
    hub.hooks[put] = lambda body, request: (409, {"error": "id_conflict"})
    assert tool.run("recovery", "new")[1]["hub_error"] == "id_conflict"
    hub.hooks[("GET", "/v1/me/bundles/recovery")] = lambda body, request: (200, {"bundle_ct": "!"})
    assert tool.run("passkey", "list")[1]["reason"] == "response_invalid"
    hub.hooks[("GET", "/v1/me/bundles/recovery")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("passkey", "list")[1]["hub_error"] == "unavailable"


def test_agents(tool: Tool, hub: HubFake, account: User) -> None:
    other = uuid7()
    hub.agents[other] = AgentRow(other, b"k" * 32, b"s" * 32, state="active", user=account.id, label_ct="AAAA")
    code, listed = tool.run("agents", "list")
    assert code == 0, listed
    rows = {row["agent_id"]: row for row in listed["agents"]}
    me = next(row for row in rows.values() if row["this"])
    assert me["label"] == {"name": "Claude Code", "host": "mac"}
    assert (rows[other]["label"], rows[other]["this"]) == (None, False)
    hub.hooks[("GET", "/v1/me/agents")] = lambda body, request: (200, {"agents": [{"agent_id": 1}, "x"]})
    assert tool.run("agents", "list")[1]["agents"] == []
    del hub.hooks[("GET", "/v1/me/agents")]
    revoked = tool.run("agents", "revoke", "--id", other)[1]
    assert (revoked["revoked"], revoked["this_agent_revoked"], hub.agents[other].state) == (1, False, "revoked")
    assert tool.run("agents", "revoke", "--id", uuid7())[1]["hub_error"] == "not_found"
    for argv in ((), ("--id", other, "--all"), ("--id", "x")):
        assert tool.run("agents", "revoke", *argv)[1]["error"] == "invalid_argument", argv
    hub.hooks[("DELETE", "/v1/me/agents")] = lambda body, request: (200, {})
    assert tool.run("agents", "revoke", "--all")[1]["revoked"] is None
    assert tool.run("status")[1]["error"] == "not_paired"


def test_revoking_all_agents_and_itself(tool: Tool, hub: HubFake, account: User) -> None:
    itself = next(row.id for row in hub.agents.values() if row.user == account.id)
    done = tool.run("agents", "revoke", "--id", itself)[1]
    assert done["this_agent_revoked"] is True
    assert not (tool.home / "state.json").exists()


def test_revoke_all(tool: Tool, hub: HubFake, account: User) -> None:
    done = tool.run("agents", "revoke", "--all")[1]
    assert (done["revoked"], done["this_agent_revoked"]) == (1, True)
    assert {row.state for row in hub.agents.values()} == {"revoked"}


def test_platform_invitations(tool: Tool, hub: HubFake, account: User) -> None:
    for argv in (("invite",), ("list",), ("revoke", "--id", "AAAA")):
        assert tool.run("platform", *argv)[1]["error"] == "not_inviter", argv
    grant_inviter(hub, account.id)
    code, invited = tool.run("platform", "invite", "--expires-hours", "720")
    assert code == 0, invited
    assert invited["invite_link"].startswith(f"{hub.origin}/invite#")
    assert invited["secret_fields"] == ["invite_link"]
    secret = invited["invite_link"].split("#", 1)[1]
    assert all(secret.encode() not in body for body in hub.bodies)
    listed = tool.run("platform", "list")[1]["invites"]
    assert [(row["invite_id"], row["state"]) for row in listed] == [(invited["invite_id"], "open")]
    assert tool.run("platform", "revoke", "--id", invited["invite_id"])[1]["revoked"] == invited["invite_id"]
    assert tool.run("platform", "list")[1]["invites"][0]["state"] == "revoked"
    assert tool.run("platform", "revoke", "--id", "AAAAAAAAAAAAAAAAAAAAAA")[1]["hub_error"] == "not_found"
    used = tool.run("platform", "invite")[1]["invite_id"]
    views(hub).platform_invites[used]["row"]["state"] = "used"
    assert tool.run("platform", "revoke", "--id", used)[1]["hub_error"] == "invite_consumed"
    for argv in (("invite", "--expires-hours", "0"), ("invite", "--expires-hours", "721"), ("revoke", "--id", "a b")):
        assert tool.run("platform", *argv)[1]["error"] == "invalid_argument", argv


def test_security_page(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprint = hashlib.sha256(b64u_decode(hub.root_text, 32)).hexdigest()
    manifest = {"version": "1.2.3", "commit": "abc", "root_key_fingerprint": fingerprint}
    hub.hooks[("GET", "/build-manifest.json")] = lambda body, request: (200, manifest)
    code, shown = tool.run("security")
    assert code == 0, shown
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    assert shown["build"] == {"hash": hashlib.sha256(raw).hexdigest(), **manifest}
    assert (shown["build_matches_root_key"], shown["root_key_fingerprint"]) == (True, fingerprint)
    assert shown["hub_key"]["id"] == "hub-test"
    answers: list[tuple[int, Any]] = [(200, {**manifest, "commit": ""}), (200, ["x"]), (404, {"error": "not_found"})]
    for answer in answers:

        def fixed(body: Any, request: Any, answer: tuple[int, Any] = answer) -> tuple[int, Any]:
            return answer

        hub.hooks[("GET", "/build-manifest.json")] = fixed
        shown = tool.run("security")[1]
        assert (shown["build"], shown["build_matches_root_key"]) == (None, False)
    assert security._manifest(b"\xff") is None
    state_file = tool.home / "state.json"
    state = json.loads(state_file.read_text())
    del state["platform_root_key"]
    state_file.write_text(json.dumps(state))
    assert tool.run("security")[1]["root_key_fingerprint"] is None

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.Client, "get", unreachable)
    assert tool.run("security")[1]["error"] == "hub_unreachable"
