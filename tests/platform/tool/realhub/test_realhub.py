"""The agent tool against the real hub (11-agent-access.md section 10): every crypto-relevant command runs over HTTP
against `agent_hub` on the test PostgreSQL, so what the tool builds is checked by the hub's own code, not by a fake
that shares the tool's reference checks. The browser's part is played through the hub's routes (`support`)."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from agent_platform import remote

from .support import RealHub, confirm_remote, opens, pair, person, ring, run_ok, stored_event


def scopes(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {(item["scope"], item["scope_id"]) for item in document["rotated"]}


def test_pairing_on_this_computer_and_from_another_one(real: RealHub, monkeypatch: pytest.MonkeyPatch) -> None:
    ada = person(real, "Ada")
    paired = pair(real, ada)
    assert paired["user_id"] == ada.id
    status = run_ok(ada.tool, "status")
    assert (status["user_id"], status["agent_id"], status["hub"]) == (ada.id, paired["agent_id"], real.origin)
    # a second person pairs an agent that runs on another computer
    monkeypatch.setattr(remote, "POLL_S", 0.05)
    grace = person(real, "Grace")
    started = run_ok(grace.tool, "pair", "--hub", real.origin, "--remote", "--name", "Claude Code", "--host", "srv")
    origin, link = started["connect_link"].split("#")
    assert origin == f"{real.origin}/connect"
    pairing_id = dict(parse_qsl(link))["p"]
    assert run_ok(grace.tool, "pair", "--wait", "--timeout", "0")["state"] == "waiting"
    back = confirm_remote(real, grace, pairing_id, started["code"])
    assert run_ok(grace.tool, "pair", "--wait", "--timeout", "0")["state"] == "confirmed"
    connected = run_ok(grace.tool, "pair", "--wait", "--timeout", "5", "--return-code", back)
    assert (connected["state"], connected["agent_id"], connected["user_id"]) == ("connected", pairing_id, grace.id)
    assert run_ok(grace.tool, "status")["user_id"] == grace.id
    agents = run_ok(grace.tool, "agents", "list")
    assert pairing_id in str(agents)


def test_an_org_its_members_and_the_rotations(real: RealHub) -> None:
    ada, bob, carol = person(real, "Ada"), person(real, "Bob"), person(real, "Carol")
    for who in (ada, bob, carol):
        pair(real, who)
    created = run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")
    org = str(created["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    for who, options in ((bob, ["--wg", f"{wg}:write"]), (carol, [])):
        invited = run_ok(ada.tool, "org", "invite", "--org", org, *options)
        assert urlsplit(invited["invite_link"]).netloc == urlsplit(real.origin).netloc
        name = "Bob" if who is bob else "Carol"
        joined = run_ok(who.tool, "org", "join", "--display-name", name, stdin=invited["invite_link"])
        assert joined["org_id"] == org
    members = {row["user_id"]: row for row in run_ok(ada.tool, "member", "list", "--org", org)["members"]}
    assert set(members) == {ada.id, bob.id, carol.id}
    # a grant opens the workgroup to Carol: she reads what Bob writes there
    run_ok(ada.tool, "wg", "grant", "--wg", wg, "--user", carol.id, "--access", "read")
    written = run_ok(bob.tool, "whiteboard", "add", "--org", org, "--wg", wg, "--text", "from Bob")
    listed = run_ok(carol.tool, "whiteboard", "list", "--org", org, "--wg", wg)
    assert [item["text"] for item in listed["items"]] == ["from Bob"]
    assert opens(ring(carol), stored_event(carol, wg, written["scope_seq"]), bob)
    # the ungrant rotates the workgroup key: Carol's keys do not open what is written afterwards
    held_by_carol = ring(carol)
    ungranted = run_ok(ada.tool, "wg", "ungrant", "--wg", wg, "--user", carol.id)
    assert ("wg", wg) in scopes(ungranted)
    after_ungrant = run_ok(ada.tool, "whiteboard", "add", "--org", org, "--wg", wg, "--text", "after the ungrant")
    event = stored_event(ada, wg, after_ungrant["scope_seq"])
    assert not opens(held_by_carol, event, ada)
    assert opens(ring(bob), event, ada)
    # the removal rotates every scope Bob read: none of the keys he ever held opens a later event
    held_by_bob = ring(bob)
    removed = run_ok(ada.tool, "member", "remove", "--org", org, "--user", bob.id)
    assert {("org", org), ("wg", wg)} <= scopes(removed)
    for scope, options, epoch in ((org, [], 2), (wg, ["--wg", wg], 3)):
        added = run_ok(ada.tool, "whiteboard", "add", "--org", org, *options, "--text", "after the removal")
        later = stored_event(ada, scope, added["scope_seq"])
        assert later["header"]["epoch"] == epoch
        assert not opens(held_by_bob, later, ada)
        assert opens(ring(ada), later, ada)
    board = run_ok(ada.tool, "whiteboard", "list", "--org", org, "--wg", wg)
    assert [item["text"] for item in board["items"]] == ["after the removal", "after the ungrant", "from Bob"]
    code, refused = bob.tool.run("whiteboard", "list", "--org", org, "--wg", wg)
    assert (code, refused["ok"]) == (1, False)
    assert run_ok(carol.tool, "whiteboard", "list", "--org", org)["items"][0]["text"] == "after the removal"


def test_tasks_and_credentials(real: RealHub) -> None:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    # without any device the task is created and waits (ADR-075): no own device and no platform access yet
    early = str(run_ok(ada.tool, "task", "create", "--wg", wg, "--text", "Fix the login bug")["task_id"])
    assert run_ok(ada.tool, "task", "show", "--task", early)["waiting"] == "platform_access_required"
    # the operator certifies a platform device and grants the org platform access; the tool assigns it
    device, _seq = real.fleet.platform_device()
    real.fleet.platform.post({"type": "org_platform_access", "org": org, "granted": True})
    assigned = run_ok(ada.tool, "device", "assign", "--wg", wg)
    assert (assigned["device_id"], assigned["cred_wrap"]) == (device.id, True)
    task = str(run_ok(ada.tool, "task", "create", "--wg", wg, "--text", "Fix the login bug")["task_id"])
    shown = run_ok(ada.tool, "task", "show", "--task", task)
    assert "Fix the login bug" in str(shown)
    prompt = run_ok(ada.tool, "task", "prompt", "--task", task, "--text", "Also the logout")
    assert prompt["task_id"] == task
    assert "Also the logout" in str(run_ok(ada.tool, "task", "show", "--task", task))
    # the credit and the task's costs as the hub's billing routes answer them (no price list in force yet)
    credit = run_ok(ada.tool, "billing", "--org", org)
    assert (credit["org_id"], credit["billing_active"], credit["exhausted"]) == (org, False, False)
    assert (credit["price_list_version"], credit["token_prices"], credit["days"]) == (None, [], [])
    assert run_ok(ada.tool, "task", "charges", "--task", task)["total_micro_usd"] == 0
    # a credential stored by the agent, then the credential key rotated for the user, its agent and the device
    assert run_ok(ada.tool, "credentials", "set-api-key", stdin="sk-test-" + "k" * 20)["stored"] is True
    rotated = run_ok(ada.tool, "credentials", "rotate", "--force")
    assert (rotated["status"], rotated["epoch"], rotated["records"], rotated["recipients"]) == ("rotated", 2, 1, 3)
    listed = run_ok(ada.tool, "credentials", "list")["credentials"]
    assert [(item["kind"], item["status"], item["epoch"]) for item in listed] == [("anthropic_api_key", "verified", 2)]
    assert "k" * 20 not in str(listed)
    # written again under the new key
    assert run_ok(ada.tool, "credentials", "set-api-key", stdin="sk-test-new")["stored"] is True
    again = run_ok(ada.tool, "credentials", "list")["credentials"]
    assert [(item["epoch"], item["version"]) for item in again] == [(2, 3)]
