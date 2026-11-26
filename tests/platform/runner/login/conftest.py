"""Fixtures of the RUN-13 suites (`agent_runner.login`): a migrated store, the mail double and the auth-page double
(DBL-03) on loopback with a shared runtime CA, the login-CLI double as `LOGIN_ARGV`, and a driver that runs one
`claude.login` (or check) request through the real request service and the hub kit of RUN-09.

Ports: the doubles bind ephemeral loopback ports chosen by the kernel (as the double suites do), so parallel packages
never collide (decisions/RUN-13.md)."""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import sys
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
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
from agent_runner.crypto import credential as credential_format
from agent_runner.crypto.encoding import parse_json
from agent_runner.login.automation import magic_pattern
from agent_runner.login.relay import LoginOptions
from agent_runner.requests.service import RequestService, service_for
from agent_runner.store.migrate import migrate

from ..credentials.kit import ALICE, Env, ck, kit_env, make_record, seal_request
from ..settle import settle as settle_tasks
from ..sync.fakehub import SCHEMAS

USER = "subscriber@example.test"
PASSWORD = "mailbox-password"  # not a secret: the mail double only compares it


class Forms(HTMLParser):
    """`data-page` of the body, hidden inputs and the authorization code of an auth page."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.page: str | None = None
        self.fields: dict[str, str] = {}
        self.code: str | None = None
        self._in_code = False
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "body":
            self.page = values.get("data-page")
        elif tag == "input" and values.get("type") == "hidden" and "value" in values:
            self.fields[values["name"]] = values["value"]
        elif tag == "code" and values.get("id") == "authorization-code":
            self._in_code = True

    def handle_data(self, data: str) -> None:
        if self._in_code:
            self.code, self._in_code = data, False


@dataclass
class Page:
    kind: str | None
    text: str
    forms: Forms


class Browser:
    """An HTTP-level browser of the user: TLS against the test CA, a cookie jar, redirects followed."""

    def __init__(self, ca: DoubleCA) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ca.client_context()),
        )

    def open(self, url: str, form: dict[str, str] | None = None) -> Page:
        data = None if form is None else urlencode(form).encode()
        with self.opener.open(url, data=data, timeout=10) as response:
            text = response.read().decode()
        forms = Forms(text)
        return Page(forms.page, text, forms)


@dataclass
class Login:
    """A started mail double and auth-page double that sends its mails to it."""

    mail: MailDouble
    auth: AuthPageDouble
    control: DoubleClient
    ca: DoubleCA


MAIL_FROM = "Claude <no-reply@mail.anthropic.com>"
"""The double sends from an address that matches the production sender filter `anthropic.com`."""
Json = dict[str, Any]


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture(scope="session")
def login_ca(tmp_path_factory: pytest.TempPathFactory) -> DoubleCA:
    return DoubleCA.create(tmp_path_factory.mktemp("run13-ca"))


@pytest.fixture
def doubles(login_ca: DoubleCA, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Login]]:
    """Start a mail double (account `USER`) and an auth-page double sending its magic links there; the legacy IMAP
    poll and the SPKI pin trust the runtime CA through `SSL_CERT_FILE`."""
    monkeypatch.setenv("SSL_CERT_FILE", str(login_ca.ca_file))
    started: list[Any] = []

    def start(scenario: Scenario | None = None) -> Login:
        mail = MailDouble(ca=login_ca).start()
        started.append(mail)
        mail.add_account(USER, PASSWORD)
        assert mail.smtp is not None
        auth = AuthPageDouble(
            ca=login_ca, smtp=(mail.smtp.host, mail.smtp.port), scenario=scenario, mail_from=MAIL_FROM
        ).start()
        started.append(auth)
        return Login(mail, auth, DoubleClient(auth.url, timeout=10), login_ca)

    yield start
    for double in reversed(started):
        double.stop()


def cli_argv(login: Login, *extra: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "agent_doubles.login_cli",
        "setup-token",
        "--auth-url",
        login.auth.page_url,
        "--ca-file",
        str(login.ca.ca_file),
        *extra,
    )


def full_argv(login: Login, *extra: str) -> tuple[str, ...]:
    """The login-CLI double as the full login (`auth login`, KOR-71)."""
    return (
        sys.executable,
        "-m",
        "agent_doubles.login_cli",
        "auth",
        "login",
        "--claudeai",
        "--auth-url",
        login.auth.page_url,
        "--ca-file",
        str(login.ca.ca_file),
        *extra,
    )


def script_argv(source: str, *args: str) -> tuple[str, ...]:
    """A Python one-off as the login command (stands for a misbehaving CLI)."""
    return (sys.executable, "-c", source, *args)


def mailbox_body(login: Login, password: str = PASSWORD) -> Json:
    assert login.mail.imaps is not None
    return {
        "imap_host": "127.0.0.1",
        "imap_port": login.mail.imaps.port,
        "imap_user": USER,
        "imap_password": password,
        "email": USER,
    }


def code_for(login: Login, url: str) -> str:
    """The `CODE#STATE` a user copies after authorizing `url` in their own browser (HTTP-level walk: e-mail, magic
    link read with the legacy poll, Authorize)."""
    assert login.mail.imaps is not None
    browser = Browser(login.ca)
    since = login.auth.clock()
    page = browser.open(url)
    assert page.kind == "email", page.text
    page = browser.open(login.auth.page_url + "/login", {"email": USER, **page.forms.fields})
    assert page.kind == "check-email", page.text
    box = Mailbox("127.0.0.1", USER, PASSWORD, login.mail.imaps.port)
    link = newest_magic_link(box, since, pattern=magic_pattern(url), mail_from="anthropic.com")
    assert link is not None
    page = browser.open(login.auth.page_url + "/magic-link/verify", {"token": link.split("#", 1)[1]})
    assert page.kind == "authorize", page.text
    page = browser.open(login.auth.page_url + "/oauth/authorize", {**page.forms.fields, "decision": "authorize"})
    assert page.kind == "code", page.text
    assert page.forms.code is not None
    return page.forms.code


FAST = LoginOptions(url_wait=20.0, prompt_wait=20.0, token_wait=20.0, page_timeout=10.0, mail_poll=0.2, mail_wait=20.0)


@dataclass
class Run:
    """One request run: the env, the service and the root request ID."""

    env: Env
    service: RequestService
    root: str
    tmp_dir: Path
    seen: list[Json] = field(default_factory=list)

    def bodies(self, request_id: str | None = None) -> list[Json]:
        return self.env.hub.bodies(request_id or self.root)

    def finals(self, request_id: str) -> list[bool]:
        return [bool(h["final"]) for h, _b in self.env.hub.responses(request_id)]

    async def wait_bodies(self, count: int, request_id: str | None = None, seconds: float = 30.0) -> list[Json]:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while len(self.bodies(request_id)) < count:
            assert loop.time() < end, self.bodies(request_id)
            await asyncio.sleep(0.05)
        return self.bodies(request_id)

    async def follow(self, body: Json, *, user: str = "alice") -> str:
        follow_id = self.env.hub.add_request(seal_request("claude.login", body, user=user, follows=self.root))
        await self.service.process(follow_id)
        return follow_id

    async def settle(self) -> None:
        await settle_tasks(lambda: [f.task for f in self.service.flows.values()], "the request flows")

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.tmp_dir.iterdir()) if self.tmp_dir.exists() else []

    def secrets_absent(self, *secrets: str) -> None:
        """No secret in the runner log, the responses (opened as the user) or the audit lines."""
        texts = [
            self.env.log(),
            json.dumps(self.bodies()),
            *(json.dumps(self.bodies(r)) for r in self.env.hub.stored_requests),
        ]
        for secret in secrets:
            assert secret
            for text in texts:
                assert secret not in text


Check = Callable[[Run], Awaitable[None]]


def run_request(
    dsn: str,
    tmp_path: Path,
    kind: str,
    body: Json,
    check: Check,
    *,
    argv: tuple[str, ...] = ("claude", "setup-token"),
    automation: bool = False,
    opts: LoginOptions = FAST,
    ck: bool = True,
    records: dict[str, Json] | None = None,
    config: dict[str, Any] | None = None,
    env_path: str = "",
) -> None:
    """Run one root request of `kind` with `body` through the service; `check(run)` drives follow-ups and asserts."""

    async def main() -> None:
        async with kit_env(dsn) as env:
            home = tmp_path / "runner-home"
            home.mkdir(mode=0o700, exist_ok=True)
            cfg = env.context.config
            cfg.login_argv = argv
            cfg.login_automation = automation
            cfg.tmp_dir = home / "tmp"
            cfg.home = home
            cfg.model_upstream_anthropic = "http://127.0.0.1:9"
            cfg.model_catalog = None
            cfg.login_sandbox = "process"
            for name, value in (config or {}).items():
                setattr(cfg, name, value)
            env.context.host.env = {"PATH": env_path}
            env.context.login_options = opts
            if ck:
                await env.add_ck()
            for kind_name, record_body in (records or {}).items():
                env.hub.records[(ALICE, kind_name)] = make_record(record_body, kind=kind_name)
            service = service_for(env.context)
            assert service is not None
            root = env.hub.add_request(seal_request(kind, body))
            await service.process(root)
            run = Run(env=env, service=service, root=root, tmp_dir=cfg.tmp_dir)
            try:
                await check(run)
            finally:
                await service.shutdown()
            assert run.leftovers() == []
            contract_violations(env)

    asyncio.run(main())


def contract_violations(env: Env) -> None:
    """Every response body fits `contracts/requests/<kind>.response.json` and a subscription stored by the device its
    credential schema."""
    problems: list[object] = []
    for request_id, stored in env.hub.stored_requests.items():
        kind = stored.envelope["header"]["kind"]
        for body in env.hub.bodies(request_id):
            problems += SCHEMAS.errors(f"requests/{kind}.response.json", body)
    record = env.hub.records.get((ALICE, "claude_subscription"))
    device = env.context.identity.device.device_id
    if record is not None and credential_format.check_header(record["header"])["author"] == device:
        plain = credential_format.open_credential(ck(), record, env.hub.device_public)
        problems += SCHEMAS.errors("credentials/claude_subscription.json", parse_json(plain))
    assert problems == []
