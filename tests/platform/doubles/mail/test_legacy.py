"""U-DBL-mail: the legacy IMAP poll `agent_cli.account_login.newest_magic_link` works unchanged against the double.

The legacy function is imported read-only (00-overview.md §5) and called with its defaults: ``imaplib.IMAP4_SSL``
and ``ssl.create_default_context()``; the runtime CA is trusted through ``SSL_CERT_FILE`` only.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable

import pytest

from agent_cli.account_login import Mailbox, newest_magic_link
from agent_doubles.mail.message import compose
from agent_doubles.mail.testca import DoubleCA

from .conftest import PASSWORD, USER, Mail

SENDER = "Login <login@auth.double.test>"
PATTERN = re.compile(r"https://127\.0\.0\.1:\d+/magic-link#[A-Za-z0-9_-]+")


@pytest.fixture
def trusted(mail_ca: DoubleCA, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(mail_ca.ca_file))


def _link(mail: Mail, token: str, *, spam: bool = False, date: str | None = None, sender: str = SENDER) -> None:
    raw = compose(
        sender,
        [USER],
        "Secure link to log in",
        when=mail.clock(),
        text=f"Click https://127.0.0.1:4443/magic-link#{token} to sign in.",
        html=f'<a href="https://127.0.0.1:4443/magic-link#{token}">Sign in</a>',
        spam=spam,
        date=date,
    )
    with mail.double.store.lock:
        mail.double.store.deliver(raw, sender="login@auth.double.test", recipients=[USER])


def _poll(mail: Mail, since: float, password: str = PASSWORD) -> str | None:
    assert mail.double.imaps is not None
    box = Mailbox("127.0.0.1", USER, password, mail.double.imaps.port)
    return newest_magic_link(box, since, pattern=PATTERN, mail_from="auth.double.test")


@pytest.mark.usefixtures("trusted")
def test_magic_link_in_junk(mail: Mail) -> None:
    _link(mail, "junkToken1", spam=True)
    assert _poll(mail, mail.clock()) == "https://127.0.0.1:4443/magic-link#junkToken1"
    assert mail.double.store.counts()[USER] == {"INBOX": 0, "Junk": 1}
    paths = [r["path"] for r in mail.double.recorder.journal.records()]
    assert paths == ["CAPABILITY", "LOGIN", "EXAMINE", "SEARCH", "LIST", "EXAMINE", "SEARCH", "FETCH", "LOGOUT"]
    assert mail.double.state()["ok"] is True


@pytest.mark.usefixtures("trusted")
def test_newest_link_wins_across_folders(mail: Mail) -> None:
    _link(mail, "inboxOld", date="Sat, 21 Nov 2026 12:00:00 +0000")
    _link(mail, "junkNew", spam=True, date="Sat, 21 Nov 2026 12:01:00 +0000")
    assert _poll(mail, mail.clock()) == "https://127.0.0.1:4443/magic-link#junkNew"
    _link(mail, "inboxNewest", date="Sat, 21 Nov 2026 12:02:00 +0000")
    assert _poll(mail, mail.clock()) == "https://127.0.0.1:4443/magic-link#inboxNewest"


@pytest.mark.usefixtures("trusted")
def test_sender_filter_and_age(mail: Mail) -> None:
    _link(mail, "foreign", sender="Other <phish@other.test>")
    assert _poll(mail, mail.clock()) is None
    _link(mail, "old", date="Fri, 20 Nov 2026 12:00:00 +0000")
    assert _poll(mail, mail.clock()) is None
    assert _poll(mail, mail.clock() - 2 * 86_400) == "https://127.0.0.1:4443/magic-link#old"


@pytest.mark.usefixtures("trusted")
def test_spam_folder_found_by_name_without_attribute(start_mail: Callable[..., Mail]) -> None:
    mail = start_mail(account=False)
    mail.double.add_account(USER, PASSWORD, junk_name="INBOX.Spam", junk_attribute=False, delimiter=".")
    _link(mail, "byName", spam=True)
    assert _poll(mail, mail.clock()) == "https://127.0.0.1:4443/magic-link#byName"


@pytest.mark.usefixtures("trusted")
def test_wrong_password_raises_the_imaplib_error(mail: Mail) -> None:
    with pytest.raises(imaplib.IMAP4.error):
        _poll(mail, mail.clock(), password="wrong")


def test_untrusted_ca_fails_verification(mail: Mail, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    with pytest.raises(ssl.SSLCertVerificationError):
        _poll(mail, mail.clock())
