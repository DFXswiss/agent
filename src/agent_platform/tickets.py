"""Captured tasks in the agent tool (E-41, ADR-102; twin of the web client's ticket fields): `task capture`, `task
edit`, `task start`, `topic list`, `topic create` and the ticket fields and filters of `task list` and `task show`.

- **Capture** is `POST /v1/tasks` with the sealed `task.meta` alone (`{title, description?, github_org?,
  github_repo?}`, sealed for the workgroup as `task create` seals its title) and the plaintext `urgency`,
  `assignee_id`, `topic_id`: no agent starts, the task waits as `todo`.
- **Edit** changes status, urgency, person and topic with `PUT /v1/tasks/{task}/fields` (`completed` closes the task,
  `todo`/`inProgress` reopen a closed one without an agent) and title, description, GitHub organisation and
  repository with a new sealed `task.meta` that carries the full current set (readers take each field from the newest
  `task.meta` that carries it: a device's `task.set_title` writes only the title).
- **Start** is the task's first prompt (`POST /v1/tasks/{task}/prompts`): by default the title, a blank line and the
  description, as the web client sends it; the hub moves `todo` to `inProgress`.
- **Topics** are sealed org-scope `topic.profile` events `{name}` with the topic ID as `ref` (the newest per ref
  wins); every member creates one. `--new-topic NAME` creates it while capturing or editing.
- **Person** is a user of the organization (`me` is the user of this tool); **filters** by status, urgency, person
  and topic go to the hub, by GitHub organisation and repository are applied here to the opened `task.meta`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_item, post_event, seal, uuid_argument
from agent_platform.hub import segment
from agent_platform.orgs import find_org
from agent_platform.reader import scope_events
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import ORG, USER, WG

STATUSES = ("todo", "inProgress", "completed")
URGENCIES = ("low", "normal", "high", "urgent")
TOPIC_KIND = "topic.profile"
META_FIELDS = ("title", "description", "github_org", "github_repo")
DESCRIPTION_MAX = 4096
TITLE_RE = re.compile("[^\r\n]{1,120}")
TOPIC_RE = re.compile("[^\r\n]{1,100}")
GITHUB_ORG_RE = re.compile("[A-Za-z0-9][A-Za-z0-9-]{0,38}")
GITHUB_REPO_RE = re.compile("([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}")
NONE = "none"
ME = "me"
ORDERS = ("activity", "created", "urgency")
"""The orders of `task list` and `task overview` (ADR-103 section 2): latest activity (default), newest
creation, most urgent first."""
OVERVIEWS = ("working", "waiting", "no-agent")
"""The overview of the open tasks (the start page's three groups: in progress, waiting for a question, no agent; the
hub's `working`, `waiting`, `no_agent`, read from the tasks' attention records)."""


def sort_key(order: str) -> Callable[[Mapping[str, Any]], tuple[Any, ...]]:
    """The key that sorts listed tasks of several workgroups in `order` (descending; each hub list's order)."""
    if order == "created":
        return lambda row: (str(row.get("created_at")), str(row.get("task_id")))
    if order == "urgency":
        return lambda row: (
            URGENCIES.index(str(row.get("urgency"))),
            str(row.get("last_activity_at")),
            str(row.get("task_id")),
        )
    return lambda row: (str(row.get("last_activity_at")), str(row.get("task_id")))


def _invalid(reason: str) -> PlatformError:
    return PlatformError("invalid_argument", reason=reason)


# --- checks of the arguments (the forms of `contracts/events/objects.json`) ---


def checked_title(text: str) -> str:
    """A title: 1-120 characters on one line, trimmed."""
    title = text.strip()
    if TITLE_RE.fullmatch(title) is None:
        raise _invalid("title_invalid")
    return title


def checked_meta(fields: Mapping[str, str]) -> dict[str, str]:
    """The sealed fields as given (`""` clears description, organisation and repository); the repository's owner is
    the organisation when both are set."""
    found = dict(fields)
    if "title" in found:
        found["title"] = checked_title(found["title"])
    if len(found.get("description", "")) > DESCRIPTION_MAX:
        raise _invalid("description_invalid")
    org, repo = found.get("github_org"), found.get("github_repo")
    if org and GITHUB_ORG_RE.fullmatch(org) is None:
        raise _invalid("github_org_invalid")
    if repo and GITHUB_REPO_RE.fullmatch(repo) is None:
        raise _invalid("github_repo_invalid")
    if repo and org is None:
        found["github_org"] = repo.split("/")[0]
    elif repo and org and repo.split("/")[0].lower() != org.lower():
        raise _invalid("github_repo_invalid")
    return found


def checked_code(value: str, allowed: tuple[str, ...], reason: str) -> str:
    if value not in allowed:
        raise _invalid(reason)
    return value


# --- topics ---


def _body(opened: Mapping[str, Any] | None) -> dict[str, Any]:
    """The body of an opened event; empty for one that did not verify or is locked."""
    body = opened.get("body") if opened is not None else None
    return body if isinstance(body, dict) else {}


def newest_topics(events: Iterable[Mapping[str, Any] | None]) -> dict[str, str]:
    """Topic ID -> name from opened `topic.profile` events in scope order: the last one of a `ref` wins; an event
    that did not verify, is locked or has no name is skipped."""
    names: dict[str, str] = {}
    for opened in events:
        name = _body(opened).get("name")
        if opened is not None and isinstance(name, str):
            names[str(opened["ref"])] = name
    return names


def topics(account: Account, org: str) -> dict[str, str]:
    """The topics of the organization: topic ID -> name of its newest verified `topic.profile`."""
    ring = account.keys(org)
    items = scope_events(account, org, {"kinds": TOPIC_KIND})
    return newest_topics(open_item(account, org, (ORG, org), item, ring=ring) for item in items)


def topic_list(account: Account, org: str) -> dict[str, Any]:
    """`topic list`: the organization's topics by name."""
    found = topics(account, uuid_argument(org))
    listed = sorted(({"topic_id": ref, "name": name} for ref, name in found.items()), key=lambda row: row["name"])
    return {"org_id": org, "topics": listed}


def topic_create(account: Account, org: str, name: str, topic: str | None = None) -> dict[str, Any]:
    """`topic create` (and `--new-topic`): a new topic, or a new name of `topic`."""
    text = name.strip()
    if TOPIC_RE.fullmatch(text) is None:
        raise _invalid("topic_invalid")
    ref = uuid7() if topic is None else uuid_argument(topic)
    uuid_argument(org)
    post_event(account, seal(account, org, (ORG, org), TOPIC_KIND, {"name": text}, ref=ref))
    return {"org_id": org, "topic_id": ref, "name": text}


# --- the sealed fields of a workgroup's tasks ---


def newest_meta(events: Iterable[Mapping[str, Any] | None]) -> dict[str, dict[str, str]]:
    """Task ID -> the sealed fields from opened `task.meta` events in scope order: each field from the last event
    that carries it (a device's `task.set_title` carries only the title); others are skipped."""
    found: dict[str, dict[str, str]] = {}
    for opened in events:
        fields = {
            name: value for name, value in _body(opened).items() if name in META_FIELDS and isinstance(value, str)
        }
        if opened is not None and fields:
            found.setdefault(str(opened["task"]), {}).update(fields)
    return found


def ticket_meta(account: Account, org: str, wg: str, task: str | None = None) -> dict[str, dict[str, str]]:
    """Task ID -> the sealed fields of the workgroup's tasks (of `task` alone when given), `newest_meta`."""
    query = {"kinds": "task.meta", **({"task": task} if task is not None else {})}
    ring = account.keys(org)
    return newest_meta(open_item(account, org, (WG, wg), item, ring=ring) for item in scope_events(account, wg, query))


def merged_meta(current: Mapping[str, str], changed: Mapping[str, str]) -> dict[str, str]:
    """The full set a new `task.meta` carries: the current fields with the changed ones (a title is required)."""
    merged = {**current, **changed}
    if "title" not in merged:
        raise PlatformError("invalid_argument", reason="title_missing")
    return merged


def answered_task(answer: Mapping[str, Any], task: str) -> dict[str, Any]:
    """The task row of a hub answer, which must name the task (`hub_refused`/`response_invalid` otherwise)."""
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task:
        raise PlatformError("hub_refused", reason="response_invalid")
    return row


def _meta_view(meta: Mapping[str, str]) -> dict[str, str | None]:
    return {name: meta.get(name) or None for name in ("description", "github_org", "github_repo")}


def status_of(row: Mapping[str, Any]) -> str:
    """The row's status; derived from the state for a hub before ADR-102."""
    status = row.get("status")
    if isinstance(status, str) and status in STATUSES:
        return status
    return "completed" if row.get("state") == "closed" else "inProgress"


def row_fields(row: Mapping[str, Any], topic_names: Mapping[str, str]) -> dict[str, Any]:
    """The plaintext ticket fields of a task row, with the topic's name."""
    topic = row.get("topic_id")
    return {
        "status": status_of(row),
        "urgency": row.get("urgency") or "normal",
        "assignee_id": row.get("assignee_id"),
        "topic_id": topic,
        "topic": topic_names.get(str(topic)) if topic else None,
    }


# --- people ---


def assignee_id(account: Account, org: str, value: str | None) -> str | None:
    """`me`, a user ID of the organization or None (`invalid_argument` for anyone else)."""
    if value is None:
        return None
    user = account.user_id if value == ME else uuid_argument(value)
    found = account.org_log(org).state.member(user)
    if found is None or found.kind != USER:
        raise _invalid("assignee_unknown")
    return user


def _topic_choice(account: Account, org: str, topic: str | None, new_topic: str | None) -> str | None:
    if topic is not None and new_topic is not None:
        raise _invalid("topic_twice")
    if new_topic is not None:
        return str(topic_create(account, org, new_topic)["topic_id"])
    return None if topic is None else uuid_argument(topic)


# --- capture, edit, start ---


@dataclass(frozen=True)
class Ticket:
    """What `task capture` and `task edit` were given (None: not given; `""` clears a sealed text field)."""

    title: str | None = None
    description: str | None = None
    github_org: str | None = None
    github_repo: str | None = None
    urgency: str | None = None
    assignee: str | None = None
    topic: str | None = None
    new_topic: str | None = None
    status: str | None = None

    def sealed(self) -> dict[str, str]:
        given = {name: getattr(self, name) for name in META_FIELDS}
        return {name: value for name, value in given.items() if value is not None}


def captured_meta(ticket: Ticket) -> dict[str, str]:
    """The sealed fields of a new captured task (a title, no status; empty fields left out)."""
    if ticket.title is None or ticket.status is not None:
        raise PlatformError("invalid_argument")
    return {name: value for name, value in checked_meta(ticket.sealed()).items() if value != ""}


def task_capture(account: Account, wg: str, ticket: Ticket) -> dict[str, Any]:
    """`task capture`: a task without an agent (module docstring)."""
    meta = captured_meta(ticket)
    plain: dict[str, Any] = {}
    if ticket.urgency is not None:
        plain["urgency"] = checked_code(ticket.urgency, URGENCIES, "urgency_invalid")
    org = find_org(account, uuid_argument(wg))
    person = assignee_id(account, org, ticket.assignee)
    if person is not None:
        plain["assignee_id"] = person
    topic = _topic_choice(account, org, ticket.topic, ticket.new_topic)
    if topic is not None:
        plain["topic_id"] = topic
    task = uuid7()
    event = seal(account, org, (WG, wg), "task.meta", meta, wg=wg, task=task)
    answer = account.call("POST", "/v1/tasks", {"task_id": task, "wg_id": wg, "events": [event], **plain})
    row = answered_task(answer, task)
    return {
        "org_id": org,
        "wg_id": wg,
        "task_id": task,
        "number": row.get("number"),
        **{name: meta.get(name) for name in META_FIELDS},
        **row_fields(row, topics(account, org) if topic is not None else {}),
    }


def _task_row(account: Account, task: str) -> dict[str, Any]:
    return answered_task(account.call("GET", f"/v1/tasks/{segment(uuid_argument(task))}"), task)


def plain_changes(ticket: Ticket) -> dict[str, Any]:
    """The plaintext fields `task edit` names that need no organization: status and urgency, and `none` for person
    or topic (the rest is resolved against the organization)."""
    plain: dict[str, Any] = {}
    if ticket.status is not None:
        plain["status"] = checked_code(ticket.status, STATUSES, "status_invalid")
    if ticket.urgency is not None:
        plain["urgency"] = checked_code(ticket.urgency, URGENCIES, "urgency_invalid")
    if ticket.assignee == NONE:
        plain["assignee_id"] = None
    if ticket.topic == NONE:
        plain["topic_id"] = None
    return plain


def task_edit(account: Account, task: str, ticket: Ticket) -> dict[str, Any]:
    """`task edit`: the given fields of the task (module docstring)."""
    sealed = checked_meta(ticket.sealed())
    plain = plain_changes(ticket)
    row = _task_row(account, task)
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    if ticket.assignee not in (None, NONE):
        plain["assignee_id"] = assignee_id(account, org, ticket.assignee)
    if ticket.topic not in (None, NONE) or ticket.new_topic is not None:
        plain["topic_id"] = _topic_choice(account, org, ticket.topic, ticket.new_topic)
    if not sealed and not plain:
        raise PlatformError("invalid_argument")
    if sealed:
        merged = merged_meta(ticket_meta(account, org, wg, task).get(task, {}), sealed)
        post_event(account, seal(account, org, (WG, wg), "task.meta", merged, wg=wg, task=task))
    if plain:
        row = answered_task(account.call("PUT", f"/v1/tasks/{segment(task)}/fields", plain), task)
    return ticket_show(account, task, row)


def ticket_show(account: Account, task: str, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The task's ticket: its plaintext and sealed fields."""
    found = row if row is not None else _task_row(account, task)
    wg = str(found["wg_id"])
    org = find_org(account, wg)
    meta = ticket_meta(account, org, wg, task).get(task, {})
    names = topics(account, org) if found.get("topic_id") else {}
    return {
        "task_id": task,
        "org_id": org,
        "wg_id": wg,
        "number": found.get("number"),
        "state": found.get("state"),
        "title": meta.get("title"),
        **_meta_view(meta),
        **row_fields(found, names),
        "agent_started": found.get("main_session_id") is not None,
    }


def start_text(meta: Mapping[str, str]) -> str:
    """The first prompt of a captured task: its title, a blank line and its description (as the web client)."""
    title = meta.get("title", "")
    description = meta.get("description", "")
    return f"{title}\n\n{description}" if description else title


def start_prompt(meta: Mapping[str, str], text: str | None) -> str:
    """The first prompt: `text` when given, else `start_text`; never blank (`invalid_argument`)."""
    prompt = text if text is not None else start_text(meta)
    if not prompt.strip():
        raise _invalid("prompt_missing")
    return prompt


def task_start(account: Account, task: str, text: str | None = None) -> dict[str, Any]:
    """`task start`: the task's agent starts with its first prompt (default `start_text`)."""
    from agent_platform.tasks import task_prompt  # noqa: PLC0415 - tasks imports this module

    row = _task_row(account, task)
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    started = task_prompt(account, task, start_prompt(ticket_meta(account, org, wg, task).get(task, {}), text))
    return {**started, "status": status_of(_task_row(account, task))}


# --- the list ---


@dataclass(frozen=True)
class Filters:
    """The ticket filters of `task list` (None: no filter)."""

    status: str | None = None
    urgency: str | None = None
    assignee: str | None = None
    topic: str | None = None
    github_org: str | None = None
    github_repo: str | None = None
    overview: str | None = None

    def query(self, account: Account, org: str) -> dict[str, str]:
        """The hub's filters (`status`, `urgency`, `assignee`, `topic`, `overview`)."""
        found: dict[str, str] = {}
        if self.overview is not None:
            found["overview"] = checked_code(self.overview, OVERVIEWS, "overview_invalid").replace("-", "_")
        if self.status is not None:
            found["status"] = checked_code(self.status, STATUSES, "status_invalid")
        if self.urgency is not None:
            found["urgency"] = checked_code(self.urgency, URGENCIES, "urgency_invalid")
        if self.assignee is not None:
            found["assignee"] = NONE if self.assignee == NONE else str(assignee_id(account, org, self.assignee))
        if self.topic is not None:
            found["topic"] = NONE if self.topic == NONE else uuid_argument(self.topic)
        return found

    def sealed(self) -> bool:
        return self.github_org is not None or self.github_repo is not None

    def matches(self, meta: Mapping[str, str]) -> bool:
        """The GitHub filters over the opened fields (case-insensitive, as GitHub names)."""
        org = (meta.get("github_org") or "").lower()
        repo = (meta.get("github_repo") or "").lower()
        if self.github_org is not None and org != self.github_org.lower():
            return False
        return self.github_repo is None or repo == self.github_repo.lower()


def list_query(account: Account, org: str, filters: Filters | None) -> str:
    """The query string of the hub's filters, `&`-prefixed (empty without)."""
    found = {} if filters is None else filters.query(account, org)
    return f"&{urlencode(found)}" if found else ""
