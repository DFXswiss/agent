"""Executor of `mail.find` (ADR-099 sections 4 to 7; 05-runner.md section 12.2; `<area>/executors.py:EXECUTORS`).

The device reads the mailbox itself - no work VM, no browser VM, no slot: the session names a mailbox released to its
workgroup (`mailboxes.choose`), the runner opens the record on the device and looks into the mailbox over IMAP
(`reader.find`, in a thread: any number of operations read any number of mailboxes at once).

- **Waiting** (`wait_seconds` > 0): it looks every `POLL_SECONDS` until a mail matches or the wait ends; only mails
  that arrived at or after `since` count - without `since`, at or after `RECENT_SECONDS` before the request (a mail the
  session triggered just before it asked counts). No match in time is no failure (`timed_out: true`).
- **Answer**: `{mailbox {id, name, email}, messages, waited_seconds, timed_out}`; the texts of the oldest answered mails
  are left out (`text_truncated`) until the answer is at most `MAX_ANSWER_BYTES`.
- **Audit** (section 7): every outcome writes `mail.read` through the outbox (target the mailbox) with the operation,
  the owner, the folder, the number of mails, the seconds waited, whether the wait ran out and the result - never an
  address, subject, text, link or the password.
- **Failures**: `not_found` (no such mailbox released here, or the mail of `id` is gone), `upstream_error` (the server
  refused the sign-in or a command), `timeout` (the server did not answer in time; retryable), `hub_unreachable`.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_runner.mail import mailboxes, reader
from agent_runner.operations.extensions import OperationContext, OperationFailed

POLL_SECONDS = 5.0
RECENT_SECONDS = 120
MAX_ANSWER_BYTES = 200 * 1024
DEFAULT_LIMIT = 5
REFUSED = "The mailbox server refused the sign-in; its owner checks the mailbox in the settings under Mailboxes."
COMMAND = "The mailbox server refused a command; try again later."
SLOW = "The mailbox server did not answer in time; try again later."
GONE = "There is no such mail in this mailbox anymore."
TLS = "The mailbox server's certificate was not accepted; its owner checks the IMAP host in the settings."
BROKEN = "The mailbox could not be read; try again later."
imap_factory: reader.ImapFactory | None = None
"""Tests replace the IMAP connection factory (None: `imaplib.IMAP4_SSL`)."""


def _time(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def query(params: Any, now: datetime) -> reader.Query:
    """The look a request asks for (module docstring)."""
    since = _time(params.get("since"))
    if since is None and params.get("wait_seconds", 0) > 0:
        since = now - timedelta(seconds=RECENT_SECONDS)
    uid = None
    if "id" in params:
        folder, number = str(params["id"]).split(":")
        uid = (folder, int(number))
    return reader.Query(
        folder=params.get("folder", "all"),
        sender=params.get("from"),
        subject=params.get("subject"),
        since=since,
        until=_time(params.get("until")),
        limit=params.get("limit", DEFAULT_LIMIT),
        body=bool(params.get("body", False)),
        uid=uid,
    )


def _time_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _message(found: reader.Message) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": found.id,
        "folder": found.folder,
        "from": found.sender,
        "to": found.to,
        "subject": found.subject,
        "date": _time_text(found.date),
        "size": found.size,
    }
    if found.text is not None:
        item["text"] = found.text
        item["links"] = found.links or []
        if found.text_truncated:
            item["text_truncated"] = True
    return item


def answer(mailbox: mailboxes.Mailbox, found: list[reader.Message], waited: int, timed_out: bool) -> dict[str, Any]:
    """The result object, within `MAX_ANSWER_BYTES`."""
    messages = [_message(item) for item in found]
    result = {
        "mailbox": {"id": mailbox.id, "name": mailbox.name, "email": mailbox.email},
        "messages": messages,
        "waited_seconds": waited,
        "timed_out": timed_out,
    }
    for item in reversed(messages):
        if len(json.dumps(result).encode()) <= MAX_ANSWER_BYTES:
            break
        if item.get("text") or item.get("links"):
            item["text"], item["links"], item["text_truncated"] = "", [], True
    return result


async def _audit(ctx: OperationContext, mailbox: mailboxes.Mailbox, detail: dict[str, Any]) -> None:
    op, env = ctx.operation, ctx.env
    row = {
        "action": "mail.read",
        "result": "ok" if detail["result"] == "ok" else "failed",
        "task_id": op.task_id,
        "session_id": op.session_id,
        "target_kind": "credential",
        "target_id": mailbox.id,
        "detail": {
            "operation_id": op.id,
            "owner_id": mailbox.owner_id,
            "folder": ctx.params.get("folder", "all"),
            **detail,
        },
    }
    async with env.transaction() as conn:
        await env.outbox.enqueue(conn, "audit", row)


async def _look(server: reader.Server, look: reader.Query) -> list[reader.Message]:
    if imap_factory is None:
        return await asyncio.to_thread(reader.find, server, look)
    return await asyncio.to_thread(reader.find, server, look, imap_factory=imap_factory)


async def find(ctx: OperationContext) -> dict[str, Any]:
    env, params = ctx.env, ctx.params
    mailbox = mailboxes.choose(await mailboxes.released(ctx.context, ctx.session), str(params["mailbox"]))
    look = query(params, datetime.fromtimestamp(env.wall(), UTC))
    wait = int(params.get("wait_seconds", 0))
    start = env.clock()
    deadline = start + wait
    found: list[reader.Message] = []
    outcome, failure = "ok", None
    try:
        while True:
            found = await _look(mailbox.server, look)
            if found or env.clock() >= deadline:
                break
            await asyncio.sleep(min(POLL_SECONDS, max(deadline - env.clock(), 0.0)))
    except reader.Refused as refused:
        outcome = "upstream_error"
        failure = OperationFailed("upstream_error", message=REFUSED if refused.login else COMMAND)
    except ssl.SSLError:
        outcome = "upstream_error"
        failure = OperationFailed("upstream_error", message=TLS)
    except (TimeoutError, OSError):
        outcome = "timeout"
        failure = OperationFailed("timeout", retryable=True, message=SLOW)
    except Exception:
        outcome = "upstream_error"
        failure = OperationFailed("upstream_error", message=BROKEN)
    if failure is None and look.uid is not None and not found:
        outcome, failure = "not_found", OperationFailed("not_found", message=GONE)
    waited = min(int(env.clock() - start), 330)
    timed_out = failure is None and not found and wait > 0
    await _audit(
        ctx, mailbox, {"count": len(found), "waited_seconds": waited, "timed_out": timed_out, "result": outcome}
    )
    if failure is not None:
        raise failure
    return answer(mailbox, found, waited, timed_out)


EXECUTORS: dict[str, Callable[..., Any]] = {"mail.find": find}
