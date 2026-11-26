"""U-RUN-mail (ADR-099 sections 4 and 6): one look into a mailbox over IMAPS against the mail double - INBOX and the
spam folder (by its `\\Junk` flag, else by its name), sender, subject and arrival filters, the newest first, text and
links of a mail (plain or HTML only), one mail by its ID, nothing marked read, the bounds, a refused sign-in."""

from __future__ import annotations

import imaplib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_doubles.mail.double import MailDouble
from agent_runner.mail import reader
from agent_runner.mail.reader import Query, Server

from .kit import PASSWORD

USER = "signup-1@example.org"


def _server(double: MailDouble, password: str = PASSWORD) -> Server:
    assert double.imaps is not None
    return Server("127.0.0.1", double.imaps.port, USER, password)


def _started(mail: Callable[[], MailDouble], **options: Any) -> MailDouble:
    double = mail()
    double.add_account(USER, PASSWORD, **options)
    return double


def _send(double: MailDouble, **body: Any) -> None:
    double.deliver({"to": USER, "from": "Sign-in <no-reply@mail.example.com>", "subject": "Your sign-in link", **body})


def test_the_newest_mails_of_inbox_and_spam_with_filters(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail)
    _send(double, text="first https://example.com/one")
    _send(double, subject="Newsletter", text="no link")
    double.deliver(
        {"to": USER, "from": "Promo <a@spam.example>", "subject": "Your sign-in link", "text": "x", "spam": True}
    )
    found = sorted(reader.find(_server(double), Query()), key=lambda item: item.id)
    assert [(item.id, item.folder, item.subject) for item in found] == [
        ("inbox:1", "inbox", "Your sign-in link"),
        ("inbox:2", "inbox", "Newsletter"),
        ("spam:1", "spam", "Your sign-in link"),
    ]
    assert found[0].sender == "Sign-in <no-reply@mail.example.com>"
    assert found[0].to == USER
    assert found[2].date is not None
    assert found[2].size > 0
    assert found[2].text is None
    assert [item.id for item in reader.find(_server(double), Query(folder="inbox", subject="sign-in"))] == ["inbox:1"]
    assert [item.id for item in reader.find(_server(double), Query(folder="spam"))] == ["spam:1"]
    assert [item.id for item in reader.find(_server(double), Query(sender="mail.example.com", limit=1))] == ["inbox:2"]
    future = datetime.now(UTC) + timedelta(hours=1)
    assert reader.find(_server(double), Query(since=future)) == []
    assert reader.find(_server(double), Query(until=future - timedelta(days=3))) == []
    # nothing was marked read
    found = reader.find(_server(double), Query(body=True))
    assert all(item.text is not None for item in found)
    assert all(
        not message.flags
        for account in double.store._accounts.values()
        for folder in account.folders.values()
        for message in folder.messages
    )


def test_text_and_links_of_plain_html_and_one_mail(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail, junk_attribute=False, junk_name="Junk")
    _send(double, text="Sign in: https://example.com/magic-link#abc. Or https://example.com/magic-link#abc")
    _send(
        double,
        html='<p>Hello</p><script>x()</script><img src="a.png"><a href="">empty</a>'
        '<a href="https://example.com/html-link">here</a><br>bye',
    )
    double.deliver(
        {"to": USER, "from": "a@spam.example", "subject": "junk", "text": "http://spam.example/x", "spam": True}
    )
    found = reader.find(_server(double), Query(folder="all", body=True))
    by_id = {item.id: item for item in found}
    assert by_id["inbox:1"].text == "Sign in: https://example.com/magic-link#abc. Or https://example.com/magic-link#abc"
    assert by_id["inbox:1"].links == ["https://example.com/magic-link#abc"]
    assert by_id["inbox:2"].links == ["https://example.com/html-link"]
    assert "Hello" in (by_id["inbox:2"].text or "")
    assert "x()" not in (by_id["inbox:2"].text or "")
    assert by_id["spam:1"].links == ["http://spam.example/x"]  # the spam folder found by its name
    one = reader.find(_server(double), Query(uid=("inbox", 2)))
    assert [item.id for item in one] == ["inbox:2"]
    assert one[0].text is not None
    assert reader.find(_server(double), Query(uid=("inbox", 99))) == []


def test_a_plain_and_html_mail_takes_the_text_and_both_links(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail)
    _send(double, text="plain https://example.com/a", html='<a href="https://example.com/b">b</a>')
    (found,) = reader.find(_server(double), Query(body=True))
    assert found.text == "plain https://example.com/a"
    assert found.links == ["https://example.com/a", "https://example.com/b"]


def test_bounds_of_text_links_and_size(mail: Callable[[], MailDouble], monkeypatch: pytest.MonkeyPatch) -> None:
    double = _started(mail)
    links = " ".join(f"https://example.com/{index}" for index in range(30))
    _send(double, text=f"{'x' * 20} {links} https://example.com/{'y' * 2100}")
    monkeypatch.setattr(reader, "MAX_TEXT", 10)
    (found,) = reader.find(_server(double), Query(body=True))
    assert found.text == "x" * 10
    assert found.text_truncated
    assert found.links is not None
    assert len(found.links) == reader.MAX_LINKS
    monkeypatch.setattr(reader, "MAX_BODY_BYTES", 10)
    (large,) = reader.find(_server(double), Query(body=True))
    assert (large.text, large.links, large.text_truncated) == ("", [], True)


def test_an_unknown_charset_and_an_account_without_a_spam_folder(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail, junk_attribute=False, junk_name="Werbung")
    raw = "From: a@example.com\nTo: x@example.org\nSubject: s\nContent-Type: text/plain; charset=x-unknown\n\nhi there"
    double.deliver({"to": USER, "from": "a@example.com", "raw": raw})
    double.deliver({"to": USER, "from": "a@example.com", "subject": "junk", "text": "x", "spam": True})
    (found,) = reader.find(_server(double), Query(body=True))
    assert (found.id, found.text) == ("inbox:1", "hi there")


def test_a_mail_without_text_parts(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail)
    raw = "From: a@example.com\nTo: x@example.org\nSubject: =?utf-8?q?Gr=C3=BC=C3=9Fe?=\nContent-Type: image/png\n\nabc"
    double.deliver({"to": USER, "from": "a@example.com", "raw": raw})
    (found,) = reader.find(_server(double), Query(body=True))
    assert (found.subject, found.text, found.links) == ("Grüße", "", [])


def test_a_refused_sign_in_and_a_refused_command(mail: Callable[[], MailDouble]) -> None:
    double = _started(mail)
    with pytest.raises(reader.Refused) as refused:
        reader.find(_server(double, "wrong"), Query())
    assert refused.value.step == "login"
    assert "wrong" not in str(refused.value)

    class Failing:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def login(self, *_args: Any) -> None:
            pass

        def list(self) -> tuple[str, Any]:
            return "OK", []

        def select(self, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
            return "NO", [b"no"]

        def logout(self) -> None:
            raise OSError("gone")

    with pytest.raises(reader.Refused) as failed:
        reader.find(_server(double), Query(), imap_factory=Failing)
    assert failed.value.step == "command"

    class Broken(Failing):
        def select(self, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
            raise imaplib.IMAP4.error("bad")

    with pytest.raises(reader.Refused):
        reader.find(_server(double), Query(folder="inbox"), imap_factory=Broken)


def test_header_and_date_parsing_tolerate_odd_values() -> None:
    assert reader._header(None, 10) == ""
    assert reader._header("=?unknown-8?q?x?=", 20) == "=?unknown-8?q?x?="
    assert reader._arrival(None) is None
    assert reader._arrival("not a date") is None
    assert reader._fetched([b")", (b"1 (FLAGS ())", b"x")]) == []
    assert reader._within(reader.Message("inbox", 1, "", "", "", None, 0), Query()) is True
    assert reader._within(reader.Message("inbox", 1, "", "", "", None, 0), Query(since=datetime.now(UTC))) is False
