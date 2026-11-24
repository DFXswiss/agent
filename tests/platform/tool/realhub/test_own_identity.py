"""The agent's own identity against the real hub (ADR-082): an agent that holds its user's identity converts itself
when its account gets a passkey (also after an interrupted run), an agent leaves every log and is revoked, the
recovery code is left to the user's browser, and the credential key's rotation re-signs what an earlier agent of the
user wrote. Every step is checked by the hub's own code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import delegate
from agent_platform.errors import PlatformError
from tests.platform.tool.conftest import make_tool, wait_for

from .support import Person, RealHub, pair, person, run_ok
from .test_alone import give_passkey, platform_invitation, register_alone


def state_of(tool: Any) -> dict[str, Any]:
    return dict(json.loads((Path(tool.env["AGENT_PLATFORM_HOME"]) / "state.json").read_text()))


def write_state(tool: Any, state: dict[str, Any]) -> None:
    (Path(tool.env["AGENT_PLATFORM_HOME"]) / "state.json").write_text(json.dumps(state))


def test_an_agent_converts_itself_once_its_account_has_a_passkey(
    real: RealHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, made = register_alone(real, "Bot", platform_invitation(real))
    assert run_ok(bot, "status")["identity"] == "user"  # an account of an agent alone: the agent holds the identity
    before = state_of(bot)
    give_passkey(real, made["user_id"])
    # a run that stops after the conversion was prepared but before it reached the hub: the next one sends it again
    real_submit = delegate.submit

    def stops(account: Any, send: Any) -> Any:
        raise PlatformError("hub_unreachable")

    monkeypatch.setattr(delegate, "submit", stops)
    assert bot.run("status")[1]["error"] == "hub_unreachable"
    assert state_of(bot)["converting"]["gen"] >= 1
    monkeypatch.setattr(delegate, "submit", real_submit)
    status = run_ok(bot, "status")
    assert (status["identity"], status["agent_id"]) == ("own", made["agent_id"])
    converted = state_of(bot)
    assert "bundle_ct" not in converted
    assert "converting" not in converted
    me = run_ok(bot, "api", "GET", "/v1/agent/me")["answer"]
    assert (me["identity"], me["user_ct"]) == ("own", converted["user_ct"])
    # the agent is in its organization under its own ID and writes as itself
    org = str(made["org_id"])
    log = run_ok(bot, "api", "GET", f"/v1/orgs/{org}/log?limit=1000")["answer"]["entries"]
    assert [item["entry"]["body"]["subject"] for item in log if item["entry"]["body"]["type"] == "agent_add"] == [
        made["agent_id"]
    ]
    assert run_ok(bot, "whiteboard", "add", "--org", org, "--text", "as myself")["scope_seq"] > 0
    # a run interrupted after the hub converted the agent but before the state was rewritten adopts the hub's anchor
    write_state(bot, before)
    assert run_ok(bot, "status")["identity"] == "own"
    assert "bundle_ct" not in state_of(bot)


def test_an_agent_leaves_every_log_when_it_revokes_itself(real: RealHub) -> None:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    revoked = run_ok(ada.tool, "agents", "revoke", "--id", ada.agent_id)
    assert (revoked["this_agent_revoked"], revoked["agent_id"]) == (True, ada.agent_id)
    assert {item["org_id"] for item in revoked["left"]} >= {org}
    assert ada.tool.run("status")[1]["error"] == "not_paired"
    entries = ada.actor.browser.get(f"/v1/orgs/{org}/log?limit=1000").json()["entries"]
    last = entries[-1]["entry"]["body"]
    assert (last["type"], last["subject"], last["by"]) == ("remove", ada.agent_id, ada.agent_id)
    states = {row["agent_id"]: row["state"] for row in ada.actor.browser.get("/v1/me/agents").json()["agents"]}
    assert states[ada.agent_id] == "revoked"


def test_revoking_every_agent_and_the_recovery_code_in_the_browser(real: RealHub) -> None:
    bob = person(real, "Bob")
    pair(real, bob)
    assert run_ok(bob.tool, "recovery", "new")["opened"] == f"{real.origin}/settings#settings-passkeys"
    assert wait_for(lambda: f"{real.origin}/settings#settings-passkeys" in bob.tool.opened())
    every = run_ok(bob.tool, "agents", "revoke", "--all")
    assert (every["this_agent_revoked"], every["revoked"]) == (True, 1)
    assert bob.tool.run("status")[1]["error"] == "not_paired"


def test_the_next_agent_re_signs_what_the_previous_one_wrote(real: RealHub) -> None:
    carol = person(real, "Carol")
    pair(real, carol)
    first = carol.agent_id
    assert run_ok(carol.tool, "credentials", "set-api-key", stdin="sk-test-" + "c" * 20)["stored"] is True
    # Carol pairs her agent on another computer: it replaces the first one in every log
    moved = Person(carol.actor, make_tool(real.root / "carol-moved"), carol.cred_key)
    pair(real, moved)
    assert moved.agent_id != first
    real.fleet.platform.ensure_root()  # the rotation reads the platform log (decertified devices)
    # the new agent's next command rotates the credential key the replaced agent lost, re-signing its record
    rotated = run_ok(moved.tool, "status")["upkeep"]["credentials"]
    assert (rotated["status"], rotated["records"], rotated["dropped"]) == ("rotated", 1, [])
    assert "upkeep" not in run_ok(moved.tool, "status")  # handled once
    listed = run_ok(moved.tool, "credentials", "list")["credentials"]
    assert [(item["kind"], item["status"]) for item in listed] == [("anthropic_api_key", "verified")]
    agents = {row["agent_id"]: row["state"] for row in run_ok(moved.tool, "agents", "list")["agents"]}
    assert agents == {first: "revoked", moved.agent_id: "active"}


def test_the_agent_adds_itself_where_its_user_is_and_it_is_missing(real: RealHub) -> None:
    dora = person(real, "Dora")
    pair(real, dora)
    # Dora founds an organization in her browser: her agent is not in its log
    draft = real.world.public_org(dora.actor)
    added = run_ok(dora.tool, "status")["upkeep"]["agent_added"]
    assert [item["org_id"] for item in added] == [draft.id]
    entries = dora.actor.browser.get(f"/v1/orgs/{draft.id}/log?limit=1000").json()["entries"]
    last = entries[-1]["entry"]["body"]
    assert (last["type"], last["subject"], last["by"]) == ("agent_add", dora.agent_id, dora.agent_id)
    assert "agent_added" not in run_ok(dora.tool, "status").get("upkeep", {})
