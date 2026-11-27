"""Verification of a prompt before it reaches a session (05-runner.md sections 2.2 step 6 and 2.4, 04-crypto.md section
6.4, 03-api.md sections 4.6.3 and 10; ADR-016).

A prompt is delivered only when **all** of these hold:

1. E1-E5 with the author's right at `lh`: the inbox (`inbox/pull.py`) checks the exact header, `alg`/`v`, the kind
   for scope and author kind (a `prompt` is authored by a user in a workgroup scope), the author's signature and the
   right `wg:write` of the author at `lh` in the verified membership log (refreshed for an `lh` beyond the verified
   head, L10). E6 for readers: the event opens with a scope key of its epoch that the device holds (`open_text`).
   Only events that passed are handed to the consumer of this area and stored (`queue.keep`).
2. Task and session relation (`check_route`): the prompt addresses a task (`session`, `ref` empty; checked by the
   consumer) and the hub routes it to a **main** session of this device of exactly that task, workgroup and org,
   with the event ID at the `scope_seq` the hub names. The hub only routes: it cannot make a session of another task
   or workgroup read a prompt.
3. Not stale (`deliver.Deliverer`): no later prompt of the task was handed to a guest yet (strict event order), and
   the event was not delivered before (final delivery rows, the hub's delivered list after a wipe).
4. `open_prompt`: the stored event is opened again with the author's verified key and the scope key (signature and
   decryption into memory only); the body is `{text, attachments}` with a non-empty text and at most ten checked
   attachment references (ADR-102; the files themselves are fetched and checked by `prompts.attachments`).

A failed check means: not delivered, audited `runner.inbound_rejected` (source `event`, metadata only) and counted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.crypto.attachment import check_refs
from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.orgs.keys import load_scope_key
from agent_runner.prompts.queue import StoredPrompt
from agent_runner.sessions.records import LocalSession

MISROUTED = "prompt_misrouted"
STALE = "prompt_stale"
UNREADABLE = "prompt_unreadable"


class PromptRefused(Exception):
    """A prompt that must not be delivered; `code` is the audit code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def addresses_task(header: Mapping[str, object]) -> bool:
    """A prompt event addresses its task (03-api.md section 10: header `session` and `ref` are empty)."""
    return header.get("session") == "" and header.get("ref") == "" and bool(header.get("task"))


def check_route(
    prompt: StoredPrompt, session: LocalSession | None, *, task_id: str, scope_id: str, scope_seq: int
) -> None:
    """The hub's routing `{task_id, session_id, scope_id, scope_seq, event_id}` fits the stored event and a main
    session of this device; `PromptRefused(prompt_misrouted)` otherwise."""
    fits = (
        session is not None
        and session.depth == 0
        and prompt.task_id == task_id == session.task_id
        and prompt.wg_id == scope_id == session.wg_id
        and prompt.org_id == session.org_id
        and prompt.scope_seq == scope_seq
    )
    if not fits:
        raise PromptRefused(MISROUTED)


@dataclass(frozen=True)
class Opened:
    """A verified prompt: its text and its checked attachment references (ADR-102; empty for text only)."""

    text: str
    attachments: tuple[dict[str, object], ...] = ()


async def open_text(conn: AsyncConnection[Any], storage_key: bytes, prompt: StoredPrompt) -> str:
    """The prompt's text (`open_prompt`)."""
    return (await open_prompt(conn, storage_key, prompt)).text


async def open_prompt(conn: AsyncConnection[Any], storage_key: bytes, prompt: StoredPrompt) -> Opened:
    """The prompt's text and attachment references, opened into memory (signature with the stored verified author
    key, scope key of the header's epoch); `PromptRefused(prompt_unreadable)` when that fails or the body is not a
    prompt body (a text that is empty or only whitespace, attachments that are not a list of at most ten distinct
    references of `agent_runner.crypto.attachment.check_refs`)."""
    header = prompt.sealed.get("header") if isinstance(prompt.sealed, Mapping) else None
    if not isinstance(header, Mapping) or header.get("id") != prompt.event_id or not addresses_task(header):
        raise PromptRefused(UNREADABLE)
    epoch = header.get("epoch")
    if type(epoch) is not int:
        raise PromptRefused(UNREADABLE)
    try:
        key = await load_scope_key(conn, storage_key, "wg", prompt.wg_id, epoch)
        if key is None:
            raise PromptRefused(UNREADABLE)
        body = parse_json(open_event(key, prompt.sealed, b64u_decode(prompt.author_sig)))
    except (CryptoError, KeyError, TypeError, ValueError):
        raise PromptRefused(UNREADABLE) from None
    text = body.get("text") if isinstance(body, dict) else None
    # the length is bounded by E8 (ciphertext at most 256 KiB), below the guest's 262144 characters
    if not isinstance(text, str) or not text.strip():
        raise PromptRefused(UNREADABLE)
    try:
        refs = check_refs(cast(dict[str, object], body).get("attachments", []))
    except CryptoError:
        raise PromptRefused(UNREADABLE) from None
    return Opened(text=text, attachments=tuple(refs))
