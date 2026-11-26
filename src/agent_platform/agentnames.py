"""Names and handles of main agents in the agent tool (KOR-52, ADR-095 sections 1, 2 and 4; twin of the web client's
agent names, 06-web.md section 4.0a).

- The handle of a main agent is `#<number>` (the task's `number` of the hub), a subagent's `#<number>.<agent_path>`.
- Its name is the newest `task.agent_name` event of the task by scope position: a name a person gave (`given`), or
  null and then the automatic name of the task's newest verified title (`automatic`, the rule of
  `contracts/tasks/agent-name.json`, `agent_runner.sessions.agent_name`); without such an event it is automatic too.
  When the newest one does not verify, is locked or breaks the body schema, the name is `unverifiable` (no text) and
  never an older one (ADR-095 section 2, as titles in 06-web.md section 1). A title that is locked or does not verify
  is no title (the name is then derived from the empty title: `Agent`).
- A main agent is shown as `<name> #<number>`; the audit prints its actor as "Main agent <name> #42 on behalf of
  <person>" or "Subagent #42.1 of <name> #42 on behalf of <person>", "(no single person)" without a person.
- `checked_name` is the client-side check of a new name as the web client runs it: white space at both ends removed,
  then 1-80 characters without any line break (also U+000B, U+000C, U+0085, U+2028 and U+2029).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_item
from agent_platform.reader import scope_events
from agent_runner.crypto.log.state import WG
from agent_runner.sessions.agent_name import automatic_name

AGENT_NAME = "task.agent_name"
NAME_KINDS = f"task.meta,{AGENT_NAME}"
GIVEN, AUTOMATIC, UNVERIFIABLE = "given", "automatic", "unverifiable"
NAME_RE = re.compile("[^\r\n]{1,80}")
"""`events/objects.json#/$defs/body_task_agent_name`: 1-80 characters on one line."""
NAME_MAX = 80
# The Unicode White_Space property (the set of `contracts/tasks/title.json`) and every line break among it.
_EDGES = re.compile(
    "^[\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|"
    "[\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$"
)
_BREAKS = re.compile("[\n\r\x0b\x0c\x85\u2028\u2029]")


@dataclass(frozen=True)
class AgentName:
    """The name of a main agent and where it comes from (`given`, `automatic` or `unverifiable`, then None)."""

    name: str | None
    source: str


def automatic(title: str) -> AgentName:
    """The automatic name of a main agent whose task is titled `title`."""
    return AgentName(automatic_name(title), AUTOMATIC)


AUTOMATIC_EMPTY = automatic("")
"""The name of a task the user knows no event of."""


def checked_name(text: str) -> str:
    """A new name as the user typed it, trimmed (`invalid_argument` when it breaks the rule of the module docstring)."""
    name = _EDGES.sub("", text)
    if not 1 <= len(name) <= NAME_MAX or _BREAKS.search(name) is not None:
        raise PlatformError("invalid_argument", reason="agent_name_invalid")
    return name


def _given(opened: Mapping[str, Any]) -> AgentName | None:
    """The name of a verified `task.agent_name` event: given, None for null (automatic), unverifiable otherwise."""
    body = opened.get("body")
    if opened.get("locked") or not isinstance(body, dict) or set(body) != {"name"}:
        return AgentName(None, UNVERIFIABLE)
    name = body["name"]
    if name is None:
        return None
    if isinstance(name, str) and NAME_RE.fullmatch(name):
        return AgentName(name, GIVEN)
    return AgentName(None, UNVERIFIABLE)


def _claimed_task(item: object) -> str | None:
    """The task an event that does not verify claims to belong to (only to mark that task's name unverifiable)."""
    event = item.get("event") if isinstance(item, dict) else None
    header = event.get("header") if isinstance(event, dict) else None
    if not isinstance(header, dict) or header.get("kind") != AGENT_NAME or not isinstance(header.get("task"), str):
        return None
    return str(header["task"])


@dataclass(frozen=True)
class Named:
    """The newest verified titles and the main agents' names of a workgroup's tasks, by task ID."""

    titles: dict[str, str]
    agents: dict[str, AgentName]


def read_names(account: Account, org: str, wg: str, task: str | None = None) -> Named:
    """The titles and names of the workgroup's tasks (of `task` alone when given) from one read of their events."""
    query = {"kinds": NAME_KINDS, **({"task": task} if task is not None else {})}
    ring = account.keys(org)
    titles: dict[str, tuple[int, str]] = {}
    newest: dict[str, tuple[int, AgentName | None]] = {}
    for item in scope_events(account, wg, query):
        seq = item.get("scope_seq") if isinstance(item, dict) else None
        if not isinstance(seq, int):
            continue
        opened = open_item(account, org, (WG, wg), item, ring=ring)
        if opened is None:
            claimed = _claimed_task(item)
            if claimed is not None and seq > newest.get(claimed, (0, None))[0]:
                newest[claimed] = (seq, AgentName(None, UNVERIFIABLE))
            continue
        owner = str(opened["task"])
        if opened["kind"] == AGENT_NAME:
            if seq > newest.get(owner, (0, None))[0]:
                newest[owner] = (seq, _given(opened))
            continue
        body = opened.get("body")
        if opened["kind"] == "task.meta" and isinstance(body, dict) and seq > titles.get(owner, (0, ""))[0]:
            titles[owner] = (seq, str(body.get("title", "")))
    agents: dict[str, AgentName] = {}
    for owner in {*titles, *newest}:
        given = newest.get(owner, (0, None))[1]
        title = titles.get(owner, (0, ""))[1]
        agents[owner] = given if given is not None else automatic(title)
    return Named({owner: title for owner, (_seq, title) in titles.items()}, agents)


def task_names(account: Account, org: str, wg: str, task: str | None = None) -> dict[str, AgentName]:
    """The names of the workgroup's main agents (of `task` alone when given), by task ID."""
    return read_names(account, org, wg, task).agents


def handle(number: object, path: object = None) -> str | None:
    """`#<number>` or `#<number>.<path>`; None without a number (a hub of an older release)."""
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return f"#{number}" if not isinstance(path, str) else f"#{number}.{path}"


def shown(name: AgentName, number: object) -> str | None:
    """A main agent as people see it: `<name> #<number>` (the handle alone for an unverifiable name, the name alone
    without a number)."""
    found = handle(number)
    if found is None:
        return name.name
    return found if name.name is None else f"{name.name} {found}"


def agent_fields(names: Mapping[str, AgentName], row: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a printed task row gets: `number`, `agent` (`<name> #<number>`) and `name_source`."""
    name = names.get(str(row.get("task_id")), AUTOMATIC_EMPTY)
    number = row.get("number")
    return {
        "number": number if handle(number) is not None else None,
        "agent": shown(name, number),
        "name_source": name.source,
    }


def actor_text(kind: str, agent: Mapping[str, Any], main: str | None, person: str | None) -> str:
    """The audit actor of a `main_agent`/`subagent` row (module docstring); `main` is the shown main agent."""
    number = agent.get("number")
    main_text = main if main is not None else str(handle(number))
    behalf = f"on behalf of {person}" if person is not None else "(no single person)"
    if kind == "main_agent":
        return f"Main agent {main_text} {behalf}"
    return f"Subagent {handle(number, agent.get('agent_path'))} of {main_text} {behalf}"
