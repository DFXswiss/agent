"""The switch of a workgroup VM to the newest golden of its device (ADR-093 sections 3 and 5; 03-api.md section 4.5;
twin of `web/src/core/tasks/vmswitch.ts`).

- `wg vm-switch --wg W --vm V --preview`: the VM's status as its device reported it (`GET /v1/workgroups/{wg}`):
  `golden_version`, `upgrade` (null: current; else the golden it moves to, `waiting` or `switching`, the holds - who or
  what keeps it, since when - and the paused sessions that move with it);
- `wg vm-switch --wg W --vm V`: asks the device to switch now (`POST /v1/workgroups/{wg}/vms/{vm}/switch`, members with
  administration rights; the hub refuses others with `forbidden`). The answer is the preview and `requested`
  (false: the VM is current, nothing was asked). The device applies its own rules: never with a working session,
  never without complete checkpoints.
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_runner.crypto.ids import is_uuid7

VM_RE = re.compile(r"vm-[0-9a-f]{8}")
ANSWER = ("vm_id", "device_id", "golden_version", "requested", "requested_at", "upgrade")


def _check(wg: str, vm: str) -> None:
    if not is_uuid7(wg) or VM_RE.fullmatch(vm) is None:
        raise PlatformError("invalid_argument")


def preview(account: Account, wg: str, vm: str) -> dict[str, Any]:
    """The VM's status as reported (`not_found` when it is no live VM of the workgroup)."""
    _check(wg, vm)
    devices = account.call("GET", f"/v1/workgroups/{segment(wg)}").get("devices")
    if not isinstance(devices, list):
        raise unverified("response_invalid")
    for device in devices:
        vms = device.get("vms") if isinstance(device, dict) else None
        for item in vms if isinstance(vms, list) else []:
            if isinstance(item, dict) and item.get("vm_id") == vm:
                upgrade = item.get("upgrade")
                return {
                    "wg_id": wg,
                    "vm_id": vm,
                    "device_id": device.get("device_id"),
                    "golden_version": item.get("golden_version"),
                    "current": upgrade is None,
                    "upgrade": upgrade,
                }
    raise PlatformError("not_found")


def request(account: Account, wg: str, vm: str) -> dict[str, Any]:
    """Ask for the switch now; the hub's answer."""
    _check(wg, vm)
    answer = account.call("POST", f"/v1/workgroups/{segment(wg)}/vms/{segment(vm)}/switch", {})
    if not isinstance(answer.get("requested"), bool):
        raise unverified("response_invalid")
    return {"wg_id": wg, **{name: answer.get(name) for name in ANSWER}}


def _switch(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return preview(account, args.wg, args.vm) if args.preview else request(account, args.wg, args.vm)


def register(tree: Tree) -> None:
    group = tree.group("wg", "workgroups")
    sub = tree.command(
        group,
        "vm-switch",
        _switch,
        "move a workgroup VM to the newest version now (administrators); --preview shows what waits and what moves",
        example="agent-platform wg vm-switch --wg <wg_id> --vm <vm_id> [--preview]",
        words="neue fassung grundabbild golden wechseln aktualisieren vm update upgrade switch version paused sessions",
        then="agent-platform wg vm-switch --wg <wg_id> --vm <vm_id> --preview",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--vm", required=True, help="vm-… of the workgroup (agent-platform device list shows them)")
    sub.add_argument("--preview", action="store_true", help="only show the status: holds, sessions that move")
