"""Pairing from another computer (ADR-062, 11-agent-access.md section 5.4): `pair --remote`, `register --remote` and
`pair --wait`, against the hub fake whose browser side seals the handover to the agent."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from agent_platform import remote
from agent_platform.errors import NEXT, NEXT_FOR_REASON, PlatformError
from agent_platform.pairing import Pairing
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.encoding import b64u_decode, b64u_encode

from .conftest import Tool, pair_user
from .hubfake import HubFake, Json, Request, User
from .test_register import platform_invite

SECRET = bytes(range(32))


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "POLL_S", 0.05)


def start(tool: Tool, hub: HubFake, *extra: str) -> dict[str, Any]:
    code, document = tool.run(
        "pair", "--hub", hub.origin, "--remote", "--name", "Claude Code", "--host", "srv 1", *extra
    )
    assert code == 0, document
    return document


def pending(tool: Tool) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tool.home / "pending.json").read_text())
    return data


def wait(tool: Tool, *extra: str) -> tuple[int, dict[str, Any]]:
    return tool.run("pair", "--wait", "--timeout", "0", *extra)


def no_secrets(document: object, *secrets_: str) -> None:
    text = json.dumps(document)
    for secret in secrets_:
        assert secret not in text


def private_keys(tool: Tool, pairing_id: str) -> str:
    item = tool.keychain / f"agent-platform__{pairing_id}"
    return item.read_text() if item.exists() else (tool.home / "pending.key").read_text().strip()


def test_pair_remote_returns_link_and_code_at_once(tool: Tool, hub: HubFake) -> None:
    result = start(tool, hub)
    assert set(result) == {"ok", "state", "hub", "connect_link", "code", "expires_at", "next"}
    assert result["state"] == "waiting"
    assert result["next"] == remote.NEXT_WAITING
    origin, fragment = result["connect_link"].split("#")
    assert origin == f"{hub.origin}/connect"
    params = parse_qsl(fragment)
    assert [key for key, _ in params] == ["p", "f", "m", "a", "h"]
    values = dict(params)
    assert values["m"] == "remote"
    assert values["h"] == "srv 1"
    row = hub.agents[values["p"]]
    assert row.remote
    assert values["f"] == agent_crypto.fingerprint(row.kem, row.sig)
    assert row.nonce is not None
    code = agent_crypto.agent_code(values["p"], values["f"], row.nonce, hub.origin)
    assert result["code"] == f"{code[:5]}-{code[5:]}"
    assert tool.opened() == []
    assert not (tool.home / "state.json").exists()
    saved = pending(tool)
    assert stat.S_IMODE((tool.home / "pending.json").stat().st_mode) == 0o600
    assert saved["pairing_id"] == values["p"]
    assert saved["mode"] == "remote"
    assert saved["kind"] == "pair"
    assert saved["key_store"] == "keychain"
    assert saved["nonce"] == b64u_encode(row.nonce)
    assert saved["platform_root_key"] == hub.root_text
    no_secrets(result, private_keys(tool, values["p"]))


def test_a_second_pending_pairing_is_refused_unless_replaced(tool: Tool, hub: HubFake) -> None:
    first = pending(tool) if start(tool, hub) else {}
    code, document = tool.run("pair", "--hub", hub.origin, "--remote")
    assert (code, document["error"], document["next"]) == (1, "pairing_pending", NEXT["pairing_pending"])
    start(tool, hub, "--replace-pending")
    assert pending(tool)["pairing_id"] != first["pairing_id"]
    assert not (tool.keychain / f"agent-platform__{first['pairing_id']}").exists()


def test_wait_without_pending_pairing(tool: Tool, hub: HubFake) -> None:
    assert wait(tool)[1]["error"] == "no_pending_pairing"
    tool.home.mkdir(mode=0o700, parents=True)
    assert wait(tool)[1]["error"] == "no_pending_pairing"


def test_wait_reports_waiting_then_confirmed_then_connects(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(display_name="Grace")
    link = start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    code, document = wait(tool)
    assert (code, document["state"], document["next"]) == (0, "waiting", remote.NEXT_WAITING)
    # a return code given while the pairing is open uses no attempt
    assert wait(tool, "--return-code", "AAAA-AAAA")[1]["state"] == "waiting"
    back = hub.confirm_remote(pairing_id, user, SECRET)
    code, document = wait(tool)
    assert (code, document["state"], document["next"]) == (0, "confirmed", remote.NEXT_CONFIRMED)
    keys = private_keys(tool, pairing_id)
    handover = hub.agents[pairing_id].handover_ct
    assert handover is not None
    code, document = wait(tool, "--return-code", back.lower().replace("-", " "))
    assert code == 0, document
    assert document["state"] == "connected"
    assert (document["agent_id"], document["user_id"], document["display_name"]) == (pairing_id, user.id, "Grace")
    assert document["kind"] == "pair"
    assert document["hub"] == hub.origin
    assert hub.agents[pairing_id].state == "active"
    assert hub.agents[pairing_id].handover_ct is None
    assert not (tool.home / "pending.json").exists()
    state = json.loads((tool.home / "state.json").read_text())
    assert (state["agent_id"], state["key_store"], state["secret"]) == (pairing_id, "keychain", b64u_encode(SECRET))
    assert (tool.keychain / f"agent-platform__{pairing_id}").exists()
    claim = b64u_encode(agent_crypto.claim(SECRET, pairing_id, user.id))
    no_secrets([link, document], keys, handover, b64u_encode(SECRET), claim)
    assert b64u_encode(SECRET).encode() not in b"".join(hub.bodies)
    assert tool.run("status")[0] == 0


def test_wait_polls_until_the_confirmation(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    timer = threading.Timer(0.3, lambda: hub.confirm_remote(pairing_id, user))
    timer.start()
    code, document = tool.run("pair", "--wait", "--timeout", "10")
    timer.join()
    assert (code, document["state"]) == (0, "confirmed")


def test_success_replaces_a_paired_state_and_keeps_files_in_file_mode(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    (tool.root / "bin" / "security.fail").write_text("")
    first = pair_user(tool, hub, user)
    start(tool, hub)
    assert pending(tool)["key_store"] == "file"
    assert stat.S_IMODE((tool.home / "pending.key").stat().st_mode) == 0o600
    # the paired state stays in place while the pairing is pending
    assert json.loads((tool.home / "state.json").read_text())["agent_id"] == first["agent_id"]
    assert tool.run("status")[0] == 0
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user)
    code, document = wait(tool, "--return-code", back)
    assert code == 0, document
    assert not (tool.home / "pending.key").exists()
    assert json.loads((tool.home / "state.json").read_text())["agent_id"] == pairing_id
    (tool.root / "bin" / "security.fail").unlink()
    assert tool.run("status")[0] == 0


def test_keychain_lost_at_the_claim_keeps_the_key_in_the_file(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user)
    original = hub.r_claim

    def claim(request: Request, body: Json) -> tuple[int, Any]:
        (tool.root / "bin" / "security.fail").write_text("")
        return original(request, body)

    hub.override("r_claim", claim)
    assert wait(tool, "--return-code", back)[0] == 0
    assert json.loads((tool.home / "state.json").read_text())["key_store"] == "file"
    # the pending keychain item is deleted (the stand-in refuses while it fails, as a locked keychain would)
    assert f"delete-generic-password -s agent-platform -a {pairing_id}" in tool.security_log.read_text()


def failed(tool: Tool, document: dict[str, Any], pairing_id: str, code: str, reason: str | None = None) -> None:
    assert (document["ok"], document["error"], document["next"]) == (False, code, NEXT[code])
    assert document.get("reason") == reason
    assert not (tool.home / "pending.json").exists()
    assert not (tool.home / "pending.key").exists()
    assert not (tool.keychain / f"agent-platform__{pairing_id}").exists()
    assert not (tool.home / "state.json").exists()


def test_a_wrong_return_code_ends_the_pairing_before_the_claim(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    wrong = "0000-0000" if back != "0000-0000" else "1111-1111"
    code, document = wait(tool, "--return-code", wrong)
    assert code == 1
    failed(tool, document, pairing_id, "pairing_failed", "return_code_mismatch")
    assert hub.agents[pairing_id].state == "confirmed"
    assert not any(request.path == "/v1/agent/claim" for request in hub.requests)
    no_secrets(document, b64u_encode(SECRET), back)


def test_a_malformed_return_code_keeps_the_pairing(tool: Tool, hub: HubFake) -> None:
    start(tool, hub)
    assert wait(tool, "--return-code", "UUUU-UUUU")[1]["error"] == "invalid_argument"
    assert (tool.home / "pending.json").exists()


def forged(hub: HubFake, user: User, pairing_id: str, sealed: str) -> str:
    back = hub.confirm_remote(pairing_id, user, SECRET)
    hub.agents[pairing_id].handover_ct = sealed
    return back


def test_a_forged_handover_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    # the hub seals an own secret to the agent's key: it opens, but the return code of the page does not match
    row = hub.agents[pairing_id]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    row.handover_ct = agent_crypto.seal_handover(row.kem, pairing_id, user.id, bytes(32), hub.root_text)
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_failed", "return_code_mismatch")


@pytest.mark.parametrize("variant", ["garbage", "other_agent", "no_user"])
def test_a_handover_that_does_not_open_fails(tool: Tool, hub: HubFake, variant: str) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    if variant == "garbage":
        hub.agents[pairing_id].handover_ct = b64u_encode(bytes(80))
    else:
        answer: Json = {"agent_id": pairing_id, "user_id": user.id, "handover_ct": hub.agents[pairing_id].handover_ct}
        answer.update({"agent_id": user.id} if variant == "other_agent" else {"user_id": None})
        hub.hooks[("GET", "/v1/agent/handover")] = lambda body, request: (200, answer)
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_failed", "handover_invalid")


def test_a_claim_the_hub_refuses_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    hub.agents[pairing_id].claim_hash = bytes(32)
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_failed", "claim_invalid")


def test_an_identity_that_does_not_match_fails(tool: Tool, hub: HubFake) -> None:
    user, other = hub.add_user(), hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    hub.agents[pairing_id].bundle_ct = agent_crypto.seal_identity(
        hub.agents[pairing_id].kem, SECRET, pairing_id, user.id, other.identity
    )
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_failed", "identity_mismatch")


def test_a_platform_root_that_differs_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    row = hub.agents[pairing_id]
    back = hub.confirm_remote(pairing_id, user, SECRET, root=b64u_encode(bytes(32)))
    assert row.handover_ct is not None
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_failed", "platform_root_mismatch")


def test_expiry_before_the_confirmation_is_a_timeout(tool: Tool, hub: HubFake) -> None:
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    hub.agents[pairing_id].expires = 0.0
    _code, document = wait(tool)
    failed(tool, document, pairing_id, "pairing_timeout")
    assert "--remote" in document["next"]


@pytest.mark.parametrize("route", [("GET", "/v1/agent/handover"), ("POST", "/v1/agent/claim")])
def test_expiry_before_the_claim_is_a_timeout(tool: Tool, hub: HubFake, route: tuple[str, str]) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    hub.hooks[route] = lambda body, request: (404, {"error": "pairing_unknown"})
    _code, document = wait(tool, "--return-code", back)
    failed(tool, document, pairing_id, "pairing_timeout")


def test_a_claim_whose_answer_was_lost_is_not_a_timeout(tool: Tool, hub: HubFake) -> None:
    """The claim succeeded at the hub but its answer was lost: the retry finds the pairing gone (`pairing_unknown`),
    which is `pairing_failed` / `claim_lost` with the step to disconnect that agent, not `pairing_timeout`."""
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    original = hub.r_claim

    def lost(request: Request, body: Json) -> tuple[int, Any]:
        original(request, body)
        return 503, {"error": "unavailable"}

    hub.override("r_claim", lost)
    code, document = wait(tool, "--return-code", back)
    assert (code, document["error"], document["hub_error"]) == (1, "hub_refused", "unavailable")
    assert pending(tool)["claim_sent"] is True
    assert hub.agents[pairing_id].state == "active"
    hub.restore("r_claim")
    _code, document = wait(tool, "--return-code", back)
    assert (document["error"], document["reason"]) == ("pairing_failed", "claim_lost")
    assert document["next"] == NEXT_FOR_REASON[("pairing_failed", "claim_lost")]
    assert "disconnect" in document["next"]
    assert not (tool.home / "pending.json").exists()
    assert not (tool.home / "state.json").exists()


@pytest.mark.parametrize(
    "route", [("GET", "/v1/agent/handover"), ("POST", "/v1/agent/claim"), ("GET", "/v1/agent-pairings")]
)
def test_a_transient_refusal_keeps_the_pairing(tool: Tool, hub: HubFake, route: tuple[str, str]) -> None:
    user = hub.add_user()
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    back = hub.confirm_remote(pairing_id, user, SECRET)
    path = route[1] if route[1] != "/v1/agent-pairings" else f"/v1/agent-pairings/{pairing_id}"
    hub.hooks[(route[0], path)] = lambda body, request: (503, {"error": "unavailable"})
    code, document = wait(tool, "--return-code", back)
    assert (code, document["error"]) == (1, "hub_refused")
    assert (tool.home / "pending.json").exists()
    del hub.hooks[(route[0], path)]
    assert wait(tool, "--return-code", back)[1]["state"] == "connected"


@pytest.mark.parametrize("answer", [{"pairing_id": "x", "state": "open"}, {"state": "active"}])
def test_a_lookup_answer_that_does_not_fit_is_refused(tool: Tool, hub: HubFake, answer: Json) -> None:
    start(tool, hub)
    pairing_id = pending(tool)["pairing_id"]
    hub.hooks[("GET", f"/v1/agent-pairings/{pairing_id}")] = lambda body, request: (
        200,
        {"pairing_id": pairing_id, **answer},
    )
    _code, document = wait(tool)
    assert (document["error"], document["reason"]) == ("hub_refused", "response_invalid")
    assert (tool.home / "pending.json").exists()


def test_a_second_wait_is_busy(tool: Tool, hub: HubFake) -> None:
    start(tool, hub)
    descriptor = os.open(tool.home / "pending.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        assert wait(tool)[1]["error"] == "pairing_busy"
        _code, document = tool.run("pair", "--hub", hub.origin, "--remote", "--replace-pending")
        assert document["error"] == "pairing_busy"
    finally:
        os.close(descriptor)
    assert wait(tool)[1]["state"] == "waiting"


def test_an_old_hub_is_reported(tool: Tool, hub: HubFake) -> None:
    hub.old_hub = True
    code, document = tool.run("pair", "--hub", hub.origin, "--remote")
    assert (code, document["error"], document["next"]) == (1, "hub_too_old", NEXT["hub_too_old"])
    assert not (tool.home / "pending.json").exists()
    hub.old_hub = False
    for answer in ({}, {"remote": True}, {"remote": False, "nonce": b64u_encode(bytes(16))}):

        def loopback(body: Json | None, request: Request, answer: Json = answer) -> tuple[int, Any]:
            assert body is not None
            return 200, {"pairing_id": body["pairing_id"], "expires_at": "2026-11-24T12:00:00.000Z", **answer}

        hub.hooks[("POST", "/v1/agent-pairings")] = loopback
        assert tool.run("pair", "--hub", hub.origin, "--remote")[1]["error"] == "hub_too_old"
    assert not (tool.home / "pending.json").exists()


def test_other_refusals_of_the_creation_stay_hub_refused(tool: Tool, hub: HubFake) -> None:
    hub.hooks[("POST", "/v1/agent-pairings")] = lambda body, request: (429, {"error": "rate_limited"})
    _code, document = tool.run("pair", "--hub", hub.origin, "--remote")
    assert (document["error"], document["hub_error"]) == ("hub_refused", "rate_limited")


def test_a_hub_without_platform_root_key(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    original = hub.r_config

    def config(request: Request, body: Any) -> tuple[int, Any]:
        answer = dict(original(request, body)[1])
        del answer["platform_root_key"]
        return 200, answer

    hub.override("r_config", config)
    start(tool, hub)
    assert pending(tool)["platform_root_key"] is None
    pairing_id = pending(tool)["pairing_id"]
    hub.restore("r_config")
    back = hub.confirm_remote(pairing_id, user)
    assert wait(tool, "--return-code", back)[1]["state"] == "connected"


@pytest.mark.parametrize(
    "damage",
    ["json", "version", "fields", "pairing_id", "nonce", "keychain"],
)
def test_a_damaged_pending_pairing(tool: Tool, hub: HubFake, damage: str) -> None:
    start(tool, hub)
    path = tool.home / "pending.json"
    data = pending(tool)
    if damage == "json":
        text = "{"
    else:
        changes: dict[str, Any] = {
            "version": {"v": 2},
            "fields": {"label": None},
            "pairing_id": {"pairing_id": "not-a-uuid"},
            "nonce": {"nonce": "AAAA"},
            "keychain": {},
        }
        text = json.dumps({**data, **changes[damage]})
        if damage == "keychain":
            for item in tool.keychain.iterdir():
                item.unlink()
    path.write_text(text)
    assert wait(tool)[1]["error"] == "state_damaged"


def test_register_remote_returns_the_invitation_with_the_pairing(tool: Tool, hub: HubFake) -> None:
    link, code_text = platform_invite(hub)
    code, document = tool.run("register", "--remote", "--display-name", " Ada Lovelace ", "--host", "srv", stdin=link)
    assert code == 0, document
    assert document["secret_fields"] == ["invite_link"]
    assert (document["state"], document["scope"], document["display_name"]) == ("waiting", "platform", "Ada Lovelace")
    assert document["code"] == document["code"].upper()
    assert len(document["code"]) == 11
    invite = document["invite_link"]
    base, fragment = invite.split("#")
    assert base == f"{hub.origin}/invite"
    first, rest = fragment.split("&", 1)
    assert first == code_text
    params = parse_qsl(rest)
    assert [key for key, _ in params] == ["p", "f", "m", "a", "h", "u"]
    assert dict(params)["m"] == "remote"
    assert unquote(dict(params)["u"]) == "Ada Lovelace"
    assert tool.opened() == []
    assert not list(tool.home.glob("invite-*.html"))
    # the invitation code appears only in the field marked secret
    outside = {key: value for key, value in document.items() if key != "invite_link"}
    no_secrets(outside, code_text, code_text.replace("-", ""))
    saved = pending(tool)
    assert (saved["kind"], saved["display_name"], saved["scope"]) == ("register", "Ada Lovelace", "platform")
    no_secrets(saved, code_text)
    user = hub.add_user(private_org=False)
    back = hub.confirm_remote(saved["pairing_id"], user)
    code, done = wait(tool, "--return-code", back)
    assert code == 0, done
    assert (done["state"], done["kind"], done["display_name"], done["scope"]) == (
        "connected",
        "register",
        "Ada Lovelace",
        "platform",
    )
    no_secrets(done, code_text)


def test_register_remote_refusals(tool: Tool, hub: HubFake) -> None:
    link, _ = platform_invite(hub, registered=True)
    _code, document = tool.run("register", "--remote", "--display-name", "Ada", stdin=link)
    assert document["hub_error"] == "invite_registered"
    link, _ = platform_invite(hub)
    assert tool.run("register", "--remote", "--display-name", "  ", stdin=link)[1]["error"] == "invalid_argument"
    assert (
        tool.run("register", "--replace-pending", "--display-name", "A", stdin=link)[1]["error"] == "invalid_argument"
    )
    hub.hooks[("POST", "/v1/invites/lookup")] = lambda body, request: (200, {"registered": False, "scope": 5})
    assert tool.run("register", "--remote", "--display-name", "Ada", stdin=link)[1]["scope"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ["pair", "--wait", "--hub", "http://127.0.0.1:1"],
        ["pair", "--wait", "--remote"],
        ["pair", "--wait", "--replace-pending"],
        ["pair", "--wait", "--timeout", "601"],
        ["pair", "--wait", "--timeout", "-1"],
        ["pair"],
        ["pair", "--hub", "http://127.0.0.1:1", "--return-code", "AAAA-AAAA"],
        ["pair", "--hub", "http://127.0.0.1:1", "--timeout", "5"],
        ["pair", "--hub", "http://127.0.0.1:1", "--remote", "--no-browser"],
        ["pair", "--hub", "http://127.0.0.1:1", "--replace-pending"],
    ],
)
def test_argument_errors(tool: Tool, argv: list[str]) -> None:
    assert tool.run(*argv)[1]["error"] == "invalid_argument"


def test_the_pending_files_are_private(tool: Tool, hub: HubFake) -> None:
    (tool.root / "bin" / "security.fail").write_text("")
    start(tool, hub)
    for name in ("pending.json", "pending.key", "pending.lock"):
        assert stat.S_IMODE((tool.home / name).stat().st_mode) == 0o600, name
    pairing_id = pending(tool)["pairing_id"]
    assert b64u_decode(private_keys(tool, pairing_id), 64)
    started = time.monotonic()
    assert wait(tool)[1]["state"] == "waiting"
    assert time.monotonic() - started < 5
    assert urlsplit(pending(tool)["hub"]).netloc == urlsplit(hub.origin).netloc
    assert Path(tool.home).is_dir()


def test_a_pairing_finished_meanwhile_is_no_longer_pending(
    tool: Tool, hub: HubFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    start(tool, hub)
    answers = iter([pending(tool), None])
    monkeypatch.setattr(Home, "load_pending", lambda self: next(answers))
    assert wait(tool)[1]["error"] == "no_pending_pairing"


def test_restore_refuses_a_pairing_id_that_is_no_uuid7(tool: Tool, hub: HubFake) -> None:
    start(tool, hub)
    home = Home.from_env(tool.env)
    data = {**pending(tool), "pairing_id": "not-a-uuid"}
    with pytest.raises(PlatformError) as refused:
        Pairing.restore(home, data, agent_crypto.new_agent())
    assert refused.value.code == "state_damaged"
