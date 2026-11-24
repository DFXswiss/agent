"""An account of an agent alone against the real hub (rule E-27, KOR-62; 11-agent-access.md section 5.5): no person,
no passkey and no browser in any step. The agent registers through a platform invitation (its private organization)
and through an org invitation (it joins), works like any member, takes its account to another computer with the
recovery code, and hands it on to an agent of a remote pairing; an account with a passkey keeps the person's
approval (concept idea 34)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

import pytest

from agent_platform import remote
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_decode
from tests.platform.tool.conftest import make_tool

from .support import ROOT_KEY, RealHub, pair, person, run_ok


def platform_invitation(real: RealHub) -> str:
    """An open platform invitation with its `pk_inv` at the hub (as the operator or an inviter creates it): the link."""
    code = secrets.token_bytes(16)
    found = invite_format.invite_secrets(code)
    now = real.harness.clock()
    with real.harness.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, created_at, expires_at, pk_inv) VALUES (%s, 'platform', 'user',"
            " %s, %s, %s)",
            (found.invite_id, now, now + timedelta(hours=72), b64u_decode(found.pk_inv, 32)),
        )
    return f"{real.origin}/invite#{invite_format.format_code(code)}"


def give_passkey(real: RealHub, user_id: str) -> None:
    """The account becomes one of a person: a registered passkey."""
    now = real.harness.clock()
    with real.harness.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.passkey (credential_id, user_id, public_key, sign_count, transports, created_at,"
            " last_used_at) VALUES (%s, %s, %s, 0, '{internal}', %s, %s)",
            (secrets.token_bytes(16), user_id, b"\x04" + bytes(64), now, now),
        )


def register_alone(real: RealHub, name: str, link: str) -> tuple[Any, dict[str, Any]]:
    tool = make_tool(real.root / name.lower())
    args = ("register", "--own-account", "--display-name", name, "--name", "Bot", "--host", "box")
    return tool, run_ok(tool, *args, stdin=link)


def remote_pairing(real: RealHub, name: str) -> tuple[Any, dict[str, Any]]:
    tool = make_tool(real.root / name.lower())
    started = run_ok(tool, "pair", "--hub", real.origin, "--remote", "--name", "Bot", "--host", "new-box")
    return tool, started


def test_an_agent_alone_registers_works_moves_and_hands_over(real: RealHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "POLL_S", 0.05)
    # 1. a platform invitation: the account, its private organization and the recovery code
    bot, made = register_alone(real, "Bot One", platform_invitation(real))
    assert made["scope"] == "platform"
    assert made["display_name"] == "Bot One"
    assert made["hub"] == real.origin
    assert made["secret_fields"] == ["recovery_code"]
    assert len(made["recovery_code"]) == 30
    me = run_ok(bot, "api", "GET", "/v1/me")["answer"]
    assert me["user_id"] == made["user_id"]
    assert me["passkeys"] == []
    status = run_ok(bot, "status")
    assert status["agent_id"] == made["agent_id"]
    assert made["org_id"] in str(run_ok(bot, "org", "list"))
    agents = run_ok(bot, "agents", "list")["agents"]
    assert [(item["agent_id"], item["state"]) for item in agents] == [(made["agent_id"], "active")]
    # 2. it invites a person's account and a second agent registers alone through an org invitation and joins
    ada = person(real, "Ada")
    pair(real, ada)
    team = run_ok(bot, "org", "create", "--name", "Robots")["org_id"]
    invited = run_ok(bot, "org", "invite", "--org", team)["invite_link"]
    _second, joined = register_alone(real, "Bot Two", invited)
    assert (joined["scope"], joined["org_id"], joined["role"]) == ("org", team, "member")
    members = run_ok(bot, "member", "list", "--org", team)
    assert "Bot Two" in str(members)
    assert joined["user_id"] in str(members)
    ada_invite = run_ok(bot, "org", "invite", "--org", team)["invite_link"]
    run_ok(ada.tool, "org", "join", "--display-name", "Ada", stdin=ada_invite)
    assert "Bot One" in str(run_ok(ada.tool, "member", "list", "--org", team))
    # 3. the first agent moves to a new computer with its recovery code; the old one is disconnected
    moved_tool = make_tool(real.root / "new-computer")
    moved = run_ok(
        moved_tool, "recover", "--hub", real.origin, "--name", "Bot", "--host", "new", stdin=made["recovery_code"]
    )
    assert moved["user_id"] == made["user_id"]
    assert moved["agent_id"] != made["agent_id"]
    assert moved["secret_fields"] == ["recovery_code"]
    assert moved["recovery_code"] != made["recovery_code"]
    assert bot.run("status")[1]["error"] == "agent_revoked"
    assert team in str(run_ok(moved_tool, "org", "list"))
    stale = moved_tool.run("recover", "--hub", real.origin, stdin=made["recovery_code"])[1]
    assert (stale["error"], stale["reason"]) == ("invalid_argument", "recovery_code_unknown")
    # 4. it hands the account to the agent of a remote pairing on a third computer
    third, started = remote_pairing(real, "third")
    confirmed = run_ok(moved_tool, "agents", "confirm", "--link", started["connect_link"], "--code", started["code"])
    pairing_id = dict(parse_qsl(started["connect_link"].split("#")[1]))["p"]
    assert (confirmed["agent_id"], confirmed["replaced"]) == (pairing_id, moved["agent_id"])
    connected = run_ok(third, "pair", "--wait", "--timeout", "5", "--return-code", confirmed["return_code"])
    assert (connected["state"], connected["user_id"]) == ("connected", made["user_id"])
    assert moved_tool.run("status")[1]["error"] == "not_paired"
    status = run_ok(third, "status")
    # the successor holds the account's identity again (ADR-082 section 7) and works without a person
    assert (status["user_id"], status["identity"]) == (made["user_id"], "user")
    assert run_ok(third, "recovery", "new")["secret_fields"] == ["recovery_code"]


def test_an_account_with_a_passkey_keeps_the_persons_approval(real: RealHub) -> None:
    bot, made = register_alone(real, "Bot", platform_invitation(real))
    give_passkey(real, made["user_id"])
    _other, started = remote_pairing(real, "other")
    refused = bot.run("agents", "confirm", "--link", started["connect_link"], "--code", started["code"])[1]
    assert refused["error"] == "not_own_account"
    moved = make_tool(real.root / "moved").run("recover", "--hub", real.origin, stdin=made["recovery_code"])[1]
    assert moved["error"] == "not_own_account"
    assert run_ok(bot, "status")["agent_id"] == made["agent_id"]


def test_a_wrong_code_or_a_foreign_link_confirms_nothing(real: RealHub) -> None:
    bot, made = register_alone(real, "Bot", platform_invitation(real))
    _other, started = remote_pairing(real, "other")
    wrong = bot.run("agents", "confirm", "--link", started["connect_link"], "--code", "00000-00000")[1]
    assert (wrong["error"], wrong["reason"]) == ("pairing_failed", "agent_code_mismatch")
    link, fragment = started["connect_link"].split("#")
    swapped = fragment.replace(dict(parse_qsl(fragment))["f"], "A" * 22)
    mismatch = bot.run("agents", "confirm", "--link", f"{link}#{swapped}", "--code", started["code"])[1]
    assert (mismatch["error"], mismatch["reason"]) == ("pairing_failed", "pairing_mismatch")
    foreign = bot.run("agents", "confirm", "--link", f"https://elsewhere.example/connect#{fragment}", "--code", "x")[1]
    assert foreign["error"] == "invalid_argument"
    assert run_ok(bot, "status")["agent_id"] == made["agent_id"]


def test_the_invitation_is_used_once_and_the_root_key_can_be_named(real: RealHub) -> None:
    link = platform_invitation(real)
    tool, _made = register_alone(real, "Bot", link)
    again = make_tool(real.root / "again").run("register", "--own-account", "--display-name", "X", stdin=link)[1]
    assert (again["error"], again["hub_error"]) == ("hub_refused", "invite_unknown")
    named = make_tool(real.root / "named")
    args = ("register", "--own-account", "--display-name", "Y", "--platform-root-key", ROOT_KEY)
    assert run_ok(named, *args, stdin=platform_invitation(real))["scope"] == "platform"
    assert run_ok(tool, "status")["hub"] == real.origin
