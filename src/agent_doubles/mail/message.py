"""Composing test e-mails (control-route deliveries of the mail double, magic-link mails of the auth-page double)."""

from __future__ import annotations

from collections.abc import Sequence
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

DOMAIN = "mail.double.test"


def compose(
    sender: str,
    to: Sequence[str],
    subject: str,
    *,
    when: float,
    text: str | None = None,
    html: str | None = None,
    date: str | None = None,
    spam: bool = False,
) -> bytes:
    """An RFC 5322 message with CRLF line ends: plain text, HTML or both (``multipart/alternative``).

    ``Date`` is `date` when given, else `when` in RFC 2822 form; ``X-Spam-Flag: YES`` marks it as spam.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message["Date"] = date if date is not None else formatdate(when, usegmt=True)
    message["Message-ID"] = make_msgid(domain=DOMAIN)
    if spam:
        message["X-Spam-Flag"] = "YES"
    if text is not None:
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    else:
        message.set_content("")
    return message.as_bytes(policy=policy.SMTP)
