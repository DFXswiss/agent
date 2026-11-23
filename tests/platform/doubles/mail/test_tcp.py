"""U-DBL-mail: the line server underneath SMTP and IMAP."""

from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from agent_doubles.mail import tcp
from agent_doubles.mail.tcp import Connection, LineServer

from .conftest import Mail, Wire


def test_an_overlong_line_ends_only_that_connection(
    mail: Mail, wire: Callable[..., Wire], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tcp, "MAX_LINE", 16)
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    client.line()
    client.send(b"NOOP " + b"x" * 32 + b"\r\n")
    assert client.closed()
    other = wire(mail.double.smtp.host, mail.double.smtp.port)
    assert other.line().startswith("220")


def test_the_base_connection_has_no_protocol() -> None:
    left, right = socket.socketpair()
    server = LineServer(Connection, "127.0.0.1", 0)
    try:
        with pytest.raises(NotImplementedError):
            Connection(left, server).serve()
    finally:
        server.stop()
        left.close()
        right.close()


def test_a_failed_tls_handshake_ends_only_that_connection(mail: Mail, wire: Callable[..., Wire]) -> None:
    assert mail.double.imaps is not None
    client = wire(mail.double.imaps.host, mail.double.imaps.port)
    client.send(b"a LOGIN plain text\r\n")
    try:
        ended = client.closed()
    except ConnectionResetError:
        ended = True
    assert ended
    assert mail.double.ca is not None
    tls = wire(mail.double.imaps.host, mail.double.imaps.port, context=mail.double.ca.client_context())
    assert tls.line().startswith("* OK")
