"""Main agents with names, their people and their helpers in the agent tool (KOR-52, ADR-095 section 7; twin of the
web client's grouping "By person" of the home page and of the agent tree, 06-web.md section 4.0a).

- `agents people --org ORG [--after]`: one row per person with open main agents in the workgroups the user can read
  (`GET /v1/orgs/{org}/people/agents`, the caller first): the person's name (verified profile events), the number of
  main agents, waiting for a decision, stuck, running, active subagents; `next_after` pages on.
- `agents list --org ORG [--owner me|USER] [--before]`: the open main agents of one person, newest activity first
  (`GET /v1/orgs/{org}/main-agents`), each with handle, name (`agentnames`), workgroup, state and attention, the
  task's GitHub identity (null = automatic: the instructing person's own account) and its subagents; `next_before`
  pages on. Without `--org` the command lists the user's connected AI agents as before (`security.agents_list`).
- `agents show --task TASK`: the agent tree of a task (`GET /v1/tasks/{task}`): every session in tree order with its
  handle, depth, state, GitHub identity (its own, else the task's), the person whose instruction created it
  (`triggered_by`) and, for a subagent, its purpose where the user can open it (the sealed `operation.params` of the
  `subagent.request` the session was started for, `origin_operation_id`).
- `agents rename --task TASK (--name TEXT | --automatic)`: seals and appends a `task.agent_name` event `{name}` (null
  = back to the automatic name) for the task's workgroup like the web client (`eventio.seal`, `POST /v1/events`);
  checked here first: the name's rule (`agentnames.checked_name`) and the user's write access to the workgroup in the
  verified log (`no_write_access`).
- `task list --wg WG --by-person`: the workgroup's task list grouped by the responsible person, the signed author of
  the task's first user-written `task.meta` (as `task overview`; the hub's `owner_id` is not used), the user first.

Rows the hub sends that do not fit the API shape are left out and counted (`unverified`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from agent_platform import identities
from agent_platform.account import Account, unverified
from agent_platform.agentnames import AGENT_NAME, AUTOMATIC_EMPTY, AgentName, checked_name, handle, shown, task_names
from agent_platform.attention import ATTENTION_KEYS
from agent_platform.attention import _org as member_org
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_all, post_event, pull, rows_of, seal, uuid_argument
from agent_platform.hub import segment
from agent_platform.orgs import find_org
from agent_platform.sessions import metas, task_row
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import WG

PAGE_DEFAULT, PAGE_MAX = 50, 100
COUNTS = ("agents", "needs_decision", "blocked", "working", "subagents_active")
SUBAGENT_REQUEST = "subagent.request"


def _limit(limit: int | None) -> int:
    if limit is not None and limit < 1:
        raise PlatformError("invalid_argument")
    return min(PAGE_MAX, PAGE_DEFAULT if limit is None else limit)


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


class Names:
    """The verified names of one org the commands print, read once each: people, workgroups and main agents."""

    def __init__(self, account: Account, org: str) -> None:
        self.account = account
        self.org = org
        profiles = account.names(org)
        self.people = profiles.members
        self.workgroups = profiles.workgroups
        self.agents: dict[str, dict[str, AgentName]] = {}

    def agent(self, wg: str, task: str) -> AgentName:
        if wg not in self.agents:
            self.agents[wg] = task_names(self.account, self.org, wg)
        return self.agents[wg].get(task, AUTOMATIC_EMPTY)


# --- people ---


def people(account: Account, org: str, after: str | None, limit: int | None) -> dict[str, Any]:
    """The person rows of the org (module docstring)."""
    org = member_org(account, org)
    query: dict[str, object] = {"limit": _limit(limit), **({"after": uuid_argument(after)} if after else {})}
    answer = account.call("GET", f"/v1/orgs/{segment(org)}/people/agents?{urlencode(query)}")
    listed = rows_of(answer, "people")
    rows = [row for row in listed if is_uuid7(str(row.get("owner_id"))) and all(_count(row.get(k)) for k in COUNTS)]
    names = account.names(org).members
    following = answer.get("next_after")
    return {
        "org_id": org,
        "people": [
            {
                "owner_id": row["owner_id"],
                "name": names.get(str(row["owner_id"])),
                "you": row["owner_id"] == account.user_id,
                **{key: row[key] for key in COUNTS},
            }
            for row in rows
        ],
        "next_after": following if isinstance(following, str) and is_uuid7(following) else None,
        "unverified": len(answer["people"]) - len(rows),
    }


# --- one person's main agents ---


def _item(org: str, item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """`(task_row, attention_row or None)` of one list item, None when it does not fit the API shape."""
    task, attention = item.get("task"), item.get("attention")
    if not isinstance(task, dict) or not is_uuid7(str(task.get("task_id"))) or not is_uuid7(str(task.get("wg_id"))):
        return None
    if task.get("org_id") != org or handle(task.get("number")) is None:
        return None
    if attention is not None and (not isinstance(attention, dict) or attention.get("task_id") != task["task_id"]):
        return None
    return task, attention


def _accounts(account: Account, rows: list[dict[str, Any]]) -> None:
    """`github_account` of every row (the task's or session's identity named as `identities.name_of` does; the text
    `automatic` without one); the identities are read once, if needed."""
    found = identities.names(account) if any(isinstance(row.get("github_identity"), str) for row in rows) else {}
    for row in rows:
        row["github_account"] = identities.name_of(found, row.get("github_identity")) or "automatic"


def main_agents(account: Account, org: str, owner: str, before: str | None, limit: int | None) -> dict[str, Any]:
    """One person's open main agents (module docstring)."""
    org = member_org(account, org)
    person = account.user_id if owner == "me" else uuid_argument(owner)
    query = {"owner": person, "limit": _limit(limit), **({"before": before} if before else {})}
    answer = account.call("GET", f"/v1/orgs/{segment(org)}/main-agents?{urlencode(query)}")
    listed = rows_of(answer, "items")
    rows = [found for found in (_item(org, item) for item in listed) if found is not None]
    names = Names(account, org)
    agents: list[dict[str, Any]] = []
    for task, attention in rows:
        wg, task_id = str(task["wg_id"]), str(task["task_id"])
        name = names.agent(wg, task_id)
        agents.append(
            {
                "task_id": task_id,
                "handle": handle(task["number"]),
                "name": name.name,
                "name_source": name.source,
                "agent": shown(name, task["number"]),
                "wg_id": wg,
                "workgroup": names.workgroups.get(wg),
                "state": task.get("state"),
                "github_identity": task.get("github_identity"),
                "last_activity_at": task.get("last_activity_at"),
                **{key: None if attention is None else attention.get(key) for key in ATTENTION_KEYS},
            }
        )
    _accounts(account, agents)
    following = answer.get("next_before")
    return {
        "org_id": org,
        "owner_id": person,
        "owner": names.people.get(person),
        "agents": agents,
        "next_before": following if isinstance(following, str) and following else None,
        "unverified": len(listed) - len(rows),
    }


# --- the agent tree of a task ---


def _purposes(account: Account, org: str, wg: str, task: str) -> dict[str, str]:
    """Operation ID -> purpose of the task's `subagent.request` operations whose `operation.params` the user opens."""
    items, _last = pull(account, wg, {"kinds": "operation.params", "task": task})
    events, _skipped = open_all(account, org, (WG, wg), items)
    found: dict[str, str] = {}
    for event in events:
        body = event.get("body")
        if event["task"] != task or not isinstance(body, dict) or body.get("type") != SUBAGENT_REQUEST:
            continue
        params, operation = body.get("params"), body.get("operation_id")
        purpose = params.get("purpose") if isinstance(params, dict) else None
        if isinstance(purpose, str) and isinstance(operation, str) and event["ref"] == operation:
            found[operation] = purpose
    return found


def _sessions(answer: dict[str, Any], task: str) -> list[dict[str, Any]]:
    """The task's session rows of `GET /v1/tasks/{task}` (rows of another task are left out)."""
    listed = answer.get("sessions")
    rows = [item.get("session") for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("task_id") == task and is_uuid7(str(row.get("session_id")))
    ]


def _tree_order(rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """`(level, row)` in tree order: main sessions by creation, each followed by its children by creation."""
    ids = {str(row["session_id"]) for row in rows}
    below: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        parent = row.get("parent_session_id")
        below.setdefault(parent if parent in ids else None, []).append(row)
    ordered: list[tuple[int, dict[str, Any]]] = []

    def visit(parent: str | None, level: int) -> None:
        for row in sorted(
            below.get(parent, []), key=lambda item: (str(item.get("created_at")), str(item["session_id"]))
        ):
            ordered.append((level, row))
            visit(str(row["session_id"]), level + 1)

    visit(None, 0)
    return ordered


def agent_show(account: Account, task: str) -> dict[str, Any]:
    """The agent tree of a task (module docstring)."""
    answer = account.call("GET", f"/v1/tasks/{segment(uuid_argument(task))}")
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task or not is_uuid7(str(row.get("wg_id"))):
        raise unverified("response_invalid")
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    names = Names(account, org)
    name = task_names(account, org, wg, task).get(task, AUTOMATIC_EMPTY)
    purposes = _purposes(account, org, wg, task)
    number = row.get("number")
    sessions = []
    for level, session in _tree_order(_sessions(answer, task)):
        depth = session.get("depth")
        path = session.get("agent_path")
        origin = session.get("origin_operation_id")
        person = session.get("triggered_by")
        sessions.append(
            {
                "session_id": session["session_id"],
                "level": level,
                "kind": "main" if depth == 0 else "subagent",
                "handle": handle(number) if depth == 0 else (handle(number, path) if isinstance(path, str) else None),
                "parent_session_id": session.get("parent_session_id"),
                "state": session.get("state"),
                "purpose": purposes.get(origin) if isinstance(origin, str) else None,
                "github_identity": session.get("github_identity") or row.get("github_identity"),
                "triggered_by": person,
                "triggered_by_name": names.people.get(person) if isinstance(person, str) else None,
            }
        )
    owner = row.get("owner_id")
    header = {
        "task_id": task,
        "wg_id": wg,
        "workgroup": names.workgroups.get(wg),
        "number": number if handle(number) is not None else None,
        "handle": handle(number),
        "name": name.name,
        "name_source": name.source,
        "agent": shown(name, number),
        "state": row.get("state"),
        "owner_id": owner,
        "owner": names.people.get(owner) if isinstance(owner, str) else None,
        "github_identity": row.get("github_identity"),
    }
    _accounts(account, [header, *sessions])
    return {"agent": header, "sessions": sessions}


# --- rename ---


def agent_rename(account: Account, task: str, name: str | None) -> dict[str, Any]:
    """Gives the task's main agent `name` (None: back to the automatic name) as a sealed `task.agent_name` event."""
    body = {"name": None if name is None else checked_name(name)}
    wg = str(task_row(account, task)["wg_id"])
    org = find_org(account, wg)
    if not account.org_log(org).state.can_write_wg(account.user_id, wg):
        raise PlatformError("no_write_access")
    event = seal(account, org, (WG, wg), AGENT_NAME, body, wg=wg, task=task)
    seq = post_event(account, event)
    found = task_names(account, org, wg, task).get(task, AUTOMATIC_EMPTY)
    return {
        "task_id": task,
        "event_id": event["header"]["id"],  # type: ignore[index]
        "scope_seq": seq,
        "name": found.name,
        "name_source": found.source,
    }


# --- task list by person ---


def by_person(account: Account, listed: dict[str, Any]) -> dict[str, Any]:
    """The task list `listed` (`tasks.task_list`) grouped by responsible person (module docstring)."""
    org, wg = str(listed["org_id"]), str(listed["wg_id"])
    owners = metas(account, org, wg)
    names = account.names(org).members
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for row in listed["tasks"]:
        groups.setdefault(owners.get(str(row.get("task_id")), (None, None))[1], []).append(row)
    order = sorted(groups, key=lambda who: (who != account.user_id, who is None, names.get(str(who), ""), str(who)))
    return {
        "org_id": org,
        "wg_id": wg,
        "people": [
            {
                "owner_id": who,
                "name": None if who is None else names.get(who),
                "you": who == account.user_id,
                "tasks": groups[who],
            }
            for who in order
        ],
    }


# --- commands ---


def agents_list_command(account: Account, args: Any) -> dict[str, Any]:
    """`agents list --org ...` (the user's connected AI agents without `--org`: `security`)."""
    return main_agents(account, args.org, args.owner or "me", args.before, args.limit)


def _people(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return people(account, args.org, args.after, args.limit)


def _show(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return agent_show(account, args.task)


def _rename(account: Account, args: Any, context: Context) -> dict[str, Any]:
    # `--name` and `--automatic` are one required choice of the parser
    return agent_rename(account, args.task, args.name)


def register(tree: Tree) -> None:
    group = tree.group("agents", "your connected AI agents and the main agents of tasks")
    sub = tree.command(
        group,
        "people",
        _people,
        "per person the open main agents of tasks in an org: how many, waiting for a decision, stuck, running, helpers",
        example="agent-platform agents people --org <org_id>",
        words="personen verantwortliche hauptagenten wer welche agenten people responsible main agents by person",
        then="agent-platform agents list --org <org_id> --owner <user_id>",
        trim="first",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--after", help="next_after of the previous page")
    sub = tree.command(
        group,
        "show",
        _show,
        "the agent tree of a task: its main agent and subagents with handles, purposes, states and GitHub identities",
        example="agent-platform agents show --task <task_id>",
        words="agentenbaum hauptagent subagenten helfer zweck handle tree subagents helpers purpose",
        trim="first",
    )
    sub.add_argument("--task", required=True)
    sub = tree.command(
        group,
        "rename",
        _rename,
        "give the main agent of a task a name, or back to the automatic name",
        writes=True,
        example='agent-platform agents rename --task <task_id> --name "Login doctor"',
        words="umbenennen agentenname name geben hauptagent rename agent name automatic",
        then="agent-platform agents show --task <task_id>",
    )
    sub.add_argument("--task", required=True)
    choice = sub.add_mutually_exclusive_group(required=True)
    choice.add_argument("--name", help="the new name (1-80 characters, one line)")
    choice.add_argument("--automatic", action="store_true", help="back to the name derived from the task title")
