"""Tasks of the agent tool (stage c, 11-agent-access.md section 8.4; twin of `web/src/core/tasks/`): `task list`,
`task create`, `task prompt`, `task show` and `task follow`.

The tool seals what the web client seals: a new task is `POST /v1/tasks` with the events `task.meta` (`{"title"}`,
derived from the prompt as `web/src/core/tasks/title.ts`, vectors `contracts/tasks/title.json`) and `prompt`
(`{"attachments": [], "text"}`), a further prompt is `POST /v1/tasks/{task}/prompts`. Events are sealed for the
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
`scheduling` with a reason). `task show` prints the device and VM of the main session and, while the task waits, its
reason (`waiting`, a code of the closed list of `diagnose`).
"""

from __future__ import annotations

import re
import time
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, KeyRing
from agent_platform.diagnose import WAITING
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_item, seal
from agent_platform.hub import segment
from agent_platform.orgs import VM_ID, find_org
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


def task_create(
    account: Account, wg: str, text: str, device: str | None = None, vm: str | None = None
) -> dict[str, Any]:
    """A new task with its first prompt in workgroup `wg` (on the chosen device and VM, if any)."""
    org = find_org(account, _id(wg))
    prompt = _prompt(text)
    placement = _placement(device, vm)
    task = uuid7()
    events = [
        _seal(account, (org, wg, task), "task.meta", {"title": derive_title(prompt)}),
        _seal(account, (org, wg, task), "prompt", {"attachments": [], "text": prompt}),
    ]
    body: dict[str, object] = {"task_id": task, "wg_id": wg, "events": events}
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
    }


def _task_answer(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/tasks/{segment(_id(task))}")
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task or not is_uuid7(str(row.get("wg_id"))):
        raise PlatformError("hub_refused", reason="response_invalid")
    return answer


def _task_row(account: Account, task: str) -> dict[str, Any]:
    return cast(dict[str, Any], _task_answer(account, task)["task"])


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


def _open(account: Account, org: str, wg: str, item: object, ring: KeyRing) -> dict[str, Any] | None:
    """The verified event as `{id, seq, kind, author, ts, task, body|locked}`, or None if it does not verify or
    belongs to another scope (`eventio.open_item`)."""
    opened = open_item(account, org, (WG, wg), item, ring=ring)
    if opened is None:
        return None
    found = {key: opened[key] for key in ("id", "seq", "kind", "author", "ts", "task")}
    return {**found, "locked": True} if opened.get("locked") else {**found, "body": opened["body"]}


def task_show(account: Account, task: str) -> dict[str, Any]:
    """The task row and its chat: every event of the task, opened and verified; where its main session runs."""
    answer = _task_answer(account, task)
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
        "title": titles[-1] if titles else None,
        "events": annotate(account, (org, wg, task), chat, metas),
        "unverified": skipped,
    }


def task_list(account: Account, wg: str, state: str | None) -> dict[str, Any]:
    """The tasks of a workgroup with their verified titles."""
    if state is not None and state not in STATES:
        raise PlatformError("invalid_argument")
    org = find_org(account, _id(wg))
    query = urlencode({"limit": 50, **({"state": state} if state else {})})
    answer = account.call("GET", f"/v1/workgroups/{segment(wg)}/tasks?{query}")
    rows = answer.get("tasks")
    if not isinstance(rows, list):
        raise PlatformError("hub_refused", reason="response_invalid")
    titles: dict[str, str] = {}
    ring = account.keys(org)
    for item in scope_events(account, wg, {"kinds": "task.meta"}):
        opened = _open(account, org, wg, item, ring)
        if opened is not None and isinstance(opened.get("body"), dict):
            titles[str(opened["task"])] = str(opened["body"].get("title", ""))
    tasks = [
        {
            "task_id": row.get("task_id"),
            "state": row.get("state"),
            "title": titles.get(str(row.get("task_id"))),
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
    state = _task_row(account, task).get("state")
    chat = annotate(account, (org, wg, task), events, None)
    return {"task_id": task, "state": state, "events": chat, "next_after": last, "unverified": skipped}
