"""`diagnose [--task <id>]` (11-agent-access.md section 8.4): why a task does not start.

Checks organizations, model access and the credentials it needs (incl. the expiry of a login token), the devices the
user's requests can reach and, with `--task`, the task's state, its waiting reason (ADR-075 section 7: a task is
always created and waits `scheduling` with a reason of `WAITING`) and its last transition. Every finding is a `reason`
of `REASONS` with its fixed `next` step; nothing the hub sends is printed as text (unknown hub reasons become
`task_waiting`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account
from agent_platform.claude import KIND as CLAUDE_KIND
from agent_platform.claude import LOGIN_TOKEN
from agent_platform.credentials import read_record
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError

REASONS: dict[str, str] = {
    "no_org": "Create an organization or join one (`agent-platform org create` / `org join`).",
    "claude_missing": "Run `agent-platform claude connect` or `agent-platform claude setup-token`.",
    "claude_expired": "Run `agent-platform claude sync` after using Claude Code once, or `claude setup-token`.",
    "claude_unreadable": "Store the Claude login again (`agent-platform claude connect` or `claude setup-token`).",
    "api_key_missing": "Store an API key (`agent-platform credentials set-api-key`) or change the model access.",
}
# Waiting reasons of a `scheduling` task (ADR-075 section 7, `contracts/enums/task-states.json`).
WAITING: dict[str, str] = {
    "no_device": (
        "Assign a device to the workgroup (`agent-platform device assign --wg <wg_id>`) or to the whole organization"
        " (`agent-platform device assign --org <org_id>`, admins), or make this computer one (`agent-platform device"
        " add --this-computer --wg <wg_id>`); the task starts by itself."
    ),
    "device_offline": "The device is offline; wait a few minutes or assign another device.",
    "capacity_exhausted": (
        "All devices are busy, or a device just added still builds its image (a few minutes); the task starts by"
        " itself when one is ready."
    ),
    "platform_access_required": (
        "The organization has no device of its own and no access to the operator's devices; assign an own device or"
        " ask the operator for access. The task starts by itself then."
    ),
    "wg_vm_limit": (
        "The workgroup uses as many VMs as allowed; the task starts when one of its VMs has room, or when the operator"
        " raises the limit."
    ),
    "placement_full": (
        "The chosen device or VM is full; wait, or let the platform choose (`agent-platform api PUT"
        ' /v1/tasks/<task_id>/placement --body \'{"device_id":null,"vm_id":null}\'`).'
    ),
    "placement_offline": (
        "The chosen device is offline; wait for it, or let the platform choose (`agent-platform api PUT"
        ' /v1/tasks/<task_id>/placement --body \'{"device_id":null,"vm_id":null}\'`).'
    ),
    "keys_pending": (
        "The device given to the organization still lacks this workgroup's key; it gets it as soon as a member's"
        " client or a device that holds the key is online. The task starts by itself."
    ),
    "device_failed": (
        "Interrupted: the device the task ran on failed. It continues by itself on another device as soon as one is"
        " free, or there once the device is back. A task tied to that device waits for it; let the platform choose"
        ' (`agent-platform api PUT /v1/tasks/<task_id>/placement --body \'{"device_id":null,"vm_id":null}\'`).'
    ),
}
REASONS |= WAITING
# The next step of a task whose newest session failed for a reason that a command fixes
# (`contracts/enums/session-failure-reasons.json`); any other reason is `task_failed`.
FAILED: dict[str, str] = {
    "model_access_unavailable": (
        "Connect the model access: `agent-platform claude connect` (the Claude Code login of this computer), or an"
        " API key (`agent-platform credentials set-api-key`). The task then continues by itself."
    ),
}
REASONS |= FAILED
REASONS |= {
    "task_failed": "Send the prompt again or start a new task.",
    "task_closed": "The task is closed; start a new task.",
    "task_waiting": "The task waits for the hub to schedule it; check again in a minute.",
    "task_running": "The task runs; nothing to do.",
}
_RUNNING = {"starting", "working", "idle", "running"}


def task_next(row: dict[str, Any]) -> str | None:
    """The next step for a task row: why a `scheduling` task waits, or what fixes a `failed` one (KOR-67)."""
    reason = row.get("state_reason")
    if row.get("state") == "scheduling":
        return WAITING.get(str(reason), REASONS["task_waiting"])
    if row.get("state") == "failed":
        return FAILED.get(str(reason), REASONS["task_failed"])
    return None


def finding(check: str, reason: str) -> dict[str, str]:
    return {"check": check, "reason": reason, "next": REASONS[reason]}


def _credentials(account: Account, access: object, kinds: set[str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    claude = None
    if CLAUDE_KIND in kinds:
        record, body = read_record(account, CLAUDE_KIND)
        if body is None:
            claude = "claude_unreadable" if record is not None else "claude_missing"
        elif body.get("kind") == LOGIN_TOKEN:
            try:
                expired = parse_time(body.get("expires_at")) <= datetime.now(UTC)
            except CryptoError:
                expired = True
            claude = "claude_expired" if expired else None
    else:
        claude = "claude_missing"
    api_key = None if "anthropic_api_key" in kinds else "api_key_missing"
    if access == "subscription" and claude is not None:
        found.append(finding("credentials", claude))
    elif access == "api_key" and api_key is not None:
        found.append(finding("credentials", api_key))
    elif access == "auto" and claude is not None and api_key is not None:
        found.append(finding("credentials", claude))
    return found


def _devices(account: Account) -> list[dict[str, str]]:
    answer = account.call("GET", "/v1/me/devices")
    devices = [item for item in answer.get("devices", []) if isinstance(item, dict)]
    if not devices:
        return [finding("devices", "no_device")]
    if not any(item.get("online") is True for item in devices):
        return [finding("devices", "device_offline")]
    return []


def _task(account: Account, task: str) -> dict[str, str]:
    answer = account.call("GET", f"/v1/tasks/{segment(task)}")
    found_row = answer.get("task")
    row: dict[str, Any] = found_row if isinstance(found_row, dict) else {}
    state = row.get("state")
    transitions = answer.get("transitions")
    last = transitions[-1] if isinstance(transitions, list) and transitions else {}
    reason = last.get("reason") if isinstance(last, dict) else None
    if state == "failed":
        return finding("task", "task_failed")
    if state == "closed":
        return finding("task", "task_closed")
    if state in _RUNNING:
        return finding("task", "task_running")
    waiting = row.get("state_reason") if state == "scheduling" else None
    for found in (waiting, reason):
        if isinstance(found, str) and found in WAITING:
            return finding("task", found)
    return finding("task", "task_waiting")


def diagnose(account: Account, task: str | None) -> dict[str, Any]:
    me = account.me()
    findings: list[dict[str, str]] = []
    if not me.get("orgs"):
        findings.append(finding("organizations", "no_org"))
    settings = me.get("settings")
    access = settings.get("model_access") if isinstance(settings, dict) else None
    listed = account.call("GET", "/v1/me/credentials").get("credentials", [])
    kinds = {str(item.get("kind")) for item in listed if isinstance(item, dict)}
    if access != "platform":
        findings += _credentials(account, access, kinds)
    findings += _devices(account)
    if task is not None:
        try:
            findings.append(_task(account, task))
        except PlatformError as error:
            if error.fields.get("hub_error") != "not_found":
                raise
            raise PlatformError("invalid_argument", hub_error="not_found") from None
    ok = all(item["reason"] == "task_running" for item in findings)
    return {"healthy": ok, "model_access": access, "findings": findings}
