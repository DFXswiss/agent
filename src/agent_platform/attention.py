"""The situation view of the agent tool (KOR-39, ADR-076 section 7; twin of the web client's home, 06-web.md section
4.0): `status --attention [--org ORG]` and `task list --org ORG --state needs-decision|blocked|working|done [--mine]
[--limit N]`.

The hub derives one attention record per task (`GET /v1/orgs/{org}/attention`, paged with `next_before`, and
`/attention/summary`); the tool adds what only the client can read: the task titles (`task.meta`), the workgroup and
owner names (the verified profile events) and, for the reasons `question` and `approval`, the question - the sealed
`operation.params` event of the operation the record names (`GET /v1/scopes/{wg}/events?kinds=operation.params&task=
&ref=`), opened only after the reader checks (device author, kind rule, signature) and only when its body names that
operation and the asking type. Items that do not fit the API shape are left out and counted (`unverified`).
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.reader import scope_events
from agent_platform.tasks import open_verified, titles
from agent_runner.crypto.ids import is_uuid7

CATEGORIES = {"needs-decision": "needs_decision", "blocked": "blocked", "working": "working", "done": "done"}
"""Command-line state -> API category."""
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


def _item(org: str, category: str, item: object) -> tuple[dict[str, Any], dict[str, Any]] | None:
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
        or attention.get("category") != category
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
        self.titles: dict[str, dict[str, str]] = {}
        self.unverified = 0

    def rows(self, category: str, *, mine: bool, limit: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """The records of one category in the hub's order, every page up to `limit`."""
        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        before: str | None = None
        while len(found) < limit:
            query: dict[str, object] = {"category": category, "limit": min(PAGE_MAX, limit - len(found))}
            if mine:
                query["mine"] = 1
            if before is not None:
                query["before"] = before
            page = self.account.call("GET", f"/v1/orgs/{segment(self.org)}/attention?{urlencode(query)}")
            items = page.get("items")
            if not isinstance(items, list):
                raise PlatformError("hub_refused", reason="response_invalid")
            for item in items:
                row = _item(self.org, category, item)
                if row is None:
                    self.unverified += 1
                else:
                    found.append(row)
            following = page.get("next_before")
            if not items or not isinstance(following, str) or not following or following == before:
                break
            before = following
        return found[:limit]

    def title(self, wg: str, task: str) -> str | None:
        if wg not in self.titles:
            self.titles[wg] = titles(self.account, self.org, wg)
        return self.titles[wg].get(task)

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
            "title": self.title(wg, task_id),
            **{key: attention.get(key) for key in ATTENTION_KEYS},
        }
        if question and attention.get("reason") in ASKING:
            found["question"] = self.question(wg, task_id, attention)
        return found

    def summary(self) -> dict[str, Any]:
        answer = self.account.call("GET", f"/v1/orgs/{segment(self.org)}/attention/summary")
        if not all(type(answer.get(key)) is int for key in SUMMARY_KEYS):
            raise PlatformError("hub_refused", reason="response_invalid")
        return {key: answer[key] for key in SUMMARY_KEYS}


def task_list(account: Account, org: str, state: str, *, mine: bool, limit: int) -> dict[str, Any]:
    """One category of the org's situation view with titles (and the questions of `needs-decision`)."""
    category = CATEGORIES.get(state)
    if category is None or not 1 <= limit <= LIST_MAX:
        raise PlatformError("invalid_argument")
    reader = Reader(account, _org(account, org))
    rows = reader.rows(category, mine=mine, limit=limit)
    tasks = [reader.entry(task, attention, question=category == "needs_decision") for task, attention in rows]
    return {"org_id": org, "category": category, "tasks": tasks, "unverified": reader.unverified}


def _status_of(account: Account, org: str) -> dict[str, Any]:
    reader = Reader(account, org)
    summary = reader.summary()
    groups = {
        category: [
            reader.entry(task, attention, question=True)
            for task, attention in reader.rows(category, mine=False, limit=STATUS_MAX)
        ]
        for category in ("needs_decision", "blocked")
    }
    return {"org_id": org, "name": reader.names.org, "summary": summary, **groups, "unverified": reader.unverified}


def status(account: Account, org: str | None) -> dict[str, Any]:
    """The counters and every `needs_decision` and `blocked` entry of one org, or of every org of the user."""
    if org is not None:
        return {"attention": [_status_of(account, _org(account, org))]}
    orgs = [str(item.get("org_id")) for item in account.me().get("orgs", [])]
    return {"attention": [_status_of(account, found) for found in orgs]}
