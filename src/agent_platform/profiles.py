"""Rights profiles, the profile of a task and the review verdicts (ADR-093; 03-api.md section 4.13; twin of
`web/src/core/profiles/profiles.ts`).

- `profile list --wg`: every profile usable in the workgroup (built-in roles, the organization's and the workgroup's
  own) and the class of every operation type;
- `profile set --wg|--org --name --operation … --reads`: create or change an own profile (writers of the workgroup,
  admins of the organization); `profile delete --wg|--org --name`;
- `task profile --task (--profile NAME | --clear)`: assign or clear the profile of a task's main sessions;
- `review verdicts --task`: the review verdicts and security reports of a task with their verified results.

A profile is a voluntary restriction (E-11): without one a session may do what its human may.
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import uuid_argument
from agent_platform.hub import segment
from agent_platform.sessions import task_operations

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
REACHES = ("task", "workgroup", "all")
REVIEW_TYPES = ("review.verdict", "security.report")


def profile_name(text: str) -> str:
    value = text.strip()
    if NAME_RE.fullmatch(value) is None:
        raise PlatformError("invalid_argument", reason="profile_invalid")
    return value


def profiles_list(account: Account, wg: str) -> dict[str, Any]:
    return account.call("GET", f"/v1/workgroups/{segment(uuid_argument(wg))}/agent-profiles")


def _path(wg: str | None, org: str | None, name: str) -> str:
    if (wg is None) == (org is None):
        raise PlatformError("invalid_argument", reason="wg_or_org")
    if wg is not None:
        return f"/v1/workgroups/{segment(uuid_argument(wg))}/agent-profiles/{segment(profile_name(name))}"
    return f"/v1/orgs/{segment(uuid_argument(str(org)))}/agent-profiles/{segment(profile_name(name))}"


def profile_set(
    account: Account, wg: str | None, org: str | None, name: str, *, operations: list[str], reads: str
) -> dict[str, Any]:
    path = _path(wg, org, name)
    account.call("PUT", path, {"operations": sorted(set(operations)), "reads": reads})
    return {"name": name, "operations": sorted(set(operations)), "reads": reads, "scope": "wg" if wg else "org"}


def profile_delete(account: Account, wg: str | None, org: str | None, name: str) -> dict[str, Any]:
    account.call("DELETE", _path(wg, org, name))
    return {"name": name, "deleted": True}


def task_profile(account: Account, task: str, name: str | None) -> dict[str, Any]:
    body = {"profile": None if name is None else profile_name(name)}
    answer = account.call("PUT", f"/v1/tasks/{segment(uuid_argument(task))}/agent-profile", body)
    return {"task_id": task, "profile": answer.get("profile")}


def _result(row: dict[str, Any]) -> dict[str, Any] | None:
    for event in row.get("events", []):
        body = event.get("body")
        if event.get("kind") == "operation.result" and isinstance(body, dict):
            return body
    return None


def review_verdicts(account: Account, task: str) -> dict[str, Any]:
    """The verdicts and security reports of the task (newest first) with their verified results."""
    found = task_operations(account, uuid_argument(task), None, None)
    items = []
    for row in found["operations"]:
        if row.get("type") in REVIEW_TYPES:
            items.append(
                {
                    "operation_id": row.get("operation_id"),
                    "type": row.get("type"),
                    "session_id": row.get("session_id"),
                    "state": row.get("state"),
                    "result": _result(row),
                }
            )
    return {"task_id": task, "items": items, "unverified": found["unverified"]}


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return profiles_list(account, args.wg)


def _set(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return profile_set(account, args.wg, args.org, args.name, operations=args.operation, reads=args.reads)


def _delete(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return profile_delete(account, args.wg, args.org, args.name)


def _task(account: Account, args: Any, context: Context) -> dict[str, Any]:
    if (args.profile is None) == (not args.clear):
        raise PlatformError("invalid_argument", reason="profile_or_clear")
    return task_profile(account, args.task, args.profile)


def _verdicts(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return review_verdicts(account, args.task)


def register(tree: Tree) -> None:
    group = tree.group("profile", "rights profiles of sessions (voluntary restrictions, ADR-093)")
    sub = tree.command(
        group,
        "list",
        _list,
        "the profiles usable in a workgroup and the class of every operation type",
        example="agent-platform profile list --wg <wg_id>",
        words="rechteprofil rolle rollen profile roles rights",
        then="agent-platform task profile --task <task_id> --profile review",
    )
    sub.add_argument("--wg", required=True)
    sub = tree.command(
        group,
        "set",
        _set,
        "create or change an own profile of a workgroup (writers) or an organization (admins)",
        writes=True,
        example="agent-platform profile set --wg <wg_id> --name triage --operation github.get_issue --reads task",
        words="rechteprofil anlegen ändern rolle create change profile",
    )
    sub.add_argument("--wg")
    sub.add_argument("--org")
    sub.add_argument("--name", required=True)
    sub.add_argument("--operation", action="append", default=[], help="an operation type, repeatable")
    sub.add_argument("--reads", choices=REACHES, default="all")
    sub = tree.command(
        group,
        "delete",
        _delete,
        "delete an own profile (assignments keep their snapshot)",
        writes=True,
        example="agent-platform profile delete --wg <wg_id> --name triage",
        words="rechteprofil löschen delete profile",
    )
    sub.add_argument("--wg")
    sub.add_argument("--org")
    sub.add_argument("--name", required=True)
    task_group = tree.group("task", "tasks")
    sub = tree.command(
        task_group,
        "profile",
        _task,
        "assign (--profile) or clear (--clear) the rights profile of a task's main sessions",
        writes=True,
        example="agent-platform task profile --task <task_id> --profile review",
        words="rechteprofil aufgabe rolle zuweisen assign task profile role",
    )
    sub.add_argument("--task", required=True)
    sub.add_argument("--profile")
    sub.add_argument("--clear", action="store_true")
    review_group = tree.group("review", "review verdicts and security reports (ADR-093)")
    sub = tree.command(
        review_group,
        "verdicts",
        _verdicts,
        "the review verdicts and security reports of a task",
        example="agent-platform review verdicts --task <task_id>",
        words="prüfurteil urteil freigabe review verdict security report",
    )
    sub.add_argument("--task", required=True)
