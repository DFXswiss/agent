from __future__ import annotations

import copy
import json
import os
import re
import ssl
import stat
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from agent_cli.account_login import (
    IMAP_TIMEOUT_SECONDS,
    LOGOUT_WAIT_SECONDS,
    MAX_MESSAGE_BYTES,
    Mailbox,
    load_mailbox,
    login,
    mailbox_path,
    newest_magic_link,
)
from agent_cli.ai_accounts import AIAccount
from agent_cli.drivers import DriverSpec, parse_driver_document
from agent_cli.main import main
from agent_cli.store import StoreError
from agent_cli.token_keepalive import try_profile_lock
from driver_fixtures import write_drivers

pytestmark = pytest.mark.no_pg

EMAIL = "seat-1@example.com"
PASSWORD = "imap-secret-password"
NOW = 1_800_000_000.0
DRIVER = "driver-l"
HOME_ENV = "LOGIN_HOME"
LOGOUT_URL = "https://sign-in.example.test/logout"
MAIL_FROM = "sign-in.example.test"
AUTHORIZE_URL = (
    "https://sign-in.example.test/oauth/authorize?code=true&client_id=client"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&state=s"
)
LINK = "https://sign-in.example.test/magic-link#c2lnbi1pbi1zZWNyZXQ:seat-1"
OLD_LINK = "https://sign-in.example.test/magic-link#b2xkLWxpbms:seat-1"
MAILBOX = Mailbox("imap.example.com", "seat-1@example.com", PASSWORD)
LOGIN_SECTION: dict[str, Any] = {
    "argv": ["login-tool", "auth", "login", "--email", "{email}"],
    "status_argv": ["login-tool", "auth", "status"],
    "email_field": "email",
    "logged_in_field": "loggedIn",
    "subscription_field": "subscriptionType",
    "logout_url": LOGOUT_URL,
    "authorize_url_marker": "redirect_uri=http%3A%2F%2Flocalhost",
    "magic_link_pattern": r"https://sign-in\.example\.test/magic-link#[A-Za-z0-9_\-:=+/]+",
    "mail_from": MAIL_FROM,
}
AMBIENT = {
    "PATH": "/usr/bin",
    HOME_ENV: "/elsewhere",
    "LOGIN_API_KEY": "sk-ambient",
    "OTHER_HOME": "/elsewhere-too",
    "BROWSER": "/usr/bin/browser",
    "HTTPS_PROXY": "http://127.0.0.1:8123",
}


def login_document(**changes: Any) -> dict[str, Any]:
    section = copy.deepcopy(LOGIN_SECTION)
    for key, value in changes.items():
        if value is None:
            del section[key]
        else:
            section[key] = value
    return {
        "drivers": {
            DRIVER: {
                "home_env": HOME_ENV,
                "credential_env": ["LOGIN_API_KEY"],
                "login": section,
            },
            "driver-n": {"home_env": "OTHER_HOME"},
        }
    }


def drivers(**changes: Any) -> dict[str, DriverSpec]:
    return parse_driver_document(login_document(**changes)).drivers


def magic_link_pattern() -> re.Pattern[str]:
    section = drivers()[DRIVER].login
    assert section is not None
    return section.magic_link_pattern


def opt_bin(name: str) -> str:
    return f"/opt/bin/{name}"


# --- Mailbox file --------------------------------------------------------------


def write_mailbox(home: Path, data: object, mode: int = 0o600) -> Path:
    path = mailbox_path(home, EMAIL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(mode)
    return path


def mailbox_data(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "imap_host": "imap.example.com",
        "imap_user": EMAIL,
        "imap_password": PASSWORD,
    }
    data.update(changes)
    return data


def test_mailbox_path_lowercases_the_address(tmp_path: Path) -> None:
    assert mailbox_path(tmp_path, "Seat-1@Example.COM") == (
        tmp_path / "account-login" / "seat-1@example.com.json"
    )
    for bad in ("seat-1", "a@b@c", "../x@example.com", "seat 1@example.com"):
        with pytest.raises(StoreError):
            mailbox_path(tmp_path, bad)


def test_load_mailbox_missing_file_is_none(tmp_path: Path) -> None:
    assert load_mailbox(tmp_path, EMAIL) is None


def test_load_mailbox_reads_a_private_file(tmp_path: Path) -> None:
    write_mailbox(tmp_path, mailbox_data())
    assert load_mailbox(tmp_path, EMAIL) == MAILBOX
    write_mailbox(tmp_path, mailbox_data(imap_port=1993))
    loaded = load_mailbox(tmp_path, EMAIL)
    assert loaded is not None and loaded.port == 1993
    assert PASSWORD not in repr(loaded)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_load_mailbox_refuses_group_or_other_permissions(tmp_path: Path, mode: int) -> None:
    write_mailbox(tmp_path, mailbox_data(), mode)
    with pytest.raises(StoreError, match="chmod 600") as info:
        load_mailbox(tmp_path, EMAIL)
    assert PASSWORD not in str(info.value)


@pytest.mark.parametrize(
    "data",
    [
        mailbox_data(extra="x"),
        {"imap_host": "imap.example.com", "imap_password": PASSWORD},
        mailbox_data(imap_host=""),
        mailbox_data(imap_user="  "),
        mailbox_data(imap_password=7),
        mailbox_data(imap_port=0),
        mailbox_data(imap_port=65536),
        mailbox_data(imap_port="993"),
        mailbox_data(imap_port=True),
        [PASSWORD],
    ],
)
def test_load_mailbox_refuses_bad_keys_without_the_password(
    tmp_path: Path, data: object
) -> None:
    write_mailbox(tmp_path, data)
    with pytest.raises(StoreError) as info:
        load_mailbox(tmp_path, EMAIL)
    assert PASSWORD not in str(info.value)


def test_load_mailbox_invalid_json_hides_the_text(tmp_path: Path) -> None:
    path = mailbox_path(tmp_path, EMAIL)
    path.parent.mkdir(parents=True)
    path.write_text('{"imap_password": "' + PASSWORD, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(StoreError, match="not valid JSON") as info:
        load_mailbox(tmp_path, EMAIL)
    assert PASSWORD not in str(info.value)
    assert info.value.__context__ is None


# --- IMAP ----------------------------------------------------------------------


def message(link: str | None, sent: float, *, html: bool = True) -> bytes:
    mail = EmailMessage()
    mail["From"] = f"Sign-in <no-reply@{MAIL_FROM}>"
    mail["To"] = EMAIL
    mail["Subject"] = "Sign in"
    mail["Date"] = format_datetime(datetime.fromtimestamp(sent, timezone.utc))
    body = "Welcome back." if link is None else f"Sign in: {link}"
    mail.set_content(body)
    if html:
        mail.add_alternative(f'<p><a href="{link or "https://example.com"}">Sign in</a></p>', subtype="html")
    return bytes(mail)


INBOX_LINE = b'(\\HasNoChildren) "/" "INBOX"'
JUNK_LINE = b'(\\HasNoChildren \\Junk) "/" "Werbung"'


class FakeImap:
    def __init__(
        self,
        messages: list[bytes],
        *,
        fail_login: bool = False,
        junk: list[bytes] | None = None,
        folders: list[Any] | None = None,
        list_status: str = "OK",
        junk_select_status: str = "OK",
    ) -> None:
        self.messages = messages
        self.fail_login = fail_login
        self.junk = junk or []
        # Without a spam folder in the LIST answer only INBOX exists.
        self.folders = [INBOX_LINE] if folders is None else folders
        self.list_status = list_status
        self.junk_select_status = junk_select_status
        self.selected = "INBOX"
        self.calls: list[tuple[Any, ...]] = []
        self.connected: list[tuple[Any, ...]] = []
        self.fetched: list[bytes] = []
        self.fetch_parts: list[str] = []
        self.logged_out = 0

    def _current(self) -> list[bytes]:
        return self.messages if self.selected == "INBOX" else self.junk

    def list(self) -> tuple[str, list[Any]]:
        self.calls.append(("list",))
        return self.list_status, self.folders

    def __call__(self, host: str, port: int, *, ssl_context: Any, timeout: float) -> FakeImap:
        self.connected.append((host, port, ssl_context, timeout))
        return self

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        if self.fail_login:
            import imaplib

            raise imaplib.IMAP4.error("authentication failed")
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        if mailbox != "INBOX" and self.junk_select_status != "OK":
            return self.junk_select_status, [b"no such folder"]
        self.selected = mailbox
        return "OK", [str(len(self._current())).encode()]

    def search(self, charset: Any, *criteria: str) -> tuple[str, list[bytes]]:
        self.calls.append(("search", charset, *criteria))
        ids = " ".join(str(index + 1) for index in range(len(self._current())))
        return "OK", [ids.encode()]

    def fetch(self, message_id: bytes, parts: str) -> tuple[str, list[Any]]:
        self.fetched.append(message_id)
        self.fetch_parts.append(parts)
        raw = self._current()[int(message_id) - 1]
        # What a server does with a partial fetch: send at most that many bytes.
        partial = re.fullmatch(r"\(BODY\.PEEK\[\]<0\.(\d+)>\)", parts)
        if partial is not None:
            raw = raw[: int(partial.group(1))]
        return "OK", [(message_id + b" (BODY[]<0> {%d}" % len(raw), raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out += 1
        return "BYE", [b""]


def newest(imap: FakeImap, *, pattern: re.Pattern[str] | None = None) -> str | None:
    return newest_magic_link(
        MAILBOX,
        NOW,
        pattern=magic_link_pattern() if pattern is None else pattern,
        mail_from=MAIL_FROM,
        imap_factory=imap,
    )


def test_newest_magic_link_returns_the_newest_matching_message() -> None:
    imap = FakeImap([message(OLD_LINK, NOW + 10), message(LINK, NOW + 20, html=False)])
    assert newest(imap) == LINK
    assert imap.fetched == [b"2"]
    host, port, context, timeout = imap.connected[0]
    assert (host, port) == ("imap.example.com", 993)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    # A stalled server cannot block the poll loop past the timeout.
    assert timeout == IMAP_TIMEOUT_SECONDS == 30
    assert ("select", "INBOX", True) in imap.calls
    assert ("search", None, "FROM", f'"{MAIL_FROM}"') in imap.calls
    assert imap.fetch_parts == [f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"]
    assert imap.logged_out == 1


def test_newest_magic_link_skips_an_oversized_message() -> None:
    assert MAX_MESSAGE_BYTES == 1024 * 1024
    huge = EmailMessage()
    huge["From"] = f"Sign-in <no-reply@{MAIL_FROM}>"
    huge["Date"] = format_datetime(datetime.fromtimestamp(NOW + 20, timezone.utc))
    huge.set_content(f"Sign in: {OLD_LINK}\n" + "x" * 76 * 15000)
    oversized = bytes(huge)
    assert len(oversized) > MAX_MESSAGE_BYTES
    imap = FakeImap([message(LINK, NOW + 10), oversized])
    # The oversized newest message is not parsed; its link is never used.
    assert newest(imap) == LINK
    assert imap.fetched == [b"2", b"1"]
    assert all(parts == f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)" for parts in imap.fetch_parts)
    assert imap.logged_out == 1


def test_newest_magic_link_reads_a_message_of_exactly_the_limit() -> None:
    small = message(LINK, NOW + 10, html=False)
    padded = small + b"\n" * (MAX_MESSAGE_BYTES - len(small))
    assert len(padded) == MAX_MESSAGE_BYTES
    assert newest(FakeImap([padded])) == LINK


def test_newest_magic_link_skips_newer_messages_without_a_link() -> None:
    imap = FakeImap([message(LINK, NOW + 10), message(None, NOW + 20, html=False)])
    assert newest(imap) == LINK
    assert imap.fetched == [b"2", b"1"]


def test_newest_magic_link_ignores_messages_older_than_since() -> None:
    imap = FakeImap([message(LINK, NOW - 3600), message(None, NOW + 20, html=False)])
    assert newest(imap) is None
    assert imap.fetched == [b"2", b"1"]
    assert imap.logged_out == 1

    # A message a few seconds before ``since`` still counts (clock skew).
    imap = FakeImap([message(LINK, NOW - 3)])
    assert newest(imap) == LINK


def test_newest_magic_link_without_link_is_none() -> None:
    imap = FakeImap([message(None, NOW + 10, html=False)])
    assert newest(imap) is None
    assert imap.logged_out == 1
    imap = FakeImap([])
    assert newest(imap) is None
    assert imap.logged_out == 1


def test_newest_magic_link_reads_at_most_ten_messages() -> None:
    imap = FakeImap([message(None, NOW + index, html=False) for index in range(15)])
    assert newest(imap) is None
    assert imap.fetched == [str(index).encode() for index in range(15, 5, -1)]


def test_newest_magic_link_logs_out_after_an_error() -> None:
    import imaplib

    imap = FakeImap([message(LINK, NOW + 10)], fail_login=True)
    with pytest.raises(imaplib.IMAP4.error):
        newest(imap)
    assert imap.logged_out == 1


def test_magic_link_pattern_comes_from_the_driver() -> None:
    pattern = magic_link_pattern()
    assert pattern.search(f'href="{LINK}"').group(0) == LINK  # type: ignore[union-attr]
    assert pattern.search("https://sign-in.example.test.evil.example/magic-link#x") is None


def test_newest_magic_link_counts_only_https_matches() -> None:
    loose = re.compile(r"[a-z]+://sign-in\.example\.test/magic-link#[A-Za-z0-9:]+")
    plain = "http://sign-in.example.test/magic-link#abc"
    imap = FakeImap([message(plain, NOW + 10, html=False)])
    assert newest(imap, pattern=loose) is None
    # The plain http match comes first in the text and is passed over.
    both = f"{plain} {LINK}"
    imap = FakeImap([message(both, NOW + 10, html=False)])
    assert newest(imap, pattern=loose) == "https://sign-in.example.test/magic-link#c2lnbi1pbi1zZWNyZXQ:seat"


def test_newest_magic_link_reads_the_junk_flagged_folder() -> None:
    imap = FakeImap([], junk=[message(LINK, NOW + 10)], folders=[INBOX_LINE, JUNK_LINE])
    assert newest(imap) == LINK
    # The folder is sent as the server listed it and opened read-only.
    assert ("select", '"Werbung"', True) in imap.calls
    assert not any(call[0] in ("store", "copy", "move", "expunge") for call in imap.calls)
    assert imap.logged_out == 1


def test_newest_magic_link_finds_the_spam_folder_by_name() -> None:
    folders = [
        INBOX_LINE,
        b'(\\HasNoChildren) "." "INBOX.Archive"',
        b'(\\HasNoChildren) "." "INBOX.Spam"',
        b'(\\HasNoChildren) "." Junk',
    ]
    imap = FakeImap([], junk=[message(LINK, NOW + 10)], folders=folders)
    assert newest(imap) == LINK
    # The first matching name in LIST order wins.
    assert ("select", '"INBOX.Spam"', True) in imap.calls
    for line, sent in (
        (b'(\\HasNoChildren) "/" "Junk E-Mail"', '"Junk E-Mail"'),
        (b'(\\HasNoChildren) NIL junk', "junk"),
        (b'(\\HasNoChildren) "/" "INBOX/SPAM"', '"INBOX/SPAM"'),
    ):
        imap = FakeImap([], junk=[message(LINK, NOW + 10)], folders=[INBOX_LINE, line])
        assert newest(imap) == LINK
        assert ("select", sent, True) in imap.calls


def test_newest_magic_link_prefers_the_junk_flag_over_a_name() -> None:
    folders = [INBOX_LINE, b'(\\HasNoChildren) "/" "Spam"', JUNK_LINE]
    imap = FakeImap([], junk=[message(LINK, NOW + 10)], folders=folders)
    assert newest(imap) == LINK
    assert ("select", '"Werbung"', True) in imap.calls
    assert ("select", '"Spam"', True) not in imap.calls


@pytest.mark.parametrize(
    "folders",
    [
        [INBOX_LINE],
        [INBOX_LINE, b'(\\HasNoChildren) "/" "Archive"', b'(\\HasNoChildren) "/" "INBOX/Archive"'],
        # A flagged INBOX, a literal name and an unparsable line are never chosen.
        [b'(\\Junk) "/" "INBOX"', (b'(\\Junk) "/" {4}', b"Spam"), b"garbage", 7],
        "not a list",
    ],
)
def test_newest_magic_link_without_a_spam_folder_reads_only_inbox(folders: Any) -> None:
    imap = FakeImap([message(LINK, NOW + 10)], junk=[message(OLD_LINK, NOW + 20)], folders=folders)
    assert newest(imap) == LINK
    assert [call for call in imap.calls if call[0] == "select"] == [("select", "INBOX", True)]
    assert imap.logged_out == 1


def test_newest_magic_link_failed_list_reads_only_inbox() -> None:
    imap = FakeImap(
        [message(LINK, NOW + 10)],
        junk=[message(OLD_LINK, NOW + 20)],
        folders=[INBOX_LINE, JUNK_LINE],
        list_status="NO",
    )
    assert newest(imap) == LINK
    assert [call for call in imap.calls if call[0] == "select"] == [("select", "INBOX", True)]


def test_newest_magic_link_spam_folder_that_cannot_be_opened_is_left_out() -> None:
    imap = FakeImap(
        [message(LINK, NOW + 10)],
        junk=[message(OLD_LINK, NOW + 20)],
        folders=[INBOX_LINE, JUNK_LINE],
        junk_select_status="NO",
    )
    assert newest(imap) == LINK
    assert imap.fetched == [b"1"]
    imap = FakeImap([], junk=[message(LINK, NOW + 20)], folders=[INBOX_LINE, JUNK_LINE], junk_select_status="NO")
    assert newest(imap) is None
    assert imap.logged_out == 1


def test_newest_magic_link_uses_the_later_message_of_both_folders() -> None:
    both = [INBOX_LINE, JUNK_LINE]
    imap = FakeImap([message(OLD_LINK, NOW + 10)], junk=[message(LINK, NOW + 20)], folders=both)
    assert newest(imap) == LINK
    imap = FakeImap([message(LINK, NOW + 20)], junk=[message(OLD_LINK, NOW + 10)], folders=both)
    assert newest(imap) == LINK
    # Equal dates: INBOX wins.
    imap = FakeImap([message(LINK, NOW + 20)], junk=[message(OLD_LINK, NOW + 20)], folders=both)
    assert newest(imap) == LINK
    # A spam message older than ``since`` does not count.
    imap = FakeImap([], junk=[message(OLD_LINK, NOW - 3600)], folders=both)
    assert newest(imap) is None


def test_newest_magic_link_missing_date_keeps_the_inbox_link() -> None:
    both = [INBOX_LINE, JUNK_LINE]
    undated = message(LINK, NOW + 10).replace(b"Date:", b"X-Date:", 1)
    imap = FakeImap([undated], junk=[message(OLD_LINK, NOW + 20)], folders=both)
    assert newest(imap) == LINK
    imap = FakeImap([message(LINK, NOW + 10)], junk=[message(OLD_LINK, NOW + 20).replace(b"Date:", b"X-Date:", 1)], folders=both)
    assert newest(imap) == LINK


# --- login ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class Harness:
    """Fakes for every outside effect of ``login``."""

    def __init__(self, tmp_path: Path, *, url: str = AUTHORIZE_URL, status_email: str = EMAIL) -> None:
        self.tmp_path = tmp_path
        self.url = url
        self.clock = Clock()
        self.process = FakeProcess()
        self.popen_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.opened: list[str] = []
        self.events: list[str] = []
        self.clipboard: list[str] = []
        self.lines: list[str] = []
        self.run_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.status: dict[str, Any] = {
            "loggedIn": True,
            "authMethod": "web",
            "email": status_email,
            "subscriptionType": "max",
        }
        self.finish_on = LINK
        self.workdir: Path | None = None

    def popen(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.popen_calls.append((argv, kwargs))
        workdir = Path(kwargs["cwd"])
        self.workdir = workdir
        helper = Path(kwargs["env"]["BROWSER"])
        assert helper.parent == workdir
        assert os.access(helper, os.X_OK)
        # What a login tool does with BROWSER: hand it the authorize URL.
        (workdir / "url.txt").write_text(self.url, encoding="utf-8")
        return self.process

    def opener(self, url: str) -> None:
        self.opened.append(url)
        self.events.append(f"open {url}")
        if url == self.finish_on:
            self.process.returncode = 0

    def sleep(self, seconds: float) -> None:
        self.events.append(f"sleep {seconds}")
        self.clock.sleep(seconds)

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.run_calls.append((argv, kwargs))
        assert argv == ["/opt/bin/login-tool", "auth", "status"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.status), "")

    def login(self, account: AIAccount, **kwargs: Any) -> None:
        defaults: dict[str, Any] = {
            "drivers": drivers(),
            "agent_home": self.tmp_path,
            "popen": self.popen,
            "run": self.run,
            "opener": self.opener,
            "clipboard": self.clipboard.append,
            "imap_factory": forbidden_imap,
            "clock": self.clock,
            "sleep": self.sleep,
            "out": self.lines.append,
            "which": opt_bin,
            "environ": AMBIENT,
        }
        defaults.update(kwargs)
        login(account, EMAIL, **defaults)


def forbidden_imap(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("no IMAP connection expected")


def seat(tmp_path: Path) -> AIAccount:
    return AIAccount("seat", DRIVER, str(tmp_path / "profiles" / "seat"))


def test_login_happy_path_with_mailbox(tmp_path: Path) -> None:
    write_mailbox(tmp_path, mailbox_data())
    harness = Harness(tmp_path)
    imap = FakeImap([message(OLD_LINK, NOW - 3600), message(LINK, NOW + 30)])
    harness.login(seat(tmp_path), imap_factory=imap)

    argv, kwargs = harness.popen_calls[0]
    assert argv == ["/opt/bin/login-tool", "auth", "login", "--email", EMAIL]
    assert kwargs["env"][HOME_ENV] == str(tmp_path / "profiles" / "seat")
    for key in ("LOGIN_API_KEY", "OTHER_HOME", "HTTPS_PROXY"):
        assert key not in kwargs["env"]
    assert kwargs["env"]["BROWSER"] != AMBIENT["BROWSER"]
    assert isinstance(kwargs["stdin"], int) and kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"]
    assert harness.clipboard == [EMAIL]
    # Sign-out first, at least five seconds before the authorize page, then the link once.
    assert harness.opened == [LOGOUT_URL, AUTHORIZE_URL, LINK]
    logout_at = harness.events.index(f"open {LOGOUT_URL}")
    authorize_at = harness.events.index(f"open {AUTHORIZE_URL}")
    assert f"sleep {LOGOUT_WAIT_SECONDS}" in harness.events[logout_at:authorize_at]
    assert imap.logged_out >= 1
    assert ("search", None, "FROM", f'"{MAIL_FROM}"') in imap.calls

    status_argv, status_kwargs = harness.run_calls[0]
    assert status_argv == ["/opt/bin/login-tool", "auth", "status"]
    assert "BROWSER" not in status_kwargs["env"]
    assert status_kwargs["env"][HOME_ENV] == str(tmp_path / "profiles" / "seat")

    output = "\n".join(harness.lines)
    assert (
        'ACTION: in the browser, paste the e-mail address if the field is empty '
        f'and choose "continue with e-mail" ({EMAIL})'
    ) in harness.lines
    assert f'ACTION: finish the sign-in in the browser and choose "Authorize" ({EMAIL})' in harness.lines
    assert harness.lines[-1] == f"account.login ok seat {EMAIL} (max)"
    assert LINK not in output and "c2lnbi1pbi1zZWNyZXQ" not in output
    assert PASSWORD not in output
    assert harness.workdir is not None and not harness.workdir.exists()


def test_login_retries_after_an_imap_error(tmp_path: Path) -> None:
    write_mailbox(tmp_path, mailbox_data())
    harness = Harness(tmp_path)
    good = FakeImap([message(LINK, NOW + 30)])
    attempts: list[int] = []

    def flaky(host: str, port: int, *, ssl_context: Any, timeout: float) -> FakeImap:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(f"cannot reach {host} with {PASSWORD}")
        if len(attempts) == 2:
            # What the socket timeout raises for a stalled server.
            raise TimeoutError("timed out")
        return good(host, port, ssl_context=ssl_context, timeout=timeout)

    harness.login(seat(tmp_path), imap_factory=flaky)
    assert len(attempts) == 3
    assert "note: mailbox check failed (OSError); retrying" in harness.lines
    assert "note: mailbox check failed (TimeoutError); retrying" in harness.lines
    assert PASSWORD not in "\n".join(harness.lines)
    assert harness.opened == [LOGOUT_URL, AUTHORIZE_URL, LINK]


def test_login_wrong_account_in_status_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path, status_email="other@example.com")
    harness.finish_on = AUTHORIZE_URL
    with pytest.raises(StoreError, match=f"signed in as other@example.com, expected {EMAIL}"):
        harness.login(seat(tmp_path), use_mailbox=False)
    assert not any(line.startswith("account.login ok") for line in harness.lines)
    assert harness.workdir is not None and not harness.workdir.exists()


def test_login_not_signed_in_reports_nobody(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.status = {"loggedIn": False}
    harness.finish_on = AUTHORIZE_URL
    with pytest.raises(StoreError, match=f"signed in as nobody, expected {EMAIL}"):
        harness.login(seat(tmp_path), use_mailbox=False)


def test_login_status_without_json_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "not json", "")

    with pytest.raises(StoreError, match=r"status command returned no JSON object \(exit 1\)"):
        harness.login(seat(tmp_path), use_mailbox=False, run=run)


def test_login_status_email_matches_without_case(tmp_path: Path) -> None:
    harness = Harness(tmp_path, status_email="Seat-1@Example.com")
    harness.finish_on = AUTHORIZE_URL
    harness.login(seat(tmp_path), use_mailbox=False)
    assert harness.lines[-1] == f"account.login ok seat {EMAIL} (max)"


def test_login_without_subscription_field_prints_no_subscription(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    harness.login(
        seat(tmp_path), use_mailbox=False, drivers=drivers(subscription_field=None)
    )
    assert harness.lines[-1] == f"account.login ok seat {EMAIL}"


def test_login_uses_the_configured_status_fields(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    harness.status = {"signedIn": True, "account": EMAIL, "plan": "team"}
    harness.login(
        seat(tmp_path),
        use_mailbox=False,
        drivers=drivers(
            email_field="account", logged_in_field="signedIn", subscription_field="plan"
        ),
    )
    assert harness.lines[-1] == f"account.login ok seat {EMAIL} (team)"


@pytest.mark.parametrize(
    "url",
    [
        "https://sign-in.example.test/oauth/authorize?redirect_uri=https%3A%2F%2Fevil.example.com%2Fcallback",
        "http://sign-in.example.test/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1%2Fcallback",
        "file:///etc/passwd",
    ],
)
def test_login_refuses_an_unexpected_authorize_url(tmp_path: Path, url: str) -> None:
    harness = Harness(tmp_path, url=url)
    with pytest.raises(StoreError, match="unexpected authorize URL"):
        harness.login(seat(tmp_path), use_mailbox=False)
    assert harness.opened == []
    assert harness.process.terminated
    assert harness.workdir is not None and not harness.workdir.exists()


def test_login_authorize_marker_comes_from_the_driver(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(StoreError, match="unexpected authorize URL"):
        harness.login(
            seat(tmp_path),
            use_mailbox=False,
            drivers=drivers(authorize_url_marker="redirect_uri=http%3A%2F%2F127.0.0.1"),
        )
    assert harness.opened == []


def test_login_without_mailbox_file_prints_the_manual_step(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    harness.login(seat(tmp_path))
    assert harness.opened == [LOGOUT_URL, AUTHORIZE_URL]
    assert any(line.startswith("note: no mailbox file") for line in harness.lines)
    assert (
        f'ACTION: open the sign-in link from the e-mail to {EMAIL}, then choose "Authorize"'
        in harness.lines
    )
    assert harness.lines[-1] == f"account.login ok seat {EMAIL} (max)"


def test_login_no_mailbox_flag_ignores_an_existing_file(tmp_path: Path) -> None:
    write_mailbox(tmp_path, mailbox_data())
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    harness.login(seat(tmp_path), use_mailbox=False)
    assert harness.opened == [LOGOUT_URL, AUTHORIZE_URL]
    assert not any(line.startswith("note: no mailbox file") for line in harness.lines)


def test_login_refuses_a_shared_mailbox_file_before_starting_the_tool(tmp_path: Path) -> None:
    write_mailbox(tmp_path, mailbox_data(), 0o644)
    harness = Harness(tmp_path)
    with pytest.raises(StoreError, match="chmod 600"):
        harness.login(seat(tmp_path))
    assert harness.popen_calls == []


def test_login_times_out_and_terminates_the_tool(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = "never"
    with pytest.raises(StoreError, match="did not finish within 600 s"):
        harness.login(seat(tmp_path), use_mailbox=False)
    assert harness.process.terminated
    assert harness.workdir is not None and not harness.workdir.exists()


def test_login_requires_a_login_section_and_one_at_sign(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(StoreError, match="driver driver-n, which has no login section"):
        harness.login(AIAccount("other", "driver-n", str(tmp_path / "other")))
    with pytest.raises(StoreError, match="driver driver-x, which has no login section"):
        harness.login(AIAccount("unknown", "driver-x", str(tmp_path / "unknown")))
    with pytest.raises(StoreError, match="exactly one @"):
        login(
            seat(tmp_path),
            "seat-1",
            drivers=drivers(),
            agent_home=tmp_path,
            popen=harness.popen,
            which=opt_bin,
        )
    assert harness.popen_calls == []


def test_login_without_the_executable_fails_before_starting(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(StoreError, match="login-tool executable not found on PATH"):
        harness.login(seat(tmp_path), use_mailbox=False, which=lambda _name: None)
    assert harness.popen_calls == []


def test_login_holds_the_profile_lock_of_the_keep_alive(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    account = seat(tmp_path)
    free_during_login: list[bool] = []
    original_run = harness.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # The status check runs while the login still holds the profile.
        with try_profile_lock(tmp_path, account.config_dir) as acquired:
            free_during_login.append(acquired)
        return original_run(argv, **kwargs)

    harness.login(account, use_mailbox=False, run=run)
    assert free_during_login == [False]
    assert not any(line.startswith("note: a token keep-alive") for line in harness.lines)
    # Released afterwards.
    with try_profile_lock(tmp_path, account.config_dir) as acquired:
        assert acquired


def test_login_waits_with_a_note_when_the_profile_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    flock_calls: list[int] = []
    real_flock = fcntl.flock

    def busy_once(fd: int, operation: int) -> None:
        flock_calls.append(operation)
        if len(flock_calls) == 1:
            # Another process holds the profile; the non-blocking attempt fails.
            assert operation & fcntl.LOCK_NB
            raise BlockingIOError(11, "Resource temporarily unavailable")
        real_flock(fd, operation)

    monkeypatch.setattr("agent_cli.token_keepalive.fcntl.flock", busy_once)
    harness.login(seat(tmp_path), use_mailbox=False)
    assert flock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_EX]
    assert (
        "note: a token keep-alive or another login holds profile "
        f"{tmp_path / 'profiles' / 'seat'}; waiting for it"
    ) in harness.lines
    assert harness.lines[-1] == f"account.login ok seat {EMAIL} (max)"


def test_capture_helper_is_private(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.finish_on = AUTHORIZE_URL
    modes: list[int] = []
    original = harness.popen

    def popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        modes.append(stat.S_IMODE(Path(kwargs["cwd"]).stat().st_mode))
        modes.append(stat.S_IMODE(Path(kwargs["env"]["BROWSER"]).stat().st_mode))
        return original(argv, **kwargs)

    harness.login(seat(tmp_path), use_mailbox=False, popen=popen)
    assert modes == [0o700, 0o700]


# --- CLI -----------------------------------------------------------------------


def test_cli_account_login_passes_the_account_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_drivers(tmp_path, login_document())
    (tmp_path / "ai-accounts.json").write_text(
        json.dumps(
            {"accounts": {"seat": {"provider": DRIVER, "config_dir": str(tmp_path / "seat")}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    calls: list[tuple[AIAccount, str, dict[str, Any]]] = []

    def fake_login(account: AIAccount, email: str, **kwargs: Any) -> None:
        calls.append((account, email, kwargs))

    monkeypatch.setattr("agent_cli.account_login.login", fake_login)
    main(["account-login", "seat", "--email", EMAIL])
    main(["account-login", "--email", EMAIL, "seat", "--no-mailbox"])
    assert [(account.name, email, kwargs["use_mailbox"]) for account, email, kwargs in calls] == [
        ("seat", EMAIL, True),
        ("seat", EMAIL, False),
    ]
    assert calls[0][2]["agent_home"] == tmp_path
    assert calls[0][2]["drivers"][DRIVER].login is not None

    with pytest.raises(SystemExit, match="unknown AI account missing"):
        main(["account-login", "missing", "--email", EMAIL])
    for argv in (["seat"], ["seat", "--email"], ["--email", EMAIL], ["seat", "--email", EMAIL, "x"]):
        with pytest.raises(SystemExit, match="Usage: agent account-login"):
            main(["account-login", *argv])
