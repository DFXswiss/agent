"""Mailbox automation of the Claude subscription login (05-runner.md section 11.3, ADR-011, ADR-098).

Instead of relaying the authorize URL to the user, the runner drives the provider's pages itself in the login's own
browser sandbox (`agent_runner.login.sandbox`: a login VM, or a browser process) and reads the sign-in mail from the
subscription's mailbox. Every page is recognised by its structure (`agent_runner.login.pages`, never by the
double's markers) and handled once, then the automation waits for a different page (`page_timeout` per step):

- `challenge` - a check for humans: handed to the owner (`human`, `HumanCheck` of `requests.py`); it returns when the
  check is gone, else the automation gives up (`human_check`). Never circumvented;
- `email` - the address is typed and the continue button pressed - progress `email_entered`;
- `check_email` - the mailbox is polled every `mail_poll` seconds until `mail_wait` (INBOX and the spam folder, sender
  filter): the sign-in link is opened (progress `magic_link_received`), else - when the page has a code input - the
  code of the mail is typed (`mail_code_received`);
- `register` - a new account: the name and date of birth of `plan.account` are filled, only the boxes whose consent
  the owner gave are ticked (`account_missing`, `consent_missing` otherwise) - progress `registered`;
- `authorize` - the allow button; `code` - progress `authorized`, the code is read;
- `error`, or no known page in time - `unknown_page`.

Every failure raises `AutomationFailed` with the reason the response names (`REASONS`); the caller falls back to the
relay. The magic link, the codes, the account data and the mailbox password are never logged.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agent_cli.account_login import Mailbox, newest_magic_link
from agent_cli.store import StoreError
from agent_runner.login import mailcode
from agent_runner.login.pages import CLASSIFY, Page, parse
from agent_runner.login.relay import LoginOptions
from agent_runner.login.sandbox import Sandbox, SandboxError

CODE_RE = re.compile(r"^[!-~]{1,512}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
EMAIL_RE = re.compile(r"^[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$")
"""Host and address patterns of `contracts/credentials/mailbox.json`."""
MAX_EMAIL = 254
MAGIC_PATH = "/magic-link#"
MAGIC_TOKEN = r"[A-Za-z0-9_:=+/.~%-]+"
REASONS = frozenset(
    {
        "disabled",
        "no_sandbox",
        "human_check",
        "consent_missing",
        "account_missing",
        "unknown_page",
        "mail_timeout",
        "browser_failed",
    }
)
"""The `reason` values of the fall-back response (`contracts/requests/claude.login.response.json`)."""
MAX_STEPS = 16
TRANSIENT = frozenset({"unknown"})

Progress = Callable[[str], Awaitable[None]]
Human = Callable[[Sandbox, Page], Awaitable[None]]


class AutomationFailed(Exception):
    """The automation cannot go on (`reason`, a short word for the log; never a secret)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def answer(self) -> str:
        """The reason as the response names it."""
        return self.reason if self.reason in REASONS else "browser_failed"


@dataclass(frozen=True)
class Plan:
    """What an automatic login signs in with: the mailbox, the address and the data of a new account."""

    mailbox: Mailbox
    email: str
    account: Mapping[str, Any] | None = None


def mailbox_fields(body: Mapping[str, Any]) -> tuple[Mailbox, str] | None:
    """The IMAP mailbox and the address of a mailbox body (`credentials/mailbox.json`); None when malformed."""
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


def target(mark: str) -> str:
    return f"[data-agent-login='{mark}']"


async def classify(sandbox: Sandbox) -> Page:
    """The current page; a page the sandbox cannot read now (navigating) is `unknown`."""
    try:
        return parse(await sandbox.evaluate(CLASSIFY))
    except SandboxError:
        return parse("")


class Driver:
    def __init__(
        self,
        sandbox: Sandbox,
        plan: Plan,
        *,
        url: str,
        opts: LoginOptions,
        progress: Progress,
        human: Human,
        end: float,
        clock: Callable[[], float],
    ) -> None:
        self.sandbox = sandbox
        self.plan = plan
        self.url = url
        self.opts = opts
        self.progress = progress
        self.human = human
        self.end = end
        self.clock = clock
        self.since: float | None = None
        self.loop = asyncio.get_running_loop()

    async def next_page(self, leave: str | None) -> Page:
        """The first page that is neither transient nor `leave`, within `page_timeout` (else `unknown_page`)."""
        deadline = min(self.loop.time() + self.opts.page_timeout, self.end)
        while True:
            page = await classify(self.sandbox)
            if page.kind not in TRANSIENT and page.kind != leave:
                return page
            if self.loop.time() >= deadline:
                raise AutomationFailed("unknown_page")
            await asyncio.sleep(self.opts.page_poll)

    async def run(self) -> str:
        await self.sandbox.goto(self.url)
        await self.progress("browser_started")
        page = await self.next_page(None)
        for _step in range(MAX_STEPS):
            if page.kind == "code":
                return await self.code(page)
            handler = {
                "challenge": self.challenge,
                "email": self.email,
                "check_email": self.check_email,
                "register": self.register,
                "authorize": self.authorize,
            }.get(page.kind)
            if handler is None:
                raise AutomationFailed("unknown_page")
            await handler(page)
            page = await self.next_page(page.kind)
        raise AutomationFailed("unknown_page")

    async def challenge(self, page: Page) -> None:
        await self.human(self.sandbox, page)

    async def email(self, page: Page) -> None:
        await self.sandbox.type(target("email"), self.plan.email)
        self.since = self.clock()
        await self.sandbox.click(target("continue"))
        await self.progress("email_entered")

    async def _mail(self, with_code: bool) -> tuple[str, str] | None:
        mailbox, opts = self.plan.mailbox, self.opts
        since = self.clock() if self.since is None else self.since
        try:
            link = await asyncio.to_thread(
                newest_magic_link,
                mailbox,
                since,
                pattern=magic_pattern(self.url),
                mail_from=opts.mail_from,
                imap_factory=opts.imap_factory,
            )
            if link is not None:
                return "link", link
            if with_code:
                code = await asyncio.to_thread(
                    mailcode.newest_code, mailbox, since, mail_from=opts.mail_from, imap_factory=opts.imap_factory
                )
                if code is not None:
                    return "code", code
        except (imaplib.IMAP4.error, OSError, StoreError):
            return None
        return None

    async def check_email(self, page: Page) -> None:
        with_code = "mail-code" in page.marks
        while True:
            found = await self._mail(with_code)
            if found is not None:
                break
            if self.loop.time() + self.opts.mail_poll > self.end:
                raise AutomationFailed("mail_timeout")
            await asyncio.sleep(self.opts.mail_poll)
        kind, value = found
        if kind == "link":
            await self.progress("magic_link_received")
            await self.sandbox.goto(value)
            return
        await self.progress("mail_code_received")
        await self.sandbox.type(target("mail-code"), value)
        if "submit" in page.marks:
            await self.sandbox.click(target("submit"))

    async def register(self, page: Page) -> None:
        account = self.plan.account
        if account is None:
            raise AutomationFailed("account_missing")
        birth = account.get("birth_date")
        parts = ("birth-day", "birth-month", "birth-year")
        needs_birth = "birth" in page.marks or any(part in page.marks for part in parts)
        if needs_birth and not isinstance(birth, str):
            raise AutomationFailed("account_missing")
        consents = set(account.get("consents") or ())
        boxes = [box for box in page.boxes if not box.checked and (box.kind in consents or box.required)]
        if any(box.kind not in consents for box in boxes):
            raise AutomationFailed("consent_missing")
        if "name" in page.marks:
            await self.sandbox.type(target("name"), str(account["name"]))
        if isinstance(birth, str):
            year, month, day = birth.split("-")
            if "birth" in page.marks:
                await self.sandbox.type(target("birth"), birth)
            for part, value in zip(parts, (day, month, year), strict=True):
                if part in page.marks:
                    await self.sandbox.type(target(part), value)
        for box in boxes:
            await self.sandbox.click(target(box.mark))
        await self.sandbox.click(target("submit"))
        await self.progress("registered")

    async def authorize(self, page: Page) -> None:
        await self.sandbox.click(target("authorize"))

    async def code(self, page: Page) -> str:
        await self.progress("authorized")
        if page.code is None or not CODE_RE.fullmatch(page.code):
            raise AutomationFailed("code_form")
        return page.code


async def authorize(
    url: str,
    plan: Plan,
    *,
    sandbox: Sandbox,
    opts: LoginOptions,
    progress: Progress,
    human: Human,
    budget: float,
    clock: Callable[[], float],
) -> str:
    """Drive the pages in `sandbox` (module docstring); the authorization code. `budget` bounds the whole run in
    seconds, `clock` is the wall clock the mail's date is compared with."""
    end = asyncio.get_running_loop().time() + min(budget, opts.mail_wait)
    driver = Driver(sandbox, plan, url=url, opts=opts, progress=progress, human=human, end=end, clock=clock)
    try:
        return await driver.run()
    except SandboxError as error:
        raise AutomationFailed(error.reason) from None
