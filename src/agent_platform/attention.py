"""The situation view of the agent tool (KOR-39, ADR-076 section 7; twin of the web client's home, 06-web.md section
4.0): `status --attention [--org ORG]`, `task list --org ORG --state needs-decision|blocked|working|done|waiting|
no-agent [--mine] [--limit N]` and `status --overview [--org ORG]` (the start page's three numbers of the open tasks:
working, waiting for a person, no agent; contracts/changes/HOME-COUNTS.md).

The hub derives one attention record per task (`GET /v1/orgs/{org}/attention`, paged with `next_before`, and
`/attention/summary`); the tool adds what only the client can read: the task titles (`task.meta`), the workgroup and
owner names (the verified profile events) and, for the reasons `question` and `approval`, the question - the sealed
`operation.params` event of the operation the record names (`GET /v1/scopes/{wg}/events?kinds=operation.params&task=
&ref=`), opened only after the reader checks (device author, kind rule, signature) and only when its body names that
operation and the asking type. Items that do not fit the API shape are left out and counted (`unverified`). An entry
carries the hub's `wait_until` (ADR-081 section 5: `working/quota_wait` and `needs_decision/quota_choice` continue by
themselves at that time; null otherwise, and from a hub of an older release). An entry names the task's main agent
(`agent`: `<name> #<number>`, KOR-52, `agentnames`).
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account
from agent_platform.agentnames import Named, agent_fields, read_names
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.reader import scope_events
from agent_platform.tasks import open_verified
from agent_runner.crypto.ids import is_uuid7

CATEGORIES = {"needs-decision": "needs_decision", "blocked": "blocked", "working": "working", "done": "done"}
"""Command-line state -> API category."""
OVERVIEWS = {"waiting": "waiting", "no-agent": "no_agent"}
"""Command-line state -> API overview of the open tasks (`working` is the category with the same tasks)."""
OVERVIEW_CATEGORIES = {"working": ("working",), "waiting": ("needs_decision",), "no_agent": ("blocked", "done")}
"""API overview -> the categories its list items may have (contracts/changes/HOME-COUNTS.md; the hub counts)."""
OVERVIEW_KEYS = ("open_working", "open_waiting", "open_no_agent")
"""The summary counts of the overview and their names in the answer of `task overview`."""
OVERVIEW_NAMES = {"open_working": "working", "open_waiting": "waiting", "open_no_agent": "no_agent"}
ASKING = {"question": "human.question", "approval": "human.approval"}
"""Reason -> the operation type whose `operation.params` holds the question."""
SUMMARY_KEYS = ("needs_decision", "needs_decision_mine", "blocked", "working", "done_since")
ATTENTION_KEYS = (
    "category",
    "reason",
    "ref_kind",
    "ref_id",
    "since",
    "seq",
    "subagents_total",
    "subagents_active",
    "updated_at",
    "wait_until",
)
PAGE_MAX = 100
LIST_DEFAULT = 100
LIST_MAX = 1000
STATUS_MAX = 1000


def _org(account: Account, org: str) -> str:
    if not is_uuid7(org):
        raise PlatformError("invalid_argument")
    if org not in {str(item.get("org_id")) for item in account.me().get("orgs", [])}:
        raise PlatformError("no_org")
    return org


def _fits(attention: dict[str, Any], category: str, overview: str | None) -> bool:
    """Whether a record belongs to the list: its category, or one of the overview's categories of an open task."""
    if overview is None:
        return attention.get("category") == category
    closed = attention.get("category") == "done" and attention.get("reason") == "closed"
    return attention.get("category") in OVERVIEW_CATEGORIES[overview] and not closed


def _item(
    org: str, category: str, item: object, overview: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """`(task_row, attention_row)` of one list item, None when it does not fit the API shape."""
    if not isinstance(item, dict):
        return None
    task, attention = item.get("task"), item.get("attention")
    if not isinstance(task, dict) or not isinstance(attention, dict):
        return None
    task_id = task.get("task_id")
    if (
        not isinstance(task_id, str)
        or not is_uuid7(task_id)
        or attention.get("task_id") != task_id
        or not _fits(cast(dict[str, Any], attention), category, overview)
        or not is_uuid7(str(task.get("wg_id")))
        or task.get("org_id", org) != org
    ):
        return None
    return cast(dict[str, Any], task), cast(dict[str, Any], attention)


class Reader:
    """Reads one org's attention records and opens what belongs to them (names, titles, questions)."""

    def __init__(self, account: Account, org: str) -> None:
        self.account = account
        self.org = org
        self.names = account.names(org)
        self.named: dict[str, Named] = {}
        self.unverified = 0

    def rows(
        self, category: str, *, mine: bool, limit: int, overview: str | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """The records of one category (or of one overview of the open tasks) in the hub's order, every page up to
        `limit`."""
        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        before: str | None = None
        while len(found) < limit:
            which = {"category": category} if overview is None else {"overview": overview}
            query: dict[str, object] = {**which, "limit": min(PAGE_MAX, limit - len(found))}
            if mine:
                query["mine"] = 1
            if before is not None:
                query["before"] = before
            page = self.account.call("GET", f"/v1/orgs/{segment(self.org)}/attention?{urlencode(query)}")
            items = page.get("items")
            if not isinstance(items, list):
                raise PlatformError("hub_refused", reason="response_invalid")
            for item in items:
                row = _item(self.org, category, item, overview)
                if row is None:
                    self.unverified += 1
                else:
                    found.append(row)
            following = page.get("next_before")
            if not items or not isinstance(following, str) or not following or following == before:
                break
            before = following
        return found[:limit]

    def of(self, wg: str) -> Named:
        """The titles and main agents' names of a workgroup's tasks, read once."""
        if wg not in self.named:
            self.named[wg] = read_names(self.account, self.org, wg)
        return self.named[wg]

    def question(self, wg: str, task: str, attention: dict[str, Any]) -> str | None:
        """The question of a `question` / `approval` record, opened and verified; None when it cannot be read."""
        kind, ref = ASKING.get(str(attention.get("reason"))), attention.get("ref_id")
        if kind is None or not isinstance(ref, str) or not is_uuid7(ref):
            return None
        for item in scope_events(self.account, wg, {"kinds": "operation.params", "task": task, "ref": ref}):
            opened = open_verified(self.account, self.org, wg, item)
            body = None if opened is None or opened["task"] != task else opened.get("body")
            if not isinstance(body, dict) or body.get("operation_id") != ref or body.get("type") != kind:
                continue
            params = body.get("params")
            question = params.get("question") if isinstance(params, dict) else None
            if isinstance(question, str):
                return question
        return None

    def entry(self, task: dict[str, Any], attention: dict[str, Any], *, question: bool) -> dict[str, Any]:
        wg, task_id, owner = str(task["wg_id"]), str(task["task_id"]), task.get("owner_id")
        found: dict[str, Any] = {
            "task_id": task_id,
            "wg_id": wg,
            "workgroup": self.names.workgroups.get(wg),
            "owner_id": owner,
            "owner": self.names.members.get(str(owner)),
            "state": task.get("state"),
            "title": self.of(wg).titles.get(task_id),
            **agent_fields(self.of(wg).agents, task),
            **{key: attention.get(key) for key in ATTENTION_KEYS},
        }
        if question and attention.get("reason") in ASKING:
            found["question"] = self.question(wg, task_id, attention)
        return found

    def counters(self) -> dict[str, Any]:
        """The hub's summary: the counters of the situation view and, from a hub that has them, the overview."""
        answer = self.account.call("GET", f"/v1/orgs/{segment(self.org)}/attention/summary")
        if not all(type(answer.get(key)) is int for key in SUMMARY_KEYS):
            raise PlatformError("hub_refused", reason="response_invalid")
        return answer


def _overview(answer: dict[str, Any]) -> dict[str, int] | None:
    """The three numbers of the open tasks of a summary, None from a hub of an older release."""
    if not all(type(answer.get(key)) is int for key in OVERVIEW_KEYS):
        return None
    return {OVERVIEW_NAMES[key]: answer[key] for key in OVERVIEW_KEYS}


def task_list(account: Account, org: str, state: str, *, mine: bool, limit: int) -> dict[str, Any]:
    """One category of the org's situation view, or one overview of its open tasks (`waiting`, `no-agent`), with titles
    (and the questions of `needs-decision` and `waiting`)."""
    category, overview = CATEGORIES.get(state), OVERVIEWS.get(state)
    if (category is None and overview is None) or not 1 <= limit <= LIST_MAX:
        raise PlatformError("invalid_argument")
    reader = Reader(account, _org(account, org))
    rows = reader.rows(category or "", mine=mine, limit=limit, overview=overview)
    asking = category == "needs_decision" or overview == "waiting"
    tasks = [reader.entry(task, attention, question=asking) for task, attention in rows]
    which = {"category": category} if overview is None else {"overview": overview}
    return {"org_id": org, **which, "tasks": tasks, "unverified": reader.unverified}


def _status_of(account: Account, org: str) -> dict[str, Any]:
    reader = Reader(account, org)
    answer = reader.counters()
    summary = {key: answer[key] for key in SUMMARY_KEYS}
    groups = {
        category: [
            reader.entry(task, attention, question=True)
            for task, attention in reader.rows(category, mine=False, limit=STATUS_MAX)
        ]
        for category in ("needs_decision", "blocked")
    }
    return {
        "org_id": org,
        "name": reader.names.org,
        "summary": summary,
        "overview": _overview(answer),
        **groups,
        "unverified": reader.unverified,
    }


def overview(account: Account, org: str | None) -> dict[str, Any]:
    """The start page's three numbers of the open tasks (`working`, `waiting`, `no_agent`): summed over every org of the
    user (or one org), with the numbers of each org. The hub assigns every open task to exactly one of them."""
    if org is not None:
        orgs = [_org(account, org)]
    else:
        orgs = [str(item.get("org_id")) for item in account.me().get("orgs", [])]
    total = dict.fromkeys(OVERVIEW_CATEGORIES, 0)
    each = []
    for found in orgs:
        reader = Reader(account, found)
        numbers = _overview(reader.counters())
        if numbers is None:
            raise PlatformError("hub_refused", reason="response_invalid")
        for name, count in numbers.items():
            total[name] += count
        each.append({"org_id": found, "name": reader.names.org, **numbers})
    return {"overview": total, "orgs": each}


def status(account: Account, org: str | None) -> dict[str, Any]:
    """The counters and every `needs_decision` and `blocked` entry of one org, or of every org of the user."""
    if org is not None:
        return {"attention": [_status_of(account, _org(account, org))]}
    orgs = [str(item.get("org_id")) for item in account.me().get("orgs", [])]
    return {"attention": [_status_of(account, found) for found in orgs]}
