"""The code of a sign-in mail (KOR-73, ADR-098 section 4): when the provider's page asks for the code from the mail
and the mail carries no sign-in link, the automation types the code instead.

Same mailbox rules as the link of the legacy `agent_cli.account_login.newest_magic_link` (whose helpers it reuses,
the legacy module stays untouched): IMAPS with certificate verification, INBOX and then the spam folder read-only,
the sender filter, at most `MAX_MESSAGES` newest messages per folder of at most `MAX_MESSAGE_BYTES`, nothing dated
before `since` (minus the slack); the code of the later dated message wins. A code is six digits within 40 characters
after the word "code" in a text part. The code is never logged.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
import ssl
from collections.abc import Callable
from email import message_from_bytes
from email import policy as email_policy
from email.message import Message
from typing import Any, cast

from agent_cli.account_login import (
    DATE_SLACK_SECONDS,
    IMAP_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    Mailbox,
    _junk_folder,
    _message_bytes,
    _message_time,
)
from agent_cli.store import StoreError

CODE_RE = re.compile(r"code[^0-9]{0,40}?\b([0-9]{6})\b", re.IGNORECASE)


def code_in(message: Message) -> str | None:
    """The code of the first text part that has one."""
    for part in message.walk():
        if part.get_content_type() not in ("text/html", "text/plain"):
            continue
        payload = cast(bytes, part.get_payload(decode=True))  # a text part always decodes to bytes
        found = CODE_RE.search(re.sub(r"<[^>]{0,200}>", " ", payload.decode("latin-1")))
        if found is not None:
            return found.group(1)
    return None


def _newest(conn: Any, since: float, mail_from: str) -> tuple[float | None, str] | None:
    status, found = conn.search(None, "FROM", f'"{mail_from}"')
    if status != "OK":
        raise StoreError("mailbox search failed")
    ids = found[0].split() if found and isinstance(found[0], bytes) else []
    for message_id in reversed(ids[-MAX_MESSAGES:]):
        status, fetched = conn.fetch(message_id, f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)")
        if status != "OK":
            raise StoreError("mailbox fetch failed")
        raw = _message_bytes(fetched)
        if raw is None or len(raw) > MAX_MESSAGE_BYTES:
            continue
        message = message_from_bytes(raw, policy=email_policy.compat32)
        sent = _message_time(message)
        if sent is not None and sent < since - DATE_SLACK_SECONDS:
            return None
        code = code_in(message)
        if code is not None:
            return sent, code
    return None


def newest_code(
    mailbox: Mailbox, since: float, *, mail_from: str, imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL
) -> str | None:
    """The code of the newest matching sign-in mail sent after `since` (module docstring), None when there is none."""
    conn = imap_factory(
        mailbox.host, mailbox.port, ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT_SECONDS
    )
    try:
        conn.login(mailbox.user, mailbox.password)
        status, _ = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise StoreError("cannot open INBOX")
        best = _newest(conn, since, mail_from)
        junk = _junk_folder(conn)
        if junk is not None and conn.select(junk, readonly=True)[0] == "OK":
            other = _newest(conn, since, mail_from)
            if other is not None and (best is None or (other[0] or 0.0) > (best[0] or 0.0)):
                best = other
        return None if best is None else best[1]
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()
