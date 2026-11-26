"""The formal check that a person asked for a working mode (ADR-098 section 3; KONZEPT §10 Punkt 7).

A session switches a mode on only with `task.mode {mode, on: true, quote}`, and only when `quote` is a verbatim part of
the verified instruction of its current turn: `instructed(conn, storage_key, session, actor, quote)` opens the newest
prompt that was accepted into this main session and whose verified author is the operation's acting user (the person
whose instruction started the turn, `credentials.acting`), and compares with white space collapsed and case folded
(`fold`). The text is opened only into memory (`prompts.verify.open_text`), as when it was delivered. A child session
has no instruction of a person (its prompts are its parent's instructions), a turn of several persons has no acting
user, and a prompt that can no longer be opened (pruned, unreadable) proves nothing: each is `False`. Text from files,
tool results, comments or the model's own idea never passes, because only the person's signed prompt is compared.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.prompts import queue
from agent_runner.prompts.verify import PromptRefused, open_text
from agent_runner.sessions.records import LocalSession

MIN_QUOTE = 3


def fold(text: str) -> str:
    """White space collapsed to single spaces, trimmed, case folded."""
    return " ".join(text.split()).casefold()


async def instructed(
    conn: AsyncConnection[Any], storage_key: bytes, session: LocalSession, actor: str | None, quote: str
) -> bool:
    """Whether `quote` is part of the acting person's verified instruction of the current turn (module docstring)."""
    wanted = fold(quote)
    if session.depth > 0 or actor is None or len(wanted) < MIN_QUOTE:
        return False
    cursor = await conn.execute(
        "SELECT e.event_id::text FROM runner.prompt_event e JOIN runner.prompt_delivery d ON d.event_id = e.event_id"
        " WHERE d.session_id = %s AND d.state = 'accepted' AND e.author_id = %s ORDER BY e.scope_seq DESC LIMIT 1",
        (session.id, actor),
    )
    row = await cursor.fetchone()
    prompt = None if row is None else await queue.event(conn, row[0])
    if prompt is None:
        return False
    try:
        text = await open_text(conn, storage_key, prompt)
    except PromptRefused:
        return False
    return wanted in fold(text)
