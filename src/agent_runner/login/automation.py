"""Mailbox automation of the Claude subscription login (05-runner.md section 11.3, ADR-011).

Instead of relaying the authorize URL to the user, the runner drives the authorize pages itself with headless Chromium
(Playwright for Python, extra `agent-runner[login]`) and reads the magic link from the subscription's mailbox:

1. open the authorize URL (page `email`), enter the address of the owner's `mailbox` credential, continue (page
   `check-email`) - progress `browser_started`, `email_entered`;
2. poll IMAP with the legacy `agent_cli.account_login.newest_magic_link` (INBOX and the spam folder, sender filter,
   at most 1 MiB per message, newest first) every `mail_poll` seconds until `mail_wait` (both from `LoginOptions`;
   5 s and 10 min) - progress `magic_link_received`; the `Mailbox` value is built from the decrypted credential;
3. open the magic link (it signs the browser in and returns to the authorize page `authorize`), click "Authorize"
   (page `code`) - progress `authorized` - and read the code (`#authorization-code`).

Every other outcome - the extra is not installed, a page that is not the expected one within `page_timeout` (a
human-verification page, an error page), a browser error, no mail in time, a code of the wrong form - raises
`AutomationFailed`; the caller falls back to the relay (response `{step:"authorize", url, automation:"failed"}`).

The pages are recognised by `body[data-page=…]` and element IDs, the markers of the auth-page double (DBL-03): the
automation is only ever exercised hermetically (no live login, PROJECT.md). Against pages without these markers it
fails over to the relay at the first step.

Trust: when `SSL_CERT_FILE` is set (a private CA, as for the IMAP poll of the legacy code), the certificate of the
authorize origin is verified against it with Python's TLS and Chromium is told to accept exactly that server key
(`--ignore-certificate-errors-spki-list`); otherwise Chromium's own trust store applies. The magic link, the code and
the mailbox password are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import imaplib
import importlib
import os
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_cli.account_login import Mailbox, newest_magic_link
from agent_cli.store import StoreError
from agent_runner.login.relay import LoginOptions
from agent_runner.requests.service import RequestClosed

CODE_RE = re.compile(r"^[!-~]{1,512}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
EMAIL_RE = re.compile(r"^[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$")
"""Host and address patterns of `contracts/credentials/mailbox.json` (the address also ends up in the stored
`claude_subscription` and in the `done` response, whose contracts use the same pattern)."""
MAX_EMAIL = 254
BROWSERS_DIR = "ms-playwright"
"""Chromium of the platform install profile, below the runner home (`profile.py`)."""
TLS_TIMEOUT = 10.0
MAGIC_PATH = "/magic-link#"
MAGIC_TOKEN = r"[A-Za-z0-9_:=+/.~%-]+"

Progress = Callable[[str], Awaitable[None]]


class AutomationFailed(Exception):
    """The automation cannot go on (`reason`, a short word for the log; never a secret)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def mailbox_fields(body: Mapping[str, Any]) -> tuple[Mailbox, str] | None:
    """The IMAP mailbox and the address of a decrypted `mailbox` credential; None for a malformed body."""
    names = ("imap_host", "imap_port", "imap_user", "imap_password", "email")
    host, port, user, password, email = (body.get(name) for name in names)
    if not (
        isinstance(host, str)
        and HOST_RE.fullmatch(host)
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
        and isinstance(user, str)
        and user
        and isinstance(password, str)
        and password
        and isinstance(email, str)
        and len(email) <= MAX_EMAIL
        and EMAIL_RE.fullmatch(email)
    ):
        return None
    return Mailbox(host=host, user=user, password=password, port=port), email


def magic_pattern(url: str) -> re.Pattern[str]:
    """The magic link of the authorize URL's origin (`<origin>/magic-link#<token>`)."""
    parts = urlsplit(url)
    return re.compile(re.escape(f"{parts.scheme}://{parts.netloc}{MAGIC_PATH}") + MAGIC_TOKEN)


def server_pin(url: str) -> str | None:
    """`base64(sha256(SPKI))` of the origin's certificate verified against `SSL_CERT_FILE`; None without that variable
    or for a plain-HTTP origin. Raises `ssl.SSLError`/`OSError` when the certificate does not verify."""
    parts = urlsplit(url)
    if not os.environ.get("SSL_CERT_FILE") or parts.scheme != "https":
        return None
    host = parts.hostname or ""
    context = ssl.create_default_context()
    with (
        socket.create_connection((host, parts.port or 443), timeout=TLS_TIMEOUT) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert der is not None
    key = x509.load_der_x509_certificate(der).public_key()
    spki = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def use_browsers(home: Path) -> None:
    """Point Playwright at the Chromium the platform profile installed below the runner home, unless the environment
    names a location already (the daemon runs as the runner account, not as the installing administrator)."""
    installed = home / BROWSERS_DIR
    if installed.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(installed))


def load_playwright(opts: LoginOptions) -> Callable[[], Any]:
    """`async_playwright` of the `login` extra; `AutomationFailed("extra_missing")` when it is not installed."""
    try:
        if opts.playwright is not None:
            return opts.playwright()
        starter: Callable[[], Any] = importlib.import_module("playwright.async_api").async_playwright
    except ImportError:
        raise AutomationFailed("extra_missing") from None
    return starter


async def magic_link(mailbox: Mailbox, since: float, pattern: re.Pattern[str], opts: LoginOptions, end: float) -> str:
    """Poll the mailbox until the magic link arrives; `AutomationFailed("mail_timeout")` at `end` (loop time)."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            link = await asyncio.to_thread(
                newest_magic_link,
                mailbox,
                since,
                pattern=pattern,
                mail_from=opts.mail_from,
                imap_factory=opts.imap_factory,
            )
        except (imaplib.IMAP4.error, OSError, StoreError):
            link = None
        if link is not None:
            return link
        if loop.time() + opts.mail_poll > end:
            raise AutomationFailed("mail_timeout")
        await asyncio.sleep(opts.mail_poll)


async def _expect(page: Any, kind: str, opts: LoginOptions) -> None:
    await page.wait_for_selector(f"body[data-page='{kind}']", timeout=opts.page_timeout * 1000)


async def authorize(
    url: str,
    mailbox: Mailbox,
    email: str,
    *,
    opts: LoginOptions,
    progress: Progress,
    budget: float,
    clock: Callable[[], float],
    profile_dir: Path,
) -> str:
    """Drive the authorize pages (see the module docstring); the authorization code. `budget` bounds the whole run
    in seconds, `clock` is the wall clock the mail's date is compared with, `profile_dir` (inside the login's temporary
    directory) holds the browser profile with the sign-in cookies, so it is removed with that directory."""
    starter = load_playwright(opts)
    loop = asyncio.get_running_loop()
    end = loop.time() + min(budget, opts.mail_wait)
    try:
        pin = await asyncio.to_thread(server_pin, url)
        args = [] if pin is None else [f"--ignore-certificate-errors-spki-list={pin}"]
        async with starter() as playwright:
            browser = await playwright.chromium.launch_persistent_context(str(profile_dir), headless=True, args=args)
            try:
                page = await browser.new_page()
                await progress("browser_started")
                await page.goto(url, timeout=opts.page_timeout * 1000)
                await _expect(page, "email", opts)
                since = clock()
                await page.fill("#email", email)
                await page.click("#continue")
                await _expect(page, "check-email", opts)
                await progress("email_entered")
                link = await magic_link(mailbox, since, magic_pattern(url), opts, end)
                await progress("magic_link_received")
                await page.goto(link, timeout=opts.page_timeout * 1000)
                await _expect(page, "authorize", opts)
                await page.click("#authorize")
                await _expect(page, "code", opts)
                await progress("authorized")
                code = ((await page.text_content("#authorization-code")) or "").strip()
            finally:
                await browser.close()
    except (AutomationFailed, RequestClosed):
        raise
    except Exception as exc:
        raise AutomationFailed(type(exc).__name__) from None
    if not CODE_RE.fullmatch(code):
        raise AutomationFailed("code_form")
    return code
