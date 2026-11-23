"""Fixtures of the mail double suites: one runtime CA per session, started doubles, a raw protocol client."""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA

USER = "user@example.test"
PASSWORD = "mail-double-password"  # not a secret: the double only compares it


class FakeClock:
    """A settable clock (seconds since the epoch); 2026-11-21 12:00:00 UTC by default."""

    def __init__(self, now: float = 1_795_262_400.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="session")
def mail_ca(tmp_path_factory: pytest.TempPathFactory) -> DoubleCA:
    """One runtime CA for the session (creating keys takes a few hundred milliseconds)."""
    return DoubleCA.create(tmp_path_factory.mktemp("mail-ca"))


@dataclass
class Mail:
    """A started mail double, its control client and its clock."""

    double: MailDouble
    client: DoubleClient
    clock: FakeClock

    def control(self, method: str, path: str, body: Any = None) -> Any:
        return self.client.request(method, f"/_double/{path}", body)


@pytest.fixture
def start_mail(
    serve: Callable[[MailDouble], MailDouble], client_for: Callable[[MailDouble], DoubleClient], mail_ca: DoubleCA
) -> Callable[..., Mail]:
    """Start a mail double (session CA unless `ca`/`tls` are given) with the account `USER`."""

    def start(*, account: bool = True, **kwargs: Any) -> Mail:
        clock = FakeClock()
        kwargs.setdefault("ca", mail_ca)
        double = serve(MailDouble(clock=clock, **kwargs))
        if account:
            double.add_account(USER, PASSWORD)
        return Mail(double, client_for(double), clock)

    return start


@pytest.fixture
def mail(start_mail: Callable[..., Mail]) -> Mail:
    return start_mail()


class Wire:
    """A raw line client for SMTP and IMAP edge cases."""

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext | None = None) -> None:
        sock = socket.create_connection((host, port), timeout=10)
        self.sock: socket.socket = context.wrap_socket(sock, server_hostname=host) if context else sock
        self.reader = self.sock.makefile("rb")

    def line(self) -> str:
        return self.reader.readline().decode()

    def send(self, data: str | bytes) -> None:
        self.sock.sendall(data if isinstance(data, bytes) else data.encode())

    def command(self, text: str) -> str:
        """Send one SMTP line and return the complete (possibly multi-line) reply."""
        self.send(text + "\r\n")
        return self.reply()

    def reply(self) -> str:
        lines = [self.line()]
        while len(lines[-1]) > 3 and lines[-1][3] == "-":
            lines.append(self.line())
        return "".join(lines)

    def imap(self, text: str, tag: str | None = None) -> list[str]:
        """Send one IMAP command; every line up to and including the tagged completion."""
        self.send(text + "\r\n")
        return self.until(tag or text.split(" ", 1)[0])

    def until(self, tag: str) -> list[str]:
        lines: list[str] = []
        while True:
            line = self.line()
            assert line, lines
            lines.append(line)
            if line.startswith(tag + " "):
                return lines

    def closed(self) -> bool:
        return self.reader.read() == b""

    def close(self) -> None:
        self.reader.close()
        self.sock.close()


@pytest.fixture
def wire() -> Iterator[Callable[..., Wire]]:
    opened: list[Wire] = []

    def connect(host: str, port: int, **kwargs: Any) -> Wire:
        client = Wire(host, port, **kwargs)
        opened.append(client)
        return client

    yield connect
    for client in opened:
        client.close()


@pytest.fixture
def imap_session(mail: Mail, wire: Callable[..., Wire]) -> Callable[..., Wire]:
    """A logged-in raw IMAP session on the plain port (optionally with a selected folder)."""

    def open_session(select: str | None = None, *, examine: bool = False) -> Wire:
        client = wire(mail.double.imap.host, mail.double.imap.port)
        assert client.line().startswith("* OK [CAPABILITY IMAP4rev1]")
        assert client.imap(f"a LOGIN {USER} {PASSWORD}")[-1].startswith("a OK")
        if select is not None:
            verb = "EXAMINE" if examine else "SELECT"
            assert client.imap(f"s {verb} {select}")[-1].startswith("s OK")
        return client

    return open_session
