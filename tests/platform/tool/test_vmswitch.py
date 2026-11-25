"""The switch of a workgroup VM to the newest golden in the agent tool (ADR-093 sections 3 and 5, KOR-70):
`wg vm-switch --preview` reads the VM's status from `GET /v1/workgroups/{wg}`, `wg vm-switch` posts the request and
prints the hub's answer; malformed arguments and answers are refused."""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .hubfake import HubFake, User

SINCE = "2026-11-25T10:00:00.000Z"
VM = "vm-0000000a"
UPGRADE = {
    "golden_version": "g2",
    "golden_built_at": SINCE,
    "state": "waiting",
    "holds": [{"reason": "session_active", "session_id": uuid7(), "since": SINCE}],
    "sessions": [],
}


def _wg(hub: HubFake, account: User) -> str:
    org = next(org for org in hub.orgs.values() if org.private and org.log.state.member(account.id))
    return str(org.log.state.default_wg)


def test_preview_shows_the_status_of_the_vm(tool: Tool, hub: HubFake, account: User) -> None:
    wg = _wg(hub, account)
    device = uuid7()
    devices: list[Any] = [
        "x",
        {"device_id": uuid7(), "vms": "x"},
        {
            "device_id": device,
            "vms": [
                None,
                {"vm_id": "vm-0000000b", "golden_version": "g2", "upgrade": None},
                {"vm_id": VM, "golden_version": "g1", "upgrade": UPGRADE},
            ],
        },
    ]
    hub.hooks[("GET", f"/v1/workgroups/{wg}")] = lambda body, request: (200, {"devices": devices})
    code, document = tool.run("wg", "vm-switch", "--wg", wg, "--vm", VM, "--preview")
    assert code == 0, document
    assert document == {
        "ok": True,
        "wg_id": wg,
        "vm_id": VM,
        "device_id": device,
        "golden_version": "g1",
        "current": False,
        "upgrade": UPGRADE,
    }
    assert tool.run("wg", "vm-switch", "--wg", wg, "--vm", "vm-0000000b", "--preview")[1]["current"] is True
    assert tool.run("wg", "vm-switch", "--wg", wg, "--vm", "vm-000000ff", "--preview")[1]["error"] == "not_found"
    hub.hooks[("GET", f"/v1/workgroups/{wg}")] = lambda body, request: (200, {"devices": "x"})
    assert tool.run("wg", "vm-switch", "--wg", wg, "--vm", VM, "--preview")[1]["reason"] == "response_invalid"


def test_switch_posts_the_request(tool: Tool, hub: HubFake, account: User) -> None:
    wg = _wg(hub, account)
    device = uuid7()
    sent: list[Any] = []
    target = f"/v1/workgroups/{wg}/vms/{VM}/switch"

    def answer(body: Any, request: Any) -> tuple[int, Any]:
        sent.append(body)
        return 200, {
            "vm_id": VM,
            "device_id": device,
            "golden_version": "g1",
            "requested": True,
            "requested_at": SINCE,
            "upgrade": UPGRADE,
        }

    hub.hooks[("POST", target)] = answer
    code, document = tool.run("wg", "vm-switch", "--wg", wg, "--vm", VM)
    assert code == 0, document
    assert sent == [{}]
    assert document == {
        "ok": True,
        "wg_id": wg,
        "vm_id": VM,
        "device_id": device,
        "golden_version": "g1",
        "requested": True,
        "requested_at": SINCE,
        "upgrade": UPGRADE,
    }
    hub.hooks[("POST", target)] = lambda body, request: (403, {"error": "forbidden"})
    refused = tool.run("wg", "vm-switch", "--wg", wg, "--vm", VM)[1]
    assert refused["error"] == "hub_refused"
    assert "forbidden" in str(refused)
    hub.hooks[("POST", target)] = lambda body, request: (200, {"requested": "yes"})
    assert tool.run("wg", "vm-switch", "--wg", wg, "--vm", VM)[1]["reason"] == "response_invalid"
    for argv in (("--wg", "x", "--vm", VM), ("--wg", wg, "--vm", "vm-1"), ("--wg", wg, "--vm", "vm-1", "--preview")):
        assert tool.run("wg", "vm-switch", *argv)[1]["error"] == "invalid_argument", argv
