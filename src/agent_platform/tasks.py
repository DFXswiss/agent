"""Tasks of the agent tool (stage c, 11-agent-access.md section 8.4; twin of `web/src/core/tasks/`): `task list`,
`task create`, `task prompt`, `task show` and `task follow`.

The tool seals what the web client seals: a new task is `POST /v1/tasks` with the events `task.meta` (`{"title"}`,
derived from the prompt as `web/src/core/tasks/title.ts`, vectors `contracts/tasks/title.json`) and `prompt`
(`{"attachments": [], "text"}`), a further prompt is `POST /v1/tasks/{task}/prompts`. Events are sealed for the
workgroup under its current epoch (the key from the user's own wraps, checked against the verified org log) and
signed by the user's identity. Reading opens the task's events of the workgroup scope after the reader checks of
`check_event` (author, kind rule, signature, write right); an event whose key the user does not hold is `locked`, one
that does not verify is left out and counted. The chat includes the work VM events of the task's sessions
(`workvm.run`, `workvm.state`); `task show` keeps only the latest `workvm.run` of a job, as the web's timeline. After
a lost answer (`hub_unreachable`) `task list` shows whether the task exists; the tool does not resend on its own.
"""

from __future__ import annotations

import re
import time
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.orgs import find_org, now_text
from agent_platform.workvm import latest_runs
from agent_runner.crypto.encoding import b64u_decode, body_json, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import new_header, open_event, seal_event
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.events import check_event
from agent_runner.crypto.log.state import WG

PROMPT_MAX = 65536
TITLE_MAX = 80
PAGE = 1000
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
    """One event of `kind` for the task `place` = (org, workgroup, task), sealed and signed."""
    org, wg, task = place
    log = account.org_log(org)
    epoch = log.state.epoch(WG, wg)
    key = account.keys(org).get((WG, wg, epoch))
    if key is None:
        raise PlatformError("hub_refused", reason="epoch_stale")
    header = new_header(
        id=uuid7(),
        org=org,
        scope=WG,
        scope_id=wg,
        epoch=epoch,
        author=account.user_id,
        kind=kind,
        ts=now_text(),
        lh=log.head.seq,
        wg=wg,
        task=task,
    )
    return seal_event(key, header, body_json(body), account.identity.sig)


def task_create(account: Account, wg: str, text: str) -> dict[str, Any]:
    """A new task with its first prompt in workgroup `wg`."""
    org = find_org(account, _id(wg))
    prompt = _prompt(text)
    task = uuid7()
    events = [
        _seal(account, (org, wg, task), "task.meta", {"title": derive_title(prompt)}),
        _seal(account, (org, wg, task), "prompt", {"attachments": [], "text": prompt}),
    ]
    body = {"task_id": task, "wg_id": wg, "events": events}
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
    }


def _task_row(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/tasks/{segment(_id(task))}")
    row = answer.get("task")
    if not isinstance(row, dict) or row.get("task_id") != task or not is_uuid7(str(row.get("wg_id"))):
        raise PlatformError("hub_refused", reason="response_invalid")
    return cast(dict[str, Any], row)


def task_prompt(account: Account, task: str, text: str) -> dict[str, Any]:
    """A further prompt to the task's session."""
    row = _task_row(account, task)
    wg = str(row["wg_id"])
    event = _seal(account, (find_org(account, wg), wg, task), "prompt", {"attachments": [], "text": _prompt(text)})
    answer = account.call("POST", f"/v1/tasks/{segment(task)}/prompts", {"event": event})
    return {"task_id": task, "scope_seq": answer.get("scope_seq"), "session_id": answer.get("session_id")}


def _events(account: Account, wg: str, query: dict[str, str], after: int = 0) -> list[Any]:
    found: list[Any] = []
    while True:
        target = f"/v1/scopes/{segment(wg)}/events?{urlencode({**query, 'limit': PAGE})}&after={after}"
        page = account.call("GET", target)
        items = page.get("events")
        if not isinstance(items, list):
            raise PlatformError("hub_refused", reason="response_invalid")
        found.extend(items)
        following = page.get("next_after")
        if not isinstance(following, int) or following <= after:
            return found
        after = following


def _open(account: Account, org: str, wg: str, item: object) -> dict[str, Any] | None:
    """The verified event as `{id, seq, kind, author, ts, task, body|locked}`, or None if it does not verify. The key
    is the requested workgroup's: an event of another scope does not open and is left out."""
    log = account.org_log(org)
    try:
        event = cast(dict[str, Any], item)["event"]
        header, _rule = check_event(event, log)
        found: dict[str, Any] = {
            "id": header["id"],
            "seq": cast(dict[str, Any], item).get("scope_seq"),
            "kind": header["kind"],
            "author": header["author"],
            "ts": header["ts"],
            "task": header["task"],
        }
        key = account.keys(org).get((WG, wg, cast(int, header["epoch"])))
        if key is None:
            return {**found, "locked": True}
        author = log.state.principals[cast(str, header["author"])]
        return {**found, "body": parse_json(open_event(key, event, b64u_decode(author.sig)))}
    except (CryptoError, KeyError, TypeError):
        return None


def task_show(account: Account, task: str) -> dict[str, Any]:
    """The task row and its chat: every event of the task, opened and verified."""
    row = _task_row(account, task)
    wg = str(row["wg_id"])
    org = find_org(account, wg)
    events: list[dict[str, Any]] = []
    skipped = 0
    for item in _events(account, wg, {"kinds": ",".join(("task.meta", *CHAT_KINDS)), "task": task}):
        opened = _open(account, org, wg, item)
        if opened is None or opened["task"] != task:
            skipped += 1
            continue
        events.append(opened)
    titles = [e["body"].get("title") for e in events if e["kind"] == "task.meta" and isinstance(e.get("body"), dict)]
    chat = latest_runs([e for e in events if e["kind"] != "task.meta"])
    return {
        "task": {key: row.get(key) for key in ("task_id", "org_id", "wg_id", "owner_id", "state", "state_reason")},
        "title": titles[-1] if titles else None,
        "events": chat,
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
    for item in _events(account, wg, {"kinds": "task.meta"}):
        opened = _open(account, org, wg, item)
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
        items = _events(account, wg, query, after)
        if items or time.monotonic() >= deadline:
            break
        time.sleep(FOLLOW_POLL_S)
    events: list[dict[str, Any]] = []
    skipped = 0
    last = after
    for item in items:
        seq = item.get("scope_seq") if isinstance(item, dict) else None
        last = max(last, seq) if isinstance(seq, int) else last
        opened = _open(account, org, wg, item)
        if opened is None or opened["task"] != task:
            skipped += 1
            continue
        events.append(opened)
    state = _task_row(account, task).get("state")
    return {"task_id": task, "state": state, "events": events, "next_after": last, "unverified": skipped}
