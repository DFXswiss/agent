"""U-DBL-mail: SMTP reception (smtplib and raw protocol edge cases)."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from typing import Any

import pytest

from agent_doubles.mail import smtp as smtp_module
from agent_doubles.mail.message import compose
from agent_doubles.mail.store import StoreError

from .conftest import USER, Mail, Wire


def _send(mail: Mail, raw: bytes, to: list[str], sender: str = "login@auth.double.test") -> dict[str, Any]:
    with smtplib.SMTP(mail.double.smtp.host, mail.double.smtp.port, timeout=10) as client:
        return client.sendmail(sender, to, raw)


def test_smtplib_delivery_to_inbox_and_junk(mail: Mail) -> None:
    raw = compose("Login <login@auth.double.test>", [USER], "Sign in", when=0, text="hello\n.dot line\n")
    assert _send(mail, raw, [USER]) == {}
    assert _send(mail, compose("x@y.test", [USER], "spam", when=0, text="x", spam=True), [USER]) == {}
    assert mail.double.store.counts() == {USER: {"INBOX": 1, "Junk": 1}}
    stored = mail.double.store.account(USER).folders["INBOX"].messages[0]  # type: ignore[union-attr]
    assert b"\r\n.dot line\r\n" in stored.raw
    assert stored.sender == "login@auth.double.test"
    data = [r for r in mail.double.recorder.journal.records() if r["path"] == "DATA"]
    assert data[0]["body"]["delivered"] == [{"user": USER, "folder": "INBOX", "uid": 1}]
    assert data[1]["body"]["delivered"] == [{"user": USER, "folder": "Junk", "uid": 1}]
    assert mail.double.state()["ok"] is True


def test_unknown_recipients_are_refused_without_failure(mail: Mail) -> None:
    raw = compose("a@x.test", [USER], "s", when=0, text="x")
    assert _send(mail, raw, [USER, "nobody@example.test"]) == {"nobody@example.test": (550, b"5.1.1 No such user")}
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        _send(mail, raw, ["nobody@example.test"])
    assert mail.double.recorder.failures() == []


def test_protocol_errors_are_failures(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    assert client.line().startswith("220 ")
    assert client.command("MAIL FROM:<a@x.test>").startswith("503")
    assert client.command("EHLO").startswith("501")
    assert client.command("HELO client.test") == "250 mail.double.test\r\n"
    assert client.command("RCPT TO:<u@example.test>").startswith("503")
    assert client.command("DATA").startswith("503")
    assert client.command("MAIL TO:<a@x.test>").startswith("501")
    assert client.command("MAIL FROM:a@x.test").startswith("501")
    assert client.command("MAIL FROM:<a@x.test> SIZE=10") == "250 OK\r\n"
    assert client.command("MAIL FROM:<b@x.test>").startswith("503")
    assert client.command("RCPT TO:<>").startswith("501")
    assert client.command("RSET") == "250 OK\r\n"
    assert client.command("NOOP") == "250 OK\r\n"
    assert client.command("VRFY someone").startswith("252")
    assert client.command("STARTTLS").startswith("500")
    assert client.command("").startswith("500")
    assert client.command("X-1").startswith("500")
    assert client.command("QUIT").startswith("221")
    assert client.closed()
    failures = mail.double.recorder.failures()
    assert len(failures) == 11
    assert "smtp MAIL: MAIL out of sequence" in failures
    assert "smtp STARTTLS: unknown command" in failures
    assert "smtp (empty): unknown command" in failures


def test_raw_data_with_bare_newlines_and_eof(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    client.line()
    assert client.command("EHLO client.test").startswith("250-mail.double.test\r\n250-8BITMIME\r\n250 SIZE")
    client.command("MAIL FROM:<a@x.test>")
    client.command(f"RCPT TO:<{USER}>")
    assert client.command("DATA").startswith("354")
    client.send(b"Subject: bare\nFrom: a@x.test\n\nline\n..leading dot\n.\n")
    assert client.reply() == "250 OK queued\r\n"
    stored = mail.double.store.account(USER).folders["INBOX"].messages[0]  # type: ignore[union-attr]
    assert stored.raw == b"Subject: bare\r\nFrom: a@x.test\r\n\r\nline\r\n.leading dot\r\n"
    # A client that disconnects inside DATA stores nothing.
    client.command("MAIL FROM:<a@x.test>")
    client.command(f"RCPT TO:<{USER}>")
    client.command("DATA")
    client.send(b"Subject: cut\r\n")
    client.sock.shutdown(2)
    client.close()
    other = wire(mail.double.smtp.host, mail.double.smtp.port)
    other.line()
    assert other.command("NOOP") == "250 OK\r\n"
    assert mail.double.store.counts()[USER]["INBOX"] == 1


def test_oversized_message_is_refused(mail: Mail, wire: Callable[..., Wire], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtp_module, "MAX_MESSAGE", 20)
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    client.line()
    client.command("HELO c.test")
    client.command("MAIL FROM:<a@x.test>")
    client.command(f"RCPT TO:<{USER}>")
    client.command("DATA")
    client.send(b"Subject: x\r\n\r\n" + b"y" * 40 + b"\r\n.\r\n")
    assert client.reply().startswith("552")
    assert mail.double.store.counts()[USER]["INBOX"] == 0
    assert client.command("MAIL FROM:<a@x.test>") == "250 OK\r\n"


def test_store_errors_after_rcpt(mail: Mail, wire: Callable[..., Wire], monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("account vanished")

    monkeypatch.setattr(mail.double.store, "deliver", refuse)
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    client.line()
    client.command("HELO c.test")
    client.command("MAIL FROM:<a@x.test>")
    client.command(f"RCPT TO:<{USER}>")
    client.command("DATA")
    client.send(b"Subject: x\r\n\r\n.\r\n")
    assert client.reply().startswith("554")
    assert mail.double.recorder.failures() == ["smtp DATA: account vanished"]


def test_end_of_stream_before_quit(mail: Mail, wire: Callable[..., Wire]) -> None:
    client = wire(mail.double.smtp.host, mail.double.smtp.port)
    client.line()
    client.sock.shutdown(1)
    assert client.closed()
