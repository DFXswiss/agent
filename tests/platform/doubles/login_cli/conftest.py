"""Fixtures of the login double suites: a shared runtime CA, mail + auth-page doubles wired together, an HTTP-level
browser (cookies, redirects, forms) and a PTY runner for the login-CLI double."""

from __future__ import annotations

import fcntl
import http.cookiejar
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from agent_cli.account_login import Mailbox, newest_magic_link
from agent_doubles.common.client import DoubleClient
from agent_doubles.login_cli.authpage import AuthPageDouble, Scenario
from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.testca import DoubleCA
from agent_runner.login.screen import Screen

USER = "subscriber@example.test"
PASSWORD = "mailbox-password"  # not a secret: the mail double only compares it
SRC = Path(__file__).resolve().parents[4] / "src"
ROWS = 50


class FakeClock:
    def __init__(self, now: float = 1_795_262_400.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="session")
def login_ca(tmp_path_factory: pytest.TempPathFactory) -> DoubleCA:
    return DoubleCA.create(tmp_path_factory.mktemp("login-ca"))


class Forms(HTMLParser):
    """``data-page`` of the body, hidden inputs, the form action and the authorization code of a page."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.page: str | None = None
        self.fields: dict[str, str] = {}
        self.action: str | None = None
        self.code: str | None = None
        self.error: str | None = None
        self._in: str | None = None
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "body":
            self.page = values.get("data-page")
        elif tag == "form":
            self.action = values.get("action")
        elif tag == "input" and values.get("type") == "hidden" and "value" in values:
            self.fields[values["name"]] = values["value"]
        elif tag == "code" and values.get("id") == "authorization-code":
            self._in = "code"
        elif tag == "p" and values.get("id") == "error-code":
            self._in = "error"

    def handle_data(self, data: str) -> None:
        if self._in is not None:
            setattr(self, self._in, data)
            self._in = None


@dataclass
class Page:
    status: int
    url: str
    text: str
    forms: Forms = field(init=False)

    def __post_init__(self) -> None:
        self.forms = Forms(self.text)

    @property
    def kind(self) -> str | None:
        return self.forms.page


class Browser:
    """An HTTP-level browser: TLS against the test CA, a cookie jar, redirects followed."""

    def __init__(self, ca: DoubleCA | None) -> None:
        self.jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        if ca is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ca.client_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def open(self, url: str, form: dict[str, str] | None = None) -> Page:
        data = None if form is None else urlencode(form).encode()
        try:
            with self.opener.open(url, data=data, timeout=10) as response:
                return Page(response.status, response.url, response.read().decode())
        except urllib.error.HTTPError as exc:
            return Page(exc.code, exc.url, exc.read().decode())


@dataclass
class Login:
    """A started mail double and auth-page double that sends its mails to it."""

    mail: MailDouble
    auth: AuthPageDouble
    control: DoubleClient
    ca: DoubleCA | None

    def browser(self) -> Browser:
        return Browser(self.ca)

    def magic_link(self, since: float) -> str | None:
        """The newest magic link, read with the legacy IMAP poll (unchanged) over IMAPS.

        The legacy poll accepts only ``https://`` links, so with plain-HTTP pages it finds nothing; `stored_link`
        reads such a link from the mail store instead.
        """
        assert self.mail.imaps is not None
        box = Mailbox("127.0.0.1", USER, PASSWORD, self.mail.imaps.port)
        pattern = re.compile(re.escape(self.auth.page_url) + r"/magic-link#[A-Za-z0-9_-]+")
        return newest_magic_link(box, since, pattern=pattern, mail_from="auth.double.test")

    def stored_link(self) -> str | None:
        """The magic link of the newest stored mail, read from the mail double directly."""
        messages = [m for f in self.mail.store.account(USER).folders.values() for m in f.messages]  # type: ignore[union-attr]
        if not messages:
            return None
        found = re.search(rb"https?://[^\s\"<>]+/magic-link#[A-Za-z0-9_-]+", messages[-1].raw)
        return None if found is None else found.group(0).decode()

    def code_for(self, authorize_url: str, browser: Browser | None = None) -> str:
        """Walk the pages over HTTP: e-mail, magic link, Authorize; the pasted ``CODE#STATE``."""
        browser = browser or self.browser()
        since = self.auth.clock()
        page = browser.open(authorize_url)
        assert page.kind == "email", page.text
        page = browser.open(self.auth.page_url + "/login", {"email": USER, **page.forms.fields})
        assert page.kind == "check-email", page.text
        link = self.magic_link(since) if self.ca is not None else self.stored_link()
        assert link is not None
        token = link.split("#", 1)[1]
        page = browser.open(self.auth.page_url + "/magic-link/verify", {"token": token})
        assert page.kind == "authorize", page.text
        page = browser.open(self.auth.page_url + "/oauth/authorize", {**page.forms.fields, "decision": "authorize"})
        assert page.kind == "code", page.text
        assert page.forms.code is not None
        return page.forms.code


@pytest.fixture
def start_login(
    login_ca: DoubleCA,
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[[Any], Any],
) -> Callable[..., Login]:
    """Start the pair; the legacy poll trusts the CA through SSL_CERT_FILE."""
    monkeypatch.setenv("SSL_CERT_FILE", str(login_ca.ca_file))

    def start(*, tls: bool = True, scenario: Scenario | None = None, **kwargs: Any) -> Login:
        ca = login_ca if tls else None
        mail = serve(MailDouble(ca=login_ca))
        mail.add_account(USER, PASSWORD)
        auth = serve(AuthPageDouble(ca=ca, smtp=(mail.smtp.host, mail.smtp.port), scenario=scenario, **kwargs))
        return Login(mail, auth, DoubleClient(auth.url, timeout=10), ca)

    return start


@pytest.fixture
def login(start_login: Callable[..., Login]) -> Login:
    return start_login()


@dataclass
class Terminal:
    """The login-CLI double running in a PTY, as the runner starts it (window `ROWS` x `columns`)."""

    process: subprocess.Popen[bytes]
    master: int
    columns: int = 120
    output: bytes = b""

    def read_until(self, marker: bytes, timeout: float = 20) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, self.output
            ready, _, _ = select.select([self.master], [], [], remaining)
            if ready:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    chunk = b""
                assert chunk, self.output
                self.output += chunk
        return self.output

    def type(self, text: str) -> None:
        os.write(self.master, text.encode() + b"\r")

    def finish(self, timeout: float = 20) -> int:
        """Read until the PTY reports the end of output (the child closed it), then reap the child.

        Reading before the wait matters: macOS drops unread PTY output once the last slave descriptor is closed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                if self.process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk
        return self.process.wait(timeout)

    @property
    def text(self) -> str:
        """The raw output (control sequences included), line ends normalised."""
        return self.output.decode("utf-8", "replace").replace("\r\n", "\n")

    @property
    def lines(self) -> list[str]:
        """The rows of the terminal screen the output draws (the runner reads the same, KOR-21)."""
        screen = Screen(ROWS, self.columns)
        screen.feed(self.output.decode("utf-8", "replace"))
        return screen.lines()

    def read_until_shown(self, text: str, timeout: float = 20) -> list[str]:
        """Read until `text` stands in a row of the screen."""
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.lines):
            assert time.monotonic() < deadline, self.lines
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                chunk = os.read(self.master, 4096)
                assert chunk, self.output
                self.output += chunk
        return self.lines


@pytest.fixture
def terminal(tmp_path: Path) -> Iterator[Callable[..., Terminal]]:
    """Start ``python -m agent_doubles.login_cli`` in a PTY with a minimal environment and a BROWSER capture helper
    (pattern of the legacy ``_write_capture_helper``); the captured URL lands in ``tmp_path/url.txt``."""
    started: list[Terminal] = []
    helper = tmp_path / "capture-url"
    # The pattern of the legacy _write_capture_helper (05-runner.md §11.2): write a partial file, then rename it.
    partial, target = tmp_path / "url.txt.partial", tmp_path / "url.txt"
    helper.write_text(f"#!/bin/sh\nprintf '%s' \"$1\" > {partial} && mv {partial} {target}\n")
    helper.chmod(0o700)

    def start(
        *args: str, browser: bool = True, extra_env: dict[str, str] | None = None, columns: int = 120
    ) -> Terminal:
        env = {k: v for k, v in os.environ.items() if k.startswith(("COVERAGE", "PATH", "LANG", "LC_", "SYSTEMROOT"))}
        env["PYTHONPATH"] = str(SRC)
        env["HOME"] = str(tmp_path)
        if browser:
            env["BROWSER"] = str(helper)
        env.update(extra_env or {})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, columns, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_doubles.login_cli", *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        term = Terminal(process, master, columns)
        started.append(term)
        return term

    yield start
    for term in started:
        if term.process.poll() is None:
            term.process.kill()
            term.process.wait()
        os.close(term.master)
