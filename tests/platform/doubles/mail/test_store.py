"""U-DBL-mail: accounts, folders, spam rules and composed messages."""

from __future__ import annotations

from email import message_from_bytes

import pytest

from agent_doubles.mail.message import compose
from agent_doubles.mail.store import INBOX, MailStore, StoreError, parse_account


def _raw(spam: bool = False, **kwargs: str) -> bytes:
    return compose("Sender <s@sender.test>", ["u@example.test"], "Hi", when=0, text="body", spam=spam, **kwargs)


def test_accounts_and_authentication() -> None:
    store = MailStore(clock=lambda: 1000.0)
    account = store.add_account("U@Example.test", "pw")
    assert store.account("u@example.TEST") is account
    assert store.authenticate("u@example.test", "pw") is account
    assert store.authenticate("u@example.test", "wrong") is None
    assert store.authenticate("nobody@example.test", "pw") is None
    assert list(account.folders) == [INBOX, "Junk"]
    assert account.folders["Junk"].attributes == ("\\HasNoChildren", "\\Junk")
    assert account.folder("inbox") is account.folders[INBOX]
    assert account.folder("Missing") is None
    replaced = store.add_account("u@example.test", "new", junk_name="INBOX.Spam", junk_attribute=False, delimiter=".")
    assert store.accounts() == [replaced]
    assert replaced.folders["INBOX.Spam"].attributes == ("\\HasNoChildren",)
    assert replaced.folders["INBOX.Spam"].uidvalidity > account.folders["Junk"].uidvalidity


@pytest.mark.parametrize(
    ("user", "password", "options", "message"),
    [
        ("", "pw", {}, "invalid user"),
        (" u", "pw", {}, "invalid user"),
        ("u", 'p"w', {}, "invalid password"),
        ("u", "pw\r\n", {}, "invalid password"),
        ("u", "pw", {"junk_name": "inbox"}, "cannot be INBOX"),
        ("u", "pw", {"junk_name": "a\\b"}, "invalid spam folder name"),
        ("u", "pw", {"delimiter": "|"}, "invalid delimiter"),
    ],
)
def test_invalid_accounts(user: str, password: str, options: dict[str, str], message: str) -> None:
    with pytest.raises(StoreError, match=message):
        MailStore().add_account(user, password, **options)  # type: ignore[arg-type]


def test_delivery_rules() -> None:
    store = MailStore(clock=lambda: 1000.0, junk_senders=["Bulk@Sender.test"])
    store.add_account("u@example.test", "pw")
    store.add_account("v@example.test", "pw", junk_name="Spam")
    assert store.deliver(_raw(), sender="s@sender.test", recipients=["u@example.test"]) == [
        ("u@example.test", "INBOX", 1)
    ]
    assert store.deliver(_raw(spam=True), sender="s@sender.test", recipients=["U@example.test", "v@example.test"]) == [
        ("u@example.test", "Junk", 1),
        ("v@example.test", "Spam", 1),
    ]
    assert store.deliver(_raw(), sender="bulk@sender.test", recipients=["u@example.test"]) == [
        ("u@example.test", "Junk", 2)
    ]
    assert store.deliver(_raw(spam=True), sender="s@sender.test", recipients=["u@example.test"], folder="inbox") == [
        ("u@example.test", "INBOX", 2)
    ]
    assert store.counts() == {"u@example.test": {"INBOX": 2, "Junk": 2}, "v@example.test": {"INBOX": 0, "Spam": 1}}
    stored = store.account("u@example.test").folders[INBOX].messages[0]  # type: ignore[union-attr]
    assert (stored.internal_date, stored.sender, stored.recipients) == (1000.0, "s@sender.test", ("u@example.test",))


def test_delivery_errors_store_nothing() -> None:
    store = MailStore()
    store.add_account("u@example.test", "pw")
    with pytest.raises(StoreError, match="no recipient"):
        store.deliver(_raw(), sender="s", recipients=[])
    with pytest.raises(StoreError, match=r"unknown recipient nobody@example\.test"):
        store.deliver(_raw(), sender="s", recipients=["u@example.test", "nobody@example.test"])
    with pytest.raises(StoreError, match="unknown folder Archive"):
        store.deliver(_raw(), sender="s", recipients=["u@example.test"], folder="Archive")
    assert store.counts() == {"u@example.test": {"INBOX": 0, "Junk": 0}}


def test_summary_filters_and_clear_keeps_uids() -> None:
    store = MailStore(clock=lambda: 5.0)
    store.add_account("u@example.test", "pw")
    store.add_account("v@example.test", "pw")
    store.deliver(_raw(), sender="s@sender.test", recipients=["u@example.test", "v@example.test"])
    store.deliver(_raw(spam=True), sender="s@sender.test", recipients=["u@example.test"])
    assert len(store.summary()) == 3
    assert [m["folder"] for m in store.summary("U@example.test")] == ["INBOX", "Junk"]
    (junk,) = store.summary(folder="Junk")
    assert junk == {
        "user": "u@example.test",
        "folder": "Junk",
        "uid": 1,
        "from": "Sender <s@sender.test>",
        "to": "u@example.test",
        "subject": "Hi",
        "date": junk["date"],
        "internal_date": 5.0,
        "sender": "s@sender.test",
        "flags": [],
        "size": junk["size"],
    }
    store.clear_messages()
    assert store.summary() == []
    store.deliver(_raw(), sender="s", recipients=["u@example.test"])
    assert store.summary()[0]["uid"] == 2


def test_compose_variants() -> None:
    both = message_from_bytes(_raw(html="<p>body</p>"))
    assert both.get_content_type() == "multipart/alternative"
    html_only = message_from_bytes(compose("a@x.test", ["b@x.test"], "s", when=0, html="<b>x</b>"))
    assert html_only.get_content_type() == "text/html"
    empty = compose("a@x.test", ["b@x.test", "c@x.test"], "s", when=0, date="Sat, 21 Nov 2026 12:00:00 +0000")
    parsed = message_from_bytes(empty)
    assert (parsed["To"], parsed["Date"], parsed["X-Spam-Flag"]) == (
        "b@x.test, c@x.test",
        "Sat, 21 Nov 2026 12:00:00 +0000",
        None,
    )
    assert b"\r\n" in empty
    assert b"\n" not in empty.replace(b"\r\n", b"")
    assert message_from_bytes(_raw(spam=True))["X-Spam-Flag"] == "YES"


def test_account_repr_hides_the_password() -> None:
    account = MailStore().add_account("u@example.test", "very-secret-pw")
    assert "very-secret-pw" not in repr(account)


def test_parse_account() -> None:
    assert parse_account({"user": "u", "password": "p", "delimiter": "."}) == ("u", "p", {"delimiter": "."})
    for bad, message in (
        ("u:p", "must be an object"),
        ({"user": "u"}, "must be strings"),
        ({"user": "u", "password": "p", "x": 1}, "unknown account keys"),
        ({"user": "u", "password": "p", "junk_attribute": 1}, "wrong type"),
    ):
        with pytest.raises(StoreError, match=message):
            parse_account(bad)
