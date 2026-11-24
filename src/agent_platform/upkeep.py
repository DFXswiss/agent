"""What the browser's background jobs do on every sync, done by the tool around its commands (11-agent-access.md
section 8.4; 06-web.md section 11 jobs 3-5, `web/src/core/jobs/catch-up.ts`, `rotation.ts`, `credential-rewrap.ts`;
owner rule E-04: an account with only an agent gets the protection a browser gives).

A browser runs the pending rotations, the catch-up wrapping and the credential-key check after every log change and
every ten minutes. The tool has no background of its own besides `claude watch`, so it runs them around commands:

- `prepare`, before every command that writes into an org or stores a credential (an event, a log entry, a record):
  for every org of the user whose verified log owes a batch the user may send (`membership.owed_planner`: for an admin
  the unassignment of decertified platform devices; the rotation of every scope a member left by itself, `pending`,
  for an admin or a writer of the workgroup) the pending-rotation job `membership.rotate_pending`; then the
  credential-key check `credkey.rotate_if_owed`. Sealing never uses an epoch whose rotation is still owed
  (`rotation_pending`, `orgs.seal_epoch`), so when the user may not rotate or the rotation fails, nothing new is
  written that a member who left could read.
- `follow_up`, after such a command succeeded: catch-up wrapping (`keywraps.org_catch_up`) of every org of the user,
  so members and devices that are entitled now get their keys (the user's `CK` for newly entitled devices included).
- `sync` (both): `status` and every round of `claude watch` (which the background service runs).

When nothing is owed nothing is sent; the reads are those that find it out (the user's orgs, their verified logs, the
wraps; the platform log only for an admin of an org with platform devices; one `GET /v1/orgs/{org}/catch-up` per org).
A failing step never fails the command: the result gets `upkeep` only when a step did something or failed, with
`rotated` (`{org_id, scope, scope_id, epoch}`), `unassigned_devices`, `credentials` (the check's result when it
rotated, is blocked or waits), `wrapped` and `errors` (`{step, org_id?, error, ...fields}`). `org rotate`,
`org catch-up` and `credentials rotate` are these jobs themselves and do not run them around.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from agent_platform import credkey, keywraps, membership
from agent_platform.account import Account
from agent_platform.errors import PlatformError

Report = dict[str, Any]


def _note(report: Report, step: str, org: str | None, error: PlatformError) -> None:
    where = {} if org is None else {"org_id": org}
    report.setdefault("errors", []).append({"step": step, **where, "error": error.code, **error.fields})


def _orgs(account: Account, report: Report) -> list[str]:
    try:
        listed = account.me().get("orgs", [])
    except PlatformError as error:
        _note(report, "orgs", None, error)
        return []
    return [str(item["org_id"]) for item in listed if isinstance(item, dict) and isinstance(item.get("org_id"), str)]


def owes(account: Account, org: str) -> bool:
    """Whether the verified log of `org` owes a batch the user may send (decided locally, nothing is sent)."""
    log = account.org_log(org)
    state = log.state
    devices = any(principal.platform_seq is not None for principal in state.principals.values())
    admin = state.is_admin(account.user_id)
    platform = account.platform() if account.platform_root and admin and devices else None
    return membership.owed_planner(platform)(log, account.user_id) is not None


def prepare(account: Account, report: Report) -> None:
    """The owed rotations of every org of the user, then the credential-key check (module docstring)."""
    for org in _orgs(account, report):
        try:
            if not owes(account, org):
                continue
            done = membership.rotate_pending(account, org)
        except PlatformError as error:
            _note(report, "rotation", org, error)
            continue
        report.setdefault("rotated", []).extend({"org_id": org, **item} for item in done["rotated"])
        if done["unassigned_devices"]:
            report.setdefault("unassigned_devices", []).extend(done["unassigned_devices"])
    try:
        checked = credkey.rotate_if_owed(account)
    except PlatformError as error:
        _note(report, "credentials", None, error)
        return
    if checked["status"] != "not_owed" or "reason" in checked:
        report["credentials"] = checked


def follow_up(account: Account, report: Report) -> None:
    """Catch-up wrapping of every org of the user (module docstring)."""
    for org in _orgs(account, report):
        try:
            wrapped = keywraps.org_catch_up(account, org)["wrapped"]
        except PlatformError as error:
            _note(report, "catch_up", org, error)
            continue
        if wrapped:
            report["wrapped"] = report.get("wrapped", 0) + wrapped


def around(account: Account, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """`prepare`, the command, `follow_up`; the report as `upkeep` when it is not empty."""
    report: Report = {}
    prepare(account, report)
    result = work()
    follow_up(account, report)
    return {**result, "upkeep": report} if report else result


def sync(account: Account) -> Report:
    """`prepare` and `follow_up` without a command (`claude watch`); failures also go to stderr as codes."""
    report: Report = {}
    prepare(account, report)
    follow_up(account, report)
    for error in report.get("errors", []):
        print(f"agent-platform: {error['step']} failed: {error['error']}", file=sys.stderr)
    return report
