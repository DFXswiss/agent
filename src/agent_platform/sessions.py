"""Tasks and sessions beyond chat (11-agent-access.md section 8.4; twins of `web/src/core/chat/actions.ts`,
`operations/load.ts`, `store/slices/sessions.ts`, `terminal/` and `tasks/overview.ts`).

- `task close`: `POST /v1/tasks/{task}/close {}`; the answer's row must be the task.
- `task operations`: the task's operations newest first (`GET /v1/tasks/{task}/operations`, a hub must not slip in
  another task's row) with their `operation.params`/`operation.result` events opened and verified; with
  `--operation` one operation with its transitions (`GET /v1/operations/{id}`) and its events (`ref=<operation>`).
  The rows keep the hub's `github_identity` and `triggered_by` (ADR-078 section 6); an operation with an identity also
  gets `github_account`, its name as the user can tell it (`identities.names`: `{kind: login, login}` for an own
  account, `{kind: company, org_id}` for a company account, `{kind: other}` otherwise).
- `session show`: `GET /v1/sessions/{id}` (row, transitions, usage, VM, children), the runtime (from the first
  `working` or the creation to the end, or to now while the session runs) and whether the hub reaches its device
  (`online` of `GET /v1/orgs/{org}/devices`; null without a device, when the device is not listed or the read fails).
- `session stop|interrupt`: `POST /v1/sessions/{id}/control {action}` ("End session" and "Stop" of the web).
- `session terminal`: the `session.terminal` chunks of the session (`kinds=session.terminal&session=<id>`), checked
  with the session's device as author (E7), as text: the output frames (`o`) in order, raw and without terminal
  control sequences; `next_after` continues after the last chunk read.
- `task overview`, the task dashboard (ADR-101 section 4): every task of the workgroups the user can read in
  every organization of the user (or of `--org`, or of the workgroup `--wg`; from the verified log, others are never
  asked), captured tasks and agent tasks in one list, merged in the chosen order (latest activity, creation or
  urgency), with verified title, owner (the signed author of the task's first `task.meta` of a person or an agent
  account; the hub's `owner_id` is ignored) and the ticket fields of ADR-100. Status, urgency, person and topic filter
  at the hub; `--mine` (the owner or the person of the owning agent account), `--search`, GitHub organisation and
  repository here because they are sealed. `counts` are the hub's counters per status and urgency
  (`GET /v1/orgs/{org}/tasks/counts`) summed over the organizations.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

from agent_platform import identities, tickets
from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_all, pull, rows_of, uuid_argument
from agent_platform.hub import segment
from agent_platform.orgs import find_org
from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import AGENT, USER, WG

ACTIONS = ("stop", "interrupt")
OPERATION_KINDS = "operation.params,operation.result"
TERMINAL_KIND = "session.terminal"
OPERATIONS_LIMIT = 50
TASK_PAGE = 50
ENDED_STATES = frozenset({"stopped", "failed", "suspended"})
TASK_STATES = ("scheduling", "starting", "working", "idle", "failed", "closed")
OVERVIEW_PAGES = 20
OPEN = "open"
"""The list state of `task overview` for every task state but `closed`."""
# The terminal's view without its control sequences: CSI, OSC (BEL or ST terminated), character set designations,
# other two-character escapes and C0 controls except tab and line feed.
_CONTROL = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()*+][0-9A-Za-z]"
    r"|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]"
)


def task_row(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/tasks/{segment(uuid_argument(task))}")
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task or not isinstance(row.get("wg_id"), str):
        raise unverified("response_invalid")
    return cast(dict[str, Any], row)


def task_close(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("POST", f"/v1/tasks/{segment(uuid_argument(task))}/close", {})
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task:
        raise unverified("response_invalid")
    return {"task": row}


def _operation_events(account: Account, wg: str, operation: str) -> tuple[list[dict[str, Any]], int]:
    """The verified events of one operation (`ref=<operation>`) and the number left out."""
    items, _last = pull(account, wg, {"ref": operation, "kinds": OPERATION_KINDS})
    events, skipped = open_all(account, find_org(account, wg), (WG, wg), items)
    mine = [event for event in events if event["ref"] == operation]
    return mine, skipped + len(events) - len(mine)


def task_operations(account: Account, task: str, before: str | None, operation: str | None) -> dict[str, Any]:
    if operation is not None:
        return _operation(account, task, uuid_argument(operation))
    wg = str(task_row(account, task)["wg_id"])
    query = urlencode({"limit": OPERATIONS_LIMIT, **({"before": before} if before else {})})
    answer = account.call("GET", f"/v1/tasks/{segment(task)}/operations?{query}")
    listed = []
    skipped = 0
    for item in rows_of(answer, "operations"):
        if item.get("task_id") != task or not isinstance(item.get("operation_id"), str):
            continue
        events, left_out = _operation_events(account, wg, item["operation_id"])
        listed.append({**item, "events": events})
        skipped += left_out
    _name_accounts(account, listed)
    following = answer.get("next_before")
    return {
        "task_id": task,
        "operations": listed,
        "next_before": following if isinstance(following, str) else None,
        "unverified": skipped,
    }


def _name_accounts(account: Account, rows: list[dict[str, Any]]) -> None:
    """`github_account` of every row with an identity (module docstring); the identities are read once, if needed."""
    if not any(isinstance(row.get("github_identity"), str) for row in rows):
        return
    found = identities.names(account)
    for row in rows:
        name = identities.name_of(found, row.get("github_identity"))
        if name is not None:
            row["github_account"] = name


def _operation(account: Account, task: str, operation: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/operations/{segment(operation)}")
    row = answer.get("operation")
    if not isinstance(row, dict) or row.get("operation_id") != operation or row.get("task_id") != task:
        raise unverified("response_invalid")
    events, skipped = _operation_events(account, str(task_row(account, task)["wg_id"]), operation)
    found = {**row, "events": events}
    _name_accounts(account, [found])
    return {
        "task_id": task,
        "operation": found,
        "transitions": rows_of(answer, "transitions"),
        "unverified": skipped,
    }


def session_row(account: Account, session: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/sessions/{segment(uuid_argument(session))}")
    row = answer.get("session")
    if not isinstance(row, dict) or row.get("session_id") != session or not isinstance(row.get("wg_id"), str):
        raise unverified("response_invalid")
    return answer


def runtime_seconds(start: object, end: object, running: bool, now: float) -> int | None:
    """Whole seconds from `start` to `end`, or to `now` while the object runs and has no end; None when a time is
    malformed or the end of an ended object is unknown (`sessions/runtime.ts`)."""
    if end is None and not running:
        return None
    try:
        begun = parse_time(start).timestamp()
        ended = now if end is None else parse_time(end).timestamp()
    except CryptoError:
        return None
    return max(0, math.floor(ended - begun))


def device_reachable(account: Account, org: object, device: object) -> bool | None:
    """Whether the hub reaches `device` (`online` of the org's device list), None when it is not known."""
    if not isinstance(org, str) or not isinstance(device, str):
        return None
    try:
        rows = rows_of(account.call("GET", f"/v1/orgs/{segment(org)}/devices"), "devices")
    except PlatformError:
        return None
    found = [row["online"] for row in rows if row.get("device_id") == device and isinstance(row.get("online"), bool)]
    return found[0] if found else None


def session_show(account: Account, session: str) -> dict[str, Any]:
    answer = session_row(account, session)
    row = answer["session"]
    started = row.get("first_working_at") if row.get("first_working_at") is not None else row.get("created_at")
    running = row.get("state") not in ENDED_STATES
    return {
        **{key: answer.get(key) for key in ("session", "transitions", "usage", "vm", "children")},
        "runtime_s": runtime_seconds(started, row.get("ended_at"), running, time.time()),
        "device_reachable": device_reachable(account, row.get("org_id"), row.get("device_id")),
    }


def session_control(account: Account, session: str, action: str) -> dict[str, Any]:
    account.call("POST", f"/v1/sessions/{segment(uuid_argument(session))}/control", {"action": action})
    return {"session_id": session, "action": action}


def _frames(body: object) -> list[tuple[int, str]] | None:
    """The output frames `(t_ms, data)` of a chunk body as `terminal/chunks.ts` allows it, None otherwise."""
    if not isinstance(body, dict) or not isinstance(body.get("frames"), list) or not body["frames"]:
        return None
    found: list[tuple[int, str]] = []
    for frame in body["frames"]:
        if not (isinstance(frame, list) and len(frame) == 3 and frame[1] in ("o", "m") and isinstance(frame[2], str)):
            return None
        if not isinstance(frame[0], int) or isinstance(frame[0], bool) or frame[0] < 0:
            return None
        if frame[1] == "o":
            found.append((frame[0], frame[2]))
    return found


def screen_text(output: str) -> str:
    """The output without terminal control sequences, carriage returns before line feeds folded."""
    return _CONTROL.sub("", output.replace("\r\n", "\n").replace("\r", "\n"))


def session_terminal(account: Account, session: str, after: int) -> dict[str, Any]:
    if after < 0:
        raise PlatformError("invalid_argument")
    row = session_row(account, session)["session"]
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    items, last = pull(account, wg, {"kinds": TERMINAL_KIND, "session": session}, after)
    device = row.get("device_id") if isinstance(row.get("device_id"), str) else None
    events, skipped = open_all(account, org, (WG, wg), items, device)
    output = ""
    chunks = 0
    for event in sorted(events, key=lambda found: int(found["seq"])):
        frames = _frames(event.get("body")) if event["session"] == session else None
        if frames is None:
            skipped += 0 if event.get("locked") else 1
            continue
        chunks += 1
        output += "".join(data for _t, data in sorted(frames, key=lambda frame: frame[0]))
    locked = sum(1 for event in events if event.get("locked"))
    return {
        "session_id": session,
        "state": row.get("state"),
        "chunks": chunks,
        "output": output,
        "text": screen_text(output),
        "next_after": last,
        "locked": locked,
        "unverified": skipped,
    }


# --- task overview ---


def search_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().strip()


def _wg_tasks(
    account: Account, wg: str, state: str | None, extra: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The rows of a workgroup's list, page by page (at most `OVERVIEW_PAGES`); `extra` the order and the hub's
    ticket filters (default newest activity first)."""
    rows: list[dict[str, Any]] = []
    before: str | None = None
    for _page in range(OVERVIEW_PAGES):
        query = {"limit": TASK_PAGE, "order": "activity", **({"state": state} if state else {}), **(extra or {})}
        answer = account.call("GET", f"/v1/workgroups/{segment(wg)}/tasks?{urlencode({**query, **_cursor(before)})}")
        rows += [row for row in rows_of(answer, "tasks") if row.get("wg_id") == wg]
        following = answer.get("next_before")
        if not isinstance(following, str):
            break
        before = following
    return rows


def _cursor(before: str | None) -> dict[str, str]:
    return {} if before is None else {"before": before}


def metas(account: Account, org: str, wg: str) -> dict[str, tuple[str | None, str | None]]:
    """Per task: the newest verified title and the author of the first `task.meta` written by a person or an agent
    account (a device's is skipped), as the web reads the owner (`selectOwner`)."""
    log = account.org_log(org)
    items, _last = pull(account, wg, {"kinds": "task.meta"})
    events, _skipped = open_all(account, org, (WG, wg), items)
    found: dict[str, tuple[str | None, str | None]] = {}
    for event in sorted(events, key=lambda item: int(item["seq"])):
        title, owner = found.get(event["task"], (None, None))
        body = event.get("body")
        if isinstance(body, dict) and isinstance(body.get("title"), str):
            title = body["title"]
        author = log.state.principals.get(event["author"])
        if owner is None and author is not None and author.kind in (USER, AGENT):
            owner = author.id
        found[event["task"]] = (title, owner)
    return found


@dataclass(frozen=True)
class Dashboard:
    """What `task overview` asks for (ADR-101 section 4): the place (every organization of the user, one org or
    one workgroup), the task state, "only mine" (the signed owner), the title search, the ticket filters of ADR-100
    and the order."""

    org: str | None = None
    wg: str | None = None
    state: str | None = None
    mine: bool = False
    search: str = ""
    filters: tickets.Filters | None = None
    order: str = "activity"


def _places(account: Account, query: Dashboard) -> list[tuple[str, list[str]]]:
    """Per organization the workgroups the user reads (from the verified log; the others are never asked)."""
    if query.org is not None and query.wg is not None:
        raise PlatformError("invalid_argument")
    if query.wg is not None:
        return [(find_org(account, uuid_argument(query.wg)), [query.wg])]
    orgs = [uuid_argument(query.org)] if query.org is not None else [str(o["org_id"]) for o in account.me()["orgs"]]
    found = []
    for org in orgs:
        state = account.org_log(org).state
        found.append((org, [wg for wg in sorted(state.workgroups) if state.can_read(account.user_id, WG, wg)]))
    return found


def _org_rows(account: Account, org: str, wgs: list[str], query: Dashboard) -> list[dict[str, Any]]:
    """The rows of one organization's workgroups for the query (before the filters applied here)."""
    hub = dict(parse_qsl(tickets.list_query(account, org, query.filters).lstrip("&")))
    sealed = query.filters is not None and query.filters.sealed()
    names = account.names(org).workgroups
    rows: list[dict[str, Any]] = []
    for wg in wgs:
        metas_of = metas(account, org, wg)
        ticket_meta = tickets.ticket_meta(account, org, wg) if sealed else {}
        state = None if query.state == OPEN else query.state
        listed = [
            row
            for row in _wg_tasks(account, wg, state, {**hub, "order": query.order})
            if query.state != OPEN or row.get("state") != "closed"
        ]
        topics = tickets.topics(account, org) if any(row.get("topic_id") for row in listed) else {}
        for row in listed:
            task = str(row.get("task_id"))
            if sealed and not cast(tickets.Filters, query.filters).matches(ticket_meta.get(task, {})):
                continue
            title, owner = metas_of.get(task, (None, None))
            rows.append(
                {
                    "task_id": row.get("task_id"),
                    "org_id": org,
                    "wg_id": wg,
                    "workgroup": names.get(wg),
                    "state": row.get("state"),
                    "title": title,
                    "owner_id": owner,
                    **tickets.row_fields(row, topics),
                    "created_at": row.get("created_at"),
                    "last_activity_at": row.get("last_activity_at"),
                }
            )
    return rows


def _counts(account: Account, org: str, query: Dashboard) -> dict[str, Any]:
    """The hub's counters of one organization (`GET /v1/orgs/{org}/tasks/counts`) for the hub filters."""
    params = dict(parse_qsl(tickets.list_query(account, org, query.filters).lstrip("&")))
    state = None if query.state == OPEN else query.state  # the counters cover every task state but closed alike
    params.update({**({"state": state} if state else {}), **({"wg": query.wg} if query.wg else {})})
    # no bare "?" without parameters: the signed target must be the path alone then
    target = f"/v1/orgs/{segment(org)}/tasks/counts" + (f"?{urlencode(params)}" if params else "")
    answer = account.call("GET", target)
    if not all(isinstance(answer.get(name), dict) for name in ("by_status", "by_urgency")):
        raise unverified("response_invalid")
    return answer


def _summed(found: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = {name: sum(int(item["by_status"].get(name, 0)) for item in found) for name in tickets.STATUSES}
    by_urgency = {name: sum(int(item["by_urgency"].get(name, 0)) for item in found) for name in tickets.URGENCIES}
    return {"by_status": by_status, "by_urgency": by_urgency, "total": sum(int(item.get("total", 0)) for item in found)}


def task_overview(account: Account, query: Dashboard) -> dict[str, Any]:
    """`task overview` (module docstring)."""
    if query.order not in tickets.ORDERS or query.state not in (None, OPEN, *TASK_STATES):
        raise PlatformError("invalid_argument")
    places = _places(account, query)
    merged = [row for org, wgs in places for row in _org_rows(account, org, wgs, query)]
    merged.sort(key=tickets.sort_key(query.order), reverse=True)
    wanted = search_text(query.search)

    def is_mine(row: dict[str, Any]) -> bool:
        # the owner, or the person whose agent account wrote the task (as the web's "only mine")
        owner = row["owner_id"]
        return owner is not None and account.org_log(row["org_id"]).state.acting(owner) == account.user_id

    rows = [
        row
        for row in merged
        if (not query.mine or is_mine(row))
        and (not wanted or (row["title"] is not None and wanted in search_text(row["title"])))
    ]
    return {
        "org_id": query.org,
        "organizations": [org for org, _wgs in places],
        "workgroups": [wg for _org, wgs in places for wg in wgs],
        "counts": _summed([_counts(account, org, query) for org, _wgs in places]),
        "tasks": rows,
    }


# --- commands ---


def _close(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return task_close(account, args.task)


def _operations(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return task_operations(account, args.task, args.before, args.operation)


def dashboard_filters(args: Any) -> tickets.Filters | None:
    """The ticket filters of the arguments (None without one)."""
    found = tickets.Filters(
        status=args.status,
        urgency=args.urgency,
        assignee=args.assignee,
        topic=args.topic,
        github_org=args.github_org,
        github_repo=args.repo,
    )
    return None if found == tickets.Filters() else found


def _overview(account: Account, args: Any, context: Context) -> dict[str, Any]:
    query = Dashboard(
        org=args.org,
        wg=args.wg,
        state=args.state,
        mine=args.mine,
        search=args.search,
        filters=dashboard_filters(args),
        order=args.order or "activity",
    )
    return task_overview(account, query)


def _show(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return session_show(account, args.session)


def _terminal(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return session_terminal(account, args.session, args.after)


def _control(action: str) -> Any:
    def work(account: Account, args: Any, context: Context) -> dict[str, Any]:
        return session_control(account, args.session, action)

    return work


# `session stop` and `session interrupt`: example and search words.
CONTROL_DOCS = {
    "stop": (
        "agent-platform session stop --session <session_id>",
        "anhalten stoppen beenden sitzung laufende kill end terminate stop running",
    ),
    "interrupt": (
        "agent-platform session interrupt --session <session_id>",
        "unterbrechen abbrechen sitzung turn cancel pause",
    ),
}


def register(tree: Tree) -> None:
    group = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.command(
        group,
        "close",
        _close,
        "close a task (its live sessions stop)",
        example="agent-platform task close --task <task_id>",
        words="schliessen abschliessen abbrechen erledigt aufgabe beenden finish done end cancel stop task",
    )
    sub.add_argument("--task", required=True)
    sub = tree.command(
        group,
        "operations",
        _operations,
        "the task's operations with their verified events",
        example="agent-platform task operations --task <task_id> [--operation <operation_id>]",
        words="vorgänge ergebnisse pullrequests aufgabe pushes pr push results",
        trim="first",
    )
    sub.add_argument("--task", required=True)
    sub.add_argument("--operation", help="one operation with its transitions")
    sub.add_argument("--before", help="next_before of the previous page")
    sub = tree.command(
        group,
        "overview",
        _overview,
        "the task dashboard: every task the user reads in all organizations (or one), filtered, sorted, counted",
        example='agent-platform task overview [--org <org_id>] [--state working] [--mine] [--search "deploy"]',
        words=(
            "gesamtübersicht dashboard alle aufgaben organisationen suchen zuständig prio priorität dringlichkeit"
            " status zähler sortieren all tasks organizations responsible priority urgency counts sort search"
        ),
        then="agent-platform task show --task <task_id>",
        trim="first",
    )
    place = sub.add_mutually_exclusive_group()
    place.add_argument("--org", help="only this organization (default every organization of the user)")
    place.add_argument("--wg", help="only this workgroup")
    sub.add_argument("--state", choices=[*TASK_STATES, OPEN], help="a task state, or open (every state but closed)")
    sub.add_argument("--mine", action="store_true", help="only tasks the user created")
    sub.add_argument("--search", default="", help="part of the title")
    sub.add_argument("--status", choices=tickets.STATUSES, help="only tasks with this status")
    sub.add_argument("--urgency", choices=tickets.URGENCIES, help="only tasks with this urgency (prio)")
    sub.add_argument("--assignee", help="only tasks of this person (a user ID, me, or none)")
    sub.add_argument("--topic", help="only tasks of this topic (from `topic list`, or none)")
    sub.add_argument("--github-org", help="only tasks of this GitHub organisation")
    sub.add_argument("--repo", help="only tasks of this repository (owner/name)")
    sub.add_argument("--order", choices=tickets.ORDERS, help="activity (default), created or urgency")
    group = tree.group("session", "sessions of tasks")
    sub = tree.command(
        group,
        "show",
        _show,
        "a session with transitions, usage, VM and children",
        example="agent-platform session show --session <session_id>",
        words="sitzung details übergänge nutzung detail usage transitions",
        then="agent-platform session terminal --session <session_id>",
    )
    sub.add_argument("--session", required=True)
    for action, text in (("stop", "end a session"), ("interrupt", "interrupt the session's current turn")):
        sub = tree.command(
            group, action, _control(action), text, example=CONTROL_DOCS[action][0], words=CONTROL_DOCS[action][1]
        )
        sub.add_argument("--session", required=True)
    sub = tree.command(
        group,
        "terminal",
        _terminal,
        "the session's terminal output as text",
        example="agent-platform session terminal --session <session_id> [--after <next_after>]",
        words="konsole ausgabe bildschirm sitzung output console screen log",
        trim="last",
    )
    sub.add_argument("--session", required=True)
    sub.add_argument("--after", type=int, default=0, help="next_after of the previous call (0: from the start)")
