"""The SMTP side of the mail double: receives messages (RFC 5321 subset) and delivers them into the store.

Commands: ``EHLO``/``HELO``, ``MAIL FROM:<addr> [params]``, ``RCPT TO:<addr>``, ``DATA`` (dot-unstuffed, at most
`MAX_MESSAGE` bytes), ``RSET``, ``NOOP``, ``VRFY`` (``252``) and ``QUIT``. No authentication and no relaying: a
recipient without an account is refused with ``550`` (a legitimate test case, not a failure). Commands out of order
are ``503``, unknown commands ``500`` and invalid arguments ``501``; these three are failures of the double's state,
because the clients under test (``smtplib`` in fixtures, the auth-page double) never send them.
"""

from __future__ import annotations

import re
import socket
from typing import TYPE_CHECKING

from agent_doubles.mail.store import MailStore, StoreError
from agent_doubles.mail.tcp import Connection

if TYPE_CHECKING:
    from agent_doubles.mail.record import Recorder
    from agent_doubles.mail.tcp import LineServer

MAX_MESSAGE = 10 * 1024 * 1024
HOSTNAME = "mail.double.test"
_PATH = re.compile(r"<([^<>\s]*)>(?:\s+(.*))?", re.DOTALL)


class SMTPConnection(Connection):
    """One SMTP session."""

    def __init__(self, sock: socket.socket, server: LineServer, *, store: MailStore, recorder: Recorder) -> None:
        super().__init__(sock, server)
        self.store = store
        self.recorder = recorder
        self.greeted = False
        self.sender: str | None = None
        self.recipients: list[str] = []

    def reply(self, code: int, text: str, command: str, body: object = None, *, failure: str | None = None) -> None:
        lines = text.split("\n")
        data = "".join(f"{code}-{line}\r\n" for line in lines[:-1]) + f"{code} {lines[-1]}\r\n"
        # Record first: a client that read the reply finds the command in the journal.
        self.recorder.record("SMTP", command, body, result=str(code)[0] + "xx", status=code, note=failure)
        if failure is not None:
            self.recorder.fail(f"smtp {command}: {failure}")
        self.write(data.encode())

    def serve(self) -> None:
        self.write(f"220 {HOSTNAME} ESMTP mail double ready\r\n".encode())
        while True:
            line = self.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, argument = text.partition(" ")
            verb = verb.upper()
            if verb == "QUIT":
                self.reply(221, "Bye", "QUIT")
                return
            handler = getattr(self, f"cmd_{verb.lower()}", None) if verb.isalpha() else None
            if handler is None:
                self.reply(500, "Command not recognized", verb or "(empty)", failure="unknown command")
                continue
            handler(argument.strip())

    def cmd_ehlo(self, argument: str) -> None:
        self._greet("EHLO", argument, f"{HOSTNAME}\n8BITMIME\nSIZE {MAX_MESSAGE}")

    def cmd_helo(self, argument: str) -> None:
        self._greet("HELO", argument, HOSTNAME)

    def _greet(self, verb: str, argument: str, text: str) -> None:
        if not argument:
            self.reply(501, "Syntax: EHLO hostname", verb, failure="greeting without a name")
            return
        self.greeted = True
        self._reset()
        self.reply(250, text, verb, argument)

    def _reset(self) -> None:
        self.sender = None
        self.recipients = []

    def cmd_rset(self, argument: str) -> None:
        self._reset()
        self.reply(250, "OK", "RSET")

    def cmd_noop(self, argument: str) -> None:
        self.reply(250, "OK", "NOOP")

    def cmd_vrfy(self, argument: str) -> None:
        self.reply(252, "Cannot verify user", "VRFY", argument)

    def _path(self, argument: str, keyword: str) -> str | None:
        if argument[: len(keyword)].upper() != keyword:
            return None
        found = _PATH.fullmatch(argument[len(keyword) :].strip())
        return None if found is None else found.group(1)

    def cmd_mail(self, argument: str) -> None:
        if not self.greeted or self.sender is not None:
            self.reply(503, "Bad sequence of commands", "MAIL", argument, failure="MAIL out of sequence")
            return
        sender = self._path(argument, "FROM:")
        if sender is None:
            self.reply(501, "Syntax: MAIL FROM:<address>", "MAIL", argument, failure="invalid MAIL argument")
            return
        self.sender = sender
        self.reply(250, "OK", "MAIL", {"from": sender})

    def cmd_rcpt(self, argument: str) -> None:
        if self.sender is None:
            self.reply(503, "Bad sequence of commands", "RCPT", argument, failure="RCPT before MAIL")
            return
        recipient = self._path(argument, "TO:")
        if not recipient:
            self.reply(501, "Syntax: RCPT TO:<address>", "RCPT", argument, failure="invalid RCPT argument")
            return
        if self.store.account(recipient) is None:
            self.reply(550, "5.1.1 No such user", "RCPT", {"to": recipient}, failure=None)
            return
        self.recipients.append(recipient)
        self.reply(250, "OK", "RCPT", {"to": recipient})

    def cmd_data(self, argument: str) -> None:
        if not self.recipients:
            self.reply(503, "Bad sequence of commands", "DATA", failure="DATA without recipients")
            return
        self.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
        lines: list[bytes] = []
        size = 0
        while True:
            line = self.readline()
            if not line:
                return
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b"."):
                line = line[1:]
            size += len(line)
            if size <= MAX_MESSAGE:
                lines.append(line if line.endswith(b"\r\n") else line.rstrip(b"\n") + b"\r\n")
        sender, recipients = self.sender or "", self.recipients
        self._reset()
        if size > MAX_MESSAGE:
            self.reply(552, "Message size exceeds fixed maximum message size", "DATA", {"size": size})
            return
        raw = b"".join(lines)
        try:
            delivered = self.store.deliver(raw, sender=sender, recipients=recipients)
        except StoreError as exc:
            self.reply(554, "Transaction failed", "DATA", {"size": size}, failure=str(exc))
            return
        stored = [{"user": user, "folder": folder, "uid": uid} for user, folder, uid in delivered]
        body = {"from": sender, "size": size, "delivered": stored}
        self.reply(250, "OK queued", "DATA", body)
