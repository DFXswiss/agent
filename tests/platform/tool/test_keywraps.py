"""`org catch-up`: the wraps the hub offers as missing are created only when the verified logs confirm them."""

from __future__ import annotations

from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .hubfake import HubFake, User
from .seeding import assign_device


def test_catch_up_wraps_what_the_logs_confirm(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    code, nothing = tool.run("org", "catch-up", "--org", org)
    assert code == 0, nothing
    assert (nothing["offered"], nothing["wrapped"]) == (0, 0)
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["recipient_id"] != device]
    code, done = tool.run("org", "catch-up", "--org", org)
    assert code == 0, done
    assert (done["offered"], done["wrapped"]) == (3, 3)
    slots = {(w["scope"], w["scope_id"]) for w in hub.wraps if w["recipient_id"] == device}
    assert slots == {("org", org), ("wg", wg), ("cred", account.id)}
    # a hub that asks for wraps to outsiders, for old epochs or in odd shapes gets nothing
    stranger = uuid7()
    offer = [
        {"scope": "org", "scope_id": org, "epoch": 1, "recipient_id": stranger},
        {"scope": "wg", "scope_id": wg, "epoch": 2, "recipient_id": device},
        {"scope": "cred", "scope_id": account.id, "epoch": 2, "recipient_id": device},
        {"scope": "cred", "scope_id": uuid7(), "epoch": 1, "recipient_id": device},
        {"scope": "cred", "scope_id": account.id, "epoch": 1, "recipient_id": stranger},
        {"scope": "org", "scope_id": org, "epoch": True, "recipient_id": device},
        {"scope": "org", "scope_id": 5, "epoch": 1, "recipient_id": device},
        "x",
    ]
    hub.hooks[("GET", f"/v1/orgs/{org}/catch-up")] = lambda body, request: (200, {"missing": offer})
    before = len(hub.wraps)
    assert tool.run("org", "catch-up", "--org", org)[1] == {"ok": True, "org_id": org, "offered": 5, "wrapped": 0}
    assert len(hub.wraps) == before
    assert tool.run("org", "catch-up", "--org", "x")[1]["error"] == "invalid_argument"


def test_catch_up_without_a_credential_key(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["recipient_id"] != device and wrap["scope"] != "cred"]
    done = tool.run("org", "catch-up", "--org", org)[1]
    assert (done["offered"], done["wrapped"]) == (4, 2)


def test_catch_up_skips_orgs_whose_log_fails(tool: Tool, hub: HubFake, account: User) -> None:
    org, _wg, device = assign_device(tool, hub, account)
    hub.wraps[:] = [wrap for wrap in hub.wraps if not (wrap["recipient_id"] == device and wrap["scope"] == "cred")]
    original = hub.r_me
    hub.override(
        "r_me",
        lambda request, body: (
            200,
            {**original(request, body)[1], "orgs": [{"org_id": uuid7()}, *original(request, body)[1]["orgs"]]},
        ),
    )
    assert tool.run("org", "catch-up", "--org", org)[1]["wrapped"] == 1
