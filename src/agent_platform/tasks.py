"""Tasks of the agent tool (stage c, 11-agent-access.md section 8.4; twin of `web/src/core/tasks/`): `task list`,
`task create`, `task prompt`, `task show`, `task follow` and `task queue`. An answer to an approval (KOR-39, ADR-076
section 8) is a prompt whose first line is exactly `Approved.` or `Declined.`, followed by a blank line and the
comment when one is given (`answer_text`, the same text the web client sends).

The tool seals what the web client seals: a new task is `POST /v1/tasks` with the events `task.meta` (`{"title"}`,
derived from the prompt as `web/src/core/tasks/title.ts`, vectors `contracts/tasks/title.json`) and `prompt`
(`{"attachments": [], "text"}`) and, when one is chosen, the task's GitHub identity (`github_identity`, ADR-078 section
3; without it the identity is automatic), a further prompt is `POST /v1/tasks/{task}/prompts`. Events are sealed for the
workgroup under its current epoch (`eventio.seal`: the key from the user's own wraps, checked against the verified
org log; refused while the workgroup's rotation is owed) and signed by the user's identity. Reading opens the task's
events of the workgroup scope with the one reader (`reader.settle`); an event whose key the user does not hold is
`locked`, one that does not verify is left out and counted. The chat includes the work VM events of the task's
sessions (`workvm.run`, `workvm.state`) and the errors the guest reported (`session.error`); `task show` keeps only
the latest `workvm.run` of a job, as the web's timeline, and both `task show` and `task follow` fold the waiting events
of held operations into one card per reason with its state (`waiting`). `task show` gives the task's runtime (from its
creation to its close, or to now while it is open). After a lost answer (`hub_unreachable`) `task list` shows whether
the task exists; the
tool does not resend on its own.

ADR-075: `task create --device [--vm]` sends the chosen device and VM (`placement`; without one the workgroup's
default or the automatic choice applies), and the task is created even when no device has room (it waits
`scheduling` with a reason). Without a choice, an admin's task in a workgroup without an assigned device first
assigns the first certified platform candidate (`ensure_device`, rule E-05, ADR-079; `assigned_device` in the answer),
as the web client does. `task show` prints the device and VM of the main session and, while the task waits, its
reason (`waiting`, a code of the closed list of `diagnose`).

The queue (ADR-083 section 3; twin of `web/src/core/chat/queue.ts`): `task queue` lists the task's prompts not yet
delivered to a live main session in event order (`GET /v1/tasks/{task}/queue`) - position, prompt event, author,
whether the hub received it from the author's agent (`via_agent`), holding session and device, state (`waiting`,
`waiting_for_device`, `withdrawing`) - and the task's withdrawals with their outcome (`withdrawn`, `withdrawing`,
`too_late`). `task prompt --cancel EVENT` withdraws one's own waiting prompt
(`POST /v1/tasks/{task}/prompts/{event}/withdraw`): `withdrawn` at once, or `withdrawing` until the session's device
decides; a device that cannot decide yet is `peer_outdated`.
"""

from __future__ import annotations

import re
import time
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, KeyRing
from agent_platform.diagnose import WAITING, task_next
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_item, seal
from agent_platform.hub import segment
from agent_platform.orgs import VM_ID, device_assign, find_org
from agent_platform.reader import scope_events
from agent_platform.sessions import runtime_seconds
from agent_platform.waiting import annotate
from agent_platform.workvm import latest_runs
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.state import WG

PROMPT_MAX = 65536
TITLE_MAX = 80
FOLLOW_POLL_S = 2.0
FOLLOW_WAIT_MAX_S = 600
STATES = ("open", "closed")
# `task list`: at most this many tasks; `open` pages through at most OPEN_PAGES pages of the hub.
LIST_LIMIT = 50
OPEN_PAGES = 10
DECISIONS = {"approve": "Approved.", "decline": "Declined."}
CHAT_KINDS = (
    "prompt",
    "session.output",
    "session.tool",
    "session.file",
    "session.hook",
    "session.status",
    "session.error",
    "operation.params",
    "operation.result",
    "workvm.run",
    "workvm.state",
)
_WHITESPACE = "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x85\u2028\u2029]")
_SPACES = re.compile(f"[{_WHITESPACE}]+")


def derive_title(prompt: str) -> str:
    """The title of a task from its first prompt (`contracts/tasks/title.json`)."""
    for line in _LINE_BREAK.split(prompt):
        text = _SPACES.sub(" ", line)
        text = text[1:] if text.startswith(" ") else text
        text = text[:-1] if text.endswith(" ") else text
        if text:
            return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1] + "…"
    return ""


def _prompt(text: str) -> str:
    if not 1 <= len(text) <= PROMPT_MAX or not text.strip():
        raise PlatformError("invalid_argument")
    return text


def answer_text(decision: str | None, text: str | None) -> str:
    """The prompt text: the text itself, or for `decision` (`approve`/`decline`) its first line plus the comment."""
    if decision is None:
        if text is None:
            raise PlatformError("invalid_argument")
        return text
    first = DECISIONS[decision]
    return first if text is None or not text.strip() else f"{first}\n\n{text}"


def _id(value: str) -> str:
    if not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def _seal(account: Account, place: tuple[str, str, str], kind: str, body: dict[str, object]) -> dict[str, object]:
    """One event of `kind` for the task `place` = (org, workgroup, task), sealed and signed (`eventio.seal`)."""
    org, wg, task = place
    return seal(account, org, (WG, wg), kind, body, wg=wg, task=task)


def _placement(device: str | None, vm: str | None) -> dict[str, str] | None:
    if device is None:
        if vm is not None:
            raise PlatformError("invalid_argument")
        return None
    if not is_uuid7(device) or (vm is not None and not VM_ID.fullmatch(vm)):
        raise PlatformError("invalid_argument")
    return {"device_id": device} if vm is None else {"device_id": device, "vm_id": vm}


def ensure_device(account: Account, org: str, wg: str) -> str | None:
    """Rule E-05 (ADR-079 section 4): an admin's task in a workgroup without an assigned device first assigns the first
    certified platform candidate, as the web client does before sending (`ensureDevice`); the device, or None when one
    is assigned already, the user is no admin or none can be assigned (the task then waits and the admins are asked)."""
    state = account.org_log(org).state
    if any(state.assigned(principal, wg) for principal in state.principals) or not state.is_admin(account.user_id):
        return None
    try:
        return str(device_assign(account, wg)["device_id"])
    except PlatformError as error:
        # nothing to assign: no candidate, no platform access, or no platform log yet (the hub has none: `not_found`)
        if error.code == "no_device" or error.fields.get("reason") == "platform_access_denied":
            return None
        if error.fields.get("hub_error") == "not_found":
            return None
        raise


def task_create(
    account: Account,
    wg: str,
    text: str,
    device: str | None = None,
    vm: str | None = None,
    *,
    github_identity: str | None = None,
) -> dict[str, Any]:
    """A new task with its first prompt in workgroup `wg` (on the chosen device and VM and with the chosen GitHub
    identity, if any; without a device choice an admin's workgroup without a device gets one first, `ensure_device`)."""
    org = find_org(account, _id(wg))
    prompt = _prompt(text)
    placement = _placement(device, vm)
    chosen = {} if github_identity is None else {"github_identity": _id(github_identity)}
    assigned = ensure_device(account, org, wg) if placement is None else None
    task = uuid7()
    events = [
        _seal(account, (org, wg, task), "task.meta", {"title": derive_title(prompt)}),
        _seal(account, (org, wg, task), "prompt", {"attachments": [], "text": prompt}),
    ]
    body: dict[str, object] = {"task_id": task, "wg_id": wg, "events": events, **chosen}
    if placement is not None:
        body["placement"] = placement
    try:
        answer = account.call("POST", "/v1/tasks", body)
    except PlatformError as error:
        if error.fields.get("hub_error") == "no_device":
            raise PlatformError("no_device") from None
        raise
    session = answer.get("session")
    return {
        "org_id": org,
        "wg_id": wg,
        "task_id": task,
        "title": derive_title(prompt),
        "session_id": session.get("session_id") if isinstance(session, dict) else None,
        "placement": placement,
        **chosen,
        "assigned_device": assigned,
    }


def task_answer(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/tasks/{segment(_id(task))}")
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task or not is_uuid7(str(row.get("wg_id"))):
        raise PlatformError("hub_refused", reason="response_invalid")
    return answer


def _task_row(account: Account, task: str) -> dict[str, Any]:
    return cast(dict[str, Any], task_answer(account, task)["task"])


def main_session(answer: dict[str, Any]) -> dict[str, Any] | None:
    """Device, VM and state of the task's main session (`main_session_id` among `sessions`), None without one."""
    wanted = answer["task"].get("main_session_id")
    sessions = answer.get("sessions")
    for item in sessions if isinstance(sessions, list) else []:
        session = item.get("session") if isinstance(item, dict) else None
        if isinstance(session, dict) and wanted is not None and session.get("session_id") == wanted:
            return {key: session.get(key) for key in ("session_id", "state", "device_id", "vm_id")}
    return None


def waiting_reason(row: dict[str, Any]) -> str | None:
    """The reason a `scheduling` task waits (a code of `diagnose.WAITING`; anything else is `task_waiting`)."""
    if row.get("state") != "scheduling":
        return None
    reason = row.get("state_reason")
    return reason if isinstance(reason, str) and reason in WAITING else "task_waiting"


def task_prompt(account: Account, task: str, text: str) -> dict[str, Any]:
    """A further prompt to the task's session."""
    row = _task_row(account, task)
    wg = str(row["wg_id"])
    event = _seal(account, (find_org(account, wg), wg, task), "prompt", {"attachments": [], "text": _prompt(text)})
    answer = account.call("POST", f"/v1/tasks/{segment(task)}/prompts", {"event": event})
    return {"task_id": task, "scope_seq": answer.get("scope_seq"), "session_id": answer.get("session_id")}


QUEUE_FIELDS = ("event_id", "author_id", "via_agent", "state", "scope_seq", "received_at", "session_id", "device_id")
WITHDRAWAL_FIELDS = ("event_id", "state", "requested_at")
WITHDRAW_STATES = ("withdrawn", "withdrawing")


def _objects(value: object) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def task_queue(account: Account, task: str) -> dict[str, Any]:
    """The task's waiting prompts in delivery order and its withdrawals (module docstring)."""
    answer = account.call("GET", f"/v1/tasks/{segment(_id(task))}/queue")
    if not isinstance(answer.get("prompts"), list):
        raise PlatformError("hub_refused", reason="response_invalid")
    prompts = [
        {"position": position, **{key: row.get(key) for key in QUEUE_FIELDS}}
        for position, row in enumerate(_objects(answer["prompts"]), start=1)
    ]
    withdrawals = [{key: row.get(key) for key in WITHDRAWAL_FIELDS} for row in _objects(answer.get("withdrawals"))]
    return {"task_id": task, "prompts": prompts, "withdrawals": withdrawals}


def prompt_withdraw(account: Account, task: str, event: str) -> dict[str, Any]:
    """Withdraws the waiting prompt `event` of the task (only its author may)."""
    path = f"/v1/tasks/{segment(_id(task))}/prompts/{segment(_id(event))}/withdraw"
    state = account.call("POST", path, {}).get("state")
    if state not in WITHDRAW_STATES:
        raise PlatformError("hub_refused", reason="response_invalid")
    return {"task_id": task, "event_id": event, "state": state}


def _open(account: Account, org: str, wg: str, item: object, ring: KeyRing) -> dict[str, Any] | None:
    """The verified event as `{id, seq, kind, author, ts, task, body|locked}`, or None if it does not verify or
    belongs to another scope (`eventio.open_item`)."""
    opened = open_item(account, org, (WG, wg), item, ring=ring)
    if opened is None:
        return None
    found = {key: opened[key] for key in ("id", "seq", "kind", "author", "ts", "task")}
    return {**found, "locked": True} if opened.get("locked") else {**found, "body": opened["body"]}


def open_verified(account: Account, org: str, wg: str, item: object) -> dict[str, Any] | None:
    """`_open` with the user's key ring of the org (the situation view of `agent_platform.attention`)."""
    return _open(account, org, wg, item, account.keys(org))


def task_show(account: Account, task: str) -> dict[str, Any]:
    """The task row and its chat: every event of the task, opened and verified; where its main session runs."""
    answer = task_answer(account, task)
    row = cast(dict[str, Any], answer["task"])
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    events: list[dict[str, Any]] = []
    skipped = 0
    ring = account.keys(org)
    for item in scope_events(account, wg, {"kinds": ",".join(("task.meta", *CHAT_KINDS)), "task": task}):
        opened = _open(account, org, wg, item, ring)
        if opened is None or opened["task"] != task:
            skipped += 1
            continue
        events.append(opened)
    metas = [e for e in events if e["kind"] == "task.meta"]
    titles = [e["body"].get("title") for e in metas if isinstance(e.get("body"), dict)]
    chat = latest_runs([e for e in events if e["kind"] != "task.meta"])
    keys = ("task_id", "org_id", "wg_id", "owner_id", "state", "state_reason", "created_at", "closed_at")
    return {
        "task": {key: row.get(key) for key in keys},
        "runtime_s": runtime_seconds(row.get("created_at"), row.get("closed_at"), True, time.time()),
        "placement": row.get("placement"),
        "main_session": main_session(answer),
        "waiting": waiting_reason(row),
        "next": task_next(row),
        "title": titles[-1] if titles else None,
        "events": annotate(account, (org, wg, task), chat, metas),
        "unverified": skipped,
    }


def titles(account: Account, org: str, wg: str) -> dict[str, str]:
    """The verified titles of the workgroup's tasks (the newest `task.meta` of each task wins)."""
    found: dict[str, str] = {}
    ring = account.keys(org)
    for item in scope_events(account, wg, {"kinds": "task.meta"}):
        opened = _open(account, org, wg, item, ring)
        if opened is not None and isinstance(opened.get("body"), dict):
            found[str(opened["task"])] = str(opened["body"].get("title", ""))
    return found


def _rows(account: Account, wg: str, query: dict[str, Any]) -> tuple[list[Any], Any]:
    """One page of the workgroup's tasks (newest first) and the hub's `next_before`."""
    answer = account.call("GET", f"/v1/workgroups/{segment(wg)}/tasks?{urlencode({'limit': LIST_LIMIT, **query})}")
    rows = answer.get("tasks")
    if not isinstance(rows, list):
        raise PlatformError("hub_refused", reason="response_invalid")
    return rows, answer.get("next_before")


def _open_rows(account: Account, wg: str) -> list[Any]:
    """The newest tasks that are not closed: the hub filters by one state of a task's sessions only, `open` is every
    state but `closed` (KOR-67), so the tool pages and filters (at most `OPEN_PAGES` pages)."""
    found: list[Any] = []
    query: dict[str, Any] = {}
    for _page in range(OPEN_PAGES):
        rows, before = _rows(account, wg, query)
        found += [row for row in rows if isinstance(row, dict) and row.get("state") != "closed"]
        if len(found) >= LIST_LIMIT or not isinstance(before, str):
            break
        query = {"before": before}
    return found[:LIST_LIMIT]


def task_list(account: Account, wg: str, state: str | None) -> dict[str, Any]:
    """The tasks of a workgroup with their verified titles."""
    if state is not None and state not in STATES:
        raise PlatformError("invalid_argument")
    org = find_org(account, _id(wg))
    rows = _open_rows(account, wg) if state == "open" else _rows(account, wg, {"state": state} if state else {})[0]
    found = titles(account, org, wg)
    tasks = [
        {
            "task_id": row.get("task_id"),
            "state": row.get("state"),
            "title": found.get(str(row.get("task_id"))),
            "created_at": row.get("created_at"),
            "last_activity_at": row.get("last_activity_at"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return {"org_id": org, "wg_id": wg, "tasks": tasks}


def task_follow(account: Account, task: str, after: int, wait: int) -> dict[str, Any]:
    """The task's chat events after the scope position `after`, waiting up to `wait` seconds for the first one.

    The tool prints one JSON object per command, so following is a loop of calls: each returns the new events (opened
    and verified like `task show`) and `next_after`, the position to pass to the next call, and the task's state.
    """
    if after < 0 or not 0 <= wait <= FOLLOW_WAIT_MAX_S:
        raise PlatformError("invalid_argument")
    row = _task_row(account, task)
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    deadline = time.monotonic() + wait
    query = {"kinds": ",".join(CHAT_KINDS), "task": task}
    while True:
        items = scope_events(account, wg, query, after)
        if items or time.monotonic() >= deadline:
            break
        time.sleep(FOLLOW_POLL_S)
    events: list[dict[str, Any]] = []
    skipped = 0
    last = after
    ring = account.keys(org)
    for item in items:
        seq = item.get("scope_seq") if isinstance(item, dict) else None
        last = max(last, seq) if isinstance(seq, int) else last
        opened = _open(account, org, wg, item, ring)
        if opened is None or opened["task"] != task:
            skipped += 1
            continue
        events.append(opened)
    now = _task_row(account, task)
    chat = annotate(account, (org, wg, task), events, None)
    return {
        "task_id": task,
        "state": now.get("state"),
        "next": task_next(now),
        "events": chat,
        "next_after": last,
        "unverified": skipped,
    }
